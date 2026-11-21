from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from repodocs.ai.openai import OpenAIProvider
from repodocs.errors import ConfigError, ProviderError


SCHEMA = {"type": "object", "properties": {"components": {"type": "array", "items": {"type": "string"}}}, "required": ["components"], "additionalProperties": False}
RESPONSE = {
    "status": "completed", "model": "requested-model", "error": None, "incomplete_details": None,
    "output": [{"type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": '{"components": ["API"]}'}]}],
    "usage": {"input_tokens": 190, "output_tokens": 70, "input_tokens_details": {"cached_tokens": 8}, "output_tokens_details": {"reasoning_tokens": 30}},
}


@contextmanager
def endpoint(payload=RESPONSE, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))), "auth": self.headers.get("Authorization")})
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "/redirected")
            self.end_headers()
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
    from conftest import invoke
    from repodocs.config import ProjectConfig
    from repodocs.architecture.discovery import load_view, STATUS_CURRENT
    from conftest import read_ir

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
        code, response = invoke(root, capsys, "architecture", "discover")
        assert code == 0, response
        assert len(calls) == 1
        status, view, _ = load_view(ProjectConfig.load(root), read_ir(root))
        assert status == STATUS_CURRENT and view["provenance"]["provider"] == "openai"
        assert invoke(root, capsys, "architecture", "discover")[0] == 0
        assert len(calls) == 1, "stored views are reused without another provider request"
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
    monkeypatch.setenv("OPENAI_API_KEY", "canary-credential")
    with endpoint({"message": "canary-credential"}, status) as (url, _):
        assert invoke(root, capsys, "ai-configure", "--provider", "openai", "--model", "requested-model", "--openai-base-url", url)[0] == 0
        code, response = invoke(root, capsys, "architecture", "discover")
    assert code == 7 and response["ok"] is False
    assert "canary-credential" not in json.dumps(response)
