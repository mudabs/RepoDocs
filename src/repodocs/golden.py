"""Golden-fixture quality evaluation (ADR-011, ADR-024).

A golden case is a reviewed description of what RepoDocs must find in a fixture:
nodes, relationships, evidence locations, reported limitations, cross-repository
candidates, diagrams, and the replayed response of a model. The harness runs the
deterministic pipeline on a copy of the fixture, scores the result against the case, and
emits one versioned JSON report.

Golden files are written from the fixture sources, never from the tool's output, and they
name facts by semantic selectors (type, repository, name) because identifiers and hashes
change with line endings and content. When the tool disagrees with a golden file, the
report lists `missing` and `unexpected` facts in the golden's own selector form; deciding
which side is wrong, and editing the golden by hand, is the review. There is deliberately
no command that rewrites a golden file from output.

This module measures RepoDocs on fixtures. A perfect score says nothing about
repositories the fixtures do not resemble, and the AI comparison replays a recorded
response rather than calling a model.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .ai.bundle import build_bundle
from .ai.claims import interpret, validate_claims
from .ai.evaluation import contribution
from .ai.providers import ProviderResponse
from .common import canonical_json, sha256_bytes
from .config import ROLE_DESTINATION, RepositorySpec, create_project
from .confirmations import apply_decisions
from .generator import generate_docs
from .ir import ir_hash
from .scanner import scan_project
from .validation import ER_ENTITY_START_PATTERN, ER_REL_PATTERN, validate_ir, validate_mermaid


CASE_VERSION = "1.0"
REPORT_VERSION = "1.0"
CASE_FILENAME = "golden.json"
MAX_LISTED = 25
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "golden-case-1.0.json"


class GoldenError(Exception):
    """A golden case is malformed or ambiguous, so no score can be trusted."""


@dataclass
class Observed:
    """What the pipeline produced for one case."""

    ir: dict[str, Any]
    artifacts: dict[str, str]
    repositories: dict[str, Path]


# --------------------------------------------------------------------------- loading
def _validate_schema(case: dict[str, Any], path: Path) -> None:
    """Check a case against the published schema, so a misspelled key cannot silently drop a constraint.

    The published schema is the contract, and the `jsonschema` package is the established way to apply it.
    Neither is optional: if either is missing the evaluation stops instead of skipping the check.
    """

    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise GoldenError(
            'Validating golden cases needs the jsonschema package; install it with: pip install "repodocs[test]"'
        ) from exc
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoldenError(
            f"The published schema {SCHEMA_PATH} is unavailable ({exc}); run the evaluation from a source checkout"
        ) from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(case), key=lambda error: [str(part) for part in error.absolute_path])
    if errors:
        shown = "; ".join(f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}" for error in errors[:5])
        raise GoldenError(f"Golden case {path} does not match schemas/golden-case-1.0.json: {shown}")


def _selectors(case: dict[str, Any]):
    """Every selector in a case with where it came from."""

    for item in case.get("nodes", []):
        yield item
    for item in case.get("relationships", []):
        yield item["source"]
        yield item["target"]
    for item in case.get("evidence", []):
        yield item["subject"]
    for group in ("expected", "absent"):
        for item in case.get("candidates", {}).get(group, []):
            yield item["request"]
            if item.get("endpoint"):
                yield item["endpoint"]


def _check_consistency(case: dict[str, Any], path: Path) -> None:
    ids = {item["id"] for item in case["repositories"]}
    if len(ids) != len(case["repositories"]):
        raise GoldenError(f"Golden case {path} lists a repository id twice")
    for selector in _selectors(case):
        if "repository" in selector and selector["repository"] not in ids:
            raise GoldenError(f"Golden case {path} names unknown repository {selector['repository']!r} in a selector")
    for item in case.get("limitations", []):
        if item["repository"] not in ids:
            raise GoldenError(f"Golden case {path} names unknown repository {item['repository']!r} in a limitation")

    def key(value: Any) -> str:
        return json.dumps(value, sort_keys=True)

    groups = {
        "expected nodes": case.get("nodes", []),
        "expected relationships": case.get("relationships", []),
        "expected evidence": case.get("evidence", []),
        "expected limitations": case.get("limitations", []),
        "expected candidates": case.get("candidates", {}).get("expected", []),
        "absent candidates": case.get("candidates", {}).get("absent", []),
    }
    for label, items in groups.items():
        keys = [key({k: v for k, v in item.items() if k != "confidence"}) for item in items]
        if len(keys) != len(set(keys)):
            raise GoldenError(f"Golden case {path} has duplicate {label}; each fact is listed once")
    expected = [(key(item["request"]), key(item["endpoint"])) for item in groups["expected candidates"]]
    for item in groups["absent candidates"]:
        request, endpoint = key(item["request"]), None if item.get("endpoint") is None else key(item["endpoint"])
        if any(r == request and (endpoint is None or e == endpoint) for r, e in expected):
            raise GoldenError(f"Golden case {path} lists a candidate as both expected and forbidden")


def load_case(path: Path) -> dict[str, Any]:
    try:
        case = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoldenError(f"Unable to read golden case {path}: {exc}") from exc
    if not isinstance(case, dict) or case.get("schema_version") != CASE_VERSION:
        raise GoldenError(f"Golden case {path} must be an object with schema_version {CASE_VERSION}")
    for key in ("name", "repositories"):
        if key not in case:
            raise GoldenError(f"Golden case {path} is missing {key!r}")
    _validate_schema(case, path)
    _check_consistency(case, path)
    case = copy.deepcopy(case)
    base = path.parent
    for repository in case["repositories"]:
        repository["resolved"] = (base / repository["path"]).resolve()
        if not repository["resolved"].is_dir():
            raise GoldenError(f"Golden case {case['name']}: repository path is not a directory: {repository['path']}")
    if "ai" in case:
        ai_path = (base / case["ai"]["claims"]).resolve()
        try:
            case["ai"]["labels"] = json.loads(ai_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GoldenError(f"Golden case {case['name']}: unreadable AI claims file {case['ai']['claims']}: {exc}") from exc
    return case


# --------------------------------------------------------------------------- pipeline
def run_pipeline(case: dict[str, Any], workdir: Path) -> Observed:
    """Scan and generate on a copy of the fixture, in the same way `scan` and `generate` do."""

    root = workdir / "project"
    root.mkdir()
    config = create_project(root, case["name"])
    repositories: dict[str, Path] = {}
    for repository in sorted(case["repositories"], key=lambda item: item["id"]):
        target = root / "sources" / repository["id"]
        shutil.copytree(repository["resolved"], target)
        repositories[repository["id"]] = target
        config.repositories.append(RepositorySpec(id=repository["id"], path=f"sources/{repository['id']}"))
    (root / "docs-out").mkdir()
    config.repositories.append(RepositorySpec(id="docs", path="docs-out", role=ROLE_DESTINATION))
    config.set_destination("docs")
    ir, _ = scan_project(config)
    apply_decisions(ir, {})
    validate_ir(ir)
    generate_docs(config, ir)
    output = config.output_path
    artifacts = {
        path.relative_to(output).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    return Observed(ir=ir, artifacts=artifacts, repositories=repositories)


# --------------------------------------------------------------------------- selectors
def _repository_of(node: dict[str, Any]) -> str | None:
    return node.get("attributes", {}).get("repository_id")


def selector_of(node: dict[str, Any]) -> dict[str, Any]:
    selector: dict[str, Any] = {"type": node["type"]}
    if _repository_of(node):
        selector["repository"] = _repository_of(node)
    selector["name"] = node["name"]
    return selector


def find(selector: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nodes matched by a selector. Zero means missing; more than one is an error in the golden."""

    found = [
        node
        for node in nodes
        if node["type"] == selector["type"]
        and node["name"] == selector["name"]
        and ("repository" not in selector or _repository_of(node) == selector["repository"])
        and all(node.get("attributes", {}).get(key) == value for key, value in selector.get("attributes", {}).items())
    ]
    if len(found) > 1:
        raise GoldenError(f"Selector {json.dumps(selector, sort_keys=True)} matches {len(found)} nodes; add attributes to disambiguate")
    return found


