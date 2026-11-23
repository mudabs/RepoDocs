"""The application workflow shared by the CLI and any in-process adapter (ADR-030).

Each operation takes an immutable request and returns a versioned ``WorkflowResult`` or raises a
``RepoDocsError``. Nothing here prints, exits, starts a server, or reads credentials from the
environment: a provider is an object the caller passes in for one run, so concurrent runs never
share process-wide state. The operations wrap the existing scan, context, discovery, generation and
validation code; they do not reimplement it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .ai.budgets import RunLimits, RunStopped, budgeted, ledger_for, write_receipt
from .ai.providers import Provider, ProviderResponse, require_source_consent
from .common import write_json
from .confirmations import apply_decisions, load_decisions, status_counts, unresolved_candidates
from .config import ProjectConfig
from .errors import (
    EXIT_AI_PROVIDER,
    EXIT_CONFIRMATION,
    EXIT_OWNERSHIP,
    EXIT_PUBLICATION,
    EXIT_SNAPSHOT,
    EXIT_USAGE,
    EXIT_VALIDATION,
    ConfigError,
    ConfirmationError,
    ConsentError,
    OwnershipError,
    ProviderError,
    PublicationError,
    RepoDocsError,
    SnapshotError,
    ValidationError,
)
from .generator import generate_docs
from .ir import write_ir
from .scanner import scan_project
from .snapshot import SNAPSHOT_MODE_LOCAL
from .validation import load_ir, validate_candidate_states, validate_ir, validate_project

SERVICE_VERSION = "1.0"

# How a run ended. A run is `partial` when discovery ended early; the stored view says why.
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

EXIT_CANCELLED = 130


class Cancelled(RepoDocsError):
    """The caller asked the run to stop. Nothing after the last checkpoint was committed."""


class CancellationToken:
    """A thread-safe flag a caller sets to stop a run at its next checkpoint."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class ProgressEvent:
    operation: str
    stage: str


@dataclass(frozen=True)
class RunHooks:
    """Optional cancellation and progress callbacks. Cancellation is cooperative, between bounded steps."""

    cancel: CancellationToken | None = None
    progress: Callable[[ProgressEvent], None] | None = None


NO_HOOKS = RunHooks()


def _stop_if_cancelled(hooks: RunHooks, operation: str, stage: str) -> None:
    if hooks.cancel is not None and hooks.cancel.cancelled:
        raise Cancelled(f"{operation} was cancelled before {stage}")


def _checkpoint(hooks: RunHooks, operation: str, stage: str) -> None:
    _stop_if_cancelled(hooks, operation, stage)
    if hooks.progress is not None:
        hooks.progress(ProgressEvent(operation, stage))


# ------------------------------------------------------------------ requests
@dataclass(frozen=True)
class ProjectRequest:
    root: Path


@dataclass(frozen=True)
class ScanRequest(ProjectRequest):
    snapshot_mode: str = SNAPSHOT_MODE_LOCAL
    pins: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pins", MappingProxyType(dict(self.pins)))


@dataclass(frozen=True)
class ContextRequest(ProjectRequest):
    """A preview of what discovery would send. ``provider`` only labels whether it is external; it is never called."""

    provider: Provider | None = None


@dataclass(frozen=True)
class DiscoverRequest(ProjectRequest):
    """``provider`` is this run's own object, already holding any credential. It is required unless a current view is reused.

    ``limits`` are this run's own usage limits. They can only lower the project's ``ai.limits`` (ADR-031). The service
    wraps the provider in a run ledger after the consent checks, unless the caller passes one already budgeted.
    """

    provider: Provider | None = None
    refresh: bool = False
    limits: RunLimits | None = None


@dataclass(frozen=True)
class GenerateRequest(ProjectRequest):
    retire_architecture: bool = False


@dataclass(frozen=True)
class ValidateRequest(ProjectRequest):
    allow_unresolved: bool = False


# ------------------------------------------------------------------ results and errors
@dataclass(frozen=True)
class WorkflowError:
    code: str
    message: str
    exit_code: int


