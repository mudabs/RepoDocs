"""Access, session, credential and consent boundary for the local web adapter (ADR-033).

Pure Python and in memory: no HTTP server, no storage, no environment access, no logging. The
HTTP adapter (#72) passes request headers to `SessionGuard` before it reveals or starts anything,
and routes every provider, token-count and retry request through `RunGate.call`.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from typing import Any, TypeVar
from urllib.parse import urlsplit

from ..errors import RepoDocsError

T = TypeVar("T")

COOKIE_NAME = "repodocs_session"
CSRF_HEADER = "x-csrf-token"
MIN_AUTH_TOKEN_LENGTH = 32
SAFE_METHODS = frozenset({"GET", "HEAD"})
CONSENT_KINDS = frozenset({"external", "source-excerpts"})
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class SecurityError(RepoDocsError):
    """A refused web operation. `code` and `status` are stable; messages never contain secrets."""

    code = "access-denied"
    status = 403


class AccessConfigError(SecurityError, ValueError):
    """The requested exposure is not safe."""

    code = "invalid-configuration"
    status = 500


class AccessDenied(SecurityError):
    """Host, origin, fetch-site or CSRF check failed."""


class SessionRequired(AccessDenied):
    code = "session-required"
    status = 401


class UnknownHandle(SecurityError):
    """Unknown, expired or inaccessible provider-session handle."""

    code = "unknown-handle"
    status = 404


class ConsentRequired(SecurityError):
    code = "consent-required"
    status = 403


class LimitExceeded(SecurityError):
    code = "limit-exceeded"
    status = 429


def generate_auth_token() -> str:
    return secrets.token_urlsafe(MIN_AUTH_TOKEN_LENGTH)


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(_digest(a), _digest(b))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _authority(host: str, port: int) -> str:
    return host if port == 80 else f"{host}:{port}"


@dataclass(frozen=True)
class AccessConfig:
    """Where the service listens and which browser origins may use it.

    Loopback with no public origin is the default and needs no token. Any other bind address, or
    any public origin (for example a reverse proxy on loopback), needs an explicit HTTPS origin
    and an authentication token of at least 32 characters. TLS termination is not done here.
    """

    bind_host: str = "127.0.0.1"
    port: int = 8000
    public_origin: str | None = None
    auth_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise AccessConfigError("The port must be between 1 and 65535")
        if not self.exposed:
            return
        if not self.public_origin:
            raise AccessConfigError(
                "Binding beyond loopback needs an explicit HTTPS public origin and an authentication token"
            )
        try:
            parts = urlsplit(self.public_origin)
            parts.port  # noqa: B018  (validates the port)
        except ValueError as exc:
            raise AccessConfigError("The public origin is not a valid URL") from exc
        if (
            parts.scheme != "https"
            or not parts.hostname
            or "*" in parts.netloc
            or "@" in parts.netloc
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise AccessConfigError("The public origin must be a bare https://host[:port] origin without a wildcard")
        if not self.auth_token or len(self.auth_token) < MIN_AUTH_TOKEN_LENGTH:
            raise AccessConfigError(
                f"Non-loopback exposure needs an authentication token of at least {MIN_AUTH_TOKEN_LENGTH} characters"
            )

    @property
    def exposed(self) -> bool:
        return not _is_loopback(self.bind_host) or self.public_origin is not None

    @property
    def allowed_origins(self) -> frozenset[str]:
        if self.exposed:
            return frozenset({f"https://{urlsplit(self.public_origin or '').netloc.lower()}"})
        return frozenset(f"http://{_authority(name, self.port)}" for name in _LOOPBACK_NAMES)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(origin.split("://", 1)[1] for origin in self.allowed_origins)


@dataclass(frozen=True)
class Access:
    """An authorized request's identity. `owner` is a public id, not the session secret."""

    owner: str


@dataclass(frozen=True)
class Bootstrap:
    """First-contact result for a trusted same-origin browser."""

    owner: str
    csrf_token: str = field(repr=False)
    set_cookie: str = field(repr=False)


@dataclass(frozen=True)
class _Session:
    owner: str
    csrf_digest: bytes
    expires_at: float


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


