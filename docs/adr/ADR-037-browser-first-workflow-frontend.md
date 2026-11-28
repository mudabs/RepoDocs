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

## Amendment (#99): URL-first home, Advanced step-by-step page and saved history

- `/` is a URL-first home page: repository URL (or an allowlisted local source name), a choice between AI
  architecture and local facts only, and one primary Generate action. First-use provider setup appears only when no
  provider session is live; a disclosure review that names the provider, destination, revisions, excerpt count,
  bytes, redactions and limits must be confirmed (both consent kinds) before anything is sent. Progress shows the
  job's own stages, a cancel action and plain-language outcomes; nothing claims verified architecture.
- The home page lists saved projects (source, revision, status, date). Opening a result never runs a model.
  Generating again for the same source and revision reuses the saved project and, if its latest run is still active,
  follows that run instead of submitting another. Delete asks for a second, explicit confirmation.
- The step-by-step workflow is unchanged in behaviour and moves to `/workflow` ("Advanced"). The #98 quick-generate
  card on that page is removed; the home page is the only one-action path.
- Home, `/viewer/:jobId` and `/workflow/:draftId` open a loopback session automatically; `/workflow` keeps its
  explicit session step. Sign out asks first: with the #95 store a new session starts a new local profile, so saved
  projects stay on disk but are no longer listed (ADR-040).
- The connected provider is remembered in page memory only (its server handle, never the key). A reload or server
  restart forgets it and the next AI run asks to connect again.
- The viewer sizes the diagram's viewBox from the drawing as laid out in the page. Mermaid's off-screen measurement
  could leave the last row of a wide graph outside the viewBox (the #93 fix did not cover this).
- Accessibility: a skip link, labelled controls, focus moved to each new step heading, a polite live region for
  progress and outcomes, 3 px focus outlines, WCAG AA text contrast, `prefers-reduced-motion`, and no horizontal
  scrolling at 320 px. `tests/test_polished_workspace_99.py` checks these in Chrome.
