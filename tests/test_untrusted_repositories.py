"""A scanned repository is untrusted: scanning must not run anything it configures."""

from __future__ import annotations

from pathlib import Path

import json

import pytest

from repodocs.errors import SnapshotError
from repodocs.snapshot import read_git_state

from conftest import git, init_git_repository, invoke


def sentinel_script(tmp_path: Path) -> tuple[Path, str]:
    """A shell script that records that it ran, and the command string that runs it."""

    sentinel = tmp_path / "ran.txt"
    script = tmp_path / "program.sh"
    script.write_text(f"#!/bin/sh\necho ran > '{sentinel.as_posix()}'\ncat\n", encoding="utf-8", newline="\n")
    return sentinel, f"sh '{script.as_posix()}'"


def configured_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    init_git_repository(repository)
    git(repository, "config", "core.autocrlf", "false")
    return repository


def test_a_repository_configured_fsmonitor_program_is_not_run(tmp_path: Path) -> None:
    sentinel, command = sentinel_script(tmp_path)
    repository = configured_repository(tmp_path)
    git(repository, "config", "core.fsmonitor", command)

    state = read_git_state(repository)

    assert state.is_repository and state.revision
    assert not sentinel.exists(), "scanning ran the program named by core.fsmonitor"


@pytest.mark.parametrize("hook", ["post-index-change", "fsmonitor-watchman", "pre-commit"])
def test_a_repository_hook_is_not_run(tmp_path: Path, hook: str) -> None:
    import os
    import time

    repository = configured_repository(tmp_path)
    sentinel = tmp_path / "ran.txt"
    script = repository / ".git" / "hooks" / hook
    script.write_text(f"#!/bin/sh\necho ran > '{sentinel.as_posix()}'\n", encoding="utf-8", newline="\n")
    os.chmod(script, 0o755)
    # Same content, new modification time: Git refreshes the index, which is what fires post-index-change.
    time.sleep(1.1)
    (repository / "README.md").touch()

    read_git_state(repository)

    assert not sentinel.exists(), f"scanning ran the {hook} hook"


@pytest.mark.parametrize("setting", ["clean", "process"])
def test_a_repository_configured_filter_program_is_not_run(tmp_path: Path, setting: str) -> None:
    sentinel, command = sentinel_script(tmp_path)
    repository = configured_repository(tmp_path)
    (repository / ".gitattributes").write_text("* filter=trap\n", encoding="utf-8")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "attributes")
    git(repository, "config", f"filter.trap.{setting}", command)
    # Rewriting a tracked file makes Git look at its content again, which is when a filter would run.
    readme = repository / "README.md"
    readme.write_text("# fixture\n", encoding="utf-8")
    readme.touch()

    read_git_state(repository)

    assert not sentinel.exists(), f"scanning ran the program named by filter.trap.{setting}"


def test_a_required_filter_that_cannot_run_does_not_hide_changes(tmp_path: Path) -> None:
    repository = configured_repository(tmp_path)
    (repository / ".gitattributes").write_text("* filter=trap\n", encoding="utf-8")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "attributes")
    git(repository, "config", "filter.trap.clean", "sh -c 'exit 1'")
    git(repository, "config", "filter.trap.required", "true")
    (repository / "README.md").write_text("changed\n", encoding="utf-8")

    assert read_git_state(repository).working_tree_state == "dirty"


def test_a_status_failure_stops_the_scan_instead_of_reporting_a_clean_tree(tmp_path: Path, monkeypatch) -> None:
    import repodocs.snapshot as snapshot

    repository = configured_repository(tmp_path)
    real = snapshot._git

    def failing(root, *options, arguments):
        completed = real(root, *options, arguments=arguments)
        if arguments and arguments[0] == "status":
            completed.returncode, completed.stderr = 128, "fatal: unable to read the index"
        return completed

    monkeypatch.setattr(snapshot, "_git", failing)

    with pytest.raises(SnapshotError, match="Git could not report the state.*unable to read the index"):
        read_git_state(repository)


