"""Server-owned workspaces for local and public GitHub sources (ADR-034).

A browser names an allowlisted local root by its configured name, or a public GitHub ``owner/repo``
at one exact commit. It never supplies a path, a URL, a Git option, or an output location. The
backend creates one workspace directory under its store root holding ``repodocs.json``, the
documentation destination, and any fetched checkouts, and returns the ordinary ``ProjectConfig``
the shared engine already scans. No repository code runs: Git is started with a scrubbed
environment and every program-launching setting off, and nothing is built or installed.

Before Git downloads anything, a fixed ``api.github.com`` request lists the immutable tree of the
pinned commit, so file counts, blob sizes, paths, links and submodules are bounded first. Local
working trees are only read; they are never copied, modified, or deleted.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote, urlsplit

from ..common import scrub, slugify, write_json_atomic
from ..config import FULL_REVISION_PATTERN, ProjectConfig, RepositorySpec, ROLE_DESTINATION, ROLE_SOURCE, create_project
from ..errors import RepoDocsError, SnapshotError
from ..snapshot import SNAPSHOT_MODE_PINNED, GitState, read_git_state, resolve_snapshot


MARKER_NAME = ".repodocs-workspace.json"
MARKER_KIND = "repodocs-web-workspace"
MARKER_VERSION = "1.0"
WORKSPACE_ID = re.compile(r"ws-[0-9a-f]{32}")
DESTINATION_ID = "documentation"
DESTINATION_DIRECTORY = "destination"
SOURCES_DIRECTORY = "sources"

ROOT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}")
SAFE_MODES = {"100644", "100755"}
_POLL_SECONDS = 0.25


class WorkspaceError(RepoDocsError):
    """A source could not be acquired safely. ``failures`` lists every recorded cause."""

    def __init__(self, message: str, failures: Sequence["SourceFailure"] = ()) -> None:
        super().__init__(message)
        self.failures = tuple(failures)


@dataclass(frozen=True)
class SourceFailure:
    source: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "message": self.message}


@dataclass(frozen=True)
class SourceLimits:
    """Explicit resource bounds. Every value must be a positive integer."""

    network_timeout_seconds: int = 15
    max_response_bytes: int = 8 * 1024 * 1024
    max_tree_entries: int = 20_000
    max_files: int = 10_000
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    git_timeout_seconds: int = 120
    max_checkout_bytes: int = 300 * 1024 * 1024
    max_sources: int = 8
    max_workspaces: int = 20
    retention_seconds: int = 7 * 24 * 3600

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise WorkspaceError(f"Source limit {name} must be a positive integer")


@dataclass(frozen=True)
class WorkspaceSettings:
    """Backend configuration: where workspaces live and which local roots may be offered by name."""

    store_root: Path
    local_roots: dict[str, Path] = field(default_factory=dict)
    limits: SourceLimits = SourceLimits()

    def __post_init__(self) -> None:
        store = Path(self.store_root).resolve()
        roots: dict[str, Path] = {}
        for name, path in self.local_roots.items():
            if not ROOT_NAME.fullmatch(name):
                raise WorkspaceError(f"Local root name {name!r} must be lowercase letters, digits, '-' or '_'")
            resolved = Path(path).resolve()
            if resolved == store or store in resolved.parents or resolved in store.parents:
                raise WorkspaceError(f"Local root {name!r} must not contain or sit inside the workspace store")
            roots[name] = resolved
        object.__setattr__(self, "store_root", store)
        object.__setattr__(self, "local_roots", roots)


@dataclass(frozen=True)
class LocalSource:
    """An allowlisted local root, selected by its configured name only."""

    root_name: str


@dataclass(frozen=True)
class GitHubSource:
    """A public GitHub repository at one exact lowercase 40-character commit."""

    owner: str
    repository: str
    revision: str


@dataclass(frozen=True)
class SourceRecord:
    """What one participating source was. Local paths are never recorded, only the configured name."""

    id: str
    kind: str
    label: str
    requested_revision: str | None
    git: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "requested_revision": self.requested_revision,
            "git": self.git,
        }


@dataclass(frozen=True)
class Workspace:
    """A prepared workspace. ``config`` is the project configuration the shared engine consumes."""

    workspace_id: str
    root: Path
    config: ProjectConfig
    sources: tuple[SourceRecord, ...]


@dataclass(frozen=True)
class TreeSummary:
    tree_sha: str
    files: int
    total_bytes: int


HttpGet = Callable[[str, float, int], bytes]
RunGit = Callable[..., str]


def _describe_status(status: int) -> str:
    if status in (301, 302, 303, 307, 308):
        return "GitHub answered with a redirect, which is refused"
    if status == 404:
        return "the repository or commit was not found, or it is private (private repositories are not supported)"
    if status in (403, 429):
        return "GitHub refused or rate-limited the unauthenticated request; try again later"
    return f"GitHub answered with HTTP {status}"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def fetch_bounded(url: str, timeout: float, max_bytes: int) -> bytes:
    """GET an http(s) URL without proxies, credentials, or redirects, within a deadline and byte cap."""

    if not url.startswith(("https://", "http://")):
        raise WorkspaceError("Only http(s) URLs may be fetched")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "repodocs-workspace"}
    )
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    try:
        with opener.open(request, timeout=timeout) as response:
            while True:
                if time.monotonic() > deadline:
                    raise WorkspaceError(f"The network request exceeded {timeout:g} seconds")
                # A buffered read(n) may wait indefinitely for n bytes while a peer trickles data.
                # read1 returns after one underlying read, allowing the total deadline to be checked.
                chunk = response.read1(min(64 * 1024, max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise WorkspaceError(f"The response exceeded {max_bytes} bytes and was discarded")
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        raise WorkspaceError(_describe_status(exc.code)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise WorkspaceError(f"The network request failed: {scrub(getattr(exc, 'reason', exc))[:120]}") from None
    return b"".join(chunks)


# ---------------------------------------------------------------------------------------------
# Source validation and the GitHub tree preflight
# ---------------------------------------------------------------------------------------------


def parse_github_url(raw_url: str) -> tuple[str, str]:
    """Normalize a public https://github.com/<owner>/<repo>[.git][/] URL into (owner, repository).

    Rejects credentials, non-https schemes, non-github.com hosts, ports, queries, fragments, and subpaths.
    """

    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > 512:
        raise WorkspaceError("Enter a public https://github.com/owner/repository URL")
    candidate = raw_url.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise WorkspaceError("The GitHub URL is not a valid URL") from exc
    if parts.scheme.lower() != "https":
        raise WorkspaceError("Only https://github.com repository URLs are supported")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise WorkspaceError("Credential-bearing URLs are not allowed")
    host = (parts.hostname or "").lower()
    if host not in ("github.com", "www.github.com") or parts.port is not None:
        raise WorkspaceError("Only public github.com repository URLs are allowed")
    if parts.query or parts.fragment:
        raise WorkspaceError("GitHub repository URLs must not include query strings or fragments")
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) != 2:
        raise WorkspaceError("Enter a repository URL of the form https://github.com/<owner>/<repository>")
    owner, repo = segments
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not GITHUB_OWNER.fullmatch(owner) or not GITHUB_REPOSITORY.fullmatch(repo) or repo in (".", ".."):
        raise WorkspaceError("GitHub URL contains an invalid owner or repository name")
    return owner, repo


def resolve_github_source(
    raw_url: str,
    limits: SourceLimits,
    http_get: HttpGet = fetch_bounded,
) -> dict[str, Any]:
    """Resolve a public github.com URL's default branch to one exact 40-char commit SHA without calling AI."""

    owner, repository = parse_github_url(raw_url)

    def fetch_json(url: str) -> Any:
        body = http_get(url, limits.network_timeout_seconds, limits.max_response_bytes)
        if len(body) > limits.max_response_bytes:
            raise WorkspaceError(f"The response exceeded {limits.max_response_bytes} bytes")
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise WorkspaceError("GitHub returned a response that is not valid JSON") from None

    repo_doc = fetch_json(f"https://api.github.com/repos/{owner}/{repository}")
    if not isinstance(repo_doc, dict) or not isinstance(repo_doc.get("default_branch"), str) or not repo_doc["default_branch"].strip():
        raise WorkspaceError("GitHub did not report a valid default branch for this repository")
    default_branch = repo_doc["default_branch"].strip()
    if any(ord(ch) < 32 or ch == "\x7f" for ch in default_branch) or ".." in default_branch:
        raise WorkspaceError("GitHub returned an unsafe default branch name")

    commit_doc = fetch_json(f"https://api.github.com/repos/{owner}/{repository}/commits/{quote(default_branch, safe='')}")
    sha = commit_doc.get("sha") if isinstance(commit_doc, dict) else None
    if not isinstance(sha, str) or not FULL_REVISION_PATTERN.fullmatch(sha):
        raise WorkspaceError("GitHub did not return an exact 40-character commit SHA for the default branch")

    return {
        "kind": "github",
        "owner": owner,
        "repository": repository,
        "default_branch": default_branch,
        "revision": sha,
        "canonical_url": f"https://github.com/{owner}/{repository}",
    }


