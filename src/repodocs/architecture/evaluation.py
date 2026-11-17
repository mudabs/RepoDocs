"""Scoring an architecture view against components and relationships written from source inspection (#54).

The expected set is written by a person reading the source, before any output is scored. This module
only compares; it never edits an expected file from output. Components are matched by the files they
claim, not by name, so a model may name things differently and still be credited. Structural validity
and a matching score are not proof of truth: every sampled relationship still needs a reviewer to open
its evidence, and ``review`` lists them with an empty verdict for that.

Run: ``python -m repodocs.architecture.evaluation --root PROJECT --expected EXPECTED.json [--summary]``
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

from ..ai.providers import ProviderResponse
from ..config import ProjectConfig
from ..validation import load_ir
from .context import is_denied
from .discovery import CONNECTION_KINDS, STATUS_CURRENT, load_view, saved_context_path
from .navigation import evidence_map, repository_remotes

REPORT_VERSION = "1.0"
STRUCTURE_TYPES = {"project", "repository", "language", "manifest", "dependency"}


class EvaluationInputError(ValueError):
    """The expected set or the scanned project cannot be scored, with what to fix."""


def check_expected(expected: Any) -> None:
    """Refuse an expected set that would make the scores meaningless, naming every problem found."""

    problems: list[str] = []
    if not isinstance(expected, dict):
        raise EvaluationInputError("The expected file must be a JSON object")
    source = expected.get("source")
    if not isinstance(expected.get("name"), str) or not expected["name"]:
        problems.append("`name` is missing")
    if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key] for key in ("repository", "revision", "license")):
        problems.append("`source` must give `repository`, `revision`, and `license`")
    components = expected.get("components")
    names: list[str] = []
    if not isinstance(components, list) or not components:
        problems.append("`components` must be a non-empty list")
        components = []
    for index, entry in enumerate(components):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            problems.append(f"components[{index}] needs a `name`")
            continue
        names.append(entry["name"])
        members = entry.get("members")
        if not isinstance(members, list) or not members or not all(isinstance(item, str) and item for item in members):
            problems.append(f"component {entry['name']!r} needs a non-empty list of `members` patterns")
    for name in sorted({name for name in names if names.count(name) > 1}):
        problems.append(f"component {name!r} is listed more than once")
    if components and not any(isinstance(item, dict) and item.get("required", True) and not item.get("deployment") for item in components):
        problems.append("at least one component must be required and not a deployment component")
    pairs: list[tuple[Any, Any]] = []
    for index, item in enumerate(expected.get("relationships", [])):
        if not isinstance(item, dict):
            problems.append(f"relationships[{index}] must be an object")
            continue
        pair = (item.get("source"), item.get("target"))
        pairs.append(pair)
        if pair[0] not in names or pair[1] not in names:
            problems.append(f"relationship {pair[0]!r} -> {pair[1]!r} names a component that is not listed")
        if pair[0] == pair[1]:
            problems.append(f"relationship {pair[0]!r} -> {pair[1]!r} connects a component to itself")
        kinds = item.get("kinds")
        if not isinstance(kinds, list) or not kinds or not set(kinds) <= set(CONNECTION_KINDS):
            problems.append(f"relationship {pair[0]!r} -> {pair[1]!r} needs `kinds` from {', '.join(CONNECTION_KINDS)}")
    for pair in sorted({pair for pair in pairs if pairs.count(pair) > 1}, key=str):
        problems.append(f"relationship {pair[0]!r} -> {pair[1]!r} is listed more than once; list the allowed kinds once")
    for item in expected.get("not_established", []):
        if isinstance(item, dict) and (item.get("source"), item.get("target")) in pairs:
            problems.append(f"{item.get('source')!r} -> {item.get('target')!r} is both expected and not established")
    if problems:
        raise EvaluationInputError("The expected set is not usable: " + "; ".join(problems))


def check_revision(ir: dict[str, Any], expected: dict[str, Any]) -> None:
    """The expected set describes one revision; the scored project must be scanned at exactly that revision.

    A working tree with uncommitted changes is a content fingerprint, never the commit the expected set
    was written from, so it is refused here too.
    """

    wanted = expected["source"]["revision"]
    scanned = [(item["repository_id"], item["revision_kind"], item["revision"]) for item in ir["snapshots"]]
    if not any(revision == wanted for _, _, revision in scanned):
        found = ", ".join(f"{name}: {kind} {revision[:12]}" for name, kind, revision in scanned) or "no snapshot"
        raise EvaluationInputError(
            f"The expected set was written from revision {wanted[:12]}, but this project is scanned at {found}. "
            "Check out that exact commit (a clean working tree) and run `repodocs scan` again"
        )


def _share(members: list[str], patterns: list[str]) -> float:
    inside = sum(any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns) for path in members)
    return inside / len(members) if members else 0.0


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def match_components(view: dict[str, Any], expected: dict[str, Any]) -> dict[str, str]:
    """View component ID to the expected component it stands for (at least half its files inside it)."""

    candidates = []
    for item in view["components"]:
        for entry in expected["components"]:
            share = _share(item["members"], entry["members"])
            if share >= 0.5:
                candidates.append((-share, item["name"], item["id"], entry["name"]))
    matched: dict[str, str] = {}
    taken: set[str] = set()
    for _, _, identity, name in sorted(candidates):
        if identity not in matched and name not in taken:
            matched[identity] = name
            taken.add(name)
    return matched


def evaluate(config: ProjectConfig, ir: dict[str, Any], view: dict[str, Any], context: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    check_expected(expected)
    check_revision(ir, expected)
    matched = match_components(view, expected)
    by_expected = {entry["name"]: entry for entry in expected["components"]}
    deployment = {name for name, entry in by_expected.items() if entry.get("deployment")}
    required = {name for name, entry in by_expected.items() if entry.get("required", True) and not entry.get("deployment")}
    names = {item["id"]: item["name"] for item in view["components"]}
    found = set(matched.values())

    extra = sorted(names[identity] for identity in names if identity not in matched)
    mixed = sorted(name for name in found if name in deployment)
    wanted = {(item["source"], item["target"]): item for item in expected["relationships"]}
    forbidden = {(item["source"], item["target"]) for item in expected.get("not_established", [])}
    categories: dict[str, list[dict[str, Any]]] = {"supported": [], "wrong-kind": [], "duplicate": [], "contradicted": [], "unexpected": [], "unmapped": []}
    # An expected relationship is recovered once: endpoints, direction, and an allowed kind must all match.
    # Any further connection between the same two components is a duplicate and adds no coverage.
    covered: set[tuple[str, str]] = set()
    for link in sorted(view["connections"], key=lambda item: item["id"]):
        source, target = matched.get(link["source"]), matched.get(link["target"])
        row = {"source": names[link["source"]], "target": names[link["target"]], "kind": link["kind"], "label": link["label"], "classification": link["classification"]}
        if source is None or target is None:
            categories["unmapped"].append(row)
        elif (source, target) in wanted:
            if link["kind"] not in wanted[(source, target)]["kinds"]:
                categories["wrong-kind"].append(row)
            elif (source, target) in covered:
                categories["duplicate"].append(row)
            else:
                covered.add((source, target))
                categories["supported"].append(row)
        elif (source, target) in forbidden or (target, source) in wanted:
            categories["contradicted"].append(row)
        else:
            categories["unexpected"].append(row)
    expected_required = {key for key, item in wanted.items() if not item.get("deployment")}

    documents = {item["id"]: item for item in context["documents"]}
    files = {entry["repository_id"]: {item["path"]: item["content_hash"] for item in entry["files"]} for entry in ir["snapshots"]}
    cited = {key for item in view["components"] + view["connections"] for key in item["evidence"]}
    resolves = [key for key in cited if key in documents and files.get(documents[key]["repository_id"], {}).get(documents[key]["path"]) == documents[key]["content_hash"]]
    members = [(item["repository_id"], path) for item in view["components"] for path in item["members"]]
    existing = [pair for pair in members if pair[1] in files.get(pair[0], {}) and not is_denied(pair[1])]
    placed = evidence_map(view, context, ir, repository_remotes(config, ir))
    items = [entry for group in placed["components"].values() for entry in group["evidence"]] + [entry for group in placed["connections"].values() for entry in group["evidence"]]
    semantic = [node for node in ir["nodes"] if node["type"] not in STRUCTURE_TYPES]
    semantic_paths = {entry["path"] for node in semantic for entry in ir["evidence"] if entry["id"] in node.get("evidence_ids", [])}
    baseline_covered = sorted(name for name, entry in by_expected.items() if not entry.get("deployment") and any(any(fnmatch.fnmatchcase(path, pattern) for pattern in entry["members"]) for path in semantic_paths))
    total_links = sum(len(group) for group in categories.values())

    return {
        "report_version": REPORT_VERSION,
        "name": expected["name"],
        "source": expected["source"],
        "view": {"status": view["status"], "provider": view["provenance"]["provider"], "requested_model": view["provenance"]["requested_model"],
                 "served_model": view["provenance"]["served_model"], "rounds": view["provenance"]["rounds"], "requests": view["provenance"]["requests"],
                 "context_bytes": context["usage"]["context_bytes"], "external": view["provenance"]["external"]},
        "components": {
            "proposed": len(view["components"]), "matched": len(matched), "required_expected": len(required), "required_found": len(required & found),
            "recall": _ratio(len(required & found), len(required)), "precision": _ratio(len(matched) - len(mixed), len(view["components"])),
            "extra": extra, "missing": sorted(required - found), "deployment_shown_as_application": mixed,
        },
        "connections": {
            "proposed": total_links, "supported": len(categories["supported"]), "precision": _ratio(len(categories["supported"]), total_links),
            "expected": len(expected_required), "covered": len(covered & expected_required), "recall": _ratio(len(covered & expected_required), len(expected_required)),
            "unsupported_for_review": {key: value for key, value in categories.items() if key != "supported" and value},
            "missing": sorted(f"{a} -> {b}" for a, b in expected_required - covered),
        },
        "evidence": {
            "citations": len(cited), "resolving": len(resolves), "resolution": _ratio(len(resolves), len(cited)),
            "member_paths": len(members), "member_paths_in_snapshot": len(existing),
        },
        "uncertainty": {
            "inferred": sum(item["classification"] == "inferred" for item in view["components"] + view["connections"]),
            "proposed": sum(item["classification"] == "proposed" for item in view["components"] + view["connections"]),
            "low_confidence": sum(item["confidence"] == "low" for item in view["components"] + view["connections"]),
            "alternatives": len(view["alternatives"]), "rejected_by_validation": view["rejected"],
        },
        "navigation": {
            "evidence_items": len(items), "with_commit_link": sum(entry["link"] is not None for entry in items),
            "working_tree_items": sum(entry["state"] == "working-tree" for entry in items),
        },
        "deterministic_baseline": {
            "semantic_nodes": len(semantic), "application_components_found": 0 if not semantic else len(baseline_covered),
            "expected_components_touched": baseline_covered,
            "note": "Nodes other than project, repository, language, manifest and dependency; none of them is an application component unless an analyzer supports the stack.",
        },
        "review": [
            {"connection": f"{names[link['source']]} -> {names[link['target']]}", "kind": link["kind"], "label": link["label"], "classification": link["classification"],
             "evidence": [f"{documents[key]['repository_id']}:{documents[key]['path']}:{documents[key]['line_start']}-{documents[key]['line_end']}" for key in link["evidence"] if key in documents],
             "verdict": None}
            for link in sorted(view["connections"], key=lambda item: item["id"])
        ],
        "limits": "Matching is by claimed files and an expected set written by one reviewer. A high score does not prove a relationship is true; the review list needs a human verdict for each sampled relationship.",
    }


class ReferenceProvider:
    """A scripted stand-in for a model that answers from the expected set, to test this harness and the workflow.

    It is not a model and its output says nothing about model quality. ``defects`` injects known faults so
    the harness can be shown to notice them: ``fabricated-citation``, ``missing-component``,
    ``unsupported-connection``, ``reversed-connection``, ``wrong-kind``, ``duplicate-connection``.
    """

    name = "ollama"
    model = "reference-replay"
    external = False

    def __init__(self, expected: dict[str, Any], defects: tuple[str, ...] = ()) -> None:
        self.expected, self.defects, self.calls = expected, defects, 0

    def _files(self, message: dict[str, Any], entry: dict[str, Any]) -> list[str]:
        paths = [path for item in message["inventory"] for path in item["paths"] if not path.endswith("/")]
        return [path for path in paths if any(fnmatch.fnmatchcase(path, pattern) for pattern in entry["members"]) and not is_denied(path)]

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        self.calls += 1
        message = json.loads(user)
        repository = message["inventory"][0]["repository_id"]
        if self.calls == 1:
            requests = [{"repository_id": repository, "path": self._files(message, entry)[0], "line_start": 1, "line_end": 60} for entry in self.expected["components"] if self._files(message, entry)]
            return ProviderResponse({"components": [], "connections": [], "alternatives": [], "requests": requests, "complete": False}, "reference-replay")
        ids = {item["path"]: item["id"] for item in message["excerpts"]}
        components, evidence_of = [], {}
        entries = list(self.expected["components"])
        if "missing-component" in self.defects:
            dropped = next(item for item in entries if item.get("required", True) and not item.get("deployment"))
            entries.remove(dropped)
        for entry in entries:
            files = self._files(message, entry)
            cited = [ids[path] for path in files if path in ids]
            if not files or not cited:
                continue
            evidence_of[entry["name"]] = cited
            components.append({"name": entry["name"], "kind": entry["kind"], "responsibility": f"{entry['name']} (reference replay).", "repository_id": repository,
                               "members": files[:6], "evidence": cited[:3], "confidence": "medium"})
        if "fabricated-citation" in self.defects and components:
            components.append({"name": "Invented service", "kind": "other", "responsibility": "Not in the source.", "repository_id": repository,
                               "members": components[0]["members"][:1], "evidence": ["ctx:0000000000000000"], "confidence": "high"})
        pairs = [(item["source"], item["target"], item["kinds"][0]) for item in self.expected["relationships"]]
        if "wrong-kind" in self.defects:
            pairs = [(source, target, next(kind for kind in CONNECTION_KINDS if kind not in item["kinds"])) for (source, target, _), item in zip(pairs, self.expected["relationships"])]
        if "duplicate-connection" in self.defects:
            pairs += [(item["source"], item["target"], item["kinds"][1]) for item in self.expected["relationships"] if len(item["kinds"]) > 1]
        if "reversed-connection" in self.defects and pairs:
            pairs[0] = (pairs[0][1], pairs[0][0], pairs[0][2])
        if "unsupported-connection" in self.defects and self.expected.get("not_established"):
            pairs.append((self.expected["not_established"][0]["source"], self.expected["not_established"][0]["target"], "writes"))
        connections = [
            {"source": source, "target": target, "source_repository": "", "target_repository": "", "kind": kind, "label": f"{kind} (reference replay)",
             "evidence": (evidence_of[source][:1] + evidence_of[target][:1]), "confidence": "medium", "candidate_id": ""}
            for source, target, kind in pairs if source in evidence_of and target in evidence_of
        ]
        return ProviderResponse({"components": components, "connections": connections, "alternatives": [], "requests": [], "complete": True}, "reference-replay")


def summary(report: dict[str, Any]) -> str:
    def cell(value: Any) -> str:
        return "n/a" if value is None else str(value)

    components, connections, evidence = report["components"], report["connections"], report["evidence"]
    rows = [
        ("components proposed / matched", f"{components['proposed']} / {components['matched']}"),
        ("component recall (required)", f"{cell(components['recall'])} ({components['required_found']}/{components['required_expected']})"),
        ("component precision", cell(components["precision"])),
        ("connection precision", cell(connections["precision"])),
        ("connection recall", f"{cell(connections['recall'])} ({connections['covered']}/{connections['expected']})"),
        ("unsupported or unmapped, for review", str(sum(len(value) for value in connections["unsupported_for_review"].values()))),
        ("citations resolving", f"{cell(evidence['resolution'])} ({evidence['resolving']}/{evidence['citations']})"),
        ("proposed (not inferred) items", str(report["uncertainty"]["proposed"])),
        ("evidence items with a commit link", f"{report['navigation']['with_commit_link']}/{report['navigation']['evidence_items']}"),
        ("deterministic components found", str(report["deterministic_baseline"]["application_components_found"])),
    ]
    width = max(len(name) for name, _ in rows)
    return f"{report['name']} ({report['view']['provider']}, {report['view']['served_model']})\n" + "\n".join(f"  {name.ljust(width)}  {value}" for name, value in rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m repodocs.architecture.evaluation", description="Score a stored architecture view against an expected set")
    parser.add_argument("--root", required=True, help="RepoDocs project root holding the scan and the stored view")
    parser.add_argument("--expected", required=True, help="Expected components and relationships (JSON)")
    parser.add_argument("--summary", action="store_true", help="Print a table instead of the JSON report")
    args = parser.parse_args(argv)
    config = ProjectConfig.load(Path(args.root))
    ir = load_ir(config.internal_path / "ir.json")
    status, view, reason = load_view(config, ir)
    if status != STATUS_CURRENT or view is None:
        print(f"The stored architecture view is {status}: {reason or 'run `repodocs architecture discover`'}", file=sys.stderr)
        return 1
    context = json.loads(saved_context_path(config, view["provenance"]["context_hash"]).read_text(encoding="utf-8"))
    try:
        report = evaluate(config, ir, view, context, json.loads(Path(args.expected).read_text(encoding="utf-8")))
    except (EvaluationInputError, OSError, ValueError) as exc:
        print(f"Cannot score: {exc}", file=sys.stderr)
        return 2
    print(summary(report) if args.summary else json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
