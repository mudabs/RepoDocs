"""Safe publication of generated documentation as a reviewable pull request (ADR-023).

Publication never touches the working tree or the checked-out branch. It builds one
commit on top of the checked-out base that contains only the generated output and its
manifest, compares that commit's tree with the bot branch already on the remote, and
pushes only when they differ. A run whose output equals what the base already contains
does nothing at all and never contacts the remote.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import __version__
from .config import ProjectConfig
from .errors import ConfigError, PublicationError
from .generator import manifest_path
from .snapshot import read_git_state


DEFAULT_BRANCH = "repodocs/docs-update"
DEFAULT_REMOTE = "origin"
PR_TITLE = "docs: update generated system documentation"
COMMIT_SUBJECT = "docs: regenerate system documentation"
AI_ARTIFACT_NAME = "architecture-ai.md"
MAX_LISTED_CHANGES = 50
TIMEOUT_SECONDS = 120

STATUS_NO_OP = "no-op"
STATUS_WOULD_PUBLISH = "would-publish"
STATUS_UNCHANGED = "unchanged"
STATUS_PUBLISHED = "published"

PR_CREATED = "created"
PR_UPDATED = "updated"
PR_EXISTING = "existing"
PR_CLOSED = "closed"

# Fixed text, never built from repository content.
NO_OP_COMMENT = (
    "Closing automatically: the generated documentation now equals what the base branch already contains, "
    "so this proposal is obsolete. RepoDocs opens a new pull request if the output changes again."
)

_BOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "RepoDocs",
    "GIT_AUTHOR_EMAIL": "repodocs@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "RepoDocs",
    "GIT_COMMITTER_EMAIL": "repodocs@users.noreply.github.com",
}


class PullRequests(Protocol):
    """The only GitHub operations publication needs; tests substitute a fake."""

    def find_open(self, head: str, base: str) -> int | None: ...

    def create(self, head: str, base: str, title: str, body: str) -> int: ...

    def update(self, number: int, title: str, body: str) -> None: ...

    def close(self, number: int, comment: str) -> None: ...


def _redact(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***").replace(base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***")
    return text.strip()


def auth_environment(remote_url: str, token: str | None) -> dict[str, str]:
    """Environment for a git process that talks to the remote.

    The token goes through git's environment configuration, so it is never written to
    disk, to the remote URL, or to a process argument list. Only https remotes get it.
    """

    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    parts = urlsplit(remote_url)
    if token and parts.scheme == "https" and parts.hostname:
        header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = f"http.https://{parts.netloc.rpartition('@')[2]}/.extraheader"
        environment["GIT_CONFIG_VALUE_0"] = header
    return environment


def _git(
    root: Path,
    *args: str,
    environment: dict[str, str] | None = None,
    token: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationError(f"git {args[0]} could not run: {_redact(str(exc), token)}") from exc
    if check and completed.returncode != 0:
        raise PublicationError(_redact(f"git {args[0]} failed: {completed.stderr or completed.stdout}", token))
    return completed


class GhPullRequests:
    """Pull requests through the GitHub CLI, which is preinstalled on GitHub-hosted runners."""

    def __init__(self, root: Path, token: str) -> None:
        self.root = root
        self.token = token

    def _gh(self, *args: str) -> str:
        environment = os.environ.copy()
        environment.update({"GH_TOKEN": self.token, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
        try:
            completed = subprocess.run(
                ["gh", *args],
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise PublicationError("The GitHub CLI `gh` is required to open pull requests and was not found") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublicationError(f"gh {args[0]} {args[1]} could not run: {_redact(str(exc), self.token)}") from exc
        if completed.returncode != 0:
            raise PublicationError(_redact(f"gh {args[0]} {args[1]} failed: {completed.stderr or completed.stdout}", self.token))
        return completed.stdout

    @staticmethod
    def _body_file(body: str) -> str:
        handle, name = tempfile.mkstemp(suffix=".md", text=True)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(body)
        return name

    def find_open(self, head: str, base: str) -> int | None:
        output = self._gh("pr", "list", "--head", head, "--base", base, "--state", "open", "--json", "number", "--limit", "10")
        try:
            numbers = sorted(item["number"] for item in json.loads(output))
        except (ValueError, TypeError, KeyError) as exc:
            raise PublicationError("gh returned an unreadable pull request list") from exc
        return numbers[0] if numbers else None

    def create(self, head: str, base: str, title: str, body: str) -> int:
        body_file = self._body_file(body)
        try:
            output = self._gh("pr", "create", "--head", head, "--base", base, "--title", title, "--body-file", body_file)
        finally:
            os.unlink(body_file)
        match = re.search(r"/pull/(\d+)\s*$", output.strip())
        if not match:
            raise PublicationError("gh did not report the number of the pull request it created")
        return int(match.group(1))

    def update(self, number: int, title: str, body: str) -> None:
        body_file = self._body_file(body)
        try:
            self._gh("pr", "edit", str(number), "--title", title, "--body-file", body_file)
        finally:
            os.unlink(body_file)

    def close(self, number: int, comment: str) -> None:
        # The branch is deleted separately, under a lease, so a moved branch is never removed.
        self._gh("pr", "close", str(number), "--comment", comment)


def render_pull_request_body(ir: dict[str, Any], changes: list[tuple[str, str]], branch: str, base: str) -> str:
    """The pull request description, built only from the IR and the list of changed files."""

    lines = [
        "Automated update of the generated system documentation.",
        "",
        f"The branch `{branch}` is owned by RepoDocs. Commits added to it by hand are overwritten by the next run,"
        " so change the source repositories or the project configuration instead of editing this branch.",
        "",
        "## Source revisions",
        "",
        "| Repository | Snapshot | Revision |",
        "|---|---|---|",
    ]
    for snapshot in ir["snapshots"]:
        lines.append(
            f"| {snapshot['repository_id']} | {snapshot['snapshot_mode']} | {snapshot['revision_kind']} `{snapshot['revision']}` |"
        )
    states: dict[str, int] = {}
    for candidate in ir.get("candidates", []):
        states[candidate["status"]] = states.get(candidate["status"], 0) + 1
    lines += ["", "## Cross-repository candidates", ""]
    lines.append(
        ", ".join(f"{count} {status}" for status, count in sorted(states.items()))
        if states
        else "No cross-repository candidates."
    )
    lines += ["", "## Changed files", ""]
    labels = {"A": "added", "M": "modified", "D": "removed"}
    for status, path in changes[:MAX_LISTED_CHANGES]:
        lines.append(f"- {labels.get(status, status)}: `{path}`")
    if len(changes) > MAX_LISTED_CHANGES:
        lines.append(f"- and {len(changes) - MAX_LISTED_CHANGES} more")
    if any(status == "D" and Path(path).name == AI_ARTIFACT_NAME for status, path in changes):
        lines += [
            "",
            "The AI-assisted document is removed because publication from CI does not run AI interpretation and"
            " no current claims match these pinned source revisions. The deterministic documentation does not"
            " depend on it.",
        ]
    lines += ["", f"Generated by RepoDocs {__version__}.", ""]
    return "\n".join(lines)


def _parse_name_status(output: str) -> list[tuple[str, str]]:
    parts = output.split("\0")
    return [(parts[index][:1], parts[index + 1]) for index in range(0, len(parts) - 1, 2) if parts[index]]


def _outside_changes(root: Path, owned: list[str]) -> list[str]:
    output = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries = output.split("\0")
    found: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            index += 1  # the original path follows a rename or copy
        if not any(path == item or path.startswith(item + "/") for item in owned):
            found.append(path)
    return found


def _remote_heads(
    root: Path, remote: str, names: list[str], environment: dict[str, str], token: str | None
) -> dict[str, str | None]:
    """The remote's tip for each branch name, or None where the branch does not exist."""

    refs = [f"refs/heads/{name}" for name in names]
    output = _git(root, "ls-remote", "--heads", remote, *refs, environment=environment, token=token).stdout
    found = {}
    for line in output.splitlines():
        sha, _, ref = line.partition("\t")
        found[ref.strip()] = sha.strip()
    return {name: found.get(ref) for name, ref in zip(names, refs)}


