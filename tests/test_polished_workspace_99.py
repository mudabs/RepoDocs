"""Real-browser acceptance for the URL-first workspace and saved history (#99).

Keyboard-only entry, announcements, focus, contrast, narrow widths, deep links, history reopen and delete, and the
AI path's disclosure review. Reopening and reloading must never build another provider.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from test_security_acceptance_71 import TOOL_DIRECTORY, _browser_or_skip, _live_server  # noqa: E402
from test_architecture_discovery import FIXTURE  # noqa: E402
from test_web_jobs import CANARY, Factory, Replay, asks  # noqa: E402


def _run(base_url: str, browser: str, node: str, scenario: str) -> dict:
    flag = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
    completed = subprocess.run(
        [node, "check-home-99.mjs", base_url, browser, scenario, *flag],
        cwd=TOOL_DIRECTORY, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
    )
    assert completed.returncode == 0, f"{scenario} failed:\n{completed.stderr[-2000:]}\n{completed.stdout[-800:]}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _wide_fixture(tmp_path: Path) -> Path:
    """The express fixture plus 16 modules, so the overview is a wide fan-out like EventShare's."""

    wide = tmp_path / "wide-shop"
    shutil.copytree(FIXTURE, wide)
    (wide / "src").mkdir(exist_ok=True)
    for i in range(16):
        (wide / "src" / f"service_{i}.js").write_text(
            f"import express from 'express';\nexport function handler_{i}() {{ return {i}; }}\n", encoding="utf-8")
    return wide


def test_home_keyboard_flow_history_contrast_and_narrow_widths(tmp_path: Path) -> None:
    node, browser = _browser_or_skip()
    factory = Factory()
    with _live_server(tmp_path / "srv", factory, shop_source=_wide_fixture(tmp_path)) as base_url:
        report = _run(base_url, browser, node, "deterministic-keyboard")
    assert report["ok"] is True
    assert report["viewerUrl"].split("/viewer/")[1].startswith("job_")
    assert report["contrastChecked"] > 5
    assert factory.contexts == [], "a local-only run, reload and reopen must never build a provider"


def test_home_ai_path_requires_review_and_reopen_never_reruns_the_model(tmp_path: Path) -> None:
    node, browser = _browser_or_skip()
    factory = Factory(Replay(asks, name="openai", external=True))
    with _live_server(tmp_path / "srv", factory) as base_url:
        report = _run(base_url, browser, node, "architecture-review")
    assert report["leak"] == {"local": 0, "session": 0, "inBody": False, "inHref": False, "inCookie": False}
    # One provider for the one consented run; reopening, reloading and the unconfirmed second attempt built none.
    assert len(factory.contexts) == 1
    for path in (tmp_path / "srv" / "store").rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes()
