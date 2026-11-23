"""Run-level usage budgets: preflight, reservation, reconciliation, cancellation, and local receipts (ADR-031).

No test here contacts a provider. Counts, usage and failures are scripted, and the one local HTTP
server used for Ollama binds to the loopback interface.
"""

from __future__ import annotations

import json
import sys
import threading
import types
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import allow_large_context, invoke
from repodocs.ai import budgets, providers
from repodocs.ai.budgets import (
    BudgetedProvider, Ledger, PriceProfile, RunLimits, RunStopped, Usage, budgeted, estimate_tokens, ledger_for, usage_from_counts,
)
from repodocs.ai.providers import AnthropicProvider, OllamaProvider, ProviderResponse
from repodocs.architecture.discovery import discover
from repodocs.config import AiSettings
from repodocs.errors import ConfigError, ConsentError, MalformedProviderOutput, ProviderError

from test_ai import scanned
from test_architecture_cli import express_project, generic, use
from test_architecture_discovery import Scripted, full_answer, setup
from test_schemas import assert_valid

ORIGINAL_COUNT = AnthropicProvider.count_input_tokens
MEASURED = Usage(input_tokens=100, output_tokens=40, cache_read_tokens=0, cache_write_tokens=0)
OBJECT = {"type": "object"}


class Fake:
    """A provider that replays steps: a ``Usage``, ``None`` (no usage reported), or an exception."""

    name = "ollama"
    model = "fake"
    external = False

    def __init__(self, *steps: Any, count: int | None = None, on_call: Any = None) -> None:
        self.steps = list(steps) or [MEASURED]
        self.calls = 0
        self.counts = 0
        self.count = count
        self.on_call = on_call
        self.lock = threading.Lock()
        if count is not None:
            self.count_input_tokens = self._count

    def _count(self, system: str, user: str, schema: dict) -> int:
        self.counts += 1
        return self.count  # type: ignore[return-value]

    def generate(self, system: str, user: str, schema: dict) -> ProviderResponse:
        with self.lock:
            self.calls += 1
            step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if self.on_call:
            self.on_call()
        if isinstance(step, BaseException):
            raise step
        return ProviderResponse({"ok": True}, "served", step)


def ledger(prices: PriceProfile | None = None, provider: str = "ollama", **limits: Any) -> Ledger:
    limits.setdefault("context_window_tokens", 100_000)
    return Ledger(RunLimits(**limits).resolved(provider), prices)


def wrap(fake: Fake, prices: PriceProfile | None = None, **limits: Any) -> BudgetedProvider:
    return BudgetedProvider(fake, ledger(prices, **limits))  # type: ignore[arg-type]


def go(provider: BudgetedProvider) -> ProviderResponse:
    return provider.generate("system", "user", OBJECT)


PRICES = PriceProfile("2026-11-01", "ollama", "fake", Decimal(10), Decimal(50), Decimal(0), Decimal(0), served_models=("served",))
PRICE_SETTINGS = {"version": "2026-11-01", "provider": "anthropic", "model": "claude-opus-5-5"}


# ------------------------------------------------------------------ usage contracts
def test_the_provider_response_contract_is_backward_compatible() -> None:
    old = ProviderResponse({"a": 1}, "model")

    assert old.usage is None and old == ProviderResponse({"a": 1}, "model", None)
    assert ProviderResponse({"a": 1}, "model", MEASURED).usage == MEASURED


def test_missing_or_malformed_usage_is_unknown_and_never_zero() -> None:
    assert usage_from_counts(None, None, None, None) is None
    partial = usage_from_counts(10, 5, None, None)
    assert partial == Usage(10, 5, None, None) and not partial.measured and partial.prompt_tokens is None
    junk = usage_from_counts("12", True, -1, 1.5)
    assert junk is None, "strings, booleans, negatives and floats are not token counts"
    assert Usage(1, 2, 3, 4, reasoning_tokens=2).prompt_tokens == 8, "reasoning is a subset of output and is never added"


# ------------------------------------------------------------------ configuration
def test_usage_settings_round_trip_and_stay_within_the_ceilings(tmp_path: Path) -> None:
    ai = AiSettings.from_dict({
        "provider": "anthropic", "external_consent": True, "effort": "low",
        "limits": {"context_window_tokens": 1_000_000, "request_output_tokens": 8000, "max_requests": 3, "max_cost_usd": 1.5},
        "prices": {**PRICE_SETTINGS, "input_usd_per_mtok": 5, "output_usd_per_mtok": 25.5, "cache_read_usd_per_mtok": 0.5,
                   "cache_write_usd_per_mtok": 6.25, "served_models": ["claude-opus-5-5-20261101"]},
    })

    assert AiSettings.from_dict(ai.to_dict()) == ai
    assert ai.limits.max_cost_usd == Decimal("1.5") and ai.prices.output_usd_per_mtok == Decimal("25.5")


