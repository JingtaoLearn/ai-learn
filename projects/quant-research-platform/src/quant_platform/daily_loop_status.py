from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .production_contract import SHA256, canonical_json_bytes
from .production_schedule_client import JOBS, REPORT_BASE_URL
from .production_store import ProductionStore


TIMEZONE = ZoneInfo("Asia/Shanghai")
RECEIPT_SCHEMA = "quantresearch-cron-receipts/v2"
MAX_RECEIPT_BYTES = 16_384
EXPECTED_DELIVERY_TARGET = {
    "channel": "feishu",
    "destination_sha256": "1114febdf88bf78ba4c44a38ccaec1abb8447dce715d3a5e36400c00ccab8dae",
}
DEADLINES = {
    "1cd5557264db": time(9, 0),
    "297c11cad0dc": time(9, 5),
}


class DailyLoopStatusError(ValueError):
    """Raised when the Cron receipt projection is invalid or unavailable."""


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DailyLoopStatusError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DailyLoopStatusError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DailyLoopStatusError(f"{field} must include a timezone")
    return parsed


def _optional_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, field).isoformat()


def _validate_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "observed_at", "jobs"}:
        raise DailyLoopStatusError("Cron receipt fields are invalid")
    if value["schema"] != RECEIPT_SCHEMA:
        raise DailyLoopStatusError("Cron receipt schema is unsupported")
    observed_at = _timestamp(value["observed_at"], "observed_at").isoformat()
    jobs = value["jobs"]
    if not isinstance(jobs, list) or len(jobs) != len(JOBS):
        raise DailyLoopStatusError("Cron receipt must contain every scheduled job exactly once")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in jobs:
        required = {
            "job_id",
            "enabled",
            "schedule",
            "last_run_at",
            "last_status",
            "last_delivery_error",
            "delivery_target",
            "execution",
            "report_readback",
        }
        if not isinstance(item, dict) or set(item) != required:
            raise DailyLoopStatusError("Cron job receipt fields are invalid")
        job_id = item["job_id"]
        if job_id not in JOBS or job_id in seen:
            raise DailyLoopStatusError("Cron job receipt identity is invalid")
        seen.add(job_id)
        schedule = item["schedule"]
        if (
            not isinstance(schedule, dict)
            or set(schedule) != {"kind", "expr"}
            or schedule != {"kind": "cron", "expr": JOBS[job_id].schedule}
        ):
            raise DailyLoopStatusError("Cron job schedule differs from the observed contract")
        if type(item["enabled"]) is not bool:
            raise DailyLoopStatusError("Cron job enabled state must be boolean")
        last_status = item["last_status"]
        if last_status is not None and (
            not isinstance(last_status, str) or not last_status or len(last_status) > 64
        ):
            raise DailyLoopStatusError("Cron job status is invalid")
        if type(item["last_delivery_error"]) is not bool:
            raise DailyLoopStatusError("Cron delivery-error receipt must be boolean")
        delivery_target = item["delivery_target"]
        if (
            not isinstance(delivery_target, dict)
            or set(delivery_target) != {"channel", "destination_sha256"}
            or (
                delivery_target["channel"] is not None
                and (
                    not isinstance(delivery_target["channel"], str)
                    or not delivery_target["channel"]
                    or len(delivery_target["channel"]) > 32
                )
            )
            or (
                delivery_target["destination_sha256"] is not None
                and (
                    not isinstance(delivery_target["destination_sha256"], str)
                    or SHA256.fullmatch(delivery_target["destination_sha256"]) is None
                )
            )
        ):
            raise DailyLoopStatusError("Cron delivery target receipt is invalid")
        report_readback = item["report_readback"]
        if (
            not isinstance(report_readback, dict)
            or set(report_readback) != {"status", "sha256", "size", "action_sha256"}
            or report_readback["status"] not in {"OK", "FAILED"}
            or (
                report_readback["status"] == "OK"
                and (
                    not isinstance(report_readback["sha256"], str)
                    or SHA256.fullmatch(report_readback["sha256"]) is None
                    or type(report_readback["size"]) is not int
                    or report_readback["size"] < 1
                    or not isinstance(report_readback["action_sha256"], str)
                    or SHA256.fullmatch(report_readback["action_sha256"]) is None
                )
            )
            or (
                report_readback["status"] == "FAILED"
                and (
                    report_readback["sha256"] is not None
                    or report_readback["size"] is not None
                    or report_readback["action_sha256"] is not None
                )
            )
        ):
            raise DailyLoopStatusError("Cron report read-back receipt is invalid")
        execution = item["execution"]
        if execution is not None:
            if not isinstance(execution, dict) or set(execution) != {
                "id",
                "status",
                "claimed_at",
                "started_at",
                "finished_at",
            }:
                raise DailyLoopStatusError("Cron execution receipt fields are invalid")
            if (
                not isinstance(execution["id"], str)
                or not execution["id"]
                or len(execution["id"]) > 128
                or execution["status"]
                not in {"claimed", "running", "completed", "failed", "unknown"}
            ):
                raise DailyLoopStatusError("Cron execution receipt is invalid")
            execution = {
                "id": execution["id"],
                "status": execution["status"],
                "claimed_at": _optional_timestamp(
                    execution["claimed_at"], "execution.claimed_at"
                ),
                "started_at": _optional_timestamp(
                    execution["started_at"], "execution.started_at"
                ),
                "finished_at": _optional_timestamp(
                    execution["finished_at"], "execution.finished_at"
                ),
            }
        validated.append(
            {
                "job_id": job_id,
                "enabled": item["enabled"],
                "schedule": schedule,
                "last_run_at": _optional_timestamp(item["last_run_at"], "last_run_at"),
                "last_status": last_status,
                "last_delivery_error": item["last_delivery_error"],
                "delivery_target": delivery_target,
                "execution": execution,
                "report_readback": report_readback,
            }
        )
    return {"schema": RECEIPT_SCHEMA, "observed_at": observed_at, "jobs": validated}


