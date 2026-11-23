from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from repodocs.ai.budgets import BudgetedProvider, Ledger, RunLimits, Usage
from repodocs.ai.openai import OpenAIProvider, openai_usage
from repodocs.errors import ConfigError, ProviderError


SCHEMA = {"type": "object", "properties": {"components": {"type": "array", "items": {"type": "string"}}}, "required": ["components"], "additionalProperties": False}
RESPONSE = {
    "status": "completed", "model": "requested-model", "error": None, "incomplete_details": None,
    "output": [{"type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": '{"components": ["API"]}'}]}],
    "usage": {
        "input_tokens": 190, "output_tokens": 70, "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 2},
        "output_tokens_details": {"reasoning_tokens": 30},
    },
}
COUNT = {"object": "response.input_tokens", "input_tokens": 321}
GENERATE = "/v1/responses"


@contextmanager
def endpoint(payload=RESPONSE, status=200, count=COUNT):
    """A loopback stand-in for the Responses API and its input-token count endpoint."""

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))), "auth": self.headers.get("Authorization")})
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "/redirected")
            self.end_headers()
            if self.path.endswith("/input_tokens"):
                response = count
            else:
                response = payload(requests[-1]["body"]) if callable(payload) else payload
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_responses_request_uses_schema_output_limits_and_private_credential():
    with endpoint() as (url, calls):
        provider = OpenAIProvider("requested-model", url, api_key="dummy-secret", max_output_tokens=256, reasoning_effort="low")
        result = provider.generate("system instructions", "bounded source", SCHEMA)
    assert result.data == {"components": ["API"]}
    assert result.model == "requested-model"
    assert len(calls) == 1 and calls[0]["path"] == "/v1/responses"
    assert calls[0]["auth"] == "Bearer dummy-secret"
    body = calls[0]["body"]
    assert body["text"]["format"]["schema"] == SCHEMA
    assert body["text"]["format"]["strict"] is True
    assert body["max_output_tokens"] == 256 and body["reasoning"] == {"effort": "low"}
    assert body["store"] is False
    assert "dummy-secret" not in json.dumps(body)


@pytest.mark.parametrize("state", [None, "incomplete", "failed", "cancelled", "queued", "in_progress"])
def test_noncompleted_terminal_states_are_not_accepted(state):
    payload = copy.deepcopy(RESPONSE)
    payload["status"] = state
    with endpoint(payload) as (url, calls), pytest.raises(ProviderError, match="completed"):
        OpenAIProvider("requested-model", url, api_key="dummy").generate("s", "u", SCHEMA)
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", ["refusal", "content-null", "message-incomplete", "duplicate-output", "missing-model", "error", "capped", "invalid-json", "non-object"])
def test_refusal_truncation_and_malformed_output_fail_closed(mutation):
    payload = copy.deepcopy(RESPONSE)
    if mutation == "refusal":
        payload["output"][0]["content"] = [{"type": "refusal", "refusal": "no"}]
    elif mutation == "content-null":
        payload["output"][0]["content"] = None
    elif mutation == "message-incomplete":
        payload["output"][0]["status"] = "incomplete"
    elif mutation == "duplicate-output":
        payload["output"] *= 2
    elif mutation == "missing-model":
        del payload["model"]
    elif mutation == "error":
        payload["error"] = {"message": "private"}
    elif mutation == "capped":
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
    elif mutation == "invalid-json":
        payload["output"][0]["content"][0]["text"] = "broken JSON"
    else:
        payload = []
    with endpoint(payload) as (url, _), pytest.raises(ProviderError):
        OpenAIProvider("requested-model", url, api_key="dummy").generate("s", "u", SCHEMA)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 429, 500])