def _require_base_on_remote(local: str, base: str, remote: str, remote_tip: str | None) -> None:
    """The documentation commit is built on the checkout, so the checkout must be the remote's base."""

    if remote_tip is None:
        raise PublicationError(f"The remote {remote!r} has no branch {base!r}, so there is nothing to open a pull request against")
    if local != remote_tip:
        raise PublicationError(
            f"The checked-out {base!r} is at {local[:12]} but {remote!r} has {base!r} at {remote_tip[:12]}. "
            "The documentation commit is built on the checkout, so publishing would put commits the remote does not "
            "have, or leave out ones it does, in the pull request. Check out the remote's current base (a fresh "
            "checkout of it, with no local commits) and run again"
        )


def _retire(
    root: Path,
    remote: str,
    branch: str,
    base: str,
    tip: str | None,
    environment: dict[str, str],
    token: str | None,
    pull_requests: PullRequests,
) -> dict[str, Any]:
    """The output equals the base, so an earlier proposal is obsolete: close it and remove its branch."""

    result: dict[str, Any] = {
        "status": STATUS_NO_OP, "branch": branch, "base": base, "changes": [], "pull_request": None, "branch_deleted": False,
    }
    if tip is None:
        return result  # without the branch there can be no open pull request from it
    number = pull_requests.find_open(branch, base)
    if number is not None:
        pull_requests.close(number, NO_OP_COMMENT)
        result["pull_request"] = {"number": number, "action": PR_CLOSED}
    # A lease on the tip that was read means a branch someone else moved meanwhile is never deleted.
    _git(
        root, "push", f"--force-with-lease=refs/heads/{branch}:{tip}", remote, f":refs/heads/{branch}",
        environment=environment, token=token,
    )
    result["branch_deleted"] = True
    return result


