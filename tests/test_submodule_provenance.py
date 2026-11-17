"""Submodule and ignored-file content must never receive a commit's provenance it does not have."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import GIT_ENVIRONMENT, git, init_git_repository, invoke, read_ir


def sh(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), "-c", "protocol.file.allow=always", *arguments],
        check=True, capture_output=True, text=True, encoding="utf-8", env={**os.environ, **GIT_ENVIRONMENT},
    )
    return completed.stdout.strip()


def commit_file(repository: Path, name: str, text: str) -> None:
    (repository / name).write_text(text, encoding="utf-8")
    sh(repository, "add", "-A")
    sh(repository, "commit", "-q", "-m", f"change {name}")


class Tree:
    """A superproject whose submodule `sub` has its own submodule `deep`."""

    def __init__(self, base: Path) -> None:
        self.base = base
        inner = base / "inner"
        inner.mkdir()
        (inner / "inner.py").write_text("INNER = 1\n", encoding="utf-8")
        init_git_repository(inner)
        library = base / "library"
        library.mkdir()
        (library / "library.py").write_text("LIBRARY = 1\n", encoding="utf-8")
        init_git_repository(library)
        sh(library, "submodule", "add", "-q", str(inner), "deep")
        sh(library, "commit", "-q", "-m", "add deep")
        self.root = base / "backend"
        self.root.mkdir()
        (self.root / "app.py").write_text("APP = 1\n", encoding="utf-8")
        init_git_repository(self.root)
        sh(self.root, "submodule", "add", "-q", str(library), "sub")
        sh(self.root, "submodule", "update", "--init", "--recursive", "-q")
        sh(self.root, "commit", "-q", "-m", "add sub")
        self.sub = self.root / "sub"
        self.deep = self.sub / "deep"

    @property
    def head(self) -> str:
        return sh(self.root, "rev-parse", "HEAD")

    def configurations(self) -> list[Path]:
        """The repository configuration of the superproject, `sub`, and `deep`."""

        return [self.root / ".git" / "config", *sorted((self.root / ".git" / "modules").rglob("config"))]


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def scan(tmp_path: Path, capsys, repository: Path, *arguments: str) -> tuple[int, dict]:
    root = tmp_path / "project"
    if not root.exists():
        root.mkdir()
        assert invoke(root, capsys, "init", "p")[0] == 0
        assert invoke(root, capsys, "add", str(repository), "--id", "backend", "--destination")[0] == 0
    return invoke(root, capsys, "scan", *arguments)


def snapshot_of(tmp_path: Path) -> dict:
    return read_ir(tmp_path / "project")["snapshots"][0]


def pinned(tree: Tree) -> tuple[str, ...]:
    return "--snapshot-mode", "pinned", "--pin", f"backend={tree.head}"


def assert_content_provenance(tmp_path: Path) -> None:
    snapshot = snapshot_of(tmp_path)
    assert snapshot["working_tree_state"] == "dirty" and snapshot["revision_kind"] == "content"
    assert snapshot["revision"] == snapshot["content_fingerprint"]
    assert all(item["revision_kind"] == "content" for item in read_ir(tmp_path / "project")["evidence"])


# ------------------------------------------------------------------ supported, unchanged input
def test_a_clean_tree_with_nested_submodules_keeps_commit_provenance_and_is_deterministic(tmp_path: Path, capsys, tree: Tree) -> None:
    assert scan(tmp_path, capsys, tree.root)[0] == 0
    first = read_ir(tmp_path / "project")
    paths = {item["path"] for item in first["snapshots"][0]["files"]}
    assert {"app.py", "sub/library.py", "sub/deep/inner.py"} <= paths
    assert first["snapshots"][0]["revision_kind"] == "commit" and first["snapshots"][0]["revision"] == tree.head

    code, payload = scan(tmp_path, capsys, tree.root, *pinned(tree))

    assert code == 0, payload["errors"]
    second = read_ir(tmp_path / "project")
    assert second["snapshots"][0]["revision"] == tree.head
    assert [item for item in second["evidence"] if item["revision_kind"] != "commit"] == []
    assert scan(tmp_path, capsys, tree.root, *pinned(tree))[0] == 0
    assert read_ir(tmp_path / "project")["nodes"] == second["nodes"]


# ------------------------------------------------------------------ changed submodule content
def modified_tracked(tree: Tree) -> None:
    (tree.sub / "library.py").write_text("LIBRARY = 'edited'\n", encoding="utf-8")


def untracked(tree: Tree) -> None:
    (tree.sub / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")


def staged(tree: Tree) -> None:
    (tree.sub / "staged.py").write_text("STAGED = 1\n", encoding="utf-8")
    sh(tree.sub, "add", "staged.py")


def nested_modified(tree: Tree) -> None:
    (tree.deep / "inner.py").write_text("INNER = 'edited'\n", encoding="utf-8")


def nested_untracked(tree: Tree) -> None:
    (tree.deep / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")


def ignored(tree: Tree) -> None:
    commit_file(tree.sub, ".gitignore", "local.py\n")
    sh(tree.root, "add", "-A")
    sh(tree.root, "commit", "-q", "-m", "record the new submodule commit")
    (tree.sub / "local.py").write_text("LOCAL = 1\n", encoding="utf-8")


def another_commit(tree: Tree) -> None:
    commit_file(tree.sub, "newer.py", "NEWER = 1\n")


def nested_another_commit(tree: Tree) -> None:
    commit_file(tree.deep, "newer.py", "NEWER = 1\n")


def checked_out_older(tree: Tree) -> None:
    commit_file(tree.sub, "newer.py", "NEWER = 1\n")
    sh(tree.root, "add", "-A")
    sh(tree.root, "commit", "-q", "-m", "record the newer commit")
    sh(tree.sub, "checkout", "-q", "HEAD~1")


CHANGES = [modified_tracked, untracked, staged, nested_modified, nested_untracked, ignored, another_commit,
           nested_another_commit, checked_out_older]


@pytest.mark.parametrize("change", CHANGES, ids=[item.__name__ for item in CHANGES])
def test_changed_submodule_content_never_passes_pinned_mode_or_gets_commit_provenance(tmp_path: Path, capsys, tree: Tree, change) -> None:
    change(tree)

    code, payload = scan(tmp_path, capsys, tree.root, *pinned(tree))
    assert code != 0 and not payload["ok"]
    assert "clean working tree" in payload["errors"][0] and "sub" in payload["errors"][0], payload["errors"]

    code, payload = scan(tmp_path, capsys, tree.root)
    assert code == 0, payload["errors"]
    assert_content_provenance(tmp_path)


def test_the_scan_notices_a_new_submodule_change_between_runs(tmp_path: Path, capsys, tree: Tree) -> None:
    assert scan(tmp_path, capsys, tree.root)[0] == 0
    before = snapshot_of(tmp_path)
    modified_tracked(tree)

    assert scan(tmp_path, capsys, tree.root)[0] == 0
    after = snapshot_of(tmp_path)

    assert before["revision_kind"] == "commit" and after["revision_kind"] == "content"
    assert before["content_fingerprint"] != after["content_fingerprint"]


# ------------------------------------------------------------------ uninitialized submodules
def test_an_uninitialized_submodule_is_reported_and_adds_no_files(tmp_path: Path, capsys, tree: Tree) -> None:
    clone = tmp_path / "clone"
    sh(tmp_path, "clone", "-q", str(tree.root), str(clone))
    assert (clone / "sub").is_dir() and not any((clone / "sub").iterdir())

    code, payload = scan(tmp_path, capsys, clone, "--snapshot-mode", "pinned", "--pin", f"backend={tree.head}")

    assert code == 0, payload["errors"]
    assert any("submodule sub is not initialized" in warning for warning in payload["warnings"]), payload["warnings"]
    snapshot = snapshot_of(tmp_path)
    assert snapshot["revision_kind"] == "commit"
    assert not any(item["path"].startswith("sub/") for item in snapshot["files"])


def test_files_in_an_uninitialized_submodule_directory_are_not_trusted(tmp_path: Path, capsys, tree: Tree) -> None:
    clone = tmp_path / "clone"
    sh(tmp_path, "clone", "-q", str(tree.root), str(clone))
    (clone / "sub" / "planted.py").write_text("PLANTED = 1\n", encoding="utf-8")

    code, payload = scan(tmp_path, capsys, clone, "--snapshot-mode", "pinned", "--pin", f"backend={tree.head}")
    assert code != 0 and "not initialized but holds files" in payload["errors"][0]

    assert scan(tmp_path, capsys, clone)[0] == 0
    assert_content_provenance(tmp_path)


def test_a_submodule_replaced_by_a_link_is_not_trusted(tmp_path: Path, capsys, tree: Tree) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    import shutil

    shutil.rmtree(tree.sub, onerror=lambda function, path, _: (os.chmod(path, 0o700), function(path)))
    try:
        os.symlink(outside, tree.sub, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symbolic links")

    code, payload = scan(tmp_path, capsys, tree.root, *pinned(tree))

    assert code != 0 and "not a plain directory" in payload["errors"][0]


# ------------------------------------------------------------------ ignored files
def test_an_ignored_file_that_is_analyzed_does_not_get_commit_provenance(tmp_path: Path, capsys) -> None:
    repository = tmp_path / "backend"
    repository.mkdir()
    (repository / ".gitignore").write_text("local.py\nnode_modules/\n", encoding="utf-8")
    (repository / "app.py").write_text("APP = 1\n", encoding="utf-8")
    init_git_repository(repository)
    sha = git(repository, "rev-parse", "HEAD")
    (repository / "node_modules").mkdir()
    (repository / "node_modules" / "dependency.js").write_text("module.exports = 1\n", encoding="utf-8")

    # An ignored directory the scan never reads cannot make the tree differ from the commit.
    assert scan(tmp_path, capsys, repository, "--snapshot-mode", "pinned", "--pin", f"backend={sha}")[0] == 0

    (repository / "local.py").write_text("LOCAL = 1\n", encoding="utf-8")
    code, payload = scan(tmp_path, capsys, repository, "--snapshot-mode", "pinned", "--pin", f"backend={sha}")
    assert code != 0 and "ignored file local.py" in payload["errors"][0]
    assert scan(tmp_path, capsys, repository)[0] == 0
    assert_content_provenance(tmp_path)


# ------------------------------------------------------------------ nothing a nested repository configures runs
def hostile_program(tmp_path: Path) -> tuple[Path, str]:
    sentinel = tmp_path / "ran.txt"
    script = tmp_path / "program.sh"
    script.write_text(f"#!/bin/sh\necho ran > '{sentinel.as_posix()}'\ncat\n", encoding="utf-8", newline="\n")
    return sentinel, f"sh '{script.as_posix()}'"


def arm(tree: Tree, tmp_path: Path) -> Path:
    """Configure every repository in the tree to run a program, and make Git look at the files again."""

    sentinel, command = hostile_program(tmp_path)
    for configuration in tree.configurations():
        sh(tree.root, "config", "-f", str(configuration), "core.fsmonitor", command)
        sh(tree.root, "config", "-f", str(configuration), "filter.trap.clean", command)
        sh(tree.root, "config", "-f", str(configuration), "filter.trap.process", command)
    for repository in (tree.root, tree.sub, tree.deep):
        (repository / ".gitattributes").write_text("* filter=trap\n", encoding="utf-8")
    for name, repository in (("deep", tree.deep), ("sub", tree.sub), ("root", tree.root)):
        hook = Path(sh(repository, "rev-parse", "--git-path", "hooks"))
        hook = hook if hook.is_absolute() else repository / hook
        hook.mkdir(parents=True, exist_ok=True)
        script = hook / "post-index-change"
        script.write_text(f"#!/bin/sh\necho {name} > '{sentinel.as_posix()}'\n", encoding="utf-8", newline="\n")
        os.chmod(script, 0o755)
    time.sleep(1.1)
    for repository in (tree.root, tree.sub, tree.deep):
        (repository / ("app.py" if repository == tree.root else "library.py" if repository == tree.sub else "inner.py")).touch()
    sentinel.unlink(missing_ok=True)
    return sentinel


def test_nothing_configured_in_a_nested_repository_runs_during_a_scan(tmp_path: Path, capsys, tree: Tree) -> None:
    sentinel = arm(tree, tmp_path)

    scan(tmp_path, capsys, tree.root)
    scan(tmp_path, capsys, tree.root, *pinned(tree))

    assert not sentinel.exists(), "a program configured in a nested repository ran"


def test_the_hostile_nested_configuration_still_gets_the_right_answer(tmp_path: Path, capsys, tree: Tree) -> None:
    arm(tree, tmp_path)
    modified_tracked(tree)

    code, payload = scan(tmp_path, capsys, tree.root, *pinned(tree))

    assert code != 0 and "sub" in payload["errors"][0]
