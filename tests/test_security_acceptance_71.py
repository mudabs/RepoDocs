"""Integrated security acceptance tests for Issue #71.

Exercises the real FastAPI/uvicorn HTTP adapter, JobService, RunGate, WorkspaceManager,
and headless Chrome React UI using replay providers and synthetic secret canaries.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

fastapi = pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.architecture.render_check import find_browser, find_renderer  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from conftest import git  # noqa: E402
from test_architecture_discovery import FIXTURE, component, connection, full_answer  # noqa: E402
from test_web_jobs import CANARY, Factory, Replay, asks, request  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOOL_DIRECTORY = ROOT / "tools" / "mermaid"
FRONTEND_DIST = ROOT / "frontend" / "dist"


def _browser_or_skip() -> tuple[str, str]:
    node = shutil.which("node")
    if node is None or find_renderer() is None or find_browser() is None or not (FRONTEND_DIST / "index.html").is_file():
        if os.environ.get("REPODOCS_REQUIRE_RENDERER") == "1":
            pytest.fail("REPODOCS_REQUIRE_RENDERER=1 but Node, Chrome, tools/mermaid, or frontend/dist is missing")
        pytest.skip("Node, Chrome, tools/mermaid, or built frontend/dist is not available")
    return node, find_browser()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _live_server(
    tmp_path: Path,
    factory: Factory | None = None,
    *,
    shop_source: Path | None = None,
    recorded_requests: list[tuple[str, str, str | None, int]] | None = None,
) -> Iterator[str]:
    port = _free_port()
    shop = shop_source or (tmp_path / "approved" / "shop")
    if not shop.exists():
        shutil.copytree(FIXTURE, shop)
    ws_settings = WebSettings(
        AccessConfig(bind_host="127.0.0.1", port=port),
        WorkspaceSettings(tmp_path / "store", {"shop": shop}),
        JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
        frontend_dist=FRONTEND_DIST,
    )
    app = create_app(ws_settings, provider_factory=factory or Factory())
    if recorded_requests is not None:
        @app.middleware("http")
        async def _record(req: Any, call_next: Any) -> Any:
            resp = await call_next(req)
            recorded_requests.append((req.method, req.url.path, req.headers.get("origin"), resp.status_code))
            return resp
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _run_security_browser(scenario: str, base_url: str, extra: str) -> dict[str, Any]:
    node, browser = _browser_or_skip()
    flag = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
    completed = subprocess.run(
        [node, "check-security-acceptance.mjs", scenario, base_url, browser, extra, *flag],
        cwd=TOOL_DIRECTORY,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, f"Browser scenario {scenario} failed:\n{completed.stderr[-1200:]}\n{completed.stdout[-800:]}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ 1. Unrelated websites & exposed auth

def test_browser_cross_origin_page_cannot_open_session_or_start_job(tmp_path: Path) -> None:
    """With Chrome holding the victim session + positive controls, an unrelated origin cannot start jobs."""
    _browser_or_skip()

    class _AttackerHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!doctype html><html><body>attacker</body></html>")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    recorded: list[tuple[str, str, str | None, int]] = []
    factory = Factory(Replay(full_answer))
    evil_port = _free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", evil_port), _AttackerHandler)
    evil_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    evil_thread.start()
    attacker_origin = f"http://127.0.0.1:{evil_port}"
    try:
        with _live_server(tmp_path, factory, recorded_requests=recorded) as base_url:
            report = _run_security_browser("cross-origin-attack", base_url, f"{attacker_origin}/")
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert report["pageErrors"] == []
    assert report["dialogs"] == []
    res = report["attackResults"]
    assert res["preflightedJob"]["readable"] is False
    assert res["simpleCorsJob"]["readable"] is False
    assert res["readJob"]["readable"] is False
    no_cors = res["noCorsJob"]
    assert (
        (no_cors.get("type") == "opaque" and no_cors.get("status") == 0)
        or "TypeError: Failed to fetch" in no_cors.get("error", "")
    ), no_cors

    ws_id = report["victimIds"]["workspaceId"]
    job_id = report["victimIds"]["jobId"]
    # Server-side telemetry: attacker cross-origin requests hit the server and were denied (403/401);
    # only the 2 positive-control jobs (deterministic + provider-backed) ran.
    attacker_hits = [r for r in recorded if r[2] == attacker_origin]
    assert any(m == "POST" and p == "/api/v1/jobs" and st == 403 for m, p, _o, st in attacker_hits), recorded
    assert any(m == "GET" and p == f"/api/v1/jobs/{job_id}" and st in (401, 403) for m, p, _o, st in attacker_hits), recorded
    assert len(factory.contexts) == 1
    jobs_dir = tmp_path / "store" / ws_id / "jobs"
    assert len(list(jobs_dir.glob("*.json"))) == 2


def test_self_hosted_auth_failure_and_success_boundary(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An exposed self-hosted configuration requires exact auth_token and never echoes it."""
    token = "self-hosted-token-" + "x" * 24
    origin = "https://repodocs.internal.example"
    shop = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop)
    app = create_app(
        WebSettings(
            AccessConfig(bind_host="127.0.0.1", port=8443, public_origin=origin, auth_token=token),
            WorkspaceSettings(tmp_path / "store", {"shop": shop}),
        )
    )
    with TestClient(app, base_url=origin) as client:
        r_missing = client.post("/api/v1/session", json={}, headers={"Origin": origin})
        assert r_missing.status_code in (401, 403)
        assert token not in r_missing.text

        r_wrong = client.post("/api/v1/session", json={"auth_token": "wrong"}, headers={"Origin": origin})
        assert r_wrong.status_code in (401, 403)
        assert token not in r_wrong.text

        r_ok = client.post("/api/v1/session", json={"auth_token": token}, headers={"Origin": origin})
        assert r_ok.status_code == 201
        assert token not in r_ok.text
        assert "access-control-allow-origin" not in r_ok.headers
    assert token not in caplog.text


