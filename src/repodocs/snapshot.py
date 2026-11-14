"""Repository snapshot modes: local working trees and pinned clean revisions.

A snapshot describes exactly which repository content a scan observed. Local
mode accepts whatever is on disk, including uncommitted changes, and records
enough Git state to explain it. Pinned mode is for automation such as GitHub
Actions: it accepts only a clean checkout of one explicitly pinned commit.
"""

from __future__ import annotations

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


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def read_git_state(root: Path) -> GitState:
    """Read Git state for a directory. Never executes repository code."""

    try:
        top_level = _run_git(root, "rev-parse", "--show-toplevel")
    except OSError:
        # Git is not installed; treat the tree as plain content.
        return GitState(False, None, None, None, TREE_NON_GIT, None)
    if top_level.returncode != 0:
        return GitState(False, None, None, None, TREE_NON_GIT, None)
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
    # a larger repository does not make this snapshot dirty.
    status_result = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".")
    status = status_result.stdout if status_result.returncode == 0 else ""
    return GitState(
        is_repository=True,
        revision=revision,
        branch=branch,
        head_state=head_state,
        working_tree_state=TREE_DIRTY if status else TREE_CLEAN,
        status_fingerprint=sha256_text(status) if status else None,
        path_prefix=path_prefix,
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
