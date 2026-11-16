"""Documentation publication: idempotence, loop safety, remote behavior, and failure handling.

Git runs for real against a bare remote. Only the GitHub pull request calls are faked,
because they cannot run without a repository and a token.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import git, init_git_repository, invoke, read_ir
from repodocs import publish as publication
from repodocs.config import ProjectConfig
from repodocs.errors import ConfigError, PublicationError
from repodocs.publish import (
    DEFAULT_BRANCH,
    GhPullRequests,
    auth_environment,
    publish,
    render_pull_request_body,
)
from repodocs.validation import load_ir
from test_schemas import assert_valid


FIXTURES = Path(__file__).parent / "fixtures"
BOT_REF = f"refs/heads/{DEFAULT_BRANCH}"
MANIFEST = ".repodocs/artifact-manifest.json"
GITIGNORE = "sources/\n.repodocs/*\n!.repodocs/artifact-manifest.json\n"


class FakePullRequests:
    def __init__(self) -> None:
        self.open: dict[tuple[str, str], int] = {}
        self.calls: list[tuple] = []
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.fail_create = 0
        self.closed: list[tuple[int, str]] = []
        self.fail_close = 0
        self.fail_find = 0
        self.on_close = None  # called as the close request arrives, to observe what exists at that moment

    def close(self, number, comment):
        self.calls.append(("close", number))
        if self.on_close:
            self.on_close()
        if self.fail_close:
            self.fail_close -= 1
            raise PublicationError("gh pr close failed: simulated outage")
        self.closed.append((number, comment))
        self.open = {key: value for key, value in self.open.items() if value != number}

    def find_open(self, head, base):
        self.calls.append(("find", head, base))
        if self.fail_find:
            self.fail_find -= 1
            raise PublicationError("gh pr list failed: simulated outage")
        return self.open.get((head, base))

    def create(self, head, base, title, body):
        self.calls.append(("create", head, base))
        if self.fail_create:
            self.fail_create -= 1
            raise PublicationError("gh pr create failed: simulated outage")
        number = len(self.open) + 1
        self.open[(head, base)] = number
        self.created.append({"number": number, "title": title, "body": body})
        return number

    def update(self, number, title, body):
        self.calls.append(("update", number))
        self.updated.append({"number": number, "title": title, "body": body})

    def mutations(self) -> list[tuple]:
        return [call for call in self.calls if call[0] != "find"]


class Project:
    def __init__(self, root: Path, origin: Path, sources: list[str], capsys) -> None:
        self.root, self.origin, self.sources, self.capsys = root, origin, sources, capsys

    def run(self, *arguments: str) -> tuple[int, dict]:
        return invoke(self.root, self.capsys, *arguments)

    def pins(self) -> list[str]:
        arguments: list[str] = []
        for name in self.sources:
            arguments += ["--pin", f"{name}={git(self.root / 'sources' / name, 'rev-parse', 'HEAD')}"]
        return arguments

    def generate(self) -> dict:
        code, response = self.run("scan", "--snapshot-mode", "pinned", *self.pins())
        assert code == 0, response
        code, response = self.run("generate")
        assert code == 0, response
        return response

    def commit_docs(self, message: str = "docs", push: bool = True) -> None:
        """Commit the generated docs to the base, and by default publish it, as merging the docs PR would."""

        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        if push:
            git(self.root, "push", "-q", "origin", "main")

    def config_and_ir(self) -> tuple[ProjectConfig, dict]:
        config = ProjectConfig.load(self.root)
        return config, load_ir(config.internal_path / "ir.json")

    def publish(self, client, **options):
        config, ir = self.config_and_ir()
        options.setdefault("base", "main")
        return publish(config, ir, pull_requests=client, **options)

    def remote_tip(self) -> str | None:
        listing = git(self.origin, "for-each-ref", "--format=%(objectname)", BOT_REF)
        return listing or None

    def remote_files(self, ref: str) -> set[str]:
        return set(git(self.origin, "ls-tree", "-r", "--name-only", ref).splitlines())


def build_project(tmp_path: Path, capsys, sources: dict[str, Path]) -> Project:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    root = tmp_path / "coordination"
    for name, fixture in sources.items():
        shutil.copytree(fixture, root / "sources" / name)
        init_git_repository(root / "sources" / name)
    git(root, "init", "-q", "-b", "main")
    project = Project(root, origin, list(sources), capsys)
    assert project.run("init", "SmartCart")[0] == 0
    for name in sources:
        assert project.run("add", f"sources/{name}", "--id", name)[0] == 0
    assert project.run("add", ".", "--id", "docs", "--role", "destination")[0] == 0
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "coordination config")
    git(root, "remote", "add", "origin", origin.as_uri())
    git(root, "push", "-q", "origin", "main")
    return project


@pytest.fixture
def basic(tmp_path, capsys) -> Project:
    return build_project(tmp_path, capsys, {"backend": FIXTURES / "basic-repo"})


@pytest.fixture
def correlated(tmp_path, capsys) -> Project:
    base = FIXTURES / "correlation-repo"
    return build_project(
        tmp_path, capsys, {"frontend": base / "frontend", "backend": base / "backend", "competing": base / "competing"}
    )


def change_source(project: Project, name: str, text: str) -> None:
    source = project.root / "sources" / name
    (source / "src").mkdir(exist_ok=True)
    (source / "src" / "extra.py").write_text(text, encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "change")


# ------------------------------------------------------------------ loop safety
def test_committing_generated_docs_does_not_cause_another_diff(basic) -> None:
    basic.generate()
    first = (basic.root / MANIFEST).read_text(encoding="utf-8")
    basic.commit_docs()
    assert MANIFEST in git(basic.root, "ls-files")

    response = basic.generate()

    # The destination's HEAD moved, yet nothing about the generated output changed.
    assert response["data"]["changed"] is False
    assert git(basic.root, "status", "--porcelain") == ""
    assert (basic.root / MANIFEST).read_text(encoding="utf-8") == first


def test_publish_is_a_noop_when_the_base_already_contains_the_output(basic) -> None:
    basic.generate()
    basic.commit_docs()
    basic.generate()
    client = FakePullRequests()

    # A dry run contacts nothing: a remote that does not exist proves it.
    dry = basic.publish(client, dry_run=True, remote="no-such-remote")
    assert dry == {"status": "no-op", "branch": DEFAULT_BRANCH, "base": "main", "changes": [], "pull_request": None, "branch_deleted": False}

    # A real run reads the remote, finds no bot branch, and so has nothing to retire.
    result = basic.publish(client)
    assert result == dry
    # The only GitHub call is a read: a retry after a partial failure must still find an open pull request.
    assert client.mutations() == [] and basic.remote_tip() is None


def test_manifest_must_be_committed_for_a_clean_clone_to_regenerate(basic, tmp_path) -> None:
    basic.generate()
    basic.commit_docs()
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", basic.root.as_uri(), str(clone))
    shutil.copytree(basic.root / "sources", clone / "sources")
    code, _ = invoke(clone, basic.capsys, "scan", "--snapshot-mode", "pinned", *basic.pins())
    assert code == 0
    code, response = invoke(clone, basic.capsys, "generate")
    assert code == 0 and response["data"]["changed"] is False

    (clone / MANIFEST).unlink()
    code, response = invoke(clone, basic.capsys, "generate")
    assert code == 4 and "unowned files" in response["errors"][0]


# ------------------------------------------------------------------ publishing
def test_first_publish_pushes_one_bot_commit_and_opens_one_pull_request(basic) -> None:
    basic.generate()
    base_sha = git(basic.root, "rev-parse", "HEAD")
    client = FakePullRequests()

    result = basic.publish(client)

    assert result["status"] == "published"
    assert result["pull_request"] == {"number": 1, "action": "created"}
    tip = basic.remote_tip()
    assert tip and git(basic.origin, "rev-parse", f"{tip}^") == base_sha
    added = basic.remote_files(tip) - basic.remote_files("main")
    assert added == {MANIFEST, "docs/generated/architecture.md", "docs/generated/architecture.mmd"}
    assert git(basic.origin, "log", "-1", "--format=%an", tip) == "RepoDocs"
    message = git(basic.origin, "log", "-1", "--format=%B", tip)
    assert "Source revisions" in message and "backend" in message and "Co-Authored-By" not in message

    # Publication never touched the working tree, the branch, or the remote base.
    assert git(basic.root, "rev-parse", "HEAD") == base_sha
    assert git(basic.root, "branch", "--show-current") == "main"
    assert (basic.root / "docs" / "generated" / "architecture.md").exists()
    assert git(basic.origin, "rev-parse", "main") == base_sha

    pull_request = client.created[0]
    assert pull_request["title"] == "docs: update generated system documentation"
    assert "owned by RepoDocs" in pull_request["body"]
    assert "| backend | pinned |" in pull_request["body"]
    assert "added: `docs/generated/architecture.md`" in pull_request["body"]
    assert client.calls == [("find", DEFAULT_BRANCH, "main"), ("create", DEFAULT_BRANCH, "main")]


def test_rerunning_with_identical_output_pushes_and_edits_nothing(basic) -> None:
    basic.generate()
    client = FakePullRequests()
    basic.publish(client)
    tip = basic.remote_tip()

    basic.generate()
    again = basic.publish(client)

    assert again["status"] == "unchanged"
    assert again["pull_request"] == {"number": 1, "action": "existing"}
    assert basic.remote_tip() == tip
    assert client.mutations() == [("create", DEFAULT_BRANCH, "main")]
    assert len(client.created) == 1 and client.updated == []


def test_changed_output_updates_the_same_branch_and_pull_request(basic) -> None:
    basic.generate()
    client = FakePullRequests()
    basic.publish(client)
    first_tip = basic.remote_tip()

    change_source(basic, "backend", "value = 2\n")
    basic.generate()
    result = basic.publish(client)

    assert result["status"] == "published"
    assert result["pull_request"] == {"number": 1, "action": "updated"}
    assert basic.remote_tip() != first_tip
    # Still exactly one bot branch and one commit on top of the base.
    assert git(basic.origin, "branch", "--list", DEFAULT_BRANCH) != ""
    assert git(basic.origin, "rev-list", "--count", f"main..{basic.remote_tip()}") == "1"
    assert len(client.created) == 1 and len(client.updated) == 1
    changed = {change["path"]: change["status"] for change in result["changes"]}
    # The pull request is always measured against the base, which has no docs until it merges.
    assert changed[MANIFEST] == "A" and changed["docs/generated/architecture.md"] == "A"
    assert "added: `docs/generated/architecture.md`" in client.updated[0]["body"]
    old_content = git(basic.origin, "show", f"{first_tip}:docs/generated/architecture.md")
    new_content = git(basic.origin, "show", f"{basic.remote_tip()}:docs/generated/architecture.md")
    assert new_content != old_content
    assert (basic.root / "docs" / "generated" / "architecture.md").read_text(encoding="utf-8").strip() == new_content.strip()


def test_a_remote_branch_moved_since_it_was_read_is_never_overwritten(basic, monkeypatch, tmp_path) -> None:
    basic.generate()
    client = FakePullRequests()
    basic.publish(client)
    stale_tip = basic.remote_tip()

    # Someone else moves the bot branch after this run read it.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", basic.origin.as_uri(), str(other))
    git(other, "checkout", "-q", "-b", "scratch", "origin/" + DEFAULT_BRANCH)
    (other / "note.txt").write_text("manual\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "manual edit")
    git(other, "push", "-q", "origin", f"scratch:{DEFAULT_BRANCH}", "--force")
    moved_tip = basic.remote_tip()
    assert moved_tip != stale_tip

    real_git = publication._git

    def stale_listing(root, *args, **kwargs):
        done = real_git(root, *args, **kwargs)
        if args[0] == "ls-remote":
            # Keep the real base line and report the bot branch where it was before it moved.
            lines = [f"{stale_tip}\t{BOT_REF}" if line.endswith(BOT_REF) else line for line in done.stdout.splitlines()]
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr="")
        return done

    monkeypatch.setattr(publication, "_git", stale_listing)
    change_source(basic, "backend", "value = 3\n")
    basic.generate()
    with pytest.raises(PublicationError, match="git push failed"):
        basic.publish(client)

    assert basic.remote_tip() == moved_tip


def test_dry_run_reports_the_change_without_contacting_anything(basic) -> None:
    basic.generate()
    client = FakePullRequests()

    result = basic.publish(client, dry_run=True, remote="no-such-remote")

    assert result["status"] == "would-publish"
    assert {change["path"] for change in result["changes"]} >= {MANIFEST, "docs/generated/architecture.md"}
    assert client.calls == [] and basic.remote_tip() is None


def test_pull_request_failure_after_push_is_recovered_without_repushing(basic) -> None:
    basic.generate()
    client = FakePullRequests()
    client.fail_create = 1

    with pytest.raises(PublicationError, match="simulated outage"):
        basic.publish(client)
    tip = basic.remote_tip()
    assert tip is not None and client.open == {}

    result = basic.publish(client)

    assert result["status"] == "unchanged"
    assert result["pull_request"] == {"number": 1, "action": "created"}
    assert basic.remote_tip() == tip


def test_removed_ai_document_is_explained_in_the_pull_request(basic) -> None:
    ir = {"snapshots": [], "candidates": []}
    body = render_pull_request_body(ir, [("D", "docs/generated/architecture-ai.md"), ("M", "docs/generated/architecture.md")], "bot", "main")
    assert "removed: `docs/generated/architecture-ai.md`" in body
    assert "does not run AI interpretation" in body
    assert "commit" not in body.split("AI-assisted document")[1]  # no unproven remediation advice
    plain = render_pull_request_body(ir, [("M", "docs/generated/architecture.md")], "bot", "main")
    assert "AI-assisted" not in plain


# ------------------------------------------------------------------ refusals
def test_publication_requires_the_base_branch_to_be_checked_out(basic) -> None:
    basic.generate()
    client = FakePullRequests()

    git(basic.root, "checkout", "-q", "-b", "feature")
    with pytest.raises(PublicationError, match="checked-out branch is feature, not the base 'main'"):
        basic.publish(client)

    git(basic.root, "checkout", "-q", "--detach", "main")
    with pytest.raises(PublicationError, match="detached HEAD"):
        basic.publish(client)

    git(basic.root, "checkout", "-q", "main")
    with pytest.raises(ConfigError, match="--base must name"):
        basic.publish(client, base="  ")
    assert client.calls == [] and basic.remote_tip() is None


def test_publication_does_not_make_a_complete_clone_shallow(basic) -> None:
    basic.generate()
    client = FakePullRequests()
    assert git(basic.root, "rev-parse", "--is-shallow-repository") == "false"
    basic.publish(client)
    change_source(basic, "backend", "value = 4\n")
    basic.generate()
    basic.publish(client)  # reads the existing bot branch, which is where a fetch happens
    assert git(basic.root, "rev-parse", "--is-shallow-repository") == "false"


def test_a_shallow_clone_fetches_the_bot_branch_with_limited_depth(basic, tmp_path) -> None:
    basic.generate()
    basic.publish(FakePullRequests())
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth=1", "--branch", "main", basic.origin.as_uri(), str(shallow))
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    shutil.copytree(basic.root / "sources", shallow / "sources")
    shutil.copy(basic.root / "repodocs.json", shallow / "repodocs.json")
    shallow_project = Project(shallow, basic.origin, basic.sources, basic.capsys)
    shallow_project.generate()
    client = FakePullRequests()
    client.open[(DEFAULT_BRANCH, "main")] = 1

    result = shallow_project.publish(client)

    assert result["status"] == "unchanged"
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"


def test_changes_outside_the_generated_paths_stop_publication(basic) -> None:
    basic.generate()
    (basic.root / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    client = FakePullRequests()

    with pytest.raises(PublicationError, match="outside the generated documentation.*notes.txt"):
        basic.publish(client)

    assert basic.remote_tip() is None and client.calls == []


def test_source_checkouts_that_are_not_ignored_stop_publication(basic) -> None:
    basic.generate()
    (basic.root / ".gitignore").write_text(".repodocs/*\n!.repodocs/artifact-manifest.json\n", encoding="utf-8")
    git(basic.root, "commit", "-q", "-am", "stop ignoring sources")
    with pytest.raises(PublicationError, match="must be listed in .gitignore"):
        basic.publish(FakePullRequests())


def test_manifest_outside_the_destination_repository_is_rejected(tmp_path, capsys) -> None:
    root = tmp_path / "project"
    (root / "backend").mkdir(parents=True)
    shutil.copytree(FIXTURES / "basic-repo", root / "backend", dirs_exist_ok=True)
    docs = tmp_path / "docs-repo"
    docs.mkdir()
    init_git_repository(docs)
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--id", "backend")[0] == 0
    assert invoke(root, capsys, "add", str(docs), "--id", "docs", "--role", "destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0

    code, response = invoke(root, capsys, "publish", "--base", "main", "--dry-run")

    assert code == 2
    assert "manifest inside the destination repository" in response["errors"][0]


@pytest.mark.parametrize("branch", ["main", "-evil", "bad..name", "has space"])
def test_invalid_publication_branches_are_rejected(basic, branch) -> None:
    basic.generate()
    with pytest.raises(ConfigError, match="Invalid publication branch"):
        basic.publish(FakePullRequests(), branch=branch)


def test_destination_must_be_the_root_of_a_git_repository_with_a_commit(tmp_path, capsys) -> None:
    root = tmp_path / "project"
    shutil.copytree(FIXTURES / "basic-repo", root / "backend")
    assert invoke(root, capsys, "init", "Plain")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--id", "backend", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    code, response = invoke(root, capsys, "publish", "--base", "main", "--dry-run")
    assert code == 2 and "root of a Git repository" in response["errors"][0]


# ------------------------------------------------------------------ CLI contract
def test_cli_publish_requires_validated_output_and_resolved_confirmations(correlated) -> None:
    correlated.generate()
    code, response = correlated.run("publish", "--base", "main", "--dry-run")
    assert code == 5
    assert "require confirmation or rejection" in response["errors"][0]
    assert_valid("cli-response-1.0.json", response)
    assert correlated.remote_tip() is None

    ids = [item["id"] for item in read_ir(correlated.root)["candidates"]]
    assert ids and correlated.run("reject", *ids)[0] == 0
    git(correlated.root, "add", "repodocs.confirmations.json")
    git(correlated.root, "commit", "-q", "-m", "decisions")
    correlated.generate()
    code, response = correlated.run("publish", "--base", "main", "--dry-run")
    assert code == 0, response
    assert response["data"]["status"] == "would-publish"

    generated = correlated.root / "docs" / "generated" / "architecture.md"
    generated.write_text(generated.read_text(encoding="utf-8") + "\nmanual edit\n", encoding="utf-8")
    code, response = correlated.run("publish", "--base", "main", "--dry-run")
    assert code == 4 and "edited unexpectedly" in response["errors"][0]


def test_cli_publish_before_generate_is_a_validation_failure(basic) -> None:
    code, response = basic.run("publish", "--base", "main", "--dry-run")
    assert code == 3 and "run scan first" in response["errors"][0]


def test_cli_publish_needs_a_token_unless_dry_run(basic, monkeypatch) -> None:
    basic.generate()
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    code, response = basic.run("publish", "--base", "main")
    assert code == 2 and "GH_TOKEN" in response["errors"][0]
    assert basic.remote_tip() is None


def test_cli_publish_end_to_end_with_a_stand_in_for_github(basic, monkeypatch) -> None:
    basic.generate()
    fake = FakePullRequests()
    monkeypatch.setattr(publication, "GhPullRequests", lambda root, token: fake)
    monkeypatch.setenv("GH_TOKEN", "test-token")

    code, response = basic.run("publish", "--base", "main")
    assert code == 0, response
    assert_valid("cli-response-1.0.json", response)
    assert response["data"]["status"] == "published"
    assert response["data"]["pull_request"] == {"number": 1, "action": "created"}
    assert basic.remote_tip() is not None

    basic.generate()
    code, response = basic.run("publish", "--base", "main")
    assert code == 0 and response["data"]["status"] == "unchanged"


def test_cli_publish_failure_uses_exit_code_8(basic, monkeypatch) -> None:
    basic.generate()
    (basic.root / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    code, response = basic.run("publish", "--base", "main")
    assert code == 8
    assert_valid("cli-response-1.0.json", response)


# ------------------------------------------------------------------ pins from workflows
def test_pins_json_supplies_pins_and_is_checked_against_the_checkout(basic) -> None:
    sha = git(basic.root / "sources" / "backend", "rev-parse", "HEAD")
    code, response = basic.run("scan", "--snapshot-mode", "pinned", "--pins-json", json.dumps({"backend": sha}))
    assert code == 0, response
    assert read_ir(basic.root)["snapshots"][0]["pinned_revision"] == sha

    code, response = basic.run("scan", "--snapshot-mode", "pinned", "--pins-json", json.dumps({"backend": "0" * 40}))
    assert code == 6 and "stale checkout" in response["errors"][0]


@pytest.mark.parametrize("value, message", [
    ("not json", "not valid JSON"),
    ('["a"]', "JSON object"),
    ('{"backend": 5}', "JSON object"),
])
def test_invalid_pins_json_is_a_usage_error(basic, value, message) -> None:
    code, response = basic.run("scan", "--pins-json", value)
    assert code == 2 and message in response["errors"][0]


def test_conflicting_pin_sources_are_rejected(basic) -> None:
    sha = git(basic.root / "sources" / "backend", "rev-parse", "HEAD")
    code, response = basic.run("scan", "--pin", f"backend={sha}", "--pins-json", json.dumps({"backend": "1" * 40}))
    assert code == 2 and "disagree" in response["errors"][0]
    assert basic.run("scan", "--pin", f"backend={sha}", "--pins-json", json.dumps({"backend": sha}))[0] == 0


def test_detached_checkout_by_sha_is_what_keeps_the_output_stable(basic) -> None:
    """A source on a branch changes the IR hash; the workflow must check sources out by SHA."""

    basic.generate()
    basic.commit_docs()
    source = basic.root / "sources" / "backend"
    sha = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", "--detach", sha)
    code, _ = basic.run("scan", "--snapshot-mode", "pinned", "--pin", f"backend={sha}")
    code, response = basic.run("generate")
    assert response["data"]["changed"] is True  # head state changed from attached to detached

    basic.commit_docs()
    code, _ = basic.run("scan", "--snapshot-mode", "pinned", "--pin", f"backend={sha}")
    code, response = basic.run("generate")
    assert response["data"]["changed"] is False


# ------------------------------------------------------------------ credentials
def test_token_reaches_git_only_through_environment_for_https_remotes() -> None:
    environment = auth_environment("https://github.com/org/repo.git", "secret-token")
    assert environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert environment["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
    assert "secret-token" not in environment["GIT_CONFIG_VALUE_0"]  # base64, not plain text
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    # Userinfo in a remote URL never becomes part of the scoped key.
    assert auth_environment("https://user:pw@github.example/org/repo.git", "t")["GIT_CONFIG_KEY_0"] == "http.https://github.example/.extraheader"
    for url in ("file:///tmp/repo.git", "ssh://git@github.com/org/repo.git", "/tmp/repo.git"):
        assert "GIT_CONFIG_VALUE_0" not in auth_environment(url, "secret-token")
    assert "GIT_CONFIG_VALUE_0" not in auth_environment("https://github.com/org/repo.git", None)


def test_git_errors_never_echo_the_token(tmp_path) -> None:
    with pytest.raises(PublicationError) as raised:
        publication._git(tmp_path, "secret-token-subcommand", token="secret-token")
    assert "secret-token" not in str(raised.value) and "***" in str(raised.value)


def test_github_client_passes_the_body_by_file_and_the_token_by_environment(tmp_path, monkeypatch) -> None:
    seen: list[dict] = []

    def fake_run(command, **kwargs):
        record = {"command": command, "env": kwargs["env"], "cwd": kwargs["cwd"]}
        if "--body-file" in command:
            record["body"] = Path(command[command.index("--body-file") + 1]).read_text(encoding="utf-8")
        seen.append(record)
        stdout = {"list": '[{"number": 9}, {"number": 4}]', "create": "https://github.com/org/repo/pull/12\n"}.get(command[2], "")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(publication.subprocess, "run", fake_run)
    client = GhPullRequests(tmp_path, "secret-token")
    body = "line one\n$(touch pwned) `id` | table\n"

    assert client.find_open("bot", "main") == 4
    assert client.create("bot", "main", "A title", body) == 12
    client.update(12, "A title", body)

    for record in seen:
        assert record["env"]["GH_TOKEN"] == "secret-token" and record["cwd"] == tmp_path
        assert "secret-token" not in " ".join(record["command"])
        assert body not in record["command"]
    assert [record["body"] for record in seen if "body" in record] == [body, body]
    leftovers = [item for item in seen if "--body-file" in item["command"] and Path(item["command"][item["command"].index("--body-file") + 1]).exists()]
    assert leftovers == []


def test_github_client_failures_are_publication_errors_without_the_token(tmp_path, monkeypatch) -> None:
    def failing(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="HTTP 403 for token secret-token")

    monkeypatch.setattr(publication.subprocess, "run", failing)
    with pytest.raises(PublicationError) as raised:
        GhPullRequests(tmp_path, "secret-token").find_open("bot", "main")
    assert "secret-token" not in str(raised.value) and "***" in str(raised.value)

    def missing(command, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(publication.subprocess, "run", missing)
    with pytest.raises(PublicationError, match="`gh` is required"):
        GhPullRequests(tmp_path, "t").find_open("bot", "main")


# ------------------------------------------------------------------ the checkout must be the remote's base
def test_unpublished_local_commits_never_enter_the_documentation_pull_request(basic) -> None:
    basic.generate()
    (basic.root / "feature.txt").write_text("unreviewed application change\n", encoding="utf-8")
    git(basic.root, "add", "feature.txt")
    git(basic.root, "commit", "-q", "-m", "an unpublished local commit")
    client = FakePullRequests()

    with pytest.raises(PublicationError, match="has 'main' at"):
        basic.publish(client)

    assert basic.remote_tip() is None and client.calls == []
    assert "feature.txt" not in basic.remote_files("main")


def test_a_remote_base_that_moved_is_refused(basic, tmp_path) -> None:
    basic.generate()
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", basic.origin.as_uri(), str(other))
    (other / "newer.txt").write_text("a newer base\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "someone published to the base")
    git(other, "push", "-q", "origin", "main")
    client = FakePullRequests()

    with pytest.raises(PublicationError, match="Check out the remote's current base"):
        basic.publish(client)

    assert basic.remote_tip() is None and client.calls == []


def test_a_local_only_base_branch_is_refused(basic) -> None:
    basic.generate()
    git(basic.root, "checkout", "-q", "-b", "release")
    with pytest.raises(PublicationError, match="no branch 'release'"):
        basic.publish(FakePullRequests(), base="release")
    assert basic.remote_tip() is None


def test_an_unpublished_commit_is_not_mistaken_for_a_no_op(basic) -> None:
    """Output equal to an unpublished local base must not close a pull request that is still right."""

    basic.generate()
    basic.commit_docs(push=False)
    basic.generate()
    client = FakePullRequests()

    with pytest.raises(PublicationError, match="has 'main' at"):
        basic.publish(client)

    assert client.calls == []


def test_a_dry_run_cannot_check_the_remote_and_says_nothing_about_it(basic) -> None:
    basic.generate()
    (basic.root / "feature.txt").write_text("unreviewed\n", encoding="utf-8")
    git(basic.root, "add", "feature.txt")
    git(basic.root, "commit", "-q", "-m", "local only")

    result = basic.publish(FakePullRequests(), dry_run=True, remote="no-such-remote")

    assert result["status"] == "would-publish"  # a dry run never contacts the remote, so it cannot know


# ------------------------------------------------------------------ obsolete proposals are retired
def reverted_to_documented_state(basic) -> str:
    """Publish a change, then return the sources to the state the base already documents."""

    source = basic.root / "sources" / "backend"
    original = git(source, "rev-parse", "HEAD")
    change_source(basic, "backend", "value = 5\n")
    basic.generate()
    return original


def test_a_no_op_run_closes_an_obsolete_pull_request_and_deletes_its_branch(basic) -> None:
    basic.generate()
    basic.commit_docs()
    original = reverted_to_documented_state(basic)
    client = FakePullRequests()
    first = basic.publish(client)
    assert first["status"] == "published" and client.open == {(DEFAULT_BRANCH, "main"): 1}
    assert basic.remote_tip() is not None

    git(basic.root / "sources" / "backend", "reset", "-q", "--hard", original)
    basic.generate()
    result = basic.publish(client)

    assert result["status"] == "no-op"
    assert result["pull_request"] == {"number": 1, "action": "closed"}
    assert result["branch_deleted"] is True
    assert client.closed == [(1, publication.NO_OP_COMMENT)] and client.open == {}
    assert basic.remote_tip() is None
    assert "src/extra.py" not in basic.remote_files("main")
    # The comment is fixed text: nothing from the repositories reaches it.
    assert "backend" not in publication.NO_OP_COMMENT and "extra.py" not in publication.NO_OP_COMMENT

    # A later change opens a fresh proposal rather than reviving the retired one.
    change_source(basic, "backend", "value = 6\n")
    basic.generate()
    again = basic.publish(client)
    assert again["status"] == "published" and again["pull_request"]["action"] == "created"


def test_a_no_op_run_deletes_a_stale_branch_whose_pull_request_was_closed_by_hand(basic) -> None:
    basic.generate()
    basic.commit_docs()
    original = reverted_to_documented_state(basic)
    client = FakePullRequests()
    basic.publish(client)
    client.open.clear()  # a maintainer closed the pull request

    git(basic.root / "sources" / "backend", "reset", "-q", "--hard", original)
    basic.generate()
    result = basic.publish(client)

    assert result["status"] == "no-op" and result["pull_request"] is None and result["branch_deleted"] is True
    assert client.closed == [] and basic.remote_tip() is None


def test_a_no_op_run_with_nothing_to_retire_changes_nothing(basic) -> None:
    basic.generate()
    basic.commit_docs()
    basic.generate()
    client = FakePullRequests()

    result = basic.publish(client)

    assert result["status"] == "no-op" and result["pull_request"] is None and result["branch_deleted"] is False
    assert client.mutations() == [] and basic.remote_tip() is None


def test_a_bot_branch_that_moved_is_not_deleted_by_a_no_op_run(basic, monkeypatch, tmp_path) -> None:
    basic.generate()
    basic.commit_docs()
    original = reverted_to_documented_state(basic)
    client = FakePullRequests()
    basic.publish(client)
    stale_tip = basic.remote_tip()

    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", basic.origin.as_uri(), str(other))
    git(other, "checkout", "-q", "-b", "scratch", "origin/" + DEFAULT_BRANCH)
    (other / "note.txt").write_text("manual\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "manual edit")
    git(other, "push", "-q", "origin", f"scratch:{DEFAULT_BRANCH}", "--force")
    moved_tip = basic.remote_tip()
    real_git = publication._git

    def stale_listing(root, *args, **kwargs):
        done = real_git(root, *args, **kwargs)
        if args[0] == "ls-remote":
            lines = [f"{stale_tip}\t{BOT_REF}" if line.endswith(BOT_REF) else line for line in done.stdout.splitlines()]
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr="")
        return done

    monkeypatch.setattr(publication, "_git", stale_listing)
    git(basic.root / "sources" / "backend", "reset", "-q", "--hard", original)
    basic.generate()
    client.calls.clear()
    with pytest.raises(PublicationError, match="git push failed"):
        basic.publish(client)

    # The failed lease leaves the moved branch and the active pull request exactly as they were.
    assert basic.remote_tip() == moved_tip
    assert client.open == {(DEFAULT_BRANCH, "main"): 1}
    assert client.closed == []
    assert not any(call[0] == "close" for call in client.calls), "a stale run must not even attempt to close the pull request"
    assert client.mutations() == []


def test_the_github_client_comments_then_closes_with_fixed_text_and_the_token_in_the_environment(tmp_path, monkeypatch) -> None:
    seen: list[dict] = []

    def fake_run(command, **kwargs):
        seen.append({"command": command, "env": kwargs["env"]})
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(publication.subprocess, "run", fake_run)
    GhPullRequests(tmp_path, "secret-token").close(12, publication.NO_OP_COMMENT)

    # The comment goes first: `gh pr close --comment` posts nothing when GitHub has already closed the pull request.
    assert [item["command"] for item in seen] == [
        ["gh", "pr", "comment", "12", "--body", publication.NO_OP_COMMENT],
        ["gh", "pr", "close", "12"],
    ]
    for item in seen:
        assert item["env"]["GH_TOKEN"] == "secret-token" and "secret-token" not in " ".join(item["command"])


def test_closing_a_pull_request_that_is_already_closed_is_not_an_error(tmp_path, monkeypatch) -> None:
    def run(stderr: str):
        def fake_run(command, **kwargs):
            failing = command[2] == "close"
            return subprocess.CompletedProcess(command, 1 if failing else 0, stdout="", stderr=stderr if failing else "")

        return fake_run

    monkeypatch.setattr(publication.subprocess, "run", run("! Pull request #12 is already closed"))
    GhPullRequests(tmp_path, "t").close(12, "comment")  # tolerated: GitHub closed it when the branch was deleted

    monkeypatch.setattr(publication.subprocess, "run", run("HTTP 403: resource not accessible"))
    with pytest.raises(PublicationError, match="HTTP 403"):
        GhPullRequests(tmp_path, "t").close(12, "comment")


# ------------------------------------------------------------------ retirement order and recovery
def open_proposal_then_revert(basic, client) -> None:
    """A documentation PR is open on the remote, and the sources have since returned to the documented state."""

    basic.generate()
    basic.commit_docs()
    original = reverted_to_documented_state(basic)
    assert basic.publish(client)["status"] == "published" and client.open == {(DEFAULT_BRANCH, "main"): 1}
    git(basic.root / "sources" / "backend", "reset", "-q", "--hard", original)
    basic.generate()
    client.calls.clear()


def test_retirement_deletes_the_branch_before_it_asks_github_to_close(basic) -> None:
    client = FakePullRequests()
    open_proposal_then_revert(basic, client)
    seen: list[str | None] = []
    client.on_close = lambda: seen.append(basic.remote_tip())

    result = basic.publish(client)

    assert result["pull_request"] == {"number": 1, "action": "closed"} and result["branch_deleted"] is True
    # When the close request arrived the leased deletion had already succeeded.
    assert seen == [None]
    assert [call[0] for call in client.calls] == ["find", "close"]  # one read, then the close


def test_a_failed_lookup_changes_nothing(basic) -> None:
    client = FakePullRequests()
    open_proposal_then_revert(basic, client)
    tip = basic.remote_tip()
    client.fail_find = 1

    with pytest.raises(PublicationError, match="gh pr list failed"):
        basic.publish(client)

    # The read comes first, so a failure leaves both the branch and the pull request alone.
    assert basic.remote_tip() == tip and client.open == {(DEFAULT_BRANCH, "main"): 1} and client.mutations() == []


def test_a_close_that_fails_after_the_branch_is_gone_is_finished_by_the_next_run(basic) -> None:
    client = FakePullRequests()
    open_proposal_then_revert(basic, client)
    client.fail_close = 1

    with pytest.raises(PublicationError, match=r"bot branch was deleted, but closing pull request #1 failed"):
        basic.publish(client)

    # Partial state: the branch is gone but the stand-in kept the pull request open.
    assert basic.remote_tip() is None and client.open == {(DEFAULT_BRANCH, "main"): 1}

    retry = basic.publish(client)

    assert retry["status"] == "no-op" and retry["branch_deleted"] is False
    assert retry["pull_request"] == {"number": 1, "action": "closed"}
    assert client.closed == [(1, publication.NO_OP_COMMENT)] and client.open == {}


def test_a_pull_request_github_closed_with_its_branch_needs_nothing_more_on_retry(basic) -> None:
    client = FakePullRequests()
    open_proposal_then_revert(basic, client)
    client.fail_close = 1
    with pytest.raises(PublicationError, match="closing pull request #1 failed"):
        basic.publish(client)

    client.open.clear()  # GitHub closes a pull request when its head branch is deleted
    client.calls.clear()
    retry = basic.publish(client)

    assert retry == {"status": "no-op", "branch": DEFAULT_BRANCH, "base": "main", "changes": [], "pull_request": None, "branch_deleted": False}
    assert client.mutations() == [] and basic.remote_tip() is None


def test_repeating_a_completed_retirement_is_safe(basic) -> None:
    client = FakePullRequests()
    open_proposal_then_revert(basic, client)
    assert basic.publish(client)["pull_request"] == {"number": 1, "action": "closed"}
    client.calls.clear()

    for _ in range(2):
        again = basic.publish(client)
        assert again["status"] == "no-op" and again["pull_request"] is None and again["branch_deleted"] is False

    assert client.mutations() == [] and basic.remote_tip() is None
