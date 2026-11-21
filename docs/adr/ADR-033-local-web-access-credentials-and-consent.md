# ADR-033: Local web access, provider credentials and source consent

- Date: 2026-11-20
- Status: Proposed for review in #71; implements the security rules of ADR-027.
- Issue: #71; epic #63. The HTTP adapter (#72) and shared service (#69) integrate this boundary.

## Context

A browser workflow can start paid, source-disclosing work. A website the user happens to visit can send requests to a loopback port, and a rebinding host name can make the browser believe the service is same-origin. Provider keys must not reach the browser, project files, logs or arguments, and consent must not silently widen when the provider changes.

## Decision

`repodocs.web.security` is a pure-Python, in-memory boundary with no HTTP server, storage, logging or environment access.

- **Exposure.** `AccessConfig` defaults to loopback. A non-loopback bind, or any public origin (for example a reverse proxy), is refused unless an explicit bare `https://host[:port]` origin and an authentication token of at least 32 characters are supplied. Only that origin and host are then accepted. TLS termination is the operator's. Wildcards are rejected and no CORS header is ever produced.
- **First session, no OAuth.** A same-origin browser loading the UI calls `open_session`: exact `Host`, any `Origin` present, and `Sec-Fetch-Site` (`same-origin` or `none`) are checked. It receives an HttpOnly, SameSite=Strict cookie and a CSRF token in the response body, which a cross-site page cannot read. Beyond loopback the token is also required here. Sessions expire (8 hours) and are bounded (32); a full table fails closed.
- **Every request.** `authorize` runs first in the adapter, before any job is revealed or started: exact `Host`; for state-changing methods a required exact `Origin`, `Sec-Fetch-Site: same-origin` when sent, a live session and a matching CSRF header. Only GET and HEAD are safe; OPTIONS needs CSRF, so preflight is never granted.
- **Credentials.** `ProviderSessions` keeps `Secret` values in memory per request-supplied connection, owned by one web session, expiring (1 hour), bounded (16) and clearable (`disconnect`, `disconnect_all`, `clear`, web session end or expiry). `Secret` and the contexts that hold it refuse repr, format, JSON, pickle and copy. The browser sees only `public_view`. `ProviderContext` is immutable and passed to the operation; nothing swaps process environment. There is no persistent or approved secure storage in this issue.
- **Consent.** `ConsentLedger` receipts bind the owner, kinds (`external`, `source-excerpts`; separate, one never implies the other), provider, host, model, policy and the reviewed preview hash, and expire (1 hour). Provider, host and model come from the credential context, so switching providers needs a new receipt. Revocation deletes the receipt.
- **Choke point.** `RunGate` re-checks session, credential and consent at admission and before every `call`. The service must route each model request, token-count request and retry through `call`, with a retry being a new call. An in-flight request cannot be recalled; the next is refused.
- **Hostile content.** `response_headers` provides a strict CSP, `nosniff`, no caching and no CORS; repository-derived artifacts (HTML, SVG, Markdown) are served as sandboxed attachments with no script. The UI (#73) must render repository text as text and use Mermaid with its strict security level; ADR-025 escaping of generated output stays in force.

## Alternatives

- A bearer token only (no session or CSRF) fails browsers' ambient cookie behavior and leaves a token in page script. Public OAuth or accounts are excluded.
- Environment-variable credential swapping races under concurrent jobs and is rejected (WEB_WORKFLOW.md).
- Persisting consent or keys would survive restarts but widens disclosure; deferred to a separately approved secure store.

## Consequences and limits

- Loopback trust is per machine: another local process can open a session and call the API. Use an exposed configuration with a token if that is not acceptable.
- No login rate limiting or token rotation; the token must be long and random (`generate_auth_token`).
- Pattern redaction of provider-side or provider-error text is not provided here; the boundary never logs and never puts a credential in its own messages.
- #69 must accept a `RunGate` (or equivalent per-request callback) and call it before every provider and count request including repairs and retries; #72 must call `authorize` first and apply `response_headers`. Until then no HTTP path exists to exercise the boundary end to end; tests use in-process fake paid operations.
