# Architecture Decision Log

**Status:** Approved design baseline; Milestone 1 implementation is in progress.

## Accepted design baseline
| ID | Decision | Rationale |
|---|---|---|
| D01 | Hybrid deterministic analysis and optional AI interpretation | Evidence plus richer explanation |
| D02 | One shared engine for CLI, GitHub Action, agents, web | Prevent drift and duplication |
| D03 | CLI + GitHub Action first | Fast, low-friction adoption |
| D04 | No mandatory persistent backend/database for CLI/Action | Local/self-hostable workflow |
| D05 | Project supports multiple repos and services | Real system architecture |
| D06 | Versioned, evidence-backed IR | Traceability and reusable generators |
| D07 | Confirm ambiguous cross-repo relationships | Avoid false claims |
| D08 | Type-specific deterministic diagram generators | Stable, valid output |
| D09 | Git-based docs and reviewable PR publication | Review, ownership, history |
| D10 | Modular monolith with replaceable boundaries | MVP simplicity, future flexibility |
| D11 | Language parsers separate from framework adapters | Extensibility |
| D12 | Initial ecosystems: Spring Boot, React, FastAPI | Focused deep support |
| D13 | AI-first preferred UX; no-key deterministic/local alternatives | Quality with choice |
| D14 | Agent-friendly noninteractive JSON CLI | Codex/Claude Code integration |
| D15 | System IR is a versioned JSON graph of nodes, edges, evidence, and claims | Portable, inspectable contract for all engine stages |
| D16 | Semantic identities are separate from source evidence and content hashes | Stable references without conflating identity, location, and bytes |
| D17 | Cross-repository relationships remain candidates until explicitly confirmed | Prevents inferred links from becoming authoritative silently |
| D18 | MVP AI providers are one cloud BYO provider and Ollama | AI remains central while keeping provider infrastructure replaceable |
| D19 | External AI requires explicit consent, redaction, and a local-only mode | Protects private repository content and preserves deterministic operation |
| D20 | Trusted built-in analyzers are Python modules; external analyzers use a versioned subprocess protocol | Fast MVP iteration with a language-neutral extension boundary |
| D21 | Generated documentation uses dedicated paths, a manifest, and fails on unexpected edits | Protects human-authored documentation from silent overwrites |
| D22 | Mermaid is the first diagram format for architecture/component and ERD; API documentation is Markdown | Limits rendering complexity while covering the MVP demo |
| D23 | Changes use full-project regeneration with no-op detection | Predictable output before incremental impact analysis exists |
| D24 | System documentation and documentation PRs use one coordination repository | Gives multi-repository publication one explicit ownership location |
| D25 | CLI output is versioned JSON with explicit exit codes; CI fails on unresolved required confirmations | Reliable automation and safe handling of ambiguity |
| D26 | Quality is evaluated with golden fixtures, precision/recall, provenance checks, and AI comparisons | Makes factual accuracy and AI contribution measurable |
| D27 | Development proceeds from contracts to one Spring Boot slice, then multi-repository AI-enhanced MVP | Controls risk without reducing the final MVP goal |
| D28 | RepoDocs is licensed under Apache 2.0 | Permissive adoption with an explicit patent grant |

## Remaining open decisions: require validation/approval
| ID | Question | Key tradeoff |
|---|---|---|
| O03 | Exact CLI command and configuration schema | UX and backward compatibility; output and exit semantics are settled by D25 |
| O04 | Managed AI availability and pricing | Zero-key convenience vs cost/privacy; deferred beyond the MVP |
| O05 | Exact cross-repo triggers and credentials | Reliability vs permissions/complexity; the coordination repository is settled by D24 |
| O08 | Sequence diagram accuracy and evidence threshold | Value vs hallucination risk |
| O10 | MVP fixture depth and deadline feasibility | Depth vs breadth; the milestone strategy is settled by D27 |
| O11 | Local vs hosted project state migration | Simplicity vs synchronization |
| O12 | AI agent-provided reasoning contract | Avoid duplicated LLM inference |

## Change process
When a major choice is finalized, add an ADR in `docs/adr/` with context, alternatives, decision, consequences, and date. Update this table. Never silently promote open decisions to accepted.
