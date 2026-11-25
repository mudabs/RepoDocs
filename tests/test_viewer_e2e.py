"""Real-browser viewer E2E tests (#75): verifies SVG node/edge selection, parallel edges,
cross-repository citations, hostile label safety, actual artifact downloads, and zero inference on navigation.
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

from test_architecture_discovery import FIXTURE, component, connection, full_answer  # noqa: E402
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
    shop2 = tmp_path / "approved" / "shop2"
    shutil.copytree(FIXTURE, shop)
    shutil.copytree(FIXTURE, shop2)
    ws_settings = WebSettings(
        AccessConfig(bind_host="127.0.0.1", port=port),
        WorkspaceSettings(tmp_path / "store", {"shop": shop, "shop2": shop2}),
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


def _run_viewer(base_url: str, scenario: str) -> dict[str, Any]:
    node, browser = _browser_or_skip()
    flag = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
    completed = subprocess.run(
        [node, "check-viewer.mjs", base_url, browser, scenario, *flag],
        cwd=TOOL_DIRECTORY,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, f"Viewer E2E ({scenario}) failed:\n{completed.stderr[-1200:]}\n{completed.stdout[-800:]}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _assert_clean_report(report: dict[str, Any]) -> None:
    assert report["pageErrors"] == []
    assert report["dialogs"] == []
    assert report["badLinks"] == []
    non_style_console = [
        err for err in report["consoleErrors"]
        if "Applying inline style violates the following Content Security Policy" not in err
    ]
    assert non_style_console == []
    assert report["details"]["svgCount"] >= 1
    assert "Verified architecture" not in report["details"]["bodyText"]
    assert len(report["downloaded"]) >= 2
    for item in report["downloaded"]:
        assert item["status"] == 200
        assert "attachment" in item["disposition"]
        assert item["bytes"] > 0


def test_viewer_node_edge_selection_and_actual_downloads(tmp_path: Path) -> None:
    """Real Chrome clicks rendered SVG node and edge, verifies downloads, and triggers no extra inference."""
    _browser_or_skip()
    provider = Replay(full_answer)
    factory = Factory(provider)
    with _live_server(tmp_path, factory) as base_url:
        report = _run_viewer(base_url, "single-repo-viewer")

    _assert_clean_report(report)
    assert report["edgeCount"] >= 1
    assert "Support:" in report["nodePanelText"]
    assert "Evidence:" in report["nodePanelText"]
    assert "Support:" in report["edgePanelText"] or "Evidence IDs:" in report["edgePanelText"]
    assert "working-tree (unverified)" in report["details"]["revisionText"]
    assert any(d["name"] == "architecture-view-evidence.json" for d in report["downloaded"])
    assert len(factory.contexts) == 1


def _cross_repo_hostile_answer(message: dict[str, Any]) -> dict[str, Any]:
    """Scripted two-repo answer with parallel edges + different endpoint pair sharing identical citations."""
    docs = message["excerpts"]
    shop_doc = next(d for d in docs if d["repository_id"] == "shop")
    shop2_doc = next(d for d in docs if d["repository_id"] == "shop2")
    shared_citations = [shop_doc["id"], shop2_doc["id"]]
    c1 = component('Shop <script>alert(1)</script> API', "backend-service", [shop_doc["path"]], [shop_doc["id"]], repository="shop")
    c2 = component("Shop2 Worker", "frontend", [shop2_doc["path"]], [shop2_doc["id"]], repository="shop2")
    c3 = component("Shop2 Cache", "storage", [shop2_doc["path"]], [shop2_doc["id"]], repository="shop2")
    return {
        "complete": True,
        "components": [c1, c2, c3],
        "connections": [
            connection(c1["name"], c2["name"], "calls", "HTTP call", shared_citations, source_repository="shop", target_repository="shop2"),
            connection(c1["name"], c2["name"], "publishes", "Event push", shared_citations, source_repository="shop", target_repository="shop2"),
            connection(c1["name"], c3["name"], "writes", "Cache write", shared_citations, source_repository="shop", target_repository="shop2"),
        ],
        "alternatives": [],
        "requests": [],
    }


def test_viewer_cross_repo_parallel_edges_and_hostile_labels(tmp_path: Path) -> None:
    """Real Chrome selects parallel edges + shared-citation endpoints and resolves distinct connection IDs."""
    _browser_or_skip()
    provider = Replay(_cross_repo_hostile_answer)
    factory = Factory(provider)
    with _live_server(tmp_path, factory) as base_url:
        report = _run_viewer(base_url, "cross-repo-viewer")

    _assert_clean_report(report)
    assert report["edgeCount"] == 3
    selections = report["edgeSelections"]
    assert len(selections) == 3
    # Every edge must resolve to its exact connection ID(s), never colliding despite identical citations.
    distinct_selected: list[tuple[str, ...]] = []
    for sel in selections:
        assert sel["expectedIds"], f"Edge {sel['index']} missing expected connection IDs"
        assert sel["selectedIds"] == sel["expectedIds"]
        assert "Support:" in sel["text"]
        assert "shop:" in sel["text"] and "shop2:" in sel["text"]
        distinct_selected.append(tuple(sel["selectedIds"]))
    assert len(set(distinct_selected)) == 3, f"Parallel/shared-citation edges collided: {distinct_selected}"
    assert "shop" in report["details"]["revisionText"] and "shop2" in report["details"]["revisionText"]
    assert len(factory.contexts) == 1