@pytest.mark.parametrize("ai, message", [
    ({"limits": {"max_requests": 7}}, "may only lower the ceiling of 6"),
    ({"limits": {"max_count_requests": 7}}, "may only lower the ceiling of 6"),
    ({"limits": {"request_output_tokens": 16001}}, "ceiling of 16000"),
    ({"limits": {"max_input_tokens": 800_001}}, "ceiling"),
    ({"limits": {"max_output_tokens": 64_001}}, "ceiling"),
    ({"limits": {"max_requests": 0}}, "positive integer"),
    ({"limits": {"max_requests": True}}, "positive integer"),
    ({"limits": {"surprise": 1}}, "Unknown ai.limits"),
    ({"limits": {"max_cost_usd": 1}}, "needs ai.prices"),
    ({"limits": {"max_cost_usd": 0}, "prices": {**PRICE_SETTINGS, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}}, "greater than zero"),
    ({"limits": {"max_cost_usd": 1}, "prices": {**PRICE_SETTINGS, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}}, "cache-read and cache-write"),
    ({"prices": {"version": "v", "model": "m", "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}}, "provider is required"),
    ({"prices": {"version": "v", "provider": "anthropic", "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}}, "model is required"),
    ({"prices": {**PRICE_SETTINGS, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1, "served_models": "x"}}, "served_models"),
    ({"limits": []}, "JSON object"),
    ({"effort": "max"}, "ai.effort must be one of"),
    ({"prices": {"input_usd_per_mtok": 1, "output_usd_per_mtok": 1}}, "version"),
    ({"prices": {**PRICE_SETTINGS, "input_usd_per_mtok": 1}}, "output_usd_per_mtok is required"),
    ({"prices": {**PRICE_SETTINGS, "input_usd_per_mtok": -1, "output_usd_per_mtok": 1}}, "non-negative"),
    ({"prices": {**PRICE_SETTINGS, "input_usd_per_mtok": "1", "output_usd_per_mtok": 1}}, "must be a number"),
    ({"prices": {**PRICE_SETTINGS, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1, "live": True}}, "Unknown ai.prices"),
])
def test_invalid_usage_settings_are_refused(ai: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        AiSettings.from_dict({"provider": "anthropic", **ai})


def test_defaults_are_explicit_and_a_runtime_that_cannot_fit_its_own_output_is_refused() -> None:
    assert RunLimits().resolved("anthropic") == RunLimits(200_000, 16000, 6, 800_000, 64_000, None, 6)
    assert RunLimits(max_requests=2).resolved("anthropic").max_count_requests == 2, "one count per request by default"
    assert RunLimits().resolved("ollama").context_window_tokens == 4096
    assert RunLimits().resolved("ollama").request_output_tokens == 2048
    with pytest.raises(ConfigError, match="smaller than the context window"):
        RunLimits(context_window_tokens=1000, request_output_tokens=1000).resolved("ollama")


def test_effort_is_explicit_and_unsupported_by_ollama() -> None:
    with pytest.raises(ConfigError, match="only by the anthropic and openai providers"):
        providers.resolve_provider(AiSettings(provider="ollama", model="m", effort="low"))
    claude = providers.resolve_provider(AiSettings(provider="anthropic", external_consent=True, effort="medium"))
    assert claude.effort == "medium" and claude.max_output_tokens == 16000


def test_ai_configure_keeps_the_usage_settings_it_does_not_edit(tmp_path: Path, capsys) -> None:
    root = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "ai-configure", "--provider", "anthropic")[0] == 0
    path = root / "repodocs.json"
    configuration = json.loads(path.read_text(encoding="utf-8"))
    configuration["ai"].update(
        effort="high", limits={"max_requests": 2},
        prices={**PRICE_SETTINGS, "input_usd_per_mtok": 5, "output_usd_per_mtok": 25},
    )
    path.write_text(json.dumps(configuration), encoding="utf-8")
    assert_valid("project-config-1.0.json", configuration)

    assert invoke(root, capsys, "ai-configure", "--consent-external", "--model", "claude-opus-5-5")[0] == 0

    saved = json.loads(path.read_text(encoding="utf-8"))["ai"]
    assert saved["effort"] == "high" and saved["limits"] == {"max_requests": 2} and saved["prices"]["version"] == "2026-11-01"
    assert saved["external_consent"] is True and saved["model"] == "claude-opus-5-5"


# ------------------------------------------------------------------ preflight
def test_a_14946_token_prompt_is_refused_before_a_4096_token_runtime_is_contacted() -> None:
    fake = Fake(count=14_946)
    provider = wrap(fake, context_window_tokens=4096)

    with pytest.raises(RunStopped) as stopped:
        go(provider)

    assert stopped.value.reason == "context-window" and "14946" in str(stopped.value) and "4096" in str(stopped.value)
    assert fake.calls == 0, "nothing was sent"
    snapshot = provider.ledger.snapshot()
    assert snapshot["totals"]["requests"] == 0 and snapshot["stop_reason"] == "context-window"


class Recorder(BaseHTTPRequestHandler):
    hits: list[dict]

    def do_POST(self) -> None:  # noqa: N802
        self.hits.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        body = json.dumps({
            "model": "llama3", "message": {"content": "{}"}, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 7,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def serve() -> tuple[HTTPServer, list[dict]]:
    hits: list[dict] = []
    handler = type("Handler", (Recorder,), {"hits": hits})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


def test_a_real_ollama_request_too_large_for_its_default_window_is_never_sent() -> None:
    server, hits = serve()
    try:
        settings = AiSettings(provider="ollama", model="llama3", ollama_host=f"http://127.0.0.1:{server.server_port}")
        provider = budgeted(providers.resolve_provider(settings), settings)

        with pytest.raises(RunStopped, match="4096-token context window"):
            provider.generate("system", "x" * 40_000, OBJECT)
        assert hits == []
        with pytest.raises(RunStopped, match="4096-token context window"):
            provider.generate("system", "small", OBJECT)
        assert hits == [], "a stopped run stays stopped; a smaller request does not resume it under the old stop reason"

        provider = budgeted(providers.resolve_provider(settings), settings)
        response = provider.generate("system", "small", OBJECT)
    finally:
        server.shutdown()
        server.server_close()

    sent = hits[0]
    assert sent["options"] == {"num_ctx": 4096, "num_predict": 2048}, "the window and output cap are sent explicitly"
    assert response.usage == Usage(50, 7, 0, 0)
    request = provider.ledger.snapshot()["requests"][0]
    assert request["estimate_method"] == "byte-bound" and request["usage_measured"] is True
    assert request["charged_input_tokens"] == 50 and request["charged_output_tokens"] == 7


def test_the_fallback_estimate_covers_the_whole_serialized_request_and_is_labeled() -> None:
    provider = OllamaProvider("llama3", None)
    small = estimate_tokens(budgets.serialized_request(provider, "s", "u", OBJECT))
    with_schema = estimate_tokens(budgets.serialized_request(provider, "s", "u", {"type": "object", "properties": {"k": {"type": "string"}}}))
    assert with_schema > small, "the schema is part of the payload"
    assert "num_ctx" in budgets.serialized_request(provider, "s", "u", OBJECT), "so is the envelope"
    assert estimate_tokens("é" * 100) > estimate_tokens("e" * 100), "bytes, not characters"
    fake = Fake()
    wrapped = wrap(fake)
    go(wrapped)
    assert wrapped.ledger.snapshot()["requests"][0]["estimate_method"] == "byte-bound"
    counted = wrap(Fake(count=321))
    go(counted)
    assert counted.ledger.snapshot()["requests"][0]["estimate_method"] == "provider-count"
    assert counted.ledger.snapshot()["requests"][0]["estimate_tokens"] == 321


@pytest.mark.parametrize("count, allowed", [(900, True), (901, False)])
def test_the_context_window_includes_the_reserved_output_at_the_boundary(count: int, allowed: bool) -> None:
    fake = Fake(count=count)
    provider = wrap(fake, context_window_tokens=1000, request_output_tokens=100)

    if allowed:
        go(provider)
        assert fake.calls == 1
    else:
        with pytest.raises(RunStopped) as stopped:
            go(provider)
        assert stopped.value.reason == "context-window" and fake.calls == 0


# ------------------------------------------------------------------ aggregate caps
def test_the_request_cap_holds_across_retries_and_repairs() -> None:
    fake = Fake(MalformedProviderOutput("bad"), MEASURED)
    provider = wrap(fake, max_requests=2)

    with pytest.raises(MalformedProviderOutput):
        go(provider)
    go(provider)
    with pytest.raises(RunStopped) as stopped:
        go(provider)

    assert stopped.value.reason == "request-limit" and fake.calls == 2
    assert provider.ledger.snapshot()["remaining"]["requests"] == 0


def test_the_input_token_cap_is_aggregate_and_unknown_usage_stays_reserved() -> None:
    fake = Fake(None, count=400)
    provider = wrap(fake, max_input_tokens=1000)

    go(provider)
    go(provider)
    with pytest.raises(RunStopped) as stopped:
        go(provider)

    assert stopped.value.reason == "input-token-limit" and fake.calls == 2
    snapshot = provider.ledger.snapshot()
    assert snapshot["totals"]["input_tokens"] == 800 and snapshot["totals"]["usage_complete"] is False
    assert snapshot["remaining"]["input_tokens"] == 200


def test_the_input_cap_boundary_is_exact() -> None:
    assert go(wrap(Fake(None, count=1000), max_input_tokens=1000)).data == {"ok": True}
    with pytest.raises(RunStopped, match="input token cap"):
        go(wrap(Fake(None, count=1001), max_input_tokens=1000))


def test_the_output_token_cap_reserves_the_full_output_cap_of_each_request() -> None:
    fake = Fake(None)
    provider = wrap(fake, request_output_tokens=100, max_output_tokens=250)

    go(provider)
    go(provider)
    with pytest.raises(RunStopped) as stopped:
        go(provider)

    assert stopped.value.reason == "output-token-limit" and fake.calls == 2


def test_the_cost_cap_reserves_the_worst_case_before_each_request() -> None:
    fake = Fake(None, count=1000)
    provider = wrap(fake, PRICES, request_output_tokens=1000, max_cost_usd=Decimal("0.10"))

    go(provider)
    with pytest.raises(RunStopped) as stopped:
        go(provider)

    assert stopped.value.reason == "cost-limit" and "0.060000" in str(stopped.value) and "0.100000" in str(stopped.value)
    assert provider.ledger.snapshot()["requests"][0]["reserved_cost_usd"] == "0.060000"


def test_a_cost_cap_without_rates_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ConfigError, match="needs ai.prices"):
        Ledger(RunLimits(max_cost_usd=Decimal(1)).resolved("ollama"))


def test_the_estimated_maximum_cost_is_bounded_by_the_caps_and_absent_without_rates() -> None:
    open_ended = ledger(PRICES, max_input_tokens=100_000, max_output_tokens=10_000, request_output_tokens=4000, max_requests=6)
    assert open_ended.snapshot()["max_estimated_cost_usd"] == "1.500000", "100k * $10 + 10k * $50 per million tokens"
    capped = ledger(PRICES, max_cost_usd=Decimal("0.25"))
    assert capped.snapshot()["max_estimated_cost_usd"] == "0.250000"
    assert ledger().snapshot()["max_estimated_cost_usd"] is None and ledger().snapshot()["totals"]["cost_usd"] is None


# ------------------------------------------------------------------ reconciliation
def test_complete_usage_replaces_the_reservation_and_frees_allowance() -> None:
    fake = Fake(Usage(100, 40, 0, 0), count=600)
    provider = wrap(fake, PRICES, max_input_tokens=1000, request_output_tokens=500)

    go(provider)
    go(provider)

    snapshot = provider.ledger.snapshot()
    assert fake.calls == 2, "the first request really used 100 tokens, not its 600 estimate"
    assert snapshot["totals"]["input_tokens"] == 200 and snapshot["totals"]["output_tokens"] == 80
    assert snapshot["totals"]["cost_usd"] == "0.006000" and snapshot["totals"]["usage_complete"] is True
    assert snapshot["requests"][0]["cost_measured"] is True and snapshot["requests"][0]["served_model"] == "served"


@pytest.mark.parametrize("usage", [None, Usage(10, 5, None, None), Usage(None, 5, 0, 0)], ids=["none", "no-cache-fields", "no-input"])
def test_missing_or_partial_usage_keeps_the_worst_case_and_shows_what_was_reported(usage: Usage | None) -> None:
    provider = wrap(Fake(usage, count=500), PRICES, request_output_tokens=200)

    go(provider)

    request = provider.ledger.snapshot()["requests"][0]
    assert request["usage_measured"] is False and request["charged_input_tokens"] == 500 and request["charged_output_tokens"] == 200
    assert request["usage"] == (None if usage is None else usage.to_dict())
    assert request["charged_cost_usd"] == request["reserved_cost_usd"] == "0.015000"
    assert provider.ledger.snapshot()["totals"]["unmeasured_requests"] == 1


def test_failures_keep_their_reservation_unless_the_provider_reported_usage() -> None:
    cut_off = ProviderError("cut off")
    cut_off.usage = Usage(300, 200, 0, 0)
    fake = Fake(ProviderError("unreachable"), cut_off, MalformedProviderOutput("bad"), count=500)
    provider = wrap(fake, request_output_tokens=200)

    for _ in range(3):
        with pytest.raises(ProviderError):
            go(provider)

    first, second, third = provider.ledger.snapshot()["requests"]
    assert (first["charged_input_tokens"], first["usage_measured"]) == (500, False), "an unreachable provider may still have billed"
    assert (second["charged_input_tokens"], second["charged_output_tokens"], second["usage_measured"]) == (300, 200, True)
    assert third["usage_measured"] is False and {first["outcome"], second["outcome"]} == {"provider-error"}


def test_an_interrupt_keeps_the_reservation_and_is_not_swallowed() -> None:
    provider = wrap(Fake(KeyboardInterrupt(), count=500))

    with pytest.raises(KeyboardInterrupt):
        go(provider)

    request = provider.ledger.snapshot()["requests"][0]
    assert request["outcome"] == "interrupted" and request["charged_input_tokens"] == 500 and request["usage_measured"] is False


def test_cache_write_rates_are_reserved_at_the_highest_applicable_rate() -> None:
    prices = PriceProfile("v1", "ollama", "fake", Decimal(10), Decimal(50), cache_read_usd_per_mtok=Decimal(1), cache_write_usd_per_mtok=Decimal("12.5"))

    assert prices.reserve_micro_usd(1_000_000, 0) == 12_500_000, "an input token may be billed as a cache write"
    assert prices.cost_micro_usd(Usage(100_000, 10_000, 200_000, 50_000)) == 1_000_000 + 500_000 + 200_000 + 625_000


def test_cache_tokens_without_a_rate_leave_the_cost_unknown() -> None:
    partial = PriceProfile("v1", "ollama", "fake", Decimal(10), Decimal(50), served_models=("served",))
    cached = Usage(100, 40, 5000, 0)
    assert partial.cost_micro_usd(cached) is None and not partial.complete
    assert partial.reserve_micro_usd(10, 10) is None, "a worst case cannot be reserved without every rate"
    provider = wrap(Fake(cached, count=500), partial, request_output_tokens=200)

    go(provider)

    snapshot = provider.ledger.snapshot()
    request = snapshot["requests"][0]
    assert request["usage_measured"] is True and request["cost_measured"] is False, "the tokens are measured, the cost is not"
    assert request["charged_cost_usd"] is None and request["reserved_cost_usd"] is None
    assert snapshot["totals"]["cost_usd"] is None and snapshot["max_estimated_cost_usd"] is None, "unknown, not zero"
    assert request["charged_input_tokens"] == 5100, "but the tokens are known"


# ------------------------------------------------------------------ concurrency and cancellation
def test_concurrent_requests_in_one_run_can_never_pass_the_cap_together() -> None:
    fake = Fake(None, count=10)
    provider = wrap(fake, max_requests=5, request_output_tokens=100)
    barrier = threading.Barrier(20)
    outcomes: list[str] = []

    def worker() -> None:
        barrier.wait()
        try:
            go(provider)
            outcomes.append("sent")
        except RunStopped:
            outcomes.append("stopped")

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("sent") == 5 and outcomes.count("stopped") == 15 and fake.calls == 5
    assert provider.ledger.snapshot()["totals"]["requests"] == 5


def test_concurrent_jobs_have_distinct_ledgers() -> None:
    first, second = Fake(None, count=10), Fake(None, count=10)
    one, two = wrap(first, max_requests=3, request_output_tokens=100), wrap(second, max_requests=3, request_output_tokens=100)
    stopped: list[str] = []

    def run(provider: BudgetedProvider, name: str) -> None:
        for _ in range(6):
            try:
                go(provider)
            except RunStopped:
                stopped.append(name)

    threads = [threading.Thread(target=run, args=(one, "one")), threading.Thread(target=run, args=(two, "two"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert (first.calls, second.calls) == (3, 3) and stopped.count("one") == 3 and stopped.count("two") == 3
    assert one.ledger is not two.ledger and one.ledger.snapshot()["totals"]["requests"] == 3


def test_cancellation_stops_the_next_request_without_counting_or_sending_and_keeps_the_one_in_flight() -> None:
    holder: list[BudgetedProvider] = []
    fake = Fake(Usage(10, 5, 0, 0), count=100, on_call=lambda: holder[0].cancel())
    provider = wrap(fake)
    holder.append(provider)

    response = go(provider)
    with pytest.raises(RunStopped) as stopped:
        go(provider)

    assert response.data == {"ok": True} and stopped.value.reason == "cancelled"
    assert fake.calls == 1 and fake.counts == 1, "the second request was refused before it was even counted"
    snapshot = provider.ledger.snapshot()
    assert snapshot["stop_reason"] == "cancelled" and snapshot["requests"][0]["usage_measured"] is True


# ------------------------------------------------------------------ providers
def fake_sdk(monkeypatch, *, usage: Any = None, stop_reason: str = "end_turn", text: str = "{}", count_error: bool = False) -> dict[str, list]:
    seen: dict[str, list] = {"create": [], "count": []}

    class Status(Exception):
        status_code = 500

    module = types.SimpleNamespace(
        AuthenticationError=type("AuthenticationError", (Status,), {}), RateLimitError=type("RateLimitError", (Status,), {}),
        APIStatusError=Status, APIConnectionError=type("APIConnectionError", (Exception,), {}),
    )

    def create(**kwargs: Any) -> Any:
        seen["create"].append(kwargs)
        block = types.SimpleNamespace(type="text", text=text)
        return types.SimpleNamespace(stop_reason=stop_reason, content=[block], model="claude-served", usage=usage)

    def count_tokens(**kwargs: Any) -> Any:
        seen["count"].append(kwargs)
        if count_error:
            raise module.APIConnectionError()
        return types.SimpleNamespace(input_tokens=1234)

    class Client(types.SimpleNamespace):
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    module.DefaultHttpxClient = lambda **kwargs: None
    module.Anthropic = lambda **kwargs: Client(beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create, count_tokens=count_tokens)))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return seen


def test_the_anthropic_request_carries_explicit_limits_and_no_silent_fallback(monkeypatch) -> None:
    seen = fake_sdk(monkeypatch)

    AnthropicProvider("claude-opus-5-5", max_output_tokens=3000, effort="low").generate("sys", "usr", OBJECT)

    request = seen["create"][0]
    assert request["max_tokens"] == 3000 and request["output_config"]["effort"] == "low"
    assert "fallbacks" not in request and "betas" not in request, "a substitute model would bill at an unreserved rate"
    assert request["model"] == "claude-opus-5-5"
    seen = fake_sdk(monkeypatch)
    AnthropicProvider(None).generate("sys", "usr", OBJECT)
    assert "effort" not in seen["create"][0]["output_config"], "effort is sent only when the project chose one"


def test_anthropic_usage_keeps_cache_categories_reasoning_and_unknowns_distinct(monkeypatch) -> None:
    raw = types.SimpleNamespace(
        input_tokens=100, output_tokens=60, cache_read_input_tokens=7, cache_creation_input_tokens=3,
        output_tokens_details=types.SimpleNamespace(thinking_tokens=25),
    )
    fake_sdk(monkeypatch, usage=raw)
    assert AnthropicProvider(None).generate("s", "u", OBJECT).usage == Usage(100, 60, 7, 3, 25)

    fake_sdk(monkeypatch, usage=types.SimpleNamespace(input_tokens=100, output_tokens=60, cache_read_input_tokens=None))
    assert AnthropicProvider(None).generate("s", "u", OBJECT).usage == Usage(100, 60, None, None, None)

    fake_sdk(monkeypatch)
    assert AnthropicProvider(None).generate("s", "u", OBJECT).usage is None


@pytest.mark.parametrize("kwargs, message", [
    ({"stop_reason": "max_tokens"}, "cut off"), ({"stop_reason": "refusal"}, "declined"), ({"text": "not json"}, "not valid JSON"),
])
def test_capped_refused_and_malformed_output_is_incomplete_but_its_usage_is_reconciled(monkeypatch, kwargs, message) -> None:
    fake_sdk(monkeypatch, usage=types.SimpleNamespace(input_tokens=11, output_tokens=22, cache_read_input_tokens=0, cache_creation_input_tokens=0), **kwargs)
    provider = BudgetedProvider(AnthropicProvider(None), ledger(provider="anthropic"))

    with pytest.raises(ProviderError, match=message):
        go(provider)

    request = provider.ledger.snapshot()["requests"][0]
    assert request["usage_measured"] is True and request["charged_input_tokens"] == 11 and request["charged_output_tokens"] == 22


def test_the_provider_token_count_is_used_after_consent_and_replaces_the_estimate(monkeypatch) -> None:
    seen = fake_sdk(monkeypatch)
    monkeypatch.setattr(AnthropicProvider, "count_input_tokens", ORIGINAL_COUNT)
    provider = BudgetedProvider(AnthropicProvider(None, effort="low"), ledger(provider="anthropic"))

    go(provider)

    counted = seen["count"][0]
    assert "max_tokens" not in counted and counted["output_config"]["effort"] == "low" and counted["system"] == "system"
    assert counted["output_config"]["format"]["schema"] == OBJECT, "the schema is part of what is counted"
    assert provider.ledger.snapshot()["requests"][0]["estimate_method"] == "provider-count"
    assert provider.ledger.snapshot()["requests"][0]["estimate_tokens"] == 1234


def test_a_failed_token_count_falls_back_to_the_labeled_estimate(monkeypatch) -> None:
    fake_sdk(monkeypatch, count_error=True)
    monkeypatch.setattr(AnthropicProvider, "count_input_tokens", ORIGINAL_COUNT)
    provider = BudgetedProvider(AnthropicProvider(None), ledger(provider="anthropic"))

    go(provider)

    assert provider.ledger.snapshot()["requests"][0]["estimate_method"] == "byte-bound"


def serve_reply(reply: dict) -> tuple[HTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


FINISHED = {"done": True, "done_reason": "stop"}


@pytest.mark.parametrize("reply, message, usage", [
    ({"message": {"content": "{}"}, "done": True, "done_reason": "length", "prompt_eval_count": 10, "eval_count": 20}, "cut off", Usage(10, 20, 0, 0)),
    ({"message": {"content": "{}"}, **FINISHED, "prompt_eval_count": 4000, "eval_count": 96}, "filled the 4096-token context window", Usage(4000, 96, 0, 0)),
    ({"message": {"content": "nope"}, **FINISHED, "prompt_eval_count": 10, "eval_count": 3}, "not valid JSON", Usage(10, 3, 0, 0)),
    ({"message": {"content": "{}"}, "done": False, "prompt_eval_count": 10, "eval_count": 3}, "finished completion", Usage(10, 3, 0, 0)),
    ({"message": {"content": "{}"}, "done": True, "done_reason": "load", "prompt_eval_count": 10, "eval_count": 3}, "finished completion", Usage(10, 3, 0, 0)),
    ({"message": {"content": "{}"}, "done": True, "prompt_eval_count": 10, "eval_count": 3}, "finished completion", Usage(10, 3, 0, 0)),
    ({"message": {"content": "{}"}, "prompt_eval_count": 10, "eval_count": 3}, "finished completion", Usage(10, 3, 0, 0)),
], ids=["length", "filled-window", "malformed", "not-done", "load", "no-reason", "no-done"])
def test_ollama_truncation_is_an_error_that_still_reports_its_usage(reply: dict, message: str, usage: Usage) -> None:
    server, host = serve_reply(reply)
    try:
        with pytest.raises(ProviderError, match=message) as error:
            OllamaProvider("llama3", host).generate("s", "u", OBJECT)
    finally:
        server.shutdown()
        server.server_close()
    assert error.value.usage == usage


def test_ollama_usage_that_was_not_reported_stays_unknown() -> None:
    server, host = serve_reply({"message": {"content": "{}"}, **FINISHED, "eval_count": 5})
    try:
        response = OllamaProvider("llama3", host).generate("s", "u", OBJECT)
    finally:
        server.shutdown()
        server.server_close()
    assert response.usage == Usage(None, 5, None, None) and not response.usage.measured


# ------------------------------------------------------------------ consent
def test_a_budget_cannot_wrap_an_external_provider_without_consent(monkeypatch) -> None:
    counted: list[int] = []
    monkeypatch.setattr(AnthropicProvider, "count_input_tokens", lambda *args: counted.append(1) or 1)

    with pytest.raises(ConsentError):
        budgeted(AnthropicProvider(None), AiSettings(provider="anthropic"))

    assert counted == []


def test_no_counting_request_precedes_the_source_consent_check(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys, provider="anthropic")
    counted: list[int] = []
    monkeypatch.setattr(AnthropicProvider, "count_input_tokens", lambda *args: counted.append(1) or 1)
    monkeypatch.setattr(AnthropicProvider, "generate", lambda *args: pytest.fail("sent without consent"))
    assert invoke(root, capsys, "ai-configure", "--consent-external")[0] == 0

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 2 and "Consent for the evidence bundle does not cover it" in payload["errors"][0]
    assert counted == [] and not (root / ".repodocs" / "receipts").exists()


# ------------------------------------------------------------------ the shared run through discovery
def test_discovery_exploration_and_one_repair_share_one_cap(tmp_path: Path, capsys) -> None:
    root, config, ir = setup(tmp_path, capsys)
    more = lambda message: {  # noqa: E731
        **full_answer(message, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 20}], complete=False),
    }
    scripted = Scripted(MalformedProviderOutput("not json"), more, more)
    provider = BudgetedProvider(scripted, ledger(max_requests=2))  # type: ignore[arg-type]

    view, _ = discover(config, ir, provider)

    assert scripted.script == [more], "the repair and the first exploration used both requests; the third was never sent"
    assert view["status"] == "partial" and view["partial_reason"] == "usage-budget", "a budget stop is not a provider failure"
    snapshot = provider.ledger.snapshot()
    assert snapshot["totals"]["requests"] == 2 and snapshot["stop_reason"] == "request-limit"
    assert [item["outcome"] for item in snapshot["requests"]] == ["provider-error", "ok"]


def test_a_stopped_run_with_no_usable_answer_fails_with_the_reason(tmp_path: Path, capsys) -> None:
    root, config, ir = setup(tmp_path, capsys)
    provider = BudgetedProvider(Scripted(generic), ledger(context_window_tokens=2000, request_output_tokens=1000))  # type: ignore[arg-type]

    with pytest.raises(ProviderError, match="context window"):
        discover(config, ir, provider)

    assert provider.ledger.snapshot()["totals"]["requests"] == 0


# ------------------------------------------------------------------ CLI receipts
def receipts(root: Path) -> list[Path]:
    return sorted((root / ".repodocs" / "receipts").glob("*.json"))


def edit_ai(root: Path, **values: Any) -> None:
    path = root / "repodocs.json"
    configuration = json.loads(path.read_text(encoding="utf-8"))
    configuration["ai"].update(values)
    path.write_text(json.dumps(configuration), encoding="utf-8")


def test_discovery_writes_a_valid_local_receipt_and_reports_usage(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    allow_large_context(root)
    edit_ai(root, prices={
        "version": "2026-11-01", "provider": "ollama", "model": "replay-model", "served_models": ["replayed:replay-model"],
        "input_usd_per_mtok": 0, "output_usd_per_mtok": 0, "cache_read_usd_per_mtok": 0, "cache_write_usd_per_mtok": 0,
    })
    use(monkeypatch, Scripted(generic))

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 0, payload["errors"]
    usage = payload["data"]["usage"]
    assert usage["requests"] == 1 and usage["stop_reason"] is None and usage["usage_complete"] is False
    assert usage["remaining"]["requests"] == 5 and usage["max_estimated_cost_usd"] == "0.000000"
    assert_valid("cli-response-1.0.json", payload)
    (path,) = receipts(root)
    assert path.as_posix() == usage["receipt"]
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert_valid("usage-receipt-1.0.json", receipt)
    assert receipt["command"] == "architecture-discover" and receipt["price_profile"]["version"] == "2026-11-01"
    assert receipt["requests"][0]["usage"] is None and receipt["requests"][0]["usage_measured"] is False, "no usage was reported, so none is invented"
    assert receipt["limits"]["context_window_tokens"] == 131072
    text = path.read_text(encoding="utf-8")
    context = json.loads((root / ".repodocs" / "architecture-context.json").read_text(encoding="utf-8"))
    assert all(item["text"][:30] not in text for item in context["documents"] if len(item["text"]) > 30), "a receipt holds no source text"


def test_a_run_that_hits_its_cap_reports_why_it_stopped(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    allow_large_context(root)
    edit_ai(root, limits={"context_window_tokens": 131072, "max_requests": 1})
    ask = lambda message: full_answer(message, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 20}], complete=False)  # noqa: E731
    use(monkeypatch, Scripted(ask, ask))

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 0 and payload["data"]["status"] == "partial"
    assert payload["data"]["usage"]["stop_reason"] == "request-limit" and payload["data"]["usage"]["requests"] == 1
    receipt = json.loads(receipts(root)[0].read_text(encoding="utf-8"))
    assert receipt["stop_reason"] == "request-limit" and "run limit of 1" in receipt["stop_message"]
    assert_valid("usage-receipt-1.0.json", receipt)


def test_a_provider_failure_still_leaves_a_receipt_that_keeps_the_reservation(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    allow_large_context(root)
    use(monkeypatch, Scripted(ProviderError("connection reset")))

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 7 and "connection reset" in payload["errors"][0]
    receipt = json.loads(receipts(root)[0].read_text(encoding="utf-8"))
    assert receipt["totals"]["requests"] == 1 and receipt["totals"]["usage_complete"] is False
    assert receipt["requests"][0]["outcome"] == "provider-error" and receipt["totals"]["input_tokens"] > 0
    assert_valid("usage-receipt-1.0.json", receipt)


def test_a_prompt_that_cannot_fit_stops_ai_interpret_before_the_runtime_is_contacted(tmp_path: Path, capsys) -> None:
    server, hits = serve()
    try:
        root = scanned(tmp_path, capsys)
        host = f"http://127.0.0.1:{server.server_port}"
        assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "llama3", "--ollama-host", host)[0] == 0

        code, payload = invoke(root, capsys, "ai-interpret")
    finally:
        server.shutdown()
        server.server_close()

    assert code == 7 and "4096-token context window" in payload["errors"][0] and hits == []
    assert_valid("cli-response-1.0.json", payload)
    receipt = json.loads(receipts(root)[0].read_text(encoding="utf-8"))
    assert receipt["command"] == "ai-interpret" and receipt["stop_reason"] == "context-window" and receipt["totals"]["requests"] == 0
    assert not (root / ".repodocs" / "ai-claims.json").exists()


def test_ai_interpret_reports_usage_when_it_fits(tmp_path: Path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "llama3")[0] == 0
    allow_large_context(root)
    monkeypatch.setattr(providers, "resolve_provider", lambda settings: Fake(Usage(900, 80, 0, 0)))

    code, payload = invoke(root, capsys, "ai-interpret")

    assert code == 0, payload["errors"]
    usage = payload["data"]["usage"]
    assert usage["input_tokens"] == 900 and usage["output_tokens"] == 80 and usage["usage_complete"] is True
    assert_valid("cli-response-1.0.json", payload)


def test_the_new_settings_validate_against_the_published_config_schema() -> None:
    config = {
        "schema_version": "1.0", "project": {"id": "p", "name": "P"}, "repositories": [], "documentation": {"output_directory": "docs/generated"},
        "ai": {
            "provider": "anthropic", "external_consent": False, "effort": "medium",
            "limits": {"context_window_tokens": 4096, "max_requests": 2, "max_cost_usd": 0.5},
            "prices": {
                "version": "v1", "provider": "anthropic", "model": "claude-opus-5-5", "input_usd_per_mtok": 5, "output_usd_per_mtok": 25,
                "cache_read_usd_per_mtok": 0.5, "cache_write_usd_per_mtok": 6.25,
            },
        },
    }
    assert_valid("project-config-1.0.json", config)
    assert replace(AiSettings(), limits=RunLimits(max_requests=2)).to_dict()["limits"] == {"max_requests": 2}



# ------------------------------------------------------------------ review regressions (#81)
def test_reported_usage_over_the_admitted_cap_stops_the_run_instead_of_succeeding() -> None:
    fake = Fake(Usage(input_tokens=1000, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0), count=1)
    provider = BudgetedProvider(fake, Ledger(RunLimits(context_window_tokens=4096, request_output_tokens=8, max_input_tokens=10).resolved("ollama")))  # type: ignore[arg-type]

    with pytest.raises(RunStopped) as stopped:
        go(provider)
    with pytest.raises(RunStopped):
        go(provider)

    assert stopped.value.reason == "reported-usage-over-limit" and "cannot be undone" in str(stopped.value)
    assert fake.calls == 1, "nothing further is sent after the overage"
    snapshot = provider.ledger.snapshot()
    assert snapshot["stop_reason"] == "reported-usage-over-limit" and snapshot["totals"]["input_tokens"] == 1000
    assert snapshot["requests"][0]["exceeded_reservation"] is True and snapshot["remaining"]["input_tokens"] == 0


def test_reported_cost_over_the_cap_stops_the_run() -> None:
    fake = Fake(Usage(input_tokens=50_000, output_tokens=10, cache_read_tokens=0, cache_write_tokens=0), count=10)
    provider = wrap(fake, PRICES, request_output_tokens=10, max_cost_usd=Decimal("0.01"))

    with pytest.raises(RunStopped) as stopped:
        go(provider)

    assert stopped.value.reason == "reported-usage-over-limit" and "USD" in str(stopped.value)


def test_partial_usage_above_the_reservation_is_charged_at_what_was_reported() -> None:
    provider = wrap(Fake(Usage(5000, None, None, None), count=100), request_output_tokens=50)

    go(provider)

    request = provider.ledger.snapshot()["requests"][0]
    assert request["usage_measured"] is False and request["charged_input_tokens"] == 5000, "a reported count is never discarded"
    assert request["charged_output_tokens"] == 50 and request["exceeded_reservation"] is True


@pytest.mark.parametrize("text", [
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" * 40,
    "東京都の非同期処理とデータベース" * 60,
    "{}[]();:,.!?<>/\\|`~@#$%^&*-_=+'\"" * 50,
    "🙂🚀✅" * 100,
], ids=["hash", "cjk", "punctuation", "emoji"])
def test_the_byte_bound_is_never_smaller_than_the_encoded_size(text: str) -> None:
    bound = estimate_tokens(text)

    assert bound >= len(text.encode("utf-8")) + budgets.REQUEST_OVERHEAD_TOKENS
    assert bound >= len(text), "at least one token per character and per byte"


def test_token_count_requests_have_their_own_bounded_allowance_and_are_recorded() -> None:
    fake = Fake(MEASURED, count=10)
    provider = wrap(fake, max_requests=3, max_count_requests=2)

    for _ in range(3):
        go(provider)

    snapshot = provider.ledger.snapshot()
    assert fake.counts == 2 and fake.calls == 3
    assert [item["estimate_method"] for item in snapshot["requests"]] == ["provider-count", "provider-count", "byte-bound"]
    assert snapshot["totals"]["count_requests"] == 2 and snapshot["remaining"]["count_requests"] == 0


def test_no_count_request_is_sent_once_a_run_has_stopped() -> None:
    fake = Fake(MEASURED, count=10)
    provider = wrap(fake, max_requests=1)

    go(provider)
    with pytest.raises(RunStopped):
        go(provider)

    assert fake.counts == 1 and provider.ledger.snapshot()["totals"]["count_requests"] == 1


def test_concurrent_counts_cannot_pass_their_allowance() -> None:
    fake = Fake(None, count=10)
    provider = wrap(fake, max_requests=6, max_count_requests=3, request_output_tokens=10)
    barrier = threading.Barrier(12)

    def worker() -> None:
        barrier.wait()
        try:
            go(provider)
        except RunStopped:
            pass

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert fake.counts == 3 and fake.calls == 6


def test_the_anthropic_client_never_retries_internally(monkeypatch) -> None:
    fake_sdk(monkeypatch)
    import anthropic

    built: list[dict] = []
    original = anthropic.Anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: built.append(kwargs) or original(**kwargs))

    AnthropicProvider(None).generate("s", "u", OBJECT)

    assert built and built[0]["max_retries"] == 0, "a retry inside the SDK would be a request the ledger never reserved"


def test_a_price_profile_for_another_model_is_not_applied() -> None:
    other = AiSettings(prices=replace(PRICES, model="cheaper-model"))

    ledger = ledger_for(Fake(), other)  # type: ignore[arg-type]

    assert ledger.prices is None and ledger.snapshot()["max_estimated_cost_usd"] is None
    assert any("does not apply" in note for note in ledger.snapshot()["notes"])
    capped = AiSettings(prices=replace(PRICES, model="cheaper-model"), limits=RunLimits(max_cost_usd=Decimal(1)))
    with pytest.raises(ConfigError, match="not ollama/fake"):
        ledger_for(Fake(), capped)  # type: ignore[arg-type]


def test_a_served_model_outside_the_profile_keeps_its_reservation_and_stops_a_cost_capped_run() -> None:
    class Swapped(Fake):
        def generate(self, system: str, user: str, schema: dict) -> ProviderResponse:
            self.calls += 1
            return ProviderResponse({"ok": True}, "substitute-model", MEASURED)

    fake = Swapped(count=100)
    provider = wrap(fake, PRICES, request_output_tokens=100, max_cost_usd=Decimal(1))

    go(provider)
    with pytest.raises(RunStopped) as stopped:
        go(provider)

    snapshot = provider.ledger.snapshot()
    request = snapshot["requests"][0]
    assert request["cost_measured"] is False and request["charged_cost_usd"] is None, "another model's cost is unknown, not this profile's"
    assert snapshot["totals"]["cost_usd"] is None and snapshot["remaining"]["cost_usd"] is None
    assert stopped.value.reason == "unpriced-usage" and "substitute-model" in str(stopped.value) and fake.calls == 1
    aliased = wrap(Fake(MEASURED, count=100), PRICES, request_output_tokens=100, max_cost_usd=Decimal(1))
    go(aliased)
    assert aliased.ledger.snapshot()["requests"][0]["cost_measured"] is True, "a listed served model is priced"


def test_a_cost_cap_needs_every_billed_rate() -> None:
    incomplete = PriceProfile("v1", "ollama", "fake", Decimal(1), Decimal(1), cache_read_usd_per_mtok=Decimal(0))

    with pytest.raises(ConfigError, match="cache-write"):
        Ledger(RunLimits(max_cost_usd=Decimal(1)).resolved("ollama"), incomplete)


def test_limits_cannot_be_raised_by_building_them_directly_and_a_run_can_only_lower_them() -> None:
    with pytest.raises(ConfigError, match="ceiling of 6"):
        RunLimits(max_requests=7)
    with pytest.raises(ConfigError, match="positive integer"):
        RunLimits(max_input_tokens=0)
    project = RunLimits(max_requests=4, max_input_tokens=1000)

    run = project.narrowed(RunLimits(max_requests=6, max_input_tokens=500, max_output_tokens=10))

    assert (run.max_requests, run.max_input_tokens, run.max_output_tokens) == (4, 500, 10)
    assert project.narrowed(None) is project


def test_a_budget_is_not_wrapped_twice() -> None:
    inner = wrap(Fake())

    assert budgeted(inner, AiSettings(limits=RunLimits(context_window_tokens=100_000))) is inner  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="exceeds the project"):
        budgeted(inner, AiSettings(limits=RunLimits(context_window_tokens=100_000, max_requests=2)))  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="not both"):
        budgeted(inner, AiSettings(limits=RunLimits(context_window_tokens=100_000)), RunLimits(max_requests=1))  # type: ignore[arg-type]
    assert not hasattr(inner, "count_input_tokens"), "only the ledger counts, so an outer wrapper cannot count again"
    assert inner.request_body("s", "u", OBJECT)["messages"][0]["content"] == "u"


# ------------------------------------------------------------------ the shared service owns the budget (#69)
def service_project(tmp_path: Path, capsys) -> Path:
    from test_service import project

    return project(tmp_path, capsys)


class Counting(Scripted):
    """A scripted local provider with an exact request body and a token counter."""

    def __init__(self, *script: Any, count: int = 500) -> None:
        super().__init__(*script)
        self.counts = 0
        self.count = count
        self.bodies = 0

    def request_body(self, system: str, user: str, schema: dict) -> dict:
        self.bodies += 1
        return {"model": self.model, "system": system, "user": user, "schema": schema}

    def count_input_tokens(self, system: str, user: str, schema: dict) -> int:
        self.counts += 1
        return self.count


def test_the_service_budgets_an_injected_provider_and_reports_its_receipt(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)
    provider = Counting(full_answer)

    result = service.execute("discover", service.DiscoverRequest(root, provider=provider))

    assert result.status == "complete", result.error
    usage = result.data["usage"]
    assert usage["requests"] == 1 and usage["count_requests"] == 1 and provider.counts == 1
    receipt = json.loads(Path(usage["receipt"]).read_text(encoding="utf-8"))
    assert_valid("usage-receipt-1.0.json", receipt)
    assert receipt["requests"][0]["estimate_method"] == "provider-count", "the cancellation wrapper did not hide the counter"


def test_service_run_limits_only_lower_the_project_limits_and_a_budget_stop_is_reported(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)
    ask = lambda message: full_answer(message, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 20}], complete=False)  # noqa: E731
    provider = Counting(ask, ask)

    result = service.execute("discover", service.DiscoverRequest(root, provider=provider, limits=RunLimits(max_requests=1)))

    assert result.status == "partial" and result.data["partial_reason"] == "usage-budget"
    assert result.data["usage"]["stop_reason"] == "request-limit" and len(provider.messages) == 1


def test_a_failed_service_run_still_returns_its_receipt(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)

    result = service.execute("discover", service.DiscoverRequest(root, provider=Counting(ProviderError("connection reset"))))

    assert result.status == "failed" and result.error.code == "provider"
    receipt = json.loads(Path(result.data["usage"]["receipt"]).read_text(encoding="utf-8"))
    assert receipt["totals"]["requests"] == 1 and receipt["requests"][0]["outcome"] == "provider-error"


def test_a_cancelled_service_run_counts_and_sends_nothing_more_and_records_the_stop(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)
    token = service.CancellationToken()

    def ask_then_cancel(message: dict) -> dict:
        token.cancel()
        return full_answer(message, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 20}], complete=False)

    provider = Counting(ask_then_cancel, full_answer)

    result = service.execute("discover", service.DiscoverRequest(root, provider=provider), service.RunHooks(cancel=token))

    assert result.status == "cancelled" and len(provider.messages) == 1 and provider.counts == 1
    receipt = json.loads(Path(result.data["usage"]["receipt"]).read_text(encoding="utf-8"))
    assert receipt["stop_reason"] == "cancelled" and receipt["totals"]["count_requests"] == 1


