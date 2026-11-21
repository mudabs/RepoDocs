from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repodocs.cli import main


FIXTURE = Path(__file__).parent / "fixtures" / "basic-repo"

GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "RepoDocs Test",
    "GIT_AUTHOR_EMAIL": "repodocs-test@example.invalid",
    "GIT_COMMITTER_NAME": "RepoDocs Test",
    "GIT_COMMITTER_EMAIL": "repodocs-test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repository: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(GIT_ENVIRONMENT)
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    return completed.stdout.strip()


def init_git_repository(repository: Path) -> str:
    """Turn a directory into a Git repository with one commit on main; returns the HEAD SHA."""

    repository.mkdir(parents=True, exist_ok=True)
    if not any(repository.iterdir()):
        (repository / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(repository, "init", "-q", "-b", "main")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "initial")
    return git(repository, "rev-parse", "HEAD")


def copy_fixture(destination: Path) -> Path:
    shutil.copytree(FIXTURE, destination)
    return destination


def invoke(root: Path, capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict]:
    code = main(["--root", str(root), *arguments])
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def read_ir(root: Path) -> dict:
    return json.loads((root / ".repodocs" / "ir.json").read_text(encoding="utf-8"))


def read_manifest(root: Path) -> dict:
    return json.loads((root / ".repodocs" / "artifact-manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_token_counting_requests(monkeypatch):
    """Ordinary tests never reach a provider, not even for a token count. A test of the count opts back in."""

    from repodocs.ai.providers import AnthropicProvider
    from repodocs.errors import ProviderError

    def refuse(self, system, user, schema):
        raise ProviderError("token counting is disabled in tests")

    monkeypatch.setattr(AnthropicProvider, "count_input_tokens", refuse)


def allow_large_context(root: Path, window: int = 131072) -> None:
    """Tell the project its (replayed) local model has a larger window than the 4096-token default.

    Without a provider count a request is bounded at one token per byte (ADR-031), so fixture prompts of a few tens
    of kilobytes need a window this size even though their real token count is several times smaller.
    """

    path = root / "repodocs.json"
    configuration = json.loads(path.read_text(encoding="utf-8"))
    configuration["ai"].setdefault("limits", {})["context_window_tokens"] = window
    path.write_text(json.dumps(configuration), encoding="utf-8")