def test_a_configuration_that_cannot_be_listed_stops_the_scan_instead_of_leaving_filters_on(tmp_path: Path, monkeypatch) -> None:
    import repodocs.snapshot as snapshot

    repository = configured_repository(tmp_path)
    real = snapshot._git

    def failing(root, *options, arguments):
        completed = real(root, *options, arguments=arguments)
        if arguments[:1] == ("config",):
            completed.returncode, completed.stderr = 128, "fatal: bad config line 1"
        return completed

    monkeypatch.setattr(snapshot, "_git", failing)

    with pytest.raises(SnapshotError, match="bad config line 1"):
        read_git_state(repository)


def test_a_scan_of_a_repository_with_a_hostile_git_config_still_completes(tmp_path: Path, capsys) -> None:
    sentinel, command = sentinel_script(tmp_path)
    repository = configured_repository(tmp_path)
    git(repository, "config", "core.fsmonitor", command)
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "repo")[0] == 0

    code, payload = invoke(root, capsys, "scan")

    assert code == 0 and payload["ok"], payload["errors"]
    assert not sentinel.exists()


# ------------------------------------------------------------------ hostile file contents
DEPTH = 100_000
SPRING_POM = (
    "<project><dependencies><dependency><groupId>org.springframework.boot</groupId>"
    "<artifactId>spring-boot-starter-web</artifactId></dependency></dependencies></project>"
)

HOSTILE_FILES = {
    "deeply nested package.json": {"package.json": "[" * DEPTH + "]" * DEPTH},
    "deeply nested React package.json": {"web/package.json": "[" * DEPTH + "]" * DEPTH, "web/App.jsx": "export default function A() { return null }"},
    "deeply nested package.json next to SQL": {"package.json": "[" * DEPTH + "]" * DEPTH, "db/migration/V1__x.sql": "CREATE TABLE t (id int);"},
    "package.json that is valid JSON of the wrong shape": {"package.json": "[1, 2]", "web/package.json": "42", "app/package.json": "null"},
    "deeply nested pyproject.toml": {"pyproject.toml": "a = " + "[" * 5000 + "]" * 5000},
    "deeply nested XML": {"pom.xml": "<project>" + "<a>" * DEPTH + "</a>" * DEPTH + "</project>"},
    "deeply nested OpenAPI": {"openapi.json": '{"openapi":"3.0.0","info":{"title":"x","version":"1"},"paths":{},"x":' + "[" * DEPTH + "]" * DEPTH + "}"},
    "self-referencing OpenAPI path": {"openapi.json": '{"openapi":"3.0.0","info":{"title":"x","version":"1"},"paths":{"/a":{"$ref":"#/paths/~1a"}}}'},
    "deeply nested Java": {"src/main/java/A.java": "class A { void f() { " + "{" * DEPTH + "}" * DEPTH + " } }", "pom.xml": SPRING_POM},
    "deeply nested SQL": {"db/migration/V1__x.sql": "CREATE TABLE t (id int DEFAULT " + "(" * DEPTH + "1" + ")" * DEPTH + ");"},
    "deeply nested JSX": {"web/package.json": '{"dependencies":{"react":"18"}}', "web/App.jsx": "export default function A() { return " + "<div>" * 20_000 + "</div>" * 20_000 + " }"},
    "one very long line": {"src/main/java/A.java": 'class A { String s = "' + "a" * 1_500_000 + '"; }', "pom.xml": SPRING_POM},
    "unterminated strings": {"src/main/java/A.java": 'class A { String s = "' * 20_000, "pom.xml": SPRING_POM},
    "bytes that are not UTF-8": {"a.py": b"\xff\xfe\xfa", "package.json": b"\xff\xfe{", "pom.xml": b"\xff<project/>", "db/V1__x.sql": b"\xff"},
}


@pytest.mark.parametrize("files", HOSTILE_FILES.values(), ids=HOSTILE_FILES.keys())
def test_hostile_file_contents_never_abort_the_scan(tmp_path: Path, capsys, files: dict) -> None:
    repository = tmp_path / "repo"
    for relative, content in files.items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "repo", "--destination")[0] == 0

    code, payload = invoke(root, capsys, "scan")

    assert code == 0 and payload["ok"], payload["errors"]
    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["ok"], payload["errors"]
    code, payload = invoke(root, capsys, "validate")
    assert code == 0 and payload["ok"], payload["errors"]


