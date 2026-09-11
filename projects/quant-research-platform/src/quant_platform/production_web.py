from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .dataset_service import DatasetResolutionError, MsftAdjustedOhlcProxyIngress
from .daily_loop_status import CronReceiptStore, DailyLoopStatusError
from .full_persistence import FullPostgresPersistence, PersistenceConflict
from .lightweight_study import LightweightStudyService
from .production_bocom import BocomProductionJob
from .production_contract import ProductionRelease
from .production_focus import FocusCalibrationProductionJob
from .production_gold import GoldProductionJob
from .production_jobs import ProductionJobs
from .production_package_authority import HttpPackageIdentityAuthorityClient
from .production_result import ProductionResultError, ProductionResultStore
from .production_service import AdmissionPolicy, ProductionAdmissionError, ProductionService
from .production_store import IdempotencyConflict, ProductionStore, ScheduledFireConflict
from .production_worker import ProductionWorker
from .study_remote import StudyRemoteError


MAX_BODY_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_048_576
VERIFIED_CLIENT_HEADER = "x-quantresearch-verified-client"


def _response(status: int, value: Mapping[str, Any]) -> Response:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(payload) > MAX_RESPONSE_BYTES:
        return JSONResponse(
            {"ok": False, "error": {"code": "RESPONSE_TOO_LARGE", "message": "response exceeds limit"}},
            status_code=500,
        )
    return Response(payload, status_code=status, media_type="application/json")


def _error(status: int, code: str, message: str) -> Response:
    return _response(status, {"ok": False, "error": {"code": code, "message": message}})