def test_http_errors_are_not_retried_redirected_or_echoed(status):
    with endpoint({"secret": "canary-credential"}, status) as (url, calls), pytest.raises(ProviderError) as exc:
        OpenAIProvider("requested-model", url, api_key="canary-credential").generate("s", "u", SCHEMA)
    assert len(calls) == 1
    assert "canary-credential" not in str(exc.value)
    assert str(status) in str(exc.value)


@pytest.mark.parametrize("url", ["http://remote.example/v1", "https://user:pass@example.com/v1", "https://example.com/v1?q=x", "https://example.com/v1#x", "file:///tmp/key", "https://example.com:bad/v1"])
def test_unsafe_base_urls_are_refused(url):
    with pytest.raises(ConfigError):
        OpenAIProvider("model", url)


@pytest.mark.parametrize("key", ["", " ", "one\ntwo", "one\rtwo", "one\x00two"])
def test_invalid_credentials_are_refused_before_network(key):
    with endpoint() as (url, calls), pytest.raises(ConfigError):
        OpenAIProvider("model", url, api_key=key).generate("s", "u", SCHEMA)
    assert calls == []


def test_missing_environment_key_has_no_network_side_effect(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with endpoint() as (url, calls), pytest.raises(ConfigError):
        OpenAIProvider("model", url).generate("s", "u", SCHEMA)
    assert calls == []


@pytest.mark.parametrize("change", [("--model", "second-model"), ("--openai-base-url", "https://other.example/v1"), ("--provider", "ollama")])
def test_changing_provider_identity_requires_new_disclosure_consent(tmp_path, capsys, change):
    from conftest import invoke

    assert invoke(tmp_path, capsys, "init", "demo")[0] == 0
    assert invoke(tmp_path, capsys, "ai-configure", "--provider", "openai", "--model", "first-model", "--consent-external", "--consent-source-excerpts")[0] == 0
    code, result = invoke(tmp_path, capsys, "ai-configure", *change)
    assert code == 0
    assert result["data"]["external_consent"] is False
    assert result["data"].get("source_excerpts_consent", False) is False


def test_identity_change_can_be_explicitly_reauthorized(tmp_path, capsys):
    from conftest import invoke

    assert invoke(tmp_path, capsys, "init", "demo")[0] == 0
    assert invoke(tmp_path, capsys, "ai-configure", "--provider", "openai", "--model", "first")[0] == 0
    code, result = invoke(tmp_path, capsys, "ai-configure", "--model", "second", "--consent-external", "--consent-source-excerpts")
    assert code == 0 and result["data"]["external_consent"] is True
    assert result["data"]["source_excerpts_consent"] is True


def test_oversized_response_is_bounded(monkeypatch):
    monkeypatch.setattr("repodocs.ai.openai.MAX_RESPONSE_BYTES", 128)
    with endpoint() as (url, _), pytest.raises(ProviderError, match="byte limit"):
        OpenAIProvider("model", url, api_key="dummy").generate("s", "u", SCHEMA)


def test_loopback_stand_in_does_not_follow_proxy_settings(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with endpoint() as (url, _):
        assert OpenAIProvider("model", url, api_key="dummy").generate("s", "u", SCHEMA).data


@pytest.mark.parametrize("limit", [0, -1, 16001, True, "100"])
def test_output_limits_cannot_exceed_policy(limit):
    with pytest.raises(ConfigError):
        OpenAIProvider("model", max_output_tokens=limit)


def test_explicit_model_and_supported_effort_are_required():
    with pytest.raises(ConfigError):
        OpenAIProvider(None)
    with pytest.raises(ConfigError):
        OpenAIProvider("model", reasoning_effort="unbounded")


def discovery_response(request):
    context = json.loads(request["input"][0]["content"])
    excerpt = next(item for item in context["excerpts"] if item["path"] == "server.js")
    graph = {"components": [{"name": "API", "kind": "api", "responsibility": "Handles requests",
                             "repository_id": excerpt["repository_id"], "members": [excerpt["path"]],
                             "evidence": [excerpt["id"]], "confidence": "medium"}],
             "connections": [], "requests": [], "alternatives": [], "complete": True}
    response = copy.deepcopy(RESPONSE)
    response["output"][0]["content"][0]["text"] = json.dumps(graph)
    return response


def test_cli_stores_and_revalidates_native_openai_architecture(tmp_path, capsys, monkeypatch):
    from conftest import allow_large_context, invoke, read_ir
    from repodocs.config import ProjectConfig
    from repodocs.architecture.discovery import load_view, STATUS_CURRENT

    source = tmp_path / "source"
    source.mkdir()
    (source / "server.js").write_text('const server = require("http");\n', encoding="utf-8")
    root = tmp_path / "docs"
    root.mkdir()
    assert invoke(root, capsys, "init", "demo")[0] == 0
    assert invoke(root, capsys, "add", str(source), "--id", "source", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-test-credential")
    with endpoint(discovery_response) as (url, calls):
        assert invoke(root, capsys, "ai-configure", "--provider", "openai", "--model", "requested-model", "--openai-base-url", url)[0] == 0
        allow_large_context(root)
        code, response = invoke(root, capsys, "architecture", "discover")
        assert code == 0, response
        assert [call["path"] for call in calls] == ["/v1/responses/input_tokens", GENERATE], "one count, then one request"
        usage = response["data"]["usage"]
        assert usage["count_requests"] == 1 and usage["requests"] == 1 and usage["usage_complete"] is True
        assert usage["input_tokens"] == 190 and usage["output_tokens"] == 70, "cached and written tokens are part of the prompt total"
        status, view, _ = load_view(ProjectConfig.load(root), read_ir(root))
        assert status == STATUS_CURRENT and view["provenance"]["provider"] == "openai"
        assert invoke(root, capsys, "architecture", "discover")[0] == 0
        assert len(calls) == 2, "stored views are reused without another provider request"
    assert "dummy-test-credential" not in (root / "repodocs.json").read_text()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_cli_provider_failures_are_json_exit_seven(tmp_path, capsys, monkeypatch, status):
    from conftest import invoke

    root = tmp_path / "docs"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "server.js").write_text("const api = {};\n", encoding="utf-8")
    assert invoke(root, capsys, "init", "demo")[0] == 0
    assert invoke(root, capsys, "add", str(source), "--id", "source", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    from conftest import allow_large_context

    monkeypatch.setenv("OPENAI_API_KEY", "canary-credential")
    with endpoint({"message": "canary-credential"}, status, count={"message": "canary-credential"}) as (url, calls):
        assert invoke(root, capsys, "ai-configure", "--provider", "openai", "--model", "requested-model", "--openai-base-url", url)[0] == 0
        allow_large_context(root)
        code, response = invoke(root, capsys, "architecture", "discover")
    assert code == 7 and response["ok"] is False
    assert [call["path"] for call in calls] == ["/v1/responses/input_tokens", GENERATE], "a failed count falls back to the byte bound once"
    assert "canary-credential" not in json.dumps(response)
    receipt = json.loads(next((root / ".repodocs" / "receipts").glob("*.json")).read_text(encoding="utf-8"))
    assert receipt["requests"][0]["estimate_method"] == "byte-bound" and "canary-credential" not in json.dumps(receipt)


# ------------------------------------------------------------------ usage and the run budget (#67)
def test_usage_separates_cached_and_written_tokens_and_keeps_reasoning_a_subset():
    assert openai_usage(RESPONSE) == Usage(input_tokens=180, output_tokens=70, cache_read_tokens=8, cache_write_tokens=2, reasoning_tokens=30)
    no_writes = {"usage": {"input_tokens": 100, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 40}}}
    assert openai_usage(no_writes) == Usage(60, 5, 40, None, None), "a category that was not reported is unknown, not zero"
    assert not openai_usage(no_writes).measured
    no_details = {"usage": {"input_tokens": 100, "output_tokens": 5}}
    assert openai_usage(no_details) == Usage(100, 5, None, None) and openai_usage(no_details).prompt_tokens is None
    assert openai_usage({"usage": {"input_tokens": 5, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 9}}}) is None
    assert openai_usage({"usage": "lots"}) is None and openai_usage({}) is None and openai_usage([]) is None


def test_a_response_carries_its_usage_and_serving_model():
    with endpoint() as (url, _):
        result = OpenAIProvider("requested-model", url, api_key="dummy").generate("s", "u", SCHEMA)
    assert result.usage == Usage(180, 70, 8, 2, 30) and result.model == "requested-model"


@pytest.mark.parametrize("mutation", ["incomplete", "invalid-json", "refusal"])
def test_unusable_replies_still_report_what_they_cost(mutation):
    payload = copy.deepcopy(RESPONSE)
    if mutation == "incomplete":
        payload["status"], payload["incomplete_details"] = "incomplete", {"reason": "max_output_tokens"}
    elif mutation == "invalid-json":
        payload["output"][0]["content"][0]["text"] = "broken JSON"
    else:
        payload["output"][0]["content"] = [{"type": "refusal", "refusal": "no"}]
    ledger = Ledger(RunLimits(context_window_tokens=100_000).resolved("openai"))
    with endpoint(payload) as (url, _), pytest.raises(ProviderError) as error:
        BudgetedProvider(OpenAIProvider("requested-model", url, api_key="dummy"), ledger).generate("s", "u", SCHEMA)
    assert error.value.usage == Usage(180, 70, 8, 2, 30)
    request = ledger.snapshot()["requests"][0]
    assert request["usage_measured"] is True and request["charged_input_tokens"] == 190 and request["charged_output_tokens"] == 70


def test_the_input_token_count_is_the_same_request_without_output_options():
    provider = OpenAIProvider("requested-model", max_output_tokens=256, reasoning_effort="low", api_key="dummy")
    with endpoint() as (url, calls):
        provider.base_url = url
        assert provider.count_input_tokens("system", "user", SCHEMA) == 321
    (call,) = calls
    assert call["path"] == "/v1/responses/input_tokens" and call["auth"] == "Bearer dummy"
    assert "max_output_tokens" not in call["body"] and "store" not in call["body"]
    assert call["body"]["text"]["format"]["schema"] == SCHEMA and call["body"]["instructions"] == "system"
    assert call["body"]["reasoning"] == {"effort": "low"}, "everything the model receives is counted"


@pytest.mark.parametrize("count", [{"object": "response.input_tokens", "input_tokens": 0}, {"input_tokens": 12}, {"object": "response.input_tokens"}, []])
def test_an_unusable_count_falls_back_to_the_byte_bound(count):
    ledger = Ledger(RunLimits(context_window_tokens=100_000).resolved("openai"))
    with endpoint(count=count) as (url, calls):
        BudgetedProvider(OpenAIProvider("requested-model", url, api_key="dummy"), ledger).generate("s", "u", SCHEMA)
    assert [call["path"] for call in calls] == ["/v1/responses/input_tokens", GENERATE]
    assert ledger.snapshot()["requests"][0]["estimate_method"] == "byte-bound"


def test_the_configured_output_cap_and_effort_reach_the_provider():
    from repodocs.ai.providers import resolve_provider
    from repodocs.config import AiSettings

    provider = resolve_provider(AiSettings(
        provider="openai", model="requested-model", external_consent=True, effort="medium",
        limits=RunLimits(context_window_tokens=64_000, request_output_tokens=3000),
    ))
    body = provider.request_body("s", "u", SCHEMA)
    assert body["max_output_tokens"] == 3000 and body["reasoning"] == {"effort": "medium"}
    assert resolve_provider(AiSettings(provider="openai", model="m", external_consent=True)).max_output_tokens == 2048, (
        "without a configured window the cautious 4,096-token default applies"
    )


def test_the_shared_service_budgets_openai_and_never_completes_a_capped_reply(tmp_path, capsys):
    from conftest import allow_large_context, invoke
    from repodocs import service

    source = tmp_path / "source"
    source.mkdir()
    (source / "server.js").write_text('const server = require("http");\n', encoding="utf-8")
    root = tmp_path / "docs"
    root.mkdir()
    assert invoke(root, capsys, "init", "demo")[0] == 0
    assert invoke(root, capsys, "add", str(source), "--id", "source", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "ai-configure", "--provider", "openai", "--model", "requested-model")[0] == 0
    allow_large_context(root)

    capped = copy.deepcopy(RESPONSE)
    capped["status"], capped["incomplete_details"] = "incomplete", {"reason": "max_output_tokens"}
    with endpoint(capped) as (url, calls):
        failed = service.execute("discover", service.DiscoverRequest(root, provider=OpenAIProvider("requested-model", url, api_key="dummy")))
    assert failed.status == "failed" and failed.view_status is None and "completed" in failed.error.message
    assert failed.data["usage"]["usage_complete"] is True and failed.data["usage"]["input_tokens"] == 190
    assert not (root / ".repodocs" / "architecture-view.json").exists(), "a capped reply never becomes a view"

    with endpoint(discovery_response) as (url, calls):
        result = service.execute("discover", service.DiscoverRequest(root, provider=OpenAIProvider("requested-model", url, api_key="dummy")))
    assert result.status == "complete" and result.data["provenance"]["provider"] == "openai"
    assert result.data["usage"]["count_requests"] == 1 and len([call for call in calls if call["path"] == GENERATE]) == 1


# ------------------------------------------------------------------ review regressions (PR #85)
@contextmanager
def raw_endpoint(reply):
    """A loopback socket answering each request with ``reply(path, head)`` bytes: broken HTTP that a real server never sends."""

    import re
    import socket

    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(0.2)
    paths: list[str] = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(re.search(rb"(?i)content-length: *(\d+)", head).group(1))
                while len(body) < length:
                    body += connection.recv(65536)
                paths.append(head.split(b" ")[1].decode())
                connection.sendall(reply(paths[-1], head, body))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}/v1", paths
    finally:
        stop.set()
        thread.join()
        server.close()


