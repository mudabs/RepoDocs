"""Evidence bundles, redaction, consent, provider adapters, and claim validation."""

from __future__ import annotations

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from conftest import invoke, read_ir
from repodocs.ai import providers
from repodocs.ai.bundle import build_bundle, redact_text
from repodocs.ai.claims import OUTPUT_SCHEMA, SYSTEM_PROMPT, validate_claims
from repodocs.ai.providers import AnthropicProvider, OllamaProvider, ProviderResponse, is_loopback
from repodocs.config import AiSettings
from repodocs.errors import ConfigError, ConsentError, ProviderError
from test_correlation import project
from test_schemas import assert_valid


def scanned(tmp_path, capsys) -> Path:
    root = project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    return root


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FakeProvider:
    name = "ollama"
    model = "fake-model"
    external = False

    def __init__(self, data: dict) -> None:
        self.data = data
        self.calls: list[tuple[str, str, dict]] = []

    def generate(self, system: str, user: str, schema: dict) -> ProviderResponse:
        self.calls.append((system, user, schema))
        return ProviderResponse(self.data, "fake-model:served")


# ---------------------------------------------------------------- bundle and redaction
@pytest.mark.parametrize("text, secret", [
    ("key AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 end", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("use sk-ant-api03-abcdefghijklmnopqrstuvwx", "sk-ant-api03-abcdefghijklmnopqrstuvwx"),
    ("Authorization: Bearer abcdefghijklmnop0123456789", "abcdefghijklmnop0123456789"),
    ("jdbc at postgres://admin:hunter2pass@db.internal/shop", "hunter2pass"),
    ('password = "s3cr3t-value"', "s3cr3t-value"),
    ("API_KEY: live_value_123", "live_value_123"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpM", "eyJzdWIiOiIxMjM0NTY3ODkwIn0"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
])
def test_redaction_removes_secret_shapes(text, secret) -> None:
    redacted, count = redact_text(text)
    assert secret not in redacted
    assert count >= 1
    assert "[REDACTED:" in redacted


def test_redaction_leaves_ordinary_text_alone() -> None:
    text = "GET /api/orders/{orderId} is declared by OrderController"
    assert redact_text(text) == (text, 0)


def test_bundle_is_curated_redacted_versioned_and_deterministic(tmp_path, capsys) -> None:
    root = scanned(tmp_path, capsys)
    ir = read_ir(root)
    ir["nodes"][0]["name"] = "leak password=hunter2 here"
    bundle = build_bundle(ir)

    assert_valid("evidence-bundle-1.0.json", bundle)
    assert bundle == build_bundle(ir)
    assert bundle["bundle_version"] == "1.0"
    assert bundle["redaction"]["replacements"] >= 1
    serialized = json.dumps(bundle)
    assert "hunter2" not in serialized
    # Curated: no source listings, content hashes, or local filesystem paths.
    assert "content_hash" not in serialized and "files" not in bundle["snapshots"][0]
    assert str(root.as_posix()) not in serialized
    for node in bundle["nodes"]:
        assert "path" not in node["attributes"]
        if node["type"] in {"project", "repository", "language"}:
            assert node["evidence_ids"] == []
    # Every cited evidence ID resolves inside the bundle, and nothing unreferenced is shipped.
    known = {item["id"] for item in bundle["evidence"]}
    cited = {
        key
        for group in ("nodes", "relationships", "candidates", "limitations")
        for item in bundle[group]
        for key in item["evidence_ids"]
    }
    assert cited == known
    assert bundle["candidates"] and all(item["status"] == "candidate" for item in bundle["candidates"])


def test_ai_bundle_command_writes_bundle_without_any_provider(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    monkeypatch.setattr(providers, "resolve_provider", lambda settings: pytest.fail("ai-bundle must not resolve a provider"))

    code, response = invoke(root, capsys, "ai-bundle")
    assert code == 0, response
    assert_valid("cli-response-1.0.json", response)
    bundle = read_json(root / ".repodocs" / "evidence-bundle.json")
    assert_valid("evidence-bundle-1.0.json", bundle)
    assert response["data"]["evidence"] == len(bundle["evidence"])
    assert response["data"]["source_ir_hash"] == bundle["source_ir_hash"]


# ---------------------------------------------------------------- claim validation
def bundle_for(tmp_path, capsys) -> dict:
    return build_bundle(read_ir(scanned(tmp_path, capsys)))


def test_claims_are_accepted_only_with_known_evidence_and_subjects(tmp_path, capsys) -> None:
    bundle = bundle_for(tmp_path, capsys)
    endpoint = next(node for node in bundle["nodes"] if node["type"] == "endpoint")
    candidate = bundle["candidates"][0]
    raw = {"claims": [
        {"text": "The backend declares an orders endpoint.", "classification": "inferred",
         "subject_ids": [endpoint["id"]], "evidence_ids": endpoint["evidence_ids"]},
        {"text": "The frontend calls the backend orders API.", "classification": "inferred",
         "subject_ids": [candidate["id"]], "evidence_ids": candidate["evidence_ids"]},
        {"text": "Consider adding an API gateway.", "classification": "inferred", "subject_ids": [], "evidence_ids": []},
        {"text": "Invented evidence.", "classification": "inferred", "subject_ids": [], "evidence_ids": ["evidence:made-up"]},
        {"text": "Invented subject.", "classification": "inferred", "subject_ids": ["node:made-up"],
         "evidence_ids": endpoint["evidence_ids"]},
        {"text": "Claims to be observed.", "classification": "observed", "subject_ids": [], "evidence_ids": endpoint["evidence_ids"]},
        {"text": "", "classification": "inferred", "subject_ids": [], "evidence_ids": []},
        "not an object",
    ]}

    accepted, rejected = validate_claims(raw, bundle)

    by_text = {item["text"]: item for item in accepted}
    assert by_text["The backend declares an orders endpoint."]["classification"] == "inferred"
    # An unconfirmed candidate can only ever support a proposal, whatever the model says.
    assert by_text["The frontend calls the backend orders API."]["classification"] == "proposed"
    # No evidence means proposed, never inferred.
    assert by_text["Consider adding an API gateway."]["classification"] == "proposed"
    assert len(accepted) == 3
    assert all(item["classification"] in {"inferred", "proposed"} for item in accepted)
    assert sorted(item["reason"] for item in rejected) == [
        "malformed-claim", "malformed-claim", "malformed-claim", "unknown-evidence", "unknown-subject",
    ]


def test_duplicate_claims_never_lose_uncertainty_whatever_the_order(tmp_path, capsys) -> None:
    bundle = bundle_for(tmp_path, capsys)
    candidate = bundle["candidates"][0]
    request = next(node for node in bundle["nodes"] if node["id"] == candidate["source"])
    text = "The frontend calls the backend orders API."
    # Same sentence and evidence: once about the unconfirmed candidate, once about a plain node.
    about_candidate = {"text": text, "classification": "inferred",
                       "subject_ids": [candidate["id"]], "evidence_ids": candidate["evidence_ids"]}
    about_node = {"text": text, "classification": "inferred",
                  "subject_ids": [request["id"]], "evidence_ids": candidate["evidence_ids"]}

    results = []
    for order in ([about_candidate, about_node], [about_node, about_candidate]):
        accepted, rejected = validate_claims({"claims": order}, bundle)
        assert rejected == []
        results.append(accepted)
    assert results[0] == results[1]
    by_subject = {tuple(item["subject_ids"]): item["classification"] for item in results[0]}
    # A node-only description with multi-repository evidence cannot bypass the
    # unconfirmed candidate it declined to name.
    assert by_subject == {(candidate["id"],): "proposed", (request["id"],): "proposed"}
    assert len({item["id"] for item in results[0]}) == 2

    # An exact duplicate with conflicting labels keeps the cautious one in either order.
    proposed = dict(about_node, classification="proposed")
    for order in ([about_node, proposed], [proposed, about_node]):
        accepted, _ = validate_claims({"claims": order}, bundle)
        assert [item["classification"] for item in accepted] == ["proposed"]


@pytest.mark.parametrize("include_endpoints", [False, True])
def test_confirmed_candidate_can_support_an_inferred_claim(tmp_path, capsys, include_endpoints) -> None:
    root = scanned(tmp_path, capsys)
    first = read_ir(root)["candidates"][0]["id"]
    assert invoke(root, capsys, "confirm", first)[0] == 0
    bundle = build_bundle(read_ir(root))
    candidate = next(item for item in bundle["candidates"] if item["id"] == first)
    assert candidate["status"] == "confirmed"

    accepted, _ = validate_claims({"claims": [{
        "text": "The frontend calls the backend orders API.", "classification": "inferred",
        "subject_ids": [first, candidate["source"], candidate["target"]] if include_endpoints else [first],
        "evidence_ids": candidate["evidence_ids"],
    }]}, bundle)
    assert accepted[0]["classification"] == "inferred"


@pytest.mark.parametrize("missing_side", ["frontend", "backend"])
@pytest.mark.parametrize("also_name_endpoint", [False, True])
def test_confirmed_candidate_requires_both_evidence_sides(
    tmp_path, capsys, missing_side, also_name_endpoint
) -> None:
    root = scanned(tmp_path, capsys)
    candidate_id = read_ir(root)["candidates"][0]["id"]
    assert invoke(root, capsys, "confirm", candidate_id)[0] == 0
    bundle = build_bundle(read_ir(root))
    candidate = next(item for item in bundle["candidates"] if item["id"] == candidate_id)
    nodes = {node["id"]: node for node in bundle["nodes"]}
    evidence = {item["id"]: item for item in bundle["evidence"]}
    missing_node = nodes[candidate["source" if missing_side == "frontend" else "target"]]
    missing_repo = missing_node["attributes"]["repository_id"]
    remaining = [key for key in candidate["evidence_ids"] if evidence[key]["repository_id"] != missing_repo]
    assert remaining and len(remaining) < len(candidate["evidence_ids"])
    subjects = [candidate_id]
    if also_name_endpoint:
        subjects.append(candidate["target" if missing_side == "frontend" else "source"])
    accepted, rejected = validate_claims({"claims": [{
        "text": "The confirmed match links these repositories.", "classification": "inferred",
        "subject_ids": subjects, "evidence_ids": remaining,
    }]}, bundle)
    assert rejected == []
    assert accepted[0]["classification"] == "proposed"


@pytest.mark.parametrize("subject_kind", ["node", "relationship"])
@pytest.mark.parametrize("cite_third_repo", [False, True])
def test_confirmed_pair_does_not_authorize_unrelated_third_repository_subjects(
    tmp_path, capsys, subject_kind, cite_third_repo
) -> None:
    root = scanned(tmp_path, capsys)
    candidate_id = read_ir(root)["candidates"][0]["id"]
    assert invoke(root, capsys, "confirm", candidate_id)[0] == 0
    bundle = build_bundle(read_ir(root))
    candidate = next(item for item in bundle["candidates"] if item["id"] == candidate_id)
    nodes = {node["id"]: node for node in bundle["nodes"]}
    participants = {
        nodes[candidate["source"]]["attributes"]["repository_id"],
        nodes[candidate["target"]]["attributes"]["repository_id"],
    }
    third_node = next(node for node in bundle["nodes"]
                      if node["type"] == "service" and node["attributes"].get("repository_id")
                      and node["attributes"]["repository_id"] not in participants
                      and node["evidence_ids"])
    subject = third_node if subject_kind == "node" else next(
        edge for edge in bundle["relationships"]
        if edge["source"] == third_node["id"] and edge["evidence_ids"]
    )
    citations = set(candidate["evidence_ids"])
    if cite_third_repo:
        citations.update(subject["evidence_ids"])
    accepted, rejected = validate_claims({"claims": [{
        "text": "The confirmed pair also establishes this third service relationship.",
        "classification": "inferred", "subject_ids": [candidate_id, subject["id"]],
        "evidence_ids": sorted(citations),
    }]}, bundle)
    assert rejected == []
    assert accepted[0]["classification"] == "proposed"


def test_malformed_output_and_secret_text_are_handled(tmp_path, capsys) -> None:
    bundle = bundle_for(tmp_path, capsys)
    assert validate_claims("nonsense", bundle) == ([], [{"text": "", "reason": "malformed-output"}])
    accepted, _ = validate_claims({"claims": [{
        "text": "Uses password=hunter2 to connect.", "classification": "proposed", "subject_ids": [], "evidence_ids": [],
    }]}, bundle)
    assert "hunter2" not in accepted[0]["text"]


# ---------------------------------------------------------------- consent and local-only
def test_ai_is_disabled_by_default_and_deterministic_flow_needs_no_provider(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # importing the SDK would fail
    root = scanned(tmp_path, capsys)
    assert "ai" not in read_json(root / "repodocs.json")

    code, response = invoke(root, capsys, "ai-interpret")
    assert code == 0
    assert response["data"] == {"status": "disabled", "provider": "none"}
    assert not (root / ".repodocs" / "ai-claims.json").exists()
    assert invoke(root, capsys, "generate")[0] == 0
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0


def test_external_provider_requires_explicit_consent(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    monkeypatch.setattr(AnthropicProvider, "generate", lambda *args: pytest.fail("nothing may be sent without consent"))
    code, response = invoke(root, capsys, "ai-configure", "--provider", "anthropic")
    assert code == 0 and response["data"]["external_consent"] is False
    assert_valid("project-config-1.0.json", read_json(root / "repodocs.json"))

    code, response = invoke(root, capsys, "ai-interpret")
    assert code == 2
    assert "no consent is recorded" in response["errors"][0]
    assert not (root / ".repodocs" / "evidence-bundle.json").exists()
    assert not (root / ".repodocs" / "ai-claims.json").exists()


def test_local_only_blocks_a_remote_ollama_host_but_allows_loopback() -> None:
    assert is_loopback("http://localhost:11434") and is_loopback("http://127.0.0.1:11434") and is_loopback("http://[::1]:11434")
    assert not is_loopback("http://ollama.internal:11434") and not is_loopback("http://10.0.0.5:11434")

    with pytest.raises(ConsentError):
        providers.resolve_provider(AiSettings(provider="ollama", model="llama3", ollama_host="http://ollama.internal:11434"))
    remote = providers.resolve_provider(
        AiSettings(provider="ollama", model="llama3", ollama_host="http://ollama.internal:11434", external_consent=True)
    )
    assert remote.external is True
    local = providers.resolve_provider(AiSettings(provider="ollama", model="llama3"))
    assert local.external is False
    with pytest.raises(ConfigError, match="needs a model"):
        providers.resolve_provider(AiSettings(provider="ollama"))
    with pytest.raises(ConfigError, match="http"):
        providers.resolve_provider(AiSettings(provider="ollama", model="llama3", ollama_host="file:///etc/passwd"))


def test_consent_can_be_granted_and_revoked(tmp_path, capsys) -> None:
    root = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "ai-configure", "--provider", "anthropic", "--consent-external")[0] == 0
    assert read_json(root / "repodocs.json")["ai"] == {"provider": "anthropic", "external_consent": True}
    code, response = invoke(root, capsys, "ai-configure", "--revoke-consent")
    assert code == 0 and response["data"] == {"provider": "anthropic", "external_consent": False}
    code, response = invoke(root, capsys, "ai-configure", "--provider", "none")
    assert response["data"]["provider"] == "none"


def test_invalid_ai_settings_fail_on_load(tmp_path, capsys) -> None:
    root = scanned(tmp_path, capsys)
    path = root / "repodocs.json"
    config = read_json(path)
    for bad in ({"provider": "openai"}, {"provider": "ollama", "external_consent": "yes"}, {"provider": "ollama", "model": ""}, []):
        config["ai"] = bad
        path.write_text(json.dumps(config), encoding="utf-8")
        code, response = invoke(root, capsys, "scan")
        assert code == 2, bad


# ---------------------------------------------------------------- interpretation and provenance
def test_interpret_records_provenance_and_validated_claims(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "fake-model")[0] == 0
    ir = read_ir(root)
    endpoint = next(node for node in ir["nodes"] if node["type"] == "endpoint")
    fake = FakeProvider({"claims": [
        {"text": "The backend declares an orders endpoint.", "classification": "inferred",
         "subject_ids": [endpoint["id"]], "evidence_ids": endpoint["evidence_ids"]},
        {"text": "Invented.", "classification": "inferred", "subject_ids": [], "evidence_ids": ["evidence:nope"]},
    ]})
    monkeypatch.setattr(providers, "resolve_provider", lambda settings: fake)

    code, response = invoke(root, capsys, "ai-interpret")
    assert code == 0, response
    assert_valid("cli-response-1.0.json", response)
    assert response["data"]["accepted"] == 1 and response["data"]["rejected"] == 1

    document = read_json(root / ".repodocs" / "ai-claims.json")
    assert_valid("ai-claims-1.0.json", document)
    bundle = read_json(root / ".repodocs" / "evidence-bundle.json")
    provenance = document["provenance"]
    assert provenance["provider"] == "ollama"
    assert provenance["model"] == "fake-model:served" and provenance["requested_model"] == "fake-model"
    assert provenance["external"] is False
    assert provenance["source_ir_hash"] == bundle["source_ir_hash"]
    assert provenance["bundle_hash"] == response["data"]["bundle"]["bundle_hash"]
    assert set(document["accepted"][0]["evidence_ids"]) <= {item["id"] for item in ir["evidence"]}

    # The provider saw exactly the persisted bundle, the fixed instructions, and the schema.
    system, user, schema = fake.calls[0]
    assert system == SYSTEM_PROMPT and schema == OUTPUT_SCHEMA
    assert json.loads(user) == bundle
    # The IR and the deterministic documentation are untouched by interpretation.
    assert read_ir(root) == ir


def test_provider_failure_exits_7_and_leaves_deterministic_output_usable(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "generate")[0] == 0
    assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "missing", "--ollama-host", "http://127.0.0.1:9")[0] == 0

    code, response = invoke(root, capsys, "ai-interpret")
    assert code == 7
    assert_valid("cli-response-1.0.json", response)
    assert "Unable to reach Ollama" in response["errors"][0]
    assert "unaffected" in response["errors"][0]
    assert not (root / ".repodocs" / "ai-claims.json").exists()
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0


# ---------------------------------------------------------------- provider adapters
def test_ollama_adapter_speaks_the_chat_api_over_loopback() -> None:
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = json.dumps({"model": "llama3:8b", "message": {"role": "assistant", "content": '{"claims": []}'}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OllamaProvider("llama3", f"http://127.0.0.1:{server.server_port}")
        response = provider.generate("system text", "user text", OUTPUT_SCHEMA)
    finally:
        server.shutdown()
        server.server_close()

    assert response == ProviderResponse({"claims": []}, "llama3:8b")
    assert seen["path"] == "/api/chat"
    assert seen["body"]["model"] == "llama3" and seen["body"]["stream"] is False
    assert seen["body"]["format"] == OUTPUT_SCHEMA
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "system text"}, {"role": "user", "content": "user text"},
    ]


@pytest.mark.parametrize("reply", [
    {"message": None},
    {"message": []},
    {"message": "text"},
    {"message": {"content": None}},
    {"message": {"content": ["x"]}},
    {"message": {"content": "not json"}},
    {"message": {"content": "[1, 2]"}},
    [],
    None,
    "just a string",
])
def test_malformed_ollama_responses_are_provider_errors(tmp_path, capsys, reply) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(ProviderError):
            OllamaProvider("llama3", host).generate("s", "u", OUTPUT_SCHEMA)
        # Through the CLI this is the documented JSON error with exit code 7, not a crash.
        root = scanned(tmp_path, capsys)
        assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "llama3", "--ollama-host", host)[0] == 0
        code, response = invoke(root, capsys, "ai-interpret")
    finally:
        server.shutdown()
        server.server_close()
    assert code == 7, response
    assert_valid("cli-response-1.0.json", response)
    assert "Unexpected error" not in response["errors"][0]
    assert not (root / ".repodocs" / "ai-claims.json").exists()


def test_ollama_model_field_of_the_wrong_type_falls_back_to_the_requested_model() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"model": {"name": "x"}, "message": {"content": '{"claims": []}'}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = OllamaProvider("llama3", f"http://127.0.0.1:{server.server_port}").generate("s", "u", OUTPUT_SCHEMA)
    finally:
        server.shutdown()
        server.server_close()
    assert response.model == "llama3"


def fake_anthropic(monkeypatch, *, stop_reason="end_turn", text='{"claims": []}', error=None):
    """A stand-in for the SDK module, so the adapter is tested without the SDK or the network."""

    calls: list[dict] = []

    class APIStatusError(Exception):
        status_code = 500

    module = types.SimpleNamespace(
        AuthenticationError=type("AuthenticationError", (APIStatusError,), {}),
        RateLimitError=type("RateLimitError", (APIStatusError,), {}),
        APIStatusError=APIStatusError,
        APIConnectionError=type("APIConnectionError", (Exception,), {}),
    )

    def create(**kwargs):
        calls.append(kwargs)
        if error:
            raise getattr(module, error)()
        block = types.SimpleNamespace(type="text", text=text)
        thinking = types.SimpleNamespace(type="thinking", thinking="")
        return types.SimpleNamespace(stop_reason=stop_reason, content=[thinking, block], model="claude-opus-5-5")

    module.Anthropic = lambda: types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    )
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return calls


def test_anthropic_adapter_requests_schema_constrained_output(monkeypatch) -> None:
    calls = fake_anthropic(monkeypatch)
    provider = AnthropicProvider(None)
    assert provider.external is True and provider.model == "claude-opus-5-5"

    response = provider.generate("system text", "user text", OUTPUT_SCHEMA)

    assert response == ProviderResponse({"claims": []}, "claude-opus-5-5")
    request = calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["system"] == "system text"
    assert request["messages"] == [{"role": "user", "content": "user text"}]
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    assert "api_key" not in request


@pytest.mark.parametrize("kwargs, message", [
    ({"stop_reason": "refusal"}, "declined"),
    ({"stop_reason": "max_tokens"}, "cut off"),
    ({"text": "not json"}, "not valid JSON"),
    ({"text": "[1]"}, "not an object"),
    ({"error": "AuthenticationError"}, "credentials"),
    ({"error": "RateLimitError"}, "rate limit"),
    ({"error": "APIStatusError"}, "HTTP 500"),
    ({"error": "APIConnectionError"}, "Unable to reach"),
])
def test_anthropic_adapter_turns_failures_into_provider_errors(monkeypatch, kwargs, message) -> None:
    fake_anthropic(monkeypatch, **kwargs)
    with pytest.raises(ProviderError, match=message):
        AnthropicProvider("claude-opus-5-5").generate("s", "u", OUTPUT_SCHEMA)


def test_anthropic_adapter_explains_a_missing_sdk(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(ConfigError, match=r"repodocs\[ai\]"):
        AnthropicProvider(None).generate("s", "u", OUTPUT_SCHEMA)
