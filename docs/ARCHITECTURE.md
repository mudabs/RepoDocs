# RepoDocs Architecture

**Status:** Proposed, subject to independent review and technical contracts.

## System shape
A modular monolith with a reusable core engine. Interfaces are adapters, not separate analysis implementations.

```text
CLI / GitHub Action / Coding Agent / Future Web UI
                     |
           Project Orchestration
                     |
      Snapshot Ingestion + Discovery
                     |
      Language Parsers + Framework Adapters
                     |
            Versioned System IR
                     |
        Cross-repo Correlation + Overrides
                     |
       Optional AI Enrichment / Interpretation
                     |
        Documentation and Diagram Planning
                     |
        Generator Registry + Renderers
                     |
          Validation + Artifact Manifest
                     |
      Local Files / GitHub PR Publication
```

Stages may iterate; AI does not automatically promote guesses to facts.

## Core modules and boundaries

### Ingestion
Read local directories or pinned Git snapshots. Inventory files and dependencies; track source commits/diffs. Exclude binaries, vendored dependencies, generated output. Never execute arbitrary repository code during normal static analysis.

### Analysis
- L1: generic manifests, config, CI/CD, framework signatures.
- L2: language syntax/semantic parsing, e.g. Tree-sitter and specialist tooling.
- L3: framework adapters, initially Spring Boot, React, FastAPI.
Separate language parsers from framework adapters. Dynamic dispatch, reflection, runtime configuration, and generated code limit completeness.

### System IR
Versioned graph with project, repo, service, module, class, function, endpoint, DB, infra, external service entities and typed edges such as contains, calls, imports, exposes, reads, writes, deploys, depends_on. Each significant fact records stable IDs, evidence IDs, repository, file/symbol/line where available, analyzer, commit SHA, observed/inferred/proposed classification, confidence, and user confirmation/override provenance. Store locally in serialized form; hosted mode may use PostgreSQL.

### Correlation
Match cross-repository relationships using routes, base URLs, API schemas, config, clients, and deployment evidence. Produce candidate links where ambiguous, requiring explicit confirmation. Record all repository revision SHAs as a consistent project snapshot.

### AI
Provider-neutral adapter. Supply curated, redacted evidence bundles; request schema-constrained outputs; validate evidence references and claims. AI can explain components, propose interactions, group meaningful subsystems, and plan diagrams. Provider options: BYO OpenAI/Anthropic/Google, Ollama, and possible authenticated managed RepoDocs AI. Managed AI requires service costs, identity, quota, and privacy controls.

### Documentation planning
Choose artifact type, scope, required evidence, coverage, destination, and generator. Do not generate unsupported views as if complete. Maintain dependencies from IR facts to artifacts.

### Diagram generation: detailed mechanics
1. Select diagram type and scope.
2. Query IR for relevant, sufficiently supported entities/edges.
3. Build typed diagram-specific model, e.g. classes/associations, tables/FKs, participants/ordered messages, or C4 containers.
4. Group/filter to readable abstraction level; preserve traceability and explicit uncertainty.
5. Deterministically serialize model to Mermaid or PlantUML using type-specific generator plugin.
6. Parse/render with diagram tool, validate syntax and output; retry/fail with actionable diagnostics.
7. Save editable `.mmd`/`.puml` and optional SVG/PNG, plus provenance manifest.

Rendering is not AI drawing: Mermaid/PlantUML perform rendering/layout; Graphviz/ELK.js may be used for complex layouts. React Flow is a possible later interactive UI, not required by CLI. Sequence diagrams are particularly difficult: a call graph alone does not establish runtime ordering or branches; do not assert observed sequence without adequate evidence.

### Publishing
Store repository-local docs in owning repos, system-wide docs in configured coordination location. Generated artifact manifest tracks ownership, hashes, source SHAs, versions, and dependencies. Preserve human-authored files. GitHub Action/hosted App creates reviewable PRs, suppresses empty PRs and bot loops.

## Deployment modes
- CLI: local core engine, local config/cache/IR; no mandatory server or GitHub login.
- GitHub Action: runs same CLI on runner; multi-repo access requires explicit credentials and coordination mechanism.
- Coding agents: noninteractive CLI/JSON; skills first, MCP later.
- Future web: React/TypeScript UI, proposed FastAPI orchestration, background workers, PostgreSQL, optional Redis, Docker Compose self-hosting; hosted SaaS later.

## Security boundaries
Untrusted repository inputs; file/path traversal protection, resource limits, sandboxing where necessary, no arbitrary builds, prompt-injection resistant AI input, secret redaction, private-code consent before external inference, least-privilege GitHub App/token, safe Markdown/SVG rendering, tenancy separation in hosted mode.

## Major failure modes
Inaccurate dynamic code inference; cross-repo false matches; stale multi-repo snapshots; hallucinated AI steps; malformed or overcrowded diagrams; nondeterministic PR churn; overwritten manual docs; leaked secrets; fork-PR token exposure; expensive AI usage; GitHub rate limits and concurrent runs; excessive MVP scope.