class SessionGuard:
    """Exact Host/Origin checks plus an expiring, bounded session with a CSRF token.

    A trusted browser gets its first session by loading the UI from the service's own origin
    (`open_session`); no account or OAuth is involved. The CSRF token is returned in the response
    body, so a cross-site page cannot read it, and the session cookie is HttpOnly and SameSite=Strict.
    Beyond loopback, `open_session` also requires the configured token.
    """

    def __init__(
        self,
        config: AccessConfig,
        *,
        ttl: float = 8 * 3600,
        max_sessions: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._ttl = ttl
        self._max = max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[bytes, _Session] = {}

    def open_session(self, headers: Mapping[str, str], auth_token: str | None = None) -> Bootstrap:
        self._check_origin(_lower(headers), unsafe=False)
        if self._config.exposed and not (auth_token and _same(auth_token, self._config.auth_token or "")):
            raise SessionRequired("A valid authentication token is required")
        cookie, csrf, owner = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_hex(8)
        with self._lock:
            self._purge()
            if len(self._sessions) >= self._max:
                raise LimitExceeded("Too many active sessions")
            self._sessions[_digest(cookie)] = _Session(owner, _digest(csrf), self._clock() + self._ttl)
        attributes = f"Path=/; HttpOnly; SameSite=Strict; Max-Age={int(self._ttl)}"
        if self._config.exposed:
            attributes += "; Secure"
        return Bootstrap(owner, csrf, f"{COOKIE_NAME}={cookie}; {attributes}")

    def authorize(self, method: str, headers: Mapping[str, str]) -> Access:
        """Check one request. Run this before revealing any job or starting any paid work."""
        h = _lower(headers)
        unsafe = method.upper() not in SAFE_METHODS
        self._check_origin(h, unsafe=unsafe)
        session = self._session(h.get("cookie", ""))
        if unsafe:
            supplied = h.get(CSRF_HEADER)
            if not supplied or not hmac.compare_digest(_digest(supplied), session.csrf_digest):
                raise AccessDenied("The CSRF token is missing or wrong")
        return Access(session.owner)

    def end_session(self, access: Access) -> None:
        with self._lock:
            for key in [k for k, s in self._sessions.items() if s.owner == access.owner]:
                del self._sessions[key]

    def is_live(self, owner: str) -> bool:
        with self._lock:
            self._purge()
            return any(s.owner == owner for s in self._sessions.values())

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[key]

    def _session(self, cookie_header: str) -> _Session:
        try:
            morsel = SimpleCookie(cookie_header).get(COOKIE_NAME)
        except CookieError:
            morsel = None
        with self._lock:
            self._purge()
            session = self._sessions.get(_digest(morsel.value)) if morsel else None
        if session is None:
            raise SessionRequired("A session is required")
        return session

    def _check_origin(self, h: Mapping[str, str], *, unsafe: bool) -> None:
        if h.get("host", "").lower() not in self._config.allowed_hosts:
            raise AccessDenied("The Host header is not allowed")
        origin = h.get("origin")
        if origin is None and unsafe:
            raise AccessDenied("State-changing requests need an Origin header")
        if origin is not None and origin.lower() not in self._config.allowed_origins:
            raise AccessDenied("The Origin is not allowed")
        site = h.get("sec-fetch-site")
        if site is not None and site not in (("same-origin",) if unsafe else ("same-origin", "none")):
            raise AccessDenied("Cross-site requests are not allowed")


def response_headers(*, artifact: bool = False) -> dict[str, str]:
    """Defensive headers for every response. There is deliberately no CORS header.

    `artifact=True` is for downloaded repository-derived files (HTML, SVG, Markdown): they are
    attachments in a script-free sandbox, so repository text cannot execute in the service origin.
    """
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:"
        ),
    }
    if artifact:
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; frame-ancestors 'none'"
        headers["Content-Disposition"] = "attachment"
    return headers


