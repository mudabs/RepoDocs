"""PostgreSQL adapter: detection, extraction, evidence, limitations, determinism."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import copy_fixture, invoke, read_ir
from test_schemas import assert_valid


POSTGRES_FIXTURE = Path(__file__).parent / "fixtures" / "postgres-repo"
MIGRATION_FIXTURE = Path(__file__).parent / "fixtures" / "postgres-migration-repo"
ORM_FIXTURE = Path(__file__).parent / "fixtures" / "postgres-orm-repo"


@pytest.mark.parametrize("statement, label", [
    ("CREATE VIEW standalone AS SELECT 1;", "CREATE VIEW"),
    ("CREATE FUNCTION standalone() RETURNS void AS $$ BEGIN RETURN; END; $$ LANGUAGE plpgsql;", "CREATE FUNCTION"),
    ("CREATE TRIGGER standalone BEFORE INSERT ON orders EXECUTE FUNCTION f();", "CREATE TRIGGER"),
    ("DO $$ BEGIN RAISE NOTICE 'not executed'; END; $$;", "DO block"),
])
def test_standalone_unsupported_migrations_are_reported(tmp_path: Path, capsys, statement: str, label: str) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    path = "db/migration/V6__unsupported.sql"
    (repository / path).write_text(statement, encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)
    evidence = {entry["id"]: entry for entry in ir["evidence"]}
    limitations = [
        entry for entry in ir["limitations"]
        if label in entry["message"] and any(evidence[key]["path"] == path for key in entry["evidence_ids"])
    ]
    assert len(limitations) == 1
    assert limitations[0]["kind"] == "unsupported-construct"
    span = evidence[limitations[0]["evidence_ids"][0]]
    assert span["kind"] == "span" and span["line_start"] == 1
    assert span["revision"] == ir["snapshots"][0]["revision"]
    assert_valid("system-ir-1.0.json", ir)


@pytest.mark.parametrize("drop, remaining, ambiguous", [
    ("DROP INDEX billing.shared;", ["audit"], False),
    ("DROP INDEX shared;", ["audit", "billing"], True),
    ("DROP INDEX billing.shared, audit.shared;", [], False),
])
def test_index_identity_is_scoped_to_schema(tmp_path: Path, capsys, drop: str, remaining: list[str], ambiguous: bool) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    migrations = repository / "db" / "migration"
    (migrations / "V6__indexes.sql").write_text(
        "CREATE TABLE billing.accounts (id BIGSERIAL PRIMARY KEY);\n"
        "CREATE TABLE audit.accounts (id BIGSERIAL PRIMARY KEY);\n"
        "CREATE INDEX shared ON billing.accounts (id);\n"
        "CREATE INDEX IF NOT EXISTS shared ON audit.accounts (id);\n"
        # A duplicate in one schema must preserve that schema's original index.
        "CREATE UNIQUE INDEX IF NOT EXISTS shared ON audit.accounts (id);\n",
        encoding="utf-8",
    )
    (migrations / "V7__drop_index.sql").write_text(drop, encoding="utf-8")
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)
    tables = [node for node in nodes_of(ir, "table") if node["name"] in ("billing.accounts", "audit.accounts")]
    assert sorted(node["attributes"]["schema"] for node in tables if node["attributes"]["indexes"]) == remaining
    for node in tables:
        for index in node["attributes"]["indexes"]:
            assert index == {"name": "shared", "columns": ["id"], "unique": False}
    assert any("ambiguous across schemas" in entry["message"] for entry in ir["limitations"]) == ambiguous
    assert_valid("system-ir-1.0.json", ir)


def postgres_project(tmp_path: Path, capsys) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "database"
    shutil.copytree(POSTGRES_FIXTURE, repository)
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    assert invoke(root, capsys, "add", "database", "--id", "database", "--destination")[0] == 0
    return root, repository


def migration_project(tmp_path: Path, capsys) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "database"
    shutil.copytree(MIGRATION_FIXTURE, repository)
    assert invoke(root, capsys, "init", "Migrations")[0] == 0
    assert invoke(root, capsys, "add", "database", "--id", "database", "--destination")[0] == 0
    return root, repository


def orm_project(tmp_path: Path, capsys) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "store"
    shutil.copytree(ORM_FIXTURE, repository)
    assert invoke(root, capsys, "init", "Store")[0] == 0
    assert invoke(root, capsys, "add", "store", "--id", "store", "--destination")[0] == 0
    return root, repository


def nodes_of(ir: dict, node_type: str, **attributes) -> list[dict]:
    return [
        node
        for node in ir["nodes"]
        if node["type"] == node_type and all(node["attributes"].get(key) == value for key, value in attributes.items())
    ]


def table_of(ir: dict, name: str) -> dict:
    (table,) = nodes_of(ir, "table", table=name.split(".")[-1]) if "." not in name else (
        node for node in nodes_of(ir, "table") if node["name"] == name
    )
    return table


def relationships_of(ir: dict, relationship_type: str) -> list[tuple[str, str, dict]]:
    names = {node["id"]: node["name"] for node in ir["nodes"]}
    return sorted(
        (names[item["source"]], names[item["target"]], item.get("attributes", {}))
        for item in ir["relationships"]
        if item["type"] == relationship_type
    )


def evidence_of(ir: dict, node: dict, index: int = 0) -> dict:
    evidence = {item["id"]: item for item in ir["evidence"]}
    return evidence[node["evidence_ids"][index]]


def test_detects_postgres_and_extracts_tables_with_evidence(tmp_path: Path, capsys) -> None:
    root, _ = postgres_project(tmp_path, capsys)
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    analyzers = {item["name"]: item for item in payload["data"]["analyzers"]}
    assert analyzers["postgresql"]["repositories"] == ["database"]
    assert analyzers["postgresql"]["version"] == "1.0"

    ir = read_ir(root)
    (database,) = nodes_of(ir, "database")
    assert database["name"] == "database database"
    assert database["attributes"]["engine"] == "postgresql"
    assert database["attributes"]["migration_tool"] == "flyway"
    assert database["attributes"]["table_count"] == 7
    assert database["attributes"]["schemas"] == ["billing"]
    assert database["attributes"]["extensions"] == ["uuid-ossp"]
    assert database["attributes"]["enum_types"] == {"order_status": ["OPEN", "COMPLETED", "ARCHIVED"]}
    assert "timestamptz" in database["attributes"]["dialect_markers"]
    assert database["attributes"]["data_statements_skipped"] == 1

    tables = {node["name"]: node for node in nodes_of(ir, "table")}
    assert sorted(tables) == [
        "audit_entries",
        "billing.invoices",
        "customers",
        "events",
        "events_2024",
        "order_items",
        "orders",
    ]
    customers = tables["customers"]
    assert customers["attributes"]["primary_key"] == ["id"]
    columns = {column["name"]: column for column in customers["attributes"]["columns"]}
    assert columns["id"]["type"] == "BIGSERIAL"
    assert columns["id"]["primary_key"] is True
    assert columns["email_address"]["type"] == "VARCHAR(255)"
    assert columns["email_address"]["nullable"] is False
    assert columns["email_address"]["unique"] is True
    assert columns["created_at"]["default"] == "now()"

    spans = [evidence_of(ir, customers, index) for index in range(len(customers["evidence_ids"]))]
    declaration = next(item for item in spans if item["path"] == "db/migration/V1__init.sql")
    assert declaration["kind"] == "span"
    assert (declaration["line_start"], declaration["line_end"]) == (7, 11)
    assert declaration["analyzer"] == "postgresql"
    assert declaration["revision"] == ir["snapshots"][0]["revision"]
    # The table also carries the evidence of the later migration that changed it.
    assert any(item["path"] == "db/migration/V3__evolve.sql" for item in spans)


def test_primary_and_foreign_keys_become_edges(tmp_path: Path, capsys) -> None:
    root, _ = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    orders = table_of(ir, "orders")
    assert orders["attributes"]["primary_key"] == ["id"]
    (foreign_key,) = [item for item in orders["attributes"]["foreign_keys"] if item["references_table"] == "customers"]
    assert foreign_key["columns"] == ["customer_id"]
    assert foreign_key["references_columns"] == ["id"]
    assert foreign_key["on_delete"] == "CASCADE"
    assert foreign_key["on_update"] is None

    items = table_of(ir, "order_items")
    assert items["attributes"]["primary_key"] == ["order_id", "line_no"]
    (foreign_key,) = items["attributes"]["foreign_keys"]
    assert foreign_key["name"] == "fk_items_order"
    assert foreign_key["on_delete"] == "CASCADE"
    assert foreign_key["on_update"] == "NO ACTION"

    invoices = table_of(ir, "billing.invoices")
    assert invoices["attributes"]["schema"] == "billing"
    (foreign_key,) = invoices["attributes"]["foreign_keys"]
    assert foreign_key["references_table"] == "orders"
    assert foreign_key["references_columns"] == []
    assert foreign_key["implicit_target_columns"] is True

    references = relationships_of(ir, "references")
    assert [(source, target) for source, target, _ in references] == [
        ("billing.invoices", "orders"),
        ("order_items", "orders"),
        ("orders", "customers"),
    ]
    assert all(
        item["classification"] == "observed" and item["confidence"] == 1.0
        for item in ir["relationships"]
        if item["type"] == "references"
    )
    evidence = {item["id"]: item for item in ir["evidence"]}
    edge = next(item for item in ir["relationships"] if item["type"] == "references")
    span = evidence[edge["evidence_ids"][0]]
    assert span["kind"] == "span"
    assert span["line_start"] >= 1


def test_migrations_are_applied_in_order(tmp_path: Path, capsys) -> None:
    root, _ = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    orders = table_of(ir, "orders")
    columns = {column["name"]: column for column in orders["attributes"]["columns"]}
    # Added, altered, and renamed by the third migration.
    assert "shipped_at" in columns
    assert "notes" not in columns
    assert columns["comment"]["type"] == "TEXT"
    # The column was made NOT NULL before it was renamed.
    assert columns["comment"]["nullable"] is False
    assert columns["total_amount"]["type"] == "NUMERIC(14, 2)"
    assert columns["id"]["identity"] is True
    assert sorted(orders["attributes"]["defined_in"]) == [
        "db/migration/V1__init.sql",
        "db/migration/V3__evolve.sql",
    ]
    # The table created and dropped in the same migration is not part of the schema.
    assert "temp_stuff" not in {node["name"] for node in nodes_of(ir, "table")}
    # The renamed table keeps the columns it was created with.
    audit = table_of(ir, "audit_entries")
    assert [column["name"] for column in audit["attributes"]["columns"]] == ["id", "message"]
    # The partition inherits the columns of its parent.
    events = table_of(ir, "events_2024")
    assert [column["name"] for column in events["attributes"]["columns"]] == ["occurred_at", "kind", "body"]
    assert events["attributes"]["partition_of"] == "events"


def test_unsupported_constructs_are_reported_as_limitations(tmp_path: Path, capsys) -> None:
    root, _ = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    by_kind: dict[str, list[str]] = {}
    for limitation in ir["limitations"]:
        assert limitation["analyzer"] in {"postgresql", "orm-metadata"}
        assert limitation["repository_id"] == "database"
        assert limitation["evidence_ids"]
        by_kind.setdefault(limitation["kind"], []).append(limitation["message"])
    assert sorted(by_kind) == ["ambiguous-reference", "unsupported-construct"]
    assert any("CREATE FUNCTION is not modeled" in message for message in by_kind["unsupported-construct"])
    assert any("CREATE TRIGGER is not modeled" in message for message in by_kind["unsupported-construct"])
    assert any("CREATE VIEW is not modeled" in message for message in by_kind["unsupported-construct"])
    assert any("action SET" in message for message in by_kind["unsupported-construct"])
    assert any("Liquibase changelog" in message for message in by_kind["unsupported-construct"])
    assert any("Flyway undo migration" in message for message in by_kind["unsupported-construct"])
    assert any("partitioned parent" in message for message in by_kind["unsupported-construct"])
    assert any("missing_table" in message for message in by_kind["ambiguous-reference"])
    assert any("warehouses" in message for message in by_kind["ambiguous-reference"])

    # A foreign key to a table that is not defined is never turned into an edge.
    assert ("orders", "warehouses") not in [(source, target) for source, target, _ in relationships_of(ir, "references")]
    # The comment that mentions a table name does not create one.
    assert "ignored_table" not in {node["name"] for node in nodes_of(ir, "table")}
    # The report query file holds no schema and is not analyzed.
    assert "scripts/report.sql" not in database_of(ir)["attributes"]["sql_files"]


def database_of(ir: dict) -> dict:
    (database,) = nodes_of(ir, "database")
    return database


def test_orm_metadata_is_linked_as_an_inferred_relationship(tmp_path: Path, capsys) -> None:
    root, _ = orm_project(tmp_path, capsys)
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    ir = read_ir(root)

    (edge,) = [item for item in ir["relationships"] if item["type"] == "maps_to"]
    names = {node["id"]: node["name"] for node in ir["nodes"]}
    assert names[edge["source"]] == "Order"
    assert names[edge["target"]] == "orders"
    assert edge["classification"] == "inferred"
    assert edge["confidence"] == 0.8
    assert edge["attributes"]["basis"] == "jpa-table-name"
    assert len(edge["evidence_ids"]) == 2

    # An entity without a declared table name and a table without an entity stay unlinked.
    assert len([item for item in ir["relationships"] if item["type"] == "maps_to"]) == 1
    inferred_claims = [item for item in ir["claims"] if item["classification"] == "inferred"]
    assert len(inferred_claims) == 1
    assert "com.smartcart.order.Order" in inferred_claims[0]["text"]

    analyzers = {item["name"]: item for item in payload["data"]["analyzers"]}
    assert analyzers["orm-metadata"]["repositories"] == ["store"]
    assert analyzers["postgresql"]["repositories"] == ["store"]

    # The declared driver is recorded, and the database node cites the manifest.
    (database,) = nodes_of(ir, "database")
    assert database["attributes"]["driver"] == {"manifest": "pom.xml", "dependency": "org.postgresql"}
    evidence = {item["id"]: item for item in ir["evidence"]}
    manifest = [evidence[item] for item in database["evidence_ids"] if evidence[item]["path"] == "pom.xml"]
    assert len(manifest) == 1
    assert manifest[0]["kind"] == "file"
    assert_valid("system-ir-1.0.json", ir)


def test_scan_is_deterministic_schema_valid_and_renders(tmp_path: Path, capsys) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    first = read_ir(root)
    assert invoke(root, capsys, "scan")[0] == 0
    assert read_ir(root) == first
    assert_valid("system-ir-1.0.json", first)
    assert first["provenance"]["analyzers"] == [
        {"name": "basic-manifest", "repositories": ["database"], "version": "1.0"},
        {"name": "orm-metadata", "repositories": [], "version": "1.0"},
        {"name": "postgresql", "repositories": ["database"], "version": "1.0"},
        {"name": "react", "repositories": [], "version": "1.0"},
        {"name": "spring-boot", "repositories": [], "version": "1.0"},
    ]

    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["changed"] is True
    assert invoke(root, capsys, "validate")[0] == 0

    markdown = (repository / "docs" / "generated" / "architecture.md").read_text(encoding="utf-8")
    assert "## Database schema" in markdown
    assert "| database database | `orders` | not qualified | 7 | id | customers (customer_id)" in markdown
    assert "| `orders` | `customer_id` | `BIGINT` | no | - |  | database |" in markdown
    assert "| `order_items` | `order_id` | `BIGINT` | no | - | primary key | database |" in markdown
    assert "## Inferred relationships" in markdown
    assert "## Reported limitations" in markdown
    assert "`unsupported-construct` (postgresql)" in markdown

    mermaid = (repository / "docs" / "generated" / "architecture.mmd").read_text(encoding="utf-8")
    assert "database database (PostgreSQL): 7 tables, flyway" in mermaid


def test_repeated_table_definition_does_not_erase_the_schema(tmp_path: Path, capsys) -> None:
    """A later `CREATE TABLE IF NOT EXISTS` must not drop what was already extracted."""

    root, _ = migration_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    orders = table_of(ir, "orders")
    assert [column["name"] for column in orders["attributes"]["columns"]] == ["id", "customer"]
    assert orders["attributes"]["primary_key"] == ["id"]
    assert [index["name"] for index in orders["attributes"]["indexes"]] == ["idx_orders_customer"]
    messages = [item["message"] for item in ir["limitations"]]
    assert any("table orders is defined more than once" in message for message in messages)
    assert any("PostgreSQL keeps the existing definition" in message for message in messages)


def test_repeated_column_and_index_do_not_duplicate_state(tmp_path: Path, capsys) -> None:
    """`IF NOT EXISTS` keeps what is already there instead of adding it twice."""

    root, _ = migration_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    orders = table_of(ir, "orders")
    assert [column["name"] for column in orders["attributes"]["columns"]] == ["id", "customer"]
    assert [index["name"] for index in orders["attributes"]["indexes"]] == ["idx_orders_customer"]
    messages = [item["message"] for item in ir["limitations"]]
    assert any("column customer already exists" in message for message in messages)
    assert any("index idx_orders_customer already exists" in message for message in messages)


def test_standalone_schema_migrations_are_applied(tmp_path: Path, capsys) -> None:
    """A migration that only changes the schema around the tables still counts."""

    root, _ = migration_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    # The table the standalone migration drops is gone.
    assert "legacy_orders" not in {node["name"] for node in nodes_of(ir, "table")}
    # The index the standalone migration adds is there, and the one it drops is not.
    customers = table_of(ir, "customers")
    assert [index["name"] for index in customers["attributes"]["indexes"]] == ["idx_customers_email"]
    # The schema and the extension the standalone migration declares are recorded.
    database = database_of(ir)
    assert database["attributes"]["schemas"] == ["reporting"]
    assert database["attributes"]["extensions"] == ["pgcrypto"]
    assert database["attributes"]["sql_files"] == [
        "db/migration/V1__init.sql",
        "db/migration/V2__evolve.sql",
        "db/migration/V3__standalone.sql",
        "db/migration/V4__repeated.sql",
        "db/migration/V5__implicit_reference.sql",
    ]

def test_drop_only_migration_removes_the_last_table(tmp_path: Path, capsys) -> None:
    root, repository = migration_project(tmp_path, capsys)
    migrations = repository / "db" / "migration"
    (migrations / "V6__drop.sql").write_text(
        "DROP TABLE customers, orders, implicit_child, implicit_parent CASCADE;\n",
        encoding="utf-8",
    )
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)
    assert nodes_of(ir, "table") == []
    assert database_of(ir)["attributes"]["table_count"] == 0
    assert not any(item["type"] == "references" for item in ir["relationships"])
    assert_valid("system-ir-1.0.json", ir)


def test_implicit_reference_goes_with_the_dropped_primary_key(tmp_path: Path, capsys) -> None:
    """A foreign key that names no column depends on the primary key of its target."""

    root, _ = migration_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    parent = table_of(ir, "implicit_parent")
    assert parent["attributes"]["primary_key"] == []
    child = table_of(ir, "implicit_child")
    assert child["attributes"]["foreign_keys"] == []
    assert ("implicit_child", "implicit_parent") not in [
        (source, target) for source, target, _ in relationships_of(ir, "references")
    ]


def test_column_migrations_keep_the_rest_of_the_schema_consistent(tmp_path: Path, capsys) -> None:
    """Renaming or dropping a column updates every key, index, and reference."""

    root, _ = migration_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    customers = table_of(ir, "customers")
    assert [column["name"] for column in customers["attributes"]["columns"]] == [
        "id",
        "email_address",
        "tenant_id",
    ]
    columns = {column["name"]: column for column in customers["attributes"]["columns"]}
    # A column-level unique constraint follows the renamed column.
    assert columns["email_address"]["unique"] is True
    # A table-level unique constraint follows the renamed column, and the one on
    # the dropped column is gone with it.
    assert customers["attributes"]["unique_constraints"] == [
        {"name": "customers_tenant_uq", "columns": ["tenant_id"]}
    ]

    orders = table_of(ir, "orders")
    assert [column["name"] for column in orders["attributes"]["columns"]] == ["id", "customer"]
    (foreign_key,) = orders["attributes"]["foreign_keys"]
    assert foreign_key["columns"] == ["customer"]
    assert foreign_key["references_table"] == "customers"
    assert foreign_key["references_columns"] == ["id"]
    # The index on the dropped column is gone with it.
    assert [index["name"] for index in orders["attributes"]["indexes"]] == ["idx_orders_customer"]
    assert all("reference" not in index["columns"] for index in orders["attributes"]["indexes"])


def test_commented_out_tables_stay_commented_out(tmp_path: Path, capsys) -> None:
    """PostgreSQL block comments nest, so a hidden table is not extracted."""

    root, _ = migration_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    assert "commented_out" not in {node["name"] for node in nodes_of(ir, "table")}
    assert sorted(node["name"] for node in nodes_of(ir, "table")) == [
        "customers",
        "implicit_child",
        "implicit_parent",
        "orders",
    ]


def test_portable_sql_without_postgres_evidence_is_not_analyzed(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    copy_fixture(root / "backend")
    assert invoke(root, capsys, "init", "Plain")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--id", "backend", "--destination")[0] == 0
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    ir = read_ir(root)
    analyzers = {item["name"]: item for item in payload["data"]["analyzers"]}
    assert analyzers["postgresql"]["repositories"] == []
    assert nodes_of(ir, "database") == []
    assert nodes_of(ir, "table") == []
    assert ir["limitations"] == []


def test_postgres_driver_dependency_detects_postgresql(tmp_path: Path, capsys) -> None:
    """A declared PostgreSQL driver is enough to treat portable SQL as PostgreSQL."""

    root = tmp_path / "project"
    root.mkdir()
    repository = root / "service"
    repository.mkdir()
    (repository / "package.json").write_text(
        json.dumps({"name": "store", "dependencies": {"pg": "^8.11.0"}}), encoding="utf-8"
    )
    (repository / "schema.sql").write_text(
        "CREATE TABLE orders (\n    id INTEGER PRIMARY KEY\n);\n", encoding="utf-8"
    )
    assert invoke(root, capsys, "init", "Driver")[0] == 0
    assert invoke(root, capsys, "add", "service", "--id", "service", "--destination")[0] == 0
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    ir = read_ir(root)
    analyzers = {item["name"]: item for item in payload["data"]["analyzers"]}
    assert analyzers["postgresql"]["repositories"] == ["service"]
    (database,) = nodes_of(ir, "database")
    assert database["attributes"]["driver"] == {"manifest": "package.json", "dependency": "pg"}
    (table,) = nodes_of(ir, "table")
    assert table["name"] == "orders"
    assert table["attributes"]["primary_key"] == ["id"]


def test_unreadable_sql_file_is_reported(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "database"
    repository.mkdir()
    (repository / "schema.sql").write_bytes(b"CREATE TABLE t (id INTEGER); -- \xff\xfe not utf-8\n")
    assert invoke(root, capsys, "init", "Broken")[0] == 0
    assert invoke(root, capsys, "add", "database", "--id", "database", "--destination")[0] == 0
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    ir = read_ir(root)
    assert [item["kind"] for item in ir["limitations"]] == ["unreadable-source"]
    assert ir["limitations"][0]["message"] == (
        "schema.sql could not be decoded as UTF-8 or changed during the scan."
    )
    assert nodes_of(ir, "database") == []
    assert nodes_of(ir, "table") == []
