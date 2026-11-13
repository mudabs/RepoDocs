"""Project configuration and repository registration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import slugify, write_json
from .errors import ConfigError


CONFIG_FILENAME = "repodocs.json"
INTERNAL_DIRECTORY = ".repodocs"
DEFAULT_OUTPUT_DIRECTORY = "docs/generated"
CONFIG_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class RepositorySpec:
    id: str
    path: str
    role: str = "source"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "path": self.path, "role": self.role}


@dataclass
class ProjectConfig:
    root: Path
    project_id: str
    project_name: str
    repositories: list[RepositorySpec]
    output_directory: str = DEFAULT_OUTPUT_DIRECTORY

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def internal_path(self) -> Path:
        return self.root / INTERNAL_DIRECTORY

    @property
    def output_path(self) -> Path:
        output = Path(self.output_directory)
        return output if output.is_absolute() else self.root / output

    def resolve_repository(self, spec: RepositorySpec) -> Path:
        path = Path(spec.path)
        return (path if path.is_absolute() else self.root / path).resolve()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "project": {"id": self.project_id, "name": self.project_name},
            "repositories": [spec.to_dict() for spec in sorted(self.repositories, key=lambda item: item.id)],
            "documentation": {"output_directory": self.output_directory},
        }

    def save(self) -> None:
        write_json(self.config_path, self.to_dict())

    @classmethod
    def load(cls, root: Path) -> "ProjectConfig":
        root = root.resolve()
        path = root / CONFIG_FILENAME
        if not path.exists():
            raise ConfigError(f"Project configuration not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Unable to read project configuration: {path}: {exc}") from exc
        if data.get("schema_version") != CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"Unsupported configuration schema: {data.get('schema_version')!r}; expected {CONFIG_SCHEMA_VERSION}"
            )
        project = data.get("project")
        if not isinstance(project, dict) or not project.get("id") or not project.get("name"):
            raise ConfigError("Configuration must contain project.id and project.name")
        repositories: list[RepositorySpec] = []
        for item in data.get("repositories", []):
            if not isinstance(item, dict) or not item.get("id") or not item.get("path"):
                raise ConfigError("Each repository must contain id and path")
            repositories.append(
                RepositorySpec(
                    id=str(item["id"]),
                    path=str(item["path"]),
                    role=str(item.get("role", "source")),
                )
            )
        if len({spec.id for spec in repositories}) != len(repositories):
            raise ConfigError("Repository IDs must be unique")
        documentation = data.get("documentation", {})
        output_directory = str(documentation.get("output_directory", DEFAULT_OUTPUT_DIRECTORY))
        return cls(
            root=root,
            project_id=str(project["id"]),
            project_name=str(project["name"]),
            repositories=repositories,
            output_directory=output_directory,
        )


def create_project(root: Path, project_name: str, output_directory: str = DEFAULT_OUTPUT_DIRECTORY) -> ProjectConfig:
    root = root.resolve()
    return ProjectConfig(
        root=root,
        project_id=slugify(project_name),
        project_name=project_name,
        repositories=[],
        output_directory=output_directory,
    )


def stored_repository_path(root: Path, repository_path: Path) -> str:
    """Store a relative path when possible, otherwise a normalized absolute path."""

    root = root.resolve()
    repository_path = repository_path.resolve()
    try:
        return repository_path.relative_to(root).as_posix() or "."
    except ValueError:
        return repository_path.as_posix()
