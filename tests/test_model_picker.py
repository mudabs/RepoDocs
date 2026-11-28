"""Provider setup offers several models instead of one hard-coded default (#77 walkthrough feedback)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.models import installed_ollama_models, model_catalog, parse_model_option  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.server import build_job_settings  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from test_security_acceptance_71 import FRONTEND_DIST, TOOL_DIRECTORY, _browser_or_skip, _free_port  # noqa: E402
from test_web_jobs import ORIGIN, Factory, shop  # noqa: E402

OPERATOR = (("openai", "gpt-6-sol"), ("openai", "gpt-5-mini"), ("ollama", "qwen2.5-coder:7b"))


def test_catalog_merges_operator_price_profile_and_installed_models_without_duplicates() -> None:
    prices = SimpleNamespace(provider="openai", model="gpt-5-mini")
    catalog = model_catalog(OPERATOR, prices, lambda: ["llama3.2:3b", "qwen2.5-coder:7b"])
    assert catalog["ollama_reachable"] is True
    assert [(m["provider"], m["model"], m["source"]) for m in catalog["models"]] == [
        ("openai", "gpt-6-sol", "operator"),
        ("openai", "gpt-5-mini", "operator"),
        ("ollama", "qwen2.5-coder:7b", "operator"),
        ("ollama", "llama3.2:3b", "installed"),
    ]
    unreachable = model_catalog((), None, lambda: None)
    assert unreachable == {"models": [], "ollama_reachable": False}


def test_model_option_parsing_keeps_colons_in_names_and_rejects_bad_values() -> None:
    assert parse_model_option("ollama:qwen2.5-coder:7b") == ("ollama", "qwen2.5-coder:7b")
    assert parse_model_option(" OpenAI : gpt-6-sol ") == ("openai", "gpt-6-sol")
    for bad in ("gpt-6-sol", "anthropic:claude", "openai:", ":x"):
        with pytest.raises(ValueError):
            parse_model_option(bad)
    settings = build_job_settings(["--store-root", "x", "--model", "openai:gpt-6-sol", "--model", "ollama:qwen2.5-coder:7b"])
    assert settings.models == (("openai", "gpt-6-sol"), ("ollama", "qwen2.5-coder:7b"))


def test_unreachable_ollama_lists_nothing() -> None:
    assert installed_ollama_models("http://127.0.0.1:9", timeout=0.5) is None


def _app(tmp_path: Path, factory: Factory) -> object:
    return create_app(
        WebSettings(AccessConfig(), WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)}),
                    JobSettings(run_limits=RunLimits(context_window_tokens=131072), models=OPERATOR)),
        provider_factory=factory, ollama_models=lambda: ["llama3.2:3b"],
    )


def test_the_models_route_needs_a_session_and_contacts_no_provider(tmp_path: Path) -> None:
    factory = Factory()  # any provider built here fails the test
    with TestClient(_app(tmp_path, factory), base_url=ORIGIN) as client:
        assert client.get("/api/v1/providers/models", headers={"Origin": ORIGIN}).status_code in (401, 403)
        client.post("/api/v1/session", headers={"Origin": ORIGIN})
        response = client.get("/api/v1/providers/models", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    names = [(m["provider"], m["model"]) for m in response.json()["models"]]
    assert ("openai", "gpt-6-sol") in names and ("ollama", "llama3.2:3b") in names
    assert factory.contexts == []


def test_browser_provider_setup_lists_models_and_choosing_one_fills_the_model(tmp_path: Path) -> None:
    node, browser = _browser_or_skip()
    port = _free_port()
    app = create_app(
        WebSettings(AccessConfig(bind_host="127.0.0.1", port=port), WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)}),
                    JobSettings(run_limits=RunLimits(context_window_tokens=131072), models=OPERATOR), frontend_dist=FRONTEND_DIST),
        provider_factory=Factory(), ollama_models=lambda: ["llama3.2:3b"],
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        completed = subprocess.run(
            [node, "check-home-99.mjs", f"http://127.0.0.1:{port}", browser, "model-picker"],
            cwd=TOOL_DIRECTORY, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["openaiOptions"] == ["gpt-6-sol (offered by the operator)", "gpt-5-mini (offered by the operator)"]
    assert report["ollamaOptions"] == ["qwen2.5-coder:7b (offered by the operator)", "llama3.2:3b (installed locally)"]
    assert report["chosen"] == "gpt-6-sol"
