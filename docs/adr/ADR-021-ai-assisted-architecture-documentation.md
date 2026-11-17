# ADR-021: AI-Assisted Architecture Documentation and Contribution Checks

> Amended by ADR-026: the rule that AI cannot change diagrams applies to the deterministic documents and the IR. A separate, validated, inferred architecture view may drive separate diagram files.

- Status: Accepted
- Date: 2026-11-15

## Context

Issue #20 uses the evidence bundles, provider adapters, and structured claims
from ADR-020 to produce architecture explanations. The deterministic IR and
documentation must remain complete and authoritative independently of model
availability. Claim citations must resolve, uncertain relationships must not
be promoted, and measured AI contribution must not masquerade as demonstrated
factual accuracy.

## Decision

### Optional artifact, no new authority

- `ai-interpret` continues to store claims beside the IR, never inside it.
  `generate` performs no inference or provider call. When stored claims are
  current and valid, it additionally renders `architecture-ai.md`.
- `architecture.md` and `architecture.mmd` remain byte-identical for the same
  IR whether AI claims exist or not. The optional document cannot add nodes,
  edges, candidate decisions, or observed claims. A model can interpret an
  explicitly confirmed candidate but cannot confirm one.
- The AI document separates inferred interpretations and proposals, cites each
  claim's subjects/evidence, counts rejected outputs without quoting their
  text, and records provider, serving model, IR hash, and evidence-bundle hash.
  Manifest entries label it `repodocs-ai`; deterministic artifacts retain
  `repodocs-deterministic`.
- Model text is rendered as escaped prose, not executable HTML, Markdown
  links, headings, or instructions. Rendering uses redacted bundle names and
  evidence locations. Redaction remains pattern-based, not a completeness
  guarantee.

### Revalidation and deterministic fallback

- Before rendering, check the claims envelope and provider provenance, source
  IR hash, current bundle hash, and all accepted claims. IDs, classification,
  references, normalization, and ordering must survive revalidation unchanged.
- An inferred claim must name subjects, cite evidence associated with those
  subjects, and not cite unrelated evidence. A claim spanning evidence from
  multiple repositories requires an explicitly named confirmed candidate with
  evidence from both sides. Otherwise it is only a proposal. Naming a plain
  frontend node or two endpoints cannot bypass an unconfirmed candidate.
- Repository scope is resolved from the subjects' complete definitions,
  including relationship/candidate endpoints, not from the model's selected
  citations. Each subject needs bound evidence from every repository in its
  scope. A multi-repository inference must stay within one explicitly named,
  fully cited confirmed candidate's repository pair; it cannot add unrelated
  third-repository subjects, even when those subjects also have valid citations.
- These are structural grounding checks. Valid IDs and subject bindings do
  **not** prove arbitrary model wording. Interpretations remain labeled as
  model inference, not observed facts; source reviewers must check wording.
- Missing claims mean deterministic-only output. Stale, malformed, or invalid
  optional claims produce a warning and deterministic-only output rather than
  making deterministic generation depend on a provider or a usable AI file.
- A previously generated optional file is retired only after manifest ownership
  and content hashes pass preflight. Unowned or edited files are never
  overwritten or deleted. Validation rejects an AI document that no longer
  matches its current claims, provenance, or generator label.
- Changing source revisions or a confirmation/rejection changes the IR hash,
  making the old interpretation stale. Claims are refreshed explicitly with
  `ai-interpret`, not regenerated automatically during `generate`.
- Provider errors still use exit code 7; they leave the deterministic workflow
  usable. Required-confirmation failures still use exit code 5. AI output
  cannot override either a candidate decision or an ownership failure.

### Evaluation

- `ai-interpret` returns a `contribution` object: deterministic claim count,
  inferred/proposed/rejected model claim counts, rejection reasons, subjects
  interpreted, evidence cited, and structurally unsupported accepted count.
  This last count must be zero; it is not a semantic entailment score.
- A reviewed golden fixture uses a deterministic fake provider, not paid or
  live-model calls. Cases include a supported endpoint explanation,
  recommendations, unconfirmed links, attempts to bypass candidate status,
  unrelated but valid citations, invented evidence/subjects, and an attempted
  observed claim.
- Tests assert the authored classification for each case and separately count
  reviewed unsupported cases promoted to inference. The current fixture yields
  1 inferred explanation, 4 proposals, 3 rejections, and 0 reviewed unsupported
  inferences, without changing the deterministic IR or its documentation.
- These counts establish regression behavior for the fixture only. They do
  not demonstrate general real-model accuracy, semantic recall, or production
  factuality. A live-provider quality study remains future evaluation work.

## Consequences

Users get optional model explanations with explicit uncertainty, citations, and
provenance while retaining a complete no-key, offline deterministic workflow.
Cached current claims can be rendered without contacting their original
provider. Disabling a provider stops new inference; removing the claims file
and regenerating retires the optional document. Generated documents and their
manifest still require normal human review before publication.

Schemas remain at 1.0 while unreleased, following ADR-014. Manifest generator
labels distinguish model-derived artifacts; no model text is merged into the
System IR. This work does not implement other diagram types, automatic
publication, automatic relationship confirmation, or a universal semantic
truth validator.
