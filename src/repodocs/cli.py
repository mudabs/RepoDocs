"""The RepoDocs command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import (
    CONFIG_FILENAME,
    DEFAULT_OUTPUT_DIRECTORY,
    ProjectConfig,
    RepositorySpec,
    create_project,
    stored_repository_path,
)
from .errors import (
    EXIT_OK,
    EXIT_OWNERSHIP,
    EXIT_USAGE,
    EXIT_VALIDATION,
    ConfigError,
    OwnershipError,
    RepoDocsError,
    ValidationError,
)
from .generator import generate_docs
from .ir import write_ir
from .scanner import scan_project
from .validation import validate_project


OUTPUT_SCHEMA_VERSION = "1.0"


def _emit(command: str, *, ok: bool, data: Any = None, errors: list[str] | None = None, warnings: list[str] | None = None) -> None:
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
    _emit(
        "init",
        ok=True,
        data={"project_id": config.project_id, "config": config_path.as_posix()},
    )
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
    config.repositories.append(
        RepositorySpec(
            id=repository_id,
            path=stored_repository_path(root, repository_path),
            role=args.role,
        )
    )
    config.save()
    _emit(
        "add",
        ok=True,
        data={"repository_id": repository_id, "path": repository_path.as_posix()},
    )
    return EXIT_OK


def _cmd_scan(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    ir, warnings = scan_project(config)
    config.internal_path.mkdir(parents=True, exist_ok=True)
    write_ir(config.internal_path / "ir.json", ir)
    _emit(
        "scan",
        ok=True,
        data={
            "ir": (config.internal_path / "ir.json").as_posix(),
            "repositories": len(ir["snapshots"]),
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
    from .validation import load_ir

    ir = load_ir(config.internal_path / "ir.json")
    result = generate_docs(config, ir)
    _emit(
        "generate",
        ok=True,
        data={
            "changed": result.changed,
            "output_directory": config.output_path.as_posix(),
            "artifacts": result.artifacts,
        },
    )
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    artifacts = validate_project(config)
    _emit("validate", ok=True, data={"artifacts": artifacts})
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repodocs", description="Evidence-backed repository documentation")
    parser.add_argument("--root", default=".", help="Project configuration root (default: current directory)")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a RepoDocs project")
    init_parser.add_argument("project_name")
    init_parser.add_argument("--output-directory", default=DEFAULT_OUTPUT_DIRECTORY)
    init_parser.set_defaults(handler=_cmd_init)

    add_parser = subparsers.add_parser("add", help="Register a repository")
    add_parser.add_argument("path")
    add_parser.add_argument("--id", dest="repository_id")
    add_parser.add_argument("--role", default="source")
    add_parser.set_defaults(handler=_cmd_add)

    for name, handler, help_text in (
        ("scan", _cmd_scan, "Scan registered repositories into the System IR"),
        ("generate", _cmd_generate, "Generate deterministic documentation"),
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
