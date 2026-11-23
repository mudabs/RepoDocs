"""Ollama-compatible /v1 transport: completion outcomes, bounded responses, security, and arbitrary schemas.

Everything here talks to a local stand-in server. No credential is real, no private endpoint is contacted, and
nothing here shows that any live model or gateway works; transport success is not model quality.
"""

import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from repodocs.ai import providers
from repodocs.ai.providers import MAX_RESPONSE_BYTES, OllamaProvider, ProviderResponse, resolve_provider
from repodocs.config import AiSettings
from repodocs.errors import ConfigError, ConsentError, ProviderError

SECRET = "never-print-this-secret"
GOOD = {"model": "qwen2.5-coder:3b", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"claims":[]}'}}]}


def serve(reply, status=200, *, raw=None, declare=True, headers=None, delay=0.0):
    """A local server. ``raw`` sends exact bytes; ``declare=False`` omits Content-Length and closes to end the body."""

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            if delay:
                time.sleep(delay)
            if raw is not None:
                payload = raw
            else:
                payload = json.dumps(reply(received[-1][2]) if callable(reply) else reply).encode()
            try:
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                if declare:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


def stop(server):
    server.shutdown()
    server.server_close()


def provider(server, path="/v1"):
    return OllamaProvider("qwen2.5-coder:3b", f"http://127.0.0.1:{server.server_port}{path}")


def run(reply=None, **options):
    server, received = serve(reply if reply is not None else GOOD, **options)
    try:
        return provider(server).generate("instructions", "bundle", {"type": "object"}), received
    finally:
        stop(server)


def fails(match, reply=None, **options):
    server, received = serve(reply if reply is not None else GOOD, **options)
    try:
        with pytest.raises(ProviderError, match=match) as caught:
            provider(server).generate("instructions", "bundle", {"type": "object"})
    finally:
        stop(server)
    return caught.value, received


# ------------------------------------------------------------------ the request
def test_authentication_schema_token_bound_and_result(monkeypatch):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", "test-secret")
    schema = {"type": "object", "properties": {"claims": {"type": "array"}}}
    server, received = serve(GOOD)
    try:
        ollama = OllamaProvider("qwen2.5-coder:3b", f"http://127.0.0.1:{server.server_port}/v1", max_output_tokens=321)
        result = ollama.generate("instructions", "redacted bundle", schema)
    finally:
        stop(server)

    assert result == ProviderResponse({"claims": []}, "qwen2.5-coder:3b")
    path, authorization, body = received[0]
    assert path == "/v1/chat/completions" and authorization == "Bearer test-secret"
    assert body["messages"] == [{"role": "system", "content": "instructions"}, {"role": "user", "content": "redacted bundle"}]
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["stream"] is False and body["max_tokens"] == 321
    assert body == ollama.request_body("instructions", "redacted bundle", schema)


def test_the_adapter_carries_any_caller_schema_not_only_claims(monkeypatch):
    """Architecture discovery sends its own schema; nothing in the request may assume claims."""

    discovery_like = {
        "type": "object",
        "properties": {"components": {"type": "array"}, "connections": {"type": "array"}, "requests": {"type": "array"}, "complete": {"type": "boolean"}},
        "required": ["components", "connections", "requests", "complete"],
        "additionalProperties": False,
    }
    reply = {"model": "m", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"components":[],"connections":[],"requests":[],"complete":true}'}}]}
    server, received = serve(reply)
    try:
        result = provider(server).generate("s", "u", discovery_like)
    finally:
        stop(server)

    assert result.data == {"components": [], "connections": [], "requests": [], "complete": True}
    assert received[0][2]["response_format"]["json_schema"]["schema"] == discovery_like
    assert "claims" not in json.dumps(received[0][2]["response_format"]["json_schema"]["name"])


def test_the_real_architecture_discovery_schema_is_accepted_when_present():
    discovery = pytest.importorskip("repodocs.architecture.discovery")
    server, received = serve(GOOD)
    try:
        provider(server).generate("s", "u", discovery.RESPONSE_SCHEMA)
    finally:
        stop(server)
    assert received[0][2]["response_format"]["json_schema"]["schema"] == discovery.RESPONSE_SCHEMA


def test_external_gateway_needs_consent_and_environment_key(monkeypatch):
    settings = AiSettings(provider="ollama", model="qwen2.5-coder:3b", ollama_host="https://gateway.example.net:8443/v1")
    with pytest.raises(ConsentError):
        resolve_provider(settings)
    monkeypatch.delenv("REPODOCS_OLLAMA_API_KEY", raising=False)
    allowed = AiSettings(provider="ollama", model=settings.model, ollama_host=settings.ollama_host, external_consent=True)
    with pytest.raises(ConfigError, match="REPODOCS_OLLAMA_API_KEY"):
        resolve_provider(allowed).generate("s", "bundle", {})