def validate_github_source(source: GitHubSource) -> None:
    owner, repository, revision = source.owner, source.repository, source.revision
    if not isinstance(owner, str) or not GITHUB_OWNER.fullmatch(owner):
        raise WorkspaceError("GitHub owner must be a canonical account name, not a URL or path")
    if (
        not isinstance(repository, str)
        or not GITHUB_REPOSITORY.fullmatch(repository)
        or repository in (".", "..")
        or repository.lower().endswith(".git")
    ):
        raise WorkspaceError("GitHub repository must be a canonical repository name, not a URL or path")
    if not isinstance(revision, str) or not FULL_REVISION_PATTERN.fullmatch(revision):
        raise WorkspaceError("GitHub revision must be an exact lowercase 40-character commit SHA")


def tree_url(source: GitHubSource, tree_sha: str) -> str:
    return f"https://api.github.com/repos/{source.owner}/{source.repository}/git/trees/{tree_sha}?recursive=1"


def clone_url(source: GitHubSource) -> str:
    return f"https://github.com/{source.owner}/{source.repository}.git"


def _unsafe_path(path: Any) -> str | None:
    if not isinstance(path, str) or not path or len(path) > 1024:
        return "an empty, non-text or overlong path"
    if path.startswith("/") or "\\" in path or any(ord(char) < 32 or char == "\x7f" for char in path):
        return "an absolute path or one with backslashes or control characters"
    for part in path.split("/"):
        if part in ("", ".", "..") or part.lower() == ".git" or ":" in part or part.endswith((".", " ")):
            return "a path with a traversal, .git, drive, or trailing-dot component"
    return None


