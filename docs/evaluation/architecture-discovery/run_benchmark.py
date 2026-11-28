"""Run the #54 benchmark protocol on one pinned subject and write a JSON summary.

Deterministic mode (the default) needs no key, no network and no model. With ``--provider`` it also runs live
architecture discovery, which needs the separate source-excerpt consent recorded by ``ai-configure`` and, for an
external provider, an approved spend cap. Nothing in a subject repository is executed.

    python docs/evaluation/architecture-discovery/run_benchmark.py --subject flaskr --source <flask checkout> --work <dir>
    python docs/evaluation/architecture-discovery/run_benchmark.py --subject flaskr --source <flask checkout> --work <dir> \
        --provider ollama --model qwen2.5-coder:7b --ai-settings limits.json

The source checkout must be clean at the revision named in ``expected/<subject>.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SUBDIR = {"flaskr": "examples/tutorial", "digitalbonebox": ""}


def remove(path: Path) -> None:
    """Delete a scratch tree, including git's read-only object files on Windows."""

    def writable(function: Any, target: str, _: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if path.exists():
        shutil.rmtree(path, onerror=writable)


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> tuple[int, str, float]:
    started = time.perf_counter()
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False)
    elapsed = round(time.perf_counter() - started, 3)
    if check and done.returncode != 0:
        raise SystemExit(f"{' '.join(args)} exited {done.returncode}\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return done.returncode, done.stdout + done.stderr, elapsed


def repodocs(root: Path, *args: str, check: bool = True) -> tuple[int, str, float]:
    return run([sys.executable, "-m", "repodocs.cli", "--root", str(root), *args], check=check)


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args])[1].strip()


def tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and ".git" not in p.parts):
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def project(work: Path, name: str, source: Path) -> tuple[Path, Path, dict[str, float]]:
    root, docs = work / f"{name}-project", work / f"{name}-docs"
    for path in (root, docs):
        remove(path)
    docs.mkdir(parents=True)
    git(docs, "init", "-q", "-b", "main")
    (docs / "README.md").write_text("docs\n", encoding="utf-8")
    git(docs, "add", "-A")
    git(docs, "-c", "user.name=eval", "-c", "user.email=eval@example.invalid", "commit", "-q", "-m", "init")
    root.mkdir()
    repodocs(root, "init", name)
    repodocs(root, "add", str(source), "--id", name)
    repodocs(root, "add", str(docs), "--id", "docs", "--role", "destination")
    times = {"scan_s": repodocs(root, "scan")[2], "generate_s": repodocs(root, "generate")[2]}
    return root, docs, times


def generated(docs: Path) -> dict[str, str]:
    out = docs / "docs" / "generated"
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*")) if p.is_file()}


def snapshot_modes(scan: Any) -> Any:
    """The snapshot state the scan reports for each source, whatever shape the report has."""

    found: list[Any] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "working_tree_state" in value and "revision_kind" in value:
                found.append({k: value[k] for k in ("repository_id", "revision", "revision_kind", "working_tree_state") if k in value})
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(scan)
    return found


