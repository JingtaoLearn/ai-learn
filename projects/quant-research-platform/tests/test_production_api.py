from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gold_research.focus_contract import Phase, canonical_json_bytes as focus_json, contract_document
from gold_research.focus_runner import claim_phase
from quant_platform.attempt_report import canonical_report_operator_bundle
from quant_platform.daily_loop_status import CronReceiptStore
from quant_platform.production_bocom import BocomProductionJob
from quant_platform.production_client import ProductionClient
from quant_platform.production_contract import (
    FOCUS_CALIBRATION_JOB_ID,
    FOCUS_CALIBRATION_OPERATION,
    ProductionRelease,
    ProductionRequest,
)
from quant_platform.production_focus import FocusCalibrationProductionJob
from quant_platform.production_gold import GoldProductionJob
from quant_platform.production_jobs import REPORT_EVIDENCE_FILE_NAMES, ProductionJobs
from quant_platform.production_package_authority import FilesystemPackageIdentityAuthority
from quant_platform.production_result import ProductionResultError, ProductionResultStore
from quant_platform.production_service import AdmissionPolicy, ProductionService
from quant_platform.production_store import ProductionStore
from quant_platform.production_store import StateConflict
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


def runtime(tmp_path, *, crash=lambda _point: None, clock=None, studies=None):
    now = datetime(2026, 3, 9, 0, 40, tzinfo=UTC)
    clock = clock or (lambda: now)
    store = ProductionStore(tmp_path / "state")
    store.initialize()
    results = ProductionResultStore(tmp_path / "result")
    bocom = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")
    gold = GoldProductionJob(FIXTURES / "gold-model-manifest.json")
    policy = AdmissionPolicy(
        {
            bocom.job_id: bocom.production_manifest_sha256,
            gold.job_id: gold.production_manifest_sha256,
        },
        release(),
    )
    service = ProductionService(store, policy, clock=clock)
    jobs = ProductionJobs([bocom, gold])
    provider = FixtureProvider()
    work_root = tmp_path / "work"
    worker = ProductionWorker(
        store,
        jobs,
        provider,
        results,
        FilesystemPackageIdentityAuthority(work_root, tmp_path / "package-identities"),
        work_root=work_root,
        owner="worker-1",
        clock=clock,
        crash=crash,
    )
    app = create_production_app(
        service,
        results,
        verified_client_identity=IDENTITY,
        studies=studies,
        cron_receipts=CronReceiptStore(tmp_path / "cron-receipts.json"),
    )
    return store, results, service, provider, worker, TestClient(app)


def test_mtls_client_rediscovers_lightweight_studies_without_detail_reads(tmp_path) -> None:
    summary = {
        "study_id": "a" * 64,
        "kind": "SYNTHETIC_TRAINING",
        "status": "RUNNING",
        "progress": {"completed_trials": 8, "total_trials": 32},
        "updated_at": "2026-09-10T06:00:01Z",
        "failure": None,
        "report_state": "NOT_APPLICABLE",
    }

    class Studies:
        def __init__(self):
            self.calls = []

        def list_summaries(self, cursor=None):
            self.calls.append(("list_summaries", cursor))
            return {"studies": [summary], "next_cursor": "older-page"}

        def detail(self, _study_id):
            raise AssertionError("list discovery must not read Feng-backed detail")

    studies = Studies()
    *_, client = runtime(tmp_path, studies=studies)

    denied = client.get("/api/v1/studies")
    listed = client.get(
        "/api/v1/studies?cursor=current-page",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )

    assert denied.status_code == 403
    assert listed.status_code == 200
    assert listed.json() == {"ok": True, "studies": [summary], "next_cursor": "older-page"}
    assert studies.calls == [("list_summaries", "current-page")]


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


