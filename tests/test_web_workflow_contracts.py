"""The local web adapter contract is additive; no HTTP server or provider is needed."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/web-workflow-1.0.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((ROOT / "docs/examples/web-workflow.example.json").read_text(encoding="utf-8"))


def validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(SCHEMA)
    return Draft202012Validator(SCHEMA)


def job(state: str) -> dict:
    return copy.deepcopy(next(item for item in EXAMPLES if item.get("state") == state))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item.get("state", "request"))
def test_every_published_workflow_example_validates(example: dict) -> None:
    validator().validate(example)


@pytest.mark.parametrize("change", ["partial-result", "no-result", "error", "unfinished-stage"])
def test_success_cannot_hide_incomplete_work(change: str) -> None:
    value = job("succeeded")
    if change == "partial-result":
        value["result"]["completeness"] = "partial"
    elif change == "no-result":
        value["result"] = None
    elif change == "error":
        value["error"] = job("failed")["error"]
    else:
        value["stage"] = "discover"
    assert not validator().is_valid(value)


@pytest.mark.parametrize("state", ["partial", "budget-exhausted", "cancelled"])
def test_non_success_states_cannot_claim_a_complete_result(state: str) -> None:
    value = job(state)
    value["result"] = job("succeeded")["result"]
    assert not validator().is_valid(value)


@pytest.mark.parametrize("state", ["failed", "interrupted"])
def test_failed_jobs_cannot_expose_a_success_result(state: str) -> None:
    value = job(state)
    value["result"] = job("succeeded")["result"]
    assert not validator().is_valid(value)


@pytest.mark.parametrize("state", ["partial", "budget-exhausted", "cancelled", "failed", "stale", "interrupted"])
def test_unsuccessful_states_require_a_reason(state: str) -> None:
    value = job(state)
    value["error"] = None
    assert not validator().is_valid(value)


@pytest.mark.parametrize("state", ["budget-exhausted", "cancelled", "stale", "interrupted"])
def test_state_and_error_code_cannot_disagree(state: str) -> None:
    value = job(state)
    value["error"]["code"] = "provider-failure"
    assert not validator().is_valid(value)


@pytest.mark.parametrize("field,value", [("contract_version", "2.0"), ("state", "success-ish"), ("sequence", -1)])
def test_unknown_contract_values_are_refused(field: str, value: object) -> None:
    candidate = job("succeeded")
    candidate[field] = value
    assert not validator().is_valid(candidate)


@pytest.mark.parametrize("location", ["request", "job", "error", "result", "usage"])
def test_credentials_cannot_be_added_to_persisted_contract_objects(location: str) -> None:
    value = copy.deepcopy(EXAMPLES[0]) if location == "request" else job("partial")
    target = value if location in {"request", "job"} else value[location]
    target["api_key"] = "dummy-not-a-credential"
    assert not validator().is_valid(value)


@pytest.mark.parametrize("field", ["provider_session_id", "context_hash"])
def test_architecture_requires_a_provider_session_and_reviewed_preview(field: str) -> None:
    value = copy.deepcopy(EXAMPLES[0])
    value[field] = None
    assert not validator().is_valid(value)


def test_architecture_requires_a_token_cap() -> None:
    value = copy.deepcopy(EXAMPLES[0])
    value["limits"]["max_total_tokens"] = None
    assert not validator().is_valid(value)


def test_architecture_cannot_admit_a_job_with_no_provider_requests() -> None:
    value = copy.deepcopy(EXAMPLES[0])
    value["limits"]["max_requests"] = 0
    assert not validator().is_valid(value)


def test_deterministic_request_needs_no_provider_and_cannot_spend() -> None:
    value = copy.deepcopy(EXAMPLES[0])
    value.update(mode="deterministic", provider_session_id=None, context_hash=None, consent_receipt_id=None)
    value["limits"] = {"max_requests": 0, "max_total_tokens": None, "max_cost_microunits": 0, "currency": "USD"}
    validator().validate(value)
    value["limits"]["max_requests"] = 1
    assert not validator().is_valid(value)


@pytest.mark.parametrize("field,value", [("max_requests", 7), ("max_total_tokens", 0), ("max_cost_microunits", -1)])
def test_invalid_or_policy_exceeding_budgets_are_refused(field: str, value: int) -> None:
    candidate = copy.deepcopy(EXAMPLES[0])
    candidate["limits"][field] = value
    assert not validator().is_valid(candidate)


def test_a_cost_cap_requires_an_explicit_currency() -> None:
    value = copy.deepcopy(EXAMPLES[0])
    value["limits"]["max_cost_microunits"] = 10000
    assert not validator().is_valid(value)
    value["limits"]["currency"] = "USD"
    validator().validate(value)


def test_unknown_usage_is_representable_without_inventing_zero_cost() -> None:
    value = job("cancelled")
    assert value["usage"]["accounting"] == "unknown"
    assert value["usage"]["cost_microunits"] is None
    validator().validate(value)
