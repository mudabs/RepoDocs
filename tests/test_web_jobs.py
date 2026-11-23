"""The HTTP jobs adapter (#72, ADR-036): replayed providers only, no live model, no credential, no network.

Every job body and every error body a test sees is validated against ``schemas/web-workflow-1.0.json``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from jsonschema import Draft202012Validator

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodocs.ai import providers as provider_module  # noqa: E402
from repodocs.ai.budgets import RunLimits  # noqa: E402
from repodocs.errors import ProviderError  # noqa: E402
from repodocs.web.app import WebSettings, create_app  # noqa: E402
from repodocs.web.jobs import JobService, JobSettings  # noqa: E402
from repodocs.web.security import Access, AccessConfig, ConsentLedger, ProviderSessions, SessionGuard  # noqa: E402
from repodocs.web.workspaces import WorkspaceManager, WorkspaceSettings  # noqa: E402

from conftest import invoke  # noqa: E402
from test_architecture_discovery import FIXTURE, Scripted, full_answer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Draft202012Validator(json.loads((ROOT / "schemas" / "web-workflow-1.0.json").read_text(encoding="utf-8")))
ORIGIN = "http://127.0.0.1:8000"
CANARY = "sk-ant-canary-0123456789abcdefghijklmnopqrstuv"
TERMINAL = {"succeeded", "partial", "budget-exhausted", "cancelled", "failed", "stale", "interrupted"}


def asks(message: dict[str, Any]) -> dict[str, Any]:
    """A useful first answer that asks for one more excerpt, so discovery wants a second request."""

    return full_answer(message, complete=False, requests=[{"repository_id": "shop", "path": "lib/storage.js", "line_start": 1, "line_end": 3}])


class Replay(Scripted):
    """A scripted provider that can hold a request open, and can pretend to be external."""

    def __init__(self, *script: Any, name: str = "ollama", external: bool = False) -> None:
        super().__init__(*script)
        self.name, self.external = name, external
        self.started, self.release = threading.Event(), threading.Event()
        self.release.set()

    def hold(self) -> "Replay":
        self.release.clear()
        return self

    def generate(self, system: str, user: str, schema: dict[str, Any]):
        self.started.set()
        assert self.release.wait(30), "the test never released the provider"
        return super().generate(system, user, schema)


class Factory:
    """Builds the run's provider from its context, as the real factory does, and records every context it saw."""

    def __init__(self, *providers: Replay) -> None:
        self.providers = list(providers)
        self.contexts: list[Any] = []
        self.secrets: list[str | None] = []

    def __call__(self, context: Any, limits: RunLimits) -> Replay:
        self.contexts.append(context)
        self.secrets.append(None if context.secret is None else context.secret.reveal())
        assert self.providers, "a provider was built that the test did not expect"
        return self.providers.pop(0)


def shop(tmp_path: Path) -> Path:
    path = tmp_path / "approved" / "shop"
    if not path.exists():
        shutil.copytree(FIXTURE, path)
    return path


def settings(tmp_path: Path, **jobs: Any) -> WebSettings:
    return WebSettings(
        AccessConfig(),
        WorkspaceSettings(tmp_path / "store", {"shop": shop(tmp_path)}),
        JobSettings(run_limits=RunLimits(context_window_tokens=131072), **jobs),
    )


class Api:
    """One browser session against the service."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.seen: list[Any] = []
        response = client.post("/api/v1/session", headers={"Origin": ORIGIN})
        assert response.status_code == 201, response.text
        self.write = {"Origin": ORIGIN, "x-csrf-token": response.json()["csrf_token"]}

    def _checked(self, response: Any) -> Any:
        self.seen.append(response)
        if response.headers.get("content-type", "").startswith("application/json") and response.content:
            body = response.json()
            if isinstance(body, dict) and body.get("kind") in ("job", "error"):
                CONTRACT.validate(body)
        return response

    def post(self, path: str, body: Any = None, **headers: str) -> Any:
        return self._checked(self.client.post(path, json=body, headers={**self.write, **headers}))

    def get(self, path: str) -> Any:
        return self._checked(self.client.get(path, headers={"Origin": ORIGIN}))

    def workspace(self, name: str = "shop") -> str:
        response = self.post("/api/v1/workspaces", {"name": "Shop", "sources": [{"kind": "local", "name": name}]})
        assert response.status_code == 201, response.text
        return response.json()["workspace_id"]

    def provider(self, provider: str = "ollama", host: str = "http://127.0.0.1:11434", api_key: str | None = None) -> str:
        body = {"provider": provider, "host": host, "model": "replay-model", **({"api_key": api_key} if api_key else {})}
        response = self.post("/api/v1/providers", body)
        assert response.status_code == 201, response.text
        return response.json()["provider_session_id"]

    def preview(self, workspace: str, provider: str | None = None, **extra: Any) -> dict[str, Any]:
        response = self.post(f"/api/v1/workspaces/{workspace}/preview", {"provider_session_id": provider, **extra})
        assert response.status_code == 200, response.text
        return response.json()

    def consent(self, workspace: str, provider: str, context_hash: str, kinds: tuple[str, ...] = ("external", "source-excerpts")) -> Any:
        return self.post("/api/v1/consents", {"workspace_id": workspace, "provider_session_id": provider, "context_hash": context_hash, "kinds": list(kinds)})

    def submit(self, body: dict[str, Any]) -> Any:
        return self.post("/api/v1/jobs", body)

    def job(self, job_id: str) -> dict[str, Any]:
        response = self.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def wait(self, job_id: str, until: Callable[[dict[str, Any]], bool] = lambda job: job["state"] in TERMINAL) -> dict[str, Any]:
        deadline, sequence = time.monotonic() + 30, 0
        while True:
            job = self.job(job_id)
            assert job["sequence"] >= sequence, "the sequence never goes back"
            sequence = job["sequence"]
            if until(job):
                return job
            assert time.monotonic() < deadline, f"job stuck in {job['state']}/{job['stage']}"
            time.sleep(0.01)

    def download(self, job_id: str, artifact: str) -> Any:
        return self.get(f"/api/v1/jobs/{job_id}/artifacts/{artifact}")


def request(workspace: str, *, key: str = "k1", mode: str = "architecture", provider: str | None = None, context: str | None = None,
            receipt: str | None = None, views: tuple[str, ...] = ("overview", "components"), max_requests: int = 3,
            total: int | None = 600_000, cost: int | None = None, refresh: bool | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract_version": "1.0", "kind": "request", "workspace_id": workspace, "idempotency_key": key, "mode": mode,
        "provider_session_id": provider, "context_hash": context, "consent_receipt_id": receipt, "views": list(views),
        "limits": {"max_requests": max_requests, "max_total_tokens": total, "max_cost_microunits": cost, "currency": None if cost is None else "USD"},
    }
    if mode == "deterministic":
        body["limits"] = {"max_requests": 0, "max_total_tokens": None, "max_cost_microunits": 0, "currency": "USD"}
        body["provider_session_id"] = body["consent_receipt_id"] = body["context_hash"] = None
    if refresh is not None:
        body["refresh"] = refresh
    return body


@contextmanager
def running(tmp_path: Path, factory: Factory | None = None, **jobs: Any) -> Iterator[tuple[Api, Any, Factory]]:
    factory = factory or Factory()
    app = create_app(settings(tmp_path, **jobs), provider_factory=factory)
    with TestClient(app, base_url=ORIGIN) as client:
        yield Api(client), app, factory


def architecture_job(api: Api, workspace: str | None = None, **overrides: Any) -> tuple[str, str, dict[str, Any]]:
    workspace = workspace or api.workspace()
    provider = api.provider()
    preview = api.preview(workspace, provider)
    response = api.submit(request(workspace, provider=provider, context=preview["context_hash"], **overrides))
    assert response.status_code == 202, response.text
    return workspace, provider, response.json()


def workspace_root(tmp_path: Path, workspace: str) -> Path:
    return tmp_path / "store" / workspace


def preview_hash(api: Api) -> str:
    """The context hash of the session's latest successful preview."""

    return next(r.json()["context_hash"] for r in reversed(api.seen) if r.request.url.path.endswith("/preview") and r.status_code == 200)


