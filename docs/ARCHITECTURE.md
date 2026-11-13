# RepoDocs Architecture

**Status:** Approved design baseline; implementation remains subject to the contracts below.

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
Read local working trees or pinned Git snapshots. Local CLI scans include uncommitted changes and record the Git revision plus a working-tree content fingerprint. GitHub Actions use pinned, clean Git revisions. Inventory files and dependencies; track exact repository revisions, dirty-tree state where applicable, source commits/diffs, and content hashes. Exclude binaries, vendored dependencies, generated output. Never execute arbitrary repository code during normal static analysis.

### Analysis
- L1: generic manifests, config, CI/CD, framework signatures.
- L2: language syntax/semantic parsing, e.g. Tree-sitter and specialist tooling.
- L3: framework adapters, with deeper Spring Boot and React analysis in Milestone 2. FastAPI remains an extension target.
Separate language parsers from framework adapters. Dynamic dispatch, reflection, runtime configuration, and generated code limit completeness.

### System IR
The System IR is a versioned JSON graph with project, repository, service, module, class, function, endpoint, database, infrastructure, and external-service nodes plus typed edges such as contains, calls, imports, exposes, reads, writes, deploys, and depends_on. Semantic IDs identify entities independently from source evidence and content hashes. Each claim records evidence IDs, repository revision, file/symbol/line where available, analyzer and model provenance, classification, confidence, and confirmation or override provenance. Store the canonical form locally; hosted mode may persist the same structures in PostgreSQL.

### Correlation
Match cross-repository relationships using routes, base URLs, API schemas, config, clients, and deployment evidence. Produce candidate links where ambiguous. Only explicit confirmation or direct evidence can make a relationship authoritative. Persist candidates, confirmations, rejections, and stale states in project data tied to the participating repository revisions.

### AI
Provider-neutral adapter. Supply curated, redacted evidence bundles only after explicit consent for external providers; local-only mode never sends repository content externally. Request schema-constrained outputs and validate evidence references and claims before documentation generation. The MVP supports one cloud BYO provider and Ollama. Managed RepoDocs AI is deferred and requires separate service costs, identity, quota, and privacy decisions.

### Documentation planning
Choose artifact type, scope, required evidence, coverage, destination, and generator. Do not generate unsupported views as if complete. Maintain dependencies from IR facts to artifacts. Generated files live in dedicated paths and are tracked by a manifest; unexpected edits fail validation rather than being overwritten.

### Diagram generation: detailed mechanics
1. Select diagram type and scope. The MVP starts with Mermaid architecture/component and ERD diagrams; API documentation is Markdown.
2. Query IR for relevant, sufficiently supported entities/edges.
3. Build typed diagram-specific model, e.g. classes/associations, tables/FKs, participants/ordered messages, or C4 containers.
4. Group/filter to readable abstraction level; preserve traceability and explicit uncertainty.
5. Deterministically serialize the model to Mermaid using a type-specific generator plugin. PlantUML remains an extension point.
6. Parse/render with diagram tool, validate syntax and output; retry/fail with actionable diagnostics.
7. Save editable `.mmd` and optional SVG/PNG, plus provenance manifest.

Rendering is not AI drawing: Mermaid performs initial rendering/layout; PlantUML and Graphviz/ELK.js remain extension points. React Flow is a possible later interactive UI, not required by CLI. Sequence diagrams are particularly difficult: a call graph alone does not establish runtime ordering or branches; do not assert observed sequence without adequate evidence.

### Publishing
Store repository-local docs in owning repos and system-wide docs in one configured coordination repository. Generated artifact manifests track ownership, hashes, source SHAs, versions, and dependencies. Preserve human-authored files and fail on unexpected edits. The GitHub Action creates reviewable PRs in the coordination repository, suppressing empty PRs and bot loops.

## Deployment modes
- CLI: local core engine, local config/cache/IR; no mandatory server or GitHub login; versioned JSON output and explicit exit codes.
- GitHub Action: runs the same CLI on a runner; multi-repo access requires explicit credentials and the coordination repository. CI fails when required relationship confirmations remain unresolved.
- Coding agents: noninteractive CLI/JSON; skills first, MCP later.
- Future web: React/TypeScript UI, proposed FastAPI orchestration, background workers, PostgreSQL, optional Redis, Docker Compose self-hosting; hosted SaaS later. It is not part of the current implementation.

## Security boundaries
Untrusted repository inputs; file/path traversal protection, resource limits, sandboxing where necessary, no arbitrary builds, prompt-injection resistant AI input, secret redaction, explicit consent before external inference, local-only mode, least-privilege GitHub App/token, safe Markdown/SVG rendering, tenancy separation in hosted mode.

## Quality evaluation

Representative golden fixtures define expected facts and relationships. Evaluation reports fact-level precision and recall, cross-repository correlation accuracy, unsupported-claim rate, evidence resolution, diagram validity, output stability across unchanged scans, and the incremental contribution of AI compared with deterministic-only generation. Human review may assess readability, but appearance alone is not an accuracy criterion.

## Major failure modes
Inaccurate dynamic code inference; cross-repo false matches; stale multi-repo snapshots; hallucinated AI steps; malformed or overcrowded diagrams; nondeterministic PR churn; overwritten manual docs; leaked secrets; fork-PR token exposure; expensive AI usage; GitHub rate limits and concurrent runs; excessive MVP scope.
