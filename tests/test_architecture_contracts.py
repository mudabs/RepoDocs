"""The architecture-discovery contracts (ADR-026): schemas, examples, and what they refuse."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
VIEW = "architecture-view-1.0.json"
CONTEXT = "architecture-context-1.0.json"


def schema(name: str) -> Draft202012Validator:
    data = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(data)
    return Draft202012Validator(data)


def example(name: str) -> dict:
    return json.loads((ROOT / "docs" / "examples" / name).read_text(encoding="utf-8"))


def test_the_published_examples_satisfy_their_schemas() -> None:
    for schema_name, example_name in ((VIEW, "architecture-view.example.json"), (CONTEXT, "architecture-context.example.json")):
        errors = [error.message for error in schema(schema_name).iter_errors(example(example_name))]
        assert not errors, errors


def test_every_example_citation_resolves_to_a_context_excerpt_of_the_same_snapshot() -> None:
    view, context = example("architecture-view.example.json"), example("architecture-context.example.json")
    documents = {item["id"]: item for item in context["documents"]}
    assert view["provenance"]["snapshots"] == context["snapshots"]
    assert view["provenance"]["source_ir_hash"] == context["source_ir_hash"]
    components = {item["id"] for item in view["components"]}
    for component in view["components"]:
        assert set(component["evidence"]) <= set(documents)
        assert all(documents[key]["repository_id"] == component["repository_id"] for key in component["evidence"])
    for connection in view["connections"]:
        assert {connection["source"], connection["target"]} <= components
        assert set(connection["evidence"]) <= set(documents)


def test_an_inferred_example_item_cites_a_member_file_of_each_component_it_names() -> None:
    """Classification is about evidence tied to the components, not only about citations that exist."""

    view, context = example("architecture-view.example.json"), example("architecture-context.example.json")
    paths = {item["id"]: (item["repository_id"], item["path"]) for item in context["documents"]}
    components = {item["id"]: item for item in view["components"]}

    def tied(component: dict) -> bool:
        return any(paths[key] == (component["repository_id"], member) for key in component["evidence"] for member in component["members"])

    def tied_to(connection: dict, component: dict) -> bool:
        return any(paths[key] == (component["repository_id"], member) for key in connection["evidence"] for member in component["members"])

    inferred_components = [item for item in view["components"] if item["classification"] == "inferred"]
    inferred_connections = [item for item in view["connections"] if item["classification"] == "inferred"]
    assert inferred_components and inferred_connections
    for component in inferred_components:
        assert tied(component), component["name"]
    for connection in inferred_connections:
        assert tied_to(connection, components[connection["source"]]) and tied_to(connection, components[connection["target"]]), connection["label"]
    # The one connection with evidence from a single side is shown as proposed, not inferred.
    one_sided = [item for item in view["connections"] if not (tied_to(item, components[item["source"]]) and tied_to(item, components[item["target"]]))]
    assert one_sided and all(item["classification"] == "proposed" for item in one_sided)


def mutate(name: str, edit) -> dict:
    data = copy.deepcopy(example(name))
    edit(data)
    return data


VIEW_BREAKS = [
    lambda d: d["components"][0].update(classification="observed"),
    lambda d: d["connections"][0].update(classification="observed"),
    lambda d: d["components"][0].update(evidence=[]),
    lambda d: d["components"][0].update(kind="microservice-mesh"),
    lambda d: d["components"][0].update(id="component-1"),
    lambda d: d["components"][0].update(responsibility="x" * 301),
    lambda d: d["components"][0].pop("members"),
    lambda d: d["connections"][0].update(kind="runs-before"),
    lambda d: d["connections"][0].update(evidence=["evidence:abc"]),
    lambda d: d["connections"][0].update(evidence=[]),
    lambda d: d["connections"][0].pop("cross_repository"),
    lambda d: d.update(status="stale"),
    lambda d: d.update(model_output="<script>"),
    lambda d: d["provenance"].pop("served_model"),
    lambda d: d["provenance"].update(provider="openai-compatible-gateway"),  # only the native providers (ADR-032)
    lambda d: d["rejected"].update(reason=-1),
]


@pytest.mark.parametrize("edit", VIEW_BREAKS, ids=[str(index) for index in range(len(VIEW_BREAKS))])
def test_the_view_schema_refuses_authority_it_must_not_grant(edit) -> None:
    assert list(schema(VIEW).iter_errors(mutate("architecture-view.example.json", edit)))


def test_a_complete_and_a_partial_view_are_both_valid() -> None:
    complete = mutate("architecture-view.example.json", lambda d: None)
    partial = mutate("architecture-view.example.json", lambda d: d.update(status="partial", partial_reason="round-budget"))

    assert complete["status"] == "complete" and "partial_reason" not in complete
    for view in (complete, partial):
        assert not list(schema(VIEW).iter_errors(view))
    for reason in ("request-budget", "context-budget", "response-budget", "timeout", "provider-error", "incomplete-response", "empty-response", "no-progress"):
        assert not list(schema(VIEW).iter_errors({**partial, "partial_reason": reason}))


@pytest.mark.parametrize("edit", [
    lambda d: d.update(status="partial"),
    lambda d: d.update(status="complete", partial_reason="round-budget"),
    lambda d: d.update(status="partial", partial_reason="because"),
], ids=["partial without a reason", "complete with a reason", "partial with an unknown reason"])
def test_status_and_partial_reason_must_agree(edit) -> None:
    assert list(schema(VIEW).iter_errors(mutate("architecture-view.example.json", edit)))


CONTEXT_BREAKS = [
    lambda d: d["documents"][0].update(id="ctx:short"),
    lambda d: d["documents"][0].update(kind="binary"),
    lambda d: d["documents"][0].pop("content_hash"),
    lambda d: d["omitted"][0].update(reason="because"),
    lambda d: d["policy"]["budgets"].pop("max_rounds"),
    lambda d: d["policy"]["budgets"].update(max_requests=0),
    lambda d: d.update(unredacted_originals=[]),
]


@pytest.mark.parametrize("edit", CONTEXT_BREAKS, ids=[str(index) for index in range(len(CONTEXT_BREAKS))])
def test_the_context_schema_refuses_malformed_or_unbounded_context(edit) -> None:
    assert list(schema(CONTEXT).iter_errors(mutate("architecture-context.example.json", edit)))
