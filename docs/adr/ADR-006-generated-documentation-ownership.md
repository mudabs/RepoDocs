# ADR-006: Protect Generated Documentation with Dedicated Paths and a Manifest

- Date: 2026-11-13
- Status: Accepted

## Context

Generated documentation must be repeatable without overwriting user-authored files or hiding manual changes.

## Alternatives

- Overwrite generated-looking files unconditionally.
- Generate inline sections inside arbitrary human-authored documents.
- Use dedicated generated paths, an artifact manifest, and fail on unexpected edits.

## Decision

Generated artifacts live in dedicated paths and are tracked by a manifest containing ownership, hashes, source revisions, generator versions, and dependencies. Unexpected edits fail validation and are not overwritten.

## Consequences

The first version favors safety and clear ownership over flexible mixed-authorship documents. Ownership transfer and inline generated sections remain future extensions.
