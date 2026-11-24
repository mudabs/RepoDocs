"""Projection regressions and licensed, revision-recorded source subsets. No network or inference."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from repodocs.architecture.context import context_hash
from repodocs.architecture.discovery import discover, save_view, load_view
from repodocs.architecture.evaluation import ReferenceProvider
from repodocs.architecture.export import architecture_artifacts, MAP_NAME
from repodocs.architecture.navigation import source_link
from repodocs.architecture import render as renderer
from repodocs.config import ProjectConfig
from conftest import invoke, read_ir
from test_readable_diagram_views import make_view

ROOT = Path(__file__).resolve().parents[1]


def test_normalized_repository_names_never_overwrite_details():
    views = [make_view(repo, [{"name": "One"}, {"name": "Two"}], []) for repo in ("repo-a", "repo_a", "Repo-A")]
    view = {**views[0], "components": [item for part in views for item in part["components"]]}
    diagrams = renderer.project(view)
    assert len(diagrams) == len(renderer.render_view(view)) == 4
    names = [diagram.name for diagram in diagrams]
    assert len(set(name.casefold() for name in names)) == len(names)
    before = renderer.render_view(view)
    view["components"].reverse()
    assert renderer.render_view(view) == before
    # Adding a colliding display slug must not rename an existing group's file.
    assert renderer.detail_name("repo-a", "backend-service") in before
    assert renderer.detail_name("repo_a", "backend-service") in before


def test_duplicate_output_names_are_refused(monkeypatch):
    diagram = renderer.Diagram("duplicate.mmd")
    monkeypatch.setattr(renderer, "project", lambda view: [diagram, diagram])
    with pytest.raises(ValueError, match="Duplicate architecture diagram"):
        renderer.render_view(make_view("repo", [], []))


@pytest.mark.parametrize("topology", ["isolated", "chain", "hub", "dense"])
def test_oversized_groups_are_bounded_and_deterministic(topology):
    components = [{"name": f"Service {i:02d}"} for i in range(30)]
    pairs = []
    if topology == "chain":
        pairs = [(i, i + 1) for i in range(29)]
    elif topology == "hub":
        pairs = [(0, i) for i in range(1, 30)]
    elif topology == "dense":
        pairs = [(i, j) for i in range(30) for j in range(i + 1, min(i + 3, 30))]
    connections = [{"source": components[i]["name"], "target": components[j]["name"], "kind": "calls"} for i, j in pairs]
    view = make_view("repo", components, connections)
    diagrams = renderer.project(view)
    details = diagrams[1:]
    assert len(details) == 3
    focal = []
    drawn = set()
    for diagram in details:
        focal.extend(node.key for node in diagram.nodes if node.classification != "stub")
        assert sum(node.classification != "stub" for node in diagram.nodes) <= renderer.MAX_DETAIL_COMPONENTS
        assert sum(node.classification == "stub" for node in diagram.nodes) <= renderer.MAX_STUBS
        drawn.update((edge.source, edge.target) for edge in diagram.edges)
    assert sorted(focal) == sorted(renderer.node_key(item["id"]) for item in view["components"])
    actual = {(renderer.node_key(edge["source"]), renderer.node_key(edge["target"])) for edge in view["connections"]}
    assert drawn <= actual  # no invented relationships
    if drawn != actual:
        assert any("omitted" in note for diagram in details for note in diagram.notes)
    original = renderer.render_view(view)
    view["components"].reverse()
    view["connections"].reverse()
    assert renderer.render_view(view) == original


@pytest.mark.parametrize("name", ["digitalbonebox", "flaskr"])
def test_reviewed_sources_flow_through_validation_generation_and_evidence(tmp_path, capsys, name):
    fixture = ROOT / "tests/fixtures/architecture-projections" / name
    provenance = json.loads((fixture / "provenance.json").read_text())
    expected = json.loads((ROOT / "docs/evaluation/architecture-discovery/expected" / f"{name}.json").read_text())
    assert provenance["revision"] == expected["source"]["revision"]
    assert (fixture / "LICENSE.txt").is_file()
    for path, digest in provenance["files"].items():
        assert hashlib.sha256((fixture / "source" / path).read_bytes()).hexdigest() == digest
    source = tmp_path / "source"
    shutil.copytree(fixture / "source", source)
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", name)[0] == 0
    assert invoke(root, capsys, "add", str(source), "--id", name, "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    config, ir = ProjectConfig.load(root), read_ir(root)
    # This subset is local content, never a fabricated upstream Git checkout or revision.
    assert ir["snapshots"][0]["revision_kind"] == "content"
    view, context = discover(config, ir, ReferenceProvider(expected))
    assert view["status"] == "complete"
    save_view(config, view, context)
    assert load_view(config, ir)[0] == "current"
    assert view["provenance"]["context_hash"] == context_hash(context)
    artifacts, warnings = architecture_artifacts(config, ir, include_html=False)
    assert not warnings
    evidence = json.loads(artifacts[MAP_NAME])
    diagram_names = {name for name in artifacts if name.endswith(".mmd")}
    assert set(evidence["diagrams"]) == diagram_names
    assert len(diagram_names) >= 2
    if os.environ.get("REPODOCS_REQUIRE_RENDERER") == "1":
        from repodocs.architecture.render_check import RENDERED, render, svg_problems
        rendered = render({name: artifacts[name] for name in diagram_names})
        assert rendered.status == RENDERED, rendered.message
        assert set(rendered.svgs) == diagram_names
        assert all(not svg_problems(svg) for svg in rendered.svgs.values())
    for name in diagram_names:
        assert f"]({name})" in artifacts["architecture-view-index.md"]
    assert {item["name"] for item in view["components"]} == {item["name"] for item in expected["components"]}
    documents = {item["id"]: item for item in context["documents"]}
    for component in view["components"]:
        assert component["members"] and component["evidence"]
        for path in component["members"]:
            assert path in provenance["files"]
        for key in component["evidence"]:
            doc = documents[key]
            assert doc["path"] in component["members"]
            assert doc["content_hash"] == provenance["files"][doc["path"]]
    for group in ("components", "connections"):
        for entry in evidence[group].values():
            for item in entry["evidence"]:
                assert item["content_hash"] == provenance["files"][item["path"]]
                lines = (source / item["path"]).read_text(encoding="utf-8").splitlines()
                assert 1 <= item["line_start"] <= item["line_end"] <= len(lines)
                assert item["state"] == "working-tree" and item["link"] is None
                # Original provenance can separately form the upstream pinned reference;
                # it must not be misrepresented as the local scan's commit.
                snapshot = {"revision_kind": "commit", "revision": provenance["revision"]}
                original_path = provenance["path_prefix"] + item["path"]
                remote = tuple(provenance["repository"].split("/")[-2:])
                link = source_link(remote, snapshot, original_path, item["line_start"], item["line_end"])
                assert provenance["revision"] in link
    assert invoke(root, capsys, "generate")[0] == 0
    assert invoke(root, capsys, "validate")[0] == 0
    code, result = invoke(root, capsys, "generate")
    assert code == 0 and result["data"]["changed"] is False


def test_omissions_stay_visible_with_detailed_comments():
    view = make_view("repo", [{"name": "Kept"}, {"name": "Hidden", "support": "unsupported"}], [])
    text = renderer.render_view(view)[renderer.OVERVIEW_NAME]
    assert 'legend_coverage["Visible coverage limited"]' in text
    assert "%% note: 1 claim not shown" in text
    assert "legend_note_" not in text
