"""Run-level usage limits, reservations, and local receipts for model requests (ADR-031).

One ``Ledger`` belongs to one run (one CLI command or one web job). ``BudgetedProvider`` wraps any
provider so that every request, including a retry or a repair, is checked against the same caps
before it is sent and reconciled with the provider's reported usage afterwards.

The rules the ledger keeps:

- A request is reserved at its worst case (prompt bound plus the full output cap) before it is
  sent, and refused when that would pass the context window or any aggregate cap.
- A reservation stays charged until the provider reports complete usage. A failure, a cancellation,
  or missing or partial usage keeps the worst case; it is never counted as zero.
- Reported usage above a cap stops the run: the response is not used and no further request is sent.
  The provider has already billed it; that cannot be undone.
- Without a provider count, a request is bounded at one token per UTF-8 byte. That is a bound for
  byte-level and byte-fallback tokenizers, not a token count, and is labeled as such.
- A token-count request goes to the same approved destination as the request it measures. It is
  admitted and counted under its own run allowance; it is not priced.
- Rates come from a configured, versioned price profile bound to one provider and model. Nothing is
  fetched or built in.
- Once a run stops, it stays stopped.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..common import write_json_atomic
from ..errors import ConfigError, ConsentError, ProviderError

if TYPE_CHECKING:
    from ..config import AiSettings
    from .providers import Provider, ProviderResponse


RECEIPT_VERSION = "1.0"
RECEIPT_DIRECTORY = "receipts"

# Hard ceilings: a project or a run may lower them, never raise them. Aggregate values cover a whole run.
# max_requests matches ADR-026's provider request ceiling (one repair included); token-count requests
# have their own allowance with the same ceiling, one per generation request.
LIMIT_CEILINGS: dict[str, int] = {
    "request_output_tokens": 16000,
    "max_requests": 6,
    "max_count_requests": 6,
    "max_input_tokens": 800_000,
    "max_output_tokens": 64_000,
}
_INTEGER_LIMITS = (
    "context_window_tokens", "request_output_tokens", "max_requests", "max_count_requests", "max_input_tokens", "max_output_tokens",
)
# Used when the project does not state a context window. They are conservative configuration
# defaults, not facts about a model; the window of the model actually served is the project's to set.
DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {"anthropic": 200_000, "ollama": 4096}
FALLBACK_CONTEXT_WINDOW = 4096
EFFORTS = ("low", "medium", "high")

# Used when the provider cannot count tokens. Byte-level BPE tokenizers (the Anthropic and OpenAI model
# families, and Llama-family models served by Ollama) and SentencePiece tokenizers with byte fallback
# never emit more pieces than the UTF-8 bytes they encode, so one token per byte of the serialized request,
# plus fixed framing for chat templates and special tokens, bounds the prompt. It does not hold for a
# tokenizer that expands text while normalizing it, and it is usually several times the real count, so it
# can refuse a request that would have fitted. It is never reported as a token count.
BYTES_PER_TOKEN_BOUND = 1
REQUEST_OVERHEAD_TOKENS = 64
METHOD_PROVIDER_COUNT = "provider-count"
METHOD_BYTE_BOUND = "byte-bound"

STOP_CANCELLED = "cancelled"
STOP_REQUESTS = "request-limit"
STOP_CONTEXT = "context-window"
STOP_INPUT = "input-token-limit"
STOP_OUTPUT = "output-token-limit"
STOP_COST = "cost-limit"
STOP_OVERAGE = "reported-usage-over-limit"
STOP_UNPRICED = "unpriced-usage"

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
    """Versioned rates in US dollars per million tokens for one provider and model, supplied by the project.

    RepoDocs ships no rates and never fetches them. ``served_models`` lists the model identities the provider
    may report for this model (for example a dated snapshot of an alias); a response from any other model is
    not priced with these rates. A cache rate that is absent means cache tokens of that kind have an unknown
    cost, so the profile is not ``complete`` and cannot back a cost cap.
    """

    version: str
    provider: str
    model: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal | None = None
    cache_write_usd_per_mtok: Decimal | None = None
    served_models: tuple[str, ...] = ()

    _KEYS = ("input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"version": self.version, "provider": self.provider, "model": self.model}
        for key in self._KEYS:
            value = getattr(self, key)
            if value is not None:
                data[key] = _number(value)
        if self.served_models:
            data["served_models"] = list(self.served_models)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "PriceProfile":
        if not isinstance(data, dict):
            raise ConfigError("ai.prices must be a JSON object")
        unknown = sorted(set(data) - {"version", "provider", "model", "served_models", *cls._KEYS})
        if unknown:
            raise ConfigError("Unknown ai.prices setting: " + ", ".join(unknown))
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise ConfigError("ai.prices.version must name the rate data, for example the date it was checked")
        for key in ("provider", "model"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ConfigError(f"ai.prices.{key} is required: rates apply to one provider and model only")
        served = data.get("served_models", [])
        if not isinstance(served, list) or not all(isinstance(item, str) and item for item in served):
            raise ConfigError("ai.prices.served_models must be a list of model names")
        for key in ("input_usd_per_mtok", "output_usd_per_mtok"):
            if key not in data:
                raise ConfigError(f"ai.prices.{key} is required")
        rates = {key: _rate(data[key], f"ai.prices.{key}") for key in cls._KEYS if data.get(key) is not None}
        return cls(version=version, provider=data["provider"], model=data["model"], served_models=tuple(served), **rates)

    @property
    def complete(self) -> bool:
        """Every billed category has a rate, so a worst case can be reserved."""

        return self.cache_read_usd_per_mtok is not None and self.cache_write_usd_per_mtok is not None

    def covers(self, provider: str, model: str | None) -> bool:
        return provider == self.provider and model == self.model

    def prices_served(self, served_model: str | None) -> bool:
        """Whether a response from this serving model may be priced with these rates."""

        return served_model is not None and served_model in (self.model, *self.served_models)

    def worst_input_rate(self) -> Decimal:
        """The highest rate a prompt token could be billed at, so a reservation never undercounts."""

        return max(self.input_usd_per_mtok, self.cache_read_usd_per_mtok or Decimal(0), self.cache_write_usd_per_mtok or Decimal(0))

    def reserve_micro_usd(self, prompt_tokens: int, output_tokens: int) -> int | None:
        """Worst-case cost, or None when a billed category has no rate. One USD per million tokens is a millionth per token."""

        if not self.complete:
            return None
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
    token and request fields cap the whole run, and ``max_cost_usd`` needs a complete price profile.
    Values are checked on construction, so no caller can exceed a ceiling by building one directly.
    """

    context_window_tokens: int | None = None
    request_output_tokens: int | None = None
    max_requests: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: Decimal | None = None
    max_count_requests: int | None = None

    def __post_init__(self) -> None:
        for name in _INTEGER_LIMITS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"ai.limits.{name} must be a positive integer")
            if name in LIMIT_CEILINGS and value > LIMIT_CEILINGS[name]:
                raise ConfigError(f"ai.limits.{name} may only lower the ceiling of {LIMIT_CEILINGS[name]}")
        if self.max_cost_usd is not None:
            if not isinstance(self.max_cost_usd, Decimal) or not self.max_cost_usd.is_finite() or self.max_cost_usd <= 0:
                raise ConfigError("ai.limits.max_cost_usd must be greater than zero")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {key: getattr(self, key) for key in _INTEGER_LIMITS if getattr(self, key) is not None}
        if self.max_cost_usd is not None:
            data["max_cost_usd"] = _number(self.max_cost_usd)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "RunLimits":
        if not isinstance(data, dict):
            raise ConfigError("ai.limits must be a JSON object")
        unknown = sorted(set(data) - {*_INTEGER_LIMITS, "max_cost_usd"})
        if unknown:
            raise ConfigError("Unknown ai.limits setting: " + ", ".join(unknown))
        values: dict[str, Any] = {name: data[name] for name in _INTEGER_LIMITS if name in data}
        if data.get("max_cost_usd") is not None:
            values["max_cost_usd"] = _rate(data["max_cost_usd"], "ai.limits.max_cost_usd")
        return cls(**values)

    def narrowed(self, other: "RunLimits | None") -> "RunLimits":
        """These limits, lowered field by field by a run's own limits. A run can never raise a project limit."""

        if other is None:
            return self
        values: dict[str, Any] = {}
        for name in (*_INTEGER_LIMITS, "max_cost_usd"):
            mine, theirs = getattr(self, name), getattr(other, name)
            values[name] = theirs if mine is None else mine if theirs is None else min(mine, theirs)
        return RunLimits(**values)

    def resolved(self, provider_name: str) -> "RunLimits":
        """Every field filled: configured values, else the provider's window and the hard ceilings."""

        window = self.context_window_tokens or DEFAULT_CONTEXT_WINDOWS.get(provider_name, FALLBACK_CONTEXT_WINDOW)
        output = self.request_output_tokens or min(LIMIT_CEILINGS["request_output_tokens"], window // 2)
        if output >= window:
            raise ConfigError(
                f"ai.limits.request_output_tokens ({output}) must be smaller than the context window ({window}), "
                "or no prompt can fit"
            )
        requests = self.max_requests or LIMIT_CEILINGS["max_requests"]
        return RunLimits(
            context_window_tokens=window,
            request_output_tokens=output,
            max_requests=requests,
            max_input_tokens=self.max_input_tokens or LIMIT_CEILINGS["max_input_tokens"],
            max_output_tokens=self.max_output_tokens or LIMIT_CEILINGS["max_output_tokens"],
            max_cost_usd=self.max_cost_usd,
            max_count_requests=self.max_count_requests or requests,
        )


class RunStopped(ProviderError):
    """The run stopped on a limit: before a request was sent, or because reported usage passed a cap.

    ``reason`` is one of the ``STOP_*`` values.
    """

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
    exceeded_reservation: bool = False

    @property
    def measured(self) -> bool:
        return self.usage is not None and self.usage.measured and (self.charged_cost is None or self.cost_measured)


class Ledger:
    """The usage of one run. Distinct runs use distinct ledgers; one ledger is safe across threads."""

    def __init__(self, limits: RunLimits, prices: PriceProfile | None = None, *, notes: tuple[str, ...] = ()) -> None:
        if None in (
            limits.context_window_tokens, limits.request_output_tokens, limits.max_requests, limits.max_input_tokens,
            limits.max_output_tokens, limits.max_count_requests,
        ):
            raise ConfigError("A ledger needs resolved limits")
        if limits.max_cost_usd is not None and (prices is None or not prices.complete):
            raise ConfigError(
                "ai.limits.max_cost_usd needs ai.prices with input, output, cache-read and cache-write rates for this "
                "provider and model (use 0 for a category the provider does not bill); RepoDocs has no built-in rates"
            )
        self.limits = limits
        self.prices = prices
        self._notes = notes
        self._lock = threading.Lock()
        self._requests: list[_Request] = []
        self._counts = 0
        self._stop: tuple[str, str] | None = None
        self._cancelled = False

    # -- limits ------------------------------------------------------------------------------
    def _charged(self) -> tuple[int, int, int | None]:
        cost = None if self.prices is None else sum(item.charged_cost or 0 for item in self._requests)
        return sum(item.charged_input for item in self._requests), sum(item.charged_output for item in self._requests), cost

    def _unpriced(self) -> bool:
        return self.prices is not None and any(item.charged_cost is None for item in self._requests)

    def _cost_cap(self) -> int | None:
        return None if self.limits.max_cost_usd is None else _ceil(self.limits.max_cost_usd * _MICRO)

    def _problem(self, estimate: int, output: int) -> tuple[str, str] | None:
        limits = self.limits
        if self._stop is not None:
            return self._stop
        if len(self._requests) + 1 > limits.max_requests:  # type: ignore[operator]
            return STOP_REQUESTS, f"The next request would be number {len(self._requests) + 1}, past the run limit of {limits.max_requests}"
        if estimate + output > limits.context_window_tokens:  # type: ignore[operator]
            return STOP_CONTEXT, (
                f"The request needs up to {estimate} prompt tokens plus {output} reserved output tokens, which does not fit "
                f"the {limits.context_window_tokens}-token context window; nothing was sent. Reduce the context budgets or "
                "set ai.limits.context_window_tokens to what the model really supports"
            )
        spent_input, spent_output, spent_cost = self._charged()
        if spent_input + estimate > limits.max_input_tokens:  # type: ignore[operator]
            return STOP_INPUT, f"The next request would take the run past its {limits.max_input_tokens} input token cap ({spent_input} already charged)"
        if spent_output + output > limits.max_output_tokens:  # type: ignore[operator]
            return STOP_OUTPUT, f"The next request could take the run past its {limits.max_output_tokens} output token cap ({spent_output} already charged)"
        cap = self._cost_cap()
        if cap is not None and self.prices is not None:
            worst = self.prices.reserve_micro_usd(estimate, output)  # type: ignore[arg-type]
            if (spent_cost or 0) + (worst or 0) > cap:
                return STOP_COST, f"The next request could cost up to {format_usd(worst)} USD, past the {format_usd(cap)} USD cap ({format_usd(spent_cost)} already charged)"
        return None

    def _refuse(self, problem: tuple[str, str]) -> RunStopped:
        if self._stop is None:
            self._stop = problem
        return RunStopped(*self._stop)

    def admit(self) -> None:
        """Refuse early, before any token counting, when the run is already stopped or out of requests."""

        with self._lock:
            problem = self._problem(0, self.limits.request_output_tokens)  # type: ignore[arg-type]
            if problem:
                raise self._refuse(problem)

    def admit_count(self) -> bool:
        """Take one token-count request from the run's allowance. False means count no more and use the byte bound."""

        with self._lock:
            if self._stop is not None or self._counts >= self.limits.max_count_requests:  # type: ignore[operator]
                return False
            self._counts += 1
            return True

    def reserve(self, estimate_tokens: int, method: str, output_cap: int | None = None) -> _Request:
        """Charge a request's worst case, or raise ``RunStopped`` without sending anything.

        ``output_cap`` is the output limit the provider will actually send; it can only be lower than the run's.
        """

        output = self.limits.request_output_tokens if output_cap is None else output_cap
        if output > self.limits.request_output_tokens:  # type: ignore[operator]
            raise ConfigError(f"A request would send an output cap of {output} tokens, above the run's {self.limits.request_output_tokens}")
        with self._lock:
            problem = self._problem(estimate_tokens, output)  # type: ignore[arg-type]
            if problem:
                raise self._refuse(problem)
            cost = None if self.prices is None else self.prices.reserve_micro_usd(estimate_tokens, output)  # type: ignore[arg-type]
            request = _Request(len(self._requests) + 1, estimate_tokens, method, output, estimate_tokens, output, cost, cost)  # type: ignore[arg-type]
            self._requests.append(request)
            return request

    def settle(self, request: _Request, usage: Usage | None, *, outcome: str, model: str | None = None) -> RunStopped | None:
        """Record what happened and return the stop, if reported usage took the run past a cap.

        Complete usage replaces the reservation. Partial usage keeps it, raised to any larger reported part, so
        a reported count is never discarded. A served model the price profile does not cover leaves the cost at
        its reservation; with a cost cap that stops the run, because later costs could not be bounded.
        """

        with self._lock:
            request.outcome = outcome
            request.usage = usage
            request.served_model = model
            if usage is not None:
                prompt = [value for value in (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) if value is not None]
                reported_prompt = sum(prompt) if prompt else None
                reported_output = usage.output_tokens
                request.exceeded_reservation = (reported_prompt or 0) > request.estimate_tokens or (reported_output or 0) > request.output_cap_tokens
                if usage.measured:
                    request.charged_input, request.charged_output = reported_prompt, reported_output  # type: ignore[assignment]
                else:
                    request.charged_input = max(request.charged_input, reported_prompt or 0)
                    request.charged_output = max(request.charged_output, reported_output or 0)
                if self.prices is not None:
                    if model is not None and not self.prices.prices_served(model):
                        # Another model's rates are unknown: no figure from this profile is an upper bound for it.
                        request.charged_cost = None
                    elif usage.measured and model is not None and (actual := self.prices.cost_micro_usd(usage)) is not None:
                        request.charged_cost, request.cost_measured = actual, True
                    elif request.charged_cost is not None:
                        # Partial usage: the charged tokens at the highest applicable rates, never less than the reservation.
                        bound = self.prices.reserve_micro_usd(request.charged_input, request.charged_output)
                        request.charged_cost = max(request.charged_cost, bound) if bound is not None else None
            return self._overage(request)

    def _overage(self, request: _Request) -> RunStopped | None:
        limits = self.limits
        spent_input, spent_output, spent_cost = self._charged()
        cap = self._cost_cap()
        problem: tuple[str, str] | None = None
        if spent_input > limits.max_input_tokens or spent_output > limits.max_output_tokens or (cap is not None and (spent_cost or 0) > cap):  # type: ignore[operator]
            problem = STOP_OVERAGE, (
                f"Request {request.index} reported usage that takes the run past its limits ({spent_input}/{limits.max_input_tokens} "
                f"input tokens, {spent_output}/{limits.max_output_tokens} output tokens"
                + (f", {format_usd(spent_cost)}/{format_usd(cap)} USD" if cap is not None else "")
                + "). Its response was not used and no further request is sent; the provider has already billed it, which cannot be undone"
            )
        elif cap is not None and self.prices is not None and request.served_model is not None and not self.prices.prices_served(request.served_model):
            problem = STOP_UNPRICED, (
                f"Request {request.index} was served by {request.served_model}, which the price profile does not cover, so its "
                "cost stays at its reservation and later cost cannot be bounded; no further request is sent"
            )
        if problem is None:
            return None
        if self._stop is None:
            self._stop = problem
        return RunStopped(*problem)

    def cancel(self) -> None:
        """Stop sending. A request already in flight finishes; its billing cannot be recalled."""

        with self._lock:
            self._cancelled = True
            if self._stop is None:
                self._stop = (STOP_CANCELLED, "The run was cancelled; no further request is sent")

    # -- reporting ---------------------------------------------------------------------------
    def max_estimated_cost_micro(self) -> int | None:
        """The most the run can be charged if every estimate holds, or None without a complete price profile."""

        if self.prices is None or not self.prices.complete:
            return None
        limits = self.limits
        output = min(limits.max_output_tokens, limits.max_requests * limits.request_output_tokens)  # type: ignore[operator]
        worst = self.prices.reserve_micro_usd(limits.max_input_tokens, output)  # type: ignore[arg-type]
        cap = self._cost_cap()
        return worst if cap is None or worst is None else min(worst, cap)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            spent_input, spent_output, spent_cost = self._charged()
            limits = self.limits
            cap = self._cost_cap()
            unmeasured = [item for item in self._requests if not item.measured]
            unpriced = self._unpriced()
            return {
                "limits": limits.to_dict(),
                "price_profile": None if self.prices is None else self.prices.to_dict(),
                "max_estimated_cost_usd": format_usd(self.max_estimated_cost_micro()),
                "requests": [self._describe(item) for item in self._requests],
                "totals": {
                    "requests": len(self._requests),
                    "count_requests": self._counts,
                    "input_tokens": spent_input,
                    "output_tokens": spent_output,
                    "cost_usd": None if unpriced else format_usd(spent_cost),
                    "unmeasured_requests": len(unmeasured),
                    "usage_complete": not unmeasured,
                },
                "remaining": {
                    "requests": max(0, limits.max_requests - len(self._requests)),  # type: ignore[operator]
                    "count_requests": max(0, limits.max_count_requests - self._counts),  # type: ignore[operator]
                    "input_tokens": max(0, limits.max_input_tokens - spent_input),  # type: ignore[operator]
                    "output_tokens": max(0, limits.max_output_tokens - spent_output),  # type: ignore[operator]
                    "cost_usd": None if cap is None or unpriced else format_usd(max(0, cap - (spent_cost or 0))),
                },
                "stop_reason": None if self._stop is None else self._stop[0],
                "stop_message": None if self._stop is None else self._stop[1],
                "notes": [
                    "Charged totals count a request that has no complete usage at its reserved worst case, so they are upper bounds.",
                    "A byte-bound is one token per UTF-8 byte of the serialized request plus framing. It is not a token count.",
                    "Token-count requests send the same request to the same provider; they are counted, not priced.",
                    *self._notes,
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
            "exceeded_reservation": item.exceeded_reservation,
            "charged_input_tokens": item.charged_input,
            "charged_output_tokens": item.charged_output,
            "reserved_cost_usd": format_usd(item.reserved_cost),
            "charged_cost_usd": format_usd(item.charged_cost),
            "cost_measured": item.cost_measured,
        }


def estimate_tokens(text: str) -> int:
    """The labeled byte bound for ``text``: see ``BYTES_PER_TOKEN_BOUND``. Not a token count."""

    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN_BOUND) + REQUEST_OVERHEAD_TOKENS


def serialized_request(provider: Any, system: str, user: str, schema: dict[str, Any]) -> str:
    """The complete request as it will be sent: system, user, schema, model and options."""

    build = getattr(provider, "request_body", None)
    body = build(system, user, schema) if build else {"system": system, "messages": [{"role": "user", "content": user}], "schema": schema}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class BudgetedProvider:
    """A provider whose every ``generate`` call is reserved and reconciled in one shared ledger.

    It is built only from a provider that already passed the consent checks, so a counting request,
    when the provider offers one, is sent to a destination the user has already approved. It is the only
    caller of the provider's ``count_input_tokens``; it does not offer one itself, so wrapping it again
    cannot count or reserve twice. ``request_body`` is passed through for previews.
    """

    def __init__(self, provider: "Provider", ledger: Ledger, *, before_send: Callable[[], None] | None = None) -> None:
        cap = getattr(provider, "max_output_tokens", None)
        limit = ledger.limits.request_output_tokens
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > limit:  # type: ignore[operator]
            raise ConfigError(
                f"The {provider.name} provider would send an output cap of {cap} tokens but this run reserves {limit}; "
                "build the provider with the run's request_output_tokens"
            )
        self._provider = provider
        self.ledger = ledger
        # The cap the provider really sends, when it says; otherwise the run's, which a provider must honor.
        self.output_cap = cap if isinstance(cap, int) and not isinstance(cap, bool) else limit
        self._before_send = before_send
        self.name = provider.name
        self.model = provider.model
        self.external = provider.external

    def request_body(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return json.loads(serialized_request(self._provider, system, user, schema))

    def _estimate(self, system: str, user: str, schema: dict[str, Any]) -> tuple[int, str]:
        counter = getattr(self._provider, "count_input_tokens", None)
        if counter is not None and self.ledger.admit_count():
            try:
                counted = counter(system, user, schema)
            except ProviderError:
                counted = None
            if isinstance(counted, int) and not isinstance(counted, bool) and counted > 0:
                return counted, METHOD_PROVIDER_COUNT
        return estimate_tokens(serialized_request(self._provider, system, user, schema)), METHOD_BYTE_BOUND

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> "ProviderResponse":
        """``before_send`` (for example a cancellation check) runs before the count and again before the request."""

        if self._before_send is not None:
            self._before_send()
        self.ledger.admit()
        estimate, method = self._estimate(system, user, schema)
        if self._before_send is not None:
            self._before_send()
        request = self.ledger.reserve(estimate, method, self.output_cap)
        try:
            response = self._provider.generate(system, user, schema)
        except ProviderError as exc:
            self.ledger.settle(request, exc.usage, outcome="provider-error")
            raise
        except BaseException:
            self.ledger.settle(request, None, outcome="interrupted")
            raise
        stopped = self.ledger.settle(request, getattr(response, "usage", None), outcome="ok", model=response.model)
        if stopped is not None and stopped.reason == STOP_OVERAGE:
            raise stopped
        return response

    def cancel(self) -> None:
        self.ledger.cancel()


def ledger_for(provider: "Provider", settings: "AiSettings", run_limits: RunLimits | None = None) -> Ledger:
    """A new ledger from the project's limits, lowered by a run's own limits, and a price profile bound to this model.

    A profile for another provider or model is not applied: its cost is unknown, and a cost cap refuses to start.
    """

    limits = settings.limits.narrowed(run_limits).resolved(provider.name)
    prices, notes = settings.prices, ()
    if prices is not None and not prices.covers(provider.name, provider.model):
        if limits.max_cost_usd is not None:
            raise ConfigError(
                f"ai.prices is for {prices.provider}/{prices.model}, not {provider.name}/{provider.model}; update the price "
                "profile before running with a cost cap"
            )
        prices, notes = None, (f"The price profile for {settings.prices.provider}/{settings.prices.model} does not apply to this model, so cost is unknown.",)  # type: ignore[union-attr]
    return Ledger(limits, prices, notes=notes)


def budgeted(
    provider: "Provider", settings: "AiSettings", run_limits: RunLimits | None = None, *, before_send: Callable[[], None] | None = None,
) -> BudgetedProvider:
    """Wrap a resolved provider with a new ledger, after consent.

    A provider the caller already budgeted is used as it is, provided its ledger stays within the project's limits;
    it cannot also be given separate run limits, which would otherwise be ignored.
    """

    if provider.external and not settings.external_consent:
        raise ConsentError("A usage budget cannot be started for an external provider without recorded consent")
    if isinstance(provider, BudgetedProvider):
        if run_limits is not None:
            raise ConfigError("Give run limits either to the caller's ledger or to the request, not both")
        project = settings.limits.resolved(provider.name)
        mine = provider.ledger.limits
        for name in (*_INTEGER_LIMITS, "max_cost_usd"):
            ceiling, value = getattr(project, name), getattr(mine, name)
            if ceiling is not None and (value is None or value > ceiling):
                raise ConfigError(f"The supplied run ledger's {name} exceeds the project's ai.limits")
        return provider
    return BudgetedProvider(provider, ledger_for(provider, settings, run_limits), before_send=before_send)


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
        "stop_message": body["stop_message"],
        "max_estimated_cost_usd": body["max_estimated_cost_usd"],
        **body["totals"],
        "remaining": body["remaining"],
    }
