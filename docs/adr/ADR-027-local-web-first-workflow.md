# ADR-027: Local web-first workflow over the shared engine

- Date: 2026-11-20
- Status: Local/self-hosted scope authorized by the owner; implementation contract submitted for review in #64.
- Issue: #64; epic #63
- Amends: D03 delivery order and the web exclusions in the product requirements. D02, D04, D07, D19 and ADR-026 authority/consent rules remain in force.

## Context

The CLI already performs scanning, bounded discovery, deterministic rendering and validation. The owner now wants a browser-first local/self-hosted experience. This does not authorize a public service, funded inference, accounts, billing or videos. A useful diagram requires supported claims, not just valid syntax or clickable citations; #54 remains the real-source evaluation gate.

## Decision

- Use React and TypeScript with Vite for a small static frontend. Reuse Mermaid and the existing evidence map; do not add another diagram engine.
- Add FastAPI and Uvicorn only through an optional web installation. Serve the built frontend and API from one origin. FastAPI supplies a thin typed HTTP boundary; it does not own analysis. No web dependency is added by this contracts PR.
- Extract a small application-service seam for scan, context preview, discovery, generation and validation (#69). Both adapters call it directly. Preserve existing CLI JSON/exit codes; do not make the CLI call HTTP.
- Start with one Python process and one active job, a bounded queue and polling. Multiple processes, Redis, PostgreSQL, websockets and a durable task broker add no value for this first local release.
- Use backend-generated opaque workspace, provider-session, consent-receipt, job and artifact handles. Browser requests never contain filesystem paths, arbitrary clone commands or raw provider credentials in job records.
- Bind to loopback by default. Non-loopback service is refused until authenticated access and explicit exposure settings are supplied (#71); no unauthenticated public binding.
- Persist atomic versioned job metadata and sanitized results in an owned workspace. Credentials remain in memory. On restart, unfinished jobs become `interrupted`; never resume inference or replay a queued paid request automatically. Provider credentials and consent for a new provider session must be supplied again.
- Use the job/request schema and behavioral rules in [the workflow contract](../WEB_WORKFLOW.md). This is an additive adapter contract, not a replacement for the IR or architecture-view schema.

## Alternatives

- A separate web analysis engine would drift from the CLI and violate D02.
- A hosted account service and distributed workers would introduce identity, retention, operating costs and failure modes before the local workflow is useful.
- SSE can follow if polling is insufficient. Initial polling uses monotonic sequence numbers and avoids a second delivery protocol.
- Replacing Mermaid with an editor would expand the product before source grounding is reliable.

## Consequences and review gates

- Existing source safety, confirmations, source-excerpt consent, ownership and provenance checks apply equally to web jobs.
- CLI/Actions still need no persistent backend or database. The web adapter needs a running local process only while the browser workflow is in use.
- Limits must be checked over the complete serialized request and aggregate usage (#67); current byte/request limits are not represented as token or monetary guarantees.
- ADR-031 (#67) implements that check: aggregate token, request and cost limits over the complete serialized request, with explicit unknown usage and local receipts.
- The release thresholds below are specified before evaluating a new model and must be reviewed with this PR. Owner usefulness go/no-go under #54/#77 remains explicit; a passing schema cannot satisfy it.
- No runtime server, frontend, dependency installation, provider change or public deployment is implemented here. Downstream issues implement these contracts.

## Deferred owner choices

- Default production model and any funded allowances, provider retention terms and public hosting policy.
- Changes to ADR-026's maximum context/disclosure limits and O13's additional human review requirement. Existing ceilings and uncertainty rules apply until separately approved.
- University endpoint access and the bounded live-evaluation spend approval. No credential or paid experiment is required to implement fixtures and adapters.
