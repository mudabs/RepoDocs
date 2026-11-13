# ADR-010: Make CLI Output Versioned and Confirmation Failures Explicit

- Date: 2026-11-13
- Status: Accepted

## Context

Agents and CI need stable machine-readable behavior, especially when ambiguous relationships cannot be confirmed interactively.

## Alternatives

- Human-readable output with best-effort exit codes.
- Auto-confirm unresolved relationships in CI.
- Versioned JSON output, explicit exit codes, and failure for unresolved required confirmations.

## Decision

CLI operations expose versioned JSON output and documented exit codes. Noninteractive CI fails when a required relationship confirmation remains unresolved.

## Consequences

Command and configuration details remain open, but automation semantics are stable and safe by default.
