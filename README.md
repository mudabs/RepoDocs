# RepoDocs: Architecture Handoff

This folder is a **planning starter kit**, not an implemented application. Its purpose is to transfer design context to ChatGPT Projects, Codex Desktop, Claude Code, and other coding agents.

## Start here
- `AGENTS.md`: persistent coding-agent instructions.
- `docs/PRODUCT_REQUIREMENTS.md`: vision, scope, acceptance goals.
- `docs/ARCHITECTURE.md`: core modules, IR, AI, diagrams, security.
- `docs/DECISIONS.md`: accepted and unresolved decisions.
- `docs/WORKFLOWS.md`: CLI, GitHub, web, and agent scenarios.
- `docs/ROADMAP.md`: phased implementation and validation.

## Setup
1. Copy this folder into your local development directory.
2. Open it in Codex Desktop as a workspace.
3. Initialize Git and connect a remote repository when ready.
4. In a new Codex chat: "Read AGENTS.md and all docs. Audit the design and propose Phase 0 contracts and the smallest vertical slice. Do not implement or create issues yet."
5. For ChatGPT Projects, upload the Markdown files as project files or make the repository accessible through a supported connection. ChatGPT does not automatically monitor the local folder.

## Keeping context current
When decisions change, update `docs/DECISIONS.md` and affected specifications. Commit them to Git. These files, not remembered conversation fragments, should be the authoritative source.
