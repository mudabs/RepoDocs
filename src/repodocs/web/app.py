"""The local HTTP adapter (ADR-027, ADR-033, ADR-036): thin FastAPI routes over ``JobService``.

Install it with ``pip install "repodocs[web]"``. The CLI and the shared service never import this module.

Every route except opening a session calls ``SessionGuard.authorize`` before it reads a body, reveals a job or starts
anything. Bodies are read only up to ``max_body_bytes``. Every response carries ``response_headers``; artifacts are
sandboxed attachments. No CORS header is ever sent, and the generated API documentation routes are disabled.

The built frontend (``frontend/dist``) is served as static files from the same origin when present. The browser UI
is added by #73; the backend routes work independently of whether the frontend is built.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

_PACKAGED_FRONTEND = Path(__file__).resolve().parent / "static"
_SOURCE_FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "dist"
_FRONTEND_DIST = _PACKAGED_FRONTEND if _PACKAGED_FRONTEND.is_dir() else _SOURCE_FRONTEND

from .health import health_payload, readiness_status
from .jobs import CONTRACT_VERSION, JobError, JobService, JobSettings, ProviderFactory, build_provider
from .security import (
    COOKIE_NAME,
    Access,
    AccessConfig,
    AccessDenied,
    ConsentLedger,
    ConsentRequired,
    LimitExceeded,
    ProviderSessions,
    SessionGuard,
    SessionRequired,
    UnknownHandle,
    response_headers,
)
from .store import STORE_FILENAME, StoreError, WebStore
from .workspaces import WorkspaceManager, WorkspaceSettings

ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class WebSettings:
    """Everything the service needs from its operator. Browsers choose none of it.

    ``frontend_dist`` is the path to the built frontend assets directory. When ``None`` (the default),
    ``create_app`` looks for ``frontend/dist`` relative to the package source. Pass an explicit path
    or ``False`` (cast to ``Path | None``) to override in tests.
    """

    access: AccessConfig
    workspaces: WorkspaceSettings
    jobs: JobSettings = JobSettings()
    max_body_bytes: int = 64 * 1024
    frontend_dist: Path | None = None
    use_store: bool = True


class _TooLarge(Exception):
    pass


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = {"contract_version": CONTRACT_VERSION, "kind": "error",
            "error": {"code": code, "message": message, "retryable": status in (429, 503)}}
    return JSONResponse(body, status_code=status, headers=response_headers())


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={**response_headers(), **(headers or {})})


def create_app(
    settings: WebSettings,
    *,
    provider_factory: ProviderFactory = build_provider,
    workspace_manager: WorkspaceManager | None = None,
    frontend_dist: Path | None = ...,  # type: ignore[assignment]
) -> FastAPI:
    """The application for one local service. ``provider_factory`` and ``workspace_manager`` are test seams.

    ``frontend_dist`` overrides the built frontend path; pass an explicit ``Path`` or ``None`` to disable
    static serving. The default (``...``) defers to ``settings.frontend_dist`` or the package-relative path.
    """

    store = WebStore(settings.workspaces.store_root / STORE_FILENAME) if settings.use_store else None
    guard = SessionGuard(settings.access, store=store)
    providers = ProviderSessions(guard)
    consent = ConsentLedger(guard)
    jobs = JobService(workspace_manager or WorkspaceManager(settings.workspaces), providers, consent, settings.jobs,
                      provider_factory=provider_factory, store=store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        app.state.closed = True
        await run_in_threadpool(jobs.close)
        providers.clear()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.state.closed = False

    @app.middleware("http")
    async def defensive_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        if "content-security-policy" not in response.headers:
            response.headers.update(response_headers())
        return response

    async def read_json(request: Request) -> Any:
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > settings.max_body_bytes):
            raise _TooLarge
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > settings.max_body_bytes:
                raise _TooLarge
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise JobError("invalid-request", "The body must be a UTF-8 JSON document", 422) from None

    async def guarded(request: Request, operation: Callable[[Access, Any], Response], *, body: bool = False) -> Response:
        """Authorize, then read a bounded body, then run the blocking operation off the event loop."""

        try:
            access = guard.authorize(request.method, dict(request.headers))
            payload = await read_json(request) if body else None
            return await run_in_threadpool(operation, access, payload)
        except _TooLarge:
            return _error(413, "too-large", f"The request body is larger than {settings.max_body_bytes} bytes")
        except JobError as exc:
            return _error(exc.status, exc.code, str(exc))
        except SessionRequired as exc:
            return _error(401, "session-required", str(exc))
        except ConsentRequired as exc:
            return _error(403, "consent-required", str(exc))
        except AccessDenied as exc:
            return _error(403, "access-denied", str(exc))
        except UnknownHandle as exc:
            return _error(404, "not-found", str(exc))
        except LimitExceeded as exc:
            return _error(429, "queue-full", str(exc))

    # ------------------------------------------------------------------ session
    @app.post("/api/v1/session")
    async def open_session(request: Request) -> Response:
        """First same-origin contact. The CSRF token is in the body, which a cross-site page cannot read."""

        try:
            payload = await read_json(request)
            token = payload.get("auth_token") if isinstance(payload, dict) else None
            bootstrap = guard.open_session(dict(request.headers), token if isinstance(token, str) else None)
        except _TooLarge:
            return _error(413, "too-large", f"The request body is larger than {settings.max_body_bytes} bytes")
        except JobError as exc:
            return _error(exc.status, exc.code, str(exc))
        except SessionRequired as exc:
            return _error(401, "session-required", str(exc))
        except AccessDenied as exc:
            return _error(403, "access-denied", str(exc))
        except LimitExceeded as exc:
            return _error(429, "queue-full", str(exc))
        return _json(201, {"csrf_token": bootstrap.csrf_token}, {"Set-Cookie": bootstrap.set_cookie})

    @app.post("/api/v1/session/end")
    async def end_session(request: Request) -> Response:
        def run(access: Access, _: Any) -> Response:
            jobs.end_session(access)
            guard.end_session(access)
            secure = "; Secure" if settings.access.exposed else ""
            return Response(
                status_code=204,
                headers={"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"},
            )

        return await guarded(request, run)

    # ------------------------------------------------------------------ profile & history (#95)
    @app.get("/api/v1/profile")
    async def get_profile(request: Request) -> Response:
        return await guarded(request, lambda access, _: _json(200, jobs.get_profile(access)))

    @app.patch("/api/v1/profile")
    async def patch_profile(request: Request) -> Response:
        return await guarded(request, lambda access, payload: _json(200, jobs.update_profile(access, payload)), body=True)

    @app.get("/api/v1/workspaces")
    async def list_workspaces(request: Request) -> Response:
        return await guarded(request, lambda access, _: _json(200, {"workspaces": jobs.list_workspaces(access)}))

    @app.delete("/api/v1/workspaces/{workspace_id}")
    async def delete_workspace(request: Request, workspace_id: str) -> Response:
        def run(access: Access, _: Any) -> Response:
            jobs.delete_workspace(access, workspace_id)
            return Response(status_code=204)

        return await guarded(request, run)

    @app.get("/api/v1/workspaces/{workspace_id}/jobs")
    async def list_workspace_jobs(request: Request, workspace_id: str) -> Response:
        return await guarded(request, lambda access, _: _json(200, {"jobs": jobs.list_workspace_jobs(access, workspace_id)}))

    # ------------------------------------------------------------------ workspaces
    @app.post("/api/v1/workspaces")
    async def create_workspace(request: Request) -> Response:
        return await guarded(request, lambda access, payload: _json(201, jobs.create_workspace(access, payload)), body=True)

    @app.post("/api/v1/workspaces/{workspace_id}/preview")
    async def preview(request: Request, workspace_id: str) -> Response:
        return await guarded(request, lambda access, payload: _json(200, jobs.preview(access, workspace_id, payload)), body=True)

    # ------------------------------------------------------------------ providers and consent
    @app.post("/api/v1/providers")
    async def connect_provider(request: Request) -> Response:
        return await guarded(request, lambda access, payload: _json(201, jobs.connect_provider(access, payload)), body=True)

    @app.get("/api/v1/providers/{handle}")
    async def provider(request: Request, handle: str) -> Response:
        return await guarded(request, lambda access, _: _json(200, jobs.provider(access, handle)))

    @app.post("/api/v1/providers/{handle}/disconnect")
    async def disconnect_provider(request: Request, handle: str) -> Response:
        def run(access: Access, _: Any) -> Response:
            jobs.disconnect_provider(access, handle)
            return Response(status_code=204)

        return await guarded(request, run)

    @app.post("/api/v1/consents")
    async def grant_consent(request: Request) -> Response:
        return await guarded(request, lambda access, payload: _json(201, jobs.grant_consent(access, payload)), body=True)

    @app.post("/api/v1/consents/{receipt}/revoke")
    async def revoke_consent(request: Request, receipt: str) -> Response:
        def run(access: Access, _: Any) -> Response:
            jobs.revoke_consent(access, receipt)
            return Response(status_code=204)

        return await guarded(request, run)

    # ------------------------------------------------------------------ jobs
    @app.post("/api/v1/jobs")
    async def submit(request: Request) -> Response:
        def run(access: Access, payload: Any) -> Response:
            job, created = jobs.submit(access, payload)
            return _json(202 if created else 200, job)

        return await guarded(request, run, body=True)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> Response:
        return await guarded(request, lambda access, _: _json(200, jobs.get(access, job_id)))

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel(request: Request, job_id: str) -> Response:
        def run(access: Access, _: Any) -> Response:
            job = jobs.cancel(access, job_id)
            return _json(202 if job["state"] in ("preflight", "running") else 200, job)

        return await guarded(request, run)

    @app.get("/api/v1/jobs/{job_id}/artifacts/{artifact_id}")
    async def artifact(request: Request, job_id: str, artifact_id: str) -> Response:
        def run(access: Access, _: Any) -> Response:
            if not ARTIFACT_NAME.fullmatch(artifact_id):
                raise JobError("not-found", "The artifact is unknown or not yours", 404)
            item = jobs.artifact(access, job_id, artifact_id)
            headers = {**response_headers(artifact=True), "Content-Disposition": f'attachment; filename="{item.name}"'}
            return Response(item.content, media_type=item.media_type, headers=headers)

        return await guarded(request, run)

    if frontend_dist is ...:  # type: ignore[comparison-overlap]
        frontend_dist = settings.frontend_dist if settings.frontend_dist is not None else _FRONTEND_DIST
    _frontend: Path | None = frontend_dist  # type: ignore[assignment]

    # ------------------------------------------------------------------ health and readiness
    @app.get("/health", include_in_schema=False)
    async def health() -> Response:
        return _json(200, health_payload())

    @app.get("/ready", include_in_schema=False)
    async def ready() -> Response:
        ui_built = _frontend is not None and _frontend.is_dir()
        status_code, payload = readiness_status(
            settings.workspaces.store_root,
            closed=bool(app.state.closed),
            ui_built=ui_built,
        )
        return _json(status_code, payload)

    # ------------------------------------------------------------------ frontend
    # Serve the built static frontend when it is present. The frontend is an
    # optional build artifact (frontend/dist). It is never required by the API
    # or the CLI. When the dist directory does not exist, the API still works;
    # any non-API GET returns a 404 JSON error so the caller knows the UI is not
    # built yet.
    if _frontend is not None and _frontend.is_dir():
        assets_dir = _frontend / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        _frontend_index = _frontend / "index.html"

        @app.get("/", include_in_schema=False)
        @app.get("/workflow", include_in_schema=False)
        @app.get("/viewer/{job_id:path}", include_in_schema=False)
        async def spa_fallback(job_id: str = "") -> Response:  # noqa: ARG001
            return FileResponse(str(_frontend_index), media_type="text/html", headers=response_headers())

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon() -> Response:
            return Response(status_code=204, headers=response_headers())

        @app.get("/{full_path:path}", include_in_schema=False)
        async def unknown_route(full_path: str) -> Response:  # noqa: ARG001
            return _error(404, "not-found", "Unknown route")
    else:
        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon() -> Response:
            return Response(status_code=204, headers=response_headers())

        @app.get("/{full_path:path}", include_in_schema=False)
        async def ui_not_built(full_path: str) -> Response:  # noqa: ARG001
            return _error(404, "not-found", "The browser UI has not been built. Run 'npm ci && npm run build' in frontend/.")

    return app


def serve(settings: WebSettings) -> None:  # pragma: no cover - starts a real server
    """Run the service on the configured address: loopback unless ``AccessConfig`` allows otherwise."""

    import uvicorn

    uvicorn.run(create_app(settings), host=settings.access.bind_host, port=settings.access.port,
                proxy_headers=False, server_header=False, date_header=False)