# ------------------------------------------------------------------ the replay journey
def test_a_replay_job_goes_from_source_selection_to_validated_downloads(tmp_path: Path, capsys) -> None:
    provider = Replay(full_answer).hold()
    with running(tmp_path, Factory(provider)) as (api, _, factory):
        workspace, _, queued = architecture_job(api)
        assert queued["state"] == "queued" and queued["stage"] == "queued" and queued["result"] is None
        assert queued["budget"]["remaining_requests"] == 3 and queued["budget"]["remaining_total_tokens"] == 600_000
        assert provider.started.wait(30)
        held = api.wait(queued["job_id"], lambda job: job["state"] == "running")
        assert held["stage"] == "discover" and held["result"] is None
        provider.release.set()
        job = api.wait(queued["job_id"])

        assert job["state"] == "succeeded" and job["stage"] == "finished" and job["error"] is None
        result = job["result"]
        assert result["completeness"] == "complete" and result["view_hash"] and result["warnings"] == []
        assert {"architecture.md", "architecture.mmd", "architecture-view.mmd", "architecture-view-evidence.json"} <= set(result["artifact_ids"])
        # The replay reports no usage, so the totals are the reservations' upper bounds: estimated, never zero.
        assert job["usage"]["accounting"] == "estimated" and job["usage"]["input_tokens"] > 0
        assert job["budget"]["requests"] == 1 and job["budget"]["usage_complete"] is False
        output = workspace_root(tmp_path, workspace) / "destination" / "docs" / "generated"
        manifest = json.loads((workspace_root(tmp_path, workspace) / ".repodocs" / "artifact-manifest.json").read_text(encoding="utf-8"))
        hashes = {item["path"]: item["content_hash"] for item in manifest["artifacts"]}
        for name in result["artifact_ids"]:
            download = api.download(job["job_id"], name)
            assert download.status_code == 200 and download.content == (output / name).read_bytes()
            assert download.headers["content-disposition"] == f'attachment; filename="{name}"'
            assert download.headers["content-security-policy"].startswith("sandbox")
            assert hashlib.sha256(download.content).hexdigest() == hashes[name]
    assert len(factory.contexts) == 1 and len(provider.messages) == 1
    # The same engine wrote them: the CLI validates the workspace and has nothing to regenerate.
    root = workspace_root(tmp_path, workspace)
    assert invoke(root, capsys, "validate")[0] == 0
    code, generated = invoke(root, capsys, "generate")
    assert code == 0 and generated["data"]["changed"] is False


def test_a_deterministic_job_needs_no_provider_and_reports_known_zero_usage(tmp_path: Path) -> None:
    with running(tmp_path) as (api, _, factory):
        workspace = api.workspace()
        response = api.submit(request(workspace, mode="deterministic", views=("overview", "components", "erd")))
        assert response.status_code == 202
        job = api.wait(response.json()["job_id"])
    assert job["state"] == "succeeded" and job["budget"] is None
    assert job["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_microunits": 0, "currency": None, "accounting": "known"}
    assert job["result"]["artifact_ids"] == ["architecture.md", "architecture.mmd"]
    assert any("No components artifact: it needs architecture mode" in item for item in job["result"]["warnings"])
    assert any(item.startswith("No erd artifact") for item in job["result"]["warnings"]), "no ERD is invented without table facts"
    assert factory.contexts == []


