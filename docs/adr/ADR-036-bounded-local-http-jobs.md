# ADR-036: Bounded local HTTP jobs over the shared service

- Date: 2026-11-22
- Status: Implemented for #72 under epic #63; submitted for review.
- Issue: #72; depends on ADR-027 (#64), ADR-030 (#69), ADR-031 (#67), ADR-033 (#71) and ADR-034 (#70).
- Amends: web workflow contract 1.0 (additive), ADR-030 (a per-run consent gate and a `provider-response` progress event) and ADR-031 (an aggregate `max_total_tokens` cap).

## Context

ADR-027 approved a thin FastAPI adapter, polling and bounded single-process jobs. ADR-030 provided the shared service, ADR-033 the access, credential and consent boundary, and ADR-034 the workspaces. Nothing yet connected them: no route existed, the service still read consent only from the project file, and the job contract had no way to report budget diagnostics or an explicit refresh. A browser user must be able to start, observe and cancel one analysis without a second engine, and an interrupted service must never repeat paid work.

## Decision

### Shape

- `repodocs.web.jobs.JobService` holds admission, the queue, workers, job records, restart recovery, revalidation and artifact reads. It imports no web framework, so every behavior is testable without HTTP. Each stage is a call to `repodocs.service` (`scan`, `preview_context`, `discover`, `generate`, `validate`); no analysis logic is copied.
- `repodocs.web.app.create_app` is the only module that imports FastAPI. Routes call `SessionGuard.authorize` first, then read a JSON body bounded by `max_body_bytes` (64 KiB by default; 413 above it), then run the blocking operation in a worker thread. Every response carries `response_headers()`; artifacts are sandboxed attachments. No CORS header is produced, and the generated API documentation routes are disabled. `serve` runs Uvicorn on the `AccessConfig` address.
- FastAPI and Uvicorn are the optional `repodocs[web]` extra. The test extra adds FastAPI and `httpx` so the required CI runs the HTTP tests. The CLI imports neither.

### Routes

| Route | Purpose | Success |
|---|---|---|
| `POST /api/v1/session` | First same-origin contact (`open_session`): HttpOnly cookie, CSRF token in the body | 201 |
| `POST /api/v1/session/end` | End the session and clear its credentials and consent receipts | 204 |
| `POST /api/v1/workspaces` | Acquire a workspace from allowlisted local names or pinned public GitHub sources | 201 |
| `POST /api/v1/workspaces/{id}/preview` | Scan and summarize the redacted context; excerpts only with `include_documents: true` | 200 |
| `POST /api/v1/providers`, `GET .../{id}`, `POST .../{id}/disconnect` | In-memory provider session; only `public_view` is returned | 201, 200, 204 |
| `POST /api/v1/consents`, `POST .../{id}/revoke` | Receipt for the workspace's current preview, or its deletion | 201, 204 |
| `POST /api/v1/jobs` | Submit; an identical repeat returns the original job | 202 new, 200 repeat |
| `GET /api/v1/jobs/{id}` | Poll | 200 |
| `POST /api/v1/jobs/{id}/cancel` | Cancel | 200 when settled, 202 while a running job stops |
| `GET /api/v1/jobs/{id}/artifacts/{name}` | Download one validated artifact the job recorded | 200 |

Errors use one envelope (`kind: error`, a stable code, a sanitized message, `retryable`): 401 `session-required`; 403 `access-denied` or `consent-required`; 404 `not-found` for any unknown or foreign handle, indistinguishable from a missing one; 409 `conflict`, `stale-preview`, `stale-artifact` or `busy`; 413 `too-large`; 422 `invalid-request`, `workspace-failure` or `operation-failure`; 429 `queue-full`; 503 `unavailable`. Messages never contain server paths (they are replaced by `<store>` and `<source:name>`), provider bodies or credentials.

### Admission and the queue

