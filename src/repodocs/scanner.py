"""Safe, deterministic static discovery and basic manifest analysis."""

from __future__ import annotations

import json
import os
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .common import sha256_bytes
from .config import ProjectConfig
from .errors import ScanError
from .ir import build_ir, claim_id, evidence_id, node_id, relationship_id
from .snapshot import SNAPSHOT_MODE_LOCAL, content_fingerprint, read_git_state, resolve_snapshot


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILES = 20_000

EXCLUDED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".repodocs",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "generated",
}

LANGUAGE_BY_SUFFIX = {
    ".py": "Python",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".sql": "SQL",
    ".css": "CSS",
    ".scss": "SCSS",
    ".html": "HTML",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
}

MANIFEST_NAMES = {
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "cargo.toml",
    "go.mod",
    "dockerfile",
}


@dataclass(frozen=True)
class DiscoveredFile:
    path: str
    size: int
    content_hash: str
    language: str | None


def detect_language(path: Path) -> str | None:
    name = path.name.lower()
    if name == "dockerfile":
        return "Dockerfile"
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def discover_files(
    root: Path, excluded_paths: Iterable[str] = ()
) -> tuple[list[DiscoveredFile], list[str], int]:
    """Walk a repository without following symlinks or executing anything.

    ``excluded_paths`` are repository-relative POSIX directory paths that are skipped in
    addition to the well-known dependency and build directories, for example the generated
    documentation directory when the repository is also the documentation destination.
    """

    root = root.resolve()
    if not root.exists() or not root.is_dir():
        raise ScanError(f"Repository path is not a directory: {root}")
    excluded = {item.strip("/").lower() for item in excluded_paths if item and item.strip("/") not in {"", "."}}
    discovered: list[DiscoveredFile] = []
    warnings: list[str] = []
    total_bytes = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.lower())
        except OSError as exc:
            raise ScanError(f"Unable to read directory {directory}: {exc}") from exc
        for entry in entries:
            if entry.is_symlink():
                warnings.append(f"Skipped symlink: {entry.path}")
                continue
            if entry.is_dir(follow_symlinks=False):
                relative_dir = Path(entry.path).relative_to(root).as_posix().lower()
                if entry.name.lower() not in EXCLUDED_DIRECTORIES and relative_dir not in excluded:
                    pending.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if len(discovered) >= MAX_FILES:
                raise ScanError(f"File limit exceeded ({MAX_FILES}) while scanning {root}")
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError as exc:
                warnings.append(f"Skipped unreadable file {entry.path}: {exc}")
                continue
            if size > MAX_FILE_BYTES:
                warnings.append(f"Skipped file over size limit: {entry.path}")
                continue
            if total_bytes + size > MAX_TOTAL_BYTES:
                raise ScanError(f"Total readable file limit exceeded ({MAX_TOTAL_BYTES} bytes) in {root}")
            try:
                content = Path(entry.path).read_bytes()
            except OSError as exc:
                warnings.append(f"Skipped unreadable file {entry.path}: {exc}")
                continue
            total_bytes += len(content)
            if _is_binary(content):
                warnings.append(f"Skipped binary file: {entry.path}")
                continue
            relative = Path(entry.path).relative_to(root).as_posix()
            discovered.append(
                DiscoveredFile(
                    path=relative,
                    size=len(content),
                    content_hash=sha256_bytes(content),
                    language=detect_language(Path(entry.path)),
                )
            )
    discovered.sort(key=lambda item: item.path)
    return discovered, sorted(set(warnings)), total_bytes


def _manifest_dependencies(path: Path) -> list[str]:
    name = path.name.lower()
    try:
        if name == "package.json":
            data = json.loads(path.read_text(encoding="utf-8"))
            result: list[str] = []
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                values = data.get(section, {})
                if isinstance(values, dict):
                    result.extend(str(key) for key in values)
            return sorted(set(result))
        if name == "pyproject.toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            result = []
            project = data.get("project", {})
            if isinstance(project, dict):
                dependencies = project.get("dependencies", [])
                if isinstance(dependencies, list):
                    result.extend(str(item).split("[", 1)[0].split("=", 1)[0].strip() for item in dependencies)
            poetry = data.get("tool", {}).get("poetry", {})
            if isinstance(poetry, dict) and isinstance(poetry.get("dependencies"), dict):
                result.extend(str(key) for key in poetry["dependencies"] if key.lower() != "python")
            return sorted({item for item in result if item})
        if name == "pom.xml":
            root = ET.fromstring(path.read_text(encoding="utf-8"))
            result = []
            for dependency in root.findall(".//{*}dependency"):
                group_id = dependency.findtext("{*}groupId") or ""
                artifact_id = dependency.findtext("{*}artifactId") or ""
                if group_id and artifact_id:
                    result.append(f"{group_id}:{artifact_id}")
            return sorted(set(result))
        if name == "requirements.txt":
            result = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    result.append(line.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0].strip())
            return sorted({item for item in result if item})
    except (OSError, ValueError, TypeError, json.JSONDecodeError, tomllib.TOMLDecodeError, ET.ParseError):
        return []
    return []


def _excluded_output_paths(config: ProjectConfig, repository_id: str) -> list[str]:
    """When a source repository is also the destination, never scan its generated output."""

    if config.destination_repository_id != repository_id:
        return []
    output_path = config.output_path
    destination_root = config.destination_root()
    return [output_path.relative_to(destination_root).as_posix()]