def test_the_cli_and_the_http_adapter_produce_identical_state_and_artifacts(tmp_path: Path, capsys, monkeypatch) -> None:
    """Both run in the same workspace in turn, because the IR records absolute source paths and hashes them."""

    def outputs(root: Path) -> dict[str, bytes]:
        state, output = root / ".repodocs", root / "destination" / "docs" / "generated"
        return {
            **{name: (state / name).read_bytes() for name in ("ir.json", "architecture-view.json", "artifact-manifest.json")},
            **{f"docs/{path.name}": path.read_bytes() for path in sorted(output.glob("*"))},
        }

    with running(tmp_path, Factory(Replay(full_answer))) as (api, _, _factory):
        workspace, _, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
        downloads = {f"docs/{name}": api.download(job["job_id"], name).content for name in job["result"]["artifact_ids"]}
    root = workspace_root(tmp_path, workspace)
    from_http = outputs(root)
    assert job["state"] == "succeeded" and all(from_http[name] == content for name, content in downloads.items())

    shutil.rmtree(root / ".repodocs")
    shutil.rmtree(root / "destination" / "docs")
    monkeypatch.setattr(provider_module, "resolve_provider", lambda _settings: Scripted(full_answer))
    for command in (("scan",), ("architecture", "discover"), ("generate",), ("validate",)):
        assert invoke(root, capsys, *command)[0] == 0, command
    assert outputs(root) == from_http


# ------------------------------------------------------------------ duplicates, concurrency, ownership
def test_a_repeated_submission_returns_the_original_job_and_a_changed_one_conflicts(tmp_path: Path) -> None:
    with running(tmp_path, Factory(Replay(full_answer))) as (api, _, factory):
        workspace, provider, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
        reviewed = preview_hash(api)
        same = api.submit(request(workspace, provider=provider, context=reviewed))
        assert same.status_code == 200 and same.json()["job_id"] == job["job_id"] and same.json()["state"] == "succeeded"
        # A reconnected provider session is not part of a submission's identity: still the same job, no new run.
        again = api.submit(request(workspace, provider=api.provider(), context=reviewed))
        assert again.status_code == 200 and again.json()["job_id"] == job["job_id"]
        changed = api.submit(request(workspace, provider=provider, context=reviewed, max_requests=2))
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "conflict"
    assert len(factory.contexts) == 1


def direct(tmp_path: Path, factory: Factory, **jobs: Any) -> tuple[JobService, Access, ProviderSessions, ConsentLedger]:
    guard = SessionGuard(AccessConfig())
    owner = guard.open_session({"host": "127.0.0.1:8000"}).owner
    providers, consent = ProviderSessions(guard), ConsentLedger(guard)
    config = settings(tmp_path, **jobs)
    service = JobService(WorkspaceManager(config.workspaces), providers, consent, config.jobs, provider_factory=factory)
    return service, Access(owner), providers, consent


def wait_for(service: JobService, access: Access, job_id: str, until: Callable[[dict[str, Any]], bool] = lambda job: job["state"] in TERMINAL) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while not until(job := service.get(access, job_id)):
        assert time.monotonic() < deadline, f"job stuck in {job['state']}/{job['stage']}"
        time.sleep(0.01)
    CONTRACT.validate(job)
    return job


def test_simultaneous_duplicate_submissions_create_exactly_one_job(tmp_path: Path) -> None:
    service, access, _, _ = direct(tmp_path, Factory())
    try:
        workspace = service.create_workspace(access, {"name": "Shop", "sources": [{"kind": "local", "name": "shop"}]})["workspace_id"]
        body, results, barrier = request(workspace, mode="deterministic"), [], threading.Barrier(8)

        def submit() -> None:
            barrier.wait()
            results.append(service.submit(access, body))

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert len({job["job_id"] for job, _ in results}) == 1 and sum(created for _, created in results) == 1
        assert wait_for(service, access, results[0][0]["job_id"])["state"] == "succeeded"
    finally:
        service.close()


def test_one_job_runs_per_workspace_one_more_may_queue_and_a_full_queue_is_refused(tmp_path: Path) -> None:
    first = Replay(full_answer).hold()
    with running(tmp_path, Factory(first, Replay(full_answer))) as (api, _, factory):
        workspace, provider, running_job = architecture_job(api)
        assert first.started.wait(30)
        busy = api.post(f"/api/v1/workspaces/{workspace}/preview", {"provider_session_id": provider})
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
        queued = api.submit(request(workspace, key="k2", provider=provider, context=preview_hash(api)))
        assert queued.status_code == 202 and queued.json()["state"] == "queued"
        full = api.submit(request(workspace, key="k3", provider=provider, context=preview_hash(api)))
        assert full.status_code == 429 and full.json()["error"] == {"code": "queue-full", "message": full.json()["error"]["message"], "retryable": True}
        assert api.job(queued.json()["job_id"])["state"] == "queued", "the second job waits for the workspace"
        first.release.set()
        assert api.wait(running_job["job_id"])["state"] == "succeeded"
        # The queued job reuses the current view: it needs no provider at all.
        assert api.wait(queued.json()["job_id"])["state"] == "succeeded"
    assert len(factory.contexts) == 1


@pytest.mark.parametrize("active, together", [(1, False), (2, True)])
def test_jobs_of_different_workspaces_run_together_only_up_to_the_active_limit(tmp_path: Path, active: int, together: bool) -> None:
    one, two = Replay(full_answer).hold(), Replay(full_answer).hold()
    with running(tmp_path, Factory(one, two), max_active_jobs=active) as (api, _, _factory):
        _, _, first = architecture_job(api)
        assert one.started.wait(30)
        _, _, second = architecture_job(api)
        assert two.started.wait(2) is together
        assert api.job(second["job_id"])["state"] == ("running" if together else "queued")
        one.release.set(), two.release.set()
        assert api.wait(first["job_id"])["state"] == api.wait(second["job_id"])["state"] == "succeeded"


def test_another_session_can_neither_see_nor_touch_a_job_or_workspace(tmp_path: Path) -> None:
    with running(tmp_path, Factory(Replay(full_answer))) as (api, app, _factory):
        workspace, provider, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
        other = Api(TestClient(app, base_url=ORIGIN))
        unknown = other.get("/api/v1/jobs/job_000000000000000000000000")
        for response in (
            other.get(f"/api/v1/jobs/{job['job_id']}"),
            other.post(f"/api/v1/jobs/{job['job_id']}/cancel"),
            other.download(job["job_id"], "architecture.md"),
            other.post(f"/api/v1/workspaces/{workspace}/preview", {}),
            other.submit(request(workspace, mode="deterministic")),
            other.get(f"/api/v1/providers/{provider}"),
        ):
            assert response.status_code == 404 and response.json()["error"]["code"] == "not-found"
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not-found", "an existing job is indistinguishable from none"
        assert api.job(job["job_id"])["state"] == "succeeded"