# ------------------------------------------------------------------ 2. Credential isolation & env keys

def _openai_stand_in(received: list[tuple[int, str, str | None]], barrier: threading.Barrier) -> Any:
    """Loopback OpenAI /responses stand-in synchronized on a barrier to prove true concurrent execution."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.server.server_port, self.path, self.headers.get("Authorization")))
            if self.path.endswith("/input_tokens"):
                payload = json.dumps({"input_tokens": 11}).encode()
            else:
                barrier.wait()
                user_msg = json.loads(body["input"][0]["content"])
                content = json.dumps(full_answer(user_msg))
                payload = json.dumps({
                    "id": "resp_test",
                    "model": body["model"],
                    "status": "completed",
                    "output": [{"type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": content}]}],
                    "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_concurrent_keyed_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, *, max_active_jobs: int,
) -> None:
    from repodocs.web.jobs import build_provider

    env_canary = "sk-env-operator-canary-999999999999999999"
    canary_a = "sk-session-a-canary-11111111111111111111"
    canary_b = "sk-session-b-canary-22222222222222222222"
    monkeypatch.setenv("OPENAI_API_KEY", env_canary)
    monkeypatch.setenv("ANTHROPIC_API_KEY", env_canary)
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", env_canary)
    caplog.set_level("DEBUG")

    received: list[tuple[int, str, str | None]] = []
    barrier = threading.Barrier(2, timeout=3.0)
    srv_a = _openai_stand_in(received, barrier)
    srv_b = _openai_stand_in(received, barrier)
    shop = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop)
    app = create_app(
        WebSettings(
            AccessConfig(),
            WorkspaceSettings(tmp_path / "store", {"shop": shop}),
            JobSettings(max_active_jobs=max_active_jobs, run_limits=RunLimits(context_window_tokens=131072)),
        ),
        provider_factory=build_provider,
    )
    origin = "http://127.0.0.1:8000"
    try:
        with TestClient(app, base_url=origin) as client_a, TestClient(app, base_url=origin) as client_b:
            sa = client_a.post("/api/v1/session", headers={"Origin": origin}).json()["csrf_token"]
            sb = client_b.post("/api/v1/session", headers={"Origin": origin}).json()["csrf_token"]
            ha = {"Origin": origin, "x-csrf-token": sa}
            hb = {"Origin": origin, "x-csrf-token": sb}

            wsa = client_a.post("/api/v1/workspaces", json={"name": "A", "sources": [{"kind": "local", "name": "shop"}]}, headers=ha).json()["workspace_id"]
            wsb = client_b.post("/api/v1/workspaces", json={"name": "B", "sources": [{"kind": "local", "name": "shop"}]}, headers=hb).json()["workspace_id"]

            pa = client_a.post("/api/v1/providers", json={"provider": "openai", "host": f"http://127.0.0.1:{srv_a.server_port}/v1", "model": "gpt-4o-mini", "api_key": canary_a}, headers=ha).json()["provider_session_id"]
            pb = client_b.post("/api/v1/providers", json={"provider": "openai", "host": f"http://127.0.0.1:{srv_b.server_port}/v1", "model": "gpt-4o-mini", "api_key": canary_b}, headers=hb).json()["provider_session_id"]

            pre_a = client_a.post(f"/api/v1/workspaces/{wsa}/preview", json={"provider_session_id": pa}, headers=ha).json()
            pre_b = client_b.post(f"/api/v1/workspaces/{wsb}/preview", json={"provider_session_id": pb}, headers=hb).json()

            ja = client_a.post("/api/v1/jobs", json=request(wsa, key="ca-1", provider=pa, context=pre_a["context_hash"]), headers=ha).json()["job_id"]
            jb = client_b.post("/api/v1/jobs", json=request(wsb, key="cb-1", provider=pb, context=pre_b["context_hash"]), headers=hb).json()["job_id"]

            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                ra = client_a.get(f"/api/v1/jobs/{ja}", headers={"Origin": origin}).json()
                rb = client_b.get(f"/api/v1/jobs/{jb}", headers={"Origin": origin}).json()
                if ra["state"] in ("succeeded", "failed") and rb["state"] in ("succeeded", "failed"):
                    break
                time.sleep(0.02)

            assert ra["state"] == "succeeded", ra
            assert rb["state"] == "succeeded", rb

            # Cross-session isolation and disconnect revocation
            assert client_b.get(f"/api/v1/jobs/{ja}", headers={"Origin": origin}).status_code == 404
            assert client_b.get(f"/api/v1/providers/{pa}", headers={"Origin": origin}).status_code == 404
            assert client_a.post(f"/api/v1/providers/{pa}/disconnect", headers=ha).status_code == 204
            assert client_a.get(f"/api/v1/providers/{pa}", headers={"Origin": origin}).status_code == 404

            for aid in rb["result"]["artifact_ids"]:
                content = client_b.get(f"/api/v1/jobs/{jb}/artifacts/{aid}", headers={"Origin": origin}).text
                assert canary_a not in content and env_canary not in content
    finally:
        for srv in (srv_a, srv_b):
            srv.shutdown()
            srv.server_close()

    by_port = {port: [auth for p, path, auth in received if p == port and path.endswith("/responses")] for port in (srv_a.server_port, srv_b.server_port)}
    assert by_port[srv_a.server_port] == [f"Bearer {canary_a}"]
    assert by_port[srv_b.server_port] == [f"Bearer {canary_b}"]
    for file_path in (tmp_path / "store").rglob("*"):
        if file_path.is_file():
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            assert canary_a not in text and canary_b not in text and env_canary not in text
    assert canary_a not in caplog.text and canary_b not in caplog.text and env_canary not in caplog.text


def test_concurrent_sessions_isolate_canaries_and_block_env_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Two overlapping keyed sessions hit real local OpenAI transports together with isolated canaries."""
    _run_concurrent_keyed_sessions(tmp_path, monkeypatch, caplog, max_active_jobs=2)


