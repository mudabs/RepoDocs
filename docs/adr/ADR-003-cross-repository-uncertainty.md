# ADR-003: Keep Ambiguous Cross-Repository Links as Candidates

- Date: 2026-11-13
- Status: Accepted

## Context

Routes, clients, URLs, schemas, and deployment configuration can suggest a relationship between repositories without proving it.

## Alternatives

- Automatically promote high-scoring matches to authoritative links.
- Never represent cross-repository matches.
- Store candidates and require explicit confirmation for authoritative use.

## Decision

Correlation produces candidate relationships. Only direct evidence or an explicit user confirmation can make a relationship authoritative. Candidates, confirmations, rejections, and stale states are persisted with the relevant repository revisions.

## Consequences

Documentation must distinguish candidates from authoritative links. CI fails when a required confirmation is unresolved rather than silently publishing an inferred relationship as fact.