# ------------------------------------------------------------------ cancellation
def test_cancelling_a_queued_job_means_it_never_starts(tmp_path: Path) -> None:
    first = Replay(full_answer).hold()
    with running(tmp_path, Factory(first)) as (api, _, factory):
        workspace, provider, active = architecture_job(api)
        assert first.started.wait(30)
        waiting = api.submit(request(workspace, key="later", provider=provider, context=preview_hash(api), refresh=True)).json()
        cancelled = api.post(f"/api/v1/jobs/{waiting['job_id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled" and cancelled.json()["stage"] == "queued"
        assert cancelled.json()["usage"]["accounting"] == "known" and cancelled.json()["usage"]["input_tokens"] == 0
        first.release.set()
        assert api.wait(active["job_id"])["state"] == "succeeded"
        assert api.job(waiting["job_id"])["state"] == "cancelled"
    assert len(factory.contexts) == 1, "the cancelled refresh never built a provider"


def test_cancelling_during_inference_sends_no_further_request(tmp_path: Path) -> None:
    provider = Replay(asks, full_answer).hold()
    with running(tmp_path, Factory(provider)) as (api, _, _factory):
        workspace, _, queued = architecture_job(api)
        assert provider.started.wait(30)
        pending = api.post(f"/api/v1/jobs/{queued['job_id']}/cancel")
        assert pending.status_code == 202 and pending.json()["state"] == "running", "acknowledged only when it takes effect"
        provider.release.set()
        job = api.wait(queued["job_id"])
    assert job["state"] == "cancelled" and job["error"]["code"] == "cancelled" and job["result"] is None
    assert len(provider.messages) == 1, "the second request discovery wanted was never sent"
    assert job["budget"]["requests"] == 1 and job["budget"]["stop_reason"] == "cancelled"
    assert job["usage"]["accounting"] == "estimated", "the in-flight request keeps its reservation, never refunded as zero"
    assert not (workspace_root(tmp_path, workspace) / ".repodocs" / "architecture-view.json").exists()


def test_a_cancel_racing_the_last_response_stores_no_view_and_writes_no_artifact(tmp_path: Path) -> None:
    provider = Replay(full_answer).hold()
    with running(tmp_path, Factory(provider)) as (api, _, _factory):
        workspace, _, queued = architecture_job(api)
        assert provider.started.wait(30)
        api.post(f"/api/v1/jobs/{queued['job_id']}/cancel")
        provider.release.set()  # the answer is complete; the cancel lands before the view is stored
        job = api.wait(queued["job_id"])
    root = workspace_root(tmp_path, workspace)
    assert job["state"] == "cancelled" and len(provider.messages) == 1
    assert not (root / ".repodocs" / "architecture-view.json").exists()
    assert not (root / "destination" / "docs" / "generated" / "architecture-view.mmd").exists()


def test_cancelling_during_preflight_builds_no_provider(tmp_path: Path, monkeypatch) -> None:
    from repodocs import service

    entered, release, original = threading.Event(), threading.Event(), service.scan

    def slow_scan(request: Any, hooks: Any = service.NO_HOOKS) -> Any:
        entered.set()
        assert release.wait(30)
        return original(request, hooks)

    with running(tmp_path, Factory()) as (api, _, factory):
        workspace = api.workspace()
        provider = api.provider()
        preview = api.preview(workspace, provider)
        monkeypatch.setattr(service, "scan", slow_scan)
        queued = api.submit(request(workspace, provider=provider, context=preview["context_hash"])).json()
        assert entered.wait(30)
        held = api.job(queued["job_id"])
        assert held["state"] == "preflight" and held["stage"] == "scan"
        assert api.post(f"/api/v1/jobs/{queued['job_id']}/cancel").status_code == 202
        release.set()
        job = api.wait(queued["job_id"])
    assert job["state"] == "cancelled" and job["usage"]["accounting"] == "known" and job["usage"]["input_tokens"] == 0
    assert factory.contexts == []


def test_a_cancel_between_dequeue_and_preflight_stops_the_job_before_any_work(tmp_path: Path, monkeypatch) -> None:
    """Regression: a worker took the job off the queue while it still read ``queued``, so a cancel tried to remove it from
    the queue again, failed with a server error and left the job to run. Claiming is now one step under the lock."""

    from repodocs import service as shared

    claimed, release = threading.Event(), threading.Event()
    run, scan, scans = JobService._run, shared.scan, []

    def paused(self: JobService, job: Any, workspace: Any) -> None:
        claimed.set()  # dequeued by the worker, nothing of the job has run yet
        assert release.wait(30), "the test never released the worker"
        run(self, job, workspace)

    with running(tmp_path, Factory()) as (api, app, factory):
        workspace = api.workspace()
        provider = api.provider()
        preview = api.preview(workspace, provider)
        monkeypatch.setattr(JobService, "_run", paused)
        monkeypatch.setattr(shared, "scan", lambda request, hooks=shared.NO_HOOKS: scans.append(request) or scan(request, hooks))
        first = api.submit(request(workspace, provider=provider, context=preview["context_hash"])).json()
        assert claimed.wait(30)
        jobs = app.state.jobs
        with jobs._cond:
            assert first["job_id"] not in jobs._queue, "the worker holds the job"

        cancelling = api.post(f"/api/v1/jobs/{first['job_id']}/cancel")
        assert cancelling.status_code == 202, cancelling.text
        assert cancelling.json()["state"] == "preflight" and cancelling.json()["stage"] == "scan"

        # The claimed job no longer occupies the workspace's queue slot: one more may queue, and the next is refused.
        second = api.submit(request(workspace, key="k2", mode="deterministic"))
        assert second.status_code == 202 and second.json()["state"] == "queued"
        third = api.submit(request(workspace, key="k3", mode="deterministic"))
        assert third.status_code == 429 and third.json()["error"]["code"] == "queue-full"
        settled = api.post(f"/api/v1/jobs/{second.json()['job_id']}/cancel")
        assert settled.status_code == 200 and settled.json()["state"] == "cancelled", "a queued job still settles at once"
        release.set()
        job = api.wait(first["job_id"])

    assert all(response.status_code < 500 for response in api.seen)
    assert job["state"] == "cancelled" and job["error"]["code"] == "cancelled" and job["result"] is None
    assert job["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_microunits": 0, "currency": None, "accounting": "known"}
    assert factory.contexts == [] and scans == [], "no provider was built and the job never scanned"
    root = workspace_root(tmp_path, workspace)
    assert not (root / "destination" / "docs" / "generated").exists(), "no artifact was generated"
    assert json.loads((root / "jobs" / f"{first['job_id']}.json").read_text(encoding="utf-8"))["job"]["state"] == "cancelled"


def test_cancelling_a_finished_job_changes_nothing(tmp_path: Path) -> None:
    with running(tmp_path) as (api, _, _factory):
        job = api.wait(api.submit(request(api.workspace(), mode="deterministic")).json()["job_id"])
        response = api.post(f"/api/v1/jobs/{job['job_id']}/cancel")
    assert response.status_code == 200 and response.json() == job


# ------------------------------------------------------------------ provider failures and budgets
def test_a_provider_timeout_on_the_first_request_is_a_failure_without_a_result(tmp_path: Path) -> None:
    with running(tmp_path, Factory(Replay(ProviderError("Unable to reach Ollama: timed out")))) as (api, _, _factory):
        workspace, _, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
        assert api.download(job["job_id"], "architecture.md").status_code == 404
    assert job["state"] == "failed" and job["result"] is None and job["error"]["code"] == "provider-failure"
    assert "timed out" in job["error"]["message"] and "No architecture result was produced" in job["error"]["message"]
    assert job["budget"]["requests"] == 1
    assert not (workspace_root(tmp_path, workspace) / ".repodocs" / "architecture-view.json").exists()


def test_a_timeout_after_useful_output_is_partial_and_never_succeeded(tmp_path: Path) -> None:
    with running(tmp_path, Factory(Replay(asks, ProviderError("timed out")))) as (api, _, _factory):
        _, _, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
    assert job["state"] == "partial" and job["error"]["code"] == "provider-failure"
    assert job["result"]["completeness"] == "partial" and "Not a current complete architecture result" in job["result"]["warnings"]


@pytest.mark.parametrize("script", [
    ({"components": "not a list"}, {"components": "still not a list"}),
    (ProviderError("The model output was cut off at the token limit"),),
    (lambda message: {"components": [], "connections": [], "alternatives": [], "requests": [], "complete": True},),
], ids=["malformed-twice", "truncated", "empty"])
def test_malformed_truncated_or_empty_answers_never_become_a_success(tmp_path: Path, script: tuple[Any, ...]) -> None:
    with running(tmp_path, Factory(Replay(*script))) as (api, _, _factory):
        _, _, queued = architecture_job(api)
        job = api.wait(queued["job_id"])
    assert job["state"] in ("failed", "partial") and job["error"] is not None
    assert job["result"] is None or job["result"]["completeness"] == "partial"


def test_a_budget_too_small_for_the_first_request_sends_nothing(tmp_path: Path) -> None:
    provider = Replay(full_answer)
    with running(tmp_path, Factory(provider)) as (api, _, _factory):
        _, _, queued = architecture_job(api, total=500)
        job = api.wait(queued["job_id"])
    assert job["state"] == "budget-exhausted" and job["error"]["code"] == "budget-exhausted" and job["result"] is None
    assert job["budget"]["stop_reason"] == "total-token-limit" and job["budget"]["requests"] == 0
    assert provider.messages == [], "nothing was sent"
    assert job["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_microunits": None, "currency": None, "accounting": "known"}


def test_a_budget_exhausted_after_a_request_keeps_its_partial_result_labelled(tmp_path: Path) -> None:
    provider = Replay(asks, full_answer)
    with running(tmp_path, Factory(provider)) as (api, _, _factory):
        _, _, queued = architecture_job(api, max_requests=1)
        job = api.wait(queued["job_id"])
    assert job["state"] == "budget-exhausted" and job["error"]["code"] == "budget-exhausted"
    assert job["result"]["completeness"] == "partial" and job["budget"]["stop_reason"] == "request-limit"
    assert len(provider.messages) == 1


def test_a_cost_cap_without_a_price_profile_is_refused_at_admission(tmp_path: Path) -> None:
    with running(tmp_path, Factory()) as (api, _, factory):
        workspace = api.workspace()
        provider = api.provider()
        preview = api.preview(workspace, provider)
        response = api.submit(request(workspace, provider=provider, context=preview["context_hash"], cost=1000))
    assert response.status_code == 422 and "ai.prices" in response.json()["error"]["message"]
    assert factory.contexts == []


# ------------------------------------------------------------------ restart
def copy_store(tmp_path: Path) -> Path:
    target = tmp_path / "restarted"
    shutil.copytree(tmp_path / "store", target / "store")
    shutil.copytree(tmp_path / "approved", target / "approved")
    return target


def test_a_restart_interrupts_unfinished_jobs_and_never_repeats_paid_work(tmp_path: Path) -> None:
    provider = Replay(full_answer).hold()
    service, access, providers, _ = direct(tmp_path, Factory(provider))
    try:
        workspace = service.create_workspace(access, {"name": "Shop", "sources": [{"kind": "local", "name": "shop"}]})["workspace_id"]
        handle = providers.connect(access, "ollama", "http://127.0.0.1:11434", "replay-model")
        preview = service.preview(access, workspace, {})
        running_job, _ = service.submit(access, request(workspace, provider=handle, context=preview["context_hash"]))
        assert provider.started.wait(30)
        waiting, _ = service.submit(access, request(workspace, key="second", mode="deterministic"))
        # What a crash leaves on disk at this moment: one job mid-request, one queued.
        restarted = copy_store(tmp_path)
    finally:
        provider.release.set()
        service.close()

    nothing = Factory()
    after, _, _, _ = direct(restarted, nothing)
    try:
        interrupted = wait_for(after, access, running_job["job_id"])
        queued = wait_for(after, access, waiting["job_id"])
        assert interrupted["state"] == "interrupted" and interrupted["error"]["code"] == "interrupted" and interrupted["result"] is None
        assert "during discover" in interrupted["error"]["message"] and "not resumed" in interrupted["error"]["message"]
        assert interrupted["usage"]["accounting"] == "unknown" and interrupted["usage"]["input_tokens"] is None, "unknown, not zero"
        assert queued["state"] == "interrupted" and queued["usage"]["accounting"] == "known"
        # Repeating the submission returns the record; it never starts the paid work again.
        replay, created = after.submit(access, request(workspace, provider=handle, context=preview["context_hash"]))
        assert created is False and replay["job_id"] == running_job["job_id"] and replay["state"] == "interrupted"
        time.sleep(0.2)
        assert nothing.contexts == [] and after.get(access, waiting["job_id"])["state"] == "interrupted"
    finally:
        after.close()


def test_recovery_ignores_foreign_malformed_and_linked_records(tmp_path: Path) -> None:
    service, access, _, _ = direct(tmp_path, Factory())
    workspace = service.create_workspace(access, {"name": "Shop", "sources": [{"kind": "local", "name": "shop"}]})["workspace_id"]
    job, _ = service.submit(access, request(workspace, mode="deterministic"))
    wait_for(service, access, job["job_id"])
    service.close()
    jobs = workspace_root(tmp_path, workspace) / "jobs"
    (jobs / "job_bad.json").write_text("{", encoding="utf-8")
    record = json.loads((jobs / f"{job['job_id']}.json").read_text(encoding="utf-8"))
    (jobs / "job_other.json").write_text(json.dumps({**record, "job": {**record["job"], "job_id": "job_elsewhere"}}), encoding="utf-8")
    try:
        (jobs / "job_link.json").symlink_to(jobs / f"{job['job_id']}.json")
    except OSError:
        pass
    after, _, _, _ = direct(tmp_path, Factory())
    try:
        assert set(after._jobs) == {job["job_id"]}
    finally:
        after.close()


# ------------------------------------------------------------------ reuse, refresh, stale, workspace access
def test_a_new_job_reuses_the_current_view_and_only_refresh_asks_the_provider_again(tmp_path: Path) -> None:
    with running(tmp_path, Factory(Replay(full_answer), Replay(full_answer))) as (api, _, factory):
        workspace, provider, first = architecture_job(api)
        assert api.wait(first["job_id"])["state"] == "succeeded"
        preview = api.preview(workspace, provider)
        reused = api.wait(api.submit(request(workspace, key="k2", provider=provider, context=preview["context_hash"])).json()["job_id"])
        assert reused["state"] == "succeeded" and reused["usage"]["input_tokens"] == 0
        assert reused["budget"]["requests"] == 0, "the admitted allowance is reported, and none of it was used"
        assert len(factory.contexts) == 1
        refreshed = api.wait(api.submit(request(workspace, key="k3", provider=provider, context=preview["context_hash"], refresh=True)).json()["job_id"])
        assert refreshed["state"] == "succeeded" and refreshed["budget"]["requests"] == 1
    assert len(factory.contexts) == 2


def test_changed_sources_make_earlier_results_stale_and_old_previews_unusable(tmp_path: Path) -> None:
    with running(tmp_path, Factory(Replay(full_answer))) as (api, _, factory):
        workspace, provider, first = architecture_job(api)
        job = api.wait(first["job_id"])
        old_hash = preview_hash(api)
        server = shop(tmp_path) / "server.js"
        server.write_text(server.read_text(encoding="utf-8") + "\n// a local edit\n", encoding="utf-8")
        new = api.preview(workspace, provider)
        stale = api.job(job["job_id"])
        assert new["context_hash"] != old_hash
        assert stale["state"] == "stale" and stale["error"]["code"] == "stale-source" and stale["result"] == job["result"]
        assert stale["sequence"] > job["sequence"]
        refused = api.submit(request(workspace, key="k2", provider=provider, context=old_hash))
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "stale-preview"
    assert len(factory.contexts) == 1


def test_an_edited_or_replaced_artifact_is_refused_as_stale(tmp_path: Path) -> None:
    with running(tmp_path) as (api, _, _factory):
        workspace = api.workspace()
        job = api.wait(api.submit(request(workspace, mode="deterministic")).json()["job_id"])
        output = workspace_root(tmp_path, workspace) / "destination" / "docs" / "generated"
        (output / "architecture.md").write_text("edited by hand\n", encoding="utf-8")
        edited = api.download(job["job_id"], "architecture.md")
        assert edited.status_code == 409 and edited.json()["error"]["code"] == "stale-artifact"
        (output / "architecture.mmd").unlink()
        try:
            (output / "architecture.mmd").symlink_to(workspace_root(tmp_path, workspace) / "repodocs.json")
        except OSError:
            pytest.skip("symbolic links are not available here")
        linked = api.download(job["job_id"], "architecture.mmd")
        assert linked.status_code == 409 and b"repodocs" not in linked.content.split(b'"message"')[0]


# A literal ".." never reaches the route: clients, like browsers, remove dot segments before sending.
@pytest.mark.parametrize("name", ["repodocs.json", "...", "ir.json", "architecture-context.json", ".repodocs-workspace.json",
                                  "%2e%2e%2frepodocs.json", "..%2f..%2frepodocs.json", "architecture.md%00"])
def test_only_artifacts_the_job_recorded_can_be_downloaded(tmp_path: Path, name: str) -> None:
    with running(tmp_path) as (api, _, _factory):
        job = api.wait(api.submit(request(api.workspace(), mode="deterministic")).json()["job_id"])
        response = api.download(job["job_id"], name)
    assert response.status_code == 404
    assert b'"kind":"job"' not in response.content and b"source_ir_hash" not in response.content


def test_source_excerpts_are_returned_only_when_asked_for(tmp_path: Path) -> None:
    marker = "storage.appendOrder"  # in the fixture's server.js, which the context excerpts
    with running(tmp_path) as (api, _, _factory):
        workspace = api.workspace()
        summary = api.post(f"/api/v1/workspaces/{workspace}/preview", {})
        assert "excerpts" not in summary.json() and marker not in summary.text
        assert str(tmp_path) not in summary.text, "no server path reaches the browser"
        detailed = api.preview(workspace, include_documents=True)
        assert any(marker in item["text"] for item in detailed["excerpts"])
        job = api.wait(api.submit(request(workspace, mode="deterministic")).json()["job_id"])
        assert all(marker not in api.download(job["job_id"], name).text for name in job["result"]["artifact_ids"])


@pytest.mark.parametrize("source", [
    {"kind": "local", "name": "../approved/shop"},
    {"kind": "local", "name": "/etc"},
    {"kind": "local", "path": "/tmp"},
    {"kind": "github", "owner": "acme", "repository": "shop", "revision": "main"},
    {"kind": "archive", "url": "https://example.test/x.zip"},
])
def test_a_browser_can_name_only_configured_sources_never_paths(tmp_path: Path, source: dict[str, Any]) -> None:
    with running(tmp_path) as (api, _, _factory):
        response = api.post("/api/v1/workspaces", {"name": "Shop", "sources": [source]})
    assert response.status_code == 422 and response.json()["error"]["code"] in ("invalid-request", "workspace-failure")
    assert str(tmp_path) not in response.text
    assert not any((tmp_path / "store").glob("ws-*"))


# ------------------------------------------------------------------ access, CSRF, CORS, consent, secrets
@pytest.mark.parametrize("headers, status", [
    ({"Origin": "https://evil.example"}, 403),
    ({"Origin": "null"}, 403),
    ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, 403),
    ({"Origin": ORIGIN, "x-csrf-token": "wrong"}, 403),
    ({"Origin": ORIGIN, "x-csrf-token": ""}, 403),
    ({"Origin": ORIGIN, "Host": "rebound.example:8000"}, 403),
], ids=["foreign-origin", "null-origin", "cross-site", "wrong-csrf", "empty-csrf", "rebinding-host"])
def test_an_unrelated_website_cannot_start_a_job(tmp_path: Path, headers: dict[str, str], status: int) -> None:
    with running(tmp_path, Factory()) as (api, app, factory):
        workspace = api.workspace()
        provider = api.provider()
        preview = api.preview(workspace, provider)
        body = request(workspace, provider=provider, context=preview["context_hash"])
        attack = {**{"Origin": ORIGIN, "x-csrf-token": api.write["x-csrf-token"]}, **headers}
        response = api.client.post("/api/v1/jobs", json=body, headers=attack)
        assert response.status_code == status and response.json()["error"]["code"] == "access-denied"
        CONTRACT.validate(response.json())
        assert app.state.jobs._jobs == {}
    assert factory.contexts == []


