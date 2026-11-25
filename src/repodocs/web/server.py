"""Local web server entrypoint for ``repodocs-web`` (#76).

Pure-Python argument parsing and configuration validation; FastAPI and Uvicorn are imported
only when starting the HTTP server so the core CLI never depends on web packages.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from ..ai.budgets import PriceProfile, RunLimits
from ..errors import ConfigError
from .cleanup import cleanup_workspaces
from .jobs import JobSettings
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
    parser.add_argument(
        "--context-window-tokens",
        type=int,
        default=None,
        help="Operator-configured per-request context window in tokens for architecture runs.",
    )
    parser.add_argument(
        "--request-output-tokens",
        type=int,
        default=None,
        help="Operator-configured per-request output token reserve/cap for architecture runs.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Maximum generation requests per architecture run (ceiling 6).",
    )
    parser.add_argument(
        "--max-input-tokens",
        type=int,
        default=None,
        help="Aggregate input token cap per architecture run.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Aggregate output token cap per architecture run.",
    )
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        default=None,
        help="Aggregate total (input + output) token cap per architecture run.",
    )
    parser.add_argument(
        "--max-cost-usd",
        default=None,
        help="Aggregate USD cost cap per architecture run (requires a complete --prices-file).",
    )
    parser.add_argument(
        "--prices-file",
        type=Path,
        default=None,
        help="Path to a versioned JSON price profile matching ai.prices for one provider/model.",
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


def build_job_settings(argv: Sequence[str] | None = None) -> JobSettings:
    """Parse and validate operator model limits and pricing settings for ``JobService``."""

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    max_cost: Decimal | None = None
    if args.max_cost_usd is not None:
        try:
            max_cost = Decimal(str(args.max_cost_usd))
        except InvalidOperation as exc:
            raise ValueError(f"--max-cost-usd must be a decimal number, got {args.max_cost_usd!r}") from exc
    try:
        run_limits = RunLimits(
            context_window_tokens=args.context_window_tokens,
            request_output_tokens=args.request_output_tokens,
            max_requests=args.max_requests,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            max_total_tokens=args.max_total_tokens,
            max_cost_usd=max_cost,
        )
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    if (
        run_limits.context_window_tokens is not None
        and run_limits.request_output_tokens is not None
        and run_limits.request_output_tokens >= run_limits.context_window_tokens
    ):
        raise ValueError(
            f"--request-output-tokens ({run_limits.request_output_tokens}) must be smaller than "
            f"--context-window-tokens ({run_limits.context_window_tokens})"
        )

    prices: PriceProfile | None = None
    if args.prices_file is not None:
        prices_path = args.prices_file.expanduser().resolve()
        try:
            raw = json.loads(prices_path.read_text(encoding="utf-8"))
            prices = PriceProfile.from_dict(raw)
        except (OSError, ValueError, ConfigError) as exc:
            raise ValueError(f"Invalid --prices-file: {exc}") from exc

    return JobSettings(run_limits=run_limits, prices=prices)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for ``repodocs-web``."""

    try:
        access, workspaces, frontend_dist, cleanup_hours = build_settings(argv)
        job_settings = build_job_settings(argv)
    except (ValueError, AccessConfigError) as exc:
        print(f"repodocs-web: {exc}", file=sys.stderr)
        return 2

    if cleanup_hours is not None:
        from .store import STORE_FILENAME, WebStore

        store_path = workspaces.store_root / STORE_FILENAME
        store = WebStore(store_path) if store_path.exists() else None
        cleanup_workspaces(workspaces.store_root, max_age_seconds=cleanup_hours * 3600.0, store=store)

    try:
        from .app import WebSettings, serve
    except ImportError as exc:
        print(
            'repodocs-web requires optional web dependencies. Install with: pip install "repodocs[web]"',
            file=sys.stderr,
        )
        return 2 if str(exc) else 2

    serve(WebSettings(access=access, workspaces=workspaces, jobs=job_settings, frontend_dist=frontend_dist))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
