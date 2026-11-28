"""Repository snapshot modes: local working trees and pinned clean revisions.

A snapshot describes exactly which repository content a scan observed. Local
mode accepts whatever is on disk, including uncommitted changes, and records
enough Git state to explain it. Pinned mode is for automation such as GitHub
Actions: it accepts only a clean checkout of one explicitly pinned commit.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import sha256_text
from .config import FULL_REVISION_PATTERN
from .errors import SnapshotError


SNAPSHOT_MODE_LOCAL = "local"
SNAPSHOT_MODE_PINNED = "pinned"
SNAPSHOT_MODES = (SNAPSHOT_MODE_LOCAL, SNAPSHOT_MODE_PINNED)

REVISION_KIND_COMMIT = "commit"
REVISION_KIND_CONTENT = "content"

TREE_CLEAN = "clean"
TREE_DIRTY = "dirty"
TREE_NON_GIT = "non-git"

HEAD_ATTACHED = "attached"
HEAD_DETACHED = "detached"


@dataclass(frozen=True)
class GitState:
    """Observed Git facts for one repository root, without interpretation."""

    is_repository: bool
    revision: str | None
    branch: str | None
    head_state: str | None
    working_tree_state: str
    status_fingerprint: str | None
    path_prefix: str | None = None
    # Why the tree is not exactly the commit, and what was left out; neither is written to the IR.
    dirty_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "vcs": "git" if self.is_repository else None,
            "git_revision": self.revision,
            "git_branch": self.branch,
            "head_state": self.head_state,
            "git_path_prefix": self.path_prefix,
            "working_tree_state": self.working_tree_state,
            "git_status_fingerprint": self.status_fingerprint,
        }


def _git(root: Path, *options: str, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *options, "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _inert_options(root: Path) -> list[str]:
    """Git options that stop the repository's own configuration from running programs.

    A scanned repository is untrusted and its ``.git/config`` can name programs that Git runs
    during ``status``: ``core.fsmonitor``, the ``clean``, ``smudge``, and ``process`` commands of any
    ``filter.<name>`` driver, and the hooks that run when ``status`` rewrites the index. Reading the
    configuration is safe, so every driver that is defined is switched off by name. Optional index
    locks are off, so ``status`` does not rewrite the index, and hooks are looked up in an empty path.
    """

    # core.longpaths: Git for Windows otherwise cannot stat paths over 260 characters and reports them deleted.
    options = [
        "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "core.longpaths=true",
    ]
    listed = _git(root, arguments=("config", "--list", "--name-only"))
    if listed.returncode != 0:
        # Without the list, filter commands could not be switched off, so nothing may be inspected.
        raise _fail(root, listed)
    names = {line.strip() for line in listed.stdout.splitlines()}
    for name in sorted(names):
        match = re.fullmatch(r"filter\.(.+)\.(clean|smudge|process|required)", name, re.IGNORECASE)
        if match:
            value = "false" if match.group(2).lower() == "required" else ""
            options += ["-c", f"{name}={value}"]
    return options


def _run_git(root: Path, *args: str, options: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    return _git(root, *(options or []), arguments=args)


MAX_SUBMODULE_DEPTH = 8


def _fail(root: Path, result: subprocess.CompletedProcess[str]) -> SnapshotError:
    detail = result.stderr.strip().splitlines()[:1]
    return SnapshotError(
        f"Git could not report the state of {root}: {detail[0] if detail else 'a git command failed'}. "
        "Evidence cannot be tied to a revision, so the scan stopped."
    )


def _differences_from_the_commit(
    root: Path, options: list[str], excluded: frozenset[str], depth: int
) -> tuple[list[str], list[str]]:
    """What makes the files on disk differ from the commit, and what could not be inspected.

    Files that Git ignores are still analyzed, so an ignored file the scan can reach counts as a
    difference. Every submodule is inspected with the same hardening, recursively: a submodule at
    another commit, with uncommitted or untracked files, or holding files Git does not track is a
    difference. A submodule that is not initialized contributes no files and is only noted.
    """

    problems: list[str] = []
    notes: list[str] = []
    ignored = _run_git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", ".", options=options)
    if ignored.returncode != 0:
        raise _fail(root, ignored)
    for path in filter(None, ignored.stdout.split("\0")):
        if not any(part.lower() in excluded for part in path.split("/")[:-1]):
            problems.append(f"ignored file {path} is analyzed but is not part of the commit")
    staged = _run_git(root, "ls-files", "--stage", "-z", "--", ".", options=options)
    if staged.returncode != 0:
        raise _fail(root, staged)
    for entry in filter(None, staged.stdout.split("\0")):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if fields[0] != "160000":
            continue
        found, mentioned = _inspect_submodule(root, path, fields[1], excluded, depth)
        problems += found
        notes += mentioned
    return problems, notes


def _inspect_submodule(
    root: Path, path: str, commit: str, excluded: frozenset[str], depth: int
) -> tuple[list[str], list[str]]:
    folder = root / path
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        return [f"submodule {path} is not a plain directory"], []
    if not folder.is_dir():
        return [], [f"submodule {path} is not checked out; its content is not analyzed"]
    if not (folder / ".git").exists():
        if any(folder.iterdir()):
            return [f"submodule {path} is not initialized but holds files that Git does not track"], []
        return [], [f"submodule {path} is not initialized; its content is not analyzed"]
    if depth >= MAX_SUBMODULE_DEPTH:
        return [f"submodule {path} is nested more than {MAX_SUBMODULE_DEPTH} levels deep and was not inspected"], []
    state = read_git_state(folder, excluded, depth + 1)
    if not state.is_repository:
        return [f"submodule {path} could not be read as a Git repository"], []
    problems: list[str] = []
    if state.revision != commit:
        problems.append(f"submodule {path} is at {state.revision or 'no commit'} but the repository records {commit}")
    problems += [f"submodule {path}: {reason}" for reason in state.dirty_reasons]
    return problems, [f"submodule {path}: {note}" for note in state.notes]


def read_git_state(root: Path, excluded: frozenset[str] = frozenset(), depth: int = 0) -> GitState:
    """Read Git state for a directory. Never executes repository code.

    ``excluded`` names directories the scan skips, so ignored files inside them do not count.
    """

    try:
        top_level = _run_git(root, "rev-parse", "--show-toplevel")
    except OSError:
        # Git is not installed; treat the tree as plain content.
        return GitState(False, None, None, None, TREE_NON_GIT, None)
    if top_level.returncode != 0:
        return GitState(False, None, None, None, TREE_NON_GIT, None)
    options = _inert_options(root)
    repository_root = Path(top_level.stdout.strip()).resolve()
    try:
        path_prefix = root.resolve().relative_to(repository_root).as_posix()
    except ValueError:
        path_prefix = ""
    path_prefix = "" if path_prefix == "." else path_prefix
    revision_result = _run_git(root, "rev-parse", "--verify", "HEAD")
    revision = revision_result.stdout.strip() if revision_result.returncode == 0 else None
    branch_result = _run_git(root, "symbolic-ref", "-q", "--short", "HEAD")
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 and branch_result.stdout.strip() else None
    head_state = HEAD_ATTACHED if branch else HEAD_DETACHED
    # The pathspec limits status to the registered subtree, so an unrelated change elsewhere in
    # a larger repository does not make this snapshot dirty. Submodules are inspected below, by a
    # separate hardened call each, so status never starts a process inside one.
    status_result = _run_git(
        root, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=all", "--", ".", options=options
    )
    if status_result.returncode != 0:
        raise _fail(root, status_result)
    problems, notes = _differences_from_the_commit(root, options, excluded, depth)
    status = status_result.stdout + "".join(f"{problem}\n" for problem in problems)
    reasons = [line for line in status_result.stdout.splitlines() if line] + problems
    return GitState(
        is_repository=True,
        revision=revision,
        branch=branch,
        head_state=head_state,
        working_tree_state=TREE_DIRTY if status else TREE_CLEAN,
        status_fingerprint=sha256_text(status) if status else None,
        path_prefix=path_prefix,
        dirty_reasons=tuple(reasons),
        notes=tuple(notes),
    )


def content_fingerprint(files: list[tuple[str, str]]) -> str:
    """Fingerprint of (path, content hash) pairs in path order."""

    ordered = sorted(files)
    return sha256_text("\n".join(f"{path}\0{content_hash}" for path, content_hash in ordered))


def validate_pin(repository_id: str, pinned_revision: str | None) -> str | None:
    if pinned_revision is None:
        return None
    candidate = pinned_revision.strip()
    if not FULL_REVISION_PATTERN.match(candidate):
        raise SnapshotError(
            f"{repository_id}: ambiguous pinned revision {pinned_revision!r}; a full 40-character commit SHA is required"
        )
    return candidate


def resolve_snapshot(
    *,
    repository_id: str,
    mode: str,
    git: GitState,
    fingerprint: str,
    pinned_revision: str | None,
) -> dict[str, Any]:
    """Apply snapshot-mode rules and return the snapshot header fields.

    The returned ``revision`` is what evidence resolves to: the commit SHA when the
    observed content is exactly that commit, otherwise the working-tree content
    fingerprint.
    """

    if mode not in SNAPSHOT_MODES:
        raise SnapshotError(f"Unsupported snapshot mode {mode!r}; expected one of: " + ", ".join(SNAPSHOT_MODES))
    pin = validate_pin(repository_id, pinned_revision)

    if mode == SNAPSHOT_MODE_PINNED:
        if not git.is_repository:
            raise SnapshotError(f"{repository_id}: pinned snapshots require a Git repository root")
        if git.revision is None:
            raise SnapshotError(f"{repository_id}: pinned snapshots require at least one commit")
        if git.working_tree_state != TREE_CLEAN:
            raise SnapshotError(
                f"{repository_id}: pinned snapshots require a clean working tree, found {git.working_tree_state}"
                + "".join(f"; {reason}" for reason in git.dirty_reasons[:5])
            )
        if pin is None:
            raise SnapshotError(
                f"{repository_id}: pinned snapshots require an explicit pinned revision; set pinned_revision in "
                f"repodocs.json or pass --pin {repository_id}=<commit-sha>"
            )
    if pin is not None:
        if git.revision is None:
            raise SnapshotError(f"{repository_id}: pinned revision {pin} cannot be verified without a Git commit")
        if git.revision != pin:
            raise SnapshotError(
                f"{repository_id}: stale checkout; HEAD is {git.revision} but the pinned revision is {pin}"
            )

    exact_commit = git.is_repository and git.revision is not None and git.working_tree_state == TREE_CLEAN
    return {
        "snapshot_mode": mode,
        **git.to_dict(),
        "pinned_revision": pin,
        "content_fingerprint": fingerprint,
        "revision_kind": REVISION_KIND_COMMIT if exact_commit else REVISION_KIND_CONTENT,
        "revision": git.revision if exact_commit else fingerprint,
    }


def read_remote_url(root: Path) -> str | None:
    """The `origin` remote URL, read from configuration only. Nothing is executed or contacted."""

    try:
        result = _git(root, arguments=("config", "--get", "remote.origin.url"))
    except OSError:
        return None
    url = result.stdout.strip()
    return url if result.returncode == 0 and url else None
