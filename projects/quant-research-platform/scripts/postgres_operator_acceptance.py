from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from quant_platform.auth import encode_scrypt_password
from quant_platform.catalog import initialize_catalog
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
            project_root=Path.cwd(),
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
            "schema": "quantresearch-postgresql-operator-acceptance/v1",
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
