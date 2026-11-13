# ADR-009: Publish System Documentation through One Coordination Repository

- Date: 2026-11-13
- Status: Accepted

## Context

System documentation spans multiple source repositories, while GitHub Actions need one clear location for project configuration and reviewable documentation changes.

## Alternatives

- Publish system documentation into whichever source repository changed.
- Maintain a separate backend for generated project state.
- Use one coordination repository for system configuration, generated system docs, and documentation PRs.

## Decision

The MVP uses one coordination repository for system-level documentation and documentation pull requests. Source repositories remain inputs and may receive repository-local documentation through the local workflow.

## Consequences

The project configuration, credentials, triggers, concurrency rules, and ownership of system artifacts must be defined around the coordination repository.