# ------------------------------------------------------------------ links that leave the repository
def make_junction(link: Path, target: Path) -> None:
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("junctions exist only on Windows")
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if completed.returncode != 0:
        pytest.skip("this account cannot create junctions")


def test_a_windows_junction_out_of_the_repository_is_not_followed(tmp_path: Path) -> None:
    from repodocs.scanner import discover_files

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("PRIVATE_MARKER = 1\n", encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_text("value = 1\n", encoding="utf-8")
    make_junction(repository / "linked", outside)

    files, warnings, _ = discover_files(repository)

    assert [item.path for item in files] == ["main.py"]
    assert any("leaves the repository" in warning for warning in warnings)


def test_content_behind_a_link_never_reaches_the_ir_or_the_documents(tmp_path: Path, capsys) -> None:
    import os

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("PRIVATE_MARKER = 1\n", encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_text("value = 1\n", encoding="utf-8")
    try:
        os.symlink(outside, repository / "linked-dir", target_is_directory=True)
        os.symlink(outside / "secret.py", repository / "linked.py")
    except OSError:
        make_junction(repository / "linked-dir", outside)
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "repo", "--destination")[0] == 0

    code, payload = invoke(root, capsys, "scan")
    assert code == 0 and payload["ok"]
    assert invoke(root, capsys, "generate")[0] == 0
    assert invoke(root, capsys, "ai-bundle")[0] == 0

    produced = [path for base in (root, repository / "docs") if base.exists() for path in base.rglob("*") if path.is_file()]
    assert produced
    for path in produced:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "PRIVATE_MARKER" not in text and "secret.py" not in text and "linked.py" not in text, path


# ------------------------------------------------------------------ generated Markdown and Mermaid
from repodocs.common import markdown_code, markdown_text, scrub
from repodocs.errors import ValidationError
from repodocs.validation import validate_markdown, validate_markdown_is_inert, validate_mermaid

HOSTILE_PATH = "/x<img src=x onerror=alert(1)>/[click](javascript:alert(1))`y`|z"


@pytest.mark.parametrize("value", [
    HOSTILE_PATH, "a`b", "``", "`", " lead", "trail ", "a|b", "line one\nline two", "tab\there", "back\\slash|", "x y",
])
def test_untrusted_text_cannot_leave_its_code_span_or_cell(value: str) -> None:
    span = markdown_code(value)
    cell = markdown_text(value)

    assert "\n" not in span + cell and " " not in span + cell
    # Remove the span the way a Markdown reader does; nothing of the value may remain outside it.
    fence = span[: len(span) - len(span.lstrip("`"))]
    assert span.endswith(fence) and span.count(fence) >= 2
    inner = span[len(fence): -len(fence)]
    assert fence not in inner.replace(fence + fence, "") or len(fence) > max((len(r) for r in __import__("re").findall("`+", inner)), default=0)
    assert "<" not in cell.replace("\\<", "") and "](" not in cell.replace("\\]", "").replace("\\[", "")


def spring_repository(tmp_path: Path, controller_path: str) -> Path:
    repository = tmp_path / "repo"
    source = repository / "src" / "main" / "java" / "A.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package a;\nimport org.springframework.web.bind.annotation.*;\n@RestController\n"
        f'@RequestMapping("{controller_path}")\npublic class A {{\n @GetMapping("/ok")\n public String get() {{ return ""; }}\n}}\n',
        encoding="utf-8",
    )
    (repository / "pom.xml").write_text(SPRING_POM, encoding="utf-8")
    return repository


def generate(tmp_path: Path, capsys, repository: Path) -> tuple[Path, str, str]:
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "repo", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    docs = next(path for path in repository.rglob("architecture.md")).parent
    return root, (docs / "architecture.md").read_text(encoding="utf-8"), (docs / "architecture.mmd").read_text(encoding="utf-8")