# Most specific first.
_ERRORS: tuple[tuple[type[RepoDocsError], str, int], ...] = (
    (Cancelled, "cancelled", EXIT_CANCELLED),
    (SnapshotError, "snapshot", EXIT_SNAPSHOT),
    (OwnershipError, "ownership", EXIT_OWNERSHIP),
    (ConfirmationError, "confirmation", EXIT_CONFIRMATION),
    (PublicationError, "publication", EXIT_PUBLICATION),
    (RunStopped, "budget", EXIT_AI_PROVIDER),
    (ProviderError, "provider", EXIT_AI_PROVIDER),
    (ValidationError, "validation", EXIT_VALIDATION),
    (RepoDocsError, "usage", EXIT_USAGE),
)


def describe_error(exc: RepoDocsError) -> WorkflowError:
    """The stable code, message and CLI exit code for an expected failure."""

    for kind, code, exit_code in _ERRORS:
        if isinstance(exc, kind):
            message = f"{exc}. Deterministic scan, generate, and validate are unaffected" if code in ("provider", "budget") else str(exc)
            return WorkflowError(code, message, exit_code)
    raise TypeError(f"{type(exc).__name__} is not a RepoDocsError")  # pragma: no cover


@dataclass(frozen=True)
class WorkflowResult:
    """A versioned, JSON-serializable outcome. ``data`` is the same payload the CLI prints.

    ``view_status`` is the stored architecture view's state (missing, current, stale or invalid) when
    the operation looked at it; a run's ``status`` is separate from it.
    """

    operation: str
    status: str
    data: Any = None
    warnings: tuple[str, ...] = ()
    error: WorkflowError | None = None
    view_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_version": SERVICE_VERSION,
            "operation": self.operation,
            "status": self.status,
            "view_status": self.view_status,
            "data": self.data,
            "warnings": list(self.warnings),
            "error": None if self.error is None else {"code": self.error.code, "message": self.error.message},
        }


# ------------------------------------------------------------------ shared helpers
def load_scanned_ir(config: ProjectConfig) -> dict[str, Any]:
    ir = load_ir(config.internal_path / "ir.json")
    validate_ir(ir)
    return ir


def require_resolved_candidates(ir: dict[str, Any]) -> None:
    unresolved = unresolved_candidates(ir)
    if unresolved:
        shown = ", ".join(item["id"] for item in unresolved[:10])
        raise ConfirmationError(
            f"{len(unresolved)} cross-repository candidate(s) require confirmation or rejection "
            f"(unresolved or stale): {shown}{', ...' if len(unresolved) > 10 else ''}; "
            "list them with `repodocs candidates`"
        )


def _view_status(config: ProjectConfig, ir: dict[str, Any]) -> str:
    from .architecture.discovery import load_view

    return load_view(config, ir)[0]


class _CancellableProvider:
    """Checks for cancellation before each request. The raised ``Cancelled`` is not a ``ProviderError``,
    so discovery does not turn it into a partial view; it leaves the run unstored."""

    def __init__(self, inner: Provider, hooks: RunHooks) -> None:
        self._inner = inner
        self._hooks = hooks
        self.name, self.model, self.external = inner.name, inner.model, inner.external
        # The exact request body is a pure function; keep it visible through this wrapper. Token counting is
        # not forwarded: the run ledger inside is its only caller (ADR-031).
        if hasattr(inner, "request_body"):
            self.request_body = inner.request_body

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        _checkpoint(self._hooks, "discover", "provider-request")
        return self._inner.generate(system, user, schema)


