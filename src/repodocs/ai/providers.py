"""Provider-neutral model access: one BYO cloud provider (Anthropic) and Ollama.

The engine only knows the `Provider` protocol. A provider receives a system
instruction, one user message holding the evidence bundle, and a JSON schema, and
returns the parsed JSON object plus the model that served it.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..config import AI_PROVIDER_ANTHROPIC, AI_PROVIDER_NONE, AI_PROVIDER_OLLAMA, AI_PROVIDER_OPENAI, AiSettings
from ..errors import ConfigError, ConsentError, MalformedProviderOutput, ProviderError
from .budgets import DEFAULT_CONTEXT_WINDOWS, Usage, serialized_request, usage_from_counts


DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
MAX_OUTPUT_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 300
# The most a local or private Ollama server may send back for one request (the whole HTTP body, counted
# while it is read, never trusting Content-Length). It is generous for MAX_OUTPUT_TOKENS of JSON, with room
# for escaping, and well above the 32 KiB architecture discovery accepts as a model answer.
MAX_RESPONSE_BYTES = 512 * 1024


@dataclass(frozen=True)
class ProviderResponse:
    """The parsed object, the model that served it, and usage when the provider reported any."""

    data: dict[str, Any]
    model: str
    usage: Usage | None = None


class Provider(Protocol):
    name: str
    model: str
    external: bool

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse: ...


def _failed(error: ProviderError, usage: Usage | None) -> ProviderError:
    """Attach the usage a request already cost to the error that ended it."""

    error.usage = usage
    return error


def _parse_object(text: str, provider: str, usage: Usage | None = None) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise _failed(MalformedProviderOutput(f"{provider} returned output that is not valid JSON"), usage) from None
    if not isinstance(data, dict):
        raise _failed(MalformedProviderOutput(f"{provider} returned JSON that is not an object"), usage)
    return data


def _anthropic_usage(response: Any) -> Usage | None:
    raw = getattr(response, "usage", None)
    if raw is None:
        return None
    details = getattr(raw, "output_tokens_details", None)
    return usage_from_counts(
        getattr(raw, "input_tokens", None), getattr(raw, "output_tokens", None),
        getattr(raw, "cache_read_input_tokens", None), getattr(raw, "cache_creation_input_tokens", None),
        getattr(details, "thinking_tokens", None),
    )


class AnthropicProvider:
    """Claude through the official SDK. Credentials come from the environment, never from config."""

    name = AI_PROVIDER_ANTHROPIC
    external = True

    def __init__(self, model: str | None, *, max_output_tokens: int = MAX_OUTPUT_TOKENS, effort: str | None = None) -> None:
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self.max_output_tokens = max_output_tokens
        self.effort = effort

    def request_body(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """The exact request. The output cap and effort are explicit and are never changed on the way."""

        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        return {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }

    @staticmethod
    def _sdk() -> Any:
        try:
            import anthropic
        except ImportError as exc:
            raise ConfigError('The anthropic provider needs the SDK; install it with: pip install "repodocs[ai]"') from None
        return anthropic

    @staticmethod
    def _client(anthropic: Any) -> Any:
        # The SDK follows redirects by default. A redirect would send the evidence bundle to a
        # target nobody approved, so the transport is built here without that, and the client
        # (which closes it) is a context manager.
        # The SDK also retries failed requests internally by default. Each retry would be a request the run ledger
        # never reserved, so retries are off; a failed request is reported, not repeated.
        return anthropic.Anthropic(max_retries=0, http_client=anthropic.DefaultHttpxClient(follow_redirects=False))

    def count_input_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int:
        """The provider's own count of this request. Callers use it only after consent for this provider."""

        anthropic = self._sdk()
        body = self.request_body(system, user, schema)
        del body["max_tokens"]
        try:
            with self._client(anthropic) as client:
                return int(client.beta.messages.count_tokens(**body).input_tokens)
        except Exception as exc:
            raise ProviderError("Anthropic could not count the request tokens") from None

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        anthropic = self._sdk()
        try:
            # No server-side fallback is requested: a substitute model would bill at a rate that was
            # never reserved, and the model must never change without the user choosing it.
            with self._client(anthropic) as client:
                response = client.beta.messages.create(**self.request_body(system, user, schema))
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the credentials; set ANTHROPIC_API_KEY or run `ant auth login`") from None
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached; retry later") from None
        except anthropic.APIStatusError as exc:
            if 300 <= exc.status_code < 400:
                raise ProviderError(
                    f"Anthropic answered with a redirect (HTTP {exc.status_code}), which is not followed so the "
                    "bundle is not sent anywhere else; check ANTHROPIC_BASE_URL"
                ) from None
            raise ProviderError(f"Anthropic API error (HTTP {exc.status_code})") from None
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Unable to reach the Anthropic API") from None
        usage = _anthropic_usage(response)
        if response.stop_reason == "refusal":
            raise _failed(ProviderError("The model declined the request"), usage)
        if response.stop_reason == "max_tokens":
            raise _failed(ProviderError("The model output was cut off at the token limit"), usage)
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise _failed(ProviderError("Anthropic returned no text output"), usage)
        return ProviderResponse(_parse_object(text, "Anthropic", usage), response.model, usage)


