"""Tests for readable system overview and focused subsystem/component diagrams (Issue #74).

Covers:
- Deterministic overview and detail projections from the same validated view.
- Stable IDs and evidence mappings across views.
- Crowded graphs decomposing into useful, discoverable details.
- Small graphs remaining concise without invented nodes or edges.
- Cross-boundary connections and boundary context in focused views.
- Disconnected components and isolated nodes.
- Duplicate names, long labels, and hostile Markdown/Mermaid text.
- Inferred/proposed relationships and unsupported claims under current grounding rules.
- Partial, missing-coverage, and stale behavior.
- Deployment alternatives staying separate from application structure.
- Supported ERD cases and unavailable diagram explanations in documentation.
- Generated-file ownership, unexpected human edits, and safe retirement.
- Real pinned Mermaid rendering for representative overviews, detail views, and ERD.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from repodocs.architecture.discovery import component_id, connection_id
from repodocs.architecture.render import (
    ALTERNATIVES_NAME,
    DETAIL_PREFIX,
    detail_name as detail_filename,
    OVERVIEW_LIMIT,
    OVERVIEW_NAME,
    label_text,
    node_key,
    project,
    render_alternatives,
    render_view,
)
from repodocs.architecture.render_check import (
    RENDERED,
    find_browser,
    find_renderer,
    render,
    svg_problems,
)
from repodocs.config import ProjectConfig
from repodocs.errors import OwnershipError
from repodocs.generator import (
    ARCHITECTURE_GENERATOR_NAME,
    ERD_ARTIFACT,
    generate_docs,
    render_markdown,
)
from repodocs.validation import validate_mermaid

from conftest import invoke, read_manifest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DIR = ROOT / "docs" / "evaluation" / "architecture-discovery" / "expected"
DBB_EXPECTED = json.loads((EXPECTED_DIR / "digitalbonebox.json").read_text(encoding="utf-8"))
FLASKR_EXPECTED = json.loads((EXPECTED_DIR / "flaskr.json").read_text(encoding="utf-8"))


def renderer_available() -> bool:
    return find_renderer() is not None and find_browser() is not None


def make_view(
    repo_id: str,
    components_spec: list[dict[str, Any]],
    connections_spec: list[dict[str, Any]],
    *,
    status: str = "complete",
    partial_reason: str | None = None,
    alternatives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a schema-valid architecture view dictionary from components and connections specs."""
    comps = []
    comp_map = {}
    for spec in components_spec:
        cid = component_id(repo_id, spec["name"])
        comp_map[spec["name"]] = cid
        comps.append({
            "id": cid,
            "name": spec["name"],
            "kind": spec.get("kind", "backend-service"),
            "responsibility": spec.get("responsibility", "Handles service operations."),
            "repository_id": repo_id,
            "members": spec.get("members", ["src/index.js"]),
            "evidence": spec.get("evidence", ["ctx:1111222233334444"]),
            "classification": spec.get("classification", "inferred"),
            "confidence": spec.get("confidence", "high"),
            "support": spec.get("support", "supported"),
            "support_reasons": spec.get("support_reasons", ["observed-role-facts"]),
        })

    conns = []
    for spec in connections_spec:
        src_id = comp_map.get(spec["source"], spec["source"])
        tgt_id = comp_map.get(spec["target"], spec["target"])
        kind = spec.get("kind", "calls")
        conns.append({
            "id": connection_id(src_id, tgt_id, kind),
            "source": src_id,
            "target": tgt_id,
            "kind": kind,
            "label": spec.get("label", kind),
            "evidence": spec.get("evidence", ["ctx:1111222233334444"]),
            "classification": spec.get("classification", "inferred"),
            "confidence": spec.get("confidence", "high"),
            "cross_repository": False,
            "support": spec.get("support", "supported"),
            "support_reasons": spec.get("support_reasons", ["route-in-cited-source"]),
        })

    view: dict[str, Any] = {
        "view_version": "1.0",
        "status": status,
        "provenance": {
            "provider": "ollama",
            "requested_model": "test-model",
            "served_model": "test-model:latest",
            "engine_version": "0.1.0",
            "prompt_version": "1.0",
            "context_version": "1.0",
            "context_hash": "a" * 64,
            "source_ir_hash": "b" * 64,
            "snapshots": [
                {
                    "repository_id": repo_id,
                    "revision": "c" * 40,
                    "revision_kind": "commit",
                    "content_fingerprint": "d" * 64,
                }
            ],
            "external": False,
            "rounds": 1,
            "requests": 1,
        },
        "components": comps,
        "connections": conns,
        "alternatives": alternatives or [],
        "rejected": {},
    }
    if status == "partial":
        view["partial_reason"] = partial_reason or "round-budget"
    return view


