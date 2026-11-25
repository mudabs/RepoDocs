"""Health and readiness checks for the local web service (#76).

Responses contain no credentials, filesystem paths, or job details.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def health_payload() -> dict[str, Any]:
    """Liveness check payload."""

    return {"status": "ok"}


def readiness_status(store_root: Path, *, closed: bool, ui_built: bool) -> tuple[int, dict[str, Any]]:
    """Return HTTP status code and readiness payload."""

    if closed or store_root.is_symlink():
        return 503, {"status": "not-ready", "ui_built": ui_built}
    if store_root.exists():
        ready = store_root.is_dir() and os.access(store_root, os.W_OK)
    else:
        parent = store_root.parent
        ready = not parent.is_symlink() and parent.is_dir() and os.access(parent, os.W_OK)
    if not ready:
        return 503, {"status": "not-ready", "ui_built": ui_built}
    return 200, {"status": "ready", "ui_built": ui_built}
