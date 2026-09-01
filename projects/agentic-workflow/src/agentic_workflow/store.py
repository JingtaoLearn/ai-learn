"""SQLite control ledger owned by the workflow kernel."""

from __future__ import annotations

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
                applied = {
                    row[0] for row in connection.execute("SELECT version FROM schema_migrations")
                }
                migrations = (
                    (1, "0001_initial.sql"),
                    (2, "0002_action_envelopes.sql"),
                    (3, "0003_operation_records.sql"),
                    (4, "0004_compatibility_decisions.sql"),
                    (5, "0005_matt_receipts.sql"),
                )
                for version, filename in migrations:
                    if version in applied:
                        continue
                    sql = files("agentic_workflow.migrations").joinpath(filename).read_text()
                    _execute_script(connection, sql)
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