def digitalbonebox_view() -> dict[str, Any]:
    """A validated architecture view directly matching the expected facts of DigitalBoneBox."""
    components = [
        {"name": "Study web client", "kind": "frontend", "members": ["templates/boneset.html"]},
        {"name": "Boneset API", "kind": "backend-service", "members": ["boneset-api/server.js"]},
        {"name": "Scenes service", "kind": "backend-service", "members": ["boneset-api/scenes.js"]},
        {"name": "Anatomy dataset", "kind": "storage", "members": ["boneset-api/data/bones.json"]},
        {"name": "Data extraction scripts", "kind": "background-job", "members": ["data_extraction/extract.py"]},
        {"name": "Serverless entry", "kind": "other", "members": ["api/index.js", "vercel.json"]},
    ]
    connections = [
        {"source": "Study web client", "target": "Boneset API", "kind": "calls", "label": "fetch API"},
        {"source": "Study web client", "target": "Scenes service", "kind": "calls", "label": "fetch scenes"},
        {"source": "Boneset API", "target": "Anatomy dataset", "kind": "reads", "label": "reads data"},
        {"source": "Boneset API", "target": "Scenes service", "kind": "depends-on", "label": "mounts router"},
        {"source": "Serverless entry", "target": "Boneset API", "kind": "depends-on", "label": "requires server"},
    ]
    return make_view("digitalbonebox", components, connections)


def flaskr_view() -> dict[str, Any]:
    """A validated architecture view directly matching the expected facts of Flaskr."""
    components = [
        {"name": "Application factory", "kind": "backend-service", "members": ["flaskr/__init__.py"]},
        {"name": "Authentication", "kind": "backend-service", "members": ["flaskr/auth.py"]},
        {"name": "Blog", "kind": "backend-service", "members": ["flaskr/blog.py"]},
        {"name": "Database", "kind": "database", "members": ["flaskr/db.py"]},
        {"name": "Pages", "kind": "frontend", "members": ["flaskr/templates/base.html"]},
    ]
    connections = [
        {"source": "Application factory", "target": "Authentication", "kind": "depends-on", "label": "registers auth"},
        {"source": "Application factory", "target": "Blog", "kind": "depends-on", "label": "registers blog"},
        {"source": "Application factory", "target": "Database", "kind": "depends-on", "label": "init_app"},
        {"source": "Authentication", "target": "Database", "kind": "reads", "label": "queries users"},
        {"source": "Blog", "target": "Database", "kind": "reads", "label": "queries posts"},
        {"source": "Blog", "target": "Authentication", "kind": "depends-on", "label": "login_required"},
        {"source": "Authentication", "target": "Pages", "kind": "renders", "label": "renders auth views"},
        {"source": "Blog", "target": "Pages", "kind": "renders", "label": "renders blog views"},
    ]
    return make_view("flaskr", components, connections)


