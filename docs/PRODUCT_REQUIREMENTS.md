# RepoDocs Product Requirements

**Status:** Design baseline, not implemented. **Initial MVP target:** November 30, 2026 (aggressive, subject to rescoping).

## Product goal
Automatically discover, generate, validate, and maintain documentation and diagrams for real software systems spanning one or multiple repositories. Documentation must remain reviewable, source-evidenced, and version-controlled.

## Users and interfaces
- Developer using local CLI, including no-account deterministic mode.
- Team enabling GitHub Actions to propose documentation PRs after source changes.
- Developer using Claude Code or Codex to operate RepoDocs through CLI/agent skill; MCP later.
- Team using future hosted or self-hosted web dashboard.

## Required capabilities
1. Project = software system, containing multiple repositories and services; a repo may contain several services.
2. Local paths and Git repository snapshots, with exact revision provenance.
3. Generic discovery plus extensible language parsers and framework adapters.
4. Initially prioritize Java/Spring Boot, JS/TS/React, Python/FastAPI; actual depth must be measured and documented.
5. Versioned intermediate system graph with nodes, edges, evidence, confidence/classification, and overrides.
6. Cross-repository relationship candidates with user confirmation for uncertainty.
7. AI-assisted architecture interpretation and prose, with optional OpenAI, Anthropic, Google, Ollama, and potential managed RepoDocs AI.
8. Diagram planning and deterministic diagram-source generation from validated models.
9. Markdown plus editable Mermaid/PlantUML sources, optionally SVG/PNG previews.
10. Applicable documentation catalog: architecture, API, ERD, class, component, dependencies, and progressively sequence, activity, state, deployment, security, CI/CD.
11. Local documentation in source repos; system-wide documentation in configured coordination location.
12. Generated artifact manifest, stable outputs, validation, and protected human edits.
13. GitHub automation producing reviewable documentation PRs, no bot loops or empty PRs.
14. Machine-readable, noninteractive CLI operations for agents and CI.

## Quality requirements
- Evidence-backed claims and explicit uncertainty.
- Meaningful diagram scoping and decomposition for large systems.
- Stable generation minimizing noisy diffs.
- Privacy, safe handling of secrets, least privilege, untrusted repository isolation.
- Reproducible output tied to per-repository commit SHAs and generator/model versions.

## Success demo
SmartCart: React frontend repository, Spring Boot backend repository, PostgreSQL. Discover components and APIs, propose/confirm cross-repo links, generate architecture/API/ERD/component docs, publish appropriate PRs, then update docs after an endpoint changes.

## Explicit non-goals for initial MVP
Polished SaaS, billing, comprehensive language coverage, guaranteed runtime-accurate sequence diagrams, interactive drag-and-drop editor, runtime tracing, complete UML suite, and advanced enterprise governance.
