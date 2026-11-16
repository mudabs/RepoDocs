"""Build a local coordination repository for the publication self-test.

Prints `revisions=` and `wrong_revisions=` lines for $GITHUB_OUTPUT. Standard library only,
so it needs neither RepoDocs nor network access.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "basic-repo"
ENVIRONMENT = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Self test",
    "GIT_AUTHOR_EMAIL": "selftest@example.invalid",
    "GIT_COMMITTER_NAME": "Self test",
    "GIT_COMMITTER_EMAIL": "selftest@example.invalid",
}
CONFIG = {
    "schema_version": "1.0",
    "project": {"id": "selftest", "name": "Self test"},
    "repositories": [
        {"id": "backend", "path": "sources/backend", "role": "source"},
        {"id": "docs", "path": ".", "role": "destination"},
    ],
    "documentation": {"output_directory": "docs/generated", "destination": {"repository_id": "docs"}},
}


def git(directory: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True, env=ENVIRONMENT
    )
    return done.stdout.strip()


def main(target: Path) -> None:
    source = target / "sources" / "backend"
    shutil.copytree(SOURCE, source)
    git(source, "init", "-q", "-b", "main")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "fixture source")
    # Detached, as actions/checkout leaves a source checked out by SHA.
    revision = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", "--detach", revision)

    git(target, "init", "-q", "-b", "main")
    (target / "repodocs.json").write_text(json.dumps(CONFIG, indent=2) + "\n", encoding="utf-8")
    (target / ".gitignore").write_text("sources/\n.repodocs/*\n!.repodocs/artifact-manifest.json\n", encoding="utf-8")
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "coordination config")
    print("revisions=" + json.dumps({"backend": revision}))
    print("wrong_revisions=" + json.dumps({"backend": "0" * 40}))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
