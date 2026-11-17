"""Typed ERD model generation and deterministic Mermaid ER diagram rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .common import markdown_code, markdown_text, scrub


@dataclass(frozen=True)
class ERDAttribute:
    name: str
    type: str
    keys: tuple[str, ...] = ()
    comment: str | None = None

    def render(self) -> str:
        key_str = f" {', '.join(self.keys)}" if self.keys else ""
        comment_str = f' "{_escape_label(self.comment)}"' if self.comment else ""
        return f"{self.type} {self.name}{key_str}{comment_str}"


@dataclass(frozen=True)
class ERDEntity:
    id: str
    display_name: str
    attributes: tuple[ERDAttribute, ...]
    table_name: str
    repository_id: str
    schema: str | None = None
    has_primary_key: bool = True
    evidence_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ERDRelationship:
    parent_entity_id: str
    child_entity_id: str
    parent_cardinality: str  # "||" or "|o"
    child_cardinality: str   # "o{" or "o|"
    identifying: bool        # True -> "--", False -> ".."
    label: str               # e.g. "order_id"
    evidence_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def render(self) -> str:
        line = "--" if self.identifying else ".."
        return f'{self.parent_entity_id} {self.parent_cardinality}{line}{self.child_cardinality} {self.child_entity_id} : "{_escape_label(self.label)}"'


@dataclass(frozen=True)
class ERDModel:
    entities: tuple[ERDEntity, ...]
    relationships: tuple[ERDRelationship, ...]
    missing_keys: tuple[str, ...] = ()
    unsupported_relationships: tuple[str, ...] = ()


def _escape_label(value: str) -> str:
    """Untrusted text inside a quoted Mermaid label, written with the entities Mermaid defines."""

    return scrub(value).replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def _evidence_sort_key(ev_id: str, evidence_map: dict[str, dict[str, Any]]) -> tuple[str, str, int, str]:
    ev = evidence_map.get(ev_id)
    if not ev:
        return ("", "", 0, ev_id)
    return (
        ev.get("repository_id", ""),
        ev.get("path", ""),
        ev.get("line_start", 0) if ev.get("kind") == "span" else 0,
        ev_id,
    )


def _format_evidence_ref(ev_id: str, evidence_map: dict[str, dict[str, Any]]) -> str:
    ev = evidence_map.get(ev_id)
    if not ev:
        return ev_id
    location = f"{ev.get('repository_id', '')}:{ev.get('path', '')}"
    if ev.get("kind") == "span":
        start = ev.get("line_start")
        end = ev.get("line_end")
        if start is not None and end is not None:
            location += f":L{start}-L{end}"
    return f"{location} ({ev_id})"


def normalize_type(type_str: str) -> str:
    if not type_str:
        return "unknown"
    normalized = type_str.strip().lower()
    multi_words = {
        "character varying": "varchar",
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
        "time with time zone": "timetz",
        "time without time zone": "time",
        "double precision": "double_precision",
    }
    for phrase, replacement in multi_words.items():
        if normalized.startswith(phrase):
            normalized = normalized.replace(phrase, replacement, 1)

    # Commas are outside the documented Mermaid attribute-type grammar;
    # replace them with underscores so precision/scale remain intact (e.g. numeric(14_2)).
    normalized = re.sub(r"\s*,\s*", "_", normalized)
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_\-\(\)\[\]]", "", normalized)
    if not normalized or not normalized[0].isalpha():
        normalized = f"t_{normalized}" if normalized else "unknown"
    return normalized


def sanitize_name(name_str: str) -> str:
    cleaned = name_str.strip().strip('"').strip("'")
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", cleaned)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"col_{cleaned}"
    return cleaned


def sanitize_entity_id(name_str: str) -> str:
    cleaned = name_str.strip().strip('"').strip("'")
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", cleaned)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"tbl_{cleaned}"
    return cleaned


def canonical_table_name(schema: str | None, table_name: str) -> str:
    """Canonical table identity following the PostgreSQL analyzer:
    schema.table if schema and schema != 'public' else table.
    """
    if schema and schema != "public":
        return f"{schema}.{table_name}"
    return table_name


def canonical_ref_name(ref: str) -> str:
    """Canonical reference identity matching PostgreSQL analyzer's _qualified_name."""
    if ref.startswith("public."):
        return ref[len("public."):]
    return ref


