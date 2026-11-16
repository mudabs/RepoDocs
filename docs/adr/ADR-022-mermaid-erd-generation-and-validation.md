# ADR-022: Typed Mermaid Entity-Relationship Diagram Generation and Validation

- Date: 2026-11-16
- Status: Accepted

## Context

Issue #21 calls for deterministic Mermaid Entity-Relationship Diagram (ERD) generation and rendering validation from static PostgreSQL schema facts. ADR-007 established Mermaid as the diagram source format for architecture and ERD views, and ADR-017 introduced database extraction (tables, columns, primary keys, and foreign keys) into the versioned System IR.

The diagram pipeline must construct typed relational models rather than ad-hoc string templates, validate Mermaid syntax against renderer constraints without mandatory external npm binaries, report missing keys and unsupported relationships honestly, and preserve source evidence and generated documentation ownership rules.

## Alternatives

- Render ERDs with a headless browser and `@mermaid-js/mermaid-cli` in all environments. This would introduce Node.js and headless browser dependencies into the CLI and GitHub Actions runners, violating the local/offline no-mandatory-backend constraint.
- Embed ERD blocks directly inside `architecture.md` only. This would lose the editable `.mmd` artifact requirement and prevent standalone diagram consumption.
- Guess relationship cardinalities without inspecting column nullability or uniqueness constraints.
- Promote unextracted or ambiguous foreign keys into guessed diagram entities.

## Decision

### Typed ERD Model and Deterministic Generation

- An intermediate typed model (`ERDModel`, `ERDEntity`, `ERDAttribute`, `ERDRelationship`) is constructed from the System IR `table` nodes and `references` relationships.
- Diagram entities use sanitized alphanumeric identifiers matching Mermaid lexical rules (`[A-Za-z_][A-Za-z0-9_]*`), preserving schema-qualified and display names in Mermaid bracketed alias labels (`entity_id["schema.table"]`).
- Column types are normalized into valid single-token Mermaid attribute words, standardizing multi-word SQL types (such as `timestamp with time zone` to `timestamptz` and `character varying` to `varchar`) and removing internal whitespace in parameterized types (such as `numeric(12, 2)` to `numeric(12,2)`).
- Keys are assigned based on declared primary keys (`PK`), single-column unique constraints (`UK`), and foreign key columns (`FK`), supporting composite markers (`PK, FK` or `UK, FK`).
- Relationships represent foreign keys with crow's-foot cardinality:
  - Parent side is mandatory (`||`) when foreign key columns are `NOT NULL`, and optional (`|o`) when nullable.
  - Child side is at most one (`o|`) when foreign key columns are unique in the child table, and zero or more (`o{`) otherwise.
  - Identifying line style (`--`) is used when the child primary key contains the foreign key columns; non-identifying (`..`) is used otherwise.
  - The relationship label records the foreign key column names (or constraint name).
- When table facts exist, `erd.mmd` is generated deterministically alongside `architecture.md` and `architecture.mmd`, categorized under `repodocs-deterministic`. When no table facts exist in the IR, `erd.mmd` is omitted. Retired ERD artifacts are safely removed upon regeneration when table evidence is absent.

### Documented Renderer-Compatible Validation

- `validate_mermaid` dispatches on the diagram header: `flowchart TD` for architecture diagrams and `erDiagram` for entity-relationship diagrams.
- ERD validation verifies diagram header, comment syntax (`%%`), entity block delimiters (`{ ... }`), attribute token structure (type, name, keys, optional comments), relationship line syntax, and entity reference integrity. Any relationship targeting an undeclared entity fails validation.
- The built-in validator operates natively in Python without requiring Node.js or browser installations, while remaining fully compatible with official Mermaid rendering rules.

### Diagnostics and Evidence Reporting

- Missing primary keys and unsupported relationships (such as foreign keys referencing tables not defined in the scanned schema) are reported as structured comments in `erd.mmd` and in a `Schema diagnostics` section in `architecture.md`.
- Evidence IDs and provenance continue to trace back to the SQL migrations that defined or altered each table.
- Generated `erd.mmd` files are tracked in `artifact-manifest.json`, protected against unexpected manual edits, and validated with exit code 0 when valid.

## Consequences

- PostgreSQL repositories automatically gain editable, renderer-validated ER diagrams (`erd.mmd`) in their generated documentation directory.
- Projects without database facts remain unaffected and produce no extraneous diagram artifacts.
- The manifest schema remains at version 1.0 because `erd.mmd` uses the existing `repodocs-deterministic` generator classification.