def change_checks(work: Path, name: str, checkout: Path, source: Path) -> dict[str, Any]:
    """Dirty tree, committed change and manual edit, on a throwaway copy of the checkout."""

    copy = work / "change" / "source"
    remove(copy.parent)
    copy.parent.mkdir(parents=True)
    git(checkout, "worktree", "prune")
    run(["git", "clone", "-q", "--no-hardlinks", str(checkout), str(copy)])
    target = copy / source.relative_to(checkout) if source != checkout else copy
    root, docs, _ = project(work / "change", name, target)
    clean = generated(docs)
    tracked = [line for line in git(copy, "ls-files").splitlines() if line.startswith(target.relative_to(copy).as_posix().strip(".") or "")]
    edited = next(path for path in sorted(tracked) if path.endswith((".py", ".js")))
    with (copy / edited).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n# benchmark edit\n")
    result: dict[str, Any] = {"edited_file": edited}
    scan = json.loads(repodocs(root, "scan")[1])
    code, out, _ = repodocs(root, "generate", check=False)
    result["dirty"] = {"generate_exit": code, "snapshot": snapshot_modes(scan), "output_changed": generated(docs) != clean}
    git(copy, "-c", "user.name=eval", "-c", "user.email=eval@example.invalid", "commit", "-q", "-am", "benchmark change")
    scan = json.loads(repodocs(root, "scan")[1])
    code, out, _ = repodocs(root, "generate", check=False)
    committed = generated(docs)
    result["committed"] = {"generate_exit": code, "snapshot": snapshot_modes(scan), "output_changed": committed != clean,
                           "new_revision": git(copy, "rev-parse", "HEAD")}
    code, out, _ = repodocs(root, "generate", check=False)
    result["noop_after_change"] = generated(docs) == committed
    manual = docs / "docs" / "generated" / "architecture.md"
    manual.write_text(manual.read_text(encoding="utf-8") + "\nA person wrote this line.\n", encoding="utf-8")
    code, out, _ = repodocs(root, "generate", check=False)
    result["manual_edit"] = {"generate_exit": code, "edit_kept": "A person wrote this line." in manual.read_text(encoding="utf-8")}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject", required=True, choices=sorted(SUBDIR))
    parser.add_argument("--source", required=True, type=Path, help="clean checkout of the subject repository")
    parser.add_argument("--work", required=True, type=Path, help="scratch directory; its subject folders are replaced")
    parser.add_argument("--provider", help="run live discovery with this provider (ollama, anthropic, openai)")
    parser.add_argument("--model")
    parser.add_argument("--consent-external", action="store_true",
                        help="record consent to send redacted source excerpts off this machine (required for any non-local provider)")
    parser.add_argument("--ai-settings", type=Path, help="JSON merged into the project's ai settings, e.g. limits and prices")
    parser.add_argument("--ai-args", nargs=argparse.REMAINDER, default=[], help="extra ai-configure arguments, e.g. --ollama-host URL")
    options = parser.parse_args()

    expected_path = HERE / "expected" / f"{options.subject}.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    checkout = options.source.resolve()
    revision = git(checkout, "rev-parse", "HEAD")
    if revision != expected["source"]["revision"] or git(checkout, "status", "--porcelain"):
        raise SystemExit(f"{checkout} must be clean at {expected['source']['revision']}; it is at {revision}")
    source = checkout / SUBDIR[options.subject] if SUBDIR[options.subject] else checkout
    work = options.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"subject": options.subject, "source_revision": revision, "engine_revision": git(HERE, "rev-parse", "HEAD"),
                              "engine_src_dirty": bool(git(HERE, "status", "--porcelain", "--", "../../../src"))}

    # Deterministic run, then the same run again from nothing: the outputs must be byte-identical.
    root, docs, times = project(work, options.subject, source)
    first = tree_digest(docs)
    report["deterministic"] = {"times": times, "validate_exit": repodocs(root, "validate", check=False)[0]}
    report["deterministic"]["second_generate_output"] = repodocs(root, "generate")[1].strip()[-300:]
    report["deterministic"]["second_generate_unchanged"] = tree_digest(docs) == first
    root2, docs2, _ = project(work / "repeat", options.subject, source)
    report["deterministic"]["fresh_rerun_identical"] = tree_digest(docs2) == first
    report["deterministic"]["change_checks"] = change_checks(work, options.subject, checkout, source)
    report["deterministic"]["generated_files"] = sorted(p.relative_to(docs).as_posix() for p in docs.rglob("*") if p.is_file() and ".git" not in p.parts)

    if options.provider:
        configure = ["ai-configure", "--provider", options.provider]
        if options.model:
            configure += ["--model", options.model]
        if options.consent_external:
            configure += ["--consent-external", "--consent-source-excerpts"]
        repodocs(root, *configure, *options.ai_args)
        if options.ai_settings:
            config_path = root / "repodocs.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.setdefault("ai", {}).update(json.loads(options.ai_settings.read_text(encoding="utf-8")))
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            report["ai_settings"] = config["ai"]
        repodocs(root, "architecture", "context")
        code, out, elapsed = repodocs(root, "architecture", "discover", check=False)
        report["live"] = {"provider": options.provider, "model": options.model, "discover_exit": code, "discover_s": elapsed,
                          "discover_tail": out[-1500:]}
        repodocs(root, "generate", check=False)
        report["live"]["validate_exit"] = repodocs(root, "validate", check=False)[0]
        code, out, _ = run([sys.executable, "-m", "repodocs.architecture.evaluation", "--root", str(root), "--expected", str(expected_path)],
                           check=False)
        report["live"]["evaluation"] = json.loads(out) if code == 0 else {"exit": code, "output": out[-1500:]}

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