def test_requests_without_a_session_or_origin_are_refused(tmp_path: Path) -> None:
    with running(tmp_path) as (api, app, _factory):
        stranger = TestClient(app, base_url=ORIGIN)
        workspace = api.workspace()
        no_session = stranger.post("/api/v1/jobs", json=request(workspace, mode="deterministic"), headers={"Origin": ORIGIN})
        no_origin = api.client.post("/api/v1/jobs", json=request(workspace, mode="deterministic"), headers={"x-csrf-token": api.write["x-csrf-token"]})
        assert no_session.status_code == 401 and no_session.json()["error"]["code"] == "session-required"
        assert no_origin.status_code == 403 and app.state.jobs._jobs == {}


def test_no_cors_is_offered_and_every_response_is_hardened(tmp_path: Path) -> None:
    with running(tmp_path) as (api, _, _factory):
        preflight = api.client.options("/api/v1/jobs", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-csrf-token",
        })
        job = api.wait(api.submit(request(api.workspace(), mode="deterministic")).json()["job_id"])
        api.download(job["job_id"], "architecture.md")
        api.get("/api/v1/jobs/job_unknown")
        api._checked(api.client.get("/no-such-route"))
        api._checked(api.client.get("/docs"))
    assert preflight.status_code >= 400
    for response in [preflight, *api.seen]:
        assert not any(name.lower().startswith("access-control-") for name in response.headers)
        assert response.headers["x-content-type-options"] == "nosniff" and response.headers["cache-control"] == "no-store"
        assert "content-security-policy" in response.headers
    assert api.seen[-2].status_code == 404 and api.seen[-1].status_code == 404, "no generated API documentation is served"


