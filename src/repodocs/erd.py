"""Typed ERD model generation and deterministic Mermaid ER diagram rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ERDAttribute:
    name: str
    type: str
    keys: tuple[str, ...] = ()
    comment: str | None = None

    def render(self) -> str:
        key_str = f" {', '.join(self.keys)}" if self.keys else ""
        comment_str = f' "{self.comment}"' if self.comment else ""
        return f"{self.type} {self.name}{key_str}{comment_str}"


@dataclass(frozen=True)
class ERDEntity:
    id: str
    display_name: str
    attributes: tuple[ERDAttribute, ...]
    table_name: str
    schema: str | None = None
    has_primary_key: bool = True
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ERDRelationship:
    parent_entity_id: str
    child_entity_id: str
    parent_cardinality: str  # "||" or "|o"
    child_cardinality: str   # "o{" or "o|"
    identifying: bool        # True -> "--", False -> ".."
    label: str               # e.g. "order_id"
    evidence_ids: tuple[str, ...] = ()

    def render(self) -> str:
        line = "--" if self.identifying else ".."
        return f'{self.parent_entity_id} {self.parent_cardinality}{line}{self.child_cardinality} {self.child_entity_id} : "{self.label}"'


@dataclass(frozen=True)
class ERDModel:
    entities: tuple[ERDEntity, ...]
    relationships: tuple[ERDRelationship, ...]
    missing_keys: tuple[str, ...] = ()
    unsupported_relationships: tuple[str, ...] = ()


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


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
    normalized = re.sub(r"\s*,\s*", ",", normalized)
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_\-\(\)\[\],]", "", normalized)
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


def build_erd_model(ir: dict[str, Any]) -> ERDModel | None:
    nodes = {node["id"]: node for node in ir.get("nodes", [])}
    table_nodes = [node for node in ir.get("nodes", []) if node.get("type") == "table"]
    if not table_nodes:
        return None

    table_nodes.sort(
        key=lambda n: (
            n.get("attributes", {}).get("repository_id", ""),
            n.get("attributes", {}).get("schema") or "",
            n.get("attributes", {}).get("table", ""),
            n.get("name", ""),
        )
    )

    node_to_entity_id: dict[str, str] = {}
    table_name_to_node: dict[str, dict[str, Any]] = {}
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
        table_name_to_node[name] = node
        table_attr = node.get("attributes", {}).get("table")
        if table_attr and table_attr not in table_name_to_node:
            table_name_to_node[table_attr] = node

    missing_keys: list[str] = []
    unsupported_relationships: list[str] = []
    entities: list[ERDEntity] = []

    for node in table_nodes:
        attrs = node.get("attributes", {})
        table_name = attrs.get("table", node.get("name", ""))
        schema = attrs.get("schema")
        qualified_name = node.get("name", table_name)
        primary_key = attrs.get("primary_key", [])
        unique_constraints = attrs.get("unique_constraints", [])
        foreign_keys = attrs.get("foreign_keys", [])
        raw_columns = attrs.get("columns", [])
        evidence_ids = tuple(node.get("evidence_ids", []))

        has_pk = bool(primary_key)
        if not has_pk:
            missing_keys.append(f"Table `{qualified_name}` defines no primary key.")

        fk_columns: set[str] = set()
        for fk in foreign_keys:
            cols = fk.get("columns", [])
            fk_columns.update(cols)
            ref_table = fk.get("references_table")
            if ref_table and ref_table not in table_name_to_node:
                unsupported_relationships.append(
                    f"Table `{qualified_name}` foreign key ({', '.join(cols)}) references `{ref_table}`, which is not defined in the extracted schema."
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
                schema=schema,
                has_primary_key=has_pk,
                evidence_ids=evidence_ids,
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
                f"Table `{child_node.get('name')}` foreign key ({', '.join(fk_cols)}) references `{parent_node.get('name')}`, which defines no primary key."
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

        relationships.append(
            ERDRelationship(
                parent_entity_id=parent_id,
                child_entity_id=child_id,
                parent_cardinality=parent_cardinality,
                child_cardinality=child_cardinality,
                identifying=is_identifying,
                label=label,
                evidence_ids=tuple(edge.get("evidence_ids", [])),
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
    if model.missing_keys or model.unsupported_relationships:
        lines.append("    %% Reported limitations and diagnostics:")
        for mk in model.missing_keys:
            lines.append(f"    %% - Missing key: {mk}")
        for ur in model.unsupported_relationships:
            lines.append(f"    %% - Unsupported relationship: {ur}")

    for entity in model.entities:
        if entity.id != entity.display_name:
            header = f'{entity.id}["{_escape_label(entity.display_name)}"] {{'
        else:
            header = f"{entity.id} {{"
        lines.append(f"    {header}")
        for attr in entity.attributes:
            lines.append(f"        {attr.render()}")
        lines.append("    }")

    for rel in model.relationships:
        lines.append(f"    {rel.render()}")

    return "\n".join(lines) + "\n"
