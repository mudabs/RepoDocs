"""Acceptance and regression tests for Issue #94: model config, preflight, and error recovery."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest
import uvicorn

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs import service  # noqa: E402
from repodocs.ai.budgets import PriceProfile, RunLimits, RunStopped, STOP_CONTEXT  # noqa: E402
from repodocs.ai.providers import ProviderResponse  # noqa: E402
from repodocs.common import write_json  # noqa: E402
from repodocs.config import ProjectConfig  # noqa: E402
from repodocs.errors import ConfigError, ConsentError, ProviderError  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobSettings  # noqa: E402
from repodocs.web.security import AccessConfig  # noqa: E402
from repodocs.web.server import build_job_settings, main as server_main  # noqa: E402
from repodocs.web.workspaces import WorkspaceSettings  # noqa: E402

from conftest import invoke  # noqa: E402
from test_architecture_discovery import FIXTURE, full_answer  # noqa: E402
from test_security_acceptance_71 import FRONTEND_DIST, TOOL_DIRECTORY, _browser_or_skip, _free_port  # noqa: E402
from test_web_jobs import Api, Factory, Replay, architecture_job, asks, request, shop  # noqa: E402


@contextmanager
def _running(
    tmp_path: Path,
    factory: Factory,
    *,
    run_limits: RunLimits = RunLimits(context_window_tokens=131072),
    prices: PriceProfile | None = None,
) -> Iterator[tuple[Api, TestClient, Factory]]:
    ws = WebSettings(
        AccessConfig(),
        WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)}),
        JobSettings(run_limits=run_limits, prices=prices),
    )
    app = create_app(ws, provider_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield Api(client), client, factory


@contextmanager
def _live_server(tmp_path: Path, factory: Factory, *, job_settings: JobSettings) -> Iterator[str]:
    port = _free_port()
    shop_dir = tmp_path / "approved" / "shop"
    shutil.copytree(FIXTURE, shop_dir)
    ws_settings = WebSettings(
        AccessConfig(bind_host="127.0.0.1", port=port),
        WorkspaceSettings(tmp_path / "store", {"shop": shop_dir}),
        job_settings,
        frontend_dist=FRONTEND_DIST,
    )
    app = create_app(ws_settings, provider_factory=factory)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_operator_cli_configures_limits_and_prices_without_code_edits(tmp_path: Path) -> None:
    store = tmp_path / "store"
    prices_path = tmp_path / "prices.json"
    write_json(
        prices_path,
        {
            "version": "2026-11-01",
            "provider": "openai",
            "model": "gpt-6-luna",
            "input_usd_per_mtok": 1.0,
            "output_usd_per_mtok": 4.0,
            "cache_read_usd_per_mtok": 0.25,
            "cache_write_usd_per_mtok": 0,
        },
    )
    settings = build_job_settings(
        [
            "--store-root",
            str(store),
            "--context-window-tokens",
            "131072",
            "--request-output-tokens",
            "4096",
            "--max-requests",
            "4",
            "--max-input-tokens",
            "200000",
            "--max-output-tokens",
            "16000",
            "--max-total-tokens",
            "216000",
            "--max-cost-usd",
            "1.50",
            "--prices-file",
            str(prices_path),
        ]
    )
    assert settings.run_limits == RunLimits(
        context_window_tokens=131072,
        request_output_tokens=4096,
        max_requests=4,
        max_input_tokens=200000,
        max_output_tokens=16000,
        max_total_tokens=216000,
        max_cost_usd=Decimal("1.50"),
    )
    assert settings.prices == PriceProfile.from_dict(json.loads(prices_path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--context-window-tokens", "0"],
        ["--context-window-tokens", "4096", "--request-output-tokens", "4096"],
        ["--max-requests", "99"],
        ["--max-cost-usd", "-1"],
        ["--prices-file", "nonexistent-prices.json"],
    ],
)
def test_operator_cli_rejects_invalid_limits_or_prices(tmp_path: Path, extra_args: list[str]) -> None:
    rc = server_main(["--store-root", str(tmp_path / "store"), *extra_args])
    assert rc == 2


def test_undersized_context_window_blocks_preflight_and_submission_with_zero_provider_calls(tmp_path: Path) -> None:
    provider = Replay(full_answer)
    with _running(tmp_path, Factory(provider), run_limits=RunLimits()) as (api, _, factory):
        workspace = api.workspace()
        handle = api.post(
            "/api/v1/providers",
            {"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-6-luna", "api_key": "sk-94-test"},
        ).json()["provider_session_id"]
        preview = api.preview(workspace, handle)

        budget = preview["budget"]
        assert budget["context_window_source"] == "fallback"
        assert budget["limits"]["context_window_tokens"] == 4096
        assert budget["limits"]["request_output_tokens"] == 2048
        assert budget["initial_request"]["estimate_method"] == "byte-bound"
        assert budget["initial_request"]["fits_context_window"] is False
        assert budget["preflight_ok"] is False
        assert budget["preflight_error"]["code"] == "context-window"

        consent = api.consent(workspace, handle, preview["context_hash"]).json()["consent_receipt_id"]
        resp = api.submit(request(workspace, provider=handle, context=preview["context_hash"], receipt=consent))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid-request"
        assert "context window" in resp.json()["error"]["message"]
        assert factory.contexts == []
        assert provider.messages == []


def test_eventshare_like_token_count_failure_stops_before_generation_with_accurate_counts(tmp_path: Path) -> None:
    class _CountingReplay(Replay):
        def __init__(self, count: int, *replies: Any) -> None:
            super().__init__(*replies)
            self.count = count
            self.count_calls = 0

        def count_input_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int:
            self.count_calls += 1
            return self.count

    # Window 22,000 allows local byte-bound (~10-15k) at HTTP admission or CLI, then provider count 20,681 + 2,048 > 22,000 fails.
    provider = _CountingReplay(20681, full_answer)
    limits = RunLimits(context_window_tokens=22000, request_output_tokens=2048)
    with _running(tmp_path, Factory(provider), run_limits=limits) as (api, _, _factory):
        ws = api.workspace()
        handle = api.post(
            "/api/v1/providers",
            {"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-6-luna", "api_key": "sk-94-count"},
        ).json()["provider_session_id"]
        prev = api.preview(ws, handle)
        receipt = api.consent(ws, handle, prev["context_hash"]).json()["consent_receipt_id"]
        sub = api.submit(request(ws, provider=handle, context=prev["context_hash"], receipt=receipt))
        assert sub.status_code == 202, sub.text
        job = api.wait(sub.json()["job_id"])

    assert job["state"] == "budget-exhausted"
    assert job["error"]["code"] == "budget-exhausted"
    assert job["budget"]["stop_reason"] == STOP_CONTEXT
    assert job["budget"]["requests"] == 0
    assert job["budget"]["count_requests"] == 1
    assert provider.count_calls == 1
    assert provider.messages == []
    assert job["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_microunits": None, "currency": None, "accounting": "known"}


def test_configured_window_succeeds_and_matches_cli_budget_semantics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    web_provider = Replay(full_answer)
    limits = RunLimits(context_window_tokens=131072, request_output_tokens=4096)
    with _running(tmp_path / "web", Factory(web_provider), run_limits=limits) as (api, _, _factory):
        workspace = api.workspace()
        handle = api.provider()
        preview = api.preview(workspace, handle)
        assert preview["budget"]["context_window_source"] == "operator"
        assert preview["budget"]["preflight_ok"] is True
        _, _, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
    assert job["state"] == "succeeded"
    assert job["budget"]["requests"] == 1

    # Equivalent CLI/shared-engine run with the same limits
    cli_root = tmp_path / "cli-proj"
    cli_root.mkdir()
    cli_repo = tmp_path / "cli-repo"
    shutil.copytree(FIXTURE, cli_repo)
    assert invoke(cli_root, capsys, "init", "cli")[0] == 0
    assert invoke(cli_root, capsys, "add", str(cli_repo), "--id", "shop", "--destination")[0] == 0
    service.scan(service.ScanRequest(cli_root))
    cli_provider = Replay(full_answer)
    cli_res = service.discover(service.DiscoverRequest(cli_root, provider=cli_provider, limits=limits))
    assert cli_res.status == service.STATUS_COMPLETE
    assert cli_res.data["usage"]["requests"] == 1


def test_mismatched_pricing_unknown_cost_and_fails_cost_cap(tmp_path: Path) -> None:
    prices = PriceProfile(
        version="2026-11-01",
        provider="openai",
        model="gpt-4o",
        input_usd_per_mtok=Decimal("2.5"),
        output_usd_per_mtok=Decimal("10"),
        cache_read_usd_per_mtok=Decimal("1.25"),
        cache_write_usd_per_mtok=Decimal("0"),
    )
    provider = Replay(full_answer)
    with _running(tmp_path, Factory(provider), run_limits=RunLimits(context_window_tokens=131072), prices=prices) as (api, _, _factory):
        workspace = api.workspace()
        handle = api.post(
            "/api/v1/providers",
            {"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-6-luna", "api_key": "sk-94-price"},
        ).json()["provider_session_id"]
        preview = api.preview(workspace, handle)
        assert preview["budget"]["price_profile"] is None
        assert preview["budget"]["max_estimated_cost_usd"] is None

        consent = api.consent(workspace, handle, preview["context_hash"]).json()["consent_receipt_id"]
        blocked = api.submit(request(workspace, provider=handle, context=preview["context_hash"], receipt=consent, cost=100_000))
        assert blocked.status_code == 422
        assert "gpt-4o" in blocked.json()["error"]["message"]


def test_count_failure_falls_back_to_byte_bound_and_revoked_consent_blocks_counting(tmp_path: Path) -> None:
    class _FailingCounter(Replay):
        def __init__(self, *replies: Any) -> None:
            super().__init__(*replies)
            self.count_calls = 0

        def count_input_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int:
            self.count_calls += 1
            raise ProviderError("count endpoint failed")

    # 1. Count failure falls back to byte-bound while recording count_requests=1
    p1 = _FailingCounter(full_answer)
    with _running(tmp_path / "count_fail", Factory(p1), run_limits=RunLimits(context_window_tokens=131072)) as (api, _, _):
        _, _, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
    assert job["state"] == "succeeded"
    assert p1.count_calls == 1
    assert job["budget"]["count_requests"] == 1
    assert job["budget"]["requests"] == 1

    # 2. Revoked consent before counting prevents both count_input_tokens and generate
    p2 = _FailingCounter(full_answer)
    with _running(tmp_path / "revoked", Factory(p2), run_limits=RunLimits(context_window_tokens=131072)) as (api, _, factory):
        ws = api.workspace()
        handle = api.post(
            "/api/v1/providers",
            {"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-6-luna", "api_key": "sk-94-rev"},
        ).json()["provider_session_id"]
        prev = api.preview(ws, handle)
        receipt = api.consent(ws, handle, prev["context_hash"]).json()["consent_receipt_id"]
        api.post(f"/api/v1/consents/{receipt}/revoke", {})
        resp = api.submit(request(ws, provider=handle, context=prev["context_hash"], receipt=receipt))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "consent-required"
        assert p2.count_calls == 0
        assert p2.messages == []
        assert factory.contexts == []


def test_real_browser_preflight_block_and_retry_recovery(tmp_path: Path) -> None:
    node, browser = _browser_or_skip()

    # 1. Undersized default 4096 window blocks Continue in the browser UI
    with _live_server(tmp_path / "blocked", Factory(Replay(full_answer)), job_settings=JobSettings(run_limits=RunLimits())) as base_url:
        res = subprocess.run(
            [node, str(TOOL_DIRECTORY / "check-preflight-94.mjs"), "blocked-context", base_url, browser, "shop"],
            cwd=TOOL_DIRECTORY,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert res.returncode == 0, f"Browser blocked-context failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"

    # 2. Aggregate budget-exhausted job displays error + Retry with current sources -> succeeds on explicit retry
    with _live_server(
        tmp_path / "retry",
        Factory(Replay(full_answer), Replay(full_answer)),
        job_settings=JobSettings(run_limits=RunLimits(context_window_tokens=131072)),
    ) as base_url:
        res = subprocess.run(
            [node, str(TOOL_DIRECTORY / "check-preflight-94.mjs"), "budget-retry", base_url, browser, "shop"],
            cwd=TOOL_DIRECTORY,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert res.returncode == 0, f"Browser budget-retry failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"


def test_later_round_is_admitted_again_and_stops_before_an_oversized_second_request(tmp_path: Path) -> None:
    """A passing first admission is not permission for later rounds: each request is counted and checked first."""

    class _GrowingCounter(Replay):
        def __init__(self, *replies: Any) -> None:
            super().__init__(*replies)
            self.counts = [1_000, 200_000]
            self.count_calls = 0

        def count_input_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int:
            self.count_calls += 1
            return self.counts[min(self.count_calls - 1, len(self.counts) - 1)]

    provider = _GrowingCounter(asks, full_answer)
    with _running(tmp_path, Factory(provider), run_limits=RunLimits(context_window_tokens=131072)) as (api, _, _factory):
        _, _, queued = architecture_job(api)
        job = api.wait(queued["job_id"])

    assert provider.count_calls == 2
    assert len(provider.messages) == 1, "the second, oversized request must never be sent"
    assert job["budget"]["requests"] == 1
    assert job["budget"]["count_requests"] == 2
    assert job["budget"]["stop_reason"] == STOP_CONTEXT
    assert job["state"] == "budget-exhausted", job["state"]


def test_changed_provider_model_needs_its_own_consent_and_reports_its_own_window(tmp_path: Path) -> None:
    """Consent and limits are bound to one provider/model; switching model refuses the old receipt before any count."""

    class _Counter(Replay):
        def __init__(self, *replies: Any) -> None:
            super().__init__(*replies)
            self.count_calls = 0

        def count_input_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int:
            self.count_calls += 1
            return 1_000

    provider = _Counter(full_answer)
    with _running(tmp_path, Factory(provider), run_limits=RunLimits()) as (api, _, factory):
        workspace = api.workspace()
        first = api.post(
            "/api/v1/providers",
            {"provider": "anthropic", "host": "https://api.anthropic.com", "model": "claude-haiku-5-5", "api_key": "sk-ant-94-a"},
        ).json()["provider_session_id"]
        first_preview = api.preview(workspace, first)
        assert first_preview["budget"]["context_window_source"] == "provider-default"
        assert first_preview["budget"]["limits"]["context_window_tokens"] == 200_000
        receipt = api.consent(workspace, first, first_preview["context_hash"]).json()["consent_receipt_id"]

        second = api.post(
            "/api/v1/providers",
            {"provider": "openai", "host": "https://api.openai.com/v1", "model": "gpt-6-luna", "api_key": "sk-94-b"},
        ).json()["provider_session_id"]
        second_preview = api.preview(workspace, second)
        assert second_preview["budget"]["context_window_source"] == "fallback"
        assert second_preview["budget"]["limits"]["context_window_tokens"] == 4096

        response = api.submit(request(workspace, provider=second, context=second_preview["context_hash"], receipt=receipt))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "consent-required"
    assert provider.count_calls == 0
    assert provider.messages == []
    assert factory.contexts == []