def scan_project(
    config: ProjectConfig,
    *,
    mode: str = SNAPSHOT_MODE_LOCAL,
    pins: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Scan every source repository into a System IR under the requested snapshot mode.

    ``pins`` maps repository IDs to expected commit SHAs for this run and overrides the
    configured ``pinned_revision``. Destination-only repositories are not scanned.
    """

    pins = pins or {}
    for repository_id in pins:
        config.repository(repository_id)
    project_node = node_id("project", config.project_id)
    snapshots: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = [
        {"id": project_node, "type": "project", "name": config.project_name, "attributes": {}, "evidence_ids": []}
    ]
    relationships: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    warnings: list[str] = []

    for repository in config.source_repositories():
        root = config.resolve_repository(repository)
        files, file_warnings, total_bytes = discover_files(root, _excluded_output_paths(config, repository.id))
        warnings.extend(f"{repository.id}: {warning}" for warning in file_warnings)
        header = resolve_snapshot(
            repository_id=repository.id,
            mode=mode,
            git=read_git_state(root),
            fingerprint=content_fingerprint([(item.path, item.content_hash) for item in files]),
            pinned_revision=pins.get(repository.id, repository.pinned_revision),
        )
        revision = header["revision"]
        revision_kind = header["revision_kind"]
        repo_node = node_id("repository", repository.id)
        repo_evidence_ids = [evidence_id(repository.id, item.path, item.content_hash) for item in files]
        languages = sorted({item.language for item in files if item.language})
        manifest_files = [item for item in files if Path(item.path).name.lower() in MANIFEST_NAMES]
        nodes.append(
            {
                "id": repo_node,
                "type": "repository",
                "name": repository.id,
                "attributes": {
                    "path": repository.path,
                    "role": repository.role,
                    "file_count": len(files),
                    "total_bytes": total_bytes,
                    "languages": languages,
                    "manifest_files": [item.path for item in manifest_files],
                },
                "evidence_ids": repo_evidence_ids,
            }
        )
        relationships.append(
            {
                "id": relationship_id("contains", project_node, repo_node),
                "type": "contains",
                "source": project_node,
                "target": repo_node,
                "classification": "observed",
                "confidence": 1.0,
                "evidence_ids": [],
            }
        )
        for item in files:
            evidence.append(
                {
                    "id": evidence_id(repository.id, item.path, item.content_hash),
                    "repository_id": repository.id,
                    "path": item.path,
                    "content_hash": item.content_hash,
                    "revision": revision,
                    "revision_kind": revision_kind,
                    "kind": "file",
                }
            )
        for language in languages:
            language_node = node_id("language", repository.id, language)
            language_evidence = [
                evidence_id(repository.id, item.path, item.content_hash)
                for item in files
                if item.language == language
            ]
            nodes.append(
                {"id": language_node, "type": "language", "name": language, "attributes": {}, "evidence_ids": language_evidence}
            )
            relationships.append(
                {
                    "id": relationship_id("contains", repo_node, language_node),
                    "type": "contains",
                    "source": repo_node,
                    "target": language_node,
                    "classification": "observed",
                    "confidence": 1.0,
                    "evidence_ids": language_evidence,
                }
            )
            text = f"Repository {repository.id} contains {language} source files."
            claims.append(
                {
                    "id": claim_id(text, language_evidence),
                    "text": text,
                    "classification": "observed",
                    "confidence": 1.0,
                    "evidence_ids": language_evidence,
                }
            )
        for item in manifest_files:
            manifest_node = node_id("manifest", repository.id, item.path)
            manifest_evidence = [evidence_id(repository.id, item.path, item.content_hash)]
            nodes.append(
                {
                    "id": manifest_node,
                    "type": "manifest",
                    "name": item.path,
                    "attributes": {"language": item.language},
                    "evidence_ids": manifest_evidence,
                }
            )
            relationships.append(
                {
                    "id": relationship_id("contains", repo_node, manifest_node),
                    "type": "contains",
                    "source": repo_node,
                    "target": manifest_node,
                    "classification": "observed",
                    "confidence": 1.0,
                    "evidence_ids": manifest_evidence,
                }
            )
            text = f"Repository {repository.id} contains manifest {item.path}."
            claims.append(
                {
                    "id": claim_id(text, manifest_evidence),
                    "text": text,
                    "classification": "observed",
                    "confidence": 1.0,
                    "evidence_ids": manifest_evidence,
                }
            )
            for dependency in _manifest_dependencies(root / item.path):
                dependency_node = node_id("dependency", repository.id, dependency)
                nodes.append(
                    {
                        "id": dependency_node,
                        "type": "dependency",
                        "name": dependency,
                        "attributes": {"source_manifest": item.path},
                        "evidence_ids": manifest_evidence,
                    }
                )
                relationships.append(
                    {
                        "id": relationship_id("depends_on", repo_node, dependency_node),
                        "type": "depends_on",
                        "source": repo_node,
                        "target": dependency_node,
                        "classification": "observed",
                        "confidence": 1.0,
                        "evidence_ids": manifest_evidence,
                    }
                )
        snapshots.append(
            {
                "repository_id": repository.id,
                "path": repository.path,
                "role": repository.role,
                **header,
                "files": [
                    {
                        "path": item.path,
                        "size": item.size,
                        "content_hash": item.content_hash,
                        "language": item.language,
                    }
                    for item in files
                ],
            }
        )
    return build_ir(
        project_id=config.project_id,
        project_name=config.project_name,
        snapshot_mode=mode,
        snapshots=snapshots,
        nodes=nodes,
        relationships=relationships,
        evidence=evidence,
        claims=claims,
    ), sorted(set(warnings))
