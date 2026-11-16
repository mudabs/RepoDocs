"""Fact-level measurement of what validated AI claims add (ADR-011, ADR-021)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .bundle import build_bundle
from .claims import CLASSIFICATION_INFERRED, CLASSIFICATION_PROPOSED, validate_claims


def contribution(ir: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """Count what the AI added beside the deterministic claims.

    `unsupported_accepted` counts claims that fail structural revalidation:
    references, subject/evidence binding, and confirmation state. It must be zero.
    It is not a semantic truth score; reviewed golden cases separately label
    whether the actual wording is supported.
    """

    bundle = build_bundle(ir)
    accepted = document["accepted"]
    inferred = [item for item in accepted if item["classification"] == CLASSIFICATION_INFERRED]
    unsupported = []
    for item in accepted:
        checked, rejected = validate_claims({"claims": [item]}, bundle)
        if rejected or not checked or checked[0]["classification"] != item["classification"]:
            unsupported.append(item)
    return {
        "deterministic_claims": len(ir["claims"]),
        "ai_inferred_claims": len(inferred),
        "ai_proposed_claims": sum(1 for item in accepted if item["classification"] == CLASSIFICATION_PROPOSED),
        "ai_rejected_claims": len(document["rejected"]),
        "ai_rejected_by_reason": dict(sorted(Counter(item["reason"] for item in document["rejected"]).items())),
        "subjects_interpreted": len({key for item in inferred for key in item["subject_ids"]}),
        "evidence_cited": len({key for item in inferred for key in item["evidence_ids"]}),
        "unsupported_accepted": len(unsupported),
    }
