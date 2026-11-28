"""Bounded local jobs over the shared application service (ADR-036, #72).

This module has no HTTP code; ``repodocs.web.app`` maps its methods to routes. Every analysis step is a call to
``repodocs.service``: a job scans, checks the reviewed context, discovers (only in architecture mode), generates and
validates exactly as the CLI does. What this module adds is admission, a bounded queue, job records, progress, restart
recovery, and the rules for reading artifacts back.

- Admission validates the request against ``schemas/web-workflow-1.0.json``, the caller's ownership of the workspace,
  the reviewed preview, the provider session and the consent receipt before a job exists. A refused admission starts
  nothing.
- One job runs per workspace, at most ``max_active_jobs`` at once, with one more queued per workspace and
  ``max_queued_jobs`` in all. A full queue is refused, never grown.
- Records are written atomically under ``<workspace>/jobs/``. They hold the public job, the owner's public id and a hash
  of the normalized request, never a credential, a provider handle, a prompt, a response or source text.
- On start, any record left queued or running becomes ``interrupted``. Nothing is resumed or retried; repeating the same
  submission returns that record.
- Cancellation is cooperative: before every later stage, token count and model request. A request already in flight
  finishes or times out, and its usage stays in the run's receipt.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import urlsplit

from .store import StoreError

if TYPE_CHECKING:
    from .store import WebStore

from .. import service
from ..ai.budgets import PriceProfile, RunLimits, RunStopped, ledger_for
from ..ai.bundle import redact_text
from ..ai.providers import AnthropicProvider, OllamaProvider, Provider, is_loopback
from ..common import canonical_json, scrub, sha256_bytes, write_json_atomic
from ..config import SOURCE_POLICY_VERSION, ProjectConfig
from ..errors import ConfigError, ConsentError, ProviderError, RepoDocsError
from ..ir import ir_hash
from .security import (
    Access,
    ConsentLedger,
    ConsentRequired,
    ProviderContext,
    ProviderSessions,
    RunGate,
    SecurityError,
    UnknownHandle,
)
from .workspaces import (
    GitHubSource,
    LocalSource,
    WorkspaceError,
    WorkspaceManager,
    _owned_path,
    resolve_github_source,
)

CONTRACT_VERSION = "1.0"
RECORD_VERSION = "1.0"
JOBS_DIRECTORY = "jobs"

ACTIVE_STATES = ("queued", "preflight", "running")
STAGES = ("queued", "scan", "context", "discover", "generate", "validate", "finished")
VIEWS = ("overview", "components", "erd")
MODES = ("deterministic", "architecture")
MAX_REQUESTS = 6
MAX_KEY_LENGTH = 200
MAX_HANDLE_LENGTH = 200
MAX_MESSAGE_LENGTH = 500

# Discovery's own structural bounds; a view that stopped on one of them is partial for a budget reason.
_STRUCTURAL_BUDGETS = ("round-budget", "request-budget", "context-budget", "response-budget")

ProviderFactory = Callable[[ProviderContext, RunLimits], Provider]


class JobError(RepoDocsError):
    """An admission or access failure with a stable code and the HTTP status the adapter returns for it."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _invalid(message: str) -> JobError:
    return JobError("invalid-request", message, 422)


def _not_found(what: str) -> JobError:
    return JobError("not-found", f"The {what} is unknown or not yours", 404)


