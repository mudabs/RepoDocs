"""ORM metadata linking between persistence entities and extracted tables.

The Spring Boot adapter extracts the table name a JPA entity declares with
``@Table``, and the PostgreSQL adapter extracts the tables a repository defines.
Nothing in the source states that an entity and a table that share a name
describe the same rows: the two facts live in different files and neither one
mentions the other. The link between them is therefore inferred, never
observed. It carries the configured confidence, names the basis it was
inferred from, and cites the evidence of both sides so a reader can check it.

An entity whose declared name matches no table is left unlinked without a
limitation, because the table may be defined by another repository or by a
tool that is not part of the scanned sources. An entity whose declared name
matches more than one table is reported as an ambiguous reference instead of
being linked.
"""

from __future__ import annotations

from typing import Any

from ..ir import claim_id, relationship_id
from . import (
    CLASSIFICATION_INFERRED,
    LIMITATION_AMBIGUOUS_REFERENCE,
    AnalysisResult,
    RepositoryContext,
)


ORM_METADATA_ANALYZER_NAME = "orm-metadata"
ORM_METADATA_ANALYZER_VERSION = "1.0"
INFERRED_NAME_MATCH_CONFIDENCE = 0.8
NAME_MATCH_BASIS = "jpa-table-name"


def link_orm_metadata(context: RepositoryContext, result: AnalysisResult) -> dict[str, Any]:
    """Link persistence entities to the tables whose name they declare.

    Returns the analyzer report entry for this step so the scan records that it
    ran and what it produced.
    """

    repository_id = context.repository_id
    tables: dict[str, list[dict[str, Any]]] = {}
    for node in result.nodes:
        attributes = node["attributes"]
        if node["type"] == "table" and attributes.get("repository_id") == repository_id:
            tables.setdefault(str(attributes["table"]).lower(), []).append(node)
    declared = [
        node
        for node in result.nodes
        if node["type"] == "class"
        and node["attributes"].get("repository_id") == repository_id
        and node["attributes"].get("stereotype") == "entity"
        and node["attributes"].get("table")
    ]
    linked = 0
    limitations = 0
    previous_analyzer = context.analyzer_name
    context.analyzer_name = ORM_METADATA_ANALYZER_NAME
    try:
        for node in declared:
            table_name = str(node["attributes"]["table"])
            matches = tables.get(table_name.lower(), [])
            if len(matches) > 1:
                result.limitations.append(
                    context.limitation(
                        LIMITATION_AMBIGUOUS_REFERENCE,
                        f"Entity {node['attributes'].get('qualified_name', node['name'])} declares table {table_name}, which matches "
                        f"{len(matches)} extracted tables; no link was created.",
                        sorted(set(node["evidence_ids"])),
                    )
                )
                limitations += 1
                continue
            if not matches:
                continue
            table = matches[0]
            evidence_ids = sorted(set(node["evidence_ids"]) | set(table["evidence_ids"]))
            result.relationships.append(
                {
                    "id": relationship_id("maps_to", node["id"], table["id"]),
                    "type": "maps_to",
                    "source": node["id"],
                    "target": table["id"],
                    "classification": CLASSIFICATION_INFERRED,
                    "confidence": INFERRED_NAME_MATCH_CONFIDENCE,
                    "evidence_ids": evidence_ids,
                    "attributes": {"basis": NAME_MATCH_BASIS, "table": table["name"]},
                }
            )
            text = (
                f"Entity {node['attributes'].get('qualified_name', node['name'])} is mapped to table {table['name']} "
                "(inferred from the matching declared table name)."
            )
            result.claims.append(
                {
                    "id": claim_id(text, evidence_ids),
                    "text": text,
                    "classification": CLASSIFICATION_INFERRED,
                    "confidence": INFERRED_NAME_MATCH_CONFIDENCE,
                    "evidence_ids": evidence_ids,
                }
            )
            linked += 1
    finally:
        context.analyzer_name = previous_analyzer
    entry: dict[str, Any] = {
        "name": ORM_METADATA_ANALYZER_NAME,
        "version": ORM_METADATA_ANALYZER_VERSION,
        "detected": bool(declared),
    }
    if entry["detected"]:
        entry["nodes"] = 0
        entry["limitations"] = limitations
        entry["relationships"] = linked
    return entry
