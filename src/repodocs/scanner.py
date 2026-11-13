"""Safe, deterministic static discovery and basic manifest analysis."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import sha256_bytes, sha256_text, semantic_id
from .config import ProjectConfig, RepositorySpec
from .errors import ScanError
from .ir import build_ir, claim_id, evidence_id, node_id, relationship_id


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


def discover_files(root: Path) -> tuple[list[DiscoveredFile], list[str], int]:
    root = root.resolve()
    if not root.exists() or not root.is_dir():
        raise ScanError(f"Repository path is not a directory: {root}")
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
                if entry.name.lower() not in EXCLUDED_DIRECTORIES:
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


def _git_context(root: Path) -> dict[str, Any]:
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    top_level = run("rev-parse", "--show-toplevel")
    if top_level.returncode != 0:
        return {
            "revision_kind": "content",
            "git_revision": None,
            "working_tree_state": "untracked",
            "git_status_fingerprint": None,
        }
    revision_result = run("rev-parse", "HEAD")
    status_result = run("status", "--porcelain=v1", "--untracked-files=all", "--")
    status = status_result.stdout
    return {
        "revision_kind": "git-working-tree",
        "git_revision": revision_result.stdout.strip() if revision_result.returncode == 0 else None,
        "working_tree_state": "dirty" if status else "clean",
        "git_status_fingerprint": sha256_text(status) if status else None,
    }


def _manifest_dependencies(path: Path, relative_path: str) -> list[str]:
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


def scan_project(config: ProjectConfig) -> tuple[dict[str, Any], list[str]]:
    snapshots: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = [
        {
            "id": node_id("project", config.project_id),
            "type": "project",
            "name": config.project_name,
            "attributes": {},
            "evidence_ids": [],
        }
    ]
    relationships: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    warnings: list[str] = []

    for repository in sorted(config.repositories, key=lambda item: item.id):
        root = config.resolve_repository(repository)
        files, file_warnings, total_bytes = discover_files(root)
        warnings.extend(f"{repository.id}: {warning}" for warning in file_warnings)
        git = _git_context(root)
        content_fingerprint = sha256_text(
            "\n".join(f"{item.path}\0{item.content_hash}" for item in files)
        )
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
                    "path": root.as_posix(),
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
                "id": relationship_id("contains", node_id("project", config.project_id), repo_node),
                "type": "contains",
                "source": node_id("project", config.project_id),
                "target": repo_node,
                "classification": "observed",
                "confidence": 1.0,
                "evidence_ids": [],
            }
        )
        for item in files:
            ev_id = evidence_id(repository.id, item.path, item.content_hash)
            evidence.append(
                {
                    "id": ev_id,
                    "repository_id": repository.id,
                    "path": item.path,
                    "content_hash": item.content_hash,
                    "revision": git["git_revision"] or content_fingerprint,
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
                {
                    "id": language_node,
                    "type": "language",
                    "name": language,
                    "attributes": {},
                    "evidence_ids": language_evidence,
                }
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
            for dependency in _manifest_dependencies(root / item.path, item.path):
                dependency_node = node_id("dependency", repository.id, dependency)
                dependency_evidence = manifest_evidence
                nodes.append(
                    {
                        "id": dependency_node,
                        "type": "dependency",
                        "name": dependency,
                        "attributes": {"source_manifest": item.path},
                        "evidence_ids": dependency_evidence,
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
                        "evidence_ids": dependency_evidence,
                    }
                )
        snapshots.append(
            {
                "repository_id": repository.id,
                "path": root.as_posix(),
                **git,
                "content_fingerprint": content_fingerprint,
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
        snapshots=snapshots,
        nodes=nodes,
        relationships=relationships,
        evidence=evidence,
        claims=claims,
    ), sorted(set(warnings))
