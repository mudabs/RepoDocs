"""The local web security boundary: origin/CSRF, exposure, credentials, consent and concurrency.

Everything here is in-process with fake paid operations; no server, provider or credential is used.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import pickle
import threading

import pytest

from repodocs.web.security import (
    COOKIE_NAME,
    CSRF_HEADER,
    AccessConfig,
    AccessConfigError,
    AccessDenied,
    ConsentLedger,
    ConsentRequired,
    LimitExceeded,
    ProviderContext,
    ProviderSessions,
    RunGate,
    Secret,
    SessionGuard,
    SessionRequired,
    UnknownHandle,
    generate_auth_token,
    response_headers,
)

CANARY = "sk-canary-7f3a9c1e5b2d4086"
OTHER_CANARY = "sk-canary-0b8d6a4c2e1f9753"
LOCAL = "127.0.0.1:8000"
ORIGIN = f"http://{LOCAL}"
KINDS = frozenset({"external"})
BOTH = frozenset({"external", "source-excerpts"})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Stack:
    """A guard, provider store and consent ledger with one trusted browser session."""

    def __init__(self, config: AccessConfig | None = None, token: str | None = None) -> None:
        self.clock = Clock()
        self.config = config or AccessConfig()
        self.guard = SessionGuard(self.config, clock=self.clock)
        self.providers = ProviderSessions(self.guard, clock=self.clock)
        self.consent = ConsentLedger(self.guard, clock=self.clock)
        self._token = token
        self.host = next(iter(sorted(self.config.allowed_hosts)))
        self.origin = f"{'https' if self.config.exposed else 'http'}://{self.host}"
        self.boot = self.guard.open_session(self.headers(), token)
        self.cookie = self.boot.set_cookie.split(";", 1)[0]
        self.access = self.guard.authorize("GET", self.headers(cookie=True))

    def headers(self, *, cookie: bool = False, csrf: bool = False, **extra: str) -> dict[str, str]:
        h = {"Host": self.host, "Origin": self.origin, "Sec-Fetch-Site": "same-origin"}
        if cookie:
            h["Cookie"] = self.cookie
        if csrf:
            h["X-CSRF-Token"] = self.boot.csrf_token
        h.update(extra)
        return h

    def new_access(self):
        boot = self.guard.open_session(self.headers(), self._token)
        cookie = boot.set_cookie.split(";", 1)[0]
        return self.guard.authorize("GET", self.headers(cookie=False, Cookie=cookie))


class FakeService:
    """A stand-in for the paid operations #69 performs; it counts what actually started."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def paid_job(self, guard: SessionGuard, method: str, headers: dict[str, str]) -> str:
        guard.authorize(method, headers)  # the adapter's first step, before any work
        self.started.append("job")
        return "started"


@pytest.fixture
def stack() -> Stack:
    return Stack()


# --- exposure configuration -------------------------------------------------------------------


def test_default_is_loopback_and_needs_no_token() -> None:
    config = AccessConfig()
    assert not config.exposed
    assert config.allowed_hosts == {"127.0.0.1:8000", "localhost:8000", "[::1]:8000"}
    assert config.allowed_origins == {"http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"}


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.20", "example.com"])
def test_non_loopback_bind_is_refused_without_origin_and_token(bind: str) -> None:
    with pytest.raises(AccessConfigError):
        AccessConfig(bind_host=bind)
    with pytest.raises(AccessConfigError):
        AccessConfig(bind_host=bind, auth_token=generate_auth_token())
    with pytest.raises(AccessConfigError):
        AccessConfig(bind_host=bind, public_origin="https://docs.example.com")


@pytest.mark.parametrize(
    "origin",
    [
        "http://docs.example.com",
        "https://*.example.com",
        "https://user@docs.example.com",
        "https://docs.example.com/path",
        "https://docs.example.com?x=1",
        "docs.example.com",
        "https://docs.example.com:99999",
    ],
)
def test_public_origin_must_be_a_bare_https_origin(origin: str) -> None:
    with pytest.raises(AccessConfigError):
        AccessConfig(bind_host="0.0.0.0", public_origin=origin, auth_token=generate_auth_token())


