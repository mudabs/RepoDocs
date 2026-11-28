"""Real-browser end-to-end tests (#73) driving the built React UI against a live FastAPI/uvicorn server."""

from __future__ import annotations

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

from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.architecture.render_check import TOOL_DIRECTORY, find_browser, find_renderer  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from test_architecture_discovery import FIXTURE  # noqa: E402
from test_web_jobs import CANARY, Factory, Replay, asks  # noqa: E402

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
def _live_server(tmp_path: Path, factory: Factory | None = None) -> Iterator[str]:
    port = _free_port()
    shop1 = tmp_path / "approved" / "shop"
    shop2 = tmp_path / "approved" / "shop2"
    shutil.copytree(FIXTURE, shop1)
    shutil.copytree(FIXTURE, shop2)

    ws_settings = WebSettings(
        AccessConfig(bind_host="127.0.0.1", port=port),
        WorkspaceSettings(tmp_path / "store", {"shop": shop1, "shop2": shop2}),
        JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
        frontend_dist=FRONTEND_DIST,
    )
    app = create_app(ws_settings, provider_factory=factory or Factory())
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn failed to start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _run_browser(base_url: str, scenario: str) -> dict[str, Any]:
    node, browser = _browser_or_skip()
    flag = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
    completed = subprocess.run(
        [node, "check-web-workflow.mjs", base_url, browser, scenario, *flag],
        cwd=TOOL_DIRECTORY,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, f"Browser scenario {scenario} failed:\n{completed.stderr[-1200:]}\n{completed.stdout[-800:]}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_browser_deterministic_multi_repo_journey(tmp_path: Path) -> None:
    """Real Chrome drives session -> multi-repo workspace -> preview -> deterministic job -> succeeded."""
    _browser_or_skip()
    with _live_server(tmp_path) as base_url:
        report = _run_browser(base_url, "deterministic-multi")
    assert report["pageErrors"] == []
    assert report["consoleErrors"] == []
    assert report["a11y"]["hasMain"] is True
    assert report["a11y"]["hasH1"] is True
    assert report["a11y"]["unnamedButtons"] == 0
    assert report["storageCheck"]["localLen"] == 0
    assert report["storageCheck"]["sessionLen"] == 0
    assert "Omitted: none" in report["text"]
    assert "shop" in report["text"] and "shop2" in report["text"]


def test_browser_architecture_canary_and_partial_budget(tmp_path: Path) -> None:
    """Real Chrome connects OpenAI canary key without storage/DOM leak and labels partial budget clearly."""
    _browser_or_skip()
    provider = Replay(asks, name="openai", external=True)
    with _live_server(tmp_path, Factory(provider)) as base_url:
        report = _run_browser(base_url, "architecture-canary-partial")
    assert report["pageErrors"] == []
    assert report["consoleErrors"] == []
    assert report["leakCheck"] == {
        "localLen": 0,
        "sessionLen": 0,
        "inBody": False,
        "inHref": False,
        "inCookie": False,
    }
    assert "not current" in report["text"]
    # Canary must not appear in any persisted workspace/job file on disk
    for file_path in (tmp_path / "store").rglob("*"):
        if file_path.is_file():
            assert CANARY not in file_path.read_text(encoding="utf-8", errors="ignore")


def test_browser_cancel_mid_job(tmp_path: Path) -> None:
    """Real Chrome cancels a held architecture job mid-run."""
    _browser_or_skip()
    provider = Replay(asks).hold()
    with _live_server(tmp_path, Factory(provider)) as base_url:
        # Release the held provider shortly after it starts so the thread exits cleanly on cancel.
        def _releaser() -> None:
            if provider.started.wait(20):
                time.sleep(2.5)
                provider.release.set()

        t = threading.Thread(target=_releaser, daemon=True)
        t.start()
        report = _run_browser(base_url, "cancel-mid-job")
        t.join(timeout=5)
    assert report["pageErrors"] == []
    assert report["consoleErrors"] == []
    assert "Cancelled" in report["text"]


def test_browser_auth_token_session_clears_input(tmp_path: Path) -> None:
    """Self-hosted auth token input opens session and leaves no token in browser storage."""
    _browser_or_skip()
    with _live_server(tmp_path) as base_url:
        report = _run_browser(base_url, "auth-token-session")
    assert report["pageErrors"] == []
    assert report["consoleErrors"] == []
    assert report["storageCheck"] == {
        "localLen": 0,
        "sessionLen": 0,
        "inBody": False,
        "inHref": False,
    }


def test_browser_poller_survives_a_dropped_job_read(tmp_path: Path) -> None:
    """Regression: one failed job poll must not stop progress updates; the view keeps polling and shows the result."""
    _browser_or_skip()
    with _live_server(tmp_path) as base_url:
        report = _run_browser(base_url, "poll-transient-error")
    assert report["dropped"] == 1
    assert report["pageErrors"] == []
    assert "Succeeded" in report["text"]


def test_browser_slow_provider_preview_still_requires_consent_before_submit(tmp_path: Path) -> None:
    """Regression: the provider shows as connected only with its own preview, so a quick user reaches consent first."""
    _browser_or_skip()
    provider = Replay(asks, name="openai", external=True)
    with _live_server(tmp_path, Factory(provider)) as base_url:
        report = _run_browser(base_url, "consent-order-slow-preview")
    assert report["pageErrors"] == []
    assert report["submitBeforeConsent"] is False
    assert "not current" in report["text"]
