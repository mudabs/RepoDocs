from __future__ import annotations

from pathlib import Path

from conftest import copy_fixture, invoke, read_ir


def prepare_project(tmp_path: Path, capsys) -> Path:
    """A single-repository project whose only source repository is also the destination."""

    root = tmp_path / "project"
    root.mkdir()
    copy_fixture(root / "backend")
    code, payload = invoke(root, capsys, "init", "SmartCart")
    assert code == 0
    assert payload["ok"] is True
    code, payload = invoke(root, capsys, "add", "backend", "--id", "backend", "--destination")
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["destination"] is True
    return root


def test_cli_end_to_end_and_noop_generation(tmp_path: Path, capsys) -> None:
    root = prepare_project(tmp_path, capsys)

    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["repositories"] == 1
    assert payload["data"]["snapshot_mode"] == "local"

    ir = read_ir(root)
    assert ir["ir_version"] == "1.0"
    assert ir["provenance"]["snapshot_mode"] == "local"
    assert any(node["type"] == "language" and node["name"] == "Python" for node in ir["nodes"])
    assert any(node["type"] == "dependency" and node["name"] == "react" for node in ir["nodes"])
    assert all(claim["evidence_ids"] for claim in ir["claims"])

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert payload["data"]["changed"] is True
    assert payload["data"]["destination"]["repository_id"] == "backend"
    output = root / "backend" / "docs" / "generated"
    assert (output / "architecture.md").exists()
    assert (output / "architecture.mmd").read_text(encoding="utf-8").startswith("flowchart TD")

    code, payload = invoke(root, capsys, "validate")
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["destination"] == "backend"

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert payload["data"]["changed"] is False


def test_rescan_after_generation_is_a_noop(tmp_path: Path, capsys) -> None:
    """Generated output inside a source repository must not feed back into the next scan."""

    root = prepare_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    first = read_ir(root)
    assert invoke(root, capsys, "generate")[0] == 0

    assert invoke(root, capsys, "scan")[0] == 0
    second = read_ir(root)
    assert first == second
    assert not any(item["path"].startswith("docs/generated/") for item in second["snapshots"][0]["files"])

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert payload["data"]["changed"] is False


def test_unexpected_generated_edit_fails_closed(tmp_path: Path, capsys) -> None:
    root = prepare_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0

    output = root / "backend" / "docs" / "generated" / "architecture.md"
    output.write_text(output.read_text(encoding="utf-8") + "\nManual edit.\n", encoding="utf-8")

    code, payload = invoke(root, capsys, "generate")
    assert code == 4
    assert payload["ok"] is False
    assert "edited unexpectedly" in payload["errors"][0]

    code, payload = invoke(root, capsys, "validate")
    assert code == 4
    assert payload["ok"] is False


def test_missing_project_configuration_is_machine_readable(tmp_path: Path, capsys) -> None:
    code, payload = invoke(tmp_path, capsys, "scan")
    assert code == 2
    assert payload["ok"] is False
    assert payload["schema_version"] == "1.0"
    assert "Project configuration not found" in payload["errors"][0]


def test_add_rejects_ambiguous_pinned_revision(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    copy_fixture(root / "backend")
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0

    code, payload = invoke(root, capsys, "add", "backend", "--pinned-revision", "abc123")
    assert code == 2
    assert "full 40-character" in payload["errors"][0]
