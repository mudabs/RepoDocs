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
    lines.extend(_render_candidate_section(ir, nodes, evidence))
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
    lines.extend(["", "## Inferred relationships", ""])
    inferred_relationships = [item for item in relationships if item.get("classification") == "inferred"]
    if inferred_relationships:
        lines.append(
            "Relationships inferred from matching names rather than read from source. They are not facts: each one"
            " names the basis it was inferred from and cites the evidence of both sides."
        )
        lines.append("")
        lines.extend(["| Type | Source | Target | Basis | Confidence | Evidence |", "|---|---|---|---|---:|---|"])
        for relationship in inferred_relationships:
            source = nodes.get(relationship["source"], {}).get("name", relationship["source"])
            target = nodes.get(relationship["target"], {}).get("name", relationship["target"])
            attributes = relationship.get("attributes", {})
            refs = ", ".join(_evidence_ref(evidence, evidence_id) for evidence_id in relationship["evidence_ids"])
            lines.append(
                f"| {relationship['type']} | {_cell(source)} | {_cell(target)} | {_cell(attributes.get('basis', 'name match'))} | "
                f"{relationship.get('confidence', '')} | {refs} |"
            )
    else:
        lines.append("No relationships were inferred.")
    lines.extend(["", "## Evidence-backed claims", ""])
    for claim in claims:
        evidence_text = ", ".join(_evidence_ref(evidence, evidence_id) for evidence_id in claim["evidence_ids"])
        lines.append(f"- **{claim['classification']}** {_cell(claim['text'])} Evidence: {evidence_text or 'none'}.")
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


