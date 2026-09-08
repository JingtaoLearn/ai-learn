from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .production_bocom import BocomProductionJob
from .production_contract import ProductionRelease
from .production_focus import FocusCalibrationProductionJob
from .production_gold import GoldProductionJob
from .production_jobs import ProductionJobs
from .production_result import ProductionResultError, ProductionResultStore
from .production_service import AdmissionPolicy, ProductionAdmissionError, ProductionService
from .production_store import IdempotencyConflict, ProductionStore, ScheduledFireConflict
from .production_worker import ProductionWorker


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
) -> FastAPI:
    if not verified_client_identity or "," in verified_client_identity:
        raise ValueError("verified client identity is invalid")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def verified_client_boundary(request: Request, call_next):
        if request.url.path in {"/health/live", "/health/ready"}:
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

    return app


class _ProxyOnlyProvider:
    def __init__(self, proxy_url: str):
        if proxy_url != "http://provider-egress-proxy:3128":
            raise RuntimeError("provider proxy URL must name the isolated Compose proxy")
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
    worker = ProductionWorker(
        store,
        jobs,
        _ProxyOnlyProvider(os.environ.get("HTTPS_PROXY", "")),
        results,
        work_root=state_root / "work",
        owner=f"production-worker:{os.getpid()}",
    )
    identity = os.environ.get("QR_VERIFIED_CLIENT_IDENTITY", "")
    return create_production_app(service, results, verified_client_identity=identity), worker


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
