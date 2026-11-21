"""Run-level usage limits, reservations, and local receipts for model requests (ADR-031).

One ``Ledger`` belongs to one run (one CLI command or one web job). ``BudgetedProvider`` wraps any
provider so that every request, including a retry or a repair, is checked against the same caps
before it is sent and reconciled with the provider's reported usage afterwards.

The rules the ledger keeps:

- A request is reserved at its worst case (estimated prompt plus the full output cap) before it is
  sent, and refused when that would pass the context window or any aggregate cap.
- A reservation stays charged until the provider reports complete usage. A failure, a cancellation,
  or missing or partial usage keeps the worst case; it is never counted as zero.
- A byte-derived estimate is labeled as an estimate. It is not an exact token count.
- Rates come from a configured, versioned price profile. Nothing is fetched or built in.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common import write_json_atomic
from ..errors import ConfigError, ConsentError, ProviderError

if TYPE_CHECKING:
    from ..config import AiSettings
    from .providers import Provider, ProviderResponse


RECEIPT_VERSION = "1.0"
RECEIPT_DIRECTORY = "receipts"

# Hard ceilings: a project may lower them, never raise them. Aggregate values cover a whole run.
LIMIT_CEILINGS: dict[str, int] = {
    "request_output_tokens": 16000,
    "max_requests": 8,
    "max_input_tokens": 800_000,
    "max_output_tokens": 64_000,
}
# Used when the project does not state a context window. They are conservative configuration
# defaults, not facts about a model; the window of the model actually served is the project's to set.
DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {"anthropic": 200_000, "ollama": 4096}
FALLBACK_CONTEXT_WINDOW = 4096
EFFORTS = ("low", "medium", "high")

# Used only when the provider cannot count tokens. Assumes at least two bytes per token, which holds
# for ordinary prose and code but not for every input (dense hashes or unusual scripts can tokenize
# worse), so it is an estimate and is always reported as one. The overhead covers chat framing that is
# not visible in the serialized request.
ESTIMATE_BYTES_PER_TOKEN = 2
REQUEST_OVERHEAD_TOKENS = 64
METHOD_PROVIDER_COUNT = "provider-count"
METHOD_FALLBACK = "fallback-estimate"

STOP_CANCELLED = "cancelled"
STOP_REQUESTS = "request-limit"
STOP_CONTEXT = "context-window"
STOP_INPUT = "input-token-limit"
STOP_OUTPUT = "output-token-limit"
STOP_COST = "cost-limit"

_MICRO = Decimal(1_000_000)


def _count(value: Any) -> int | None:
    """A reported token count, or None. A missing or malformed value is unknown, never zero."""

    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _rate(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    try:
        rate = Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - str() of a number always parses
        raise ConfigError(f"{name} must be a number") from exc
    if not rate.is_finite() or rate < 0:
        raise ConfigError(f"{name} must be a finite, non-negative number")
    return rate


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def format_usd(micro: int | None) -> str | None:
    """Exact decimal dollars for an integer number of millionths of a dollar."""

    return None if micro is None else str((Decimal(micro) / _MICRO).quantize(Decimal("0.000001")))


@dataclass(frozen=True)
class Usage:
    """What a provider reported for one request. ``None`` means not reported, which is not zero.

    The token fields are disjoint billed categories: ``input_tokens`` is the prompt at the base input
    rate (cached tokens excluded), and ``output_tokens`` is every generated token, reasoning included.
    ``reasoning_tokens`` is an informational subset of ``output_tokens`` and is never added to it. A
    provider with no caching reports ``0`` for both cache fields only when its API says so.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def measured(self) -> bool:
        """True when every billed category was reported, so the reservation can be reconciled."""

        return None not in (self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens)

    @property
    def prompt_tokens(self) -> int | None:
        """All prompt-side tokens, which is what the context window and the input cap count."""

        parts = (self.input_tokens, self.cache_read_tokens, self.cache_write_tokens)
        return None if None in parts else sum(parts)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


def usage_from_counts(input_tokens: Any, output_tokens: Any, cache_read: Any, cache_write: Any, reasoning: Any = None) -> Usage | None:
    """A ``Usage`` from raw provider values, or None when the provider reported nothing at all."""

    usage = Usage(_count(input_tokens), _count(output_tokens), _count(cache_read), _count(cache_write), _count(reasoning))
    return None if usage == Usage() else usage