def test_the_service_never_counts_before_both_consents(tmp_path: Path, capsys) -> None:
    from repodocs import service
    from repodocs.config import ProjectConfig

    root = service_project(tmp_path, capsys)
    config = ProjectConfig.load(root)
    config.ai = replace(config.ai, external_consent=False)
    config.save()
    provider = Counting(full_answer)
    provider.external = True

    result = service.execute("discover", service.DiscoverRequest(root, provider=provider))

    assert result.status == "failed" and provider.counts == 0 and provider.bodies == 0 and not provider.messages
    assert not (root / ".repodocs" / "receipts").exists()


def test_a_caller_supplied_budget_is_used_as_it_is(tmp_path: Path, capsys) -> None:
    from repodocs import service
    from repodocs.config import ProjectConfig

    root = service_project(tmp_path, capsys)
    provider = Counting(full_answer)
    shared = BudgetedProvider(provider, ledger_for(provider, ProjectConfig.load(root).ai, RunLimits(max_requests=2)))  # type: ignore[arg-type]

    result = service.execute("discover", service.DiscoverRequest(root, provider=shared))

    assert result.status == "complete" and shared.ledger.snapshot()["totals"]["requests"] == 1
    assert result.data["usage"]["remaining"]["requests"] == 1, "the caller's ledger, not a second one"


