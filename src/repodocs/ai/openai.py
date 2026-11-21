"""OpenAI Responses transport. No SDK, retries, redirects or model fallback."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from ..errors import ConfigError, MalformedProviderOutput, ProviderError
from .providers import MAX_OUTPUT_TOKENS, REQUEST_TIMEOUT_SECONDS, ProviderResponse, _opener, _parse_object, is_loopback

DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_RESPONSE_BYTES = 512 * 1024
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class OpenAIProvider:
    name = "openai"

    def __init__(
        self, model: str | None, base_url: str | None = None, *, api_key: str | None = None,
        max_output_tokens: int = MAX_OUTPUT_TOKENS, reasoning_effort: str | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ConfigError("The openai provider requires an explicit model")
        self.model = model.strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if any(c.isspace() or ord(c) < 32 for c in self.base_url):
            raise ConfigError("OpenAI base URL cannot contain whitespace or control characters")
        try:
            url = urlsplit(self.base_url)
            valid = url.hostname and url.port != 0
        except ValueError:
            valid = False
        if not valid or url.username or url.password or url.query or url.fragment:
            raise ConfigError("OpenAI base URL must be a plain HTTPS endpoint without credentials, query or fragment")
        if url.scheme != "https" and not (url.scheme == "http" and is_loopback(self.base_url)):
            raise ConfigError("OpenAI requires HTTPS; HTTP is allowed only for loopback testing")
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

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        key = self._api_key if self._api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not isinstance(key, str) or not key.strip() or any(c in key.strip() for c in "\r\n\x00"):
            raise ConfigError("Set OPENAI_API_KEY or supply a non-empty in-memory provider credential")
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(self.request_body(system, user, schema), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            headers={"Authorization": "Bearer " + key.strip(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _opener(self.external).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderError("OpenAI response exceeds the response byte limit")
            payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Do not include the body: it can echo prompts, credentials or private endpoint details.
            raise ProviderError(f"OpenAI request failed (HTTP {exc.code}); redirects are never followed") from None
        except (urllib.error.URLError, OSError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("Unable to read a valid OpenAI response") from None
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ProviderError("OpenAI did not return a completed response")
        if payload.get("error") is not None or payload.get("incomplete_details") is not None:
            raise ProviderError("OpenAI returned an error or incomplete response")
        output = payload.get("output")
        if not isinstance(output, list):
            raise ProviderError("OpenAI returned no output messages")
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError("OpenAI returned malformed output")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("status") != "completed" or item.get("role") != "assistant":
                raise ProviderError("OpenAI returned unsupported or incomplete output")
            content = item.get("content")
            if not isinstance(content, list):
                raise ProviderError("OpenAI returned malformed message content")
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                    raise ProviderError("OpenAI refused the request or returned unsupported content")
                texts.append(block["text"])
        if len(texts) != 1:
            raise ProviderError("OpenAI must return exactly one structured JSON output")
        model = payload.get("model")
        if not isinstance(model, str) or not model or len(model) > 256:
            raise ProviderError("OpenAI response is missing the serving model")
        try:
            data = _parse_object(texts[0], "OpenAI")
        except RecursionError:
            raise MalformedProviderOutput("OpenAI output exceeds JSON nesting limits") from None
        return ProviderResponse(data, model)