def test_markup_from_analyzed_source_is_inert_in_the_generated_documents(tmp_path: Path, capsys) -> None:
    repository = spring_repository(tmp_path, "/x<img src=x onerror=alert(1)>/[click](javascript:alert(1))`y`|z")

    root, markdown, _ = generate(tmp_path, capsys, repository)

    endpoint_lines = [line for line in markdown.splitlines() if "/ok" in line and line.startswith("| GET")]
    assert len(endpoint_lines) == 1, "the hostile mapping was not extracted, so this test proves nothing"
    validate_markdown_is_inert(markdown)
    # Whatever the repository wrote is inside one code span, so the table row still has its six cells.
    assert endpoint_lines[0].replace("\\|", "").count("|") == 7
    assert invoke(root, capsys, "validate")[0] == 0


@pytest.mark.parametrize("line", [
    "Hello <img src=x onerror=alert(1)>",
    "See [a link](https://example.invalid/phish)",
    "![beacon](https://example.invalid/pixel.png)",
    "[ref][1]",
    "<!-- hidden -->",
])
def test_validation_rejects_active_markdown_outside_code_spans(line: str) -> None:
    with pytest.raises(ValidationError, match="outside a code span"):
        validate_markdown(f"# Title\n\n{line}\n", {"claims": []})


@pytest.mark.parametrize("line", [
    "`<img src=x>` and ``a`b`` and `[a](b)`",
    r"escaped \<b\> and \[a\]\(b\) text",
    "| a | `x\\|y` |",
])
def test_validation_accepts_the_same_text_when_it_is_escaped(line: str) -> None:
    validate_markdown(f"# Title\n\n{line}\n", {"claims": []})


def test_tampering_with_a_generated_document_fails_validation(tmp_path: Path, capsys) -> None:
    repository = spring_repository(tmp_path, "/safe")
    root, markdown, _ = generate(tmp_path, capsys, repository)
    document = next(path for path in repository.rglob("architecture.md"))
    document.write_text(markdown + "\n[click me](javascript:alert(1))\n", encoding="utf-8", newline="\n")

    code, payload = invoke(root, capsys, "validate")

    assert code != 0 and not payload["ok"]


@pytest.mark.parametrize("label", [
    'a"]; click x href "javascript:alert(1)',
    "a<img src=x onerror=alert(1)>b",
    'a" --> evil["b',
])
def test_mermaid_labels_never_close_their_quotes_or_carry_markup(label: str) -> None:
    from repodocs.generator import _escape_label

    escaped = _escape_label(label)
    assert '"' not in escaped and "<" not in escaped and ">" not in escaped
    validate_mermaid(f'flowchart TD\n    a["{escaped}"]\n')


@pytest.mark.parametrize("diagram", [
    'flowchart TD\n    a["x<img src=x onerror=alert(1)>"]\n',
    'flowchart TD\n    a["x"]\n    click a href "javascript:alert(1)"\n',
    '%%{init: {"securityLevel": "loose"}}%%\nflowchart TD\n    a["x"]\n',
    'erDiagram\n    %%{init: {"securityLevel": "loose"}}%%\n    A {\n    }\n',
    'erDiagram\n    A["x<b>"] {\n    }\n',
    'erDiagram\n    A {\n        int id "<b>"\n    }\n',
])
def test_validation_rejects_unsafe_mermaid(diagram: str) -> None:
    with pytest.raises(ValidationError):
        validate_mermaid(diagram)


