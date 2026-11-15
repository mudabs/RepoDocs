"""Confirmation and rejection decisions for cross-repository candidates (ADR-019).

Decisions are human input, so they live in a reviewable project file next to
``repodocs.json`` instead of in scan state. Each decision records the evidence
and the repository revisions it was made against; when that evidence changes,
the decision is stale and the candidate needs a new decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import write_json
from .errors import ConfigError
from .snapshot import REVISION_KIND_COMMIT, REVISION_KIND_CONTENT

REVISION_KINDS = (REVISION_KIND_COMMIT, REVISION_KIND_CONTENT)


CONFIRMATIONS_FILENAME = "repodocs.confirmations.json"
CONFIRMATIONS_SCHEMA_VERSION = "1.0"

DECISION_CONFIRMED = "confirmed"
DECISION_REJECTED = "rejected"
DECISIONS = (DECISION_CONFIRMED, DECISION_REJECTED)

STATUS_CANDIDATE = "candidate"
STATUS_STALE = "stale"
STATUSES = (STATUS_CANDIDATE, DECISION_CONFIRMED, DECISION_REJECTED, STATUS_STALE)
UNRESOLVED_STATUSES = (STATUS_CANDIDATE, STATUS_STALE)


def confirmations_path(root: Path) -> Path:
    return root / CONFIRMATIONS_FILENAME


def _valid_snapshots(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(
        isinstance(repo, str)
        and isinstance(item, dict)
        and isinstance(item.get("revision"), str)
        and bool(item["revision"])
        and item.get("revision_kind") in REVISION_KINDS
        for repo, item in value.items()
    )


def load_decisions(root: Path) -> dict[str, dict[str, Any]]:
    """Read recorded decisions keyed by candidate ID; a missing file means none."""

    path = confirmations_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to read confirmations: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != CONFIRMATIONS_SCHEMA_VERSION:
        raise ConfigError(f"Unsupported confirmations file: {path}; expected schema {CONFIRMATIONS_SCHEMA_VERSION}")
    records = data.get("decisions")
    if not isinstance(records, list):
        raise ConfigError(f"Confirmations file must contain a decisions array: {path}")
    decisions: dict[str, dict[str, Any]] = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("candidate_id"), str)
            or record.get("decision") not in DECISIONS
            or not isinstance(record.get("source"), str)
            or not isinstance(record.get("target"), str)
            or not isinstance(record.get("evidence_ids"), list)
            or any(not isinstance(item, str) for item in record["evidence_ids"])
            or len(set(record["evidence_ids"])) != len(record["evidence_ids"])
            or not _valid_snapshots(record.get("snapshots"))
            or not isinstance(record.get("note", ""), str)
        ):
            raise ConfigError(f"Invalid decision record in {path}")
        if record["candidate_id"] in decisions:
            raise ConfigError(f"Duplicate decision for {record['candidate_id']} in {path}")
        decisions[record["candidate_id"]] = record
    return decisions


def save_decisions(root: Path, decisions: dict[str, dict[str, Any]]) -> None:
    write_json(
        confirmations_path(root),
        {
            "schema_version": CONFIRMATIONS_SCHEMA_VERSION,
            "decisions": [decisions[key] for key in sorted(decisions)],
        },
    )


def record_decision(
    ir: dict[str, Any], decisions: dict[str, dict[str, Any]], candidate_id: str, decision: str, note: str | None
) -> None:
    """Bind a decision to the candidate's current evidence and snapshot revisions."""

    candidate = next((item for item in ir["candidates"] if item["id"] == candidate_id), None)
    if candidate is None:
        raise ConfigError(f"Unknown candidate ID: {candidate_id}; run scan and list candidates first")
    record: dict[str, Any] = {
        "candidate_id": candidate_id,
        "decision": decision,
        "source": candidate["source"],
        "target": candidate["target"],
        "evidence_ids": sorted(candidate["evidence_ids"]),
        "snapshots": candidate["snapshots"],
    }
    if note:
        record["note"] = note
    decisions[candidate_id] = record


def apply_decisions(ir: dict[str, Any], decisions: dict[str, dict[str, Any]]) -> None:
    """Set each candidate's status from the recorded decisions.

    A decision holds only while it is bound to this candidate: the same source and target
    nodes, the same participating repositories, and the evidence it was made against.
    Anything else makes the candidate stale, never silently confirmed or rejected.
    """

    for candidate in ir["candidates"]:
        candidate.pop("confirmation", None)
        candidate["status"] = STATUS_CANDIDATE
        record = decisions.get(candidate["id"])
        if record is None:
            continue
        fresh = (
            record["source"] == candidate["source"]
            and record["target"] == candidate["target"]
            and set(record["snapshots"]) == set(candidate["snapshots"])
            and sorted(record["evidence_ids"]) == sorted(candidate["evidence_ids"])
        )
        candidate["status"] = record["decision"] if fresh else STATUS_STALE
        confirmation: dict[str, Any] = {"decision": record["decision"], "snapshots": record["snapshots"]}
        if record.get("note"):
            confirmation["note"] = record["note"]
        candidate["confirmation"] = confirmation


def status_counts(ir: dict[str, Any]) -> dict[str, int]:
    return {status: sum(1 for item in ir["candidates"] if item["status"] == status) for status in STATUSES}


def unresolved_candidates(ir: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in ir["candidates"] if item["status"] in UNRESOLVED_STATUSES]


def orphaned_decisions(ir: dict[str, Any], decisions: dict[str, dict[str, Any]]) -> list[str]:
    """Decisions whose candidate no longer exists in the current scan."""

    present = {item["id"] for item in ir["candidates"]}
    return sorted(key for key in decisions if key not in present)
