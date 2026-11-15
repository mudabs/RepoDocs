"""Deterministic, non-authoritative cross-repository HTTP candidates (ADR-018).

No matching score establishes service ownership. Even an exact URL/method/path
match against a declared server remains an inferred candidate for issue #18.
"""

from __future__ import annotations

import re
from typing import Any

from .common import semantic_id
from .urls import url_target


CORRELATOR_NAME = "http-candidates"
CORRELATOR_VERSION = "1.0"
PARAMETER = re.compile(r"\{[A-Za-z_$][\w$]*(?:\.[\w$]+)*\}")


def paths_match(request: str, endpoint: str) -> bool:
    """Match whole path segments only; do not treat arbitrary expressions as regex."""

    left, right = request.split("/"), endpoint.split("/")
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if any(("{" in segment or "}" in segment) and not PARAMETER.fullmatch(segment) for segment in (a, b)):
            return False
        if "*" in a or "*" in b:
            return False
        if a == b:
            continue
        if (PARAMETER.fullmatch(a) and b) or (PARAMETER.fullmatch(b) and a):
            continue
        return False
    return True


def discover_candidates(ir: dict[str, Any]) -> list[dict[str, Any]]:
    requests = sorted((node for node in ir["nodes"] if node["type"] == "http_request"), key=lambda node: node["id"])
    endpoints = sorted((node for node in ir["nodes"] if node["type"] == "endpoint"), key=lambda node: node["id"])
    snapshots = {
        entry["repository_id"]: {"revision": entry["revision"], "revision_kind": entry["revision_kind"]}
        for entry in ir["snapshots"]
    }
    candidates = []
    for request in requests:
        source = request["attributes"]
        target_url = url_target(source.get("url", ""), source.get("base_url"))
        if target_url is None or source.get("http_method") == "ANY":
            continue
        origin, path = target_url
        # An unknown client base makes a relative request's real path unknown. Only an
        # absolute request URL, which overrides the base, is still a usable target.
        if source.get("base_url_unresolved") and origin is None:
            continue
        for endpoint in endpoints:
            target = endpoint["attributes"]
            source_repo, target_repo = source.get("repository_id"), target.get("repository_id")
            if source_repo == target_repo or source_repo not in snapshots or target_repo not in snapshots:
                continue
            if source.get("http_method") != target.get("http_method"):
                continue
            servers = target.get("servers", [])
            matches = []
            # A declared server URL belongs to this OpenAPI endpoint, not to
            # unrelated code endpoints that merely have the same method/path.
            for server in servers or [None]:
                destination = url_target(target.get("path", ""), server)
                if destination is None:
                    continue
                server_origin, endpoint_path = destination
                if origin and server_origin and origin != server_origin:
                    continue
                if paths_match(path, endpoint_path):
                    matches.append((server_origin, endpoint_path))
            if not matches:
                continue
            origin_supported = any(origin and server_origin == origin for server_origin, _ in matches)
            exact_path = any(path == endpoint_path for _, endpoint_path in matches)
            reasons = ["method-match", "literal-path-match" if exact_path else "template-path-match"]
            if origin_supported:
                reasons.append("declared-server-origin-match")
            candidates.append({
                "id": semantic_id("candidate", "http", request["id"], endpoint["id"]),
                "type": "http",
                "source": request["id"],
                "target": endpoint["id"],
                "status": "candidate",
                "classification": "inferred",
                "confidence": 0.9 if origin_supported else (0.65 if exact_path else 0.55),
                "reasons": reasons,
                "evidence_ids": sorted(set(request["evidence_ids"]) | set(endpoint["evidence_ids"])),
                "snapshots": {repo: snapshots[repo] for repo in sorted((source_repo, target_repo))},
                "attributes": {"method": source["http_method"], "path": path, "origin_supported": origin_supported},
            })
    # Competing endpoints are kept, never reduced to a winning backend.
    return sorted(candidates, key=lambda entry: entry["id"])