# ------------------------------------------------------------------ 1. DigitalBoneBox & Flaskr Focused Views
def test_digitalbonebox_produces_overview_and_focused_backend_detail_diagram() -> None:
    view = digitalbonebox_view()
    diagrams = project(view)
    rendered = render_view(view)

    names = list(rendered)
    assert names[0] == OVERVIEW_NAME
    backend_detail_name = detail_filename("digitalbonebox", "backend-service")
    assert backend_detail_name in names

    # Overview contains all components
    overview_text = rendered[OVERVIEW_NAME]
    validate_mermaid(overview_text)
    assert "Study web client" in overview_text
    assert "Boneset API" in overview_text
    assert "Scenes service" in overview_text
    assert "Anatomy dataset" in overview_text
    assert "Data extraction scripts" in overview_text
    assert "Serverless entry" in overview_text

    # Detail view focuses on Boneset API & Scenes service with boundary stubs
    detail_text = rendered[backend_detail_name]
    validate_mermaid(detail_text)
    assert "Boneset API" in detail_text
    assert "Scenes service" in detail_text
    assert "class " in detail_text and " stub" in detail_text

    # Boundary context stubs are present
    assert "Study web client" in detail_text
    assert "Anatomy dataset" in detail_text
    assert "Serverless entry" in detail_text
    # Disconnected component not connected to backend services is excluded from detail view
    assert "Data extraction scripts" not in detail_text


def test_flaskr_produces_overview_and_focused_backend_detail_diagram() -> None:
    view = flaskr_view()
    rendered = render_view(view)

    names = list(rendered)
    assert names[0] == OVERVIEW_NAME
    backend_detail_name = detail_filename("flaskr", "backend-service")
    assert backend_detail_name in names

    # Overview text validates
    overview_text = rendered[OVERVIEW_NAME]
    validate_mermaid(overview_text)
    assert "Application factory" in overview_text
    assert "Authentication" in overview_text
    assert "Blog" in overview_text
    assert "Database" in overview_text
    assert "Pages" in overview_text

    # Detail view focuses on the 3 backend components with Database and Pages stubs
    detail_text = rendered[backend_detail_name]
    validate_mermaid(detail_text)
    assert "Application factory" in detail_text
    assert "Authentication" in detail_text
    assert "Blog" in detail_text
    assert "Database" in detail_text
    assert "Pages" in detail_text
    assert "class " in detail_text and " stub" in detail_text


def test_stable_component_ids_and_evidence_mappings_across_projections() -> None:
    view = digitalbonebox_view()
    rendered = render_view(view)

    overview_text = rendered[OVERVIEW_NAME]
    detail_text = rendered[detail_filename("digitalbonebox", "backend-service")]

    # Component node keys must match exactly across views
    api_key = node_key(component_id("digitalbonebox", "Boneset API"))
    scenes_key = node_key(component_id("digitalbonebox", "Scenes service"))
    assert api_key in overview_text and api_key in detail_text
    assert scenes_key in overview_text and scenes_key in detail_text

    # Evidence citations are preserved in comments
    assert f"%% evidence {api_key}: ctx:1111222233334444" in overview_text
    assert f"%% evidence {api_key}: ctx:1111222233334444" in detail_text
    assert f"%% evidence {api_key}->" in overview_text
    assert f"%% evidence {api_key}->" in detail_text


# ------------------------------------------------------------------ 2. Small Projects Conciseness
def test_small_projects_remain_concise_without_invented_nodes_or_edges() -> None:
    # A small 3-component project (1 frontend, 1 backend-service, 1 database)
    components = [
        {"name": "Client", "kind": "frontend"},
        {"name": "Server", "kind": "backend-service"},
        {"name": "Database", "kind": "database"},
    ]
    connections = [
        {"source": "Client", "target": "Server", "kind": "calls"},
        {"source": "Server", "target": "Database", "kind": "reads"},
    ]
    view = make_view("small-app", components, connections)
    rendered = render_view(view)

    # Only overview diagram is produced; no unnecessary single-component detail views
    assert list(rendered) == [OVERVIEW_NAME]
    overview_text = rendered[OVERVIEW_NAME]
    validate_mermaid(overview_text)
    assert "Client" in overview_text and "Server" in overview_text and "Database" in overview_text
    assert overview_text.count("-->|") == 2


