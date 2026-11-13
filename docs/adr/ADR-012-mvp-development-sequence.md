# ADR-012: Build Contracts, Then a Slice, Then the Multi-Repository MVP

- Date: 2026-11-13
- Status: Accepted

## Context

The final MVP must include AI-enhanced multi-repository understanding, but implementing every adapter and interface at once would create avoidable schedule risk.

## Alternatives

- Implement all target ecosystems and interfaces in parallel.
- Build only a deterministic single-repository product.
- Define contracts, build one Spring Boot end-to-end slice, then add React, correlation, AI, and publication.

## Decision

Development proceeds in that order. The single-repository slice is an engineering milestone, not the final product boundary. The final MVP preserves AI-enhanced multi-repository SmartCart analysis.

## Consequences

Phase 1 must expose the contracts needed by later AI and correlation work. Scope reductions apply to framework depth and diagram breadth, not to the final multi-repository and AI goals.