def validate_tree(document: Any, limits: SourceLimits) -> TreeSummary:
    """Check a recursive GitHub tree listing against the limits before anything is downloaded."""

    if not isinstance(document, dict) or not isinstance(document.get("tree"), list):
        raise WorkspaceError("GitHub returned an unexpected tree document")
    tree_sha = document.get("sha")
    if not isinstance(tree_sha, str) or not FULL_REVISION_PATTERN.fullmatch(tree_sha):
        raise WorkspaceError("GitHub returned a tree without a valid object ID")
    if document.get("truncated") is not False:
        raise WorkspaceError("The repository tree is too large for GitHub to list completely, so it cannot be bounded")
    entries = document["tree"]
    if len(entries) > limits.max_tree_entries:
        raise WorkspaceError(f"The repository has more than {limits.max_tree_entries} tree entries")
    files = 0
    total = 0
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise WorkspaceError("GitHub returned a malformed tree entry")
        path, mode, kind = entry.get("path"), entry.get("mode"), entry.get("type")
        problem = _unsafe_path(path)
        if problem:
            raise WorkspaceError(f"The repository contains {problem}: {scrub(path)[:80]!r}")
        if path.lower() in seen:
            raise WorkspaceError(f"The repository has paths that collide on a case-insensitive disk: {scrub(path)[:80]!r}")
        seen.add(path.lower())
        if kind == "tree" and mode == "040000":
            continue
        if mode == "160000" or kind == "commit":
            raise WorkspaceError(f"The repository contains a submodule ({scrub(path)[:80]!r}); submodules are not initialized")
        if mode == "120000":
            raise WorkspaceError(f"The repository contains a symbolic link ({scrub(path)[:80]!r}); links are refused")
        if kind != "blob" or mode not in SAFE_MODES:
            raise WorkspaceError(f"The repository contains an unsupported entry type at {scrub(path)[:80]!r}")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise WorkspaceError(f"GitHub reported no usable size for {scrub(path)[:80]!r}")
        if size > limits.max_file_bytes:
            raise WorkspaceError(f"{scrub(path)[:80]!r} is {size} bytes, over the {limits.max_file_bytes} byte file limit")
        files += 1
        total += size
        if files > limits.max_files:
            raise WorkspaceError(f"The repository has more than {limits.max_files} files")
        if total > limits.max_total_bytes:
            raise WorkspaceError(f"The repository holds more than {limits.max_total_bytes} bytes of files")
    return TreeSummary(tree_sha=tree_sha, files=files, total_bytes=total)