def test_short_auth_token_is_refused_and_never_appears_in_repr() -> None:
    with pytest.raises(AccessConfigError) as error:
        AccessConfig(bind_host="0.0.0.0", public_origin="https://docs.example.com", auth_token="short")
    assert "short" not in str(error.value)
    token = generate_auth_token()
    config = AccessConfig(bind_host="0.0.0.0", public_origin="https://docs.example.com", auth_token=token)
    assert token not in repr(config) and config.exposed
    assert config.allowed_origins == {"https://docs.example.com"} and config.allowed_hosts == {"docs.example.com"}


def test_a_public_origin_on_loopback_still_needs_a_token() -> None:
    with pytest.raises(AccessConfigError):
        AccessConfig(public_origin="https://docs.example.com")


# --- origin, host, session and CSRF ------------------------------------------------------------


def test_unrelated_website_cannot_launch_a_paid_job(stack: Stack) -> None:
    service = FakeService()
    attacks = [
        # A hostile page posts with the browser's cookie: wrong Origin and fetch site.
        stack.headers(cookie=True, csrf=True, Origin="https://evil.example", **{"Sec-Fetch-Site": "cross-site"}),
        stack.headers(cookie=True, csrf=True, Origin="https://evil.example"),
        stack.headers(cookie=True, csrf=True, Origin="null"),
        # Same origin claim but a lookalike with the right host text.
        stack.headers(cookie=True, csrf=True, Origin=ORIGIN + ".evil.example"),
        stack.headers(cookie=True, csrf=True, Origin="http://localhost:8001"),
        # No Origin at all on a state-changing request.
        {k: v for k, v in stack.headers(cookie=True, csrf=True).items() if k != "Origin"},
        # DNS rebinding: the browser sends the attacker's host name.
        stack.headers(cookie=True, csrf=True, Host="evil.example:8000"),
        stack.headers(cookie=True, csrf=True, Host="127.0.0.1"),
        # Same site but not same origin.
        stack.headers(cookie=True, csrf=True, **{"Sec-Fetch-Site": "same-site"}),
        # Cookie but no CSRF token, or a wrong one.
        stack.headers(cookie=True),
        stack.headers(cookie=True, **{CSRF_HEADER: "wrong"}),
        # CSRF token but no session.
        stack.headers(csrf=True),
        stack.headers(csrf=True, Cookie=f"{COOKIE_NAME}=forged"),
    ]
    for headers in attacks:
        with pytest.raises(AccessDenied):
            service.paid_job(stack.guard, "POST", headers)
    assert service.started == []
    assert service.paid_job(stack.guard, "POST", stack.headers(cookie=True, csrf=True)) == "started"
    assert service.started == ["job"]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_every_state_changing_or_unknown_method_needs_csrf(stack: Stack, method: str) -> None:
    with pytest.raises(AccessDenied):
        stack.guard.authorize(method, stack.headers(cookie=True))
    assert stack.guard.authorize(method, stack.headers(cookie=True, csrf=True)) == stack.access


def test_reads_need_a_session_and_a_matching_host_but_not_csrf(stack: Stack) -> None:
    assert stack.guard.authorize("GET", {"Host": LOCAL, "Cookie": stack.cookie}) == stack.access
    with pytest.raises(SessionRequired):
        stack.guard.authorize("GET", {"Host": LOCAL})
    with pytest.raises(AccessDenied):
        stack.guard.authorize("GET", {"Host": "evil.example", "Cookie": stack.cookie})
    with pytest.raises(AccessDenied):
        stack.guard.authorize("GET", {"Host": LOCAL, "Cookie": stack.cookie, "Origin": "https://evil.example"})


def test_first_session_for_a_same_origin_browser_needs_no_account(stack: Stack) -> None:
    # A typed URL (Sec-Fetch-Site: none) or the UI's own request opens a session.
    boot = stack.guard.open_session({"Host": "localhost:8000", "Sec-Fetch-Site": "none"})
    assert "HttpOnly" in boot.set_cookie and "SameSite=Strict" in boot.set_cookie
    assert "Secure" not in boot.set_cookie  # plain-HTTP loopback
    assert boot.csrf_token not in boot.set_cookie and boot.csrf_token not in repr(boot)
    # A cross-site page cannot even obtain one, nor via a rebinding host.
    with pytest.raises(AccessDenied):
        stack.guard.open_session({"Host": LOCAL, "Sec-Fetch-Site": "cross-site"})
    with pytest.raises(AccessDenied):
        stack.guard.open_session({"Host": "evil.example"})
    with pytest.raises(AccessDenied):
        stack.guard.open_session({"Host": LOCAL, "Origin": "https://evil.example"})


