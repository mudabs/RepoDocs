"""Persisted files and CLI responses must conform to the published JSON schemas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from conftest import copy_fixture, init_git_repository, invoke, read_ir, read_manifest


SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def assert_valid(name: str, instance: object) -> None:
    errors = sorted(validator(name).iter_errors(instance), key=lambda error: list(error.absolute_path))
    assert not errors, "\n".join(f"{list(error.absolute_path)}: {error.message}" for error in errors)


@pytest.mark.parametrize("name", sorted(path.name for path in SCHEMAS.glob("*.json")))
def test_schema_files_are_valid_drafts(name: str) -> None:
    validator(name)


def test_persisted_files_and_responses_match_schemas(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    init_git_repository(copy_fixture(root / "backend"))
    (root / "docs-repo").mkdir()

    responses = []
    for arguments in (
        ("init", "SmartCart"),
        ("add", "backend", "--id", "backend"),
        ("add", "docs-repo", "--id", "docs", "--role", "destination"),
        ("scan",),
        ("generate",),
        ("validate",),
        ("scan", "--snapshot-mode", "pinned"),
    ):
        code, payload = invoke(root, capsys, *arguments)
        assert code == 0, (arguments, payload)
        responses.append(payload)

    config = json.loads((root / "repodocs.json").read_text(encoding="utf-8"))
    assert_valid("project-config-1.0.json", config)
    assert_valid("system-ir-1.0.json", read_ir(root))
    assert_valid("artifact-manifest-1.0.json", read_manifest(root))
    for payload in responses:
        assert_valid("cli-response-1.0.json", payload)


def test_error_responses_match_schema(tmp_path: Path, capsys) -> None:
    code, payload = invoke(tmp_path, capsys, "generate")
    assert code == 2
    assert_valid("cli-response-1.0.json", payload)


def test_non_git_snapshot_matches_schema(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    copy_fixture(root / "backend")
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    assert_valid("system-ir-1.0.json", read_ir(root))
    assert invoke(root, capsys, "generate")[0] == 0
    assert_valid("artifact-manifest-1.0.json", read_manifest(root))
