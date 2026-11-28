"""The models the browser may offer in provider setup (#77 walkthrough feedback).

Three sources, all chosen by the operator's machine, never by the browser: models the operator lists with
``--model PROVIDER:NAME``, the model of the operator's price profile, and models installed in the local Ollama.
Listing never contacts a paid provider, sends no source and needs no credential. A browser can still type any model
name; the list is a convenience, not an allowlist.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable

from ..ai.providers import DEFAULT_OLLAMA_HOST

PROVIDERS = ("ollama", "openai")
OLLAMA_LIST_TIMEOUT_SECONDS = 2.0
MAX_LISTED = 50


def installed_ollama_models(host: str = DEFAULT_OLLAMA_HOST, timeout: float = OLLAMA_LIST_TIMEOUT_SECONDS) -> list[str] | None:
    """Names of the models installed in the local Ollama, or None when it cannot be reached.

    Only the default loopback host is asked; a response that is not the expected shape lists nothing.
    """

    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=timeout) as response:  # noqa: S310 - fixed loopback URL
            data = json.loads(response.read(256 * 1024).decode("utf-8"))
    except (OSError, ValueError):
        return None
    names = [item.get("name") for item in data.get("models", []) if isinstance(item, dict)] if isinstance(data, dict) else []
    return sorted({name for name in names if isinstance(name, str) and 0 < len(name) <= 200})[:MAX_LISTED]


def model_catalog(
    operator_models: tuple[tuple[str, str], ...], price_profile: Any, ollama: Callable[[], list[str] | None],
) -> dict[str, Any]:
    """Offered models per provider with where each came from, and whether the local Ollama answered."""

    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str, source: str) -> None:
        if provider in PROVIDERS and model and (provider, model) not in seen:
            seen.add((provider, model))
            entries.append({"provider": provider, "model": model, "source": source})

    for provider, model in operator_models:
        add(provider, model, "operator")
    if price_profile is not None:
        add(price_profile.provider, price_profile.model, "price-profile")
    installed = ollama()
    for model in installed or []:
        add("ollama", model, "installed")
    return {"models": entries, "ollama_reachable": installed is not None}


def parse_model_option(value: str) -> tuple[str, str]:
    """``PROVIDER:NAME`` from ``--model``; the name may itself contain colons (``qwen2.5-coder:7b``)."""

    provider, sep, model = value.partition(":")
    provider, model = provider.strip().lower(), model.strip()
    if not sep or provider not in PROVIDERS or not model or len(model) > 200:
        raise ValueError(f"--model must be PROVIDER:NAME with provider one of {', '.join(PROVIDERS)}, got {value!r}")
    return provider, model