def preflight_github(source: GitHubSource, limits: SourceLimits, http_get: HttpGet) -> TreeSummary:
    def fetch_json(url: str) -> Any:
        body = http_get(url, limits.network_timeout_seconds, limits.max_response_bytes)
        if len(body) > limits.max_response_bytes:
            raise WorkspaceError(f"The response exceeded {limits.max_response_bytes} bytes")
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise WorkspaceError("GitHub returned a response that is not valid JSON") from None

    # A commit SHA is not a tree SHA. Resolve the immutable commit first, and never follow
    # a URL supplied in the response: construct the fixed GitHub endpoint from its validated hash.
    commit = fetch_json(f"https://api.github.com/repos/{source.owner}/{source.repository}/git/commits/{source.revision}")
    tree = commit.get("tree") if isinstance(commit, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(commit, dict) or commit.get("sha") != source.revision or not isinstance(tree_sha, str) or not FULL_REVISION_PATTERN.fullmatch(tree_sha):
        raise WorkspaceError("GitHub returned an invalid commit or root tree object ID")
    summary = validate_tree(fetch_json(tree_url(source, tree_sha)), limits)
    if summary.tree_sha != tree_sha:
        raise WorkspaceError("GitHub returned a different root tree")
    return summary


# ---------------------------------------------------------------------------------------------
# Hardened, monitored Git
# ---------------------------------------------------------------------------------------------

_INHERITED = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG")


def git_environment(home: Path) -> dict[str, str]:
    """A scrubbed environment: no inherited Git variables, tokens, proxies, or user configuration."""

    environment = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    return environment


GIT_OPTIONS = (
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", f"core.attributesFile={os.devnull}",
    "-c", "core.protectNTFS=true",
    "-c", "core.protectHFS=true",
    "-c", "core.autocrlf=false",
    # Git for Windows otherwise skips paths over 260 characters, so a deep checkout looks dirty. No-op elsewhere.
    "-c", "core.longpaths=true",
    "-c", "credential.helper=",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "http.followRedirects=false",
    "-c", "transfer.fsckObjects=true",
    "-c", "fetch.recurseSubmodules=false",
    "-c", "submodule.recurse=false",
)


def directory_size(path: Path) -> int:
    """Bytes held under a directory, never following links."""

    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            # lstat, not DirEntry.stat: on Windows the latter reports stale sizes for files still being written.
                            total += os.lstat(entry.path).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    process.kill()
    process.wait()


def run_bounded(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    watch: Path,
    max_bytes: int,
) -> str:
    """Run a command with no shell and no stdin, stopping it at the deadline or the disk cap.

    Returns standard output. The whole process tree is killed when a bound is reached.
    """

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            list(args), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            start_new_session=sys.platform != "win32",
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    code = process.wait(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() > deadline:
                        _kill_tree(process)
                        raise WorkspaceError(f"Git exceeded {timeout:g} seconds and was stopped") from None
                    if directory_size(watch) > max_bytes:
                        _kill_tree(process)
                        raise WorkspaceError(f"Checkout exceeded {max_bytes} bytes of disk and was stopped") from None
        except BaseException:
            if process.poll() is None:
                _kill_tree(process)
            raise
        if directory_size(watch) > max_bytes:
            raise WorkspaceError(f"Checkout exceeded {max_bytes} bytes of disk")
        if code != 0:
            err.seek(0)
            detail = scrub(err.read(2000).decode("utf-8", "replace")).strip()[:200]
            raise WorkspaceError(f"Git failed with exit code {code}: {detail}")
        out.seek(0)
        return out.read(65536).decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------------------------


def _owned_path(store_root: Path, workspace_id: str) -> Path | None:
    """The workspace directory, only when it is a real directory directly under the store with our marker."""

    if not isinstance(workspace_id, str) or not WORKSPACE_ID.fullmatch(workspace_id):
        return None
    path = store_root / workspace_id
    try:
        if path.is_symlink() or not path.is_dir() or path.resolve() != path:
            return None
        marker = path / MARKER_NAME
        if marker.is_symlink() or not marker.is_file():
            return None
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != MARKER_KIND or data.get("workspace_id") != workspace_id:
        return None
    return path


def _remove_tree(path: Path) -> None:
    def writable(function: Callable[[str], Any], target: str, _: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=writable)


class WorkspaceManager:
    """Creates, prunes and deletes workspaces. ``http_get`` and ``run_git`` are test seams."""

    def __init__(
        self,
        settings: WorkspaceSettings,
        *,
        http_get: HttpGet = fetch_bounded,
        run_git: RunGit = run_bounded,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._http_get = http_get
        self._run_git = run_git
        self._clock = clock

    # -- retention -------------------------------------------------------------------------

    def owned_workspaces(self) -> dict[str, float]:
        """Workspace IDs under the store that carry our marker, with their creation time."""

        found: dict[str, float] = {}
        store = self.settings.store_root
        if not store.is_dir():
            return found
        for child in store.iterdir():
            path = _owned_path(store, child.name)
            if path is not None:
                try:
                    found[child.name] = float(json.loads((path / MARKER_NAME).read_text(encoding="utf-8"))["created_at"])
                except (OSError, ValueError, KeyError, TypeError):
                    found[child.name] = 0.0
        return found

    def prune(self) -> list[str]:
        """Delete owned workspaces older than the retention period."""

        cutoff = self._clock() - self.settings.limits.retention_seconds
        removed = [name for name, created in sorted(self.owned_workspaces().items()) if created < cutoff]
        for name in removed:
            self.cleanup(name)
        return removed

    def cleanup(self, workspace_id: str) -> bool:
        """Delete one workspace if, and only if, it is a verified marker-owned directory under the store."""

        path = _owned_path(self.settings.store_root, workspace_id)
        if path is None:
            return False
        _remove_tree(path)
        return True

    # -- acquisition -----------------------------------------------------------------------

    def acquire(self, project_name: str, sources: Sequence[LocalSource | GitHubSource]) -> Workspace:
        """Create a workspace for one or more sources, or raise ``WorkspaceError`` leaving nothing behind."""

        limits = self.settings.limits
        name = scrub(project_name).strip()
        if not isinstance(project_name, str) or name != project_name.strip() or not 1 <= len(name) <= 80:
            raise WorkspaceError("Project name must be 1 to 80 characters without control characters")
        if not 1 <= len(sources) <= limits.max_sources:
            raise WorkspaceError(f"Between 1 and {limits.max_sources} sources are required")
        plans = self._validate(sources)
        self.prune()
        if len(self.owned_workspaces()) >= limits.max_workspaces:
            raise WorkspaceError(
                f"The workspace limit of {limits.max_workspaces} is reached; delete a workspace or wait for retention to expire"
            )
        workspace_id = f"ws-{secrets.token_hex(16)}"
        root = self.settings.store_root / workspace_id
        root.mkdir(parents=True)
        marker = {"kind": MARKER_KIND, "schema_version": MARKER_VERSION, "workspace_id": workspace_id, "created_at": self._clock()}
        write_json_atomic(root / MARKER_NAME, marker)
        try:
            workspace = self._build(root, workspace_id, name, plans)
        except BaseException:
            self.cleanup(workspace_id)
            raise
        write_json_atomic(root / MARKER_NAME, {**marker, "sources": [record.to_dict() for record in workspace.sources]})
        return workspace

    def _validate(self, sources: Sequence[LocalSource | GitHubSource]) -> list[tuple[str, LocalSource | GitHubSource]]:
        plans: list[tuple[str, LocalSource | GitHubSource]] = []
        failures: list[SourceFailure] = []
        used = {DESTINATION_ID}
        for source in sources:
            try:
                if isinstance(source, LocalSource):
                    label = source.root_name if isinstance(source.root_name, str) else ""
                    if label not in self.settings.local_roots:
                        raise WorkspaceError("Unknown local source; choose one of the configured source names")
                    source_id = label
                elif isinstance(source, GitHubSource):
                    validate_github_source(source)
                    label = f"{source.owner}/{source.repository}"
                    source_id = slugify(f"{source.owner}-{source.repository}")
                else:
                    raise WorkspaceError("Unsupported source type")
                if source_id in used:
                    raise WorkspaceError("Each source may be used once and needs a distinct name")
                used.add(source_id)
                plans.append((source_id, source))
            except WorkspaceError as exc:
                failures.append(SourceFailure(scrub(getattr(source, "root_name", None) or f"{getattr(source, 'owner', '?')}/{getattr(source, 'repository', '?')}")[:100], str(exc)))
        if failures:
            raise WorkspaceError("Invalid sources: " + "; ".join(f"{item.source}: {item.message}" for item in failures), failures)
        return plans

    def _build(
        self, root: Path, workspace_id: str, name: str, plans: list[tuple[str, LocalSource | GitHubSource]]
    ) -> Workspace:
        limits = self.settings.limits
        config = create_project(root, name)
        (root / DESTINATION_DIRECTORY).mkdir()
        (root / SOURCES_DIRECTORY).mkdir()
        records: list[SourceRecord] = []
        for source_id, source in plans:
            if isinstance(source, LocalSource):
                path = self.settings.local_roots[source.root_name]
                if not path.is_dir():
                    raise self._failed(source.root_name, "The configured local source is not an available directory")
                try:
                    state = read_git_state(path)
                except SnapshotError as exc:
                    raise self._failed(source.root_name, str(exc)) from None
                config.repositories.append(RepositorySpec(id=source_id, path=path.as_posix()))
                records.append(SourceRecord(source_id, "local", source.root_name, None, state.to_dict()))
            else:
                label = f"{source.owner}/{source.repository}"
                try:
                    state = self._fetch(root / SOURCES_DIRECTORY / source_id, source, limits)
                except WorkspaceError as exc:
                    raise self._failed(label, str(exc)) from None
                config.repositories.append(
                    RepositorySpec(id=source_id, path=f"{SOURCES_DIRECTORY}/{source_id}", pinned_revision=source.revision)
                )
                records.append(SourceRecord(source_id, "github", label, source.revision, state.to_dict()))
        config.repositories.append(
            RepositorySpec(id=DESTINATION_ID, path=DESTINATION_DIRECTORY, role=ROLE_DESTINATION)
        )
        config.destination_repository_id = DESTINATION_ID
        config.save()
        return Workspace(workspace_id, root, ProjectConfig.load(root), tuple(records))

    @staticmethod
    def _failed(source: str, message: str) -> WorkspaceError:
        failure = SourceFailure(source, message)
        return WorkspaceError(f"Could not acquire {source}: {message}", [failure])

    def _fetch(self, checkout: Path, source: GitHubSource, limits: SourceLimits) -> GitState:
        summary = preflight_github(source, limits, self._http_get)
        checkout.mkdir()
        environment = git_environment(checkout.parent)
        deadline = time.monotonic() + limits.git_timeout_seconds

        def git(*arguments: str) -> str:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkspaceError("Git acquisition exceeded its total time limit")
            return self._run_git(
                ["git", *GIT_OPTIONS, *arguments], cwd=checkout, env=environment,
                timeout=remaining, watch=checkout, max_bytes=limits.max_checkout_bytes,
            )

        git("init", "-q", "--template=")
        git("remote", "add", "origin", clone_url(source))
        git("fetch", "-q", "--depth=1", "--no-tags", "--no-recurse-submodules", "--", clone_url(source), source.revision)
        if git("rev-parse", "--verify", f"{source.revision}^{{tree}}") != summary.tree_sha:
            raise WorkspaceError("The fetched commit does not match the tree GitHub listed for it")
        git("checkout", "-q", "--detach", "--no-recurse-submodules", source.revision, "--")
        state = read_git_state(checkout)
        try:
            resolve_snapshot(
                repository_id=f"{source.owner}/{source.repository}", mode=SNAPSHOT_MODE_PINNED, git=state,
                fingerprint="", pinned_revision=source.revision,
            )
        except SnapshotError as exc:
            raise WorkspaceError(str(exc)) from None
        return state
