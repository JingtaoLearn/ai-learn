import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import quant_platform.production_schedule_client as schedule_client
from quant_platform import full_persistence
from quant_platform.production_bocom import BocomProductionJob
from quant_platform.production_result import ProductionResultStore, verify_production_result


FIXTURES = Path(__file__).parent / "fixtures" / "production"


class _Cursor:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row or {"identity": full_persistence.FULL_SCHEMA_IDENTITY}


class _Connection:
    def __init__(self, row=None) -> None:
        self.statements: list[str] = []
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def transaction(self):
        return nullcontext()

    def execute(self, statement: str, parameters=()):
        self.statements.append(" ".join(statement.split()))
        return _Cursor(self.row)


class _SequenceConnection(_Connection):
    def __init__(self, rows) -> None:
        super().__init__()
        self.rows = iter(rows)

    def execute(self, statement: str, parameters=()):
        self.statements.append(" ".join(statement.split()))
        return _Cursor(next(self.rows))


class _Config:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self):
        return self.connection

    def validated(self):
        return self


def _production_result() -> tuple[dict[str, Any], dict[str, bytes]]:
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")
    raw = (FIXTURES / "bocom-yahoo-chart.json").read_bytes()
    computation = job.compute(
        raw,
        "fixture://bocom-yahoo-chart.json",
        datetime(2026, 3, 9, 0, 40, tzinfo=UTC),
    )
    members = ProductionResultStore._artifact_payloads(computation)
    core = ProductionResultStore._manifest_core(
        {
            "request_id": "request",
            "production_run_id": "run",
            "production_release_id": "release",
        },
        computation,
        members,
    )
    result_id = hashlib.sha256(full_persistence.canonical_json_bytes(core)).hexdigest()
    return core | {"result_id": result_id}, members


def _replace_result_id(manifest: dict[str, Any]) -> None:
    core = {key: value for key, value in manifest.items() if key != "result_id"}
    manifest["result_id"] = hashlib.sha256(
        full_persistence.canonical_json_bytes(core)
    ).hexdigest()


def test_schema_installer_keeps_replay_tokens_purgeable_but_not_updatable(
    monkeypatch, tmp_path: Path
) -> None:
    connection = _Connection()
    monkeypatch.setattr(full_persistence, "migrate_schema", lambda *args, **kwargs: None)

    full_persistence.install_full_schema(
        cast(Any, _Config(connection)),
        runtime_user="qr_runtime",
        runtime_password_file=tmp_path / "runtime-password",
    )

    statements = connection.statements
    assert not any(
        'CREATE TRIGGER "replay_tokens_immutable"' in statement
        for statement in statements
    )
    assert (
        'DROP TRIGGER IF EXISTS "replay_tokens_immutable" '
        'ON qr_catalog.replay_tokens'
    ) in statements
    assert (
        'REVOKE UPDATE ON qr_catalog.replay_tokens FROM "qr_runtime"'
    ) in statements
    assert (
        'GRANT DELETE ON qr_catalog.replay_tokens TO "qr_runtime"'
    ) in statements


def test_schema_has_one_mutable_stable_pointer_to_immutable_production_results() -> None:
    assert "CREATE TABLE IF NOT EXISTS qr.production_report_current" in (
        full_persistence.CATALOG_SCHEMA_SQL
    )
    assert "result_id char(64) NOT NULL REFERENCES qr.production_results(result_id)" in (
        full_persistence.CATALOG_SCHEMA_SQL
    )
    assert ("qr", "production_results") in full_persistence.IMMUTABLE_TABLES
    assert ("qr", "production_report_current") not in full_persistence.IMMUTABLE_TABLES


def test_schema_installer_denies_runtime_row_lock_privilege_on_production_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = _Connection()
    monkeypatch.setattr(full_persistence, "migrate_schema", lambda *args, **kwargs: None)

    full_persistence.install_full_schema(
        cast(Any, _Config(connection)),
        runtime_user="qr_runtime",
        runtime_password_file=tmp_path / "runtime-password",
    )

    assert (
        'REVOKE UPDATE, DELETE ON "qr"."production_results" FROM "qr_runtime"'
    ) in connection.statements


def test_postgres_production_result_accepts_complete_verified_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, members = _production_result()
    connection = _Connection(
        {"artifact_set_id": "artifact-set", "manifest": manifest}
    )
    persistence = full_persistence.FullPostgresPersistence(
        cast(Any, _Config(connection)), admit_schema=False
    )
    monkeypatch.setattr(persistence, "read_artifact_set", lambda _artifact_set_id: members)

    assert persistence.production_result(manifest["result_id"]) == {
        "manifest": manifest,
        "members": members,
    }


