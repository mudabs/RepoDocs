"""AI documentation must never replace facts, bypass decisions, or break fallback."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from conftest import invoke, read_ir, read_manifest
from repodocs.ai import providers
from repodocs.ai.bundle import build_bundle
from repodocs.ai.claims import interpret
from repodocs.ai.docs import claims_status
from repodocs.ai.evaluation import contribution
from repodocs.common import write_json
from test_ai import FakeProvider, read_json, scanned
from test_schemas import assert_valid


GOLDEN = Path(__file__).parent / "fixtures" / "ai-architecture" / "golden.json"


def example(ir: dict) -> dict:
    endpoint = next(node for node in ir["nodes"] if node["type"] == "endpoint")
    return {
        "text": "The backend declares an orders operation.",
        "classification": "inferred",
        "subject_ids": [endpoint["id"]],
        "evidence_ids": endpoint["evidence_ids"],
    }


def interpreted(root, capsys, monkeypatch, claims=None) -> dict:
    fake = FakeProvider({"claims": claims if claims is not None else [example(read_ir(root))]})
    monkeypatch.setattr(providers, "resolve_provider", lambda settings: fake)
    assert invoke(root, capsys, "ai-configure", "--provider", "ollama", "--model", "fake-model")[0] == 0
    code, response = invoke(root, capsys, "ai-interpret")
    assert code == 0, response
    assert response["data"]["contribution"]["unsupported_accepted"] == 0
    return read_json(root / ".repodocs" / "ai-claims.json")


def output(root) -> Path:
    return root / "docs" / "docs" / "generated"


def generate(root, capsys) -> dict:
    code, response = invoke(root, capsys, "generate")
    assert code == 0, response
    return response


def test_ai_document_is_separate_provenanced_and_deterministic_output_is_identical(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    ir_before = read_ir(root)
    generate(root, capsys)
    baseline = {name: (output(root) / name).read_bytes() for name in ("architecture.md", "architecture.mmd")}
    document = interpreted(root, capsys, monkeypatch)
    response = generate(root, capsys)
    assert response["data"]["ai"] == {"claims": "current", "rendered": True}
    assert "architecture-ai.md" in response["data"]["artifacts"]
    for name, content in baseline.items():
        assert (output(root) / name).read_bytes() == content
    assert read_ir(root) == ir_before
    markdown = (output(root) / "architecture-ai.md").read_text(encoding="utf-8")
    for text in ("AI-assisted", "not observed fact", "does not prove", "fake-model", "ollama", "Evidence:"):
        assert text in markdown
    assert document["provenance"]["bundle_hash"] in markdown
    for claim in document["accepted"]:
        assert claim["id"] in markdown
        assert all(key in markdown for key in claim["evidence_ids"])
    manifest = read_manifest(root)
    assert_valid("artifact-manifest-1.0.json", manifest)
    assert {entry["path"]: entry["generator"] for entry in manifest["artifacts"]} == {
        "architecture.md": "repodocs-deterministic",
        "architecture.mmd": "repodocs-deterministic",
        "architecture-ai.md": "repodocs-ai",
    }
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0
    assert invoke(root, capsys, "validate")[0] == 5  # AI did not confirm anything.
    assert generate(root, capsys)["data"]["changed"] is False


@pytest.mark.parametrize("contents", [
    "{ not json",
    "[]",
    "{}",
    '{"schema_version":"wrong"}',
    '{"accepted":[{"text":"secret=DoNotEcho"}]}',
])
def test_malformed_optional_claims_leave_fallback_complete(tmp_path, capsys, contents, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    generate(root, capsys)
    baseline = (output(root) / "architecture.md").read_bytes()
    (root / ".repodocs" / "ai-claims.json").write_text(contents, encoding="utf-8")
    monkeypatch.setattr(providers, "resolve_provider", lambda *args: pytest.fail("generation must never contact a provider"))
    response = generate(root, capsys)
    assert response["data"]["ai"] == {"claims": "invalid", "rendered": False}
    assert response["warnings"] and "DoNotEcho" not in json.dumps(response)
    assert (output(root) / "architecture.md").read_bytes() == baseline
    assert not (output(root) / "architecture-ai.md").exists()
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0


@pytest.mark.parametrize("mutation", ["source", "decision", "removed", "tampered", "bad-rejection", "bad-provenance"])
def test_stale_or_invalid_ai_document_is_retired_safely(tmp_path, capsys, monkeypatch, mutation) -> None:
    root = scanned(tmp_path, capsys)
    interpreted(root, capsys, monkeypatch)
    generate(root, capsys)
    claims_path = root / ".repodocs" / "ai-claims.json"
    if mutation == "source":
        app = root / "frontend" / "src" / "App.tsx"
        app.write_text("// revision changed\n" + app.read_text(encoding="utf-8"), encoding="utf-8")
        assert invoke(root, capsys, "scan")[0] == 0
    elif mutation == "decision":
        assert invoke(root, capsys, "confirm", read_ir(root)["candidates"][0]["id"])[0] == 0
    elif mutation == "removed":
        claims_path.unlink()
    else:
        document = read_json(claims_path)
        if mutation == "tampered":
            document["accepted"][0]["evidence_ids"] = ["evidence:invented"]
        elif mutation == "bad-rejection":
            document["rejected"] = [{"raw": "invalid structure"}]
        else:
            document["provenance"]["external"] = "false"
        write_json(claims_path, document)
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 3
    response = generate(root, capsys)
    expected = "stale" if mutation in ("source", "decision") else "absent" if mutation == "removed" else "invalid"
    assert response["data"]["ai"] == {"claims": expected, "rendered": False}
    assert not (output(root) / "architecture-ai.md").exists()
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0
    assert all(item["path"] != "architecture-ai.md" for item in read_manifest(root)["artifacts"])


@pytest.mark.parametrize("owned", [True, False])
def test_ai_artifact_ownership_is_enforced_before_overwriting_or_retiring(tmp_path, capsys, monkeypatch, owned) -> None:
    root = scanned(tmp_path, capsys)
    if owned:
        interpreted(root, capsys, monkeypatch)
    generate(root, capsys)
    path = output(root) / "architecture-ai.md"
    path.write_text("# Human work\n", encoding="utf-8")
    if owned:
        (root / ".repodocs" / "ai-claims.json").unlink()
    code, response = invoke(root, capsys, "generate")
    assert code == 4, response
    assert path.read_text(encoding="utf-8") == "# Human work\n"


def test_model_markdown_is_inert_and_rejected_text_is_not_rendered(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    claim = dict(example(read_ir(root)), text='<script>alert(1)</script> [run](javascript:evil) password=DoNotLeak')
    rejected = dict(claim, text="Rejected text must not appear", evidence_ids=["evidence:missing"])
    interpreted(root, capsys, monkeypatch, [claim, rejected])
    generate(root, capsys)
    markdown = (output(root) / "architecture-ai.md").read_text(encoding="utf-8")
    assert "<script>" not in markdown and "[run](" not in markdown and "DoNotLeak" not in markdown
    assert "Rejected text must not appear" not in markdown
    assert "unknown-evidence" in markdown
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0


def test_confirmed_candidate_interpretation_does_not_create_a_graph_edge(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    candidate = read_ir(root)["candidates"][0]
    assert invoke(root, capsys, "confirm", candidate["id"])[0] == 0
    ir = read_ir(root)
    document = interpreted(root, capsys, monkeypatch, [{
        "text": "A maintainer confirmed this API match.", "classification": "inferred",
        "subject_ids": [candidate["id"]], "evidence_ids": candidate["evidence_ids"],
    }])
    assert document["accepted"][0]["classification"] == "inferred"
    generate(root, capsys)
    assert read_ir(root) == ir
    assert ir["relationships"] == read_ir(root)["relationships"]


@pytest.mark.parametrize("missing_side", ["source", "target"])
def test_legacy_single_sided_inference_is_invalid_for_rendering_and_evaluation(
    tmp_path, capsys, monkeypatch, missing_side
) -> None:
    root = scanned(tmp_path, capsys)
    candidate = read_ir(root)["candidates"][0]
    assert invoke(root, capsys, "confirm", candidate["id"])[0] == 0
    ir = read_ir(root)
    nodes = {node["id"]: node for node in ir["nodes"]}
    evidence = {item["id"]: item for item in ir["evidence"]}
    missing_repo = nodes[candidate[missing_side]]["attributes"]["repository_id"]
    partial = [key for key in candidate["evidence_ids"] if evidence[key]["repository_id"] != missing_repo]
    document = interpreted(root, capsys, monkeypatch, [{
        "text": "One-sided evidence must not establish the confirmed pair.",
        "classification": "inferred", "subject_ids": [candidate["id"]], "evidence_ids": partial,
    }])
    assert document["accepted"][0]["classification"] == "proposed"
    # Simulate an accepted document written by the buggy validator. Claim IDs
    # exclude classification, so other checks cannot hide this regression.
    document["accepted"][0]["classification"] = "inferred"
    assert claims_status(ir, document) == "invalid"
    assert contribution(ir, document)["unsupported_accepted"] == 1
    write_json(root / ".repodocs" / "ai-claims.json", document)
    response = generate(root, capsys)
    assert response["data"]["ai"] == {"claims": "invalid", "rendered": False}
    assert not (output(root) / "architecture-ai.md").exists()


def test_provider_failure_keeps_existing_deterministic_artifacts_usable(tmp_path, capsys, monkeypatch) -> None:
    from repodocs.errors import ProviderError

    root = scanned(tmp_path, capsys)
    generate(root, capsys)
    baseline = (output(root) / "architecture.md").read_bytes()

    class BrokenProvider(FakeProvider):
        def generate(self, *args, **kwargs):
            raise ProviderError("Unavailable fixture provider")

    monkeypatch.setattr(providers, "resolve_provider", lambda settings: BrokenProvider({}))
    assert invoke(root, capsys, "ai-interpret")[0] == 7
    assert generate(root, capsys)["data"]["ai"]["rendered"] is False
    assert (output(root) / "architecture.md").read_bytes() == baseline
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 0


def test_manifest_cannot_relabel_an_ai_document_as_deterministic(tmp_path, capsys, monkeypatch) -> None:
    root = scanned(tmp_path, capsys)
    interpreted(root, capsys, monkeypatch)
    generate(root, capsys)
    manifest = read_manifest(root)
    for entry in manifest["artifacts"]:
        if entry["path"] == "architecture-ai.md":
            entry["generator"] = "repodocs-deterministic"
    write_json(root / ".repodocs" / "artifact-manifest.json", manifest)
    assert invoke(root, capsys, "validate", "--allow-unresolved")[0] == 3
    (root / ".repodocs" / "ai-claims.json").unlink()
    assert invoke(root, capsys, "generate")[0] == 4
    assert (output(root) / "architecture-ai.md").exists()


def test_reviewed_golden_fixture_measures_contribution_without_unsupported_inference(tmp_path, capsys) -> None:
    root = scanned(tmp_path, capsys)
    ir = read_ir(root)
    bundle = build_bundle(ir)
    fixture = read_json(GOLDEN)
    candidate = bundle["candidates"][0]
    endpoint = next(node for node in bundle["nodes"] if node["id"] == candidate["target"])
    request = next(node for node in bundle["nodes"] if node["id"] == candidate["source"])
    objects = {"endpoint": endpoint, "candidate": candidate, "request": request}
    raw = []
    for case in fixture["claims"]:
        raw.append({
            "text": case["text"], "classification": case["classification"],
            "subject_ids": [] if case["subject"] == "none" else ["node:missing"] if case["subject"] == "unknown"
                           else [objects[case["subject"]]["id"]],
            "evidence_ids": [] if case["evidence"] == "none" else ["evidence:missing"] if case["evidence"] == "unknown"
                            else objects[case["evidence"]]["evidence_ids"],
        })
    before = copy.deepcopy(ir)
    document = interpret(bundle, FakeProvider({"claims": raw}))
    assert ir == before
    assert claims_status(ir, document) == "current"
    accepted = {item["text"]: item for item in document["accepted"]}
    rejected = {item["text"] for item in document["rejected"]}
    for case in fixture["claims"]:
        assert (case["text"] in rejected if case["expected"] == "rejected"
                else accepted[case["text"]]["classification"] == case["expected"])
    report = contribution(ir, document)
    reviewed_unsupported = sum(
        not case["supported"] and accepted.get(case["text"], {}).get("classification") == "inferred"
        for case in fixture["claims"]
    )
    assert reviewed_unsupported == fixture["expected"]["reviewed_unsupported_inferred"] == 0
    for name in ("ai_inferred_claims", "ai_proposed_claims", "ai_rejected_claims", "unsupported_accepted"):
        assert report[name] == fixture["expected"][name]
    assert report["deterministic_claims"] == len(ir["claims"]) > 0
    assert report["subjects_interpreted"] == 1 and report["evidence_cited"] > 0
    assert_valid("ai-claims-1.0.json", document)
