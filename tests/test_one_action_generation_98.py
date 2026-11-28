"""Acceptance and regression tests for Issue #98: one-action URL-to-architecture orchestration."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")

from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import WorkspaceManager, WorkspaceSettings  # noqa: E402

from conftest import git  # noqa: E402
from test_architecture_discovery import FIXTURE, full_answer  # noqa: E402
from test_security_acceptance_71 import FRONTEND_DIST, TOOL_DIRECTORY, _browser_or_skip, _free_port  # noqa: E402
from test_web_jobs import Factory, Replay  # noqa: E402
from test_web_workspaces import Remote  # noqa: E402


def test_one_action_url_to_architecture_and_back_navigation_without_duplicate_inference(tmp_path: Path) -> None:
    node, browser = _browser_or_skip()

    repo = tmp_path / "upstream"
    shutil.copytree(FIXTURE, repo)
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")

    class _UrlRemote(Remote):
        def http_get(self, url: str, timeout: float, max_bytes: int) -> bytes:
            if url == "https://api.github.com/repos/mudabs/EventShare":
                self.urls.append(url)
                return json.dumps({"default_branch": "main"}).encode("utf-8")
            if url == "https://api.github.com/repos/mudabs/EventShare/commits/main":
                self.urls.append(url)
                return json.dumps({"sha": self.sha}).encode("utf-8")
            return super().http_get(url, timeout, max_bytes)

    def _eventshare_answer(message: dict) -> dict:
        return json.loads(json.dumps(full_answer(message)).replace('"shop"', '"mudabs-eventshare"'))

    remote = _UrlRemote(repo)
    port = _free_port()
    ws_settings = WorkspaceSettings(tmp_path / "store", {})
    manager = WorkspaceManager(ws_settings, http_get=remote.http_get, run_git=remote.run_git)
    provider = Replay(_eventshare_answer)
    factory = Factory(provider)
    app = create_app(
        WebSettings(
            AccessConfig(bind_host="127.0.0.1", port=port),
            ws_settings,
            JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
            frontend_dist=FRONTEND_DIST,
        ),
        provider_factory=factory,
        workspace_manager=manager,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        res = subprocess.run(
            [
                node,
                str(TOOL_DIRECTORY / "check-one-action-98.mjs"),
                f"http://127.0.0.1:{port}",
                browser,
                "https://github.com/mudabs/EventShare",
            ],
            cwd=TOOL_DIRECTORY,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert res.returncode == 0, f"check-one-action-98 failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)

    assert len(factory.contexts) == 1
    assert len(provider.messages) == 1
