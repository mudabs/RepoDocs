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

    remote = _UrlRemote(repo)
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
