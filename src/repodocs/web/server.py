"""Local web server entrypoint for ``repodocs-web`` (#76).

Pure-Python argument parsing and configuration validation; FastAPI and Uvicorn are imported
only when starting the HTTP server so the core CLI never depends on web packages.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from typing import Sequence

from .cleanup import cleanup_workspaces
from .security import AccessConfig, AccessConfigError
from .workspaces import WorkspaceSettings


def _parse_source(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise ValueError(f"Source must have the form NAME=PATH, got {raw!r}")
    name, path_str = raw.split("=", 1)
    if not name or not path_str:
        raise ValueError(f"Source must have non-empty NAME and PATH, got {raw!r}")
    return name, Path(path_str).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repodocs-web",
        description="Run the local/self-hosted RepoDocs web service.",
    )
    parser.add_argument(
        "--store-root",
        type=Path,
        default=None,
        help="Directory where server-owned workspaces and job records are stored (or set REPODOCS_WEB_STORE).",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Allowlisted local source repository in NAME=PATH form (repeatable).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1). Non-loopback hosts require --public-origin and --auth-token-env.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Listen port (default: 8000).",
    )
    parser.add_argument(
        "--public-origin",
        default=None,
        help="Explicit HTTPS origin (for example https://docs.example.com) when exposing beyond loopback.",
    )
    parser.add_argument(
        "--auth-token-env",
        default=None,
        help="Environment variable name holding the session auth token (minimum 32 characters).",
    )
    parser.add_argument(
        "--frontend-dist",
        type=Path,
        default=None,
        help="Path to the built frontend directory (defaults to frontend/dist or packaged static assets).",
    )
    parser.add_argument(
        "--cleanup-max-age-hours",
        type=float,
        default=None,
        help="Remove inactive marker-owned workspaces older than this many hours on startup.",
    )
    return parser


def build_settings(argv: Sequence[str] | None = None) -> tuple[AccessConfig, WorkspaceSettings, Path | None, float | None]:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    store_raw = args.store_root or os.environ.get("REPODOCS_WEB_STORE")
    if not store_raw:
        raise ValueError("Provide --store-root or set REPODOCS_WEB_STORE")
    store_root = Path(store_raw).expanduser().resolve()
    store_root.mkdir(parents=True, exist_ok=True)

    local_roots: dict[str, Path] = {}
    for item in args.source:
        name, path = _parse_source(item)
        local_roots[name] = path

    auth_token: str | None = None
    if args.auth_token_env:
        auth_token = os.environ.get(args.auth_token_env)
        if not auth_token:
            raise ValueError(f"Environment variable {args.auth_token_env} is unset or empty")

    access = AccessConfig(
        bind_host=args.host,
        port=args.port,
        public_origin=args.public_origin,
        auth_token=auth_token,
    )
    if args.cleanup_max_age_hours is not None:
        if not math.isfinite(args.cleanup_max_age_hours) or args.cleanup_max_age_hours < 0:
            raise ValueError("--cleanup-max-age-hours must be a finite non-negative number")
    workspaces = WorkspaceSettings(store_root=store_root, local_roots=local_roots)
    frontend_dist = args.frontend_dist.expanduser().resolve() if args.frontend_dist else None
    return access, workspaces, frontend_dist, args.cleanup_max_age_hours


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for ``repodocs-web``."""

    try:
        access, workspaces, frontend_dist, cleanup_hours = build_settings(argv)
    except (ValueError, AccessConfigError) as exc:
        print(f"repodocs-web: {exc}", file=sys.stderr)
        return 2

    from .store import STORE_FILENAME, StoreError, WebStore

    if cleanup_hours is not None:
        store_path = workspaces.store_root / STORE_FILENAME
        try:
            store = WebStore(store_path) if store_path.exists() else None
        except StoreError as exc:
            print(f"repodocs-web: {exc}; nothing was cleaned up", file=sys.stderr)
            return 2
        cleanup_workspaces(workspaces.store_root, max_age_seconds=cleanup_hours * 3600.0, store=store)

    try:
        from .app import WebSettings, serve
    except ImportError as exc:
        print(
            'repodocs-web requires optional web dependencies. Install with: pip install "repodocs[web]"',
            file=sys.stderr,
        )
        return 2 if str(exc) else 2

    try:
        serve(WebSettings(access=access, workspaces=workspaces, frontend_dist=frontend_dist))
    except StoreError as exc:
        # A corrupt, newer or locked metadata store fails closed; it is never replaced or silently reset.
        print(f"repodocs-web: {exc}. Restore {STORE_FILENAME} from a backup or move it aside to start empty", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