def test_an_external_provider_needs_both_consents_for_the_reviewed_preview(tmp_path: Path) -> None:
    external = Replay(full_answer, name="anthropic", external=True)
    with running(tmp_path, Factory(external)) as (api, _, factory):
        workspace = api.workspace()
        provider = api.provider("anthropic", "https://api.example.test", api_key=CANARY)
        preview = api.preview(workspace, provider)
        assert preview["provider_is_external"] is True and preview["provider"]["credential"] == "stored"
        body = request(workspace, provider=provider, context=preview["context_hash"])
        missing = api.submit(body)
        assert missing.status_code == 403 and missing.json()["error"]["code"] == "consent-required"
        partial = api.consent(workspace, provider, preview["context_hash"], ("external",)).json()["consent_receipt_id"]
        narrow = api.submit({**body, "consent_receipt_id": partial})
        assert narrow.status_code == 403
        stale = api.consent(workspace, provider, "0" * 64)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale-preview"
        receipt = api.consent(workspace, provider, preview["context_hash"]).json()["consent_receipt_id"]
        job = api.wait(api.submit({**body, "consent_receipt_id": receipt}).json()["job_id"])
        assert job["state"] == "succeeded"
    assert factory.secrets == [CANARY], "the credential reached the run, and only the run"
    project = json.loads((workspace_root(tmp_path, workspace) / "repodocs.json").read_text(encoding="utf-8"))
    assert project["ai"]["external_consent"] is False and "source_excerpts_consent" not in project["ai"], "consent is never persisted"