def _ratio(numerator: int, denominator: int) -> float | None:
    """A zero denominator is not 1.0: the metric does not apply."""

    return round(numerator / denominator, 4) if denominator else None


def _listed(items: list[Any]) -> list[Any]:
    ordered = sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return ordered[:MAX_LISTED]


def _score_sets(expected: int, observed: int, matched: int, missing: list[Any], unexpected: list[Any]) -> dict[str, Any]:
    return {
        "expected": expected,
        "observed": observed,
        "matched": matched,
        "precision": _ratio(matched, observed),
        "recall": _ratio(matched, expected),
        "missing": _listed(missing),
        "unexpected": _listed(unexpected),
    }


# --------------------------------------------------------------------------- scoring
def score_nodes(case: dict[str, Any], ir: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("nodes", [])
    closed = {item["type"] for item in expected} | set(case.get("closed", {}).get("node_types", []))
    matched: set[str] = set()
    missing: list[dict[str, Any]] = []
    for selector in expected:
        found = find(selector, ir["nodes"])
        if not found:
            missing.append(selector)
        elif found[0]["id"] in matched:
            raise GoldenError(f"Selectors overlap on node {json.dumps(selector_of(found[0]), sort_keys=True)}")
        else:
            matched.add(found[0]["id"])
    observed = [node for node in ir["nodes"] if node["type"] in closed]
    unexpected = [selector_of(node) for node in observed if node["id"] not in matched]
    return _score_sets(len(expected), len(observed), len(matched), missing, unexpected)


def score_relationships(case: dict[str, Any], ir: dict[str, Any]) -> dict[str, Any]:
    nodes = {node["id"]: node for node in ir["nodes"]}
    expected = case.get("relationships", [])
    closed = {item["type"] for item in expected} | set(case.get("closed", {}).get("relationship_types", []))
    wanted: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    for item in expected:
        source, target = find(item["source"], ir["nodes"]), find(item["target"], ir["nodes"])
        if not source or not target:
            missing.append(item)
            continue
        key = (item["type"], source[0]["id"], target[0]["id"])
        if key in wanted:
            raise GoldenError(f"Two expected relationships resolve to the same {item['type']} edge; list it once")
        wanted[key] = item
    observed = [edge for edge in ir["relationships"] if edge["type"] in closed]
    matched: set[tuple[str, str, str]] = set()
    unexpected: list[dict[str, Any]] = []
    for edge in observed:
        key = (edge["type"], edge["source"], edge["target"])
        item = wanted.get(key)
        duplicate = key in matched
        if item is not None and not duplicate and item.get("classification", edge["classification"]) == edge["classification"]:
            matched.add(key)
        else:
            entry = {
                "type": edge["type"],
                "source": selector_of(nodes[edge["source"]]),
                "target": selector_of(nodes[edge["target"]]),
                "classification": edge["classification"],
            }
            if duplicate:
                entry["duplicate"] = True  # a second edge with the same identity is never silently absorbed
            unexpected.append(entry)
    missing += [item for key, item in wanted.items() if key not in matched]
    return _score_sets(len(expected), len(observed), len(matched), missing, unexpected)


def _spans(ir: dict[str, Any], evidence_ids: list[str]) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in ir["evidence"]}
    return [by_id[key] for key in evidence_ids if key in by_id]