# ------------------------------------------------------------------ operations
def scan(request: ScanRequest, hooks: RunHooks = NO_HOOKS) -> WorkflowResult:
    config = ProjectConfig.load(request.root)
    _checkpoint(hooks, "scan", "scanning")
    ir, warnings = scan_project(config, mode=request.snapshot_mode, pins=dict(request.pins))
    apply_decisions(ir, load_decisions(config.root))
    validate_ir(ir)
    _checkpoint(hooks, "scan", "writing-ir")
    config.internal_path.mkdir(parents=True, exist_ok=True)
    ir_path = config.internal_path / "ir.json"
    write_ir(ir_path, ir)
    data = {
        "ir": ir_path.as_posix(),
        "snapshot_mode": request.snapshot_mode,
        "repositories": len(ir["snapshots"]),
        "snapshots": [
            {
                "repository_id": snapshot["repository_id"],
                "revision_kind": snapshot["revision_kind"],
                "revision": snapshot["revision"],
                "working_tree_state": snapshot["working_tree_state"],
            }
            for snapshot in ir["snapshots"]
        ],
        "nodes": len(ir["nodes"]),
        "relationships": len(ir["relationships"]),
        "evidence": len(ir["evidence"]),
        "claims": len(ir["claims"]),
        "limitations": len(ir["limitations"]),
        "candidates": len(ir["candidates"]),
        "candidate_states": status_counts(ir),
        "analyzers": ir["provenance"]["analyzers"],
    }
    return WorkflowResult("scan", STATUS_COMPLETE, data, tuple(warnings))


