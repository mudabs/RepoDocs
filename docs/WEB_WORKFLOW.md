# Local web-first workflow contract 1.0

This contract (#64, epic #63) is implemented on the backend by the local HTTP jobs adapter (#72, ADR-036, the optional `repodocs[web]` extra); the browser UI (#73) is not built yet. The existing CLI remains usable independently. See ADR-027 and `schemas/web-workflow-1.0.json`.

## Browser journey

1. Open the locally served UI. Register one or more sources through an allowlisted local root or a public GitHub repository at an exact revision (#70). A pasted `https://github.com/<owner>/<repo>` URL is resolved server-side to its default branch head and pinned to that exact SHA before acquisition (#96); explicit owner, repository and SHA remain available under Advanced. A browser cannot select an arbitrary server path. Local sources include working-tree changes; fetched sources are pinned clean revisions.
2. Scan through the shared engine and display each snapshot, limitations and unresolved cross-repository candidates. Select one documentation destination; downloads use a backend-owned export directory until publication is explicitly configured.
3. Select deterministic mode or a user-connected provider/model. Enter a credential only when the provider needs it. A provider-session handle refers to a backend in-memory credential; no key goes into localStorage, URLs, job JSON, logs or `repodocs.json`.
4. Preview the redacted context, full serialized request size, omissions and limits. These operations contact no model. Budget estimates are labeled estimates; unknown usage/cost is null, never zero.
5. Give provider/host/model/policy-bound external and source-excerpt consent when needed. A preview hash binds the request to the reviewed content. Revoking consent invalidates pending admission; a changed source/context or provider requires a fresh preview/consent check. Existing CLI consent scopes are not widened.
6. Submit once with an idempotency key. Poll progress, stage and usage. The UI offers cancellation and renders partial/exhausted/error states explicitly. Refreshing or reconnecting never starts another run.
7. Inspect overview/subsystem diagrams and evidence at their precise revision. Inferred/proposed relationships retain their labels; claims with resolving citations are not automatically semantically supported. Each shows its computed `support` and reason codes (ADR-028, #65); unsupported claims are absent from the default diagram but listed with their reasons.
8. Download Markdown, Mermaid, JSON and optional HTML/SVG artifacts that actually exist. An ERD is offered only when supported table facts exist. Downloads, zoom and scope navigation never contact a provider. Publishing remains an explicit reviewable-PR operation.

## Shared service and HTTP boundary

- #69 implements (ADR-030, `repodocs.service`) ordinary Python request/result types and callbacks around existing core operations, not an event bus or plugin framework. Credential/provider objects are passed per run; never swap process-wide environment variables per user.
- #72 implements `POST /api/v1/jobs`, `GET /api/v1/jobs/{job_id}` and `POST /api/v1/jobs/{job_id}/cancel`, plus the session, workspace, preview, provider-session, consent and artifact routes the journey needs (ADR-036). They are adapter routes, not CLI commands. POST returns 202 for a new accepted job; replaying an identical admitted request returns its original job. Same workspace/key with a different request returns 409. Authentication and access checks run before revealing even an existing job.
- Invalid input is 422, missing consent is 403, stale preview/idempotency conflict is 409, a full bounded queue is 429, and an unknown/inaccessible handle is 404. Admission failure creates no model request. Errors contain a stable code and a sanitized message, never provider bodies or keys; non-2xx bodies use the contract's `api_error` envelope.
- Polling returns `sequence`, a monotonic per-job revision. Start at one-second polling, back off to five seconds and stop on terminal state. No elapsed percentage is invented. There is no automatic retry of paid work; any permitted repair counts against the same budget.
- A workspace retains one active job and at most one queued job by default. Idempotency covers the normalized request and source/context hashes, not credentials. Queue admission reserves the configured worst-case allowance. #67 defines measured/estimated/unknown accounting and prevents retries from bypassing aggregate caps.
- A result references existing IR/view hashes and opaque artifact handles, not embedded copies of the IR, executable markup or provider responses. The service resolves handles through an owned manifest and checks access, path containment and unexpected edits before returning bytes.

## Security boundary (ADR-033, #71)

- `repodocs.web.security` is implemented and tested without HTTP. #72 must call `SessionGuard.authorize` first on every request (and `open_session` for the first same-origin contact, which needs no OAuth), apply `response_headers`, and never add CORS headers. Non-loopback exposure needs `AccessConfig` with an HTTPS origin and an auth token.
- Provider credentials arrive per request into `ProviderSessions`, stay in memory and are never returned (`public_view` is the browser form). Consent receipts come from `ConsentLedger.grant` after the preview. #69 must run every model, token-count and retry request through `RunGate.call`, so revoked, expired, missing or mismatched consent starts nothing.
- An Ollama session takes no credential, and a remote authenticated Ollama `/v1` gateway is refused before any request; such gateways are CLI-only in this release (ADR-036). A web run never reads `REPODOCS_OLLAMA_API_KEY` or any other operator environment credential.
- Not provided: persistent or secure credential storage, login rate limiting, an HTTP server and UI rendering. Loopback trusts local processes of the same machine.

## State and recovery rules

| State | Meaning | Result treatment |
|---|---|---|
| queued | Admitted, no execution yet | No result |
| preflight | Claimed by a worker; scanning and checking the reviewed context; no provider request, no artifact write | No result |
| running | Stage executing | No final result |
| succeeded | All requested supported outputs generated and validated | Complete current result; not a semantic-quality claim |
| partial | Discovery explicitly ended incomplete | Partial preview, warning and reason; no complete-success badge |
| budget-exhausted | Next request cannot fit the remaining cap | Optional partial result, explicit budget error |
| cancelled | Cancellation acknowledged | Optional partial preview; no further provider requests |
| failed | Provider/operation failure | Sanitized error, no result from this job |
| stale | Result snapshot no longer matches the sources | Historical result may be inspected, never represented as current |
| interrupted | Restart/crash ended queued/running work | No automatic retry or inference resumption |

- Terminal jobs do not return to running. `succeeded`/`partial` results may subsequently be marked stale on source revalidation. Refresh is a new explicitly requested job with its own identity and budget.
- Cancellation is cooperative between bounded operations and provider requests. Abort a pending request when supported, but do not promise the provider stopped billing. Preserve unknown usage and keep the reservation unresolved until it can be reconciled; never refund it as measured zero. Generation checks cancellation before committing an artifact transaction.
- A terminal update is atomic with its manifest/result reference. Interrupted runs must not expose half-written artifacts or overwrite a previous complete current view. Existing discovery rules preserve a complete current view when refresh ends early; the job still reports the new partial/failure separately.
- Web job records are metadata, separate from `.repodocs/architecture-view.json`. Restart preserves inspectable owned results but not credentials. Expire in-memory provider sessions on restart or explicit disconnect; no default credential persistence. Cleanup only backend-owned workspace paths; #76 documents retention and manual deletion.
- No job record can confirm a candidate. Unresolved required confirmations still fail CLI validation; the web adapter displays that same failure.

## Contract examples and fixtures

- `docs/examples/web-workflow.example.json` contains a request and every job state. `tests/test_web_workflow_contracts.py` checks schema shape and rejects false success, malformed errors, unknown state/version, injected credentials and missing architecture budget fields.
- For #69/#72/#73 use `tests/fixtures/express-shop-repo` through a scripted provider: scan, preview, configure an ephemeral fake session, submit, poll queued/running/succeeded, inspect Mermaid/evidence, download and compare hashes with CLI outputs. This is the acceptance journey the HTTP adapter tests run.
- Run separate fixtures for partial discovery, exhausted aggregate budget, cancellation during inference, failed provider, changed source and interrupted restart. Assert no extra provider requests on reconnect, cancel, viewing or download. No CI credential or paid model is needed. `tests/test_web_jobs.py` runs this journey and these fixtures through the HTTP adapter with scripted providers.
- PR #55-60 is merged and supplies the core discovery, rendering, navigation and CLI workflow. PR #61 (the #54 evaluation harness) is merged. PR #62, the authenticated gateway adapter, is open and separate from #72; it is not assumed to be part of main.

## Release quality rubric (review with #64 before new scoring)

- Score pinned DigitalBoneBox and Flaskr independently against expected sets reviewed from source before running the model. Preserve earlier failed runs; do not rewrite expected facts to match output. Reuse #54 and PR #61's harness after review.
- Require component precision >= 0.90 and required-component recall >= 0.80 on each subject. Require supported connection precision >= 0.90 and required-connection recall >= 0.70. Null/empty denominators do not pass a required-coverage gate. Report exact counts alongside ratios.
- Independently inspect every connection in these small evaluation diagrams. Default supported views must contain zero source-reviewed fabricated endpoints, reversed connections or incorrect repository/component attribution. Proposals remain separate and are scored/reviewed explicitly; hiding them does not recover missing coverage.
- Require all cited paths/spans to resolve to the recorded source hash/revision, zero unconfirmed cross-repository authority promotions, and zero secret disclosures in reviewed outbound samples. Pattern redaction is not a guarantee of absence.
- Render every generated diagram with pinned real Mermaid tooling. An intended-user review must identify entry point, main responsibilities and storage path from each overview without opening source, and approve readability at normal zoom; record failures, not just screenshots.
- Require CLI/web artifact parity for identical stored inputs, no-op regeneration, stale-result labeling, edit protection, enforced budgets, cancellation/restart behavior and no duplicate paid requests. Reuse stored validated views for deterministic comparisons; do not assert independent model reruns are byte-identical.
- Owner go/no-go remains mandatory under #54/#77 even when these thresholds pass. A failed gate is a recorded result, not a reason to relax thresholds after scoring.

## Delivery boundaries

- #65 grounding; #66 bounded source context; #67 model-aware usage; #68 OpenAI provider; #69 application seam; #70 sources; #71 security; #72 jobs; #73 browser journey; #74 scoped diagrams; #75 evidence/downloads; #76 packaging; #77 parity/quality gate.
- #70 is implemented as the backend workspace core in `src/repodocs/web/workspaces.py` (ADR-034, D50). It returns the `ProjectConfig` that #69 consumes and adds no routes or UI. Local sources are chosen by configured name; public GitHub sources need an exact SHA, a bounded tree preflight and hardened depth-1 Git. Symlinks, submodules, private repositories and other hosts are refused.
- The #64 spike added schemas, examples, tests and documentation only. #72 adds the local runtime adapter (ADR-036). No public hosting, account/billing service, new paid calls, video generation or speculative backend is included.
- Public deployment policy, production default model, funded quotas and changed disclosure ceilings require separate decisions. Version 1.0 of this adapter contract may evolve through reviewed PRs before the first web release; CLI/IR contracts do not change implicitly.