@dataclass(frozen=True)
class PriceProfile:
    """Versioned rates in US dollars per million tokens, supplied by the project.

    RepoDocs ships no rates and never fetches them. A cache rate that is absent means a request that
    reports cache tokens has an unknown cost, and its worst-case reservation stays.
    """

    version: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal | None = None
    cache_write_usd_per_mtok: Decimal | None = None

    _KEYS = ("input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"version": self.version}
        for key in self._KEYS:
            value = getattr(self, key)
            if value is not None:
                data[key] = _number(value)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "PriceProfile":
        if not isinstance(data, dict):
            raise ConfigError("ai.prices must be a JSON object")
        unknown = sorted(set(data) - {"version", *cls._KEYS})
        if unknown:
            raise ConfigError("Unknown ai.prices setting: " + ", ".join(unknown))
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise ConfigError("ai.prices.version must name the rate data, for example the date it was checked")
        for key in ("input_usd_per_mtok", "output_usd_per_mtok"):
            if key not in data:
                raise ConfigError(f"ai.prices.{key} is required")
        rates = {key: _rate(data[key], f"ai.prices.{key}") for key in cls._KEYS if data.get(key) is not None}
        return cls(version=version, **rates)

    def worst_input_rate(self) -> Decimal:
        """The highest rate a prompt token could be billed at, so a reservation never undercounts."""

        return max(self.input_usd_per_mtok, self.cache_read_usd_per_mtok or Decimal(0), self.cache_write_usd_per_mtok or Decimal(0))

    def reserve_micro_usd(self, prompt_tokens: int, output_tokens: int) -> int:
        """Worst-case cost. One USD per million tokens is exactly one millionth of a USD per token."""

        return _ceil(prompt_tokens * self.worst_input_rate() + output_tokens * self.output_usd_per_mtok)

    def cost_micro_usd(self, usage: Usage) -> int | None:
        """Cost of measured usage, or None when it cannot be priced from this profile."""

        if not usage.measured:
            return None
        total = usage.input_tokens * self.input_usd_per_mtok + usage.output_tokens * self.output_usd_per_mtok  # type: ignore[operator]
        for tokens, rate in ((usage.cache_read_tokens, self.cache_read_usd_per_mtok), (usage.cache_write_tokens, self.cache_write_usd_per_mtok)):
            if tokens:
                if rate is None:
                    return None
                total += tokens * rate
        return _ceil(total)


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class RunLimits:
    """Limits for one run. As configuration, ``None`` means the default; ``resolved`` fills them in.

    ``context_window_tokens`` is the runtime's window, which a request must fit including its reserved
    output. ``request_output_tokens`` is the explicit output cap sent with every request. The other
    token and request fields cap the whole run, and ``max_cost_usd`` needs a price profile.
    """

    context_window_tokens: int | None = None
    request_output_tokens: int | None = None
    max_requests: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key in ("context_window_tokens", "request_output_tokens", "max_requests", "max_input_tokens", "max_output_tokens"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        if self.max_cost_usd is not None:
            data["max_cost_usd"] = _number(self.max_cost_usd)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "RunLimits":
        if not isinstance(data, dict):
            raise ConfigError("ai.limits must be a JSON object")
        names = {"context_window_tokens", "request_output_tokens", "max_requests", "max_input_tokens", "max_output_tokens", "max_cost_usd"}
        unknown = sorted(set(data) - names)
        if unknown:
            raise ConfigError("Unknown ai.limits setting: " + ", ".join(unknown))
        values: dict[str, Any] = {}
        for name in names - {"max_cost_usd"}:
            if name not in data:
                continue
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"ai.limits.{name} must be a positive integer")
            if name in LIMIT_CEILINGS and value > LIMIT_CEILINGS[name]:
                raise ConfigError(f"ai.limits.{name} may only lower the ceiling of {LIMIT_CEILINGS[name]}")
            values[name] = value
        if data.get("max_cost_usd") is not None:
            cost = _rate(data["max_cost_usd"], "ai.limits.max_cost_usd")
            if cost == 0:
                raise ConfigError("ai.limits.max_cost_usd must be greater than zero")
            values["max_cost_usd"] = cost
        return cls(**values)

    def resolved(self, provider_name: str) -> "RunLimits":
        """Every field filled: configured values, else the provider's window and the hard ceilings."""

        window = self.context_window_tokens or DEFAULT_CONTEXT_WINDOWS.get(provider_name, FALLBACK_CONTEXT_WINDOW)
        output = self.request_output_tokens or min(LIMIT_CEILINGS["request_output_tokens"], window // 2)
        if output >= window:
            raise ConfigError(
                f"ai.limits.request_output_tokens ({output}) must be smaller than the context window ({window}), "
                "or no prompt can fit"
            )
        return RunLimits(
            context_window_tokens=window,
            request_output_tokens=output,
            max_requests=self.max_requests or LIMIT_CEILINGS["max_requests"],
            max_input_tokens=self.max_input_tokens or LIMIT_CEILINGS["max_input_tokens"],
            max_output_tokens=self.max_output_tokens or LIMIT_CEILINGS["max_output_tokens"],
            max_cost_usd=self.max_cost_usd,
        )


class RunStopped(ProviderError):
    """The next request was refused before it was sent. ``reason`` is one of the ``STOP_*`` values."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class _Request:
    index: int
    estimate_tokens: int
    estimate_method: str
    output_cap_tokens: int
    charged_input: int
    charged_output: int
    charged_cost: int | None
    reserved_cost: int | None
    outcome: str = "in-flight"
    usage: Usage | None = None
    served_model: str | None = None
    cost_measured: bool = False

    @property
    def measured(self) -> bool:
        return self.usage is not None and self.usage.measured and (self.charged_cost is None or self.cost_measured)


class Ledger:
    """The usage of one run. Distinct runs use distinct ledgers; one ledger is safe across threads."""

    def __init__(self, limits: RunLimits, prices: PriceProfile | None = None) -> None:
        if None in (limits.context_window_tokens, limits.request_output_tokens, limits.max_requests, limits.max_input_tokens, limits.max_output_tokens):
            raise ConfigError("A ledger needs resolved limits")
        if limits.max_cost_usd is not None and prices is None:
            raise ConfigError("ai.limits.max_cost_usd needs ai.prices; RepoDocs has no built-in rates to reserve cost with")
        self.limits = limits
        self.prices = prices
        self._lock = threading.Lock()
        self._requests: list[_Request] = []
        self._stop: tuple[str, str] | None = None
        self._cancelled = False

    # -- limits ------------------------------------------------------------------------------
    def _charged(self) -> tuple[int, int, int | None]:
        cost = None if self.prices is None else sum(item.charged_cost or 0 for item in self._requests)
        return sum(item.charged_input for item in self._requests), sum(item.charged_output for item in self._requests), cost

    def _problem(self, estimate: int) -> tuple[str, str] | None:
        limits = self.limits
        if self._cancelled:
            return STOP_CANCELLED, "The run was cancelled; no further request is sent"
        if len(self._requests) + 1 > limits.max_requests:  # type: ignore[operator]
            return STOP_REQUESTS, f"The next request would be number {len(self._requests) + 1}, past the run limit of {limits.max_requests}"
        output = limits.request_output_tokens
        if estimate + output > limits.context_window_tokens:  # type: ignore[operator]
            return STOP_CONTEXT, (
                f"The request needs about {estimate} prompt tokens plus {output} reserved output tokens, which does not fit "
                f"the {limits.context_window_tokens}-token context window; nothing was sent. Reduce the context budgets or "
                "set ai.limits.context_window_tokens to what the model really supports"
            )
        spent_input, spent_output, spent_cost = self._charged()
        if spent_input + estimate > limits.max_input_tokens:  # type: ignore[operator]
            return STOP_INPUT, f"The next request would take the run past its {limits.max_input_tokens} input token cap ({spent_input} already charged)"
        if spent_output + output > limits.max_output_tokens:  # type: ignore[operator]
            return STOP_OUTPUT, f"The next request could take the run past its {limits.max_output_tokens} output token cap ({spent_output} already charged)"
        if limits.max_cost_usd is not None and self.prices is not None:
            cap = _ceil(limits.max_cost_usd * _MICRO)
            worst = self.prices.reserve_micro_usd(estimate, output)
            if (spent_cost or 0) + worst > cap:
                return STOP_COST, f"The next request could cost up to {format_usd(worst)} USD, past the {format_usd(cap)} USD cap ({format_usd(spent_cost)} already charged)"
        return None

    def _refuse(self, problem: tuple[str, str]) -> RunStopped:
        if self._stop is None:
            self._stop = problem
        return RunStopped(*problem)

    def admit(self) -> None:
        """Refuse early, before any token counting, when the run is already stopped or out of requests."""

        with self._lock:
            problem = self._problem(0)
            if problem:
                raise self._refuse(problem)

    def reserve(self, estimate_tokens: int, method: str) -> _Request:
        """Charge a request's worst case, or raise ``RunStopped`` without sending anything."""

        with self._lock:
            problem = self._problem(estimate_tokens)
            if problem:
                raise self._refuse(problem)
            output = self.limits.request_output_tokens
            cost = None if self.prices is None else self.prices.reserve_micro_usd(estimate_tokens, output)  # type: ignore[arg-type]
            request = _Request(len(self._requests) + 1, estimate_tokens, method, output, estimate_tokens, output, cost, cost)  # type: ignore[arg-type]
            self._requests.append(request)
            return request

    def settle(self, request: _Request, usage: Usage | None, *, outcome: str, model: str | None = None) -> None:
        """Record what happened. Only complete usage replaces the reservation; anything else keeps it."""

        with self._lock:
            request.outcome = outcome
            request.usage = usage
            request.served_model = model
            if usage is None or not usage.measured:
                return
            request.charged_input = usage.prompt_tokens  # type: ignore[assignment]
            request.charged_output = usage.output_tokens  # type: ignore[assignment]
            if self.prices is not None:
                actual = self.prices.cost_micro_usd(usage)
                if actual is not None:
                    request.charged_cost, request.cost_measured = actual, True

    def cancel(self) -> None:
        """Stop sending. A request already in flight finishes; its billing cannot be recalled."""

        with self._lock:
            self._cancelled = True
            if self._stop is None:
                self._stop = (STOP_CANCELLED, "The run was cancelled")

    # -- reporting ---------------------------------------------------------------------------
    def max_estimated_cost_micro(self) -> int | None:
        """The most the run can be charged if every estimate holds, or None without a price profile."""

        if self.prices is None:
            return None
        limits = self.limits
        output = min(limits.max_output_tokens, limits.max_requests * limits.request_output_tokens)  # type: ignore[operator]
        worst = self.prices.reserve_micro_usd(limits.max_input_tokens, output)  # type: ignore[arg-type]
        return worst if limits.max_cost_usd is None else min(worst, _ceil(limits.max_cost_usd * _MICRO))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            spent_input, spent_output, spent_cost = self._charged()
            limits = self.limits
            cap = None if limits.max_cost_usd is None else _ceil(limits.max_cost_usd * _MICRO)
            unmeasured = [item for item in self._requests if not item.measured]
            return {
                "limits": limits.to_dict(),
                "price_profile": None if self.prices is None else self.prices.to_dict(),
                "max_estimated_cost_usd": format_usd(self.max_estimated_cost_micro()),
                "requests": [self._describe(item) for item in self._requests],
                "totals": {
                    "requests": len(self._requests),
                    "input_tokens": spent_input,
                    "output_tokens": spent_output,
                    "cost_usd": format_usd(spent_cost),
                    "unmeasured_requests": len(unmeasured),
                    "usage_complete": not unmeasured,
                },
                "remaining": {
                    "requests": max(0, limits.max_requests - len(self._requests)),  # type: ignore[operator]
                    "input_tokens": max(0, limits.max_input_tokens - spent_input),  # type: ignore[operator]
                    "output_tokens": max(0, limits.max_output_tokens - spent_output),  # type: ignore[operator]
                    "cost_usd": None if cap is None else format_usd(max(0, cap - (spent_cost or 0))),
                },
                "stop_reason": None if self._stop is None else self._stop[0],
                "stop_message": None if self._stop is None else self._stop[1],
                "notes": [
                    "Charged totals count a request that has no complete usage at its reserved worst case, so they are upper bounds.",
                    "A fallback-estimate is derived from the serialized request size. It is not an exact token count.",
                ],
            }

    def _describe(self, item: _Request) -> dict[str, Any]:
        return {
            "index": item.index,
            "outcome": item.outcome,
            "served_model": item.served_model,
            "estimate_tokens": item.estimate_tokens,
            "estimate_method": item.estimate_method,
            "output_cap_tokens": item.output_cap_tokens,
            "usage": None if item.usage is None else item.usage.to_dict(),
            "usage_measured": item.measured,
            "charged_input_tokens": item.charged_input,
            "charged_output_tokens": item.charged_output,
            "reserved_cost_usd": format_usd(item.reserved_cost),
            "charged_cost_usd": format_usd(item.charged_cost),
            "cost_measured": item.cost_measured,
        }


def estimate_tokens(text: str) -> int:
    """A labeled estimate from the UTF-8 size of ``text``. See ``ESTIMATE_BYTES_PER_TOKEN``."""

    return -(-len(text.encode("utf-8")) // ESTIMATE_BYTES_PER_TOKEN) + REQUEST_OVERHEAD_TOKENS


def serialized_request(provider: Any, system: str, user: str, schema: dict[str, Any]) -> str:
    """The complete request as it will be sent: system, user, schema, model and options."""

    build = getattr(provider, "request_body", None)
    body = build(system, user, schema) if build else {"system": system, "messages": [{"role": "user", "content": user}], "schema": schema}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class BudgetedProvider:
    """A provider whose every ``generate`` call is reserved and reconciled in one shared ledger.

    It is built only from a provider that already passed the consent checks, so a counting request,
    when the provider offers one, is sent to a destination the user has already approved.
    """

    def __init__(self, provider: "Provider", ledger: Ledger) -> None:
        self._provider = provider
        self.ledger = ledger
        self.name = provider.name
        self.model = provider.model
        self.external = provider.external

    def _estimate(self, system: str, user: str, schema: dict[str, Any]) -> tuple[int, str]:
        counter = getattr(self._provider, "count_input_tokens", None)
        if counter is not None:
            try:
                counted = counter(system, user, schema)
            except ProviderError:
                counted = None
            if isinstance(counted, int) and not isinstance(counted, bool) and counted > 0:
                return counted, METHOD_PROVIDER_COUNT
        return estimate_tokens(serialized_request(self._provider, system, user, schema)), METHOD_FALLBACK

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> "ProviderResponse":
        self.ledger.admit()
        estimate, method = self._estimate(system, user, schema)
        request = self.ledger.reserve(estimate, method)
        try:
            response = self._provider.generate(system, user, schema)
        except ProviderError as exc:
            self.ledger.settle(request, exc.usage, outcome="provider-error")
            raise
        except BaseException:
            self.ledger.settle(request, None, outcome="interrupted")
            raise
        self.ledger.settle(request, getattr(response, "usage", None), outcome="ok", model=response.model)
        return response

    def cancel(self) -> None:
        self.ledger.cancel()


def budgeted(provider: "Provider", settings: "AiSettings") -> BudgetedProvider:
    """Wrap a resolved provider with a new ledger built from the project's limits and price profile."""

    if provider.external and not settings.external_consent:
        raise ConsentError("A usage budget cannot be started for an external provider without recorded consent")
    return BudgetedProvider(provider, Ledger(settings.limits.resolved(provider.name), settings.prices))


def write_receipt(internal_path: Path, command: str, provider: BudgetedProvider) -> dict[str, Any]:
    """Store the run's receipt under the project's internal directory and return its summary.

    A receipt holds counts, limits, and outcomes. It never holds a prompt, a response, or a credential.
    """

    run_id = secrets.token_hex(4)
    started = datetime.now(timezone.utc)
    path = internal_path / RECEIPT_DIRECTORY / f"{started.strftime('%Y%m%dT%H%M%SZ')}-{command}-{run_id}.json"
    body = provider.ledger.snapshot()
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "run_id": run_id,
        "command": command,
        "recorded_at": started.isoformat(timespec="seconds"),
        "provider": provider.name,
        "requested_model": provider.model,
        "external": provider.external,
        **body,
    }
    write_json_atomic(path, receipt)
    return {
        "receipt": path.as_posix(),
        "stop_reason": body["stop_reason"],
        "max_estimated_cost_usd": body["max_estimated_cost_usd"],
        **body["totals"],
        "remaining": body["remaining"],
    }
