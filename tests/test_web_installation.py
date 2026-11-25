"""Web installation, health/readiness, cleanup, and CLI independence tests (#76)."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.web import server  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.cleanup import cleanup_workspaces  # noqa: E402
from repodocs.web.jobs import JOBS_DIRECTORY, RECORD_VERSION  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import MARKER_KIND, MARKER_NAME, MARKER_VERSION, WorkspaceSettings  # noqa: E402

from test_architecture_discovery import full_answer  # noqa: E402
from test_web_jobs import ORIGIN, Factory, Replay, request, running, settings  # noqa: E402


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
        (jobs_dir / "job-1.json").write_text(
            json.dumps(
                {
                    "record_version": RECORD_VERSION,
                    "created_at": time.time() - age_seconds,
                    "job": {"job_id": "job-1", "workspace_id": ws_id, "state": "running"},
                }
            ),
            encoding="utf-8",
        )
    if age_seconds > 0:
        old_time = time.time() - age_seconds
        os.utime(ws_dir / MARKER_NAME, (old_time, old_time))
        if (ws_dir / JOBS_DIRECTORY).is_dir():
            for p in (ws_dir / JOBS_DIRECTORY).iterdir():
                os.utime(p, (old_time, old_time))
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


def test_cleanup_preserves_workspace_with_real_persisted_running_job(tmp_path: Path) -> None:
    """Cleanup reading real JobService-persisted records never deletes an active workspace."""
    provider = Replay(full_answer).hold()
    store = tmp_path / "store"
    with running(tmp_path, Factory(provider)) as (api, _app, _factory):
        workspace_id = api.workspace()
        prov_id = api.provider()
        preview = api.preview(workspace_id, prov_id)
        r = api.submit(request(workspace_id, key="cleanup-real-1", provider=prov_id, context=preview["context_hash"]))
        assert r.status_code == 202
        assert provider.started.wait(15)

        ws_dir = store / workspace_id
        assert (ws_dir / JOBS_DIRECTORY).is_dir()
        # Even if the root directory mtime is backdated, the real nested running record protects it.
        old_time = time.time() - 7200
        os.utime(ws_dir, (old_time, old_time))

        report = cleanup_workspaces(store, max_age_seconds=3600)
        assert workspace_id in report.skipped_active
        assert workspace_id not in report.deleted
        assert ws_dir.exists()

        provider.release.set()
        api.wait(r.json()["job_id"])


def test_cleanup_preserves_uncertain_job_record_and_rejects_invalid_retention(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    ws_id = "ws-" + "f" * 32
    ws_dir = _make_owned_workspace(store, ws_id, age_seconds=7200)
    jobs_dir = ws_dir / JOBS_DIRECTORY
    jobs_dir.mkdir()
    (jobs_dir / "corrupt.json").write_text("{not valid json", encoding="utf-8")

    report = cleanup_workspaces(store, max_age_seconds=3600)
    assert ws_id in report.skipped_active
    assert ws_dir.exists()

    for bad_age in (-1.0, math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            cleanup_workspaces(store, max_age_seconds=bad_age)

    assert server.main(["--store-root", str(store), "--cleanup-max-age-hours", "-5"]) == 2
    assert server.main(["--store-root", str(store), "--cleanup-max-age-hours", "nan"]) == 2


def test_wheel_packages_frontend_assets_and_runs_outside_checkout(tmp_path: Path) -> None:
    """Building a wheel bundles frontend assets and serves the UI outside the checkout without Node."""
    root = Path(__file__).resolve().parents[1]
    frontend_dist = root / "frontend" / "dist"
    static_dir = root / "src" / "repodocs" / "web" / "static"
    if not (frontend_dist / "index.html").is_file():
        if os.environ.get("REPODOCS_REQUIRE_RENDERER") == "1":
            pytest.fail("REPODOCS_REQUIRE_RENDERER=1 but frontend/dist is missing")
        pytest.skip("frontend/dist is not built")

    created_static = not static_dir.exists()
    wheel_dir = tmp_path / "wheels"
    install_dir = tmp_path / "site"
    outside_dir = tmp_path / "outside"
    wheel_dir.mkdir()
    install_dir.mkdir()
    outside_dir.mkdir()

    try:
        if static_dir.exists():
            shutil.rmtree(static_dir)
        shutil.copytree(frontend_dist, static_dir)

        wheel_proc = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(wheel_dir), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert wheel_proc.returncode == 0, wheel_proc.stderr
        wheels = list(wheel_dir.glob("repodocs-*.whl"))
        assert len(wheels) == 1

        with zipfile.ZipFile(wheels[0]) as zf:
            names = set(zf.namelist())
        assert "repodocs/web/static/index.html" in names
        assert any(n.startswith("repodocs/web/static/assets/") for n in names)

        install_proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(install_dir), str(wheels[0])],
            capture_output=True,
            text=True,
            check=False,
        )
        assert install_proc.returncode == 0, install_proc.stderr

        # Run from outside the checkout with installed package first on sys.path
        verify_code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from pathlib import Path; "
            "from fastapi.testclient import TestClient; "
            "from repodocs.web.app import WebSettings, create_app; "
            "from repodocs.web.security import AccessConfig; "
            "from repodocs.web.workspaces import WorkspaceSettings; "
            "store = Path(sys.argv[2]); store.mkdir(exist_ok=True); "
            "app = create_app(WebSettings(AccessConfig(), WorkspaceSettings(store, {}))); "
            "c = TestClient(app, base_url='http://127.0.0.1:8000'); "
            "assert c.get('/').status_code == 200; "
            "assert c.get('/viewer/job-1').status_code == 200; "
            "assert c.get('/ready').json() == {'status': 'ready', 'ui_built': True}; "
            "assert c.get('/health').json() == {'status': 'ok'}; "
            "assert c.get('/api/v1/jobs/x', headers={'Origin': 'http://127.0.0.1:8000'}).status_code == 401; "
            "print('wheel-ok')"
        )
        run_proc = subprocess.run(
            [sys.executable, "-c", verify_code, str(install_dir), str(outside_dir / "store")],
            cwd=outside_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert run_proc.returncode == 0, run_proc.stderr
        assert "wheel-ok" in run_proc.stdout

        # Standalone CLI runs outside checkout with empty PATH (no Node)
        cli_proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from repodocs.cli import main; raise SystemExit(main(['--help']))", str(install_dir)],
            cwd=outside_dir,
            env={"PATH": ""},
            capture_output=True,
            text=True,
            check=False,
        )
        assert cli_proc.returncode == 0, cli_proc.stderr
    finally:
        if created_static and static_dir.exists():
            shutil.rmtree(static_dir, ignore_errors=True)
