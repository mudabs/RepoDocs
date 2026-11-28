"""Regression and computed-style tests for Issue #93: Mermaid CSP styling and wide-diagram sizing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from repodocs.web.security import response_headers  # noqa: E402

from test_architecture_discovery import FIXTURE  # noqa: E402
from test_security_acceptance_71 import TOOL_DIRECTORY, _browser_or_skip, _live_server  # noqa: E402
from test_web_jobs import Factory  # noqa: E402


def test_csp_allows_mermaid_styles_without_loosening_script_or_artifact_sandbox() -> None:
    page_csp = response_headers()["Content-Security-Policy"]
    directives = {part.strip().split(" ", 1)[0]: part.strip() for part in page_csp.split(";") if part.strip()}
    assert directives.get("script-src") == "script-src 'self'"
    assert directives.get("style-src") == "style-src 'self' 'unsafe-inline'"
    assert directives.get("object-src") == "object-src 'none'"
    assert directives.get("frame-ancestors") == "frame-ancestors 'none'"
    assert "'unsafe-eval'" not in page_csp

    artifact_csp = response_headers(artifact=True)["Content-Security-Policy"]
    assert artifact_csp == "sandbox; default-src 'none'; frame-ancestors 'none'"


def test_wide_deterministic_mermaid_diagram_renders_legible_fills_labels_and_fit(tmp_path: Path) -> None:
    node, browser = _browser_or_skip()

    # Create a wide multi-module fixture (16 JS modules) so overview.mmd produces a wide graph
    wide_repo = tmp_path / "wide-app"
    shutil.copytree(FIXTURE, wide_repo)
    src_dir = wide_repo / "src"
    src_dir.mkdir(exist_ok=True)
    for i in range(16):
        (src_dir / f"service_{i}.js").write_text(
            f"import express from 'express';\nexport function handler_{i}() {{ return {i}; }}\n",
            encoding="utf-8",
        )

    with _live_server(tmp_path / "srv", Factory(), shop_source=wide_repo) as base_url:
        res = subprocess.run(
            [node, str(TOOL_DIRECTORY / "check-mermaid-93.mjs"), base_url, browser, "shop"],
            cwd=TOOL_DIRECTORY,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert res.returncode == 0, f"check-mermaid-93 failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