def echo_request(path, head, body):
    """A status line made of the request's own Authorization header and body: urllib reports it as BadStatusLine."""

    authorization = next(line for line in head.split(b"\r\n") if line.lower().startswith(b"authorization"))
    return authorization + b" " + body[:120] + b"\r\n\r\n"


BROKEN_REPLIES = {
    "echoed-status-line": echo_request,
    "cut-off-chunked-body": lambda *_: b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nabcde",
    "too-many-headers": lambda *_: b"HTTP/1.1 200 OK\r\n" + b"X-Filler: canary-credential\r\n" * 120 + b"\r\n",
}


@pytest.mark.parametrize("broken", sorted(BROKEN_REPLIES))
def test_broken_http_is_a_sanitized_provider_error(broken):
    with raw_endpoint(BROKEN_REPLIES[broken]) as (url, paths), pytest.raises(ProviderError) as error:
        OpenAIProvider("requested-model", url, api_key="canary-credential").generate("system", "user canary-prompt", SCHEMA)
    assert paths == ["/v1/responses"]
    assert str(error.value) == "Unable to read a valid OpenAI response"
    assert error.value.__cause__ is None and error.value.__context__ is not None and error.value.__suppress_context__


def test_a_count_that_fails_at_the_http_layer_falls_back_to_the_byte_bound():
    good = json.dumps(RESPONSE).encode()

    def reply(path, head, body):
        if path.endswith("/input_tokens"):
            return echo_request(path, head, body)
        return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(good), good)

    ledger = Ledger(RunLimits(context_window_tokens=100_000).resolved("openai"))
    with raw_endpoint(reply) as (url, paths):
        result = BudgetedProvider(OpenAIProvider("requested-model", url, api_key="canary-credential"), ledger).generate("s", "u", SCHEMA)
    assert result.data == {"components": ["API"]} and paths == ["/v1/responses/input_tokens", GENERATE]
    snapshot = ledger.snapshot()
    assert snapshot["requests"][0]["estimate_method"] == "byte-bound" and snapshot["totals"]["count_requests"] == 1


