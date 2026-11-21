"""Safe source acquisition for web workspaces: local roots, pinned public GitHub checkouts, hostile input, limits."""

from __future__ import annotations

import copy
import hashlib
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import copy_fixture, git, init_git_repository
from repodocs.config import ProjectConfig
from repodocs.scanner import scan_project
from repodocs.web import workspaces
from repodocs.web.workspaces import (
    GitHubSource,
    LocalSource,
    SourceLimits,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceSettings,
    fetch_bounded,
    run_bounded,
)


def tree_of(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def tree_document(repository: Path) -> dict[str, Any]:
    """The GitHub recursive-tree document for HEAD, built from the real fixture repository."""

    entries = []
    for line in git(repository, "ls-tree", "-r", "-t", "-l", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha, size = meta.split()
        entry: dict[str, Any] = {"path": path, "mode": mode, "type": kind, "sha": sha}
        if kind == "blob":
            entry["size"] = int(size)
        entries.append(entry)
    return {"sha": git(repository, "rev-parse", "HEAD^{tree}"), "truncated": False, "tree": entries}


class Remote:
    """A trusted local stand-in for GitHub: serves the tree listing and lets real Git fetch over file://."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.sha = git(repository, "rev-parse", "HEAD")
        self.document = tree_document(repository)
        self.urls: list[str] = []
        self.git_calls: list[list[str]] = []
        self.git_environments: list[dict[str, str]] = []

    def http_get(self, url: str, timeout: float, max_bytes: int) -> bytes:
        self.urls.append(url)
        return json.dumps(self.document).encode()

    def run_git(self, args: list[str], **kwargs: Any) -> str:
        self.git_calls.append(list(args))
        self.git_environments.append(dict(kwargs["env"]))
        args = [self.repository.as_uri() if item.startswith("https://github.com/") else item for item in args]
        args[1:1] = ["-c", "protocol.file.allow=always"]
        kwargs["env"] = {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "file"}
        return run_bounded(args, **kwargs)


def build_remote(directory: Path) -> Path:
    repository = copy_fixture(directory / "remote")
    init_git_repository(repository)
    return repository


@pytest.fixture(scope="module")
def shared_remote(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_remote(tmp_path_factory.mktemp("shared"))


@pytest.fixture
def remote(shared_remote: Path) -> Remote:
    return Remote(shared_remote)


def make_manager(tmp_path: Path, remote: Remote | None = None, *, roots: dict[str, Path] | None = None, **limits: int) -> WorkspaceManager:
    settings = WorkspaceSettings(tmp_path / "store", roots or {}, SourceLimits(**limits))
    if remote is None:
        return WorkspaceManager(settings)
    return WorkspaceManager(settings, http_get=remote.http_get, run_git=remote.run_git)


def local_checkout(tmp_path: Path, name: str = "backend") -> Path:
    repository = copy_fixture(tmp_path / "approved" / name)
    init_git_repository(repository)
    return repository


# --- positive paths and provenance ------------------------------------------------------------


def test_single_local_source_gives_commit_provenance_and_owned_destination(tmp_path: Path) -> None:
    checkout = local_checkout(tmp_path)
    sha = git(checkout, "rev-parse", "HEAD")
    before = tree_of(checkout)
    workspace = make_manager(tmp_path, roots={"backend": checkout}).acquire("Demo", [LocalSource("backend")])

    config = ProjectConfig.load(workspace.root)
    assert workspace.root.parent == (tmp_path / "store").resolve()
    assert config.destination_root() == workspace.root / "destination"
    assert config.output_path.is_relative_to(workspace.root)
    assert [spec.id for spec in config.source_repositories()] == ["backend"]
    ir, _ = scan_project(config)
    snapshot = ir["snapshots"][0]
    assert snapshot["revision"] == sha and snapshot["revision_kind"] == "commit"
    assert workspace.sources[0].git["git_revision"] == sha
    assert "approved" not in (workspace.root / workspaces.MARKER_NAME).read_text(encoding="utf-8")
    assert tree_of(checkout) == before


def test_dirty_local_tree_uses_content_fingerprint_and_is_never_modified(tmp_path: Path) -> None:
    checkout = local_checkout(tmp_path)
    sha = git(checkout, "rev-parse", "HEAD")
    (checkout / "README.md").write_text("uncommitted edit\n", encoding="utf-8")
    (checkout / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    before = tree_of(checkout)
    workspace = make_manager(tmp_path, roots={"backend": checkout}).acquire("Demo", [LocalSource("backend")])

    assert workspace.sources[0].git["working_tree_state"] == "dirty"
    snapshot = scan_project(workspace.config)[0]["snapshots"][0]
    assert snapshot["revision_kind"] == "content" and snapshot["revision"] != sha
    assert tree_of(checkout) == before
    make_manager(tmp_path, roots={"backend": checkout}).cleanup(workspace.workspace_id)
    assert tree_of(checkout) == before


def test_non_git_local_tree_never_gets_a_commit_revision(tmp_path: Path) -> None:
    plain = copy_fixture(tmp_path / "approved" / "plain")
    workspace = make_manager(tmp_path, roots={"plain": plain}).acquire("Demo", [LocalSource("plain")])

    assert workspace.sources[0].git["git_revision"] is None
    snapshot = scan_project(workspace.config)[0]["snapshots"][0]
    assert snapshot["revision_kind"] == "content" and snapshot["git_revision"] is None


def test_pinned_github_checkout_and_local_source_share_one_workspace(tmp_path: Path, remote: Remote) -> None:
    checkout = local_checkout(tmp_path, "frontend")
    manager = make_manager(tmp_path, remote, roots={"frontend": checkout})
    workspace = manager.acquire("Shop", [LocalSource("frontend"), GitHubSource("Octo-Org", "shop.api", remote.sha)])

    assert remote.urls == [f"https://api.github.com/repos/Octo-Org/shop.api/git/trees/{remote.sha}?recursive=1"]
    ir, _ = scan_project(workspace.config)
    revisions = sorted((item["revision_kind"], item["revision"]) for item in ir["snapshots"])
    assert ("commit", remote.sha) in revisions and len(revisions) == 2
    record = {item.kind: item for item in workspace.sources}
    assert record["github"].requested_revision == remote.sha
    assert record["github"].git["git_revision"] == remote.sha
    assert record["github"].git["working_tree_state"] == "clean"
    assert record["github"].git["head_state"] == "detached"
    assert "github.com" not in (workspace.root / "sources" / "octo-org-shop-api" / ".git" / "config").read_text(encoding="utf-8")


def test_pinned_github_scan_in_pinned_mode_accepts_the_checkout(tmp_path: Path, remote: Remote) -> None:
    workspace = make_manager(tmp_path, remote).acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])

    ir, _ = scan_project(workspace.config, mode="pinned")
    assert ir["snapshots"][0]["revision"] == remote.sha


def test_git_runs_hardened_with_a_scrubbed_environment(tmp_path: Path, remote: Remote, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GIT_SSH_COMMAND", "calc")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    make_manager(tmp_path, remote).acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])

    fetch = next(call for call in remote.git_calls if "fetch" in call)
    assert "--depth=1" in fetch and "--no-recurse-submodules" in fetch
    assert fetch[fetch.index("--") + 1] == "https://github.com/octo/shop.git" and fetch[-1] == remote.sha
    for call in remote.git_calls:
        for setting in ("core.fsmonitor=false", f"core.hooksPath={os.devnull}", "protocol.allow=never", "http.followRedirects=false", "credential.helper="):
            assert setting in call
    for environment in remote.git_environments:
        assert not {"GITHUB_TOKEN", "GIT_SSH_COMMAND", "HTTPS_PROXY"} & set(environment)
        assert environment["GIT_TERMINAL_PROMPT"] == "0" and environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert any("checkout" in call and "--detach" in call and "--no-recurse-submodules" in call for call in remote.git_calls)


def test_hooks_and_filters_in_a_fetched_repository_do_not_run(tmp_path: Path) -> None:
    remote = Remote(build_remote(tmp_path))
    marker = tmp_path / "ran.txt"
    (remote.repository / ".gitattributes").write_text("*.md filter=evil\n", encoding="utf-8")
    (remote.repository / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = https://evil.invalid/x.git\n', encoding="utf-8")
    git(remote.repository, "config", "filter.evil.smudge", f"python -c \"open(r'{marker}','w')\"")
    hooks = remote.repository / ".git" / "hooks"
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {marker.as_posix()}\n", encoding="utf-8")
    git(remote.repository, "add", "-A")
    git(remote.repository, "commit", "-q", "-m", "attributes")
    remote.sha = git(remote.repository, "rev-parse", "HEAD")
    remote.document = tree_document(remote.repository)

    workspace = make_manager(tmp_path, remote).acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    assert not marker.exists()
    assert not (workspace.root / "sources" / "octo-shop" / ".git" / "modules").exists()


# --- hostile input ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["../backend", "/etc", "C:\\Windows", "backend/..", "Backend", "", "unknown", "backend\n"])
def test_local_sources_are_chosen_by_configured_name_only(tmp_path: Path, name: str) -> None:
    manager = make_manager(tmp_path, roots={"backend": local_checkout(tmp_path)})
    with pytest.raises(WorkspaceError) as caught:
        manager.acquire("Demo", [LocalSource(name)])
    assert caught.value.failures and str(tmp_path) not in str(caught.value)
    assert manager.owned_workspaces() == {}


@pytest.mark.parametrize(
    "owner, repository",
    [
        ("https://evil.example", "repo"), ("evil.example/x", "repo"), ("octo", "https://github.com/a/b"), ("octo", "a/b"),
        ("octo", "--upload-pack=calc"), ("-octo", "repo"), ("octo-", "repo"), ("a--", "repo"), ("user@host", "repo"),
        ("octo", ".."), ("octo", "."), ("octo", "repo.git"), ("octo", "repo?x=1"), ("octo", "repo#f"), ("octo", "re po"),
        ("octo", "repo\n"), ("octo", "repo%2f.."), ("o" * 40, "repo"), ("octo", "r" * 101), ("octo", ""), ("", "repo"),
        ("octo", "..\\x"), ("127.0.0.1", "repo"),
    ],
)
def test_hostile_github_names_are_rejected_before_any_network_or_git(tmp_path: Path, remote: Remote, owner: str, repository: str) -> None:
    manager = make_manager(tmp_path, remote)
    with pytest.raises(WorkspaceError):
        manager.acquire("Demo", [GitHubSource(owner, repository, remote.sha)])
    assert remote.urls == [] and remote.git_calls == [] and manager.owned_workspaces() == {}


@pytest.mark.parametrize("revision", ["main", "HEAD", "abc123", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n", " " + "a" * 40, "--" + "a" * 38, "a" * 40 + "^"])
def test_only_an_exact_lowercase_full_sha_is_accepted(tmp_path: Path, remote: Remote, revision: str) -> None:
    with pytest.raises(WorkspaceError, match="40-character"):
        make_manager(tmp_path, remote).acquire("Demo", [GitHubSource("octo", "shop", revision)])
    assert remote.urls == []


def test_duplicate_and_reserved_source_ids_are_rejected(tmp_path: Path, remote: Remote) -> None:
    roots = {"documentation": local_checkout(tmp_path, "d"), "octo-shop": local_checkout(tmp_path, "o")}
    manager = make_manager(tmp_path, remote, roots=roots)
    for sources in (
        [LocalSource("documentation")],
        [LocalSource("octo-shop"), GitHubSource("octo", "shop", remote.sha)],
        [GitHubSource("octo", "shop", remote.sha), GitHubSource("Octo", "Shop", remote.sha)],
    ):
        with pytest.raises(WorkspaceError, match="distinct|once"):
            manager.acquire("Demo", sources)
    assert remote.urls == []


def test_project_name_and_source_count_are_bounded(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, roots={"a": local_checkout(tmp_path, "a")}, max_sources=1)
    for name in ("", "x" * 81, "bad\nname"):
        with pytest.raises(WorkspaceError, match="Project name"):
            manager.acquire(name, [LocalSource("a")])
    with pytest.raises(WorkspaceError, match="Between 1 and 1"):
        manager.acquire("Demo", [LocalSource("a"), LocalSource("a")])
    with pytest.raises(WorkspaceError, match="Between 1 and 1"):
        manager.acquire("Demo", [])


def test_local_roots_may_not_overlap_the_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    for root in (store, store / "inside", tmp_path):
        with pytest.raises(WorkspaceError, match="workspace store"):
            WorkspaceSettings(store, {"r": root})
    with pytest.raises(WorkspaceError, match="Local root name"):
        WorkspaceSettings(store, {"../x": tmp_path / "x"})


def test_limits_must_be_positive_integers() -> None:
    for value in (0, -1, True, 1.5):
        with pytest.raises(WorkspaceError):
            SourceLimits(max_files=value)  # type: ignore[arg-type]


# --- hostile tree shapes and resource bounds (checked before Git runs) -----------------------


def blob(path: str, size: int = 1, mode: str = "100644") -> dict[str, Any]:
    return {"path": path, "mode": mode, "type": "blob", "sha": "a" * 40, "size": size}


def shape(remote: Remote, **changes: Any) -> None:
    remote.document = {**copy.deepcopy(remote.document), **changes}


HOSTILE_TREES = {
    "truncated": ({"truncated": True}, "too large"),
    "truncated-missing": ({"truncated": None}, "too large"),
    "symlink": ({"tree": [blob("link", mode="120000")]}, "symbolic link"),
    "submodule": ({"tree": [{"path": "vendor", "mode": "160000", "type": "commit", "sha": "b" * 40}]}, "submodule"),
    "traversal": ({"tree": [blob("a/../../evil")]}, "traversal"),
    "dotdot": ({"tree": [blob("..")]}, "traversal"),
    "absolute": ({"tree": [blob("/etc/passwd")]}, "absolute"),
    "backslash": ({"tree": [blob("a\\b")]}, "backslashes"),
    "dot-git": ({"tree": [blob(".git/config")]}, ".git"),
    "dot-git-case": ({"tree": [blob("x/.GIT/hooks/pre-commit")]}, ".git"),
    "drive": ({"tree": [blob("c:evil")]}, "drive"),
    "stream": ({"tree": [blob("a.txt:hidden")]}, "drive"),
    "trailing-dot": ({"tree": [blob("evil.")]}, "trailing-dot"),
    "control": ({"tree": [blob("a\x00b")]}, "control"),
    "empty-component": ({"tree": [blob("a//b")]}, "traversal"),
    "case-collision": ({"tree": [blob("README"), blob("readme")]}, "case-insensitive"),
    "negative-size": ({"tree": [blob("a", -1)]}, "no usable size"),
    "bool-size": ({"tree": [blob("a", True)]}, "no usable size"),
    "missing-size": ({"tree": [{"path": "a", "mode": "100644", "type": "blob", "sha": "a" * 40}]}, "no usable size"),
    "odd-mode": ({"tree": [blob("a", mode="100666")]}, "unsupported"),
    "entry-not-object": ({"tree": ["a"]}, "malformed"),
    "no-tree-sha": ({"sha": "xyz"}, "object ID"),
    "tree-not-list": ({"tree": {}}, "unexpected"),
}


@pytest.mark.parametrize("name", sorted(HOSTILE_TREES))
def test_hostile_tree_is_refused_before_git_and_leaves_nothing(tmp_path: Path, remote: Remote, name: str) -> None:
    changes, expected = HOSTILE_TREES[name]
    shape(remote, **changes)
    manager = make_manager(tmp_path, remote)
    with pytest.raises(WorkspaceError, match=expected) as caught:
        manager.acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    assert remote.git_calls == []
    assert caught.value.failures[0].source == "octo/shop"
    assert manager.owned_workspaces() == {} and list((tmp_path / "store").iterdir()) == []


@pytest.mark.parametrize(
    "limits, tree, expected",
    [
        ({"max_tree_entries": 2}, [blob("a"), blob("b"), blob("c")], "tree entries"),
        ({"max_files": 2}, [blob("a"), blob("b"), blob("c")], "more than 2 files"),
        ({"max_file_bytes": 10}, [blob("a", 11)], "file limit"),
        ({"max_total_bytes": 10}, [blob("a", 6), blob("b", 6)], "bytes of files"),
    ],
)
def test_oversized_sources_are_refused_before_git(tmp_path: Path, remote: Remote, limits: dict[str, int], tree: list, expected: str) -> None:
    shape(remote, tree=tree)
    with pytest.raises(WorkspaceError, match=expected):
        make_manager(tmp_path, remote, **limits).acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    assert remote.git_calls == []


def test_non_json_and_oversized_responses_are_refused(tmp_path: Path, remote: Remote) -> None:
    manager = make_manager(tmp_path, remote)
    manager._http_get = lambda url, timeout, limit: b"<html>"
    with pytest.raises(WorkspaceError, match="not valid JSON"):
        manager.acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    manager._http_get = lambda url, timeout, limit: b"{" * 100_000
    with pytest.raises(WorkspaceError, match="not valid JSON|exceeded"):
        manager.acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    manager._http_get = lambda url, timeout, limit: b" " * (manager.settings.limits.max_response_bytes + 1)
    with pytest.raises(WorkspaceError, match="exceeded"):
        manager.acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    assert remote.git_calls == []


def test_fetched_commit_must_match_the_listed_tree(tmp_path: Path, remote: Remote) -> None:
    shape(remote, sha="c" * 40)
    manager = make_manager(tmp_path, remote)
    with pytest.raises(WorkspaceError, match="does not match the tree"):
        manager.acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    assert manager.owned_workspaces() == {} and list((tmp_path / "store").iterdir()) == []


def test_unreachable_commit_is_an_actionable_failure_and_cleans_up(tmp_path: Path, remote: Remote) -> None:
    manager = make_manager(tmp_path, remote)
    with pytest.raises(WorkspaceError, match="Git failed") as caught:
        manager.acquire("Shop", [GitHubSource("octo", "shop", "d" * 40)])
    assert caught.value.failures[0].source == "octo/shop"
    assert list((tmp_path / "store").iterdir()) == []


# --- the bounded HTTP client ------------------------------------------------------------------


class Server:
    def __init__(self, handler: type[http.server.BaseHTTPRequestHandler]) -> None:
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve():
    servers: list[Server] = []

    def start(handler: type[http.server.BaseHTTPRequestHandler]) -> str:
        servers.append(Server(handler))
        return servers[-1].url

    yield start
    for server in servers:
        server.close()


def handler_for(status: int, body: bytes = b"{}", headers: dict[str, str] | None = None, delay: float = 0, seen: list | None = None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if seen is not None:
                seen.append(self.path)
            time.sleep(delay)
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def test_fetch_returns_a_small_response(serve) -> None:
    assert fetch_bounded(serve(handler_for(200, b'{"a":1}')), 5, 100) == b'{"a":1}'


def test_redirects_are_not_followed(serve) -> None:
    seen: list[str] = []
    target = serve(handler_for(200, b"{}", seen=seen)) + "secret"
    start = serve(handler_for(302, b"", {"Location": target}))
    with pytest.raises(WorkspaceError, match="redirect"):
        fetch_bounded(start, 5, 100)
    assert seen == []


def test_oversized_response_is_discarded(serve) -> None:
    with pytest.raises(WorkspaceError, match="exceeded 100 bytes"):
        fetch_bounded(serve(handler_for(200, b"x" * 1000)), 5, 100)


def test_slow_response_times_out(serve) -> None:
    started = time.monotonic()
    with pytest.raises(WorkspaceError, match="failed|exceeded"):
        fetch_bounded(serve(handler_for(200, delay=3)), 0.5, 100)
    assert time.monotonic() - started < 2.5


@pytest.mark.parametrize("status, text", [(404, "private"), (403, "rate-limited"), (429, "rate-limited"), (500, "HTTP 500")])
def test_http_errors_become_actionable_messages(serve, status: int, text: str) -> None:
    with pytest.raises(WorkspaceError, match=text):
        fetch_bounded(serve(handler_for(status)), 5, 100)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.invalid/x", "javascript:1", "//example.invalid"])
def test_only_http_urls_are_fetched(url: str) -> None:
    with pytest.raises(WorkspaceError, match="http"):
        fetch_bounded(url, 1, 10)


def test_unreachable_host_fails_cleanly() -> None:
    with pytest.raises(WorkspaceError, match="network request failed"):
        fetch_bounded("http://127.0.0.1:1/", 1, 10)


# --- monitored processes ----------------------------------------------------------------------


def test_a_runaway_process_is_killed_at_the_deadline(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(WorkspaceError, match="exceeded 1 seconds"):
        run_bounded([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, env=dict(os.environ), timeout=1, watch=tmp_path, max_bytes=10**9)
    assert time.monotonic() - started < 10


def test_a_process_filling_the_disk_is_killed_at_the_cap(tmp_path: Path) -> None:
    script = "import time\nf = open('big.bin', 'wb')\nwhile True:\n    f.write(b'x' * 65536); f.flush(); time.sleep(0.01)\n"
    started = time.monotonic()
    with pytest.raises(WorkspaceError, match="bytes of disk"):
        run_bounded([sys.executable, "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=60, watch=tmp_path, max_bytes=500_000)
    assert time.monotonic() - started < 30
    assert (tmp_path / "big.bin").stat().st_size < 50_000_000


def test_run_bounded_reports_failures_and_output(tmp_path: Path) -> None:
    assert run_bounded([sys.executable, "-c", "print('ok')"], cwd=tmp_path, env=dict(os.environ), timeout=30, watch=tmp_path, max_bytes=10**6) == "ok"
    with pytest.raises(WorkspaceError, match="exit code 3"):
        run_bounded([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], cwd=tmp_path, env=dict(os.environ), timeout=30, watch=tmp_path, max_bytes=10**6)


def test_interrupted_acquisition_cleans_up_its_workspace(tmp_path: Path, remote: Remote) -> None:
    manager = make_manager(tmp_path, remote)

    def interrupted(*args: Any, **kwargs: Any) -> str:
        raise KeyboardInterrupt

    manager._run_git = interrupted
    with pytest.raises(KeyboardInterrupt):
        manager.acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    assert list((tmp_path / "store").iterdir()) == []


def test_checkout_disk_cap_stops_a_real_fetch_and_cleans_up(tmp_path: Path, remote: Remote) -> None:
    manager = make_manager(tmp_path, remote, max_checkout_bytes=1)
    with pytest.raises(WorkspaceError, match="bytes of disk"):
        manager.acquire("Shop", [GitHubSource("octo", "shop", remote.sha)])
    assert list((tmp_path / "store").iterdir()) == []


# --- cleanup and retention --------------------------------------------------------------------


def test_cleanup_removes_only_a_verified_owned_workspace(tmp_path: Path, remote: Remote) -> None:
    checkout = local_checkout(tmp_path)
    manager = make_manager(tmp_path, remote, roots={"backend": checkout})
    workspace = manager.acquire("Shop", [LocalSource("backend"), GitHubSource("octo", "shop", remote.sha)])
    before = tree_of(checkout)

    assert manager.cleanup(workspace.workspace_id) is True
    assert not workspace.root.exists()
    assert tree_of(checkout) == before and checkout.is_dir()
    assert manager.cleanup(workspace.workspace_id) is False


def test_cleanup_refuses_unmarked_foreign_and_malformed_targets(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    store = manager.settings.store_root
    store.mkdir()
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "docs.md").write_text("human docs\n", encoding="utf-8")
    unmarked = store / ("ws-" + "a" * 32)
    unmarked.mkdir()
    (unmarked / "keep.txt").write_text("x", encoding="utf-8")
    wrong_id = store / ("ws-" + "b" * 32)
    wrong_id.mkdir()
    (wrong_id / workspaces.MARKER_NAME).write_text(json.dumps({"kind": workspaces.MARKER_KIND, "workspace_id": "ws-" + "c" * 32}), encoding="utf-8")
    wrong_kind = store / ("ws-" + "d" * 32)
    wrong_kind.mkdir()
    (wrong_kind / workspaces.MARKER_NAME).write_text(json.dumps({"kind": "other", "workspace_id": wrong_kind.name}), encoding="utf-8")
    bad_json = store / ("ws-" + "e" * 32)
    bad_json.mkdir()
    (bad_json / workspaces.MARKER_NAME).write_text("not json", encoding="utf-8")

    for target in ("../precious", str(precious), unmarked.name, wrong_id.name, wrong_kind.name, bad_json.name, "..", ".", "", "ws-" + "f" * 32, "ws-" + "A" * 32):
        assert manager.cleanup(target) is False
    assert (precious / "docs.md").read_text(encoding="utf-8") == "human docs\n"
    assert unmarked.is_dir() and wrong_id.is_dir() and wrong_kind.is_dir() and bad_json.is_dir()
    assert manager.prune() == []


def test_cleanup_refuses_a_symlinked_workspace(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    store = manager.settings.store_root
    store.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    name = "ws-" + "1" * 32
    (victim / workspaces.MARKER_NAME).write_text(json.dumps({"kind": workspaces.MARKER_KIND, "workspace_id": name}), encoding="utf-8")
    try:
        (store / name).symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")
    assert manager.cleanup(name) is False
    assert (victim / workspaces.MARKER_NAME).exists()


def test_retention_prunes_expired_workspaces_and_enforces_a_count(tmp_path: Path) -> None:
    checkout = local_checkout(tmp_path)
    now = [1_000_000.0]
    settings = WorkspaceSettings(tmp_path / "store", {"backend": checkout}, SourceLimits(max_workspaces=2, retention_seconds=100))
    manager = WorkspaceManager(settings, clock=lambda: now[0])
    first = manager.acquire("A", [LocalSource("backend")])
    now[0] += 60
    second = manager.acquire("B", [LocalSource("backend")])
    with pytest.raises(WorkspaceError, match="workspace limit"):
        manager.acquire("C", [LocalSource("backend")])
    now[0] += 60
    third = manager.acquire("C", [LocalSource("backend")])

    assert not first.root.exists() and second.root.exists() and third.root.exists()
    now[0] += 1000
    assert sorted(manager.prune()) == sorted([second.workspace_id, third.workspace_id])
    assert list(settings.store_root.iterdir()) == []