class Secret:
    """A credential that never appears in repr, str, format, JSON, pickle or copies."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("A credential cannot be empty")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(redacted)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("A credential cannot be serialized or copied")


@dataclass(frozen=True)
class ProviderContext:
    """Immutable per-run provider identity and credential, passed to an operation, never global."""

    provider: str
    host: str
    model: str
    secret: Secret | None


def _scope(context: ProviderContext) -> tuple[str, str, str]:
    return (context.provider, context.host, context.model)


@dataclass(frozen=True)
class _ProviderEntry:
    owner: str
    context: ProviderContext
    expires_at: float


class ProviderSessions:
    """In-memory, expiring, bounded provider credentials, each owned by one web session."""

    def __init__(
        self,
        guard: SessionGuard,
        *,
        ttl: float = 3600,
        max_sessions: int = 16,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._guard = guard
        self._ttl = ttl
        self._max = max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _ProviderEntry] = {}

    def connect(self, access: Access, provider: str, host: str, model: str, api_key: str | None = None) -> str:
        if not all(isinstance(v, str) and v.strip() and len(v) <= 200 for v in (provider, host, model)):
            raise ValueError("Provider, host and model are required")
        self._require_live(access)
        context = ProviderContext(
            provider.strip(), host.strip().lower(), model.strip(), Secret(api_key) if api_key else None
        )
        handle = "ps_" + secrets.token_urlsafe(16)
        with self._lock:
            self._purge()
            if len(self._entries) >= self._max:
                raise LimitExceeded("Too many provider sessions")
            self._entries[handle] = _ProviderEntry(access.owner, context, self._clock() + self._ttl)
        return handle

    def context(self, access: Access, handle: str) -> ProviderContext:
        self._require_live(access)
        with self._lock:
            self._purge()
            entry = self._entries.get(handle)
        if entry is None or entry.owner != access.owner:
            raise UnknownHandle("The provider session is unknown or expired")
        return entry.context

    def public_view(self, access: Access, handle: str) -> dict[str, Any]:
        """The only form of a provider session that is safe to return to the browser."""
        context = self.context(access, handle)
        return {
            "provider_session_id": handle,
            "provider": context.provider,
            "host": context.host,
            "model": context.model,
            "credential": "stored" if context.secret else "none",
        }

    def disconnect(self, access: Access, handle: str) -> None:
        """Clear one credential. Idempotent; a handle owned by someone else is left alone."""
        with self._lock:
            entry = self._entries.get(handle)
            if entry is not None and entry.owner == access.owner:
                del self._entries[handle]

    def disconnect_all(self, access: Access) -> None:
        with self._lock:
            for handle in [h for h, e in self._entries.items() if e.owner == access.owner]:
                del self._entries[handle]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _require_live(self, access: Access) -> None:
        if not self._guard.is_live(access.owner):
            raise SessionRequired("A session is required")

    def _purge(self) -> None:
        now = self._clock()
        for handle in [h for h, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[handle]


@dataclass(frozen=True)
class _Consent:
    owner: str
    kinds: frozenset[str]
    scope: tuple[str, str, str]
    policy: str
    preview_hash: str
    expires_at: float


class ConsentLedger:
    """Consent receipts bound to owner, provider, host, model, policy and reviewed preview.

    `external` (a provider receives the redacted evidence) and `source-excerpts` are separate
    kinds: one never implies the other. Revoking deletes the receipt. Consent is not persisted.
    """

    def __init__(
        self,
        guard: SessionGuard,
        *,
        ttl: float = 3600,
        max_receipts: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._guard = guard
        self._ttl = ttl
        self._max = max_receipts
        self._clock = clock
        self._lock = threading.Lock()
        self._receipts: dict[str, _Consent] = {}

    def grant(
        self, access: Access, context: ProviderContext, *, kinds: frozenset[str], policy: str, preview_hash: str
    ) -> str:
        if not kinds or not kinds <= CONSENT_KINDS:
            raise ValueError("Consent kinds must be a non-empty subset of external and source-excerpts")
        if not policy or not preview_hash:
            raise ValueError("Consent is bound to a policy and a reviewed preview hash")
        if not self._guard.is_live(access.owner):
            raise SessionRequired("A session is required")
        receipt = "cr_" + secrets.token_urlsafe(16)
        with self._lock:
            self._purge()
            if len(self._receipts) >= self._max:
                raise LimitExceeded("Too many consent receipts")
            self._receipts[receipt] = _Consent(
                access.owner, kinds, _scope(context), policy, preview_hash, self._clock() + self._ttl
            )
        return receipt

    def revoke(self, access: Access, receipt: str) -> None:
        with self._lock:
            consent = self._receipts.get(receipt)
            if consent is not None and consent.owner == access.owner:
                del self._receipts[receipt]

    def revoke_all(self, access: Access) -> None:
        with self._lock:
            for receipt in [r for r, c in self._receipts.items() if c.owner == access.owner]:
                del self._receipts[receipt]

    def check(
        self,
        access: Access,
        receipt: str | None,
        context: ProviderContext,
        *,
        kinds: frozenset[str],
        policy: str,
        preview_hash: str,
    ) -> None:
        with self._lock:
            self._purge()
            consent = self._receipts.get(receipt or "")
        if (
            consent is None
            or consent.owner != access.owner
            or not self._guard.is_live(access.owner)
            or not kinds <= consent.kinds
            or consent.scope != _scope(context)
            or consent.policy != policy
            or consent.preview_hash != preview_hash
        ):
            raise ConsentRequired(
                "Consent is missing, revoked, expired or does not match this provider, host, model, policy and preview"
            )

    def _purge(self) -> None:
        now = self._clock()
        for receipt in [r for r, c in self._receipts.items() if c.expires_at <= now]:
            del self._receipts[receipt]


class RunGate:
    """The choke point for paid or source-disclosing work of one admitted run.

    Every model request, token-count request and retry must go through `call`, which re-checks the
    session, the credential and the consent immediately before the operation starts. Creation does
    the same check, so admission fails before any request. A request already in flight when consent
    is revoked cannot be recalled; the next one is refused. The caller decides which consent
    `kinds` its provider needs (an empty set, for example a loopback Ollama host, skips the
    consent check but still requires a live provider session).
    """

    def __init__(
        self,
        providers: ProviderSessions,
        consent: ConsentLedger,
        access: Access,
        *,
        provider_session_id: str,
        consent_receipt_id: str | None,
        kinds: frozenset[str],
        policy: str,
        preview_hash: str,
    ) -> None:
        self._providers = providers
        self._consent = consent
        self._access = access
        self._handle = provider_session_id
        self._receipt = consent_receipt_id
        self._kinds = kinds
        self._policy = policy
        self._preview = preview_hash
        self._context()

    def _context(self) -> ProviderContext:
        context = self._providers.context(self._access, self._handle)
        if self._kinds:
            self._consent.check(
                self._access,
                self._receipt,
                context,
                kinds=self._kinds,
                policy=self._policy,
                preview_hash=self._preview,
            )
        return context

    def call(self, operation: Callable[[ProviderContext], T]) -> T:
        return operation(self._context())
