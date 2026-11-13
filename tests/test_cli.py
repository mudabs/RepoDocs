from __future__ import annotations

import json
import shutil
from pathlib import Path

from repodocs.cli import main


FIXTURE = Path(__file__).parent / "fixtures" / "basic-repo"


def invoke(root: Path, capsys, *arguments: str) -> tuple[int, dict]:
    code = main(["--root", str(root), *arguments])
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def prepare_project(tmp_path: Path, capsys) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(FIXTURE, root / "backend")
    code, payload = invoke(root, capsys, "init", "SmartCart")
    assert code == 0
    assert payload["ok"] is True
    code, payload = invoke(root, capsys, "add", "backend", "--id", "backend")
    assert code == 0
    assert payload["ok"] is True
    return root


def test_cli_end_to_end_and_noop_generation(tmp_path: Path, capsys) -> None:
    root = prepare_project(tmp_path, capsys)

    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["repositories"] == 1

    ir = json.loads((root / ".repodocs" / "ir.json").read_text(encoding="utf-8"))
    assert ir["ir_version"] == "1.0"
    assert any(node["type"] == "language" and node["name"] == "Python" for node in ir["nodes"])
    assert any(node["type"] == "dependency" and node["name"] == "react" for node in ir["nodes"])
    assert all(claim["evidence_ids"] for claim in ir["claims"])

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert payload["data"]["changed"] is True
    assert (root / "docs" / "generated" / "architecture.md").exists()
    assert (root / "docs" / "generated" / "architecture.mmd").read_text(encoding="utf-8").startswith("flowchart TD")

    code, payload = invoke(root, capsys, "validate")
    assert code == 0
    assert payload["ok"] is True

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert payload["data"]["changed"] is False


def test_unexpected_generated_edit_fails_closed(tmp_path: Path, capsys) -> None:
    root = prepare_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0

    output = root / "docs" / "generated" / "architecture.md"
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
    assert "Project configuration not found" in payload["errors"][0]
