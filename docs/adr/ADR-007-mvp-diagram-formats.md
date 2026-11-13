# ADR-007: Start with Mermaid Architecture and ERD Diagrams

- Date: 2026-11-13
- Status: Accepted

## Context

The MVP needs editable, deterministic diagrams without taking on the maintenance cost of the complete UML and rendering matrix.

## Alternatives

- Support Mermaid and PlantUML across many diagram types immediately.
- Produce rendered images only.
- Start with Mermaid architecture/component and ERD diagrams, with API documentation as Markdown.

## Decision

Mermaid is the first diagram source format. The MVP generates architecture/component and ERD diagrams when evidence supports them. API documentation is Markdown. PlantUML remains an extension point.

## Consequences

The generator contract must be type-specific and deterministic. Sequence, activity, deployment, and advanced UML diagrams remain out of scope until their evidence and rendering requirements are validated.