def test_session_expires_and_is_bounded() -> None:
    s = Stack()
    s.clock.now += 8 * 3600
    with pytest.raises(SessionRequired):
        s.guard.authorize("GET", s.headers(cookie=True))
    small = SessionGuard(AccessConfig(), max_sessions=2, clock=s.clock)
    small.open_session({"Host": LOCAL})
    small.open_session({"Host": LOCAL})
    with pytest.raises(LimitExceeded):
        small.open_session({"Host": LOCAL})
    s.clock.now += 8 * 3600
    small.open_session({"Host": LOCAL})  # expired sessions are reclaimed


def test_end_session_invalidates_the_cookie(stack: Stack) -> None:
    stack.guard.end_session(stack.access)
    with pytest.raises(SessionRequired):
        stack.guard.authorize("GET", stack.headers(cookie=True))


def test_exposed_service_needs_the_token_to_open_a_session() -> None:
    token = generate_auth_token()
    config = AccessConfig(bind_host="0.0.0.0", port=8443, public_origin="https://docs.example.com", auth_token=token)
    guard = SessionGuard(config)
    host = {"Host": "docs.example.com", "Origin": "https://docs.example.com"}
    for bad in (None, "", "x" * 32, token[:-1]):
        with pytest.raises(SessionRequired):
            guard.open_session(host, bad)
    boot = guard.open_session(host, token)
    assert "Secure" in boot.set_cookie
    cookie = boot.set_cookie.split(";", 1)[0]
    # The LAN address and the loopback names are not accepted hosts for an exposed service.
    for rejected in ("127.0.0.1:8443", "192.168.1.20:8443", "localhost"):
        with pytest.raises(AccessDenied):
            guard.authorize("GET", {"Host": rejected, "Cookie": cookie})
    # A token alone never authorizes a request without the session.
    with pytest.raises(SessionRequired):
        guard.authorize("GET", host)
    assert guard.authorize("POST", {**host, "Cookie": cookie, CSRF_HEADER: boot.csrf_token})


def test_no_credentialed_cors_is_ever_offered() -> None:
    for artifact in (False, True):
        headers = {name.lower(): value for name, value in response_headers(artifact=artifact).items()}
        assert not any(name.startswith("access-control-") for name in headers)
        assert headers["x-content-type-options"] == "nosniff" and headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]


def test_hostile_artifacts_cannot_execute() -> None:
    page = response_headers()["Content-Security-Policy"]
    assert "script-src 'self'" in page and "'unsafe-inline'" not in page and "object-src 'none'" in page
    artifact = response_headers(artifact=True)
    assert artifact["Content-Disposition"] == "attachment"
    assert artifact["Content-Security-Policy"].startswith("sandbox;") and "script-src" not in artifact["Content-Security-Policy"]


# --- credentials ---------------------------------------------------------------------------------


def test_secret_never_renders_serializes_or_copies() -> None:
    secret = Secret(CANARY)
    context = ProviderContext("openai", "api.example.com", "m", secret)
    for text in (repr(secret), str(secret), f"{secret}", f"{secret!r:>40}", repr(context), str(context)):
        assert CANARY not in text
    with pytest.raises(TypeError):
        json.dumps({"key": secret})
    for operation in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            operation(secret)
    assert secret.reveal() == CANARY


