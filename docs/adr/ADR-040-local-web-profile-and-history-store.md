# ADR-040: Optional SQLite store for local web profiles and saved history

- Date: 2026-11-27
- Status: Accepted
- Issue: #95; epic #63
- Amends: ADR-027 (versioned local metadata), ADR-033 (in-memory sessions), ADR-036 (job records and restart)

## Context

Web sessions and workspace ownership were held only in memory (ADR-033, ADR-036). A browser or
server restart gave the user a new random owner, so their own saved workspaces and results were
no longer reachable. Users need their profile and history to survive navigation and restarts,
without moving the engine into a database and without persisting credentials or consent.

## Decision

- `repodocs.web.store.WebStore` is an optional, web-only SQLite file at
  `<store_root>/repodocs-web.sqlite3` (standard-library `sqlite3`, WAL, foreign keys, bounded busy
  timeout, transactional versioned migrations in `schema_migrations`). The CLI, the shared service
  and GitHub Actions never import or need it.
- It holds local profiles (opaque `prof_` ID, optional display name, non-secret preferences), the
  SHA-256 digests of session cookies and CSRF tokens with their wall-clock expiry, workspace
  ownership and names, and a mirror of job records. Source files, IR, manifests, artifacts and the
  authoritative job files stay in the existing workspace layout (ADR-034, ADR-036).
- Provider keys, raw session cookies, CSRF tokens, the operator auth token and consent receipts are
  never stored. Provider sessions and consent stay in memory and end with the server (ADR-033 is
  unchanged for them), so reconnecting never reuses a credential or an old authorization.
- **Ownership.** A profile is reachable only through a live session whose hashed cookie the store
  recorded. Opening a session with such a cookie rotates it (the old cookie stops working) and
  renews its expiry; without one a new empty profile is created. A profile ID, display name or
  remembered URL never grants access. Non-loopback access still needs the operator auth token to
  open any session; a shared operator token is single-operator trust, not multi-user isolation.
- **Lifetimes.** With the store, a loopback session lasts 30 days, sliding on each reopen; this is
  local-machine trust: anyone who can use the browser profile, or read the store directory, can
  reach that history. Exposed access keeps the 8-hour session.
- **Logout and deletion.** `POST /api/v1/session/end` revokes every session of the profile and its
  provider sessions and consent. Saved workspaces stay on disk and in the store, but a new session
  is a new profile and cannot reach them; there is no self-service adoption. Deleting a workspace
  removes its directory and its metadata together.
- **Restart and reconciliation.** Job files remain authoritative. On start, unfinished jobs become
  `interrupted` as before and nothing is resumed; the store's job mirror is re-synced from the
  files. A job file whose owner contradicts the recorded workspace owner is shown to nobody.
  Recorded workspaces whose directory is gone are not listed. Workspaces created before the store
  (random in-memory owners) are not adopted by any profile.
- **Failure.** A corrupt or newer-schema store fails closed: the server refuses to start or clean
  up, and never resets the file. A busy or failing store returns `503 store-unavailable` for
  requests that need it; a failed metadata write never stops a running job, and a workspace whose
  ownership cannot be recorded is removed rather than left unowned.
- **Retention.** Age-based cleanup skips workspaces the store marks saved.

## Alternatives

- Keep memory-only ownership: simplest, but loses legitimate users' work on every restart.
- Store everything in the database: duplicates file contents and couples the engine to a server.
- One shared local profile with no cookie: simpler recovery after logout, but merges every browser
  on the machine into one owner and weakens the per-session credential isolation of ADR-033.

## Consequences

- Backup is the SQLite file (online `WebStore.backup_to` or `sqlite3 .backup`) together with the
  `ws-*` directories; restore both while the server is stopped.
- Logging out makes saved history unreachable from the browser until an operator deletes it.
  Multi-user accounts, sign-in, OAuth and recovery flows remain deferred.

## Amendment (#97): workflow drafts and browser routes

- Schema version 2 adds a `drafts` table (`draft_id`, `profile_id`, `version`, `data_json`, `updated_at`). The
  migration from version 1 is transactional like the others.
- Routes: `GET /api/v1/drafts`, `GET|PUT|DELETE /api/v1/drafts/{draft_id}`, behind the same session and owner checks.
  A draft holds only non-secret workflow state: sources and resolved revisions, workspace ID, mode, views, non-secret
  provider choice, requested limits, step, the active job ID and a pending idempotency key. Keys named like
  credentials or consent (`api_key`, `auth_token`, `csrf_token`, `consent_receipt_id`, `secret`, `password`) are
  refused with 422.
- `PUT` takes an optional `expected_version`; a stale version returns 409 `conflict`, so a second tab cannot silently
  overwrite a newer draft. A draft ID owned by another profile returns 404, never revealing that it exists. A busy or
  failing store returns 503 `store-unavailable`.
- The UI persists the pending idempotency key before `POST /api/v1/jobs`, so reconnecting after a lost response
  replays the same request and receives the existing job (200), never a second one.
- `/workflow/{draft_id}`, `/viewer/{job_id}` and `/jobs/{job_id}` serve the single-page app. Restoring a draft never
  restores a provider session or consent: both live in server memory, so after a restart the provider reads as
  disconnected and a new request needs a fresh preview and consent; a changed source makes the old preview stale
  (409 `stale-preview`).