@dataclass(frozen=True)
class JobSettings:
    """Operator limits for the job service. Every count is a positive integer.

    ``run_limits`` and ``prices`` describe the operator's models (for example a local model's context window) and are
    written to each new workspace's ``ai`` settings, so the CLI and the web service apply the same limits to it. A job
    request can only lower them.
    """

    max_active_jobs: int = 1
    max_queued_jobs: int = 8
    max_jobs_per_workspace: int = 50
    max_download_bytes: int = 16 * 1024 * 1024
    run_limits: RunLimits = RunLimits()
    prices: PriceProfile | None = None

    def __post_init__(self) -> None:
        for name in ("max_active_jobs", "max_queued_jobs", "max_jobs_per_workspace", "max_download_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"Job setting {name} must be a positive integer")


@dataclass(frozen=True)
class JobRequest:
    """A validated submission. Handles are kept for admission and the run, never persisted."""

    workspace_id: str
    idempotency_key: str
    mode: str
    provider_session_id: str | None
    context_hash: str | None
    consent_receipt_id: str | None
    views: tuple[str, ...]
    max_requests: int
    max_total_tokens: int | None
    max_cost_microunits: int | None
    refresh: bool

    def normalized(self) -> dict[str, Any]:
        """What identifies the submission. Credentials and their handles are not part of it (WEB_WORKFLOW.md)."""

        return {
            "mode": self.mode, "context_hash": self.context_hash, "views": sorted(self.views), "refresh": self.refresh,
            "limits": {"max_requests": self.max_requests, "max_total_tokens": self.max_total_tokens, "max_cost_microunits": self.max_cost_microunits},
        }

    def run_limits(self) -> RunLimits:
        cost = None if self.max_cost_microunits is None else Decimal(self.max_cost_microunits) / Decimal(1_000_000)
        return RunLimits(max_requests=self.max_requests, max_total_tokens=self.max_total_tokens, max_cost_usd=cost)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _handle(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload[name]
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_HANDLE_LENGTH:
        raise _invalid(f"{name} must be null or a non-empty string")
    return value


def parse_request(payload: Any) -> JobRequest:
    """Check a submission against the request contract. Any deviation is an ``invalid-request``."""

    if not isinstance(payload, dict):
        raise _invalid("The request must be a JSON object")
    required = {"contract_version", "kind", "workspace_id", "idempotency_key", "mode", "provider_session_id", "context_hash",
                "consent_receipt_id", "views", "limits"}
    unknown, missing = set(payload) - required - {"refresh"}, required - set(payload)
    if unknown or missing:
        raise _invalid("Unknown fields: " + ", ".join(sorted(unknown)) if unknown else "Missing fields: " + ", ".join(sorted(missing)))
    if payload["contract_version"] != CONTRACT_VERSION or payload["kind"] != "request":
        raise _invalid(f"contract_version must be {CONTRACT_VERSION} and kind must be request")
    for name in ("workspace_id", "idempotency_key"):
        value = payload[name]
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_KEY_LENGTH or scrub(value) != value:
            raise _invalid(f"{name} must be a non-empty string of at most {MAX_KEY_LENGTH} characters without control characters")
    mode = payload["mode"]
    if mode not in MODES:
        raise _invalid("mode must be deterministic or architecture")
    views = payload["views"]
    if not isinstance(views, list) or not views or any(item not in VIEWS for item in views) or len(set(views)) != len(views):
        raise _invalid("views must be a non-empty list of distinct values from overview, components and erd")
    limits = payload["limits"]
    names = {"max_requests", "max_total_tokens", "max_cost_microunits", "currency"}
    if not isinstance(limits, dict) or set(limits) != names:
        raise _invalid("limits must hold exactly max_requests, max_total_tokens, max_cost_microunits and currency")
    max_requests, total, cost, currency = (limits[name] for name in ("max_requests", "max_total_tokens", "max_cost_microunits", "currency"))
    if not _is_int(max_requests) or not 0 <= max_requests <= MAX_REQUESTS:
        raise _invalid(f"limits.max_requests must be an integer from 0 to {MAX_REQUESTS}")
    if total is not None and (not _is_int(total) or total < 1):
        raise _invalid("limits.max_total_tokens must be null or a positive integer")
    if cost is not None and (not _is_int(cost) or cost < 0):
        raise _invalid("limits.max_cost_microunits must be null or a non-negative integer")
    if currency not in ("USD", None) or (cost is not None and currency != "USD"):
        raise _invalid("limits.currency must be USD when a cost cap is given, otherwise null or USD")
    refresh = payload.get("refresh", False)
    if not isinstance(refresh, bool):
        raise _invalid("refresh must be true or false")
    provider, context, consent = (_handle(payload, name) for name in ("provider_session_id", "context_hash", "consent_receipt_id"))
    if mode == "architecture":
        if provider is None or context is None:
            raise _invalid("An architecture job needs provider_session_id and the context_hash of a reviewed preview")
        if total is None or max_requests < 1:
            raise _invalid("An architecture job needs max_total_tokens and at least one request")
        if cost == 0:
            raise _invalid("max_cost_microunits of 0 allows no model request; use null for no cost cap or a positive cap")
    else:
        if provider is not None or consent is not None or max_requests != 0 or cost != 0:
            raise _invalid("A deterministic job takes no provider or consent, max_requests 0 and max_cost_microunits 0")
        if refresh:
            raise _invalid("refresh applies only to architecture jobs")
    return JobRequest(payload["workspace_id"], payload["idempotency_key"], mode, provider, context, consent, tuple(views),
                      max_requests, total, cost, refresh)


def build_provider(context: ProviderContext, limits: RunLimits) -> Provider:
    """The run's provider from its in-memory credential, sent only to the session's host, never to the environment's."""

    from ..ai.openai import OpenAIProvider

    secret = None if context.secret is None else context.secret.reveal()
    if context.provider == "anthropic":
        if secret is None:
            raise ConfigError("The anthropic provider needs a credential for this provider session")
        return AnthropicProvider(context.model, max_output_tokens=limits.request_output_tokens or 0, api_key=secret, base_url=context.host)
    if context.provider == "openai":
        return OpenAIProvider(context.model, context.host, api_key=secret or "", max_output_tokens=limits.request_output_tokens or 0)
    if secret is not None:
        raise ConfigError("An Ollama provider session takes no credential in this release")
    # Authenticated /v1 gateways are CLI-only (ADR-020, ADR-036): a web run has no gateway credential and must never borrow
    # the operator's REPODOCS_OLLAMA_API_KEY, so a remote gateway is refused here, before any request.
    if urlsplit(context.host).path.rstrip("/") == "/v1" and not is_loopback(context.host):
        raise ConfigError("Authenticated Ollama /v1 gateways are not available to web sessions in this release; use the CLI")
    return OllamaProvider(context.model, context.host, context_window=limits.context_window_tokens, max_output_tokens=limits.request_output_tokens)


def is_external(context: ProviderContext) -> bool:
    """Anthropic always leaves the machine; Ollama and OpenAI-compatible servers do unless the host is loopback."""

    return context.provider == "anthropic" or not is_loopback(context.host)


def _identity(context: ProviderContext) -> Any:
    """Name, model and destination for budgeting and previews. It holds no credential and cannot send anything."""

    return SimpleNamespace(name=context.provider, model=context.model, external=is_external(context))


def _microunits(usd: str | None) -> int | None:
    return None if usd is None else int((Decimal(usd) * 1_000_000).to_integral_value())


def _usage(summary: Mapping[str, Any] | None, *, unknown: bool = False) -> dict[str, Any]:
    """The contract's usage object. Unknown is null, never zero; a total with unreported parts is an estimate."""

    if unknown or summary is None:
        return {"input_tokens": None, "output_tokens": None, "cost_microunits": None, "currency": None, "accounting": "unknown"}
    cost = _microunits(summary.get("cost_usd"))
    return {
        "input_tokens": summary["input_tokens"], "output_tokens": summary["output_tokens"],
        "cost_microunits": cost, "currency": None if cost is None else "USD",
        "accounting": "known" if summary["usage_complete"] else "estimated",
    }


NO_REQUESTS = {"input_tokens": 0, "output_tokens": 0, "cost_microunits": 0, "currency": None, "accounting": "known"}


def _budget(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Budget diagnostics: what was used, what remains, the worst-case estimate and why a run stopped."""

    remaining = summary["remaining"]
    return {
        "requests": summary["requests"], "count_requests": summary["count_requests"],
        "remaining_requests": remaining["requests"], "remaining_total_tokens": remaining.get("total_tokens"),
        "remaining_cost_microunits": _microunits(remaining.get("cost_usd")),
        "max_estimated_cost_microunits": _microunits(summary.get("max_estimated_cost_usd")),
        "usage_complete": summary["usage_complete"],
        "stop_reason": summary.get("stop_reason"), "stop_message": summary.get("stop_message"),
    }


def _summary_of(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """A ledger snapshot in the receipt summary's flat form."""

    return {**snapshot["totals"], "remaining": snapshot["remaining"], "stop_reason": snapshot["stop_reason"],
            "stop_message": snapshot["stop_message"], "max_estimated_cost_usd": snapshot["max_estimated_cost_usd"]}


def _view_of(name: str) -> str:
    if name.startswith("architecture-view"):
        return "components"
    if name.startswith("erd"):
        return "erd"
    return "overview"


_MEDIA_TYPES = {
    ".md": "text/markdown; charset=utf-8", ".mmd": "text/plain; charset=utf-8", ".json": "application/json",
    ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml",
}


def media_type(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


@dataclass(frozen=True)
class Artifact:
    name: str
    content: bytes
    media_type: str


@dataclass
class _Workspace:
    workspace_id: str
    owner: str
    root: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    preview: dict[str, str] | None = None
    busy: bool = False


@dataclass
class _Job:
    job_id: str
    workspace_id: str
    owner: str
    idempotency_key: str
    request_hash: str
    request: JobRequest | None
    state: str
    stage: str
    sequence: int
    usage: dict[str, Any]
    budget: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    cancel: service.CancellationToken = field(default_factory=service.CancellationToken)

    def public(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION, "kind": "job", "job_id": self.job_id, "workspace_id": self.workspace_id,
            "sequence": self.sequence, "state": self.state, "stage": self.stage, "error": self.error, "result": self.result,
            "usage": self.usage, "budget": self.budget,
        }


def _error(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable}


class JobService:
    """Workspaces, previews, consent and jobs for authorized callers. Every method takes the caller's ``Access``."""

    def __init__(
        self,
        workspaces: WorkspaceManager,
        providers: ProviderSessions,
        consent: ConsentLedger,
        settings: JobSettings = JobSettings(),
        *,
        provider_factory: ProviderFactory = build_provider,
        clock: Callable[[], float] = time.time,
        store: "WebStore | None" = None,
    ) -> None:
        self.settings = settings
        self._manager = workspaces
        self._providers = providers
        self._consent = consent
        self._factory = provider_factory
        self._clock = clock
        self._store = store
        self._cond = threading.Condition()
        self._workspaces: dict[str, _Workspace] = {}
        self._jobs: dict[str, _Job] = {}
        self._keys: dict[tuple[str, str, str], str] = {}
        self._queue: deque[str] = deque()
        self._workers: list[threading.Thread] = []
        self._closed = False
        self._recover()

    # ------------------------------------------------------------------ lifecycle
    def close(self, timeout: float = 10.0) -> None:
        """Stop accepting work, ask running jobs to stop at their next checkpoint and wait for the workers."""

        with self._cond:
            self._closed = True
            for job in self._jobs.values():
                if job.state in ACTIVE_STATES:
                    job.cancel.cancel()
            self._cond.notify_all()
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout)

    def _recover(self) -> None:
        """Rebuild records after a restart. Unfinished jobs become ``interrupted``; none is resumed or retried."""

        for workspace_id in sorted(self._manager.owned_workspaces()):
            root = _owned_path(self._manager.settings.store_root, workspace_id)
            directory = None if root is None else root / JOBS_DIRECTORY
            if directory is None or directory.is_symlink() or not directory.is_dir():
                continue
            recorded = self._recorded_owner(workspace_id)
            for path in sorted(directory.glob("job_*.json"))[: self.settings.max_jobs_per_workspace]:
                job = self._load(path, workspace_id)
                if job is None:
                    continue
                if recorded is not None and job.owner != recorded:
                    continue  # file and metadata disagree about the owner: show it to nobody
                self._workspaces.setdefault(workspace_id, _Workspace(workspace_id, job.owner, root))  # type: ignore[arg-type]
                if job.state in ACTIVE_STATES:
                    started = job.stage in ("discover", "generate", "validate")
                    job.error = _error("interrupted", (
                        f"The service stopped before this job finished (during {job.stage}). It was not resumed and no request was "
                        "repeated; submit a new job to run it again"
                    ))
                    job.state, job.result = "interrupted", None
                    # A model request may have been in flight: what it cost is unknown, not zero.
                    if job.stage == "discover":
                        job.usage = _usage(None, unknown=True)
                    elif not started:
                        job.usage = dict(NO_REQUESTS)
                    job.sequence += 1
                    self._persist(job)
                elif self._store is not None:
                    try:
                        self._store_job(job)  # reconcile history that a failed metadata write left behind
                    except StoreError:
                        pass
                self._jobs[job.job_id] = job
                self._keys[(job.owner, job.workspace_id, job.idempotency_key)] = job.job_id
        if self._store is not None:
            for workspace_id in sorted(self._manager.owned_workspaces()):
                if workspace_id in self._workspaces:
                    continue
                record = self._store.get_workspace(workspace_id)
                root = _owned_path(self._manager.settings.store_root, workspace_id)
                if record is not None and root is not None:
                    self._workspaces[workspace_id] = _Workspace(workspace_id, record["profile_id"], root)

    def _recorded_owner(self, workspace_id: str) -> str | None:
        if self._store is None:
            return None
        record = self._store.get_workspace(workspace_id)
        return None if record is None else str(record["profile_id"])

    def _load(self, path: Path, workspace_id: str) -> _Job | None:
        try:
            if path.is_symlink():
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            public = record["job"]
            if record.get("record_version") != RECORD_VERSION or public["workspace_id"] != workspace_id or path.stem != public["job_id"]:
                return None
            return _Job(
                public["job_id"], workspace_id, record["owner"], record["idempotency_key"], record["request_hash"], None,
                public["state"], public["stage"], public["sequence"], public["usage"], public.get("budget"), public["error"],
                public["result"], dict(record.get("artifacts", {})), float(record.get("created_at", 0.0)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _persist(self, job: _Job) -> None:
        workspace = self._workspaces[job.workspace_id]
        write_json_atomic(workspace.root / JOBS_DIRECTORY / f"{job.job_id}.json", {
            "record_version": RECORD_VERSION, "owner": job.owner, "idempotency_key": job.idempotency_key,
            "request_hash": job.request_hash, "artifacts": job.artifacts, "created_at": job.created_at, "job": job.public(),
        })
        if self._store is not None:
            # The job file above is the authoritative record and is rebuilt into the store's history on restart. A locked
            # or failing metadata store must never stop a worker or leave a job without its terminal state.
            try:
                self._store_job(job)
            except StoreError:
                pass

    def _store_job(self, job: _Job) -> None:
        assert self._store is not None
        self._store.save_job(
            job_id=job.job_id,
            workspace_id=job.workspace_id,
            profile_id=job.owner,
            idempotency_key=job.idempotency_key,
            request_hash=job.request_hash,
            state=job.state,
            stage=job.stage,
            sequence=job.sequence,
            error=job.error,
            result=job.result,
            usage=job.usage,
            budget=job.budget,
            artifacts=job.artifacts,
            created_at=job.created_at,
            updated_at=self._clock(),
        )

    # ------------------------------------------------------------------ workspaces and previews
    def resolve_github_url(self, access: Access, payload: Any) -> dict[str, Any]:
        """Resolve a public github.com URL to its default branch and exact commit SHA."""

        if not isinstance(payload, dict) or set(payload) != {"url"} or not isinstance(payload["url"], str):
            raise _invalid("A GitHub resolution request holds only {url}")
        with self._cond:
            if self._closed:
                raise JobError("unavailable", "The service is shutting down", 503)
        try:
            return resolve_github_source(payload["url"], self._manager.settings.limits, self._manager._http_get)
        except WorkspaceError as exc:
            raise JobError("workspace-failure", self._safe(str(exc)), 422) from None

    def create_workspace(self, access: Access, payload: Any) -> dict[str, Any]:
        """Acquire a workspace for allowlisted local roots or pinned public GitHub sources."""

        if not isinstance(payload, dict) or set(payload) != {"name", "sources"} or not isinstance(payload["sources"], list):
            raise _invalid("A workspace request holds exactly name and a list of sources")
        sources: list[LocalSource | GitHubSource] = []
        for item in payload["sources"]:
            if isinstance(item, dict) and item.get("kind") == "local" and set(item) == {"kind", "name"}:
                sources.append(LocalSource(item["name"]))
            elif isinstance(item, dict) and item.get("kind") == "github" and set(item) == {"kind", "owner", "repository", "revision"}:
                sources.append(GitHubSource(item["owner"], item["repository"], item["revision"]))
            else:
                raise _invalid("Each source is {kind: local, name} or {kind: github, owner, repository, revision}")
        if not isinstance(payload["name"], str):
            raise _invalid("name must be a string")
        with self._cond:
            if self._closed:
                raise JobError("unavailable", "The service is shutting down", 503)
        try:
            workspace = self._manager.acquire(payload["name"], sources)
        except WorkspaceError as exc:
            raise JobError("workspace-failure", self._safe(str(exc)), 422) from None
        config = workspace.config
        config.ai = replace(config.ai, limits=self.settings.run_limits, prices=self.settings.prices)
        config.save()
        source_dicts = [record.to_dict() for record in workspace.sources]
        if self._store is not None:
            try:
                self._store.save_workspace(workspace.workspace_id, access.owner, payload["name"], source_dicts, now=self._clock())
            except StoreError:
                shutil.rmtree(workspace.root, ignore_errors=True)
                raise JobError("store-unavailable", "The saved-history store is unavailable; no workspace was kept. Try again", 503) from None
        with self._cond:
            self._workspaces[workspace.workspace_id] = _Workspace(workspace.workspace_id, access.owner, workspace.root)
        return {"workspace_id": workspace.workspace_id, "sources": source_dicts}

    # ------------------------------------------------------------------ profile & history (#95)
    def get_profile(self, access: Access) -> dict[str, Any]:
        if self._store is None:
            return {"profile_id": access.owner, "display_name": None, "preferences": {}}
        return self._store.get_profile(access.owner) or {"profile_id": access.owner, "display_name": None, "preferences": {}}

    def update_profile(self, access: Access, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"display_name", "preferences"}:
            raise _invalid("Profile updates may hold only display_name and preferences")
        display_name = payload.get("display_name", ...)
        if display_name is not ... and display_name is not None and (not isinstance(display_name, str) or len(display_name) > 120):
            raise _invalid("display_name must be null or a string of at most 120 characters")
        preferences = payload.get("preferences")
        if preferences is not None and not isinstance(preferences, dict):
            raise _invalid("preferences must be a JSON object")
        if self._store is None:
            return {"profile_id": access.owner, "display_name": None if display_name is ... else display_name, "preferences": preferences or {}}
        updated = self._store.update_profile(access.owner, display_name=display_name, preferences=preferences, now=self._clock())
        if updated is None:
            raise _not_found("profile")
        return updated

    def list_workspaces(self, access: Access) -> list[dict[str, Any]]:
        if self._store is not None:
            records = self._store.list_workspaces(access.owner)
            items: list[dict[str, Any]] = []
            for rec in records:
                ws_id = rec["workspace_id"]
                if _owned_path(self._manager.settings.store_root, ws_id) is None:
                    continue
                ws_jobs = [job.public() for job in self._jobs.values() if job.workspace_id == ws_id and job.owner == access.owner]
                ws_jobs.sort(key=lambda j: j["sequence"], reverse=True)
                items.append({**rec, "latest_job": ws_jobs[0] if ws_jobs else None})
            return items
        return [
            {"workspace_id": ws.workspace_id, "profile_id": ws.owner, "name": ws.workspace_id, "sources": [], "saved": True, "latest_job": None}
            for ws in self._workspaces.values()
            if ws.owner == access.owner and _owned_path(self._manager.settings.store_root, ws.workspace_id) is not None
        ]

    def list_workspace_jobs(self, access: Access, workspace_id: str) -> list[dict[str, Any]]:
        with self._cond:
            self._workspace(access, workspace_id)
            jobs = [job.public() for job in self._jobs.values() if job.workspace_id == workspace_id and job.owner == access.owner]
        jobs.sort(key=lambda j: j["job_id"])
        return jobs

    def delete_workspace(self, access: Access, workspace_id: str) -> None:
        with self._cond:
            workspace = self._workspace(access, workspace_id)
            if workspace.busy or any(job.workspace_id == workspace_id and job.state in ACTIVE_STATES for job in self._jobs.values()):
                raise JobError("busy", "Cannot delete a workspace with an active job", 409)
            owned = _owned_path(self._manager.settings.store_root, workspace_id)
            if owned is None:
                raise _not_found("workspace")
            shutil.rmtree(owned, ignore_errors=False)
            self._workspaces.pop(workspace_id, None)
            for jid in [jid for jid, job in self._jobs.items() if job.workspace_id == workspace_id]:
                job = self._jobs.pop(jid)
                self._keys.pop((job.owner, job.workspace_id, job.idempotency_key), None)
            if self._store is not None:
                self._store.delete_workspace(workspace_id)

    # ------------------------------------------------------------------ drafts (#97)
    _FORBIDDEN_DRAFT_KEYS = frozenset({"api_key", "auth_token", "csrf_token", "consent_receipt_id", "secret", "password"})

    @classmethod
    def _validate_draft_data(cls, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if not isinstance(k, str) or k.lower() in cls._FORBIDDEN_DRAFT_KEYS:
                    raise _invalid(f"Drafts must not contain credential or consent fields ({k})")
                cls._validate_draft_data(v)
        elif isinstance(value, list):
            for item in value:
                cls._validate_draft_data(item)

    def list_drafts(self, access: Access) -> list[dict[str, Any]]:
        if self._store is None:
            return []
        return self._store.list_drafts(access.owner)

    def get_draft(self, access: Access, draft_id: str) -> dict[str, Any]:
        if self._store is None:
            raise _not_found("draft")
        draft = self._store.get_draft(draft_id, access.owner)
        if draft is None:
            raise _not_found("draft")
        return draft

    def save_draft(self, access: Access, draft_id: str, payload: Any) -> dict[str, Any]:
        if not isinstance(draft_id, str) or not 1 <= len(draft_id) <= 64 or scrub(draft_id) != draft_id:
            raise _invalid("Invalid draft_id")
        if not isinstance(payload, dict) or set(payload) - {"data", "expected_version"} or "data" not in payload:
            raise _invalid("Draft payload must hold data and optional expected_version")
        if not isinstance(payload["data"], dict):
            raise _invalid("Draft data must be a JSON object")
        expected = payload.get("expected_version")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
            raise _invalid("expected_version must be a non-negative integer")
        self._validate_draft_data(payload["data"])
        if self._store is None:
            return {"draft_id": draft_id, "profile_id": access.owner, "version": 1, "data": payload["data"]}
        from .store import DraftConflict, DraftNotOwned, StoreError

        try:
            return self._store.save_draft(
                draft_id, access.owner, payload["data"], expected_version=expected, now=self._clock()
            )
        except DraftNotOwned:
            raise _not_found("draft") from None
        except DraftConflict as exc:
            raise JobError("conflict", str(exc), 409) from None
        except StoreError:
            raise JobError("store-unavailable", "The saved-history store is busy or unavailable; try again", 503) from None

    def delete_draft(self, access: Access, draft_id: str) -> None:
        if self._store is None or not self._store.delete_draft(draft_id, access.owner):
            raise _not_found("draft")

    def _workspace(self, access: Access, workspace_id: Any) -> _Workspace:
        workspace = self._workspaces.get(workspace_id) if isinstance(workspace_id, str) else None
        if workspace is None or workspace.owner != access.owner or _owned_path(self._manager.settings.store_root, workspace_id) is None:
            raise _not_found("workspace")
        return workspace

    def preview(self, access: Access, workspace_id: str, payload: Any) -> dict[str, Any]:
        """Scan and build the redacted context a model would see. Nothing is sent and nothing is counted.

        The response summarizes the context. The redacted excerpts themselves are included only when the caller asks for
        them with ``include_documents``; source bundles are never returned by default.
        """

        if not isinstance(payload, dict) or set(payload) - {"provider_session_id", "include_documents"}:
            raise _invalid("A preview request may hold only provider_session_id and include_documents")
        include = payload.get("include_documents", False)
        handle = payload.get("provider_session_id")
        if not isinstance(include, bool) or (handle is not None and not isinstance(handle, str)):
            raise _invalid("include_documents must be a boolean and provider_session_id a string or null")
        with self._cond:
            workspace = self._workspace(access, workspace_id)
            if workspace.busy or any(job.workspace_id == workspace_id and job.state in ACTIVE_STATES for job in self._jobs.values()):
                raise JobError("busy", "A job is queued or running in this workspace; preview again when it finishes", 409)
            workspace.busy = True
        try:
            context = None if handle is None else self._provider_context(access, handle)
            identity = None if context is None else _identity(context)
            with workspace.lock:
                scan = self._call(service.scan, service.ScanRequest(workspace.root))
                self._revalidate(workspace)
                preview = self._call(service.preview_context, service.ContextRequest(workspace.root, provider=identity))
            data = preview.data
            workspace.preview = {"context_hash": data["context_hash"], "source_ir_hash": data["source_ir_hash"]}
            response: dict[str, Any] = {
                "workspace_id": workspace_id, "context_hash": data["context_hash"], "source_ir_hash": data["source_ir_hash"],
                "policy": SOURCE_POLICY_VERSION, "documents": data["documents"], "context_bytes": data["context_bytes"],
                "redactions": data["redactions"], "omitted": data["omitted"], "view_status": preview.view_status,
                "snapshots": scan.data["snapshots"], "limitations": scan.data["limitations"],
                "candidate_states": scan.data["candidate_states"], "warnings": [self._safe(item) for item in scan.warnings],
                "provider": None if context is None else self._providers.public_view(access, handle),  # type: ignore[arg-type]
                "provider_is_external": None if context is None else is_external(context),
                "budget": data["budget"],
            }
            if include:
                bundle = json.loads((ProjectConfig.load(workspace.root).internal_path / "architecture-context.json").read_text(encoding="utf-8"))
                response["excerpts"] = [
                    {key: item[key] for key in ("repository_id", "path", "line_start", "line_end", "redactions", "text")}
                    for item in bundle["documents"]
                ]
            return response
        finally:
            with self._cond:
                workspace.busy = False
                self._cond.notify_all()

    def _call(self, operation: Callable[..., service.WorkflowResult], request: Any) -> service.WorkflowResult:
        try:
            return operation(request)
        except RepoDocsError as exc:
            raise JobError("operation-failure", self._safe(str(exc)), 422) from None

    def _provider_context(self, access: Access, handle: str) -> ProviderContext:
        try:
            return self._providers.context(access, handle)
        except UnknownHandle:
            raise _not_found("provider session") from None

    # ------------------------------------------------------------------ providers and consent
    def connect_provider(self, access: Access, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"provider", "host", "model", "api_key"} or not {"provider", "host", "model"} <= set(payload):
            raise _invalid("A provider connection holds provider, host, model and an optional api_key")
        key = payload.get("api_key")
        if key is not None and not isinstance(key, str):
            raise _invalid("api_key must be a string")
        try:
            handle = self._providers.connect(access, payload["provider"], payload["host"], payload["model"], key or None)
        except ValueError as exc:
            raise _invalid(str(exc)) from None
        return self._providers.public_view(access, handle)

    def provider(self, access: Access, handle: str) -> dict[str, Any]:
        self._provider_context(access, handle)
        return self._providers.public_view(access, handle)

    def disconnect_provider(self, access: Access, handle: str) -> None:
        self._providers.disconnect(access, handle)

    def grant_consent(self, access: Access, payload: Any) -> dict[str, Any]:
        """A receipt for the reviewed preview of one workspace and one provider session. It is held in memory only."""

        if not isinstance(payload, dict) or set(payload) != {"workspace_id", "provider_session_id", "context_hash", "kinds"}:
            raise _invalid("A consent request holds exactly workspace_id, provider_session_id, context_hash and kinds")
        kinds = payload["kinds"]
        if not isinstance(kinds, list) or not all(isinstance(item, str) for item in kinds):
            raise _invalid("kinds must be a list of consent kinds")
        workspace = self._workspace(access, payload["workspace_id"])
        if workspace.preview is None or payload["context_hash"] != workspace.preview["context_hash"]:
            raise JobError("stale-preview", "Consent is given for the current reviewed preview of this workspace; preview again", 409)
        handle = payload["provider_session_id"]
        context = self._provider_context(access, handle if isinstance(handle, str) else "")
        try:
            receipt = self._consent.grant(access, context, kinds=frozenset(kinds), policy=SOURCE_POLICY_VERSION, preview_hash=payload["context_hash"])
        except ValueError as exc:
            raise _invalid(str(exc)) from None
        return {"consent_receipt_id": receipt, "kinds": sorted(set(kinds)), "policy": SOURCE_POLICY_VERSION,
                "context_hash": payload["context_hash"], "provider": self._providers.public_view(access, handle)}

    def revoke_consent(self, access: Access, receipt: str) -> None:
        self._consent.revoke(access, receipt)

    def end_session(self, access: Access) -> None:
        """Clear every credential and consent receipt the session holds."""

        self._consent.revoke_all(access)
        self._providers.disconnect_all(access)

    def _gate(self, access: Access, request: JobRequest, context: ProviderContext) -> RunGate:
        kinds = frozenset({"external", "source-excerpts"}) if is_external(context) else frozenset()
        return RunGate(
            self._providers, self._consent, access, provider_session_id=request.provider_session_id or "",
            consent_receipt_id=request.consent_receipt_id, kinds=kinds, policy=SOURCE_POLICY_VERSION, preview_hash=request.context_hash or "",
        )

    # ------------------------------------------------------------------ jobs
    def submit(self, access: Access, payload: Any) -> tuple[dict[str, Any], bool]:
        """Admit a job, or return the original job for an identical repeated submission. ``True`` means newly created."""

        request = parse_request(payload)
        request_hash = sha256_bytes(canonical_json(request.normalized()).encode("utf-8"))
        with self._cond:
            if self._closed:
                raise JobError("unavailable", "The service is shutting down", 503)
            workspace = self._workspace(access, request.workspace_id)
            existing = self._keys.get((access.owner, request.workspace_id, request.idempotency_key))
            if existing is not None:
                job = self._jobs[existing]
                if job.request_hash != request_hash:
                    raise JobError("conflict", "This idempotency key was used for a different request in this workspace", 409)
                return job.public(), False
            budget = None
            if request.mode == "architecture":
                budget = self._admit_architecture(access, workspace, request)
            jobs = [job for job in self._jobs.values() if job.workspace_id == workspace.workspace_id]
            if len(jobs) >= self.settings.max_jobs_per_workspace:
                raise JobError("queue-full", f"This workspace has {len(jobs)} job records, the limit; use a new workspace", 429)
            if sum(job.state == "queued" for job in jobs) >= 1:
                raise JobError("queue-full", "This workspace already has a queued job; wait for it or cancel it", 429)
            if len(self._queue) >= self.settings.max_queued_jobs:
                raise JobError("queue-full", "The job queue is full; try again when a job finishes", 429)
            job = _Job(
                "job_" + secrets.token_hex(12), workspace.workspace_id, access.owner, request.idempotency_key, request_hash, request,
                "queued", "queued", 1, _usage(None, unknown=True), budget, created_at=self._clock(),
            )
            self._persist(job)
            self._jobs[job.job_id] = job
            self._keys[(access.owner, workspace.workspace_id, request.idempotency_key)] = job.job_id
            self._queue.append(job.job_id)
            self._start_workers()
            self._cond.notify_all()
            return job.public(), True

    def _admit_architecture(self, access: Access, workspace: _Workspace, request: JobRequest) -> dict[str, Any]:
        """Admission for paid or source-disclosing work: preview, provider session, consent and allowance."""

        if workspace.preview is None or request.context_hash != workspace.preview["context_hash"]:
            raise JobError("stale-preview", "The context changed or was never previewed; preview again and review it before submitting", 409)
        context = self._provider_context(access, request.provider_session_id or "")
        try:
            self._gate(access, request, context)
        except ConsentRequired as exc:
            raise JobError("consent-required", str(exc), 403) from None
        except UnknownHandle:
            raise _not_found("provider session") from None
        try:
            config = ProjectConfig.load(workspace.root)
            ledger = ledger_for(_identity(context), config.ai, request.run_limits())  # type: ignore[arg-type]
        except ConfigError as exc:
            raise _invalid(self._safe(str(exc))) from None
        if not service.can_reuse_view(service.DiscoverRequest(workspace.root, refresh=request.refresh, limits=request.run_limits())):
            fit = service.initial_request_fit(_identity(context), config, service.load_scanned_ir(config), ledger.limits)  # type: ignore[arg-type]
            if fit["error"] is not None:
                raise JobError("invalid-request", fit["error"]["message"], 422)
        # The worst case this job may use, reported before it starts. Unknown cost is null, never zero.
        return _budget(_summary_of(ledger.snapshot()))

    def get(self, access: Access, job_id: str) -> dict[str, Any]:
        with self._cond:
            return self._owned_job(access, job_id).public()

    def _owned_job(self, access: Access, job_id: Any) -> _Job:
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None or job.owner != access.owner:
            raise _not_found("job")
        return job

    def cancel(self, access: Access, job_id: str) -> dict[str, Any]:
        """Cancel a queued job at once, or ask a claimed or running one to stop at its next checkpoint.

        A finished job is unchanged. This runs under the same lock as ``_claim``, so a ``queued`` job is always still in the
        queue, and a job a worker has taken is already ``preflight``: its cancellation is checked before any work starts.
        """

        with self._cond:
            job = self._owned_job(access, job_id)
            if job.state == "queued":
                self._queue.remove(job.job_id)
                self._finish(job, "cancelled", "queued", _error("cancelled", "The job was cancelled before it started"), usage=dict(NO_REQUESTS))
            elif job.state in ACTIVE_STATES:
                job.cancel.cancel()
            return job.public()

    def artifact(self, access: Access, job_id: str, artifact_id: str) -> Artifact:
        """Bytes of one artifact this job produced, only while they are exactly what the job validated.

        The name is looked up in the job's own record, never joined to a path from the request. The file must be a regular
        file under the workspace's output directory with no link on the way, match the hash the job recorded and still be
        owned by the destination manifest. Anything else is refused as missing or stale.
        """

        with self._cond:
            job = self._owned_job(access, job_id)
            expected = job.artifacts.get(artifact_id) if isinstance(artifact_id, str) else None
        if expected is None or job.result is None or artifact_id not in job.result["artifact_ids"]:
            raise _not_found("artifact")
        root = _owned_path(self._manager.settings.store_root, job.workspace_id)
        if root is None:
            raise _not_found("artifact")
        config = ProjectConfig.load(root)
        output = config.output_path
        path = output / artifact_id
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise JobError("stale-artifact", "The artifact is not a regular file in the workspace output", 409)
        try:
            status = os.lstat(path)
        except OSError:
            raise JobError("stale-artifact", "The artifact no longer exists; run a new job", 409) from None
        if not stat.S_ISREG(status.st_mode) or path.resolve().parent != output.resolve() or root.resolve() not in path.resolve().parents:
            raise JobError("stale-artifact", "The artifact is not a regular file in the workspace output", 409)
        if status.st_size > self.settings.max_download_bytes:
            raise JobError("too-large", f"The artifact is larger than the {self.settings.max_download_bytes}-byte download limit", 413)
        content = path.read_bytes()
        manifest = self._manifest_hashes(config)
        if sha256_bytes(content) != expected or manifest.get(artifact_id) != expected:
            raise JobError("stale-artifact", "The artifact changed after this job validated it; run a new job for current files", 409)
        return Artifact(artifact_id, content, media_type(artifact_id))

    @staticmethod
    def _manifest_hashes(config: ProjectConfig) -> dict[str, str]:
        try:
            manifest = json.loads((config.internal_path / "artifact-manifest.json").read_text(encoding="utf-8"))
            return {item["path"]: item["content_hash"] for item in manifest["artifacts"]}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    # ------------------------------------------------------------------ the worker
    def _start_workers(self) -> None:
        while len(self._workers) < self.settings.max_active_jobs:
            worker = threading.Thread(target=self._work, name=f"repodocs-job-{len(self._workers)}", daemon=True)
            self._workers.append(worker)
            worker.start()

    def _claim(self) -> tuple[_Job, _Workspace] | None:
        """Take the first queued job whose workspace is free. Called with the condition lock held.

        Leaving the queue, marking the workspace busy and moving the job to ``preflight`` are one step, so no other thread
        can see a job that is ``queued`` but no longer in the queue. Nothing is claimed once the service is closing.
        """

        if self._closed:
            return None
        for job_id in self._queue:
            job = self._jobs[job_id]
            workspace = self._workspaces[job.workspace_id]
            if not workspace.busy:
                self._queue.remove(job_id)
                workspace.busy = True
                job.state, job.stage = "preflight", "scan"
                job.sequence += 1
                self._persist(job)
                return job, workspace
        return None

    def _work(self) -> None:
        while True:
            with self._cond:
                claimed = self._claim()
                while claimed is None and not self._closed:
                    self._cond.wait()
                    claimed = self._claim()
                if claimed is None:
                    return
            job, workspace = claimed
            try:
                self._run(job, workspace)
            finally:
                with self._cond:
                    workspace.busy = False
                    job.request = None  # handles are dropped with the run
                    self._cond.notify_all()

    def _transition(self, job: _Job, state: str, stage: str) -> None:
        """Move an active job to its next stage. A job that already ended keeps its terminal state."""

        with self._cond:
            if job.state not in ACTIVE_STATES:
                raise service.Cancelled("The job already ended")
            job.state, job.stage = state, stage
            job.sequence += 1
            self._persist(job)

    def _finish(self, job: _Job, state: str, stage: str, error: dict[str, Any] | None, *, result: dict[str, Any] | None = None,
                usage: dict[str, Any] | None = None, budget: dict[str, Any] | None = None, artifacts: dict[str, str] | None = None) -> None:
        """One atomic terminal update: state, error, result, usage and the artifact hashes together."""

        with self._cond:
            if job.state not in ACTIVE_STATES:
                return
            job.state, job.stage, job.error, job.result = state, stage, error, result
            if usage is not None:
                job.usage = usage
            if budget is not None:
                job.budget = budget
            job.artifacts = artifacts or {}
            job.sequence += 1
            self._persist(job)
            self._cond.notify_all()

    def _progress(self, job: _Job) -> Callable[[service.ProgressEvent], None]:
        def report(event: service.ProgressEvent) -> None:
            if event.usage is None:
                return
            summary = _summary_of(event.usage)
            with self._cond:
                if job.state not in ACTIVE_STATES:
                    return
                job.usage, job.budget = _usage(summary), _budget(summary)
                job.sequence += 1
                self._persist(job)

        return report

    def _run(self, job: _Job, workspace: _Workspace) -> None:
        request = job.request
        assert request is not None
        hooks = service.RunHooks(cancel=job.cancel, progress=self._progress(job))
        access = Access(job.owner)
        scrub_secret: Callable[[str], str] = lambda text: text  # noqa: E731
        summary: Mapping[str, Any] | None = None
        try:
            with workspace.lock:
                # The claim already made this job preflight/scan; a cancel that arrived since then stops it before any work.
                if job.cancel.cancelled:
                    raise service.Cancelled("The job was cancelled before it started")
                service.scan(service.ScanRequest(workspace.root), hooks)
                self._revalidate(workspace, keep=job.job_id)
                discovered: service.WorkflowResult | None = None
                if request.mode == "architecture":
                    self._transition(job, "preflight", "context")
                    preview = service.preview_context(service.ContextRequest(workspace.root), hooks)
                    if preview.data["context_hash"] != request.context_hash:
                        self._finish(job, "failed", "context", _error(
                            "stale-source", "The source context changed after it was previewed; nothing was sent. Preview again and give consent"
                        ), usage=dict(NO_REQUESTS))
                        return
                    discover_request = service.DiscoverRequest(workspace.root, refresh=request.refresh, limits=request.run_limits())
                    if not service.can_reuse_view(discover_request):
                        context = self._providers.context(access, request.provider_session_id or "")
                        gate = self._gate(access, request, context)
                        limits = ledger_for(_identity(context), ProjectConfig.load(workspace.root).ai, request.run_limits()).limits  # type: ignore[arg-type]

                        def make(current: ProviderContext) -> Provider:
                            nonlocal scrub_secret
                            if current.secret is not None:
                                value = current.secret.reveal()
                                scrub_secret = lambda text: text.replace(value, "[redacted]")  # noqa: E731
                            return self._factory(current, limits)

                        provider = gate.call(make)
                        discover_request = replace(discover_request, provider=provider, gate=gate)
                    self._transition(job, "running", "discover")
                    discovered = service.discover(discover_request, hooks)
                    summary = discovered.data.get("usage")
                self._transition(job, "running", "generate")
                generated = service.generate(service.GenerateRequest(workspace.root), hooks)
                self._transition(job, "running", "validate")
                validated = service.validate(service.ValidateRequest(workspace.root), hooks)
            self._complete(job, workspace, request, discovered, generated, validated, summary, scrub_secret)
        except service.Cancelled as exc:
            self._finish(job, "cancelled", job.stage, _error("cancelled", "The job was cancelled; no further request was sent"),
                         **self._spent(job, getattr(exc, "receipt", None) or summary))
        except BaseException as exc:  # noqa: BLE001 - a job failure must never stop the worker or leak a traceback
            spent = self._spent(job, getattr(exc, "receipt", None) or summary)
            if isinstance(exc, RunStopped):
                self._finish(job, "budget-exhausted", job.stage, _error("budget-exhausted", self._safe(scrub_secret(str(exc)))), **spent)
            elif isinstance(exc, ProviderError):
                self._finish(job, "failed", job.stage, _error("provider-failure", self._provider_message(exc, scrub_secret)), **spent)
            elif isinstance(exc, (SecurityError, ConsentError)):
                message = "The provider session, its credential or the consent ended or no longer matches; no further request was sent"
                self._finish(job, "failed", job.stage, _error("consent-required", message), **spent)
            elif isinstance(exc, RepoDocsError):
                self._finish(job, "failed", job.stage, _error("operation-failure", self._safe(scrub_secret(str(exc)))), **spent)
            else:
                self._finish(job, "failed", job.stage, _error("operation-failure", "The job failed unexpectedly; no result was produced"), **spent)
                if not isinstance(exc, Exception):
                    raise

    def _spent(self, job: _Job, summary: Mapping[str, Any] | None) -> dict[str, Any]:
        """Usage for a run that ended early: the receipt when one exists, the last progress otherwise, and known zero only
        when no model request could have started."""

        if summary is not None:
            return {"usage": _usage(summary), "budget": _budget(summary)}
        if job.stage in ("queued", "scan", "context") or job.request is None or job.request.mode == "deterministic":
            return {"usage": dict(NO_REQUESTS)}
        return {}

    def _complete(
        self, job: _Job, workspace: _Workspace, request: JobRequest, discovered: service.WorkflowResult | None,
        generated: service.WorkflowResult, validated: service.WorkflowResult, summary: Mapping[str, Any] | None,
        scrub_secret: Callable[[str], str],
    ) -> None:
        config = ProjectConfig.load(workspace.root)
        ir = service.load_scanned_ir(config)
        manifest = self._manifest_hashes(config)
        produced = {name: manifest[name] for name in validated.data["artifacts"] if name in manifest}
        chosen = {name: digest for name, digest in produced.items() if _view_of(name) in request.views}
        warnings = [self._safe(scrub_secret(item)) for item in (*(discovered.warnings if discovered else ()), *generated.warnings, *validated.warnings)]
        for view in request.views:
            if not any(_view_of(name) == view for name in chosen):
                reason = "it needs architecture mode" if view == "components" and request.mode == "deterministic" else "the engine produced no supported output for it"
                warnings.append(f"No {view} artifact: {reason}")
        view_hash = None
        if (config.internal_path / "architecture-view.json").exists():
            from ..architecture.discovery import load_view

            status, view, _ = load_view(config, ir)
            if view is not None and status == "current":
                view_hash = sha256_bytes(canonical_json(view).encode("utf-8"))
        state, error, completeness = "succeeded", None, "complete"
        if discovered is not None and discovered.status == service.STATUS_PARTIAL:
            reason = discovered.data.get("replacement_discarded") or discovered.data.get("partial_reason") or "provider-error"
            completeness = "partial"
            if reason == "usage-budget":
                state, code = "budget-exhausted", "budget-exhausted"
            else:
                state, code = "partial", "budget-exhausted" if reason in _STRUCTURAL_BUDGETS else "provider-failure"
            kept = "replacement_discarded" in discovered.data
            error = _error(code, (
                f"Discovery ended early ({reason}); "
                + ("the previous complete view was kept and the artifacts show it" if kept else "the architecture result is partial")
            ))
            warnings.append("Not a current complete architecture result")
        result = {"completeness": completeness, "view_hash": view_hash, "source_ir_hash": ir_hash(ir),
                  "artifact_ids": sorted(chosen), "warnings": warnings}
        usage = NO_REQUESTS if summary is None else _usage(summary)
        budget = None if summary is None else _budget(summary)
        self._finish(job, state, "finished", error, result=result, usage=dict(usage), budget=budget, artifacts=chosen)

    def _revalidate(self, workspace: _Workspace, keep: str | None = None) -> None:
        """After a scan, mark earlier results of this workspace stale when the sources they describe have changed."""

        ir = service.load_scanned_ir(ProjectConfig.load(workspace.root))
        current = ir_hash(ir)
        with self._cond:
            for job in self._jobs.values():
                if (job.workspace_id == workspace.workspace_id and job.job_id != keep and job.result is not None
                        and job.state != "stale" and job.result["source_ir_hash"] != current):
                    job.state, job.stage = "stale", "finished"
                    job.error = _error("stale-source", "The sources changed after this result; it is kept for inspection and is not current")
                    job.sequence += 1
                    self._persist(job)

    # ------------------------------------------------------------------ messages
    def _safe(self, message: str) -> str:
        """A message fit for the browser: no server paths, no secret-shaped text, no control characters, bounded."""

        text = scrub(message)
        settings = self._manager.settings
        for name, path in sorted(settings.local_roots.items(), key=lambda pair: -len(str(pair[1]))):
            text = text.replace(str(path), f"<source:{name}>").replace(path.as_posix(), f"<source:{name}>")
        store = settings.store_root
        text = text.replace(str(store), "<store>").replace(store.as_posix(), "<store>")
        text, _ = redact_text(text)
        return text[:MAX_MESSAGE_LENGTH] or "The operation failed"

    def _provider_message(self, exc: ProviderError, scrub_secret: Callable[[str], str]) -> str:
        """Our own provider message, without any provider body. Providers build their messages; nothing echoes a response."""

        return self._safe(scrub_secret(str(exc)).splitlines()[0] if str(exc) else "The provider request failed") + (
            ". No architecture result was produced from it; deterministic outputs are unaffected"
        )
