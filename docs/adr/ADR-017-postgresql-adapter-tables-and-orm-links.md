# ADR-017: PostgreSQL Adapter Vocabulary: Databases, Tables, and ORM Links

- Date: 2026-11-15
- Status: Accepted

## Context

Issue #16 asks for static extraction of supported PostgreSQL structure from SQL migrations, schema files, and explicitly supported ORM metadata, with evidence, database nodes and relationship edges that do not depend on Mermaid, reported unsupported constructs, and no relationship inferred without source evidence. ADR-015 provides the analyzer contract, span evidence, and the limitations list, but the System IR had no vocabulary for database facts, and no rule said how a persistence entity and a table that share a name relate to each other.

## Alternatives

- Parse SQL with a full PostgreSQL grammar and treat anything unparsed as an error.
- Record every SQL file as one opaque database node and let the AI stage read the migrations.
- Apply the JPA default naming strategy so an entity without `@Table` is linked to a table by convention.
- Connect an entity and a table by name match and record the link as observed.
- Read the live database schema instead of the migrations.

## Decision

- The `postgresql` adapter reads SQL files as text. It masks comments, string literals, dollar-quoted bodies, and quoted identifiers, splits statements on top-level semicolons, and applies the data-definition statements it supports in migration order: Flyway versioned files by version, then repeatable files, then other SQL files by path. No database is contacted and no SQL is executed.
- A `database` node represents the PostgreSQL schema a repository defines. It records the engine, the PostgreSQL-only constructs that were found (types such as `bigserial`, `jsonb`, `timestamptz`, and `uuid`, `CREATE EXTENSION`, `INHERITS`, `TABLESPACE`, procedural bodies), the migration tool inferred from the file names, the schema names, the extensions, the enum types, the number of tables, and the number of data statements that were skipped. A `contains` edge connects the repository to the database, and the database to every table.
- A `table` node records the qualified table name, the schema when the source qualified the name, the columns with their type, nullability, default, identity, generated, and unique flags, the primary key, the unique constraints, the foreign keys, the number of check constraints, the indexes, the partition parent, and the files that defined or changed the table. Columns are attributes of the table, not separate nodes, so the table node stays the unit an entity-relationship diagram is built from.
- Primary keys, unique constraints, and foreign keys are read from the statements that declared them. A foreign key between two tables of the same repository becomes a `references` edge between the table nodes with the constraint name, the local and referenced columns, and the referential actions. Every such edge is `observed` with confidence 1.0 and cites the span of the statement that declared it.
- A foreign key to a table that is not defined in the scanned SQL is an `ambiguous-reference` limitation, not an edge. Nothing links a table to a table the sources do not describe.
- `CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE`, and `DROP TABLE` are applied. `ALTER TABLE` supports adding, dropping, renaming, and retyping columns, adding and dropping constraints, renaming a table, and setting or dropping defaults, identity, and nullability. `CREATE TABLE ... AS query`, `LIKE`, `INHERITS`, `EXCLUDE` constraints, partitioned parents, `CREATE TABLE ... PARTITION OF` with an unknown parent, and unsupported `ALTER TABLE` actions are reported as `unsupported-construct` limitations instead of being applied.
- Views, materialized views, functions, procedures, triggers, rules, policies, domains, `DO` blocks, and psql meta-commands are reported, not analyzed. Data statements (`INSERT`, `UPDATE`, `DELETE`, `SELECT`, `TRUNCATE`) are counted and skipped. Liquibase changelogs are reported because only SQL files are analyzed, and Flyway undo migrations are reported because the schema reflects the versioned and repeatable migrations only.
- A `maps_to` edge connects a persistence entity to the table it declares with `@Table`, when the declared name matches exactly one extracted table of the same repository. The edge is `inferred` with confidence 0.8, names its basis (`jpa-table-name`), and cites the evidence of both sides. An entity whose declared name matches no table is left unlinked without a limitation, because the table may be defined by another repository or by a tool outside the scanned sources. An entity whose declared name matches more than one table is an `ambiguous-reference` limitation.
- The linking step runs after every adapter of a repository, is recorded in `provenance.analyzers` under the name `orm-metadata`, and is the only source of `maps_to` edges. It never turns a name match into an observed fact.

## Consequences

Database facts are available in the IR without a diagram renderer, so the later entity-relationship work (issues #21 and #28) can consume `table` nodes and `references` edges directly. The generated Markdown gains a Database schema section and an Inferred relationships section, and the architecture diagram gains the database as a child of its repository. The adapter understands the PostgreSQL subset that migrations use in practice; it is not a PostgreSQL parser, so the constructs it does not model are reported as limitations rather than dropped silently. Reading migrations rather than a live database means the schema is the one the repository declares, not the one a server happens to run, and it means nothing in the sources can be executed. The IR, CLI response, and manifest schemas stay at version 1.0 because nothing has been released (ADR-014).
