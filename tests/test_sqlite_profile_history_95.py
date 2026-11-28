"""Acceptance and regression tests for Issue #95: SQLite local profile, durable sessions, and workspace history."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
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


def _open(client: TestClient) -> str:
    response = client.post("/api/v1/session", headers={"Origin": ORIGIN})
    assert response.status_code == 201, response.text
    return response.json()["csrf_token"]


def _workspace(client: TestClient, csrf: str, name: str = "Shop") -> str:
    response = client.post(
        "/api/v1/workspaces",
        json={"name": name, "sources": [{"kind": "local", "name": "shop"}]},
        headers={"Origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert response.status_code == 201, response.text
    return response.json()["workspace_id"]


def _submit(client: TestClient, csrf: str, workspace_id: str) -> str:
    response = client.post(
        "/api/v1/jobs", json=request(workspace_id, mode="deterministic"), headers={"Origin": ORIGIN, "x-csrf-token": csrf}
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _finished(client: TestClient, job_id: str) -> dict:
    for _ in range(400):
        job = client.get(f"/api/v1/jobs/{job_id}", headers={"Origin": ORIGIN}).json()
        if job["state"] not in ("queued", "preflight", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _make_running(store_root: Path, workspace_id: str, job_id: str) -> None:
    """Rewrite a job file as if the server had stopped during discovery."""

    path = store_root / workspace_id / "jobs" / f"{job_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["job"].update(state="running", stage="discover", result=None)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_locked_store_times_out_with_store_error(tmp_path: Path) -> None:
    store = WebStore(tmp_path / STORE_FILENAME, busy_timeout_ms=100)
    holder = sqlite3.connect(str(tmp_path / STORE_FILENAME), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(StoreError):
            store.create_profile("prof_locked")
        assert time.monotonic() - started < 5
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert store.create_profile("prof_locked")["profile_id"] == "prof_locked"


def test_failing_metadata_writes_never_strand_a_job_and_reads_report_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN) as c:
        csrf = _open(c)
        ws_id = _workspace(c, csrf)

        def locked(*_: object, **__: object) -> None:
            raise StoreError("Metadata store error: database is locked")

        monkeypatch.setattr(WebStore, "save_job", locked)
        assert _finished(c, _submit(c, csrf, ws_id))["state"] == "succeeded"

        monkeypatch.setattr(WebStore, "list_workspaces", locked)
        listed = c.get("/api/v1/workspaces", headers={"Origin": ORIGIN})
        assert listed.status_code == 503
        assert listed.json()["error"]["code"] == "store-unavailable"
        assert listed.json()["error"]["retryable"] is True
        assert "locked" not in listed.text and str(tmp_path) not in listed.text


def test_interrupted_workspace_write_leaves_no_unowned_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN) as c:
        csrf = _open(c)

        def fail(*_: object, **__: object) -> None:
            raise StoreError("Metadata store error: disk I/O error")

        monkeypatch.setattr(WebStore, "save_workspace", fail)
        response = c.post(
            "/api/v1/workspaces",
            json={"name": "Lost", "sources": [{"kind": "local", "name": "shop"}]},
            headers={"Origin": ORIGIN, "x-csrf-token": csrf},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "store-unavailable"
    assert not list((tmp_path / "store").glob("ws-*"))


def test_restart_marks_running_job_interrupted_and_never_calls_a_provider(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as c1:
        csrf = _open(c1)
        cookie = c1.cookies.get(COOKIE_NAME)
        ws_id = _workspace(c1, csrf)
        job_id = _submit(c1, csrf, ws_id)
        _finished(c1, job_id)
    _make_running(tmp_path / "store", ws_id, job_id)

    factory = Factory()  # building any provider fails the test
    with TestClient(create_app(_settings(tmp_path), provider_factory=factory), base_url=ORIGIN) as c2:
        c2.cookies.set(COOKIE_NAME, cookie, domain="127.0.0.1")
        _open(c2)
        assert c2.get(f"/api/v1/jobs/{job_id}", headers={"Origin": ORIGIN}).json()["state"] == "interrupted"
        history = c2.get("/api/v1/workspaces", headers={"Origin": ORIGIN}).json()["workspaces"]
        assert history[0]["latest_job"]["state"] == "interrupted"
        time.sleep(0.3)
    assert factory.contexts == []
    stored = WebStore(tmp_path / "store" / STORE_FILENAME).list_jobs(history[0]["profile_id"], ws_id)
    assert stored[0]["state"] == "interrupted"


def test_legacy_workspaces_are_not_adopted_by_a_new_profile(tmp_path: Path) -> None:
    legacy = WebSettings(
        AccessConfig(),
        WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)}),
        JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
        use_store=False,
    )
    with TestClient(create_app(legacy), base_url=ORIGIN) as c1:
        csrf = _open(c1)
        ws_id = _workspace(c1, csrf)
        job_id = _submit(c1, csrf, ws_id)
        _finished(c1, job_id)
    _make_running(tmp_path / "store", ws_id, job_id)

    # Starting with the store over a pre-store layout must neither fail nor hand the old work to anyone new.
    with TestClient(create_app(_settings(tmp_path), provider_factory=Factory()), base_url=ORIGIN) as c2:
        _open(c2)
        assert c2.get("/api/v1/workspaces", headers={"Origin": ORIGIN}).json()["workspaces"] == []
        assert c2.get(f"/api/v1/jobs/{job_id}", headers={"Origin": ORIGIN}).status_code == 404
        assert c2.get(f"/api/v1/workspaces/{ws_id}/jobs", headers={"Origin": ORIGIN}).status_code == 404
    assert (tmp_path / "store" / ws_id).is_dir()


def test_file_and_metadata_owner_mismatch_is_shown_to_nobody(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as c1:
        csrf = _open(c1)
        cookie = c1.cookies.get(COOKIE_NAME)
        ws_id = _workspace(c1, csrf)
        gone_id = _workspace(c1, csrf, "Gone")
        job_id = _submit(c1, csrf, ws_id)
        _finished(c1, job_id)

    db = tmp_path / "store" / STORE_FILENAME
    WebStore(db).create_profile("prof_other")
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE workspaces SET profile_id = 'prof_other' WHERE workspace_id = ?", (ws_id,))
    conn.commit()
    conn.close()
    shutil.rmtree(tmp_path / "store" / gone_id)  # metadata without files

    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as c2:
        c2.cookies.set(COOKIE_NAME, cookie, domain="127.0.0.1")
        _open(c2)
        assert c2.get("/api/v1/workspaces", headers={"Origin": ORIGIN}).json()["workspaces"] == []
        assert c2.get(f"/api/v1/jobs/{job_id}", headers={"Origin": ORIGIN}).status_code == 404


def test_loopback_saved_session_lasts_30_days_and_exposed_keeps_8_hours(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as c:
        header = c.post("/api/v1/session", headers={"Origin": ORIGIN}).headers["set-cookie"]
        assert f"Max-Age={30 * 24 * 3600}" in header

    origin = "https://repodocs.example"
    access = AccessConfig(bind_host="0.0.0.0", port=8000, public_origin=origin, auth_token="t" * 40)
    with TestClient(create_app(_settings(tmp_path / "exposed", access=access)), base_url=origin) as c:
        header = c.post("/api/v1/session", json={"auth_token": "t" * 40}, headers={"Origin": origin}).headers["set-cookie"]
        assert f"Max-Age={8 * 3600}" in header


def test_server_refuses_to_start_or_clean_up_over_a_corrupt_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from repodocs.web import server

    store = tmp_path / "store"
    store.mkdir()
    (store / STORE_FILENAME).write_bytes(b"not a sqlite database")
    assert server.main(["--store-root", str(store)]) == 2
    assert server.main(["--store-root", str(store), "--cleanup-max-age-hours", "1"]) == 2
    assert "Restore" in capsys.readouterr().err
    assert (store / STORE_FILENAME).read_bytes() == b"not a sqlite database"
