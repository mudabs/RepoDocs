"""Acceptance and regression tests for Issue #95: SQLite local profile, durable sessions, and workspace history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.cleanup import cleanup_workspaces  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig, COOKIE_NAME  # noqa: E402
from repodocs.web.store import SCHEMA_VERSION, STORE_FILENAME, StoreError, WebStore  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from test_architecture_discovery import full_answer  # noqa: E402
from test_web_jobs import Factory, Replay, request, shop  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"


def _settings(tmp_path: Path, *, access: AccessConfig | None = None) -> WebSettings:
    return WebSettings(
        access or AccessConfig(),
        WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)}),
        JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
    )


def test_profile_and_workspace_history_survive_server_restart_with_cookie_rotation(tmp_path: Path) -> None:
    provider_factory = Factory(Replay(full_answer))
    app1 = create_app(_settings(tmp_path), provider_factory=provider_factory)
    with TestClient(app1, base_url=ORIGIN) as c1:
        s1 = c1.post("/api/v1/session", headers={"Origin": ORIGIN})
        assert s1.status_code == 201
        csrf1 = s1.json()["csrf_token"]
        cookie1 = c1.cookies.get(COOKIE_NAME)
        assert cookie1

        p_resp = c1.patch(
            "/api/v1/profile",
            json={"display_name": "Local Maintainer", "preferences": {"default_mode": "deterministic"}},
            headers={"Origin": ORIGIN, "x-csrf-token": csrf1},
        )
        assert p_resp.status_code == 200
        profile_id = p_resp.json()["profile_id"]

        ws_resp = c1.post(
            "/api/v1/workspaces",
            json={"name": "Saved Shop", "sources": [{"kind": "local", "name": "shop"}]},
            headers={"Origin": ORIGIN, "x-csrf-token": csrf1},
        )
        assert ws_resp.status_code == 201
        ws_id = ws_resp.json()["workspace_id"]

        job_resp = c1.post(
            "/api/v1/jobs",
            json=request(ws_id, mode="deterministic"),
            headers={"Origin": ORIGIN, "x-csrf-token": csrf1},
        )
        assert job_resp.status_code == 202
        job_id = job_resp.json()["job_id"]

    # Restart server against the same store_root; reconnect with cookie1 -> rotates cookie & preserves profile + history
    app2 = create_app(_settings(tmp_path), provider_factory=Factory())
    with TestClient(app2, base_url=ORIGIN) as c2:
        c2.cookies.set(COOKIE_NAME, cookie1, domain="127.0.0.1")
        s2 = c2.post("/api/v1/session", headers={"Origin": ORIGIN})
        assert s2.status_code == 201
        csrf2 = s2.json()["csrf_token"]
        cookie2 = c2.cookies.get(COOKIE_NAME, domain="127.0.0.1")
        assert cookie2 and cookie2 != cookie1  # rotated

        prof2 = c2.get("/api/v1/profile", headers={"Origin": ORIGIN}).json()
        assert prof2["profile_id"] == profile_id
        assert prof2["display_name"] == "Local Maintainer"
        assert prof2["preferences"]["default_mode"] == "deterministic"

        workspaces = c2.get("/api/v1/workspaces", headers={"Origin": ORIGIN}).json()["workspaces"]
        assert len(workspaces) == 1 and workspaces[0]["workspace_id"] == ws_id
        jobs = c2.get(f"/api/v1/workspaces/{ws_id}/jobs", headers={"Origin": ORIGIN}).json()["jobs"]
        assert len(jobs) == 1 and jobs[0]["job_id"] == job_id

        # Old cookie1 is invalidated after rotation
        c_old = TestClient(app2, base_url=ORIGIN)
        c_old.cookies.set(COOKIE_NAME, cookie1)
        assert c_old.get("/api/v1/profile", headers={"Origin": ORIGIN}).status_code == 401

        # A completely new browser session gets a separate profile and cannot access ws_id
        with TestClient(app2, base_url=ORIGIN) as c3:
            assert c3.post("/api/v1/session", headers={"Origin": ORIGIN}).status_code == 201
            assert c3.get(f"/api/v1/workspaces/{ws_id}/jobs", headers={"Origin": ORIGIN}).status_code == 404
            assert c3.get("/api/v1/workspaces", headers={"Origin": ORIGIN}).json()["workspaces"] == []


def test_logout_revokes_session_while_preserving_saved_history_and_delete_removes_both(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN) as c:
        csrf = c.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        ws_id = c.post(
            "/api/v1/workspaces",
            json={"name": "Keep Me", "sources": [{"kind": "local", "name": "shop"}]},
            headers={"Origin": ORIGIN, "x-csrf-token": csrf},
        ).json()["workspace_id"]

        # Logout revokes session cookie/CSRF
        end = c.post("/api/v1/session/end", headers={"Origin": ORIGIN, "x-csrf-token": csrf})
        assert end.status_code == 204
        assert c.get("/api/v1/workspaces", headers={"Origin": ORIGIN}).status_code == 401

        # Workspace still exists on disk and in SQLite store
        assert (tmp_path / "store" / ws_id).is_dir()
        store = WebStore(tmp_path / "store" / STORE_FILENAME)
        assert store.get_workspace(ws_id) is not None

        # Open a new session, create a workspace, then delete it explicitly
        csrf2 = c.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        ws2 = c.post(
            "/api/v1/workspaces",
            json={"name": "Delete Me", "sources": [{"kind": "local", "name": "shop"}]},
            headers={"Origin": ORIGIN, "x-csrf-token": csrf2},
        ).json()["workspace_id"]
        assert c.delete(f"/api/v1/workspaces/{ws2}", headers={"Origin": ORIGIN, "x-csrf-token": csrf2}).status_code == 204
        assert not (tmp_path / "store" / ws2).exists()
        assert store.get_workspace(ws2) is None


def test_secret_canaries_absent_from_sqlite_and_backup_and_non_loopback_still_requires_token(tmp_path: Path) -> None:
    api_canary = "sk-canary-95-never-persist"
    auth_token = "t" * 40
    access = AccessConfig(bind_host="0.0.0.0", port=8000, public_origin="https://repodocs.example", auth_token=auth_token)
    origin = "https://repodocs.example"
    app = create_app(_settings(tmp_path, access=access), provider_factory=Factory(Replay(full_answer)))
    with TestClient(app, base_url=origin) as c:
        # Missing auth_token rejected
        assert c.post("/api/v1/session", json={}, headers={"Origin": origin}).status_code == 401
        s = c.post("/api/v1/session", json={"auth_token": auth_token}, headers={"Origin": origin})
        assert s.status_code == 201
        csrf = s.json()["csrf_token"]
        cookie = c.cookies.get(COOKIE_NAME) or ""

        c.post(
            "/api/v1/providers",
            json={"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": api_canary},
            headers={"Origin": origin, "x-csrf-token": csrf},
        )
        c.post(
            "/api/v1/workspaces",
            json={"name": "Secret Check", "sources": [{"kind": "local", "name": "shop"}]},
            headers={"Origin": origin, "x-csrf-token": csrf},
        )

    db_path = tmp_path / "store" / STORE_FILENAME
    backup_path = tmp_path / "backup.sqlite3"
    store = WebStore(db_path)
    assert store.schema_version() == SCHEMA_VERSION
    store.backup_to(backup_path)

    for candidate in (db_path, backup_path):
        raw = candidate.read_bytes()
        assert api_canary.encode("utf-8") not in raw
        assert auth_token.encode("utf-8") not in raw
        assert cookie.encode("utf-8") not in raw


def test_corrupt_or_newer_schema_db_fails_closed_and_cleanup_preserves_saved_workspaces(tmp_path: Path) -> None:
    # 1. Corrupt DB raises StoreError cleanly
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(StoreError):
        WebStore(corrupt)

    # 2. Future schema version raises StoreError cleanly
    future = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(str(future))
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 1.0)")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError):
        WebStore(future)

    # 3. Saved workspace is never silently age-deleted by cleanup_workspaces
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN) as c:
        csrf = c.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        ws_id = c.post(
            "/api/v1/workspaces",
            json={"name": "Saved", "sources": [{"kind": "local", "name": "shop"}]},
            headers={"Origin": ORIGIN, "x-csrf-token": csrf},
        ).json()["workspace_id"]
    store = WebStore(tmp_path / "store" / STORE_FILENAME)
    report = cleanup_workspaces(tmp_path / "store", max_age_seconds=0.0, now=9e12, store=store)
    assert ws_id not in report.deleted
    assert (tmp_path / "store" / ws_id).is_dir()