def test_browser_view_and_errors_carry_no_secret(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    handle = stack.providers.connect(stack.access, "openai", "API.Example.com", "model-a", CANARY)
    view = stack.providers.public_view(stack.access, handle)
    assert view == {
        "provider_session_id": handle,
        "provider": "openai",
        "host": "https://api.example.com",
        "model": "model-a",
        "credential": "stored",
    }
    other = stack.new_access()
    with pytest.raises(UnknownHandle) as unknown:
        stack.providers.context(other, handle)
    with pytest.raises(ConsentRequired) as missing:
        RunGate(
            stack.providers, stack.consent, stack.access, provider_session_id=handle, consent_receipt_id=None,
            kinds=KINDS, policy="p", preview_hash="h",
        )
    everything = json.dumps(view) + str(unknown.value) + str(missing.value) + repr(stack.providers.__dict__)
    assert CANARY not in everything and CANARY not in caplog.text and handle not in caplog.text


def test_nothing_is_persisted_or_put_in_the_environment(stack: Stack, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = dict(os.environ)
    seen: list[dict[str, str]] = []
    handle = stack.providers.connect(stack.access, "openai", "api.example.com", "m", CANARY)
    context = stack.providers.context(stack.access, handle)
    receipt = stack.consent.grant(stack.access, context, kinds=KINDS, policy="p", preview_hash="h")
    gate = RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
                   consent_receipt_id=receipt, kinds=KINDS, policy="p", preview_hash="h")
    gate.call(lambda ctx: seen.append(dict(os.environ)))
    assert dict(os.environ) == before and seen == [before]
    assert not any(CANARY in value for value in os.environ.values())
    assert list(tmp_path.iterdir()) == []


def test_provider_sessions_are_owned_expiring_bounded_and_clearable() -> None:
    s = Stack()
    other = s.new_access()
    handle = s.providers.connect(s.access, "openai", "h", "m", CANARY)
    with pytest.raises(UnknownHandle):
        s.providers.context(other, handle)
    s.providers.disconnect(other, handle)  # not the owner: no effect
    assert s.providers.context(s.access, handle).secret.reveal() == CANARY
    s.providers.disconnect(s.access, handle)
    s.providers.disconnect(s.access, handle)  # idempotent
    with pytest.raises(UnknownHandle):
        s.providers.context(s.access, handle)

    handle = s.providers.connect(s.access, "openai", "h", "m", CANARY)
    s.clock.now += 3600
    with pytest.raises(UnknownHandle):
        s.providers.context(s.access, handle)

    small = ProviderSessions(s.guard, max_sessions=1, clock=s.clock)
    first = small.connect(s.access, "openai", "h", "m", "k")
    with pytest.raises(LimitExceeded):
        small.connect(s.access, "anthropic", "h", "m", "k")
    small.disconnect_all(s.access)
    with pytest.raises(UnknownHandle):
        small.context(s.access, first)
    small.connect(s.access, "anthropic", "h", "m", "k")
    small.clear()


def test_ending_the_web_session_makes_credentials_unreachable(stack: Stack) -> None:
    handle = stack.providers.connect(stack.access, "openai", "h", "m", CANARY)
    stack.guard.end_session(stack.access)
    with pytest.raises(SessionRequired):
        stack.providers.context(stack.access, handle)
    with pytest.raises(SessionRequired):
        stack.providers.connect(stack.access, "openai", "h", "m", CANARY)


@pytest.mark.parametrize("bad", [("", "h", "m"), ("p", " ", "m"), ("p", "h", ""), ("p" * 201, "h", "m")])
def test_provider_identity_is_required(stack: Stack, bad: tuple[str, str, str]) -> None:
    with pytest.raises(ValueError):
        stack.providers.connect(stack.access, *bad, "key")


def test_keyless_provider_session_is_allowed(stack: Stack) -> None:
    handle = stack.providers.connect(stack.access, "ollama", "localhost:11434", "m")
    assert stack.providers.context(stack.access, handle).secret is None
    assert stack.providers.public_view(stack.access, handle)["credential"] == "none"


def test_concurrent_runs_never_see_each_others_credentials() -> None:
    s = Stack()
    runs = 8
    accesses = [s.access] + [s.new_access() for _ in range(runs - 1)]
    keys = [f"sk-canary-run-{i:02d}" for i in range(runs)]
    gates = []
    for i, (access, key) in enumerate(zip(accesses, keys, strict=True)):
        handle = s.providers.connect(access, "openai", f"host-{i}.example", f"model-{i}", key)
        context = s.providers.context(access, handle)
        receipt = s.consent.grant(access, context, kinds=KINDS, policy="p", preview_hash=f"h{i}")
        gates.append(RunGate(s.providers, s.consent, access, provider_session_id=handle,
                             consent_receipt_id=receipt, kinds=KINDS, policy="p", preview_hash=f"h{i}"))
    barrier = threading.Barrier(runs)
    seen: list[tuple[int, str, str, str]] = []
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            barrier.wait()
            for _ in range(50):
                seen.append(
                    (i, *gates[i].call(lambda c: (c.secret.reveal(), c.provider, c.host)))  # type: ignore[union-attr]
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(runs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(seen) == runs * 50
    assert all(key == keys[i] and provider == "openai" and host == f"https://host-{i}.example"
               for i, key, provider, host in seen)


# --- consent ------------------------------------------------------------------------------------


def admitted(s: Stack, kinds: frozenset[str] = KINDS, *, granted: frozenset[str] = KINDS):
    handle = s.providers.connect(s.access, "openai", "api.example.com", "model-a", CANARY)
    context = s.providers.context(s.access, handle)
    receipt = s.consent.grant(s.access, context, kinds=granted, policy="policy-1", preview_hash="preview-1")
    gate = RunGate(s.providers, s.consent, s.access, provider_session_id=handle,
                   consent_receipt_id=receipt, kinds=kinds, policy="policy-1", preview_hash="preview-1")
    return handle, context, receipt, gate


class FakeProvider:
    """Counts requests; a source request must never start without consent."""

    def __init__(self, gate: RunGate) -> None:
        self.gate = gate
        self.requests: list[str] = []

    def request(self, kind: str) -> str:
        return self.gate.call(lambda ctx: self.requests.append(f"{kind}:{ctx.secret.reveal()[:3]}") or kind)  # type: ignore[union-attr]


def test_consent_allows_model_count_and_retry_until_revoked(stack: Stack) -> None:
    _, _, receipt, gate = admitted(stack)
    provider = FakeProvider(gate)
    for kind in ("count", "model", "retry"):
        provider.request(kind)
    assert provider.requests == ["count:sk-", "model:sk-", "retry:sk-"]
    stack.consent.revoke(stack.access, receipt)
    for kind in ("count", "model", "retry"):
        with pytest.raises(ConsentRequired):
            provider.request(kind)
    assert len(provider.requests) == 3  # nothing started after revocation


def test_missing_consent_fails_admission_before_any_request(stack: Stack) -> None:
    handle = stack.providers.connect(stack.access, "openai", "api.example.com", "model-a", CANARY)
    with pytest.raises(ConsentRequired):
        RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
                consent_receipt_id=None, kinds=KINDS, policy="policy-1", preview_hash="preview-1")
    with pytest.raises(ConsentRequired):
        RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
                consent_receipt_id="cr_unknown", kinds=KINDS, policy="policy-1", preview_hash="preview-1")


def test_source_excerpt_consent_is_separate_from_external_consent(stack: Stack) -> None:
    handle = stack.providers.connect(stack.access, "openai", "api.example.com", "model-a", CANARY)
    context = stack.providers.context(stack.access, handle)
    external_only = stack.consent.grant(stack.access, context, kinds=KINDS, policy="policy-1", preview_hash="preview-1")
    with pytest.raises(ConsentRequired):
        RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
                consent_receipt_id=external_only, kinds=BOTH, policy="policy-1", preview_hash="preview-1")
    both = stack.consent.grant(stack.access, context, kinds=BOTH, policy="policy-1", preview_hash="preview-1")
    RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
            consent_receipt_id=both, kinds=BOTH, policy="policy-1", preview_hash="preview-1")