def test_the_context_preview_shows_the_limits_and_worst_case_cost_without_contacting_the_provider(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)
    provider = Counting(full_answer)

    result = service.execute("context", service.ContextRequest(root, provider=provider))

    budget = result.data["budget"]
    assert budget["limits"]["max_requests"] == 6 and budget["max_estimated_cost_usd"] is None and budget["price_profile"] is None
    assert provider.counts == 0 and not provider.messages


# ------------------------------------------------------------------ second review regressions (#81)
def test_the_reserved_output_is_the_output_cap_the_provider_really_sends() -> None:
    with pytest.raises(ConfigError, match="output cap of 16000 tokens but this run reserves 1000"):
        BudgetedProvider(AnthropicProvider(None), ledger(provider="anthropic", request_output_tokens=1000))
    smaller = BudgetedProvider(AnthropicProvider(None, max_output_tokens=500), ledger(provider="anthropic", request_output_tokens=1000))
    assert smaller.output_cap == 500
    fake = Fake(None, count=100)
    fake.max_output_tokens = 300
    provider = BudgetedProvider(fake, ledger(request_output_tokens=1000))  # type: ignore[arg-type]
    go(provider)
    assert provider.ledger.snapshot()["requests"][0]["output_cap_tokens"] == 300 and provider.ledger.snapshot()["totals"]["output_tokens"] == 300


