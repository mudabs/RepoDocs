"""Curated, redacted evidence bundles built from the System IR.

A bundle is the only thing a model provider ever receives. It contains analyzed
facts and evidence locations, never source text, file listings, content hashes,
or local filesystem paths. Every string is passed through secret redaction.
"""

from __future__ import annotations

import re
from typing import Any

from ..common import canonical_json, sha256_text
from ..ir import ir_hash


BUNDLE_SCHEMA_VERSION = "1.0"
REDACTION_RULES_VERSION = "1.1"

# Node types whose evidence is every file in a repository or language. Their facts are
# kept; their evidence lists are omitted so a bundle does not become a file listing.
BULK_EVIDENCE_TYPES = {"project", "repository", "language"}
# Attributes that are local paths or bulk listings, not architecture facts.
OMITTED_ATTRIBUTES = {"path", "manifest_files", "file_count", "total_bytes", "lines"}

# The key may carry a prefix (client_secret, DB_PASSWORD, x-api-key). The prefix is bounded so a very
# long unbroken word cannot make the search quadratic.
_ASSIGNED = r"(?i)\b([\w.-]{0,64}?(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|credential))(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
REDACTION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED:private-key]"),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws-access-key]"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[REDACTED:github-token]"),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "[REDACTED:github-token]"),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "[REDACTED:gitlab-token]"),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "[REDACTED:npm-token]"),
    ("slack-token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED:google-api-key]"),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}"), "[REDACTED:stripe-key]"),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[REDACTED:api-key]"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED:jwt]"),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [REDACTED:bearer-token]"),
    ("url-credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED:url-credentials]@"),
    ("assigned-secret", re.compile(_ASSIGNED), r"\1\2[REDACTED:assigned-secret]"),
)


def redact_text(value: str) -> tuple[str, int]:
    """Replace secret-looking substrings; returns the text and the number of replacements."""

    total = 0
    for _, pattern, replacement in REDACTION_RULES:
        value, count = pattern.subn(replacement, value)
        total += count
    return value, total


def _redact(value: Any, counter: list[int]) -> Any:
    if isinstance(value, str):
        text, count = redact_text(value)
        counter[0] += count
        return text
    if isinstance(value, list):
        return [_redact(item, counter) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, counter) for key, item in value.items()}
    return value


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _curated_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Keep scalars and lists of scalars; drop paths, listings, and nested structures."""

    return {
        key: value
        for key, value in sorted(attributes.items())
        if key not in OMITTED_ATTRIBUTES and (_scalar(value) or (isinstance(value, list) and all(map(_scalar, value))))
    }


def build_bundle(ir: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic evidence bundle for one System IR."""

    nodes = [
        {
            "id": node["id"],
            "type": node["type"],
            "name": node["name"],
            "attributes": _curated_attributes(node.get("attributes", {})),
            "evidence_ids": [] if node["type"] in BULK_EVIDENCE_TYPES else list(node["evidence_ids"]),
        }
        for node in ir["nodes"]
    ]
    bulk_ids = {node["id"] for node in nodes if node["type"] in BULK_EVIDENCE_TYPES}
    relationships = [
        {
            "id": edge["id"],
            "type": edge["type"],
            "source": edge["source"],
            "target": edge["target"],
            "classification": edge["classification"],
            "evidence_ids": [] if edge["target"] in bulk_ids else list(edge["evidence_ids"]),
        }
        for edge in ir["relationships"]
    ]
    candidates = [
        {
            "id": item["id"],
            "source": item["source"],
            "target": item["target"],
            "status": item["status"],
            "classification": item["classification"],
            "confidence": item["confidence"],
            "reasons": list(item["reasons"]),
            "evidence_ids": list(item["evidence_ids"]),
        }
        for item in ir.get("candidates", [])
    ]
    limitations = [
        {
            "id": item["id"],
            "repository_id": item["repository_id"],
            "kind": item["kind"],
            "message": item["message"],
            "evidence_ids": list(item["evidence_ids"]),
        }
        for item in ir.get("limitations", [])
    ]
    referenced = {
        evidence_id
        for group in (nodes, relationships, candidates, limitations)
        for item in group
        for evidence_id in item["evidence_ids"]
    }
    evidence = []
    for item in ir["evidence"]:
        if item["id"] not in referenced:
            continue
        entry = {
            "id": item["id"],
            "repository_id": item["repository_id"],
            "path": item["path"],
            "kind": item["kind"],
            "revision": item["revision"],
            "revision_kind": item["revision_kind"],
        }
        for key in ("line_start", "line_end", "symbol"):
            if item.get(key) is not None:
                entry[key] = item[key]
        evidence.append(entry)

    counter = [0]
    facts = _redact(
        {
            "nodes": nodes,
            "relationships": relationships,
            "candidates": candidates,
            "limitations": limitations,
            "evidence": evidence,
        },
        counter,
    )
    return {
        "bundle_version": BUNDLE_SCHEMA_VERSION,
        "project": _redact(dict(ir["project"]), counter),
        "source_ir_hash": ir_hash(ir),
        "snapshots": [
            {
                "repository_id": snapshot["repository_id"],
                "revision": snapshot["revision"],
                "revision_kind": snapshot["revision_kind"],
            }
            for snapshot in ir["snapshots"]
        ],
        **facts,
        "redaction": {"rules_version": REDACTION_RULES_VERSION, "replacements": counter[0]},
    }


def bundle_hash(bundle: dict[str, Any]) -> str:
    return sha256_text(canonical_json(bundle))