def create_production_app(
    service: ProductionService,
    results: ProductionResultStore,
    *,
    verified_client_identity: str,
    msft_ingress: MsftAdjustedOhlcProxyIngress | None = None,
    studies: LightweightStudyService | None = None,
    cron_receipts: CronReceiptStore | None = None,
) -> FastAPI:
    if not verified_client_identity or "," in verified_client_identity:
        raise ValueError("verified client identity is invalid")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def verified_client_boundary(request: Request, call_next):
        if request.url.path in {"/health", "/health/live", "/health/ready"}:
            return await call_next(request)
        supplied = request.headers.get(VERIFIED_CLIENT_HEADER)
        if (
            supplied != verified_client_identity
            or "cookie" in request.headers
            or "authorization" in request.headers
            or "x-csrf-token" in request.headers
        ):
            return _error(403, "CLIENT_IDENTITY_REJECTED", "verified mTLS client identity is required")
        return await call_next(request)

    @app.exception_handler(ProductionAdmissionError)
    async def admission_error(_request: Request, exc: ProductionAdmissionError):
        return _error(exc.status_code, exc.code, str(exc))

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_error(_request: Request, exc: IdempotencyConflict):
        return _error(409, "IDEMPOTENCY_CONFLICT", str(exc))

    @app.exception_handler(ScheduledFireConflict)
    async def scheduled_fire_error(_request: Request, exc: ScheduledFireConflict):
        return _error(409, "SCHEDULED_FIRE_CONFLICT", str(exc))

    @app.get("/health/live")
    async def live():
        return _response(200, {"status": "live"})

    @app.get("/health/ready")
    async def ready():
        return _response(200, {"status": "ready" if service.policy.ready() else "not-ready"})

    @app.get("/health")
    async def health():
        return _response(200, {"status": "ok", "persistence": "postgresql", "schema": "operator-v1"})

    @app.post("/api/v1/production/runs")
    async def create_run(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return _error(422, "CONTENT_TYPE_REJECTED", "Content-Type must be application/json")
        body = await request.body()
        if not body or len(body) > MAX_BODY_BYTES:
            return _error(422, "BODY_SIZE_REJECTED", "request body size is invalid")
        key = request.headers.get("idempotency-key", "")
        status, value = await run_in_threadpool(service.create_or_read, body, key)
        return _response(status, value)

    @app.post("/api/v1/production/daily-loop/cron-receipts")
    async def record_cron_receipts(request: Request):
        if cron_receipts is None:
            return _error(503, "RECEIPT_STORE_UNAVAILABLE", "Cron receipt storage is unavailable")
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return _error(422, "CONTENT_TYPE_REJECTED", "Content-Type must be application/json")
        body = await request.body()
        if not body or len(body) > MAX_BODY_BYTES:
            return _error(422, "BODY_SIZE_REJECTED", "request body size is invalid")
        try:
            receipt = await run_in_threadpool(cron_receipts.replace, body)
        except (DailyLoopStatusError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            return _error(422, "INVALID_CRON_RECEIPT", str(exc))
        return _response(200, {"ok": True, **receipt})

    @app.get("/api/v1/production/runs/{production_run_id}")
    async def run_status(production_run_id: str):
        status, value = await run_in_threadpool(service.run_status, production_run_id)
        return _response(status, value)

    @app.get("/api/v1/production/results/{result_id}")
    async def result(result_id: str):
        try:
            value = await run_in_threadpool(results.verify, result_id)
        except ProductionResultError as exc:
            return _error(404, "RESULT_NOT_FOUND", str(exc))
        return _response(200, value)

    @app.get("/api/v1/production/results/{result_id}/files/{name}")
    async def result_file(result_id: str, name: str):
        try:
            value = await run_in_threadpool(results.read_client_file, result_id, name)
        except ProductionResultError as exc:
            return _error(404, "RESULT_FILE_NOT_FOUND", str(exc))
        return Response(value, status_code=200, media_type="application/octet-stream")

    @app.post("/api/v1/datasets/msft/snapshots")
    async def create_msft_snapshot(request: Request):
        if msft_ingress is None:
            return _error(503, "MSFT_INGRESS_UNAVAILABLE", "MSFT Dataset ingress is unavailable")
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return _error(422, "CONTENT_TYPE_REJECTED", "Content-Type must be application/json")
        body = await request.body()
        if not body or len(body) > MAX_BODY_BYTES:
            return _error(422, "BODY_SIZE_REJECTED", "request body size is invalid")
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise DatasetResolutionError("MSFT ingress body must be an object")
            receipt = await run_in_threadpool(
                msft_ingress.ingest,
                value,
                request.headers.get("idempotency-key", ""),
            )
        except (json.JSONDecodeError, DatasetResolutionError, ValueError) as exc:
            return _error(422, "MSFT_INGRESS_REJECTED", str(exc))
        except PersistenceConflict as exc:
            return _error(409, "MSFT_INGRESS_CONFLICT", str(exc))
        return _response(201, {"ok": True, "snapshot": receipt})

    @app.get("/api/v1/datasets/msft/snapshots/{snapshot_id}")
    async def msft_snapshot(snapshot_id: str):
        if studies is None or studies.platform is None:
            return _error(503, "MSFT_SNAPSHOT_UNAVAILABLE", "MSFT Snapshot authority is unavailable")
        try:
            value = await run_in_threadpool(studies.platform.msft_snapshot, snapshot_id)
        except ValueError as exc:
            return _error(404, "MSFT_SNAPSHOT_NOT_FOUND", str(exc))
        return _response(200, {"ok": True, "snapshot": value})

    @app.post("/api/v1/studies/msft")
    async def create_msft_study(request: Request):
        if studies is None:
            return _error(503, "MSFT_STUDY_UNAVAILABLE", "MSFT Study service is unavailable")
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return _error(422, "CONTENT_TYPE_REJECTED", "Content-Type must be application/json")
        body = await request.body()
        if not body or len(body) > MAX_BODY_BYTES:
            return _error(422, "BODY_SIZE_REJECTED", "request body size is invalid")
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or set(value) != {"action_id", "snapshot_id"}:
                raise StudyRemoteError("MSFT Study body fields are invalid")
            study = await run_in_threadpool(
                studies.submit_msft,
                action_id=value["action_id"],
                snapshot_id=value["snapshot_id"],
            )
        except (json.JSONDecodeError, StudyRemoteError, ValueError) as exc:
            return _error(422, "MSFT_STUDY_REJECTED", str(exc))
        return _response(201, {"ok": True, "study": study})

    @app.get("/api/v1/studies/{study_id}")
    async def msft_study(study_id: str):
        if studies is None:
            return _error(503, "MSFT_STUDY_UNAVAILABLE", "MSFT Study service is unavailable")
        try:
            value = await run_in_threadpool(studies.detail, study_id)
        except (StudyRemoteError, ValueError) as exc:
            return _error(404, "MSFT_STUDY_NOT_FOUND", str(exc))
        return _response(200, {"ok": True, "study": value})

    @app.get("/api/v1/studies/{study_id}/report")
    async def msft_study_report(study_id: str):
        if studies is None:
            return _error(503, "MSFT_STUDY_UNAVAILABLE", "MSFT Study service is unavailable")
        try:
            value = await run_in_threadpool(studies.report, study_id)
        except (StudyRemoteError, ValueError) as exc:
            return _error(404, "MSFT_STUDY_REPORT_NOT_FOUND", str(exc))
        return Response(
            value["html"],
            status_code=200,
            media_type="text/html",
            headers={
                "X-QuantResearch-Report-Artifact": value["report_artifact_id"],
                "X-QuantResearch-Report-Sequence": str(value["sequence"]),
                "X-QuantResearch-Classification": value["document"].get("classification", ""),
            },
        )

    @app.post("/api/v1/studies/{study_id}/invalidation")
    async def invalidate_msft_study_report(request: Request, study_id: str):
        if studies is None:
            return _error(503, "MSFT_STUDY_UNAVAILABLE", "MSFT Study service is unavailable")
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return _error(422, "CONTENT_TYPE_REJECTED", "Content-Type must be application/json")
        body = await request.body()
        if not body or len(body) > MAX_BODY_BYTES:
            return _error(422, "BODY_SIZE_REJECTED", "request body size is invalid")
        try:
            value = json.loads(body)
            from .msft_trend_study import PROXY_INVALIDATION_CLASSIFICATION

            if not isinstance(value, dict) or set(value) != {
                "classification",
                "report_artifact_id",
            }:
                raise StudyRemoteError("MSFT Study invalidation body fields are invalid")
            if value["classification"] != PROXY_INVALIDATION_CLASSIFICATION:
                raise StudyRemoteError("MSFT Study invalidation classification is invalid")
            result = await run_in_threadpool(
                studies.invalidate_msft_report,
                study_id=study_id,
                report_artifact_id=value["report_artifact_id"],
            )
        except (json.JSONDecodeError, StudyRemoteError, ValueError) as exc:
            return _error(422, "MSFT_STUDY_INVALIDATION_REJECTED", str(exc))
        except PersistenceConflict as exc:
            return _error(409, "MSFT_STUDY_INVALIDATION_CONFLICT", str(exc))
        return _response(
            201,
            {
                "ok": True,
                "invalidation": {key: item for key, item in result.items() if key != "html"},
            },
        )

    return app


class _ProxyOnlyProvider:
    def __init__(self, proxy_url: str):
        if proxy_url != "http://provider-egress-proxy:3128":
            raise RuntimeError("provider proxy URL must name the isolated Compose proxy")
        self.proxy_url = proxy_url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy_url}))

    def get(self, url: str, *, headers: Mapping[str, str], maximum_bytes: int) -> bytes:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        with self.opener.open(request, timeout=25) as response:
            payload = response.read(maximum_bytes + 1)
            if len(payload) > maximum_bytes:
                raise RuntimeError("provider response exceeds size limit")
            if response.status != 200 or response.geturl() != url:
                raise RuntimeError("provider response status or URL is invalid")
            return payload

    def get_sanitized_msft_proxy(
        self,
        url: str,
        *,
        maximum_bytes: int,
        start: str,
        end: str,
    ) -> bytes:
        """Keep the provider response inside a short-lived sanitizer process."""

        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "quant_platform.yahoo_proxy_sanitizer",
                    "--url",
                    url,
                    "--start",
                    start,
                    "--end",
                    end,
                    "--maximum-bytes",
                    str(maximum_bytes),
                    "--proxy-url",
                    self.proxy_url,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=35,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Yahoo proxy sanitizer process failed") from exc
        if completed.returncode != 0 or not completed.stdout or len(completed.stdout) > maximum_bytes:
            raise RuntimeError("Yahoo proxy sanitizer rejected the provider response")
        return completed.stdout


