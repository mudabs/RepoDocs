# RepoDocs Roadmap and Validation Checklist

**Target:** November 30, 2026 MVP, high schedule risk. Do not interpret this as a delivery commitment.

## Phase 0: validate before coding
- Independent architecture review and threat model.
- Resolve the remaining CLI/configuration, credential, agent reasoning, and hosted-state questions; the approved baseline is recorded in `docs/DECISIONS.md`.
- Define the versioned JSON IR, repository/project snapshots, evidence and claim states, plugin protocols, ownership rules, AI consent/redaction boundary, and publication policy.
- Define contracts: project config, IR/evidence, analyzer plugin, generator plugin, sync/publication policy, security boundary.
- Create representative SmartCart fixture repos and expected facts, evidence, diagrams, and AI comparison baselines.

## Phase 1: smallest end-to-end vertical slice
- CLI reads one Spring Boot repo and pinned revision.
- A trusted built-in Python adapter extracts endpoints/services/entities with evidence.
- Versioned JSON IR serializes locally with semantic IDs, evidence, and hashes.
- One generator creates API Markdown and a Mermaid architecture/component diagram with deterministic output.
- Validator checks diagram syntax, provenance, required confirmations, and the artifact manifest.
- Tests prove reproducibility, deletions, no-op detection, evaluation metrics, and manual-file safety.

## Phase 2: multi-repo and AI
- Add the React repository and cross-repo candidate matching for the SmartCart system.
- Persist confirmation overrides.
- Add AI adapter with structured evidence-constrained responses, one cloud BYO provider, and Ollama.
- Add Mermaid ERD generation only when evidence supports it.

## Phase 3: GitHub automation
- GitHub Action runner integration and permissions.
- Coordination-repository config for multi-repo projects.
- Docs PR creation, no-op detection, loop prevention, concurrency controls.
- CI failure for unresolved required confirmations.
- End-to-end SmartCart change-to-docs-PR demonstration.

## Phase 4: post-MVP
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
Produce a short architecture critique and an ordered implementation proposal. Ask for approval before making major decisions or creating a large issue backlog.
