# RepoDocs

RepoDocs is an evidence-backed repository documentation engine. The current implementation provides the Milestone 1 foundation and first working CLI. It scans local working trees without executing repository code, stores a versioned JSON System IR, and generates deterministic Markdown and Mermaid artifacts into one configured documentation destination.

## Local web-first development

The next release targets a local/self-hosted browser workflow over the same Python engine. It is specified, not implemented yet: see [the workflow contract](docs/WEB_WORKFLOW.md), ADR-027 and epic #63. The CLI remains standalone. Public hosting, managed funded AI, accounts, payments and videos are deferred.

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
- `repodocs validate` checks Mermaid syntax with a built-in Python validator that needs no Node.js or browser tooling and enforces attribute type syntax. It is not the Mermaid renderer and does not prove that a diagram renders.
- `erd.mmd` is tracked in `artifact-manifest.json`, protected against unexpected edits, and safely retired when database facts are no longer present. See ADR-022.

### Checking diagrams with the real Mermaid renderer (optional)

RepoDocs' built-in validator checks the grammar it emits and is not Mermaid. The test suite can additionally render every generated diagram with the real, pinned Mermaid renderer: run `npm ci` in `tools/mermaid` (Node 18.19 or newer, and a local Chrome or Chromium; set `PUPPETEER_EXECUTABLE_PATH` if it is not found). Without it those tests are skipped locally and a missing renderer is reported as `not-available`, never as success. Pull-request CI installs it and fails if it is missing. Ordinary Python-only use needs none of this.

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
- Usage is bounded per run (ADR-031). Before every request the whole serialized request is checked against `ai.limits.context_window_tokens` (default 4,096 for Ollama, 200,000 for Anthropic) plus its reserved output; a request that does not fit is refused and nothing is sent. Optional `ai.limits` (`request_output_tokens`, `max_requests` up to 6, `max_count_requests`, `max_input_tokens`, `max_output_tokens`, `max_cost_usd`) can only lower the hard ceilings, and `ai.effort` (`low`, `medium` or `high`, Anthropic only) is sent only when you set it. Cost limits need `ai.prices`, a versioned rate profile you supply for one provider and model in US dollars per million tokens, with all four rates; RepoDocs ships no prices. Token counts are provider counts where the provider offers them (Anthropic, after consent); otherwise a request is bounded at one token per byte, which is a bound and not a count and is often several times larger than the real number. Usage a provider did not report stays reserved at its worst case, never zero, and reported usage past a cap stops the run. `ai-interpret` and `architecture discover` print a `usage` summary (charged tokens and cost, estimated maximum cost, remaining allowance, stop reason) and write `.repodocs/receipts/*.json`. An Ollama prompt larger than its window is refused instead of being truncated silently; raise `ai.limits.context_window_tokens` to what your model supports, or lower the discovery context budgets.

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

### Architecture discovery (optional, ADR-026)

For an application that no analyzer understands, such as a plain JavaScript and Express app, the deterministic documents list languages and dependencies but no components. Architecture discovery asks a model for a structured view of the application's components and connections, checks every claim against the scanned snapshot, and renders it. It is optional, off by default, and never changes the System IR, a cross-repository candidate, or a confirmation.

Workflow:

1. `repodocs scan`, then choose a provider: `repodocs ai-configure --provider ollama --model <name>` (a local model; nothing leaves the machine) or `--provider anthropic` (external).
2. `repodocs architecture context` writes the preview `.repodocs/architecture-context.json` and contacts nothing. It is separate from the context a stored view was built from, so previewing again changes nothing already discovered. It holds redacted, bounded excerpts of each repository's README, manifests, and entry-point files, a name inventory, and what was omitted and why (denied names such as `.env` and `*.pem`, binary, oversized, stale, links, budgets). Review it.
3. An external provider needs two consents: `--consent-external` for the evidence bundle and, separately, `--consent-source-excerpts` for these excerpts. One never implies the other, and consent for an older policy version does not apply.
4. `repodocs architecture discover` runs a bounded exploration (at most 3 rounds, 6 requests, 8 files per round, 200 KiB of context, 32 KiB per response by default; `architecture.budgets` in `repodocs.json` can only lower them) and stores the validated view in `.repodocs/architecture-view.json`. A current stored view is reused without contacting the provider; `--refresh` runs new inference. `generate` never calls a provider.
5. `repodocs generate` writes `architecture-view.mmd` (and detail diagrams for crowded views) and `architecture-view-evidence.json` beside the other documents when a current view is stored, plus `architecture-view-alternatives.md` when the model reported competing interpretations (each with the components it concerns and its evidence locations; they are never drawn into the diagram). With `"architecture": {"html_export": true}` it also writes one self-contained `architecture-view.html` (zoom, pan, fit, evidence on click, and a list of the alternatives). The page embeds the pinned Mermaid bundle, so it needs the optional tooling and fails clearly without it.
6. `repodocs validate` checks the files against the current view and reports a stale or invalid view as a warning.

