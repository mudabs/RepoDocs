"""The shared application service: CLI parity, cancellation, progress, failures and per-run providers.

Providers are replayed, never live. There is no HTTP server, no frontend runtime and no credential here.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from repodocs import service
from repodocs.ai import providers
from repodocs.ai.providers import ProviderResponse
from repodocs.config import SOURCE_POLICY_VERSION, ProjectConfig
from repodocs.errors import ConfigError, ConfirmationError, ConsentError, ProviderError
from repodocs.service import (
    CancellationToken, ContextRequest, DiscoverRequest, GenerateRequest, ProgressEvent, RunHooks, ScanRequest, ValidateRequest,
)

from conftest import allow_large_context, invoke, read_ir
from test_architecture_discovery import FIXTURE, Scripted, full_answer


def project(tmp_path: Path, capsys, *, scan: bool = True) -> Path:
    """An Express project configured through the CLI, whose own scan, if wanted, is the CLI's."""

    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(FIXTURE, tmp_path / "shop")
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(tmp_path / "shop"), "--id", "shop", "--destination")[0] == 0
    assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "replay-model")[0] == 0
    allow_large_context(root)
    if scan:
        assert invoke(root, capsys, "scan")[0] == 0
    return root


def generated(root: Path) -> dict[str, bytes]:
    folder = root.parent / "shop" / "docs" / "generated"
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*"))}


def listen() -> tuple[list[ProgressEvent], RunHooks]:
    events: list[ProgressEvent] = []
    return events, RunHooks(progress=events.append)


# ------------------------------------------------------------------ parity with the CLI
def test_cli_and_an_in_process_adapter_produce_identical_ir_view_and_artifacts(tmp_path: Path, capsys, monkeypatch) -> None:
    """Both run in the same place in turn, because the IR records absolute source paths and hashes them."""

    monkeypatch.setattr(providers, "resolve_provider", lambda settings: Scripted(full_answer))
    base = tmp_path / "run"

    def outputs(root: Path) -> dict[str, bytes]:
        state = root / ".repodocs"
        return {
            **{name: (state / name).read_bytes() for name in ("ir.json", "architecture-view.json", "artifact-manifest.json")},
            **{f"docs/{name}": content for name, content in generated(root).items()},
        }

    base.mkdir()
    cli_root = project(base, capsys, scan=False)
    assert invoke(cli_root, capsys, "scan")[0] == 0
    assert invoke(cli_root, capsys, "architecture", "discover")[0] == 0
    assert invoke(cli_root, capsys, "generate")[0] == 0
    code, validated = invoke(cli_root, capsys, "validate")
    assert code == 0
    from_cli = outputs(cli_root)
    shutil.rmtree(base)

    base.mkdir()
    web_root = project(base, capsys, scan=False)
    scanned = service.execute("scan", ScanRequest(web_root))
    discovered = service.execute("discover", DiscoverRequest(web_root, provider=Scripted(full_answer)))
    built = service.execute("generate", GenerateRequest(web_root))
    checked = service.execute("validate", ValidateRequest(web_root))

    assert [item.status for item in (scanned, discovered, built, checked)] == ["complete"] * 4
    assert outputs(web_root) == from_cli and "docs/architecture-view.mmd" in from_cli
    assert checked.data["artifacts"] == validated["data"]["artifacts"]
    assert checked.view_status == "current"