def test_concurrent_credential_isolation_fails_when_restricted_to_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Restricting JobSettings to max_active_jobs=1 fails the Barrier(2) overlap check."""
    with pytest.raises(AssertionError):
        _run_concurrent_keyed_sessions(tmp_path, monkeypatch, caplog, max_active_jobs=1)


# ------------------------------------------------------------------ 3. Mid-run consent revocation & scope binding

def test_mid_run_consent_revocation_lets_inflight_finish_and_blocks_next_request(tmp_path: Path) -> None:
    """Revoking consent while request #1 is in flight allows #1 to finish and blocks request #2."""
    shop = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop)
    provider = Replay(asks, full_answer, name="openai", external=True).hold()
    app = create_app(
        WebSettings(
            AccessConfig(),
            WorkspaceSettings(tmp_path / "store", {"shop": shop}),
            JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
        ),
        provider_factory=Factory(provider),
    )
    origin = "http://127.0.0.1:8000"
    with TestClient(app, base_url=origin) as client:
        csrf = client.post("/api/v1/session", headers={"Origin": origin}).json()["csrf_token"]
        hdrs = {"Origin": origin, "x-csrf-token": csrf}
        ws = client.post("/api/v1/workspaces", json={"name": "Shop", "sources": [{"kind": "local", "name": "shop"}]}, headers=hdrs).json()["workspace_id"]
        prov = client.post("/api/v1/providers", json={"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": CANARY}, headers=hdrs).json()["provider_session_id"]
        pre = client.post(f"/api/v1/workspaces/{ws}/preview", json={"provider_session_id": prov}, headers=hdrs).json()
        receipt = client.post("/api/v1/consents", json={"workspace_id": ws, "provider_session_id": prov, "context_hash": pre["context_hash"], "kinds": ["external", "source-excerpts"]}, headers=hdrs).json()["consent_receipt_id"]

        job_id = client.post("/api/v1/jobs", json=request(ws, key="revoke-mid-1", provider=prov, context=pre["context_hash"], receipt=receipt, max_requests=3), headers=hdrs).json()["job_id"]
        assert provider.started.wait(15)

        # Revoke consent while request #1 is in flight, then release request #1
        assert client.post(f"/api/v1/consents/{receipt}/revoke", headers=hdrs).status_code == 204
        provider.release.set()

        deadline = time.monotonic() + 20
        final_job: dict[str, Any] = {}
        while time.monotonic() < deadline:
            final_job = client.get(f"/api/v1/jobs/{job_id}", headers={"Origin": origin}).json()
            if final_job["state"] in ("succeeded", "partial", "failed", "cancelled", "budget-exhausted"):
                break
            time.sleep(0.02)

        assert final_job["state"] != "succeeded"
        assert final_job["error"] is not None
        assert final_job["error"]["code"] == "consent-required"
        # Request #1 was in-flight and recorded; request #2 (full_answer) never started.
        assert final_job["budget"]["requests"] == 1
        assert len(provider.script) == 1


