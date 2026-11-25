"""Viewer artifact download and parity tests (#75): replayed providers only.

Verifies artifact downloads, content types, CLI/web byte parity, partial/stale labeling,
and rejection of unsafe artifact names.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from repodocs.common import sha256_bytes  # noqa: E402
from repodocs.config import ProjectConfig  # noqa: E402
from repodocs import service  # noqa: E402

from conftest import invoke  # noqa: E402
from test_architecture_discovery import FIXTURE, full_answer  # noqa: E402
from test_web_jobs import Api, Factory, Replay, TERMINAL, asks, request, running  # noqa: E402


def _deterministic_request(workspace_id: str, key: str = "viewer-det-1") -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "kind": "request",
        "workspace_id": workspace_id,
        "idempotency_key": key,
        "mode": "deterministic",
        "provider_session_id": None,
        "context_hash": None,
        "consent_receipt_id": None,
        "views": ["overview", "erd"],
        "limits": {
            "max_requests": 0,
            "max_total_tokens": None,
            "max_cost_microunits": 0,
            "currency": "USD",
        },
    }


def test_artifact_downloads_have_attachment_disposition_and_expected_types(tmp_path: Path) -> None:
    """Every artifact returned in result.artifact_ids downloads cleanly with attachment headers."""
    with running(tmp_path) as (api, _app, _factory):
        workspace_id = api.workspace()
        api.preview(workspace_id)
        r = api.submit(_deterministic_request(workspace_id))
        assert r.status_code == 202, r.text
        job = api.wait(r.json()["job_id"])
        assert job["state"] == "succeeded"
        assert job["result"] is not None
        artifact_ids = job["result"]["artifact_ids"]
        assert artifact_ids, "expected deterministic job to produce artifacts"

        for artifact_id in artifact_ids:
            resp = api.download(job["job_id"], artifact_id)
            assert resp.status_code == 200, resp.text
            assert f'attachment; filename="{artifact_id}"' == resp.headers.get("content-disposition")
            assert len(resp.content) > 0
            if artifact_id.endswith(".mmd"):
                assert resp.headers.get("content-type", "").startswith("text/plain")
            elif artifact_id.endswith(".json"):
                assert resp.headers.get("content-type", "").startswith("application/json")
                json.loads(resp.content.decode("utf-8"))
            elif artifact_id.endswith(".md"):
                assert resp.headers.get("content-type", "").startswith("text/markdown")


def test_web_artifact_bytes_match_cli_for_same_source(tmp_path: Path, capsys) -> None:
    """Deterministic artifacts downloaded from the web API match CLI output byte-for-byte."""
    with running(tmp_path) as (api, _app, _factory):
        workspace_id = api.workspace()
        api.preview(workspace_id)
        r = api.submit(_deterministic_request(workspace_id, key="parity-1"))
        assert r.status_code == 202
        job = api.wait(r.json()["job_id"])
        assert job["state"] == "succeeded"
        web_mmd = {
            aid: api.download(job["job_id"], aid).content
            for aid in job["result"]["artifact_ids"]
            if aid.endswith(".mmd")
        }
        assert web_mmd

    # Run the CLI on an identical copy of FIXTURE
    cli_root = tmp_path / "cli-project"
    cli_root.mkdir()
    cli_repo = tmp_path / "cli-shop"
    shutil.copytree(FIXTURE, cli_repo)
    assert invoke(cli_root, capsys, "init", "Shop")[0] == 0
    assert invoke(cli_root, capsys, "add", str(cli_repo), "--id", "shop", "--destination")[0] == 0
    assert invoke(cli_root, capsys, "scan")[0] == 0
    assert invoke(cli_root, capsys, "generate")[0] == 0
    config = ProjectConfig.load(cli_root)
    output_dir = config.output_path
    for aid, web_bytes in web_mmd.items():
        cli_file = output_dir / aid
        assert cli_file.exists(), f"CLI did not produce {aid}"
        assert sha256_bytes(web_bytes) == sha256_bytes(cli_file.read_bytes())


def test_partial_budget_exhausted_result_is_labeled_incomplete(tmp_path: Path) -> None:
    """When discovery stops on request budget, job state and result clearly indicate partial."""
    provider = Replay(asks)
    with running(tmp_path, Factory(provider)) as (api, _app, _factory):
        workspace_id = api.workspace()
        prov_id = api.provider()
        preview = api.preview(workspace_id, prov_id)
        r = api.submit(
            request(
                workspace_id,
                key="partial-viewer-1",
                provider=prov_id,
                context=preview["context_hash"],
                max_requests=1,
            )
        )
        assert r.status_code == 202
        job = api.wait(r.json()["job_id"])
        assert job["state"] in ("partial", "budget-exhausted")
        assert job["error"] is not None
        assert job["error"]["code"] in ("budget-exhausted", "provider-failure")
        if job["result"] is not None:
            assert job["result"]["completeness"] == "partial"


def test_stale_job_preserves_stale_state_and_error(tmp_path: Path) -> None:
    """Rescanning after source changes marks an earlier job as stale with stale-source error."""
    provider = Replay(full_answer)
    with running(tmp_path, Factory(provider)) as (api, _app, _factory):
        workspace_id = api.workspace()
        prov_id = api.provider()
        preview = api.preview(workspace_id, prov_id)
        r1 = api.submit(
            request(
                workspace_id,
                key="stale-viewer-1",
                provider=prov_id,
                context=preview["context_hash"],
            )
        )
        assert r1.status_code == 202
        job1 = api.wait(r1.json()["job_id"])
        assert job1["state"] == "succeeded"

        # Modify the approved local source so the next scan produces a new source_ir_hash
        extra_file = tmp_path / "approved" / "shop" / "new_module.js"
        extra_file.write_text("export function extra() { return 1; }\n", encoding="utf-8")

        api.preview(workspace_id)
        job1_after = api.job(job1["job_id"])
        assert job1_after["state"] == "stale"
        assert job1_after["error"] is not None
        assert job1_after["error"]["code"] == "stale-source"


def test_unsafe_artifact_id_is_rejected(tmp_path: Path) -> None:
    """Path traversal or malformed artifact names return 404."""
    with running(tmp_path) as (api, _app, _factory):
        workspace_id = api.workspace()
        api.preview(workspace_id)
        r = api.submit(_deterministic_request(workspace_id, key="unsafe-art-1"))
        assert r.status_code == 202
        job = api.wait(r.json()["job_id"])

        for bad_id in ("..%2Frepodocs.json", "not-an-artifact.mmd", "a/b.mmd"):
            resp = api.download(job["job_id"], bad_id)
            assert resp.status_code == 404


def test_generated_architecture_evidence_json_uses_dictionary_contract(tmp_path: Path) -> None:
    """architecture-view-evidence.json stores dict-keyed components/connections and repository metadata."""
    provider = Replay(full_answer)
    with running(tmp_path, Factory(provider)) as (api, _app, factory):
        workspace_id = api.workspace()
        prov_id = api.provider()
        preview = api.preview(workspace_id, prov_id)
        r = api.submit(
            request(
                workspace_id,
                key="evidence-contract-1",
                provider=prov_id,
                context=preview["context_hash"],
                views=("overview", "components"),
            )
        )
        assert r.status_code == 202
        job = api.wait(r.json()["job_id"])
        assert job["state"] == "succeeded"
        assert "architecture-view-evidence.json" in job["result"]["artifact_ids"]

        ev_resp = api.download(job["job_id"], "architecture-view-evidence.json")
        assert ev_resp.status_code == 200
        ev = json.loads(ev_resp.content.decode("utf-8"))
        assert isinstance(ev["components"], dict) and len(ev["components"]) > 0
        assert isinstance(ev["connections"], dict)
        assert isinstance(ev["repositories"], list) and len(ev["repositories"]) > 0
        assert "content_fingerprint" in ev["repositories"][0]
        assert "revision_kind" in ev["repositories"][0]
        # Downloading evidence did not trigger another provider call.
        assert len(factory.contexts) == 1


def test_offline_html_export_download_when_bundle_installed(tmp_path: Path) -> None:
    """When architecture.html_export is enabled and Mermaid bundle is installed, offline HTML downloads safely."""
    from repodocs.architecture.export import HTML_NAME, mermaid_bundle

    if mermaid_bundle() is None:
        pytest.skip("optional Mermaid bundle is not installed")

    provider = Replay(full_answer)
    with running(tmp_path, Factory(provider)) as (api, _app, factory):
        workspace_id = api.workspace()
        cfg_path = tmp_path / "store" / workspace_id / "repodocs.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.setdefault("architecture", {})["html_export"] = True
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        prov_id = api.provider()
        preview = api.preview(workspace_id, prov_id)
        r = api.submit(
            request(
                workspace_id,
                key="offline-html-1",
                provider=prov_id,
                context=preview["context_hash"],
                views=("overview", "components"),
            )
        )
        assert r.status_code == 202
        job = api.wait(r.json()["job_id"])
        assert job["state"] == "succeeded"
        assert HTML_NAME in job["result"]["artifact_ids"]

        html_resp = api.download(job["job_id"], HTML_NAME)
        assert html_resp.status_code == 200
        assert f'attachment; filename="{HTML_NAME}"' == html_resp.headers.get("content-disposition")
        assert b"Content-Security-Policy" in html_resp.content
        assert len(factory.contexts) == 1
