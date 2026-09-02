"""SQLite control ledger owned by the workflow kernel."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

_PRIVATE_TRANSACTION_BEGIN_HOOK: Callable[[], None] | None = None


class ControlStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _configure(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")

    def _migrate(self) -> None:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        try:
            self._configure(connection)
            self._enable_wal(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
                )
                migrations = (
                    (1, "0001_initial.sql"),
                    (2, "0002_action_envelopes.sql"),
                    (3, "0003_operation_records.sql"),
                    (4, "0004_compatibility_decisions.sql"),
                    (5, "0005_matt_receipts.sql"),
                    (6, "0006_route_handoffs.sql"),
                    (7, "0007_replay_shadow_operations.sql"),
                )
                applied_rows = connection.execute(
                    "SELECT version, typeof(version) FROM schema_migrations "
                    "ORDER BY version, rowid"
                ).fetchall()
                applied_versions = [row[0] for row in applied_rows]
                known_versions = [version for version, _ in migrations]
                if (
                    any(row[1] != "integer" for row in applied_rows)
                    or applied_versions != known_versions[: len(applied_versions)]
                ):
                    raise sqlite3.IntegrityError("schema migration history is invalid")
                applied = set(applied_versions)
                for version, filename in migrations:
                    if version in applied:
                        continue
                    sql = files("agentic_workflow.migrations").joinpath(filename).read_text()
                    _execute_script(connection, sql)
                    if version == 7:
                        _backfill_v6_operations(connection)
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        finally:
            connection.close()

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        for attempt in range(5):
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            self._configure(connection)
            begin_hook = _PRIVATE_TRANSACTION_BEGIN_HOOK
            if begin_hook is not None:
                connection.set_trace_callback(
                    lambda statement: begin_hook() if statement == "BEGIN IMMEDIATE" else None
                )
            try:
                connection.execute("BEGIN IMMEDIATE")
            finally:
                if begin_hook is not None:
                    connection.set_trace_callback(None)
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        uri = f"{self.database_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()


def _execute_script(connection: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete migration statement")


def _backfill_v6_operations(connection: sqlite3.Connection) -> None:
    """Validate and preserve both legal closed-v6 Operation histories exactly."""
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT o.*, e.action_envelope_digest FROM operation_records AS o "
        "JOIN action_envelopes AS e ON e.action_envelope_id = o.action_envelope_id "
        "WHERE o.mode IS NULL ORDER BY o.reserved_at, o.operation_id"
    ).fetchall()
    for row in rows:
        legacy = _canonical_object(row["operation_json"], row["operation_digest"])
        binding = {
            "active_intent_digest": row["active_intent_digest"],
            "constitution_revision": row["constitution_revision"],
            "goal_revision": row["goal_revision"],
            "operating_profile_revision": row["operating_profile_revision"],
        }
        if legacy != {
            "action_envelope_digest": row["action_envelope_digest"],
            "effect_kind": "BOUNDED_WORK",
            "intent_binding": binding,
            "operation_id": row["operation_id"],
        }:
            raise sqlite3.IntegrityError("legacy v6 Operation Record is invalid")
        events = connection.execute(
            "SELECT * FROM operation_events WHERE operation_id = ? ORDER BY event_number",
            (row["operation_id"],),
        ).fetchall()
        event_types = tuple(event["event_type"] for event in events)
        if event_types not in {("RESERVED",), ("RESERVED", "CONCLUDED")}:
            raise sqlite3.IntegrityError("legacy v6 Operation event is invalid")
        for number, event in enumerate(events, 1):
            expected = {
                "event_type": event_types[number - 1],
                "intent_binding": binding,
                "operation_digest": row["operation_digest"],
            }
            if event_types[number - 1] == "CONCLUDED":
                expected["result"] = "NO_EXTERNAL_EFFECT"
            expected_json = _canonical_json(expected)
            if (
                event["event_number"] != number
                or event["payload_json"] != expected_json
                or event["payload_digest"] != _digest(expected_json)
                or event["constitution_revision"] != binding["constitution_revision"]
                or event["goal_revision"] != binding["goal_revision"]
                or event["operating_profile_revision"]
                != binding["operating_profile_revision"]
                or event["active_intent_digest"] != binding["active_intent_digest"]
                or not isinstance(event["recorded_at"], str)
                or not event["recorded_at"]
            ):
                raise sqlite3.IntegrityError("legacy v6 Operation event is invalid")
    connection.execute(
        "CREATE TRIGGER operation_records_no_update BEFORE UPDATE ON operation_records "
        "BEGIN SELECT RAISE(ABORT, 'operation records are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER operation_records_no_delete BEFORE DELETE ON operation_records "
        "BEGIN SELECT RAISE(ABORT, 'operation records are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER operation_events_no_update BEFORE UPDATE ON operation_events "
        "BEGIN SELECT RAISE(ABORT, 'operation events are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER operation_events_no_delete BEFORE DELETE ON operation_events "
        "BEGIN SELECT RAISE(ABORT, 'operation events are append-only'); END"
    )


def _canonical_object(payload_json: str, payload_digest: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError) as error:
        raise sqlite3.IntegrityError("legacy v6 Operation Record is invalid") from error
    if (
        not isinstance(payload, dict)
        or _canonical_json(payload) != payload_json
        or _digest(payload_json) != payload_digest
    ):
        raise sqlite3.IntegrityError("legacy v6 Operation Record is invalid")
    return payload


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
