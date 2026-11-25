# ADR-039: Repeatable local and self-hosted web installation

- Date: 2026-11-24
- Status: Accepted
- Issue: #76; epic #63
- Implements: D43, ADR-027, ADR-033, ADR-034, ADR-036

## Context

Issues #72, #73, and #75 delivered the HTTP jobs API, browser workflow UI, and interactive
viewer. A repeatable installation, startup command, health/readiness endpoints, and bounded
workspace cleanup are needed so operators can run the local/self-hosted web service cleanly on
Linux and Windows without affecting the standalone CLI.

## Decision

- Add a dedicated optional script entrypoint `repodocs-web = "repodocs.web.server:main"` in
  `pyproject.toml`. The core `repodocs` CLI never imports `repodocs.web.app`, `fastapi`, or
  `uvicorn`.
- Pass auth tokens for non-loopback deployments via `--auth-token-env ENV_VAR` rather than a
  CLI flag so secrets are not exposed in process listings.
- Add unauthenticated `/health` and `/ready` routes in `repodocs.web.app` (implemented via
  `repodocs.web.health`) returning minimal JSON status with standard defensive headers and no
  credentials, paths, or job details.
- Add `repodocs.web.cleanup.cleanup_workspaces` to remove marker-owned workspace directories
  older than a configured age, skipping symlinks, unowned directories, and workspaces with
  active jobs.
- Resolve static frontend assets from packaged `repodocs/web/static` first when present,
  falling back to `frontend/dist` in source checkouts or an explicit `--frontend-dist` path.
- Document installation, startup, security boundaries, retention, and troubleshooting in
  `docs/WEB_INSTALLATION.md`.

## Alternatives considered

- Adding `repodocs web` as a subcommand of `repodocs.cli`: kept separate as `repodocs-web`
  so the core CLI parser and package footprint remain unchanged, while allowing future
  delegation if desired.
- Shipping Dockerfiles or reverse-proxy configs: deferred to keep local/self-hosted
  installation simple and avoid implying a hosted multi-tenant service.

## Consequences

- `repodocs` CLI continues to run without `fastapi`, `uvicorn`, or Node.js.
- Operators have a single documented command (`repodocs-web`) for startup, cleanup, and
  explicit non-loopback configuration.
