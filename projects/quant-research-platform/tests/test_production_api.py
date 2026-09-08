from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quant_platform.production_bocom import BocomProductionJob
from quant_platform.production_contract import ProductionRelease, ProductionRequest
from quant_platform.production_gold import GoldProductionJob
from quant_platform.production_jobs import ProductionJobs
from quant_platform.production_result import ProductionResultError, ProductionResultStore
from quant_platform.production_service import AdmissionPolicy, ProductionService
from quant_platform.production_store import ProductionStore
from quant_platform.production_web import VERIFIED_CLIENT_HEADER, create_production_app
from quant_platform.production_worker import ProductionWorker, SimulatedWorkerCrash


FIXTURES = Path(__file__).parent / "fixtures" / "production"
MANIFEST = "6f9f10ed235c6229582ca2843c8b983a887dbdc0ac289170ca834e580bcae969"
FIRE = "2026-03-09T00:40:00Z"
IDENTITY = "CN=quantresearch-ailearn-v1"


class FixtureProvider:
    calls = 0

    def get(self, url, *, headers, maximum_bytes):
        self.calls += 1
        name = "bocom-yahoo-chart.json" if "yahoo" in url else "gold-au9999.tsv"
        payload = (FIXTURES / name).read_bytes()
        assert len(payload) <= maximum_bytes
        return payload


def release() -> ProductionRelease:
    return ProductionRelease.from_mapping(
        {
            "schema": "quantresearch-production-release/v1",
            "production_api_image_digest": "a" * 64,
            "source_commit": "b" * 40,
            "source_tree_sha256": "c" * 64,
            "dependency_lock_sha256": "d" * 64,
            "effective_compose_config_sha256": "e" * 64,
            "provider_contract_sha256": "f" * 64,
            "api_contract_sha256": "0" * 64,
        }
    )


def runtime(tmp_path, *, crash=lambda _point: None, clock=None):
    now = datetime(2026, 3, 9, 0, 40, tzinfo=UTC)
    clock = clock or (lambda: now)
    store = ProductionStore(tmp_path / "state")
    store.initialize()
    results = ProductionResultStore(tmp_path / "result")
    policy = AdmissionPolicy({"297c11cad0dc": MANIFEST}, release())
    service = ProductionService(store, policy, clock=clock)
    jobs = ProductionJobs(
        [
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
        ]
    )
    provider = FixtureProvider()
    worker = ProductionWorker(
        store,
        jobs,
        provider,
        results,
        work_root=tmp_path / "work",
        owner="worker-1",
        clock=clock,
        crash=crash,
    )
    app = create_production_app(service, results, verified_client_identity=IDENTITY)
    return store, results, service, provider, worker, TestClient(app)


def request() -> ProductionRequest:
    return ProductionRequest.build(
        job_id="297c11cad0dc", scheduled_for=FIRE, production_manifest_sha256=MANIFEST
    )


def headers(value: ProductionRequest) -> dict[str, str]:
    return {
        VERIFIED_CLIENT_HEADER: IDENTITY,
        "Idempotency-Key": value.request_id,
        "Content-Type": "application/json",
    }


def test_authenticated_synthetic_asgi_client_to_verified_result(tmp_path) -> None:
    store, results, _, provider, worker, client = runtime(tmp_path)
    value = request()
    assert client.post("/api/v1/production/runs", content=value.canonical_body, headers=headers(value)).status_code == 202
    assert provider.calls == 0

    terminal = worker.run_once()

    assert terminal is not None and terminal["status"] == "SUCCEEDED"
    assert provider.calls == 1
    status = client.get(
        f"/api/v1/production/runs/{terminal['production_run_id']}",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert status.status_code == 200
    result = client.get(
        f"/api/v1/production/results/{terminal['result_id']}",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert result.status_code == 200
    assert results.verify(terminal["result_id"]) == result.json()
    notification = client.get(
        f"/api/v1/production/results/{terminal['result_id']}/files/notification.txt",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert notification.status_code == 200
    assert notification.content == (
        "交通银行 WAIT · 2026-01-22 · report 8991e9a8-1caa-41f5-b76b-6368259db5b4"
    ).encode()
    denied = client.get(
        f"/api/v1/production/results/{terminal['result_id']}/files/action.json",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert denied.status_code == 404
    assert store.get_run(terminal["production_run_id"])["result_id"] == terminal["result_id"]


def test_identity_boundary_rejects_browser_auth_and_unverified_calls(tmp_path) -> None:
    _, _, _, _, _, client = runtime(tmp_path)
    value = request()
    assert client.post("/api/v1/production/runs", content=value.canonical_body).status_code == 403
    browser = headers(value) | {"Cookie": "session=browser", "X-CSRF-Token": "browser"}
    assert client.post("/api/v1/production/runs", content=value.canonical_body, headers=browser).status_code == 403
    assert client.get("/health/live").status_code == 200


def test_crash_recovery_reuses_sealed_acquisition_and_computation(tmp_path) -> None:
    current = [datetime(2026, 3, 9, 0, 40, tzinfo=UTC)]
    compute_calls = [0]
    crash_points = iter(["after_acquisition_sealed", "after_computation_sealed", "after_result_and_report_sealed"])
    armed = [next(crash_points)]

    def clock():
        return current[0]

    def crash(point):
        if point == armed[0]:
            try:
                armed[0] = next(crash_points)
            except StopIteration:
                armed[0] = "none"
            raise SimulatedWorkerCrash(point)

    store, results, service, provider, worker, _ = runtime(tmp_path, crash=crash, clock=clock)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    original_compute = worker.jobs.compute

    def counted(*args, **kwargs):
        compute_calls[0] += 1
        return original_compute(*args, **kwargs)

    worker.jobs.compute = counted
    for _ in range(3):
        with pytest.raises(SimulatedWorkerCrash):
            worker.run_once()
        current[0] += timedelta(seconds=121)
    terminal = worker.run_once()

    assert terminal is not None and terminal["status"] == "SUCCEEDED"
    assert provider.calls == 1
    assert compute_calls[0] == 1
    assert results.verify(terminal["result_id"])["production_run_id"] == terminal["production_run_id"]


def test_result_verifier_rejects_tamper_links_writable_and_missing(tmp_path) -> None:
    _, results, service, _, worker, _ = runtime(tmp_path)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    terminal = worker.run_once()
    assert terminal is not None
    result_id = terminal["result_id"]
    root = results.results_root / result_id

    action = root / "action.json"
    root.chmod(0o755)
    action.chmod(0o644)
    with pytest.raises(ProductionResultError, match="unsafe"):
        results.verify(result_id)
    action.chmod(0o444)
    root.chmod(0o555)

    with pytest.raises(ProductionResultError):
        results.verify("../" + result_id)


def test_prior_report_is_retained_when_publication_readback_fails(tmp_path, monkeypatch) -> None:
    _, results, service, _, worker, _ = runtime(tmp_path)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    terminal = worker.run_once()
    assert terminal is not None
    report = results.publication_root / "current" / "8991e9a8-1caa-41f5-b76b-6368259db5b4.html"
    previous = report.read_bytes()
    original = results._publish_report

    def fail(computation):
        original(computation)
        raise ProductionResultError("injected failure after verified promotion")

    monkeypatch.setattr(results, "_publish_report", fail)
    assert report.read_bytes() == previous