def _render_candidate_section(
    ir: dict[str, Any], nodes: dict[str, dict[str, Any]], evidence: dict[str, dict[str, Any]]
) -> list[str]:
    lines = ["", "## Cross-repository candidates (unconfirmed)", ""]
    lines.append(
        "These are inferred matches, not authoritative service links or observed traffic."
        " All matches, including unique high-confidence ones, require explicit confirmation."
        " Method/path agreement alone does not establish service ownership; competing matches are preserved."
    )
    candidates = ir.get("candidates", [])
    if not candidates:
        lines.extend(["", "No cross-repository candidates were discovered."])
        return lines
    lines.extend(["", "| Candidate | Request | Endpoint | Confidence | Basis | Revisions | Evidence |",
                  "|---|---|---|---:|---|---|---|"])
    for candidate in sorted(candidates, key=lambda item: item["id"]):
        source, target = nodes[candidate["source"]], nodes[candidate["target"]]
        revisions = ", ".join(
            f"{_cell(repo)}: {item['revision_kind']} `{_cell(item['revision'])}`"
            for repo, item in sorted(candidate["snapshots"].items())
        )
        refs = ", ".join(_evidence_ref(evidence, key) for key in candidate["evidence_ids"])
        lines.append(
            f"| `{candidate['id']}` (candidate) | {_cell(source['attributes']['repository_id'])}: {_cell(source['name'])} | "
            f"{_cell(target['attributes']['repository_id'])}: {_cell(target['name'])} | {candidate['confidence']} | "
            f"{_cell(', '.join(candidate['reasons']))} | {revisions} | {refs} |"
        )
    return lines


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
    contains, exposes, references, requests = _relationship_index(ir)

    services = sorted((node for node in nodes.values() if node["type"] == "service"), key=lambda item: (item["attributes"].get("repository_id", ""), item["name"]))
    lines.extend(["", "## Services", ""])
    if services:
        lines.extend(["| Service | Repository | Framework | Entry | Extracted |", "|---|---|---|---|---|"])
        for service in services:
            attributes = service["attributes"]
            framework = attributes.get("framework", "")
            if attributes.get("framework_version"):
                framework += f" {attributes['framework_version']}"
            entry = attributes.get("application_class") or attributes.get("package_path") or ""
            lines.append(
                f"| {_cell(service['name'])} | {attributes.get('repository_id', '')} | {_cell(framework)} | "
                f"`{_cell(entry)}` | {_service_summary(service['id'], nodes, contains, exposes, requests)} |"
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

    lines.extend(_render_database_section(ir, nodes))

    routes = sorted(
        (node for node in nodes.values() if node["type"] == "route"),
        key=lambda item: (item["attributes"].get("repository_id", ""), item["attributes"].get("path", ""), item["attributes"].get("module", ""), item["attributes"].get("line", 0)),
    )
    lines.extend(["", "## Routes", ""])
    if routes:
        lines.extend(["| Path | Router | Component | Module | Repository | Evidence |", "|---|---|---|---|---|---|"])
        for route in routes:
            attributes = route["attributes"]
            refs = ", ".join(_evidence_ref(evidence, evidence_id) for evidence_id in route["evidence_ids"])
            path = f"`{_cell(attributes.get('path', ''))}`" + (" (index)" if attributes.get("index") else "")
            lines.append(
                f"| {path} | {_cell(attributes.get('router', ''))} | {_cell(attributes.get('component') or 'not literal')} | "
                f"`{_cell(attributes.get('module', ''))}` | {attributes.get('repository_id', '')} | {refs} |"
            )
    else:
        lines.append("No client-side routes with literal paths were extracted.")

    request_nodes = sorted(
        (node for node in nodes.values() if node["type"] == "http_request"),
        key=lambda item: (item["attributes"].get("repository_id", ""), item["attributes"].get("url", ""), item["attributes"].get("http_method", ""), item["attributes"].get("module", "")),
    )
    lines.extend(["", "## Outgoing HTTP requests", ""])
    if request_nodes:
        lines.append(
            "Request sites whose URL is a string or template literal. Template holes are shown in braces; a base URL"
            " is listed when the client instance declares one literally. These are declared targets, not observed traffic."
        )
        lines.append("")
        lines.extend(["| Method | URL | Client | Base URL | Module | Repository | Evidence |", "|---|---|---|---|---|---|---|"])
        for request in request_nodes:
            attributes = request["attributes"]
            refs = ", ".join(_evidence_ref(evidence, evidence_id) for evidence_id in request["evidence_ids"])
            lines.append(
                f"| {attributes.get('http_method', '')} | `{_cell(attributes.get('url', ''))}` | {_cell(attributes.get('client', ''))} | "
                f"{_cell(attributes.get('base_url') or '')} | `{_cell(attributes.get('module', ''))}` | {attributes.get('repository_id', '')} | {refs} |"
            )
    else:
        lines.append("No HTTP request sites with literal URLs were extracted.")

    components = sorted(
        (node for node in nodes.values() if node["type"] == "component"),
        key=lambda item: (item["attributes"].get("repository_id", ""), item["attributes"].get("module", ""), item["name"]),
    )
    lines.extend(["", "## Components", ""])
    if components:
        lines.extend(["| Component | Kind | Module | Exported | References | Repository |", "|---|---|---|---|---:|---|"])
        for component in components:
            attributes = component["attributes"]
            exported = "default" if attributes.get("default_export") else ("named" if attributes.get("exported") else "no")
            lines.append(
                f"| {_cell(component['name'])} | {_cell(attributes.get('kind', ''))} | `{_cell(attributes.get('module', ''))}` | "
                f"{exported} | {references.get(component['id'], 0)} | {attributes.get('repository_id', '')} |"
            )
        lines.append("")
        lines.append("References count JSX elements that resolve to components defined in the same repository.")
    else:
        lines.append("No UI components were extracted.")
    return lines


def _render_database_section(ir: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> list[str]:
    """Database and table facts extracted from SQL migrations and schema files."""

    databases = sorted(
        (node for node in nodes.values() if node["type"] == "database"),
        key=lambda item: (item["attributes"].get("repository_id", ""), item["name"]),
    )
    tables = sorted(
        (node for node in nodes.values() if node["type"] == "table"),
        key=lambda item: (item["attributes"].get("repository_id", ""), item["name"]),
    )
    lines = ["", "## Database schema", ""]
    if not databases:
        lines.append("No database schema was extracted.")
        return lines
    owner = _database_owner(nodes, ir)
    lines.append(
        "Tables extracted from SQL migrations and schema files. Columns, keys, and constraints are read from the"
        " statements that defined them; data statements are not analyzed, and a foreign key to a table that is not"
        " defined in the same repository is reported as a limitation instead of an edge. The schema is listed only"
        " when the source qualified the table name; an unqualified table uses the search path of the connection."
    )
    lines.append("")
    lines.extend(["| Database | Table | Schema | Columns | Primary key | Foreign keys | Repository |", "|---|---|---|---:|---|---|---|"])
    for table in tables:
        attributes = table["attributes"]
        foreign_keys = attributes.get("foreign_keys", [])
        targets = ", ".join(
            f"{item.get('references_table', '')} ({', '.join(item.get('columns', []))})" for item in foreign_keys
        )
        lines.append(
            f"| {_cell(owner.get(table['id'], attributes.get('repository_id', '')))} | `{_cell(table['name'])}` | "
            f"{_cell(attributes.get('schema') or 'not qualified')} | {len(attributes.get('columns', []))} | "
            f"{_cell(', '.join(attributes.get('primary_key', [])) or 'none')} | {_cell(targets or 'none')} | "
            f"{attributes.get('repository_id', '')} |"
        )
    lines.append("")
    lines.extend(["### Table columns", "", "| Table | Column | Type | Nullable | Default | Key | Repository |", "|---|---|---|---|---|---|---|"])
    for table in tables:
        attributes = table["attributes"]
        unique_columns = {column for constraint in attributes.get("unique_constraints", []) for column in constraint.get("columns", [])}
        for column in attributes.get("columns", []):
            if column.get("primary_key"):
                key = "primary key"
            elif column.get("unique") or column.get("name") in unique_columns:
                key = "unique"
            elif column.get("identity"):
                key = "identity"
            else:
                key = ""
            lines.append(
                f"| `{_cell(table['name'])}` | `{_cell(column.get('name', ''))}` | `{_cell(column.get('type', ''))}` | "
                f"{'yes' if column.get('nullable', True) else 'no'} | {_cell(column.get('default') or '-')} | "
                f"{key} | {attributes.get('repository_id', '')} |"
            )
    return lines


def _database_owner(nodes: dict[str, dict[str, Any]], ir: dict[str, Any]) -> dict[str, str]:
    """Map every table to the database node that contains it."""

    owner: dict[str, str] = {}
    for relationship in ir.get("relationships", []):
        if relationship["type"] != "contains":
            continue
        database = nodes.get(relationship["source"])
        table = nodes.get(relationship["target"])
        if database and table and database["type"] == "database" and table["type"] == "table":
            owner[table["id"]] = database["name"]
    return owner


def _relationship_index(
    ir: dict[str, Any],
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, int], dict[str, list[str]]]:
    contains: dict[str, list[str]] = {}
    exposes: dict[str, list[str]] = {}
    references: dict[str, int] = {}
    requests: dict[str, list[str]] = {}
    for relationship in ir["relationships"]:
        if relationship["type"] == "contains":
            contains.setdefault(relationship["source"], []).append(relationship["target"])
        elif relationship["type"] == "exposes":
            exposes.setdefault(relationship["source"], []).append(relationship["target"])
        elif relationship["type"] == "references":
            references[relationship["source"]] = references.get(relationship["source"], 0) + 1
        elif relationship["type"] == "requests":
            requests.setdefault(relationship["source"], []).append(relationship["target"])
    return contains, exposes, references, requests


def _descendants(node_key: str, nodes: dict[str, dict[str, Any]], contains: dict[str, list[str]], depth: int = 3) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    frontier = [node_key]
    for _ in range(depth):
        next_frontier: list[str] = []
        for key in frontier:
            for target in contains.get(key, []):
                if target in nodes:
                    found.append(nodes[target])
                    next_frontier.append(target)
        frontier = next_frontier
    return found


def _service_summary(
    service_key: str,
    nodes: dict[str, dict[str, Any]],
    contains: dict[str, list[str]],
    exposes: dict[str, list[str]],
    requests: dict[str, list[str]],
) -> str:
    """Counts of what was extracted under a service, in a fixed order, listing only non-zero kinds."""

    members = _descendants(service_key, nodes, contains)
    counts = [
        ("controllers", sum(1 for item in members if item["type"] == "class" and item["attributes"].get("stereotype") == "controller")),
        ("endpoints", sum(len(exposes.get(item["id"], [])) for item in members)),
        ("entities", sum(1 for item in members if item["type"] == "class" and item["attributes"].get("stereotype") == "entity")),
        ("modules", sum(1 for item in members if item["type"] == "module")),
        ("components", sum(1 for item in members if item["type"] == "component")),
        ("routes", sum(1 for item in members if item["type"] == "route")),
        ("requests", sum(len(requests.get(item["id"], [])) for item in members)),
    ]
    return ", ".join(f"{count} {label}" for label, count in counts if count) or "no framework facts"


def render_mermaid(ir: dict[str, Any]) -> str:
    project = ir["project"]
    snapshots = sorted(ir["snapshots"], key=lambda item: item["repository_id"])
    lines = ["flowchart TD"]
    project_key = "project"
    lines.append(f'    {project_key}["{_escape_label(project["name"])}"]')
    nodes = {node["id"]: node for node in ir["nodes"]}
    contains, exposes, _, requests = _relationship_index(ir)
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
            label = (
                f"{service['name']} ({service['attributes'].get('framework', 'service')}): "
                + _service_summary(service["id"], nodes, contains, exposes, requests)
            )
            service_key = "svc_" + sha256_text(service["id"])[:10]
            lines.append(f'    {service_key}["{_escape_label(label)}"]')
            lines.append(f"    {repo_key} --> {service_key}")
        databases = sorted(
            (
                node
                for node in nodes.values()
                if node["type"] == "database" and node["attributes"].get("repository_id") == repo_id
            ),
            key=lambda item: item["id"],
        )
        for database in databases:
            attributes = database["attributes"]
            label = (
                f"{database['name']} (PostgreSQL): {attributes.get('table_count', 0)} tables, "
                f"{attributes.get('migration_tool', 'sql files')}"
            )
            database_key = "db_" + sha256_text(database["id"])[:10]
            lines.append(f'    {database_key}["{_escape_label(label)}"]')
            lines.append(f"    {repo_key} --> {database_key}")
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