def test_revoked_consent_stops_the_next_request_and_a_queued_job(tmp_path: Path) -> None:
    held: dict[str, Any] = {}

    def revoke_then_ask(message: dict[str, Any]) -> dict[str, Any]:
        held["api"].post(f"/api/v1/consents/{held['receipt']}/revoke")
        return asks(message)

    external = Replay(revoke_then_ask, full_answer, name="anthropic", external=True)
    with running(tmp_path, Factory(external)) as (api, _, _factory):
        workspace = api.workspace()
        provider = api.provider("anthropic", "https://api.example.test", api_key=CANARY)
        preview = api.preview(workspace, provider)
        held.update(api=api, receipt=api.consent(workspace, provider, preview["context_hash"]).json()["consent_receipt_id"])
        job = api.wait(api.submit(request(workspace, provider=provider, context=preview["context_hash"], receipt=held["receipt"])).json()["job_id"])
    assert job["state"] == "failed" and job["error"]["code"] == "consent-required" and job["result"] is None
    assert len(external.messages) == 1, "the request after the revocation was never sent"
    assert job["budget"]["requests"] == 1


def test_a_disconnected_credential_cannot_start_its_queued_job(tmp_path: Path) -> None:
    first = Replay(full_answer).hold()
    with running(tmp_path, Factory(first)) as (api, _, factory):
        workspace, provider, active = architecture_job(api)
        assert first.started.wait(30)
        waiting = api.submit(request(workspace, key="k2", provider=provider, context=preview_hash(api), refresh=True)).json()
        assert api.post(f"/api/v1/providers/{provider}/disconnect").status_code == 204
        first.release.set()
        assert api.wait(active["job_id"])["state"] == "succeeded"
        job = api.wait(waiting["job_id"])
    assert job["state"] == "failed" and job["error"]["code"] == "consent-required"
    assert len(factory.contexts) == 1