def test_provider_model_or_host_switch_cannot_inherit_consent(tmp_path: Path) -> None:
    """Switching model or host refuses job submission with the previous consent receipt."""
    shop = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop)
    app = create_app(
        WebSettings(AccessConfig(), WorkspaceSettings(tmp_path / "store", {"shop": shop})),
        provider_factory=Factory(),
    )
    origin = "http://127.0.0.1:8000"
    with TestClient(app, base_url=origin) as client:
        csrf = client.post("/api/v1/session", headers={"Origin": origin}).json()["csrf_token"]
        hdrs = {"Origin": origin, "x-csrf-token": csrf}
        ws = client.post("/api/v1/workspaces", json={"name": "Shop", "sources": [{"kind": "local", "name": "shop"}]}, headers=hdrs).json()["workspace_id"]
        p1 = client.post("/api/v1/providers", json={"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": CANARY}, headers=hdrs).json()["provider_session_id"]
        pre = client.post(f"/api/v1/workspaces/{ws}/preview", json={"provider_session_id": p1}, headers=hdrs).json()
        receipt = client.post("/api/v1/consents", json={"workspace_id": ws, "provider_session_id": p1, "context_hash": pre["context_hash"], "kinds": ["external", "source-excerpts"]}, headers=hdrs).json()["consent_receipt_id"]

        # Switch model on same host
        p2 = client.post("/api/v1/providers", json={"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-4o", "api_key": CANARY}, headers=hdrs).json()["provider_session_id"]
        r_model = client.post("/api/v1/jobs", json=request(ws, key="switch-model", provider=p2, context=pre["context_hash"], receipt=receipt), headers=hdrs)
        assert r_model.status_code == 403
        assert r_model.json()["error"]["code"] == "consent-required"

        # Switch host with same provider and model
        p3 = client.post("/api/v1/providers", json={"provider": "openai", "host": "https://api.example.invalid/v1", "model": "gpt-4o-mini", "api_key": CANARY}, headers=hdrs).json()["provider_session_id"]
        r_host = client.post("/api/v1/jobs", json=request(ws, key="switch-host", provider=p3, context=pre["context_hash"], receipt=receipt), headers=hdrs)
        assert r_host.status_code == 403
        assert r_host.json()["error"]["code"] == "consent-required"


# ------------------------------------------------------------------ 4. Hostile content + pinned GitHub links

