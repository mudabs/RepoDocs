"""Spring Boot adapter: detection, extraction, evidence, limitations, determinism."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import copy_fixture, invoke, read_ir
from test_schemas import assert_valid


SPRING_FIXTURE = Path(__file__).parent / "fixtures" / "spring-boot-repo"


def spring_project(tmp_path: Path, capsys) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "backend"
    shutil.copytree(SPRING_FIXTURE, repository)
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--id", "backend", "--destination")[0] == 0
    return root, repository


def nodes_of(ir: dict, node_type: str, **attributes) -> list[dict]:
    return [
        node
        for node in ir["nodes"]
        if node["type"] == node_type and all(node["attributes"].get(key) == value for key, value in attributes.items())
    ]


def relationships_of(ir: dict, relationship_type: str) -> list[tuple[str, str, dict]]:
    names = {node["id"]: node["name"] for node in ir["nodes"]}
    return sorted(
        (names[item["source"]], names[item["target"]], item.get("attributes", {}))
        for item in ir["relationships"]
        if item["type"] == relationship_type
    )


def line_of(repository: Path, relative: str, needle: str) -> int:
    lines = (repository / relative).read_text(encoding="utf-8").splitlines()
    return next(index for index, line in enumerate(lines, start=1) if line.strip() == needle)


def test_detects_spring_boot_from_pom_and_creates_service(tmp_path: Path, capsys) -> None:
    root, _ = spring_project(tmp_path, capsys)
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    analyzers = {item["name"]: item for item in payload["data"]["analyzers"]}
    assert analyzers["spring-boot"]["repositories"] == ["backend"]
    assert analyzers["basic-manifest"]["repositories"] == ["backend"]
    assert payload["data"]["limitations"] > 0

    ir = read_ir(root)
    (framework,) = nodes_of(ir, "framework")
    assert framework["name"] == "Spring Boot"
    assert framework["attributes"]["version"] == "3.2.5"
    assert framework["attributes"]["build_tool"] == "maven"
    assert framework["attributes"]["manifests"] == ["pom.xml"]

    (service,) = nodes_of(ir, "service")
    assert service["name"] == "smartcart-backend"
    assert service["attributes"]["application_class"] == "com.smartcart.SmartCartApplication"
    assert service["attributes"]["base_package"] == "com.smartcart"
    contains = relationships_of(ir, "contains")
    assert ("backend", "smartcart-backend", {}) in contains
    assert ("smartcart-backend", "OrderController", {}) in contains
    assert ("smartcart-backend", "src/main/resources/application.yml", {}) in contains

    stereotypes = {node["name"]: node["attributes"]["stereotype"] for node in nodes_of(ir, "class")}
    assert stereotypes == {
        "SmartCartApplication": "application",
        "OrderController": "controller",
        "CustomerController": "controller",
        "OrderService": "service",
        "OrderRepository": "repository",
        "CustomerRepository": "repository",
        "Order": "entity",
        "OrderItem": "entity",
        "Customer": "entity",
        "SecurityConfig": "configuration",
        "FunctionalRoutes": "configuration",
        "PluginLoader": "component",
    }
    # Test sources, Kotlin sources, and plain DTOs do not become classes.
    assert "TestOnlyController" not in stereotypes
    assert "ExtrasController" not in stereotypes
    assert "OrderSummary" not in stereotypes


def test_endpoints_have_literal_paths_and_line_evidence(tmp_path: Path, capsys) -> None:
    root, repository = spring_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    endpoints = {node["name"]: node for node in nodes_of(ir, "endpoint")}
    assert sorted(endpoints) == [
        "GET /api/customers/{id}",
        "GET /api/orders",
        "GET /api/orders/{id}",
        "GET /api/v1/customers/{id}",
        "PATCH /api/orders/{id}",
        "POST /api/orders",
        "PUT /api/orders/{id}",
    ]
    assert endpoints["POST /api/orders"]["attributes"]["consumes"] == ["application/json"]
    assert endpoints["PUT /api/orders/{id}"]["attributes"]["handler"] == "update"
    assert not any("/archive" in name or "ignored" in name or "not-a-route" in name for name in endpoints)

    evidence = {item["id"]: item for item in ir["evidence"]}
    (evidence_id,) = endpoints["GET /api/orders"]["evidence_ids"]
    span = evidence[evidence_id]
    controller = "src/main/java/com/smartcart/order/OrderController.java"
    assert span["kind"] == "span"
    assert span["path"] == controller
    assert span["line_start"] == line_of(repository, controller, "@GetMapping")
    assert span["line_end"] == line_of(repository, controller, "public List<Order> list(@RequestParam(required = false) String status) {")
    assert span["symbol"] == "com.smartcart.order.OrderController.list"
    assert span["analyzer"] == "spring-boot"
    assert span["revision"] == ir["snapshots"][0]["revision"]
    assert span["content_hash"] == next(
        item["content_hash"] for item in ir["snapshots"][0]["files"] if item["path"] == controller
    )

    exposes = relationships_of(ir, "exposes")
    assert ("OrderController", "GET /api/orders", {"handler": "list"}) in exposes
    assert ("CustomerController", "GET /api/v1/customers/{id}", {"handler": "get"}) in exposes
    assert all(item["classification"] == "observed" for item in ir["relationships"])


def test_entities_repositories_and_dependencies(tmp_path: Path, capsys) -> None:
    root, _ = spring_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    (order,) = nodes_of(ir, "class", qualified_name="com.smartcart.order.Order")
    assert order["attributes"]["table"] == "orders"
    assert order["attributes"]["id_fields"] == ["id"]
    fields = {item["name"]: item for item in order["attributes"]["fields"]}
    assert "displayLabel" not in fields and "serialVersionUID" not in fields
    assert fields["total"]["column"] == "total_amount"
    assert fields["customer"]["association"] == "ManyToOne"
    assert fields["customer"]["join_column"] == "customer_id"
    assert fields["items"]["association"] == "OneToMany"
    assert fields["items"]["target"] == "OrderItem"
    assert fields["items"]["mapped_by"] == "order"
    (order_item,) = nodes_of(ir, "class", qualified_name="com.smartcart.order.OrderItem")
    assert order_item["attributes"]["table"] is None

    references = relationships_of(ir, "references")
    assert [(source, target, attributes["association"]) for source, target, attributes in references] == [
        ("Customer", "Order", "OneToMany"),
        ("Order", "Customer", "ManyToOne"),
        ("Order", "OrderItem", "OneToMany"),
        ("OrderItem", "Order", "ManyToOne"),
    ]

    (repository_node,) = nodes_of(ir, "class", qualified_name="com.smartcart.order.OrderRepository")
    assert repository_node["attributes"]["entity"] == "Order"
    assert repository_node["attributes"]["id_type"] == "Long"
    assert repository_node["attributes"]["query_methods"] == ["findByStatus"]
    assert repository_node["attributes"]["custom_query_methods"] == ["archiveCompleted"]

    depends_on = relationships_of(ir, "depends_on")
    assert ("OrderController", "OrderService", {"via": "constructor", "member": "orderService"}) in depends_on
    assert ("OrderService", "OrderRepository", {"via": "constructor", "member": "orderRepository"}) in depends_on
    assert ("OrderRepository", "Order", {"via": "repository-entity"}) in depends_on
    assert ("CustomerRepository", "Customer", {"via": "repository-entity"}) in depends_on
    assert ("CustomerController", "CustomerRepository", {"via": "constructor", "member": "customers"}) in depends_on
    assert ("backend", "Spring Boot", {"framework": "Spring Boot"}) in depends_on


def test_runtime_constructs_are_reported_as_limitations(tmp_path: Path, capsys) -> None:
    root, repository = spring_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    by_kind: dict[str, list[str]] = {}
    for limitation in ir["limitations"]:
        assert limitation["analyzer"] == "spring-boot"
        assert limitation["repository_id"] == "backend"
        assert limitation["evidence_ids"]
        by_kind.setdefault(limitation["kind"], []).append(limitation["message"])
    assert sorted(by_kind) == [
        "dynamic-routing",
        "generated-code",
        "reflection",
        "runtime-configuration",
        "unsupported-construct",
        "unsupported-language",
    ]
    assert any("OrderController.archive" in message for message in by_kind["dynamic-routing"])
    assert sum("FunctionalRoutes" in message for message in by_kind["dynamic-routing"]) == 1
    assert any("OrderSummary uses Lombok" in message for message in by_kind["generated-code"])
    assert any("PluginLoader.java line 9" in message for message in by_kind["reflection"])
    assert any("SecurityConfig is annotated @Profile" in message for message in by_kind["runtime-configuration"])
    assert any("OrderService.region uses a SpEL" in message for message in by_kind["runtime-configuration"])
    assert any("archiveCompleted declares a custom @Query" in message for message in by_kind["unsupported-construct"])
    assert any("application.yml line 17: flow collection" in message for message in by_kind["unsupported-construct"])
    assert by_kind["unsupported-language"] == [
        "1 Kotlin source file(s) are not analyzed by the Spring Boot adapter, which reads Java sources only."
    ]

    evidence = {item["id"]: item for item in ir["evidence"]}
    reflection = next(item for item in ir["limitations"] if item["kind"] == "reflection")
    span = evidence[reflection["evidence_ids"][0]]
    assert span["path"] == "src/main/java/com/smartcart/util/PluginLoader.java"
    assert span["line_start"] == line_of(repository, span["path"], "Class<?> type = Class.forName(className);")


def test_configuration_records_keys_but_never_values(tmp_path: Path, capsys) -> None:
    root, _ = spring_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    configurations = {node["name"]: node for node in nodes_of(ir, "configuration")}
    assert sorted(configurations) == [
        "src/main/resources/application-prod.properties",
        "src/main/resources/application.yml",
    ]
    yaml = configurations["src/main/resources/application.yml"]["attributes"]
    assert yaml["format"] == "yaml"
    assert yaml["profile"] is None
    assert yaml["values_recorded"] is False
    assert "spring.datasource.password" in yaml["keys"]
    assert "spring.config.activate.on-profile" in yaml["keys"]
    assert "smartcart.features" in yaml["keys"]
    properties = configurations["src/main/resources/application-prod.properties"]["attributes"]
    assert properties["profile"] == "prod"
    assert properties["keys"] == ["server.port", "smartcart.payments.api-key", "spring.datasource.password"]

    (service,) = nodes_of(ir, "class", qualified_name="com.smartcart.order.OrderService")
    assert service["attributes"]["property_references"] == ["smartcart.orders.page-size"]

    serialized = json.dumps(ir)
    for secret in ("S3cretValueMustNotLeak", "ProdSecretMustNotLeak", "pk_live", "jdbc:postgresql", "db.internal"):
        assert secret not in serialized


def test_scan_is_deterministic_schema_valid_and_renders(tmp_path: Path, capsys) -> None:
    root, repository = spring_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    first = read_ir(root)
    assert invoke(root, capsys, "scan")[0] == 0
    assert read_ir(root) == first
    assert_valid("system-ir-1.0.json", first)
    assert first["provenance"]["analyzers"] == [
        {"name": "basic-manifest", "repositories": ["backend"], "version": "1.0"},
        {"name": "orm-metadata", "repositories": ["backend"], "version": "1.0"},
        {"name": "postgresql", "repositories": [], "version": "1.0"},
        {"name": "react", "repositories": [], "version": "1.0"},
        {"name": "spring-boot", "repositories": ["backend"], "version": "1.0"},
    ]

    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["changed"] is True
    assert invoke(root, capsys, "validate")[0] == 0
    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["changed"] is False

    markdown = (repository / "docs" / "generated" / "architecture.md").read_text(encoding="utf-8")
    assert "## Analysis coverage" in markdown and "`spring-boot` 1.0: backend" in markdown
    assert (
        "| smartcart-backend | backend | Spring Boot 3.2.5 | `com.smartcart.SmartCartApplication` | "
        "2 controllers, 7 endpoints, 3 entities |"
    ) in markdown
    assert "| GET | `/api/orders/{id}` | `com.smartcart.order.OrderController` | get | backend |" in markdown
    assert "| `com.smartcart.order.Order` | orders | 5 | 2 | backend |" in markdown
    assert "## Reported limitations" in markdown
    assert "`dynamic-routing` (spring-boot): com.smartcart.order.OrderController.archive" in markdown
    assert "OrderController.java:L" in markdown
    mermaid = (repository / "docs" / "generated" / "architecture.mmd").read_text(encoding="utf-8")
    assert "smartcart-backend (Spring Boot): 2 controllers, 7 endpoints, 3 entities" in mermaid


def test_repository_without_spring_boot_runs_no_adapter(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    copy_fixture(root / "backend")
    assert invoke(root, capsys, "init", "Plain")[0] == 0
    assert invoke(root, capsys, "add", "backend", "--destination")[0] == 0
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    assert payload["data"]["limitations"] == 0
    ir = read_ir(root)
    assert ir["limitations"] == []
    assert {item["name"]: item["repositories"] for item in ir["provenance"]["analyzers"]} == {
        "basic-manifest": ["backend"],
        "orm-metadata": [],
        "postgresql": [],
        "react": [],
        "spring-boot": [],
    }
    assert nodes_of(ir, "service") == [] and nodes_of(ir, "framework") == []
    # The fixture only holds portable SQL, so the PostgreSQL adapter does not run.
    assert all(item["analyzer"] == "basic-manifest" and item["kind"] == "file" for item in ir["evidence"])
    assert_valid("system-ir-1.0.json", ir)


def test_gradle_detection_and_missing_application_class(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "service"
    source = repository / "src" / "main" / "java" / "shop"
    source.mkdir(parents=True)
    (repository / "build.gradle.kts").write_text(
        'plugins {\n    id("org.springframework.boot") version "3.3.1"\n    java\n}\n'
        'dependencies {\n    implementation("org.springframework.boot:spring-boot-starter-web")\n}\n',
        encoding="utf-8",
    )
    (repository / "settings.gradle.kts").write_text('rootProject.name = "shop-service"\n', encoding="utf-8")
    (source / "PingController.java").write_text(
        "package shop;\n\nimport org.springframework.web.bind.annotation.*;\n\n"
        '@RestController\npublic class PingController {\n    @GetMapping("/ping")\n'
        '    public String ping() { return "pong"; }\n}\n',
        encoding="utf-8",
    )
    assert invoke(root, capsys, "init", "Shop")[0] == 0
    assert invoke(root, capsys, "add", "service", "--id", "service", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    (framework,) = nodes_of(ir, "framework")
    assert framework["attributes"]["build_tool"] == "gradle"
    assert framework["attributes"]["version"] == "3.3.1"
    assert framework["attributes"]["manifests"] == ["build.gradle.kts"]
    assert nodes_of(ir, "service") == []
    assert [node["name"] for node in nodes_of(ir, "endpoint")] == ["GET /ping"]
    assert ("service", "PingController", {}) in relationships_of(ir, "contains")
    kinds = {item["kind"]: item["message"] for item in ir["limitations"]}
    assert kinds == {
        "runtime-configuration": "No @SpringBootApplication class was found; extracted classes are attached to the "
        "repository and the application boundary is unknown."
    }
    assert "Kotlin" not in json.dumps(ir["limitations"])
    assert_valid("system-ir-1.0.json", ir)
