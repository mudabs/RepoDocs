"""The RepoDocs command-line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import __version__
from .common import write_json
from .config import (
    AI_PROVIDERS,
    CONFIG_FILENAME,
    DEFAULT_OUTPUT_DIRECTORY,
    REPOSITORY_ROLES,
    ROLE_DESTINATION,
    ROLE_SOURCE,
    AiSettings,
    ProjectConfig,
    RepositorySpec,
    create_project,
    stored_repository_path,
    validate_pinned_revision,
)
from .confirmations import (
    DECISION_CONFIRMED,
    DECISION_REJECTED,
    STATUSES,
    UNRESOLVED_STATUSES,
    apply_decisions,
    confirmations_path,
    load_decisions,
    orphaned_decisions,
    record_decision,
    save_decisions,
    status_counts,
    unresolved_candidates,
)
from .errors import (
    EXIT_AI_PROVIDER,
    EXIT_CONFIRMATION,
    EXIT_OK,
    EXIT_OWNERSHIP,
    EXIT_SNAPSHOT,
    EXIT_USAGE,
    EXIT_VALIDATION,
    ConfigError,
    ConfirmationError,
    OwnershipError,
    ProviderError,
    RepoDocsError,
    SnapshotError,
    ValidationError,
)
from .generator import generate_docs
from .ir import write_ir
from .scanner import scan_project
from .snapshot import SNAPSHOT_MODE_LOCAL, SNAPSHOT_MODES
from .validation import load_ir, validate_candidate_states, validate_ir, validate_project


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


def _cmd_set_destination(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    previous = config.destination_repository_id
    config.set_destination(args.repository_id, replace=args.replace)
    config.save()
    _emit(
        "set-destination",
        ok=True,
        data={
            "repository_id": args.repository_id,
            "previous_repository_id": previous,
            "output_directory": config.output_directory,
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
    apply_decisions(ir, load_decisions(config.root))
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
            "limitations": len(ir["limitations"]),
            "candidates": len(ir["candidates"]),
            "candidate_states": status_counts(ir),
            "analyzers": ir["provenance"]["analyzers"],
        },
        warnings=warnings,
    )
    return EXIT_OK


def _cmd_generate(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    ir = _load_scanned_ir(config)
    # Never render a decision the confirmations file no longer records.
    validate_candidate_states(config, ir)
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
    artifacts, ir = validate_project(config)
    unresolved = unresolved_candidates(ir)
    if unresolved and not args.allow_unresolved:
        shown = ", ".join(item["id"] for item in unresolved[:10])
        raise ConfirmationError(
            f"{len(unresolved)} cross-repository candidate(s) require confirmation or rejection "
            f"(unresolved or stale): {shown}{', ...' if len(unresolved) > 10 else ''}; "
            "list them with `repodocs candidates`"
        )
    _emit(
        "validate",
        ok=True,
        data={
            "destination": config.destination().id,
            "artifacts": artifacts,
            "candidate_states": status_counts(ir),
        },
    )
    return EXIT_OK


def _candidate_summary(candidate: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def side(node_id: str) -> dict[str, Any]:
        node = nodes[node_id]
        return {"id": node_id, "repository_id": node["attributes"].get("repository_id"), "name": node["name"]}

    summary = {
        "id": candidate["id"],
        "status": candidate["status"],
        "confidence": candidate["confidence"],
        "reasons": candidate["reasons"],
        "method": candidate["attributes"]["method"],
        "path": candidate["attributes"]["path"],
        "source": side(candidate["source"]),
        "target": side(candidate["target"]),
        "snapshots": candidate["snapshots"],
    }
    if "confirmation" in candidate:
        summary["confirmation"] = candidate["confirmation"]
    return summary


def _load_scanned_ir(config: ProjectConfig) -> dict[str, Any]:
    ir = load_ir(config.internal_path / "ir.json")
    validate_ir(ir)
    return ir


def _cmd_candidates(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    ir = _load_scanned_ir(config)
    validate_candidate_states(config, ir)
    wanted = UNRESOLVED_STATUSES if args.status == "unresolved" else STATUSES if args.status == "all" else (args.status,)
    nodes = {node["id"]: node for node in ir["nodes"]}
    _emit(
        "candidates",
        ok=True,
        data={
            "status": args.status,
            "candidate_states": status_counts(ir),
            "candidates": [_candidate_summary(item, nodes) for item in ir["candidates"] if item["status"] in wanted],
            "orphaned_decisions": orphaned_decisions(ir, load_decisions(config.root)),
        },
    )
    return EXIT_OK


def _cmd_decide(args: argparse.Namespace) -> int:
    """Record confirm or reject decisions for candidates in the current scan, all or nothing."""

    config = ProjectConfig.load(_root(args))
    ir = _load_scanned_ir(config)
    decisions = load_decisions(config.root)
    # Start from the states the current file produces, in case it was edited since the scan.
    apply_decisions(ir, decisions)
    decision = DECISION_CONFIRMED if args.command == "confirm" else DECISION_REJECTED
    for candidate_id in args.candidate_ids:
        record_decision(ir, decisions, candidate_id, decision, args.note)
    save_decisions(config.root, decisions)
    apply_decisions(ir, decisions)
    write_ir(config.internal_path / "ir.json", ir)
    _emit(
        args.command,
        ok=True,
        data={
            "decision": decision,
            "candidate_ids": sorted(set(args.candidate_ids)),
            "confirmations": confirmations_path(config.root).as_posix(),
            "candidate_states": status_counts(ir),
        },
    )
    return EXIT_OK


def _cmd_ai_configure(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    current = config.ai
    consent = True if args.consent_external else False if args.revoke_consent else current.external_consent
    config.ai = AiSettings(
        provider=args.provider or current.provider,
        model=args.model or (current.model if not args.provider or args.provider == current.provider else None),
        external_consent=consent,
        ollama_host=args.ollama_host or current.ollama_host,
    )
    config.save()
    _emit("ai-configure", ok=True, data=config.ai.to_dict())
    return EXIT_OK


def _write_bundle(config: ProjectConfig) -> tuple[dict[str, Any], Path]:
    from .ai.bundle import build_bundle
    from .ai.claims import BUNDLE_FILENAME

    ir = _load_scanned_ir(config)
    validate_candidate_states(config, ir)
    bundle = build_bundle(ir)
    path = config.internal_path / BUNDLE_FILENAME
    write_json(path, bundle)
    return bundle, path


def _bundle_summary(bundle: dict[str, Any], path: Path) -> dict[str, Any]:
    from .ai.bundle import bundle_hash

    return {
        "bundle": path.as_posix(),
        "bundle_version": bundle["bundle_version"],
        "bundle_hash": bundle_hash(bundle),
        "source_ir_hash": bundle["source_ir_hash"],
        "redactions": bundle["redaction"]["replacements"],
        **{key: len(bundle[key]) for key in ("nodes", "relationships", "candidates", "limitations", "evidence")},
    }


def _cmd_ai_bundle(args: argparse.Namespace) -> int:
    """Write the evidence bundle for review. Never contacts a provider."""

    config = ProjectConfig.load(_root(args))
    bundle, path = _write_bundle(config)
    _emit("ai-bundle", ok=True, data=_bundle_summary(bundle, path))
    return EXIT_OK


def _cmd_ai_interpret(args: argparse.Namespace) -> int:
    from .ai import providers
    from .ai.claims import CLAIMS_FILENAME, interpret

    config = ProjectConfig.load(_root(args))
    # Resolve first: a missing consent or setting must fail before anything is built or sent.
    provider = providers.resolve_provider(config.ai)
    if provider is None:
        _emit("ai-interpret", ok=True, data={"status": "disabled", "provider": "none"})
        return EXIT_OK
    bundle, bundle_path = _write_bundle(config)
    document = interpret(bundle, provider)
    claims_path = config.internal_path / CLAIMS_FILENAME
    write_json(claims_path, document)
    _emit(
        "ai-interpret",
        ok=True,
        data={
            "status": "completed",
            "claims": claims_path.as_posix(),
            "provenance": document["provenance"],
            "accepted": len(document["accepted"]),
            "rejected": len(document["rejected"]),
            "bundle": _bundle_summary(bundle, bundle_path),
        },
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

    destination_parser = subparsers.add_parser(
        "set-destination", help="Make an already registered repository the generated-documentation destination"
    )
    destination_parser.add_argument("repository_id")
    destination_parser.add_argument(
        "--replace",
        action="store_true",
        help="Switch away from a previously configured destination; remove its generated output first",
    )
    destination_parser.set_defaults(handler=_cmd_set_destination)

    scan_parser = subparsers.add_parser("scan", help="Scan registered source repositories into the System IR")
    scan_parser.add_argument(
        "--snapshot-mode",
        default=SNAPSHOT_MODE_LOCAL,
        choices=SNAPSHOT_MODES,
        help="local scans working trees including uncommitted changes; pinned requires clean, explicitly pinned commits",
    )
    scan_parser.add_argument(
        "--pin",
        dest="pins",
        action="append",
        metavar="REPOSITORY_ID=COMMIT_SHA",
        help="Expected commit for a repository in this run; overrides the configured pinned_revision",
    )
    scan_parser.set_defaults(handler=_cmd_scan)

    generate_parser = subparsers.add_parser(
        "generate", help="Generate deterministic documentation into the destination"
    )
    generate_parser.set_defaults(handler=_cmd_generate)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate IR, generated artifacts, and required confirmations"
    )
    validate_parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="Do not fail with exit code 5 while cross-repository candidates are unresolved or stale",
    )
    validate_parser.set_defaults(handler=_cmd_validate)

    candidates_parser = subparsers.add_parser("candidates", help="List cross-repository candidates from the last scan")
    candidates_parser.add_argument(
        "--status",
        default="unresolved",
        choices=("unresolved", *STATUSES, "all"),
        help="unresolved (default) lists candidates without a decision and stale decisions",
    )
    candidates_parser.set_defaults(handler=_cmd_candidates)

    for name, help_text in (
        ("confirm", "Confirm cross-repository candidates as authoritative links"),
        ("reject", "Reject cross-repository candidates"),
    ):
        decide_parser = subparsers.add_parser(name, help=help_text)
        decide_parser.add_argument("candidate_ids", nargs="+", metavar="CANDIDATE_ID")
        decide_parser.add_argument("--note", help="Reason recorded with the decision")
        decide_parser.set_defaults(handler=_cmd_decide)

    ai_configure = subparsers.add_parser("ai-configure", help="Choose the optional AI provider and record consent")
    ai_configure.add_argument("--provider", choices=AI_PROVIDERS)
    ai_configure.add_argument("--model")
    ai_configure.add_argument("--ollama-host", help="Ollama server URL (default http://localhost:11434)")
    consent = ai_configure.add_mutually_exclusive_group()
    consent.add_argument(
        "--consent-external",
        action="store_true",
        help="Consent to sending the redacted evidence bundle off this machine",
    )
    consent.add_argument("--revoke-consent", action="store_true", help="Return the project to local-only")
    ai_configure.set_defaults(handler=_cmd_ai_configure)

    ai_bundle = subparsers.add_parser("ai-bundle", help="Write the redacted evidence bundle for review; sends nothing")
    ai_bundle.set_defaults(handler=_cmd_ai_bundle)

    ai_interpret = subparsers.add_parser(
        "ai-interpret", help="Ask the configured provider for claims and validate them against the evidence"
    )
    ai_interpret.set_defaults(handler=_cmd_ai_interpret)
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
    except ConfirmationError as exc:
        _emit(args.command, ok=False, errors=[str(exc)])
        return EXIT_CONFIRMATION
    except ProviderError as exc:
        _emit(args.command, ok=False, errors=[f"{exc}. Deterministic scan, generate, and validate are unaffected"])
        return EXIT_AI_PROVIDER
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
