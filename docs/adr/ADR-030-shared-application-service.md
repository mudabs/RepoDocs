# ADR-030: One application service for the CLI and in-process adapters

- Date: 2026-11-20
- Status: Implemented in #69 under epic #63.
- Issue: #69; epic #63
- Builds on: ADR-027 (the seam it names), ADR-010 (CLI output and exit codes), ADR-006/ADR-008 (ownership and no-op), ADR-026 (discovery and consent).

## Context

ADR-027 requires the CLI and the browser workflow to call one Python implementation. Before this change each CLI handler held its own scan, context, discovery, generation and validation logic, so a web adapter could only copy it or shell out.

## Decision

- `repodocs.service` holds `scan`, `preview_context`, `discover`, `generate` and `validate`. Each takes a frozen request dataclass and returns a `WorkflowResult` or raises the existing `RepoDocsError` subclass. The CLI handlers parse arguments, call the service and print `result.data` unchanged; JSON schema 1.0 and exit codes are preserved. Analyzers, the renderer, discovery and validation are called, not rewritten.
- The service prints nothing, never exits, starts no server and imports no web framework. CLI and Actions still need no backend.
- A provider is an object in the request (`DiscoverRequest.provider`), built by the caller for that run. The service reads no credential from the environment and changes no process-wide state, so concurrent runs in one process cannot see each other's credentials. `ContextRequest.provider` only labels whether the provider is external and is never called. Discovery still re-checks source-excerpt consent for an injected provider.
- `generate`, `validate` and `preview_context` take no provider that they call; only `discover` contacts one.
- `RunHooks` carries an optional `CancellationToken` and a progress callback receiving `ProgressEvent(operation, stage)`. Cancellation is cooperative: it is checked at stage boundaries and before each provider request, and a discovery cancelled after a request stores no view. `generate` checks it before any file is written, so a cancelled run leaves the previous output and manifest unchanged. A request already in flight is not aborted and billing is not claimed stopped.
- `WorkflowResult.status` is `complete`, `partial` (discovery ended incomplete, including a discarded partial replacement), `failed` or `cancelled`. `view_status` separately reports the stored architecture view as `missing`, `current`, `stale` or `invalid`. `execute(operation, request, hooks)` returns an expected failure or cancellation as a result with a stable `error.code` (`snapshot`, `ownership`, `confirmation`, `publication`, `provider`, `validation`, `usage`, `cancelled`) and the CLI exit code; unexpected exceptions still propagate. `to_dict()` yields a versioned (`service_version` 1.0) JSON-safe form.
- Working-tree and pinned-revision behavior, ownership manifests, no-op detection, source evidence and the confirmation gate are unchanged because the same code runs.

## Not decided here

- Budget enforcement and provider configuration (#67, #68), HTTP routes, job queueing, job statuses such as `queued` or `budget-exhausted`, consent receipts and idempotency (#72), and `ai-interpret`/`publish`, which stay in the CLI until a web workflow needs them. `WorkflowResult` is a service contract, not the web job schema; the adapter maps it.

## Consequences

- A second adapter calls the same functions and gets the same IR, view and artifacts for the same inputs and provider replay; tests assert byte parity with the CLI.
- Because `ai-interpret`, `publish` and candidate decisions remain in the CLI, a web workflow that needs them requires a follow-up extraction.