def _covers(record: dict[str, Any], path: str, line: int | None, repository: str | None = None) -> bool:
    """Whether evidence is in the named file, and the named repository when one is given.

    A path alone is not enough: the same relative path, such as openapi.json, exists in several repositories.
    """

    if repository is not None and record["repository_id"] != repository:
        return False
    if record["path"] != path:
        return False
    if line is None:
        return True
    return record.get("kind") == "span" and record["line_start"] <= line <= record["line_end"]


def score_evidence(case: dict[str, Any], ir: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("evidence", [])
    missing = []
    for item in expected:
        found = find(item["subject"], ir["nodes"])
        records = _spans(ir, found[0]["evidence_ids"]) if found else []
        repository = (_repository_of(found[0]) if found else None) or item["subject"].get("repository")
        if not any(_covers(record, item["path"], item.get("line"), repository) for record in records):
            missing.append(item)
    return {
        "expected": len(expected),
        "matched": len(expected) - len(missing),
        "recall": _ratio(len(expected) - len(missing), len(expected)),
        "missing": _listed(missing),
    }


def score_limitations(case: dict[str, Any], ir: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("limitations", [])
    observed = list(ir.get("limitations", []))
    unmatched = list(observed)
    missing: list[dict[str, Any]] = []
    matched = 0
    for item in expected:
        for candidate in unmatched:
            if (
                candidate["repository_id"] == item["repository"]
                and candidate["kind"] == item["kind"]
                and any(
                    _covers(record, item["path"], item.get("line"), item["repository"])
                    for record in _spans(ir, candidate["evidence_ids"])
                )
            ):
                unmatched.remove(candidate)
                matched += 1
                break
        else:
            missing.append(item)
    closed = case.get("closed", {}).get("limitations", False)

    def describe(limitation: dict[str, Any]) -> dict[str, Any]:
        record = next(iter(_spans(ir, limitation["evidence_ids"])), {})
        entry = {"repository": limitation["repository_id"], "kind": limitation["kind"], "path": record.get("path")}
        if record.get("line_start") is not None:
            entry["line"] = record["line_start"]
        return entry

    result = _score_sets(len(expected), len(observed), matched, missing, [describe(item) for item in unmatched])
    if not closed:
        result["precision"], result["unexpected"] = None, []
    return result


def _candidate_ref(candidate: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"request": selector_of(nodes[candidate["source"]]), "endpoint": selector_of(nodes[candidate["target"]])}


def score_candidates(case: dict[str, Any], ir: dict[str, Any]) -> dict[str, Any]:
    nodes = {node["id"]: node for node in ir["nodes"]}
    observed = list(ir.get("candidates", []))
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for item in observed:  # a repeated pair is reported, not absorbed into the first one
        if (item["source"], item["target"]) in by_pair:
            duplicates.append(item)
        else:
            by_pair[(item["source"], item["target"])] = item
    repository_of = {node["id"]: _repository_of(node) for node in ir["nodes"]}
    result: dict[str, Any] = {
        "observed": len(observed),
        "non_authoritative_violations": sum(
            1 for item in observed if item["classification"] != "inferred" or item["confidence"] >= 1
        ),
        "cross_repository_relationships": sum(
            1
            for edge in ir["relationships"]
            if repository_of.get(edge["source"]) and repository_of.get(edge["target"])
            and repository_of[edge["source"]] != repository_of[edge["target"]]
        ),
    }
    spec = case.get("candidates")
    if spec is None:
        return {**result, "expected": None, "matched": None, "precision": None, "recall": None, "accuracy": None,
                "missing": [], "unexpected": [], "forbidden_present": [], "confidence_mismatches": [],
                "unresolved_absent": []}
    matched: set[tuple[str, str]] = set()
    missing: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for item in spec.get("expected", []):
        request, endpoint = find(item["request"], ir["nodes"]), find(item["endpoint"], ir["nodes"])
        pair = (request[0]["id"], endpoint[0]["id"]) if request and endpoint else None
        if pair is None or pair not in by_pair:
            missing.append({"request": item["request"], "endpoint": item["endpoint"]})
            continue
        if pair in matched:
            raise GoldenError("Two expected candidates resolve to the same request and endpoint; list the pair once")
        matched.add(pair)
        if "confidence" in item and by_pair[pair]["confidence"] != item["confidence"]:
            mismatches.append({**_candidate_ref(by_pair[pair], nodes), "expected": item["confidence"], "observed": by_pair[pair]["confidence"]})
    unexpected = [_candidate_ref(item, nodes) for item in by_pair.values() if (item["source"], item["target"]) not in matched]
    unexpected += [{**_candidate_ref(item, nodes), "duplicate": True} for item in duplicates]
    forbidden: list[dict[str, Any]] = []
    unresolved_absent: list[dict[str, Any]] = []
    for item in spec.get("absent", []):
        request = find(item["request"], ir["nodes"])
        target = find(item["endpoint"], ir["nodes"]) if item.get("endpoint") else None
        if not request or (item.get("endpoint") and not target):
            # A forbidden pair whose selector matches nothing checks nothing, so it is reported as a failure.
            unresolved_absent.append(item)
            continue
        for candidate in observed:
            if candidate["source"] == request[0]["id"] and (item.get("endpoint") is None or (target and candidate["target"] == target[0]["id"])):
                forbidden.append(_candidate_ref(candidate, nodes))
    expected_count = len(spec.get("expected", []))
    union = expected_count + len(observed) - len(matched)
    result.update(_score_sets(expected_count, len(observed), len(matched), missing, unexpected))
    result["accuracy"] = _ratio(len(matched), union)
    result["forbidden_present"] = _listed(forbidden)
    result["confidence_mismatches"] = _listed(mismatches)
    result["unresolved_absent"] = _listed(unresolved_absent)
    return result


def _inside(root: Path, relative: str) -> Path | None:
    """The file a recorded evidence path names, or None when it is not a plain path inside the repository.

    Evidence paths are relative and use forward slashes. An absolute path, a drive, a backslash, or a `..`
    segment could resolve to a real file outside the repository copy and make bad evidence look good.
    """

    posix, windows = PurePosixPath(relative), PureWindowsPath(relative)
    if not relative or "\\" in relative or posix.is_absolute() or windows.drive or windows.root:
        return None
    if ".." in posix.parts or posix == PurePosixPath("."):
        return None
    target = (root / posix).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def score_provenance(observed: Observed) -> dict[str, Any]:
    """Check evidence against the files on disk, not only against the IR's own bookkeeping."""

    ir = observed.ir
    snapshots = {item["repository_id"]: item for item in ir["snapshots"]}
    unresolved: list[dict[str, Any]] = []
    files: dict[Path, tuple[str, int] | None] = {}  # each file is read once, however many records cite it

    def file_facts(path: Path) -> tuple[str, int] | None:
        if path not in files:
            if path.is_file():
                data = path.read_bytes()
                files[path] = (sha256_bytes(data), len(data.decode("utf-8", errors="replace").splitlines()))
            else:
                files[path] = None
        return files[path]

    for record in ir["evidence"]:
        root = observed.repositories.get(record["repository_id"])
        problem = None
        if root is None or record["repository_id"] not in snapshots:
            problem = "unknown-repository"
        elif (target := _inside(root, record["path"])) is None:
            problem = "path-escapes-repository"
        else:
            facts = file_facts(target)
            snapshot = snapshots[record["repository_id"]]
            if facts is None:
                problem = "file-missing"
            elif facts[0] != record["content_hash"]:
                problem = "hash-mismatch"
            elif record["revision"] != snapshot["revision"]:
                problem = "revision-mismatch"
            elif record.get("revision_kind") != snapshot["revision_kind"]:
                problem = "revision-kind-mismatch"
            elif record["kind"] == "span" and not 1 <= record["line_start"] <= record["line_end"] <= facts[1]:
                problem = "span-out-of-range"
        if problem:
            unresolved.append({"repository": record["repository_id"], "path": record["path"], "reason": problem})
    known = {item["id"] for item in ir["evidence"]}
    references = [
        key
        for group in ("nodes", "relationships", "claims", "limitations", "candidates")
        for item in ir.get(group, [])
        for key in item.get("evidence_ids", [])
    ]
    resolved_references = sum(1 for key in references if key in known)
    total = len(ir["evidence"])
    # A node or limitation that names its repository may only cite that repository's evidence.
    # Candidates are exempt because they join two repositories by design.
    by_id = {item["id"]: item for item in ir["evidence"]}
    misbound: list[dict[str, Any]] = []
    for label, group in (("node", ir["nodes"]), ("limitation", ir.get("limitations", []))):
        for item in group:
            owner = _repository_of(item) if label == "node" else item.get("repository_id")
            for key in item.get("evidence_ids", []):
                record = by_id.get(key)
                if owner and record is not None and record["repository_id"] != owner:
                    misbound.append({"subject": label, "repository": owner, "evidence_repository": record["repository_id"], "path": record["path"]})
    return {
        "misbound_references": len(misbound),
        "misbound": _listed(misbound),
        "evidence_records": total,
        "evidence_resolved": total - len(unresolved),
        "resolution": _ratio(total - len(unresolved), total),
        "references": len(references),
        "references_resolved": resolved_references,
        "reference_resolution": _ratio(resolved_references, len(references)),
        "unresolved": _listed(unresolved),
    }


def score_claims(ir: dict[str, Any]) -> dict[str, Any]:
    """A deterministic claim is unsupported when it cites no evidence or evidence that does not exist."""

    known = {item["id"] for item in ir["evidence"]}
    unsupported = [
        item for item in ir["claims"] if not item["evidence_ids"] or any(key not in known for key in item["evidence_ids"])
    ]
    return {
        "deterministic": len(ir["claims"]),
        "unsupported": len(unsupported),
        "unsupported_rate": _ratio(len(unsupported), len(ir["claims"])),
    }


def score_diagrams(case: dict[str, Any], artifacts: dict[str, str]) -> dict[str, Any]:
    """Validity is checked with the built-in Python validator, not a Mermaid renderer."""

    spec = case.get("diagrams", {})
    diagrams = {name: text for name, text in artifacts.items() if name.endswith(".mmd")}
    invalid = []
    for name, text in sorted(diagrams.items()):
        try:
            validate_mermaid(text)
        except Exception as exc:  # the validator's own error is the finding
            invalid.append({"artifact": name, "reason": str(exc)[:200]})
    result: dict[str, Any] = {
        "expected": sorted(spec.get("artifacts", [])),
        "missing": sorted(set(spec.get("artifacts", [])) - set(diagrams)),
        "total": len(diagrams),
        "valid": len(diagrams) - len(invalid),
        "validity": _ratio(len(diagrams) - len(invalid), len(diagrams)),
        "invalid": invalid,
        "erd": None,
    }
    if "erd" in spec:
        lines = [line.strip() for line in artifacts.get("erd.mmd", "").splitlines() if line.strip() and not line.strip().startswith("%%")]
        entities = sum(1 for line in lines if ER_ENTITY_START_PATTERN.match(line))
        relationships = sum(1 for line in lines if ER_REL_PATTERN.match(line))
        result["erd"] = {
            "expected_entities": spec["erd"]["entities"],
            "entities": entities,
            "expected_relationships": spec["erd"]["relationships"],
            "relationships": relationships,
            "matches": entities == spec["erd"]["entities"] and relationships == spec["erd"]["relationships"],
        }
    return result


# --------------------------------------------------------------------------- AI replay
class _Recorded:
    """A provider that returns a recorded response, so the comparison needs no model or network."""

    name = "recorded"
    model = "golden-replay"
    external = False

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        return ProviderResponse(self.data, self.model)


def _raw_claims(labels: dict[str, Any], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    if not bundle["candidates"]:
        raise GoldenError("The AI comparison needs a case that produces cross-repository candidates")
    candidate = bundle["candidates"][0]
    nodes = {node["id"]: node for node in bundle["nodes"]}
    objects = {"endpoint": nodes[candidate["target"]], "candidate": candidate, "request": nodes[candidate["source"]]}

    def ids(kind: str, field: str, unknown: str) -> list[str]:
        if kind == "none":
            return []
        if kind == "unknown":
            return [unknown]
        return [objects[kind]["id"]] if field == "id" else list(objects[kind]["evidence_ids"])

    return [
        {
            "text": item["text"],
            "classification": item["classification"],
            "subject_ids": ids(item["subject"], "id", "node:missing"),
            "evidence_ids": ids(item["evidence"], "evidence_ids", "evidence:missing"),
        }
        for item in labels["claims"]
    ]


LABEL_KINDS = ("none", "unknown", "endpoint", "candidate", "request")
LABEL_OUTCOMES = ("inferred", "proposed", "rejected")
EXPECTED_METRICS = (
    "ai_inferred_claims", "ai_proposed_claims", "ai_rejected_claims", "unsupported_accepted", "reviewed_unsupported_inferred",
)


def _check_labels(labels: Any) -> None:
    """The reviewed-claims file has no published schema, so its shape is checked here and any gap is an error."""

    claims = labels.get("claims") if isinstance(labels, dict) else None
    if not isinstance(claims, list) or not claims:
        raise GoldenError("The AI labels need a non-empty claims list")
    for index, item in enumerate(claims):
        for key in ("text", "classification", "subject", "evidence", "expected", "supported"):
            if not isinstance(item, dict) or key not in item:
                raise GoldenError(f"AI label {index} is missing {key!r}")
        if not isinstance(item["text"], str) or not item["text"].strip():
            raise GoldenError(f"AI label {index} has no text")
        for key in ("subject", "evidence"):
            if item[key] not in LABEL_KINDS:
                raise GoldenError(f"AI label {index} has invalid {key} {item[key]!r}; expected one of {', '.join(LABEL_KINDS)}")
        if item["expected"] not in LABEL_OUTCOMES:
            raise GoldenError(f"AI label {index} has invalid expected outcome {item['expected']!r}")
        if not isinstance(item["supported"], bool):
            raise GoldenError(f"AI label {index} must say whether it is supported with true or false")
    expected = labels.get("expected")
    if not isinstance(expected, dict) or any(
        isinstance(expected.get(key), bool) or not isinstance(expected.get(key), int) for key in EXPECTED_METRICS
    ):
        raise GoldenError("The AI labels must state integer expected metrics for: " + ", ".join(EXPECTED_METRICS))


def score_ai(case: dict[str, Any], ir: dict[str, Any], deterministic: dict[str, Any]) -> dict[str, Any] | None:
    if "ai" not in case:
        return None
    labels = case["ai"]["labels"]
    _check_labels(labels)
    bundle = build_bundle(ir)
    raw = _raw_claims(labels, bundle)
    # Each label is judged on its own. Keying outcomes by claim text would let two different claims that share
    # wording stand in for each other and hide a wrong outcome.
    outcomes: list[str] = []
    signatures: dict[str, int] = {}
    for index, claim in enumerate(raw):
        signature = json.dumps([claim["text"], sorted(claim["subject_ids"]), sorted(claim["evidence_ids"])])
        if signature in signatures:
            raise GoldenError(f"AI labels {signatures[signature]} and {index} describe the same claim; list it once")
        signatures[signature] = index
        checked, _ = validate_claims({"claims": [claim]}, bundle)
        outcomes.append(checked[0]["classification"] if checked else "rejected")
    document = interpret(bundle, _Recorded({"claims": raw}))
    report = contribution(ir, document)
    reviewed_unsupported = sum(
        1 for label, outcome in zip(labels["claims"], outcomes) if not label["supported"] and outcome == "inferred"
    )
    wrong_outcome = [
        {"claim": label["text"], "expected": label["expected"], "observed": outcome}
        for label, outcome in zip(labels["claims"], outcomes)
        if outcome != label["expected"]
    ]
    expected = {**labels["expected"]}
    observed_counts = {**report, "reviewed_unsupported_inferred": reviewed_unsupported}
    mismatches = [
        {"metric": key, "expected": value, "observed": observed_counts.get(key)}
        for key, value in sorted(expected.items())
        if observed_counts.get(key) != value
    ]
    ai_unsupported = report["unsupported_accepted"] + reviewed_unsupported
    ai_total = len(document["accepted"])
    with_total = deterministic["deterministic"] + ai_total
    with_unsupported = deterministic["unsupported"] + ai_unsupported
    with_rate = _ratio(with_unsupported, with_total)
    baseline_rate = deterministic["unsupported_rate"]
    delta = None if with_rate is None or baseline_rate is None else round(with_rate - baseline_rate, 4)
    # The delta above is rounded for display. Whether AI increased the rate is decided on exact counts:
    # with_unsupported / with_total > unsupported / deterministic, compared by cross-multiplication.
    if deterministic["deterministic"] == 0:
        increases = with_unsupported > 0
    else:
        increases = with_unsupported * deterministic["deterministic"] > deterministic["unsupported"] * with_total
    return {
        "basis": "recorded-response-replay",
        "contribution": report,
        "reviewed_unsupported_inferred": reviewed_unsupported,
        "expectation_mismatches": mismatches,
        "wrong_outcomes": wrong_outcome,
        "comparison": {
            "deterministic": {"claims": deterministic["deterministic"], "unsupported": deterministic["unsupported"], "unsupported_rate": baseline_rate},
            "with_ai": {"claims": with_total, "unsupported": with_unsupported, "unsupported_rate": with_rate},
            "unsupported_rate_delta": delta,
            "increases_unsupported_rate": increases,
        },
    }


# --------------------------------------------------------------------------- reporting
def score_case(case: dict[str, Any], observed: Observed, stability: dict[str, bool]) -> dict[str, Any]:
    claims = score_claims(observed.ir)
    report: dict[str, Any] = {
        "name": case["name"],
        "nodes": score_nodes(case, observed.ir),
        "relationships": score_relationships(case, observed.ir),
        "evidence": score_evidence(case, observed.ir),
        "limitations": score_limitations(case, observed.ir),
        "candidates": score_candidates(case, observed.ir),
        "provenance": score_provenance(observed),
        "claims": claims,
        "diagrams": score_diagrams(case, observed.artifacts),
        "stability": stability,
        "ai": score_ai(case, observed.ir, claims),
    }
    report["failures"] = failures_of(report)
    return report


def failures_of(report: dict[str, Any]) -> list[str]:
    """Invariants that must hold on every case. Recall below 1 is tracked, not a failure."""

    failed = []
    provenance, diagrams, candidates, ai = report["provenance"], report["diagrams"], report["candidates"], report["ai"]
    # Decisions use exact counts. The ratios are rounded to four places for display, and one failure
    # among tens of thousands of records rounds to 1.0. Zero records or references is valid.
    if provenance["evidence_resolved"] != provenance["evidence_records"]:
        failed.append("evidence-does-not-resolve-to-files")
    if provenance["references_resolved"] != provenance["references"]:
        failed.append("evidence-reference-unresolved")
    if provenance["misbound_references"]:
        failed.append("evidence-bound-to-another-repository")
    if candidates["unresolved_absent"]:
        failed.append("absent-selector-unresolved")
    if report["claims"]["unsupported"]:
        failed.append("unsupported-deterministic-claim")
    if diagrams["missing"]:
        failed.append("expected-diagram-missing")
    if diagrams["invalid"]:
        failed.append("invalid-diagram")
    if diagrams["erd"] is not None and not diagrams["erd"]["matches"]:
        failed.append("erd-counts-differ")
    if not all(report["stability"].values()):
        failed.append("unstable-output")
    if candidates["non_authoritative_violations"]:
        failed.append("authoritative-candidate")
    if candidates["cross_repository_relationships"]:
        failed.append("observed-cross-repository-relationship")
    if candidates["forbidden_present"]:
        failed.append("forbidden-candidate-present")
    if ai is not None:
        if ai["comparison"]["increases_unsupported_rate"]:
            failed.append("ai-increases-unsupported-rate")
        if ai["expectation_mismatches"] or ai["wrong_outcomes"]:
            failed.append("ai-replay-differs-from-review")
        if ai["contribution"]["unsupported_accepted"]:
            failed.append("ai-unsupported-accepted")
    return failed


def evaluate_case(path: Path) -> dict[str, Any]:
    case = load_case(path)
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        observed = run_pipeline(case, Path(first))
        repeated = run_pipeline(case, Path(second))
        stability = {
            "ir_identical": ir_hash(observed.ir) == ir_hash(repeated.ir),
            "artifacts_identical": observed.artifacts == repeated.artifacts,
        }
        return score_case(case, observed, stability)


def evaluate_cases(directory: Path) -> dict[str, Any]:
    paths = sorted(directory.glob(f"*/{CASE_FILENAME}"))
    if not paths:
        raise GoldenError(f"No golden cases found under {directory}")
    cases = [evaluate_case(path) for path in paths]
    return {
        "report_version": REPORT_VERSION,
        "basis": "golden fixtures; scores describe these fixtures only",
        "cases": cases,
        "failures": sum(len(item["failures"]) for item in cases),
    }


def diff_reports(expected: Any, observed: Any, path: str = "") -> list[str]:
    """Where two reports differ, as dotted paths, so a changed baseline is easy to review."""

    if type(expected) is not type(observed):
        return [f"{path or '$'}: {expected!r} != {observed!r}"]
    if isinstance(expected, dict):
        keys = sorted(set(expected) | set(observed))
        return [line for key in keys for line in diff_reports(expected.get(key), observed.get(key), f"{path}.{key}" if path else key)]
    if isinstance(expected, list):
        if len(expected) != len(observed):
            return [f"{path}: length {len(expected)} != {len(observed)}"]
        return [line for index, (a, b) in enumerate(zip(expected, observed)) for line in diff_reports(a, b, f"{path}[{index}]")]
    return [] if expected == observed else [f"{path}: {expected!r} != {observed!r}"]


def _cell(value: Any) -> str:
    return "n/a" if value is None else f"{value:.4g}" if isinstance(value, float) else str(value)


def render_summary(report: dict[str, Any]) -> str:
    """A Markdown table for a CI step summary. The JSON report is the authoritative output."""

    lines = [
        "## Golden-fixture evaluation",
        "",
        "Scores describe the fixtures only. `n/a` means the metric has no denominator.",
        "",
        "| Case | Node P | Node R | Rel P | Rel R | Limit R | Cand acc | Provenance | Unsupported | Diagrams | Stable | AI delta | Failures |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for item in report["cases"]:
        ai = item["ai"]["comparison"]["unsupported_rate_delta"] if item["ai"] else None
        lines.append(
            f"| {item['name']} | {_cell(item['nodes']['precision'])} | {_cell(item['nodes']['recall'])} | "
            f"{_cell(item['relationships']['precision'])} | {_cell(item['relationships']['recall'])} | "
            f"{_cell(item['limitations']['recall'])} | {_cell(item['candidates']['accuracy'])} | "
            f"{_cell(item['provenance']['resolution'])} | {_cell(item['claims']['unsupported_rate'])} | "
            f"{_cell(item['diagrams']['validity'])} | {'yes' if all(item['stability'].values()) else 'NO'} | "
            f"{_cell(ai)} | {', '.join(item['failures']) or 'none'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m repodocs.golden", description="Evaluate RepoDocs against golden fixtures"
    )
    parser.add_argument("cases", type=Path, help="Directory holding <case>/golden.json files")
    parser.add_argument("--baseline", type=Path, help="Committed report the new report must equal exactly")
    parser.add_argument("--output", type=Path, help="Also write the JSON report to this file")
    parser.add_argument("--summary", action="store_true", help="Print a Markdown table instead of JSON")
    args = parser.parse_args(argv)
    try:
        report = evaluate_cases(args.cases)
        text = canonical_json(report)
        differences: list[str] = []
        if args.baseline:
            try:
                baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GoldenError(f"Unable to read baseline {args.baseline}: {exc}") from exc
            differences = diff_reports(baseline, report)
    except GoldenError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    if args.output:
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(render_summary(report) if args.summary else text, end="")
    for line in differences[:50]:
        print(f"baseline differs: {line}", file=sys.stderr)
    return 1 if report["failures"] or differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
