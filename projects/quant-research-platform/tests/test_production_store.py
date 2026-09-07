from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from quant_platform.production_contract import ProductionRelease, ProductionRequest, canonical_json_bytes
from quant_platform.production_service import AdmissionPolicy, ProductionService
from quant_platform.production_store import (
    IdempotencyConflict,
    ProductionStore,
    ScheduledFireConflict,
)


MANIFEST = "6f9f10ed235c6229582ca2843c8b983a887dbdc0ac289170ca834e580bcae969"
FIRE = "2026-03-09T00:40:00Z"
NOW = datetime(2026, 3, 9, 0, 40, tzinfo=UTC)


def release(fill: str = "a") -> ProductionRelease:
    return ProductionRelease.from_mapping(
        {
            "schema": "quantresearch-production-release/v1",
            "production_api_image_digest": fill * 64,
            "source_commit": "b" * 40,
            "source_tree_sha256": "c" * 64,
            "dependency_lock_sha256": "d" * 64,
            "effective_compose_config_sha256": "e" * 64,
            "provider_contract_sha256": "f" * 64,
            "api_contract_sha256": "0" * 64,
        }
    )


def setup(tmp_path):
    store = ProductionStore(tmp_path / "state")
    store.initialize()
    policy = AdmissionPolicy({"297c11cad0dc": MANIFEST}, release())
    return store, ProductionService(store, policy, clock=lambda: NOW)


def request() -> ProductionRequest:
    return ProductionRequest.build(
        job_id="297c11cad0dc", scheduled_for=FIRE, production_manifest_sha256=MANIFEST
    )


def test_concurrent_same_key_creates_one_row_and_one_identity(tmp_path) -> None:
    store, service = setup(tmp_path)
    value = request()

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _: service.create_or_read(value.canonical_body, value.request_id), range(16))
        )

    assert {status for status, _ in responses} == {202}
    assert len({body["production_run_id"] for _, body in responses}) == 1
    connection = store.connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM production_requests").fetchone()[0] == 1
    finally:
        connection.close()


def test_existing_key_precedes_changed_window_release_and_allowlist(tmp_path) -> None:
    store, service = setup(tmp_path)
    value = request()
    _, original = service.create_or_read(value.canonical_body, value.request_id)
    changed = ProductionService(
        store,
        AdmissionPolicy({}, release("1"), ready=lambda: False),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    status, replay = changed.create_or_read(value.canonical_body, value.request_id)

    assert status == 202
    assert replay["production_run_id"] == original["production_run_id"]
    assert replay["production_release_id"] == original["production_release_id"]


def test_both_conflict_classes_execute_before_new_admission_gates(tmp_path) -> None:
    store, service = setup(tmp_path)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    changed_body = value.body | {"production_manifest_sha256": "1" * 64}
    with pytest.raises(IdempotencyConflict):
        service.create_or_read(canonical_json_bytes(changed_body), value.request_id)

    other = ProductionRequest.build(
        job_id=value.job_id,
        scheduled_for=value.scheduled_for,
        production_manifest_sha256="1" * 64,
    )
    with pytest.raises(ScheduledFireConflict):
        service.create_or_read(other.canonical_body, other.request_id)


def test_terminal_rows_and_results_are_update_delete_immutable(tmp_path) -> None:
    store, service = setup(tmp_path)
    value = request()
    service.create_or_read(value.canonical_body, value.request_id)
    row = store.claim("worker", now=NOW)
    assert row is not None
    for source, target in (
        ("ACCEPTED", "ACQUIRING"),
        ("ACQUIRING", "COMPUTING"),
        ("COMPUTING", "PUBLISHING"),
    ):
        store.transition(value.request_id, "worker", source, target, now=NOW)
    store.finish_success(
        value.request_id,
        "worker",
        experiment_id="1" * 64,
        attempt_id="2" * 64,
        result_id="3" * 64,
        result_manifest={"result_id": "3" * 64},
        now=NOW,
    )
    connection = store.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE production_requests SET result_id = ? WHERE request_id = ?",
                ("4" * 64, value.request_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM production_requests WHERE request_id = ?", (value.request_id,))
    finally:
        connection.close()


def test_ledger_is_separate_production_sqlite_v1(tmp_path) -> None:
    store, _ = setup(tmp_path)
    assert store.database_path.name == "production.sqlite3"
    assert not (store.state_root / "catalog.sqlite3").exists()
    connection = store.connect()
    try:
        assert json.loads(json.dumps(connection.execute("SELECT version FROM schema_migrations").fetchone()[0])) == 1
    finally:
        connection.close()
