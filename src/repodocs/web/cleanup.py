"""Bounded retention cleanup for server-owned web workspaces (#76).

Only marker-verified directories directly under ``store_root`` are eligible.
Symlinks and directories with active or queued jobs are never deleted.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .jobs import ACTIVE_STATES, JOBS_DIRECTORY
from .workspaces import WORKSPACE_ID, _owned_path


@dataclass(frozen=True)
class CleanupReport:
    """Summary of one bounded workspace cleanup pass."""

    scanned: int
    deleted: tuple[str, ...]
    skipped_active: tuple[str, ...]
    skipped_unowned: tuple[str, ...]


def _has_active_job(workspace_path: Path) -> bool:
    jobs_dir = workspace_path / JOBS_DIRECTORY
    if jobs_dir.is_symlink() or not jobs_dir.is_dir():
        return False
    for record in jobs_dir.iterdir():
        if record.is_symlink() or not record.is_file() or record.suffix != ".json":
            continue
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("state") in ACTIVE_STATES:
            return True
    return False


def cleanup_workspaces(
    store_root: Path,
    *,
    max_age_seconds: float,
    now: float | None = None,
) -> CleanupReport:
    """Remove marker-owned workspaces older than ``max_age_seconds`` without active jobs."""

    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    if not store_root.exists() or store_root.is_symlink() or not store_root.is_dir():
        return CleanupReport(0, (), (), ())

    current_time = time.time() if now is None else now
    cutoff = current_time - max_age_seconds
    scanned = 0
    deleted: list[str] = []
    skipped_active: list[str] = []
    skipped_unowned: list[str] = []

    for entry in sorted(store_root.iterdir(), key=lambda p: p.name):
        scanned += 1
        if entry.is_symlink() or not entry.is_dir():
            skipped_unowned.append(entry.name)
            continue
        if not WORKSPACE_ID.fullmatch(entry.name):
            skipped_unowned.append(entry.name)
            continue
        owned = _owned_path(store_root, entry.name)
        if owned is None:
            skipped_unowned.append(entry.name)
            continue
        if _has_active_job(owned):
            skipped_active.append(entry.name)
            continue
        try:
            mtime = owned.stat().st_mtime
        except OSError:
            continue
        if mtime <= cutoff:
            shutil.rmtree(owned, ignore_errors=False)
            deleted.append(entry.name)

    return CleanupReport(
        scanned=scanned,
        deleted=tuple(deleted),
        skipped_active=tuple(skipped_active),
        skipped_unowned=tuple(skipped_unowned),
    )
