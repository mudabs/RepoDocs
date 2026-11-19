"""The evaluation harness (#54): it scores faithfully, notices injected faults, and says what it cannot show.

The reference provider answers from the expected set. It tests the harness and the workflow; it is not
a model, and nothing here measures how well a real model describes an application.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from repodocs.architecture import evaluation
from repodocs.architecture.context import ContextResolver
from repodocs.architecture.discovery import discover, save_view
from repodocs.architecture.evaluation import ReferenceProvider, evaluate, main, match_components, summary
from repodocs.config import ProjectConfig

from conftest import invoke, read_ir
from test_architecture_discovery import FIXTURE

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DIRECTORY = ROOT / "docs" / "evaluation" / "architecture-discovery" / "expected"

SHOP = {
    "name": "shop",
    "source": {"repository": "tests/fixtures/express-shop-repo", "revision": "local", "license": "test fixture"},
    "components": [
        {"name": "Web page", "kind": "frontend", "members": ["public/**"]},
        {"name": "Orders API", "kind": "backend-service", "members": ["server.js", "lib/storage.js"]},
        {"name": "Order file", "kind": "storage", "members": ["orders.json"], "required": False},
        {"name": "Serverless entry", "kind": "other", "deployment": True, "members": ["package.json"]},
    ],
    "relationships": [
        {"source": "Web page", "target": "Orders API", "kinds": ["calls"]},
        {"source": "Orders API", "target": "Order file", "kinds": ["reads", "writes"]},
        {"source": "Serverless entry", "target": "Orders API", "kinds": ["depends-on"], "deployment": True},
    ],
    "not_established": [{"source": "Web page", "target": "Order file", "why": "The page reaches orders only through the API."}],
}


def project(tmp_path: Path, capsys) -> tuple[Path, ProjectConfig, dict]:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(FIXTURE, tmp_path / "shop")
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(tmp_path / "shop"), "--id", "shop", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    return root, ProjectConfig.load(root), read_ir(root)


def at(ir: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """The expected set bound to the revision the fixture was scanned at, as a real one is bound to its commit."""

    return {**expected, "source": {**expected["source"], "revision": ir["snapshots"][0]["revision"]}}


def report(tmp_path: Path, capsys, *defects: str, expected: dict[str, Any] = SHOP) -> dict[str, Any]:
    root, config, ir = project(tmp_path, capsys)
    expected = at(ir, expected)
    view, context = discover(config, ir, ReferenceProvider(expected, defects))
    return evaluate(config, ir, view, context, expected)


# ------------------------------------------------------------------ scoring
def test_a_faithful_answer_scores_fully_and_the_report_says_what_it_cannot_show(tmp_path: Path, capsys) -> None:
    result = report(tmp_path, capsys)

    components, connections = result["components"], result["connections"]
    assert components["recall"] == 1.0 and components["missing"] == [] and components["extra"] == []
    assert components["deployment_shown_as_application"] == ["Serverless entry"] and components["precision"] == 0.75
    assert connections["precision"] == 1.0 and connections["recall"] == 1.0 and connections["unsupported_for_review"] == {}
    assert result["evidence"]["resolution"] == 1.0 and result["evidence"]["member_paths_in_snapshot"] == result["evidence"]["member_paths"]
    assert "does not prove a relationship is true" in result["limits"]
    assert result["deterministic_baseline"]["application_components_found"] == 0 and result["deterministic_baseline"]["semantic_nodes"] == 0
    assert result["review"] and all(row["verdict"] is None for row in result["review"]), "a person must give every verdict"


@pytest.mark.parametrize("defect, check", [
    ("missing-component", lambda r: r["components"]["recall"] == 0.5 and r["components"]["missing"] == ["Web page"] and r["connections"]["recall"] < 1),
    ("fabricated-citation", lambda r: r["uncertainty"]["rejected_by_validation"].get("unsupported-component") == 1 and r["components"]["proposed"] == 4),
    ("unsupported-connection", lambda r: len(r["connections"]["unsupported_for_review"].get("contradicted", [])) == 1 and r["connections"]["precision"] < 1),
    ("reversed-connection", lambda r: r["connections"]["recall"] < 1 and len(r["connections"]["unsupported_for_review"].get("contradicted", [])) == 1),
])
def test_each_injected_fault_is_noticed(tmp_path: Path, capsys, defect: str, check) -> None:
    result = report(tmp_path, capsys, defect)

    assert check(result), result


def test_a_connection_of_the_wrong_kind_is_unsupported_and_recovers_nothing(tmp_path: Path, capsys) -> None:
    result = report(tmp_path, capsys, "wrong-kind")["connections"]

    assert result["supported"] == 0 and result["precision"] == 0.0
    assert result["covered"] == 0 and result["recall"] == 0.0
    assert len(result["unsupported_for_review"]["wrong-kind"]) == 3, "including the deployment relationship, which is scored apart from recall"
    assert result["missing"] == ["Orders API -> Order file", "Web page -> Orders API"]


def test_a_second_connection_between_the_same_components_adds_no_coverage(tmp_path: Path, capsys) -> None:
    result = report(tmp_path, capsys, "duplicate-connection")["connections"]

    assert result["supported"] == 3 and result["covered"] == 2 and result["recall"] == 1.0
    assert len(result["unsupported_for_review"]["duplicate"]) == 1
    assert result["precision"] == 0.75, "the duplicate counts against precision, never for recall"


def test_an_expected_set_that_would_distort_the_scores_is_refused_with_every_problem_named(tmp_path: Path, capsys) -> None:
    root, config, ir = project(tmp_path, capsys)
    good = at(ir, SHOP)
    view, context = discover(config, ir, ReferenceProvider(good))
    broken = copy.deepcopy(good)
    broken["components"].append(copy.deepcopy(broken["components"][0]))
    broken["relationships"].append(copy.deepcopy(broken["relationships"][0]))
    broken["relationships"].append({"source": "Web page", "target": "Ghost", "kinds": ["calls"]})
    broken["relationships"].append({"source": "Orders API", "target": "Order file", "kinds": ["teleports"]})
    broken["not_established"].append({"source": "Web page", "target": "Orders API", "why": "contradiction"})

    with pytest.raises(evaluation.EvaluationInputError) as caught:
        evaluate(config, ir, view, context, broken)

    text = str(caught.value)
    for fragment in ("component 'Web page' is listed more than once", "'Web page' -> 'Orders API' is listed more than once", "names a component that is not listed",
                     "needs `kinds` from", "both expected and not established"):
        assert fragment in text, fragment
    for bad in (None, [], {"name": "x"}):
        with pytest.raises(evaluation.EvaluationInputError):
            evaluate(config, ir, view, context, bad)


def _with(**changes: Any) -> dict[str, Any]:
    expected = copy.deepcopy(SHOP)
    expected.update(changes)
    return expected


def _relationship(**changes: Any) -> dict[str, Any]:
    return _with(relationships=[{**copy.deepcopy(SHOP["relationships"][0]), **changes}])


def _component(**changes: Any) -> dict[str, Any]:
    expected = copy.deepcopy(SHOP)
    expected["components"][0].update(changes)
    return expected


MALFORMED = {
    "relationships null": _with(relationships=None),
    "relationships number": _with(relationships=42),
    "relationships text": _with(relationships="calls"),
    "relationships object": _with(relationships={}),
    "relationships missing": {key: value for key, value in SHOP.items() if key != "relationships"},
    "kinds nested": _relationship(kinds=[["calls"]]),
    "kinds number": _relationship(kinds=7),
    "kinds text": _relationship(kinds="calls"),
    "source list": _relationship(source=["Web page"]),
    "target number": _relationship(target=3),
    "relationship deployment": _relationship(deployment="yes"),
    "not_established null": _with(not_established=None),
    "not_established number": _with(not_established=42),
    "not_established entry": _with(not_established=[1]),
    "not_established empty entry": _with(not_established=[{}]),
    "not_established endpoint": _with(not_established=[{"source": ["Web page"], "target": "Order file"}]),
    "required text": _component(required="yes"),
    "deployment number": _component(deployment=1),
    "members nested": _component(members=[["server.js"]]),
    "components entry": _with(components=[None]),
    "source not an object": _with(source=["x"]),
    "source field number": _with(source={"repository": 1, "revision": "r", "license": "MIT"}),
}


@pytest.mark.parametrize("expected", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_expected_set_is_an_input_error_never_a_crash(expected: Any) -> None:
    with pytest.raises(evaluation.EvaluationInputError, match="not usable"):
        evaluation.check_expected(expected)


def test_a_valid_expected_set_still_passes_the_check() -> None:
    evaluation.check_expected(copy.deepcopy(SHOP))


def test_the_command_reports_a_malformed_expected_set_as_cannot_score_with_exit_2(tmp_path: Path, capsys) -> None:
    root, config, ir = project(tmp_path, capsys)
    save_view(config, *discover(config, ir, ReferenceProvider(at(ir, SHOP))))
    capsys.readouterr()
    for name in ("relationships null", "relationships number", "kinds nested", "not_established endpoint", "required text"):
        expected = tmp_path / "expected.json"
        expected.write_text(json.dumps(MALFORMED[name]), encoding="utf-8")

        assert main(["--root", str(root), "--expected", str(expected)]) == 2, name
        error = capsys.readouterr().err
        assert "Cannot score" in error and "Traceback" not in error, name


def test_the_expected_set_must_describe_the_revision_that_was_scanned(tmp_path: Path, capsys) -> None:
    root, config, ir = project(tmp_path, capsys)
    view, context = discover(config, ir, ReferenceProvider(at(ir, SHOP)))

    with pytest.raises(evaluation.EvaluationInputError, match="written from revision local.*scanned at shop: content"):
        evaluate(config, ir, view, context, SHOP)


def test_the_command_reports_unusable_inputs_with_exit_2_and_a_diagnostic(tmp_path: Path, capsys) -> None:
    root, config, ir = project(tmp_path, capsys)
    save_view(config, *discover(config, ir, ReferenceProvider(at(ir, SHOP))))
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(SHOP), encoding="utf-8")

    assert main(["--root", str(root), "--expected", str(expected)]) == 2
    assert "Cannot score" in capsys.readouterr().err
    expected.write_text("{not json", encoding="utf-8")
    assert main(["--root", str(root), "--expected", str(expected)]) == 2
    assert main(["--root", str(root), "--expected", str(tmp_path / "missing.json")]) == 2


def test_components_are_matched_by_the_files_they_claim_not_by_name() -> None:
    expected = SHOP
    view = {"components": [
        {"id": "arch:1", "name": "Frontend", "members": ["public/app.js", "public/index.html"]},
        {"id": "arch:2", "name": "Server", "members": ["server.js", "lib/storage.js", "README.md"]},
        {"id": "arch:3", "name": "Mixed", "members": ["public/app.js", "server.js", "lib/x.js", "README.md"]},
    ]}

    assert match_components(view, expected) == {"arch:1": "Web page", "arch:2": "Orders API"}


def test_an_expected_component_is_credited_once() -> None:
    view = {"components": [
        {"id": "arch:1", "name": "A", "members": ["server.js"]},
        {"id": "arch:2", "name": "B", "members": ["lib/storage.js"]},
    ]}

    matched = match_components(view, SHOP)

    assert list(matched.values()) == ["Orders API"] and len(matched) == 1


def test_the_report_is_reproducible(tmp_path: Path, capsys) -> None:
    first = report(tmp_path / "a", capsys) if (tmp_path / "a").mkdir() is None else None
    second = report(tmp_path / "b", capsys) if (tmp_path / "b").mkdir() is None else None

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_summary_is_a_readable_table(tmp_path: Path, capsys) -> None:
    text = summary(report(tmp_path, capsys))

    assert "shop (ollama, reference-replay)" in text and "component recall (required)" in text and "1.0 (2/2)" in text


# ------------------------------------------------------------------ the command
def test_the_command_scores_a_stored_current_view_and_refuses_a_stale_one(tmp_path: Path, capsys) -> None:
    root, config, ir = project(tmp_path, capsys)
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(at(ir, SHOP)), encoding="utf-8")
    view, context = discover(config, ir, ReferenceProvider(SHOP))
    save_view(config, view, context)

    assert main(["--root", str(root), "--expected", str(expected), "--summary"]) == 0
    printed = capsys.readouterr().out
    assert "component recall" in printed

    assert main(["--root", str(root), "--expected", str(expected)]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "shop"

    (tmp_path / "shop" / "server.js").write_text("// changed\n", encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0
    assert main(["--root", str(root), "--expected", str(expected)]) == 1
    assert "stale" in capsys.readouterr().err


# ------------------------------------------------------------------ the expected files
@pytest.mark.parametrize("name", ["digitalbonebox", "flaskr"])
def test_the_expected_files_are_complete_consistent_and_state_their_sources(name: str) -> None:
    expected = json.loads((EXPECTED_DIRECTORY / f"{name}.json").read_text(encoding="utf-8"))

    assert expected["source"]["revision"] and len(expected["source"]["revision"]) == 40
    assert expected["source"]["license"] and expected["source"]["repository"].startswith("https://github.com/")
    assert "Before any output was scored" in expected["written"]
    names = [item["name"] for item in expected["components"]]
    assert len(names) == len(set(names)) >= 5
    for item in expected["components"]:
        assert item["members"] and item["inspected"] and item["kind"]
    pairs = [(item["source"], item["target"]) for item in expected["relationships"]]
    assert len(pairs) == len(set(pairs)) >= 4
    for item in expected["relationships"]:
        assert {item["source"], item["target"]} <= set(names) and item["kinds"] and item["inspected"]
    for item in expected["not_established"]:
        assert {item["source"], item["target"]} <= set(names) and item["why"]
        assert (item["source"], item["target"]) not in pairs, "a relationship cannot be both expected and not established"


# ------------------------------------------------------------------ what the evaluation found in the context selection
def write(base: Path, relative: str, text: str = "x\n") -> None:
    target = base / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def crowded_project(tmp_path: Path, capsys, repositories: dict[str, dict[str, str]]) -> ContextResolver:
    root = tmp_path / "project"
    root.mkdir()
    assert invoke(root, capsys, "init", "p")[0] == 0
    for index, (name, files) in enumerate(sorted(repositories.items())):
        for relative, text in files.items():
            write(tmp_path / name, relative, text)
        arguments = ["add", str(tmp_path / name), "--id", name] + (["--destination"] if index == 0 else [])
        assert invoke(root, capsys, *arguments)[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    return ContextResolver(ProjectConfig.load(root), read_ir(root))


def test_a_large_data_directory_cannot_crowd_the_source_out_of_the_inventory(tmp_path: Path, capsys) -> None:
    files = {f"data/item-{index:04d}.json": "{}\n" for index in range(700)}
    files.update({"src/server.js": "const app = 1;\n", "src/routes/orders.js": "module.exports = 1;\n", "README.md": "# App\n"})
    resolver = crowded_project(tmp_path, capsys, {"app": files})
    resolver.select_initial()

    listing = resolver.bundle()["inventory"][0]

    assert listing["truncated"] is False and len(listing["paths"]) < 60
    assert "src/server.js" in listing["paths"] and "src/routes/orders.js" in listing["paths"]
    assert "data/(+688 more files, not listed)" in listing["paths"]
    assert sum(path.startswith("data/item-") for path in listing["paths"]) == 12


def test_the_first_round_gives_each_repository_a_share_and_puts_container_files_last(tmp_path: Path, capsys) -> None:
    def repository(entry: str) -> dict[str, str]:
        return {"README.md": "# R\n", "package.json": "{}\n", "Dockerfile": "FROM node\n", "docker-compose.yml": "services: {}\n",
                f"{entry}": "x\n", "other/Dockerfile": "FROM node\n", "other/package.json": "{}\n"}

    resolver = crowded_project(tmp_path, capsys, {"alpha": repository("server.js"), "beta": repository("app.js")})
    resolver.select_initial()

    chosen = {(item["repository_id"], item["path"]) for item in resolver.documents.values()}
    for name, entry in (("alpha", "server.js"), ("beta", "app.js")):
        assert (name, "README.md") in chosen and (name, "package.json") in chosen and (name, entry) in chosen
    assert len(chosen) <= 8