@pytest.mark.parametrize("change", ["provider", "host", "model", "policy", "preview"])
def test_consent_does_not_transfer_to_a_different_scope(stack: Stack, change: str) -> None:
    _, _, receipt, _ = admitted(stack)
    provider, host, model = ("openai", "api.example.com", "model-a")
    policy, preview = "policy-1", "preview-1"
    if change == "provider":
        provider = "anthropic"
    elif change == "host":
        host = "api.other.example"
    elif change == "model":
        model = "model-b"
    elif change == "policy":
        policy = "policy-2"
    else:
        preview = "preview-2"
    handle = stack.providers.connect(stack.access, provider, host, model, OTHER_CANARY)
    with pytest.raises(ConsentRequired):
        RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
                consent_receipt_id=receipt, kinds=KINDS, policy=policy, preview_hash=preview)


def test_another_session_cannot_use_or_revoke_a_receipt(stack: Stack) -> None:
    handle, _, receipt, gate = admitted(stack)
    other = stack.new_access()
    other_handle = stack.providers.connect(other, "openai", "api.example.com", "model-a", OTHER_CANARY)
    with pytest.raises(ConsentRequired):
        RunGate(stack.providers, stack.consent, other, provider_session_id=other_handle,
                consent_receipt_id=receipt, kinds=KINDS, policy="policy-1", preview_hash="preview-1")
    stack.consent.revoke(other, receipt)  # not the owner: no effect
    gate.call(lambda ctx: None)