def test_cli_output_is_the_service_payload(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    code, payload = invoke(root, capsys, "generate")
    again = service.generate(GenerateRequest(root))
    assert code == 0 and payload["data"]["changed"] is True
    assert again.data == {**payload["data"], "changed": False}, "a repeat run through the service is a no-op"


def test_results_serialize_with_a_version(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    result = service.generate(GenerateRequest(root)).to_dict()
    assert json.loads(json.dumps(result)) == result
    assert result["service_version"] == "1.0" and result["status"] == "complete" and result["error"] is None


# ------------------------------------------------------------------ no server or frontend
def test_the_service_imports_without_a_web_stack(tmp_path: Path) -> None:
    code = "import sys, repodocs.service, repodocs.cli; print(sorted(m for m in ('fastapi', 'uvicorn', 'starlette', 'http.server', 'flask') if m in sys.modules))"
    source = str(Path(service.__file__).resolve().parents[1])
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=tmp_path, env={**os.environ, "PYTHONPATH": source}
    )
    assert done.stdout.strip() == "[]"


# ------------------------------------------------------------------ requests are immutable
def test_requests_are_immutable(tmp_path: Path) -> None:
    pins = {"shop": "a" * 40}
    request = ScanRequest(tmp_path, pins=pins)
    pins["other"] = "b" * 40
    assert dict(request.pins) == {"shop": "a" * 40}
    with pytest.raises(TypeError):
        request.pins["x"] = "y"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.root = tmp_path / "elsewhere"  # type: ignore[misc]


def test_pinned_revision_semantics_are_preserved_through_the_service(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys, scan=False)
    result = service.execute("scan", ScanRequest(root, snapshot_mode="pinned", pins={"shop": "0" * 40}))
    assert result.status == "failed" and result.error.code == "snapshot" and result.error.exit_code == 6
    assert not (root / ".repodocs" / "ir.json").exists()


# ------------------------------------------------------------------ progress and cancellation
def test_progress_events_report_each_stage_without_a_server(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys, scan=False)
    events, hooks = listen()
    service.scan(ScanRequest(root), hooks)
    service.discover(DiscoverRequest(root, provider=Scripted(full_answer)), hooks)
    service.generate(GenerateRequest(root), hooks)
    assert [(item.operation, item.stage) for item in events] == [
        ("scan", "scanning"), ("scan", "writing-ir"),
        ("discover", "loading"), ("discover", "provider-request"), ("discover", "storing-view"),
        ("generate", "loading"), ("generate", "writing-artifacts"),
    ]


def test_cancelling_before_a_scan_writes_nothing(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys, scan=False)
    token = CancellationToken()
    token.cancel()
    result = service.execute("scan", ScanRequest(root), RunHooks(cancel=token))
    assert result.status == "cancelled" and result.error.code == "cancelled" and result.data is None
    assert not (root / ".repodocs" / "ir.json").exists()


def test_cancelling_before_discovery_sends_nothing_to_the_provider(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    provider, token = Scripted(full_answer), CancellationToken()
    token.cancel()
    result = service.execute("discover", DiscoverRequest(root, provider=provider), RunHooks(cancel=token))
    assert result.status == "cancelled" and provider.messages == []
    assert not (root / ".repodocs" / "architecture-view.json").exists()


def test_cancelling_during_a_request_stores_no_view_and_keeps_the_previous_one(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    assert service.discover(DiscoverRequest(root, provider=Scripted(full_answer))).status == "complete"
    before = (root / ".repodocs" / "architecture-view.json").read_bytes()
    token = CancellationToken()

    def cancel_midway(message: dict[str, Any]) -> dict[str, Any]:
        token.cancel()
        return full_answer(message)

    provider = Scripted(cancel_midway)
    result = service.execute("discover", DiscoverRequest(root, provider=provider, refresh=True), RunHooks(cancel=token))

    assert result.status == "cancelled" and len(provider.messages) == 1, "no further request after cancellation"
    assert (root / ".repodocs" / "architecture-view.json").read_bytes() == before


def test_cancelling_a_later_round_is_not_recorded_as_a_provider_failure(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    token = CancellationToken()

    def ask_then_cancel(message: dict[str, Any]) -> dict[str, Any]:
        token.cancel()
        return full_answer(message, complete=False, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 3}])

    provider = Scripted(ask_then_cancel, full_answer)
    result = service.execute("discover", DiscoverRequest(root, provider=provider), RunHooks(cancel=token))
    assert result.status == "cancelled" and len(provider.messages) == 1
    assert not (root / ".repodocs" / "architecture-view.json").exists()


def test_cancelling_before_artifacts_are_written_leaves_the_output_untouched(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    token = CancellationToken()
    hooks = RunHooks(cancel=token, progress=lambda event: token.cancel() if event.stage == "loading" else None)
    result = service.execute("generate", GenerateRequest(root), hooks)
    assert result.status == "cancelled"
    assert not (root.parent / "shop" / "docs" / "generated").exists()
    assert not (root / ".repodocs" / "artifact-manifest.json").exists()


# ------------------------------------------------------------------ provider calls stay explicit
def test_preview_generate_and_validate_never_call_the_provider(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    provider = Scripted()
    preview = service.preview_context(ContextRequest(root, provider=provider))
    service.generate(GenerateRequest(root))
    service.validate(ValidateRequest(root))
    assert provider.messages == [] and preview.data["provider_is_external"] is False and preview.view_status == "missing"


def test_discovery_without_a_provider_fails_clearly_and_without_a_view(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    with pytest.raises(ConfigError, match="needs an AI provider"):
        service.discover(DiscoverRequest(root))
    result = service.execute("discover", DiscoverRequest(root))
    assert result.status == "failed" and result.error.code == "usage" and result.view_status is None


def external_provider() -> Scripted:
    provider = Scripted(full_answer)
    provider.external = True
    return provider


def set_consent(root: Path, *, external: bool, source: bool) -> None:
    config = ProjectConfig.load(root)
    config.ai = replace(
        config.ai, external_consent=external, source_excerpts_consent=source, source_consent_policy=SOURCE_POLICY_VERSION if source else None
    )
    config.save()


@pytest.mark.parametrize("external,source", [(False, False), (True, False), (False, True)])
def test_an_injected_external_provider_needs_both_consent_scopes(tmp_path: Path, capsys, external: bool, source: bool) -> None:
    root = project(tmp_path, capsys)
    set_consent(root, external=external, source=source)
    provider = external_provider()
    with pytest.raises(ConsentError):
        service.discover(DiscoverRequest(root, provider=provider))
    result = service.execute("discover", DiscoverRequest(root, provider=provider))
    assert result.status == "failed" and result.error.code == "usage" and result.error.exit_code == 2
    assert provider.messages == [] and not (root / ".repodocs" / "architecture-view.json").exists()


def test_an_injected_external_provider_with_both_consents_is_called(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    set_consent(root, external=True, source=True)
    provider = external_provider()
    result = service.discover(DiscoverRequest(root, provider=provider))
    assert result.status == "complete" and len(provider.messages) == 1 and result.data["provenance"]["external"] is True


def test_a_local_provider_needs_no_consent(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    set_consent(root, external=False, source=False)
    provider = Scripted(full_answer)
    assert service.discover(DiscoverRequest(root, provider=provider)).status == "complete" and len(provider.messages) == 1


def test_a_current_view_is_reused_without_a_provider_and_refresh_needs_one(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    assert not service.can_reuse_view(DiscoverRequest(root))
    service.discover(DiscoverRequest(root, provider=Scripted(full_answer)))
    assert service.can_reuse_view(DiscoverRequest(root)) and not service.can_reuse_view(DiscoverRequest(root, refresh=True))
    reused = service.discover(DiscoverRequest(root))
    assert reused.data["reused"] is True and reused.status == "complete"


# ------------------------------------------------------------------ view and run states
def test_view_states_are_distinct_from_run_states(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    assert service.generate(GenerateRequest(root)).view_status == "missing"
    assert service.validate(ValidateRequest(root)).view_status == "missing"
    service.discover(DiscoverRequest(root, provider=Scripted(full_answer)))
    service.generate(GenerateRequest(root))
    assert service.validate(ValidateRequest(root)).view_status == "current"

    (tmp_path / "shop" / "server.js").write_text("// changed\n" + (tmp_path / "shop" / "server.js").read_text(encoding="utf-8"), encoding="utf-8")
    assert service.scan(ScanRequest(root)).status == "complete"
    stale = service.generate(GenerateRequest(root))
    assert stale.status == "complete" and stale.view_status == "stale" and stale.data["architecture"]["view"] == "stale"

    (root / ".repodocs" / "architecture-view.json").write_text("{", encoding="utf-8")
    assert service.validate(ValidateRequest(root)).view_status == "invalid"


def test_a_partial_discovery_is_a_partial_run_with_a_stored_partial_view(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    asks = lambda message: full_answer(message, complete=False, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 3}])  # noqa: E731
    result = service.execute("discover", DiscoverRequest(root, provider=Scripted(asks, ProviderError("timed out"))))
    assert result.status == "partial" and result.data["partial_reason"] == "provider-error" and result.view_status == "current"
    assert any("partial" in warning for warning in result.warnings)


def test_a_discarded_partial_replacement_keeps_the_complete_view_and_reports_partial(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    service.discover(DiscoverRequest(root, provider=Scripted(full_answer)))
    before = (root / ".repodocs" / "architecture-view.json").read_bytes()
    asks = lambda message: full_answer(message, complete=False, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 3}])  # noqa: E731
    result = service.discover(DiscoverRequest(root, provider=Scripted(asks, ProviderError("timed out")), refresh=True))
    assert result.status == "partial" and result.data["replacement_discarded"] == "provider-error" and result.data["status"] == "complete"
    assert (root / ".repodocs" / "architecture-view.json").read_bytes() == before


def test_a_failed_provider_is_a_failed_run_with_a_stable_code(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    result = service.execute("discover", DiscoverRequest(root, provider=Scripted(ProviderError("Unable to reach Ollama"))))
    assert result.status == "failed" and result.error.code == "provider" and result.error.exit_code == 7
    assert not (root / ".repodocs" / "architecture-view.json").exists()
    assert result.to_dict()["error"] == {"code": "provider", "message": result.error.message}


# ------------------------------------------------------------------ ownership, no-op and confirmation regressions
def test_a_second_generation_is_a_no_op_and_an_edited_file_is_protected(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    assert service.generate(GenerateRequest(root)).data["changed"] is True
    assert service.generate(GenerateRequest(root)).data["changed"] is False

    path = tmp_path / "shop" / "docs" / "generated" / "architecture.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nmy edit\n", encoding="utf-8")
    result = service.execute("generate", GenerateRequest(root))
    assert result.status == "failed" and result.error.code == "ownership" and result.error.exit_code == 4
    assert path.read_text(encoding="utf-8").endswith("my edit\n")
    assert service.execute("validate", ValidateRequest(root)).error.code == "ownership"


def test_unresolved_candidates_fail_validation_unless_allowed(tmp_path: Path, capsys) -> None:
    root = project(tmp_path, capsys)
    ir = read_ir(root)
    assert service.require_resolved_candidates(ir) is None
    ir["candidates"] = [{"id": "candidate:x", "status": "candidate"}]
    with pytest.raises(ConfirmationError, match="candidate:x"):
        service.require_resolved_candidates(ir)


def test_unexpected_exceptions_are_not_hidden(tmp_path: Path, capsys, monkeypatch) -> None:
    root = project(tmp_path, capsys)
    monkeypatch.setattr(service, "OPERATIONS", {"validate": lambda request, hooks: 1 / 0})
    with pytest.raises(ZeroDivisionError):
        service.execute("validate", ValidateRequest(root))


# ------------------------------------------------------------------ concurrent runs with separate credentials
class Credentialed:
    """A provider holding its own credential. It overlaps with another run before answering."""

    name, external = "ollama", False

    def __init__(self, model: str, credential: str, barrier: threading.Barrier) -> None:
        self.model, self.credential, self.barrier = model, credential, barrier
        self.seen_environment: list[str | None] = []

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        self.barrier.wait(timeout=30)  # both runs are inside a request at the same time
        self.seen_environment.append(os.environ.get("REPODOCS_TEST_CREDENTIAL"))
        return ProviderResponse(full_answer(json.loads(user)), f"served-by-{self.model}")


def test_concurrent_runs_keep_their_own_provider_and_never_touch_the_environment(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("REPODOCS_TEST_CREDENTIAL", raising=False)
    roots = []
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        roots.append(project(tmp_path / name, capsys))
    barrier = threading.Barrier(2)
    providers_ = [Credentialed("model-one", "sk-secret-one", barrier), Credentialed("model-two", "sk-secret-two", barrier)]
    results: dict[int, service.WorkflowResult] = {}

    def run(index: int) -> None:
        results[index] = service.execute("discover", DiscoverRequest(roots[index], provider=providers_[index]))

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert {index: item.status for index, item in results.items()} == {0: "complete", 1: "complete"}
    for index in range(2):
        view = json.loads((roots[index] / ".repodocs" / "architecture-view.json").read_text(encoding="utf-8"))
        assert view["provenance"]["served_model"] == ("served-by-model-one", "served-by-model-two")[index]
        assert providers_[index].seen_environment == [None]
    assert "REPODOCS_TEST_CREDENTIAL" not in os.environ
    assert "sk-secret" not in json.dumps([item.to_dict() for item in results.values()])
