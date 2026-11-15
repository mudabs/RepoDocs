"""React adapter: detection, modules and imports, components, routes, request sites, limitations."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import copy_fixture, invoke, read_ir
from test_schemas import assert_valid
from test_spring_boot import line_of, nodes_of, relationships_of


REACT_FIXTURE = Path(__file__).parent / "fixtures" / "react-repo"


def react_project(tmp_path: Path, capsys) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "frontend"
    shutil.copytree(REACT_FIXTURE, repository)
    assert invoke(root, capsys, "init", "SmartCart")[0] == 0
    assert invoke(root, capsys, "add", "frontend", "--id", "frontend", "--destination")[0] == 0
    return root, repository


def test_detects_react_package_and_lists_modules(tmp_path: Path, capsys) -> None:
    root, _ = react_project(tmp_path, capsys)
    code, payload = invoke(root, capsys, "scan")
    assert code == 0
    analyzers = {item["name"]: item["repositories"] for item in payload["data"]["analyzers"]}
    assert analyzers == {
        "basic-manifest": ["frontend"],
        "openapi": [],
        "orm-metadata": [],
        "postgresql": [],
        "react": ["frontend"],
        "spring-boot": [],
    }

    ir = read_ir(root)
    (framework,) = nodes_of(ir, "framework")
    assert framework["name"] == "React"
    assert framework["attributes"]["version"] == "^18.3.1"
    assert framework["attributes"]["version_kind"] == "declared-range"
    (service,) = nodes_of(ir, "service")
    assert service["name"] == "smartcart-frontend"
    assert service["attributes"] == {
        "repository_id": "frontend",
        "framework": "React",
        "framework_version": "^18.3.1",
        "package_name": "smartcart-frontend",
        "package_path": "package.json",
        "package_directory": "",
        "toolchain": ["vite"],
        "routers": ["react-router-dom"],
        "http_clients": ["axios"],
        "typescript": True,
    }

    modules = {node["name"]: node["attributes"] for node in nodes_of(ir, "module")}
    assert sorted(modules) == [
        "src/App.tsx",
        "src/admin/adminRouter.tsx",
        "src/api/client.ts",
        "src/components/Layout.tsx",
        "src/components/Legacy.jsx",
        "src/components/OrderCard.tsx",
        "src/main.tsx",
        "src/pages/Admin.tsx",
        "src/pages/Home.tsx",
        "src/pages/OrderDetail.tsx",
        "src/pages/OrdersPage.tsx",
        "src/pages/Users.tsx",
        "src/types.ts",
        "src/utils/format.ts",
    ]
    assert modules["src/components/Legacy.jsx"]["language"] == "JavaScript" and modules["src/components/Legacy.jsx"]["jsx"] is True
    assert modules["src/api/client.ts"]["named_exports"] == ["Format", "api"]
    assert modules["src/App.tsx"]["default_export"] == "App"
    contains = relationships_of(ir, "contains")
    assert ("frontend", "smartcart-frontend", {}) in contains
    assert all(("smartcart-frontend", name, {}) in contains for name in modules)


def test_components_are_literal_declarations_with_jsx_references(tmp_path: Path, capsys) -> None:
    root, repository = react_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    components = {node["name"]: node["attributes"] for node in nodes_of(ir, "component")}
    assert {name: attributes["kind"] for name, attributes in components.items()} == {
        "Admin": "class",
        "App": "function",
        "Component": "function",
        "Home": "function",
        "Layout": "function",
        "Legacy": "function",
        "OrderCard": "memo",
        "OrderDetail": "function",
        "OrdersPage": "function",
    }
    assert components["App"]["default_export"] is True
    assert components["OrdersPage"] == {
        "repository_id": "frontend",
        "module": "src/pages/OrdersPage.tsx",
        "line": line_of(repository, "src/pages/OrdersPage.tsx", "export function OrdersPage() {"),
        "kind": "function",
        "exported": True,
        "default_export": False,
    }
    # PascalCase helpers without JSX and components in test files are not components.
    assert "Format" not in components and "TestHarness" not in components

    evidence = {item["id"]: item for item in ir["evidence"]}
    (app,) = nodes_of(ir, "component", module="src/App.tsx")
    span = evidence[app["evidence_ids"][0]]
    assert span["kind"] == "span" and span["analyzer"] == "react" and span["symbol"] == "App"
    assert span["line_start"] == line_of(repository, "src/App.tsx", "export default function App() {")
    assert span["line_end"] == len((repository / "src" / "App.tsx").read_text(encoding="utf-8").splitlines())

    references = [(source, target) for source, target, attributes in relationships_of(ir, "references") if attributes.get("kind") == "jsx-element"]
    assert references == [
        ("App", "Home"),
        ("App", "Layout"),
        ("App", "OrderDetail"),
        ("App", "OrdersPage"),
        ("OrdersPage", "OrderCard"),
    ]
    assert all(item["classification"] == "observed" for item in ir["relationships"])


def test_routes_from_nested_jsx_and_route_objects(tmp_path: Path, capsys) -> None:
    root, repository = react_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    routes = sorted(
        (node["attributes"]["path"], node["attributes"]["component"], node["attributes"]["index"], node["attributes"]["module"])
        for node in nodes_of(ir, "route")
    )
    assert routes == [
        ("/", "Home", True, "src/App.tsx"),
        ("/", "Layout", False, "src/App.tsx"),
        ("/admin", "Admin", True, "src/admin/adminRouter.tsx"),
        ("/admin", "Layout", False, "src/admin/adminRouter.tsx"),
        ("/admin/orders", "OrdersPage", False, "src/admin/adminRouter.tsx"),
        ("/admin/users", None, False, "src/admin/adminRouter.tsx"),
        ("/orders", "OrdersPage", False, "src/App.tsx"),
        ("/orders/:id", "OrderDetail", False, "src/App.tsx"),
    ]
    assert all(node["attributes"]["router"] == "react-router" for node in nodes_of(ir, "route"))
    route_references = sorted(
        (source, target) for source, target, attributes in relationships_of(ir, "references") if attributes.get("kind") == "route-element"
    )
    assert ("/orders/:id", "OrderDetail") in route_references and ("/admin/orders", "OrdersPage") in route_references

    (orders_route,) = [node for node in nodes_of(ir, "route", path="/orders")]
    evidence = {item["id"]: item for item in ir["evidence"]}
    span = evidence[orders_route["evidence_ids"][0]]
    assert span["path"] == "src/App.tsx"
    assert span["line_start"] == line_of(repository, "src/App.tsx", '<Route path="orders" element={<OrdersPage />} />')

    dynamic = [item for item in ir["limitations"] if item["kind"] == "dynamic-routing" and "<Route> path" in item["message"]]
    assert len(dynamic) == 1
    assert evidence[dynamic[0]["evidence_ids"][0]]["line_start"] == line_of(
        repository, "src/App.tsx", "<Route path={settingsPath} element={<Admin />} />"
    )
    assert not any(node["attributes"]["path"] == "/commented-out" for node in nodes_of(ir, "route"))


def test_import_graph_distinguishes_static_dynamic_and_unresolved(tmp_path: Path, capsys) -> None:
    root, _ = react_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    imports = relationships_of(ir, "imports")
    assert ("src/App.tsx", "src/pages/OrdersPage.tsx", {"kind": "static", "specifier": "./pages/OrdersPage"}) in imports
    assert ("src/App.tsx", "src/pages/Admin.tsx", {"kind": "dynamic", "specifier": "./pages/Admin"}) in imports
    assert ("src/main.tsx", "react-dom", {"kind": "static", "specifier": "react-dom/client"}) in imports
    assert ("src/components/OrderCard.tsx", "src/types.ts", {"kind": "static", "specifier": "../types"}) in imports
    assert not any(source == "src/components/Legacy.jsx" for source, _, _ in imports)

    by_kind: dict[str, list[str]] = {}
    for limitation in ir["limitations"]:
        assert limitation["analyzer"] == "react" and limitation["evidence_ids"]
        by_kind.setdefault(limitation["kind"], []).append(limitation["message"])
    assert sorted(by_kind) == ["ambiguous-reference", "dynamic-routing", "reflection", "runtime-configuration"]
    assert by_kind["ambiguous-reference"] == [
        "src/components/Legacy.jsx: 1 import specifier(s) could not be resolved to a file or a declared dependency "
        "and were not linked: @/styles/legacy.module.css. Path aliases are not applied."
    ]
    assert sorted(by_kind["runtime-configuration"]) == [
        "src/App.tsx: 1 module(s) are loaded at runtime with import(): ./pages/Admin. The import edge is recorded; "
        "whether and when the load happens is not.",
        "src/admin/adminRouter.tsx: 1 module(s) are loaded at runtime with import(): ../pages/Users. The import edge "
        "is recorded; whether and when the load happens is not.",
    ]
    assert by_kind["reflection"] == [
        "src/pages/Admin.tsx line 10 evaluates code at runtime (eval or new Function); its behavior is not analyzed."
    ]


def test_http_request_sites_with_literal_and_template_urls(tmp_path: Path, capsys) -> None:
    root, repository = react_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    requests = {
        (node["attributes"]["module"], node["name"]): node["attributes"] for node in nodes_of(ir, "http_request")
    }
    assert sorted(requests) == [
        ("src/pages/OrderDetail.tsx", "GET /api/orders/{id}"),
        ("src/pages/OrderDetail.tsx", "POST /api/orders"),
        ("src/pages/OrdersPage.tsx", "GET /orders"),
        ("src/pages/OrdersPage.tsx", "GET {API_BASE}/orders/{id}"),
    ]
    via_instance = requests[("src/pages/OrdersPage.tsx", "GET /orders")]
    assert via_instance["client"] == "axios" and via_instance["instance"] == "api" and via_instance["base_url"] == "/api"
    assert via_instance["url_kind"] == "literal"
    template = requests[("src/pages/OrdersPage.tsx", "GET {API_BASE}/orders/{id}")]
    assert template["client"] == "fetch" and template["url_kind"] == "template"
    post = requests[("src/pages/OrderDetail.tsx", "POST /api/orders")]
    assert post["client"] == "fetch" and post["lines"] == [line_of(repository, "src/pages/OrderDetail.tsx", 'await fetch("/api/orders", { method: "POST", body });')]
    assert all(item["classification"] == "observed" for item in ir["relationships"] if item["type"] == "requests")
    assert ("src/pages/OrdersPage.tsx", "GET /orders", {"client": "axios"}) in relationships_of(ir, "requests")

    evidence = {item["id"]: item for item in ir["evidence"]}
    dynamic = [item for item in ir["limitations"] if "request URL is not a string" in item["message"]]
    assert len(dynamic) == 1
    assert evidence[dynamic[0]["evidence_ids"][0]]["line_start"] == line_of(
        repository, "src/pages/OrderDetail.tsx", "const again = await fetch(url); // dynamic: the URL is a variable"
    )


def test_scan_is_deterministic_schema_valid_and_renders(tmp_path: Path, capsys) -> None:
    root, repository = react_project(tmp_path, capsys)
    assert invoke(root, capsys, "scan")[0] == 0
    first = read_ir(root)
    assert invoke(root, capsys, "scan")[0] == 0
    assert read_ir(root) == first
    assert_valid("system-ir-1.0.json", first)

    assert invoke(root, capsys, "generate")[0] == 0
    assert invoke(root, capsys, "validate")[0] == 0
    code, payload = invoke(root, capsys, "generate")
    assert code == 0 and payload["data"]["changed"] is False

    markdown = (repository / "docs" / "generated" / "architecture.md").read_text(encoding="utf-8")
    assert "| smartcart-frontend | frontend | React ^18.3.1 | `package.json` | 14 modules, 9 components, 8 routes, 4 requests |" in markdown
    assert "| `/orders/:id` | react-router | OrderDetail | `src/App.tsx` | frontend |" in markdown
    assert "| `/` (index) | react-router | Home | `src/App.tsx` | frontend |" in markdown
    assert "| GET | `/orders` | axios | /api | `src/pages/OrdersPage.tsx` | frontend |" in markdown
    assert "| OrderCard | memo | `src/components/OrderCard.tsx` | named | 0 | frontend |" in markdown
    assert "`ambiguous-reference` (react): src/components/Legacy.jsx" in markdown
    mermaid = (repository / "docs" / "generated" / "architecture.mmd").read_text(encoding="utf-8")
    assert "smartcart-frontend (React): 14 modules, 9 components, 8 routes, 4 requests" in mermaid


def test_react_dependency_without_modules_is_not_detected(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    copy_fixture(root / "repo")  # package.json lists react but the repository has no JS or TS modules
    assert invoke(root, capsys, "init", "Plain")[0] == 0
    assert invoke(root, capsys, "add", "repo", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)
    assert {item["name"]: item["repositories"] for item in ir["provenance"]["analyzers"]}["react"] == []
    assert nodes_of(ir, "service") == [] and ir["limitations"] == []


def single_module_project(tmp_path: Path, capsys, files: dict[str, str]) -> dict:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    repository = root / "web"
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    assert invoke(root, capsys, "init", "Web")[0] == 0
    assert invoke(root, capsys, "add", "web", "--id", "web", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    return read_ir(root)


def test_source_only_detection_carries_evidence(tmp_path: Path, capsys) -> None:
    """Without a package.json, the framework node, its edge, and the limitation cite the importing modules."""

    ir = single_module_project(
        tmp_path,
        capsys,
        {
            "src/App.jsx": 'import React from "react";\n\nexport default function App() {\n  return <h1>Hi</h1>;\n}\n',
            "src/util.js": "export const x = 1;\n",
        },
    )
    assert {item["name"]: item["repositories"] for item in ir["provenance"]["analyzers"]}["react"] == ["web"]
    evidence = {item["id"]: item for item in ir["evidence"]}
    (framework,) = nodes_of(ir, "framework")
    assert framework["attributes"]["detected_from"] == "source"
    assert framework["attributes"]["manifests"] == [] and framework["attributes"]["version"] is None
    assert [evidence[item]["path"] for item in framework["evidence_ids"]] == ["src/App.jsx"]
    (edge,) = [item for item in ir["relationships"] if item["type"] == "depends_on" and item["target"] == framework["id"]]
    assert edge["evidence_ids"] == framework["evidence_ids"]
    by_kind = {item["kind"]: item for item in ir["limitations"]}
    assert sorted(by_kind) == ["ambiguous-reference", "unsupported-construct"]
    limitation = by_kind["unsupported-construct"]
    assert limitation["message"].startswith("React was detected from 1 module(s) that import react; no package.json declares react")
    assert limitation["evidence_ids"] == framework["evidence_ids"]
    # Without a manifest there is no dependency node for react, so the import is reported, not linked.
    assert "react" in by_kind["ambiguous-reference"]["message"]
    assert nodes_of(ir, "service") == []
    assert ("web", "src/App.jsx", {}) in relationships_of(ir, "contains")
    assert [node["name"] for node in nodes_of(ir, "component")] == ["App"]
    assert all(item["evidence_ids"] for item in ir["limitations"])
    # Every analyzer contribution carries evidence; only the basic project-to-repository edge has none.
    project_node = next(node["id"] for node in ir["nodes"] if node["type"] == "project")
    assert all(item["evidence_ids"] for item in ir["relationships"] if item["source"] != project_node)
    assert_valid("system-ir-1.0.json", ir)


def test_jsx_without_react_dependency_or_import_is_not_detected(tmp_path: Path, capsys) -> None:
    """JSX is used by Preact, Solid, and others; it is not a React signal on its own."""

    ir = single_module_project(
        tmp_path,
        capsys,
        {
            "package.json": json.dumps({"name": "solid-app", "dependencies": {"solid-js": "^1.8.0"}}),
            "src/App.tsx": 'import { render } from "solid-js/web";\n\nexport default function App() {\n  return <h1>Hi</h1>;\n}\n',
        },
    )
    assert {item["name"]: item["repositories"] for item in ir["provenance"]["analyzers"]}["react"] == []
    assert nodes_of(ir, "framework") == [] and nodes_of(ir, "component") == [] and ir["limitations"] == []


def test_react_mentioned_only_in_comments_or_strings_is_not_detected(tmp_path: Path, capsys) -> None:
    """Detection reads parsed import declarations, never raw text."""

    ir = single_module_project(
        tmp_path,
        capsys,
        {
            "src/notes.js": (
                '// Example: import React from "react"\n'
                "/* Legacy code used:\n"
                '   const React = require("react");\n'
                "*/\n"
                "const snippet = 'import React from \"react\"';\n"
                "const other = `import { useState } from \"react\"`;\n"
                "export const message = snippet + other;\n"
            ),
        },
    )
    assert {item["name"]: item["repositories"] for item in ir["provenance"]["analyzers"]}["react"] == []
    assert nodes_of(ir, "framework") == [] and ir["limitations"] == []

    detected = single_module_project(
        tmp_path / "second",
        capsys,
        {"src/real.js": 'const React = require("react/jsx-runtime");\nexport const x = React;\n'},
    )
    assert {item["name"]: item["repositories"] for item in detected["provenance"]["analyzers"]}["react"] == ["web"]


def test_distinct_client_instances_are_not_merged(tmp_path: Path, capsys) -> None:
    ir = single_module_project(
        tmp_path,
        capsys,
        {
            "package.json": json.dumps({"name": "web", "dependencies": {"react": "^18.3.1", "axios": "^1.7.2"}}),
            "src/clients.ts": (
                'import axios from "axios";\n\n'
                'export const api = axios.create({ baseURL: "/api" });\n'
                'export const admin = axios.create({ baseURL: "/admin" });\n'
                "export async function load() {\n"
                '  await api.get("/users");\n'
                '  await admin.get("/users");\n'
                '  await axios.get("/users");\n'
                '  await api.get("/users");\n'
                "}\n"
            ),
        },
    )
    sites = sorted(
        (
            (node["attributes"]["instance"], node["attributes"]["base_url"], node["name"], node["attributes"]["lines"])
            for node in nodes_of(ir, "http_request")
        ),
        key=lambda item: (item[0] or "", item[1] or ""),
    )
    assert sites == [
        (None, None, "GET /users", [8]),
        ("admin", "/admin", "GET /users", [7]),
        ("api", "/api", "GET /users", [6, 9]),
    ]
    assert len({node["id"] for node in nodes_of(ir, "http_request")}) == 3
    assert len(relationships_of(ir, "requests")) == 3
    assert ir["limitations"] == []


def test_next_file_conventions_become_routes_and_api_endpoints(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "web"
    (repository / "pages" / "orders").mkdir(parents=True)
    (repository / "pages" / "api").mkdir(parents=True)
    (repository / "app" / "(shop)" / "dashboard").mkdir(parents=True)
    (repository / "app" / "api" / "health").mkdir(parents=True)
    (repository / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"next": "14.2.5", "react": "18.3.1", "react-dom": "18.3.1"}}),
        encoding="utf-8",
    )
    (repository / "pages" / "index.tsx").write_text("export default function Home() {\n  return <h1>Home</h1>;\n}\n", encoding="utf-8")
    (repository / "pages" / "_app.tsx").write_text("export default function App({ Component }) {\n  return <Component />;\n}\n", encoding="utf-8")
    (repository / "pages" / "orders" / "[id].tsx").write_text("export default function OrderPage() {\n  return <p>order</p>;\n}\n", encoding="utf-8")
    (repository / "pages" / "api" / "orders.ts").write_text("export default function handler(req, res) {\n  res.status(200).json([]);\n}\n", encoding="utf-8")
    (repository / "app" / "(shop)" / "dashboard" / "page.tsx").write_text("export default function Dashboard() {\n  return <main />;\n}\n", encoding="utf-8")
    (repository / "app" / "api" / "health" / "route.ts").write_text(
        "export async function GET() {\n  return Response.json({ ok: true });\n}\nexport const POST = async () => new Response(null);\n",
        encoding="utf-8",
    )
    assert invoke(root, capsys, "init", "Shop")[0] == 0
    assert invoke(root, capsys, "add", "web", "--id", "web", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    ir = read_ir(root)

    (service,) = nodes_of(ir, "service")
    assert service["attributes"]["toolchain"] == ["next"] and service["attributes"]["routers"] == ["next"]
    routes = sorted((node["attributes"]["path"], node["attributes"]["router"], node["attributes"]["component"]) for node in nodes_of(ir, "route"))
    assert routes == [
        ("/", "next-pages", "Home"),
        ("/dashboard", "next-app", "Dashboard"),
        ("/orders/[id]", "next-pages", "OrderPage"),
    ]
    endpoints = sorted((node["name"], node["attributes"]["router"]) for node in nodes_of(ir, "endpoint"))
    assert endpoints == [("ANY /api/orders", "next-pages"), ("GET /api/health", "next-app"), ("POST /api/health", "next-app")]
    assert ("pages/api/orders.ts", "ANY /api/orders", {"router": "next-pages"}) in relationships_of(ir, "exposes")
    assert ir["limitations"] == []
    assert_valid("system-ir-1.0.json", ir)
