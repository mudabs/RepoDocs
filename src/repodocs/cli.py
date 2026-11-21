"""The RepoDocs command-line interface."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__, service
from .common import write_json
from .config import (
    AI_PROVIDERS,
    CONFIG_FILENAME,
    DEFAULT_OUTPUT_DIRECTORY,
    REPOSITORY_ROLES,
    ROLE_DESTINATION,
    ROLE_SOURCE,
    SOURCE_POLICY_VERSION,
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
)
from .errors import EXIT_OK, EXIT_USAGE, ConfigError, RepoDocsError
from .ir import write_ir
from .service import load_scanned_ir, require_resolved_candidates
from .snapshot import SNAPSHOT_MODE_LOCAL, SNAPSHOT_MODES
from .validation import validate_candidate_states, validate_project


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


def _emit_result(command: str, result: service.WorkflowResult) -> None:
    _emit(command, ok=True, data=result.data, warnings=list(result.warnings))


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


def _parse_pins_json(value: str | None) -> dict[str, str]:
    """Pins as a JSON object of repository ID to commit SHA, for workflows that resolve them first."""

    if not value:
        return {}
    try:
        data = json.loads(value)
    except ValueError as exc:
        raise ConfigError(f"--pins-json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in data.items()):
        raise ConfigError("--pins-json must be a JSON object mapping repository IDs to commit SHAs")
    return data


def _parse_pins(values: list[str] | None) -> dict[str, str]:
    pins: dict[str, str] = {}
    for value in values or []:
        repository_id, separator, revision = value.partition("=")
        if not separator or not repository_id or not revision:
            raise ConfigError(f"--pin expects REPOSITORY_ID=COMMIT_SHA, got {value!r}")
        pins[repository_id] = revision
    return pins


def _cmd_scan(args: argparse.Namespace) -> int:
    pins = _parse_pins(args.pins)
    for repository_id, revision in _parse_pins_json(args.pins_json).items():
        if pins.setdefault(repository_id, revision) != revision:
            raise ConfigError(f"Conflicting pins for {repository_id}: --pin and --pins-json disagree")
    _emit_result("scan", service.scan(service.ScanRequest(_root(args), snapshot_mode=args.snapshot_mode, pins=pins)))
    return EXIT_OK


def _cmd_generate(args: argparse.Namespace) -> int:
    _emit_result("generate", service.generate(service.GenerateRequest(_root(args), retire_architecture=args.retire_architecture)))
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    _emit_result("validate", service.validate(service.ValidateRequest(_root(args), allow_unresolved=args.allow_unresolved)))
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


def _cmd_candidates(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    ir = load_scanned_ir(config)
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
    ir = load_scanned_ir(config)
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


def _cmd_publish(args: argparse.Namespace) -> int:
    """Publish generated documentation as a reviewable pull request, or do nothing when unchanged."""

    from .publish import GhPullRequests, publish

    config = ProjectConfig.load(_root(args))
    # Never publish output that has not just been validated, with confirmations resolved.
    _, ir = validate_project(config)
    require_resolved_candidates(ir)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    pull_requests = None
    if not args.dry_run:
        if not token:
            raise ConfigError(
                "Publishing needs a token with contents and pull request write access to the destination "
                "repository in GH_TOKEN; use --dry-run to check without publishing"
            )
        pull_requests = GhPullRequests(config.destination_root(), token)
    result = publish(
        config,
        ir,
        base=args.base,
        branch=args.branch,
        remote=args.remote,
        dry_run=args.dry_run,
        token=token,
        pull_requests=pull_requests,
    )
    _emit("publish", ok=True, data=result)
    return EXIT_OK


def _cmd_ai_configure(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(_root(args))
    current = config.ai
    consent = True if args.consent_external else False if args.revoke_consent else current.external_consent
    source = True if args.consent_source_excerpts else False if args.revoke_source_consent else current.source_excerpts_consent
    config.ai = AiSettings(
        source_excerpts_consent=source,
        source_consent_policy=SOURCE_POLICY_VERSION if source else None,
        provider=args.provider or current.provider,
        model=args.model or (current.model if not args.provider or args.provider == current.provider else None),
        external_consent=consent,
        ollama_host=args.ollama_host or current.ollama_host,
    )
    config.save()
    _emit("ai-configure", ok=True, data=config.ai.to_dict())
    return EXIT_OK


def _cmd_architecture_context(args: argparse.Namespace) -> int:
    """Write the bounded, redacted source context for review. Never contacts a provider."""

    from .ai import providers

    config = ProjectConfig.load(_root(args))
    provider = None
    if config.ai.provider != "none":
        try:
            provider = providers.resolve_provider(replace(config.ai, external_consent=True))
        except RepoDocsError:
            provider = None
    _emit_result("architecture-context", service.preview_context(service.ContextRequest(_root(args), provider=provider)))
    return EXIT_OK


def _cmd_architecture_discover(args: argparse.Namespace) -> int:
    """Run bounded architecture discovery and store the validated view. Contacts the provider only here."""

    from .ai import providers

    request = service.DiscoverRequest(_root(args), refresh=args.refresh)
    if not service.can_reuse_view(request):
        # Consent and settings are checked before any context is sent.
        provider = providers.resolve_discovery_provider(ProjectConfig.load(_root(args)).ai)
        request = replace(request, provider=provider)
    _emit_result("architecture-discover", service.discover(request))
    return EXIT_OK


def _write_bundle(config: ProjectConfig) -> tuple[dict[str, Any], Path]:
    from .ai.bundle import build_bundle
    from .ai.claims import BUNDLE_FILENAME

    ir = load_scanned_ir(config)
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
    from .ai.evaluation import contribution

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
            "contribution": contribution(load_scanned_ir(config), document),
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
    scan_parser.add_argument(
        "--pins-json",
        metavar="JSON",
        help='Expected commits as a JSON object, for example \'{"frontend": "<sha>"}\'; merged with --pin',
    )
    scan_parser.set_defaults(handler=_cmd_scan)

    generate_parser = subparsers.add_parser(
        "generate", help="Generate deterministic documentation into the destination"
    )
    generate_parser.add_argument(
        "--retire-architecture",
        action="store_true",
        help="Remove the generated architecture-view files (owned and unedited only) instead of keeping or rendering them",
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

    publish_parser = subparsers.add_parser(
        "publish", help="Open or update a reviewable documentation pull request; does nothing when output is unchanged"
    )
    publish_parser.add_argument("--base", required=True, help="Branch the pull request targets, normally the default branch")
    publish_parser.add_argument("--branch", default="repodocs/docs-update", help="Bot-owned branch holding the documentation commit")
    publish_parser.add_argument("--remote", default="origin")
    publish_parser.add_argument("--dry-run", action="store_true", help="Compute the change without contacting the remote")
    publish_parser.set_defaults(handler=_cmd_publish)

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
    source_consent = ai_configure.add_mutually_exclusive_group()
    source_consent.add_argument(
        "--consent-source-excerpts",
        action="store_true",
        help="Consent to sending bounded, redacted source excerpts to an external provider for architecture discovery",
    )
    source_consent.add_argument("--revoke-source-consent", action="store_true", help="Withdraw consent for source excerpts")
    ai_configure.set_defaults(handler=_cmd_ai_configure)

    ai_bundle = subparsers.add_parser("ai-bundle", help="Write the redacted evidence bundle for review; sends nothing")
    ai_bundle.set_defaults(handler=_cmd_ai_bundle)

    ai_interpret = subparsers.add_parser(
        "ai-interpret", help="Ask the configured provider for claims and validate them against the evidence"
    )
    ai_interpret.set_defaults(handler=_cmd_ai_interpret)

    architecture = subparsers.add_parser("architecture", help="Architecture discovery (ADR-026)")
    architecture_commands = architecture.add_subparsers(dest="architecture_command", required=True)
    architecture_context = architecture_commands.add_parser(
        "context", help="Write the bounded, redacted source context for review; sends nothing"
    )
    architecture_context.set_defaults(handler=_cmd_architecture_context)
    architecture_discover = architecture_commands.add_parser(
        "discover", help="Ask the configured provider for an architecture view and validate it; reuses a current stored view"
    )
    architecture_discover.add_argument("--refresh", action="store_true", help="Run new inference even when a current view is stored")
    architecture_discover.set_defaults(handler=_cmd_architecture_discover)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except RepoDocsError as exc:
        error = service.describe_error(exc)
        _emit(args.command, ok=False, errors=[error.message])
        return error.exit_code
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _emit(args.command, ok=False, errors=[f"Unexpected error: {exc}"])
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
