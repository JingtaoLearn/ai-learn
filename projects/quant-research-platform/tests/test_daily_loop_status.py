from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

import pytest

from quant_platform.daily_loop_status import EXPECTED_DELIVERY_TARGET
from quant_platform.production_contract import canonical_json_bytes
from quant_platform.postgres_persistence import PostgresOperatorPersistence
from scripts.daily_loop_receipt_observer import (
    ObservationError,
    _canonical_action,
    _delivery_target,
)
from test_web_api import authenticate, make_app


GOLD = "1cd5557264db"
BOCOM = "297c11cad0dc"
GOLD_REPORT_UUID = "f642b386-74c0-4e9f-92e6-563e7c6a5d69"


def _receipt(
    *,
    gold_action_sha256: str = "c" * 64,
    gold_delivery_target: dict[str, str | None] = EXPECTED_DELIVERY_TARGET,
) -> bytes:
    return json.dumps(
        {
            "schema": "quantresearch-cron-receipts/v2",
            "observed_at": "2026-08-27T01:00:00+00:00",
            "jobs": [
                {
                    "job_id": GOLD,
                    "enabled": True,
                    "schedule": {"kind": "cron", "expr": "40 8 * * 1-5"},
                    "last_run_at": "2026-08-27T08:40:38+08:00",
                    "last_status": "ok",
                    "last_delivery_error": False,
                    "delivery_target": gold_delivery_target,
                    "report_readback": {
                        "status": "OK",
                        "sha256": "a" * 64,
                        "size": 123,
                        "action_sha256": gold_action_sha256,
                    },
                    "execution": {
                        "id": "gold-execution",
                        "status": "completed",
                        "claimed_at": "2026-08-27T08:40:15+08:00",
                        "started_at": "2026-08-27T08:40:16+08:00",
                        "finished_at": "2026-08-27T08:40:38+08:00",
                    },
                },
                {
                    "job_id": BOCOM,
                    "enabled": True,
                    "schedule": {"kind": "cron", "expr": "45 8 * * 1-5"},
                    "last_run_at": "2026-08-26T08:45:30+08:00",
                    "last_status": "ok",
                    "last_delivery_error": False,
                    "delivery_target": EXPECTED_DELIVERY_TARGET,
                    "report_readback": {
                        "status": "OK",
                        "sha256": "b" * 64,
                        "size": 123,
                        "action_sha256": "d" * 64,
                    },
                    "execution": {
                        "id": "old-bocom-execution",
                        "status": "completed",
                        "claimed_at": "2026-08-26T08:45:10+08:00",
                        "started_at": "2026-08-26T08:45:11+08:00",
                        "finished_at": "2026-08-26T08:45:30+08:00",
                    },
                },
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def test_observer_projects_canonical_report_and_delivery_identities() -> None:
    action = {"job_id": GOLD, "report_uuid": GOLD_REPORT_UUID, "action": "HOLD"}
    document = (
        '<html><pre data-action="canonical">'
        + html.escape(json.dumps(action, ensure_ascii=False))
        + "</pre></html>"
    ).encode()

    assert _canonical_action(document, GOLD, GOLD_REPORT_UUID) == hashlib.sha256(
        canonical_json_bytes(action)
    ).hexdigest()
    with pytest.raises(ObservationError, match="identity"):
        _canonical_action(document, BOCOM, GOLD_REPORT_UUID)
    assert _delivery_target("local") == {
        "channel": "local",
        "destination_sha256": hashlib.sha256(b"local").hexdigest(),
    }


def _insert_gold_success(app) -> None:
    manifest = {
        "schema": "quantresearch-production-result/v1",
        "job_id": GOLD,
        "report_filename": "f642b386-74c0-4e9f-92e6-563e7c6a5d69.html",
        "action_sha256": "c" * 64,
        "files": {"report.html": {"sha256": "a" * 64, "size": 123}},
    }
    with app.state.daily_loop.production.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO production_requests(
                request_id, request_digest, request_body_json, job_id, scheduled_for,
                production_manifest_sha256, production_release_id, production_run_id,
                status, experiment_id, attempt_id, result_id, result_manifest_json,
                created_at, updated_at
            ) VALUES (?, ?, '{}', ?, ?, ?, ?, ?, 'SUCCEEDED', ?, ?, ?, ?, ?, ?)
            """,
            (
                "1" * 64,
                "2" * 64,
                GOLD,
                "2026-08-27T00:40:00Z",
                "3" * 64,
                "4" * 64,
                "5" * 64,
                "6" * 64,
                "7" * 64,
                "8" * 64,
                json.dumps(manifest, separators=(",", ":"), sort_keys=True),
                "2026-08-27T00:40:15Z",
                "2026-08-27T00:40:38Z",
            ),
        )
        validation_manifest = {
            **manifest,
            "files": {"report.html": {"sha256": "f" * 64, "size": 456}},
        }
        connection.execute(
            """
            INSERT INTO production_requests(
                request_id, request_digest, request_body_json, job_id, scheduled_for,
                production_manifest_sha256, production_release_id, production_run_id,
                status, experiment_id, attempt_id, result_id, result_manifest_json,
                created_at, updated_at
            ) VALUES (?, ?, '{}', ?, ?, ?, ?, ?, 'SUCCEEDED', ?, ?, ?, ?, ?, ?)
            """,
            (
                "9" * 64,
                "a" * 64,
                GOLD,
                "2026-08-27T01:30:00Z",
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "0" * 64,
                json.dumps(validation_manifest, separators=(",", ":"), sort_keys=True),
                "2026-08-27T01:30:00Z",
                "2026-08-27T01:30:30Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO validation_invocations(
                validation_id, request_id, job_id, validation_for, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "1" * 63 + "0",
                "9" * 64,
                GOLD,
                "2026-08-27T01:30:00Z",
                "2026-08-27T01:30:00Z",
            ),
        )


def test_authenticated_ui_and_api_share_complete_and_silent_miss_status(
    tmp_path: Path, monkeypatch
) -> None:
    class OperatorPersistence(PostgresOperatorPersistence):
        def __init__(self):
            pass

        def verify_schema(self):
            return None

        def list_operators(self):
            return []

    monkeypatch.setattr(
        "quant_platform.web.PostgresOperatorPersistence.from_environment",
        lambda: OperatorPersistence(),
    )
    app, client = make_app(tmp_path)
    _insert_gold_success(app)
    stored = app.state.daily_loop.receipts.replace(_receipt())

    assert client.get("/api/daily-loop/status").status_code == 401
    authenticate(app, client)
    response = client.get("/api/daily-loop/status")

    assert response.status_code == 200
    status = response.json()
    by_id = {job["job_id"]: job for job in status["jobs"]}
    assert status["cron_receipt_sha256"] == stored["receipt_sha256"]
    assert by_id[GOLD]["state"] == "COMPLETE"
    assert by_id[GOLD]["run"]["state"] == "SUCCEEDED"
    assert by_id[GOLD]["report"]["state"] == "VERIFIED"
    assert by_id[GOLD]["message"] == {
        "state": "DELIVERED",
        "receipt_id": "gold-execution",
        "finished_at": "2026-08-27T08:40:38+08:00",
        "target": {
            "expected": EXPECTED_DELIVERY_TARGET,
            "observed": EXPECTED_DELIVERY_TARGET,
            "matched": True,
        },
    }
    assert by_id[BOCOM]["state"] == "SILENT_MISS"
    assert by_id[BOCOM]["first_failure"] == "RUN"
    assert by_id[BOCOM]["run"]["state"] == "MISSING"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'data-testid="daily-loop-status"' in dashboard.text
    assert "COMPLETE" in dashboard.text
    assert "SILENT_MISS" in dashboard.text
    assert "gold-execution"[:12] in dashboard.text

    health = client.get("/health")
    assert health.status_code == 200
    assert "jobs" not in health.json()


def test_report_readback_identity_mismatch_cannot_complete(tmp_path: Path, monkeypatch) -> None:
    class OperatorPersistence(PostgresOperatorPersistence):
        def __init__(self):
            pass

        def verify_schema(self):
            return None

        def list_operators(self):
            return []

    monkeypatch.setattr(
        "quant_platform.web.PostgresOperatorPersistence.from_environment",
        lambda: OperatorPersistence(),
    )
    app, client = make_app(tmp_path)
    _insert_gold_success(app)
    app.state.daily_loop.receipts.replace(_receipt(gold_action_sha256="e" * 64))
    authenticate(app, client)

    gold = {job["job_id"]: job for job in client.get("/api/daily-loop/status").json()["jobs"]}[
        GOLD
    ]
    assert gold["state"] == "SILENT_MISS"
    assert gold["first_failure"] == "REPORT_READBACK"
    assert gold["report"]["state"] == "PUBLISHED"
    assert gold["report"]["identity_matched"] is False


def test_wrong_or_local_delivery_target_cannot_complete(tmp_path: Path, monkeypatch) -> None:
    class OperatorPersistence(PostgresOperatorPersistence):
        def __init__(self):
            pass

        def verify_schema(self):
            return None

        def list_operators(self):
            return []

    monkeypatch.setattr(
        "quant_platform.web.PostgresOperatorPersistence.from_environment",
        lambda: OperatorPersistence(),
    )
    app, client = make_app(tmp_path)
    _insert_gold_success(app)
    local_target = {"channel": "local", "destination_sha256": "f" * 64}
    app.state.daily_loop.receipts.replace(_receipt(gold_delivery_target=local_target))
    authenticate(app, client)

    gold = {job["job_id"]: job for job in client.get("/api/daily-loop/status").json()["jobs"]}[
        GOLD
    ]
    assert gold["state"] == "SILENT_MISS"
    assert gold["first_failure"] == "MESSAGE_RECEIPT"
    assert gold["message"]["state"] == "NOT_SENT"
    assert gold["message"]["receipt_id"] is None
    assert gold["message"]["target"] == {
        "expected": EXPECTED_DELIVERY_TARGET,
        "observed": local_target,
        "matched": False,
    }
