# ADR-022: Typed Mermaid Entity-Relationship Diagram Generation and Validation

- Date: 2026-11-16
- Status: Accepted

## Context

Issue #21 calls for deterministic Mermaid Entity-Relationship Diagram (ERD) generation and rendering validation from static PostgreSQL schema facts. ADR-007 established Mermaid as the diagram source format for architecture and ERD views, and ADR-017 introduced database extraction (tables, columns, primary keys, and foreign keys) into the versioned System IR.

The diagram pipeline must construct typed relational models rather than ad-hoc string templates, validate Mermaid syntax against renderer constraints without mandatory external npm binaries, report missing keys and unsupported relationships honestly without cross-repository suppression, and preserve source evidence and generated documentation ownership rules.

## Alternatives

- Render ERDs with a headless browser and `@mermaid-js/mermaid-cli` in all environments. This would introduce Node.js and headless browser dependencies into the CLI and GitHub Actions runners, violating the local/offline no-mandatory-backend constraint.
- Embed ERD blocks directly inside `architecture.md` only. This would lose the editable `.mmd` artifact requirement and prevent standalone diagram consumption.
- Allow commas in Mermaid attribute types. Commas are outside the documented Mermaid attribute-type grammar and break parser compatibility across Mermaid renderer versions.
- Resolve foreign-key targets globally across all repositories. Relational databases are repository-scoped; matching names in other repositories must not suppress missing-target diagnostics.

## Decision

### Typed ERD Model and Deterministic Generation

- An intermediate typed model (`ERDModel`, `ERDEntity`, `ERDAttribute`, `ERDRelationship`) is constructed from the System IR `table` nodes and `references` relationships.
- Diagram entities use sanitized alphanumeric identifiers matching Mermaid lexical rules (`[A-Za-z_][A-Za-z0-9_]*`), preserving schema-qualified and display names in Mermaid bracketed alias labels (`entity_id["schema.table"]`).
- Column types are normalized into valid single-token Mermaid attribute words conforming strictly to the documented grammar (`[A-Za-z][A-Za-z0-9_\-\(\)\[\]]*`). Commas in precision/scale parameters are normalized to underscores (e.g. `numeric(14_2)` and `decimal(10_2)`), and multi-word SQL types are standardized (e.g. `timestamp with time zone` to `timestamptz`).
- Keys are assigned based on declared primary keys (`PK`), single-column unique constraints (`UK`), and foreign key columns (`FK`), supporting composite markers (`PK, FK` or `UK, FK`).
- Relationships represent foreign keys with crow's-foot cardinality:
  - Parent side is mandatory (`||`) when foreign key columns are `NOT NULL`, and optional (`|o`) when nullable.
  - Child side is at most one (`o|`) when foreign key columns are unique in the child table, and zero or more (`o{`) otherwise.
  - Identifying line style (`--`) is used when the child primary key contains the foreign key columns; non-identifying (`..`) is used otherwise.
  - The relationship label records the foreign key column names (or constraint name).
- When table facts exist, `erd.mmd` is generated deterministically alongside `architecture.md` and `architecture.mmd`, categorized under `repodocs-deterministic`. When no table facts exist in the IR, `erd.mmd` is omitted. Retired ERD artifacts are safely removed upon regeneration when table evidence is absent.

### Documented Renderer-Compatible Validation

- `validate_mermaid` dispatches on the diagram header: `flowchart TD` for architecture diagrams and `erDiagram` for entity-relationship diagrams.
- ERD validation strictly enforces the documented grammar for attribute types (`[A-Za-z][A-Za-z0-9_\-\(\)\[\]]*`), rejecting raw commas in data types. It verifies diagram header, comment syntax (`%%`), entity block delimiters (`{ ... }`), attribute tokens (type, name, keys, optional comments), relationship line syntax, and entity reference integrity. Any relationship targeting an undeclared entity fails validation.
- The built-in validator operates natively in Python without requiring Node.js or browser installations, and checks the documented syntax rules that RepoDocs relies on. It is not the Mermaid renderer, so it does not prove that a diagram renders. Rendering with the real Mermaid renderer is not implemented and remains open work under epic #8.

### Diagnostics and Evidence Reporting

- Foreign key targets are resolved strictly within the declaring table's repository and follow the PostgreSQL analyzer's canonical repository and schema identity. Schema-qualified tables (e.g. `billing.customers`) are registered exclusively under their qualified name and do not receive bare-name aliases, preventing cross-schema false suppression of unresolved foreign-key warnings.
- Missing primary keys and unsupported relationships are reported as structured comments in `erd.mmd` and in a `Schema diagnostics` section in `architecture.md`.
- `erd.mmd` explicitly preserves entity and relationship evidence mappings with repository, file path, line span, and evidence ID in a top-level `%% Evidence mappings:` block and inline `%% evidence:` comments immediately preceding each entity and relationship.
- Generated `erd.mmd` files are tracked in `artifact-manifest.json`, protected against unexpected manual edits, and validated with exit code 0 when valid.

## Consequences

- PostgreSQL repositories automatically gain editable ER diagrams, checked by the built-in syntax validator, (`erd.mmd`) in their generated documentation directory with full evidence traceability.
- Attribute types remain strictly compatible with all Mermaid parser versions.
- Missing-target warnings remain accurate across multi-repository boundaries and distinct schema namespaces.
- Projects without database facts remain unaffected and produce no extraneous diagram artifacts.
- The manifest schema remains at version 1.0 because `erd.mmd` uses the existing `repodocs-deterministic` generator classification.
