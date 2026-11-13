# RepoDocs

RepoDocs is an evidence-backed repository documentation engine. The current implementation provides the Milestone 1 foundation and first working CLI. It scans local working trees without executing repository code, stores a versioned JSON System IR, and generates deterministic Markdown and Mermaid artifacts.

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

Create a project, register repositories, scan the current working trees, generate documentation, and validate it:

```powershell
repodocs init smartcart
repodocs add ..\smartcart-backend --id backend
repodocs add ..\smartcart-frontend --id frontend
repodocs scan
repodocs generate
repodocs validate
```

The CLI writes versioned JSON responses to standard output. Scan state is stored under `.repodocs/`; generated artifacts are stored under the configured dedicated output directory, which defaults to `docs/generated/`. The foundation slice reports basic languages, manifests, and dependencies. Framework-aware Spring Boot and React analysis is planned for Milestone 2.

## Setup
1. Open the repository in Codex Desktop, Claude Code, or another development environment.
2. Read `AGENTS.md` and all files under `docs/` before changing architecture or code.
3. Work on a feature branch and submit all changes through a pull request to `main`.
4. Run the test suite with `python -m pytest`.

## Keeping context current
When decisions change, update `docs/DECISIONS.md`, the relevant ADR, and affected specifications. These files, not remembered conversation fragments, should be the authoritative source.
