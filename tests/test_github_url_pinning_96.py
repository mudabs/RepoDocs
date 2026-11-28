"""Acceptance and regression tests for Issue #96: GitHub URL parsing and exact revision pinning."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import (  # noqa: E402
    SourceLimits,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceSettings,
    parse_github_url,
    resolve_github_source,
)

from conftest import git  # noqa: E402
from test_architecture_discovery import FIXTURE  # noqa: E402
from test_web_jobs import Factory  # noqa: E402
from test_web_workspaces import Remote  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"
SHA_1 = "93970207969ecdb9b35cf5c1f8e811ef930a2a55"
SHA_2 = "1111111111111111111111111111111111111111"
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


class UrlRemote(Remote):
    """GitHub stand-in that also answers the repository and branch-head API calls; Missing is a 404."""

    branch_head: str | None = None

    def http_get(self, url: str, timeout: float, max_bytes: int) -> bytes:
        if url == "https://api.github.com/repos/mudabs/EventShare":
            self.urls.append(url)
            return json.dumps({"default_branch": "main"}).encode("utf-8")
        if url == "https://api.github.com/repos/mudabs/EventShare/commits/main":
            self.urls.append(url)
            return json.dumps({"sha": self.branch_head or self.sha}).encode("utf-8")
        if "/repos/mudabs/Missing" in url:
            self.urls.append(url)
            raise WorkspaceError("the repository or commit was not found, or it is private (private repositories are not supported)")
        return super().http_get(url, timeout, max_bytes)


def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    shutil.copytree(FIXTURE, repo)
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


def _app(tmp_path: Path, remote: UrlRemote, factory: Factory, **web: object):
    ws_settings = WorkspaceSettings(tmp_path / "store", {})
    manager = WorkspaceManager(ws_settings, http_get=remote.http_get, run_git=remote.run_git)
    settings = WebSettings(web.pop("access", AccessConfig()), ws_settings,
                           JobSettings(run_limits=RunLimits(context_window_tokens=131072)), **web)
    return create_app(settings, provider_factory=factory, workspace_manager=manager)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://github.com/mudabs/EventShare", ("mudabs", "EventShare")),
        ("https://github.com/mudabs/EventShare/", ("mudabs", "EventShare")),
        ("https://github.com/mudabs/EventShare.git", ("mudabs", "EventShare")),
        ("https://www.github.com/mudabs/EventShare.git/", ("mudabs", "EventShare")),
    ],
)
def test_parse_github_url_normalizes_supported_forms(raw: str, expected: tuple[str, str]) -> None:
    assert parse_github_url(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "http://github.com/mudabs/EventShare",
        "ssh://git@github.com/mudabs/EventShare.git",
        "file:///etc/passwd",
        "https://user:pass@github.com/mudabs/EventShare",
        "https://user@github.com/mudabs/EventShare",
        "https://127.0.0.1/mudabs/EventShare",
        "https://169.254.169.254/latest/meta-data",
        "https://evil.example/mudabs/EventShare",
        "https://api.github.com/mudabs/EventShare",
        "https://github.com:8443/mudabs/EventShare",
        "https://github.com/mudabs/EventShare?token=abc",
        "https://github.com/mudabs/EventShare#readme",
        "https://github.com/mudabs/EventShare/tree/main",
        "https://github.com/mudabs",
    ],
)
def test_parse_github_url_rejects_ssrf_credentials_and_unsafe_urls(bad: str) -> None:
    with pytest.raises(WorkspaceError):
        parse_github_url(bad)


def test_resolve_github_source_pins_default_branch_without_mixing_branch_movement() -> None:
    seen_urls: list[str] = []
    head_sha = SHA_1

    def fake_get(url: str, timeout: float, max_bytes: int) -> bytes:
        seen_urls.append(url)
        if url == "https://api.github.com/repos/mudabs/EventShare":
            return json.dumps({"default_branch": "main"}).encode("utf-8")
        if url == "https://api.github.com/repos/mudabs/EventShare/commits/main":
            return json.dumps({"sha": head_sha}).encode("utf-8")
        raise WorkspaceError(f"Unexpected URL: {url}")

    resolved = resolve_github_source("https://github.com/mudabs/EventShare.git", SourceLimits(), fake_get)
    assert resolved == {
        "kind": "github",
        "owner": "mudabs",
        "repository": "EventShare",
        "default_branch": "main",
        "revision": SHA_1,
        "canonical_url": "https://github.com/mudabs/EventShare",
    }
    assert seen_urls == [
        "https://api.github.com/repos/mudabs/EventShare",
        "https://api.github.com/repos/mudabs/EventShare/commits/main",
    ]
    # Even if default branch moves later, the pinned source retains SHA_1
    head_sha = SHA_2
    assert resolved["revision"] == SHA_1


def test_resolve_github_source_reports_errors_and_rate_limits_without_claiming_pin() -> None:
    def rate_limited(url: str, timeout: float, max_bytes: int) -> bytes:
        raise WorkspaceError("GitHub refused or rate-limited the unauthenticated request; try again later")

    with pytest.raises(WorkspaceError, match="rate-limited"):
        resolve_github_source("https://github.com/mudabs/EventShare", SourceLimits(), rate_limited)


def test_eventshare_url_resolves_creates_workspace_and_previews_without_calling_ai(tmp_path: Path) -> None:
    remote = UrlRemote(upstream(tmp_path))
    ws_settings = WorkspaceSettings(tmp_path / "store", {})
    manager = WorkspaceManager(ws_settings, http_get=remote.http_get, run_git=remote.run_git)
    factory = Factory()
    app = create_app(
        WebSettings(AccessConfig(), ws_settings, JobSettings(run_limits=RunLimits(context_window_tokens=131072))),
        provider_factory=factory,
        workspace_manager=manager,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        headers = {"Origin": ORIGIN, "x-csrf-token": csrf}

        resolved = client.post("/api/v1/sources/github/resolve", json={"url": "https://github.com/mudabs/EventShare"}, headers=headers)
        assert resolved.status_code == 200
        src = resolved.json()
        assert src["revision"] == remote.sha

        ws = client.post(
            "/api/v1/workspaces",
            json={"name": "EventShare", "sources": [{"kind": "github", "owner": src["owner"], "repository": src["repository"], "revision": src["revision"]}]},
            headers=headers,
        )
        assert ws.status_code == 201, ws.text
        ws_id = ws.json()["workspace_id"]

        preview = client.post(f"/api/v1/workspaces/{ws_id}/preview", json={}, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["snapshots"][0]["revision"] == remote.sha
        assert factory.contexts == []

        # Denied host via HTTP route returns 422 without calling AI
        bad = client.post("/api/v1/sources/github/resolve", json={"url": "https://127.0.0.1/a/b"}, headers=headers)
        assert bad.status_code == 422


def test_branch_moving_after_resolution_keeps_the_workspace_on_the_resolved_sha(tmp_path: Path) -> None:
    """The default branch advances between resolution and acquisition; evidence stays on the resolved commit."""

    repo = upstream(tmp_path)
    remote = UrlRemote(repo)
    pinned = remote.sha
    with TestClient(_app(tmp_path, remote, Factory()), base_url=ORIGIN) as client:
        csrf = client.post("/api/v1/session", headers={"Origin": ORIGIN}).json()["csrf_token"]
        headers = {"Origin": ORIGIN, "x-csrf-token": csrf}
        src = client.post("/api/v1/sources/github/resolve", json={"url": "https://github.com/mudabs/EventShare"}, headers=headers).json()
        assert src["revision"] == pinned

        # The branch moves on: a new commit lands and GitHub now reports it as the branch head.
        (repo / "MOVED.md").write_text("added after resolution\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "moved")
        remote.branch_head = git(repo, "rev-parse", "HEAD")
        assert remote.branch_head != pinned

        ws = client.post(
            "/api/v1/workspaces",
            json={"name": "EventShare", "sources": [{"kind": "github", "owner": "mudabs", "repository": "EventShare", "revision": pinned}]},
            headers=headers,
        )
        assert ws.status_code == 201, ws.text
        preview = client.post(f"/api/v1/workspaces/{ws.json()['workspace_id']}/preview", json={}, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["snapshots"][0]["revision"] == pinned
        fetches = [call for call in remote.git_calls if "fetch" in call]
        assert fetches and all(pinned in call for call in fetches)
        assert not any(remote.branch_head in call for call in remote.git_calls)


# ------------------------------------------------------------------ real browser


def _browser(tmp_path: Path, scenario: str) -> tuple[dict, UrlRemote, Factory]:
    import os
    import socket
    import subprocess
    import threading
    import time

    from test_browser_e2e import TOOL_DIRECTORY, _browser_or_skip

    uvicorn = pytest.importorskip("uvicorn")
    node, chrome = _browser_or_skip()
    remote, factory = UrlRemote(upstream(tmp_path)), Factory()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    app = _app(tmp_path, remote, factory, access=AccessConfig(bind_host="127.0.0.1", port=port), frontend_dist=FRONTEND_DIST)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn failed to start"
    try:
        flag = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
        done = subprocess.run([node, "check-github-url-96.mjs", f"http://127.0.0.1:{port}", chrome, scenario, *flag],
                              cwd=TOOL_DIRECTORY, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert done.returncode == 0, f"{done.stderr[-1500:]}\n{done.stdout[-500:]}"
    return json.loads(done.stdout.strip().splitlines()[-1]), remote, factory


def test_browser_github_url_alone_reaches_preview_on_the_resolved_sha(tmp_path: Path) -> None:
    report, remote, factory = _browser(tmp_path, "url-to-preview")
    assert report["pageErrors"] == [] and report["externalRequests"] == []
    assert remote.sha[:12] in report["text"]
    assert "https://api.github.com/repos/mudabs/EventShare/commits/main" in remote.urls
    assert factory.contexts == []  # resolving and scanning never builds an AI provider


def test_browser_changed_url_never_reuses_the_previous_pin(tmp_path: Path) -> None:
    """Regression: after a URL was resolved, editing it must drop the old pin instead of acquiring the old repository."""

    report, remote, _ = _browser(tmp_path, "stale-pin")
    assert report["pageErrors"] == []
    assert report["hasScan"] is False
    assert not any("fetch" in call for call in remote.git_calls)
