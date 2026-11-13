# ADR-008: Use Full-Project Regeneration Before Incremental Impact Analysis

- Date: 2026-11-13
- Status: Accepted

## Context

Incremental dependency impact analysis adds complexity before the IR and artifact ownership contracts are proven.

## Alternatives

- Implement fine-grained incremental regeneration in the first release.
- Always regenerate and always publish the result.
- Regenerate the full project, then suppress publication when the artifact result is unchanged.

## Decision

RepoDocs performs full-project regeneration for the MVP and uses manifest/hash comparison to detect no-op output.

## Consequences

Runs may do more analysis than necessary, but output behavior is simpler and safer. Incremental analysis can be added without changing publication semantics later.
