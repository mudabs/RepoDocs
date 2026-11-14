"""Validation for persisted IR and generated artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import sha256_text
from .config import ProjectConfig
from .errors import OwnershipError, ValidationError
from .generator import MANIFEST_SCHEMA_VERSION, _all_files, _read_manifest, manifest_path
from .ir import IR_SCHEMA_VERSION, ir_hash
from .snapshot import REVISION_KIND_COMMIT, REVISION_KIND_CONTENT, SNAPSHOT_MODES


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
    required = {"ir_version", "project", "snapshots", "nodes", "relationships", "evidence", "claims", "provenance"}
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
    return []


def validate_artifacts(config: ProjectConfig, ir: dict[str, Any]) -> list[str]:
    destination = config.destination()
    output_dir = config.output_path
    path_to_manifest = manifest_path(config)
    manifest = _read_manifest(path_to_manifest)
    if manifest is None:
        raise ValidationError(f"Artifact manifest not found: {path_to_manifest}; run generate first")
    if manifest.get("manifest_version") != MANIFEST_SCHEMA_VERSION:
        raise ValidationError(f"Unsupported artifact manifest version: {manifest.get('manifest_version')!r}")
    recorded = manifest.get("destination")
    if not isinstance(recorded, dict) or recorded.get("repository_id") != destination.id:
        raise ValidationError(
            f"Artifact manifest destination {recorded.get('repository_id') if isinstance(recorded, dict) else None!r} "
            f"does not match the configured destination {destination.id!r}"
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


def validate_project(config: ProjectConfig) -> list[str]:
    ir = load_ir(config.internal_path / "ir.json")
    validate_ir(ir)
    return validate_artifacts(config, ir)