def test_secret_canaries_never_leave_the_backend(tmp_path: Path, caplog) -> None:
    leaky = Replay(ProviderError(f"upstream said: invalid key {CANARY}"), name="anthropic", external=True)
    caplog.set_level("DEBUG")
    with running(tmp_path, Factory(Replay(full_answer, name="anthropic", external=True), leaky)) as (api, _, factory):
        workspace = api.workspace()
        provider = api.provider("anthropic", "https://api.example.test", api_key=CANARY)
        preview = api.preview(workspace, provider)
        receipt = api.consent(workspace, provider, preview["context_hash"]).json()["consent_receipt_id"]
        body = request(workspace, provider=provider, context=preview["context_hash"], receipt=receipt)
        done = api.wait(api.submit(body).json()["job_id"])
        failed = api.wait(api.submit({**body, "idempotency_key": "k2", "refresh": True}).json()["job_id"])
        for name in done["result"]["artifact_ids"]:
            api.download(done["job_id"], name)
        api.get(f"/api/v1/providers/{provider}")
    assert done["state"] == "succeeded" and failed["state"] == "failed" and "[redacted]" in failed["error"]["message"]
    assert factory.secrets == [CANARY, CANARY]
    for response in api.seen:
        assert CANARY.encode() not in response.content and CANARY not in str(response.headers)
    for path in (tmp_path / "store").rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path
    assert CANARY not in caplog.text


def test_oversized_bodies_are_refused_before_they_are_read(tmp_path: Path) -> None:
    with running(tmp_path, max_queued_jobs=1) as (api, app, _factory):
        workspace = api.workspace()
        body = request(workspace, mode="deterministic")
        body["idempotency_key"] = "x" * (70 * 1024)
        response = api.post("/api/v1/jobs", body)
    assert response.status_code == 413 and response.json()["error"]["code"] == "too-large"
    assert app.state.jobs._jobs == {}


@pytest.mark.parametrize("change", [
    lambda body: body.update(extra=1),
    lambda body: body.update(contract_version="2.0"),
    lambda body: body.update(mode="interactive"),
    lambda body: body.update(views=[]),
    lambda body: body.update(views=["overview", "overview"]),
    lambda body: body["limits"].update(max_requests=7),
    lambda body: body["limits"].update(max_total_tokens=None),
    lambda body: body["limits"].update(max_cost_microunits=0, currency="USD"),
    lambda body: body["limits"].update(max_cost_microunits=5, currency=None),
    lambda body: body.update(refresh="yes"),
    lambda body: body.update(provider_session_id=None),
    lambda body: body.update(idempotency_key="line\nbreak"),
], ids=["unknown-field", "version", "mode", "no-views", "duplicate-views", "too-many-requests", "no-token-cap", "zero-cost-cap",
        "cost-without-currency", "refresh-type", "no-provider", "control-character"])
def test_invalid_requests_are_refused_before_anything_starts(tmp_path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    with running(tmp_path, Factory()) as (api, app, factory):
        workspace = api.workspace()
        provider = api.provider()
        preview = api.preview(workspace, provider)
        body = request(workspace, provider=provider, context=preview["context_hash"])
        change(body)
        response = api.submit(body)
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid-request"
        assert app.state.jobs._jobs == {}
    assert factory.contexts == []


def test_a_deterministic_request_refuses_providers_consent_and_refresh(tmp_path: Path) -> None:
    with running(tmp_path) as (api, _, _factory):
        workspace = api.workspace()
        for change in ({"provider_session_id": "ps_x"}, {"consent_receipt_id": "cr_x"}, {"refresh": True}):
            response = api.submit({**request(workspace, mode="deterministic"), **change})
            assert response.status_code == 422, change
