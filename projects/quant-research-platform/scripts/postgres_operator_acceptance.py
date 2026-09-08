from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

import psycopg
import pandas as pd
from fastapi.testclient import TestClient

from quant_platform.auth import encode_scrypt_password
from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.operator_importer import (
    build_source_manifest,
    import_operators,
)
from quant_platform.operator_worker import validate_candidate
from quant_platform.postgres_persistence import (
    PersistenceUnavailableError,
    PostgresConfig,
    PostgresOperatorPersistence,
)
from quant_platform.resolved_runner import ResolvedAttemptExecutor, ResolvedExecutionError
from quant_platform.schemas import canonical_json_bytes
from quant_platform.settings import Settings
from quant_platform.submissions import EXECUTION_ENVELOPE
from quant_platform.web import create_app


IMAGE = "acceptance.invalid/quantresearch@sha256:" + "a" * 64


def _submission(operator_id: str, documentation: str) -> dict[str, Any]:
    return {
        "operator_id": operator_id,
        "slot": "fit",
        "version": "1.0.0",
        "source": (
            "OPERATOR_API_VERSION = 1\n"
            "SLOT = 'fit'\n"
            "def apply(payload, parameters):\n"
            "    values = payload['values']\n"
            "    selected = values[-parameters['window']:]\n"
            "    return sum(selected) / len(selected)\n"
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "integer", "minimum": 1, "maximum": 3}
            },
            "required": ["window"],
            "additionalProperties": False,
        },
        "defaults": {"window": 2},
        "title_zh": f"合成研究算子 {operator_id}",
        "summary_zh": "仅用于 PostgreSQL 持久化验收，不产生交易或信号影响。",
        "documentation": documentation,
        "tests": [
            {
                "input": {"values": [1.0, 2.0, 4.0]},
                "parameters": {"window": 2},
                "expected": 3.0,
            }
        ],
    }


def _validator(candidate: Path) -> dict[str, Any]:
    result = validate_candidate(candidate)
    return {
        "schema_version": 1,
        "passed": True,
        "slot": result["slot"],
        "candidate_digest": result["candidate_digest"],
        "fixture_digest": result["fixture_digest"],
        "validator_image": IMAGE,
        "execution_envelope": EXECUTION_ENVELOPE,
        "started_at": "2026-09-08T00:00:00Z",
        "finished_at": "2026-09-08T00:00:01Z",
        "duration_seconds": 1.0,
        "exit_status": 0,
        "outcome": "SUCCEEDED",
        "container_id": "a" * 64,
        "termination_confirmed": True,
        "stdout_sha256": hashlib.sha256(b"synthetic acceptance").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "control_path": "transient/acceptance",
        "observations": result["observations"],
    }


def _form(submission: dict[str, Any], csrf: str) -> dict[str, str]:
    return {
        "csrf_token": csrf,
        "operator_id": submission["operator_id"],
        "slot": submission["slot"],
        "version": submission["version"],
        "source": submission["source"],
        "parameter_schema": canonical_json_bytes(submission["parameter_schema"]).decode(),
        "defaults": canonical_json_bytes(submission["defaults"]).decode(),
        "title_zh": submission["title_zh"],
        "summary_zh": submission["summary_zh"],
        "documentation": submission["documentation"],
        "tests": canonical_json_bytes(submission["tests"]).decode(),
    }


def _digest_map(detail: dict[str, Any]) -> dict[str, str]:
    return {
        artifact["logical_name"]: artifact["artifact_sha256"]
        for artifact in detail["artifacts"]
    }