def _required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required path configuration: {name}")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError(f"configuration path must be absolute: {name}")
    return path


def build_runtime_app() -> tuple[FastAPI, ProductionWorker]:
    state_root = _required_path("QR_PRODUCTION_STATE_ROOT")
    work_root = _required_path("QR_PRODUCTION_WORK_ROOT")
    release_path = _required_path("QR_PRODUCTION_RELEASE_MANIFEST")
    authorities = _required_path("QR_PRODUCTION_AUTHORITIES_ROOT")
    release = ProductionRelease.from_mapping(json.loads(release_path.read_bytes()))
    store = ProductionStore(state_root)
    store.initialize()
    results = ProductionResultStore(state_root)
    bocom = BocomProductionJob(authorities / "bocom-model-manifest.json")
    gold = GoldProductionJob(authorities / "gold-model-manifest.json")
    focus = FocusCalibrationProductionJob(
        authorities / "focus-calibration-authority.json", state_root / "focus-calibration"
    )
    policy = AdmissionPolicy(
        {
            bocom.job_id: bocom.production_manifest_sha256,
            gold.job_id: gold.production_manifest_sha256,
            focus.job_id: focus.production_manifest_sha256,
        },
        release,
        ready=lambda: True,
    )
    service = ProductionService(store, policy)
    jobs = ProductionJobs([bocom, gold, focus])
    package_identity_authority = HttpPackageIdentityAuthorityClient(
        os.environ.get("QR_PACKAGE_IDENTITY_AUTHORITY_URL", "")
    )
    provider = _ProxyOnlyProvider(os.environ.get("HTTPS_PROXY", ""))
    worker = ProductionWorker(
        store,
        jobs,
        provider,
        results,
        package_identity_authority,
        work_root=work_root,
        owner=f"production-worker:{os.getpid()}",
    )
    identity = os.environ.get("QR_VERIFIED_CLIENT_IDENTITY", "")
    persistence = FullPostgresPersistence.from_environment()
    ingress = MsftAdjustedOhlcProxyIngress(provider, persistence)
    studies = LightweightStudyService.from_environment()
    receipt_path = Path(
        os.environ.get(
            "QR_DAILY_LOOP_RECEIPTS_PATH",
            str(state_root / "daily-loop-cron-receipts.json"),
        )
    )
    return create_production_app(
        service,
        results,
        verified_client_identity=identity,
        msft_ingress=ingress,
        studies=studies,
        cron_receipts=CronReceiptStore(receipt_path),
    ), worker


def main() -> None:
    import time

    import uvicorn

    app, worker = build_runtime_app()

    def loop() -> None:
        while True:
            try:
                if worker.run_once() is None:
                    time.sleep(1)
            except Exception:
                time.sleep(1)

    threading.Thread(target=loop, name="production-worker", daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8090, workers=1)


if __name__ == "__main__":
    main()