def test_consent_expires_and_revoke_all_clears_every_receipt() -> None:
    s = Stack()
    _, _, _, gate = admitted(s)
    s.clock.now += 3601
    with pytest.raises(UnknownHandle):  # the provider session expired at the same moment
        gate.call(lambda ctx: None)
    s2 = Stack()
    _, _, _, gate2 = admitted(s2)
    _, _, _, gate3 = admitted(s2)
    s2.consent.revoke_all(s2.access)
    for g in (gate2, gate3):
        with pytest.raises(ConsentRequired):
            g.call(lambda ctx: None)


def test_disconnect_and_logout_stop_the_next_request(stack: Stack) -> None:
    handle, _, _, gate = admitted(stack)
    stack.providers.disconnect(stack.access, handle)
    with pytest.raises(UnknownHandle):
        gate.call(lambda ctx: None)
    _, _, _, gate = admitted(stack)
    stack.guard.end_session(stack.access)
    with pytest.raises(SessionRequired):
        gate.call(lambda ctx: None)


def test_consent_grant_validation(stack: Stack) -> None:
    context = ProviderContext("p", "h", "m", None)
    for kinds in (frozenset(), frozenset({"everything"})):
        with pytest.raises(ValueError):
            stack.consent.grant(stack.access, context, kinds=kinds, policy="p", preview_hash="h")
    with pytest.raises(ValueError):
        stack.consent.grant(stack.access, context, kinds=KINDS, policy="p", preview_hash="")
    small = ConsentLedger(stack.guard, max_receipts=1, clock=stack.clock)
    small.grant(stack.access, context, kinds=KINDS, policy="p", preview_hash="h")
    with pytest.raises(LimitExceeded):
        small.grant(stack.access, context, kinds=KINDS, policy="p", preview_hash="h")


def test_empty_kinds_skips_consent_but_still_needs_a_live_provider_session(stack: Stack) -> None:
    handle = stack.providers.connect(stack.access, "ollama", "localhost:11434", "m")
    gate = RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
                   consent_receipt_id=None, kinds=frozenset(), policy="p", preview_hash="h")
    assert gate.call(lambda ctx: ctx.provider) == "ollama"
    stack.providers.disconnect(stack.access, handle)
    with pytest.raises(UnknownHandle):
        gate.call(lambda ctx: None)


@pytest.mark.parametrize("host", [
    "https://user:canary-secret@example.com/v1", "https://example.com/v1?key=canary-secret",
    "https://example.com/#canary-secret", "http://remote.example/v1", "https://example.com:bad/v1",
    "https://example.com/with space", "https://example.com/line\nbreak",
])
def test_provider_identity_refuses_url_credentials_and_unsafe_endpoints(stack: Stack, host: str, caplog) -> None:
    with pytest.raises(ValueError) as error:
        stack.providers.connect(stack.access, "openai", host, "model", CANARY)
    assert "canary-secret" not in str(error.value) + caplog.text + repr(stack.providers.__dict__)


def test_endpoint_canonicalization_preserves_path_case_and_consent_scope(stack: Stack) -> None:
    first = stack.providers.connect(stack.access, "openai", "https://API.Example.com/MyModel/API", "m", CANARY)
    second = stack.providers.connect(stack.access, "openai", "https://api.example.com/mymodel/api", "m", CANARY)
    original = stack.providers.context(stack.access, first)
    assert original.host == "https://api.example.com/MyModel/API"
    receipt = stack.consent.grant(stack.access, original, kinds=BOTH, policy="p", preview_hash="h")
    with pytest.raises(ConsentRequired):
        stack.consent.check(stack.access, receipt, stack.providers.context(stack.access, second), kinds=BOTH, policy="p", preview_hash="h")


def test_external_provider_cannot_skip_consent_with_empty_requested_scopes(stack: Stack) -> None:
    handle = stack.providers.connect(stack.access, "openai", "https://api.example.com", "m", CANARY)
    with pytest.raises(ConsentRequired):
        RunGate(stack.providers, stack.consent, stack.access, provider_session_id=handle,
                consent_receipt_id=None, kinds=frozenset(), policy="p", preview_hash="h")


@pytest.mark.parametrize("value", [123, " ", "one\ntwo", "\x00key", "x" * 8193])
def test_invalid_credentials_are_refused_without_echoing_them(value) -> None:
    with pytest.raises(ValueError):
        Secret(value)


def test_a_credential_read_with_a_trailing_newline_is_trimmed() -> None:
    assert Secret(CANARY + "\n").reveal() == CANARY
