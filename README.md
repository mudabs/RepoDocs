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

The CLI writes versioned JSON responses to standard output. Scan state and the artifact manifest are stored under `.repodocs/` next to `repodocs.json`; generated artifacts are written to the destination repository under the configured dedicated output directory, which defaults to `docs/generated/`. The foundation slice reports basic languages, manifests, and dependencies. Framework-aware Spring Boot and React analysis is planned for Milestone 2.

## CLI contract

### Repository roles and the destination
- `source` repositories are scanned into the System IR.
- Exactly one registered repository is the documentation destination: a source repository marked with `--destination`, or one `--role destination` repository that is never scanned.
- `documentation.output_directory` is relative to the destination root and must be a dedicated subdirectory inside it. When a source repository is also the destination, that directory is excluded from its own scan.
- `generate` and `validate` fail with a JSON error when the destination is missing, unknown, or not a directory.

### Snapshot modes
- `repodocs scan --snapshot-mode local` (default) scans the working tree including uncommitted and untracked files. Evidence resolves to the commit SHA only when the tree is exactly that commit; otherwise it resolves to a content fingerprint of the scanned tree.
- `repodocs scan --snapshot-mode pinned` is for automation. It requires a Git repository with a clean working tree and rejects anything else.
- `--pin <repository-id>=<sha>` or `pinned_revision` in `repodocs.json` states the commit a repository must be checked out at. Abbreviated SHAs are rejected as ambiguous, and a different HEAD is rejected as stale.

### Exit codes
| Code | Meaning |
|---:|---|
| 0 | Success |
| 2 | Usage or configuration error, including a missing destination |
| 3 | Persisted IR or generated artifacts failed validation |
| 4 | Ownership failure: unowned files or unexpected edits in generated output |
| 5 | Reserved: required relationship confirmation unresolved |
| 6 | Snapshot rejected: dirty, non-Git, stale, or ambiguous revision |

## Setup
1. Open the repository in Codex Desktop, Claude Code, or another development environment.
2. Read `AGENTS.md` and all files under `docs/` before changing architecture or code.
3. Work on a feature branch and submit all changes through a pull request to `main`.
4. Run the test suite with `python -m pytest`.

## Keeping context current
When decisions change, update `docs/DECISIONS.md`, the relevant ADR, and affected specifications. These files, not remembered conversation fragments, should be the authoritative source.
