"""Local and pinned snapshot modes across clean, dirty, detached, non-Git, and changed trees."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import copy_fixture, git, init_git_repository, invoke, read_ir


def project_with_repository(tmp_path: Path, capsys, *, make_git: bool) -> tuple[Path, Path, str | None]:
    root = tmp_path / "project"
    root.mkdir()
    repository = copy_fixture(root / "backend")
    sha = init_git_repository(repository) if make_git else None
    assert invoke(root, capsys, "init", "Snapshot fixture")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--id", "backend", "--destination")[0] == 0
    return root, repository, sha


def snapshot(root: Path) -> dict:
    ir = read_ir(root)
    assert len(ir["snapshots"]) == 1
    return ir["snapshots"][0]


def test_local_clean_tree_resolves_to_commit(tmp_path: Path, capsys) -> None:
    root, _, sha = project_with_repository(tmp_path, capsys, make_git=True)

    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    item = snapshot(root)
    assert item["snapshot_mode"] == "local"
    assert item["vcs"] == "git"
    assert item["git_revision"] == sha
    assert item["git_branch"] == "main"
    assert item["head_state"] == "attached"
    assert item["git_path_prefix"] == ""
    assert item["working_tree_state"] == "clean"
    assert item["revision_kind"] == "commit"
    assert item["revision"] == sha
    assert payload["data"]["snapshots"][0]["revision"] == sha
    ir = read_ir(root)
    assert ir["evidence"]
    assert all(evidence["revision"] == sha and evidence["revision_kind"] == "commit" for evidence in ir["evidence"])


def test_local_dirty_tree_resolves_to_content_fingerprint(tmp_path: Path, capsys) -> None:
    root, repository, sha = project_with_repository(tmp_path, capsys, make_git=True)
    (repository / "src" / "main.py").write_text("value = 2\n", encoding="utf-8")
    (repository / "untracked.py").write_text("x = 1\n", encoding="utf-8")

    code, _ = invoke(root, capsys, "scan")
    assert code == 0
    item = snapshot(root)
    assert item["working_tree_state"] == "dirty"
    assert item["git_revision"] == sha
    assert item["git_status_fingerprint"]
    assert item["revision_kind"] == "content"
    assert item["revision"] == item["content_fingerprint"]
    assert item["revision"] != sha
    ir = read_ir(root)
    assert all(evidence["revision"] == item["content_fingerprint"] for evidence in ir["evidence"])
    assert any(file["path"] == "untracked.py" for file in item["files"])


def test_local_detached_head_is_recorded(tmp_path: Path, capsys) -> None:
    root, repository, sha = project_with_repository(tmp_path, capsys, make_git=True)
    git(repository, "checkout", "-q", "--detach", sha)

    assert invoke(root, capsys, "scan")[0] == 0
    item = snapshot(root)
    assert item["head_state"] == "detached"
    assert item["git_branch"] is None
    assert item["revision_kind"] == "commit"
    assert item["revision"] == sha


def test_local_non_git_tree_resolves_to_content(tmp_path: Path, capsys) -> None:
    root, _, _ = project_with_repository(tmp_path, capsys, make_git=False)

    assert invoke(root, capsys, "scan")[0] == 0
    item = snapshot(root)
    assert item["vcs"] is None
    assert item["git_revision"] is None
    assert item["head_state"] is None
    assert item["working_tree_state"] == "non-git"
    assert item["revision_kind"] == "content"
    assert item["revision"] == item["content_fingerprint"]


def test_changed_content_changes_fingerprint_and_evidence(tmp_path: Path, capsys) -> None:
    root, repository, _ = project_with_repository(tmp_path, capsys, make_git=False)
    assert invoke(root, capsys, "scan")[0] == 0
    before = read_ir(root)

    (repository / "src" / "main.py").write_text("value = 'changed'\n", encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0
    after = read_ir(root)

    assert before["snapshots"][0]["content_fingerprint"] != after["snapshots"][0]["content_fingerprint"]
    changed_before = next(item for item in before["evidence"] if item["path"] == "src/main.py")
    changed_after = next(item for item in after["evidence"] if item["path"] == "src/main.py")
    assert changed_before["id"] != changed_after["id"]
    assert changed_before["content_hash"] != changed_after["content_hash"]
    unchanged_before = {item["id"] for item in before["evidence"] if item["path"] != "src/main.py"}
    unchanged_after = {item["id"] for item in after["evidence"] if item["path"] != "src/main.py"}
    assert unchanged_before == unchanged_after


def test_pinned_mode_accepts_clean_exact_commit(tmp_path: Path, capsys) -> None:
    root, _, sha = project_with_repository(tmp_path, capsys, make_git=True)

    code, payload = invoke(root, capsys, "scan", "--snapshot-mode", "pinned", "--pin", f"backend={sha}")
    assert code == 0
    assert payload["data"]["snapshot_mode"] == "pinned"
    item = snapshot(root)
    assert item["snapshot_mode"] == "pinned"
    assert item["pinned_revision"] == sha
    assert item["revision_kind"] == "commit"
    assert item["revision"] == sha
    assert read_ir(root)["provenance"]["snapshot_mode"] == "pinned"


def test_pinned_mode_accepts_detached_checkout(tmp_path: Path, capsys) -> None:
    root, repository, sha = project_with_repository(tmp_path, capsys, make_git=True)
    git(repository, "checkout", "-q", "--detach", sha)

    assert invoke(root, capsys, "scan", "--snapshot-mode", "pinned")[0] == 0
    assert snapshot(root)["head_state"] == "detached"


def test_pinned_mode_rejects_dirty_tree(tmp_path: Path, capsys) -> None:
    root, repository, _ = project_with_repository(tmp_path, capsys, make_git=True)
    (repository / "src" / "main.py").write_text("value = 2\n", encoding="utf-8")

    code, payload = invoke(root, capsys, "scan", "--snapshot-mode", "pinned")
    assert code == 6
    assert payload["ok"] is False
    assert "clean working tree" in payload["errors"][0]
    assert not (root / ".repodocs" / "ir.json").exists()


def test_pinned_mode_rejects_untracked_files(tmp_path: Path, capsys) -> None:
    root, repository, _ = project_with_repository(tmp_path, capsys, make_git=True)
    (repository / "new.py").write_text("x = 1\n", encoding="utf-8")

    code, payload = invoke(root, capsys, "scan", "--snapshot-mode", "pinned")
    assert code == 6
    assert "found dirty" in payload["errors"][0]


def test_pinned_mode_rejects_non_git_tree(tmp_path: Path, capsys) -> None:
    root, _, _ = project_with_repository(tmp_path, capsys, make_git=False)

    code, payload = invoke(root, capsys, "scan", "--snapshot-mode", "pinned")
    assert code == 6
    assert "require a Git repository" in payload["errors"][0]


def test_stale_pin_is_rejected_in_both_modes(tmp_path: Path, capsys) -> None:
    root, repository, first = project_with_repository(tmp_path, capsys, make_git=True)
    (repository / "src" / "main.py").write_text("value = 2\n", encoding="utf-8")
    git(repository, "commit", "-q", "-am", "second")
    second = git(repository, "rev-parse", "HEAD")
    assert first != second

    for mode in ("local", "pinned"):
        code, payload = invoke(root, capsys, "scan", "--snapshot-mode", mode, "--pin", f"backend={first}")
        assert code == 6, mode
        assert "stale checkout" in payload["errors"][0]
        assert first in payload["errors"][0] and second in payload["errors"][0]


def test_ambiguous_pin_is_rejected(tmp_path: Path, capsys) -> None:
    root, _, sha = project_with_repository(tmp_path, capsys, make_git=True)

    code, payload = invoke(root, capsys, "scan", "--pin", f"backend={sha[:7]}")
    assert code == 6
    assert "ambiguous pinned revision" in payload["errors"][0]

    code, payload = invoke(root, capsys, "scan", "--pin", "backend")
    assert code == 2
    assert "REPOSITORY_ID=COMMIT_SHA" in payload["errors"][0]

    code, payload = invoke(root, capsys, "scan", "--pin", f"frontend={sha}")
    assert code == 2
    assert "Unknown repository ID" in payload["errors"][0]


def test_configured_pin_applies_without_cli_override(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repository = copy_fixture(root / "backend")
    sha = init_git_repository(repository)
    assert invoke(root, capsys, "init", "Pinned fixture")[0] == 0
    stale = "0" * 40
    code, payload = invoke(root, capsys, "add", "backend", "--destination", "--pinned-revision", stale)
    assert code == 0
    assert payload["data"]["pinned_revision"] == stale

    code, payload = invoke(root, capsys, "scan", "--snapshot-mode", "pinned")
    assert code == 6
    assert "stale checkout" in payload["errors"][0]

    code, _ = invoke(root, capsys, "scan", "--snapshot-mode", "pinned", "--pin", f"backend={sha}")
    assert code == 0
    assert snapshot(root)["pinned_revision"] == sha


def test_registered_subdirectory_of_repository_tracks_commit_and_subtree_status(tmp_path: Path, capsys) -> None:
    monorepo = tmp_path / "monorepo"
    copy_fixture(monorepo / "services" / "backend")
    (monorepo / "services" / "frontend").mkdir()
    (monorepo / "services" / "frontend" / "app.tsx").write_text("export default 1;\n", encoding="utf-8")
    sha = init_git_repository(monorepo)
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "Monorepo fixture")[0] == 0
    assert invoke(root, capsys, "add", str(monorepo / "services" / "backend"), "--id", "backend", "--destination")[0] == 0

    assert invoke(root, capsys, "scan")[0] == 0
    item = snapshot(root)
    assert item["git_path_prefix"] == "services/backend"
    assert item["revision"] == sha
    assert item["revision_kind"] == "commit"

    (monorepo / "services" / "frontend" / "app.tsx").write_text("export default 2;\n", encoding="utf-8")
    assert invoke(root, capsys, "scan", "--snapshot-mode", "pinned", "--pin", f"backend={sha}")[0] == 0
    assert snapshot(root)["working_tree_state"] == "clean"

    (monorepo / "services" / "backend" / "src" / "main.py").write_text("value = 2\n", encoding="utf-8")
    code, _ = invoke(root, capsys, "scan", "--snapshot-mode", "pinned")
    assert code == 6


@pytest.mark.parametrize("mode", ["local", "pinned"])
def test_scan_output_is_reproducible(tmp_path: Path, capsys, mode: str) -> None:
    root, _, _ = project_with_repository(tmp_path, capsys, make_git=True)
    assert invoke(root, capsys, "scan", "--snapshot-mode", mode)[0] == 0
    first = (root / ".repodocs" / "ir.json").read_text(encoding="utf-8")
    assert invoke(root, capsys, "scan", "--snapshot-mode", mode)[0] == 0
    second = (root / ".repodocs" / "ir.json").read_text(encoding="utf-8")
    assert first == second