def build_erd_model(ir: dict[str, Any]) -> ERDModel | None:
    nodes = {node["id"]: node for node in ir.get("nodes", [])}
    table_nodes = [node for node in ir.get("nodes", []) if node.get("type") == "table"]
    if not table_nodes:
        return None

    evidence_map = {ev["id"]: ev for ev in ir.get("evidence", [])}

    table_nodes.sort(
        key=lambda n: (
            n.get("attributes", {}).get("repository_id", ""),
            n.get("attributes", {}).get("schema") or "",
            n.get("attributes", {}).get("table", ""),
            n.get("name", ""),
        )
    )

    # Map tables per repository following the PostgreSQL analyzer's canonical repository/schema identity.
    # Schema-qualified tables (e.g. billing.customers) are registered exclusively under their qualified name
    # and do not receive bare-name aliases, preventing cross-schema false suppression of missing foreign-key targets.
    repo_tables: dict[str, dict[str, dict[str, Any]]] = {}
    for node in table_nodes:
        attrs = node.get("attributes", {})
        repo_id = attrs.get("repository_id", "")
        schema = attrs.get("schema")
        table_attr = attrs.get("table") or node.get("name", "")
        canon_name = canonical_table_name(schema, table_attr)

        names = repo_tables.setdefault(repo_id, {})
        names[canon_name] = node
        node_name = node.get("name", "")
        if node_name:
            names[canonical_ref_name(node_name)] = node
        if not schema or schema == "public":
            names[f"public.{table_attr}"] = node

    node_to_entity_id: dict[str, str] = {}
    used_ids: set[str] = set()

    for node in table_nodes:
        name = node.get("name", "")
        base_id = sanitize_entity_id(name)
        entity_id = base_id
        suffix = 2
        while entity_id in used_ids:
            entity_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(entity_id)
        node_to_entity_id[node["id"]] = entity_id

    missing_keys: list[str] = []
    unsupported_relationships: list[str] = []
    entities: list[ERDEntity] = []

    for node in table_nodes:
        attrs = node.get("attributes", {})
        repo_id = attrs.get("repository_id", "")
        table_name = attrs.get("table", node.get("name", ""))
        schema = attrs.get("schema")
        qualified_name = node.get("name", table_name)
        primary_key = attrs.get("primary_key", [])
        unique_constraints = attrs.get("unique_constraints", [])
        foreign_keys = attrs.get("foreign_keys", [])
        raw_columns = attrs.get("columns", [])
        raw_evidence_ids = set(node.get("evidence_ids", []))
        sorted_evidence_ids = tuple(sorted(raw_evidence_ids, key=lambda eid: _evidence_sort_key(eid, evidence_map)))
        evidence_refs = tuple(_format_evidence_ref(eid, evidence_map) for eid in sorted_evidence_ids)

        has_pk = bool(primary_key)
        if not has_pk:
            missing_keys.append(f"Table {markdown_code(qualified_name)} defines no primary key.")

        # Check foreign keys strictly against the declaring table's repository and canonical schema identity
        local_tables = repo_tables.get(repo_id, {})
        fk_columns: set[str] = set()
        for fk in foreign_keys:
            cols = fk.get("columns", [])
            fk_columns.update(cols)
            ref_table = fk.get("references_table")
            if ref_table:
                canon_ref = canonical_ref_name(ref_table)
                if canon_ref not in local_tables and ref_table not in local_tables:
                    unsupported_relationships.append(
                        f"Table {markdown_code(qualified_name)} foreign key ({markdown_text(', '.join(cols))}) references "
                        f"{markdown_code(ref_table)}, which is not defined in repository {markdown_code(repo_id)}."
                    )

        unique_columns: set[str] = {
            uc["columns"][0]
            for uc in unique_constraints
            if len(uc.get("columns", [])) == 1
        }

        erd_attrs: list[ERDAttribute] = []
        for col in raw_columns:
            col_name = col.get("name", "")
            safe_name = sanitize_name(col_name)
            col_type = normalize_type(col.get("type", "unknown"))
            is_pk = (col_name in primary_key) or col.get("primary_key", False)
            is_fk = col_name in fk_columns
            is_uk = (col_name in unique_columns) or col.get("unique", False)

            keys_list: list[str] = []
            if is_pk:
                keys_list.append("PK")
            if is_uk and not is_pk:
                keys_list.append("UK")
            if is_fk:
                keys_list.append("FK")

            erd_attrs.append(
                ERDAttribute(
                    name=safe_name,
                    type=col_type,
                    keys=tuple(keys_list),
                )
            )

        entity_id = node_to_entity_id[node["id"]]
        entities.append(
            ERDEntity(
                id=entity_id,
                display_name=qualified_name,
                attributes=tuple(erd_attrs),
                table_name=table_name,
                repository_id=repo_id,
                schema=schema,
                has_primary_key=has_pk,
                evidence_ids=sorted_evidence_ids,
                evidence_refs=evidence_refs,
            )
        )

    ref_edges = [
        rel for rel in ir.get("relationships", [])
        if rel.get("type") == "references" and rel.get("source") in node_to_entity_id and rel.get("target") in node_to_entity_id
    ]

    relationships: list[ERDRelationship] = []
    for edge in ref_edges:
        child_node = nodes[edge["source"]]
        parent_node = nodes[edge["target"]]
        child_id = node_to_entity_id[child_node["id"]]
        parent_id = node_to_entity_id[parent_node["id"]]

        child_attrs = child_node.get("attributes", {})
        parent_attrs = parent_node.get("attributes", {})
        edge_attrs = edge.get("attributes", {})

        fk_cols = edge_attrs.get("columns", [])
        parent_pk = parent_attrs.get("primary_key", [])
        child_pk = child_attrs.get("primary_key", [])
        child_cols = {col.get("name"): col for col in child_attrs.get("columns", [])}

        if not parent_pk:
            missing_keys.append(
                f"Table {markdown_code(child_node.get('name'))} foreign key ({markdown_text(', '.join(fk_cols))}) references "
                f"{markdown_code(parent_node.get('name'))}, which defines no primary key."
            )

        all_not_null = bool(fk_cols and all(not child_cols.get(c, {}).get("nullable", True) for c in fk_cols))
        parent_cardinality = "||" if all_not_null else "|o"

        child_unique_constraints = child_attrs.get("unique_constraints", [])
        is_unique = (
            (bool(fk_cols) and set(fk_cols) == set(child_pk))
            or any(set(uc.get("columns", [])) == set(fk_cols) for uc in child_unique_constraints)
            or (len(fk_cols) == 1 and child_cols.get(fk_cols[0], {}).get("unique", False))
        )
        child_cardinality = "o|" if is_unique else "o{"

        is_identifying = bool(child_pk and fk_cols and set(fk_cols).issubset(set(child_pk)))
        label = ", ".join(fk_cols) if fk_cols else "references"
        raw_edge_evidence_ids = set(edge.get("evidence_ids", []))
        sorted_edge_evidence_ids = tuple(sorted(raw_edge_evidence_ids, key=lambda eid: _evidence_sort_key(eid, evidence_map)))
        edge_evidence_refs = tuple(_format_evidence_ref(eid, evidence_map) for eid in sorted_edge_evidence_ids)

        relationships.append(
            ERDRelationship(
                parent_entity_id=parent_id,
                child_entity_id=child_id,
                parent_cardinality=parent_cardinality,
                child_cardinality=child_cardinality,
                identifying=is_identifying,
                label=label,
                evidence_ids=sorted_edge_evidence_ids,
                evidence_refs=edge_evidence_refs,
            )
        )

    relationships.sort(key=lambda r: (r.parent_entity_id, r.child_entity_id, r.label))

    return ERDModel(
        entities=tuple(entities),
        relationships=tuple(relationships),
        missing_keys=tuple(sorted(set(missing_keys))),
        unsupported_relationships=tuple(sorted(set(unsupported_relationships))),
    )


