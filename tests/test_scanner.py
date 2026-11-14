from __future__ import annotations

from pathlib import Path

from repodocs.scanner import discover_files


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


def test_discovery_excludes_configured_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('fixture')\n", encoding="utf-8")
    output = tmp_path / "documentation" / "auto"
    output.mkdir(parents=True)
    (output / "architecture.md").write_text("# generated\n", encoding="utf-8")
    (tmp_path / "documentation" / "handwritten.md").write_text("# mine\n", encoding="utf-8")

    files, _, _ = discover_files(tmp_path, excluded_paths=["documentation/auto"])

    assert [item.path for item in files] == ["documentation/handwritten.md", "src/main.py"]


def test_discovery_is_deterministic_and_hashes_content(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")

    first, _, total = discover_files(tmp_path)
    second, _, _ = discover_files(tmp_path)

    assert first == second
    assert [item.path for item in first] == ["a.py", "b.py"]
    assert total == sum(item.size for item in first)
    assert len({item.content_hash for item in first}) == 2