def _hostile_pinned_answer(message: dict[str, Any]) -> dict[str, Any]:
    docs = message["excerpts"]
    d0 = docs[0]
    c1 = component('API <script>alert(1)</script> <img src=x onerror=alert(1)>', "backend-service", [d0["path"]], [d0["id"]])
    c2 = component("[xss](javascript:alert(1)) Worker", "frontend", [d0["path"]], [d0["id"]])
    return {
        "complete": True,
        "components": [c1, c2],
        "connections": [
            connection(c1["name"], c2["name"], "calls", 'javascript:alert(1) <svg onload=alert(1)>', [d0["id"]]),
        ],
        "alternatives": [],
        "requests": [],
    }


def test_browser_hostile_content_with_pinned_github_links_never_executes(tmp_path: Path) -> None:
    """Hostile labels/Markdown/SVG/URLs never execute in Chrome while pinned GitHub #Lx-Ly links stay valid."""
    _browser_or_skip()
    shop = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop)
    git(shop, "init")
    git(shop, "remote", "add", "origin", "https://github.com/example/shop.git")
    git(shop, "add", ".")
    git(shop, "commit", "-m", "initial")

    provider = Replay(_hostile_pinned_answer)
    with _live_server(tmp_path, Factory(provider), shop_source=shop) as base_url:
        report = _run_security_browser("hostile-pinned-viewer", base_url, "")

    assert report["pageErrors"] == []
    assert report["dialogs"] == []
    assert report["externalRequests"] == []
    assert report["consoleErrors"] == []

    hrefs = [item["href"] for item in report["links"]]
    assert not any(h.startswith("javascript:") or h.startswith("data:") or "evil.example" in h for h in hrefs)
    assert any(h.startswith("https://github.com/example/shop/blob/") and "#L" in h for h in hrefs), f"Missing pinned #Lx-Ly link in {hrefs}"
    for item in report["links"]:
        if item["href"].startswith("https://github.com/"):
            assert "noopener" in item["rel"] and "noreferrer" in item["rel"]


# ------------------------------------------------------------------ 5. Restart recovery

def test_restart_recovery_marks_unfinished_job_interrupted_and_never_replays(tmp_path: Path) -> None:
    """Restarting the service marks in-flight jobs interrupted and never re-invokes the provider."""
    shop = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop)
    ws_settings = WorkspaceSettings(tmp_path / "store", {"shop": shop})
    p1 = Replay(asks).hold()
    app1 = create_app(WebSettings(AccessConfig(), ws_settings, JobSettings(run_limits=RunLimits(context_window_tokens=131072))), provider_factory=Factory(p1))
    origin = "http://127.0.0.1:8000"
    with TestClient(app1, base_url=origin) as c1:
        csrf = c1.post("/api/v1/session", headers={"Origin": origin}).json()["csrf_token"]
        hdrs = {"Origin": origin, "x-csrf-token": csrf}
        ws = c1.post("/api/v1/workspaces", json={"name": "Shop", "sources": [{"kind": "local", "name": "shop"}]}, headers=hdrs).json()["workspace_id"]
        prov = c1.post("/api/v1/providers", json={"provider": "ollama", "host": "http://127.0.0.1:11434", "model": "replay-model"}, headers=hdrs).json()["provider_session_id"]
        pre = c1.post(f"/api/v1/workspaces/{ws}/preview", json={"provider_session_id": prov}, headers=hdrs).json()
        job_id = c1.post("/api/v1/jobs", json=request(ws, key="restart-1", provider=prov, context=pre["context_hash"]), headers=hdrs).json()["job_id"]
        assert p1.started.wait(15)
        p1.release.set()

    # Simulate ungraceful restart where the persisted record was left in 'running' state
    rec_path = tmp_path / "store" / ws / "jobs" / f"{job_id}.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    rec["job"]["state"] = "running"
    rec["job"]["error"] = None
    rec_path.write_text(json.dumps(rec), encoding="utf-8")

    # Restart with a fresh app and an empty Factory (any provider call would fail!)
    app2 = create_app(WebSettings(AccessConfig(), ws_settings), provider_factory=Factory())
    with TestClient(app2, base_url=origin) as c2:
        csrf2 = c2.post("/api/v1/session", headers={"Origin": origin}).json()["csrf_token"]
        assert csrf2
    recovered = json.loads(rec_path.read_text(encoding="utf-8"))
    assert recovered["job"]["state"] == "interrupted"
    assert recovered["job"]["error"]["code"] == "interrupted"
