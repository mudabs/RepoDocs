from __future__ import annotations

import json
import subprocess
from pathlib import Path

from repodocs.cli import main
from repodocs.scanner import discover_files


def invoke(root: Path, capsys, *arguments: str) -> tuple[int, dict]:
    code = main(["--root", str(root), *arguments])
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def test_discovery_excludes_generated_and_dependency_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('fixture')\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    (tmp_path / ".repodocs").mkdir()
    (tmp_path / ".repodocs" / "ignored.json").write_text("{}", encoding="utf-8")

    files, warnings, _ = discover_files(tmp_path)

    assert [item.path for item in files] == ["src/main.py"]
    assert warnings == []


def test_scan_records_dirty_git_working_tree(tmp_path: Path, capsys) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    git_environment = {
        "GIT_AUTHOR_NAME": "RepoDocs Test",
        "GIT_AUTHOR_EMAIL": "repodocs-test@example.invalid",
        "GIT_COMMITTER_NAME": "RepoDocs Test",
        "GIT_COMMITTER_EMAIL": "repodocs-test@example.invalid",
    }
    import os

    environment = os.environ.copy()
    environment.update(git_environment)
    subprocess.run(["git", "add", "main.py"], cwd=repository, check=True, env=environment)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, env=environment, capture_output=True)
    source.write_text("value = 2\n", encoding="utf-8")

    code, payload = invoke(tmp_path, capsys, "init", "Dirty fixture")
    assert code == 0
    code, payload = invoke(tmp_path, capsys, "add", "repo", "--id", "repo")
    assert code == 0
    code, payload = invoke(tmp_path, capsys, "scan")
    assert code == 0

    ir = json.loads((tmp_path / ".repodocs" / "ir.json").read_text(encoding="utf-8"))
    snapshot = ir["snapshots"][0]
    assert snapshot["revision_kind"] == "git-working-tree"
    assert snapshot["working_tree_state"] == "dirty"
    assert len(snapshot["git_revision"]) == 40
    assert snapshot["content_fingerprint"]
