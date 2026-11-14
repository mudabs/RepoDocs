"""Destination repository configuration and ownership."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import copy_fixture, git, init_git_repository, invoke, read_ir, read_manifest


def project_with_sources(tmp_path: Path, capsys) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    copy_fixture(root / "backend")
    (root / "frontend").mkdir()
    (root / "frontend" / "app.tsx").write_text("export default 1;\n", encoding="utf-8")
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--id", "backend")[0] == 0
    assert invoke(root, capsys, "add", "frontend", "--id", "frontend")[0] == 0
    return root


def test_generate_fails_without_destination(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0

    for command in ("generate", "validate"):
        code, payload = invoke(root, capsys, command)
        assert code == 2, command
        assert payload["ok"] is False
        assert payload["schema_version"] == "1.0"
        assert "No documentation destination is configured" in payload["errors"][0]


def test_separate_destination_repository(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    docs = root / "docs-repo"
    docs.mkdir()
    code, payload = invoke(root, capsys, "add", "docs-repo", "--id", "docs", "--role", "destination")
    assert code == 0
    assert payload["data"]["role"] == "destination"
    assert payload["data"]["destination"] is True

    config = json.loads((root / "repodocs.json").read_text(encoding="utf-8"))
    assert config["documentation"]["destination"] == {"repository_id": "docs"}
    assert [item["role"] for item in config["repositories"]] == ["source", "destination", "source"]

    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)
    assert sorted(item["repository_id"] for item in ir["snapshots"]) == ["backend", "frontend"]
    assert not any(node["type"] == "repository" and node["name"] == "docs" for node in ir["nodes"])

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert payload["data"]["destination"]["repository_id"] == "docs"
    assert (docs / "docs" / "generated" / "architecture.md").exists()
    assert not (root / "docs" / "generated").exists()
    assert not (root / "backend" / "docs" / "generated").exists()

    manifest = read_manifest(root)
    assert manifest["ownership"] == "repodocs-generated"
    assert manifest["destination"]["repository_id"] == "docs"
    assert manifest["destination"]["role"] == "destination"
    assert manifest["destination"]["path"] == "docs-repo"
    assert manifest["destination"]["output_directory"] == "docs/generated"
    assert manifest["destination"]["vcs"] is None
    assert manifest["destination"]["working_tree_state"] == "non-git"

    code, payload = invoke(root, capsys, "validate")
    assert code == 0
    assert payload["data"]["destination"] == "docs"


def test_only_one_destination_is_allowed(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    (root / "docs-a").mkdir()
    (root / "docs-b").mkdir()
    assert invoke(root, capsys, "add", "docs-a", "--id", "docs-a", "--role", "destination")[0] == 0

    code, payload = invoke(root, capsys, "add", "docs-b", "--id", "docs-b", "--role", "destination")
    assert code == 2
    assert "already configured: docs-a" in payload["errors"][0]

    code, payload = invoke(root, capsys, "add", "docs-b", "--id", "docs-b-source", "--destination")
    assert code == 2
    assert "already configured: docs-a" in payload["errors"][0]


def test_missing_destination_path_fails_with_json_error(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    (root / "docs-repo").mkdir()
    assert invoke(root, capsys, "add", "docs-repo", "--id", "docs", "--role", "destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    (root / "docs-repo").rmdir()

    code, payload = invoke(root, capsys, "generate")
    assert code == 2
    assert payload["ok"] is False
    assert "destination path is not a directory" in payload["errors"][0]


def test_unknown_destination_reference_is_rejected_on_load(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    config_path = root / "repodocs.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["documentation"]["destination"] = {"repository_id": "ghost"}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    code, payload = invoke(root, capsys, "scan")
    assert code == 2
    assert "unknown repository: ghost" in payload["errors"][0]


def test_destination_role_requires_matching_destination_entry(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    (root / "docs-repo").mkdir()
    assert invoke(root, capsys, "add", "docs-repo", "--id", "docs", "--role", "destination")[0] == 0
    config_path = root / "repodocs.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    del config["documentation"]["destination"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    code, payload = invoke(root, capsys, "scan")
    assert code == 2
    assert "has role destination but documentation.destination is None" in payload["errors"][0]


def test_output_directory_must_stay_inside_destination(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    (root / "docs-repo").mkdir()
    assert invoke(root, capsys, "add", "docs-repo", "--id", "docs", "--role", "destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    config_path = root / "repodocs.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    for bad in ("../escaped", ".", str(tmp_path / "absolute")):
        config["documentation"]["output_directory"] = bad
        config_path.write_text(json.dumps(config), encoding="utf-8")
        code, payload = invoke(root, capsys, "generate")
        assert code == 2, bad
        assert "output_directory" in payload["errors"][0]
        assert not (root / "escaped").exists()


def test_manifest_destination_must_match_configuration(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    (root / "docs-repo").mkdir()
    assert invoke(root, capsys, "add", "docs-repo", "--id", "docs", "--role", "destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0

    config_path = root / "repodocs.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repositories"] = [item for item in config["repositories"] if item["id"] != "docs"]
    config["documentation"]["destination"] = {"repository_id": "backend"}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    code, payload = invoke(root, capsys, "validate")
    assert code == 3
    assert "does not match the configured destination" in payload["errors"][0]

    code, payload = invoke(root, capsys, "generate")
    assert code == 4
    assert "belongs to destination 'docs'" in payload["errors"][0]
    assert not (root / "backend" / "docs" / "generated").exists()


def test_pinned_destination_must_match_checkout(tmp_path: Path, capsys) -> None:
    root = project_with_sources(tmp_path, capsys)
    docs = root / "docs-repo"
    sha = init_git_repository(docs)
    stale = "f" * 40
    assert invoke(root, capsys, "add", "docs-repo", "--id", "docs", "--role", "destination", "--pinned-revision", stale)[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0

    code, payload = invoke(root, capsys, "generate")
    assert code == 6
    assert "stale destination checkout" in payload["errors"][0]
    assert not (docs / "docs").exists()

    config_path = root / "repodocs.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for item in config["repositories"]:
        if item["id"] == "docs":
            item["pinned_revision"] = sha
    config_path.write_text(json.dumps(config), encoding="utf-8")

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    manifest = read_manifest(root)
    assert manifest["destination"]["vcs"] == "git"
    assert manifest["destination"]["git_revision"] == sha
    assert manifest["destination"]["pinned_revision"] == sha
    assert manifest["destination"]["working_tree_state"] == "clean"

    git(docs, "add", "-A")
    git(docs, "commit", "-q", "-m", "docs")
    code, payload = invoke(root, capsys, "generate")
    assert code == 6
