"""Mermaid ERD generation, rendering validation, diagnostics, and ownership."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import invoke, read_ir, read_manifest
from repodocs.erd import (
    build_erd_model,
    normalize_type,
    render_erd,
    sanitize_entity_id,
    sanitize_name,
)
from repodocs.errors import OwnershipError, ValidationError
from repodocs.validation import validate_mermaid
from test_schemas import assert_valid


POSTGRES_FIXTURE = Path(__file__).parent / "fixtures" / "postgres-repo"
ORM_FIXTURE = Path(__file__).parent / "fixtures" / "postgres-orm-repo"
BASIC_FIXTURE = Path(__file__).parent / "fixtures" / "basic-repo"


def postgres_project(tmp_path: Path, capsys) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "database"
    shutil.copytree(POSTGRES_FIXTURE, repository)
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    assert invoke(root, capsys, "add", "database", "--id", "database", "--destination")[0] == 0
    return root, repository


def test_erd_generation_from_postgresql_tables(tmp_path: Path, capsys) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0

    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["changed"] is True
    assert "erd.mmd" in payload["data"]["artifacts"]
    assert invoke(root, capsys, "validate")[0] == 0

    erd_path = repository / "docs" / "generated" / "erd.mmd"
    assert erd_path.exists()
    content = erd_path.read_text(encoding="utf-8")

    assert content.startswith("erDiagram\n")

    # Check entities
    assert "audit_entries {" in content
    assert 'billing_invoices["billing.invoices"] {' in content
    assert "customers {" in content
    assert "events {" in content
    assert "events_2024 {" in content
    assert "order_items {" in content
    assert "orders {" in content

    # Check attribute types and keys
    assert "bigserial id PK" in content
    assert "varchar(255) email_address UK" in content
    assert "bigint order_id PK, FK" in content
    assert "integer line_no PK" in content
    assert "bigint order_id UK, FK" in content
    assert "numeric(12,2) unit_price" in content
    assert "numeric(14,2) total_amount" in content

    # Check relationships
    assert 'customers ||..o{ orders : "customer_id"' in content
    assert 'orders ||--o{ order_items : "order_id"' in content
    assert 'orders |o..o| billing_invoices : "order_id"' in content

    # Check diagnostic comments
    assert "%% Reported limitations and diagnostics:" in content
    assert "%% - Missing key: Table `events` defines no primary key." in content
    assert "%% - Missing key: Table `events_2024` defines no primary key." in content
    assert (
        "%% - Unsupported relationship: Table `orders` foreign key (warehouse_id) references `warehouses`"
        in content
    )

    # Check manifest
    manifest = read_manifest(root)
    assert_valid("artifact-manifest-1.0.json", manifest)
    erd_entry = next(entry for entry in manifest["artifacts"] if entry["path"] == "erd.mmd")
    assert erd_entry["generator"] == "repodocs-deterministic"
    assert erd_entry["generator_version"] == "1.0"


def test_erd_is_deterministic(tmp_path: Path, capsys) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0

    assert invoke(root, capsys, "generate")[0] == 0
    erd_path = repository / "docs" / "generated" / "erd.mmd"
    content_first = erd_path.read_bytes()

    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["changed"] is False
    content_second = erd_path.read_bytes()

    assert content_first == content_second


def test_erd_omitted_when_no_tables(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "app"
    shutil.copytree(BASIC_FIXTURE, repository)
    assert invoke(root, capsys, "init", "BasicApp")[0] == 0
    assert invoke(root, capsys, "add", "app", "--id", "app", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert "erd.mmd" not in payload["data"]["artifacts"]

    manifest = read_manifest(root)
    paths = {item["path"] for item in manifest["artifacts"]}
    assert "erd.mmd" not in paths
    assert not (repository / "docs" / "generated" / "erd.mmd").exists()


def test_erd_safe_retirement(tmp_path: Path, capsys) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    erd_path = repository / "docs" / "generated" / "erd.mmd"
    assert erd_path.exists()

    # Remove migrations so no tables are detected
    shutil.rmtree(repository / "db")
    assert invoke(root, capsys, "scan")[0] == 0

    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["changed"] is True
    assert "erd.mmd" not in payload["data"]["artifacts"]
    assert not erd_path.exists()

    assert invoke(root, capsys, "validate")[0] == 0


def test_erd_ownership_protection(tmp_path: Path, capsys) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0
    erd_path = repository / "docs" / "generated" / "erd.mmd"

    # Human edits generated file
    erd_path.write_text("erDiagram\n%% manual edit\n", encoding="utf-8")

    code, _ = invoke(root, capsys, "validate")
    assert code != 0


def test_validate_mermaid_er_diagram_valid() -> None:
    valid_erd = (
        "erDiagram\n"
        "    %% Comment line\n"
        "    customers {\n"
        "        bigserial id PK\n"
        '        varchar(255) email UK "unique email"\n'
        "    }\n"
        '    billing_invoices["billing.invoices"] {\n'
        "        uuid id PK\n"
        "        bigint order_id UK, FK\n"
        "    }\n"
        "    orders {\n"
        "        bigint id PK\n"
        "        bigint customer_id FK\n"
        "    }\n"
        '    customers ||..o{ orders : "customer_id"\n'
        '    orders ||..o| billing_invoices : "order_id"\n'
    )
    validate_mermaid(valid_erd)


def test_validate_mermaid_er_diagram_errors() -> None:
    # Empty
    with pytest.raises(ValidationError, match="cannot be empty"):
        validate_mermaid("")

    # Unknown header
    with pytest.raises(ValidationError, match="Unsupported Mermaid diagram header"):
        validate_mermaid("classDiagram\n    class Foo\n")

    # Undefined entity in relationship
    with pytest.raises(ValidationError, match="references undefined entities: missing_tbl"):
        validate_mermaid(
            "erDiagram\n"
            "    tbl1 {\n"
            "        int id PK\n"
            "    }\n"
            '    tbl1 ||--o{ missing_tbl : "rel"\n'
        )

    # Malformed cardinality
    with pytest.raises(ValidationError, match="Unsupported or malformed Mermaid ERD line"):
        validate_mermaid(
            "erDiagram\n"
            "    tbl1 {\n"
            "        int id PK\n"
            "    }\n"
            "    tbl2 {\n"
            "        int id PK\n"
            "    }\n"
            '    tbl1 xx--yy tbl2 : "rel"\n'
        )

    # Malformed attribute
    with pytest.raises(ValidationError, match="Unsupported or malformed Mermaid ERD attribute line"):
        validate_mermaid(
            "erDiagram\n"
            "    tbl1 {\n"
            "        123bad attribute name\n"
            "    }\n"
        )

    # Unclosed block
    with pytest.raises(ValidationError, match="Unclosed entity block"):
        validate_mermaid(
            "erDiagram\n"
            "    tbl1 {\n"
            "        int id PK\n"
        )


def test_markdown_reports_schema_diagnostics(tmp_path: Path, capsys) -> None:
    root, repository = postgres_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    assert invoke(root, capsys, "generate")[0] == 0

    markdown = (repository / "docs" / "generated" / "architecture.md").read_text(encoding="utf-8")
    assert "### Schema diagnostics" in markdown
    assert "- **Missing key**: Table `events` defines no primary key." in markdown
    assert "- **Missing key**: Table `events_2024` defines no primary key." in markdown
    assert (
        "- **Unsupported relationship**: Table `orders` foreign key (warehouse_id) references `warehouses`"
        in markdown
    )


def test_type_and_name_normalization() -> None:
    assert normalize_type("NUMERIC(14, 2)") == "numeric(14,2)"
    assert normalize_type("CHARACTER VARYING(255)") == "varchar(255)"
    assert normalize_type("DOUBLE PRECISION") == "double_precision"
    assert normalize_type("TIMESTAMP WITH TIME ZONE") == "timestamptz"
    assert normalize_type("TIMESTAMP WITHOUT TIME ZONE") == "timestamp"
    assert normalize_type("TEXT[]") == "text[]"
    assert normalize_type("") == "unknown"
    assert normalize_type("123bad") == "t_123bad"

    assert sanitize_name("id") == "id"
    assert sanitize_name('"IssuedAt"') == "IssuedAt"
    assert sanitize_name("user-name") == "user_name"
    assert sanitize_name("1col") == "col_1col"

    assert sanitize_entity_id("orders") == "orders"
    assert sanitize_entity_id("billing.invoices") == "billing_invoices"
    assert sanitize_entity_id("1table") == "tbl_1table"


def test_self_referencing_relationship_erd(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repo = root / "database"
    repo.mkdir()
    (repo / "pom.xml").write_text(
        '<project><dependencies><dependency><groupId>org.postgresql</groupId>'
        '<artifactId>postgresql</artifactId></dependency></dependencies></project>',
        encoding="utf-8",
    )
    migrations = repo / "db" / "migration"
    migrations.mkdir(parents=True)
    (migrations / "V1__org.sql").write_text(
        "CREATE TABLE employees (\n"
        "    id BIGINT PRIMARY KEY,\n"
        "    manager_id BIGINT REFERENCES employees(id),\n"
        "    name TEXT NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    assert invoke(root, capsys, "init", "OrgApp")[0] == 0
    assert invoke(root, capsys, "add", "database", "--id", "database", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert "erd.mmd" in payload["data"]["artifacts"]

    erd_content = (repo / "docs" / "generated" / "erd.mmd").read_text(encoding="utf-8")
    assert 'employees |o..o{ employees : "manager_id"' in erd_content
    assert invoke(root, capsys, "validate")[0] == 0


def test_orm_entities_erd(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repo = root / "store"
    shutil.copytree(ORM_FIXTURE, repo)
    assert invoke(root, capsys, "init", "Store")[0] == 0
    assert invoke(root, capsys, "add", "store", "--id", "store", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0

    code, payload = invoke(root, capsys, "generate")
    assert code == 0
    assert "erd.mmd" in payload["data"]["artifacts"]

    erd_content = (repo / "docs" / "generated" / "erd.mmd").read_text(encoding="utf-8")
    assert "customers {" in erd_content
    assert "orders {" in erd_content
    assert 'customers ||..o{ orders : "customer_id"' in erd_content
    assert invoke(root, capsys, "validate")[0] == 0