def _write_create_only(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _experiment_task(snapshot_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset": {"instrument": "SYNTH.SS", "snapshot_id": snapshot_id},
        "template": {
            "name": "single_stock_daily_causal",
            "version": "1",
            "parameters": {
                "instrument_display_name": "Synthetic Operator seam",
                "evaluation_start": "2026-01-06",
                "evaluation_end": "2026-01-12",
            },
        },
        "operators": {
            "fit": {
                "operator_id": "pg_ui_synthetic",
                "version": "1.0.0",
                "parameters": {"window": 2},
            },
            "smoothing": {
                "operator_id": "recursive_log_ema",
                "version": "1.0.0",
                "parameters": {"span_sessions": 1},
            },
            "statistic": {
                "operator_id": "adjacent_curve_pct_slope",
                "version": "1.0.0",
                "parameters": {},
            },
            "decision": {
                "operator_id": "post_start_threshold_crossing_hysteresis",
                "version": "1.0.0",
                "parameters": {
                    "buy_threshold_pct_per_day": 1.0,
                    "sell_threshold_abs_pct_per_day": 1.0,
                },
            },
            "sizing": {
                "operator_id": "all_in_all_out_a_share_lots",
                "version": "1.0.0",
                "parameters": {},
            },
            "cost": {
                "operator_id": "cms_china_a_share",
                "version": "1.0.0",
                "parameters": {},
            },
            "report": {
                "operator_id": "concise_chinese_causal_trade",
                "version": "1.0.0",
                "parameters": {},
            },
        },
    }


def _publish_synthetic_snapshot(state_root: Path) -> dict[str, Any]:
    frame = pd.DataFrame(
        [
            ("2026-01-01", 10.0, 10.2, 9.8, 10.0, 10_000, 100.0),
            ("2026-01-02", 10.0, 10.2, 9.8, 10.0, 10_100, 100.0),
            ("2026-01-05", 10.0, 10.2, 9.8, 10.0, 10_200, 100.0),
            ("2026-01-06", 10.0, 10.3, 9.9, 10.1, 10_300, 102.0),
            ("2026-01-07", 10.0, 10.4, 9.9, 10.2, 10_400, 98.0),
            ("2026-01-08", 11.0, 11.3, 10.8, 11.1, 10_500, 98.0),
            ("2026-01-09", 9.0, 9.3, 8.9, 9.2, 10_600, 102.0),
            ("2026-01-12", 10.5, 10.8, 10.3, 10.6, 10_700, 103.0),
        ],
        columns=["Date", "Open", "High", "Low", "Close", "Volume", "AdjustedClose"],
    )
    frame["Date"] = pd.to_datetime(frame["Date"])
    return publish_snapshot(
        frame,
        state_root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )


def run(output: Path) -> dict[str, Any]:
    persistence = PostgresOperatorPersistence.from_environment()
    with tempfile.TemporaryDirectory(prefix="qr-postgres-acceptance-") as temporary:
        root = Path(temporary)
        frozen_source = root / "frozen-source"
        initialize_catalog(frozen_source)
        manifest_path = root / "source-manifest.json"
        _write_create_only(manifest_path, build_source_manifest(frozen_source))
        importer_receipt = import_operators(frozen_source, manifest_path, persistence)

        password = secrets.token_urlsafe(32)
        session_secret = secrets.token_urlsafe(48)
        state_root = root / "web-state"
        settings = Settings(
            environment="test",
            auth_mode="password",
            state_root=state_root,
            public_url="http://testserver",
            allowed_hosts=("testserver",),
            auth_shared_secret=None,
            session_secret=session_secret,
            allowed_emails_file=None,
            sso_login_url="https://login.invalid/auth/login",
            sso_audience="unused",
            sso_callback_url="http://testserver/auth/callback",
            password_scrypt_hash=encode_scrypt_password(password),
            secure_cookies=False,
            runner_image=IMAGE,
            project_root=Path("/opt/project"),
        )
        app = create_app(
            settings,
            operator_persistence=persistence,
            operator_validator=_validator,
        )

        client = TestClient(app, base_url="http://testserver")
        unauthenticated = client.get("/api/operators")
        if unauthenticated.status_code != 401:
            raise AssertionError("Operator machine route did not require authentication")
        issued = app.state.auth.issue_session(
            {"email": "acceptance@invalid", "display_name": "Acceptance"}
        )
        client.cookies.set("quant_session", issued.cookie)

        ui_submission = _submission(
            "pg_ui_synthetic", "# UI synthetic\n\nPostgreSQL-only Operator acceptance."
        )
        ui_publish = client.post(
            "/operators/submit",
            data=_form(ui_submission, issued.csrf_token),
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        if ui_publish.status_code != 303:
            raise AssertionError(f"UI Operator publish failed: {ui_publish.status_code}")
        ui_digest = hashlib.sha256(canonical_json_bytes(ui_submission)).hexdigest()
        machine_read = client.get("/api/operators/pg_ui_synthetic?version=1.0.0")
        machine_detail = machine_read.json()["operator"]
        if (
            machine_read.status_code != 200
            or machine_detail["content_digest"] != ui_digest
            or machine_detail["parameter_schema"] != ui_submission["parameter_schema"]
            or machine_detail["documentation"] != ui_submission["documentation"]
        ):
            raise AssertionError("UI publish to machine detail parity failed")
        machine_artifact_hashes = _digest_map(machine_detail)
        for logical_name, expected_digest in machine_artifact_hashes.items():
            artifact = client.get(
                f"/api/operators/pg_ui_synthetic/artifacts/{logical_name}?version=1.0.0"
            )
            if (
                artifact.status_code != 200
                or hashlib.sha256(artifact.content).hexdigest() != expected_digest
            ):
                raise AssertionError("machine artifact byte parity failed")

        machine_submission = _submission(
            "pg_machine_synthetic",
            "# Machine synthetic\n\nIndependent PostgreSQL-only Operator acceptance.",
        )
        machine_publish = client.post(
            "/api/operators",
            json=machine_submission,
            headers={"origin": "http://testserver", "x-csrf-token": issued.csrf_token},
        )
        if machine_publish.status_code != 201:
            raise AssertionError(f"machine Operator publish failed: {machine_publish.status_code}")
        machine_digest = hashlib.sha256(canonical_json_bytes(machine_submission)).hexdigest()
        ui_read = client.get("/operators/pg_machine_synthetic/1.0.0")
        ui_detail = persistence.operator_detail("pg_machine_synthetic", "1.0.0")
        if (
            ui_read.status_code != 200
            or machine_digest not in ui_read.text
            or ui_detail["parameter_schema"] != machine_submission["parameter_schema"]
            or ui_detail["documentation"] != machine_submission["documentation"]
        ):
            raise AssertionError("machine publish to UI detail parity failed")
        ui_artifact_hashes = _digest_map(ui_detail)
        for logical_name, expected_digest in ui_artifact_hashes.items():
            artifact = client.get(
                f"/operators/pg_machine_synthetic/1.0.0/artifacts/{logical_name}"
            )
            if (
                artifact.status_code != 200
                or hashlib.sha256(artifact.content).hexdigest() != expected_digest
            ):
                raise AssertionError("UI artifact byte parity failed")

        replay = client.post(
            "/api/operators",
            json=machine_submission,
            headers={"origin": "http://testserver", "x-csrf-token": issued.csrf_token},
        )
        if replay.status_code != 200 or replay.json()["status"] != "NO_CHANGE":
            raise AssertionError("same Operator action/content did not return exact existing")
        changed = dict(machine_submission)
        changed["documentation"] += "\nChanged under the same action."
        conflict = client.post(
            "/api/operators",
            json=changed,
            headers={"origin": "http://testserver", "x-csrf-token": issued.csrf_token},
        )
        if conflict.status_code != 400 or conflict.json()["error"]["code"] != "DOMAIN_ERROR":
            raise AssertionError("changed Operator content under one action did not conflict")

        metadata_conflict_submission = dict(machine_submission)
        metadata_conflict_submission["version"] = "2.0.0"
        metadata_conflict_submission["title_zh"] = "冲突的不可变标题"
        metadata_conflict_submission["summary_zh"] = "冲突的不可变摘要"
        metadata_conflict = client.post(
            "/api/operators",
            json=metadata_conflict_submission,
            headers={"origin": "http://testserver", "x-csrf-token": issued.csrf_token},
        )
        if (
            metadata_conflict.status_code != 400
            or metadata_conflict.json()["error"]["code"] != "DOMAIN_ERROR"
        ):
            raise AssertionError("conflicting cross-version Operator metadata did not fail closed")
        unchanged = persistence.operator_detail("pg_machine_synthetic", "1.0.0")
        if (
            unchanged["title_zh"] != machine_submission["title_zh"]
            or unchanged["summary_zh"] != machine_submission["summary_zh"]
        ):
            raise AssertionError("rejected metadata conflict changed immutable read-back")
        try:
            persistence.operator_detail("pg_machine_synthetic", "2.0.0")
        except ValueError:
            pass
        else:
            raise AssertionError("rejected metadata conflict created a later version")

        snapshot = _publish_synthetic_snapshot(state_root)
        created = app.state.experiments.submit(
            _experiment_task(snapshot["snapshot_id"]),
            action_id="postgres-operator-experiment",
        )
        attempt = app.state.experiments.claim_next_attempt()
        if attempt is None or attempt["attempt_id"] != created["attempt_id"]:
            raise AssertionError("PostgreSQL Operator experiment was not resolved and claimed")
        expected_fit = persistence.operator_detail("pg_ui_synthetic", "1.0.0")
        expected_fit_hashes = _digest_map(expected_fit)
        runner_observation: dict[str, Any] = {}

        def observe_materialized_runner_input(command: list[str], **_: Any) -> None:
            mounts = [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--mount"
            ]
            fit_mount = next(
                value for value in mounts if "dst=/operators/fit,readonly" in value
            )
            fit_path = Path(fit_mount.split("src=", 1)[1].split(",dst=", 1)[0])
            observed_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in fit_path.iterdir()
            }
            if observed_hashes != expected_fit_hashes:
                raise AssertionError("ResolvedRunner input bytes differ from PostgreSQL authority")
            if state_root in fit_path.parents or "quant-operator-bundles-" not in str(fit_path):
                raise AssertionError("ResolvedRunner used a persistent Operator bundle path")
            runner_observation.update(
                {
                    "artifact_hashes": observed_hashes,
                    "transient_path": str(fit_path),
                }
            )
            raise OSError("acceptance stops before disposable execution")

        executor = ResolvedAttemptExecutor(
            app.state.catalog,
            operator_persistence=persistence,
            output_root=state_root / "experiment-runs",
            project_root=settings.project_root,
            runner_image=IMAGE,
            attempt_controller=app.state.experiments,
            process_launcher=observe_materialized_runner_input,
        )
        try:
            executor(attempt)
        except ResolvedExecutionError as exc:
            if "process launch failed" not in str(exc):
                raise
        else:
            raise AssertionError("bounded runner-input probe unexpectedly executed a run")
        transient_path = Path(runner_observation["transient_path"])
        if transient_path.exists():
            raise AssertionError("transient PostgreSQL Operator input was not cleaned up")
        with app.state.catalog.connect() as connection:
            sqlite_operator_rows = connection.execute(
                "SELECT count(*) FROM operator_versions"
            ).fetchone()[0]
        if sqlite_operator_rows != 0:
            raise AssertionError("web runtime retained SQLite Operator authority")

        immutable_denials = 0
        with persistence.config.connect() as connection:
            for statement in (
                "UPDATE qr.operator_versions SET documentation = documentation",
                "DELETE FROM qr.artifacts",
            ):
                try:
                    connection.execute(statement)
                    connection.commit()
                except psycopg.errors.InsufficientPrivilege:
                    connection.rollback()
                    immutable_denials += 1
        if immutable_denials != 2:
            raise AssertionError("runtime role could mutate immutable PostgreSQL records")
        closed_member_denials = 0
        with persistence.config.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO qr.artifact_set_members(
                        artifact_set_id, logical_name, artifact_sha256, ordinal
                    )
                    SELECT v.artifact_set_id, 'late-member', min(a.artifact_sha256), 999
                    FROM qr.operator_versions AS v CROSS JOIN qr.artifacts AS a
                    WHERE v.operator_id = 'pg_ui_synthetic'
                    GROUP BY v.artifact_set_id
                    """
                )
                connection.commit()
            except psycopg.errors.ObjectNotInPrerequisiteState:
                connection.rollback()
                closed_member_denials = 1
        if closed_member_denials != 1:
            raise AssertionError("published PostgreSQL artifact membership was not closed")

        result = {
            "schema": "quantresearch-postgresql-operator-acceptance/v2",
            "status": "PASS",
            "importer": importer_receipt,
            "authentication": {
                "unauthenticated_machine_status": unauthenticated.status_code,
                "authenticated_surfaces": ["operator-ui", "operator-machine"],
            },
            "ui_publish_machine_read": {
                "content_digest": ui_digest,
                "artifact_hashes": machine_artifact_hashes,
            },
            "machine_publish_ui_read": {
                "content_digest": machine_digest,
                "artifact_hashes": ui_artifact_hashes,
            },
            "idempotency": replay.json()["status"],
            "conflict_status": conflict.status_code,
            "cross_version_metadata_conflict": {
                "status": metadata_conflict.status_code,
                "immutable_readback": "UNCHANGED",
                "conflicting_version_present": False,
            },
            "experiment_runner_authority": {
                "experiment_id": created["experiment_id"],
                "resolved_content_digest": attempt["resolved"]["operators"]["fit"][
                    "content_digest"
                ],
                "runner_artifact_hashes": runner_observation["artifact_hashes"],
                "sqlite_operator_rows": sqlite_operator_rows,
                "persistent_bundle_path": False,
                "transient_cleanup": not transient_path.exists(),
            },
            "immutable_runtime_denials": immutable_denials,
            "closed_member_denials": closed_member_denials,
        }
    _write_create_only(output, result)
    return result


def expect_unavailable(output: Path) -> dict[str, Any]:
    persistence = PostgresOperatorPersistence(
        PostgresConfig.from_environment(), admit_schema=False
    )
    try:
        persistence.operator_detail("pg_ui_synthetic", "1.0.0")
    except PersistenceUnavailableError:
        result = {
            "schema": "quantresearch-postgresql-fail-closed/v1",
            "status": "PASS",
            "database_read": "UNAVAILABLE",
            "sqlite_or_file_fallback": False,
        }
    else:
        raise AssertionError("Operator read succeeded while PostgreSQL was unavailable")
    _write_create_only(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-unavailable", action="store_true")
    args = parser.parse_args()
    if args.expect_unavailable:
        expect_unavailable(args.output)
    else:
        run(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