class CronReceiptStore:
    """Atomic, bounded projection of Hermes-owned Cron delivery evidence."""

    def __init__(self, path: Path | str):
        self.path = Path(path).absolute()

    def replace(self, payload: bytes) -> dict[str, Any]:
        if not payload or len(payload) > MAX_RECEIPT_BYTES:
            raise DailyLoopStatusError("Cron receipt body size is invalid")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DailyLoopStatusError("Cron receipt is invalid JSON") from exc
        validated = _validate_receipt(value)
        canonical = canonical_json_bytes(validated)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        metadata = os.stat(self.path.parent, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DailyLoopStatusError("Cron receipt directory is unsafe")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o640)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        if self.path.read_bytes() != canonical:
            raise DailyLoopStatusError("Cron receipt read-back differs after publication")
        return {
            "observed_at": validated["observed_at"],
            "receipt_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def read(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return None, None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > MAX_RECEIPT_BYTES
        ):
            raise DailyLoopStatusError("Cron receipt file is unsafe")
        payload = self.path.read_bytes()
        if len(payload) != metadata.st_size:
            raise DailyLoopStatusError("Cron receipt changed while reading")
        try:
            value = _validate_receipt(json.loads(payload.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DailyLoopStatusError("Cron receipt file is invalid JSON") from exc
        return value, hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class DailyLoopStatusService:
    production: ProductionStore
    receipts: CronReceiptStore
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @staticmethod
    def _latest_expected(job_id: str, now: datetime) -> tuple[datetime, datetime]:
        job = JOBS[job_id]
        local_now = now.astimezone(TIMEZONE)
        day = local_now.date()
        occurrence = datetime.combine(day, time(job.fire_hour, job.fire_minute), TIMEZONE)
        if local_now < occurrence:
            day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        occurrence = datetime.combine(day, time(job.fire_hour, job.fire_minute), TIMEZONE)
        deadline = datetime.combine(day, DEADLINES[job_id], TIMEZONE)
        return occurrence, deadline

    def _production_row(self, job_id: str, day: date) -> dict[str, Any] | None:
        start = datetime.combine(day, time.min, TIMEZONE).astimezone(UTC)
        end = start + timedelta(days=1)
        connection = self.production.connect()
        try:
            row = connection.execute(
                "SELECT * FROM production_requests "
                "WHERE job_id = ? AND scheduled_for >= ? AND scheduled_for < ? "
                "AND request_id NOT IN (SELECT request_id FROM validation_invocations) "
                "ORDER BY scheduled_for DESC LIMIT 1",
                (
                    job_id,
                    start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    end.isoformat(timespec="seconds").replace("+00:00", "Z"),
                ),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        result = dict(row)
        manifest = result.pop("result_manifest_json", None)
        if manifest is not None:
            result["result_manifest"] = (
                manifest if isinstance(manifest, dict) else json.loads(manifest)
            )
        return result

    @staticmethod
    def _receipt_for(
        snapshot: Mapping[str, Any] | None, job_id: str, expected_day: date
    ) -> Mapping[str, Any] | None:
        if snapshot is None:
            return None
        item = next((job for job in snapshot["jobs"] if job["job_id"] == job_id), None)
        if item is None or not item["enabled"]:
            return None
        execution = item.get("execution")
        if not isinstance(execution, Mapping):
            return None
        claimed_at = execution.get("claimed_at")
        if claimed_at is None or _timestamp(claimed_at, "execution.claimed_at").astimezone(
            TIMEZONE
        ).date() != expected_day:
            return None
        return item

    @staticmethod
    def _report(
        row: Mapping[str, Any] | None,
        receipt: Mapping[str, Any] | None,
        job_id: str,
    ) -> dict[str, Any]:
        job = JOBS[job_id]
        result_id = row.get("result_id") if row else None
        manifest = row.get("result_manifest") if row else None
        files = manifest.get("files") if isinstance(manifest, Mapping) else None
        descriptor = files.get("report.html") if isinstance(files, Mapping) else None
        action_sha256 = manifest.get("action_sha256") if isinstance(manifest, Mapping) else None
        published = (
            row is not None
            and row.get("status") == "SUCCEEDED"
            and isinstance(result_id, str)
            and isinstance(descriptor, Mapping)
            and isinstance(descriptor.get("sha256"), str)
            and isinstance(action_sha256, str)
            and SHA256.fullmatch(action_sha256) is not None
        )
        execution = receipt.get("execution") if receipt else None
        readback = receipt.get("report_readback") if receipt else None
        readback_action_sha256 = (
            readback.get("action_sha256") if isinstance(readback, Mapping) else None
        )
        identity_matched = published and readback_action_sha256 == action_sha256
        client_completed = (
            published
            and isinstance(execution, Mapping)
            and execution.get("status") == "completed"
            and receipt.get("last_status") == "ok"
            and isinstance(readback, Mapping)
            and readback.get("status") == "OK"
            and identity_matched
        )
        return {
            "state": "VERIFIED" if client_completed else "PUBLISHED" if published else "MISSING",
            "url": f"{REPORT_BASE_URL}/{job.report_filename}",
            "result_id": result_id,
            "sha256": descriptor.get("sha256") if isinstance(descriptor, Mapping) else None,
            "action_sha256": action_sha256,
            "readback_sha256": readback.get("sha256") if isinstance(readback, Mapping) else None,
            "readback_size": readback.get("size") if isinstance(readback, Mapping) else None,
            "readback_action_sha256": readback_action_sha256,
            "identity_matched": identity_matched,
        }

    @staticmethod
    def _message(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
        execution = receipt.get("execution") if receipt else None
        observed_target = receipt.get("delivery_target") if receipt else None
        target_matched = observed_target == EXPECTED_DELIVERY_TARGET
        target = {
            "expected": EXPECTED_DELIVERY_TARGET,
            "observed": observed_target,
            "matched": target_matched,
        }
        if not isinstance(execution, Mapping):
            return {
                "state": "MISSING",
                "receipt_id": None,
                "finished_at": None,
                "target": target,
            }
        if execution.get("status") == "completed" and receipt.get("last_status") == "ok":
            if receipt.get("last_delivery_error"):
                state = "FAILED"
            else:
                state = "DELIVERED" if target_matched else "NOT_SENT"
        elif execution.get("status") in {"failed", "unknown"} or receipt.get("last_status") not in {
            None,
            "ok",
        }:
            state = "NOT_SENT"
        else:
            state = "PENDING"
        return {
            "state": state,
            "receipt_id": execution.get("id") if state == "DELIVERED" else None,
            "finished_at": execution.get("finished_at"),
            "target": target,
        }

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DailyLoopStatusError("status clock must be timezone-aware")
        cron_snapshot, receipt_sha256 = self.receipts.read()
        jobs = []
        for job_id, job in JOBS.items():
            expected, deadline = self._latest_expected(job_id, now)
            row = self._production_row(job_id, expected.date())
            receipt = self._receipt_for(cron_snapshot, job_id, expected.date())
            configured = (
                None
                if cron_snapshot is None
                else next(
                    (item for item in cron_snapshot["jobs"] if item["job_id"] == job_id),
                    None,
                )
            )
            run_state = "MISSING" if row is None else str(row.get("status"))
            report = self._report(row, receipt, job_id)
            message = self._message(receipt)
            execution = receipt.get("execution") if receipt else None
            explicit_failure = (
                run_state == "FAILED"
                or (
                    isinstance(execution, Mapping)
                    and execution.get("status") in {"failed", "unknown"}
                )
                or (receipt is not None and receipt.get("last_status") not in {None, "ok"})
            )
            complete = (
                run_state == "SUCCEEDED"
                and report["state"] == "VERIFIED"
                and message["state"] == "DELIVERED"
            )
            finished_at = message["finished_at"]
            late_completion = (
                complete
                and finished_at is not None
                and _timestamp(finished_at, "execution.finished_at") > deadline
            )
            if configured is not None and not configured["enabled"]:
                state = "NOT_EXPECTED"
            elif explicit_failure:
                state = "FAILED"
            elif run_state == "SUCCEEDED" and report["state"] == "VERIFIED" and message["state"] == "FAILED":
                state = "DELIVERY_FAILED"
            elif complete and not late_completion:
                state = "COMPLETE"
            elif now.astimezone(TIMEZONE) >= deadline:
                state = "SILENT_MISS"
            else:
                state = "PENDING"
            if run_state not in {"SUCCEEDED", "FAILED"}:
                incomplete_tier = "RUN"
            elif report["state"] != "VERIFIED":
                incomplete_tier = "REPORT_READBACK"
            elif message["state"] != "DELIVERED":
                incomplete_tier = "MESSAGE_RECEIPT"
            elif late_completion:
                incomplete_tier = "DEADLINE"
            else:
                incomplete_tier = None
            jobs.append(
                {
                    "job_id": job_id,
                    "name": job.name,
                    "schedule": f"{job.schedule} Asia/Shanghai",
                    "expected_for": expected.isoformat(),
                    "deadline": deadline.isoformat(),
                    "state": state,
                    "first_failure": (
                        incomplete_tier
                        if state not in {"PENDING", "NOT_EXPECTED"}
                        else None
                    ),
                    "next_incomplete_tier": incomplete_tier,
                    "run": {
                        "state": run_state,
                        "production_run_id": row.get("production_run_id") if row else None,
                        "result_id": row.get("result_id") if row else None,
                    },
                    "report": report,
                    "message": message,
                }
            )
        return {
            "schema": "quantresearch-daily-loop-status/v1",
            "generated_at": now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "timezone": "Asia/Shanghai",
            "cron_receipt_observed_at": cron_snapshot.get("observed_at") if cron_snapshot else None,
            "cron_receipt_sha256": receipt_sha256,
            "jobs": jobs,
        }
