"""Scanner safety boundaries, manifest dependency parsing, and the module entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import repodocs
from repodocs import scanner
from repodocs.errors import ScanError
from repodocs.scanner import discover_files

from conftest import invoke, read_ir


def test_binary_files_are_skipped_with_a_warning(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"\x89PNG\x00\x00binary")

    files, warnings, _ = discover_files(tmp_path)

    assert [item.path for item in files] == ["main.py"]
    assert len(warnings) == 1 and "Skipped binary file" in warnings[0]


def test_oversized_files_are_skipped_with_a_warning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scanner, "MAX_FILE_BYTES", 16)
    (tmp_path / "small.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "large.py").write_text("a = 1\n" * 10, encoding="utf-8")

    files, warnings, _ = discover_files(tmp_path)

    assert [item.path for item in files] == ["small.py"]
    assert len(warnings) == 1 and "over size limit" in warnings[0]


def test_file_and_total_size_limits_fail_the_scan(tmp_path: Path, monkeypatch) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("value = 1\n", encoding="utf-8")

    monkeypatch.setattr(scanner, "MAX_FILES", 2)
    with pytest.raises(ScanError, match="File limit exceeded"):
        discover_files(tmp_path)

    monkeypatch.setattr(scanner, "MAX_FILES", 100)
    monkeypatch.setattr(scanner, "MAX_TOTAL_BYTES", 15)
    with pytest.raises(ScanError, match="Total readable file limit exceeded"):
        discover_files(tmp_path)


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("token = 'outside'\n", encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_text("value = 1\n", encoding="utf-8")
    try:
        os.symlink(outside, repository / "linked-dir", target_is_directory=True)
        os.symlink(outside / "secret.py", repository / "linked.py")
    except OSError:
        pytest.skip("this platform or account cannot create symlinks")

    files, warnings, _ = discover_files(repository)

    assert [item.path for item in files] == ["main.py"]
    assert len(warnings) == 2 and all("Skipped symlink" in warning for warning in warnings)


def test_missing_repository_path_is_a_scan_error(tmp_path: Path) -> None:
    with pytest.raises(ScanError, match="not a directory"):
        discover_files(tmp_path / "missing")


def test_python_manifest_dependencies_are_extracted(tmp_path: Path, capsys) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    (repository / "requirements.txt").write_text(
        "# comment\n-r other.txt\nrequests>=2.31\nflask[async]==3.0.0\n\n", encoding="utf-8"
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "service"\ndependencies = ["httpx>=0.27", "pydantic"]\n', encoding="utf-8"
    )
    assert invoke(tmp_path, capsys, "init", "Service")[0] == 0
    assert invoke(tmp_path, capsys, "add", "service", "--destination")[0] == 0
    assert invoke(tmp_path, capsys, "scan")[0] == 0

    ir = read_ir(tmp_path)
    dependencies = {node["name"]: node for node in ir["nodes"] if node["type"] == "dependency"}
    assert {"requests", "flask", "pydantic"} <= set(dependencies)
    assert any(name.startswith("httpx") for name in dependencies)
    assert dependencies["requests"]["attributes"]["source_manifest"] == "requirements.txt"
    assert all(node["evidence_ids"] for node in dependencies.values())


def test_malformed_manifest_does_not_fail_the_scan(tmp_path: Path, capsys) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    (repository / "package.json").write_text("{ not json", encoding="utf-8")
    (repository / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    assert invoke(tmp_path, capsys, "init", "Service")[0] == 0
    assert invoke(tmp_path, capsys, "add", "service", "--destination")[0] == 0

    code, payload = invoke(tmp_path, capsys, "scan")

    assert code == 0, payload
    ir = read_ir(tmp_path)
    assert not [node for node in ir["nodes"] if node["type"] == "dependency"]
    assert {node["name"] for node in ir["nodes"] if node["type"] == "manifest"} == {"package.json", "pyproject.toml"}


def test_module_entry_point_reports_the_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "repodocs.cli", "--version"], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == repodocs.__version__
