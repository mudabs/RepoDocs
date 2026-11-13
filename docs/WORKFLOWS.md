# RepoDocs Workflows

All commands below are **illustrative proposals**, not verified working commands.

## A. Local CLI: SmartCart
1. Install CLI.
2. `repodocs init smartcart`.
3. `repodocs add ./smartcart-frontend` and `repodocs add ./smartcart-backend`.
4. `repodocs scan --format json` produces source-evidenced findings and uncertainty.
5. User confirms or rejects ambiguous frontend-to-backend links.
6. Configure optional AI (managed sign-in, BYO key, or local Ollama), or use deterministic mode.
7. `repodocs generate --interactive` or noninteractive flags selects architecture, API, ERD, component, class docs.
8. `repodocs validate` checks diagrams, references, and artifact ownership.
9. Review local Markdown and Mermaid/PlantUML files.
10. Commit manually or configure GitHub PR publication.

## B. GitHub Actions
1. User commits project config and workflow; grants required repository permissions.
2. Source PR merges in frontend or backend.
3. Action/coordination trigger starts on pinned project revisions.
4. Analyze changes and dependent facts; initial MVP may safely regenerate all artifacts.
5. Optional configured AI enrichment interprets affected evidence.
6. Render and validate docs; skip no-op output.
7. Open or update reviewable docs PR in correct repository/location.
8. Maintainer reviews and merges. Avoid bot-triggered loops and duplicated PRs.

## C. Future hosted/self-hosted web UI
1. Sign in and authorize GitHub App.
2. Create SmartCart project and select both repositories.
3. Worker analyzes pinned revisions; dashboard displays graph and evidence.
4. User resolves uncertain links and chooses diagram/document scope.
5. AI enriches findings if configured; generate and preview docs.
6. Create GitHub docs PRs; opt into webhook-driven future synchronization.

## D. Claude Code / Codex
1. Agent reads `AGENTS.md` and project docs.
2. User asks for project documentation.
3. Agent invokes CLI with structured JSON, rather than inventing a separate parser.
4. Agent summarizes uncertain links and asks user for confirmation.
5. Agent invokes generators/validators and reviews diffs.
6. Agent may prepare workflow/PR only with user approval.
7. Future MCP may expose the same core operations.

## AI access and honest UX
- No account/key: deterministic local output, possibly less semantic depth.
- Local Ollama: no cloud API key but requires installed model and suitable hardware.
- BYO cloud provider: API key and network required.
- RepoDocs-managed AI: no provider key for end user, but needs RepoDocs identity, hosted service, quota, and costs.
- Coding-agent AI: can assist via structured interface; do not assume agent subscriptions grant arbitrary API access to RepoDocs.