def render_erd(ir: dict[str, Any]) -> str | None:
    model = build_erd_model(ir)
    if model is None:
        return None

    lines = ["erDiagram"]

    # Header evidence mappings preserving entity and relationship traceability
    lines.append("    %% Evidence mappings:")
    for entity in model.entities:
        refs_str = ", ".join(entity.evidence_refs) if entity.evidence_refs else ", ".join(entity.evidence_ids) or "none"
        lines.append(scrub(f"    %% - entity {entity.id} ({entity.display_name}): {refs_str}"))
    for rel in model.relationships:
        refs_str = ", ".join(rel.evidence_refs) if rel.evidence_refs else ", ".join(rel.evidence_ids) or "none"
        lines.append(scrub(f"    %% - relationship {rel.parent_entity_id} -> {rel.child_entity_id} ({rel.label}): {refs_str}"))

    if model.missing_keys or model.unsupported_relationships:
        lines.append("    %% Reported limitations and diagnostics:")
        for mk in model.missing_keys:
            lines.append(scrub(f"    %% - Missing key: {mk}"))
        for ur in model.unsupported_relationships:
            lines.append(scrub(f"    %% - Unsupported relationship: {ur}"))

    for entity in model.entities:
        refs_str = ", ".join(entity.evidence_refs) if entity.evidence_refs else ", ".join(entity.evidence_ids)
        if refs_str:
            lines.append(scrub(f"    %% evidence: {refs_str}"))
        if entity.id != entity.display_name:
            header = f'{entity.id}["{_escape_label(entity.display_name)}"] {{'
        else:
            header = f"{entity.id} {{"
        lines.append(f"    {header}")
        for attr in entity.attributes:
            lines.append(f"        {attr.render()}")
        lines.append("    }")

    for rel in model.relationships:
        refs_str = ", ".join(rel.evidence_refs) if rel.evidence_refs else ", ".join(rel.evidence_ids)
        if refs_str:
            lines.append(scrub(f"    %% evidence: {refs_str}"))
        lines.append(f"    {rel.render()}")

    return "\n".join(lines) + "\n"
