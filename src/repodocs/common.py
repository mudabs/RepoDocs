"""Small shared utilities with deterministic serialization."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize JSON in a stable, human-readable form."""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value), encoding="utf-8", newline="\n")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "project"


def semantic_id(kind: str, *parts: str) -> str:
    normalized = "|".join(part.strip().replace("\\", "/").lower() for part in parts)
    return f"{kind}:{sha256_text(normalized)[:16]}"