def test_the_cli_never_prints_an_echoed_credential_or_prompt(tmp_path, capsys, monkeypatch):
    from conftest import allow_large_context, invoke

    root = tmp_path / "docs"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "server.js").write_text("const api = {};\n", encoding="utf-8")
    assert invoke(root, capsys, "init", "demo")[0] == 0
    assert invoke(root, capsys, "add", str(source), "--id", "source", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    monkeypatch.setenv("OPENAI_API_KEY", "sk-CANARY-123")
    with raw_endpoint(echo_request) as (url, paths):
        assert invoke(root, capsys, "ai-configure", "--provider", "openai", "--model", "requested-model", "--openai-base-url", url)[0] == 0
        allow_large_context(root)
        code, response = invoke(root, capsys, "architecture", "discover")
    assert code == 7 and response["ok"] is False
    assert paths == ["/v1/responses/input_tokens", GENERATE]
    printed = json.dumps(response)
    assert "sk-CANARY-123" not in printed and "excerpts" not in printed and "Unexpected error" not in printed


@pytest.mark.parametrize("url", ["https://user:sk-SECRET@gw.example/v1", "https://gw.example/v1?api-key=sk-SECRET", "http://remote.example/v1"])
def test_ai_configure_refuses_an_unusable_base_url_before_saving_anything(tmp_path, capsys, url):
    from conftest import invoke

    assert invoke(tmp_path, capsys, "init", "demo")[0] == 0
    before = (tmp_path / "repodocs.json").read_bytes()
    for provider in ("openai", "ollama"):
        code, response = invoke(tmp_path, capsys, "ai-configure", "--provider", provider, "--model", "m", "--openai-base-url", url)
        assert code != 0 and response["ok"] is False
        assert "sk-SECRET" not in json.dumps(response) and "remote.example" not in json.dumps(response)
    assert (tmp_path / "repodocs.json").read_bytes() == before


def test_more_reasoning_than_output_is_contradictory_and_not_usage():
    contradictory = copy.deepcopy(RESPONSE)
    contradictory["usage"]["output_tokens"], contradictory["usage"]["output_tokens_details"]["reasoning_tokens"] = 10, 900
    assert openai_usage(contradictory) is None
    equal = copy.deepcopy(RESPONSE)
    equal["usage"]["output_tokens_details"]["reasoning_tokens"] = 70
    assert openai_usage(equal).reasoning_tokens == 70, "all output may be reasoning"


@pytest.mark.parametrize("extra", [
    {"type": "message", "status": "completed", "role": "assistant", "content": []},
    {"type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": '{"components": []}'}]},
])
def test_exactly_one_assistant_message_is_accepted(extra):
    payload = copy.deepcopy(RESPONSE)
    payload["output"].insert(0, extra)
    with endpoint(payload) as (url, _), pytest.raises(ProviderError, match="exactly one"):
        OpenAIProvider("requested-model", url, api_key="dummy").generate("s", "u", SCHEMA)


def test_a_count_refused_for_a_missing_credential_is_not_recorded_as_sent(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ledger = Ledger(RunLimits(context_window_tokens=100_000).resolved("openai"))
    with endpoint() as (url, calls), pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        BudgetedProvider(OpenAIProvider("requested-model", url), ledger).generate("s", "u", SCHEMA)
    assert calls == [] and ledger.snapshot()["totals"]["count_requests"] == 0
