"""Real-browser viewer E2E test (#75): verifies evidence map consumption, node/component navigation,
fit control, per-repository revision badges, downloads, and zero inference during navigation.
"""

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
from repodocs.architecture.render_check import find_browser, find_renderer  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from test_architecture_discovery import FIXTURE, full_answer  # noqa: E402
from test_web_jobs import Factory, Replay  # noqa: E402

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
def _live_server(tmp_path: Path, factory: Factory) -> Iterator[str]:
    port = _free_port()
    shop = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop)
    ws_settings = WebSettings(
        AccessConfig(bind_host="127.0.0.1", port=port),
        WorkspaceSettings(tmp_path / "store", {"shop": shop}),
        JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
        frontend_dist=FRONTEND_DIST,
    )
    app = create_app(ws_settings, provider_factory=factory)
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


def test_viewer_renders_real_evidence_map_and_never_retriggers_inference(tmp_path: Path) -> None:
    """Real Chrome renders Mermaid, consumes architecture-view-evidence.json, navigates evidence, and downloads."""
    node, browser = _browser_or_skip()
    provider = Replay(full_answer)
    factory = Factory(provider)
    with _live_server(tmp_path, factory) as base_url:
        flag = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
        completed = subprocess.run(
            [node, "check-viewer.mjs", base_url, browser, *flag],
            cwd=TOOL_DIRECTORY,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, f"Viewer E2E failed:\n{completed.stderr[-1200:]}\n{completed.stdout[-800:]}"
        report = json.loads(completed.stdout.strip().splitlines()[-1])

    assert report["pageErrors"] == []
    # Mermaid injects an internal <style> element inside rendered SVGs; no script-src or runtime errors may occur.
    non_style_console = [
        err for err in report["consoleErrors"]
        if "Applying inline style violates the following Content Security Policy" not in err
    ]
    assert non_style_console == []
    assert report["details"]["svgCount"] >= 1
    assert "architecture-view-evidence.json" in report["details"]["downloadLinks"]
    assert "working-tree (unverified)" in report["details"]["revisionText"]
    assert "Verified architecture" not in report["details"]["bodyText"]
    # Provider was built exactly once during job execution; viewing/navigation triggered no extra inference.
    assert len(factory.contexts) == 1
