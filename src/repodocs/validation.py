"""Validation for persisted IR and generated artifacts."""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

from .common import semantic_id, sha256_text
from .config import ProjectConfig
from .confirmations import DECISIONS, STATUS_CANDIDATE, STATUS_STALE, STATUSES, apply_decisions, load_decisions
from .errors import OwnershipError, ValidationError
from .generator import (
    MANIFEST_SCHEMA_VERSION,
    _all_files,
    _read_manifest,
    describe_destination,
    describe_destination_change,
    destination_identity,
    manifest_path,
)
from .ir import IR_SCHEMA_VERSION, ir_hash
from .snapshot import REVISION_KIND_COMMIT, REVISION_KIND_CONTENT, SNAPSHOT_MODES
from .urls import safe_url


CLASSIFICATIONS = {"observed", "inferred", "proposed"}
REQUIRED_SNAPSHOT_FIELDS = {
    "repository_id",
    "path",
    "snapshot_mode",
    "working_tree_state",
    "content_fingerprint",
    "revision_kind",
    "revision",
    "files",
}


def load_ir(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValidationError(f"System IR not found: {path}; run scan first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Unable to read System IR: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"System IR must be a JSON object: {path}")
    return data


def validate_ir(ir: dict[str, Any]) -> list[str]:
    required = {"ir_version", "project", "snapshots", "nodes", "relationships", "evidence", "claims", "candidates", "provenance"}
    missing = sorted(required - set(ir))
    if missing:
        raise ValidationError("System IR is missing fields: " + ", ".join(missing))
    if ir["ir_version"] != IR_SCHEMA_VERSION:
        raise ValidationError(f"Unsupported IR version: {ir['ir_version']!r}")
    if ir["provenance"].get("snapshot_mode") not in SNAPSHOT_MODES:
        raise ValidationError(f"Unsupported snapshot mode in IR provenance: {ir['provenance'].get('snapshot_mode')!r}")
    snapshot_revisions: dict[str, str] = {}
    for snapshot in ir["snapshots"]:
        missing_fields = sorted(REQUIRED_SNAPSHOT_FIELDS - set(snapshot))
        if missing_fields:
            raise ValidationError(
                f"Snapshot {snapshot.get('repository_id')!r} is missing fields: " + ", ".join(missing_fields)
            )
        if snapshot["snapshot_mode"] != ir["provenance"]["snapshot_mode"]:
            raise ValidationError(f"Snapshot {snapshot['repository_id']} does not match the IR snapshot mode")
        kind = snapshot["revision_kind"]
        if kind == REVISION_KIND_COMMIT:
            if snapshot["revision"] != snapshot.get("git_revision"):
                raise ValidationError(f"Snapshot {snapshot['repository_id']} commit revision does not match HEAD")
        elif kind == REVISION_KIND_CONTENT:
            if snapshot["revision"] != snapshot["content_fingerprint"]:
                raise ValidationError(
                    f"Snapshot {snapshot['repository_id']} content revision does not match its fingerprint"
                )
        else:
            raise ValidationError(f"Snapshot {snapshot['repository_id']} has unknown revision kind {kind!r}")
        snapshot_revisions[snapshot["repository_id"]] = snapshot["revision"]
    node_ids = {item.get("id") for item in ir["nodes"]}
    evidence_ids = {item.get("id") for item in ir["evidence"]}
    for item in ir["evidence"]:
        repository_id = item.get("repository_id")
        if repository_id not in snapshot_revisions:
            raise ValidationError(f"Evidence references an unknown snapshot: {item.get('id')}")
        if item.get("revision") != snapshot_revisions[repository_id]:
            raise ValidationError(f"Evidence does not resolve to its snapshot revision: {item.get('id')}")
        if item.get("kind") == "span":
            start, end = item.get("line_start"), item.get("line_end")
            if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
                raise ValidationError(f"Span evidence has an invalid line range: {item.get('id')}")
    for limitation in ir.get("limitations", []):
        if limitation.get("repository_id") not in snapshot_revisions:
            raise ValidationError(f"Limitation references an unknown snapshot: {limitation.get('id')}")
        if not limitation.get("kind") or not limitation.get("message") or not limitation.get("analyzer"):
            raise ValidationError(f"Limitation is missing analyzer, kind, or message: {limitation.get('id')}")
        if not set(limitation.get("evidence_ids", [])) <= evidence_ids:
            raise ValidationError(f"Limitation references unknown evidence: {limitation.get('id')}")
    for relationship in ir["relationships"]:
        if relationship.get("source") not in node_ids or relationship.get("target") not in node_ids:
            raise ValidationError(f"Relationship references an unknown node: {relationship.get('id')}")
        if relationship.get("classification") not in CLASSIFICATIONS:
            raise ValidationError(f"Invalid relationship classification: {relationship.get('id')}")
        if not set(relationship.get("evidence_ids", [])) <= evidence_ids:
            raise ValidationError(f"Relationship references unknown evidence: {relationship.get('id')}")
    for claim in ir["claims"]:
        if claim.get("classification") not in CLASSIFICATIONS:
            raise ValidationError(f"Invalid claim classification: {claim.get('id')}")
        if not set(claim.get("evidence_ids", [])) <= evidence_ids:
            raise ValidationError(f"Claim references unknown evidence: {claim.get('id')}")
    for node in ir["nodes"]:
        if not set(node.get("evidence_ids", [])) <= evidence_ids:
            raise ValidationError(f"Node references unknown evidence: {node.get('id')}")
        if node.get("type") == "http_request":
            for field in ("url", "base_url"):
                value = node["attributes"].get(field)
                if value is not None and (not isinstance(value, str) or safe_url(value) != value):
                    raise ValidationError("HTTP request URLs must be sanitized; run scan again")
    validate_candidates(ir)
    return []


def validate_candidates(ir: dict[str, Any]) -> None:
    """Check identity, uncertainty, both evidence sides, and exact snapshot bindings."""

    candidates = ir["candidates"]
    if not isinstance(candidates, list):
        raise ValidationError("IR candidates must be an array")
    nodes = {node["id"]: node for node in ir["nodes"]}
    evidence = {item["id"]: item for item in ir["evidence"]}
    snapshots = {
        item["repository_id"]: {"revision": item["revision"], "revision_kind": item["revision_kind"]}
        for item in ir["snapshots"]
    }
    seen = set()
    for candidate in candidates:
        def fail() -> None:
            raise ValidationError("Invalid cross-repository candidate: identity, evidence, state, or revisions")

        if not isinstance(candidate, dict):
            fail()
        if set(candidate) - {"confirmation"} != {
            "id", "type", "source", "target", "status", "classification",
            "confidence", "reasons", "evidence_ids", "snapshots", "attributes",
        }:
            fail()
        status, confirmation = candidate.get("status"), candidate.get("confirmation")
        if status == STATUS_CANDIDATE:
            if confirmation is not None:
                fail()
        elif (status not in STATUSES or not isinstance(confirmation, dict)
                or confirmation.get("decision") not in DECISIONS
                or (status != STATUS_STALE and confirmation["decision"] != status)):
            fail()
        if not isinstance(candidate.get("source"), str) or not isinstance(candidate.get("target"), str):
            fail()
        source, target = nodes.get(candidate.get("source")), nodes.get(candidate.get("target"))
        if source is None or target is None or source["type"] != "http_request" or target["type"] != "endpoint":
            fail()
        source_repo = source["attributes"].get("repository_id")
        target_repo = target["attributes"].get("repository_id")
        if source_repo == target_repo or source_repo not in snapshots or target_repo not in snapshots:
            fail()
        expected_id = semantic_id("candidate", "http", source["id"], target["id"])
        if candidate.get("id") != expected_id or expected_id in seen:
            fail()
        seen.add(expected_id)
        confidence = candidate.get("confidence")
        if (candidate.get("type") != "http"
                or candidate.get("classification") != "inferred"
                or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence < 1):
            fail()
        if candidate.get("snapshots") != {repo: snapshots[repo] for repo in (source_repo, target_repo)}:
            fail()
        required_evidence = set(source["evidence_ids"]) | set(target["evidence_ids"])
        evidence_ids = candidate.get("evidence_ids")
        if (not source["evidence_ids"] or not target["evidence_ids"] or not isinstance(evidence_ids, list)
                or any(not isinstance(key, str) for key in evidence_ids)
                or set(evidence_ids) != required_evidence):
            fail()
        if any(evidence[key]["repository_id"] != source_repo for key in source["evidence_ids"]):
            fail()
        if any(evidence[key]["repository_id"] != target_repo for key in target["evidence_ids"]):
            fail()
        reasons = candidate.get("reasons")
        if not isinstance(reasons, list) or not reasons or any(not isinstance(reason, str) or reason not in {
            "method-match", "literal-path-match", "template-path-match", "declared-server-origin-match"
        } for reason in reasons):
            fail()
        attributes = candidate.get("attributes")
        if not isinstance(attributes, dict) or set(attributes) != {"method", "path", "origin_supported"}:
            fail()
        path = attributes.get("path")
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or safe_url(path) != path
                or attributes.get("method") != source["attributes"].get("http_method")
                or not isinstance(attributes.get("origin_supported"), bool)):
            fail()
        if (attributes["origin_supported"] != ("declared-server-origin-match" in reasons)
                or source["attributes"].get("http_method") != target["attributes"].get("http_method")):
            fail()


def validate_artifacts(config: ProjectConfig, ir: dict[str, Any]) -> list[str]:
    destination = describe_destination(config)
    output_dir = config.output_path
    path_to_manifest = manifest_path(config)
    manifest = _read_manifest(path_to_manifest)
    if manifest is None:
        raise ValidationError(f"Artifact manifest not found: {path_to_manifest}; run generate first")
    if manifest.get("manifest_version") != MANIFEST_SCHEMA_VERSION:
        raise ValidationError(f"Unsupported artifact manifest version: {manifest.get('manifest_version')!r}")
    recorded = manifest.get("destination")
    if destination_identity(recorded) != destination_identity(destination):
        raise ValidationError(
            "Artifact manifest destination does not match the configured destination ("
            + describe_destination_change(recorded, destination)
            + ")"
        )
    if manifest.get("source_ir_hash") != ir_hash(ir):
        raise ValidationError("Artifact manifest was generated from a different System IR")
    artifacts = manifest.get("artifacts", [])
    expected = {item.get("path") for item in artifacts}
    unexpected = _all_files(output_dir) - expected
    if unexpected:
        raise OwnershipError("Generated output contains unowned files: " + ", ".join(sorted(unexpected)))
    for item in artifacts:
        relative_path = item.get("path")
        if not relative_path:
            raise ValidationError("Artifact manifest contains an entry without a path")
        path = output_dir / relative_path
        if not path.exists():
            raise ValidationError(f"Generated artifact is missing: {path}")
        content = path.read_text(encoding="utf-8")
        if sha256_text(content) != item.get("content_hash"):
            raise OwnershipError(f"Generated file was edited unexpectedly: {path}")
        if path.suffix == ".mmd":
            validate_mermaid(content)
        elif path.suffix == ".md":
            validate_markdown(content, ir)
    return sorted(expected)


def validate_mermaid(content: str) -> None:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines or lines[0] != "flowchart TD":
        raise ValidationError("Mermaid artifact must begin with `flowchart TD`")
    node_pattern = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\[".*"\]$')
    edge_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+-->\s+([A-Za-z_][A-Za-z0-9_]*)$")
    defined: set[str] = set()
    references: list[str] = []
    for line in lines[1:]:
        node_match = node_pattern.match(line)
        if node_match:
            defined.add(node_match.group(1))
            continue
        edge_match = edge_pattern.match(line)
        if edge_match:
            references.extend(edge_match.groups())
            continue
        raise ValidationError(f"Unsupported or malformed Mermaid line: {line}")
    missing = sorted(set(references) - defined)
    if missing:
        raise ValidationError("Mermaid references undefined nodes: " + ", ".join(missing))


def validate_markdown(content: str, ir: dict[str, Any]) -> None:
    if not content.startswith("# "):
        raise ValidationError("Markdown artifact must begin with a level-one heading")
    for claim in ir.get("claims", []):
        for evidence_id in claim.get("evidence_ids", []):
            if evidence_id not in content:
                raise ValidationError(f"Markdown artifact is missing evidence reference: {evidence_id}")


def validate_candidate_states(config: ProjectConfig, ir: dict[str, Any]) -> None:
    """The persisted candidate states must be what the recorded decisions produce."""

    expected = copy.deepcopy(ir)
    apply_decisions(expected, load_decisions(config.root))
    if expected["candidates"] != ir["candidates"]:
        raise ValidationError("Candidate states do not match the recorded decisions; run scan again")


def validate_project(config: ProjectConfig) -> tuple[list[str], dict[str, Any]]:
    ir = load_ir(config.internal_path / "ir.json")
    validate_ir(ir)
    validate_candidate_states(config, ir)
    return validate_artifacts(config, ir), ir
