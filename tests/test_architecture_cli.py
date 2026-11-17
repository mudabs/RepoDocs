"""The architecture workflow through the CLI: scan, context, discover, generate, validate, and what can go wrong.

Providers are replayed, never live. Nothing here proves how well a real model describes an application.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from repodocs.ai import providers
from repodocs.ai.providers import AnthropicProvider, ProviderResponse
from repodocs.architecture.export import HTML_NAME, mermaid_bundle, validate_export_page
from repodocs.errors import ProviderError, ValidationError
from repodocs.publish import render_pull_request_body

from conftest import invoke, read_ir, read_manifest
from test_ai import project as react_spring_project
from test_architecture_discovery import FIXTURE, Scripted, full_answer
from test_publish import FakePullRequests, build_project, FIXTURES
from test_schemas import assert_valid

VIEW = ".repodocs/architecture-view.json"


def express_project(tmp_path: Path, capsys, *, provider: str = "ollama") -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(FIXTURE, tmp_path / "shop")
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(tmp_path / "shop"), "--id", "shop", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    if provider:
        assert invoke(root, capsys, "ai-configure", "--provider", provider, "--model", "replay-model")[0] == 0
    return root


def use(monkeypatch, provider: Scripted) -> Scripted:
    monkeypatch.setattr(providers, "resolve_provider", lambda settings: provider)
    return provider


def generic(message: dict[str, Any]) -> dict[str, Any]:
    """One component per repository, citing the first excerpt of its own repository."""

    first: dict[str, dict[str, Any]] = {}
    for item in message["excerpts"]:
        first.setdefault(item["repository_id"], item)
    return {
        "components": [
            {"name": f"Service {repository}", "kind": "other", "responsibility": "Does its work.", "repository_id": repository,
             "members": [item["path"]], "evidence": [item["id"]], "confidence": "medium"}
            for repository, item in sorted(first.items())
        ],
        "connections": [], "alternatives": [], "requests": [], "complete": True,
    }


def files(root: Path) -> set[str]:
    return {path.name for path in (root.parent / "shop" / "docs" / "generated").glob("*")}


# ------------------------------------------------------------------ the whole workflow
def test_scan_context_discover_generate_validate_and_a_quiet_second_run(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    provider = use(monkeypatch, Scripted(full_answer, full_answer))
    ir_before = (root / ".repodocs" / "ir.json").read_bytes()

    code, context = invoke(root, capsys, "architecture", "context")
    assert code == 0 and context["data"]["documents"] >= 3 and provider.messages == [], "the preview must not contact the provider"

    code, discovered = invoke(root, capsys, "architecture", "discover")
    assert code == 0, discovered["errors"]
    data = discovered["data"]
    assert data["status"] == "complete" and data["reused"] is False and data["components"] == 3 and data["connections"] == 2
    assert data["provenance"]["provider"] == "ollama" and data["provenance"]["external"] is False
    assert (root / ".repodocs" / "ir.json").read_bytes() == ir_before and not (root / ".repodocs" / "confirmations.json").exists()

    code, generated = invoke(root, capsys, "generate")
    assert code == 0 and generated["data"]["architecture"] == {"view": "current", "rendered": True}
    assert {"architecture-view.mmd", "architecture-view-evidence.json"} <= set(generated["data"]["artifacts"])
    assert "architecture.md" in generated["data"]["artifacts"] and "architecture-ai.md" not in generated["data"]["artifacts"]
    labels = {item["path"]: item["generator"] for item in read_manifest(root)["artifacts"]}
    assert labels["architecture-view.mmd"] == "repodocs-architecture" and labels["architecture.mmd"] == "repodocs-deterministic"

    code, validated = invoke(root, capsys, "validate")
    assert code == 0 and validated["warnings"] == []

    code, again = invoke(root, capsys, "generate")
    assert code == 0 and again["data"]["changed"] is False
    assert len(provider.messages) == 1, "generate must never call the provider"
    for payload in (context, discovered, generated, validated, again):
        assert_valid("cli-response-1.0.json", payload)


def test_a_current_view_is_reused_without_the_provider_until_a_refresh_is_asked_for(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    provider = use(monkeypatch, Scripted(full_answer, full_answer))
    assert invoke(root, capsys, "architecture", "discover")[0] == 0
    first = (root / VIEW).read_bytes()

    code, payload = invoke(root, capsys, "architecture", "discover")
    assert code == 0 and payload["data"]["reused"] is True and len(provider.messages) == 1

    code, payload = invoke(root, capsys, "architecture", "discover", "--refresh")
    assert code == 0 and payload["data"]["reused"] is False and len(provider.messages) == 2
    assert (root / VIEW).read_bytes() == first, "identical replies give an identical stored view"


def test_reuse_needs_no_provider_at_all(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    use(monkeypatch, Scripted(full_answer))
    assert invoke(root, capsys, "architecture", "discover")[0] == 0
    assert invoke(root, capsys, "ai-configure", "--provider", "none")[0] == 0

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 0 and payload["data"]["reused"] is True


# ------------------------------------------------------------------ failures
def test_a_provider_failure_exits_7_with_a_machine_readable_error_and_leaves_everything_usable(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    use(monkeypatch, Scripted(ProviderError("Unable to reach Ollama")))

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 7 and payload["ok"] is False and payload["data"] is None
    assert "Unable to reach Ollama" in payload["errors"][0] and "unaffected" in payload["errors"][0]
    assert_valid("cli-response-1.0.json", payload)
    assert not (root / VIEW).exists()
    assert invoke(root, capsys, "generate")[0] == 0 and invoke(root, capsys, "validate")[0] == 0
    assert "architecture-view.mmd" not in files(root) and "architecture.md" in files(root)


def test_without_a_provider_discovery_says_how_to_choose_one(tmp_path: Path, capsys) -> None:
    root = express_project(tmp_path, capsys, provider="")

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 2 and "ai-configure --provider" in payload["errors"][0] and "work without one" in payload["errors"][0]


def test_an_external_provider_needs_the_source_consent_before_anything_is_built_or_sent(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys, provider="anthropic")
    monkeypatch.setattr(AnthropicProvider, "generate", lambda *args: pytest.fail("sent without consent"))

    code, payload = invoke(root, capsys, "architecture", "discover")
    assert code == 2 and "no consent is recorded" in payload["errors"][0]

    assert invoke(root, capsys, "ai-configure", "--consent-external")[0] == 0
    code, payload = invoke(root, capsys, "architecture", "discover")
    assert code == 2 and "Consent for the evidence bundle does not cover it" in payload["errors"][0]
    assert not (root / ".repodocs" / "architecture-context.json").exists(), "nothing was prepared for sending"
    assert not (root / VIEW).exists()


def test_with_both_consents_an_external_provider_is_used_and_recorded_as_external(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys, provider="anthropic")
    monkeypatch.setattr(AnthropicProvider, "generate", lambda self, system, user, schema: ProviderResponse(full_answer(json.loads(user)), "claude-replay"))
    assert invoke(root, capsys, "ai-configure", "--consent-external", "--consent-source-excerpts")[0] == 0

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 0, payload["errors"]
    assert payload["data"]["provenance"]["external"] is True and payload["data"]["provenance"]["served_model"] == "claude-replay"
    assert (root / ".repodocs" / "architecture-context.json").exists()
    assert invoke(root, capsys, "ai-configure", "--revoke-source-consent")[0] == 0
    code, payload = invoke(root, capsys, "architecture", "discover", "--refresh")
    assert code == 2 and "Consent for the evidence bundle does not cover it" in payload["errors"][0]


def test_a_partial_discovery_is_reported_and_still_renders_with_a_visible_note(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    asks = lambda message: full_answer(message, complete=False, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 3}])  # noqa: E731
    use(monkeypatch, Scripted(asks, ProviderError("timed out")))

    code, payload = invoke(root, capsys, "architecture", "discover")

    assert code == 0 and payload["data"]["status"] == "partial" and payload["data"]["partial_reason"] == "provider-error"
    assert any("partial" in warning for warning in payload["warnings"])
    assert invoke(root, capsys, "generate")[0] == 0
    diagram = (tmp_path / "shop" / "docs" / "generated" / "architecture-view.mmd").read_text(encoding="utf-8")
    assert "Partial discovery: provider-error" in diagram


# ------------------------------------------------------------------ staleness and ownership
def test_a_changed_source_makes_the_view_stale_visible_and_retires_its_files_safely(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    provider = use(monkeypatch, Scripted(full_answer, full_answer))
    assert invoke(root, capsys, "architecture", "discover")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    (tmp_path / "shop" / "lib" / "storage.js").write_text("module.exports = {};\n", encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0

    code, generated = invoke(root, capsys, "generate")
    assert code == 0 and generated["data"]["architecture"] == {"view": "stale", "rendered": False}
    assert any("stale" in warning and "no architecture diagrams" in warning for warning in generated["warnings"])
    assert "architecture-view.mmd" not in files(root) and "architecture.mmd" in files(root)

    code, validated = invoke(root, capsys, "validate")
    assert code == 0 and any("stale" in warning for warning in validated["warnings"])

    code, refreshed = invoke(root, capsys, "architecture", "discover")
    assert code == 0 and refreshed["data"]["reused"] is False and any("replaced" in warning for warning in refreshed["warnings"])
    assert len(provider.messages) == 2


def test_a_tampered_view_is_invalid_and_never_rendered(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    use(monkeypatch, Scripted(full_answer))
    assert invoke(root, capsys, "architecture", "discover")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    view = json.loads((root / VIEW).read_text(encoding="utf-8"))
    view["components"][0]["members"] = ["../../etc/passwd"]
    (root / VIEW).write_text(json.dumps(view), encoding="utf-8")

    code, generated = invoke(root, capsys, "generate")

    assert code == 0 and generated["data"]["architecture"]["view"] == "invalid" and generated["data"]["architecture"]["rendered"] is False
    assert any("invalid" in warning for warning in generated["warnings"])


def test_an_edited_architecture_file_stops_generation_and_validation(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    use(monkeypatch, Scripted(full_answer))
    assert invoke(root, capsys, "architecture", "discover")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    target = tmp_path / "shop" / "docs" / "generated" / "architecture-view.mmd"
    target.write_text(target.read_text(encoding="utf-8") + "%% edited\n", encoding="utf-8", newline="\n")

    code, payload = invoke(root, capsys, "generate")
    assert code == 4 and payload["ok"] is False and "edited unexpectedly" in payload["errors"][0]
    code, payload = invoke(root, capsys, "validate")
    assert code == 4 and "edited unexpectedly" in payload["errors"][0]
    assert "%% edited" in target.read_text(encoding="utf-8")


def test_a_hand_written_architecture_file_that_matches_nothing_current_fails_validation(tmp_path: Path, capsys, monkeypatch) -> None:
    root = express_project(tmp_path, capsys)
    use(monkeypatch, Scripted(full_answer))
    assert invoke(root, capsys, "architecture", "discover")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    target = tmp_path / "shop" / "docs" / "generated" / "architecture-view.mmd"
    manifest_path = tmp_path / "project" / ".repodocs" / "artifact-manifest.json"
    forged = "flowchart LR\n    a[\"forged\"]\n"
    target.write_text(forged, encoding="utf-8", newline="\n")
    from repodocs.common import sha256_text

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    next(item for item in manifest["artifacts"] if item["path"] == "architecture-view.mmd")["content_hash"] = sha256_text(forged)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    code, payload = invoke(root, capsys, "validate")

    assert code == 3 and "does not match the current architecture view" in payload["errors"][0]


# ------------------------------------------------------------------ confirmations and the IR stay in charge
def test_unresolved_cross_repository_confirmations_still_fail_validation_and_discovery_changes_none(tmp_path: Path, capsys, monkeypatch) -> None:
    root = react_spring_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "replay-model")[0] == 0
    use(monkeypatch, Scripted(generic))
    before = read_ir(root)

    code, discovered = invoke(root, capsys, "architecture", "discover")
    assert code == 0, discovered["errors"]
    assert read_ir(root) == before and not (root / ".repodocs" / "confirmations.json").exists()
    assert invoke(root, capsys, "generate")[0] == 0

    code, payload = invoke(root, capsys, "validate")
    assert code == 5 and "require confirmation" in payload["errors"][0]
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0
    unresolved = [item["id"] for item in before["candidates"]]
    assert unresolved
    assert invoke(root, capsys, "confirm", *unresolved)[0] == 0
    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["architecture"]["view"] == "stale", "a confirmation changes the IR, so the view must be refreshed"


# ------------------------------------------------------------------ publication
def test_publication_sees_the_architecture_files_and_explains_their_removal(tmp_path: Path, capsys, monkeypatch) -> None:
    project = build_project(tmp_path, capsys, {"backend": FIXTURES / "basic-repo"})
    assert project.run("ai-configure", "--provider", "ollama", "--model", "replay-model")[0] == 0
    project.commit_docs("configure the provider", push=False)
    project.generate()
    use(monkeypatch, Scripted(generic))
    assert project.run("architecture", "discover")[0] == 0
    project.generate()

    result = project.publish(FakePullRequests(), dry_run=True, remote="no-such-remote")

    paths = {change["path"] for change in result["changes"]}
    assert {"docs/generated/architecture-view.mmd", "docs/generated/architecture-view-evidence.json"} <= paths

    project.commit_docs()
    (project.root / VIEW).unlink()   # a clean CI clone has no stored view
    project.generate()
    removed = project.publish(FakePullRequests(), dry_run=True, remote="no-such-remote")
    deleted = {change["path"] for change in removed["changes"] if change["status"] == "D"}
    assert "docs/generated/architecture-view.mmd" in deleted
    body = render_pull_request_body(
        {"snapshots": [], "candidates": []}, [("D", "docs/generated/architecture-view.mmd"), ("M", "docs/generated/architecture.md")], "bot", "main"
    )
    assert "removed: `docs/generated/architecture-view.mmd`" in body and "architecture discover --refresh" in body
    assert "Architecture-view files" not in render_pull_request_body({"snapshots": [], "candidates": []}, [("M", "docs/generated/architecture.md")], "bot", "main")


# ------------------------------------------------------------------ the optional page
def test_the_optional_page_is_generated_validated_and_checked_for_tampering(tmp_path: Path, capsys, monkeypatch) -> None:
    if mermaid_bundle() is None:
        pytest.skip("the optional Mermaid bundle is not installed")
    root = express_project(tmp_path, capsys)
    configuration = json.loads((root / "repodocs.json").read_text(encoding="utf-8"))
    configuration["architecture"] = {"html_export": True}
    (root / "repodocs.json").write_text(json.dumps(configuration), encoding="utf-8")
    use(monkeypatch, Scripted(full_answer))
    assert invoke(root, capsys, "architecture", "discover")[0] == 0

    code, generated = invoke(root, capsys, "generate")
    assert code == 0 and HTML_NAME in generated["data"]["artifacts"]
    page = (tmp_path / "shop" / "docs" / "generated" / HTML_NAME).read_text(encoding="utf-8")
    validate_export_page(page)
    assert invoke(root, capsys, "validate")[0] == 0
    for broken in (page.replace("connect-src 'none'", "connect-src *"), page.replace("<script>", "<script src=\"https://evil.example/x.js\"></script><script>", 1), page + "<img src=x>"):
        with pytest.raises(ValidationError):
            validate_export_page(broken)
