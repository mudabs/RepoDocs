"""The golden-fixture harness: the committed baseline, schema conformance, and proof that every metric can fail."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from repodocs import golden
from repodocs.golden import (
    GoldenError,
    Observed,
    diff_reports,
    evaluate_cases,
    load_case,
    main,
    run_pipeline,
    score_case,
)


ROOT = Path(__file__).parent
CASES_DIR = ROOT / "golden"
BASELINE = CASES_DIR / "baseline-report.json"
SCHEMAS = ROOT.parent / "schemas"
NAMES = ("smartcart-correlation", "spring-boot", "postgres")
STABLE = {"ir_identical": True, "artifacts_identical": True}
REGENERATE = "python -m repodocs.golden tests/golden --output tests/golden/baseline-report.json"


def schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / name).read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def report() -> dict:
    return evaluate_cases(CASES_DIR)


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict:
    result = {}
    for name in NAMES:
        case = load_case(CASES_DIR / name / "golden.json")
        result[name] = (case, run_pipeline(case, tmp_path_factory.mktemp(name)))
    return result


def baseline_case(name: str) -> dict:
    return next(item for item in json.loads(BASELINE.read_text(encoding="utf-8"))["cases"] if item["name"] == name)


def scored(runs, name, mutate=None, edit_case=None, stability=STABLE) -> dict:
    """Score a damaged copy of a real observed result, leaving the shared one untouched."""

    case, observed = runs[name]
    case = copy.deepcopy(case)
    damaged = Observed(copy.deepcopy(observed.ir), dict(observed.artifacts), observed.repositories)
    if mutate:
        mutate(damaged)
    if edit_case:
        edit_case(case)
    return score_case(case, damaged, stability)


def node(ir: dict, kind: str, name: str, repository: str | None = None) -> dict:
    return next(
        item for item in ir["nodes"]
        if item["type"] == kind and item["name"] == name and (repository is None or item["attributes"].get("repository_id") == repository)
    )


def drop_node(ir: dict, target: dict) -> None:
    """Remove a node and everything that depended on it, as a tool that never found it would."""

    ir["nodes"] = [item for item in ir["nodes"] if item["id"] != target["id"]]
    ir["relationships"] = [e for e in ir["relationships"] if target["id"] not in (e["source"], e["target"])]
    ir["candidates"] = [c for c in ir["candidates"] if target["id"] not in (c["source"], c["target"])]


# ------------------------------------------------------------------ the committed baseline
def test_report_equals_the_committed_baseline_exactly(report) -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    differences = diff_reports(baseline, report)
    assert not differences, (
        "The evaluation report changed. If the change is intended, review it and regenerate the baseline with\n"
        f"  {REGENERATE}\n" + "\n".join(differences[:20])
    )


def test_report_and_cases_conform_to_their_schemas(report) -> None:
    assert not list(schema("golden-report-1.0.json").iter_errors(report))
    for name in NAMES:
        data = json.loads((CASES_DIR / name / "golden.json").read_text(encoding="utf-8"))
        assert not list(schema("golden-case-1.0.json").iter_errors(data)), name


def test_baseline_has_no_invariant_failures_and_covers_every_case(report) -> None:
    assert report["failures"] == 0
    assert [item["name"] for item in report["cases"]] == sorted(NAMES)
    assert all(item["failures"] == [] for item in report["cases"])


def test_the_pipeline_is_repeatable_across_independent_evaluations(report) -> None:
    assert evaluate_cases(CASES_DIR) == report


@pytest.mark.parametrize("seed", ["0", "4242"])
def test_report_does_not_depend_on_the_hash_seed(seed) -> None:
    """The in-report stability check runs in one interpreter and cannot see seed-dependent ordering."""

    environment = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT.parent / "src")}
    done = subprocess.run(
        [sys.executable, "-m", "repodocs.golden", str(CASES_DIR)], capture_output=True, text=True, env=environment, check=False
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == json.loads(BASELINE.read_text(encoding="utf-8"))


def test_golden_files_hold_no_identifiers_or_hashes() -> None:
    """Identifiers and hashes change with line endings and content, so a golden may not depend on them."""

    paths = [CASES_DIR / name / "golden.json" for name in NAMES] + [ROOT / "fixtures" / "ai-architecture" / "golden.json"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b(node|evidence|claim|relationship|candidate|ai-claim):[0-9a-f]{8,}", text), path
        assert not re.search(r"\b[0-9a-f]{40,64}\b", text), path


def test_spring_limitations_cite_the_annotation_that_triggers_them(report) -> None:
    """The harness first recorded these as misses: the analyzer cited the line next to the @Value SpEL and
    @Profile annotations. The golden never changed; the analyzer was fixed, and only the baseline moved."""

    spring = next(item for item in report["cases"] if item["name"] == "spring-boot")["limitations"]
    assert spring["recall"] == 1.0 and spring["precision"] == 1.0
    assert spring["missing"] == [] and spring["unexpected"] == []


# ------------------------------------------------------------------ the harness can fail
def test_dropping_a_node_lowers_node_recall_and_names_it(runs) -> None:
    result = scored(runs, "spring-boot", lambda o: drop_node(o.ir, node(o.ir, "endpoint", "POST /api/orders")))
    assert result["nodes"]["recall"] < 1 and result["nodes"]["precision"] == 1.0
    assert {"type": "endpoint", "repository": "backend", "name": "POST /api/orders"} in result["nodes"]["missing"]
    assert result["relationships"]["recall"] < 1
    assert result["evidence"]["recall"] < baseline_case("spring-boot")["evidence"]["recall"]


def test_adding_a_spurious_node_lowers_precision_and_names_it(runs) -> None:
    def add(o: Observed) -> None:
        source = node(o.ir, "endpoint", "POST /api/orders")
        o.ir["nodes"].append({**copy.deepcopy(source), "id": "node:spurious", "name": "GET /spurious"})

    result = scored(runs, "spring-boot", add)
    assert result["nodes"]["precision"] < 1 and result["nodes"]["recall"] == 1.0
    assert {"type": "endpoint", "repository": "backend", "name": "GET /spurious"} in result["nodes"]["unexpected"]


def test_relationship_errors_are_detected_in_both_directions(runs) -> None:
    def drop(o: Observed) -> None:
        target = node(o.ir, "endpoint", "GET /api/orders")
        o.ir["relationships"] = [e for e in o.ir["relationships"] if not (e["type"] == "exposes" and e["target"] == target["id"])]

    dropped = scored(runs, "spring-boot", drop)
    assert dropped["relationships"]["recall"] < 1 and dropped["relationships"]["missing"]

    def spurious(o: Observed) -> None:
        edge = next(e for e in o.ir["relationships"] if e["type"] == "exposes")
        other = node(o.ir, "class", "CustomerController")
        o.ir["relationships"].append({**edge, "id": "relationship:x", "source": other["id"], "target": node(o.ir, "endpoint", "POST /api/orders")["id"]})

    added = scored(runs, "spring-boot", spurious)
    assert added["relationships"]["precision"] < 1 and added["relationships"]["unexpected"]


def test_a_wrong_classification_counts_as_both_missing_and_unexpected(runs) -> None:
    def weaken(o: Observed) -> None:
        for edge in o.ir["relationships"]:
            if edge["type"] == "references":
                edge["classification"] = "inferred"

    result = scored(runs, "postgres", weaken)
    assert result["relationships"]["recall"] == 0.0 and result["relationships"]["precision"] == 0.0
    assert len(result["relationships"]["missing"]) == 3 and len(result["relationships"]["unexpected"]) == 3


def test_evidence_that_no_longer_points_at_the_expected_line_is_a_miss(runs) -> None:
    def move(o: Observed) -> None:
        for record in o.ir["evidence"]:
            if record["path"].endswith("OrderController.java") and record.get("kind") == "span":
                record["line_start"], record["line_end"] = 1, 2

    result = scored(runs, "spring-boot", move)
    assert result["evidence"]["recall"] < 1 and result["evidence"]["missing"]


@pytest.mark.parametrize("damage, reason", [
    (lambda r: r.update(line_end=10_000, line_start=9_999), "span-out-of-range"),
    (lambda r: r.update(content_hash="0" * 64), "hash-mismatch"),
    (lambda r: r.update(path="does/not/exist.java"), "file-missing"),
    (lambda r: r.update(revision="not-the-snapshot-revision"), "revision-mismatch"),
    (lambda r: r.update(repository_id="unknown-repository"), "unknown-repository"),
])
def test_provenance_is_checked_against_the_files_on_disk(runs, damage, reason) -> None:
    def hurt(o: Observed) -> None:
        record = next(item for item in o.ir["evidence"] if item["kind"] == "span")
        damage(record)

    result = scored(runs, "spring-boot", hurt)
    assert result["provenance"]["resolution"] < 1
    assert [item["reason"] for item in result["provenance"]["unresolved"]] == [reason]
    assert "evidence-does-not-resolve-to-files" in result["failures"]


def test_a_dangling_evidence_reference_is_a_failure(runs) -> None:
    def dangle(o: Observed) -> None:
        o.ir["nodes"][0]["evidence_ids"].append("evidence:missing")

    result = scored(runs, "postgres", dangle)
    assert result["provenance"]["reference_resolution"] < 1
    assert "evidence-reference-unresolved" in result["failures"]


def test_candidate_errors_move_the_cross_repository_metrics(runs) -> None:
    dropped = scored(runs, "smartcart-correlation", lambda o: o.ir["candidates"].pop())
    assert dropped["candidates"]["recall"] < 1 and dropped["candidates"]["accuracy"] < 1 and dropped["candidates"]["missing"]

    def forbidden(o: Observed) -> None:
        delete = node(o.ir, "http_request", "DELETE /orders")
        target = node(o.ir, "endpoint", "GET /orders", "backend")
        o.ir["candidates"].append({**copy.deepcopy(o.ir["candidates"][0]), "id": "candidate:x", "source": delete["id"], "target": target["id"]})

    result = scored(runs, "smartcart-correlation", forbidden)
    assert result["candidates"]["precision"] < 1 and result["candidates"]["forbidden_present"]
    assert "forbidden-candidate-present" in result["failures"]

    def confidence(o: Observed) -> None:
        o.ir["candidates"][0]["confidence"] = 0.31

    assert scored(runs, "smartcart-correlation", confidence)["candidates"]["confidence_mismatches"]


def test_a_candidate_presented_as_authoritative_is_a_failure(runs) -> None:
    def promote(o: Observed) -> None:
        o.ir["candidates"][0]["classification"] = "observed"
        o.ir["candidates"][0]["confidence"] = 1.0

    result = scored(runs, "smartcart-correlation", promote)
    assert result["candidates"]["non_authoritative_violations"] == 1
    assert "authoritative-candidate" in result["failures"]


def test_an_observed_cross_repository_relationship_is_a_failure(runs) -> None:
    def link(o: Observed) -> None:
        request = node(o.ir, "http_request", "GET /orders")
        endpoint = node(o.ir, "endpoint", "GET /orders", "backend")
        o.ir["relationships"].append({"id": "relationship:x", "type": "calls", "source": request["id"], "target": endpoint["id"],
                                       "classification": "observed", "confidence": 1.0, "evidence_ids": []})

    result = scored(runs, "smartcart-correlation", link)
    assert result["candidates"]["cross_repository_relationships"] == 1
    assert "observed-cross-repository-relationship" in result["failures"]


def test_limitation_errors_move_recall_and_precision(runs) -> None:
    dropped = scored(runs, "postgres", lambda o: o.ir["limitations"].pop())
    assert dropped["limitations"]["recall"] < 1 and dropped["limitations"]["missing"]

    def add(o: Observed) -> None:
        o.ir["limitations"].append({**copy.deepcopy(o.ir["limitations"][0]), "id": "limitation:x", "kind": "reflection"})

    added = scored(runs, "postgres", add)
    assert added["limitations"]["precision"] < 1 and added["limitations"]["unexpected"]


def test_an_unsupported_deterministic_claim_is_a_failure(runs) -> None:
    def hollow(o: Observed) -> None:
        o.ir["claims"][0]["evidence_ids"] = []

    result = scored(runs, "postgres", hollow)
    assert result["claims"]["unsupported"] == 1 and result["claims"]["unsupported_rate"] > 0
    assert "unsupported-deterministic-claim" in result["failures"]


def test_diagram_problems_are_detected(runs) -> None:
    def corrupt(o: Observed) -> None:
        o.artifacts["erd.mmd"] = o.artifacts["erd.mmd"].replace("erDiagram", "erDiagram\n    this is not valid mermaid", 1)

    corrupted = scored(runs, "postgres", corrupt)
    assert corrupted["diagrams"]["validity"] == 0.5 and corrupted["diagrams"]["invalid"][0]["artifact"] == "erd.mmd"
    assert "invalid-diagram" in corrupted["failures"]

    removed = scored(runs, "postgres", lambda o: o.artifacts.pop("erd.mmd"))
    assert removed["diagrams"]["missing"] == ["erd.mmd"] and "expected-diagram-missing" in removed["failures"]

    def extra_entity(o: Observed) -> None:
        o.artifacts["erd.mmd"] += "    extra {\n    }\n"

    assert "erd-counts-differ" in scored(runs, "postgres", extra_entity)["failures"]


def test_unstable_output_is_a_failure(runs) -> None:
    result = scored(runs, "postgres", stability={"ir_identical": True, "artifacts_identical": False})
    assert "unstable-output" in result["failures"]


def test_an_unsupported_ai_claim_that_is_accepted_raises_the_unsupported_rate(runs) -> None:
    def add_bad_claim(case: dict) -> None:
        case["ai"]["labels"]["claims"].append({
            "text": "The orders endpoint processes payments.", "classification": "inferred",
            "subject": "endpoint", "evidence": "endpoint", "expected": "inferred", "supported": False,
        })

    result = scored(runs, "smartcart-correlation", edit_case=add_bad_claim)
    comparison = result["ai"]["comparison"]
    assert result["ai"]["reviewed_unsupported_inferred"] == 1
    assert comparison["unsupported_rate_delta"] > 0 and comparison["increases_unsupported_rate"] is True
    assert "ai-increases-unsupported-rate" in result["failures"]
    assert result["ai"]["expectation_mismatches"] and "ai-replay-differs-from-review" in result["failures"]


def test_ai_replay_that_disagrees_with_the_review_is_a_failure(runs) -> None:
    def mislabel(case: dict) -> None:
        case["ai"]["labels"]["claims"][1]["expected"] = "inferred"  # "Consider adding an API gateway" must stay a proposal

    result = scored(runs, "smartcart-correlation", edit_case=mislabel)
    assert result["ai"]["wrong_outcomes"] and "ai-replay-differs-from-review" in result["failures"]


def test_the_ai_comparison_needs_candidates_to_replay_against(runs) -> None:
    def clear(o: Observed) -> None:
        o.ir["candidates"] = []

    with pytest.raises(GoldenError, match="cross-repository candidates"):
        scored(runs, "smartcart-correlation", clear)


# ------------------------------------------------------------------ definitions
def test_zero_denominators_are_null_never_one(runs) -> None:
    result = scored(runs, "smartcart-correlation")
    assert result["limitations"]["expected"] == 0 and result["limitations"]["recall"] is None
    assert result["limitations"]["precision"] is None
    spring = scored(runs, "spring-boot")
    assert spring["candidates"]["expected"] is None and spring["candidates"]["accuracy"] is None
    assert spring["ai"] is None

    def nothing(case: dict) -> None:
        case["nodes"], case["relationships"] = [], []

    empty = scored(runs, "postgres", edit_case=nothing)
    assert empty["nodes"]["recall"] is None and empty["nodes"]["precision"] is None


def test_candidate_accuracy_counts_extra_and_missing_candidates_alike(runs) -> None:
    result = scored(runs, "smartcart-correlation")
    assert result["candidates"]["accuracy"] == 1.0

    def extra(o: Observed) -> None:
        request = node(o.ir, "http_request", "GET https://other.example.invalid/api/orders")
        target = node(o.ir, "endpoint", "GET /orders", "backend")
        o.ir["candidates"].append({**copy.deepcopy(o.ir["candidates"][0]), "id": "candidate:y", "source": request["id"], "target": target["id"]})

    assert scored(runs, "smartcart-correlation", extra)["candidates"]["accuracy"] == round(5 / 6, 4)


def test_selectors_must_identify_exactly_one_node(runs) -> None:
    def ambiguous(case: dict) -> None:
        case["nodes"].append({"type": "endpoint", "name": "GET /orders"})  # backend and competing both match

    with pytest.raises(GoldenError, match="matches 2 nodes"):
        scored(runs, "smartcart-correlation", edit_case=ambiguous)

    def overlapping(case: dict) -> None:
        case["nodes"].append({"type": "endpoint", "repository": "backend", "name": "GET /orders"})

    with pytest.raises(GoldenError, match="overlap"):
        scored(runs, "smartcart-correlation", edit_case=overlapping)

    def attributes(case: dict) -> None:
        case["nodes"] = [item for item in case["nodes"] if item["name"] != "GET /api/orders"] + [
            {"type": "http_request", "repository": "frontend", "name": "GET /api/orders", "attributes": {"client": "fetch"}}
        ]

    assert scored(runs, "smartcart-correlation", edit_case=attributes)["nodes"]["recall"] == 1.0


def test_malformed_cases_are_rejected(tmp_path) -> None:
    (tmp_path / "golden.json").write_text("not json", encoding="utf-8")
    with pytest.raises(GoldenError, match="Unable to read"):
        load_case(tmp_path / "golden.json")
    (tmp_path / "golden.json").write_text(json.dumps({"schema_version": "9", "name": "x", "repositories": []}), encoding="utf-8")
    with pytest.raises(GoldenError, match="schema_version"):
        load_case(tmp_path / "golden.json")
    (tmp_path / "golden.json").write_text(json.dumps({"schema_version": "1.0", "name": "x"}), encoding="utf-8")
    with pytest.raises(GoldenError, match="missing 'repositories'"):
        load_case(tmp_path / "golden.json")
    (tmp_path / "golden.json").write_text(
        json.dumps({"schema_version": "1.0", "name": "x", "repositories": [{"id": "a", "path": "nowhere"}]}), encoding="utf-8"
    )
    with pytest.raises(GoldenError, match="not a directory"):
        load_case(tmp_path / "golden.json")


def test_reports_are_free_of_hashes_and_machine_paths(report) -> None:
    text = json.dumps(report)
    assert not re.search(r"\b[0-9a-f]{40,64}\b", text)
    assert not re.search(r"[A-Za-z]:\\\\|/tmp/|/home/|/Users/", text)


def test_diff_reports_names_where_reports_differ() -> None:
    assert diff_reports({"a": {"b": 1, "c": [1, 2]}}, {"a": {"b": 1, "c": [1, 2]}}) == []
    assert diff_reports({"a": {"b": 1}}, {"a": {"b": 2}}) == ["a.b: 1 != 2"]
    assert diff_reports({"a": [1]}, {"a": [1, 2]}) == ["a: length 1 != 2"]
    assert diff_reports({"a": 1}, {}) == ["a: 1 != None"]


# ------------------------------------------------------------------ runner
def test_runner_passes_against_the_baseline_and_writes_the_report(tmp_path, capsys) -> None:
    out = tmp_path / "report.json"
    code = main([str(CASES_DIR), "--baseline", str(BASELINE), "--output", str(out)])
    captured = capsys.readouterr()
    assert code == 0 and captured.err == ""
    assert json.loads(captured.out) == json.loads(out.read_text(encoding="utf-8")) == json.loads(BASELINE.read_text(encoding="utf-8"))


def test_runner_fails_when_the_report_differs_from_the_baseline(tmp_path, capsys) -> None:
    changed = json.loads(BASELINE.read_text(encoding="utf-8"))
    changed["cases"][0]["nodes"]["recall"] = 0.5
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(changed), encoding="utf-8")
    code = main([str(CASES_DIR), "--baseline", str(baseline)])
    assert code == 1
    assert "baseline differs: cases[0].nodes.recall: 0.5 != 1.0" in capsys.readouterr().err


def test_runner_summary_is_a_markdown_table(capsys) -> None:
    assert main([str(CASES_DIR), "--summary"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("## Golden-fixture evaluation") and "| spring-boot | 1 | 1 | 1 | 1 | 1 |" in text
    assert "n/a" in text


def test_runner_reports_usage_errors_with_exit_code_2(tmp_path, capsys) -> None:
    assert main([str(tmp_path)]) == 2
    assert "No golden cases found" in capsys.readouterr().err
    assert main([str(CASES_DIR), "--baseline", str(tmp_path / "missing.json")]) == 2
    assert "Unable to read baseline" in capsys.readouterr().err


def test_runner_fails_on_an_invariant_failure(monkeypatch, capsys) -> None:
    real = golden.evaluate_cases

    def broken(directory: Path) -> dict:
        report = real(directory)
        report["failures"] = 1
        return report

    monkeypatch.setattr(golden, "evaluate_cases", broken)
    assert main([str(CASES_DIR)]) == 1
