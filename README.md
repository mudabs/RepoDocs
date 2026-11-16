# RepoDocs

RepoDocs is an evidence-backed repository documentation engine. The current implementation provides the Milestone 1 foundation and first working CLI. It scans local working trees without executing repository code, stores a versioned JSON System IR, and generates deterministic Markdown and Mermaid artifacts into one configured documentation destination.

## Start here
- `AGENTS.md`: persistent coding-agent instructions.
- `docs/PRODUCT_REQUIREMENTS.md`: vision, scope, acceptance goals.
- `docs/ARCHITECTURE.md`: core modules, IR, AI, diagrams, security.
- `docs/DECISIONS.md`: accepted and unresolved decisions.
- `docs/WORKFLOWS.md`: CLI, GitHub, web, and agent scenarios.
- `docs/ROADMAP.md`: phased implementation and validation.
- `docs/adr/`: accepted architectural decisions.
- `schemas/`: versioned JSON schemas for persisted project, IR, manifest, and CLI response data.

## Milestone 1 quick start

Install the package in editable mode:

```powershell
python -m pip install -e ".[test]"
```

Run the commands from the coordination repository that will hold the project configuration and the generated system documentation. Register the source repositories, then register the destination, scan, generate, and validate:

```powershell
repodocs init smartcart
repodocs add ..\smartcart-backend --id backend
repodocs add ..\smartcart-frontend --id frontend
repodocs add . --id docs --role destination
repodocs scan
repodocs generate
repodocs validate
```

For a single repository that documents itself, mark it as both source and destination:

```powershell
repodocs add . --id app --destination
```

The CLI writes versioned JSON responses to standard output. Scan state and the artifact manifest are stored under `.repodocs/` next to `repodocs.json`; generated artifacts are written to the destination repository under the configured dedicated output directory, which defaults to `docs/generated/`. Every scan reports languages, manifests, and dependencies; the Spring Boot, React, PostgreSQL, and OpenAPI adapters add supported source facts (see Framework-aware analysis and Cross-repository candidates below).

## CLI contract

### Repository roles and the destination
- `source` repositories are scanned into the System IR.
- Exactly one registered repository is the documentation destination: a source repository marked with `--destination`, or one `--role destination` repository that is never scanned.
- `documentation.output_directory` is relative to the destination root and must be a dedicated subdirectory inside it. When a source repository is also the destination, that directory is excluded from its own scan.
- `repodocs set-destination <repository-id>` promotes an already registered repository; `--replace` switches from a previous destination after its generated output is removed.
- The artifact manifest records the destination's id, role, path, and output directory. Changing any of them for an existing manifest is refused rather than written elsewhere.
- `generate` and `validate` fail with a JSON error when the destination is missing, unknown, or not a directory.

### Snapshot modes
- `repodocs scan --snapshot-mode local` (default) scans the working tree including uncommitted and untracked files. Evidence resolves to the commit SHA only when the tree is exactly that commit; otherwise it resolves to a content fingerprint of the scanned tree.
- `repodocs scan --snapshot-mode pinned` is for automation. It requires a Git repository with a clean working tree and an explicit pin for every source repository, and rejects anything else.
- `--pin <repository-id>=<sha>` or `pinned_revision` in `repodocs.json` states the commit a repository must be checked out at. Abbreviated SHAs are rejected as ambiguous, and a different HEAD is rejected as stale.

### Framework-aware analysis
Built-in analyzers (ADR-015) run after the basic scan on every source repository where they detect their framework. `scan` reports which analyzers ran under `data.analyzers`, and the IR records the same under `provenance.analyzers`. Analyzers read source text only and never execute repository code.

The Spring Boot adapter (`spring-boot`) detects Spring Boot from `pom.xml`, `build.gradle(.kts)`, or an `@SpringBootApplication` class and extracts, from Java sources outside `src/test/`:
- the application as a `service` node, with build tool, artifact id, and Spring Boot version when they are literal in the manifest;
- `class` nodes for controllers, services, repositories, JPA entities, configuration classes, and components, each with span evidence (file and line range);
- `endpoint` nodes for `@RequestMapping`, `@GetMapping`, and the other mapping annotations whose paths are string literals, combined with class-level paths;
- `depends_on` edges from constructor parameters and fields whose declared type is another extracted class, and from Spring Data repositories to the entity named in their generic argument;
- entity fields, `@Id`, explicit `@Column`, `@Table`, and `@JoinColumn` names, and `references` edges for JPA associations;
- `configuration` nodes for `application*.properties|yml` files containing property keys only. Values are never recorded.

