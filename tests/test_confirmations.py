"""Confirmation, rejection, staleness, and the required-confirmation exit code."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import git, init_git_repository, invoke, read_ir
from repodocs.validation import validate_ir
from test_correlation import project
from test_schemas import assert_valid


def decisions_file(root: Path) -> dict:
    return json.loads((root / "repodocs.confirmations.json").read_text(encoding="utf-8"))


def scanned(tmp_path, capsys) -> tuple[Path, list[str]]:
    root = project(tmp_path, capsys)
    code, response = invoke(root, capsys, "scan")
    assert code == 0, response
    ids = [item["id"] for item in read_ir(root)["candidates"]]
    assert len(ids) >= 3
    assert response["data"]["candidate_states"] == {
        "candidate": len(ids), "confirmed": 0, "rejected": 0, "stale": 0,
    }
    return root, ids


def test_list_unresolved_candidates(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)

    code, response = invoke(root, capsys, "candidates")
    assert code == 0
    assert_valid("cli-response-1.0.json", response)
    data = response["data"]
    assert data["status"] == "unresolved"
    assert [item["id"] for item in data["candidates"]] == ids
    first = data["candidates"][0]
    assert first["status"] == "candidate"
    assert first["source"]["repository_id"] == "frontend"
    assert first["target"]["repository_id"] in {"backend", "competing"}
    assert set(first) >= {"confidence", "reasons", "method", "path", "snapshots"}
    assert data["orphaned_decisions"] == []

    code, response = invoke(root, capsys, "candidates", "--status", "confirmed")
    assert code == 0
    assert response["data"]["candidates"] == []


def test_candidates_requires_a_scan(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    code, response = invoke(root, capsys, "candidates")
    assert code == 3
    assert "run scan first" in response["errors"][0]


def test_confirm_and_reject_are_persisted_with_revisions(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)
    before = {item["id"]: item for item in read_ir(root)["candidates"]}

    code, response = invoke(root, capsys, "confirm", ids[0], "--note", "Verified against the gateway config")
    assert code == 0
    assert_valid("cli-response-1.0.json", response)
    assert response["data"]["decision"] == "confirmed"
    assert response["data"]["candidate_states"]["confirmed"] == 1
    code, response = invoke(root, capsys, "reject", ids[1], ids[2])
    assert code == 0
    assert response["data"]["candidate_states"]["rejected"] == 2

    stored = decisions_file(root)
    assert_valid("confirmations-1.0.json", stored)
    records = {item["candidate_id"]: item for item in stored["decisions"]}
    assert records[ids[0]]["decision"] == "confirmed"
    assert records[ids[0]]["note"] == "Verified against the gateway config"
    assert records[ids[0]]["snapshots"] == before[ids[0]]["snapshots"]
    assert records[ids[0]]["evidence_ids"] == sorted(before[ids[0]]["evidence_ids"])
    assert "note" not in records[ids[1]]

    ir = read_ir(root)
    assert_valid("system-ir-1.0.json", ir)
    validate_ir(ir)
    states = {item["id"]: item for item in ir["candidates"]}
    assert states[ids[0]]["status"] == "confirmed"
    assert states[ids[0]]["confirmation"]["snapshots"] == before[ids[0]]["snapshots"]
    assert states[ids[1]]["status"] == states[ids[2]]["status"] == "rejected"
    # A decision never turns an inferred match into an observed relationship.
    assert all(item["classification"] == "inferred" for item in ir["candidates"])
    assert ids[0] not in {edge["id"] for edge in ir["relationships"]}

    # Decisions survive a rescan of unchanged sources.
    assert invoke(root, capsys, "scan")[0] == 0
    assert read_ir(root)["candidates"] == ir["candidates"]

    code, response = invoke(root, capsys, "candidates")
    assert ids[0] not in [item["id"] for item in response["data"]["candidates"]]
    code, response = invoke(root, capsys, "candidates", "--status", "all")
    assert len(response["data"]["candidates"]) == len(ids)


def test_a_later_decision_overrides_an_earlier_one(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "confirm", ids[0])[0] == 0
    assert invoke(root, capsys, "reject", ids[0], "--note", "Wrong service")[0] == 0

    records = decisions_file(root)["decisions"]
    assert len(records) == 1
    assert records[0]["decision"] == "rejected" and records[0]["note"] == "Wrong service"
    assert next(item for item in read_ir(root)["candidates"] if item["id"] == ids[0])["status"] == "rejected"


def test_unknown_candidate_is_rejected_without_partial_write(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)

    code, response = invoke(root, capsys, "confirm", ids[0], "candidate:missing")
    assert code == 2
    assert "Unknown candidate ID: candidate:missing" in response["errors"][0]
    assert not (root / "repodocs.confirmations.json").exists()
    assert all(item["status"] == "candidate" for item in read_ir(root)["candidates"])


def test_changed_source_makes_a_decision_stale(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "confirm", *ids)[0] == 0
    decided = {item["id"]: item for item in read_ir(root)["candidates"]}

    app = root / "frontend" / "src" / "App.tsx"
    app.write_text("// Changed after confirmation.\n" + app.read_text(encoding="utf-8"), encoding="utf-8")
    code, response = invoke(root, capsys, "scan")
    assert code == 0
    assert response["data"]["candidate_states"]["stale"] > 0

    ir = read_ir(root)
    validate_ir(ir)
    assert_valid("system-ir-1.0.json", ir)
    stale = [item for item in ir["candidates"] if item["status"] == "stale"]
    assert stale
    for item in stale:
        # The old decision and the revisions it was made against stay visible.
        assert item["confirmation"]["decision"] == "confirmed"
        assert item["confirmation"]["snapshots"] == decided[item["id"]]["snapshots"]
        assert item["confirmation"]["snapshots"] != item["snapshots"]

    code, response = invoke(root, capsys, "candidates")
    assert {item["id"] for item in response["data"]["candidates"]} == {item["id"] for item in stale}

    # Deciding again binds the decision to the new evidence and revisions.
    assert invoke(root, capsys, "confirm", *[item["id"] for item in stale])[0] == 0
    refreshed = read_ir(root)
    assert all(item["status"] == "confirmed" for item in refreshed["candidates"])
    assert all(item["confirmation"]["snapshots"] == item["snapshots"] for item in refreshed["candidates"])


def test_removed_candidate_leaves_an_orphaned_decision(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "confirm", *ids)[0] == 0
    (root / "frontend" / "src" / "App.tsx").write_text("export const nothing = 1;\n", encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0

    code, response = invoke(root, capsys, "candidates", "--status", "all")
    assert code == 0
    assert response["data"]["candidates"] == []
    assert response["data"]["orphaned_decisions"] == sorted(ids)


def test_validate_exits_5_until_every_candidate_is_decided(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "generate")[0] == 0

    code, response = invoke(root, capsys, "validate")
    assert code == 5
    assert response["ok"] is False
    assert_valid("cli-response-1.0.json", response)
    assert f"{len(ids)} cross-repository candidate(s) require confirmation" in response["errors"][0]

    code, response = invoke(root, capsys, "validate", "--allow-unresolved")
    assert code == 0
    assert response["data"]["candidate_states"]["candidate"] == len(ids)

    assert invoke(root, capsys, "confirm", ids[0])[0] == 0
    assert invoke(root, capsys, "reject", *ids[1:])[0] == 0
    # The IR changed, so documentation must be regenerated before it validates.
    code, response = invoke(root, capsys, "validate")
    assert code == 3
    code, response = invoke(root, capsys, "generate")
    assert code == 0 and response["data"]["changed"] is True
    markdown = (root / "docs" / "docs" / "generated" / "architecture.md").read_text(encoding="utf-8")
    assert f"`{ids[0]}` (confirmed)" in markdown
    assert f"`{ids[1]}` (rejected)" in markdown

    code, response = invoke(root, capsys, "validate")
    assert code == 0, response
    assert response["data"]["candidate_states"] == {
        "candidate": 0, "confirmed": 1, "rejected": len(ids) - 1, "stale": 0,
    }


def test_stale_decisions_fail_pinned_ci_validation(tmp_path, capsys) -> None:
    root = project(tmp_path, capsys)
    repos = ("frontend", "backend", "competing")

    for repo in repos:
        init_git_repository(root / repo)

    def pinned_scan() -> None:
        args = ["scan", "--snapshot-mode", "pinned"]
        for repo in repos:
            args.extend(["--pin", f"{repo}={git(root / repo, 'rev-parse', 'HEAD')}"])
        code, response = invoke(root, capsys, *args)
        assert code == 0, response

    pinned_scan()
    ids = [item["id"] for item in read_ir(root)["candidates"]]
    assert invoke(root, capsys, "reject", *ids)[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    assert invoke(root, capsys, "validate")[0] == 0
    stored = decisions_file(root)["decisions"][0]
    assert all(item["revision_kind"] == "commit" for item in stored["snapshots"].values())

    client = root / "frontend" / "src" / "client.ts"
    client.write_text(client.read_text(encoding="utf-8") + "// new revision\n", encoding="utf-8")
    git(root / "frontend", "commit", "-q", "-am", "change client")
    pinned_scan()
    assert invoke(root, capsys, "generate")[0] == 0
    code, response = invoke(root, capsys, "validate")
    assert code == 5
    assert "stale" in response["errors"][0]


def test_hand_edited_candidate_state_is_detected(tmp_path, capsys) -> None:
    root, ids = scanned(tmp_path, capsys)
    assert invoke(root, capsys, "confirm", ids[0])[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    (root / "repodocs.confirmations.json").unlink()

    code, response = invoke(root, capsys, "validate", "--allow-unresolved")
    assert code == 3
    assert "do not match the recorded decisions" in response["errors"][0]


def test_invalid_confirmations_file_fails_with_json_error(tmp_path, capsys) -> None:
    root, _ = scanned(tmp_path, capsys)
    path = root / "repodocs.confirmations.json"
    for content in ("not json", '{"schema_version": "9"}', '{"schema_version": "1.0", "decisions": [{"decision": "maybe"}]}'):
        path.write_text(content, encoding="utf-8")
        code, response = invoke(root, capsys, "scan")
        assert code == 2, content
        assert response["ok"] is False
