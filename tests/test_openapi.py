"""The bounded OpenAPI JSON subset and the no-fetch/no-value-persistence boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import invoke, read_ir
from test_schemas import assert_valid


def api_project(tmp_path: Path, capsys, document: dict) -> tuple[Path, Path]:
    root = tmp_path / "project"
    repository = root / "api"
    repository.mkdir(parents=True)
    (repository / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    assert invoke(root, capsys, "init", "API")[0] == 0
    assert invoke(root, capsys, "add", "api", "--id", "api", "--destination")[0] == 0
    return root, repository


def scan(root, capsys) -> dict:
    code, response = invoke(root, capsys, "scan")
    assert code == 0, response
    ir = read_ir(root)
    assert_valid("system-ir-1.0.json", ir)
    return ir


@pytest.mark.parametrize("version, supported", [
    ("3.0.0", True), ("3.0.3", True), ("3.1.0", True), ("3.1.1", True),
    ("2.0", False), ("3.2.0", False), ("3.1", False),
])
def test_supported_versions(tmp_path, capsys, version, supported) -> None:
    root, _ = api_project(tmp_path, capsys, {"openapi": version, "paths": {"/orders": {"get": {}}}})
    ir = scan(root, capsys)
    endpoints = [node for node in ir["nodes"] if node["type"] == "endpoint"]
    assert len(endpoints) == (1 if supported else 0)
    assert bool(ir["limitations"]) != supported
    # Even single-repository inputs never create cross-repository candidates.
    assert ir["candidates"] == []


def test_servers_inherit_and_override_and_local_path_item_references_resolve(tmp_path, capsys) -> None:
    document = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://root.example.invalid"}],
        "paths": {
            "/root": {"get": {}},
            "/path": {"servers": [{"url": "/v2"}], "get": {}},
            "/operation": {"get": {"servers": [{"url": "https://operation.example.invalid"}]}},
            "/local/{id}": {"$ref": "#/components/pathItems/Local"},
        },
        "components": {"pathItems": {"Local": {"post": {}}}},
    }
    root, _ = api_project(tmp_path, capsys, document)
    ir = scan(root, capsys)
    endpoints = {node["attributes"]["path"]: node for node in ir["nodes"] if node["type"] == "endpoint"}
    assert endpoints["/root"]["attributes"]["servers"] == ["https://root.example.invalid/"]
    assert endpoints["/path"]["attributes"]["servers"] == ["/v2"]
    assert endpoints["/operation"]["attributes"]["servers"] == ["https://operation.example.invalid/"]
    assert endpoints["/local/{id}"]["attributes"]["http_method"] == "POST"
    assert ir["limitations"] == []
    assert all(node["evidence_ids"] for node in endpoints.values())


@pytest.mark.parametrize("reference", [
    "https://outside.invalid/private?token=RefSecret",
    "../outside.json#/Path",
    "file:///C:/private/secrets.json",
    "#/missing",
    "#/components/pathItems/Cycle",
])
def test_references_are_bounded_and_never_fetched(tmp_path, capsys, reference, monkeypatch) -> None:
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("OpenAPI processing attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    root, _ = api_project(tmp_path, capsys, {
        "openapi": "3.1.0", "paths": {"/bad": {"$ref": reference}, "/ok": {"get": {}}},
        "components": {"pathItems": {"Cycle": {"$ref": "#/components/pathItems/Cycle"}}},
    })
    (root / "outside.json").write_text('{"Path": {"get": {"description": "OutsideSecret"}}}', encoding="utf-8")
    ir = scan(root, capsys)
    assert [node["name"] for node in ir["nodes"] if node["type"] == "endpoint"] == ["GET /ok"]
    assert ir["limitations"] and all(item["evidence_ids"] for item in ir["limitations"])
    assert "RefSecret" not in json.dumps(ir) and "OutsideSecret" not in json.dumps(ir)


def test_yaml_templates_and_remote_schema_refs_are_reported_without_leaking_values(tmp_path, capsys) -> None:
    root, repo = api_project(tmp_path, capsys, {
        "openapi": "3.0.3",
        "servers": [
            {"url": "https://name:ServerSecret@api.example.invalid/v1?key=QuerySecret#FragmentSecret"},
            {"url": "https://{tenant}.example.invalid"},
        ],
        "paths": {"/orders": {"get": {"responses": {
            "200": {"$ref": "https://outside.invalid/schema"}
        }}}},
    })
    (repo / "openapi.yaml").write_text("openapi: 3.0.3\npaths: {}\n", encoding="utf-8")
    ir = scan(root, capsys)
    messages = " ".join(item["message"] for item in ir["limitations"])
    assert "YAML" in messages and "reference" in messages and "server URL" in messages
    (endpoint,) = [node for node in ir["nodes"] if node["type"] == "endpoint"]
    assert endpoint["attributes"]["servers"] == ["https://api.example.invalid/v1"]
    for value in ("ServerSecret", "QuerySecret", "FragmentSecret"):
        assert value not in json.dumps(ir)


def test_malformed_reference_and_invalid_servers_do_not_crash(tmp_path, capsys) -> None:
    root, _ = api_project(tmp_path, capsys, {
        "openapi": "3.1.0", "paths": {
            "/bad": {"$ref": []},
            "/siblings": {"$ref": "#/components/pathItems/Local", "get": {}},
            "/servers": {"get": {"servers": {"not": "an array"}}},
        }, "components": {"pathItems": {"Local": {"get": {}}}},
    })
    ir = scan(root, capsys)
    assert len(ir["limitations"]) >= 3


def test_json_pointer_escapes_and_nonliteral_servers_are_bounded(tmp_path, capsys) -> None:
    root, _ = api_project(tmp_path, capsys, {
        "openapi": "3.1.0",
        "servers": [{"url": "/api"}],
        "paths": {
            "/source": {"$ref": "#/components/pathItems/a~1b~0c"},
            "/invalid": {"$ref": "#/components/pathItems/bad~escape"},
        },
        "components": {"pathItems": {"a/b~c": {"get": {"servers": [{"url": "relative/api"}]}}}},
    })
    ir = scan(root, capsys)
    (endpoint,) = [node for node in ir["nodes"] if node["type"] == "endpoint"]
    assert endpoint["attributes"]["path"] == "/source"
    assert endpoint["attributes"]["servers"] == []
    assert any("reference" in item["message"] for item in ir["limitations"])
    assert any("server URL" in item["message"] for item in ir["limitations"])