- A request is checked against the 1.0 request contract field by field before anything else. An architecture job additionally needs: the caller's workspace, a `context_hash` equal to that workspace's latest preview (409 otherwise), a live provider session, a `RunGate` that accepts the consent receipt (`external` and `source-excerpts` for an external provider; none for a loopback Ollama or OpenAI-compatible host), and run limits the project limits accept, including a cost cap only with a complete price profile. `max_cost_microunits: 0` is refused for architecture jobs because it allows no request. Admission reports the job's worst-case allowance in `budget` before it runs.
- Idempotency is per owner, workspace and key, over the normalized request (mode, views, limits, refresh and context hash). Provider and consent handles are not part of it, so a reconnected browser repeating a submission gets the original job and no new run. The same key with a different request is 409.
- At most `max_active_jobs` jobs run (default 1, as ADR-027), one per workspace; each workspace may hold one more queued job, and `max_queued_jobs` (default 8) bounds the queue. A workspace keeps at most `max_jobs_per_workspace` (default 50) records. Each is refused with 429, never grown. A preview is refused (409) while a job of that workspace is queued or running, because both write its scan state.
- Operator `run_limits` and `prices` (for example a local model's context window) are written into each new workspace's `ai` settings at creation, so the CLI and the web service apply the same limits to that workspace; a job can only lower them.

### States and stages

- `queued` (stage `queued`) → `preflight` (stages `scan`, `context`: sources and the reviewed context are checked; no provider is built, nothing is sent, no artifact is written) → `running` (stages `discover`, `generate`, `validate`) → one terminal state. `preflight` is added to the 1.0 state enum because #72 requires it as a distinct state; "completed" in the issue is the contract's `succeeded`.
- `succeeded` needs a complete discovery (or none, in deterministic mode) and successful generation and validation. A discovery that ended early is `partial` with code `provider-failure`, or `budget-exhausted` for discovery's own round, request, context or response budgets; a stop on the run's usage ledger is state `budget-exhausted`. A kept earlier complete view after an early-ending refresh is still `partial`, and says so. A provider failure, a refused gate, an operation error or an unexpected exception is `failed` with no result. No failed or incomplete model call becomes `succeeded`.
- After any scan of a workspace, earlier results describing different sources become `stale` (`stale-source`), keeping their result for inspection.
- `sequence` increases on every change, including each provider response, whose ledger snapshot updates `usage` and `budget`. Clients poll as WEB_WORKFLOW.md says; there is no second delivery protocol.

### Consent, credentials and budgets

- `DiscoverRequest.gate` (ADR-030 amendment) is how the service receives a `RunGate`. With a gate the service requires both consent kinds for an external provider, calls the gate at admission, and applies consent for that run only, in memory; it is never written to `repodocs.json`. A `_GatedProvider` inside the run ledger sends every model and token-count request through `gate.call`, and the ledger's `before_send` hook also calls it, so a revoked, expired or mismatched receipt is refused before a count, a reservation or a request. A retry or repair is a new call.
- The provider is built at the start of `discover` from the gate's `ProviderContext` by `build_provider`: Anthropic and OpenAI receive the session's key and host explicitly (`AnthropicProvider` gained `api_key` and `base_url` for this; the CLI still leaves both unset), so the request goes only to the host consent was given for and the environment is never consulted. An Ollama session with a credential is refused, and so is a remote Ollama `/v1` gateway, before any request: authenticated gateways (#62) are CLI-only, because their key is the operator's `REPODOCS_OLLAMA_API_KEY` and a browser session must never use it or send it to a host the session chose. A loopback `/v1` host still works and is sent no credential. `build_provider` never reads that variable; with #62 the Ollama provider reads it only when the CLI explicitly selects the environment credential source (ADR-020), so the web service does not depend on its own check alone. The provider object, and with it the credential, is dropped when the run ends. A credential found in a provider's error text is replaced before the message is stored.
- `RunLimits.max_total_tokens` (ADR-031 amendment) caps prompt and output tokens together, in the reservation and in reported usage. It has no default and can only be lowered; a run stopped by it reports `total-token-limit`. The job's `max_total_tokens` maps to it, `max_requests` to `max_requests` and `max_cost_microunits` to `max_cost_usd`.
- `usage` is null and `unknown` until a run reports; `known` zero only when no request could have started; `estimated` when any request has no complete usage, which keeps its reservation (an upper bound). Cost is null without an applicable price profile, never zero.

### Cancellation and restart

- A worker claims a job in one step under the service lock: it leaves the queue, its workspace becomes busy and its state becomes `preflight` (stage `scan`) together, so a job is `queued` exactly while it is in the queue. A worker claims nothing once the service is closing.
- Cancelling a queued job settles it at once with known zero usage. A claimed job's cancellation is checked before any work, so it ends `cancelled` without scanning, building a provider or writing an artifact. A job that has ended is never moved again: a later stage transition stops the run instead, and late progress cannot rewrite the record, so a `cancelled` record stays `cancelled`. A running job's token is checked before every later stage, token count and model request, and before the view and artifacts are committed. A request already in flight finishes or reaches the provider's own timeout; its usage stays in the receipt and is not refunded. Upstream connections are opened per request and closed when it returns; an in-flight HTTP request is not aborted.
- Records are written atomically under `<workspace>/jobs/`: the public job, the owner's public id, the idempotency key, the request hash and the recorded artifact hashes. No credential, handle, prompt, response or source text. On start, a queued, preflight or running record becomes `interrupted` (`retryable: false`) with its stage in the message; one interrupted during `discover` has `unknown` usage, because a request may have been in flight. Nothing is resumed or retried, and repeating the submission returns the interrupted record. Records that are malformed, linked or belong to another workspace are ignored.

### Artifacts and source bundles

- A result lists the artifacts of the requested views that validation reported and the manifest owns (`overview`: `architecture.md`, `architecture.mmd`; `components`: `architecture-view*`; `erd`: `erd*`). A requested view with no output adds a warning, never an invented artifact.
- A download looks its name up in the job's own record, never joining request text to a path. The file must be a regular file in the workspace output directory with no link on the way, at most `max_download_bytes` (16 MiB), and both its bytes and the manifest must still match the hash the job recorded. Otherwise it is `stale-artifact`. The context bundle, the IR and project files are never artifacts. The preview returns counts and hashes only; redacted excerpts are returned only on explicit request.

## Alternatives

- Server-sent events: ADR-027 chose polling for the first release; a second protocol was not added.
- Writing web consent into `repodocs.json` so the unchanged service accepts it: persists a decision ADR-033 keeps in memory and would widen later CLI runs.
- Mapping `max_total_tokens` onto separate input and output caps: either cap alone can let the total reach twice the request.
- A durable task broker or database for jobs: not needed for one local process (ADR-027).
- Rebuilding a provider for every request from the gate: stronger credential hygiene, but every provider would need a per-request client; the run-scoped object is dropped when the run ends.

## Consequences and limitations

- No frontend (#73), installer, command-line entry point, retention schedule or health check (#76) is included; `create_app` and `serve` are the entry points.
- Sessions are in memory (ADR-033). After a restart, earlier records and artifacts stay on disk and are reported `interrupted` or as they ended, but a new browser session cannot reach them; workspace retention (ADR-034) removes them.
- An in-flight model request cannot be aborted, and a provider may bill for it.
- Jobs are bounded by stage checks, the run ledger, discovery's rounds and the providers' request timeouts, not by a separate wall-clock limit. A preview runs inside its request, bounded by the workspace's source limits.
- `views` select which artifacts a result references; the engine still generates its full set. Scoped views are #74, interactive evidence and the remaining downloads #75.
- `ai-interpret` and `publish` are not exposed over HTTP.
- Tests use replayed providers and dummy credentials only. Live model quality is unmeasured here; #54 and #77 remain the gate.

## Amendment (2026-11-27, #95)

ADR-040 adds an optional web-only SQLite store for local profiles, hashed session authenticators and saved workspace/job history, so a legitimate owner keeps their work across browser and server restarts. Provider credentials and consent receipts remain in memory only, job files remain authoritative, and nothing resumes inference after a restart.
