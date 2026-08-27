from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .schemas import parse_semantic_version


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    slots_json TEXT NOT NULL,
    parameter_schema_json TEXT NOT NULL,
    defaults_json TEXT NOT NULL,
    content_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS operators (
    operator_id TEXT PRIMARY KEY,
    slot TEXT NOT NULL,
    title_zh TEXT NOT NULL,
    summary_zh TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_versions (
    operator_id TEXT NOT NULL REFERENCES operators(operator_id),
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    parameter_schema_json TEXT NOT NULL,
    defaults_json TEXT NOT NULL,
    documentation TEXT NOT NULL,
    bundle_path TEXT NOT NULL,
    validation_evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PUBLISHED', 'REJECTED')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (operator_id, version),
    UNIQUE (content_digest)
);

CREATE TABLE IF NOT EXISTS operator_latest (
    operator_id TEXT PRIMARY KEY REFERENCES operators(operator_id),
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    FOREIGN KEY (operator_id, version)
        REFERENCES operator_versions(operator_id, version)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    canonical_attempt_id TEXT,
    canonical_result_digest TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    action_id TEXT NOT NULL UNIQUE,
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    requested_json TEXT NOT NULL,
    resolved_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    logs TEXT,
    result_path TEXT,
    result_digest TEXT,
    comparison TEXT,
    UNIQUE (experiment_id, sequence)
);

CREATE TABLE IF NOT EXISTS replay_tokens (
    token_hash TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operator_versions_status
    ON operator_versions(operator_id, status);
CREATE INDEX IF NOT EXISTS idx_attempts_status_created
    ON attempts(status, created_at);

CREATE TRIGGER IF NOT EXISTS immutable_templates_update
BEFORE UPDATE ON templates BEGIN
    SELECT RAISE(ABORT, 'templates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_templates_delete
BEFORE DELETE ON templates BEGIN
    SELECT RAISE(ABORT, 'templates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_operator_versions_update
BEFORE UPDATE ON operator_versions BEGIN
    SELECT RAISE(ABORT, 'operator versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_operator_versions_delete
BEFORE DELETE ON operator_versions BEGIN
    SELECT RAISE(ABORT, 'operator versions are immutable');
END;
"""


class Catalog:
    def __init__(self, state_root: Path | str):
        self.state_root = Path(state_root).absolute()
        self.database_path = self.state_root / "catalog.sqlite3"

    def _validate_state_root(self) -> None:
        candidate = self.state_root
        if candidate.exists() and candidate.is_symlink():
            raise ValueError(f"catalog state root cannot be a symlink: {candidate}")
        for component in (candidate, *candidate.parents):
            if component.exists():
                metadata = os.stat(component, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(
                        f"catalog state root contains a symlink: {component}"
                    )

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        self._validate_state_root()
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state_root / ".catalog.lock"
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
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

    def initialize(self) -> Catalog:
        with self._initialization_lock():
            connection = self.connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(MIGRATION_1)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (1, '2026-08-27T00:00:00Z')
                    """
                )
            finally:
                connection.close()
            from .seed import seed_catalog

            seed_catalog(self)
        return self

    def template_detail(self, name: str, version: str) -> dict[str, Any]:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM templates WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError(f"unknown template: {name}@{version}")
        return {
            "name": row["name"],
            "version": row["version"],
            "slots": json.loads(row["slots_json"]),
            "parameter_schema": json.loads(row["parameter_schema_json"]),
            "defaults": json.loads(row["defaults_json"]),
            "content_digest": row["content_digest"],
            "created_at": row["created_at"],
        }

    def list_operators(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """
                SELECT o.operator_id, o.slot, o.title_zh, o.summary_zh,
                       l.version AS latest_version, l.content_digest
                FROM operators AS o
                LEFT JOIN operator_latest AS l USING (operator_id)
                ORDER BY o.slot, o.operator_id
                """
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def operator_detail(
        self, operator_id: str, version: str | None = None
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            if version is None:
                row = connection.execute(
                    """
                    SELECT o.*, v.*
                    FROM operator_latest AS l
                    JOIN operators AS o USING (operator_id)
                    JOIN operator_versions AS v
                      ON v.operator_id = l.operator_id AND v.version = l.version
                    WHERE l.operator_id = ?
                    """,
                    (operator_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT o.*, v.*
                    FROM operators AS o
                    JOIN operator_versions AS v USING (operator_id)
                    WHERE o.operator_id = ? AND v.version = ?
                    """,
                    (operator_id, version),
                ).fetchone()
        finally:
            connection.close()
        if row is None:
            selector = "latest" if version is None else version
            raise ValueError(f"unknown published operator: {operator_id}@{selector}")
        return {
            "operator_id": row["operator_id"],
            "slot": row["slot"],
            "version": row["version"],
            "content_digest": row["content_digest"],
            "parameter_schema": json.loads(row["parameter_schema_json"]),
            "defaults": json.loads(row["defaults_json"]),
            "title_zh": row["title_zh"],
            "summary_zh": row["summary_zh"],
            "documentation": row["documentation"],
            "bundle_path": row["bundle_path"],
            "validation_evidence": json.loads(row["validation_evidence_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def _set_latest_if_newer(
        self,
        connection: sqlite3.Connection,
        operator_id: str,
        version: str,
        content_digest: str,
        status: str,
    ) -> None:
        if status != "PUBLISHED":
            return
        current = connection.execute(
            "SELECT version FROM operator_latest WHERE operator_id = ?",
            (operator_id,),
        ).fetchone()
        if current is None or parse_semantic_version(version) > parse_semantic_version(
            current["version"]
        ):
            connection.execute(
                """
                INSERT INTO operator_latest(operator_id, version, content_digest)
                VALUES (?, ?, ?)
                ON CONFLICT(operator_id) DO UPDATE SET
                    version = excluded.version,
                    content_digest = excluded.content_digest
                """,
                (operator_id, version, content_digest),
            )

    def insert_operator_version_for_test(
        self,
        *,
        operator_id: str,
        slot: str,
        version: str,
        content_digest: str,
        parameter_schema: dict[str, Any],
        status: str = "PUBLISHED",
    ) -> None:
        parse_semantic_version(version)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO operators(
                    operator_id, slot, title_zh, summary_zh, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (operator_id, slot, operator_id, operator_id, "2026-08-27T00:00:00Z"),
            )
            connection.execute(
                """
                INSERT INTO operator_versions(
                    operator_id, version, content_digest, parameter_schema_json,
                    defaults_json, documentation, bundle_path,
                    validation_evidence_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operator_id,
                    version,
                    content_digest,
                    json.dumps(parameter_schema, sort_keys=True),
                    "{}",
                    "Test descriptor",
                    f"operators/{operator_id}/{version}",
                    '{"kind":"test"}',
                    status,
                    "2026-08-27T00:00:00Z",
                ),
            )
            self._set_latest_if_newer(
                connection, operator_id, version, content_digest, status
            )


def initialize_catalog(state_root: Path | str) -> Catalog:
    return Catalog(state_root).initialize()
