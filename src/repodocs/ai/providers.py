"""Provider-neutral model access: one BYO cloud provider (Anthropic) and Ollama.

The engine only knows the `Provider` protocol. A provider receives a system
instruction, one user message holding the evidence bundle, and a JSON schema, and
returns the parsed JSON object plus the model that served it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..config import AI_PROVIDER_ANTHROPIC, AI_PROVIDER_NONE, AI_PROVIDER_OLLAMA, AiSettings
from ..errors import ConfigError, ConsentError, ProviderError


DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
MAX_OUTPUT_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 300
# The most a local or private Ollama server may send back for one request (the whole HTTP body, counted
# while it is read, never trusting Content-Length). It is generous for MAX_OUTPUT_TOKENS of JSON, with room
# for escaping, and well above the 32 KiB architecture discovery accepts as a model answer.
MAX_RESPONSE_BYTES = 512 * 1024
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class ProviderResponse:
    data: dict[str, Any]
    model: str


class Provider(Protocol):
    name: str
    model: str
    external: bool

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse: ...


def _parse_object(text: str, provider: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"{provider} returned output that is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"{provider} returned JSON that is not an object")
    return data


class AnthropicProvider:
    """Claude through the official SDK. Credentials come from the environment, never from config."""

    name = AI_PROVIDER_ANTHROPIC
    external = True

    def __init__(self, model: str | None) -> None:
        self.model = model or DEFAULT_ANTHROPIC_MODEL

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        try:
            import anthropic
        except ImportError as exc:
            raise ConfigError('The anthropic provider needs the SDK; install it with: pip install "repodocs[ai]"') from exc
        try:
            # The SDK follows redirects by default. A redirect would send the evidence bundle to a
            # target nobody approved, so the transport is built here without that, and the client
            # (which closes it) is a context manager.
            with anthropic.Anthropic(http_client=anthropic.DefaultHttpxClient(follow_redirects=False)) as client:
                response = client.beta.messages.create(
                    model=self.model,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                    # On a safety decline the API retries on a fallback model inside the same call.
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
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
        if response.stop_reason == "refusal":
            raise ProviderError("The model declined the request")
        if response.stop_reason == "max_tokens":
            raise ProviderError("The model output was cut off at the token limit")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise ProviderError("Anthropic returned no text output")
        return ProviderResponse(_parse_object(text, "Anthropic"), response.model)


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
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
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
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("Ollama returned no message content")
    return content


class OllamaProvider:
    """Ollama through its native /api/chat or an authenticated Ollama-compatible /v1 gateway.

    A host ending in /v1 uses chat completions, as Ollama serves them (``response_format`` with a JSON
    schema, ``max_tokens``, ``finish_reason``). This is a transport for Ollama and gateways that
    proxy it, not a claim of compatibility with every OpenAI-style provider. Other supported hosts use
    the native endpoint, unchanged. Both keep the same provider identity and claim validation, and both
    take whatever schema the caller passes.
    """

    name = AI_PROVIDER_OLLAMA

    def __init__(self, model: str | None, host: str | None) -> None:
        if not model:
            raise ConfigError("The ollama provider needs a model; set one with repodocs ai-configure --provider ollama --model NAME")
        self.model = model
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
        parts = urlsplit(self.host)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError("Ollama host must be an http(s) URL")
        if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/v1"):
            raise ConfigError("Ollama host must be a base URL or end in /v1, without URL credentials, query or fragment")
        self.openai_compatible = parts.path == "/v1"
        self.external = not is_loopback(self.host)
        if self.openai_compatible and self.external and parts.scheme != "https":
            raise ConfigError("A remote Ollama /v1 gateway must use HTTPS")

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        headers = {"Content-Type": "application/json"}
        if self.openai_compatible:
            # The private gateway authenticates with a bearer token. Never persist it in
            # repodocs.json or include it in an error message.
            key = os.environ.get("REPODOCS_OLLAMA_API_KEY", "")
            if self.external and not key:
                raise ConfigError("The remote Ollama /v1 gateway needs REPODOCS_OLLAMA_API_KEY in the environment")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            url = f"{self.host}/chat/completions"
            body = {
                "model": self.model,
                "stream": False,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "repodocs_response", "schema": schema},
                },
            }
        else:
            url = f"{self.host}/api/chat"
            body = {"model": self.model, "stream": False, "format": schema, "messages": messages}
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
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
                ) from exc
            if self.openai_compatible and exc.code in (401, 403):
                raise ProviderError(f"Ollama gateway authentication failed (HTTP {exc.code}); check REPODOCS_OLLAMA_API_KEY") from exc
            if exc.code == 429:
                raise ProviderError("Ollama gateway is rate-limiting requests (HTTP 429)") from exc
            raise ProviderError(f"Ollama error (HTTP {exc.code}); is the model pulled and gateway configured?") from exc
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderError(f"Ollama did not answer within {REQUEST_TIMEOUT_SECONDS} seconds") from exc
        except ValueError as exc:
            raise ProviderError("Ollama returned a response that is not valid JSON") from exc
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise ProviderError(f"Ollama did not answer within {REQUEST_TIMEOUT_SECONDS} seconds") from exc
            raise ProviderError("Unable to reach Ollama") from exc

        if not isinstance(payload, dict):
            raise ProviderError("Ollama returned a malformed response")
        if self.openai_compatible:
            content = _openai_completion_text(payload)
        else:
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str):
                raise ProviderError("Ollama returned no message content")
        model = payload.get("model")
        return ProviderResponse(_parse_object(content, "Ollama"), model if isinstance(model, str) and model else self.model)


def resolve_provider(settings: AiSettings) -> Provider | None:
    """Build the configured provider, or None when AI is disabled.

    External providers are refused unless the project records explicit consent, so a
    project without consent is local-only: nothing can be sent off the machine.
    """

    if settings.provider == AI_PROVIDER_NONE:
        return None
    provider: Provider
    if settings.provider == AI_PROVIDER_ANTHROPIC:
        provider = AnthropicProvider(settings.model)
    else:
        provider = OllamaProvider(settings.model, settings.ollama_host)
    if provider.external and not settings.external_consent:
        raise ConsentError(
            f"The {provider.name} provider would send the evidence bundle off this machine and no consent is "
            "recorded; this project is local-only. Review the bundle with `repodocs ai-bundle`, then run "
            "`repodocs ai-configure --consent-external` to allow it"
        )
    return provider
