"""Validation for persisted IR and generated artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import sha256_text
from .config import ProjectConfig
from .errors import OwnershipError, ValidationError
from .generator import _all_files, _read_manifest
from .ir import IR_SCHEMA_VERSION, ir_hash


def load_ir(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValidationError(f"System IR not found: {path}; run scan first")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Unable to read System IR: {path}: {exc}") from exc


def validate_ir(ir: dict[str, Any]) -> list[str]:
    required = {"ir_version", "project", "snapshots", "nodes", "relationships", "evidence", "claims", "provenance"}
    missing = sorted(required - set(ir))
    if missing:
        raise ValidationError("System IR is missing fields: " + ", ".join(missing))
    if ir["ir_version"] != IR_SCHEMA_VERSION:
        raise ValidationError(f"Unsupported IR version: {ir['ir_version']!r}")
    node_ids = {item.get("id") for item in ir["nodes"]}
    evidence_ids = {item.get("id") for item in ir["evidence"]}
    for relationship in ir["relationships"]:
        if relationship.get("source") not in node_ids or relationship.get("target") not in node_ids:
            raise ValidationError(f"Relationship references an unknown node: {relationship.get('id')}")
        if relationship.get("classification") not in {"observed", "inferred", "proposed"}:
            raise ValidationError(f"Invalid relationship classification: {relationship.get('id')}")
        if not set(relationship.get("evidence_ids", [])) <= evidence_ids:
            raise ValidationError(f"Relationship references unknown evidence: {relationship.get('id')}")
    for claim in ir["claims"]:
        if claim.get("classification") not in {"observed", "inferred", "proposed"}:
            raise ValidationError(f"Invalid claim classification: {claim.get('id')}")
        if not set(claim.get("evidence_ids", [])) <= evidence_ids:
            raise ValidationError(f"Claim references unknown evidence: {claim.get('id')}")
    for node in ir["nodes"]:
        if not set(node.get("evidence_ids", [])) <= evidence_ids:
            raise ValidationError(f"Node references unknown evidence: {node.get('id')}")
    return []


def validate_artifacts(config: ProjectConfig, ir: dict[str, Any]) -> list[str]:
    output_dir = config.output_path
    manifest_path = config.internal_path / "artifact-manifest.json"
    manifest = _read_manifest(manifest_path)
    if manifest is None:
        raise ValidationError(f"Artifact manifest not found: {manifest_path}; run generate first")
    if manifest.get("manifest_version") != "1.0":
        raise ValidationError(f"Unsupported artifact manifest version: {manifest.get('manifest_version')!r}")
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
        current_hash = sha256_text(path.read_text(encoding="utf-8"))
        if current_hash != item.get("content_hash"):
            raise OwnershipError(f"Generated file was edited unexpectedly: {path}")
        content = path.read_text(encoding="utf-8")
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
