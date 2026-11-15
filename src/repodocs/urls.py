"""Safe literal URL handling shared by extraction and correlation.

Only HTTP(S) URLs and relative references are supported. Query strings and
fragments are deliberately omitted in their entirety, not filtered by a
secret-name denylist. Credentials never become part of persisted URL identities.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def safe_url(value: str) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme and parts.scheme.lower() not in ("http", "https"):
            return None
        if parts.netloc:
            host = parts.hostname
            if not host or "{" in host or "}" in host:
                return None
            host = host.lower()
            if ":" in host:
                host = f"[{host}]"
            port = parts.port
            if port is not None and (parts.scheme.lower(), port) not in (("https", 443), ("http", 80)):
                host += f":{port}"
            return urlunsplit((parts.scheme.lower(), host, parts.path or "/", "", ""))
        if parts.scheme:
            return None
        return urlunsplit(("", "", parts.path, "", ""))
    except ValueError:
        return None


def url_target(url: str, base: str | None = None) -> tuple[str | None, str] | None:
    """A sanitized origin/path pair. Axios-style base paths are concatenated."""

    clean = safe_url(url)
    if clean is None:
        return None
    parts = urlsplit(clean)
    if parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, "", "", "")), parts.path or "/"
    if base:
        clean_base = safe_url(base)
        if clean_base is None:
            return None
        base_parts = urlsplit(clean_base)
        origin = urlunsplit((base_parts.scheme, base_parts.netloc, "", "", "")) if base_parts.netloc else None
        return origin, base_parts.path.rstrip("/") + "/" + parts.path.lstrip("/")
    return None, "/" + parts.path.lstrip("/")
