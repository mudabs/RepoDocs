"""Project configuration, repository roles, and the documentation destination."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import slugify, write_json
from .errors import ConfigError


CONFIG_FILENAME = "repodocs.json"
INTERNAL_DIRECTORY = ".repodocs"
DEFAULT_OUTPUT_DIRECTORY = "docs/generated"
CONFIG_SCHEMA_VERSION = "1.0"

ROLE_SOURCE = "source"
ROLE_DESTINATION = "destination"
REPOSITORY_ROLES = (ROLE_SOURCE, ROLE_DESTINATION)

AI_PROVIDER_NONE = "none"
AI_PROVIDER_ANTHROPIC = "anthropic"
AI_PROVIDER_OLLAMA = "ollama"
AI_PROVIDER_OPENAI = "openai"
AI_PROVIDERS = (AI_PROVIDER_NONE, AI_PROVIDER_ANTHROPIC, AI_PROVIDER_OLLAMA, AI_PROVIDER_OPENAI)

FULL_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def validate_pinned_revision(value: str, *, context: str) -> str:
    """Accept only a full, lowercase 40-character Git commit SHA."""

    candidate = value.strip()
    if not FULL_REVISION_PATTERN.match(candidate):
        raise ConfigError(
            f"{context}: pinned revision must be a full 40-character lowercase commit SHA, got {value!r}"
        )
    return candidate


@dataclass(frozen=True)
class RepositorySpec:
    id: str
    path: str
    role: str = ROLE_SOURCE
    pinned_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "path": self.path, "role": self.role}
        if self.pinned_revision:
            data["pinned_revision"] = self.pinned_revision
        return data


# Upper bounds for one architecture-discovery run (ADR-026). A project may lower them, never raise them.
ARCHITECTURE_BUDGET_DEFAULTS: dict[str, int] = {
    "max_rounds": 3,
    "max_requests": 6,
    "max_files_per_round": 8,
    "max_excerpt_bytes": 8 * 1024,
    "max_file_bytes": 64 * 1024,
    "max_context_bytes": 200 * 1024,
    "max_response_bytes": 32 * 1024,
    "max_inventory_paths": 500,
}
# The version of the source-excerpt selection and redaction policy that consent is given for.
SOURCE_POLICY_VERSION = "1.0"


@dataclass(frozen=True)
class ArchitectureSettings:
    """Optional limits for architecture discovery: lower budgets and extra denied file names."""

    budgets: dict[str, int] = field(default_factory=dict)
    deny_names: tuple[str, ...] = ()
    html_export: bool = False  # also write the self-contained architecture-view.html (needs the optional Mermaid bundle)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.budgets:
            data["budgets"] = dict(sorted(self.budgets.items()))
        if self.deny_names:
            data["deny_names"] = list(self.deny_names)
        if self.html_export:
            data["html_export"] = True
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "ArchitectureSettings":
        if not isinstance(data, dict):
            raise ConfigError("architecture must be a JSON object")
        unknown = sorted(set(data) - {"budgets", "deny_names", "html_export"})
        if unknown:
            raise ConfigError("Unknown architecture setting: " + ", ".join(unknown))
        budgets = data.get("budgets", {})
        if not isinstance(budgets, dict):
            raise ConfigError("architecture.budgets must be a JSON object")
        for name, value in budgets.items():
            if name not in ARCHITECTURE_BUDGET_DEFAULTS:
                raise ConfigError(f"Unknown architecture budget: {name}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"architecture.budgets.{name} must be a positive integer")
            if value > ARCHITECTURE_BUDGET_DEFAULTS[name]:
                raise ConfigError(
                    f"architecture.budgets.{name} may only lower the default of {ARCHITECTURE_BUDGET_DEFAULTS[name]}"
                )
        names = data.get("deny_names", [])
        if not isinstance(names, list) or not all(isinstance(item, str) and item for item in names):
            raise ConfigError("architecture.deny_names must be a list of non-empty strings")
        export = data.get("html_export", False)
        if not isinstance(export, bool):
            raise ConfigError("architecture.html_export must be true or false")
        return cls(budgets=dict(budgets), deny_names=tuple(names), html_export=export)


@dataclass(frozen=True)
class AiSettings:
    """Optional AI interpretation. The default is disabled and local-only.

    Credentials are never stored here; providers read them from the environment.
    """

    provider: str = AI_PROVIDER_NONE
    model: str | None = None
    external_consent: bool = False
    ollama_host: str | None = None
    # Consent to send bounded source excerpts (architecture discovery) to an external provider. It is a
    # separate decision from external_consent and names the policy version it was given for.
    source_excerpts_consent: bool = False
    source_consent_policy: str | None = None
    openai_base_url: str | None = None

    @property
    def source_consent_current(self) -> bool:
        return self.source_excerpts_consent and self.source_consent_policy == SOURCE_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"provider": self.provider, "external_consent": self.external_consent}
        if self.source_excerpts_consent:
            data["source_excerpts_consent"] = True
            data["source_consent_policy"] = self.source_consent_policy or SOURCE_POLICY_VERSION
        if self.model:
            data["model"] = self.model
        if self.ollama_host:
            data["ollama_host"] = self.ollama_host
        if self.openai_base_url:
            data["openai_base_url"] = self.openai_base_url
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "AiSettings":
        if not isinstance(data, dict):
            raise ConfigError("ai must be a JSON object")
        provider = data.get("provider", AI_PROVIDER_NONE)
        if provider not in AI_PROVIDERS:
            raise ConfigError(f"Unsupported ai.provider {provider!r}; expected one of: " + ", ".join(AI_PROVIDERS))
        consent = data.get("external_consent", False)
        if not isinstance(consent, bool):
            raise ConfigError("ai.external_consent must be true or false")
        for key in ("model", "ollama_host", "openai_base_url"):
            if data.get(key) is not None and (not isinstance(data[key], str) or not data[key]):
                raise ConfigError(f"ai.{key} must be a non-empty string")
        source = data.get("source_excerpts_consent", False)
        if not isinstance(source, bool):
            raise ConfigError("ai.source_excerpts_consent must be true or false")
        policy = data.get("source_consent_policy")
        if policy is not None and (not isinstance(policy, str) or not policy):
            raise ConfigError("ai.source_consent_policy must be a non-empty string")
        return cls(
            provider=provider, model=data.get("model"), external_consent=consent, ollama_host=data.get("ollama_host"),
            source_excerpts_consent=source, source_consent_policy=policy if source else None,
            openai_base_url=data.get("openai_base_url"),
        )


@dataclass
class ProjectConfig:
    root: Path
    project_id: str
    project_name: str
    repositories: list[RepositorySpec]
    output_directory: str = DEFAULT_OUTPUT_DIRECTORY
    destination_repository_id: str | None = None
    ai: AiSettings = AiSettings()
    architecture: ArchitectureSettings = ArchitectureSettings()

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def internal_path(self) -> Path:
        return self.root / INTERNAL_DIRECTORY

    def repository(self, repository_id: str) -> RepositorySpec:
        for spec in self.repositories:
            if spec.id == repository_id:
                return spec
        raise ConfigError(f"Unknown repository ID: {repository_id}")

    def source_repositories(self) -> list[RepositorySpec]:
        """Repositories that are scanned into the System IR, in stable order."""

        return sorted((spec for spec in self.repositories if spec.role == ROLE_SOURCE), key=lambda item: item.id)

    def destination(self) -> RepositorySpec:
        """The repository that receives generated system documentation."""

        if not self.destination_repository_id:
            raise ConfigError(
                "No documentation destination is configured; register one with "
                "`repodocs add <path> --role destination` or mark a source repository with "
                "`repodocs add <path> --destination`"
            )
        try:
            return self.repository(self.destination_repository_id)
        except ConfigError as exc:
            raise ConfigError(
                f"Documentation destination references an unknown repository: {self.destination_repository_id}"
            ) from exc

    def resolve_repository(self, spec: RepositorySpec) -> Path:
        path = Path(spec.path)
        return (path if path.is_absolute() else self.root / path).resolve()

    def destination_root(self) -> Path:
        spec = self.destination()
        root = self.resolve_repository(spec)
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"Documentation destination path is not a directory: {spec.id} -> {root.as_posix()}")
        return root

    @property
    def output_path(self) -> Path:
        """Generated output directory, which must stay inside the destination repository."""

        destination_root = self.destination_root()
        output = Path(self.output_directory)
        if output.is_absolute():
            raise ConfigError(
                f"documentation.output_directory must be relative to the destination repository: {self.output_directory}"
            )
        resolved = (destination_root / output).resolve()
        try:
            resolved.relative_to(destination_root)
        except ValueError as exc:
            raise ConfigError(
                f"documentation.output_directory escapes the destination repository: {self.output_directory}"
            ) from exc
        if resolved == destination_root:
            raise ConfigError("documentation.output_directory must be a dedicated subdirectory, not the repository root")
        return resolved

    def set_destination(self, repository_id: str, *, replace: bool = False) -> None:
        """Make a registered repository the documentation destination.

        A destination-only repository must remain the destination while it is registered, so
        switching away from it requires removing it from the configuration first.
        """

        self.repository(repository_id)
        destination_only = sorted(
            spec.id for spec in self.repositories if spec.role == ROLE_DESTINATION and spec.id != repository_id
        )
        if destination_only:
            raise ConfigError(
                f"Repository {destination_only[0]} has role destination and must remain the destination; "
                f"remove it from {CONFIG_FILENAME} before choosing {repository_id}"
            )
        if (
            self.destination_repository_id
            and self.destination_repository_id != repository_id
            and not replace
        ):
            raise ConfigError(
                f"A documentation destination is already configured: {self.destination_repository_id}; "
                "pass --replace to switch"
            )
        self.destination_repository_id = repository_id

    def to_dict(self) -> dict[str, Any]:
        documentation: dict[str, Any] = {"output_directory": self.output_directory}
        if self.destination_repository_id:
            documentation["destination"] = {"repository_id": self.destination_repository_id}
        data: dict[str, Any] = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "project": {"id": self.project_id, "name": self.project_name},
            "repositories": [spec.to_dict() for spec in sorted(self.repositories, key=lambda item: item.id)],
            "documentation": documentation,
        }
        if self.ai != AiSettings():
            data["ai"] = self.ai.to_dict()
        if self.architecture.to_dict():
            data["architecture"] = self.architecture.to_dict()
        return data

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
        if not isinstance(data, dict):
            raise ConfigError("Project configuration must be a JSON object")
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
            role = str(item.get("role", ROLE_SOURCE))
            if role not in REPOSITORY_ROLES:
                raise ConfigError(
                    f"Repository {item['id']} has unsupported role {role!r}; expected one of: "
                    + ", ".join(REPOSITORY_ROLES)
                )
            pinned = item.get("pinned_revision")
            if pinned is not None:
                pinned = validate_pinned_revision(str(pinned), context=f"Repository {item['id']}")
            repositories.append(
                RepositorySpec(id=str(item["id"]), path=str(item["path"]), role=role, pinned_revision=pinned)
            )
        if len({spec.id for spec in repositories}) != len(repositories):
            raise ConfigError("Repository IDs must be unique")
        documentation = data.get("documentation", {})
        if not isinstance(documentation, dict):
            raise ConfigError("documentation must be a JSON object")
        output_directory = str(documentation.get("output_directory", DEFAULT_OUTPUT_DIRECTORY))
        destination_id: str | None = None
        destination = documentation.get("destination")
        if destination is not None:
            if not isinstance(destination, dict) or not destination.get("repository_id"):
                raise ConfigError("documentation.destination must contain repository_id")
            destination_id = str(destination["repository_id"])
            if destination_id not in {spec.id for spec in repositories}:
                raise ConfigError(f"Documentation destination references an unknown repository: {destination_id}")
        destination_only = sorted(spec.id for spec in repositories if spec.role == ROLE_DESTINATION)
        if len(destination_only) > 1:
            raise ConfigError(
                "Only one destination-only repository is allowed; found: " + ", ".join(destination_only)
            )
        if destination_only and destination_id != destination_only[0]:
            raise ConfigError(
                f"Repository {destination_only[0]} has role destination but documentation.destination is "
                f"{destination_id!r}"
            )
        return cls(
            root=root,
            project_id=str(project["id"]),
            project_name=str(project["name"]),
            repositories=repositories,
            output_directory=output_directory,
            destination_repository_id=destination_id,
            ai=AiSettings.from_dict(data["ai"]) if "ai" in data else AiSettings(),
            architecture=ArchitectureSettings.from_dict(data["architecture"]) if "architecture" in data else ArchitectureSettings(),
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
