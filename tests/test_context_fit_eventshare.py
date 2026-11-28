"""The source context is fitted to a small model window before consent (EventShare preflight failure during #77).

A synthetic, EventShare-shaped repository (Spring Boot backend, React frontend, SQL migrations, files large enough
that the default context overflows a 32,768-token window) stands in for mudabs/EventShare at 9397020, so the test
needs no network and no credential. Every provider here is a local replay.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs import service  # noqa: E402
from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from test_architecture_discovery import FIXTURE, component  # noqa: E402
from test_web_jobs import ORIGIN, TERMINAL, Api, Factory, Replay, request  # noqa: E402

WINDOW = 32_768


def _eventshare_shaped(tmp_path: Path) -> Path:
    """A multi-module app like EventShare: many sizeable controllers, components and migrations."""

    root = tmp_path / "eventshare"
    shutil.copytree(FIXTURE, root)
    body = "\n".join(f"    // handles case {i}: validate input, load the event, check the owner and respond" for i in range(140))
    java = root / "backend" / "src" / "main" / "java" / "com" / "eventshare" / "api"
    for name in ("Event", "Media", "Gallery", "Admin", "Billing", "Upload", "Invite", "Comment"):
        package = java / name.lower()
        package.mkdir(parents=True, exist_ok=True)
        (package / f"{name}Controller.java").write_text(
            f"package com.eventshare.api.{name.lower()};\n\n@RestController\n@RequestMapping(\"/api/{name.lower()}s\")\n"
            f"public class {name}Controller {{\n{body}\n}}\n", encoding="utf-8")
    (root / "backend" / "pom.xml").write_text("<project>\n" + "  <dependency>spring-boot-starter-web</dependency>\n" * 120 + "</project>\n", encoding="utf-8")
    components = root / "frontend" / "src" / "components"
    components.mkdir(parents=True, exist_ok=True)
    for name in ("Gallery", "Upload", "EventPage", "AdminPanel", "Billing", "Invite"):
        (components / f"{name}.tsx").write_text(
            f"import {{ api }} from '../api'\n\nexport function {name}() {{\n{body}\n  return null\n}}\n", encoding="utf-8")
    migrations = root / "backend" / "src" / "main" / "resources" / "db" / "migration"
    migrations.mkdir(parents=True, exist_ok=True)
    for i in range(1, 5):
        (migrations / f"V{i}__tables.sql").write_text(
            "\n".join(f"CREATE TABLE t{i}_{j} (id uuid PRIMARY KEY, name varchar(255), created_at timestamptz);" for j in range(60)),
            encoding="utf-8")
    return root


def _answer(message: dict[str, Any]) -> dict[str, Any]:
    """A minimal valid proposal citing the first excerpt the model was shown."""

    first = message["excerpts"][0]
    return {
        "components": [component("Event service", "backend-service", [first["path"]], [first["id"]], repository="eventshare")],
        "connections": [], "alternatives": [], "requests": [], "complete": True,
    }


def _app(tmp_path: Path, source: Path, factory: Factory, window: int = WINDOW) -> Any:
    return create_app(
        WebSettings(AccessConfig(), WorkspaceSettings(tmp_path / "store", {"eventshare": source}),
                    JobSettings(run_limits=RunLimits(context_window_tokens=window))),
        provider_factory=factory,
    )


def _workspace(api: Api) -> str:
    response = api.post("/api/v1/workspaces", {"name": "EventShare", "sources": [{"kind": "local", "name": "eventshare"}]})
    assert response.status_code == 201, response.text
    return response.json()["workspace_id"]


def test_without_fitting_the_eventshare_shaped_context_overflows_the_window(tmp_path: Path, monkeypatch) -> None:
    """The failure as found: the default context cannot fit a 32,768-token window, so preflight refuses."""

    monkeypatch.setattr(service, "fitted_context_bytes", lambda *args, **kwargs: None)
    with TestClient(_app(tmp_path, _eventshare_shaped(tmp_path), Factory()), base_url=ORIGIN) as client:
        api = Api(client)
        preview = api.preview(_workspace(api), api.provider())
    assert preview["budget"]["preflight_ok"] is False
    assert preview["budget"]["preflight_error"]["code"] == "context-window"
    assert preview["context_fit"]["fitted"] is False


def test_the_context_is_fitted_disclosed_and_the_job_runs_without_a_stale_source(tmp_path: Path) -> None:
    provider = Replay(_answer)
    factory = Factory(provider)
    source = _eventshare_shaped(tmp_path)
    with TestClient(_app(tmp_path, source, factory), base_url=ORIGIN) as client:
        api = Api(client)
        workspace = _workspace(api)
        handle = api.provider()
        preview = api.preview(workspace, handle, include_documents=True)
        fit, initial = preview["context_fit"], preview["budget"]["initial_request"]
        # It fits, and says so before consent: what was reduced, how much of the source is covered, what was left out.
        assert preview["budget"]["preflight_ok"] is True
        assert initial["total_tokens"] <= WINDOW
        assert fit["fitted"] is True and fit["max_context_bytes"] < 200 * 1024
        assert 3 <= fit["opened_files"] < fit["candidate_files"]
        layers = {excerpt["path"].split("/")[0] for excerpt in preview["excerpts"]}
        assert {"backend", "frontend"} <= layers, "the fitted context still spans backend and frontend"
        # The CLI path (no web adapter) previews the same context for the same provider name.
        cli = service.preview_context(service.ContextRequest(tmp_path / "store" / workspace, provider=factory_identity()))
        assert cli.data["context_hash"] == preview["context_hash"]
        job = api.submit(request(workspace, provider=handle, context=preview["context_hash"], max_requests=1)).json()
        done = api.wait(job["job_id"])
    assert done["state"] in TERMINAL
    assert (done["error"] or {}).get("code") != "stale-source", done
    assert len(provider.messages) == 1, "the fitted request was admitted and sent once"
    assert len(factory.contexts) == 1


def factory_identity() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(name="ollama", model="replay-model", external=False)


def test_a_window_too_small_for_the_minimum_useful_context_still_refuses(tmp_path: Path) -> None:
    factory = Factory()  # any provider built here fails the test
    with TestClient(_app(tmp_path, _eventshare_shaped(tmp_path), factory, window=12_288), base_url=ORIGIN) as client:
        api = Api(client)
        workspace = _workspace(api)
        handle = api.provider()
        preview = api.preview(workspace, handle)
        assert preview["budget"]["preflight_ok"] is False
        assert "smallest useful size" in preview["budget"]["preflight_error"]["message"]
        refused = api.submit(request(workspace, provider=handle, context=preview["context_hash"], max_requests=1))
        assert refused.status_code == 422
    assert factory.contexts == []


def test_a_context_that_already_fits_is_unchanged(tmp_path: Path) -> None:
    """Small sources keep the default budget, so existing previews, consent and hashes are byte-identical."""

    shop = tmp_path / "shop"
    shutil.copytree(FIXTURE, shop)
    with TestClient(_app(tmp_path, shop, Factory()), base_url=ORIGIN) as client:
        api = Api(client)
        workspace = _workspace(api)
        with_provider = api.preview(workspace, api.provider())
        without = api.preview(workspace)
    assert with_provider["context_fit"]["fitted"] is False
    assert with_provider["context_hash"] == without["context_hash"]