What to expect:
- Items are `inferred` only when they cite excerpts of their own member files, and are otherwise `proposed`, drawn dashed and labelled in the text. A connection between repositories is drawn as unconfirmed unless a confirmed candidate is bound to it: same direction, and the candidate's observed request and endpoint files are members of the two components, with evidence cited from both sides. Valid citations show that a view is well formed, not that a relationship is true: check the evidence. Each item also has a computed `support` (ADR-028), separate from `classification`: `supported` means a deterministic fact backs it (a matching observed request/endpoint pair, observed role facts in member files, or a bound confirmed candidate), `unverified` means nothing checkable was found (the normal state on a stack with no analyzer; marked `(unverified)` and drawn dashed), and `unsupported` means a checkable literal is absent (which can still be real outside the checked excerpts). Unsupported items are left out of the diagrams and kept, with reason codes, in `.repodocs/architecture-view.json`, the evidence map and the HTML page. Supported identifies a static fact match, not proof of responsibilities or runtime behavior, and no output says verified. Routes assembled from a prefix in another file are reported unsupported by the literal check. A view stored before this change is reported invalid; refresh it with `discover --refresh`.
- The evidence map links to the exact commit on GitHub only for a committed snapshot of a `github.com` repository. Uncommitted content is labelled with its fingerprint and not linked. The map and page hold locations and hashes, never source text.
- A changed scan makes the view stale: its files are retired with a warning, and the deterministic documents are unaffected. CI publication from a clean clone has no stored view (it is local state and holds source excerpts, so it is not committed). Earlier architecture files are then kept as they are, `generate` and `validate` warn that their freshness was not verified, the manifest keeps the provenance each file was recorded with, and the pull request lists them as unverified. Refresh them locally with `discover --refresh`, or remove them on purpose with `repodocs generate --retire-architecture`. A discovery that ends early is stored as `partial` with its reason (`incomplete-response`, `empty-response`, `no-progress`, a budget, or a provider error), and a `--refresh` that ends early does not replace a complete current view.
- Exit codes are the existing ones: 2 for configuration or missing consent, 7 for provider failure, 4 for edited generated files, 5 for unresolved confirmations (`validate`). A partial discovery exits 0 with a warning.
- Redaction is pattern-based, not a guarantee, and what a model returns is untrusted. Replayed tests prove the validation contract; they do not show how good any live model is (see the evaluation, #54).

### Publishing documentation from GitHub Actions

`repodocs publish` opens or updates one documentation pull request from the destination repository, and does nothing when the generated output is unchanged. It is meant to run in a coordination repository's workflow after `scan --snapshot-mode pinned`, `generate`, and `validate`.

- It builds a single commit of the output directory and the artifact manifest on top of the checked-out base, without touching the working tree. A real run first reads the remote and requires the checked-out base to be exactly the remote base branch's tip, so unpublished local commits, or a base that has moved since checkout, are refused instead of entering the pull request.
- If that commit equals the base, nothing is pushed and no pull request is opened. If an earlier documentation pull request is still open for the bot branch, it is obsolete. The bot branch is deleted first, under a lease, so a branch someone else has pushed to is never removed and its pull request is left open and untouched. Only after that succeeds is the pull request commented on and closed, and a repeated run finishes whatever a failure left behind.
- The commit goes to a bot-owned branch (`repodocs/docs-update` by default) with `--force-with-lease`. A branch already holding identical content is not pushed again, and an open pull request is updated instead of duplicated. To reject a documentation pull request, close it and delete the branch, or the next run opens it again.
- The base branch must be checked out by name, because the commit is built on the checkout; a detached HEAD or another branch is refused.
- It refuses to publish while any cross-repository candidate is unresolved (exit code 5), when the destination has changes outside the generated paths, or when the artifact manifest is not inside the destination repository.
- Commit `.repodocs/artifact-manifest.json`: a clone without it cannot regenerate. Ignore the rest of `.repodocs/` and the `sources/` checkouts. The manifest no longer records the destination's own revision, so merging a documentation pull request does not change the next run's output.
- `--dry-run` reports the change without contacting the remote, so it cannot check the remote base or find pull requests; it is a local preview only. A real run needs `GH_TOKEN` with contents and pull request write access to the destination, and the GitHub CLI.
- `repodocs scan --pins-json '{"frontend": "<sha>"}'` supplies pins from a workflow, merged with `--pin`. Check sources out by that exact SHA, which leaves them detached, so the output does not depend on a branch name.
- AI interpretation does not run in CI. Where no current claims match the pinned sources, `architecture-ai.md` is removed from the documentation pull request, which says so. Keeping AI documentation through CI publication is not supported yet.
- Decisions in `repodocs.confirmations.json` bind to file content hashes. Record them from a checkout whose line endings were not converted, or they go stale on a Linux runner and fail the gate.

The reusable action is `.github/actions/publish`; `docs/examples/repodocs-publish.yml` is a complete example for a coordination repository, including the one-time token and `.gitignore` setup. See ADR-023 for the design and for the choices that still await the owner's approval.

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
| 8 | Publication failed: git, remote, or pull request error, or unsafe destination state |

## Setup
1. Open the repository in Codex Desktop, Claude Code, or another development environment.
2. Read `AGENTS.md` and all files under `docs/` before changing architecture or code.
3. Work on a feature branch and submit all changes through a pull request to `main`.
4. Run the test suite with `python -m pytest`.

## Untrusted repositories

RepoDocs treats every scanned repository as untrusted (ADR-025).

- Scanning reads files and runs only `git`. It never imports, builds, or executes repository code, and it turns off the git settings a repository could use to run a program (`core.fsmonitor`, filter commands, and hooks), in nested submodules too. If git cannot report a repository's state, the scan stops instead of assuming it is clean.
- Evidence names a commit only when the files analyzed are exactly that commit. A modified, untracked, or differently checked-out submodule, or an ignored file that is analyzed, gives content-fingerprint provenance in a local scan, and a pinned scan refuses it and names the cause. An uninitialized submodule adds no files and is reported as a warning.
- Symlinks, directories that leave the repository (including Windows junctions), binary files, files over 2 MiB, more than 20,000 files, and more than 50 MiB in total are skipped or refused with a message. A file that cannot be parsed is skipped, and the rest of the repository is still analyzed.
- Text from analyzed source is escaped or placed in code spans in generated Markdown, and Mermaid labels use Mermaid entities. `repodocs validate` rejects raw HTML, link and image syntax, and Mermaid directives in generated output.
- Redaction is pattern-based, not a guarantee. The generated documentation is not redacted, so do not publish documentation from a repository whose analyzed literals contain secrets.

## Testing and CI

The `Publication self-test` workflow runs the publish action in dry-run mode against a throwaway repository on every pull request, with no secrets, and is not a required check. It does not open a real pull request.

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
- Cases are validated against `schemas/golden-case-1.0.json` (needs the `jsonschema` test dependency), and provenance failures are decided on exact counts, not rounded ratios.
- These scores describe the fixtures only. The AI comparison replays a recorded response and says nothing about a model's quality, and diagram validity uses the built-in validator, not a Mermaid renderer.

The suite does not measure coverage and is not comprehensive. Known untested areas include the Poetry and Maven manifest dependency paths outside the framework fixtures and behavior on macOS.

## License

RepoDocs is licensed under the Apache License, Version 2.0. The complete license text is in `LICENSE`, and attribution notices are in `NOTICE`. The decision is recorded in `docs/DECISIONS.md` (D28) and `docs/adr/ADR-013-open-source-license.md`. See `docs/RELEASE.md` for release notes on licensing.

## Keeping context current
When decisions change, update `docs/DECISIONS.md`, the relevant ADR, and affected specifications. These files, not remembered conversation fragments, should be the authoritative source.
