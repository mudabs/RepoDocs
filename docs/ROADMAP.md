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
- Local CLI scans working trees, including uncommitted changes.
- Safe file discovery, language detection, basic manifest analysis, and content fingerprints run without executing repository code.
- Versioned JSON IR serializes locally with semantic IDs, evidence, and hashes.
- Deterministic Markdown and one Mermaid architecture/component diagram are generated with ownership protection.
- Tests cover reproducibility, deletions, no-op detection, provenance, and manual-file safety.

## Phase 2: framework and multi-repository analysis
- Add Spring Boot and React adapters with deeper framework-aware extraction.
- Add supported PostgreSQL static schema extraction.
- Add the React repository and cross-repo candidate matching for the SmartCart system.
- Persist confirmation overrides and add Mermaid ERD generation when evidence supports it.

## Phase 3: AI integration
- Add evidence-bundle construction and structured claim validation.
- Add one cloud BYO provider, Ollama, explicit consent, redaction, and deterministic fallback.
- Measure deterministic-versus-AI output using golden fixtures.

## Phase 4: GitHub automation
- GitHub Action runner integration and permissions for pinned clean revisions.
- Coordination-repository config for multi-repo projects.
- Docs PR creation, no-op detection, loop prevention, concurrency controls.
- CI failure for unresolved required confirmations.
- End-to-end SmartCart change-to-docs-PR demonstration.

## Phase 5: post-MVP
- Hosted managed AI with quotas, privacy and cost controls.
- Web dashboard and hosted/self-hosted workers.
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
Complete Milestone 1, validate it with fixture repositories, then report the remaining gaps before beginning framework-aware analysis.
