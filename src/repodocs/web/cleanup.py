"""Bounded retention cleanup for server-owned web workspaces (#76).

Only marker-verified directories directly under ``store_root`` are eligible.
Symlinks, NTFS junctions, directories with active or uncertain job records,
and recently active workspaces are never deleted.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .jobs import CONTRACT_VERSION, JOBS_DIRECTORY, RECORD_VERSION
from .workspaces import MARKER_NAME, WORKSPACE_ID, _owned_path

if TYPE_CHECKING:
    from .store import WebStore

TERMINAL_STATES = frozenset(
    {"succeeded", "partial", "budget-exhausted", "cancelled", "failed", "interrupted", "stale"}
)


@dataclass(frozen=True)
class CleanupReport:
    """Summary of one bounded workspace cleanup pass."""

    scanned: int
    deleted: tuple[str, ...]
    skipped_active: tuple[str, ...]
    skipped_unowned: tuple[str, ...]


def _is_reparse_or_link(path: Path) -> bool:
    if path.is_symlink() or os.path.islink(path):
        return True
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return True
    # Windows NTFS junctions / mount points have FILE_ATTRIBUTE_REPARSE_POINT (0x400) or st_reparse_tag != 0.
    if getattr(st, "st_reparse_tag", 0):
        return True
    return False


def _inspect_jobs(workspace_path: Path) -> tuple[bool, float]:
    """Return (has_active_or_uncertain_job, latest_job_mtime)."""

    jobs_dir = workspace_path / JOBS_DIRECTORY
    if not jobs_dir.exists():
        return False, 0.0
    if _is_reparse_or_link(jobs_dir) or not jobs_dir.is_dir():
        return True, 0.0

    latest_mtime = 0.0
    for record_path in jobs_dir.iterdir():
        if _is_reparse_or_link(record_path):
            return True, latest_mtime
        if not record_path.is_file() or record_path.suffix != ".json":
            continue
        try:
            st = record_path.stat()
            latest_mtime = max(latest_mtime, st.st_mtime)
            data = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Unreadable or malformed job record: treat conservatively as uncertain/active.
            return True, latest_mtime
        if not isinstance(data, dict) or data.get("record_version") != RECORD_VERSION:
            return True, latest_mtime
        job = data.get("job")
        if not isinstance(job, dict):
            return True, latest_mtime
        if (
            job.get("contract_version") != CONTRACT_VERSION
            or job.get("kind") != "job"
            or job.get("workspace_id") != workspace_path.name
            or job.get("job_id") != record_path.stem
        ):
            return True, latest_mtime
        state = job.get("state")
        if not isinstance(state, str) or state not in TERMINAL_STATES:
            return True, latest_mtime
        created_at = data.get("created_at")
        if isinstance(created_at, (int, float)) and math.isfinite(created_at):
            latest_mtime = max(latest_mtime, float(created_at))
    return False, latest_mtime


def _latest_activity_time(workspace_path: Path, latest_job_time: float) -> float | None:
    timestamps = [latest_job_time]
    try:
        timestamps.append(workspace_path.stat().st_mtime)
    except OSError:
        return None
    marker = workspace_path / MARKER_NAME
    try:
        timestamps.append(marker.stat().st_mtime)
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
        created_at = marker_data.get("created_at")
        if isinstance(created_at, (int, float)) and math.isfinite(created_at):
            timestamps.append(float(created_at))
    except (OSError, ValueError, TypeError):
        return None
    return max(timestamps)


def cleanup_workspaces(
    store_root: Path,
    *,
    max_age_seconds: float,
    now: float | None = None,
    store: "WebStore | None" = None,
    include_saved: bool = False,
) -> CleanupReport:
    """Remove marker-owned workspaces older than ``max_age_seconds`` without active or uncertain jobs."""

    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be a finite non-negative number")
    if not store_root.exists() or _is_reparse_or_link(store_root) or not store_root.is_dir():
        return CleanupReport(0, (), (), ())

    resolved_root = store_root.resolve()
    current_time = time.time() if now is None else now
    cutoff = current_time - max_age_seconds
    scanned = 0
    deleted: list[str] = []
    skipped_active: list[str] = []
    skipped_unowned: list[str] = []

    for entry in sorted(store_root.iterdir(), key=lambda p: p.name):
        scanned += 1
        if _is_reparse_or_link(entry) or not entry.is_dir():
            skipped_unowned.append(entry.name)
            continue
        try:
            entry.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError):
            skipped_unowned.append(entry.name)
            continue
        if not WORKSPACE_ID.fullmatch(entry.name):
            skipped_unowned.append(entry.name)
            continue
        owned = _owned_path(store_root, entry.name)
        if owned is None:
            skipped_unowned.append(entry.name)
            continue

        has_active, latest_job_time = _inspect_jobs(owned)
        if has_active:
            skipped_active.append(entry.name)
            continue
        if store is not None and not include_saved and store.is_workspace_saved(entry.name):
            skipped_active.append(entry.name)
            continue
        latest_activity = _latest_activity_time(owned, latest_job_time)
        if latest_activity is None:
            skipped_active.append(entry.name)
            continue
        if latest_activity <= cutoff:
            shutil.rmtree(owned, ignore_errors=False)
            if store is not None:
                store.delete_workspace(entry.name)
            deleted.append(entry.name)

    return CleanupReport(
        scanned=scanned,
        deleted=tuple(deleted),
        skipped_active=tuple(skipped_active),
        skipped_unowned=tuple(skipped_unowned),
    )
