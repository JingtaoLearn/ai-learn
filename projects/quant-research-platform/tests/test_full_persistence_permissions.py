import hashlib
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast

from quant_platform import full_persistence


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


class _Config:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self):
        return self.connection

    def validated(self):
        return self


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
