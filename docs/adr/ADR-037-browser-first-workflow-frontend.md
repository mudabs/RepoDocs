# ADR-037: Browser-first workflow frontend

- Date: 2026-11-24
- Status: Accepted
- Issue: #73; epic #63
- Implements: D43, ADR-027

## Context

ADR-027 authorized a local/self-hosted browser workflow over the existing Python engine and specified
React/TypeScript with Vite. The backend HTTP adapter was delivered by #72. This ADR records the
implementation decisions for the frontend build that #73 adds.

## Decision

- Use React 18, TypeScript 5, and Vite 5 for a small static single-page application.
  No Redux, no large UI framework. Plain React hooks and fetch with inline styles.
- Build output goes to `frontend/dist`. The FastAPI app serves it as static files from the same
  origin when the directory exists. The SPA fallback route returns `index.html` for any non-API
  GET, so browser navigation works without a separate server.
- When `frontend/dist` is absent, any non-API GET returns a JSON 404 error. The API works
  independently of the frontend build.
- The `mermaid` npm package (^11) is a production dependency so the viewer (#75) can import it
  from the same build.
- Provider API keys are accepted as transient form input, passed to the backend in a single
  request, and cleared from component state immediately after. They are never written to
  `localStorage`, `sessionStorage`, URLs, job records, logs, or analytics.
- The idempotency key is generated client-side using `crypto.getRandomValues` (RFC 4122 v4 UUID).
  Refreshing the page or reconnecting does not silently resubmit a job: the idempotency key is
  produced once per workflow page mount.
- Partial, stale, budget-exhausted, cancelled, failed, and interrupted results carry a visible
  "not current" notice. The UI never shows a "verified architecture" badge.
- Budget estimates are displayed as estimates. Unknown cost or token usage is shown as "unknown",
  never as zero.
- A `frontend-build` CI job runs `npm ci`, `tsc --noEmit`, and `vite build` on every pull request.

## Alternatives considered

- Serving the frontend from a separate dev server: rejected because it would require CORS headers
  or a proxy, and the contract forbids CORS headers.
- Using a heavier UI framework (Next.js, Remix): rejected because the workflow is a single
  multi-step form, not a full application requiring server-side rendering or a router.
- Generating the idempotency key server-side: rejected because the browser must control it to
  prevent silent resubmission on page refresh.

## Consequences

- Node.js and npm are required only to build the frontend. The CLI and the Python tests do not
  require them.
- The `frontend/dist` directory is a build artifact and must not be committed.
- The `frontend-build` CI job is independent of the Python test matrix; both must pass.
- Authenticated remote Ollama gateways are CLI-only. The web UI only supports loopback Ollama.
- Public hosting, managed AI inference, and accounts are not part of this release.