def test_a_key_with_surrounding_whitespace_is_trimmed_not_blamed_on_the_server(monkeypatch):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", "test-secret\r\n")
    result, received = run()

    assert result.data == {"claims": []} and received[0][1] == "Bearer test-secret"


@pytest.mark.parametrize("key", ["bad\nkey", "kéy", "  \n"])
def test_a_key_that_cannot_be_a_header_is_a_configuration_error_that_does_not_echo_it(monkeypatch, key):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", key)
    settings = AiSettings(provider="ollama", model="m", ollama_host="https://gateway.example.net/v1", external_consent=True)
    with pytest.raises(ConfigError, match="REPODOCS_OLLAMA_API_KEY") as caught:
        resolve_provider(settings).generate("s", "bundle", {})

    assert key.strip() not in str(caught.value) or not key.strip()


@pytest.mark.parametrize("host", [
    "http://remote.example:8443/v1",
    "https://user:pass@remote.example/v1",
    "https://remote.example/v1?token=abc",
    "https://remote.example/v1#fragment",
    "https://remote.example/custom",
])
def test_invalid_gateway_urls_are_rejected(host):
    with pytest.raises(ConfigError):
        OllamaProvider("m", host)


# ------------------------------------------------------------------ completion outcomes
def completion(finish, **message):
    return {"model": "m", "choices": [{"finish_reason": finish, "message": {"role": "assistant", **message}}]}