Anything the adapter sees but cannot support statically is listed in the IR `limitations` array and in the generated Markdown, each with evidence: non-literal mapping paths, functional routes, property placeholders in routes, reflection, SpEL, `@Profile` and `@ConditionalOn*`, Lombok and `@Generated`, custom `@Query` statements, JPA inheritance and embeddable mappings, Kotlin, Groovy, and Scala sources, ambiguous class names, and classes outside every application base package. Default JPA naming strategies are not applied, method bodies are not analyzed, and runtime call order is never inferred.

The React adapter (`react`, ADR-016) detects React from a `package.json` that declares `react` and owns JavaScript or TypeScript modules, or from modules that import `react` explicitly (JSX alone is not a signal, since Preact and Solid use it too), and extracts from modules outside test, story, mock, and `*.config.*` files:
- a `service` node per React package with the declared version range and the recognized toolchain (Vite, Next.js, react-scripts, and others), router, and HTTP client packages;
- `module` nodes per source file with `imports` edges to other modules (relative specifiers that resolve to a file) or to declared dependencies, each marked `static`, `dynamic`, `require`, or re-export;
- `component` nodes for top-level PascalCase function, arrow, `memo`, `forwardRef`, and class declarations containing JSX, with `references` edges only for JSX elements that resolve to components defined in the repository;
- `route` nodes with full literal paths composed from nested React Router `<Route>` elements and route objects, plus Next.js `pages/` and `app/**/page` files; Next.js `pages/api/` and `app/**/route` files become `endpoint` nodes;
- `http_request` nodes for `fetch`, `axios`, `ky`, and `axios.create`/`ky.create` instances (including instances imported from another module) whose URL is a string or template literal. Template holes are kept as `{expression}`; nothing is substituted, and sites on different client instances or base URLs stay distinct. These are declared targets, not observed traffic.

Reported rather than extracted: `import()` loads (the edge is kept, the timing is not), non-literal route paths and request URLs, import specifiers that match neither a file nor a declared dependency (path aliases are not applied), and `eval` or `new Function`. Function calls, hooks, and props never become relationships, and regex literals are not recognized by the scanner.

The PostgreSQL adapter (`postgresql`, ADR-017) reads SQL schema and migration files as text. No database is contacted and no SQL is executed. The adapter masks comments, string literals, dollar-quoted bodies, and quoted identifiers, splits statements on top-level semicolons, and applies the statements it supports in migration order: Flyway versioned files by version, then repeatable files, then other SQL files by path. Block comments nest, as they do in PostgreSQL, so a commented-out table stays commented out. It extracts:
- a `database` node per repository, with the PostgreSQL-only constructs that were found (`bigserial`, `jsonb`, `timestamptz`, `uuid`, `CREATE EXTENSION`, `INHERITS`, `TABLESPACE`, procedural bodies), the declared driver, the migration tool inferred from the file names, the schema names, the extensions, the enum types, the table count, and the number of skipped data statements;
- a `table` node per table, with the qualified name, the schema when the source qualified it, the columns (type, nullability, default, identity, generated, unique), the primary key, the unique constraints, the foreign keys, the check constraint count, the indexes, the partition parent, and the files that defined or changed it;
- `references` edges between the tables of the same repository, with the constraint name, the local and referenced columns, and the referential actions. Every edge is `observed` and cites the span of the statement that declared it.

The adapter applies to a repository that shows PostgreSQL usage: a manifest that declares a PostgreSQL driver, or a table definition that uses a PostgreSQL-only type. A repository that only holds portable SQL is not treated as PostgreSQL, because the dialect would be a guess.

`CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE`, and `DROP TABLE` are applied. A table that is defined twice keeps its first definition, which is what PostgreSQL does, and the repeated statement is reported. Renaming a column updates everything that names it (primary key, unique constraints, foreign keys, indexes, and the columns other tables reference); dropping a column removes the constraints and indexes that depend on it, as PostgreSQL does. Reported rather than extracted: views, materialized views, functions, procedures, triggers, rules, policies, domains, `DO` blocks, psql meta-commands, `CREATE TABLE ... AS query`, `LIKE`, `INHERITS`, `EXCLUDE` constraints, partitioned parents, unsupported `ALTER TABLE` actions, data statements, Liquibase changelogs, and Flyway undo migrations. A foreign key to a table that is not defined in the scanned SQL is an `ambiguous-reference` limitation, not an edge: no relationship is created without source evidence.

The ORM metadata step (`orm-metadata`) connects a persistence entity to the table it declares with `@Table` when the declared name matches exactly one extracted table of the same repository. Nothing in the sources states that the entity and the table describe the same rows, so the `maps_to` edge is `inferred` with confidence 0.8, names its basis, and cites the evidence of both sides. The generated documentation lists inferred relationships in their own section. An entity without a declared table name, and a table without an entity, stay unlinked.

Standalone index, table-drop, schema, extension, and enum-type migrations are included in migration order. Repeated column and named-index definitions preserve existing state rather than duplicating it. Implicit foreign keys are removed when a referenced primary-key column is dropped. A recorded driver declaration cites its manifest in the database node's evidence.

Standalone unsupported DDL and procedural migrations are reported with evidence too. Index names are scoped to the owning table's schema; qualified index drops target that schema, while ambiguous unqualified drops are reported without changing state. Runtime search paths are not guessed.

### Entity-relationship diagrams (ERD)

