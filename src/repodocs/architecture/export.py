"""The static local HTML export of an architecture view (ADR-026), and the artifacts of a stored view.

One self-contained file: the diagram is drawn in the browser by the pinned Mermaid bundle (at
``securityLevel: strict``), embedded byte for byte, so the page works offline and loads nothing.
A Content-Security-Policy allows only the two inline scripts it carries, by hash, and forbids every
network connection. All text from the repository or the model reaches the page through
``textContent``, and the data block escapes ``<``, ``>``, ``&`` and line separators. Links appear
only when they match the source-link allowlist. There is no backend and no editor.

The export is optional: it needs the Mermaid bundle from the optional tooling, so it is produced
only when the project asks for it (``architecture.html_export``). A project that asks and lacks the
bundle gets a clear error, never a silently different page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..common import canonical_json
from ..config import ProjectConfig
from ..errors import ConfigError
from .discovery import STATUS_CURRENT, load_view
from .navigation import MAP_NAME, evidence_map, repository_remotes
from .render import BANNER, node_key, project, render_view
from .render_check import TOOL_DIRECTORY
from .viewer_script import VIEWER_SCRIPT

HTML_NAME = "architecture-view.html"
MERMAID_BUNDLE = Path("node_modules") / "mermaid" / "dist" / "mermaid.min.js"
NOTE = "Locations and hashes only: this page holds no source text. A link opens the exact commit on GitHub; working-tree content has none."
STYLE = """
body { margin: 0; font: 14px system-ui, sans-serif; color: #1f2328; background: #fff; display: flex; flex-direction: column; height: 100vh; }
header { padding: 8px 12px; border-bottom: 1px solid #d0d7de; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
header h1 { font-size: 16px; margin: 0 12px 0 0; }
.banner { color: #9a6700; font-size: 12px; }
main { flex: 1; display: flex; min-height: 0; }
#stage { flex: 1; overflow: hidden; position: relative; cursor: grab; background: #f6f8fa; }
#canvas { transform-origin: 0 0; position: absolute; }
aside { width: 360px; overflow: auto; padding: 8px 12px; border-left: 1px solid #d0d7de; }
aside ul { padding-left: 18px; } .evidence small { display: block; color: #57606a; }
.clickable { cursor: pointer; } button { font: inherit; } li button { background: none; border: 0; text-align: left; cursor: pointer; padding: 2px 0; text-decoration: underline; }
.note { color: #57606a; font-size: 12px; }
@media (prefers-color-scheme: dark) { body { background: #0d1117; color: #e6edf3; } #stage { background: #161b22; } header, aside { border-color: #30363d; } }
"""


def mermaid_bundle(tool_directory: Path | None = None) -> str | None:
    path = (tool_directory or TOOL_DIRECTORY) / MERMAID_BUNDLE
    return path.read_text(encoding="utf-8") if path.exists() else None


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _data_block(value: Any) -> str:
    """JSON that cannot end its script element or break out of it."""

    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _refuse_unsafe_bundle(bundle: str) -> None:
    if re.search(r"<\s*/?\s*script", bundle, re.I):
        raise ConfigError("The Mermaid bundle contains a script tag and cannot be embedded safely in the HTML export")


def build_html(view: dict[str, Any], diagrams: dict[str, str], evidence: dict[str, Any], bundle: str) -> str:
    by_id = {item["id"]: item for item in view["components"]}
    groups = {
        node.key: {"label": node.label, "members": list(node.members)}
        for diagram in project(view) for node in diagram.nodes if node.members
    }
    data = {
        "banner": BANNER + (f" Partial discovery: {view.get('partial_reason', 'stopped early')}." if view["status"] == "partial" else ""),
        "note": NOTE,
        "diagrams": [{"name": name, "text": text} for name, text in sorted(diagrams.items(), key=lambda item: (item[0] != "architecture-view.mmd", item[0]))],
        "components": [
            {"key": node_key(item["id"]), "id": item["id"], "name": item["name"], "kind": item["kind"], "responsibility": item["responsibility"],
             "classification": item["classification"], "confidence": item["confidence"], "repository": item["repository_id"]}
            for item in sorted(view["components"], key=lambda entry: (entry["repository_id"], entry["name"], entry["id"]))
        ],
        "connections": [
            {"id": item["id"], "source": by_id[item["source"]]["name"], "target": by_id[item["target"]]["name"], "kind": item["kind"], "label": item["label"],
             "classification": item["classification"], "confidence": item["confidence"], "cross_repository": item["cross_repository"]}
            for item in sorted(view["connections"], key=lambda entry: entry["id"])
        ],
        "groups": groups,
        "evidence": evidence,
        "repositories": evidence["repositories"],
    }
    _refuse_unsafe_bundle(bundle)
    data_script = _data_block(data)
    policy = (
        f"default-src 'none'; script-src {_hash(bundle)} {_hash(VIEWER_SCRIPT)}; style-src 'unsafe-inline'; "
        "img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
    )
    snapshot_lines = "".join(
        f"<li>{_escape(item['repository_id'])}: {_escape(item['revision_kind'])} {_escape(item['revision'][:12])}. {_escape(item['note'])}</li>"
        for item in evidence["repositories"]
    )
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{policy}\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Architecture view</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n"
        "<header><h1>Architecture view</h1><select id=\"diagram\" aria-label=\"Diagram\"></select>"
        "<button id=\"fit\" type=\"button\">Fit</button><button id=\"in\" type=\"button\">Zoom in</button><button id=\"out\" type=\"button\">Zoom out</button>"
        f"<span class=\"banner\">{_escape(data['banner'])}</span></header>\n"
        "<main><div id=\"stage\"><div id=\"canvas\"></div></div><aside><div id=\"details\"></div>"
        "<h3>Components</h3><ul id=\"components\"></ul><h3>Connections</h3><ul id=\"connections\"></ul>"
        f"<h3>Snapshots</h3><ul>{snapshot_lines}</ul></aside></main>\n"
        f"<script type=\"application/json\" id=\"data\">{data_script}</script>\n"
        f"<script>{bundle}</script>\n<script>{VIEWER_SCRIPT}</script>\n</body>\n</html>\n"
    )


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def architecture_artifacts(
    config: ProjectConfig, ir: dict[str, Any], tool_directory: Path | None = None, *, include_html: bool = True
) -> tuple[dict[str, str], list[str]]:
    """The optional artifacts of the stored view and warnings; nothing when there is no usable view.

    A stale or invalid view is reported and produces no artifacts, so it can never be shown as current.
    ``include_html=False`` skips the page, which needs the optional Mermaid bundle, for checks that only compare files.
    """

    status, view, reason = load_view(config, ir)
    if status != STATUS_CURRENT or view is None:
        return {}, ([] if status == "missing" else [f"The architecture view is {status}: {reason}; no architecture diagrams were generated"])
    context = json.loads((config.internal_path / "architecture-context.json").read_text(encoding="utf-8"))
    diagrams = render_view(view)
    evidence = evidence_map(view, context, ir, repository_remotes(config, ir))
    artifacts = dict(diagrams)
    artifacts[MAP_NAME] = canonical_json(evidence)
    if config.architecture.html_export and include_html:
        bundle = mermaid_bundle(tool_directory)
        if bundle is None:
            raise ConfigError(
                "architecture.html_export is on but the Mermaid bundle is not installed; run `npm ci` in tools/mermaid, "
                "or turn the setting off. The export is never produced in a different form"
            )
        artifacts[HTML_NAME] = build_html(view, diagrams, evidence, bundle)
    return artifacts, []


def validate_export_page(content: str) -> None:
    """The page is what this module builds: three scripts, locked by hash, with nothing loaded from outside."""

    from html.parser import HTMLParser

    from ..errors import ValidationError

    class Scripts(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.scripts: list[tuple[dict[str, str | None], str]] = []
            self.forbidden: list[str] = []
            self._attributes: dict[str, str | None] = {}
            self._text: list[str] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attributes = dict(attrs)
            if tag == "script":
                self._attributes, self._text = attributes, []
            if tag in ("link", "img", "iframe", "object", "embed", "form", "base") or (tag == "script" and "src" in attributes):
                self.forbidden.append(tag)

        def handle_data(self, data: str) -> None:
            if self._text is not None:
                self._text.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag == "script" and self._text is not None:
                self.scripts.append((self._attributes, "".join(self._text)))
                self._text = None

    parser = Scripts()
    parser.feed(content)
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', content)
    inline = [text for attributes, text in parser.scripts if attributes.get("type") != "application/json"]
    if parser.forbidden or policy is None or len(parser.scripts) != 3 or len(inline) != 2:
        raise ValidationError("The architecture export page does not have the expected structure")
    if any(_hash(text) not in policy.group(1) for text in inline) or "connect-src 'none'" not in policy.group(1):
        raise ValidationError("The architecture export page's scripts do not match its Content-Security-Policy")
