"""Web installation, health/readiness, cleanup, and CLI independence tests (#76)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.web import server  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.cleanup import cleanup_workspaces  # noqa: E402
from repodocs.web.jobs import JOBS_DIRECTORY  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import MARKER_KIND, MARKER_NAME, MARKER_VERSION, WorkspaceSettings  # noqa: E402

from test_web_jobs import ORIGIN, settings  # noqa: E402


def _make_owned_workspace(store_root: Path, ws_id: str, *, age_seconds: float = 0.0, active_job: bool = False) -> Path:
    ws_dir = store_root / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "kind": MARKER_KIND,
        "schema_version": MARKER_VERSION,
        "workspace_id": ws_id,
        "created_at": time.time() - age_seconds,
    }
    (ws_dir / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    if active_job:
        jobs_dir = ws_dir / JOBS_DIRECTORY
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (jobs_dir / "job-1.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")
    if age_seconds > 0:
        old_time = time.time() - age_seconds
        os.utime(ws_dir, (old_time, old_time))
    return ws_dir


# ---------------------------------------------------------------------- health & readiness

def test_health_and_readiness_endpoints(tmp_path: Path) -> None:
    ws = settings(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    app = create_app(WebSettings(ws.access, ws.workspaces, ws.jobs, frontend_dist=dist))

    with TestClient(app, base_url=ORIGIN) as client:
        h = client.get("/health", headers={"Origin": ORIGIN})
        assert h.status_code == 200
        assert h.json() == {"status": "ok"}
        assert "access-control-allow-origin" not in h.headers
        assert str(tmp_path) not in h.text

        r = client.get("/ready", headers={"Origin": ORIGIN})
        assert r.status_code == 200
        assert r.json() == {"status": "ready", "ui_built": True}
        assert "access-control-allow-origin" not in r.headers
        assert str(tmp_path) not in r.text

        # Simulate shutdown state
        app.state.closed = True
        r_closed = client.get("/ready", headers={"Origin": ORIGIN})
        assert r_closed.status_code == 503
        assert r_closed.json() == {"status": "not-ready", "ui_built": True}


def test_readiness_reports_ui_built_false_when_dist_missing(tmp_path: Path) -> None:
    ws = settings(tmp_path)
    app = create_app(WebSettings(ws.access, ws.workspaces, ws.jobs, frontend_dist=tmp_path / "missing"))
    with TestClient(app, base_url=ORIGIN) as client:
        r = client.get("/ready", headers={"Origin": ORIGIN})
        assert r.status_code == 200
        assert r.json() == {"status": "ready", "ui_built": False}


# ---------------------------------------------------------------------- cleanup

def test_cleanup_removes_only_expired_inactive_owned_workspaces(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()

    expired_id = "ws-" + "a" * 32
    fresh_id = "ws-" + "b" * 32
    active_id = "ws-" + "c" * 32
    unowned_dir = store / "not-a-workspace"
    unowned_dir.mkdir()
    unmarked_ws = store / ("ws-" + "d" * 32)
    unmarked_ws.mkdir()

    expired_dir = _make_owned_workspace(store, expired_id, age_seconds=7200)
    fresh_dir = _make_owned_workspace(store, fresh_id, age_seconds=10)
    active_dir = _make_owned_workspace(store, active_id, age_seconds=7200, active_job=True)

    report = cleanup_workspaces(store, max_age_seconds=3600)
    assert report.deleted == (expired_id,)
    assert report.skipped_active == (active_id,)
    assert set(report.skipped_unowned) == {"not-a-workspace", "ws-" + "d" * 32}

    assert not expired_dir.exists()
    assert fresh_dir.exists()
    assert active_dir.exists()
    assert unowned_dir.exists()
    assert unmarked_ws.exists()


def test_cleanup_skips_symlinks(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete", encoding="utf-8")

    link_path = store / ("ws-" + "e" * 32)
    try:
        link_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available here")

    report = cleanup_workspaces(store, max_age_seconds=0)
    assert report.deleted == ()
    assert link_path.name in report.skipped_unowned
    assert (outside / "keep.txt").exists()


# ---------------------------------------------------------------------- server CLI

def test_server_build_settings_loopback_and_non_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store"
    repo = tmp_path / "repo"
    repo.mkdir()

    access, workspaces, dist, cleanup_hours = server.build_settings(
        ["--store-root", str(store), "--source", f"shop={repo}", "--port", "9001", "--cleanup-max-age-hours", "12"]
    )
    assert access.bind_host == "127.0.0.1"
    assert access.port == 9001
    assert not access.exposed
    assert workspaces.local_roots["shop"] == repo.resolve()
    assert dist is None
    assert cleanup_hours == 12.0

    # Non-loopback without public origin or auth token fails with exit code 2
    rc = server.main(["--store-root", str(store), "--host", "0.0.0.0"])
    assert rc == 2

    # Non-loopback with valid HTTPS origin and 32+ char token in env succeeds in build_settings
    monkeypatch.setenv("TEST_REPODOCS_TOKEN", "x" * 40)
    exposed_access, _, _, _ = server.build_settings(
        [
            "--store-root", str(store),
            "--host", "0.0.0.0",
            "--public-origin", "https://repodocs.example.com",
            "--auth-token-env", "TEST_REPODOCS_TOKEN",
        ]
    )
    assert exposed_access.exposed
    assert exposed_access.public_origin == "https://repodocs.example.com"


def test_core_cli_imports_without_web_dependencies() -> None:
    """The core repodocs CLI must remain importable even when fastapi/uvicorn are absent."""
    code = (
        "import sys; "
        "sys.modules['fastapi'] = None; "
        "sys.modules['uvicorn'] = None; "
        "from repodocs.cli import main; "
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
