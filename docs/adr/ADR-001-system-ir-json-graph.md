# ADR-001: Represent the System IR as a Versioned JSON Graph

- Date: 2026-11-13
- Status: Accepted

## Context

The CLI, analyzers, correlation, AI, generators, validators, and future hosted UI need a shared intermediate contract that works without a persistent backend.

## Alternatives

- Backend-first graph storage.
- Process-local in-memory structures only.
- A versioned serialized graph with a local canonical representation.

## Decision

The System IR is a versioned JSON graph containing nodes, edges, evidence, claims, overrides, and run metadata. The canonical form is stored locally and can later be persisted by hosted services without changing its meaning.

## Consequences

The schema, canonical serialization, migration policy, and extension mechanism must be defined before implementation. JSON makes artifacts inspectable and portable, while large projects may later need indexed derived views.
