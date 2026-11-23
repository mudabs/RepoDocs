"""Bounded, redacted source context for architecture discovery (ADR-026).

The context is built only from files in the scan's snapshot, read through the same hash check the
analyzers use. Nothing here executes, imports, or builds repository code, follows links, or reaches
the network. Repository text is data: it is never interpreted, only redacted and bounded.

Selection (ADR-029) ranks substantive source by role and size, spreads the first round across
repositories and across backend, frontend and storage files, and picks coherent spans of long files.
The inventory, the reference index, the diagnostics and a reserve for the instructions count against
the same total budget as the excerpts. References come from static imports in opened files only.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from ..ai.bundle import REDACTION_RULES_VERSION, redact_text
from ..common import canonical_json, sha256_bytes, sha256_text
from ..config import ARCHITECTURE_BUDGET_DEFAULTS, ProjectConfig
from ..ir import ir_hash
from .source_index import (
    CODE_SUFFIXES, CONFIG_CANDIDATE_SUFFIXES, LAYERS, ROLE_TOKENS, SUPPORTED_SUFFIXES, choose_span as pick_span,
    extract_references, is_asset, is_noise, is_vendored, java_targets, layer_of, path_tokens, resolve_reference,
)


CONTEXT_VERSION = "1.0"
SELECTION_VERSION = "1.1"
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
README_MAX_LINES = 40
MANIFEST_MAX_LINES = 80
MIN_SUBSTANTIVE_BYTES = 350  # about ten lines; smaller files rank after every substantive file
INVENTORY_FILES_PER_DIRECTORY = 12
INSTRUCTION_RESERVE_BYTES = 6 * 1024  # the system prompt, the response schema and the message envelope of discovery
MAX_OMITTED = 100
MAX_UNOPENED = 5
CONTAINER_NAMES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}
INDEX_NOTE = (
    "Static import statements found in the opened files of JavaScript, TypeScript, Python and Java. "
    "They show what a file names, not runtime order, calls, or data flow, and nothing about unopened files."
)


def is_denied(path: str, extra: tuple[str, ...] = ()) -> bool:
    """Whether a path is a name or place the context never reads: secrets, keys, locks, version control."""

    parts = PurePosixPath(path).parts
    if any(part.lower() in DENIED_DIRECTORIES for part in parts[:-1]):
        return True
    name = parts[-1].lower() if parts else ""
    return any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in DENIED_NAMES + tuple(extra))


def well_formed_path(path: str) -> bool:
    """A repository-relative POSIX path with no traversal, drive, or control characters."""

    if not isinstance(path, str) or not path or path != path.strip() or "\\" in path or ":" in path or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/")) and all(ord(char) >= 32 for char in path)



class ContextResolver:
    """Selects and serves bounded, redacted excerpts of one scan's snapshot."""

    def __init__(self, config: ProjectConfig, ir: dict[str, Any]) -> None:
        settings = config.architecture
        self.budgets = {**ARCHITECTURE_BUDGET_DEFAULTS, **settings.budgets}
        self.extra_deny = settings.deny_names
        self.ir = ir
        self.repositories: dict[str, tuple[Path, dict[str, dict[str, Any]]]] = {}
        for snapshot in ir["snapshots"]:
            root = config.resolve_repository(config.repository(snapshot["repository_id"]))
            self.repositories[snapshot["repository_id"]] = (root, {item["path"]: item for item in snapshot["files"]})
        self.documents: dict[str, dict[str, Any]] = {}
        # Per opened file: (line, redacted specifier, redactions, resolved target), resolved once when the file is read.
        self.references: dict[tuple[str, str], list[tuple[int, str, int, str | None]]] = {}
        self._java: dict[str, dict[str, str]] = {}
        self.omitted: dict[tuple[str, str, str], dict[str, str]] = {}
        self.context_bytes = 0
        self.replacements = 0
        self.rounds = 0
        self.requests = 0
        self._round_files = 0
        self._full = False
        self._pools: dict[str, dict[str, list[tuple[tuple[int, int, str], str]]]] = {}
        self._inventory: list[dict[str, Any]] | None = None
        self._paths = {repository_id: set(files) for repository_id, (_, files) in self.repositories.items()}
        self._denied = {repository_id: sorted(path for path in files if self.is_denied(path)) for repository_id, (_, files) in self.repositories.items()}

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
        return is_denied(path, self.extra_deny)

    well_formed = staticmethod(lambda path: well_formed_path(path))

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

    def fetch(
        self, repository_id: str, path: str, line_start: int = 1, line_end: int | None = None, *, kind: str | None = None,
        choose_span: bool = False,
    ) -> str | None:
        """Add one excerpt and return its ID, or record why it was refused and return None.

        ``choose_span`` lets the resolver pick the span: a short head of a README or manifest, or the
        block-aligned window of a long source file that holds the most definitions and routes.
        """

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
        kind = kind or kind_of(path)
        if choose_span:
            if kind == "source":
                line_start, line_end = pick_span(lines, MAX_LINES, self.budgets["max_excerpt_bytes"])
            else:
                line_start, line_end = 1, README_MAX_LINES if kind == "readme" else MANIFEST_MAX_LINES
        start = max(1, int(line_start))
        end = min(len(lines), int(line_end) if line_end else start + MAX_LINES - 1, start + MAX_LINES - 1)
        if start > max(len(lines), 1):
            start, end = 1, min(len(lines), MAX_LINES)
        selected = lines[start - 1 : end]
        spanned = bool(selected)
        while selected and len("\n".join(selected).encode("utf-8")) > self.budgets["max_excerpt_bytes"]:
            selected.pop()
        if spanned and not selected:
            # Not one line of the span fits the excerpt limit: nothing would be shown, so nothing is opened.
            self._omit(repository_id, path, "too-large")
            return None
        end = start + len(selected) - 1 if selected else start
        identity = sha256_text(f"{repository_id}|{path}|{entry['content_hash']}|{start}|{end}")[:16]
        document_id = f"ctx:{identity}"
        if document_id in self.documents:
            self._omit(repository_id, path, "duplicate")
            return None
        text, count = redact_text("\n".join(selected) + ("\n" if selected else ""))
        key = (repository_id, path)
        # The whole file is parsed, so a declaration spanning the excerpt boundary is read correctly; only references on
        # excerpted lines are sent (see index()).
        indexed = key not in self.references and PurePosixPath(path).suffix.lower() in SUPPORTED_SUFFIXES
        document = {
            "id": document_id, "repository_id": repository_id, "path": path, "content_hash": entry["content_hash"],
            "kind": kind, "line_start": start, "line_end": end, "text": text, "redactions": count,
            "truncated": end < len(lines), "requested_in_round": self.rounds,
        }
        size = sent_size(document)
        self.context_bytes += size
        self.documents[document_id] = document
        if indexed:
            self.references[key] = self._resolve(repository_id, path, lines)
        if self.total_bytes() > self.budgets["max_context_bytes"]:
            self.context_bytes -= size
            del self.documents[document_id]
            if indexed:
                del self.references[key]
            self._omit(repository_id, path, "budget-exhausted")
            self._full = True
            return None
        self.replacements += count
        self._round_files += 1
        return document_id

    def _resolve(self, repository_id: str, path: str, lines: list[str]) -> list[tuple[int, str, int, str | None]]:
        paths = self._paths[repository_id]
        if repository_id not in self._java:
            self._java[repository_id] = java_targets(paths)
        resolved = []
        for line, specifier, names in extract_references(path, lines):
            redacted, count = redact_text(specifier)
            target = resolve_reference(path, redacted, names, paths, self._java[repository_id])
            resolved.append((line, redacted, count, None if target is not None and self.is_denied(target) else target))
        return resolved

    # -------------------------------------------------------------- the first round
    def _pool(self, repository_id: str) -> dict[str, list[tuple[tuple[int, int, str], str]]]:
        """Substantive source and config files of a repository by layer, best first, as ``(sort key, path)``."""

        if repository_id in self._pools:
            return self._pools[repository_id]
        pool: dict[str, list[tuple[tuple[int, int, str], str]]] = {layer: [] for layer in LAYERS}
        for path, entry in self.repositories[repository_id][1].items():
            name = path.rsplit("/", 1)[-1].lower()
            suffix = PurePosixPath(name).suffix
            if (
                self.is_denied(path) or is_asset(path) or is_vendored(path) or name in README_NAMES or name in MANIFEST_NAMES
                or suffix not in CODE_SUFFIXES | CONFIG_CANDIDATE_SUFFIXES
            ):
                continue
            entry_point = name in ENTRY_NAMES or name.endswith("application.java")
            role = bool(path_tokens(path) & ROLE_TOKENS)
            if entry["size"] > self.budgets["max_file_bytes"]:
                if entry_point or role:
                    self._omit(repository_id, path, "too-large")
                continue
            if entry["size"] == 0 or (entry["size"] < MIN_SUBSTANTIVE_BYTES and not entry_point):
                continue
            # A tiny entry point is usually a re-export or wrapper: it is kept for a small repository but follows every substantive file.
            score = min(entry["size"], 6144) // 96 + 40 * entry_point + 25 * role - 45 * is_noise(path) - 20 * (suffix in CONFIG_CANDIDATE_SUFFIXES)
            score -= 1000 * (entry["size"] < MIN_SUBSTANTIVE_BYTES)
            pool[layer_of(path)].append(((-score, path.count("/"), path), path))
        self._pools[repository_id] = {layer: sorted(items) for layer, items in pool.items()}
        return self._pools[repository_id]

    def _queue(self, repository_id: str) -> list[str]:
        """The order in which one repository's files are requested in the first round.

        One language manifest and the best source file come first, then the README (a short head), then
        the layers in turn so that a large backend cannot crowd out the frontend or storage, and last the
        remaining manifests, with container files after language manifests.
        """

        files = sorted(self.repositories[repository_id][1], key=lambda item: (item.count("/"), item))
        names = [(path, path.rsplit("/", 1)[-1].lower()) for path in files if not self.is_denied(path)]
        manifests = [path for path, name in names if name in MANIFEST_NAMES]
        manifests.sort(key=lambda path: path.rsplit("/", 1)[-1].lower() in CONTAINER_NAMES)
        readme = [path for path, name in names if name in README_NAMES][:1]
        lanes = {layer: [path for _, path in items] for layer, items in self._pool(repository_id).items()}
        heads = [items[0] for items in self._pool(repository_id).values() if items]
        queue = manifests[:1]
        if heads:
            best = min(heads)[1]
            lanes[layer_of(best)].remove(best)
            queue.append(best)
        queue += readme
        while any(lanes.values()):
            for layer in LAYERS:
                if lanes[layer]:
                    queue.append(lanes[layer].pop(0))
        return queue + manifests[1:]

    def select_initial(self) -> None:
        """The start of round one: a fair, layered share of every repository within the round and total budgets."""

        self.start_round()
        queues = {repository_id: self._queue(repository_id) for repository_id in sorted(self.repositories)}
        limit = self.budgets["max_files_per_round"]
        while any(queues.values()) and self._round_files < limit and not self._full:
            for repository_id, queue in queues.items():  # one file per repository in turn
                while queue and self._round_files < limit and not self._full:
                    if self.fetch(repository_id, queue.pop(0), choose_span=True):
                        break

    # -------------------------------------------------------------- the bundle
    def inventory(self) -> list[dict[str, Any]]:
        """Names of directories and files, with assets and crowded directories summarized as counts.

        The listing is cut by path count and by its share of the total budget; shallow paths survive.
        """

        if self._inventory is not None:
            return self._inventory
        limit = self.budgets["max_inventory_paths"]
        share = self.budgets["max_context_bytes"] // 5 // max(1, len(self.repositories))
        result = []
        for repository_id in sorted(self.repositories):
            paths = [path for path in self.repositories[repository_id][1] if not self.is_denied(path)]
            assets: dict[str, int] = {}
            by_directory: dict[str, list[str]] = {}
            for path in sorted(paths, key=lambda item: (item.count("/"), item)):
                directory = path.rsplit("/", 1)[0] if "/" in path else ""
                if is_asset(path):
                    assets[directory] = assets.get(directory, 0) + 1
                else:
                    by_directory.setdefault(directory, []).append(path)
            depth = lambda name: (name.rstrip("/").count("/"), name)  # noqa: E731 - shallow entries first, then by name
            more = [
                {"directory": directory, "count": len(members) - INVENTORY_FILES_PER_DIRECTORY}
                for directory, members in sorted(by_directory.items(), key=lambda pair: depth(pair[0]))
                if len(members) > INVENTORY_FILES_PER_DIRECTORY
            ]
            files = [path for members in by_directory.values() for path in members[:INVENTORY_FILES_PER_DIRECTORY]]
            directories = {parent + "/" for path in files for parent in _parents(path)}
            listing = sorted([*directories, *files], key=depth)
            item = {
                "repository_id": repository_id, "paths": listing[:limit], "truncated": len(listing) > limit,
                "more_files": more,
                "assets": [{"directory": directory, "count": count} for directory, count in sorted(assets.items(), key=lambda pair: depth(pair[0]))],
            }
            # Every list is cut, longest first, until the inventory fits its share; the shallowest entries survive.
            while sent_size(item) > share and (item["paths"] or item["more_files"] or item["assets"]):
                longest = max(("paths", "more_files", "assets"), key=lambda name: len(item[name]))
                item[longest] = item[longest][: len(item[longest]) * 3 // 4]
                item["truncated"] = True
            result.append(item)
        self._inventory = result
        return result

    def _status(self) -> dict[tuple[str, str], str]:
        """``read`` for a file whose excerpt is the whole file, ``excerpted`` for a span of it."""

        status: dict[tuple[str, str], str] = {}
        for document in self.documents.values():
            key = (document["repository_id"], document["path"])
            whole = document["line_start"] == 1 and not document["truncated"]
            status[key] = "read" if whole or status.get(key) == "read" else "excerpted"
        return status

    def index(self) -> tuple[dict[str, Any], int]:
        """Static references of the opened files, and how many redactions the specifiers needed."""

        limit = self.budgets["max_context_bytes"] // 8
        status = self._status()
        spans: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for document in self.documents.values():
            spans.setdefault((document["repository_id"], document["path"]), []).append((document["line_start"], document["line_end"]))
        used, redactions, truncated = len(INDEX_NOTE), 0, False
        files = []
        for repository_id, path in sorted(self.references):
            references = []
            for line, specifier, count, target in self.references[(repository_id, path)]:
                # Only references on lines the model is shown: nothing outside a selected span is disclosed (ADR-026).
                if not any(start <= line <= end for start, end in spans.get((repository_id, path), ())):
                    continue
                references.append({
                    "line": line, "specifier": specifier, "target": target,
                    "status": "unresolved" if target is None else status.get((repository_id, target), "unopened"),
                })
                redactions += count
            entry = {
                "repository_id": repository_id, "path": path,
                "content_hash": self.repositories[repository_id][1][path]["content_hash"], "references": references,
            }
            while entry["references"] and used + len(canonical_json(entry).encode("utf-8")) > limit:
                entry["references"].pop()
                truncated = True
            if entry["references"]:
                used += len(canonical_json(entry).encode("utf-8"))
                files.append(entry)
        return {"note": INDEX_NOTE, "files": files, "truncated": truncated}, redactions

    def coverage(self) -> list[dict[str, Any]]:
        """Per repository: what was opened, which layers have files but none opened, and the best unopened files."""

        opened: dict[str, set[str]] = {}
        for document in self.documents.values():
            opened.setdefault(document["repository_id"], set()).add(document["path"])
        result = []
        for repository_id in sorted(self.repositories):
            seen = opened.get(repository_id, set())
            pool = self._pool(repository_id)
            layers = [
                {"layer": layer, "files": len(items), "opened": sum(1 for _, path in items if path in seen)}
                for layer, items in pool.items() if items
            ]
            unopened = sorted((key, path) for items in pool.values() for key, path in items if path not in seen)
            result.append({
                "repository_id": repository_id, "candidate_files": sum(item["files"] for item in layers), "opened_files": len(seen),
                "layers": layers, "missing_layers": [item["layer"] for item in layers if item["layer"] != "other" and not item["opened"]],
                "unopened_priority": [path for _, path in unopened[:MAX_UNOPENED]],
            })
        return result

    def omitted_view(self) -> tuple[list[dict[str, str]], dict[str, int], bool]:
        """Omissions with the material reasons first, each reason's total, and whether the list was cut."""

        entries = dict(self.omitted)
        for repository_id, denied in self._denied.items():
            for path in denied:
                entries[(repository_id, path, "denied-name")] = {"repository_id": repository_id, "path": path, "reason": "denied-name"}
        ordered = [entries[key] for key in sorted(entries, key=lambda key: (key[2] == "denied-name", key))]
        counts: dict[str, int] = {}
        for item in ordered:
            counts[item["reason"]] = counts.get(item["reason"], 0) + 1
        return ordered[:MAX_OMITTED], dict(sorted(counts.items())), len(ordered) > MAX_OMITTED

    def reserved_bytes(self) -> int:
        """Room kept for the instructions the provider receives with the context."""

        return min(INSTRUCTION_RESERVE_BYTES, self.budgets["max_context_bytes"] // 4)

    def metadata(self) -> dict[str, Any]:
        """Inventory, index, coverage and omissions.

        The omission list is the lowest priority: refusals keep being recorded after a document is admitted, so the
        list is cut, and says ``omitted_truncated``, whenever it would push the total past ``max_context_bytes``.
        ``omitted_counts`` always holds every reason's full count.
        """

        omitted, counts, cut = self.omitted_view()
        index, _ = self.index()
        result = {
            "inventory": self.inventory(), "index": index, "coverage": self.coverage(),
            "omitted": omitted, "omitted_counts": counts, "omitted_truncated": cut,
        }
        room = self.budgets["max_context_bytes"] - self.context_bytes - self.reserved_bytes()
        while result["omitted"] and sent_size(result) > room:
            result["omitted"] = result["omitted"][: len(result["omitted"]) * 3 // 4]
            result["omitted_truncated"] = True
        return result

    def total_bytes(self) -> int:
        """Everything counted against ``max_context_bytes``: excerpts, inventory, index, diagnostics and the instruction reserve.

        Excerpts and metadata are counted as they appear inside the request, encoded once as the user message and
        again as a string of the request body, so the total tracks what is actually sent.
        """

        return self.context_bytes + self.reserved_bytes() + sent_size(self.metadata())

    def bundle(self) -> dict[str, Any]:
        metadata = self.metadata()
        _, index_redactions = self.index()
        return {
            "context_version": CONTEXT_VERSION,
            "policy": {"selection_version": SELECTION_VERSION, "redaction_version": REDACTION_RULES_VERSION, "budgets": dict(self.budgets)},
            "source_ir_hash": ir_hash(self.ir),
            "snapshots": [
                {key: snapshot[key] for key in ("repository_id", "revision", "revision_kind", "content_fingerprint")}
                for snapshot in self.ir["snapshots"]
            ],
            **metadata,
            "documents": sorted(self.documents.values(), key=lambda item: (item["repository_id"], item["path"], item["line_start"])),
            "usage": {
                "rounds": self.rounds, "requests": self.requests, "context_bytes": self.context_bytes,
                "metadata_bytes": sent_size(metadata), "reserved_bytes": self.reserved_bytes(),
            },
            "redaction": {"rules_version": REDACTION_RULES_VERSION, "replacements": self.replacements + index_redactions},
        }


def sent_size(value: Any) -> int:
    """Bytes of ``value`` as it appears in a request: JSON inside the user message, which is itself a JSON string."""

    return len(json.dumps(canonical_json(value), ensure_ascii=False).encode("utf-8"))


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
