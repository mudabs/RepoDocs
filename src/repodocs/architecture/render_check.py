"""Optional check with the real Mermaid renderer (ADR-026, ADR-022).

RepoDocs' own validator checks the grammar RepoDocs emits. It is not Mermaid. This module renders
diagrams with a pinned ``@mermaid-js/mermaid-cli`` from ``tools/mermaid`` and a locally installed
Chrome, so a diagram is only reported as rendered when the real renderer produced an SVG.

The tooling is optional. Without it the result is ``not-available``, which is never reported as
success. Inputs are RepoDocs' own generated text, never repository files, and the renderer is run
with Mermaid's strict security level and without HTML labels.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

RENDERED = "rendered"
NOT_AVAILABLE = "not-available"
FAILED = "failed"

TOOL_DIRECTORY = Path(__file__).resolve().parents[3] / "tools" / "mermaid"
TIMEOUT_SECONDS = 180
MERMAID_CONFIG = {"securityLevel": "strict", "htmlLabels": False, "flowchart": {"htmlLabels": False}, "er": {"useMaxWidth": True}, "theme": "default"}
BROWSER_PATHS = (
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
UNSAFE_SVG = (
    (re.compile(r"<\s*script", re.I), "a script element"),
    (re.compile(r"<\s*(?:iframe|object|embed|foreignObject)\b", re.I), "an embedded-content element"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "an event handler attribute"),
    (re.compile(r"(?:href|src)\s*=\s*[\"']\s*(?:javascript|data|vbscript):", re.I), "a script or data URL"),
    (re.compile(r"(?:href|src)\s*=\s*[\"']\s*(?:https?:)?//", re.I), "a link to another site"),
)


@dataclass
class RenderResult:
    status: str
    message: str = ""
    svgs: dict[str, str] = field(default_factory=dict)


def find_renderer(tool_directory: Path | None = None) -> Path | None:
    binary = (tool_directory or TOOL_DIRECTORY) / "node_modules" / ".bin" / ("mmdc.cmd" if os.name == "nt" else "mmdc")
    return binary if binary.exists() else None


def find_browser() -> str | None:
    for name in ("PUPPETEER_EXECUTABLE_PATH", "CHROME_PATH"):
        if os.environ.get(name) and Path(os.environ[name]).exists():
            return os.environ[name]
    for path in BROWSER_PATHS:
        if Path(path).exists():
            return path
    for name in ("google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def svg_problems(svg: str) -> list[str]:
    """What in a rendered SVG would be unsafe to publish or open; empty when nothing is found."""

    return [f"the SVG contains {description}" for pattern, description in UNSAFE_SVG if pattern.search(svg)]


def render(diagrams: dict[str, str], tool_directory: Path | None = None) -> RenderResult:
    """Render every diagram with the real renderer; one Chrome start for the whole set when it works."""

    renderer = find_renderer(tool_directory)
    if renderer is None:
        return RenderResult(NOT_AVAILABLE, "The Mermaid renderer is not installed; run `npm ci` in tools/mermaid. Diagrams were not checked with Mermaid")
    browser = find_browser()
    if browser is None:
        return RenderResult(NOT_AVAILABLE, "No Chrome or Chromium was found; set PUPPETEER_EXECUTABLE_PATH. Diagrams were not checked with Mermaid")
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        (work / "mermaid.json").write_text(json.dumps(MERMAID_CONFIG), encoding="utf-8")
        arguments = ["--no-sandbox"] if os.environ.get("REPODOCS_MERMAID_NO_SANDBOX") == "1" else []
        (work / "puppeteer.json").write_text(json.dumps({"executablePath": browser, "args": arguments}), encoding="utf-8")
        names = sorted(diagrams)
        document = "\n\n".join(f"```mermaid\n{diagrams[name]}```" for name in names) + "\n"
        (work / "input.md").write_text(document, encoding="utf-8", newline="\n")
        outcome = _run(renderer, work, ["-i", "input.md", "-o", "output.md"])
        if outcome is None:
            produced = sorted(work.glob("output-*.svg"), key=lambda path: int(path.stem.rsplit("-", 1)[1]))
            if len(produced) == len(names):
                svgs = {name: path.read_text(encoding="utf-8") for name, path in zip(names, produced)}
                return _screened(svgs)
            outcome = f"the renderer produced {len(produced)} of {len(names)} images"
        # Say which diagram failed: render them one at a time.
        failures = []
        for index, name in enumerate(names):
            (work / f"one-{index}.mmd").write_text(diagrams[name], encoding="utf-8", newline="\n")
            problem = _run(renderer, work, ["-i", f"one-{index}.mmd", "-o", f"one-{index}.svg"])
            if problem is not None:
                failures.append(f"{name}: {problem}")
        return RenderResult(FAILED, "; ".join(failures) or outcome)


def _screened(svgs: dict[str, str]) -> RenderResult:
    problems = [f"{name}: {problem}" for name, svg in sorted(svgs.items()) for problem in svg_problems(svg)]
    if problems:
        return RenderResult(FAILED, "; ".join(problems), svgs)
    return RenderResult(RENDERED, "", svgs)


def _run(renderer: Path, work: Path, arguments: list[str]) -> str | None:
    """None when the renderer succeeded, otherwise its first useful diagnostic line."""

    command = [str(renderer), *arguments, "-c", "mermaid.json", "-p", "puppeteer.json", "--quiet"]
    try:
        completed = subprocess.run(command, cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return f"the renderer did not finish within {TIMEOUT_SECONDS} seconds"
    except OSError as exc:
        return f"the renderer could not start: {exc}"
    if completed.returncode == 0:
        return None
    lines = [line.strip() for line in (completed.stderr or completed.stdout).splitlines() if line.strip()]
    useful = next((line for line in lines if "error" in line.lower()), lines[0] if lines else "the renderer failed without a message")
    return useful[:300]