def _commit_message(ir: dict[str, Any]) -> str:
    revisions = "\n".join(
        f"- {snapshot['repository_id']}: {snapshot['revision_kind']} {snapshot['revision']}" for snapshot in ir["snapshots"]
    )
    return f"{COMMIT_SUBJECT}\n\nSource revisions:\n{revisions}\n"


def publish(
    config: ProjectConfig,
    ir: dict[str, Any],
    *,
    base: str,
    branch: str = DEFAULT_BRANCH,
    remote: str = DEFAULT_REMOTE,
    dry_run: bool = False,
    token: str | None = None,
    pull_requests: PullRequests | None = None,
) -> dict[str, Any]:
    """Publish the generated documentation of the destination repository for review.

    The caller must already have validated the output. Returns a JSON-ready result whose
    `status` is `no-op`, `would-publish`, `unchanged` (the bot branch already holds exactly
    this content), or `published`.
    """

    root = config.destination_root()
    git = read_git_state(root)
    if not git.is_repository or git.path_prefix or git.revision is None:
        raise ConfigError("Publication needs the destination to be the root of a Git repository with at least one commit")
    try:
        manifest = manifest_path(config).resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise ConfigError(
            "Publication needs the artifact manifest inside the destination repository, but the project "
            "configuration root is outside it; use the coordination repository as the destination"
        ) from exc
    output = config.output_path.relative_to(root).as_posix()
    owned = [output, manifest]
    if not base.strip():
        raise ConfigError("--base must name the branch the pull request targets")
    current = _git(root, "branch", "--show-current").stdout.strip()
    if current != base:
        raise PublicationError(
            f"The checked-out branch is {current or 'a detached HEAD'}, not the base {base!r}. The documentation commit "
            "is built on top of the checkout, so publishing from anything else would put its commits in the pull "
            "request; check out the base branch by name"
        )
    if not branch or branch.startswith("-") or branch == base or _git(root, "check-ref-format", "--branch", branch, check=False).returncode != 0:
        raise ConfigError(f"Invalid publication branch {branch!r}; it must be a valid branch name different from the base {base!r}")
    outside = _outside_changes(root, owned)
    if outside:
        shown = ", ".join(outside[:5])
        raise PublicationError(
            "The destination has changes outside the generated documentation, so nothing was published: "
            f"{shown}{', ...' if len(outside) > 5 else ''}. Source checkouts inside the coordination repository "
            "must be listed in .gitignore"
        )

    # Build the documentation commit in a throwaway index so the work tree and branch stay untouched.
    with tempfile.TemporaryDirectory() as scratch:
        environment = os.environ.copy()
        environment.update(_BOT_IDENTITY)
        environment["GIT_INDEX_FILE"] = str(Path(scratch) / "index")
        _git(root, "read-tree", "HEAD", environment=environment)
        existing = [item for item in owned if (root / item).exists() or _git(root, "ls-files", "--", item).stdout.strip()]
        if existing:
            _git(root, "add", "-A", "-f", "--", *existing, environment=environment)
        changes = _parse_name_status(_git(root, "diff", "--cached", "--name-status", "-z", "HEAD", environment=environment).stdout)
        tree = commit = ""
        if changes:
            tree = _git(root, "write-tree", environment=environment).stdout.strip()
            commit = _git(root, "commit-tree", tree, "-p", "HEAD", "-m", _commit_message(ir), environment=environment).stdout.strip()

    if not dry_run:
        # Before anything else, prove the checkout is the remote's base. Matching the branch name proves nothing:
        # unpublished local commits would otherwise become part of the documentation pull request.
        if pull_requests is None:
            raise ConfigError("A pull request client is required to publish")
        url = _git(root, "remote", "get-url", remote).stdout.strip()
        remote_environment = auth_environment(url, token)
        heads = _remote_heads(root, remote, [base, branch], remote_environment, token)
        _require_base_on_remote(git.revision, base, remote, heads[base])
        tip = heads[branch]
        if not changes:
            return _retire(root, remote, branch, base, tip, remote_environment, token, pull_requests)
    elif not changes:
        return {"status": STATUS_NO_OP, "branch": branch, "base": base, "changes": [], "pull_request": None, "branch_deleted": False}

    result: dict[str, Any] = {
        "status": STATUS_WOULD_PUBLISH,
        "branch": branch,
        "base": base,
        "commit": commit,
        "tree": tree,
        "changes": [{"status": status, "path": path} for status, path in changes],
        "pull_request": None,
    }
    if dry_run:
        return result

    pushed = True
    if tip:
        # A depth-limited fetch would make a complete clone shallow, so only a shallow clone gets one.
        shallow = _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        depth = ["--depth=1"] if shallow else []
        _git(root, "fetch", "--no-tags", *depth, remote, f"refs/heads/{branch}", environment=remote_environment, token=token)
        pushed = _git(root, "rev-parse", "FETCH_HEAD^{tree}").stdout.strip() != tree
    if pushed:
        lease = f"--force-with-lease=refs/heads/{branch}:{tip or ''}"
        _git(
            root, "push", lease, remote, f"{commit}:refs/heads/{branch}",
            environment=remote_environment, token=token,
        )
    result["status"] = STATUS_PUBLISHED if pushed else STATUS_UNCHANGED

    title, body = PR_TITLE, render_pull_request_body(ir, changes, branch, base)
    number = pull_requests.find_open(branch, base)
    if number is None:
        number = pull_requests.create(branch, base, title, body)
        result["pull_request"] = {"number": number, "action": PR_CREATED}
    elif pushed:
        pull_requests.update(number, title, body)
        result["pull_request"] = {"number": number, "action": PR_UPDATED}
    else:
        result["pull_request"] = {"number": number, "action": PR_EXISTING}
    return result