def test_mtls_client_can_publish_bounded_cron_receipt(tmp_path) -> None:
    *_, client = runtime(tmp_path)
    payload = {
        "schema": "quantresearch-cron-receipts/v2",
        "observed_at": "2026-09-11T01:06:00+00:00",
        "jobs": [
            {
                "job_id": job_id,
                "enabled": True,
                "schedule": {
                    "kind": "cron",
                    "expr": "40 8 * * 1-5" if job_id == "1cd5557264db" else "45 8 * * 1-5",
                },
                "last_run_at": "2026-09-11T08:45:45+08:00",
                "last_status": "ok",
                "last_delivery_error": False,
                "delivery_target": {
                    "channel": "feishu",
                    "destination_sha256": "b" * 64,
                },
                "report_readback": {
                    "status": "OK",
                    "sha256": "a" * 64,
                    "size": 123,
                    "action_sha256": "c" * 64,
                },
                "execution": {
                    "id": f"execution-{job_id}",
                    "status": "completed",
                    "claimed_at": "2026-09-11T08:45:15+08:00",
                    "started_at": "2026-09-11T08:45:16+08:00",
                    "finished_at": "2026-09-11T08:45:45+08:00",
                },
            }
            for job_id in ("1cd5557264db", "297c11cad0dc")
        ],
    }

    denied = client.post(
        "/api/v1/production/daily-loop/cron-receipts", json=payload
    )
    accepted = client.post(
        "/api/v1/production/daily-loop/cron-receipts",
        json=payload,
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )

    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["ok"] is True
    assert len(accepted.json()["receipt_sha256"]) == 64


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
    operator = canonical_report_operator_bundle()
    assert result.json()["report_operator"] == {
        "api_version": operator["manifest"]["api_version"],
        "content_digest": operator["content_digest"],
        "operator_id": operator["manifest"]["operator_id"],
        "source_sha256": operator["source_sha256"],
        "version": operator["manifest"]["semantic_version"],
    }
    assert REPORT_EVIDENCE_FILE_NAMES <= result.json()["files"].keys()
    report_document = client.get(
        f"/api/v1/production/results/{terminal['result_id']}/files/report-document.json",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert report_document.status_code == 200
    assert hashlib.sha256(report_document.content).hexdigest() == result.json()[
        "report_document_sha256"
    ]
    normalized = client.get(
        f"/api/v1/production/results/{terminal['result_id']}/files/normalized-snapshot.json",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert normalized.status_code == 200
    assert hashlib.sha256(normalized.content).hexdigest() == result.json()["files"][
        "normalized-snapshot.json"
    ]["sha256"]
    notification = client.get(
        f"/api/v1/production/results/{terminal['result_id']}/files/notification.txt",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert notification.status_code == 200
    text = notification.content.decode("utf-8")
    assert len(notification.content) > 300
    assert all(
        marker in text
        for marker in (
            "交通银行生产信号｜市场日期：2026-01-22",
            "完成收盘",
            "日涨跌",
            "仓位：当前",
            "动作：WAIT",
            "斜率：上一",
            "下一完整收盘买入边界",
            "执行时点",
            "automatic_ordering=false",
        )
    )
    denied = client.get(
        f"/api/v1/production/results/{terminal['result_id']}/files/action.json",
        headers={VERIFIED_CLIENT_HEADER: IDENTITY},
    )
    assert denied.status_code == 404
    assert store.get_run(terminal["production_run_id"])["result_id"] == terminal["result_id"]


def test_public_stable_report_is_exact_html_from_bound_immutable_result(tmp_path) -> None:
    _, results, service, _, worker, client = runtime(tmp_path)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    terminal = worker.run_once()
    assert terminal is not None
    manifest = results.verify(terminal["result_id"])

    response = client.get(
        "/api/v1/production/stable-reports/8991e9a8-1caa-41f5-b76b-6368259db5b4.html"
    )

    assert response.status_code == 200
    assert response.content == results.read_client_file(terminal["result_id"], "report.html")
    assert response.headers["etag"] == f'"sha256:{manifest["report_sha256"]}"'
    assert response.headers["x-quantresearch-result-id"] == terminal["result_id"]
    assert response.headers["cache-control"] == "no-store"


def test_public_stable_report_returns_not_found_for_unknown_uuid(tmp_path) -> None:
    *_, client = runtime(tmp_path)

    response = client.get(
        "/api/v1/production/stable-reports/11111111-1111-4111-8111-111111111111.html"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "STABLE_REPORT_NOT_FOUND"


def test_competing_complete_results_leave_one_coherent_stable_pointer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = ProductionResultStore(tmp_path / "results")
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")
    provider = FixtureProvider()
    scheduled = [
        datetime(2026, 3, 9, 0, 40, tzinfo=UTC),
        datetime(2026, 3, 10, 0, 40, tzinfo=UTC),
    ]
    computations = []
    for timestamp in scheduled:
        provider_url, raw = job.acquire(provider, timestamp)
        computations.append(job.compute(raw, provider_url, timestamp))
    rows = [
        {
            "request_id": str(index),
            "production_run_id": f"run-{index}",
            "production_release_id": "release",
        }
        for index in range(2)
    ]
    original_read = results.stable_report
    both_advanced = threading.Barrier(2)

    def delayed_read(report_uuid: str):
        both_advanced.wait(timeout=5)
        return original_read(report_uuid)

    monkeypatch.setattr(results, "stable_report", delayed_read)
    manifests: list[dict] = []
    errors: list[BaseException] = []

    def publish(index: int) -> None:
        try:
            manifest = results.publish(rows[index], computations[index])
            results.complete_stable_report(manifest)
            manifests.append(manifest)
        except BaseException as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(manifests) == 2
    stable = original_read(job.report_uuid)
    selected = next(item for item in manifests if item["result_id"] == stable["result_id"])
    assert stable["report_sha256"] == selected["report_sha256"]
    assert stable["html"] == results.read_client_file(selected["result_id"], "report.html")


def test_failure_before_pointer_advance_retains_prior_immutable_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = ProductionResultStore(tmp_path / "results")
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")
    provider = FixtureProvider()
    computations = []
    for day in (9, 10):
        timestamp = datetime(2026, 3, day, 0, 40, tzinfo=UTC)
        provider_url, raw = job.acquire(provider, timestamp)
        computations.append(job.compute(raw, provider_url, timestamp))
    rows = [
        {
            "request_id": str(index),
            "production_run_id": f"run-{index}",
            "production_release_id": "release",
        }
        for index in range(2)
    ]
    first = results.publish(rows[0], computations[0])
    results.complete_stable_report(first)
    prior = results.stable_report(job.report_uuid)
    original_advance = results._advance_stable_report

    def fail_before_advance(manifest):
        if manifest["result_id"] != first["result_id"]:
            raise ProductionResultError("injected failure before pointer advance")
        original_advance(manifest)

    monkeypatch.setattr(results, "_advance_stable_report", fail_before_advance)
    second = results.publish(rows[1], computations[1])
    with pytest.raises(ProductionResultError, match="before pointer"):
        results.complete_stable_report(second)

    assert results.stable_report(job.report_uuid) == prior
    assert len(list(results.results_root.iterdir())) == 2


def test_failed_terminal_commit_never_advances_stable_pointer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, results, service, _, worker, client = runtime(tmp_path)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    monkeypatch.setattr(
        worker.store,
        "finish_success",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StateConflict("lost lease")),
    )

    with pytest.raises(StateConflict, match="lost lease"):
        worker.run_once()

    response = client.get(
        "/api/v1/production/stable-reports/8991e9a8-1caa-41f5-b76b-6368259db5b4.html"
    )
    assert response.status_code == 404


def test_crash_after_terminal_is_reconciled_from_durable_success_manifest(tmp_path) -> None:
    armed = [True]

    def crash(point: str) -> None:
        if point == "after_terminal_before_stable_pointer" and armed[0]:
            armed[0] = False
            raise SimulatedWorkerCrash(point)

    _, _, service, _, worker, client = runtime(tmp_path, crash=crash)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    with pytest.raises(SimulatedWorkerCrash, match="after_terminal"):
        worker.run_once()

    url = "/api/v1/production/stable-reports/8991e9a8-1caa-41f5-b76b-6368259db5b4.html"
    assert client.get(url).status_code == 404
    assert worker.run_once() is None
    assert client.get(url).status_code == 200


def test_idle_worker_reconciles_historical_reports_only_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, results, _, _, worker, _ = runtime(tmp_path)
    manifest = {
        "schema": "quantresearch-production-result/v1",
        "result_id": "historical-result",
        "report_operator": {"operator_id": "canonical_attempt_report"},
    }
    monkeypatch.setattr(store, "successful_result_manifests", lambda: [manifest])
    reconciled: list[str] = []
    monkeypatch.setattr(
        results,
        "complete_stable_report",
        lambda value: reconciled.append(value["result_id"]),
    )

    assert worker.run_once() is None
    assert worker.run_once() is None

    assert reconciled == ["historical-result"]


def test_legacy_success_does_not_block_new_canonical_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, service, _, worker, _ = runtime(tmp_path)
    legacy = {
        "schema": "quantresearch-production-result/v1",
        "files": {
            "provider-response.bin": {"sha256": "0" * 64, "size": 1},
            "normalized-snapshot.json": {"sha256": "1" * 64, "size": 1},
            "action.json": {"sha256": "2" * 64, "size": 1},
            "report.html": {"sha256": "3" * 64, "size": 1},
            "notification.txt": {"sha256": "4" * 64, "size": 1},
        },
    }
    monkeypatch.setattr(store, "successful_result_manifests", lambda: [legacy])
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)

    terminal = worker.run_once()

    assert terminal is not None and terminal["status"] == "SUCCEEDED"


def test_tampered_crash_reconciliation_preserves_prior_stable_report(tmp_path) -> None:
    current = [datetime(2026, 3, 9, 0, 40, tzinfo=UTC)]
    armed = [False]

    def crash(point: str) -> None:
        if armed[0] and point == "after_terminal_before_stable_pointer":
            armed[0] = False
            raise SimulatedWorkerCrash(point)

    store, results, service, _, worker, client = runtime(
        tmp_path, crash=crash, clock=lambda: current[0]
    )
    first = request()
    service.create_or_read(first.canonical_body, first.request_id)
    assert worker.run_once() is not None
    report_url = (
        "/api/v1/production/stable-reports/"
        "8991e9a8-1caa-41f5-b76b-6368259db5b4.html"
    )
    prior_response = client.get(report_url)
    assert prior_response.status_code == 200
    with sqlite3.connect(results.stable_database_path) as connection:
        connection.row_factory = sqlite3.Row
        prior_pointer = dict(connection.execute("SELECT * FROM stable_production_reports").fetchone())

    current[0] = datetime(2026, 3, 10, 0, 40, tzinfo=UTC)
    second = ProductionRequest.build(
        job_id="297c11cad0dc",
        scheduled_for="2026-03-10T00:40:00Z",
        production_manifest_sha256=MANIFEST,
    )
    second_row = service.create_or_read(second.canonical_body, second.request_id)
    armed[0] = True
    with pytest.raises(SimulatedWorkerCrash, match="after_terminal"):
        worker.run_once()
    newer = store.get_run(second_row[1]["production_run_id"])
    assert newer is not None
    report = results.results_root / newer["result_id"] / "report.html"
    results.results_root.joinpath(newer["result_id"]).chmod(0o755)
    report.chmod(0o644)
    report.write_bytes(report.read_bytes() + b"tampered")
    report.chmod(0o444)
    results.results_root.joinpath(newer["result_id"]).chmod(0o555)

    with pytest.raises(ProductionResultError, match="member identity"):
        worker.run_once()

    with sqlite3.connect(results.stable_database_path) as connection:
        connection.row_factory = sqlite3.Row
        stored_pointer = dict(connection.execute("SELECT * FROM stable_production_reports").fetchone())
    stable_response = client.get(report_url)
    assert stored_pointer == prior_pointer
    assert stable_response.status_code == 200
    assert stable_response.content == prior_response.content


def test_delayed_older_completion_cannot_replace_newer_stable_report(tmp_path) -> None:
    results = ProductionResultStore(tmp_path / "results")
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")
    provider = FixtureProvider()
    computations = []
    for day in (9, 10):
        timestamp = datetime(2026, 3, day, 0, 40, tzinfo=UTC)
        provider_url, raw = job.acquire(provider, timestamp)
        computations.append(job.compute(raw, provider_url, timestamp))
    rows = [
        {
            "request_id": str(index),
            "production_run_id": f"run-{index}",
            "production_release_id": "release",
        }
        for index in range(2)
    ]
    newer = results.publish(rows[1], computations[1])
    results.complete_stable_report(newer)
    older = results.publish(rows[0], computations[0])
    results.complete_stable_report(older)

    assert results.stable_report(job.report_uuid)["result_id"] == newer["result_id"]


def test_stable_endpoint_fails_closed_on_pointer_identity_tamper(tmp_path) -> None:
    _, results, service, _, worker, client = runtime(tmp_path)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    assert worker.run_once() is not None
    with sqlite3.connect(results.stable_database_path) as connection:
        connection.execute(
            "UPDATE stable_production_reports SET report_sha256 = ?",
            ("0" * 64,),
        )

    response = client.get(
        "/api/v1/production/stable-reports/8991e9a8-1caa-41f5-b76b-6368259db5b4.html"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "STABLE_REPORT_NOT_FOUND"


def test_capacity_overlap_retries_both_jobs_and_replay_has_no_duplicate_result(tmp_path) -> None:
    store, _, _, provider, worker, api = runtime(tmp_path)
    bocom_request = request()
    gold_request = ProductionRequest.build(
        job_id=GoldProductionJob.job_id,
        scheduled_for=FIRE,
        production_manifest_sha256=GoldProductionJob.production_manifest_sha256,
    )
    first = api.post(
        "/api/v1/production/runs",
        content=bocom_request.canonical_body,
        headers=headers(bocom_request),
    )
    assert first.status_code == 202

    class ASGITransport:
        def __init__(self):
            self.post_statuses = []

        def request(self, method, path, *, headers, body):
            response = api.request(
                method,
                path,
                headers=dict(headers) | {VERIFIED_CLIENT_HEADER: IDENTITY},
                content=body,
            )
            if method == "POST":
                self.post_statuses.append(response.status_code)
            return response.status_code, dict(response.headers), response.content

    terminal = []

    def advance_worker(_delay):
        completed = worker.run_once()
        if completed is not None:
            terminal.append(completed)

    transport = ASGITransport()
    client = ProductionClient(transport, transport_attempts=3, sleep=advance_worker)
    gold_result = client.submit_and_wait(gold_request)
    bocom_result = client.submit_and_wait(bocom_request)
    gold_notification = client.fetch_verified_file(gold_result, "notification.txt").decode("utf-8")
    bocom_notification = client.fetch_verified_file(bocom_result, "notification.txt").decode("utf-8")

    assert transport.post_statuses == [429, 202, 200]
    assert [item["request_id"] for item in terminal] == [
        bocom_request.request_id,
        gold_request.request_id,
    ]
    assert provider.calls == 2
    assert bocom_result["result_id"] == terminal[0]["result_id"]
    stored = store.get_request(bocom_request.request_id)
    assert stored is not None and stored["result_id"] == bocom_result["result_id"]
    assert "黄金生产信号" in gold_notification and len(gold_notification.encode()) > 300
    assert "交通银行生产信号" in bocom_notification and len(bocom_notification.encode()) > 300


def test_identity_boundary_rejects_browser_auth_and_unverified_calls(tmp_path) -> None:
    _, _, _, _, _, client = runtime(tmp_path)
    value = request()
    assert client.post("/api/v1/production/runs", content=value.canonical_body).status_code == 403
    browser = headers(value) | {"Cookie": "session=browser", "X-CSRF-Token": "browser"}
    assert client.post("/api/v1/production/runs", content=value.canonical_body, headers=browser).status_code == 403
    assert client.get("/health/live").status_code == 200


def test_focus_operation_traverses_authenticated_idempotent_client_path_without_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verified = "1" * 64
    reviewed = "2" * 64
    authority = {
        "schema": "quant-research/focus-calibration-authority/v1",
        "contract_sha256": hashlib.sha256(focus_json(contract_document()) + b"\n").hexdigest(),
        "implementation_verified_sha256": verified,
        "implementation_reviewed_sha256": reviewed,
    }
    authority_path = tmp_path / "focus-calibration-authority.json"
    authority_path.write_bytes(focus_json(authority) + b"\n")
    authority_path.chmod(0o444)
    focus_root = tmp_path / "focus"
    claim_phase(focus_root / "phase-ledger", Phase.IMPLEMENTATION_VERIFIED, verified)
    claim_phase(focus_root / "phase-ledger", Phase.IMPLEMENTATION_REVIEWED, reviewed)

    def synthetic():
        return {
            "schema": "focus-synthetic-calibration/v1",
            "status": "PASS",
            "totals": {
                "generated_paths": 140_000,
                "provider_requests": 0,
                "sge_requests": 0,
                "evaluation_executions": 0,
                "flearn_fallbacks": 0,
                "market_outcome_bytes": 0,
            },
        }

    monkeypatch.setattr("gold_research.focus_runner.execute_synthetic_calibration", synthetic)
    store = ProductionStore(tmp_path / "state")
    store.initialize()
    results = ProductionResultStore(tmp_path / "results")
    job = FocusCalibrationProductionJob(authority_path, focus_root)
    service = ProductionService(
        store, AdmissionPolicy({job.job_id: job.production_manifest_sha256}, release())
    )
    provider = FixtureProvider()
    provider.calls = 0
    work_root = tmp_path / "work"
    worker = ProductionWorker(
        store,
        ProductionJobs([job]),
        provider,
        results,
        FilesystemPackageIdentityAuthority(work_root, tmp_path / "package-identities"),
        work_root=work_root,
        owner="focus-worker",
    )
    api = TestClient(create_production_app(service, results, verified_client_identity=IDENTITY))

    class MTLSTestTransport:
        def request(self, method, path, *, headers, body):
            response = api.request(
                method,
                path,
                headers=dict(headers) | {VERIFIED_CLIENT_HEADER: IDENTITY},
                content=body,
            )
            return response.status_code, dict(response.headers), response.content

    request_value = ProductionRequest.build_operation(
        job_id=FOCUS_CALIBRATION_JOB_ID,
        operation=FOCUS_CALIBRATION_OPERATION,
        production_manifest_sha256=job.production_manifest_sha256,
    )

    def run_worker(_seconds):
        worker.run_once()

    production_client = ProductionClient(MTLSTestTransport(), sleep=run_worker)
    first = production_client.submit_and_wait(request_value)
    document = production_client.verify_focus_calibration(first)
    second = production_client.submit_and_wait(request_value)

    assert first == second
    assert document["totals"]["generated_paths"] == 140_000
    assert first["input"]["network_access"] is False
    assert provider.calls == 0
    assert api.post(
        "/api/v1/production/runs",
        content=request_value.canonical_body,
        headers={"Idempotency-Key": request_value.request_id, "Content-Type": "application/json"},
    ).status_code == 403


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
