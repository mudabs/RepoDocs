"""Authenticated OpenAI-compatible Ollama gateway contract tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from repodocs.ai.providers import OllamaProvider, ProviderResponse, resolve_provider
from repodocs.config import AiSettings
from repodocs.errors import ConfigError, ConsentError, ProviderError


def serve(reply, status=200):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers.get("Authorization"), json.loads(raw)))
            payload = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


def stop(server):
    server.shutdown()
    server.server_close()


def test_openai_gateway_authentication_schema_and_result(monkeypatch):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", "test-secret")
    server, received = serve({
        "model": "qwen2.5-coder:3b",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"claims":[]}'}}],
    })
    try:
        host = f"http://127.0.0.1:{server.server_port}/v1"
        p = OllamaProvider("qwen2.5-coder:3b", host)
        schema = {"type": "object", "properties": {"claims": {"type": "array"}}}
        result = p.generate("instructions", "redacted bundle", schema)
    finally:
        stop(server)
    assert result == ProviderResponse({"claims": []}, "qwen2.5-coder:3b")
    assert received[0][0] == "/v1/chat/completions"
    assert received[0][1] == "Bearer test-secret"
    body = received[0][2]
    assert body["messages"] == [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "redacted bundle"},
    ]
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["stream"] is False


def test_external_gateway_needs_consent_and_environment_key(monkeypatch):
    settings = AiSettings(provider="ollama", model="qwen2.5-coder:3b",
                          ollama_host="https://vps01.example.ts.net:8443/v1")
    with pytest.raises(ConsentError):
        resolve_provider(settings)
    monkeypatch.delenv("REPODOCS_OLLAMA_API_KEY", raising=False)
    allowed = AiSettings(provider="ollama", model=settings.model,
                         ollama_host=settings.ollama_host, external_consent=True)
    with pytest.raises(ConfigError, match="REPODOCS_OLLAMA_API_KEY"):
        resolve_provider(allowed).generate("s", "bundle", {})


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


@pytest.mark.parametrize("reply, phrase", [
    ({"choices": []}, "no chat completion"),
    ({"choices": [{"message": {"content": "not-json"}}]}, "not valid JSON"),
    ({"choices": [{"finish_reason": "length", "message": {"content": '{"claims":[]}'}}]}, "cut off"),
    ({"choices": [{"message": {"content": None}}]}, "no message content"),
])
def test_bad_gateway_responses_fail_closed(reply, phrase):
    server, _ = serve(reply)
    try:
        with pytest.raises(ProviderError, match=phrase):
            OllamaProvider("m", f"http://127.0.0.1:{server.server_port}/v1").generate("s", "u", {})
    finally:
        stop(server)


def test_gateway_auth_error_does_not_print_secret(monkeypatch):
    monkeypatch.setenv("REPODOCS_OLLAMA_API_KEY", "never-print-this-secret")
    server, _ = serve({"error": "unauthorized"}, status=401)
    try:
        with pytest.raises(ProviderError) as caught:
            OllamaProvider("m", f"http://127.0.0.1:{server.server_port}/v1").generate("s", "u", {})
    finally:
        stop(server)
    assert "authentication failed" in str(caught.value)
    assert "never-print-this-secret" not in str(caught.value)
