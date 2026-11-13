# RepoDocs Agent Instructions

You are contributing to **RepoDocs**, an open-source, AI-enhanced repository documentation system. Read these files before proposing or changing architecture or code:

1. `docs/PRODUCT_REQUIREMENTS.md`
2. `docs/ARCHITECTURE.md`
3. `docs/DECISIONS.md`
4. `docs/WORKFLOWS.md`
5. `docs/ROADMAP.md`

## Mission
Build evidence-backed, maintainable documentation and diagrams for single- and multi-repository software systems. The CLI, GitHub Actions, agent integrations, and future web UI must share one core engine.

## Non-negotiable constraints
- CLI and GitHub Actions must run without a persistent RepoDocs backend or database.
- AI-enhanced generation is the preferred experience, but local analysis must not require a cloud account or API key. Do not claim managed cloud AI is free or offline.
- Keep deterministic extraction, AI interpretation, diagram modeling, rendering, and publishing separate.
- Represent observed, inferred, and proposed claims distinctly; preserve evidence and exact repository commit revisions.
- Do not silently treat inferred cross-repository links as verified.
- Publish generated documentation through reviewable PRs; never overwrite user-authored docs without explicit ownership.
- Treat repositories as untrusted; do not execute their code or leak secrets to model providers.
- Favor modular monolith boundaries and plugin interfaces over premature microservices.
- Use machine-readable CLI output and noninteractive commands for automation and agents.

## Working rules
- Do not assume proposed CLI commands, schemas, packages, or hosted services already exist.
- Before implementation, identify which decisions are settled and which require user approval.
- Prefer small, testable vertical slices; add tests for parsers, provenance, diagram generation, and publication safety.
- When changing architecture, update `docs/DECISIONS.md` and affected documentation in the same PR.
- Do not invent source relationships, runtime execution order, or coverage guarantees.
- When uncertain, propose options and tradeoffs rather than silently making consequential choices.
- Keep explanations concise, technical, and practical. Avoid em dashes.

## First task for a new coding agent
Inspect repository state, compare it with the documents, identify gaps, and propose the smallest end-to-end implementation plan. Do not start large-scale implementation or create GitHub issues without approval.
