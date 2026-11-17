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
    lambda d: d["provenance"].update(provider="openai"),
    lambda d: d["rejected"].update(reason=-1),
]


@pytest.mark.parametrize("edit", VIEW_BREAKS, ids=[str(index) for index in range(len(VIEW_BREAKS))])
def test_the_view_schema_refuses_authority_it_must_not_grant(edit) -> None:
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