def is_loopback(host_url: str) -> bool:
    host = urlsplit(host_url).hostname
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A provider answers the request it was sent; a redirect would send the bundle somewhere unchecked."""

    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


def _opener(external: bool) -> urllib.request.OpenerDirector:
    """An opener that never redirects, and never routes a loopback request through a proxy.

    ``HTTP_PROXY`` in the environment would otherwise carry a bundle meant for this machine to
    another host without the consent that sending it there requires.
    """

    handlers: list[Any] = [_NoRedirects]
    if not external:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _read_bounded(response: Any, limit: int) -> bytes:
    """The response body, refusing to read more than ``limit`` bytes.

    A declared length over the limit is refused before reading. An undeclared or understated length
    is caught while reading, so a hostile or broken server cannot make RepoDocs buffer an unbounded body.
    """

    declared = response.headers.get("Content-Length")
    if declared is not None and declared.strip().isdigit() and int(declared) > limit:
        raise ProviderError(f"Ollama response is larger than the {limit} byte limit")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ProviderError(f"Ollama response is larger than the {limit} byte limit")
    return data


def _openai_completion_text(payload: dict[str, Any]) -> str:
    """The text of a successful chat completion, or a ProviderError saying why it is not one.

    Only one outcome is a success: exactly one assistant message, finished with ``stop``, holding
    non-empty text and nothing else the request did not ask for (no refusal, no tool calls).
    """

    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError("Ollama gateway returned no chat completion")
    choice = choices[0]
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    finish = choice.get("finish_reason")
    if message.get("refusal"):
        raise ProviderError("The model declined the request")
    if finish == "length":
        raise ProviderError("Ollama gateway output was cut off at the token limit")
    if finish == "content_filter":
        raise ProviderError("Ollama gateway withheld the output (content filter)")
    if finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
        raise ProviderError("Ollama gateway answered with a tool call, which this request does not allow")
    if finish != "stop":
        raise ProviderError("Ollama gateway ended the completion in an unsupported state")
    if message.get("role") != "assistant":
        raise ProviderError("Ollama gateway returned no assistant message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("Ollama returned no message content")
    return content


class OllamaProvider:
    """Ollama through its native /api/chat or an authenticated Ollama-compatible /v1 gateway.

    A host ending in /v1 uses chat completions, as Ollama serves them (``response_format`` with a JSON
    schema, ``max_tokens``, ``finish_reason``). This is a transport for Ollama and gateways that
    proxy it, not a claim of compatibility with every OpenAI-style provider. Other supported hosts use
    the native endpoint. Both keep the same provider identity and claim validation, and both
    take whatever schema the caller passes.
    """

    name = AI_PROVIDER_OLLAMA

    def __init__(
        self, model: str | None, host: str | None, *, context_window: int | None = None, max_output_tokens: int | None = None
    ) -> None:
        if not model:
            raise ConfigError("The ollama provider needs a model; set one with repodocs ai-configure --provider ollama --model NAME")
        self.model = model
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
        try:
            parts = urlsplit(self.host)
            port = parts.port
        except ValueError:
            raise ConfigError("Ollama host must be a valid http(s) URL") from None
        if parts.scheme not in ("http", "https") or not parts.hostname or port == 0:
            raise ConfigError("Ollama host must be an http(s) URL")
        if parts.username is not None or parts.password is not None or "?" in self.host or "#" in self.host or parts.path not in ("", "/v1"):
            raise ConfigError("Ollama host must be a base URL or end in /v1, without URL credentials, query or fragment")
        self.openai_compatible = parts.path == "/v1"
        self.external = not is_loopback(self.host)
        if self.openai_compatible and self.external and parts.scheme != "https":
            raise ConfigError("A remote Ollama /v1 gateway must use HTTPS")
        self.context_window = context_window or DEFAULT_CONTEXT_WINDOWS[AI_PROVIDER_OLLAMA]
        self.max_output_tokens = max_output_tokens or min(MAX_OUTPUT_TOKENS, self.context_window // 2)

    def request_body(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.openai_compatible:
            return {
                **body,
                "max_tokens": self.max_output_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "repodocs_response", "schema": schema},
                },
            }
        return {
            **body,
            "format": schema,
            "options": {"num_ctx": self.context_window, "num_predict": self.max_output_tokens},
        }

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        headers = {"Content-Type": "application/json"}
        url = f"{self.host}/api/chat"
        if self.openai_compatible:
            url = f"{self.host}/chat/completions"
            key = os.environ.get("REPODOCS_OLLAMA_API_KEY", "").strip()
            if self.external and not key:
                raise ConfigError("The remote Ollama /v1 gateway needs REPODOCS_OLLAMA_API_KEY in the environment")
            if key and not (key.isascii() and key.isprintable()):
                raise ConfigError("REPODOCS_OLLAMA_API_KEY must be printable ASCII with no line breaks")
            if key:
                headers["Authorization"] = f"Bearer {key}"
        body = serialized_request(self, system, user, schema).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        try:
            with _opener(self.external).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
                raw = _read_bounded(response, MAX_RESPONSE_BYTES)
            payload = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ProviderError(
                    f"Ollama returned a redirect (HTTP {exc.code}), which is not followed so the bundle "
                    "is not sent anywhere else; configure the final host address"
                ) from None
            if self.openai_compatible and exc.code in (401, 403):
                raise ProviderError(f"Ollama gateway authentication failed (HTTP {exc.code}); check REPODOCS_OLLAMA_API_KEY") from None
            if self.openai_compatible and exc.code == 429:
                raise ProviderError("Ollama gateway is rate-limiting requests (HTTP 429)") from None
            raise ProviderError(f"Ollama error (HTTP {exc.code}); is the model pulled and gateway configured?") from None
        except TimeoutError:
            raise ProviderError(f"Ollama did not answer within {REQUEST_TIMEOUT_SECONDS} seconds") from None
        except (ValueError, RecursionError):
            raise ProviderError("Ollama returned a response that is not valid JSON") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise ProviderError(f"Ollama did not answer within {REQUEST_TIMEOUT_SECONDS} seconds") from None
            raise ProviderError("Unable to reach Ollama") from None

        if not isinstance(payload, dict):
            raise ProviderError("Ollama returned a malformed response")
        usage = _ollama_usage(payload, gateway=self.openai_compatible)
        try:
            content = _openai_completion_text(payload) if self.openai_compatible else _native_completion_text(payload)
        except ProviderError as exc:
            raise _failed(exc, usage) from None
        if usage is not None and usage.prompt_tokens is not None and usage.output_tokens is not None:
            if usage.prompt_tokens + usage.output_tokens >= self.context_window:
                raise _failed(ProviderError(
                    f"The prompt and output filled the {self.context_window}-token context window, so the runtime may have "
                    "truncated the prompt or the answer; raise ai.limits.context_window_tokens only if the model supports it"
                ), usage)
        model = payload.get("model")
        return ProviderResponse(_parse_object(content, "Ollama", usage), model if isinstance(model, str) and model else self.model, usage)


def _native_completion_text(payload: dict[str, Any]) -> str:
    if payload.get("done_reason") == "length":
        raise ProviderError("The Ollama output was cut off at the token limit")
    if payload.get("done") is not True or payload.get("done_reason") != "stop":
        raise ProviderError("Ollama did not report a finished completion (done_reason was not stop)")
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ProviderError("Ollama returned no message content")
    return content


def _ollama_usage(payload: dict[str, Any], *, gateway: bool = False) -> Usage | None:
    if gateway:
        raw = payload.get("usage")
        if not isinstance(raw, dict):
            return None
        usage = usage_from_counts(raw.get("prompt_tokens"), raw.get("completion_tokens"), None, None)
    else:
        usage = usage_from_counts(payload.get("prompt_eval_count"), payload.get("eval_count"), None, None)
    if usage is not None and usage.input_tokens is not None and usage.output_tokens is not None:
        # Both Ollama transports count total prompt and output with no separate billed cache tier.
        usage = replace(usage, cache_read_tokens=0, cache_write_tokens=0)
    return usage


def require_source_consent(provider: Provider, settings: AiSettings) -> None:
    """Source excerpts leave the machine only with their own consent, given for the current policy.

    Consent for the IR-only evidence bundle never covers excerpts. A loopback Ollama host sends
    nothing off the machine and needs none.
    """

    if provider.external and not settings.source_consent_current:
        raise ConsentError(
            f"The {provider.name} provider would receive bounded, redacted source excerpts, and no consent for "
            "that is recorded for the current policy. Review them with `repodocs architecture context`, then run "
            "`repodocs ai-configure --consent-source-excerpts`. Consent for the evidence bundle does not cover it"
        )


def resolve_discovery_provider(settings: AiSettings) -> Provider | None:
    """The provider for architecture discovery: external consent and source consent are both required."""

    provider = resolve_provider(settings)
    if provider is not None:
        require_source_consent(provider, settings)
    return provider


def resolve_provider(settings: AiSettings) -> Provider | None:
    """Build the configured provider, or None when AI is disabled.

    External providers are refused unless the project records explicit consent, so a
    project without consent is local-only: nothing can be sent off the machine.
    """

    if settings.provider == AI_PROVIDER_NONE:
        return None
    provider: Provider
    limits = settings.limits.resolved(settings.provider)
    if settings.provider == AI_PROVIDER_OPENAI:
        from .openai import OpenAIProvider

        provider = OpenAIProvider(
            settings.model, settings.openai_base_url, max_output_tokens=limits.request_output_tokens, reasoning_effort=settings.effort,
        )
    elif settings.provider == AI_PROVIDER_ANTHROPIC:
        provider = AnthropicProvider(settings.model, max_output_tokens=limits.request_output_tokens, effort=settings.effort)
    else:
        if settings.effort:
            raise ConfigError("ai.effort is supported only by the anthropic and openai providers; remove it for ollama")
        provider = OllamaProvider(
            settings.model, settings.ollama_host,
            context_window=limits.context_window_tokens, max_output_tokens=limits.request_output_tokens,
        )
    if provider.external and not settings.external_consent:
        raise ConsentError(
            f"The {provider.name} provider would send the evidence bundle off this machine and no consent is "
            "recorded; this project is local-only. Review the bundle with `repodocs ai-bundle`, then run "
            "`repodocs ai-configure --consent-external` to allow it"
        )
    return provider
