from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from .production_contract import ProductionRelease, ProductionRequest
from .production_store import IdempotencyConflict, ProductionStore, TERMINAL_STATES


class ProductionAdmissionError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class AdmissionPolicy:
    allowed_manifests: Mapping[str, str]
    release: ProductionRelease
    ready: Callable[[], bool] = lambda: True
    capacity: int = 1
    before_window: timedelta = timedelta(minutes=15)
    after_window: timedelta = timedelta(hours=24)
    validation_window: timedelta = timedelta(minutes=15)

    def validate_new(
        self,
        request: ProductionRequest,
        store: ProductionStore,
        connection: sqlite3.Connection,
        now: datetime,
    ) -> None:
        expected = self.allowed_manifests.get(request.job_id)
        if expected != request.production_manifest_sha256:
            raise ProductionAdmissionError(422, "AUTHORITY_REJECTED", "job/model authority is not allowed")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ProductionAdmissionError(503, "CLOCK_INVALID", "admission clock is not timezone-aware")
        clock = now.astimezone(UTC)
        invocation = (
            None
            if request.is_operation
            else datetime.fromisoformat(request.effective_for[:-1] + "+00:00")
        )
        if request.is_validation and invocation is not None and abs(clock - invocation) > self.validation_window:
            raise ProductionAdmissionError(
                422, "VALIDATION_WINDOW_REJECTED", "validation invocation is not immediate"
            )
        if (
            invocation is not None
            and not request.is_validation
            and not invocation - self.before_window <= clock <= invocation + self.after_window
        ):
            raise ProductionAdmissionError(422, "FIRE_WINDOW_REJECTED", "scheduled fire is outside admission window")
        if not self.ready():
            raise ProductionAdmissionError(503, "RELEASE_NOT_READY", "production release is not ready")
        if store.active_count(connection) >= self.capacity:
            raise ProductionAdmissionError(429, "CAPACITY_UNAVAILABLE", "production capacity is unavailable")


class ProductionService:
    """Create-or-read orchestration around one immutable release and ledger."""

    def __init__(
        self,
        store: ProductionStore,
        policy: AdmissionPolicy,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.store = store
        self.policy = policy
        self.clock = clock

    @staticmethod
    def response(row: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": row["request_id"],
            "production_run_id": row["production_run_id"],
            "production_release_id": row["production_release_id"],
            "status": row["status"],
            "poll_uri": f"/api/v1/production/runs/{row['production_run_id']}",
        }
        request_body = row.get("request_body")
        if isinstance(request_body, Mapping) and "validation_id" in request_body:
            payload.update(
                {
                    "validation_id": request_body["validation_id"],
                    "validation_for": request_body["validation_for"],
                }
            )
        if isinstance(request_body, Mapping) and "operation" in request_body:
            payload["operation"] = request_body["operation"]
        if row["status"] == "SUCCEEDED":
            payload.update(
                {
                    "experiment_id": row["experiment_id"],
                    "attempt_id": row["attempt_id"],
                    "result_id": row["result_id"],
                    "result_uri": f"/api/v1/production/results/{row['result_id']}",
                }
            )
        elif row["status"] == "FAILED":
            payload["failure_reason"] = row["failure_reason"]
        return payload

    def create_or_read(self, body: bytes, idempotency_key: str) -> tuple[int, dict[str, Any]]:
        request = ProductionRequest.from_bytes(body, validate_identity=False)
        if idempotency_key != request.request_id:
            raise ProductionAdmissionError(
                422, "REQUEST_ID_MISMATCH", "Idempotency-Key must equal request_id"
            )
        if request.request_id != request.expected_request_id:
            if self.store.get_request(request.request_id) is not None:
                raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
            raise ProductionAdmissionError(
                422,
                "REQUEST_ID_MISMATCH",
                "request_id does not match the canonical request subject",
            )
        now = self.clock()
        row, created = self.store.admit(
            request,
            self.policy.release.production_release_id,
            validate_new=lambda connection: self.policy.validate_new(
                request, self.store, connection, now
            ),
            now=now,
        )
        return (202 if created or row["status"] not in TERMINAL_STATES else 200), self.response(row)

    def run_status(self, production_run_id: str) -> tuple[int, dict[str, Any]]:
        row = self.store.get_run(production_run_id)
        if row is None:
            raise ProductionAdmissionError(404, "NOT_FOUND", "production run is unknown")
        return 200, self.response(row)