def test_the_service_refuses_a_provider_whose_output_cap_exceeds_the_run(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)
    provider = Counting(full_answer)
    provider.max_output_tokens = 16000

    result = service.execute("discover", service.DiscoverRequest(root, provider=provider, limits=RunLimits(request_output_tokens=1000)))

    assert result.status == "failed" and "output cap" in result.error.message and not provider.messages and provider.counts == 0


def test_partial_usage_raises_the_charged_cost_with_the_charged_tokens() -> None:
    fake = Fake(Usage(50_000, 100, None, None), count=100)
    provider = wrap(fake, PRICES, request_output_tokens=100, max_cost_usd=Decimal("0.05"))

    with pytest.raises(RunStopped) as stopped:
        go(provider)
    with pytest.raises(RunStopped):
        go(provider)

    request = provider.ledger.snapshot()["requests"][0]
    assert stopped.value.reason == "reported-usage-over-limit" and fake.calls == 1
    assert request["charged_input_tokens"] == 50_000 and request["charged_cost_usd"] == "0.505000", "50,000 x $10 + 100 x $50 per million"


def test_a_cancel_during_a_token_count_sends_no_request(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)
    token = service.CancellationToken()

    class CancelWhileCounting(Counting):
        def count_input_tokens(self, system: str, user: str, schema: dict) -> int:
            token.cancel()
            return super().count_input_tokens(system, user, schema)

    provider = CancelWhileCounting(full_answer)
    result = service.execute("discover", service.DiscoverRequest(root, provider=provider), service.RunHooks(cancel=token))

    assert result.status == "cancelled" and provider.counts == 1 and not provider.messages
    receipt = json.loads(Path(result.data["usage"]["receipt"]).read_text(encoding="utf-8"))
    assert receipt["totals"]["requests"] == 0 and receipt["stop_reason"] == "cancelled"