def preview_context(request: ContextRequest, hooks: RunHooks = NO_HOOKS) -> WorkflowResult:
    """Write the bounded, redacted source context for review. Never contacts a provider."""

    from .architecture.context import CONTEXT_FILENAME, build_context, context_hash

    config = ProjectConfig.load(request.root)
    ir = load_scanned_ir(config)
    _checkpoint(hooks, "context", "building-context")
    bundle, _ = build_context(config, ir)
    path = config.internal_path / CONTEXT_FILENAME
    write_json(path, bundle)
    reasons: dict[str, int] = {}
    for item in bundle["omitted"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    budget: dict[str, Any] | None = None
    if request.provider is not None:
        # The limits and worst-case cost a run would start with. Nothing is counted or sent.
        snapshot = ledger_for(request.provider, config.ai).snapshot()
        budget = {key: snapshot[key] for key in ("limits", "price_profile", "max_estimated_cost_usd", "notes")}
    data = {
        "context": path.as_posix(),
        "context_version": bundle["context_version"],
        "context_hash": context_hash(bundle),
        "source_ir_hash": bundle["source_ir_hash"],
        "documents": len(bundle["documents"]),
        "context_bytes": bundle["usage"]["context_bytes"],
        "redactions": bundle["redaction"]["replacements"],
        "omitted": dict(sorted(reasons.items())),
        "provider": config.ai.provider,
        "provider_is_external": None if request.provider is None else bool(request.provider.external),
        "source_consent": config.ai.source_consent_current,
        "budget": budget,
    }
    return WorkflowResult("context", STATUS_COMPLETE, data, view_status=_view_status(config, ir))


def can_reuse_view(request: DiscoverRequest) -> bool:
    """Whether ``discover`` would return the stored view without needing a provider."""

    from .architecture.discovery import STATUS_CURRENT, load_view

    config = ProjectConfig.load(request.root)
    ir = load_scanned_ir(config)
    validate_candidate_states(config, ir)
    status, view, _ = load_view(config, ir)
    return status == STATUS_CURRENT and view is not None and not request.refresh


def _discovery_summary(config: ProjectConfig, view: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    from .architecture.discovery import VIEW_FILENAME

    return {
        "status": view["status"],
        "reused": reused,
        "view": (config.internal_path / VIEW_FILENAME).as_posix(),
        "components": len(view["components"]),
        "connections": len(view["connections"]),
        "inferred": sum(item["classification"] == "inferred" for item in view["components"] + view["connections"]),
        "proposed": sum(item["classification"] == "proposed" for item in view["components"] + view["connections"]),
        "rejected": view["rejected"],
        "provenance": view["provenance"],
        **({"partial_reason": view["partial_reason"]} if view["status"] == "partial" else {}),
    }


def discover(request: DiscoverRequest, hooks: RunHooks = NO_HOOKS) -> WorkflowResult:
    """Run bounded architecture discovery and store the validated view. Contacts the provider only here."""

    from .architecture.context import CONTEXT_FILENAME, build_context
    from .architecture.discovery import STATUS_CURRENT, discover as run_discovery, load_view, save_view

    config = ProjectConfig.load(request.root)
    _checkpoint(hooks, "discover", "loading")
    ir = load_scanned_ir(config)
    validate_candidate_states(config, ir)
    status, view, _ = load_view(config, ir)
    if status == STATUS_CURRENT and not request.refresh and view is not None:
        return _discovered(config, view, reused=True, view_status=status)
    provider = request.provider
    if provider is None:
        raise ConfigError(
            "Architecture discovery needs an AI provider; choose one with `repodocs ai-configure --provider ollama --model <name>`. "
            "Scan, generate, and validate work without one"
        )
    # Both consent scopes are re-checked here, in the CLI resolver's order, because an adapter injects providers
    # directly and so never passes through that resolver.
    if provider.external and not config.ai.external_consent:
        raise ConsentError(
            f"The {provider.name} provider would send the evidence bundle off this machine and no consent is "
            "recorded; this project is local-only. Review the bundle with `repodocs ai-bundle`, then run "
            "`repodocs ai-configure --consent-external` to allow it"
        )
    require_source_consent(provider, config.ai)
    # Every request, count, retry and repair of this run is reserved in one ledger, created only after consent.
    budget = budgeted(provider, config.ai, request.limits, before_send=lambda: _stop_if_cancelled(hooks, "discover", "provider-request"))
    preview, _ = build_context(config, ir)
    write_json(config.internal_path / CONTEXT_FILENAME, preview)
    try:
        new_view, context = run_discovery(config, ir, _CancellableProvider(budget, hooks))
        _checkpoint(hooks, "discover", "storing-view")
    except BaseException as exc:
        if isinstance(exc, Cancelled):
            budget.cancel()
        receipt = write_receipt(config.internal_path, "architecture-discover", budget)
        if isinstance(exc, RepoDocsError):
            exc.receipt = receipt  # type: ignore[attr-defined]
        raise
    usage = write_receipt(config.internal_path, "architecture-discover", budget)
    # A replacement that ended early never displaces a current, complete view the user can still rely on.
    if status == STATUS_CURRENT and view is not None and view["status"] == "complete" and new_view["status"] != "complete":
        return _discovered(
            config, view, reused=True, view_status=status, discarded=new_view["partial_reason"], usage=usage,
            warnings=(f"The new discovery ended early ({new_view['partial_reason']}); the previous complete view was kept",),
        )
    try:
        save_view(config, new_view, context)
    except OSError as exc:
        error = RepoDocsError(f"The architecture view could not be stored ({exc}); the previous view, if any, was left unchanged")
        error.receipt = usage  # type: ignore[attr-defined]
        raise error from exc
    warnings = []
    if status in ("stale", "invalid"):
        warnings.append(f"The previous architecture view was {status} and has been replaced")
    if new_view["status"] == "partial":
        warnings.append(f"Discovery stopped early ({new_view['partial_reason']}); the view is partial")
    return _discovered(config, new_view, reused=False, view_status=STATUS_CURRENT, usage=usage, warnings=tuple(warnings))


def _discovered(
    config: ProjectConfig, view: dict[str, Any], *, reused: bool, view_status: str, discarded: str | None = None,
    usage: dict[str, Any] | None = None, warnings: tuple[str, ...] = (),
) -> WorkflowResult:
    data = _discovery_summary(config, view, reused=reused)
    if discarded is not None:
        data["replacement_discarded"] = discarded
    if usage is not None:
        data["usage"] = usage
    # The run is partial when the view it ended with, or the replacement it discarded, was incomplete.
    status = STATUS_PARTIAL if discarded is not None or view["status"] == "partial" else STATUS_COMPLETE
    return WorkflowResult("discover", status, data, warnings, view_status=view_status)


def generate(request: GenerateRequest, hooks: RunHooks = NO_HOOKS) -> WorkflowResult:
    """Render artifacts from stored state. Never contacts a provider."""

    config = ProjectConfig.load(request.root)
    _checkpoint(hooks, "generate", "loading")
    ir = load_scanned_ir(config)
    # Never render a decision the confirmations file no longer records.
    validate_candidate_states(config, ir)
    # The AI package is loaded only when claims have been stored; otherwise generation is
    # purely deterministic and needs nothing from it.
    ai_status, optional = "absent", {}
    architecture_status, architecture_warnings = "absent", []
    view_status = "missing"
    if (config.internal_path / "ai-claims.json").exists():
        from .ai.docs import ai_artifact

        ai_status, optional = ai_artifact(config.internal_path, ir)
    # Stored architecture views are rendered from disk only. With no stored view at all (a clean clone: the
    # view is local state) earlier architecture files are kept and reported as unverified rather than deleted.
    # `--retire-architecture` removes them on purpose.
    retain_architecture = False
    if request.retire_architecture:
        architecture_status = "retired"
    elif (config.internal_path / "architecture-view.json").exists():
        from .architecture.discovery import load_view
        from .architecture.export import architecture_artifacts

        architecture_status = view_status = load_view(config, ir)[0]
        architecture, architecture_warnings = architecture_artifacts(config, ir)
        optional = {**optional, **architecture}
    else:
        retain_architecture = True
    # The last chance to stop: after this, files are written.
    _checkpoint(hooks, "generate", "writing-artifacts")
    result = generate_docs(config, ir, optional, retain_architecture=retain_architecture)
    warnings = list(architecture_warnings)
    kept = [name for name in result.artifacts if name.startswith("architecture-view") and name not in optional]
    if kept:
        architecture_status = "unverified"
        warnings.append(
            "No stored architecture view is available here, so these earlier files were kept and their freshness was not "
            "verified against the current source revisions: " + ", ".join(kept) + ". Run `repodocs architecture discover` "
            "to refresh them, or `repodocs generate --retire-architecture` to remove them"
        )
    if ai_status in ("stale", "invalid"):
        warnings.append(
            f"Stored AI claims are {ai_status} for the current System IR and were not rendered; "
            "run `repodocs ai-interpret` to refresh them"
        )
    data = {
        "changed": result.changed,
        "destination": result.destination,
        "output_directory": config.output_path.as_posix(),
        "artifacts": result.artifacts,
        "ai": {"claims": ai_status, "rendered": "architecture-ai.md" in optional},
        "architecture": {"view": architecture_status, "rendered": any(name.startswith("architecture-view") for name in optional)},
    }
    return WorkflowResult("generate", STATUS_COMPLETE, data, tuple(warnings), view_status=view_status)


def validate(request: ValidateRequest, hooks: RunHooks = NO_HOOKS) -> WorkflowResult:
    config = ProjectConfig.load(request.root)
    _checkpoint(hooks, "validate", "validating")
    artifacts, ir = validate_project(config)
    if not request.allow_unresolved:
        require_resolved_candidates(ir)
    warnings = []
    view_status = "missing"
    if (config.internal_path / "architecture-view.json").exists():
        from .architecture.discovery import STATUS_CURRENT, load_view

        view_status, _, reason = load_view(config, ir)
        if view_status != STATUS_CURRENT:
            warnings.append(f"The stored architecture view is {view_status}: {reason}")
    else:
        kept = [name for name in artifacts if name.startswith("architecture-view")]
        if kept:
            warnings.append(
                "No stored architecture view is available here; these files are unedited but their freshness against the "
                "current source revisions was not verified: " + ", ".join(kept)
            )
    data = {
        "destination": config.destination().id,
        "artifacts": artifacts,
        "candidate_states": status_counts(ir),
    }
    return WorkflowResult("validate", STATUS_COMPLETE, data, tuple(warnings), view_status=view_status)


OPERATIONS: Mapping[str, Callable[[Any, RunHooks], WorkflowResult]] = MappingProxyType(
    {"scan": scan, "context": preview_context, "discover": discover, "generate": generate, "validate": validate}
)


def execute(operation: str, request: ProjectRequest, hooks: RunHooks = NO_HOOKS) -> WorkflowResult:
    """Run an operation and report an expected failure or cancellation as a result instead of raising.

    A bug (any exception that is not a ``RepoDocsError``) still propagates.
    """

    try:
        return OPERATIONS[operation](request, hooks)
    except RepoDocsError as exc:
        error = describe_error(exc)
        # A run that reached a provider keeps its usage receipt even when it failed or was cancelled.
        receipt = getattr(exc, "receipt", None)
        return WorkflowResult(
            operation, STATUS_CANCELLED if isinstance(exc, Cancelled) else STATUS_FAILED,
            data=None if receipt is None else {"usage": receipt}, error=error,
        )