def test_a_table_name_cannot_break_out_of_the_erd_comments_or_labels(tmp_path: Path, capsys) -> None:
    repository = tmp_path / "repo"
    migration = repository / "db" / "migration" / "V1__x.sql"
    migration.parent.mkdir(parents=True)
    hostile = 'a`b<i> click x href javascript:alert(1)'
    migration.write_text(
        f'CREATE TABLE "{hostile}" (id BIGSERIAL PRIMARY KEY);\n'
        f'CREATE TABLE "no key" (parent bigint REFERENCES "{hostile}"(id), other bigint REFERENCES missing(id));\n',
        encoding="utf-8",
    )

    root, markdown, _ = generate(tmp_path, capsys, repository)

    erd = next(path for path in repository.rglob("erd.mmd")).read_text(encoding="utf-8")
    assert "javascript" in erd, "the hostile name did not reach the diagram, so this test proves nothing"
    validate_mermaid(erd)
    validate_markdown(markdown, {"claims": []})
    for line in erd.splitlines():
        # A comment is ignored by Mermaid; every other line must be free of markup and directives.
        assert line.strip().startswith("%%") or ("<" not in line and not line.strip().startswith("click")), line
        assert "\n" not in line and (not line.strip().startswith("%%") or line.strip().startswith("%% ")), line
    assert invoke(root, capsys, "validate")[0] == 0


def test_scrub_removes_every_kind_of_line_break() -> None:
    assert scrub("a\r\nb c\u0085d\x00e") == "a b c d e"


# ------------------------------------------------------------------ writing into an untrusted destination
def link(path: Path, target: Path, *, directory: bool = False) -> None:
    import os

    try:
        os.symlink(target, path, target_is_directory=directory)
    except OSError:
        if directory:
            make_junction(path, target)
        else:
            pytest.skip("this account cannot create symbolic links")


def destination_project(tmp_path: Path, capsys) -> tuple[Path, Path, Path]:
    """A scanned project whose destination is also the repository, and a folder outside it."""

    repository = spring_repository(tmp_path, "/safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "repo", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    return root, repository, outside


@pytest.mark.parametrize("existing", [False, True], ids=["dangling link", "link to an existing file"])
def test_generate_does_not_write_through_a_link_at_an_artifact_name(tmp_path: Path, capsys, existing: bool) -> None:
    root, repository, outside = destination_project(tmp_path, capsys)
    target = outside / "victim.txt"
    if existing:
        target.write_text("precious\n", encoding="utf-8")
    output = repository / "docs" / "generated"
    output.mkdir(parents=True)
    link(output / "architecture.md", target)

    code, payload = invoke(root, capsys, "generate")

    assert code != 0 and not payload["ok"]
    assert target.read_text(encoding="utf-8") == "precious\n" if existing else not target.exists()
    assert list(outside.iterdir()) == ([target] if existing else [])


def test_generate_does_not_write_into_an_output_directory_that_is_a_link(tmp_path: Path, capsys) -> None:
    root, repository, outside = destination_project(tmp_path, capsys)
    (repository / "docs").mkdir()
    link(repository / "docs" / "generated", outside, directory=True)

    code, payload = invoke(root, capsys, "generate")

    assert code != 0 and not payload["ok"]
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("leaving", ["../../../outside/victim.txt", "ABSOLUTE"])
def test_a_manifest_naming_a_path_outside_the_output_directory_is_refused(tmp_path: Path, capsys, leaving: str) -> None:
    root, repository, outside = destination_project(tmp_path, capsys)
    assert invoke(root, capsys, "generate")[0] == 0
    victim = outside / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    manifest_file = root / ".repodocs" / "artifact-manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    from repodocs.common import sha256_text

    manifest["artifacts"].append(
        {"path": str(victim) if leaving == "ABSOLUTE" else leaving, "content_hash": sha256_text("precious\n"), "generator": "x"}
    )
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    for command in ("validate", "generate"):
        code, payload = invoke(root, capsys, command)
        assert code != 0 and not payload["ok"], command
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_an_escaped_backtick_does_not_open_a_code_span() -> None:
    # An escaped backtick is text, and the markup after it is outside any span, so it must be rejected.
    with pytest.raises(ValidationError, match="outside a code span"):
        validate_markdown("# T\n\n\` <img src=x> \`\n", {"claims": []})
    # The same characters, escaped and fenced by the helpers on one line, are inert.
    line = markdown_text("a`b") + " " + markdown_code("x<y") + " " + markdown_text("[c](d)")
    validate_markdown(f"# T\n\n{line}\n", {"claims": []})