def test_a_caller_budget_and_separate_run_limits_are_refused(tmp_path: Path, capsys) -> None:
    from repodocs import service
    from repodocs.config import ProjectConfig

    root = service_project(tmp_path, capsys)
    provider = Counting(full_answer)
    shared = BudgetedProvider(provider, ledger_for(provider, ProjectConfig.load(root).ai))  # type: ignore[arg-type]

    result = service.execute("discover", service.DiscoverRequest(root, provider=shared, limits=RunLimits(max_requests=1)))

    assert result.status == "failed" and "not both" in result.error.message and not provider.messages


def test_a_budget_stop_before_any_request_is_a_budget_error_not_a_model_failure(tmp_path: Path, capsys) -> None:
    from repodocs import service

    root = service_project(tmp_path, capsys)
    provider = Counting(full_answer, count=200_000)

    result = service.execute("discover", service.DiscoverRequest(root, provider=provider))

    assert result.status == "failed" and result.error.code == "budget" and result.error.exit_code == 7
    assert "model returned" not in result.error.message and "context window" in result.error.message
    assert result.data["usage"]["stop_reason"] == "context-window" and result.data["usage"]["requests"] == 0 and not provider.messages


def test_failed_cli_runs_print_their_usage(tmp_path: Path, capsys) -> None:
    server, hits = serve()
    try:
        root = scanned(tmp_path, capsys)
        host = f"http://127.0.0.1:{server.server_port}"
        assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "llama3", "--ollama-host", host)[0] == 0

        code, payload = invoke(root, capsys, "ai-interpret")
    finally:
        server.shutdown()
        server.server_close()

    assert code == 7 and hits == [] and payload["ok"] is False
    usage = payload["data"]["usage"]
    assert usage["stop_reason"] == "context-window" and usage["requests"] == 0 and Path(usage["receipt"]).exists()
    assert_valid("cli-response-1.0.json", payload)


