"""Acceptance and regression tests for Issue #97: saved drafts and browser navigation recovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig, COOKIE_NAME  # noqa: E402
from repodocs.web.store import STORE_FILENAME, WebStore  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from test_security_acceptance_71 import TOOL_DIRECTORY, _browser_or_skip, _live_server  # noqa: E402
from test_web_jobs import Factory, request, shop  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"


def test_draft_persistence_optimistic_concurrency_and_secret_rejection(tmp_path: Path) -> None:
    app = create_app(
        WebSettings(
            AccessConfig(),
            WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)}),
            JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
        )
    )
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        headers = {"Origin": ORIGIN, "x-csrf-token": csrf}

        # Secret fields are refused in drafts
        bad = client.put(
            "/api/v1/drafts/draft_1",
            json={"data": {"api_key": "sk-secret", "mode": "architecture"}},
            headers=headers,
        )
        assert bad.status_code == 422

        # Save draft v1
        d1 = client.put(
            "/api/v1/drafts/draft_1",
            json={"data": {"workspace_name": "Shop", "mode": "deterministic", "step": "preview"}},
            headers=headers,
        )
        assert d1.status_code == 200
        assert d1.json()["version"] == 1

        # Update with matching expected_version=1 -> v2
        d2 = client.put(
            "/api/v1/drafts/draft_1",
            json={"data": {"workspace_name": "Shop", "mode": "architecture", "step": "limits"}, "expected_version": 1},
            headers=headers,
        )
        assert d2.status_code == 200
        assert d2.json()["version"] == 2

        # Stale tab writing with expected_version=1 is rejected with 409 conflict
        conflict = client.put(
            "/api/v1/drafts/draft_1",
            json={"data": {"workspace_name": "Stale"}, "expected_version": 1},
            headers=headers,
        )
        assert conflict.status_code == 409

        # Another session cannot read draft_1
        with TestClient(app, base_url=ORIGIN) as other:
            other.post("/api/v1/session", headers={"Origin": ORIGIN})
            assert other.get("/api/v1/drafts/draft_1", headers={"Origin": ORIGIN}).status_code == 404


def test_lost_submit_response_recovers_idempotently_across_restart(tmp_path: Path) -> None:
    ws_settings = WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)})
    app1 = create_app(WebSettings(AccessConfig(), ws_settings, JobSettings(run_limits=RunLimits(context_window_tokens=131072))))
    with TestClient(app1, base_url=ORIGIN) as c1:
        csrf1 = c1.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        cookie1 = c1.cookies.get(COOKIE_NAME) or ""
        h1 = {"Origin": ORIGIN, "x-csrf-token": csrf1}
        ws_id = c1.post("/api/v1/workspaces", json={"name": "Shop", "sources": [{"kind": "local", "name": "shop"}]}, headers=h1).json()["workspace_id"]
        pending_key = "lost-post-key-1"
        c1.put("/api/v1/drafts/draft_lost", json={"data": {"workspace_id": ws_id, "pending_idempotency_key": pending_key}}, headers=h1)
        sub = c1.post("/api/v1/jobs", json=request(ws_id, key=pending_key, mode="deterministic"), headers=h1)
        assert sub.status_code == 202
        orig_job_id = sub.json()["job_id"]

    # After restart, reconnecting with same cookie + re-submitting with persisted pending_idempotency_key returns existing job (200, not 202)
    app2 = create_app(WebSettings(AccessConfig(), ws_settings, JobSettings(run_limits=RunLimits(context_window_tokens=131072))))
    with TestClient(app2, base_url=ORIGIN) as c2:
        c2.cookies.set(COOKIE_NAME, cookie1, domain="127.0.0.1")
        csrf2 = c2.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        h2 = {"Origin": ORIGIN, "x-csrf-token": csrf2}
        restored = c2.get("/api/v1/drafts/draft_lost", headers={"Origin": ORIGIN}).json()
        replay = c2.post("/api/v1/jobs", json=request(ws_id, key=restored["data"]["pending_idempotency_key"], mode="deterministic"), headers=h2)
        assert replay.status_code == 200
        assert replay.json()["job_id"] == orig_job_id


def test_real_browser_back_forward_refresh_and_deep_link_restoration(tmp_path: Path) -> None:
    node, browser = _browser_or_skip()
    factory = Factory()
    with _live_server(tmp_path / "srv", factory) as base_url:
        res = subprocess.run(
            [node, str(TOOL_DIRECTORY / "check-nav-97.mjs"), base_url, browser, "shop"],
            cwd=TOOL_DIRECTORY,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert res.returncode == 0, f"check-nav-97 failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    assert factory.contexts == []
