"""Cross-repository discovery, uncertainty, identity, provenance, and URL safety."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from conftest import init_git_repository, invoke, read_ir
from repodocs.correlation import discover_candidates
from repodocs.errors import ValidationError
from repodocs.validation import validate_ir
from test_schemas import assert_valid


FIXTURE = Path(__file__).parent / "fixtures" / "correlation-repo"


def project(tmp_path: Path, capsys) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    (root / "docs").mkdir()
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    for repo in ("frontend", "backend", "competing"):
        assert invoke(root, capsys, "add", repo, "--id", repo)[0] == 0
    assert invoke(root, capsys, "add", "docs", "--id", "docs", "--role", "destination")[0] == 0
    return root


def scan(root, capsys) -> dict:
    code, response = invoke(root, capsys, "scan")
    assert code == 0, response
    ir = read_ir(root)
    assert response["data"]["candidates"] == len(ir["candidates"])
    assert_valid("cli-response-1.0.json", response)
    assert_valid("system-ir-1.0.json", ir)
    validate_ir(ir)
    return ir


def test_matches_are_candidates_even_when_unique_and_high_confidence(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    ir = scan(root, capsys)
    nodes = {node["id"]: node for node in ir["nodes"]}
    assert ir["candidates"]
    assert any(candidate["confidence"] == 0.9 for candidate in ir["candidates"])
    for candidate in ir["candidates"]:
        assert candidate["classification"] == "inferred"
        assert candidate["status"] == "candidate"
        assert candidate["id"] not in {edge["id"] for edge in ir["relationships"]}
        source, target = nodes[candidate["source"]], nodes[candidate["target"]]
        assert source["attributes"]["repository_id"] != target["attributes"]["repository_id"]
        assert source["attributes"]["http_method"] == target["attributes"]["http_method"]
        assert set(candidate["evidence_ids"]) == set(source["evidence_ids"]) | set(target["evidence_ids"])
    # Extraction is observed, but no observed edge joins the two repositories.
    assert all(edge["classification"] == "observed" for edge in ir["relationships"])
    evidence = {item["id"]: item for item in ir["evidence"]}
    via_instance = next(
        candidate for candidate in ir["candidates"]
        if nodes[candidate["source"]]["attributes"]["instance"] == "api"
    )
    assert any(evidence[key]["path"] == "src/client.ts" for key in via_instance["evidence_ids"])
    assert set(via_instance["snapshots"]) == {"frontend", "backend"}


def test_methods_hosts_templates_and_competing_backends(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    ir = scan(root, capsys)
    nodes = {node["id"]: node for node in ir["nodes"]}
    for candidate in ir["candidates"]:
        request = nodes[candidate["source"]]["attributes"]
        assert request["http_method"] != "DELETE"
        assert "other.example.invalid" not in request["url"]
    relative = [
        candidate for candidate in ir["candidates"]
        if nodes[candidate["source"]]["attributes"]["url"] == "/api/orders"
    ]
    assert {nodes[item["target"]]["attributes"]["repository_id"] for item in relative} == {"backend", "competing"}
    assert all(item["confidence"] == 0.65 and not item["attributes"]["origin_supported"] for item in relative)
    templated = [
        candidate for candidate in ir["candidates"]
        if nodes[candidate["source"]]["attributes"]["url_kind"] == "template"
    ]
    assert len(templated) == 1
    assert "template-path-match" in templated[0]["reasons"]
    assert nodes[templated[0]["target"]]["attributes"]["path"] == "/orders/{orderId}"
    # An unknown base variable is not evaluated or promoted to service ownership.
    requests = [node for node in nodes.values() if node["type"] == "http_request"]
    assert any(node["attributes"]["url"].startswith("https://other.") for node in requests)


def test_smartcart_react_and_spring_findings_produce_candidates_without_ownership_guess(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    spring = root / "spring"
    shutil.copytree(Path(__file__).parent / "fixtures" / "spring-boot-repo", spring)
    assert invoke(root, capsys, "add", "spring", "--id", "spring")[0] == 0
    ir = scan(root, capsys)
    nodes = {node["id"]: node for node in ir["nodes"]}
    spring_candidates = [
        item for item in ir["candidates"] if nodes[item["target"]]["attributes"]["repository_id"] == "spring"
    ]
    assert spring_candidates
    assert all(not item["attributes"]["origin_supported"] and item["confidence"] <= 0.65 for item in spring_candidates)
    # A client host alone cannot eliminate a method/path match whose backend
    # has no origin evidence. Even the "other" host stays a low-confidence candidate.
    assert any("other.example.invalid" in nodes[item["source"]]["attributes"]["url"] for item in spring_candidates)


def test_candidate_ids_stay_stable_when_evidence_revisions_and_scores_change(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    first = scan(root, capsys)
    assert scan(root, capsys) == first
    app = root / "frontend" / "src" / "App.tsx"
    app.write_text("// Added a comment, not a new request.\n" + app.read_text(encoding="utf-8"), encoding="utf-8")
    second = scan(root, capsys)
    assert [item["id"] for item in first["candidates"]] == [item["id"] for item in second["candidates"]]
    assert first["candidates"][0]["snapshots"] != second["candidates"][0]["snapshots"]
    assert first["candidates"][0]["evidence_ids"] != second["candidates"][0]["evidence_ids"]
    changed = copy.deepcopy(second)
    for node in changed["nodes"]:
        if node["type"] == "endpoint":
            node["attributes"]["servers"] = ["/api"]
    rescored = discover_candidates(changed)
    before = {item["id"]: item for item in second["candidates"]}
    assert any(item["confidence"] != before[item["id"]]["confidence"] for item in rescored if item["id"] in before)


def test_urls_are_sanitized_before_persistence_and_generation(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    ir = scan(root, capsys)
    assert invoke(root, capsys, "generate")[0] == 0
    assert invoke(root, capsys, "validate")[0] == 0
    markdown = (root / "docs" / "docs" / "generated" / "architecture.md").read_text(encoding="utf-8")
    assert "Cross-repository candidates (unconfirmed)" in markdown
    assert "not authoritative" in markdown
    for candidate in ir["candidates"]:
        assert candidate["id"] in markdown
    for secret in ("CredentialSecret", "QuerySecret", "FragmentSecret", "alice:"):
        assert secret not in json.dumps(ir)
        assert secret not in markdown
        assert secret not in (root / ".repodocs" / "ir.json").read_text(encoding="utf-8")
    # No-op generation stays no-op; IDs and artifacts are deterministic.
    code, response = invoke(root, capsys, "generate")
    assert code == 0 and response["data"]["changed"] is False


@pytest.mark.parametrize("field, value", [
    ("status", "confirmed"),
    ("classification", "observed"),
    ("confidence", 1),
    ("id", "candidate:bogus"),
    ("target", "node:missing"),
    ("evidence_ids", []),
    ("evidence_ids", [[]]),
    ("source", []),
    ("reasons", [{}]),
    ("attributes", {"method": "GET", "path": "/orders?secret=secret", "origin_supported": False}),
    ("snapshots", {}),
])
def test_candidate_validation_rejects_invalid_metadata(tmp_path, capsys, field, value) -> None:
    ir = scan(project(tmp_path, capsys), capsys)
    ir["candidates"][0][field] = value
    with pytest.raises(ValidationError):
        validate_ir(ir)


def test_pinned_candidates_bind_to_both_exact_commits(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    pins = {repo: init_git_repository(root / repo) for repo in ("frontend", "backend", "competing")}
    args = ["scan", "--snapshot-mode", "pinned"]
    for repo, revision in pins.items():
        args.extend(["--pin", f"{repo}={revision}"])
    code, response = invoke(root, capsys, *args)
    assert code == 0, response
    ir = read_ir(root)
    for candidate in ir["candidates"]:
        for repo, snapshot in candidate["snapshots"].items():
            assert snapshot == {"revision_kind": "commit", "revision": pins[repo]}
    assert_valid("system-ir-1.0.json", ir)
    validate_ir(ir)
