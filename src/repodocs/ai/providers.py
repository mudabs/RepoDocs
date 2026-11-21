"""Provider-neutral model access: one BYO cloud provider (Anthropic) and Ollama.

The engine only knows the `Provider` protocol. A provider receives a system
instruction, one user message holding the evidence bundle, and a JSON schema, and
returns the parsed JSON object plus the model that served it.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..config import AI_PROVIDER_ANTHROPIC, AI_PROVIDER_NONE, AI_PROVIDER_OLLAMA, AiSettings
from ..errors import ConfigError, ConsentError, MalformedProviderOutput, ProviderError
from .budgets import DEFAULT_CONTEXT_WINDOWS, Usage, usage_from_counts


DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
MAX_OUTPUT_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 300


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
    except (TypeError, ValueError) as exc:
        raise _failed(MalformedProviderOutput(f"{provider} returned output that is not valid JSON"), usage) from exc
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
            raise ConfigError('The anthropic provider needs the SDK; install it with: pip install "repodocs[ai]"') from exc
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
            raise ProviderError("Anthropic could not count the request tokens") from exc

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        anthropic = self._sdk()
        try:
            # No server-side fallback is requested: a substitute model would bill at a rate that was
            # never reserved, and the model must never change without the user choosing it.
            with self._client(anthropic) as client:
                response = client.beta.messages.create(**self.request_body(system, user, schema))
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the credentials; set ANTHROPIC_API_KEY or run `ant auth login`") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached; retry later") from exc
        except anthropic.APIStatusError as exc:
            if 300 <= exc.status_code < 400:
                raise ProviderError(
                    f"Anthropic answered with a redirect (HTTP {exc.status_code}), which is not followed so the "
                    "bundle is not sent anywhere else; check ANTHROPIC_BASE_URL"
                ) from exc
            raise ProviderError(f"Anthropic API error (HTTP {exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Unable to reach the Anthropic API") from exc
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


class OllamaProvider:
    """A local Ollama server. A non-loopback host leaves the machine and counts as external."""

    name = AI_PROVIDER_OLLAMA

    def __init__(
        self, model: str | None, host: str | None, *, context_window: int | None = None, max_output_tokens: int | None = None
    ) -> None:
        if not model:
            raise ConfigError("The ollama provider needs a model; set one with `repodocs ai-configure --provider ollama --model <name>`")
        self.model = model
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
        parts = urlsplit(self.host)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError(f"Ollama host must be an http(s) URL: {self.host}")
        self.external = not is_loopback(self.host)
        # The window is sent with every request. A runtime that is not told silently truncates a prompt
        # that does not fit its own default, which is what the preflight check exists to prevent.
        self.context_window = context_window or DEFAULT_CONTEXT_WINDOWS[AI_PROVIDER_OLLAMA]
        self.max_output_tokens = max_output_tokens or min(MAX_OUTPUT_TOKENS, self.context_window // 2)

    def request_body(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"num_ctx": self.context_window, "num_predict": self.max_output_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        body = json.dumps(self.request_body(system, user, schema)).encode("utf-8")
        request = urllib.request.Request(
            f"{self.host}/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with _opener(self.external).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - scheme checked above
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ProviderError(
                    f"Ollama at {self.host} answered with a redirect (HTTP {exc.code}), which is not followed so the "
                    "bundle is not sent anywhere else; set ollama_host to the final address"
                ) from exc
            raise ProviderError(f"Ollama error (HTTP {exc.code}); is the model `{self.model}` pulled?") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderError(f"Unable to reach Ollama at {self.host}") from exc
        message = payload.get("message") if isinstance(payload, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ProviderError("Ollama returned no message content")
        usage = _ollama_usage(payload)
        if payload.get("done_reason") == "length":
            raise _failed(ProviderError("The Ollama output was cut off at the token limit"), usage)
        # Only a finished completion is an answer. An unfinished frame, or one that ended for another reason
        # (a model load or unload, an abort), is incomplete even when its content happens to parse.
        if payload.get("done") is not True or payload.get("done_reason") != "stop":
            raise _failed(ProviderError("Ollama did not report a finished completion (done_reason was not stop)"), usage)
        if usage is not None and usage.measured and usage.prompt_tokens + usage.output_tokens >= self.context_window:  # type: ignore[operator]
            raise _failed(ProviderError(
                f"The prompt and output filled the {self.context_window}-token context window, so the runtime may have "
                "truncated the prompt or the answer; raise ai.limits.context_window_tokens only if the model supports it"
            ), usage)
        model = payload.get("model")
        return ProviderResponse(_parse_object(content, "Ollama", usage), model if isinstance(model, str) and model else self.model, usage)


def _ollama_usage(payload: Any) -> Usage | None:
    usage = usage_from_counts(payload.get("prompt_eval_count"), payload.get("eval_count"), None, None)
    if usage is not None and usage.input_tokens is not None and usage.output_tokens is not None:
        # A local server has no cache tier that bills, so complete counts mean both cache fields are zero.
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
    if settings.provider == AI_PROVIDER_ANTHROPIC:
        provider = AnthropicProvider(settings.model, max_output_tokens=limits.request_output_tokens, effort=settings.effort)
    else:
        if settings.effort:
            raise ConfigError("ai.effort is supported only by the anthropic provider; remove it for ollama")
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
