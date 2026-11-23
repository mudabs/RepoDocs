"""OpenAI Responses transport (ADR-032). No SDK, retries, redirects or model fallback.

Usage follows ADR-031: ``input_tokens`` in a response includes cached and cache-written tokens, so the
base-rate share is what remains after both are removed; ``output_tokens`` already includes reasoning, which
is recorded as a subset and never added again. A missing or malformed count is unknown, never zero.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from ..errors import ConfigError, MalformedProviderOutput, ProviderError
from .budgets import Usage, usage_from_counts
from .providers import MAX_OUTPUT_TOKENS, REQUEST_TIMEOUT_SECONDS, ProviderResponse, _failed, _opener, _parse_object, is_loopback

DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_COUNT_RESPONSE_BYTES = 4096
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
# Fields of a Responses request that the input-token count endpoint does not take.
_NOT_COUNTED = ("max_output_tokens", "store")


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def openai_usage(payload: Any) -> Usage | None:
    """The disjoint billed categories of a Responses ``usage`` object, or None when nothing usable was reported."""

    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    inputs = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    outputs = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    total, cached, written = _whole(usage.get("input_tokens")), _whole(inputs.get("cached_tokens")), _whole(inputs.get("cache_write_tokens"))
    output, reasoning = _whole(usage.get("output_tokens")), _whole(outputs.get("reasoning_tokens"))
    if output is not None and reasoning is not None and reasoning > output:
        return None  # reasoning is part of output: more reasoning than output is contradictory, not usage
    base: int | None = None
    if total is not None and cached is not None:
        base = total - cached - (written or 0)
        if base < 0:
            return None  # contradictory counts are not usage
    elif total is not None and cached is None:
        base, written = total, None  # categories unknown: the total is kept, the request stays unmeasured
    return usage_from_counts(base, output, cached, written, reasoning)


def check_base_url(value: str | None) -> str:
    """The endpoint as it will be used, or ``ConfigError`` naming the rule (never the value, which may hold a secret)."""

    base_url = (value or DEFAULT_BASE_URL).rstrip("/")
    if any(c.isspace() or ord(c) < 32 for c in base_url):
        raise ConfigError("OpenAI base URL cannot contain whitespace or control characters")
    try:
        url = urlsplit(base_url)
        valid = url.hostname and url.port != 0
    except ValueError:
        valid = False
    if not valid or url.username or url.password or url.query or url.fragment:
        raise ConfigError("OpenAI base URL must be a plain HTTPS endpoint without credentials, query or fragment")
    if url.scheme != "https" and not (url.scheme == "http" and is_loopback(base_url)):
        raise ConfigError("OpenAI requires HTTPS; HTTP is allowed only for loopback testing")
    return base_url


class OpenAIProvider:
    name = "openai"

    def __init__(
        self, model: str | None, base_url: str | None = None, *, api_key: str | None = None,
        max_output_tokens: int = MAX_OUTPUT_TOKENS, reasoning_effort: str | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ConfigError("The openai provider requires an explicit model")
        self.model = model.strip()
        self.base_url = check_base_url(base_url)
        self.external = not is_loopback(self.base_url)
        self._api_key = api_key
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ConfigError(f"max_output_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
        if reasoning_effort is not None and reasoning_effort not in EFFORTS:
            raise ConfigError("Unsupported OpenAI reasoning effort")
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort

    def request_body(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model, "instructions": system,
            "input": [{"role": "user", "content": user}],
            "text": {"format": {"type": "json_schema", "name": "repodocs", "strict": True, "schema": schema}},
            "max_output_tokens": self.max_output_tokens, "store": False,
        }
        if self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        return payload

    def _key(self) -> str:
        key = self._api_key if self._api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not isinstance(key, str) or not key.strip() or any(c in key.strip() for c in "\r\n\x00"):
            raise ConfigError("Set OPENAI_API_KEY or supply a non-empty in-memory provider credential")
        return key.strip()

    def _post(self, path: str, body: dict[str, Any], limit: int) -> Any:
        """One POST, never retried or redirected, with a bounded, sanitized answer."""

        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._key(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _opener(self.external).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ProviderError("OpenAI response exceeds the response byte limit")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Do not include the body: it can echo prompts, credentials or private endpoint details.
            raise ProviderError(f"OpenAI request failed (HTTP {exc.code}); redirects are never followed") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, UnicodeError, RecursionError):
            # http.client errors (a bad status line, a cut-off body, too many headers) can carry raw bytes the
            # endpoint sent back, including an echo of the request, so none of their text is kept.
            raise ProviderError("Unable to read a valid OpenAI response") from None

    def count_input_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int:
        """OpenAI's own count of this request (``/responses/input_tokens``). Only the run ledger calls it, after consent."""

        body = {key: value for key, value in self.request_body(system, user, schema).items() if key not in _NOT_COUNTED}
        payload = self._post("/responses/input_tokens", body, MAX_COUNT_RESPONSE_BYTES)
        count = _whole(payload.get("input_tokens")) if isinstance(payload, dict) and payload.get("object") == "response.input_tokens" else None
        if not count:
            raise ProviderError("OpenAI returned no usable input token count")
        return count

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        payload = self._post("/responses", self.request_body(system, user, schema), MAX_RESPONSE_BYTES)
        # Reported usage travels with every error below, so the run ledger reconciles a request that was billed.
        usage = openai_usage(payload)
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise _failed(ProviderError("OpenAI did not return a completed response"), usage)
        if payload.get("error") is not None or payload.get("incomplete_details") is not None:
            raise _failed(ProviderError("OpenAI returned an error or incomplete response"), usage)
        output = payload.get("output")
        if not isinstance(output, list):
            raise _failed(ProviderError("OpenAI returned no output messages"), usage)
        texts: list[str] = []
        messages = 0
        for item in output:
            if not isinstance(item, dict):
                raise _failed(ProviderError("OpenAI returned malformed output"), usage)
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("status") != "completed" or item.get("role") != "assistant":
                raise _failed(ProviderError("OpenAI returned unsupported or incomplete output"), usage)
            messages += 1
            content = item.get("content")
            if not isinstance(content, list):
                raise _failed(ProviderError("OpenAI returned malformed message content"), usage)
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                    raise _failed(ProviderError("OpenAI refused the request or returned unsupported content"), usage)
                texts.append(block["text"])
        if messages != 1 or len(texts) != 1:
            raise _failed(ProviderError("OpenAI must return exactly one structured JSON output"), usage)
        model = payload.get("model")
        if not isinstance(model, str) or not model or len(model) > 256:
            raise _failed(ProviderError("OpenAI response is missing the serving model"), usage)
        try:
            data = _parse_object(texts[0], "OpenAI", usage)
        except RecursionError:
            raise _failed(MalformedProviderOutput("OpenAI output exceeds JSON nesting limits"), usage) from None
        return ProviderResponse(data, model, usage)