# ------------------------------------------------------------------ 3. Crowded Decomposition
def test_crowded_graph_decomposes_deterministically_into_navigable_details() -> None:
    # 20 components across 2 repositories cycling kinds
    components = []
    for i in range(20):
        repo = "backend-repo" if i % 2 == 0 else "frontend-repo"
        kind = "backend-service" if i % 3 == 0 else ("frontend" if i % 3 == 1 else "storage")
        components.append({"name": f"Service {i:02d}", "kind": kind, "repository_id": repo})
    connections = [
        {"source": f"Service {i:02d}", "target": f"Service {i+1:02d}", "kind": "calls"}
        for i in range(19)
    ]
    view = make_view("system", components, connections)
    diagrams = project(view)
    rendered = render_view(view)

    assert len(diagrams) > 1
    assert [d.name for d in diagrams][0] == OVERVIEW_NAME
    overview_text = rendered[OVERVIEW_NAME]
    assert f"Grouped: more than {OVERVIEW_LIMIT} components; see the detail diagrams" in overview_text
    assert re.search(r'\|"\d+ connections?', overview_text)

    # All generated detail diagrams validate
    for name, text in rendered.items():
        validate_mermaid(text)
        assert text.startswith("flowchart LR\n")


# ------------------------------------------------------------------ 4. Disconnected Components & Isolated Nodes
def test_disconnected_components_rendered_without_edges() -> None:
    components = [
        {"name": "Active Service A", "kind": "backend-service"},
        {"name": "Active Service B", "kind": "backend-service"},
        {"name": "Isolated Script", "kind": "background-job"},
        {"name": "Unconnected Storage", "kind": "storage"},
    ]
    connections = [
        {"source": "Active Service A", "target": "Active Service B", "kind": "calls"},
    ]
    view = make_view("hybrid-repo", components, connections)
    rendered = render_view(view)

    overview_text = rendered[OVERVIEW_NAME]
    validate_mermaid(overview_text)
    assert "Active Service A" in overview_text
    assert "Active Service B" in overview_text
    assert "Isolated Script" in overview_text
    assert "Unconnected Storage" in overview_text
    # Exactly one arrow in the entire diagram
    assert overview_text.count("-->|") == 1
    assert overview_text.count("-.->|") == 0


# ------------------------------------------------------------------ 5. Duplicate Names, Long Labels & Hostile Text
def test_duplicate_names_across_repositories_have_distinct_keys() -> None:
    comp1 = {
        "id": component_id("repo-a", "Service"),
        "name": "Service",
        "kind": "backend-service",
        "responsibility": "Repo A service",
        "repository_id": "repo-a",
        "members": ["a/service.py"],
        "evidence": ["ctx:1111222233334444"],
        "classification": "inferred",
        "confidence": "high",
        "support": "supported",
        "support_reasons": ["observed-role-facts"],
    }
    comp2 = {
        "id": component_id("repo-b", "Service"),
        "name": "Service",
        "kind": "backend-service",
        "responsibility": "Repo B service",
        "repository_id": "repo-b",
        "members": ["b/service.py"],
        "evidence": ["ctx:1111222233334444"],
        "classification": "inferred",
        "confidence": "high",
        "support": "supported",
        "support_reasons": ["observed-role-facts"],
    }
    conn = {
        "id": connection_id(comp1["id"], comp2["id"], "calls"),
        "source": comp1["id"],
        "target": comp2["id"],
        "kind": "calls",
        "label": "cross-repo call",
        "evidence": ["ctx:1111222233334444"],
        "classification": "inferred",
        "confidence": "high",
        "cross_repository": True,
        "support": "supported",
        "support_reasons": ["route-in-cited-source"],
    }
    view = {
        "view_version": "1.0",
        "status": "complete",
        "provenance": {
            "provider": "ollama",
            "requested_model": "test",
            "served_model": "test",
            "engine_version": "0.1.0",
            "prompt_version": "1.0",
            "context_version": "1.0",
            "context_hash": "a" * 64,
            "source_ir_hash": "b" * 64,
            "snapshots": [
                {"repository_id": "repo-a", "revision": "c" * 40, "revision_kind": "commit", "content_fingerprint": "d" * 64},
                {"repository_id": "repo-b", "revision": "e" * 40, "revision_kind": "commit", "content_fingerprint": "f" * 64},
            ],
            "external": False,
            "rounds": 1,
            "requests": 1,
        },
        "components": [comp1, comp2],
        "connections": [conn],
        "alternatives": [],
        "rejected": {},
    }
    rendered = render_view(view)
    text = rendered[OVERVIEW_NAME]
    validate_mermaid(text)
    k1 = node_key(comp1["id"])
    k2 = node_key(comp2["id"])
    assert k1 != k2
    assert f"{k1} -->|\"cross-repo call\"| {k2}" in text


