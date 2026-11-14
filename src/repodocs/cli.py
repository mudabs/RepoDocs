"""The RepoDocs command-line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    CONFIG_FILENAME,
    DEFAULT_OUTPUT_DIRECTORY,
    REPOSITORY_ROLES,
    ROLE_DESTINATION,
    ROLE_SOURCE,
    ProjectConfig,
    RepositorySpec,
    create_project,
    stored_repository_path,
    validate_pinned_revision,
)
from .errors import (
    EXIT_OK,
    EXIT_OWNERSHIP,
    EXIT_SNAPSHOT,
    EXIT_USAGE,
    EXIT_VALIDATION,
    ConfigError,
    OwnershipError,
    RepoDocsError,
    SnapshotError,
    ValidationError,
)
from .generator import generate_docs
from .ir import write_ir
from .scanner import scan_project
from .snapshot import SNAPSHOT_MODE_LOCAL, SNAPSHOT_MODES
from .validation import load_ir, validate_ir, validate_project


OUTPUT_SCHEMA_VERSION = "1.0"


def _emit(
    command: str,
    *,
    ok: bool,
    data: Any = None,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> None:
    payload = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "command": command,
        "ok": ok,
        "data": data,
        "errors": errors or [],
        "warnings": warnings or [],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _root(args: argparse.Namespace) -> Path:
    return Path(args.root).resolve()


def _cmd_init(args: argparse.Namespace) -> int:
    root = _root(args)
    config_path = root / CONFIG_FILENAME
    if config_path.exists():
        raise ConfigError(f"Project configuration already exists: {config_path}")
    config = create_project(root, args.project_name, args.output_directory)
    config.save()
    config.internal_path.mkdir(parents=True, exist_ok=True)
    _emit("init", ok=True, data={"project_id": config.project_id, "config": config_path.as_posix()})
    return EXIT_OK


def _cmd_add(args: argparse.Namespace) -> int:
    root = _root(args)
    config = ProjectConfig.load(root)
    repository_path = Path(args.path)
    if not repository_path.is_absolute():
        repository_path = root / repository_path
    repository_path = repository_path.resolve()
    if not repository_path.exists() or not repository_path.is_dir():
        raise ConfigError(f"Repository path is not a directory: {repository_path}")
    repository_id = args.repository_id or repository_path.name.lower().replace(" ", "-")
    if any(spec.id == repository_id for spec in config.repositories):
        raise ConfigError(f"Repository ID already exists: {repository_id}")
    pinned = (
        validate_pinned_revision(args.pinned_revision, context=f"Repository {repository_id}")
        if args.pinned_revision
        else None
    )
    is_destination = args.destination or args.role == ROLE_DESTINATION
    if is_destination and config.destination_repository_id:
        raise ConfigError(f"A documentation destination is already configured: {config.destination_repository_id}")
    config.repositories.append(
        RepositorySpec(
            id=repository_id,
            path=stored_repository_path(root, repository_path),
            role=args.role,
            pinned_revision=pinned,
        )
    )
    if is_destination:
        config.set_destination(repository_id)
    config.save()
    _emit(
        "add",
        ok=True,
        data={
            "repository_id": repository_id,
            "path": repository_path.as_posix(),
            "role": args.role,
            "destination": is_destination,
            "pinned_revision": pinned,
        },
    )
    return EXIT_OK


def _parse_pins(values: list[str] | None) -> dict[str, str]:
    pins: dict[str, str] = {}
    for value in values or []:
        repository_id, separator, revision = value.partition("=")
        if not separator or not repository_id or not revision:
            raise ConfigError(f"--pin expects REPOSITORY_ID=COMMIT_SHA, got {value!r}")
        pins[repository_id] = revision
    return pins


def _cmd_scan(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    ir, warnings = scan_project(config, mode=args.snapshot_mode, pins=_parse_pins(args.pins))
    validate_ir(ir)
    config.internal_path.mkdir(parents=True, exist_ok=True)
    ir_path = config.internal_path / "ir.json"
    write_ir(ir_path, ir)
    _emit(
        "scan",
        ok=True,
        data={
            "ir": ir_path.as_posix(),
            "snapshot_mode": args.snapshot_mode,
            "repositories": len(ir["snapshots"]),
            "snapshots": [
                {
                    "repository_id": snapshot["repository_id"],
                    "revision_kind": snapshot["revision_kind"],
                    "revision": snapshot["revision"],
                    "working_tree_state": snapshot["working_tree_state"],
                }
                for snapshot in ir["snapshots"]
            ],
            "nodes": len(ir["nodes"]),
            "relationships": len(ir["relationships"]),
            "evidence": len(ir["evidence"]),
            "claims": len(ir["claims"]),
        },
        warnings=warnings,
    )
    return EXIT_OK


def _cmd_generate(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    ir = load_ir(config.internal_path / "ir.json")
    validate_ir(ir)
    result = generate_docs(config, ir)
    _emit(
        "generate",
        ok=True,
        data={
            "changed": result.changed,
            "destination": result.destination,
            "output_directory": config.output_path.as_posix(),
            "artifacts": result.artifacts,
        },
    )
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    artifacts = validate_project(config)
    _emit(
        "validate",
        ok=True,
        data={"destination": config.destination().id, "artifacts": artifacts},
    )
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repodocs", description="Evidence-backed repository documentation")
    parser.add_argument("--root", default=".", help="Project configuration root (default: current directory)")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a RepoDocs project")
    init_parser.add_argument("project_name")
    init_parser.add_argument(
        "--output-directory",
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="Generated output directory, relative to the destination repository",
    )
    init_parser.set_defaults(handler=_cmd_init)

    add_parser = subparsers.add_parser("add", help="Register a source or destination repository")
    add_parser.add_argument("path")
    add_parser.add_argument("--id", dest="repository_id")
    add_parser.add_argument(
        "--role",
        default=ROLE_SOURCE,
        choices=REPOSITORY_ROLES,
        help="source repositories are scanned; a destination-only repository receives generated documentation",
    )
    add_parser.add_argument(
        "--destination",
        action="store_true",
        help="Also use this repository as the generated-documentation destination",
    )
    add_parser.add_argument(
        "--pinned-revision",
        help="Full commit SHA this repository is expected to be checked out at",
    )
    add_parser.set_defaults(handler=_cmd_add)

    scan_parser = subparsers.add_parser("scan", help="Scan registered source repositories into the System IR")
    scan_parser.add_argument(
        "--snapshot-mode",
        default=SNAPSHOT_MODE_LOCAL,
        choices=SNAPSHOT_MODES,
        help="local scans working trees including uncommitted changes; pinned requires clean exact commits",
    )
    scan_parser.add_argument(
        "--pin",
        dest="pins",
        action="append",
        metavar="REPOSITORY_ID=COMMIT_SHA",
        help="Expected commit for a repository in this run; overrides the configured pinned_revision",
    )
    scan_parser.set_defaults(handler=_cmd_scan)

    for name, handler, help_text in (
        ("generate", _cmd_generate, "Generate deterministic documentation into the destination"),
        ("validate", _cmd_validate, "Validate IR and generated artifacts"),
    ):
        command_parser = subparsers.add_parser(name, help=help_text)
        command_parser.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except SnapshotError as exc:
        _emit(args.command, ok=False, errors=[str(exc)])
        return EXIT_SNAPSHOT
    except OwnershipError as exc:
        _emit(args.command, ok=False, errors=[str(exc)])
        return EXIT_OWNERSHIP
    except ValidationError as exc:
        _emit(args.command, ok=False, errors=[str(exc)])
        return EXIT_VALIDATION
    except RepoDocsError as exc:
        _emit(args.command, ok=False, errors=[str(exc)])
        return EXIT_USAGE
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _emit(args.command, ok=False, errors=[f"Unexpected error: {exc}"])
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
