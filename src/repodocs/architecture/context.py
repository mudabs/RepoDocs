"""Bounded, redacted source context for architecture discovery (ADR-026).

The context is built only from files in the scan's snapshot, read through the same hash check the
analyzers use. Nothing here executes, imports, or builds repository code, follows links, or reaches
the network. Repository text is data: it is never interpreted, only redacted and bounded.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
from typing import Any

from ..ai.bundle import REDACTION_RULES_VERSION, redact_text
from ..common import canonical_json, sha256_bytes, sha256_text
from ..config import ARCHITECTURE_BUDGET_DEFAULTS, ProjectConfig
from ..ir import ir_hash


CONTEXT_VERSION = "1.0"
SELECTION_VERSION = "1.0"
CONTEXT_FILENAME = "architecture-context.json"

# Names that hold secrets or no architecture. A project may add names and can never remove these.
DENIED_NAMES = (
    ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials*", "secrets.*", "secret.*", ".npmrc", ".netrc", ".pypirc", "kubeconfig", "*.tfstate", "*.tfvars",
    ".git*", "*.lock", "package-lock.json", "npm-shrinkwrap.json", "*.min.js", "*.min.css", "*.map", "*.sqlite", "*.db",
)
DENIED_DIRECTORIES = {".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".repodocs"}

README_NAMES = {"readme", "readme.md", "readme.txt", "readme.rst", "readme.markdown"}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "requirements.txt", "pom.xml", "build.gradle", "build.gradle.kts", "go.mod",
    "cargo.toml", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
}
CONFIG_SUFFIXES = {".yml", ".yaml", ".json", ".toml", ".properties", ".xml", ".sql", ".ini", ".cfg"}
ENTRY_NAMES = {
    "server.js", "app.js", "index.js", "main.js", "server.ts", "app.ts", "index.ts", "main.ts", "main.py", "app.py",
    "manage.py", "wsgi.py", "asgi.py", "main.go", "main.rs", "program.cs", "index.html",
}
MAX_LINES = 200
BINARY_PROBE = 8192


class ContextResolver:
    """Selects and serves bounded, redacted excerpts of one scan's snapshot."""

    def __init__(self, config: ProjectConfig, ir: dict[str, Any]) -> None:
        settings = config.architecture
        self.budgets = {**ARCHITECTURE_BUDGET_DEFAULTS, **settings.budgets}
        self.denied = DENIED_NAMES + tuple(name.lower() for name in settings.deny_names)
        self.ir = ir
        self.repositories: dict[str, tuple[Path, dict[str, dict[str, Any]]]] = {}
        for snapshot in ir["snapshots"]:
            root = config.resolve_repository(config.repository(snapshot["repository_id"]))
            self.repositories[snapshot["repository_id"]] = (root, {item["path"]: item for item in snapshot["files"]})
        self.documents: dict[str, dict[str, Any]] = {}
        self.omitted: dict[tuple[str, str, str], dict[str, str]] = {}
        self.context_bytes = 0
        self.replacements = 0
        self.rounds = 0
        self.requests = 0
        self._round_files = 0

    # -------------------------------------------------------------- rounds and requests
    def start_round(self) -> bool:
        """Begin a round of file requests; False once the round budget is spent."""

        if self.rounds >= self.budgets["max_rounds"]:
            return False
        self.rounds += 1
        self._round_files = 0
        return True

    def count_request(self) -> bool:
        """Count one provider request; False once the request budget is spent."""

        if self.requests >= self.budgets["max_requests"]:
            return False
        self.requests += 1
        return True

    # -------------------------------------------------------------- reading
    def _omit(self, repository_id: str, path: str, reason: str) -> None:
        self.omitted[(repository_id, path, reason)] = {"repository_id": repository_id, "path": path, "reason": reason}

    def is_denied(self, path: str) -> bool:
        parts = PurePosixPath(path).parts
        if any(part.lower() in DENIED_DIRECTORIES for part in parts[:-1]):
            return True
        name = parts[-1].lower() if parts else ""
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.denied)

    @staticmethod
    def well_formed(path: str) -> bool:
        """A repository-relative POSIX path with no traversal, drive, or control characters."""

        if not path or path != path.strip() or "\\" in path or ":" in path or "\x00" in path or path.startswith("/"):
            return False
        return all(part not in ("", ".", "..") for part in path.split("/")) and all(ord(char) >= 32 for char in path)

    def _read(self, root: Path, relative: str) -> bytes | None:
        """The file's bytes, only when no path component is a link and it stays inside the repository.

        At most one byte more than ``max_file_bytes`` is read, so a file that grew after the scan is never buffered whole.
        """

        target = root / relative
        try:
            real = Path(os.path.realpath(target))
            real.relative_to(Path(os.path.realpath(root)))
        except (OSError, ValueError):
            return None
        current = root
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                return None
        try:
            with target.open("rb") as handle:
                return handle.read(self.budgets["max_file_bytes"] + 1)
        except OSError:
            return None

    def fetch(self, repository_id: str, path: str, line_start: int = 1, line_end: int | None = None, *, kind: str | None = None) -> str | None:
        """Add one excerpt and return its ID, or record why it was refused and return None."""

        if repository_id not in self.repositories:
            self._omit(repository_id, path, "not-in-snapshot")
            return None
        root, files = self.repositories[repository_id]
        if not self.well_formed(path):
            self._omit(repository_id, path[:200], "outside-repository")
            return None
        if self.is_denied(path):
            self._omit(repository_id, path, "denied-name")
            return None
        entry = files.get(path)
        if entry is None:
            self._omit(repository_id, path, "not-in-snapshot")
            return None
        if self._round_files >= self.budgets["max_files_per_round"]:
            self._omit(repository_id, path, "budget-exhausted")
            return None
        if entry["size"] > self.budgets["max_file_bytes"]:
            self._omit(repository_id, path, "too-large")
            return None
        data = self._read(root, path)
        if data is None:
            self._omit(repository_id, path, "outside-repository" if (root / path).is_symlink() else "unreadable")
            return None
        if len(data) > self.budgets["max_file_bytes"] or sha256_bytes(data) != entry["content_hash"]:
            self._omit(repository_id, path, "stale")
            return None
        if b"\x00" in data[:BINARY_PROBE]:
            self._omit(repository_id, path, "binary")
            return None
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            self._omit(repository_id, path, "not-text")
            return None
        start = max(1, int(line_start))
        end = min(len(lines), int(line_end) if line_end else start + MAX_LINES - 1, start + MAX_LINES - 1)
        if start > max(len(lines), 1):
            start, end = 1, min(len(lines), MAX_LINES)
        selected = lines[start - 1 : end]
        while selected and len("\n".join(selected).encode("utf-8")) > self.budgets["max_excerpt_bytes"]:
            selected.pop()
        end = start + len(selected) - 1 if selected else start
        identity = sha256_text(f"{repository_id}|{path}|{entry['content_hash']}|{start}|{end}")[:16]
        document_id = f"ctx:{identity}"
        if document_id in self.documents:
            self._omit(repository_id, path, "duplicate")
            return None
        text, count = redact_text("\n".join(selected) + ("\n" if selected else ""))
        size = len(text.encode("utf-8"))
        if self.context_bytes + size > self.budgets["max_context_bytes"]:
            self._omit(repository_id, path, "budget-exhausted")
            return None
        self.context_bytes += size
        self.replacements += count
        self._round_files += 1
        self.documents[document_id] = {
            "id": document_id, "repository_id": repository_id, "path": path, "content_hash": entry["content_hash"],
            "kind": kind or kind_of(path), "line_start": start, "line_end": end, "text": text, "redactions": count,
            "truncated": end < len(lines), "requested_in_round": self.rounds,
        }
        return document_id

    # -------------------------------------------------------------- the first round
    def select_initial(self) -> None:
        """README, manifests, and entry points of every repository: the start of round one."""

        self.start_round()
        for repository_id in sorted(self.repositories):
            files = sorted(self.repositories[repository_id][1], key=lambda item: (item.count("/"), item))
            names = [(path, path.rsplit("/", 1)[-1].lower()) for path in files if not self.is_denied(path)]
            chosen = [path for path, name in names if name in README_NAMES][:1]
            chosen += [path for path, name in names if name in MANIFEST_NAMES]
            chosen += [path for path, name in names if name in ENTRY_NAMES or name.endswith("application.java")]
            seen: set[str] = set()
            for path in chosen:
                if path not in seen:
                    seen.add(path)
                    self.fetch(repository_id, path)

    # -------------------------------------------------------------- the bundle
    def inventory(self) -> list[dict[str, Any]]:
        limit = self.budgets["max_inventory_paths"]
        result = []
        for repository_id in sorted(self.repositories):
            paths = [path for path in self.repositories[repository_id][1] if not self.is_denied(path)]
            directories = {parent + "/" for path in paths for parent in _parents(path)}
            listing = sorted(directories) + sorted(paths)
            result.append({"repository_id": repository_id, "paths": listing[:limit], "truncated": len(listing) > limit})
        return result

    def bundle(self) -> dict[str, Any]:
        for repository_id, (_, files) in sorted(self.repositories.items()):
            for path in sorted(files):
                if self.is_denied(path):
                    self._omit(repository_id, path, "denied-name")
        return {
            "context_version": CONTEXT_VERSION,
            "policy": {"selection_version": SELECTION_VERSION, "redaction_version": REDACTION_RULES_VERSION, "budgets": dict(self.budgets)},
            "source_ir_hash": ir_hash(self.ir),
            "snapshots": [
                {key: snapshot[key] for key in ("repository_id", "revision", "revision_kind", "content_fingerprint")}
                for snapshot in self.ir["snapshots"]
            ],
            "inventory": self.inventory(),
            "documents": sorted(self.documents.values(), key=lambda item: (item["repository_id"], item["path"], item["line_start"])),
            "omitted": [self.omitted[key] for key in sorted(self.omitted)],
            "usage": {"rounds": self.rounds, "requests": self.requests, "context_bytes": self.context_bytes},
            "redaction": {"rules_version": REDACTION_RULES_VERSION, "replacements": self.replacements},
        }


def _parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def kind_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name in README_NAMES:
        return "readme"
    if name in MANIFEST_NAMES:
        return "manifest"
    if PurePosixPath(name).suffix in CONFIG_SUFFIXES:
        return "config"
    return "source"


def context_hash(bundle: dict[str, Any]) -> str:
    return sha256_text(canonical_json(bundle))


def build_context(config: ProjectConfig, ir: dict[str, Any]) -> tuple[dict[str, Any], ContextResolver]:
    """The first-round context for a scan, and the resolver that serves later requests."""

    resolver = ContextResolver(config, ir)
    resolver.select_initial()
    return resolver.bundle(), resolver
