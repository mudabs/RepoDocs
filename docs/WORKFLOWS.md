# RepoDocs Workflows

Milestone 1 CLI commands below are implemented. GitHub Actions, hosted UI, and agent workflows remain implementation proposals unless noted otherwise.

## A. Local CLI: SmartCart
1. Install CLI.
2. `repodocs init smartcart`.
3. `repodocs add ./smartcart-frontend` and `repodocs add ./smartcart-backend`, then register the coordination repository that receives generated system documentation with `repodocs add . --id docs --role destination` (or mark a single source repository with `--destination`).
4. `repodocs scan` produces versioned JSON findings with source evidence and uncertainty. The default local snapshot mode includes uncommitted changes and records whether evidence resolves to a commit or to a working-tree fingerprint.
5. Inspect `candidates` in the persisted IR and the scan response's candidate count. Generated Markdown lists every frontend-to-backend match as unconfirmed, with evidence, confidence, and both snapshot revisions. List them with `repodocs candidates`, then run `repodocs confirm <id>` or `repodocs reject <id>`; decisions are stored in `repodocs.confirmations.json` with the evidence and revisions they were made against, and become stale when that evidence changes.
6. Optional AI: review the redacted evidence bundle with `repodocs ai-bundle`, choose a provider with `repodocs ai-configure`, give explicit consent with `--consent-external` before anything leaves the machine, then run `repodocs ai-interpret`. Local Ollama needs no consent, and skipping this step leaves the deterministic output complete.
7. `repodocs generate` produces the complete deterministic architecture/component output and optionally `architecture-ai.md` when current validated claims exist. Stale or invalid AI input is omitted with a warning; refreshing it requires an explicit `ai-interpret`. Interactive scope selection and ERD/API expansion follow in later milestones.
8. `repodocs validate` checks diagrams, references, artifact ownership, and required confirmations; it exits with code 5 while any candidate is unresolved or stale.
9. Optional architecture discovery (ADR-026): `repodocs architecture context` previews the redacted source excerpts a provider would see and contacts nothing; `repodocs architecture discover` runs bounded discovery (external providers also need `--consent-source-excerpts`) and stores a validated view; `generate` then renders it, with an evidence map and an optional offline HTML page, from the stored view and never calls a provider. A stale view is retired with a warning.
10. Review local Markdown and Mermaid files.
11. Full-project regeneration skips a no-op; commit manually or publish system documentation through the coordination repository.

## B. GitHub Actions
1. User commits project config and workflow; grants required repository permissions.
2. Source PR merges in frontend or backend.
3. The coordination repository's Action starts on pinned, clean project revisions using explicit repository credentials: `repodocs scan --snapshot-mode pinned --pin <id>=<sha>` fails closed (exit code 6) on dirty trees, non-Git paths, unpinned repositories, or a checkout that does not match the pin.
4. Analyze changes and dependent facts; initial MVP may safely regenerate all artifacts.
5. Optional configured AI enrichment interprets curated, redacted evidence after consent; local-only mode remains available.
6. Render and validate docs; skip no-op output.
7. `repodocs publish` opens or updates one reviewable documentation PR on a bot-owned branch of the coordination repository, and does nothing when the output equals the base. See `docs/examples/repodocs-publish.yml`.
8. Maintainer reviews and merges. CI fails when required confirmations are unresolved; avoid bot-triggered loops and duplicated PRs.

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
3. Agent invokes the CLI and consumes its versioned JSON output, rather than inventing a separate parser.
4. Agent summarizes uncertain links and asks user for confirmation.
5. Agent invokes generators/validators and reviews diffs.
6. Agent may prepare the coordination-repository workflow/PR only with user approval.
7. Future MCP may expose the same core operations.

## AI access and honest UX
- No account/key: deterministic local output, possibly less semantic depth.
- Local Ollama: no cloud API key but requires installed model and suitable hardware.
- BYO cloud provider: explicit consent, redaction, API key, and network required.
- RepoDocs-managed AI: no provider key for end user, but needs RepoDocs identity, hosted service, quota, and costs.
- Coding-agent AI: can assist via structured interface; do not assume agent subscriptions grant arbitrary API access to RepoDocs.
