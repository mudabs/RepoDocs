"""Optional web-only SQLite metadata store for local profiles, durable sessions, and workspace/job history (#95).

Core CLI and GitHub Actions never import or require this module. Source repositories, IR, manifests,
and generated artifacts remain in their existing owned filesystem layout under ``store_root``.
Provider credentials and consent receipts are never persisted here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2
STORE_FILENAME = "repodocs-web.sqlite3"

_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    display_name TEXT,
    preferences_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash BLOB PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    csrf_digest BLOB NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    saved INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspaces_profile ON workspaces(profile_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    error_json TEXT,
    result_json TEXT,
    usage_json TEXT NOT NULL,
    budget_json TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(profile_id, workspace_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id);
"""

_MIGRATION_V2 = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    data_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_profile ON drafts(profile_id);
"""


class StoreError(RuntimeError):
    """Raised when the web metadata store is corrupt, locked, or incompatible."""


@dataclass(frozen=True)
class StoredSession:
    profile_id: str
    csrf_digest: bytes
    expires_at: float


class WebStore:
    """Thread-safe, transactionally migrated SQLite metadata store for ``repodocs-web``."""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self._busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            conn.execute("PRAGMA journal_mode = WAL")
            return conn
        except sqlite3.Error as exc:
            raise StoreError(f"Could not open web metadata store: {exc}") from exc

    @contextmanager
    def _tx(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StoreError(f"Metadata store error: {exc}") from exc
            finally:
                conn.close()

    def _migrate(self) -> None:
        with self._tx(write=True) as conn:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            current = 0
            if has_table is not None:
                row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
                current = int(row["v"]) if row is not None and row["v"] is not None else 0
            if current > SCHEMA_VERSION:
                raise StoreError(
                    f"Unsupported metadata store schema version {current} (max supported {SCHEMA_VERSION})"
                )
            if current < 1:
                for stmt in _MIGRATION_V1.split(";"):
                    sql = stmt.strip()
                    if sql:
                        conn.execute(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, time.time()),
                )
            if current < 2:
                for stmt in _MIGRATION_V2.split(";"):
                    sql = stmt.strip()
                    if sql:
                        conn.execute(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, time.time()),
                )

    def schema_version(self) -> int:
        with self._tx(write=False) as conn:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            return int(row["v"]) if row is not None and row["v"] is not None else 0

    # ------------------------------------------------------------------ profiles & sessions
    def create_profile(
        self,
        profile_id: str,
        *,
        display_name: str | None = None,
        preferences: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else now
        prefs_json = json.dumps(preferences or {}, ensure_ascii=False, sort_keys=True)
        with self._tx(write=True) as conn:
            conn.execute(
                "INSERT INTO profiles(profile_id, display_name, preferences_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (profile_id, display_name, prefs_json, ts, ts),
            )
        return self.get_profile(profile_id) or {
            "profile_id": profile_id,
            "display_name": display_name,
            "preferences": preferences or {},
            "created_at": ts,
            "updated_at": ts,
        }

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self._tx(write=False) as conn:
            row = conn.execute("SELECT * FROM profiles WHERE profile_id = ?", (profile_id,)).fetchone()
            if row is None:
                return None
            return {
                "profile_id": row["profile_id"],
                "display_name": row["display_name"],
                "preferences": json.loads(row["preferences_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def update_profile(
        self,
        profile_id: str,
        *,
        display_name: str | None = ...,  # type: ignore[assignment]
        preferences: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get_profile(profile_id)
        if existing is None:
            return None
        ts = time.time() if now is None else now
        next_name = existing["display_name"] if display_name is ... else display_name
        next_prefs = existing["preferences"] if preferences is None else {**existing["preferences"], **preferences}
        with self._tx(write=True) as conn:
            conn.execute(
                "UPDATE profiles SET display_name = ?, preferences_json = ?, updated_at = ? WHERE profile_id = ?",
                (next_name, json.dumps(next_prefs, ensure_ascii=False, sort_keys=True), ts, profile_id),
            )
        return self.get_profile(profile_id)

    def put_session(
        self,
        token_hash: bytes,
        profile_id: str,
        csrf_digest: bytes,
        expires_at: float,
        *,
        replace_token_hash: bytes | None = None,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else now
        with self._tx(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (ts,))
            if replace_token_hash is not None:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (replace_token_hash,))
            conn.execute(
                "INSERT OR REPLACE INTO sessions(token_hash, profile_id, csrf_digest, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, profile_id, csrf_digest, expires_at, ts),
            )

    def get_session(self, token_hash: bytes, *, now: float | None = None) -> StoredSession | None:
        ts = time.time() if now is None else now
        with self._tx(write=False) as conn:
            row = conn.execute(
                "SELECT profile_id, csrf_digest, expires_at FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= ts:
                return None
            return StoredSession(row["profile_id"], bytes(row["csrf_digest"]), float(row["expires_at"]))

    def delete_session(self, token_hash: bytes) -> None:
        with self._tx(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_profile_sessions(self, profile_id: str) -> None:
        with self._tx(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,))

    # ------------------------------------------------------------------ workspaces & jobs
    def save_workspace(
        self,
        workspace_id: str,
        profile_id: str,
        name: str,
        sources: list[dict[str, Any]],
        *,
        saved: bool = True,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else now
        with self._tx(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO workspaces(workspace_id, profile_id, name, sources_json, saved, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM workspaces WHERE workspace_id = ?), ?), ?)",
                (workspace_id, profile_id, name, json.dumps(sources, ensure_ascii=False), 1 if saved else 0, workspace_id, ts, ts),
            )

    def list_workspaces(self, profile_id: str) -> list[dict[str, Any]]:
        with self._tx(write=False) as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces WHERE profile_id = ? ORDER BY updated_at DESC",
                (profile_id,),
            ).fetchall()
            return [
                {
                    "workspace_id": r["workspace_id"],
                    "profile_id": r["profile_id"],
                    "name": r["name"],
                    "sources": json.loads(r["sources_json"]),
                    "saved": bool(r["saved"]),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self._tx(write=False) as conn:
            r = conn.execute("SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
            if r is None:
                return None
            return {
                "workspace_id": r["workspace_id"],
                "profile_id": r["profile_id"],
                "name": r["name"],
                "sources": json.loads(r["sources_json"]),
                "saved": bool(r["saved"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }

    def is_workspace_saved(self, workspace_id: str) -> bool:
        with self._tx(write=False) as conn:
            row = conn.execute("SELECT saved FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
            return bool(row is not None and row["saved"])

    def delete_workspace(self, workspace_id: str) -> None:
        with self._tx(write=True) as conn:
            conn.execute("DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,))

    def save_job(
        self,
        *,
        job_id: str,
        workspace_id: str,
        profile_id: str,
        idempotency_key: str,
        request_hash: str,
        state: str,
        stage: str,
        sequence: int,
        error: dict[str, Any] | None,
        result: dict[str, Any] | None,
        usage: dict[str, Any],
        budget: dict[str, Any] | None,
        artifacts: dict[str, str],
        created_at: float,
        updated_at: float | None = None,
    ) -> None:
        ts = time.time() if updated_at is None else updated_at
        with self._tx(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs("
                "job_id, workspace_id, profile_id, idempotency_key, request_hash, state, stage, sequence, "
                "error_json, result_json, usage_json, budget_json, artifacts_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    workspace_id,
                    profile_id,
                    idempotency_key,
                    request_hash,
                    state,
                    stage,
                    sequence,
                    None if error is None else json.dumps(error, ensure_ascii=False),
                    None if result is None else json.dumps(result, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                    None if budget is None else json.dumps(budget, ensure_ascii=False),
                    json.dumps(artifacts, ensure_ascii=False),
                    created_at,
                    ts,
                ),
            )
            conn.execute("UPDATE workspaces SET updated_at = ? WHERE workspace_id = ?", (ts, workspace_id))

    def list_jobs(self, profile_id: str, workspace_id: str | None = None) -> list[dict[str, Any]]:
        with self._tx(write=False) as conn:
            if workspace_id is not None:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE profile_id = ? AND workspace_id = ? ORDER BY created_at DESC",
                    (profile_id, workspace_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE profile_id = ? ORDER BY created_at DESC",
                    (profile_id,),
                ).fetchall()
            return [
                {
                    "job_id": r["job_id"],
                    "workspace_id": r["workspace_id"],
                    "profile_id": r["profile_id"],
                    "idempotency_key": r["idempotency_key"],
                    "request_hash": r["request_hash"],
                    "state": r["state"],
                    "stage": r["stage"],
                    "sequence": r["sequence"],
                    "error": None if r["error_json"] is None else json.loads(r["error_json"]),
                    "result": None if r["result_json"] is None else json.loads(r["result_json"]),
                    "usage": json.loads(r["usage_json"]),
                    "budget": None if r["budget_json"] is None else json.loads(r["budget_json"]),
                    "artifacts": json.loads(r["artifacts_json"]),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    # ------------------------------------------------------------------ drafts (#97)
    def save_draft(
        self,
        draft_id: str,
        profile_id: str,
        data: dict[str, Any],
        *,
        expected_version: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else now
        payload_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._tx(write=True) as conn:
            row = conn.execute(
                "SELECT profile_id, version FROM drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                if expected_version is not None and expected_version != 0:
                    raise StoreError("Draft version conflict")
                version = 1
                conn.execute(
                    "INSERT INTO drafts(draft_id, profile_id, version, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (draft_id, profile_id, version, payload_json, ts),
                )
            else:
                if row["profile_id"] != profile_id:
                    raise StoreError("Draft belongs to another profile")
                current_version = int(row["version"])
                if expected_version is not None and expected_version != current_version:
                    raise StoreError(f"Draft version conflict: expected {expected_version}, found {current_version}")
                version = current_version + 1
                conn.execute(
                    "UPDATE drafts SET version = ?, data_json = ?, updated_at = ? WHERE draft_id = ?",
                    (version, payload_json, ts, draft_id),
                )
        return {"draft_id": draft_id, "profile_id": profile_id, "version": version, "data": data, "updated_at": ts}

    def get_draft(self, draft_id: str, profile_id: str) -> dict[str, Any] | None:
        with self._tx(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM drafts WHERE draft_id = ? AND profile_id = ?",
                (draft_id, profile_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "draft_id": row["draft_id"],
                "profile_id": row["profile_id"],
                "version": int(row["version"]),
                "data": json.loads(row["data_json"]),
                "updated_at": row["updated_at"],
            }

    def list_drafts(self, profile_id: str) -> list[dict[str, Any]]:
        with self._tx(write=False) as conn:
            rows = conn.execute(
                "SELECT * FROM drafts WHERE profile_id = ? ORDER BY updated_at DESC",
                (profile_id,),
            ).fetchall()
            return [
                {
                    "draft_id": r["draft_id"],
                    "profile_id": r["profile_id"],
                    "version": int(r["version"]),
                    "data": json.loads(r["data_json"]),
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    def delete_draft(self, draft_id: str, profile_id: str) -> bool:
        with self._tx(write=True) as conn:
            cur = conn.execute("DELETE FROM drafts WHERE draft_id = ? AND profile_id = ?", (draft_id, profile_id))
            return cur.rowcount > 0

    def backup_to(self, target_path: Path) -> None:
        """Create a consistent SQLite online backup at ``target_path``."""

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            src = self._connect()
            try:
                dst = sqlite3.connect(str(target_path))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