When a project contains PostgreSQL table facts, `repodocs generate` produces **`erd.mmd`** alongside `architecture.md` and `architecture.mmd`:
- Generated deterministically from a typed relational model (`repodocs.erd`) using Mermaid `erDiagram` syntax.
- Entities declare attributes with normalized types (commas replaced with underscores to strictly conform to Mermaid's documented attribute grammar) and `PK`, `FK`, and `UK` key constraints. Schema-qualified table names use Mermaid alias labels (`entity_id["schema.table"]`).
- Foreign keys produce crow's-foot relationship lines with identifying (`--`) or non-identifying (`..`) styles depending on child primary keys, and cardinality (`||`, `|o`, `o{`, `o|`) reflecting column nullability and uniqueness.
- Entity and relationship evidence mappings (file paths, line spans, and evidence IDs) are preserved in `erd.mmd` as structured comment blocks and inline declarations.
- Missing primary keys and repository-scoped unsupported relationships (such as foreign keys referencing tables outside the declaring repository) are reported in `erd.mmd` diagram comments and under `### Schema diagnostics` in `architecture.md`.
- `repodocs validate` performs documented renderer-compatible verification without requiring external Node.js or browser tooling, strictly enforcing attribute type syntax.
- `erd.mmd` is tracked in `artifact-manifest.json`, protected against unexpected edits, and safely retired when database facts are no longer present. See ADR-022.

### Cross-repository candidates

After analysis, `scan` matches React `http_request` findings to endpoints in
other repositories. Exact methods and literal/template paths must agree.
All discovered matches remain **unconfirmed, inferred candidates**, even if
only one high-confidence match exists. Candidate IDs use source/target semantic
identities; confidence, evidence, and both snapshot revisions are stored separately.
A client base URL alone does not prove service ownership. Competing endpoints
are retained; an origin match only strengthens a candidate when backend OpenAPI
server evidence supports it. Conflicting explicit origins are excluded.

The `openapi` analyzer supports **OpenAPI 3.0.x and 3.1.x JSON** paths and
standard HTTP operations, literal HTTP(S)/root-relative servers (operation,
Path Item, then document precedence), and same-document `#/...` JSON Pointers.
Local Path Item reference chains are bounded; cycles, missing targets, siblings,
operation references, YAML, 3.2.x, variable servers, and all external references
are reported rather than guessed. Remote references are never fetched, and
file references are never opened, even within the repository.

URL credentials, all queries, and fragments are removed before request identities,
metadata, claims, and docs are created. Dynamic hosts and unsupported schemes
are rejected. Imported client configuration is included in request evidence.
See ADR-018 for matching rules, confidence ranks, and the exact OpenAPI subset.

The IR has a required `candidates` array; the JSON scan response reports its count.
Generated Markdown labels candidates as unconfirmed and cites both sides'
evidence and revisions. Candidates are not authoritative diagram edges.

### Confirming and rejecting candidates

Every candidate needs a human decision before it is treated as resolved:

```powershell
repodocs candidates
repodocs confirm <candidate-id> --note "Verified against the gateway config"
repodocs reject <candidate-id> <candidate-id>
repodocs generate
repodocs validate
```

- `repodocs candidates` lists unresolved candidates from the last scan; `--status` selects `candidate`, `confirmed`, `rejected`, `stale`, or `all`.
- `confirm` and `reject` are noninteractive and write `repodocs.confirmations.json` next to `repodocs.json`. Commit that file. A later decision for the same candidate replaces the earlier one.
- Each decision records the candidate's evidence and both repository revisions. When a participating source file changes, the decision becomes `stale` and the candidate must be decided again.
- `repodocs validate` exits with code 5 while any candidate is unresolved or stale. Use `--allow-unresolved` for local drafts only.
- A decision changes the IR, so run `generate` again before `validate`. See ADR-019.
- If `repodocs.confirmations.json` is edited or deleted by hand, `candidates`, `generate`, and `validate` fail until `scan` reapplies it, so an outdated decision is never shown as in force.

### Optional AI interpretation

AI is off by default, and `scan`, `generate`, and `validate` never need a provider, key, network, or extra dependency.

```powershell
repodocs ai-bundle
repodocs ai-configure --provider ollama --model llama3
repodocs ai-interpret
```

- `repodocs ai-bundle` writes `.repodocs/evidence-bundle.json` and contacts nothing. The bundle is the only thing a provider receives: analyzed facts and evidence locations, with no source text, file listings, content hashes, or local paths, and with secret-looking strings redacted. Review it before consenting to external use.
- Providers are `ollama` (local) and `anthropic` (cloud, bring your own key; install with `python -m pip install -e ".[ai]"` and set `ANTHROPIC_API_KEY`). Keys are never written to `repodocs.json`.
- Anything that would send the bundle off the machine, including an Ollama host that is not a loopback address, is refused until you run `repodocs ai-configure --consent-external`. `--revoke-consent` returns the project to local-only.
- `repodocs ai-interpret` writes `.repodocs/ai-claims.json` with provider and model provenance. A claim is accepted only when every evidence and subject ID it cites exists in the bundle; others are rejected with a reason. Accepted claims are `inferred` or `proposed`, never `observed`, and a claim about an unconfirmed cross-repository candidate is always `proposed`.
- Using a cloud provider costs money on your provider account and requires a network connection. See ADR-020.

After `ai-interpret`, run `repodocs generate` to add **`architecture-ai.md`**.
It separates inferred interpretations from proposals and records citations,
provider/model provenance, the bundle hash, and the IR hash. Model text is
escaped prose, not executable HTML or Markdown links. The deterministic
`architecture.md`, `architecture.mmd`, and IR remain unchanged by model output.
No model claim creates an authoritative relationship or confirms a candidate.

Generation uses stored claims only: it never calls a provider. Missing claims
leave the deterministic output complete; stale or invalid claims produce a
warning and are omitted. After source or confirmation changes, explicitly run
`ai-interpret` again to refresh them. Removing the claims file and regenerating
retires the optional document, but only if it is manifest-owned and unedited.
Validation rejects stale or mismatched AI artifacts. Human edits and unowned
files remain protected.

Inferred claims require evidence bound to their named subjects. Multi-repository
claims must explicitly name a confirmed candidate and cite both sides; otherwise
they are proposals. Scope comes from the named subjects, not just the supplied
citations: omitting either repository's evidence does not make a confirmed
candidate single-repository. An unrelated third-repository subject is outside
that confirmed pair's scope. **Valid citations do not prove model wording true.**
Review interpretations with their evidence. `ai-interpret` reports contribution
counts, including proposals, rejections, and structural grounding failures.
Reviewed fake-provider golden cases currently yield 1 inference, 4 proposals,
3 rejections, and no reviewed unsupported inferences. These are regression
results, not a real-model factuality benchmark. See ADR-021.

### Exit codes
| Code | Meaning |
|---:|---|
| 0 | Success |
| 2 | Usage or configuration error, including a missing destination |
| 3 | Persisted IR or generated artifacts failed validation |
| 4 | Ownership failure: unowned files or unexpected edits in generated output |
| 5 | Cross-repository candidates are unresolved or their decisions are stale |
| 6 | Snapshot rejected: dirty, non-Git, unpinned in pinned mode, stale, or ambiguous revision |
| 7 | AI provider unavailable or returned unusable output; deterministic commands are unaffected |

## Setup
1. Open the repository in Codex Desktop, Claude Code, or another development environment.
2. Read `AGENTS.md` and all files under `docs/` before changing architecture or code.
3. Work on a feature branch and submit all changes through a pull request to `main`.
4. Run the test suite with `python -m pytest`.

## Testing and CI

The `Tests` workflow (`.github/workflows/tests.yml`) runs the complete pytest suite on every pull request targeting `main`, on every new commit to such a pull request, on pushes to `main`, and on manual dispatch. It tests Ubuntu and Windows on Python 3.11 (the declared minimum) and Python 3.14, and it always runs the tests present in the commit under test, so tests added by a pull request run automatically. The check names are `test (ubuntu-latest, 3.11)`, `test (ubuntu-latest, 3.14)`, `test (windows-latest, 3.11)`, and `test (windows-latest, 3.14)`.

- A change in behavior needs tests that cover it. A bug fix needs a regression test that fails without the fix.
- Run `python -m pytest` locally before pushing. Local runs supplement the GitHub checks; they do not replace them.
- The workflow has read-only permissions, uses no secrets, and pins its actions to commit SHAs. Update a pin by replacing the SHA and the version comment together.
- This is development CI for RepoDocs itself. Documentation-publication automation for user projects is separate work in Milestone 4.

### Quality evaluation

`python -m repodocs.golden tests/golden --summary` scores RepoDocs against reviewed golden fixtures and prints a table; without `--summary` it prints the versioned JSON report (`schemas/golden-report-1.0.json`). It reports node, relationship, and limitation precision and recall, evidence recall, cross-repository candidate accuracy, evidence resolution against the files on disk, the unsupported-claim rate, diagram validity, unchanged-scan stability, and a deterministic-versus-AI comparison. A ratio is `null` when it has no denominator.

- A case is `tests/golden/<name>/golden.json`. Write it from the fixture sources, never from tool output, and name facts by type, repository, and name, not by ID or hash. See ADR-024 for the metric definitions.
- The report lists `missing` and `unexpected` facts in the golden's own form. Decide which side is wrong, then edit the golden by hand; there is no command that rewrites it. Where the tool misses a real fact, keep it in the golden.
- `tests/golden/baseline-report.json` holds exact scores, and the suite fails if they change in either direction. After a reviewed change, regenerate it with `python -m repodocs.golden tests/golden --output tests/golden/baseline-report.json` and review the diff.
- These scores describe the fixtures only. The AI comparison replays a recorded response and says nothing about a model's quality, and diagram validity uses the built-in validator, not a Mermaid renderer.

The suite does not measure coverage and is not comprehensive. Known untested areas include the Poetry and Maven manifest dependency paths outside the framework fixtures and behavior on macOS.

## License

RepoDocs is licensed under the Apache License, Version 2.0. The complete license text is in `LICENSE`, and attribution notices are in `NOTICE`. The decision is recorded in `docs/DECISIONS.md` (D28) and `docs/adr/ADR-013-open-source-license.md`. See `docs/RELEASE.md` for release notes on licensing.

## Keeping context current
When decisions change, update `docs/DECISIONS.md`, the relevant ADR, and affected specifications. These files, not remembered conversation fragments, should be the authoritative source.
