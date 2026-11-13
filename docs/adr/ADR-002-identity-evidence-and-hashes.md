# ADR-002: Separate Semantic Identity, Evidence, and Content Hashes

- Date: 2026-11-13
- Status: Accepted

## Context

Entity identity must remain useful across scans even when files move, while evidence must point to the exact source revision and content used for a claim.

## Alternatives

- Use file paths and line numbers as entity IDs.
- Use content hashes as the only identity.
- Use semantic IDs with separate evidence references and content hashes.

## Decision

RepoDocs uses semantic IDs for entities and relationships. Source evidence records repository revision, path, symbol or line range where available, analyzer, and content hash independently.

## Consequences

Identity resolution and rename handling become explicit responsibilities. Evidence remains auditable and can detect stale or changed source content without changing semantic identity automatically.
