"""The external data boundary: what is redacted, what needs consent, and where a request can go."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from repodocs.ai.bundle import redact_text
from repodocs.ai.providers import AnthropicProvider, OllamaProvider, is_loopback
from repodocs.errors import ProviderError

from conftest import invoke


# ------------------------------------------------------------------ redaction
def shaped(*parts: str) -> str:
    """A token-shaped sample built at run time, so no complete token appears in the source."""

    return "".join(parts)


SHAPES = [
    (shaped("token xox", "b-123456789012-abcdefghijklmnop"), "abcdefghijklmnop"),
    (shaped("key AI", "zaSyA1234567890abcdefghijklmnopqrstuv"), shaped("AI", "zaSyA1234567890abcdefghijklmnopqrstuv")),
    (shaped("sk_", "live_abcdefghijklmnop1234"), shaped("sk_", "live_abcdefghijklmnop1234")),
    (shaped("rk_", "test_abcdefghijklmnop1234"), shaped("rk_", "test_abcdefghijklmnop1234")),
    (shaped("glp", "at-abcdefghijklmnopqrstuvwx"), shaped("glp", "at-abcdefghijklmnopqrstuvwx")),
    (shaped("np", "m_abcdefghijklmnopqrstuvwxyz0123456789"), shaped("np", "m_abcdefghijklmnopqrstuvwxyz0123456789")),
    (shaped("github_", "pat_", "A1b2C3d4E5" * 5), "A1b2C3d4E5A1b2C3d4E5"),
    ('client_secret = "s3cr3t-value"', "s3cr3t-value"),
    ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ("x-api-key: abc123def456", "abc123def456"),
    ("aws.secret.access_key: wJalrXUtnFEMI", "wJalrXUtnFEMI"),
    ("spring.datasource.password=letmein99", "letmein99"),
]


@pytest.mark.parametrize("text, secret", SHAPES, ids=[str(index) for index in range(len(SHAPES))])
def test_more_secret_shapes_are_redacted(text: str, secret: str) -> None:
    redacted, count = redact_text(text)

    assert secret not in redacted and count >= 1 and "[REDACTED:" in redacted


@pytest.mark.parametrize("text", [
    "tokenizer = WhitespaceTokenizer",
    "secretary: Alice",
    "token_count: 5",
    "GET /api/passwords/{id} is handled by PasswordController",
    "skill_level = 3",
    "the key is in the config",
])
def test_ordinary_text_that_resembles_a_secret_name_is_left_alone(text: str) -> None:
    assert redact_text(text) == (text, 0)


def test_redaction_is_linear_on_a_long_unbroken_word() -> None:
    word = "a" * 2_000_000
    started = time.monotonic()

    assert redact_text(word) == (word, 0)
    assert redact_text("x " * 50_000 + "password=" + "y" * 100_000)[1] == 1
    # A quadratic search over two megabytes would take hours; the bound only has to be far below that.
    assert time.monotonic() - started < 60


def test_redaction_is_idempotent() -> None:
    once, _ = redact_text("password=abc123 and ghp_abcdefghijklmnopqrstuvwxyz0123456789")

    assert redact_text(once)[0].replace("[REDACTED:assigned-secret]", "") == once.replace("[REDACTED:assigned-secret]", "")


def spring_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    source = repository / "src" / "main" / "java" / "A.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package a;\nimport org.springframework.web.bind.annotation.*;\n"
        "// SOURCE_TEXT_MARKER an ordinary comment that is not a fact\n"
        '@RestController\n@RequestMapping("/AKIAIOSFODNN7EXAMPLE")\npublic class A {\n'
        ' @GetMapping("/ok")\n public String get() { return ""; }\n}\n',
        encoding="utf-8",
    )
    (repository / "pom.xml").write_text(
        "<project><dependencies><dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId></dependency></dependencies></project>",
        encoding="utf-8",
    )
    return repository


def scanned_project(tmp_path: Path, capsys) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(spring_repository(tmp_path)), "--id", "repo", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    return root


def test_the_bundle_holds_no_secret_and_no_source_text(tmp_path: Path, capsys) -> None:
    root = scanned_project(tmp_path, capsys)

    code, payload = invoke(root, capsys, "ai-bundle")
    bundle = (root / ".repodocs" / "evidence-bundle.json").read_text(encoding="utf-8")

    assert code == 0 and payload["data"]["redactions"] >= 1
    assert "AKIAIOSFODNN7EXAMPLE" not in bundle and "[REDACTED:aws-access-key]" in bundle
    assert "SOURCE_TEXT_MARKER" not in bundle
    assert str(tmp_path) not in bundle and "\\\\" not in json.loads(bundle)["project"].get("name", "")


# ------------------------------------------------------------------ consent
def test_no_consent_means_no_provider_even_when_a_key_is_present(tmp_path: Path, capsys, monkeypatch) -> None:
    root = scanned_project(tmp_path, capsys)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abcdefghijklmnopqrstuvwx")
    monkeypatch.setattr(AnthropicProvider, "generate", lambda *args: pytest.fail("sent without consent"))
    assert invoke(root, capsys, "ai-configure", "--provider", "anthropic")[0] == 0

    code, payload = invoke(root, capsys, "ai-interpret")

    assert code == 2 and "no consent is recorded" in payload["errors"][0]
    assert "ai-configure --consent-external" in payload["errors"][0], "the error must say how to proceed"
    assert not (root / ".repodocs" / "ai-claims.json").exists()


def test_revoking_consent_stops_the_provider_again(tmp_path: Path, capsys, monkeypatch) -> None:
    root = scanned_project(tmp_path, capsys)
    monkeypatch.setattr(AnthropicProvider, "generate", lambda *args: pytest.fail("sent after consent was revoked"))
    assert invoke(root, capsys, "ai-configure", "--provider", "anthropic", "--consent-external")[0] == 0
    assert invoke(root, capsys, "ai-configure", "--revoke-consent")[0] == 0

    code, payload = invoke(root, capsys, "ai-interpret")

    assert code == 2 and "no consent is recorded" in payload["errors"][0]


@pytest.mark.parametrize("host", [
    "http://127.0.0.1.evil.example",
    "http://localhost.evil.example",
    "http://127.0.0.1@evil.example",
    "http://evil.example#@127.0.0.1",
    "http://evil.example/@127.0.0.1",
    "http://localhost.:11434",
    "http://0.0.0.0:11434",
    "http://2130706433",
    "http://192.168.1.10:11434",
    "http://",
    "localhost:11434",
    "",
])
def test_only_a_real_loopback_host_counts_as_local(host: str) -> None:
    assert not is_loopback(host)


@pytest.mark.parametrize("host", ["http://LOCALHOST:11434", "http://127.0.0.1:1", "http://127.1.2.3", "http://[::1]:11434"])
def test_loopback_hosts_are_local(host: str) -> None:
    assert is_loopback(host)


# ------------------------------------------------------------------ where a request can go
class Recorder(BaseHTTPRequestHandler):
    """Records every request made to its server; the server decides the reply."""

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append((self.command, self.path, self.rfile.read(length)))
        self.server.respond(self)

    do_GET = do_POST = _record  # noqa: N815

    def log_message(self, *args) -> None:
        pass


def start(respond) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), Recorder)
    server.requests, server.respond = [], respond
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(*servers: HTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


def ollama_reply(handler: BaseHTTPRequestHandler) -> None:
    body = json.dumps({"model": "m", "message": {"content": '{"claims": []}'}}).encode()
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_a_loopback_request_never_goes_through_a_proxy(monkeypatch) -> None:
    proxy = start(ollama_reply)
    ollama = start(ollama_reply)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    try:
        OllamaProvider("m", f"http://127.0.0.1:{ollama.server_port}").generate("s", "bundle text", {})
    finally:
        stop(proxy, ollama)

    assert len(ollama.requests) == 1
    assert proxy.requests == [], "the bundle was sent to the proxy"


def test_a_redirect_is_not_followed(monkeypatch) -> None:
    elsewhere = start(ollama_reply)

    def redirect(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(307)
        handler.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}/api/chat")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    first = start(redirect)
    try:
        with pytest.raises(ProviderError, match="redirect.*not followed"):
            OllamaProvider("m", f"http://127.0.0.1:{first.server_port}").generate("s", "bundle text", {})
    finally:
        stop(first, elsewhere)

    assert len(first.requests) == 1
    assert elsewhere.requests == [], "the redirect target received a request"
