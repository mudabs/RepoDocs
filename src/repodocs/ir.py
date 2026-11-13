"""Versioned System IR construction helpers."""

from __future__ import annotations

from typing import Any

from . import __version__
from .common import canonical_json, semantic_id, sha256_text, write_json


IR_SCHEMA_VERSION = "1.0"


def build_ir(
    *,
    project_id: str,
    project_name: str,
    snapshots: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    def unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list({item["id"]: item for item in items}.values())

    return {
        "ir_version": IR_SCHEMA_VERSION,
        "project": {"id": project_id, "name": project_name},
        "snapshots": sorted(snapshots, key=lambda item: item["repository_id"]),
        "nodes": sorted(unique(nodes), key=lambda item: item["id"]),
        "relationships": sorted(unique(relationships), key=lambda item: item["id"]),
        "evidence": sorted(unique(evidence), key=lambda item: item["id"]),
        "claims": sorted(unique(claims), key=lambda item: item["id"]),
        "provenance": {
            "engine": "repodocs",
            "engine_version": __version__,
            "analyzer": "basic-manifest",
            "analyzer_version": "1.0",
        },
    }


def write_ir(path, ir: dict[str, Any]) -> None:
    write_json(path, ir)


def ir_hash(ir: dict[str, Any]) -> str:
    return sha256_text(canonical_json(ir))


def node_id(node_type: str, *parts: str) -> str:
    return semantic_id("node", node_type, *parts)


def relationship_id(relationship_type: str, source: str, target: str) -> str:
    return semantic_id("relationship", relationship_type, source, target)


def evidence_id(repository_id: str, path: str, content_hash: str) -> str:
    return semantic_id("evidence", repository_id, path, content_hash)


def claim_id(text: str, evidence_ids: list[str]) -> str:
    return semantic_id("claim", text, *sorted(evidence_ids))
