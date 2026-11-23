"""Valid citations are not supported claims (ADR-028).

The negative fixture reproduces the shape of reviewed false diagram claims: a frontend given only backend
files, and route labels that no cited source holds. Every response is scripted, so these tests prove the
computed support contract and say nothing about how well a live model performs.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from repodocs.architecture.discovery import (
    STATUS_CURRENT, STATUS_INVALID, SUPPORT_REASONS, discover, load_view, save_view, validate_proposal,
)
from repodocs.architecture.export import build_html, validate_export_page
from repodocs.architecture.navigation import evidence_map
from repodocs.architecture.render import project, render_view
from repodocs.config import ProjectConfig

from conftest import invoke, read_ir
from test_architecture_discovery import (
    FIXTURE as EXPRESS, Scripted, component, connection, cross, excerpt_ids, full_answer, setup as express_setup,
)

NEGATIVE = Path(__file__).parent / "fixtures" / "grounding-negative-repo"
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "architecture-view-1.0.json").read_text(encoding="utf-8"))
VIEW_SCHEMA = Draft202012Validator(SCHEMA)
CONTROLLER = "src/main/java/com/example/specimens/SpecimenController.java"
SERVICE = "src/main/java/com/example/specimens/SpecimenService.java"
PAGE = "src/main/resources/static/app.js"
HTML = "src/main/resources/static/index.html"


def catalog(tmp_path: Path, capsys) -> tuple[Path, ProjectConfig, dict]:
    root = tmp_path / "project"
    root.mkdir()
    repository = tmp_path / "catalog"
    shutil.copytree(NEGATIVE, repository)
    assert invoke(root, capsys, "init", "p")[0] == 0
    assert invoke(root, capsys, "add", str(repository), "--id", "catalog", "--destination")[0] == 0
    assert invoke(root, capsys, "scan")[0] == 0
    return root, ProjectConfig.load(root), read_ir(root)


def request_controller(message: dict[str, Any]) -> dict[str, Any]:
    """Ask for the controller, or answer at once when the first round already opened it (ADR-029 selection)."""

    if CONTROLLER in excerpt_ids(message):
        return misleading(message)
    return {"components": [], "connections": [], "alternatives": [], "complete": False,
            "requests": [{"repository_id": "catalog", "path": CONTROLLER, "line_start": 1, "line_end": 40}]}


def misleading(message: dict[str, Any]) -> dict[str, Any]:
    """What a weak model says about the catalog: right pieces, a mislabelled UI, and made-up routes."""

    ids = excerpt_ids(message)
    return {
        "components": [
            component("Specimen page", "frontend", [PAGE, HTML], [ids[PAGE]], repository="catalog"),
            component("Specimen service", "backend-service", [CONTROLLER, SERVICE], [ids[CONTROLLER]], repository="catalog"),
            component("Specimen UI", "frontend", [CONTROLLER], [ids[CONTROLLER]], repository="catalog"),
        ],
        "connections": [
            connection("Specimen page", "Specimen service", "calls", "GET /api/specimens", [ids[PAGE], ids[CONTROLLER]]),
            connection("Specimen page", "Specimen service", "reads", "GET /bones", [ids[PAGE], ids[CONTROLLER], ids["README.md"]]),
            connection("Specimen page", "Specimen service", "depends-on", "POST /bones", [ids["README.md"]]),
            connection("Specimen page", "Specimen service", "writes", "POST /api/specimens", [ids[PAGE], ids[CONTROLLER]]),
        ],
        "alternatives": [], "requests": [], "complete": True,
    }


def catalog_view(tmp_path: Path, capsys) -> tuple[dict, dict, dict, ProjectConfig]:
    _, config, ir = catalog(tmp_path, capsys)
    view, context = discover(config, ir, Scripted(request_controller, misleading))
    return view, context, ir, config


def by_name(view: dict) -> dict[str, dict]:
    return {item["name"]: item for item in view["components"]}


def by_label(view: dict) -> dict[str, dict]:
    return {item["label"]: item for item in view["connections"]}


# ------------------------------------------------------------------ the false-confidence cases
def test_backend_only_facts_leave_a_frontend_role_unverified_and_inspectable(tmp_path: Path, capsys) -> None:
    view, *_ = catalog_view(tmp_path, capsys)

    ui = by_name(view)["Specimen UI"]
    assert ui["classification"] == "inferred", "its citation is a member file, which is all that classification means"
    assert (ui["support"], ui["support_reasons"]) == ("unverified", ["observed-role-conflict"])
    assert by_name(view)["Specimen service"]["support"] == "supported"
    assert by_name(view)["Specimen page"]["support"] == "unverified", "no analyzer observed a frontend fact in the static page"
    assert view["rejected"] == {}, "unsupported output is kept with reasons, not dropped"
    assert not list(VIEW_SCHEMA.iter_errors(view))


def test_route_labels_that_no_cited_source_holds_are_unsupported_whatever_they_cite(tmp_path: Path, capsys) -> None:
    view, *_ = catalog_view(tmp_path, capsys)
    links = by_label(view)

    for label in ("GET /bones", "POST /bones"):
        assert links[label]["support"] == "unsupported" and links[label]["support_reasons"] == ["route-not-in-cited-source"]
    assert links["GET /bones"]["evidence"], "its citations all resolve; that alone proves nothing"
    assert links["GET /bones"]["classification"] == "inferred"


def test_known_supported_facts_stay_supported_and_a_one_sided_call_stays_unverified(tmp_path: Path, capsys) -> None:
    view, *_ = catalog_view(tmp_path, capsys)
    links = by_label(view)

    assert (links["GET /api/specimens"]["support"], links["GET /api/specimens"]["support_reasons"]) == (
        "unverified", ["route-in-cited-source"],
    )
    # The endpoint exists, but the page never posts to it: half of the claim is unchecked.
    assert (links["POST /api/specimens"]["support"], links["POST /api/specimens"]["support_reasons"]) == ("unverified", ["route-on-one-side-only"])


def test_the_default_diagram_omits_unsupported_claims_and_marks_what_is_unchecked(tmp_path: Path, capsys) -> None:
    view, *_ = catalog_view(tmp_path, capsys)

    text = render_view(view)["architecture-view.mmd"]

    assert "/bones" not in text and "Specimen UI (unverified)" in text
    assert "GET /api/specimens" in text
    assert "Specimen page (unverified)" in text and "POST /api/specimens (unverified)" in text
    assert "Specimen service (" not in text, "a supported component carries no mark"
    assert "2 claims not shown" in text and "4 shown claims are unverified" in text
    assert '-.->|"GET /api/specimens (unverified)"|' in text
    assert '-.->|"POST /api/specimens (unverified)"|' in text


def test_every_claim_stays_inspectable_in_the_evidence_map_and_the_page_with_its_reasons(tmp_path: Path, capsys) -> None:
    view, context, ir, _ = catalog_view(tmp_path, capsys)

    evidence = evidence_map(view, context, ir, {"catalog": None})
    ui = by_name(view)["Specimen UI"]
    fabricated = by_label(view)["GET /bones"]
    assert evidence["components"][ui["id"]]["support"] == "unverified"
    assert evidence["components"][ui["id"]]["support_reasons"] == ["observed-role-conflict"]
    assert evidence["connections"][fabricated["id"]]["support_reasons"] == ["route-not-in-cited-source"]
    assert len(evidence["connections"]) == 4 and len(evidence["components"]) == 3

    page = build_html(view, render_view(view), evidence, "var mermaid = {};")
    validate_export_page(page)
    assert "Specimen UI" in page and "observed-role-conflict" in page
    assert all(code in page for code in SUPPORT_REASONS), "the page carries the reason sentences"


def test_a_stored_view_reproduces_its_support_and_a_hand_edited_badge_is_invalid(tmp_path: Path, capsys) -> None:
    view, context, ir, config = catalog_view(tmp_path, capsys)
    save_view(config, view, context)
    assert load_view(config, ir)[0] == STATUS_CURRENT

    forged = copy.deepcopy(view)
    next(item for item in forged["connections"] if item["label"] == "GET /bones").update(support="supported", support_reasons=["route-in-cited-source"])
    (config.internal_path / "architecture-view.json").write_text(json.dumps(forged), encoding="utf-8")

    status, _, reason = load_view(config, ir)
    assert status == STATUS_INVALID and "no longer passes validation" in reason


def test_a_model_cannot_supply_its_own_support(tmp_path: Path, capsys) -> None:
    _, config, ir = catalog(tmp_path, capsys)

    def boastful(message: dict[str, Any]) -> dict[str, Any]:
        answer = misleading(message)
        for item in answer["components"] + answer["connections"]:
            item.update(support="supported", support_reasons=["observed-endpoint"], verified=True)
        return answer

    view, _ = discover(config, ir, Scripted(request_controller, boastful))

    assert by_name(view)["Specimen UI"]["support"] == "unverified"
    assert by_label(view)["GET /bones"]["support"] == "unsupported"


# ------------------------------------------------------------------ generic stacks are reported honestly
def test_resolving_citations_alone_never_yield_a_supported_component_on_a_generic_stack(tmp_path: Path, capsys) -> None:
    _, config, ir = express_setup(tmp_path, capsys)

    view, _ = discover(config, ir, Scripted(full_answer))

    assert {item["support"] for item in view["components"]} == {"unverified"}
    assert all(item["support_reasons"] == ["no-checkable-fact"] for item in view["components"])
    links = by_label(view)
    assert links["reads orders.json"]["support"] == "unverified" and links["reads orders.json"]["support_reasons"] == ["no-checkable-fact"]
    assert links["GET /api/orders"]["support"] == "unverified", "literal text alone is not an observed request/endpoint pair"
    text = render_view(view)["architecture-view.mmd"]
    assert "Web page (unverified)" in text and "Orders API (unverified)" in text
    assert "no observed or literal fact was checked" in text


# ------------------------------------------------------------------ what the checks do and refuse to do
def synthetic(nodes: list[dict], evidence: list[dict], texts: dict[str, str]) -> tuple[dict, dict]:
    paths = sorted(set(texts) | {item["path"] for item in evidence})
    ir = {
        "snapshots": [{"repository_id": "app", "files": [{"path": path} for path in paths]}],
        "nodes": nodes, "evidence": evidence, "candidates": [],
    }
    documents = [
        {"id": "ctx:" + format(index, "x").rjust(16, "0"), "repository_id": "app", "path": path, "content_hash": "c" * 64, "text": text}
        for index, (path, text) in enumerate(sorted(texts.items()), start=1)
    ]
    return ir, {"documents": documents}


def ids_of(context: dict) -> dict[str, str]:
    return {item["path"]: item["id"] for item in context["documents"]}


def fact(kind: str, path: str, key: str) -> tuple[dict, dict]:
    return (
        {"id": f"node:{key}", "type": kind, "name": key, "attributes": {"repository_id": "app"}, "evidence_ids": [f"ev:{key}"]},
        {"id": f"ev:{key}", "repository_id": "app", "path": path},
    )


def single(ir: dict, context: dict, name: str, kind: str, members: list[str]) -> dict:
    ids = ids_of(context)
    result = validate_proposal({"components": [component(name, kind, members, [ids[members[0]]], repository="app")], "connections": []}, context, ir)
    assert result.rejected == {}
    return next(iter(result.components.values()))


def test_a_shared_file_with_frontend_and_server_facts_is_never_refused_as_a_frontend() -> None:
    nodes, evidence = zip(fact("component", "shared.tsx", "ui"), fact("endpoint", "shared.tsx", "route"))
    ir, context = synthetic(list(nodes), list(evidence), {"shared.tsx": "export const Page = () => null;"})

    item = single(ir, context, "Server rendered page", "frontend", ["shared.tsx"])

    assert (item["support"], item["support_reasons"]) == ("supported", ["observed-role-facts"])


def test_a_role_is_never_judged_from_a_file_name() -> None:
    ir, context = synthetic([], [], {"server/routes.py": "x = 1", "static/app.js": "y = 2"})

    for name, kind, path in (("Interface", "frontend", "server/routes.py"), ("Service", "backend-service", "static/app.js")):
        item = single(ir, context, name, kind, [path])
        assert (item["support"], item["support_reasons"]) == ("unverified", ["no-checkable-fact"])


def test_an_http_client_fact_alone_does_not_establish_a_frontend_role() -> None:
    node, evidence = fact("http_request", "service.ts", "request")
    ir, context = synthetic([node], [evidence], {"service.ts": "fetch('/orders');"})
    assert single(ir, context, "Interface", "frontend", ["service.ts"])["support"] == "unverified"


def test_a_service_with_only_data_layer_facts_is_not_refused() -> None:
    nodes, evidence = zip(fact("table", "dao.py", "orders"))
    ir, context = synthetic(list(nodes), list(evidence), {"dao.py": "x = 1"})

    assert single(ir, context, "Orders service", "backend-service", ["dao.py"])["support"] == "unverified"


def test_a_backend_component_whose_only_observed_facts_are_frontend_is_unsupported() -> None:
    nodes, evidence = zip(fact("component", "view.tsx", "ui"))
    ir, context = synthetic(list(nodes), list(evidence), {"view.tsx": "export const View = () => null;"})

    assert single(ir, context, "Orders API", "api", ["view.tsx"])["support_reasons"] == ["observed-role-conflict"]


def link(ir: dict, context: dict, kind: str, label: str, cite: list[str], *, source: str = "client.js", target: str = "server.py") -> dict | None:
    ids = ids_of(context)
    raw = {
        "components": [
            component("Client", "frontend", [source], [ids[source]], repository="app"),
            component("Server", "backend-service", [target], [ids[target]], repository="app"),
        ],
        "connections": [connection("Client", "Server", kind, label, [ids[path] for path in cite])],
    }
    result = validate_proposal(raw, context, ir)
    assert result.rejected == {}
    return next(iter(result.connections.values()))


FLASK = "@bp.route('/bones/<int:bone_id>', methods=['GET'])\ndef one(bone_id): ...\n\n@bp.post('/bones')\ndef add(): ...\n"
CLIENT = "fetch(`/bones/${id}`);\nfetch('/bones', {\n  method: 'POST',\n});\n"


@pytest.mark.parametrize("kind, label, support, reasons", [
    ("calls", "GET /bones/:id", "unverified", ["route-in-cited-source"]),
    ("calls", "GET /bones/{id}", "unverified", ["route-in-cited-source"]),
    ("calls", "POST /bones", "unverified", ["route-in-cited-source"]),
    ("calls", "DELETE /bones", "unsupported", ["method-not-in-cited-source"]),
    ("calls", "GET /bones", "unverified", ["route-on-one-side-only"]),  # the server end declares only POST there
    ("calls", "GET /skeletons", "unsupported", ["route-not-in-cited-source"]),
    ("calls", "/bone", "unsupported", ["route-not-in-cited-source"]),
    ("serves", "GET /bones/:id", "unverified", ["route-in-cited-source"]),
    ("calls", "bones are stored", "unverified", ["no-checkable-fact"]),
    ("depends-on", "uses bones", "unverified", ["no-checkable-fact"]),
], ids=lambda value: str(value))
def test_a_route_label_is_checked_against_the_cited_member_excerpts(kind, label, support, reasons) -> None:
    ir, context = synthetic([], [], {"client.js": CLIENT, "server.py": FLASK})

    item = link(ir, context, kind, label, ["client.js", "server.py"])

    assert (item["support"], item["support_reasons"]) == (support, reasons)


def test_a_route_in_an_uncited_or_unrelated_excerpt_does_not_count() -> None:
    ir, context = synthetic([], [], {"client.js": CLIENT, "server.py": FLASK, "NOTES.md": "GET /ghosts is documented here"})

    unrelated = link(ir, context, "calls", "GET /ghosts", ["client.js", "server.py", "NOTES.md"])
    uncited = link(ir, context, "calls", "GET /bones/:id", ["server.py"])

    assert unrelated["support"] == "unsupported"
    assert (uncited["support"], uncited["support_reasons"]) == ("unverified", ["route-on-one-side-only"]), "only the server end was cited"


def test_an_observed_endpoint_in_a_member_file_backs_the_route_and_one_elsewhere_does_not() -> None:
    node, ev = fact("endpoint", "server.py", "bones")
    node["attributes"].update(http_method="GET", path="/bones")
    elsewhere, ev_other = fact("endpoint", "other.py", "ghosts")
    elsewhere["attributes"].update(http_method="GET", path="/ghosts")
    request, ev_request = fact("http_request", "client.js", "request")
    request["attributes"].update(http_method="GET", url="/bones")
    ir, context = synthetic([node, elsewhere, request], [ev, ev_other, ev_request], {"client.js": "fetch('/bones'); fetch('/ghosts');", "server.py": "pass", "other.py": "pass"})

    backed = link(ir, context, "calls", "GET /bones", ["client.js", "server.py"])
    foreign = link(ir, context, "calls", "GET /ghosts", ["client.js", "server.py"])
    wrong_method = link(ir, context, "calls", "POST /bones", ["client.js", "server.py"])

    assert (backed["support"], backed["support_reasons"]) == ("supported", ["observed-request-endpoint"])
    assert (foreign["support"], foreign["support_reasons"]) == ("unverified", ["route-on-one-side-only"])
    assert wrong_method["support"] == "unsupported"


# ------------------------------------------------------------------ confirmation bindings are preserved
def test_a_bound_confirmed_candidate_is_supported_and_an_unbound_claim_is_not() -> None:
    ok = cross(("Checkout", "shop", ["server.js"]), ("Payments", "billing", ["api.js"]), [0, 1])
    unrelated = cross(("Reports", "shop", ["other.js"]), ("Ledger", "billing", ["other.js"]), [2, 3])

    assert (ok["link"]["support"], ok["link"]["support_reasons"]) == ("supported", ["confirmed-candidate"])
    assert ok["link"]["classification"] == "inferred"
    assert unrelated["link"]["classification"] == "proposed" and unrelated["link"]["support"] == "unsupported"
    assert unrelated["rejected"] == {"unbound-candidate": 1}


# ------------------------------------------------------------------ rendering
def test_a_server_rendered_frontend_without_frontend_facts_is_not_rejected() -> None:
    nodes, evidence = zip(fact("endpoint", "a.py", "e"))
    ir, context = synthetic(list(nodes), list(evidence), {"a.py": "x"})
    item = single(ir, context, "Interface", "frontend", ["a.py"])
    view = {"status": "complete", "provenance": {"context_hash": "a" * 64, "source_ir_hash": "b" * 64}, "components": [item], "connections": [], "alternatives": []}

    (diagram,) = project(view)

    assert len(diagram.nodes) == 1 and "unverified" in diagram.nodes[0].label
    assert item["support"] == "unverified", "missing frontend facts do not disprove SSR behavior"


@pytest.mark.parametrize("client,server", [
    ("// GET /bones", "# GET /bones"),
    ('const example = "GET /bones";', 'example = "GET /bones"'),
    ("app.get('/bones', handler);", "@bp.get('/bones')\ndef handler(): pass"),
])
def test_text_examples_and_two_route_declarations_never_prove_a_call(client, server) -> None:
    ir, context = synthetic([], [], {"client.js": client, "server.py": server})
    assert link(ir, context, "calls", "GET /bones", ["client.js", "server.py"])["support"] != "supported"
