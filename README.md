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

The CLI writes versioned JSON responses to standard output. Scan state and the artifact manifest are stored under `.repodocs/` next to `repodocs.json`; generated artifacts are written to the destination repository under the configured dedicated output directory, which defaults to `docs/generated/`. Every scan reports languages, manifests, and dependencies; the Spring Boot adapter adds framework-aware facts for repositories where it detects Spring Boot (see Framework-aware analysis below). React and PostgreSQL analysis follow in Milestone 2.

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

### Exit codes
| Code | Meaning |
|---:|---|
| 0 | Success |
| 2 | Usage or configuration error, including a missing destination |
| 3 | Persisted IR or generated artifacts failed validation |
| 4 | Ownership failure: unowned files or unexpected edits in generated output |
| 5 | Reserved: required relationship confirmation unresolved |
| 6 | Snapshot rejected: dirty, non-Git, unpinned in pinned mode, stale, or ambiguous revision |

## Setup
1. Open the repository in Codex Desktop, Claude Code, or another development environment.
2. Read `AGENTS.md` and all files under `docs/` before changing architecture or code.
3. Work on a feature branch and submit all changes through a pull request to `main`.
4. Run the test suite with `python -m pytest`.

## License

RepoDocs is licensed under the Apache License, Version 2.0. The complete license text is in `LICENSE`, and attribution notices are in `NOTICE`. The decision is recorded in `docs/DECISIONS.md` (D28) and `docs/adr/ADR-013-open-source-license.md`. See `docs/RELEASE.md` for release notes on licensing.

## Keeping context current
When decisions change, update `docs/DECISIONS.md`, the relevant ADR, and affected specifications. These files, not remembered conversation fragments, should be the authoritative source.