def test_a_failed_cli_discovery_prints_why_it_stopped(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    edit_ai(root, limits={"context_window_tokens": 4096})
    use(monkeypatch, Scripted(generic))

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 7 and payload["data"]["usage"]["stop_reason"] == "context-window"
    assert "model returned" not in payload["errors"][0]


def test_an_unlisted_served_model_without_usage_has_unknown_cost() -> None:
    class Swapped(Fake):
        def generate(self, system: str, user: str, schema: dict) -> ProviderResponse:
            self.calls += 1
            return ProviderResponse({"ok": True}, "other-bigger-model", None)

    provider = wrap(Swapped(count=100), PRICES, request_output_tokens=100)
    go(provider)

    snapshot = provider.ledger.snapshot()
    assert snapshot["requests"][0]["charged_cost_usd"] is None and snapshot["totals"]["cost_usd"] is None


def test_a_caller_budget_must_use_the_projects_price_profile_and_still_stops_on_cancel(tmp_path: Path, capsys) -> None:
    from repodocs import service
    from repodocs.config import ProjectConfig

    root = service_project(tmp_path, capsys)
    config = ProjectConfig.load(root)
    config.ai = replace(config.ai, prices=replace(PRICES, provider="ollama", model="replay-model"))
    config.save()
    provider = Counting(full_answer)
    cheap = BudgetedProvider(provider, Ledger(ledger_for(provider, config.ai).limits, replace(PRICES, provider="ollama", model="other")))  # type: ignore[arg-type]

    refused = service.execute("discover", service.DiscoverRequest(root, provider=cheap))

    assert refused.status == "failed" and "price profile" in refused.error.message and not provider.messages
    token = service.CancellationToken()

    class CancelWhileCounting(Counting):
        def count_input_tokens(self, system: str, user: str, schema: dict) -> int:
            token.cancel()
            return super().count_input_tokens(system, user, schema)

    counting = CancelWhileCounting(full_answer)
    own = BudgetedProvider(counting, ledger_for(counting, config.ai))  # type: ignore[arg-type]
    result = service.execute("discover", service.DiscoverRequest(root, provider=own), service.RunHooks(cancel=token))

    assert result.status == "cancelled" and counting.counts == 1 and not counting.messages