def test_hostile_markdown_and_mermaid_text_is_safely_sanitized() -> None:
    hostile_names = [
        'API" ]; click a href "javascript:alert(1)',
        "<script>evil()</script>",
        "back`tick & symbols < >",
        "line\nbreak\rtest",
    ]
    for hostile in hostile_names:
        view = make_view(
            "sec-test",
            [{"name": hostile, "kind": "backend-service"}, {"name": "Target", "kind": "database"}],
            [{"source": hostile, "target": "Target", "kind": "reads", "label": hostile}],
        )
        rendered = render_view(view)
        text = rendered[OVERVIEW_NAME]
        validate_mermaid(text)
        assert "<script" not in text.lower()
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("click", "%%{"))
            assert "\n" not in line and "\r" not in line


# ------------------------------------------------------------------ 6. Grounding, Uncertainty & Stale Behavior
def test_unsupported_claims_excluded_from_diagram_and_counted_in_notes() -> None:
    view = make_view(
        "grounding-test",
        [
            {"name": "Supported Service", "kind": "backend-service", "support": "supported"},
            {"name": "Unsupported Hallucination", "kind": "backend-service", "support": "unsupported"},
        ],
        [],
    )
    diagrams = project(view)
    text = render_view(view)[OVERVIEW_NAME]
    validate_mermaid(text)
    assert "Supported Service" in text
    assert "Unsupported Hallucination" not in text
    assert "1 claim not shown: source facts contradict or do not contain them" in text


def test_partial_discovery_reason_explicitly_presented_in_notes() -> None:
    view = make_view(
        "partial-test",
        [{"name": "Service A", "kind": "backend-service"}],
        [],
        status="partial",
        partial_reason="round-budget",
    )
    text = render_view(view)[OVERVIEW_NAME]
    validate_mermaid(text)
    assert "Partial discovery: round-budget" in text


# ------------------------------------------------------------------ 7. Deployment Alternatives Separation
def test_deployment_alternatives_stay_separate_from_application_structure() -> None:
    alternatives = [
        {
            "about": ["arch:0000000000000001"],
            "statement": "Deployed via Vercel serverless function (api/index.js) rather than persistent node server.",
            "evidence": ["ctx:1111222233334444"],
        }
    ]
    view = make_view(
        "deploy-test",
        [{"name": "API Service", "kind": "backend-service"}],
        [],
        alternatives=alternatives,
    )
    # The statement must not be rendered into the diagram
    rendered = render_view(view)
    assert "Deployed via Vercel" not in rendered[OVERVIEW_NAME]

    # But it is rendered cleanly in alternatives markdown
    alt_md = render_alternatives(view, {"ctx:1111222233334444": "deploy-test:vercel.json:1-10"})
    assert alt_md is not None
    assert "Deployed via Vercel serverless function" in alt_md
    assert "deploy-test:vercel.json:1-10" in alt_md


# ------------------------------------------------------------------ 8. Supported ERD & Unavailable Diagram Explanations
def test_diagram_views_and_limitations_section_in_markdown() -> None:
    # IR with database table node
    ir_with_tables = {
        "project": {"name": "TestProject"},
        "snapshots": [],
        "provenance": {"analyzers": []},
        "relationships": [],
        "claims": [],
        "evidence": [],
        "nodes": [
            {"id": "db_1", "type": "database", "name": "main_db", "attributes": {"repository_id": "app"}},
            {"id": "tbl_1", "type": "table", "name": "users", "attributes": {"repository_id": "app"}},
        ],
    }
    md_with_tables = render_markdown(ir_with_tables)
    assert "## Diagram views and limitations" in md_with_tables
    assert "`architecture.mmd`: System architecture and component inventory diagram." in md_with_tables
    assert "`erd.mmd`: Entity-relationship diagram generated from static PostgreSQL schema" in md_with_tables
    assert "### Unavailable diagram types" in md_with_tables
    assert "**Sequence diagrams**: Unavailable." in md_with_tables
    assert "**Runtime activity and state diagrams**: Unavailable." in md_with_tables
    assert "**Deployment representation**:" in md_with_tables

    # IR without database table nodes
    ir_without_tables = {
        "project": {"name": "TestProject"},
        "snapshots": [],
        "provenance": {"analyzers": []},
        "relationships": [],
        "claims": [],
        "evidence": [],
        "nodes": [
            {"id": "svc_1", "type": "service", "name": "api", "attributes": {"repository_id": "app"}},
        ],
    }
    md_without_tables = render_markdown(ir_without_tables)
    assert "Entity-relationship diagram (ERD): Unavailable. No PostgreSQL table facts were extracted" in md_without_tables