def test_production_result_binds_to_exact_verified_historical_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, members = _production_result()
    historical = {
        "api_version": 2,
        "content_digest": "275a68f011fe9b45fadc8e1960966f5e7a94809df975507c15f92025b696932f",
        "operator_id": "canonical_attempt_report",
        "source_sha256": "11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8",
        "version": "1.0.0",
    }
    manifest["report_operator"] = historical
    _replace_result_id(manifest)
    original = schedule_client._verify_report_document_sources

    def verify_sources(document, evidence):
        return {**original(document, evidence), "report_operator": historical}

    monkeypatch.setattr(schedule_client, "_verify_report_document_sources", verify_sources)

    assert verify_production_result(manifest["result_id"], manifest, members) == manifest


@pytest.mark.parametrize(
    "tamper",
    [
        "manifest-subset",
        "manifest-extra",
        "stored-result-id",
        "automatic-ordering",
        "semantic-attestation",
        "semantic-subject",
        "model-identity",
        "production-manifest",
        "report-operator",
        "member",
    ],
)
def test_postgres_production_result_rejects_incomplete_or_tampered_identity(
    monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    manifest, members = _production_result()
    requested_result_id = manifest["result_id"]
    if tamper == "manifest-subset":
        manifest["files"].pop("notification.txt")
        members.pop("notification.txt")
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    elif tamper == "manifest-extra":
        payload = b"unexpected"
        manifest["files"]["unexpected.bin"] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        members["unexpected.bin"] = payload
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    elif tamper == "stored-result-id":
        requested_result_id = "f" * 64
    elif tamper == "automatic-ordering":
        manifest["automatic_ordering"] = True
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    elif tamper == "semantic-attestation":
        attestation = json.loads(members["semantic-attestation.json"])
        attestation["status"] = "REJECTED"
        payload = full_persistence.canonical_json_bytes(attestation)
        members["semantic-attestation.json"] = payload
        manifest["files"]["semantic-attestation.json"] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    elif tamper == "semantic-subject":
        attestation = json.loads(members["semantic-attestation.json"])
        attestation["subject_digest"] = "f" * 64
        payload = full_persistence.canonical_json_bytes(attestation)
        members["semantic-attestation.json"] = payload
        manifest["files"]["semantic-attestation.json"] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    elif tamper == "model-identity":
        manifest["model_id"] = "wrong-model"
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    elif tamper == "production-manifest":
        manifest["production_manifest_sha256"] = "f" * 64
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    elif tamper == "report-operator":
        manifest["report_operator"] = dict(manifest["report_operator"])
        manifest["report_operator"]["content_digest"] = "f" * 64
        _replace_result_id(manifest)
        requested_result_id = manifest["result_id"]
    else:
        members["report.html"] += b"tampered"
    connection = _Connection(
        {"artifact_set_id": "artifact-set", "manifest": manifest}
    )
    persistence = full_persistence.FullPostgresPersistence(
        cast(Any, _Config(connection)), admit_schema=False
    )
    monkeypatch.setattr(persistence, "read_artifact_set", lambda _artifact_set_id: members)

    with pytest.raises(full_persistence.PersistenceUnavailableError, match="verification"):
        persistence.production_result(requested_result_id)


def test_stable_production_report_rejects_pointer_manifest_or_member_mismatch(
    monkeypatch,
) -> None:
    report_uuid = "8991e9a8-1caa-41f5-b76b-6368259db5b4"
    result_id = "a" * 64
    html = b"<html>canonical report</html>"
    digest = hashlib.sha256(html).hexdigest()
    pointer = {
        "report_uuid": report_uuid,
        "job_id": "297c11cad0dc",
        "result_id": result_id,
        "report_sha256": digest,
        "generated_at": "2026-03-09T00:40:00Z",
        "report_size": len(html),
    }
    connection = _Connection(pointer)
    persistence = full_persistence.FullPostgresPersistence(
        cast(Any, _Config(connection)),
        admit_schema=False,
    )
    manifest = {
        "result_id": result_id,
        "job_id": pointer["job_id"],
        "report_filename": f"{report_uuid}.html",
        "report_sha256": digest,
        "generated_at": "2026-03-09T00:40:00Z",
        "files": {"report.html": {"sha256": digest, "size": len(html)}},
    }
    monkeypatch.setattr(
        persistence,
        "production_result",
        lambda _result_id: {"manifest": manifest, "members": {"report.html": html}},
    )

    assert persistence.stable_production_report(report_uuid) == {**pointer, "html": html}

    pointer["job_id"] = "wrong-job"
    with pytest.raises(full_persistence.PersistenceUnavailableError, match="binding"):
        persistence.stable_production_report(report_uuid)


def test_stable_pointer_advances_and_reads_back_inside_one_database_transaction(
    monkeypatch,
) -> None:
    report_uuid = "8991e9a8-1caa-41f5-b76b-6368259db5b4"
    result_id = "a" * 64
    html = b"<html>canonical report</html>"
    digest = hashlib.sha256(html).hexdigest()
    manifest = {
        "result_id": result_id,
        "job_id": "297c11cad0dc",
        "report_filename": f"{report_uuid}.html",
        "report_sha256": digest,
        "generated_at": "2026-03-09T00:40:00Z",
        "files": {"report.html": {"sha256": digest, "size": len(html)}},
    }
    pointer = {
        "report_uuid": report_uuid,
        "job_id": manifest["job_id"],
        "result_id": result_id,
        "report_sha256": digest,
        "generated_at": "2026-03-09T00:40:00Z",
        "report_size": len(html),
    }
    connection = _SequenceConnection(
        [None, {"manifest": manifest}, None, pointer]
    )
    persistence = full_persistence.FullPostgresPersistence(
        cast(Any, _Config(connection)),
        admit_schema=False,
    )
    verified = {"manifest": manifest, "members": {"report.html": html}}
    monkeypatch.setattr(persistence, "production_result", lambda _result_id: verified)

    assert persistence.advance_stable_production_report(manifest) == {
        **pointer,
        "html": html,
    }
    assert "pg_advisory_xact_lock" in connection.statements[0]
    assert connection.statements[1] == (
        "SELECT manifest FROM qr.production_results WHERE result_id=%s"
    )
    assert "ON CONFLICT(report_uuid) DO UPDATE" in connection.statements[2]
    assert "FROM qr.production_report_current" in connection.statements[3]


def test_dataset_lineage_reads_verified_payload_from_migrated_member_index() -> None:
    instrument = "601328.SS"
    snapshot_id = "a" * 64
    document = {
        "instrument": instrument,
        "snapshot_id": snapshot_id,
        "lineage": {"kind": "legacy_snapshot"},
    }
    payload = full_persistence.canonical_json_bytes(document) + b"\n"
    digest = hashlib.sha256(payload).hexdigest()
    connection = _Connection(
        {
            "document": {"member_sha256": {"lineage.json": digest}},
            "artifact_sha256": digest,
            "payload": payload,
        }
    )
    persistence = full_persistence.FullPostgresPersistence(
        cast(Any, _Config(connection)),
        admit_schema=False,
    )

    assert persistence.dataset_snapshot_lineage(instrument, snapshot_id) == {
        "kind": "legacy_snapshot"
    }


def test_identical_current_msft_report_is_read_before_closed_membership_publish(
    monkeypatch,
) -> None:
    from quant_platform.msft_trend_study import chinese_report

    study_id = "a" * 64
    result = {
        "schema": "quantresearch-msft-study-result/v1",
        "snapshot_id": "b" * 64,
        "trial_count": 15,
        "selection": None,
        "final": None,
        "verdict": "REJECTED_VALIDATION",
    }
    provenance = {"source": "synthetic"}
    document, html = chinese_report(result, provenance)
    connection = _Connection(
        {"report_artifact_id": document["report_artifact_id"], "sequence": 1}
    )
    persistence = full_persistence.FullPostgresPersistence(
        cast(Any, _Config(connection)),
        admit_schema=False,
    )
    readback = {
        "study_id": study_id,
        "report_artifact_id": document["report_artifact_id"],
        "sequence": 1,
        "document": document,
        "html": html,
    }
    monkeypatch.setattr(persistence, "current_msft_study_report", lambda value: readback)
    monkeypatch.setattr(
        persistence,
        "_publish_artifact_set_in_transaction",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate publish")),
    )

    assert persistence.publish_msft_study_report(
        study_id=study_id,
        result=result,
        provenance=provenance,
    ) == readback
    assert not any("INSERT INTO" in statement for statement in connection.statements)