@pytest.mark.parametrize("reply, phrase", [
    (completion("length", content='{"claims":[]}'), "cut off"),
    (completion("content_filter", content='{"claims":[]}'), "content filter"),
    (completion("content_filter", content=None), "content filter"),
    (completion("stop", content=None, refusal="I cannot help with that."), "declined"),
    (completion("stop", content='{"claims":[]}', refusal="no"), "declined"),
    (completion("tool_calls", content=None, tool_calls=[{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]), "tool call"),
    (completion("stop", content='{"claims":[]}', tool_calls=[{"id": "1"}]), "tool call"),
    (completion("function_call", content=None), "tool call"),
    (completion(None, content='{"claims":[]}'), "unsupported state"),
    (completion("eos", content='{"claims":[]}'), "unsupported state"),
    ({"model": "m", "choices": [{"message": {"content": '{"claims":[]}'}}]}, "unsupported state"),
    (completion("stop", content=""), "no message content"),
    (completion("stop", content="   "), "no message content"),
    (completion("stop", content=None), "no message content"),
    (completion("stop", content=["not", "text"]), "no message content"),
    ({"model": "m", "choices": []}, "no chat completion"),
    ({"model": "m", "choices": ["x"]}, "no chat completion"),
    ({"choices": GOOD["choices"] * 2}, "no chat completion"),
    (completion("stop", role="tool", content="{}"), "no assistant message"),
    ({"model": "m"}, "no chat completion"),
    ([1, 2], "malformed"),
    (completion("stop", content="not-json"), "not valid JSON"),
    (completion("stop", content="[1]"), "not an object"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_anything_but_one_finished_text_completion_is_refused(reply, phrase):
    fails(phrase, reply)


def test_the_one_supported_success_is_a_stop_with_json_object_text():
    result, _ = run(completion("stop", content='{"claims":[]}'))

    assert result.data == {"claims": []} and result.model == "m"


def test_a_missing_served_model_falls_back_to_the_requested_one():
    result, _ = run({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]})

    assert result.model == "qwen2.5-coder:3b"


# ------------------------------------------------------------------ bounded responses
def test_a_declared_oversized_response_is_refused_before_it_is_read():
    server, _ = serve(None, raw=b"x" * (MAX_RESPONSE_BYTES + 1), declare=True)
    try:
        with pytest.raises(ProviderError, match="byte limit"):
            provider(server).generate("s", "u", {})
    finally:
        stop(server)


def test_an_undeclared_oversized_response_is_cut_off_while_reading(monkeypatch):
    reads = []
    real = providers._read_bounded

    def spying(response, limit):
        original = response.read

        def read(amount=-1):
            data = original(amount)
            reads.append((amount, len(data)))
            return data

        response.read = read
        return real(response, limit)

    monkeypatch.setattr(providers, "_read_bounded", spying)
    body = b'{"choices":[{"finish_reason":"stop","message":{"content":"' + b"a" * (MAX_RESPONSE_BYTES * 2) + b'"}}]}'
    server, _ = serve(None, raw=body, declare=False)
    try:
        with pytest.raises(ProviderError, match="byte limit"):
            provider(server).generate("s", "u", {})
    finally:
        stop(server)
    assert reads and all(amount == MAX_RESPONSE_BYTES + 1 for amount, _ in reads), "a bounded read, never read-everything"
    assert sum(size for _, size in reads) <= MAX_RESPONSE_BYTES + 1, "no more than the limit plus one byte was buffered"


def test_an_understated_content_length_does_not_get_past_the_limit():
    body = b"y" * (MAX_RESPONSE_BYTES + 10)
    server, _ = serve(None, raw=body, declare=False, headers={"Content-Length": "10"})
    try:
        with pytest.raises(ProviderError):
            provider(server).generate("s", "u", {})
    finally:
        stop(server)


def test_a_response_exactly_at_the_limit_is_read_and_then_judged_by_its_content():
    padded = json.dumps(completion("stop", content="{}")).encode()
    padded += b" " * (MAX_RESPONSE_BYTES - len(padded))
    assert len(padded) == MAX_RESPONSE_BYTES

    result, _ = run(None, raw=padded)

    assert result.data == {}


def test_native_ollama_responses_are_bounded_too():
    server, _ = serve(None, raw=b"z" * (MAX_RESPONSE_BYTES + 1))
    try:
        with pytest.raises(ProviderError, match="byte limit"):
            provider(server, path="").generate("s", "u", {})
    finally:
        stop(server)


@pytest.mark.parametrize("raw, phrase", [
    (b"{not json", "not valid JSON"),
    (b"", "not valid JSON"),
    (b"\xff\xfe", "not valid JSON"),
    (b"[]", "malformed"),
])
def test_malformed_bodies_are_provider_errors(raw, phrase):
    fails(phrase, None, raw=raw)


def test_a_slow_gateway_times_out_with_a_provider_error(monkeypatch):
    monkeypatch.setattr(providers, "REQUEST_TIMEOUT_SECONDS", 0.3)
    server, _ = serve(GOOD, delay=1.5)
    try:
        with pytest.raises(ProviderError, match="did not answer within"):
            provider(server).generate("s", "u", {})
    finally:
        stop(server)


def test_a_refused_connection_is_a_provider_error():
    server, _ = serve(GOOD)
    port = server.server_port
    stop(server)

    with pytest.raises(ProviderError, match="Unable to reach Ollama"):
        OllamaProvider("m", f"http://127.0.0.1:{port}/v1").generate("s", "u", {})


# ------------------------------------------------------------------ security
@pytest.mark.parametrize("status, phrase", [
    (401, "authentication failed"),
    (403, "authentication failed"),
    (429, "rate-limiting"),
    (500, "Ollama error"),
    (503, "Ollama error"),
])
def test_http_failures_are_provider_errors_that_never_print_the_credential(monkeypatch, status, phrase):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", SECRET)
    server, _ = serve({"error": f"echo {SECRET}"}, status=status)
    try:
        with pytest.raises(ProviderError, match=phrase) as caught:
            provider(server).generate("s", "u", {})
    finally:
        stop(server)
    text = str(caught.value)
    assert SECRET not in text and "Bearer" not in text and "127.0.0.1" not in text


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_not_followed_and_the_credential_goes_nowhere_else(monkeypatch, status):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", SECRET)
    target, target_received = serve(GOOD)
    server, received = serve({}, status=status, headers={"Location": f"http://127.0.0.1:{target.server_port}/v1/chat/completions"})
    try:
        with pytest.raises(ProviderError, match="redirect") as caught:
            provider(server).generate("s", "u", {})
    finally:
        stop(server)
        stop(target)
    assert target_received == [] and len(received) == 1
    assert SECRET not in str(caught.value)


def test_a_loopback_request_never_uses_an_environment_proxy(monkeypatch):
    proxy, proxy_received = serve(GOOD)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
    server, received = serve(GOOD)
    try:
        provider(server).generate("s", "u", {})
    finally:
        stop(server)
        stop(proxy)
    assert len(received) == 1 and proxy_received == []


def test_a_key_in_the_environment_is_never_sent_to_native_endpoints(monkeypatch):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", SECRET)
    server, received = serve({"model": "m", "done": True, "done_reason": "stop", "message": {"content": "{}"}})
    try:
        provider(server, path="").generate("s", "u", {})
    finally:
        stop(server)
    assert received[0][1] is None and received[0][0] == "/api/chat"


def test_the_credential_never_reaches_a_persisted_artifact_or_the_cli_output(tmp_path, capsys, monkeypatch):
    """A failing and a succeeding gateway run through the real CLI, with the key only in the environment."""

    from conftest import invoke
    from test_ai import scanned

    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", SECRET)
    root = scanned(tmp_path, capsys)
    for status in (401, 500):
        server, _ = serve({"error": SECRET}, status=status)
        try:
            host = f"http://127.0.0.1:{server.server_port}/v1"
            assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "m", "--ollama-host", host)[0] == 0
            code, payload = invoke(root, capsys, "ai-interpret")
        finally:
            stop(server)
        assert code == 7 and payload["ok"] is False and SECRET not in json.dumps(payload)
        assert invoke(root, capsys, "generate")[0] == 0, "the deterministic documents are unaffected by the failure"
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert SECRET.encode() not in path.read_bytes(), path
    assert SECRET not in capsys.readouterr().err


@pytest.mark.parametrize("transport", ["", "/v1"], ids=["native", "v1"])
@pytest.mark.parametrize("scenario", ["repair", "budget", "truncated", "unknown", "full-context", "admission"])
def test_shared_discovery_transport(tmp_path, capsys, monkeypatch, transport, scenario):
    from repodocs import service
    from repodocs.ai.budgets import RunLimits, estimate_tokens, serialized_request
    from repodocs.architecture.discovery import RESPONSE_SCHEMA
    from test_ai_budgets import service_project
    from test_architecture_discovery import full_answer

    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", SECRET)
    root = service_project(tmp_path, capsys)
    replies = 0

    def reply(body):
        nonlocal replies
        replies += 1
        finish = "length" if scenario == "truncated" else "stop"
        if scenario in ("repair", "budget") and replies == 1:
            content = "malformed JSON"
        else:
            content = json.dumps(full_answer(json.loads(body["messages"][1]["content"])))
        prompt = 65529 if scenario == "full-context" else 23
        if transport:
            payload = completion(finish, content=content)
            if scenario != "unknown":
                payload["usage"] = {"prompt_tokens": prompt, "completion_tokens": 7}
            return payload
        payload = {"model": "m", "done": True, "done_reason": finish, "message": {"content": content}}
        if scenario != "unknown":
            payload.update(prompt_eval_count=prompt, eval_count=7)
        return payload

    server, received = serve(reply)
    try:
        ollama = OllamaProvider(
            "m", f"http://127.0.0.1:{server.server_port}{transport}",
            context_window=65536, max_output_tokens=321,
        )
        limits = RunLimits(max_requests=1) if scenario == "budget" else None
        if scenario == "admission":
            limits = RunLimits(context_window_tokens=512, request_output_tokens=321)
        result = service.execute("discover", service.DiscoverRequest(root, provider=ollama, limits=limits))
    finally:
        stop(server)

    assert result.status == ("complete" if scenario in ("repair", "unknown") else "failed"), result
    receipt = json.loads(Path(result.data["usage"]["receipt"]).read_text(encoding="utf-8"))
    requests = receipt["requests"]
    assert len(received) == len(requests) == {"admission": 0, "repair": 2}.get(scenario, 1)
    if scenario == "budget":
        assert result.data["usage"]["stop_reason"] == "request-limit"
    for (path, auth, body), entry in zip(received, requests, strict=True):
        messages = body["messages"]
        system, user = (message["content"] for message in messages)
        assert body == ollama.request_body(system, user, RESPONSE_SCHEMA)
        assert entry["estimate_tokens"] == estimate_tokens(serialized_request(ollama, system, user, RESPONSE_SCHEMA))
        assert entry["output_cap_tokens"] == 321
        if transport:
            assert path == "/v1/chat/completions" and auth == f"Bearer {SECRET}"
            assert body["max_tokens"] == 321
        else:
            assert path == "/api/chat" and auth is None
            assert body["options"] == {"num_ctx": 65536, "num_predict": 321}
        assert entry["usage_measured"] is (scenario != "unknown")
        if scenario != "unknown":
            assert entry["usage"]["input_tokens"] == (65529 if scenario == "full-context" else 23)
            assert entry["usage"]["output_tokens"] == 7
        else:
            assert entry["usage"] is None
    assert SECRET not in repr(result)
    assert SECRET not in json.dumps(receipt)
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert SECRET.encode() not in path.read_bytes(), path


def test_a_remote_gateway_needs_the_separate_source_excerpt_consent_for_discovery(monkeypatch):
    pytest.importorskip("repodocs.architecture.discovery")
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", SECRET)
    evidence_only = AiSettings(provider="ollama", model="m", ollama_host="https://gateway.example.net/v1", external_consent=True)

    with pytest.raises(ConsentError, match="source excerpts"):
        providers.resolve_discovery_provider(evidence_only)
    assert resolve_provider(evidence_only) is not None, "the evidence-bundle path is unaffected"
    loopback = AiSettings(provider="ollama", model="m", ollama_host="http://127.0.0.1:11434/v1")
    assert providers.resolve_discovery_provider(loopback) is not None
