# RepoDocs Roadmap and Validation Checklist

**Target:** November 30, 2026 MVP, high schedule risk. Do not interpret this as a delivery commitment. Milestone 1 implementation is in progress.

## Phase 0: contracts and threat model
- Independent architecture review and threat model.
- Resolve the remaining CLI/configuration, credential, agent reasoning, and hosted-state questions; the approved baseline is recorded in `docs/DECISIONS.md`.
- Define the versioned JSON IR, repository/project snapshots, evidence and claim states, plugin protocols, ownership rules, AI consent/redaction boundary, and publication policy.
- Define contracts: project config, IR/evidence, analyzer plugin, generator plugin, sync/publication policy, security boundary.
- Create representative SmartCart fixture repos and expected facts, evidence, diagrams, and AI comparison baselines.

## Phase 1: foundation and first working CLI
- Python package provides `init`, `add`, `scan`, `generate`, and `validate`.
- Local CLI scans working trees, including uncommitted changes; pinned scans accept only clean exact commits for automation.
- Project configuration models source repositories and one documentation destination, which may be a source repository or a separate destination repository.
- Safe file discovery, language detection, basic manifest analysis, and content fingerprints run without executing repository code.
- Versioned JSON IR serializes locally with semantic IDs, evidence, and hashes.
- Deterministic Markdown and one Mermaid architecture/component diagram are generated with ownership protection.
- Tests cover reproducibility, no-op detection, provenance, manual-file safety, scanner safety limits, snapshot modes, destination ownership, and schema conformance, and run in CI on Linux and Windows for every pull request.

## Phase 2: framework and multi-repository analysis
- Analyzer contract with span evidence and reported limitations (ADR-015), the Spring Boot adapter, and the React adapter (ADR-016) are implemented.
- Supported PostgreSQL static schema extraction is implemented (ADR-017): databases, tables, columns, keys, foreign keys, reported unsupported constructs, and inferred ORM table links.
- React/Spring Boot cross-repository HTTP candidate discovery is implemented with OpenAPI 3.0.x/3.1.x JSON input, semantic candidate IDs, snapshot/evidence bindings, conservative origin matching, URL sanitization, and explicitly unconfirmed documentation (ADR-018).
- Confirmation and rejection decisions are persisted with evidence and revision bindings, stale detection, and the exit code 5 validation failure (ADR-019).
- Mermaid ERD generation and validation with a built-in syntax validator (not the Mermaid renderer; renderer validation is still open under epic #8) are implemented (ADR-022): deterministic `erd.mmd` generation from PostgreSQL table facts, crow's-foot cardinality, identifying relationships, reported missing keys and unresolved references, and ownership protection.

## Phase 3: AI integration
- Evidence-bundle construction, redaction, structured claim validation, the Anthropic and Ollama adapters, recorded consent, and deterministic fallback are implemented (ADR-020).
- Current validated claims produce optional AI architecture explanations with provenance, subject-bound citations, safe deterministic fallback, and no graph or confirmation changes (ADR-021).
- Golden fixtures measure bounded deterministic-versus-AI contribution and reviewed unsupported-inference counts. Real-provider semantic accuracy and broader precision/recall evaluation remain follow-up work.

## Phase 3b: architecture discovery (epic #47)
- Contracts for architecture views, bounded source context, consent, failure semantics, and the threat model are defined (ADR-026, #48). Implementation proceeds in order: context (#49), discovery (#50), rendering with a real Mermaid check (#51), source navigation (#52), CLI integration (#53), and an evaluation on DigitalBoneBox and a contrasting stack with an owner go/no-go decision (#54).
- Implemented so far: the bounded, redacted source context and the separate source-excerpt consent (#49), replay-tested discovery with the Anthropic and Ollama adapters, validation, storage, and revalidation (#50; no CLI command yet), deterministic Mermaid architecture views with a pinned real-renderer check in CI (#51), and pinned source links, an evidence map, and an optional static HTML export (#52). and the CLI workflow `architecture context`, `architecture discover`, generate, and validate with stale, tamper, ownership, and publication handling (#53). Not started: the evaluation on real stacks (#54), so usefulness and live-model quality are unmeasured. Live-model quality is unmeasured, and replayed tests do not prove it.

## Phase 4: GitHub automation
- GitHub Action runner integration for pinned clean revisions, `repodocs publish`, no-op detection, loop prevention, concurrency controls, and the failing check for unresolved confirmations are implemented (ADR-023).
- Coordination-repository config for multi-repo projects uses the existing destination configuration with sources checked out under `sources/`.
- Not yet demonstrated: a real documentation pull request opened on GitHub, which needs a token and a coordination repository, and the end-to-end SmartCart change-to-docs-PR run.

## Next delivery: local web-first diagram baseline (epic #63)
- #64 specifies workflow, shared-engine contracts, quality rubric and ADR-027; no runtime web code is included in the spike.
- After contracts: #65 grounding, #66 useful context, #67 aggregate token/cost limits, #69 shared application service; #68 provider integration follows usage contracts.
- #71's access, credential and consent boundary (`repodocs.web.security`, ADR-033) is implemented as a pure-Python module; #69 and #72 integrate it.
- #70 safe sources and #71 local access/credential protection precede #72 bounded HTTP jobs and #73 browser workflow. #74 scoped views runs alongside this once its dependencies land.
- #75 interactive evidence/downloads, #76 packaging and #77 CLI/web parity complete the local baseline; #54 remains the existing real-source quality gate and owner go/no-go.
- Each issue ships in a focused PR with relevant tests and `Closes #<issue>` once acceptance criteria are met; verify issue closure and board status after merge to main.

## Phase 5: post-MVP
- Hosted managed AI with quotas, privacy and cost controls.
- Public hosted web service, accounts, payments and videos. No public service is implied by the local/self-hosted plan.
- More framework adapters, advanced UML, sequence verification, MCP, interactive diagrams, sophisticated incremental impact tracking.

## Acceptance criteria
- Evidence links resolve to correct file and commit.
- Uncertain cross-repo matches are not presented as observed.
- Diagram source renders and has stable output between identical runs.
- Golden-fixture precision/recall and AI comparison results are recorded.
- No arbitrary repo code execution during ordinary scanning.
- No AI key or backend needed for deterministic CLI; external AI requires consent and redaction.
- Generated changes do not silently overwrite human docs.
- Automation proposes a PR only when content changes and does not loop.
- All advertised limitations are visible in output.

## Agent's next action
Complete and review #64 before dependent local web implementation. Preserve the existing CLI/Actions contracts and the unresolved real-model quality acceptance in #54.
