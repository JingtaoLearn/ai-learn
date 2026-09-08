from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .production_contract import ProductionRequest, canonical_json_bytes, production_run_id


class ProductionStoreError(RuntimeError):
    """Base class for durable production-ledger failures."""


class IdempotencyConflict(ProductionStoreError):
    pass


class ScheduledFireConflict(ProductionStoreError):
    pass


class StateConflict(ProductionStoreError):
    pass


TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED"})
ACTIVE_STATES = frozenset({"ACCEPTED", "ACQUIRING", "COMPUTING", "PUBLISHING"})
MIGRATION_AUTHORITY_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""
SCHEMA_V1 = (
    """
CREATE TABLE production_requests (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    request_body_json TEXT NOT NULL,
    job_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    production_manifest_sha256 TEXT NOT NULL,
    production_release_id TEXT NOT NULL,
    production_run_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('ACCEPTED','ACQUIRING','COMPUTING','PUBLISHING','SUCCEEDED','FAILED')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    experiment_id TEXT,
    attempt_id TEXT,
    result_id TEXT,
    result_manifest_json TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, scheduled_for),
    CHECK((status = 'SUCCEEDED') = (result_id IS NOT NULL)),
    CHECK((status = 'SUCCEEDED') = (result_manifest_json IS NOT NULL)),
    CHECK((status = 'FAILED') = (failure_reason IS NOT NULL))
)
""",
    "CREATE INDEX production_claimable ON production_requests(status, lease_expires_at, created_at)",
    """
CREATE TRIGGER immutable_terminal_update
BEFORE UPDATE ON production_requests
WHEN OLD.status IN ('SUCCEEDED','FAILED')
BEGIN SELECT RAISE(ABORT, 'terminal production rows are immutable'); END
""",
    """
CREATE TRIGGER immutable_terminal_delete
BEFORE DELETE ON production_requests
WHEN OLD.status IN ('SUCCEEDED','FAILED')
BEGIN SELECT RAISE(ABORT, 'terminal production rows are immutable'); END
""",
)
SCHEMA_V2 = """
CREATE TABLE validation_invocations (
validation_id TEXT PRIMARY KEY,
request_id TEXT NOT NULL UNIQUE REFERENCES production_requests(request_id),
job_id TEXT NOT NULL,
validation_for TEXT NOT NULL,
created_at TEXT NOT NULL,
UNIQUE(job_id, validation_for)
)
"""
EXPECTED_SCHEMA_V1 = {
    "schema_migrations": MIGRATION_AUTHORITY_SQL,
    "production_requests": SCHEMA_V1[0],
    "production_claimable": SCHEMA_V1[1],
    "immutable_terminal_update": SCHEMA_V1[2],
    "immutable_terminal_delete": SCHEMA_V1[3],
}
EXPECTED_SCHEMA_V2 = EXPECTED_SCHEMA_V1 | {"validation_invocations": SCHEMA_V2}
MIGRATION_AUTHORITY_COLUMNS = (
    (0, "version", "INTEGER", 0, None, 1, 0),
    (1, "applied_at", "TEXT", 1, None, 0, 0),
)
MIGRATION_AUTHORITY_TABLE = (
    "main",
    "schema_migrations",
    "table",
    len(MIGRATION_AUTHORITY_COLUMNS),
    0,
    0,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProductionStoreError("ledger timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row(value: sqlite3.Row | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result = dict(value)
    for field in ("request_body_json", "result_manifest_json"):
        if result.get(field) is not None:
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
    return result


def _normalized_sql(value: str) -> str:
    return " ".join(value.strip().removesuffix(";").split())


def _schema_objects(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        item["name"]: item["sql"]
        for item in connection.execute(
            "SELECT name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    }


def _require_exact_schema(objects: dict[str, str], expected: dict[str, str]) -> None:
    if set(objects) != set(expected) or any(
        _normalized_sql(objects[name]) != _normalized_sql(statement)
        for name, statement in expected.items()
    ):
        raise ProductionStoreError("production ledger schema is partial or unsupported")


def _require_migration_authority(connection: sqlite3.Connection) -> None:
    object_identity = [
        tuple(item)
        for item in connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_schema WHERE name = 'schema_migrations'"
        )
    ]
    columns = [tuple(item) for item in connection.execute("PRAGMA table_xinfo('schema_migrations')")]
    indexes = [tuple(item) for item in connection.execute("PRAGMA index_list('schema_migrations')")]
    foreign_keys = [
        tuple(item) for item in connection.execute("PRAGMA foreign_key_list('schema_migrations')")
    ]
    table = [tuple(item) for item in connection.execute("PRAGMA table_list('schema_migrations')")]
    if (
        object_identity != [("table", "schema_migrations", "schema_migrations")]
        or columns != list(MIGRATION_AUTHORITY_COLUMNS)
        or indexes
        or foreign_keys
        or table != [MIGRATION_AUTHORITY_TABLE]
    ):
        raise ProductionStoreError("production ledger schema is partial or unsupported")


class ProductionStore:
    """Production ledger using PostgreSQL in normal configured runtime."""

    def __init__(self, state_root: Path | str):
        self.state_root = Path(state_root).absolute()
        self.database_path = self.state_root / "production.sqlite3"
        self._postgres = None
        if os.environ.get("QUANT_POSTGRES_PASSWORD_FILE"):
            from .full_persistence import FullPostgresPersistence

            self._postgres = FullPostgresPersistence.from_environment()

    def _prepare_root(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.stat(self.state_root, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ProductionStoreError("production state root is unsafe")
        if self.database_path.exists() and (
            self.database_path.is_symlink() or not self.database_path.is_file()
        ):
            raise ProductionStoreError("production ledger path is unsafe")

    def connect(self) -> Any:
        if self._postgres is not None:
            return self._postgres.production_connection()
        self._prepare_root()
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._postgres is not None:
            connection = self.connect()
            try:
                versions = [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM production_schema_migrations ORDER BY version"
                    ).fetchall()
                ]
            finally:
                connection.close()
            if versions != [1, 2]:
                raise ProductionStoreError("unsupported PostgreSQL production ledger schema")
            return
        with self.transaction(immediate=True) as connection:
            objects = _schema_objects(connection)
            if "schema_migrations" not in objects:
                if objects:
                    raise ProductionStoreError("production ledger schema is partial or unsupported")
                connection.execute(MIGRATION_AUTHORITY_SQL)
                objects = _schema_objects(connection)
            _require_migration_authority(connection)
            existing = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            if not existing:
                if set(objects) != {"schema_migrations"}:
                    raise ProductionStoreError("production ledger schema is partial or unsupported")
                for statement in SCHEMA_V1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                    (utc_text(utc_now()),),
                )
                existing = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            versions = [item["version"] for item in existing]
            if versions not in ([1], [1, 2]):
                raise ProductionStoreError("unsupported production ledger schema")
            _require_migration_authority(connection)
            expected_v1 = {
                name: statement
                for name, statement in EXPECTED_SCHEMA_V1.items()
                if name != "schema_migrations"
            }
            application_schema = {
                name: statement
                for name, statement in _schema_objects(connection).items()
                if name != "schema_migrations"
            }
            if versions == [1]:
                _require_exact_schema(application_schema, expected_v1)
                connection.execute(SCHEMA_V2)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                    (utc_text(utc_now()),),
                )
                application_schema = {
                    name: statement
                    for name, statement in _schema_objects(connection).items()
                    if name != "schema_migrations"
                }
            expected_v2 = {
                name: statement
                for name, statement in EXPECTED_SCHEMA_V2.items()
                if name != "schema_migrations"
            }
            _require_exact_schema(application_schema, expected_v2)

    def admit(
        self,
        request: ProductionRequest,
        production_release_id: str,
        *,
        validate_new: Callable[[sqlite3.Connection], None],
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        timestamp = utc_text(now or utc_now())
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM production_requests WHERE request_id = ?", (request.request_id,)
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request.request_digest:
                    raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
                return _row(existing) or {}, False
            if request.validation_id is not None:
                validation = connection.execute(
                    "SELECT request_id FROM validation_invocations WHERE validation_id = ?",
                    (request.validation_id,),
                ).fetchone()
                if validation is not None:
                    raise IdempotencyConflict("VALIDATION_ID_CONFLICT")
            fire = connection.execute(
                "SELECT request_id FROM production_requests WHERE job_id = ? AND scheduled_for = ?",
                (request.job_id, request.effective_for),
            ).fetchone()
            if fire is not None:
                raise ScheduledFireConflict("SCHEDULED_FIRE_CONFLICT")
            validate_new(connection)
            run_id = production_run_id(request.request_id, production_release_id)
            connection.execute(
                """
                INSERT INTO production_requests(
                    request_id, request_digest, request_body_json, job_id, scheduled_for,
                    production_manifest_sha256, production_release_id, production_run_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACCEPTED', ?, ?)
                """,
                (
                    request.request_id,
                    request.request_digest,
                    request.canonical_body.decode("utf-8"),
                    request.job_id,
                    request.effective_for,
                    request.production_manifest_sha256,
                    production_release_id,
                    run_id,
                    timestamp,
                    timestamp,
                ),
            )
            if request.validation_id is not None:
                connection.execute(
                    """
                    INSERT INTO validation_invocations(
                        validation_id, request_id, job_id, validation_for, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        request.validation_id,
                        request.request_id,
                        request.job_id,
                        request.effective_for,
                        timestamp,
                    ),
                )
            created = connection.execute(
                "SELECT * FROM production_requests WHERE request_id = ?", (request.request_id,)
            ).fetchone()
            return _row(created) or {}, True

    def active_count(self, connection: sqlite3.Connection | None = None) -> int:
        owned = connection is None
        connection = connection or self.connect()
        try:
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM production_requests WHERE status IN ({placeholders})",
                tuple(sorted(ACTIVE_STATES)),
            ).fetchone()
            return int(row["count"])
        finally:
            if owned:
                connection.close()

    def get_run(self, production_run_id_value: str) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            return _row(
                connection.execute(
                    "SELECT * FROM production_requests WHERE production_run_id = ?",
                    (production_run_id_value,),
                ).fetchone()
            )
        finally:
            connection.close()

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            return _row(
                connection.execute(
                    "SELECT * FROM production_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
            )
        finally:
            connection.close()

    def claim(self, owner: str, *, lease_seconds: int = 120, now: datetime | None = None) -> dict[str, Any] | None:
        if not owner or lease_seconds < 1:
            raise ProductionStoreError("claim identity or lease is invalid")
        clock = now or utc_now()
        current = utc_text(clock)
        expiry = utc_text(clock + timedelta(seconds=lease_seconds))
        with self.transaction(immediate=True) as connection:
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            selected = connection.execute(
                f"""
                SELECT * FROM production_requests
                WHERE status IN ({placeholders})
                  AND (lease_owner IS NULL OR lease_expires_at <= ?)
                ORDER BY created_at, request_id LIMIT 1
                """,
                (*tuple(sorted(ACTIVE_STATES)), current),
            ).fetchone()
            if selected is None:
                return None
            connection.execute(
                """
                UPDATE production_requests
                SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (owner, expiry, current, selected["request_id"]),
            )
            return _row(
                connection.execute(
                    "SELECT * FROM production_requests WHERE request_id = ?",
                    (selected["request_id"],),
                ).fetchone()
            )

    def transition(
        self,
        request_id: str,
        owner: str,
        expected: str,
        target: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if expected not in ACTIVE_STATES or target not in ACTIVE_STATES:
            raise StateConflict("transition state is invalid")
        timestamp = utc_text(now or utc_now())
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                """
                UPDATE production_requests SET status = ?, updated_at = ?
                WHERE request_id = ? AND status = ? AND lease_owner = ?
                """,
                (target, timestamp, request_id, expected, owner),
            ).rowcount
            if changed != 1:
                raise StateConflict("production row state or lease changed")
            return _row(
                connection.execute(
                    "SELECT * FROM production_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
            ) or {}

    def finish_success(
        self,
        request_id: str,
        owner: str,
        *,
        experiment_id: str,
        attempt_id: str,
        result_id: str,
        result_manifest: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = utc_text(now or utc_now())
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                """
                UPDATE production_requests SET
                    status = 'SUCCEEDED', experiment_id = ?, attempt_id = ?, result_id = ?,
                    result_manifest_json = ?, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE request_id = ? AND status = 'PUBLISHING' AND lease_owner = ?
                """,
                (
                    experiment_id,
                    attempt_id,
                    result_id,
                    canonical_json_bytes(result_manifest).decode("utf-8"),
                    timestamp,
                    request_id,
                    owner,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflict("terminal success lost its state or lease")
            return _row(
                connection.execute(
                    "SELECT * FROM production_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
            ) or {}

    def finish_failure(
        self,
        request_id: str,
        owner: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not reason or len(reason.encode("utf-8")) > 4096:
            raise ProductionStoreError("failure reason is invalid")
        timestamp = utc_text(now or utc_now())
        with self.transaction(immediate=True) as connection:
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            changed = connection.execute(
                f"""
                UPDATE production_requests SET status = 'FAILED', failure_reason = ?,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE request_id = ? AND lease_owner = ? AND status IN ({placeholders})
                """,
                (reason, timestamp, request_id, owner, *tuple(sorted(ACTIVE_STATES))),
            ).rowcount
            if changed != 1:
                raise StateConflict("terminal failure lost its state or lease")
            return _row(
                connection.execute(
                    "SELECT * FROM production_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
            ) or {}
