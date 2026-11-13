# ADR-011: Evaluate RepoDocs with Golden Fixtures and Fact-Level Metrics

- Date: 2026-11-13
- Status: Accepted

## Context

Documentation appearance is not a sufficient measure of architectural accuracy or AI value.

## Alternatives

- Manual review only.
- Text similarity against reference documentation.
- Golden fixtures with precision/recall, provenance checks, stability checks, and deterministic-versus-AI comparisons.

## Decision

Representative fixture repositories define expected facts and relationships. Evaluation measures fact discovery, correlation accuracy, unsupported claims, evidence resolution, diagram validity, unchanged-scan stability, and AI contribution.

## Consequences

Fixture maintenance becomes part of the project. Human review remains useful for readability, but it cannot replace fact-level validation.
