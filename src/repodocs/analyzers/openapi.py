"""Bounded, read-only OpenAPI 3.0.x/3.1.x JSON extraction for correlation.

Only paths, standard HTTP operations, literal servers, and same-document JSON
Pointer references are interpreted. No remote or filesystem reference is opened.
Schemas, payloads, examples, security values, and descriptions are not persisted.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

from ..ir import node_id, relationship_id
from ..urls import safe_url
from . import AnalysisResult, LIMITATION_UNREADABLE_SOURCE, LIMITATION_UNSUPPORTED_CONSTRUCT, RepositoryContext, SourceFile


METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
VERSION = re.compile(r"3\.(0|1)\.\d+")


class OpenApiAnalyzer:
    name = "openapi"
    version = "1.0"

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], Any] = {}

    def _document(self, context: RepositoryContext, source: SourceFile) -> Any:
        key = (context.repository_id, source.path, source.content_hash)
        if key not in self._cache:
            try:
                self._cache[key] = json.loads(context.text(source.path) or "")
            except (ValueError, RecursionError):
                self._cache[key] = None
        return self._cache[key]

    def _files(self, context: RepositoryContext) -> list[SourceFile]:
        result = []
        for source in context.files_with_suffix(".json", ".yaml", ".yml"):
            name = source.path.rsplit("/", 1)[-1].lower()
            named = name.startswith(("openapi.", "swagger."))
            document = self._document(context, source) if source.path.lower().endswith(".json") else None
            if named or isinstance(document, dict) and "openapi" in document:
                result.append(source)
        return result

    def detect(self, context: RepositoryContext) -> bool:
        return bool(self._files(context))

    def analyze(self, context: RepositoryContext) -> AnalysisResult:
        result = AnalysisResult()
        for source in self._files(context):
            evidence = context.file_evidence(source.path)
            result.evidence.append(evidence)

            def report(message: str) -> None:
                # Never interpolate raw URL, ref, description, or example values.
                result.limitations.append(context.limitation(
                    LIMITATION_UNSUPPORTED_CONSTRUCT, f"{source.path}: {message}", [evidence["id"]]
                ))

            if not source.path.lower().endswith(".json"):
                report("OpenAPI YAML is not supported; use OpenAPI 3.0.x or 3.1.x JSON.")
                continue
            if context.text(source.path) is None:
                result.limitations.append(context.limitation(
                    LIMITATION_UNREADABLE_SOURCE,
                    f"{source.path}: OpenAPI JSON could not be read as hash-checked UTF-8.",
                    [evidence["id"]],
                ))
                continue
            document = self._document(context, source)
            if not isinstance(document, dict) or not VERSION.fullmatch(str(document.get("openapi", ""))):
                report("Invalid or unsupported OpenAPI version; supported versions are 3.0.x and 3.1.x JSON.")
                continue
            # Inspect references throughout the document without following them
            # outside this object, including refs in unmodeled payload schemas.
            pending = [document]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if "$ref" in value:
                        try:
                            local_reference(document, value["$ref"])
                        except ValueError:
                            report("An external, malformed, or unresolved reference was not followed.")
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            paths = document.get("paths", {})
            if not isinstance(paths, dict):
                report("OpenAPI paths must be an object.")
                continue
            service = node_id("service", context.repository_id, source.path, "openapi")
            result.nodes.append({
                "id": service, "type": "service", "name": source.path,
                "attributes": {"repository_id": context.repository_id, "framework": "OpenAPI",
                               "framework_version": document["openapi"], "schema_path": source.path},
                "evidence_ids": [evidence["id"]],
            })
            result.relationships.append(edge("contains", context.repository_node_id, service, evidence["id"]))
            for declared_path, raw_item in sorted(paths.items()):
                path = safe_url(declared_path) if declared_path.startswith("/") and not declared_path.startswith("//") else None
                if not path:
                    report("An invalid OpenAPI path was skipped.")
                    continue
                try:
                    item = resolve_object(document, raw_item)
                except ValueError:
                    report("A path item reference could not be resolved locally.")
                    continue
                for method in METHODS:
                    if method not in item:
                        continue
                    operation = item[method]
                    if not isinstance(operation, dict) or "$ref" in operation:
                        report("Operation references or non-object operations are unsupported.")
                        continue
                    servers = operation.get("servers", item.get("servers", document.get("servers", [])))
                    literal_servers = []
                    if not isinstance(servers, list):
                        report("OpenAPI servers must be an array.")
                        servers = []
                    for server in servers:
                        url = server.get("url") if isinstance(server, dict) else None
                        clean = safe_url(url) if isinstance(url, str) and "{" not in url and "}" not in url else None
                        if clean is not None and not (
                            clean.startswith(("http://", "https://")) or clean.startswith("/") and not clean.startswith("//")
                        ):
                            clean = None
                        if clean is None:
                            report("A non-literal or unsupported server URL was not interpreted.")
                        else:
                            literal_servers.append(clean)
                    endpoint = node_id("endpoint", context.repository_id, source.path, "openapi", method.upper(), path)
                    result.nodes.append({
                        "id": endpoint, "type": "endpoint", "name": f"{method.upper()} {path}",
                        "attributes": {
                            "repository_id": context.repository_id, "http_method": method.upper(), "path": path,
                            "router": "openapi", "schema_path": source.path,
                            "servers": sorted(set(literal_servers)),
                        },
                        "evidence_ids": [evidence["id"]],
                    })
                    result.relationships.append(edge("exposes", service, endpoint, evidence["id"]))
        return result


def local_reference(document: dict, reference: Any) -> Any:
    """Same-document JSON Pointers only. Never interpret a path or fetch a URI."""

    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError("Only local JSON Pointers are supported")
    value: Any = document
    for token in unquote(reference[2:], errors="strict").split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("Invalid JSON Pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, dict):
                value = value[token]
            elif isinstance(value, list) and token.isdecimal() and (token == "0" or not token.startswith("0")):
                value = value[int(token)]
            else:
                raise ValueError("Invalid reference target")
        except (KeyError, IndexError):
            raise ValueError("Unresolved local reference") from None
    return value


def resolve_object(document: dict, value: Any) -> dict:
    seen = set()
    for _ in range(64):
        if not isinstance(value, dict):
            break
        if "$ref" not in value:
            return value
        reference = value["$ref"]
        if not isinstance(reference, str) or reference in seen:
            break
        seen.add(reference)
        # PathItem siblings have undefined/conflicting semantics; do not merge.
        if set(value) != {"$ref"}:
            raise ValueError("Reference siblings are not interpreted")
        value = local_reference(document, reference)
    raise ValueError("Cyclic, deep, or non-object reference")


def edge(kind: str, source: str, target: str, evidence: str) -> dict:
    return {
        "id": relationship_id(kind, source, target), "type": kind, "source": source, "target": target,
        "classification": "observed", "confidence": 1.0, "evidence_ids": [evidence],
    }
