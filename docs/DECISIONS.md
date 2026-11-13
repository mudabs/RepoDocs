# Architecture Decision Log

**Status:** Design decisions are conceptual, not evidence of implemented features.

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

## Open decisions: require validation/approval
| ID | Question | Key tradeoff |
|---|---|---|
| O01 | Core language/runtime | Python vs TypeScript vs polyglot analyzers |
| O02 | Exact IR schema and extension mechanism | Generality vs semantic richness |
| O03 | CLI command and configuration schema | UX and backward compatibility |
| O04 | Managed AI availability and pricing | Zero-key convenience vs cost/privacy |
| O05 | Cross-repo triggers and credentials | Reliability vs permissions/complexity |
| O06 | Generated/manual documentation ownership model | Safe updates vs flexibility |
| O07 | Mermaid/PlantUML MVP support matrix | Coverage vs maintenance |
| O08 | Sequence diagram accuracy and evidence threshold | Value vs hallucination risk |
| O09 | Open-source license | Adoption vs commercial options |
| O10 | MVP scope/deadline feasibility | Depth vs breadth |
| O11 | Local vs hosted project state migration | Simplicity vs synchronization |
| O12 | AI agent-provided reasoning contract | Avoid duplicated LLM inference |

## Change process
When a major choice is finalized, add an ADR in `docs/adr/` with context, alternatives, decision, consequences, and date. Update this table. Never silently promote open decisions to accepted.
