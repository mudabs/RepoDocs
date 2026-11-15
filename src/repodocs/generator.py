"""Deterministic Markdown and Mermaid generation with ownership protection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import canonical_json, sha256_text, write_json
from .config import ProjectConfig
from .errors import OwnershipError, SnapshotError
from .ir import ir_hash
from .snapshot import read_git_state


MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_FILENAME = "artifact-manifest.json"
GENERATOR_NAME = "repodocs-deterministic"
GENERATOR_VERSION = "1.0"

# The fields that identify where generated output lives. Revision and tree state change
# legitimately between runs and are deliberately not part of the identity.
DESTINATION_IDENTITY_FIELDS = ("repository_id", "role", "path", "output_directory")


def destination_identity(destination: Any) -> dict[str, Any]:
    source = destination if isinstance(destination, dict) else {}
    return {field: source.get(field) for field in DESTINATION_IDENTITY_FIELDS}


def describe_destination_change(recorded: Any, current: dict[str, Any]) -> str:
    before = destination_identity(recorded)
    after = destination_identity(current)
    return ", ".join(
        f"{field} {before[field]!r} -> {after[field]!r}" for field in DESTINATION_IDENTITY_FIELDS if before[field] != after[field]
    )


@dataclass(frozen=True)
class GenerationResult:
    changed: bool
    artifacts: list[str]
    destination: dict[str, Any]


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_markdown(ir: dict[str, Any]) -> str:
    project = ir["project"]
    snapshots = ir["snapshots"]
    relationships = ir["relationships"]
    claims = ir["claims"]
    evidence = {item["id"]: item for item in ir["evidence"]}
    lines = [
        f"# {project['name']} architecture overview",
        "",
        "This document was generated from the RepoDocs System IR. It contains deterministic static findings only.",
        "",
        "## Repositories",
        "",
        "| Repository | Snapshot | Revision | Working tree | Files | Languages |",
        "|---|---|---|---|---:|---|",
    ]
    for snapshot in snapshots:
        languages = sorted({item["language"] for item in snapshot["files"] if item.get("language")})
        revision = f"{snapshot['revision_kind']} `{snapshot['revision'][:12]}`"
        lines.append(
            f"| {snapshot['repository_id']} | {snapshot['snapshot_mode']} | {revision} | "
            f"{snapshot['working_tree_state']} | {len(snapshot['files'])} | {', '.join(languages) or 'Unknown'} |"
        )
    lines.extend(
        [
            "",
            "Evidence resolves to a commit revision only when the scanned tree was exactly that commit;"
            " otherwise it resolves to the working-tree content fingerprint.",
        ]
    )
    nodes = {node["id"]: node for node in ir["nodes"]}
    lines.extend(_render_analysis_sections(ir, nodes, evidence))
    lines.extend(["", "## Observed relationships", ""])
    observed_relationships = [item for item in relationships if item.get("classification") == "observed"]
    if observed_relationships:
        lines.extend(["| Type | Source | Target |", "|---|---|---|"])
        for relationship in observed_relationships:
            source = nodes.get(relationship["source"], {}).get("name", relationship["source"])
            target = nodes.get(relationship["target"], {}).get("name", relationship["target"])
            lines.append(f"| {relationship['type']} | {_cell(source)} | {_cell(target)} |")
    else:
        lines.append("No observed relationships were extracted.")
    lines.extend(["", "## Evidence-backed claims", ""])
    for claim in claims:
        evidence_text = ", ".join(_evidence_ref(evidence, evidence_id) for evidence_id in claim["evidence_ids"])
        lines.append(f"- {_cell(claim['text'])} Evidence: {evidence_text or 'none'}.")
    lines.extend(["", "## Reported limitations", ""])
    limitations = ir.get("limitations", [])
    if limitations:
        lines.append(
            "Each entry names a construct the analyzers saw but did not turn into a fact. Nothing listed here"
            " is inferred into the documentation."
        )
        lines.append("")
        for limitation in sorted(limitations, key=lambda item: (item["repository_id"], item["kind"], item["message"])):
            evidence_text = ", ".join(_evidence_ref(evidence, evidence_id) for evidence_id in limitation["evidence_ids"])
            lines.append(
                f"- **{limitation['repository_id']}** `{limitation['kind']}` ({limitation['analyzer']}): "
                f"{_cell(limitation['message'])} Evidence: {evidence_text or 'none'}."
            )
    else:
        lines.append("The analyzers that ran reported no limitations.")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "Only the analyzers listed under Analysis coverage ran, and only for the repositories named there."
            " Facts are extracted from source text; dynamic behavior, reflection, generated code, and runtime"
            " configuration are reported above rather than inferred. Runtime execution order is never inferred.",
            "",
        ]
    )
    return "\n".join(lines)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _evidence_ref(evidence: dict[str, dict[str, Any]], evidence_id: str) -> str:
    source = evidence.get(evidence_id)
    if not source:
        return f"`{evidence_id}`"
    location = f"{source['repository_id']}:{source['path']}"
    if source.get("kind") == "span":
        location += f":L{source['line_start']}-L{source['line_end']}"
    return f"`{location}` ({evidence_id})"


def _render_analysis_sections(
    ir: dict[str, Any], nodes: dict[str, dict[str, Any]], evidence: dict[str, dict[str, Any]]
) -> list[str]:
    """Framework-aware sections: coverage, services, endpoints, entities."""

    lines = ["", "## Analysis coverage", ""]
    for analyzer in ir["provenance"].get("analyzers", []):
        repositories = ", ".join(analyzer.get("repositories", [])) or "no repository detected"
        lines.append(f"- `{analyzer['name']}` {analyzer['version']}: {repositories}")
    relationships = ir["relationships"]
    contains: dict[str, list[str]] = {}
    exposes: dict[str, list[str]] = {}
    references: dict[str, int] = {}
    for relationship in relationships:
        if relationship["type"] == "contains":
            contains.setdefault(relationship["source"], []).append(relationship["target"])
        elif relationship["type"] == "exposes":
            exposes.setdefault(relationship["source"], []).append(relationship["target"])
        elif relationship["type"] == "references":
            references[relationship["source"]] = references.get(relationship["source"], 0) + 1

    services = sorted((node for node in nodes.values() if node["type"] == "service"), key=lambda item: (item["attributes"].get("repository_id", ""), item["name"]))
    lines.extend(["", "## Services", ""])
    if services:
        lines.extend(
            [
                "| Service | Repository | Framework | Application class | Controllers | Endpoints | Entities |",
                "|---|---|---|---|---:|---:|---:|",
            ]
        )
        for service in services:
            attributes = service["attributes"]
            members = [nodes[target] for target in contains.get(service["id"], []) if target in nodes]
            controllers = [item for item in members if item["attributes"].get("stereotype") == "controller"]
            entities = [item for item in members if item["attributes"].get("stereotype") == "entity"]
            endpoint_count = sum(len(exposes.get(item["id"], [])) for item in controllers)
            framework = attributes.get("framework", "")
            if attributes.get("framework_version"):
                framework += f" {attributes['framework_version']}"
            lines.append(
                f"| {_cell(service['name'])} | {attributes.get('repository_id', '')} | {_cell(framework)} | "
                f"`{_cell(attributes.get('application_class', ''))}` | {len(controllers)} | {endpoint_count} | {len(entities)} |"
            )
    else:
        lines.append("No framework-aware services were detected.")

    endpoints = sorted(
        (node for node in nodes.values() if node["type"] == "endpoint"),
        key=lambda item: (item["attributes"].get("repository_id", ""), item["attributes"].get("path", ""), item["attributes"].get("http_method", "")),
    )
    lines.extend(["", "## Endpoints", ""])
    if endpoints:
        lines.extend(["| Method | Path | Controller | Handler | Repository | Evidence |", "|---|---|---|---|---|---|"])
        for endpoint in endpoints:
            attributes = endpoint["attributes"]
            refs = ", ".join(_evidence_ref(evidence, evidence_id) for evidence_id in endpoint["evidence_ids"])
            lines.append(
                f"| {attributes.get('http_method', '')} | `{_cell(attributes.get('path', ''))}` | "
                f"`{_cell(attributes.get('controller', ''))}` | {_cell(attributes.get('handler', ''))} | "
                f"{attributes.get('repository_id', '')} | {refs} |"
            )
    else:
        lines.append("No request mappings with literal paths were extracted.")

    entities = sorted(
        (node for node in nodes.values() if node["type"] == "class" and node["attributes"].get("stereotype") == "entity"),
        key=lambda item: (item["attributes"].get("repository_id", ""), item["attributes"].get("qualified_name", "")),
    )
    lines.extend(["", "## Entities", ""])
    if entities:
        lines.extend(["| Entity | Table | Fields | Associations | Repository |", "|---|---|---:|---:|---|"])
        for entity in entities:
            attributes = entity["attributes"]
            table = attributes.get("table") or "not declared"
            lines.append(
                f"| `{_cell(attributes.get('qualified_name', entity['name']))}` | {_cell(table)} | "
                f"{len(attributes.get('fields', []))} | {references.get(entity['id'], 0)} | {attributes.get('repository_id', '')} |"
            )
        lines.append("")
        lines.append("Table names are listed only when declared with @Table; default naming strategies are not applied.")
    else:
        lines.append("No persistence entities were extracted.")
    return lines


def render_mermaid(ir: dict[str, Any]) -> str:
    project = ir["project"]
    snapshots = sorted(ir["snapshots"], key=lambda item: item["repository_id"])
    lines = ["flowchart TD"]
    project_key = "project"
    lines.append(f'    {project_key}["{_escape_label(project["name"])}"]')
    nodes = {node["id"]: node for node in ir["nodes"]}
    exposes: dict[str, int] = {}
    contains: dict[str, list[str]] = {}
    for relationship in ir["relationships"]:
        if relationship["type"] == "exposes":
            exposes[relationship["source"]] = exposes.get(relationship["source"], 0) + 1
        elif relationship["type"] == "contains":
            contains.setdefault(relationship["source"], []).append(relationship["target"])
    for snapshot in snapshots:
        repo_id = snapshot["repository_id"]
        repo_key = "repo_" + sha256_text(repo_id)[:10]
        lines.append(f'    {repo_key}["{_escape_label(repo_id)}"]')
        lines.append(f"    {project_key} --> {repo_key}")
        languages = sorted({item["language"] for item in snapshot["files"] if item.get("language")})
        for language in languages:
            language_key = "lang_" + sha256_text(f"{repo_id}:{language}")[:10]
            lines.append(f'    {language_key}["{_escape_label(language)}"]')
            lines.append(f"    {repo_key} --> {language_key}")
        services = sorted(
            (node for node in nodes.values() if node["type"] == "service" and node["attributes"].get("repository_id") == repo_id),
            key=lambda item: item["id"],
        )
        for service in services:
            members = [nodes[target] for target in contains.get(service["id"], []) if target in nodes]
            controllers = [item for item in members if item["attributes"].get("stereotype") == "controller"]
            endpoint_count = sum(exposes.get(item["id"], 0) for item in controllers)
            entity_count = sum(1 for item in members if item["attributes"].get("stereotype") == "entity")
            label = (
                f"{service['name']} ({service['attributes'].get('framework', 'service')}): "
                f"{len(controllers)} controllers, {endpoint_count} endpoints, {entity_count} entities"
            )
            service_key = "svc_" + sha256_text(service["id"])[:10]
            lines.append(f'    {service_key}["{_escape_label(label)}"]')
            lines.append(f"    {repo_key} --> {service_key}")
    return "\n".join(lines) + "\n"


def manifest_path(config: ProjectConfig) -> Path:
    return config.internal_path / MANIFEST_FILENAME


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"Unable to read artifact manifest: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OwnershipError(f"Artifact manifest must be a JSON object: {path}")
    return data


def _all_files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def describe_destination(config: ProjectConfig) -> dict[str, Any]:
    """Resolve the configured destination and record its ownership facts.

    Fails when the destination is missing, is not a directory, or is pinned to a revision
    that the checkout does not match. Paths are recorded as configured so the manifest is
    stable across machines.
    """

    spec = config.destination()
    destination_root = config.destination_root()
    output_path = config.output_path
    git = read_git_state(destination_root)
    if spec.pinned_revision is not None:
        if git.revision is None:
            raise SnapshotError(
                f"{spec.id}: destination is pinned to {spec.pinned_revision} but is not a Git repository with a commit"
            )
        if git.revision != spec.pinned_revision:
            raise SnapshotError(
                f"{spec.id}: stale destination checkout; HEAD is {git.revision} but the pinned revision is "
                f"{spec.pinned_revision}"
            )
    return {
        "repository_id": spec.id,
        "role": spec.role,
        "path": spec.path,
        "output_directory": output_path.relative_to(destination_root).as_posix(),
        "vcs": "git" if git.is_repository else None,
        "git_revision": git.revision,
        "working_tree_state": git.working_tree_state,
        "pinned_revision": spec.pinned_revision,
    }


def _preflight(output_dir: Path, manifest: dict[str, Any] | None, planned: set[str]) -> None:
    if manifest is None:
        unexpected = _all_files(output_dir)
        if unexpected:
            raise OwnershipError(
                "Generated output directory contains unowned files: " + ", ".join(sorted(unexpected))
            )
        return
    existing_entries = {item["path"]: item for item in manifest.get("artifacts", [])}
    unexpected_manifest = set(existing_entries) - planned
    if unexpected_manifest:
        raise OwnershipError(
            "Artifact manifest contains unexpected paths: " + ", ".join(sorted(unexpected_manifest))
        )
    unexpected_files = _all_files(output_dir) - set(existing_entries)
    if unexpected_files:
        raise OwnershipError("Generated output contains unowned files: " + ", ".join(sorted(unexpected_files)))
    for relative_path, entry in existing_entries.items():
        path = output_dir / relative_path
        if path.exists():
            current_hash = sha256_text(path.read_text(encoding="utf-8"))
            if current_hash != entry.get("content_hash"):
                raise OwnershipError(f"Generated file was edited unexpectedly: {path}")


def _manifest(ir: dict[str, Any], destination: dict[str, Any], artifact_contents: dict[str, str]) -> dict[str, Any]:
    source_hash = ir_hash(ir)
    return {
        "manifest_version": MANIFEST_SCHEMA_VERSION,
        "ownership": "repodocs-generated",
        "destination": destination,
        "source_ir_hash": source_hash,
        "generator": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "artifacts": [
            {
                "path": path,
                "content_hash": sha256_text(content),
                "source_ir_hash": source_hash,
                "generator": GENERATOR_NAME,
                "generator_version": GENERATOR_VERSION,
            }
            for path, content in sorted(artifact_contents.items())
        ],
    }


def generate_docs(config: ProjectConfig, ir: dict[str, Any]) -> GenerationResult:
    destination = describe_destination(config)
    output_dir = config.output_path
    path_to_manifest = manifest_path(config)
    manifest = _read_manifest(path_to_manifest)
    if manifest is not None:
        recorded = manifest.get("destination")
        if destination_identity(recorded) != destination_identity(destination):
            raise OwnershipError(
                "Artifact manifest belongs to a different destination ("
                + describe_destination_change(recorded, destination)
                + "); remove the previous generated output and manifest before switching"
            )
    contents = {
        "architecture.md": render_markdown(ir),
        "architecture.mmd": render_mermaid(ir),
    }
    _preflight(output_dir, manifest, set(contents))
    output_dir.mkdir(parents=True, exist_ok=True)
    new_manifest = _manifest(ir, destination, contents)
    changed = False
    for relative_path, content in contents.items():
        path = output_dir / relative_path
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            changed = True
    manifest_text = canonical_json(new_manifest)
    if not path_to_manifest.exists() or path_to_manifest.read_text(encoding="utf-8") != manifest_text:
        write_json(path_to_manifest, new_manifest)
        changed = True
    return GenerationResult(changed=changed, artifacts=sorted(contents), destination=destination)
