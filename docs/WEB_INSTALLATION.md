# RepoDocs Local and Self-Hosted Web Installation

This guide covers installing, building, starting, and maintaining the optional RepoDocs web
interface on Linux and Windows.

## 1. Prerequisites

- Python 3.11 or newer
- Git on `PATH` (used with a scrubbed environment for pinned GitHub checkouts)
- Node.js 20 or newer (needed only to build the static frontend in `frontend/`)
- Optional: local Ollama (`http://127.0.0.1:11434`) or an OpenAI API key for architecture mode

The core `repodocs` CLI and GitHub Actions workflows do not require Node.js, FastAPI, Uvicorn,
or a running web server.

## 2. Installation

Install RepoDocs with the optional `web` extra:

```bash
python -m pip install -e ".[web]"
```

Build the static frontend once (from the repository root):

```bash
cd frontend
npm ci
npm run build
```

This writes static assets to `frontend/dist`. When `frontend/dist` (or packaged
`repodocs/web/static`) exists, the HTTP server serves the browser UI from the same origin as the
API. When the UI is not built, the API remains usable and non-API routes return a JSON `404`.

## 3. Starting and stopping the service

Start the loopback service with an explicit workspace store directory and one or more
allowlisted local source repositories:

```bash
repodocs-web \
  --store-root ./repodocs-store \
  --source my-service=../my-service
```

Default bind address: `127.0.0.1:8000` (loopback only, no auth token required).

To stop the service, send `SIGINT` (Ctrl+C) or `SIGTERM`. In-flight jobs are stopped
cooperatively. On next startup, any job left `queued`, `preflight`, or `running` transitions
to `interrupted` and is never resumed or re-submitted automatically.

## 4. Non-loopback / self-hosted configuration

Binding beyond loopback (`--host 0.0.0.0` or a non-loopback IP) or exposing behind a reverse
proxy requires both an explicit HTTPS `--public-origin` and a long authentication token
(at least 32 characters) passed via an environment variable:

```bash
export REPODOCS_WEB_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
repodocs-web \
  --store-root /var/lib/repodocs/store \
  --source my-service=/srv/repos/my-service \
  --host 0.0.0.0 \
  --port 8000 \
  --public-origin https://repodocs.internal.example \
  --auth-token-env REPODOCS_WEB_TOKEN
```

RepoDocs does not terminate TLS itself and sends no CORS headers.

## 5. Health and readiness

- `GET /health`: returns `200 {"status": "ok"}` while the process is up.
- `GET /ready`: returns `200 {"status": "ready", "ui_built": true|false}` when `store_root` is
  writable and the service is not shutting down, or `503 {"status": "not-ready", ...}` otherwise.

Neither endpoint exposes credentials, filesystem paths, or job records.

## 6. Providers, credentials, and billing

- **Deterministic mode**: runs static analysis only; requires no AI provider or API key.
- **Local Ollama**: supported on loopback addresses (`http://127.0.0.1:11434`). Authenticated
  remote Ollama gateways are CLI-only and are not supported in the web UI.
- **OpenAI (BYO key)**: keys are entered per session in the browser, held in memory only for
  that session, never written to `localStorage`, `sessionStorage`, URLs, logs, or job records.
  External providers require explicit separate consent for external transmission and source
  excerpts. You are responsible for all usage charges on your own API key. RepoDocs does not
  provide managed or free cloud inference.

## 7. Data locations, retention, and cleanup

Server-owned workspaces are created under `<store_root>/ws-<id>/`, containing:
- `.repodocs-workspace.json` (ownership marker)
- `repodocs.json` and `.repodocs/` (IR, manifests, receipts)
- `jobs/` (atomic job metadata without credentials)
- `destination/` and `sources/` (fetched pinned checkouts)

To remove old inactive workspaces on startup:

```bash
repodocs-web --store-root ./repodocs-store --cleanup-max-age-hours 24
```

Cleanup inspects only direct children of `store_root` with a valid `.repodocs-workspace.json`
marker, skips symlinks and non-workspace directories, and never deletes a workspace with a
`queued`, `preflight`, or `running` job.

## 8. Troubleshooting

- **JSON 404 on `/`**: run `npm ci && npm run build` inside `frontend/` (or pass
  `--frontend-dist PATH`).
- **`repodocs-web` reports missing web dependencies**: install with `pip install "repodocs[web]"`.
- **Non-loopback startup rejected (exit code 2)**: ensure both `--public-origin https://...` and
  `--auth-token-env VAR` (with at least 32 characters) are set.

## 9. Deferred scope

Public multi-tenant hosting, user accounts, hosted billing, Dockerfiles, and reverse-proxy
configurations are outside the scope of this release.
