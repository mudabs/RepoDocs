"""Deterministic Markdown and Mermaid generation with ownership protection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import canonical_json, sha256_text, write_json
from .config import ProjectConfig
from .errors import OwnershipError
from .ir import ir_hash


MANIFEST_SCHEMA_VERSION = "1.0"
GENERATOR_VERSION = "1.0"


@dataclass(frozen=True)
class GenerationResult:
    changed: bool
    artifacts: list[str]


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
        "| Repository | Revision | State | Files | Languages |",
        "|---|---|---|---:|---|",
    ]
    for snapshot in snapshots:
        languages = sorted(
            {
                item["language"]
                for item in snapshot["files"]
                if item.get("language")
            }
        )
        revision = snapshot.get("git_revision") or snapshot["content_fingerprint"][:12]
        lines.append(
            f"| {snapshot['repository_id']} | `{revision}` | {snapshot['working_tree_state']} | "
            f"{len(snapshot['files'])} | {', '.join(languages) or 'Unknown'} |"
        )
    lines.extend(["", "## Observed relationships", ""])
    observed_relationships = [
        item for item in relationships if item.get("classification") == "observed"
    ]
    if observed_relationships:
        lines.extend(["| Type | Source | Target |", "|---|---|---|"])
        nodes = {node["id"]: node for node in ir["nodes"]}
        for relationship in observed_relationships:
            source = nodes.get(relationship["source"], {}).get("name", relationship["source"])
            target = nodes.get(relationship["target"], {}).get("name", relationship["target"])
            lines.append(f"| {relationship['type']} | {source} | {target} |")
    else:
        lines.append("No observed relationships were extracted.")
    lines.extend(["", "## Evidence-backed claims", ""])
    for claim in claims:
        evidence_refs = []
        for evidence_id in claim["evidence_ids"]:
            source = evidence.get(evidence_id)
            if source:
                evidence_refs.append(f"`{source['repository_id']}:{source['path']}` ({evidence_id})")
            else:
                evidence_refs.append(f"`{evidence_id}`")
        evidence_text = ", ".join(evidence_refs)
        lines.append(f"- {claim['text']} Evidence: {evidence_text or 'none'}.")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "Framework-aware endpoint and entity extraction is not enabled by this foundation slice."
            " Dynamic behavior and runtime execution are not inferred.",
            "",
        ]
    )
    return "\n".join(lines)


def render_mermaid(ir: dict[str, Any]) -> str:
    project = ir["project"]
    nodes = {node["id"]: node for node in ir["nodes"]}
    snapshots = sorted(ir["snapshots"], key=lambda item: item["repository_id"])
    lines = ["flowchart TD"]
    project_key = "project"
    lines.append(f'    {project_key}["{_escape_label(project["name"])}"]')
    for snapshot in snapshots:
        repo_id = snapshot["repository_id"]
        repo_key = "repo_" + sha256_text(repo_id)[:10]
        lines.append(f'    {repo_key}["{_escape_label(repo_id)}"]')
        lines.append(f"    {project_key} --> {repo_key}")
        languages = sorted(
            {item["language"] for item in snapshot["files"] if item.get("language")}
        )
        for language in languages:
            language_key = "lang_" + sha256_text(f"{repo_id}:{language}")[:10]
            lines.append(f'    {language_key}["{_escape_label(language)}"]')
            lines.append(f"    {repo_key} --> {language_key}")
    return "\n".join(lines) + "\n"


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"Unable to read artifact manifest: {path}: {exc}") from exc
    return data


def _all_files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
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
        raise OwnershipError(
            "Generated output contains unowned files: " + ", ".join(sorted(unexpected_files))
        )
    for relative_path, entry in existing_entries.items():
        path = output_dir / relative_path
        if path.exists():
            current_hash = sha256_text(path.read_text(encoding="utf-8"))
            if current_hash != entry.get("content_hash"):
                raise OwnershipError(f"Generated file was edited unexpectedly: {path}")


def _manifest(ir: dict[str, Any], artifact_contents: dict[str, str]) -> dict[str, Any]:
    source_hash = ir_hash(ir)
    return {
        "manifest_version": MANIFEST_SCHEMA_VERSION,
        "ownership": "repodocs-generated",
        "source_ir_hash": source_hash,
        "generator": "repodocs-deterministic",
        "generator_version": GENERATOR_VERSION,
        "artifacts": [
            {
                "path": path,
                "content_hash": sha256_text(content),
                "source_ir_hash": source_hash,
                "generator": "repodocs-deterministic",
                "generator_version": GENERATOR_VERSION,
            }
            for path, content in sorted(artifact_contents.items())
        ],
    }


def generate_docs(config: ProjectConfig, ir: dict[str, Any]) -> GenerationResult:
    output_dir = config.output_path
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = config.internal_path / "artifact-manifest.json"
    manifest = _read_manifest(manifest_path)
    contents = {
        "architecture.md": render_markdown(ir),
        "architecture.mmd": render_mermaid(ir),
    }
    _preflight(output_dir, manifest, set(contents))
    new_manifest = _manifest(ir, contents)
    changed = False
    for relative_path, content in contents.items():
        path = output_dir / relative_path
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            changed = True
    manifest_text = canonical_json(new_manifest)
    if not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") != manifest_text:
        write_json(manifest_path, new_manifest)
        changed = True
    return GenerationResult(changed=changed, artifacts=sorted(contents))
