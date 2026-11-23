"""Deterministic Mermaid views of an architecture view, their grammar, ownership, and the real renderer."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from repodocs.architecture.discovery import component_id, connection_id
from repodocs.architecture.render import ALTERNATIVES_NAME, OVERVIEW_LIMIT, label_text, project, render_alternatives, render_view
from repodocs.architecture.render_check import FAILED, NOT_AVAILABLE, RENDERED, find_renderer, render, svg_problems, _screened
from repodocs.config import ProjectConfig
from repodocs.errors import OwnershipError, ValidationError
from repodocs.generator import ARCHITECTURE_GENERATOR_NAME, generate_docs
from repodocs.validation import validate_mermaid

from conftest import copy_fixture, invoke, read_ir, read_manifest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / "docs" / "examples" / "architecture-view.example.json").read_text(encoding="utf-8"))
HOSTILE = ['Orders" ]; click a href "javascript:alert(1)', "<img src=x onerror=alert(1)>", "back`tick", "%%{init: {\"securityLevel\": \"loose\"}}%%", "line\nbreak", "a --> b", "<script>alert(1)</script>"]


def example() -> dict:
    return copy.deepcopy(EXAMPLE)


def synthetic(count: int, *, repositories: tuple[str, ...] = ("shop",)) -> dict:
    """A crowded view: `count` components cycling through kinds, each connected to the next."""

    view = example()
    kinds = ["frontend", "backend-service", "storage", "api"]
    evidence = view["components"][0]["evidence"]
    components = []
    for index in range(count):
        repository = repositories[index % len(repositories)]
        name = f"Part {index:02d}"
        components.append({
            "id": component_id(repository, name), "name": name, "kind": kinds[index % len(kinds)], "responsibility": "x",
            "repository_id": repository, "members": ["a.js"], "evidence": evidence,
            "classification": "inferred" if index % 3 else "proposed", "confidence": "low" if index % 5 == 0 else "high",
            "support": "supported", "support_reasons": ["observed-role-facts"],
        })
    view["components"] = components
    view["connections"] = [
        {"id": connection_id(a["id"], b["id"], "calls"), "source": a["id"], "target": b["id"], "kind": "calls", "label": f"call {i}",
         "evidence": evidence, "classification": "inferred" if i % 2 else "proposed", "confidence": "high",
         "cross_repository": a["repository_id"] != b["repository_id"], "support": "supported", "support_reasons": ["route-in-cited-source"]}
        for i, (a, b) in enumerate(zip(components, components[1:]))
    ]
    return view


# ------------------------------------------------------------------ determinism and shape
def test_identical_views_give_byte_stable_mermaid_whatever_the_input_order() -> None:
    view = example()
    shuffled = example()
    shuffled["components"].reverse()
    shuffled["connections"].reverse()
    reloaded = json.loads(json.dumps(view))

    assert render_view(view) == render_view(shuffled) == render_view(reloaded)
    assert list(render_view(view)) == ["architecture-view.mmd"]


def test_every_generated_diagram_satisfies_the_architecture_grammar() -> None:
    for view in (example(), synthetic(5), synthetic(30), synthetic(30, repositories=("shop", "billing"))):
        for name, text in render_view(view).items():
            validate_mermaid(text)
            assert text.startswith("flowchart LR\n") and name.endswith(".mmd")


def test_proposed_low_confidence_and_unconfirmed_links_are_marked_in_the_text_not_only_in_style() -> None:
    view = synthetic(6, repositories=("shop", "billing"))
    text = render_view(view)["architecture-view.mmd"]

    assert "(proposed)" in text and "(low confidence)" in text and "(unconfirmed)" in text
    assert "-.->" in text and "-->|" in text
    assert "classDef proposed" in text and "stroke-dasharray" in text
    assert "Model-proposed architecture. Citations are evidence to check, not proof." in text
    solid = [line for line in text.splitlines() if "-->|" in line]
    assert solid and all("(proposed)" not in line and "(unconfirmed)" not in line for line in solid)


def test_a_confirmed_cross_repository_connection_is_solid_and_not_called_unconfirmed() -> None:
    view = synthetic(2, repositories=("shop", "billing"))
    link = view["connections"][0]
    link.update(classification="inferred", candidate_id="candidate:pay")

    text = render_view(view)["architecture-view.mmd"]

    assert "(unconfirmed)" not in text


def test_a_partial_view_says_so_in_the_diagram() -> None:
    view = example()
    view.update(status="partial", partial_reason="round-budget")

    assert "Partial discovery: round-budget" in render_view(view)["architecture-view.mmd"]


def test_an_edge_to_a_component_that_is_not_in_the_view_is_never_drawn() -> None:
    view = example()
    view["connections"].append({**view["connections"][0], "id": "archc:" + "0" * 16, "target": "arch:" + "f" * 16})

    text = render_view(view)["architecture-view.mmd"]

    assert text.count("-->|") + text.count("-.->|") == len(example()["connections"])


def test_repository_subgraphs_appear_only_for_several_repositories() -> None:
    assert "subgraph g_" not in render_view(synthetic(4))["architecture-view.mmd"]
    assert render_view(synthetic(4, repositories=("shop", "billing")))["architecture-view.mmd"].count("subgraph g_") == 2


# ------------------------------------------------------------------ crowded views
def test_a_crowded_view_is_grouped_and_each_group_gets_a_detail_diagram() -> None:
    view = synthetic(OVERVIEW_LIMIT + 8)

    diagrams = project(view)
    rendered = render_view(view)

    assert [item.name for item in diagrams][0] == "architecture-view.mmd" and len(diagrams) > 1
    overview = rendered["architecture-view.mmd"]
    assert "Grouped: more than 12 components" in overview
    assert overview.count("[\"") < len(view["components"]) + 6, "the overview holds groups, not every component"
    assert re.search(r"\|\"\d+ connections?", overview)
    shown = set()
    for name, text in rendered.items():
        if name != "architecture-view.mmd":
            assert name.startswith("architecture-view-shop-") and name.endswith(".mmd")
            validate_mermaid(text)
            shown |= {part for part in re.findall(r'(c_[0-9a-f]{16})\[?\(?\[?"', text)}
    assert len(shown) >= len(view["components"]) - 4, "components of single-member groups appear only in the overview group"
    assert "class " in rendered[sorted(rendered)[1]] and " stub" in "\n".join(rendered.values())


def test_the_decomposition_is_deterministic() -> None:
    view = synthetic(30, repositories=("shop", "billing"))

    assert render_view(view) == render_view(copy.deepcopy(view))
    assert list(render_view(view)) == sorted(render_view(view), key=lambda name: (name != "architecture-view.mmd", name))


# ------------------------------------------------------------------ hostile text
@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_names_and_labels_cannot_add_syntax(text: str) -> None:
    view = example()
    view["components"][0]["name"] = text
    view["components"][1]["name"] = "API " + text
    view["connections"][0]["label"] = text

    diagrams = render_view(view)

    for name, rendered in diagrams.items():
        validate_mermaid(rendered)
        for line in rendered.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("click", "%%{")), line
            assert stripped.startswith("%%") or ("<" not in stripped and ">" not in stripped.replace("-->", "").replace("-.->", "")), line
            assert "\n" not in line and "\r" not in line


def test_label_text_uses_mermaid_entities() -> None:
    assert label_text('a"b<c>d\ne') == "a#quot;b#lt;c#gt;d e"


@pytest.mark.parametrize("line", [
    'click c_1 href "javascript:alert(1)"',
    'c_1["x<b>"]',
    '%%{init: {"securityLevel": "loose"}}%%',
    "c_1 --> missing",
    "subgraph g_1[\"x\"]",
    "end",
    'c_1["x"] --> c_2["y"]',
    "classDef proposed stroke:url(javascript:alert(1))",
    "class missing inferred",
])
def test_the_architecture_grammar_rejects_unsupported_lines(line: str) -> None:
    text = f'flowchart LR\n    c_1["x"]\n    {line}\n'

    with pytest.raises(ValidationError):
        validate_mermaid(text)


# ------------------------------------------------------------------ generated-file ownership
def scanned_project(tmp_path: Path, capsys) -> tuple[Path, ProjectConfig, dict]:
    root = tmp_path / "project"
    root.mkdir()
    repository = tmp_path / "shop"
    shutil.copytree(ROOT / "tests" / "fixtures" / "express-shop-repo", repository)
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "shop", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    return root, ProjectConfig.load(root), read_ir(root)


def test_architecture_diagrams_are_owned_labelled_and_no_op_when_unchanged(tmp_path: Path, capsys) -> None:
    root, config, ir = scanned_project(tmp_path, capsys)
    artifacts = render_view(example())

    first = generate_docs(config, ir, artifacts)
    second = generate_docs(config, ir, artifacts)

    manifest = read_manifest(root)
    labels = {item["path"]: item["generator"] for item in manifest["artifacts"]}
    assert labels["architecture-view.mmd"] == ARCHITECTURE_GENERATOR_NAME and labels["architecture.mmd"] == "repodocs-deterministic"
    assert first.changed is True and second.changed is False
    # With no stored view at all (a clean clone) the unedited file passes structurally and is reported as not
    # verified; the equality check against a stored view is covered in test_architecture_cli.
    code, payload = invoke(root, capsys, "validate")
    assert code == 0 and any("not verified" in warning for warning in payload["warnings"])


# ------------------------------------------------------------------ alternatives
def test_stored_alternatives_are_rendered_with_their_components_and_citations() -> None:
    view = example()
    alternative = view["alternatives"][0]
    name = next(item["name"] for item in view["components"] if item["id"] in alternative["about"])

    text = render_alternatives(view, {alternative["evidence"][0]: "shop:server.js:1-40"})

    assert "orders.json may be a seed file" in text and name in text and "shop:server.js:1-40" in text
    assert "Citations are evidence to check, not proof" in text
    assert text == render_alternatives(view, {alternative["evidence"][0]: "shop:server.js:1-40"}), "deterministic"


def test_an_uncited_location_falls_back_to_the_evidence_id_and_no_alternatives_gives_nothing() -> None:
    view = example()
    assert view["alternatives"][0]["evidence"][0] in render_alternatives(view)
    view["alternatives"] = []
    assert render_alternatives(view) is None


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_alternative_text_stays_plain_text(text: str) -> None:
    view = example()
    view["alternatives"][0].update(statement=text)
    view["components"][0]["name"] = text
    view["alternatives"][0]["about"] = [view["components"][0]["id"]]

    rendered = render_alternatives(view, {view["alternatives"][0]["evidence"][0]: text}) or ""

    body = rendered.split("\n", 4)[4]  # past the title and the fixed banner
    for special in "<>`[]|":
        assert not re.search(r"(?<!\\)" + re.escape(special), body), special
    statement = body.split("## Alternative 1")[1].split("\n\n")[1]
    assert "\n" not in statement


def test_the_alternatives_document_is_owned_retired_and_no_op_when_unchanged(tmp_path: Path, capsys) -> None:
    root, config, ir = scanned_project(tmp_path, capsys)
    view = example()
    artifacts = {**render_view(view), ALTERNATIVES_NAME: render_alternatives(view) or ""}

    first = generate_docs(config, ir, artifacts)
    second = generate_docs(config, ir, artifacts)

    labels = {item["path"]: item["generator"] for item in read_manifest(root)["artifacts"]}
    assert labels[ALTERNATIVES_NAME] == ARCHITECTURE_GENERATOR_NAME
    assert first.changed is True and second.changed is False
    generate_docs(config, ir, render_view(view))
    assert not (config.output_path / ALTERNATIVES_NAME).exists()


def test_a_diagram_that_is_no_longer_produced_is_retired_safely(tmp_path: Path, capsys) -> None:
    root, config, ir = scanned_project(tmp_path, capsys)
    crowded = render_view(synthetic(30))
    generate_docs(config, ir, crowded)
    output = config.output_path
    assert len([path for path in output.iterdir() if path.name.startswith("architecture-view")]) == len(crowded) > 2

    generate_docs(config, ir, render_view(example()))

    assert sorted(path.name for path in output.iterdir() if path.name.startswith("architecture-view")) == ["architecture-view.mmd"]
    generate_docs(config, ir, {})
    assert not any(path.name.startswith("architecture-view") for path in output.iterdir())


def test_an_edited_architecture_diagram_is_never_overwritten_or_deleted(tmp_path: Path, capsys) -> None:
    root, config, ir = scanned_project(tmp_path, capsys)
    generate_docs(config, ir, render_view(example()))
    target = config.output_path / "architecture-view.mmd"
    target.write_text(target.read_text(encoding="utf-8") + "%% edited by hand\n", encoding="utf-8", newline="\n")

    with pytest.raises(OwnershipError, match="edited unexpectedly"):
        generate_docs(config, ir, render_view(example()))
    with pytest.raises(OwnershipError, match="edited unexpectedly"):
        generate_docs(config, ir, {})
    assert "edited by hand" in target.read_text(encoding="utf-8")
    code, payload = invoke(root, capsys, "validate")
    assert code != 0 and not payload["ok"]


def test_an_unowned_file_with_an_architecture_name_is_not_taken_over(tmp_path: Path, capsys) -> None:
    root, config, ir = scanned_project(tmp_path, capsys)
    generate_docs(config, ir, {})
    stranger = config.output_path / "architecture-view.mmd"
    stranger.write_text("flowchart LR\n    mine[\"mine\"]\n", encoding="utf-8")

    with pytest.raises(OwnershipError, match="unowned files"):
        generate_docs(config, ir, render_view(example()))
    assert "mine" in stranger.read_text(encoding="utf-8")


def test_names_that_are_not_architecture_artifacts_are_still_refused(tmp_path: Path, capsys) -> None:
    root, config, ir = scanned_project(tmp_path, capsys)

    for name in ("notes.mmd", "architecture-view/evil.mmd", "architecture-view.exe", "../architecture-view.mmd"):
        with pytest.raises(OwnershipError):
            generate_docs(config, ir, {name: "flowchart LR\n"})


# ------------------------------------------------------------------ the real renderer
def renderer_or_skip() -> None:
    if find_renderer() is None or not (os.environ.get("PUPPETEER_EXECUTABLE_PATH") or find_renderer()):
        if os.environ.get("REPODOCS_REQUIRE_RENDERER") == "1":
            pytest.fail("REPODOCS_REQUIRE_RENDERER=1 but the Mermaid renderer is not installed (npm ci in tools/mermaid)")
        pytest.skip("the optional Mermaid renderer is not installed; run `npm ci` in tools/mermaid")
    from repodocs.architecture.render_check import find_browser

    if find_browser() is None:
        if os.environ.get("REPODOCS_REQUIRE_RENDERER") == "1":
            pytest.fail("REPODOCS_REQUIRE_RENDERER=1 but no Chrome was found")
        pytest.skip("no Chrome or Chromium was found")


def test_the_real_renderer_draws_representative_architecture_views_including_hostile_text() -> None:
    renderer_or_skip()
    hostile = example()
    hostile["components"][0]["name"] = 'Orders" ]; click a href "javascript:alert(1)'
    hostile["components"][1]["name"] = "<img src=x onerror=alert(1)> <script>alert(2)</script>"
    hostile["connections"][0]["label"] = "%%{init: {\"securityLevel\": \"loose\"}}%% `tick`"
    diagrams = {}
    for label, view in (("example", example()), ("hostile", hostile), ("crowded", synthetic(30, repositories=("shop", "billing")))):
        for name, text in render_view(view).items():
            diagrams[f"{label}-{name}"] = text

    result = render(diagrams)

    assert result.status == RENDERED, result.message
    assert set(result.svgs) == set(diagrams)
    for name, svg in result.svgs.items():
        assert svg.lstrip().startswith("<svg") and svg_problems(svg) == [], name
    assert "alert(1)" in result.svgs["hostile-architecture-view.mmd"], "hostile text is drawn as text"
    assert "<script" not in result.svgs["hostile-architecture-view.mmd"].lower()


def test_the_real_renderer_draws_the_existing_erd_and_overview_fixtures(tmp_path: Path, capsys) -> None:
    renderer_or_skip()
    root = tmp_path / "project"
    root.mkdir()
    repository = copy_fixture_named(tmp_path, "postgres-repo")
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "database", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    output = ProjectConfig.load(root).output_path
    diagrams = {name: (output / name).read_text(encoding="utf-8") for name in ("erd.mmd", "architecture.mmd")}
    assert diagrams["erd.mmd"].startswith("erDiagram")

    result = render(diagrams)

    assert result.status == RENDERED, result.message
    assert "customers" in result.svgs["erd.mmd"] and "orders" in result.svgs["erd.mmd"]


def copy_fixture_named(tmp_path: Path, name: str) -> Path:
    destination = tmp_path / name
    shutil.copytree(ROOT / "tests" / "fixtures" / name, destination)
    return destination


def test_a_diagram_the_real_renderer_rejects_is_reported_by_name() -> None:
    renderer_or_skip()

    result = render({"good.mmd": render_view(example())["architecture-view.mmd"], "broken.mmd": "flowchart LR\n    a --> \n"})

    assert result.status == FAILED
    assert "broken.mmd" in result.message and "good.mmd" not in result.message


def test_a_missing_renderer_is_not_success(tmp_path: Path) -> None:
    result = render({"a.mmd": "flowchart LR\n    a[\"a\"]\n"}, tool_directory=tmp_path / "nowhere")

    assert result.status == NOT_AVAILABLE and result.status != RENDERED and result.svgs == {}
    assert "not checked" in result.message and "npm ci" in result.message


@pytest.mark.parametrize("svg", [
    "<svg><script>alert(1)</script></svg>",
    '<svg><a href="javascript:alert(1)"><text>x</text></a></svg>',
    '<svg><image href="data:image/svg+xml;base64,AAAA"/></svg>',
    '<svg onload="alert(1)"></svg>',
    '<svg><foreignObject><div>x</div></foreignObject></svg>',
    '<svg><a href="https://evil.example/">x</a></svg>',
    '<svg><a xlink:href="//evil.example/">x</a></svg>',
])
def test_an_unsafe_rendered_svg_is_refused(svg: str) -> None:
    assert svg_problems(svg)
    assert _screened({"x.mmd": svg}).status == FAILED


def test_a_plain_svg_is_accepted() -> None:
    assert svg_problems('<svg xmlns="http://www.w3.org/2000/svg"><g class="node"><text>Orders</text></g></svg>') == []
