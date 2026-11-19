"""Source links, the evidence map, and the static HTML export of an architecture view."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from repodocs.architecture.discovery import discover, save_view, saved_context_path
from repodocs.architecture.export import HTML_NAME, architecture_artifacts, build_html, mermaid_bundle
from repodocs.architecture.navigation import MAP_NAME, SOURCE_LINK, evidence_map, github_repository, source_link
from repodocs.architecture.render import ALTERNATIVES_NAME, render_alternatives, render_view
from repodocs.architecture.render_check import TOOL_DIRECTORY, find_browser, find_renderer
from repodocs.config import ProjectConfig
from repodocs.errors import ConfigError, OwnershipError
from repodocs.generator import generate_docs

from conftest import git, init_git_repository, invoke, read_ir, read_manifest
from test_architecture_discovery import Scripted, component, connection, excerpt_ids, full_answer

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "express-shop-repo"
SHA = "0123456789abcdef0123456789abcdef01234567"
TOKEN = "ghs_" + "T0kenValue" * 3


# ------------------------------------------------------------------ remotes and links
@pytest.mark.parametrize("remote, expected", [
    ("https://github.com/acme/shop", ("acme", "shop")),
    ("https://github.com/acme/shop.git", ("acme", "shop")),
    ("https://github.com/acme/shop/", ("acme", "shop")),
    ("git@github.com:acme/shop.git", ("acme", "shop")),
    ("ssh://git@github.com/acme/shop.git", ("acme", "shop")),
    (f"https://x-access-token:{TOKEN}@github.com/acme/shop.git", ("acme", "shop")),
    ("https://github.com/acme/my.repo-2", ("acme", "my.repo-2")),
])
def test_github_remotes_are_recognized_in_every_common_form(remote: str, expected: tuple[str, str]) -> None:
    assert github_repository(remote) == expected


@pytest.mark.parametrize("remote", [
    None, "", "https://gitlab.com/acme/shop", "https://github.com.evil.example/acme/shop", "https://evil.example/github.com/acme/shop",
    "http://github.com/acme/shop", "file:///github.com/acme/shop", "javascript:alert(1)", "https://github.com/acme", "https://github.com/acme/shop/tree/main",
    "https://github.com/../shop", "https://github.com/acme/..", "git@github.com.evil.example:acme/shop.git", "https://github.com/acme/shop?x=1", "https://github.com/a b/shop",
])
def test_other_hosts_and_malformed_remotes_get_no_links(remote) -> None:
    assert github_repository(remote) is None


def commit_snapshot(prefix: str = "") -> dict[str, Any]:
    return {"revision": SHA, "revision_kind": "commit", "git_path_prefix": prefix}


@pytest.mark.parametrize("path, lines, expected", [
    ("server.js", (1, 5), f"https://github.com/acme/shop/blob/{SHA}/server.js#L1-L5"),
    ("server.js", (7, 7), f"https://github.com/acme/shop/blob/{SHA}/server.js#L7"),
    ("server.js", (None, None), f"https://github.com/acme/shop/blob/{SHA}/server.js"),
    ("lib/my file #1?.js", (2, 3), f"https://github.com/acme/shop/blob/{SHA}/lib/my%20file%20%231%3F.js#L2-L3"),
    ("docs/naïve é.md", (1, 1), f"https://github.com/acme/shop/blob/{SHA}/docs/na%C3%AFve%20%C3%A9.md#L1"),
    ("a/b%20c.js", (1, 2), f"https://github.com/acme/shop/blob/{SHA}/a/b%2520c.js#L1-L2"),
])
def test_links_pin_the_commit_and_encode_paths_and_spans_exactly(path, lines, expected) -> None:
    link = source_link(("acme", "shop"), commit_snapshot(), path, *lines)

    assert link == expected and SOURCE_LINK.match(link)


def test_a_subdirectory_of_a_larger_repository_links_with_its_prefix() -> None:
    assert source_link(("acme", "mono"), commit_snapshot("services/shop"), "server.js", 3, 4) == f"https://github.com/acme/mono/blob/{SHA}/services/shop/server.js#L3-L4"


@pytest.mark.parametrize("snapshot, path, repository", [
    ({"revision": "f" * 64, "revision_kind": "content", "git_path_prefix": ""}, "server.js", ("acme", "shop")),
    ({"revision": SHA, "revision_kind": "content"}, "server.js", ("acme", "shop")),
    ({"revision": SHA[:7], "revision_kind": "commit"}, "server.js", ("acme", "shop")),
    ({"revision": SHA.upper(), "revision_kind": "commit"}, "server.js", ("acme", "shop")),
    (commit_snapshot(), "../../etc/passwd", ("acme", "shop")),
    (commit_snapshot(), "/etc/passwd", ("acme", "shop")),
    (commit_snapshot(), "a\\b.js", ("acme", "shop")),
    (commit_snapshot(), "a/../b.js", ("acme", "shop")),
    (commit_snapshot(), "", ("acme", "shop")),
    (commit_snapshot(), "server.js", None),
], ids=["working tree", "working tree with a commit-like value", "short sha", "uppercase sha", "traversal", "absolute", "backslash", "dot-dot", "empty", "no remote"])
def test_no_link_is_offered_when_it_would_be_misleading_or_unsafe(snapshot, path, repository) -> None:
    assert source_link(repository, snapshot, path, 1, 2) is None


# ------------------------------------------------------------------ end to end on real repositories
def make_repository(base: Path, name: str, remote: str | None, *, source: Path = FIXTURE, subdirectory: str = "") -> tuple[Path, Path]:
    """A git repository holding a copy of the fixture (optionally in a subdirectory); returns (git root, registered path)."""

    root = base / name
    registered = root / subdirectory if subdirectory else root
    shutil.copytree(source, registered)
    (registered / "lib").mkdir(exist_ok=True)
    (registered / "lib" / "my file #1.js").write_text("module.exports = {};\n", encoding="utf-8")
    init_git_repository(root)
    if remote:
        git(root, "remote", "add", "origin", remote)
    return root, registered


def scanned(tmp_path: Path, capsys, repositories: dict[str, Path], *, html: bool = False) -> tuple[Path, ProjectConfig, dict]:
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    for index, (identifier, path) in enumerate(sorted(repositories.items())):
        arguments = ["add", str(path), "--id", identifier] + (["--destination"] if index == 0 else [])
        assert invoke(root, capsys, *arguments)[0] == 0
    if html:
        configuration = json.loads((root / "repodocs.json").read_text(encoding="utf-8"))
        configuration["architecture"] = {"html_export": True}
        (root / "repodocs.json").write_text(json.dumps(configuration), encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0
    return root, ProjectConfig.load(root), read_ir(root)


def helpers_answer(message: dict[str, Any]) -> dict[str, Any]:
    ids = excerpt_ids(message)
    data = full_answer(message)
    data["components"].append(component("Helpers", "library", ["lib/my file #1.js"], [ids["lib/my file #1.js"]], confidence="medium"))
    return data


def ask_for_helpers(message: dict[str, Any]) -> dict[str, Any]:
    return {"components": [], "connections": [], "alternatives": [], "complete": False,
            "requests": [{"repository_id": "shop", "path": "lib/my file #1.js", "line_start": 1, "line_end": 5}]}


def store(config: ProjectConfig, ir: dict[str, Any], *script: Any) -> dict[str, Any]:
    view, context = discover(config, ir, Scripted(*script))
    save_view(config, view, context)
    return view


def test_a_committed_checkout_links_every_citation_to_its_exact_commit_and_never_leaks_the_remote_credentials(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", f"https://x-access-token:{TOKEN}@github.com/acme/shop.git")
    sha = git(root_git, "rev-parse", "HEAD")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    store(config, ir, ask_for_helpers, helpers_answer)

    artifacts, warnings = architecture_artifacts(config, ir)

    assert warnings == [] and set(artifacts) == {"architecture-view.mmd", ALTERNATIVES_NAME, MAP_NAME}
    evidence = json.loads(artifacts[MAP_NAME])
    helpers = next(item for item in evidence["components"].values() if any("my file" in file["path"] for file in item["files"]))
    assert helpers["files"][0]["link"] == f"https://github.com/acme/shop/blob/{sha}/lib/my%20file%20%231.js"
    cited = helpers["evidence"][0]
    assert cited["state"] == "committed" and cited["link"] == f"https://github.com/acme/shop/blob/{sha}/lib/my%20file%20%231.js#L1"
    every_link = [item["link"] for entry in evidence["components"].values() for item in entry["evidence"] + entry["files"]]
    every_link += [item["link"] for entry in evidence["connections"].values() for item in entry["evidence"]]
    assert every_link and all(link is None or SOURCE_LINK.match(link) for link in every_link)
    assert evidence["repositories"][0]["remote"] == "acme/shop" and evidence["repositories"][0]["revision"] == sha
    assert all(TOKEN not in text for text in artifacts.values()), "the remote's credentials reached an output"
    assert "x-access-token" not in "".join(artifacts.values())


def test_several_repositories_link_to_their_own_commits_and_remotes(tmp_path: Path, capsys) -> None:
    shop_root, shop = make_repository(tmp_path, "shop", "git@github.com:acme/shop.git")
    billing_source = tmp_path / "billing-source"
    (billing_source / "src").mkdir(parents=True)
    (billing_source / "package.json").write_text('{"name": "billing", "dependencies": {"express": "4"}}\n', encoding="utf-8")
    (billing_source / "server.js").write_text("const express = require('express');\napp.post('/pay');\n", encoding="utf-8")
    billing_root, billing = make_repository(tmp_path, "billing", "https://github.com/acme/billing", source=billing_source)
    root, config, ir = scanned(tmp_path, capsys, {"shop": shop, "billing": billing})

    def both(message: dict[str, Any]) -> dict[str, Any]:
        ids = excerpt_ids(message)
        by_repository = {(item["repository_id"], item["path"]): item["id"] for item in message["excerpts"]}
        return {
            "components": [
                component("Storefront", "frontend", ["public/app.js"], [by_repository[("shop", "public/app.js")]]),
                component("Payments", "backend-service", ["server.js"], [by_repository[("billing", "server.js")]], repository="billing"),
            ],
            "connections": [connection("Storefront", "Payments", "calls", "POST /pay", [by_repository[("shop", "public/app.js")], by_repository[("billing", "server.js")]])],
            "alternatives": [], "requests": [], "complete": True,
        }

    view = store(config, ir, both)
    artifacts, _ = architecture_artifacts(config, ir)

    evidence = json.loads(artifacts[MAP_NAME])
    shop_sha, billing_sha = git(shop_root, "rev-parse", "HEAD"), git(billing_root, "rev-parse", "HEAD")
    by_name = {item["name"]: item for item in view["components"]}
    storefront = evidence["components"][by_name["Storefront"]["id"]]
    payments = evidence["components"][by_name["Payments"]["id"]]
    assert storefront["evidence"][0]["link"].startswith(f"https://github.com/acme/shop/blob/{shop_sha}/public/app.js")
    assert payments["evidence"][0]["link"].startswith(f"https://github.com/acme/billing/blob/{billing_sha}/server.js")
    link = next(iter(evidence["connections"].values()))["evidence"]
    assert {item["link"].split("/blob/")[0] for item in link} == {"https://github.com/acme/shop", "https://github.com/acme/billing"}
    assert view["connections"][0]["cross_repository"] is True and view["connections"][0]["classification"] == "proposed"
    assert "(unconfirmed)" in artifacts["architecture-view.mmd"]


def test_uncommitted_content_is_labelled_and_never_linked_to_a_commit(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    (path / "server.js").write_text((path / "server.js").read_text(encoding="utf-8") + "// local change\n", encoding="utf-8")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path}, html=True)
    assert ir["snapshots"][0]["revision_kind"] == "content"
    store(config, ir, ask_for_helpers, helpers_answer)

    artifacts, _ = architecture_artifacts(config, ir)

    evidence = json.loads(artifacts[MAP_NAME])
    items = [item for entry in evidence["components"].values() for item in entry["evidence"] + entry["files"]]
    assert items and all(item["link"] is None for item in items)
    assert {item["state"] for entry in evidence["components"].values() for item in entry["evidence"]} == {"working-tree"}
    assert "no source link is offered" in evidence["repositories"][0]["note"]
    assert "github.com/acme/shop/blob" not in "".join(artifacts.values()), "a commit link was offered for uncommitted content"


@pytest.mark.parametrize("remote", [None, "https://gitlab.com/acme/shop.git", "https://evil.example/github.com/acme/shop"])
def test_no_github_remote_means_no_links_even_for_a_commit(tmp_path: Path, capsys, remote) -> None:
    root_git, path = make_repository(tmp_path, "shop", remote)
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    store(config, ir, full_answer)

    artifacts, _ = architecture_artifacts(config, ir)

    evidence = json.loads(artifacts[MAP_NAME])
    assert evidence["repositories"][0]["remote"] is None
    assert all(item["link"] is None for entry in evidence["components"].values() for item in entry["evidence"])


def test_a_registered_subdirectory_links_with_the_repository_path(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "mono", "https://github.com/acme/mono", subdirectory="services/shop")
    sha = git(root_git, "rev-parse", "HEAD")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    store(config, ir, full_answer)

    artifacts, _ = architecture_artifacts(config, ir)

    evidence = json.loads(artifacts[MAP_NAME])
    links = {item["link"] for entry in evidence["components"].values() for item in entry["evidence"]}
    lines = len((path / "server.js").read_text(encoding="utf-8").splitlines())
    assert f"https://github.com/acme/mono/blob/{sha}/services/shop/server.js#L1-L{lines}" in links


def test_the_evidence_map_is_deterministic_and_holds_no_source_text(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    store(config, ir, full_answer)

    first, _ = architecture_artifacts(config, ir)
    second, _ = architecture_artifacts(config, ir)

    assert first == second
    context = json.loads(saved_context_path(config, json.loads((root / ".repodocs" / "architecture-view.json").read_text(encoding="utf-8"))["provenance"]["context_hash"]).read_text(encoding="utf-8"))
    for document in context["documents"]:
        for line in document["text"].splitlines():
            if len(line.strip()) > 12:
                assert line.strip() not in first[MAP_NAME] and line.strip() not in first["architecture-view.mmd"]
    view = json.loads((root / ".repodocs" / "architecture-view.json").read_text(encoding="utf-8"))
    evidence = json.loads(first[MAP_NAME])
    assert set(evidence["components"]) == {item["id"] for item in view["components"]}
    assert set(evidence["connections"]) == {item["id"] for item in view["connections"]}


def test_a_stale_or_missing_view_produces_no_artifacts_and_says_why(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    assert architecture_artifacts(config, ir) == ({}, [])
    store(config, ir, full_answer)
    (path / "server.js").write_text("// rewritten\n", encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0

    artifacts, warnings = architecture_artifacts(ProjectConfig.load(root), read_ir(root))

    assert artifacts == {} and warnings and "stale" in warnings[0] and "no architecture diagrams were generated" in warnings[0]


def test_competing_interpretations_and_their_citations_are_published(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    sha = git(root_git, "rev-parse", "HEAD")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    view = store(config, ir, full_answer)
    statement = view["alternatives"][0]["statement"]

    artifacts, _ = architecture_artifacts(config, ir)

    document = artifacts[ALTERNATIVES_NAME]
    assert statement in document and "Order file" in document and "server.js" in document
    evidence = json.loads(artifacts[MAP_NAME])
    cited = evidence["alternatives"][0]["evidence"][0]
    assert cited["path"] == "server.js" and cited["link"].startswith(f"https://github.com/acme/shop/blob/{sha}/server.js")
    assert evidence["alternatives"][0]["about"] == view["alternatives"][0]["about"]
    assert statement not in artifacts["architecture-view.mmd"], "alternatives stay out of the diagram"
    first = architecture_artifacts(config, ir)[0]
    assert first == artifacts, "deterministic"
    assert generate_docs(config, ir, artifacts).changed is True and generate_docs(config, ir, artifacts).changed is False


def test_a_view_with_no_alternatives_publishes_no_alternatives_document(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    store(config, ir, lambda message: {**full_answer(message), "alternatives": []})

    artifacts, _ = architecture_artifacts(config, ir)

    assert ALTERNATIVES_NAME not in artifacts and json.loads(artifacts[MAP_NAME])["alternatives"] == []
    generate_docs(config, ir, {**artifacts, ALTERNATIVES_NAME: "# stale\n"})
    generate_docs(config, ir, artifacts)
    assert not (config.output_path / ALTERNATIVES_NAME).exists(), "an alternatives document that is no longer produced is retired"


def test_hostile_alternative_text_is_plain_text_in_the_published_document(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    store(config, ir, lambda message: {**full_answer(message), "alternatives": [{
        "about": ["Order file"], "statement": "<img src=x onerror=alert(1)> [click](javascript:alert(2)) `x`", "evidence": full_answer(message)["alternatives"][0]["evidence"]}]})

    document = architecture_artifacts(config, ir)[0][ALTERNATIVES_NAME]

    body = document.split("## Alternative 1")[1]
    assert not re.search(r"(?<!\\)<", body) and not re.search(r"(?<!\\)\]\(", body) and "\\<img" in body


# ------------------------------------------------------------------ ownership of the exports
def test_the_map_and_page_are_owned_labelled_retired_and_never_overwritten_when_edited(tmp_path: Path, capsys) -> None:
    pytest.importorskip("jsonschema")
    if mermaid_bundle() is None:
        pytest.skip("the optional Mermaid bundle is not installed")
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path}, html=True)
    store(config, ir, full_answer)
    artifacts, _ = architecture_artifacts(config, ir)
    assert set(artifacts) == {"architecture-view.mmd", ALTERNATIVES_NAME, MAP_NAME, HTML_NAME}
    del artifacts[ALTERNATIVES_NAME]  # `validate` learns this file in the CLI integration; its ownership is covered above

    generate_docs(config, ir, artifacts)
    second = generate_docs(config, ir, artifacts)

    labels = {item["path"]: item["generator"] for item in read_manifest(root)["artifacts"]}
    assert labels[HTML_NAME] == labels[MAP_NAME] == "repodocs-architecture" and second.changed is False
    assert invoke(root, capsys, "validate")[0] == 0

    page = config.output_path / HTML_NAME
    original = page.read_text(encoding="utf-8")
    page.write_text(original.replace("Architecture view", "Hacked"), encoding="utf-8", newline="\n")
    with pytest.raises(OwnershipError, match="edited unexpectedly"):
        generate_docs(config, ir, artifacts)
    page.write_text(original, encoding="utf-8", newline="\n")

    without_page = {name: text for name, text in artifacts.items() if name != HTML_NAME}
    generate_docs(config, ir, without_page)
    assert not page.exists() and (config.output_path / MAP_NAME).exists()
    generate_docs(config, ir, {})
    assert not (config.output_path / MAP_NAME).exists()


def test_an_unowned_page_with_the_export_name_is_not_taken_over(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    generate_docs(config, ir, {})
    (config.output_path / HTML_NAME).write_text("<p>mine</p>", encoding="utf-8")

    with pytest.raises(OwnershipError, match="unowned files"):
        generate_docs(config, ir, {HTML_NAME: "<p>theirs</p>"})
    assert (config.output_path / HTML_NAME).read_text(encoding="utf-8") == "<p>mine</p>"


def test_the_page_is_produced_only_when_asked_for_and_never_in_another_form(tmp_path: Path, capsys) -> None:
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path})
    store(config, ir, full_answer)
    assert HTML_NAME not in architecture_artifacts(config, ir)[0]

    asking = scanned_config_with_export(root)
    with pytest.raises(ConfigError, match="not installed"):
        architecture_artifacts(asking, ir, tool_directory=tmp_path / "nowhere")


def scanned_config_with_export(root: Path) -> ProjectConfig:
    configuration = json.loads((root / "repodocs.json").read_text(encoding="utf-8"))
    configuration["architecture"] = {"html_export": True}
    (root / "repodocs.json").write_text(json.dumps(configuration), encoding="utf-8")
    return ProjectConfig.load(root)


# ------------------------------------------------------------------ the page itself
class Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self.tags: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag, attrs) -> None:
        self.tags.append(tag)
        if tag == "script":
            self._current = []
            self._attrs = dict(attrs)

    def handle_data(self, data) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag) -> None:
        if tag == "script" and self._current is not None:
            self.scripts.append((self._attrs, "".join(self._current)))
            self._current = None


def small_view() -> tuple[dict[str, Any], dict[str, Any]]:
    view = json.loads((ROOT / "docs" / "examples" / "architecture-view.example.json").read_text(encoding="utf-8"))
    evidence = {
        "map_version": "1.0", "context_hash": "8" * 64, "source_ir_hash": "9" * 64,
        "repositories": [{"repository_id": "shop", "revision": SHA, "revision_kind": "commit", "content_fingerprint": "f" * 64, "remote": "acme/shop", "note": "Pinned to this commit."}],
        "components": {item["id"]: {"files": [], "evidence": []} for item in view["components"]},
        "connections": {item["id"]: {"evidence": []} for item in view["connections"]},
        "alternatives": [{"about": item["about"], "evidence": []} for item in view["alternatives"]],
    }
    return view, evidence


HOSTILE_TEXT = "</script><img src=x onerror=alert(1)><script>alert(2)</script>&amp;   <!-- "


def test_hostile_text_cannot_add_elements_or_scripts_to_the_page() -> None:
    view, evidence = small_view()
    view["components"][0]["name"] = HOSTILE_TEXT
    view["components"][0]["responsibility"] = HOSTILE_TEXT
    view["connections"][0]["label"] = HOSTILE_TEXT
    html = build_html(view, render_view(view), evidence, "window.mermaid = {};")

    parser = Scripts()
    parser.feed(html)

    assert len(parser.scripts) == 3, "hostile text started or ended a script"
    assert "img" not in parser.tags and parser.tags.count("script") == 3
    data = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "data"))
    assert HOSTILE_TEXT.replace(" ", " ") in [item["name"] for item in data["components"]]
    assert " " not in html and "<img" not in html


def test_alternatives_reach_the_page_as_data_and_hostile_text_stays_inert() -> None:
    view, evidence = small_view()
    view["alternatives"][0]["statement"] = HOSTILE_TEXT
    html = build_html(view, render_view(view), evidence, "window.mermaid = {};")

    parser = Scripts()
    parser.feed(html)

    assert len(parser.scripts) == 3 and "img" not in parser.tags and "<img" not in html
    data = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "data"))
    assert [item["statement"] for item in data["alternatives"]] == [HOSTILE_TEXT]
    assert data["alternatives"][0]["about"] == ["Order file"]


def test_a_view_without_alternatives_gives_the_page_an_empty_list_and_no_document() -> None:
    view, evidence = small_view()
    view["alternatives"], evidence["alternatives"] = [], []
    parser = Scripts()
    parser.feed(build_html(view, render_view(view), evidence, "window.mermaid = {};"))

    data = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "data"))
    assert data["alternatives"] == [] and render_alternatives(view) is None


def test_the_policy_allows_only_the_two_inline_scripts_by_hash_and_no_connections() -> None:
    import base64
    import hashlib

    view, evidence = small_view()
    html = build_html(view, render_view(view), evidence, "window.mermaid = {};")
    parser = Scripts()
    parser.feed(html)
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)

    inline = [text for attrs, text in parser.scripts if attrs.get("type") != "application/json"]
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'" for text in inline]
    assert all(value in policy for value in hashes) and len(hashes) == 2
    assert "default-src 'none'" in policy and "connect-src 'none'" in policy and "script-src 'unsafe" not in policy and "form-action 'none'" in policy
    assert not re.search(r"<(?:script|link|img|iframe)[^>]+(?:src|href)=", html, re.I), "the page loads something from outside itself"


@pytest.mark.parametrize("bundle", ["x</script><script>alert(1)", "x<SCRIPT >y", "<script"])
def test_a_bundle_that_could_end_its_script_is_refused(bundle: str) -> None:
    view, evidence = small_view()

    with pytest.raises(ConfigError, match="script tag"):
        build_html(view, render_view(view), evidence, bundle)


def test_the_page_is_byte_stable() -> None:
    view, evidence = small_view()

    assert build_html(view, render_view(view), evidence, "window.mermaid = {};") == build_html(json.loads(json.dumps(view)), render_view(view), evidence, "window.mermaid = {};")


def test_only_allowlisted_links_are_ever_offered_by_the_viewer_script() -> None:
    from repodocs.architecture.viewer_script import VIEWER_SCRIPT

    assert "SOURCE_LINK.test(url)" in VIEWER_SCRIPT and "innerHTML" not in VIEWER_SCRIPT.replace("canvas.innerHTML = result.svg", "")
    assert VIEWER_SCRIPT.count("href") == 1 and "eval(" not in VIEWER_SCRIPT and "fetch(" not in VIEWER_SCRIPT and "XMLHttpRequest" not in VIEWER_SCRIPT
    pattern = SOURCE_LINK.pattern
    assert "https:\\/\\/github\\.com" in VIEWER_SCRIPT and "github" in pattern


# ------------------------------------------------------------------ the page in a real browser
def browser_or_skip() -> tuple[str, str]:
    node = shutil.which("node")
    if node is None or find_renderer() is None or find_browser() is None or mermaid_bundle() is None:
        if os.environ.get("REPODOCS_REQUIRE_RENDERER") == "1":
            pytest.fail("REPODOCS_REQUIRE_RENDERER=1 but Node, the Mermaid tooling, or Chrome is missing")
        pytest.skip("the optional Mermaid tooling or a browser is not available")
    return node, find_browser()


def run_page(tmp_path: Path, html: str) -> dict[str, Any]:
    node, browser = browser_or_skip()
    page = tmp_path / "page.html"
    page.write_text(html, encoding="utf-8", newline="\n")
    flag = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
    completed = subprocess.run([node, "check-export.mjs", str(page), browser, *flag], cwd=TOOL_DIRECTORY, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False)
    assert completed.returncode == 0, completed.stderr[-800:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_real_page_draws_navigates_zooms_and_loads_nothing(tmp_path: Path, capsys) -> None:
    browser_or_skip()
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    sha = git(root_git, "rev-parse", "HEAD")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path}, html=True)
    view = store(config, ir, ask_for_helpers, helpers_answer)
    artifacts, _ = architecture_artifacts(config, ir)

    report = run_page(tmp_path, artifacts[HTML_NAME])

    assert report["drawn"] and report["zoomed"] and report["scripts"] == 3
    assert report["violations"] == [] and report["requests"] == [] and report["dialogs"] == []
    assert not [line for line in report["console"] if line.startswith(("error", "pageerror"))], report["console"]
    assert any(item["name"] in report["clicked"] for item in view["components"]), report["clicked"]
    assert "sha256" in report["clicked"] and "no source text" in report["clicked"]
    assert view["alternatives"][0]["statement"] in report["alternative"] and "Order file" in report["alternative"] and "sha256" in report["alternative"]
    assert report["links"] and all(link.startswith(f"https://github.com/acme/shop/blob/{sha}/") for link in report["links"])


def test_the_real_page_stays_inert_with_hostile_text_and_unlinked_for_uncommitted_content(tmp_path: Path, capsys) -> None:
    browser_or_skip()
    root_git, path = make_repository(tmp_path, "shop", "https://github.com/acme/shop")
    (path / "server.js").write_text((path / "server.js").read_text(encoding="utf-8") + "// local\n", encoding="utf-8")
    root, config, ir = scanned(tmp_path, capsys, {"shop": path}, html=True)

    def hostile(message: dict[str, Any]) -> dict[str, Any]:
        data = full_answer(message)
        data["components"][1]["name"] = "<img src=x onerror=alert(1)> Orders"
        data["components"][1]["responsibility"] = "</script><script>alert(2)</script> [x](javascript:alert(3))"
        data["connections"][0]["source"] = "Web page"
        data["connections"][0]["target"] = data["components"][1]["name"]
        return data

    store(config, ir, hostile)
    artifacts, _ = architecture_artifacts(config, ir)

    report = run_page(tmp_path, artifacts[HTML_NAME])

    assert report["drawn"] and report["dialogs"] == [] and report["violations"] == [] and report["requests"] == []
    assert report["links"] == [], "uncommitted content must not link to a commit"
    assert report["scripts"] == 3


def test_dark_mode_keeps_the_drawing_light_and_the_links_readable() -> None:
    """Found by looking at the page in dark mode (#54): dark links on a dark panel, and edge labels on a dark ground."""

    view, evidence = small_view()
    html = build_html(view, render_view(view), evidence, "window.mermaid = {};")
    dark = html.split("prefers-color-scheme: dark", 1)[1].split("</style>", 1)[0]

    assert "#stage" not in dark and "aside a" in dark and "#58a6ff" in dark
    assert "#stage { flex: 1; overflow: hidden; position: relative; cursor: grab; background: #f6f8fa; }" in html
