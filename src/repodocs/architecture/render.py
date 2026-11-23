"""Deterministic Mermaid views of a stored architecture view (ADR-026).

The model supplies no markup. A validated view is projected into a typed diagram model, and Mermaid
text is generated from that model only: identical views give identical bytes. Proposed and
low-confidence items are visibly marked in the text, not only by colour, and a connection between
repositories that no confirmed candidate backs is drawn dashed and labelled as unconfirmed.
Evidence lookup stays in the view and in comments, so every node and edge can be traced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any

from ..common import markdown_text, scrub, sha256_text, slugify

OVERVIEW_LIMIT = 12  # more components than this are grouped; each group gets its own detail diagram
OVERVIEW_NAME = "architecture-view.mmd"
DETAIL_PREFIX = "architecture-view-"
ALTERNATIVES_NAME = "architecture-view-alternatives.md"
BANNER = "Model-proposed architecture. Citations are evidence to check, not proof."
SUPPORT_NOTE = "A solid line means inferred with matching static facts. Role or request matches do not prove responsibilities or runtime behavior."
CLASS_DEFINITIONS = (
    "classDef inferred stroke:#1f6f43,stroke-width:2px",
    "classDef proposed stroke:#9a6700,stroke-width:2px,stroke-dasharray:5 5",
    "classDef stub stroke:#6e7781,stroke-width:1px,stroke-dasharray:2 2",
)


@dataclass(frozen=True)
class DiagramNode:
    key: str
    label: str
    kind: str
    classification: str
    group: str
    evidence: tuple[str, ...] = ()
    members: tuple[str, ...] = ()  # names of the components a group node stands for


@dataclass(frozen=True)
class DiagramEdge:
    source: str
    target: str
    label: str
    classification: str
    evidence: tuple[str, ...] = ()


@dataclass
class Diagram:
    name: str
    nodes: list[DiagramNode] = field(default_factory=list)
    edges: list[DiagramEdge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def label_text(value: str) -> str:
    """Untrusted text inside a quoted Mermaid label, written with the entities Mermaid defines."""

    return scrub(value).replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;").strip()


def node_key(component_id: str) -> str:
    return "c_" + component_id.split(":", 1)[1]


def group_key(repository_id: str, kind: str = "") -> str:
    return "g_" + sha256_text(f"{repository_id}|{kind}")[:12]


def _status(item: dict[str, Any]) -> str:
    """Drawn as inferred only when it is tied to its own member files and a fact backs it."""

    return "inferred" if item["classification"] == "inferred" and item["support"] == "supported" else "proposed"


def _suffix(item: dict[str, Any]) -> str:
    marks = []
    if item["classification"] == "proposed":
        marks.append("proposed")
    if item["support"] == "unverified":
        marks.append("unverified")
    if item["confidence"] == "low":
        marks.append("low confidence")
    return f" ({', '.join(marks)})" if marks else ""


def supported_view(view: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """The view without unsupported claims, and how many were left out. They stay in the stored view and the evidence map."""

    components = [item for item in view["components"] if item["support"] != "unsupported"]
    known = {item["id"] for item in components}
    connections = [item for item in view["connections"] if item["support"] != "unsupported" and item["source"] in known and item["target"] in known]
    hidden = len(view["components"]) - len(components) + len(view["connections"]) - len(connections)
    return {**view, "components": components, "connections": connections}, hidden


def _component_diagram(name: str, view: dict[str, Any], components: list[dict[str, Any]], stubs: list[dict[str, Any]]) -> Diagram:
    diagram = Diagram(name)
    chosen = {item["id"] for item in components}
    known = chosen | {item["id"] for item in stubs}
    for item in components + stubs:
        diagram.nodes.append(DiagramNode(
            node_key(item["id"]), item["name"] + _suffix(item), item["kind"],
            "stub" if item["id"] not in chosen else _status(item), item["repository_id"], tuple(item["evidence"]),
        ))
    for link in sorted(view["connections"], key=lambda item: item["id"]):
        if link["source"] in known and link["target"] in known and (link["source"] in chosen or link["target"] in chosen):
            unconfirmed = link["cross_repository"] and link["classification"] != "inferred"
            text = link["label"] + _suffix(link) + (" (unconfirmed)" if unconfirmed else "")
            diagram.edges.append(DiagramEdge(node_key(link["source"]), node_key(link["target"]), text, _status(link), tuple(sorted(link["evidence"]))))
    return diagram


def project(view: dict[str, Any]) -> list[Diagram]:
    """One diagram when the view is small, otherwise a grouped overview and a detail diagram per crowded group."""

    view, hidden = supported_view(view)
    components = sorted(view["components"], key=lambda item: (item["repository_id"], item["kind"], item["name"], item["id"]))
    notes = [SUPPORT_NOTE]
    if hidden:
        notes.append(f"{hidden} claim{'s' if hidden != 1 else ''} not shown: source facts contradict or do not contain them. They are in the evidence map")
    unverified = sum(item["support"] == "unverified" for item in components + view["connections"])
    if unverified:
        notes.append(f"{unverified} shown claim{'s are' if unverified != 1 else ' is'} unverified: no observed or literal fact was checked")
    if view["status"] == "partial":
        notes.append(f"Partial discovery: {view.get('partial_reason', 'stopped early')}")
    if len(components) <= OVERVIEW_LIMIT:
        diagram = _component_diagram(OVERVIEW_NAME, view, components, [])
        diagram.notes = notes
        return [diagram]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in components:
        groups.setdefault((item["repository_id"], item["kind"]), []).append(item)
    where = {item["id"]: (item["repository_id"], item["kind"]) for item in components}
    overview = Diagram(OVERVIEW_NAME, notes=notes + [f"Grouped: more than {OVERVIEW_LIMIT} components; see the detail diagrams"])
    for (repository, kind), members in sorted(groups.items()):
        classification = "inferred" if all(_status(item) == "inferred" for item in members) else "proposed"
        overview.nodes.append(DiagramNode(group_key(repository, kind), f"{kind} ({len(members)})", kind, classification, repository, (), tuple(item["name"] for item in members)))
    counts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for link in view["connections"]:
        pair = (group_key(*where[link["source"]]), group_key(*where[link["target"]]))
        if pair[0] != pair[1]:
            counts.setdefault(pair, []).append(link)
    for (source, target), links in sorted(counts.items()):
        inferred = all(_status(item) == "inferred" for item in links)
        unconfirmed = any(item["cross_repository"] and item["classification"] != "inferred" for item in links)
        text = f"{len(links)} connection{'s' if len(links) != 1 else ''}" + ("" if inferred else " (proposed)") + (" (unconfirmed)" if unconfirmed else "")
        overview.edges.append(DiagramEdge(source, target, text, "inferred" if inferred else "proposed", tuple(sorted({key for item in links for key in item["evidence"]}))))
    diagrams = [overview]
    for (repository, kind), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        ids = {item["id"] for item in members}
        neighbours = sorted(
            {link[end] for link in view["connections"] for end, other in (("source", "target"), ("target", "source")) if link[other] in ids and link[end] not in ids}
        )
        stubs = [item for item in components if item["id"] in neighbours]
        diagrams.append(_component_diagram(f"{DETAIL_PREFIX}{slugify(repository)}-{slugify(kind)}.mmd", view, members, stubs))
    return diagrams


def _shape(kind: str, label: str) -> str:
    text = f'"{label_text(label)}"'
    if kind in ("database", "storage"):
        return f"[({text})]"
    if kind == "external-service":
        return "{{" + text + "}}"
    if kind == "frontend":
        return f"([{text}])"
    return f"[{text}]"


def to_mermaid(diagram: Diagram, view: dict[str, Any]) -> str:
    """Mermaid source for one diagram. Every line comes from the typed model above."""

    lines = ["flowchart LR", f"    %% {BANNER}", f"    %% context {view['provenance']['context_hash'][:16]}; snapshot {view['provenance']['source_ir_hash'][:16]}"]
    by_group: dict[str, list[DiagramNode]] = {}
    for node in diagram.nodes:
        by_group.setdefault(node.group, []).append(node)
    multiple = len(by_group) > 1
    for repository, nodes in sorted(by_group.items()):
        indent = "    "
        if multiple:
            lines.append(f'    subgraph {group_key(repository, "repo")}["{label_text(repository)}"]')
            indent = "        "
        for node in nodes:
            lines.append(f"{indent}{node.key}{_shape(node.kind, node.label)}")
        if multiple:
            lines.append("    end")
    for edge in diagram.edges:
        arrow = "-->" if edge.classification == "inferred" else "-.->"
        lines.append(f'    {edge.source} {arrow}|"{label_text(edge.label)}"| {edge.target}')
    lines.extend(f"    {definition}" for definition in CLASS_DEFINITIONS)
    for classification in ("inferred", "proposed", "stub"):
        keys = [node.key for node in diagram.nodes if node.classification == classification]
        if keys:
            lines.append(f"    class {','.join(keys)} {classification}")
    lines.append('    subgraph legend["Legend"]')
    lines.append('        legend_inferred["Inferred: matching static facts, not verified behavior"]')
    lines.append('        legend_proposed["Proposed or unverified: needs checking (dashed)"]')
    for index, note in enumerate(diagram.notes):
        lines.append(f'        legend_note_{index}["{label_text(note)}"]')
    lines.append("    end")
    lines.append("    class legend_inferred inferred")
    lines.append("    class legend_proposed proposed")
    for node in diagram.nodes:
        if node.evidence:
            lines.append(f"    %% evidence {node.key}: {', '.join(scrub(key) for key in node.evidence)}")
    for edge in diagram.edges:
        lines.append(f"    %% evidence {edge.source}->{edge.target}: {', '.join(scrub(key) for key in edge.evidence)}")
    return "\n".join(lines) + "\n"


def render_view(view: dict[str, Any]) -> dict[str, str]:
    """File name to Mermaid text for every diagram of the view, in a stable order."""

    return {diagram.name: to_mermaid(diagram, view) for diagram in project(view)}


def render_alternatives(view: dict[str, Any], citations: Mapping[str, str] | None = None) -> str | None:
    """The competing interpretations the model reported, as Markdown, or None when it reported none.

    ADR-026 shows alternatives and never drops them, but they do not belong in the diagram, which
    would crowd. Statements and names are untrusted and written as plain text. ``citations`` maps an
    evidence ID to its location; an ID with no location is listed as the ID alone.
    """

    if not view["alternatives"]:
        return None
    names = {item["id"]: item["name"] for item in view["components"]}
    lines = [
        "# Architecture alternatives",
        "",
        f"> {BANNER} Other readings the model offered; the diagram does not settle between them.",
        "",
    ]
    for index, item in enumerate(sorted(view["alternatives"], key=lambda entry: (entry["about"], entry["statement"])), start=1):
        about = ", ".join(markdown_text(names.get(key, key)) for key in item["about"]) or "the whole view"
        lines += [f"## Alternative {index}", "", markdown_text(item["statement"]), "", f"About: {about}", "", "Evidence to check:"]
        lines += [f"- {markdown_text((citations or {}).get(key, key))}" for key in item["evidence"]]
        lines.append("")
    return "\n".join(lines)
