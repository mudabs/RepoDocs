# RepoDocs Architecture

**Status:** Approved design baseline; implementation remains subject to the contracts below.

## System shape
A modular monolith with a reusable core engine. Interfaces are adapters, not separate analysis implementations.

```text
CLI / GitHub Action / Coding Agent / Local Web UI (planned)
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
Read local working trees or pinned Git snapshots. Local CLI scans (`--snapshot-mode local`) include uncommitted changes and record the Git revision, branch, head state, dirty state, and a working-tree content fingerprint. Pinned scans (`--snapshot-mode pinned`) for GitHub Actions accept only clean checkouts of one explicitly pinned commit and reject unpinned repositories and stale or abbreviated pins. Every snapshot and every evidence record states whether its revision is a commit SHA or a content fingerprint (ADR-014). Inventory files and dependencies; track exact repository revisions, dirty-tree state where applicable, source commits/diffs, and content hashes. Exclude binaries, vendored dependencies, generated output. Never execute arbitrary repository code during normal static analysis.

### Analysis
- L1: generic manifests, config, CI/CD, framework signatures.
- L2: language syntax/semantic parsing, e.g. Tree-sitter and specialist tooling.
- L3: framework adapters, with deeper Spring Boot and React analysis in Milestone 2. FastAPI remains an extension target.
Separate language parsers from framework adapters. Dynamic dispatch, reflection, runtime configuration, and generated code limit completeness.

Built-in analyzers follow the contract in ADR-015: `detect(context)` decides whether an analyzer applies to a repository, `analyze(context)` contributes nodes, relationships, evidence, claims, and limitations, and the context gives hash-checked, read-only access to file text. Evidence is whole-file or a line span with an optional symbol. Facts read literally from source are `observed`; name matches are `inferred`. Constructs an analyzer cannot support statically are recorded in the IR `limitations` list and rendered in documentation, never promoted to facts. Configuration analyzers record keys only, never values. The Spring Boot adapter (`repodocs.analyzers.spring_boot`) is implemented on this contract with a structural Java declaration scanner; its measured depth is described in the README and ADR-015. The React adapter (`repodocs.analyzers.react`) uses a JavaScript and TypeScript scanner that masks comments and strings, and records modules, components, routes, and literal-URL request sites as described in ADR-016; those request sites are the front-end input to cross-repository correlation. The PostgreSQL adapter (`repodocs.analyzers.postgresql`) applies the data-definition statements it supports from SQL migrations and schema files, in migration order, and records databases, tables, columns, keys, and foreign keys as described in ADR-017; the linking step (`repodocs.analyzers.orm`) connects a persistence entity to the table it declares as an `inferred` edge, never as an observed one.

### System IR
The System IR is a versioned JSON graph with project, repository, service, module, class, function, endpoint, database, infrastructure, and external-service nodes plus typed edges such as contains, calls, imports, exposes, reads, writes, deploys, and depends_on. Semantic IDs identify entities independently from source evidence and content hashes. Each claim records evidence IDs, repository revision, file/symbol/line where available, analyzer and model provenance, classification, confidence, and confirmation or override provenance. Store the canonical form locally; hosted mode may persist the same structures in PostgreSQL.

### Correlation
Match cross-repository relationships using routes, base URLs, API schemas, config, clients, and deployment evidence. Produce candidate links where ambiguous. Only explicit confirmation or direct evidence can make a relationship authoritative. Persist candidates, confirmations, rejections, and stale states in project data tied to the participating repository revisions.

The implemented HTTP correlator (ADR-018) runs after repository analysis and stores every discovered match, even unique high-confidence ones, in a separate `candidates` array. Matching requires method/path agreement and preserves competing backend endpoints. A literal client origin strengthens a candidate only when that endpoint has matching OpenAPI server evidence. Candidate IDs use source/target semantic IDs; evidence, confidence, and both repository revisions are stored separately. The OpenAPI analyzer supports 3.0.x/3.1.x JSON, paths/operations, literal servers, and same-document JSON Pointer references only. No remote or filesystem reference is followed. URL userinfo, queries, and fragments are removed before persistence. Decisions (ADR-019) are recorded noninteractively with `confirm` and `reject` in `repodocs.confirmations.json`, bound to the candidate's evidence and both snapshot revisions. A candidate is `candidate`, `confirmed`, `rejected`, or `stale`; a decision goes stale when its evidence changes, and `validate` exits with code 5 while any candidate is unresolved or stale.

### AI
Provider-neutral adapter. Supply curated, redacted evidence bundles only after explicit consent for external providers; local-only mode never sends repository content externally. Request schema-constrained outputs and validate evidence references and claims before documentation generation. The MVP supports one cloud BYO provider and Ollama. Managed RepoDocs AI is deferred and requires separate service costs, identity, quota, and privacy decisions.

Implemented (ADR-020): `ai-bundle` builds a deterministic, schema-versioned evidence bundle from the IR alone, with no source text and with secret redaction; `ai-interpret` sends it to Anthropic (official SDK, optional extra) or Ollama through one provider protocol and stores validated claims with provider and model provenance beside the IR, never inside it. External use requires recorded consent, so a project without it is local-only. Claims citing unknown evidence or subjects are rejected, and claims without evidence or about unconfirmed candidates are only ever `proposed`.

ADR-026 (implemented as `repodocs architecture context` and `architecture discover`, with generation and validation of the stored view) defines a separate architecture-discovery path for applications that no analyzer understands: a bounded, redacted source-excerpt context bundle under its own consent, a controlled resolver for further file requests, and a schema-validated inferred architecture view (`schemas/architecture-view-1.0.json`) that drives separate diagram files. The view is `proposed` or `inferred`, never `observed`, and cannot change the IR, candidates, or confirmations; a confirmed candidate supports a cross-repository connection only when it is bound to that connection's actual components, direction, and evidence on both sides. Schema validation checks shape; semantic validation against the IR is separate. Deterministic mode is unchanged and has no application-component model for an unsupported stack.

ADR-021 adds optional `architecture-ai.md` generation from current, revalidated claims. The deterministic overview and diagrams stay unchanged, and no model output changes graph authority or candidate decisions. Subject-bound evidence is required for inference; multi-repository claims must explicitly cite a confirmed candidate. Stale or invalid optional claims are omitted with a warning, and owned unedited obsolete AI artifacts are retired. Model prose is escaped, and validation checks the artifact against current claims. Structural validation and golden-fixture contribution checks do not prove arbitrary model wording true.

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
Store repository-local docs in owning repos and system-wide docs in one configured coordination repository. In project configuration this is the single documentation destination: a `source` repository or one `destination`-only repository, with a dedicated output directory inside it (ADR-014). Generated artifact manifests track ownership, hashes, source SHAs, versions, and dependencies. Preserve human-authored files and fail on unexpected edits. The GitHub Action creates reviewable PRs in the coordination repository, suppressing empty PRs and bot loops.

## Deployment modes
- CLI: local core engine, local config/cache/IR; no mandatory server or GitHub login; versioned JSON output and explicit exit codes.
- GitHub Action: runs the same CLI on a runner; multi-repo access requires explicit credentials and the coordination repository. CI fails when required relationship confirmations remain unresolved. Implemented as a composite action over `scan --snapshot-mode pinned`, `generate`, `validate`, and `publish` (ADR-023): sources are checked out by SHA, the artifact manifest is committed, and publication builds one commit on a bot-owned branch, skipping any run whose output equals the base.
- Coding agents: noninteractive CLI/JSON; skills first, MCP later.
- Local web first (specified; the access/credential/consent boundary `repodocs.web.security` is implemented per ADR-033, the HTTP adapter and UI are not): React/TypeScript with Vite, served by an optional FastAPI/Uvicorn adapter over the same Python application service. One process, bounded jobs, polling and atomic local job metadata; no required PostgreSQL, Redis or external task broker. Loopback by default, ephemeral in-memory credentials, explicit provider/source consent, and no automatic inference retry on restart. Public hosting is deferred. See ADR-027 and `WEB_WORKFLOW.md`; CLI and Actions remain backend-independent.

## Security boundaries
Untrusted repository inputs; file/path traversal protection, resource limits, sandboxing where necessary, no arbitrary builds, prompt-injection resistant AI input, secret redaction, explicit consent before external inference, local-only mode, least-privilege GitHub App/token, safe Markdown/SVG rendering, tenancy separation in hosted mode.

Implemented (ADR-025): scanning disables the git settings a repository can use to run programs (`core.fsmonitor`, filter commands, hooks), inspects submodules with the same hardening, gives content provenance (and refuses pinned mode) when a submodule, an ignored analyzed file, or anything else differs from the commit, and stops when git state cannot be read; links that leave a repository, including Windows junctions, are skipped; unparseable or hostile file contents skip that file, not the scan; text from analyzed source is escaped or fenced in generated Markdown and Mermaid, and `validate` rejects active Markdown and Mermaid directives; loopback AI requests ignore proxies and neither provider follows a redirect (tested against local servers); and redaction covers more token shapes and prefixed secret keys. Not implemented: sandboxing for external analyzers, redaction of the deterministic documents, and parse time limits.

## Quality evaluation

Implemented as `repodocs.golden` (ADR-024): reviewed golden cases under `tests/golden`, one versioned JSON report, and an exact committed baseline that the test suite enforces. Golden files are written from fixture sources, not tool output, and are updated by hand.

Representative golden fixtures define expected facts and relationships. Evaluation reports fact-level precision and recall, cross-repository correlation accuracy, unsupported-claim rate, evidence resolution, diagram validity, output stability across unchanged scans, and the incremental contribution of AI compared with deterministic-only generation. Human review may assess readability, but appearance alone is not an accuracy criterion.

## Major failure modes
Inaccurate dynamic code inference; cross-repo false matches; stale multi-repo snapshots; hallucinated AI steps; malformed or overcrowded diagrams; nondeterministic PR churn; overwritten manual docs; leaked secrets; fork-PR token exposure; expensive AI usage; GitHub rate limits and concurrent runs; excessive MVP scope.
