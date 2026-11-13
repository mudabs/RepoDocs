# RepoDocs Roadmap and Validation Checklist

**Target:** November 30, 2026 MVP, high schedule risk. Do not interpret this as a delivery commitment.

## Phase 0: validate before coding
- Independent architecture review and threat model.
- Resolve core runtime, IR schema, initial diagram formats, ownership rules, and AI modes.
- Define contracts: project config, IR/evidence, analyzer plugin, generator plugin, sync/publication policy, security boundary.
- Create representative SmartCart fixture repos and expected evidence/diagrams.

## Phase 1: smallest end-to-end vertical slice
- CLI reads one local repo and pinned revision.
- One framework adapter extracts endpoints/services/entities with evidence.
- Versioned IR serializes locally.
- One generator creates Markdown + Mermaid diagram with deterministic output.
- Validator checks diagram syntax and artifact manifest.
- Tests prove reproducibility, deletions, and manual-file safety.

## Phase 2: multi-repo and AI
- Add second repo and cross-repo candidate matching.
- Persist confirmation overrides.
- Add AI adapter with structured evidence-constrained responses, BYO provider and Ollama.
- Add diagram types only when evidence supports them.

## Phase 3: GitHub automation
- GitHub Action runner integration and permissions.
- Coordination config for multi-repo projects.
- Docs PR creation, no-op detection, loop prevention, concurrency controls.
- End-to-end SmartCart change-to-docs-PR demonstration.

## Phase 4: post-MVP
- Hosted managed AI with quotas, privacy and cost controls.
- Web dashboard and hosted/self-hosted workers.
- More framework adapters, advanced UML, sequence verification, MCP, interactive diagrams, sophisticated incremental impact tracking.

## Acceptance criteria
- Evidence links resolve to correct file and commit.
- Uncertain cross-repo matches are not presented as observed.
- Diagram source renders and has stable output between identical runs.
- No arbitrary repo code execution during ordinary scanning.
- No AI key or backend needed for deterministic CLI.
- Generated changes do not silently overwrite human docs.
- Automation proposes a PR only when content changes and does not loop.
- All advertised limitations are visible in output.

## Agent's next action
Produce a short architecture critique and an ordered implementation proposal. Ask for approval before making major decisions or creating a large issue backlog.
