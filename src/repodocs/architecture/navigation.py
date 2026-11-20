"""Source links and the evidence map for architecture views (ADR-026).

A link is generated only for a committed snapshot of a repository whose remote is on github.com,
pinned to the exact commit SHA, with an encoded path and line span. A snapshot of working-tree
content has no commit to point at, so it never gets a link: it is labelled with its content
fingerprint instead. The remote string itself is never copied into any output, so credentials in
a CI checkout URL cannot leak. The map holds locations and hashes, never source text.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import ProjectConfig
from ..snapshot import read_remote_url
from .context import well_formed_path

MAP_VERSION = "1.0"
MAP_NAME = "architecture-view-evidence.json"
_GITHUB_REMOTE = re.compile(
    r"^(?:https://(?:[^/@\s]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SOURCE_LINK = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/[0-9a-f]{40}/[A-Za-z0-9._~%/+@!$&'()*,;=:-]+(?:#L[0-9]+(?:-L[0-9]+)?)?$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def github_repository(remote: str | None) -> tuple[str, str] | None:
    """Owner and name of a github.com remote, or None for any other host or form."""

    match = _GITHUB_REMOTE.match((remote or "").strip())
    if not match or match.group(1) in (".", "..") or match.group(2) in (".", ".."):
        return None
    return match.group(1), match.group(2)


def source_link(
    repository: tuple[str, str] | None, snapshot: dict[str, Any], path: str, line_start: int | None = None, line_end: int | None = None
) -> str | None:
    """A link to the exact committed source, or None when no honest link exists."""

    if repository is None or snapshot.get("revision_kind") != "commit" or not COMMIT_SHA.match(str(snapshot.get("revision"))):
        return None
    prefix = (snapshot.get("git_path_prefix") or "").strip("/")
    full = f"{prefix}/{path}" if prefix else path
    if not well_formed_path(full):
        return None
    link = f"https://github.com/{repository[0]}/{repository[1]}/blob/{snapshot['revision']}/{quote(full, safe='/')}"
    if line_start:
        link += f"#L{int(line_start)}" + (f"-L{int(line_end)}" if line_end and int(line_end) > int(line_start) else "")
    return link if SOURCE_LINK.match(link) else None


def repository_remotes(config: ProjectConfig, ir: dict[str, Any]) -> dict[str, tuple[str, str] | None]:
    remotes: dict[str, tuple[str, str] | None] = {}
    for snapshot in ir["snapshots"]:
        root = config.resolve_repository(config.repository(snapshot["repository_id"]))
        remotes[snapshot["repository_id"]] = github_repository(read_remote_url(root)) if snapshot.get("vcs") == "git" else None
    return remotes


def evidence_map(view: dict[str, Any], context: dict[str, Any], ir: dict[str, Any], remotes: dict[str, tuple[str, str] | None]) -> dict[str, Any]:
    """Where every component, connection and alternative of a view can be checked, with exact provenance.

    ``alternatives`` follows the order of the view's own list.
    """

    snapshots = {item["repository_id"]: item for item in ir["snapshots"]}
    documents = {item["id"]: item for item in context["documents"]}

    def item(key: str) -> dict[str, Any]:
        document = documents[key]
        snapshot = snapshots[document["repository_id"]]
        committed = snapshot["revision_kind"] == "commit"
        return {
            "id": key, "repository_id": document["repository_id"], "path": document["path"],
            "line_start": document["line_start"], "line_end": document["line_end"], "content_hash": document["content_hash"],
            "state": "committed" if committed else "working-tree",
            "link": source_link(remotes.get(document["repository_id"]), snapshot, document["path"], document["line_start"], document["line_end"]),
        }

    def member(repository_id: str, path: str) -> dict[str, Any]:
        return {"path": path, "link": source_link(remotes.get(repository_id), snapshots[repository_id], path)}

    return {
        "map_version": MAP_VERSION,
        "context_hash": view["provenance"]["context_hash"],
        "source_ir_hash": view["provenance"]["source_ir_hash"],
        "repositories": [
            {
                "repository_id": snapshot["repository_id"], "revision": snapshot["revision"], "revision_kind": snapshot["revision_kind"],
                "content_fingerprint": snapshot["content_fingerprint"],
                "remote": "/".join(remotes[snapshot["repository_id"]]) if remotes.get(snapshot["repository_id"]) else None,
                "note": (
                    "Pinned to this commit." if snapshot["revision_kind"] == "commit"
                    else "Working-tree content, not a commit: no source link is offered. Open the file in your checkout."
                ),
            }
            for snapshot in sorted(snapshots.values(), key=lambda entry: entry["repository_id"])
        ],
        "components": {
            entry["id"]: {"files": [member(entry["repository_id"], path) for path in entry["members"]], "evidence": [item(key) for key in entry["evidence"]]}
            for entry in view["components"]
        },
        "connections": {entry["id"]: {"evidence": [item(key) for key in entry["evidence"]]} for entry in view["connections"]},
        "alternatives": [{"about": list(entry["about"]), "evidence": [item(key) for key in entry["evidence"]]} for entry in view["alternatives"]],
    }