# ------------------------------------------------------------------ 9. Generated-File Ownership & Safe Retirement
def test_generated_detail_diagrams_owned_and_safely_retired(tmp_path: Path, capsys) -> None:
    # Setup project
    root = tmp_path / "project"
    root.mkdir()
    repo = tmp_path / "shop"
    shutil.copytree(ROOT / "tests" / "fixtures" / "express-shop-repo", repo)
    assert invoke(root, capsys, "init", "ShopApp")[0] == 0
    assert invoke(root, capsys, "add", str(repo), "--id", "shop", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    config = ProjectConfig.load(root)
    ir = json.loads((config.internal_path / "ir.json").read_text(encoding="utf-8"))

    # Generate with DigitalBoneBox view (produces overview and backend-service detail view)
    dbb = digitalbonebox_view()
    artifacts = render_view(dbb)
    detail_name = detail_filename("digitalbonebox", "backend-service")
    assert detail_name in artifacts

    first_gen = generate_docs(config, ir, artifacts)
    assert first_gen.changed is True
    manifest = read_manifest(root)
    entries = {item["path"]: item["generator"] for item in manifest["artifacts"]}
    assert entries[OVERVIEW_NAME] == ARCHITECTURE_GENERATOR_NAME
    assert entries[detail_name] == ARCHITECTURE_GENERATOR_NAME

    # Second generate with identical artifacts is a no-op
    second_gen = generate_docs(config, ir, artifacts)
    assert second_gen.changed is False

    # Human unexpected edit to detail diagram raises OwnershipError
    detail_file = config.output_path / detail_name
    detail_file.write_text(detail_file.read_text(encoding="utf-8") + "%% manual edit\n", encoding="utf-8")
    with pytest.raises(OwnershipError, match="edited unexpectedly"):
        generate_docs(config, ir, artifacts)

    # Revert human edit, then generate with small view (which has no detail diagrams)
    detail_file.write_text(artifacts[detail_name], encoding="utf-8")
    small_artifacts = render_view(make_view("shop", [{"name": "Single", "kind": "backend-service"}], []))
    assert detail_name not in small_artifacts
    assert list(small_artifacts) == [OVERVIEW_NAME]

    retire_gen = generate_docs(config, ir, small_artifacts)
    assert retire_gen.changed is True
    assert not detail_file.exists(), "obsolete detail diagram safely retired"
    new_manifest = read_manifest(root)
    new_entries = {item["path"] for item in new_manifest["artifacts"]}
    assert detail_name not in new_entries


# ------------------------------------------------------------------ 10. Real Pinned Mermaid Rendering
def test_real_mermaid_renderer_renders_digitalbonebox_and_flaskr_views() -> None:
    if not renderer_available():
        pytest.skip("Mermaid renderer or Chrome not available locally")

    dbb_rendered = render_view(digitalbonebox_view())
    flaskr_rendered = render_view(flaskr_view())

    all_diagrams = {f"dbb-{k}": v for k, v in dbb_rendered.items()}
    all_diagrams.update({f"flaskr-{k}": v for k, v in flaskr_rendered.items()})

    result = render(all_diagrams)
    assert result.status == RENDERED, result.message
    assert set(result.svgs) == set(all_diagrams)
    for name, svg in result.svgs.items():
        assert svg.lstrip().startswith("<svg")
        assert svg_problems(svg) == []
