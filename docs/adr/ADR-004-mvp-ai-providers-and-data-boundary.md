# ADR-004: Use One BYO Cloud Provider and Ollama for MVP AI

- Date: 2026-11-13
- Status: Accepted

## Context

AI interpretation is central to RepoDocs, but the CLI and GitHub Action must remain usable without a mandatory service or provider account.

## Alternatives

- Deterministic analysis only.
- RepoDocs-managed AI as the primary MVP path.
- One cloud BYO provider plus Ollama behind a provider-neutral adapter.

## Decision

The MVP supports one cloud provider supplied by the user and Ollama. External repository content requires explicit consent and redaction. Local-only mode must not send repository content externally. Managed RepoDocs AI is deferred.

## Consequences

AI requests must use curated evidence bundles, schema-constrained outputs, and provider/model provenance. Provider-specific behavior must not leak into the core engine.
