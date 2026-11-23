"""AI-assisted architecture documentation rendered from validated claims (ADR-021).

The AI document is a separate, optional artifact. It is produced only when the
stored claims were made against the current System IR and still validate against
it; otherwise it is left out and the deterministic documentation stands alone.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ..generator import AI_ARTIFACT, _evidence_ref
from ..ir import ir_hash
from .bundle import BUNDLE_SCHEMA_VERSION, build_bundle, bundle_hash, redact_text
from .claims import CLAIMS_FILENAME, CLAIMS_SCHEMA_VERSION, CLASSIFICATION_INFERRED, CLASSIFICATION_PROPOSED, validate_claims


STATUS_ABSENT = "absent"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"
STATUS_CURRENT = "current"


def load_claims(internal_path: Path) -> dict[str, Any] | None:
    path = internal_path / CLAIMS_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # A broken optional input must not break deterministic generation. Its
        # content and parser diagnostics may contain secrets, so never echo them.
        return {}
    return data if isinstance(data, dict) else {}


def claims_status(ir: dict[str, Any], document: dict[str, Any] | None) -> str:
    """Whether stored claims may be rendered for this IR.

    `stale` means they were made against a different IR, for example before a source
    change or a confirmation decision. `invalid` means they do not survive validation
    against the current evidence, so a hand-edited file is never rendered.
    """

    if document is None:
        return STATUS_ABSENT
    if not isinstance(document, dict):
        return STATUS_INVALID
    provenance = document.get("provenance")
    accepted = document.get("accepted")
    if (
        document.get("schema_version") != CLAIMS_SCHEMA_VERSION
        or set(document) != {"schema_version", "provenance", "accepted", "rejected"}
        or not isinstance(provenance, dict)
        or set(provenance) != {
            "engine", "engine_version", "provider", "model", "requested_model",
            "external", "bundle_version", "bundle_hash", "source_ir_hash",
        }
        or not isinstance(accepted, list)
        or not isinstance(document.get("rejected"), list)
        or any(not isinstance(provenance.get(key), str) or not provenance[key] for key in (
            "engine_version", "provider", "model", "requested_model", "bundle_hash", "source_ir_hash"
        ))
        or provenance.get("engine") != "repodocs"
        or provenance.get("provider") not in ("anthropic", "ollama", "openai")
        or type(provenance.get("external")) is not bool
        or provenance.get("bundle_version") != BUNDLE_SCHEMA_VERSION
    ):
        return STATUS_INVALID
    if any(not isinstance(item, dict) or set(item) != {"text", "reason"}
           or not isinstance(item["text"], str) or item["reason"] not in (
               "malformed-output", "malformed-claim", "unknown-evidence", "unknown-subject"
           ) for item in document["rejected"]):
        return STATUS_INVALID
    if provenance["source_ir_hash"] != ir_hash(ir):
        return STATUS_STALE
    bundle = build_bundle(ir)
    if provenance["bundle_hash"] != bundle_hash(bundle):
        return STATUS_STALE
    revalidated, rejected = validate_claims({"claims": accepted}, bundle)
    if rejected or revalidated != accepted:
        return STATUS_INVALID
    return STATUS_CURRENT


def _prose(value: str) -> str:
    """Model text is inert prose, never HTML, Markdown links, or directives."""

    value = html.escape(redact_text(value)[0].replace("\n", " ").replace("\r", " "), quote=False)
    return re.sub(r"([\\`*_\[\]{}()#+.!|~])", r"\\\1", value)


def render_ai_markdown(ir: dict[str, Any], document: dict[str, Any]) -> str:
    """Render validated claims. Rejected model output is counted, never quoted."""

    provenance = document["provenance"]
    bundle = build_bundle(ir)
    evidence = {item["id"]: item for item in bundle["evidence"]}
    names = {node["id"]: f"{node['type']} {node['name']}" for node in bundle["nodes"]}
    names.update({item["id"]: f"candidate {item['id']} ({item['status']})" for item in ir.get("candidates", [])})
    names.update({edge["id"]: f"{edge['type']} relationship" for edge in ir["relationships"]})

    def section(title: str, intro: str, classification: str) -> list[str]:
        lines = ["", f"## {title}", "", intro, ""]
        claims = [item for item in document["accepted"] if item["classification"] == classification]
        if not claims:
            return lines + ["None."]
        for claim in claims:
            subjects = "; ".join(_prose(names.get(key, key)) for key in claim["subject_ids"]) or "none"
            refs = ", ".join(_evidence_ref(evidence, key) for key in claim["evidence_ids"]) or "none"
            lines.append(f"- {_prose(claim['text'])} Subjects: {subjects}. Evidence: {refs}. (`{claim['id']}`)")
        return lines

    where = "left this machine" if provenance.get("external") else "stayed on this machine"
    lines = [
        f"# {_prose(bundle['project']['name'])} architecture interpretation (AI-assisted)",
        "",
        f"This document was written by a model ({_prose(provenance['model'])} through the {_prose(provenance['provider'])} "
        f"provider) from a redacted evidence bundle that {where}. It is interpretation, not observed fact."
        " Every statement below passed validation against the System IR: the evidence and subjects it cites exist."
        " Validation does not prove that the evidence supports the wording, so read each statement with its evidence.",
        "",
        "The deterministic overview in `architecture.md` is complete without this file and contains no model output.",
    ]
    lines += section(
        "Evidence-backed interpretations",
        "Inferred from the cited evidence. These are not observed relationships and add nothing to the diagrams.",
        CLASSIFICATION_INFERRED,
    )
    lines += section(
        "Proposals",
        "Not established. These either cite no evidence or concern a cross-repository candidate that has not been confirmed.",
        CLASSIFICATION_PROPOSED,
    )
    reasons = Counter(item["reason"] for item in document["rejected"])
    lines += ["", "## Rejected model output", ""]
    if reasons:
        lines.append("The following model statements failed validation and are not shown:")
        lines.append("")
        lines += [f"- {count} rejected for `{reason}`" for reason, count in sorted(reasons.items())]
    else:
        lines.append("No model statements were rejected.")
    lines += [
        "",
        "## Provenance",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Provider | {_prose(provenance['provider'])} |",
        f"| Model | {_prose(provenance['model'])} |",
        f"| Evidence bundle | `{provenance['bundle_hash']}` |",
        f"| System IR | `{provenance['source_ir_hash']}` |",
        "",
    ]
    return "\n".join(lines)


def ai_artifact(internal_path: Path, ir: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """The claims status and, when current, the optional artifact to generate."""

    document = load_claims(internal_path)
    status = claims_status(ir, document)
    if status != STATUS_CURRENT:
        return status, {}
    return status, {AI_ARTIFACT: render_ai_markdown(ir, document)}
