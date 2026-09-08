from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .schemas import canonical_json_bytes, parse_semantic_version


SCHEMA_NAME = "qr"
SCHEMA_IDENTITY = "quantresearch-postgresql-operator-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class PersistenceUnavailableError(RuntimeError):
    """Raised when PostgreSQL cannot satisfy the persistence interface."""


class PersistenceSchemaError(PersistenceUnavailableError):
    """Raised when the connected database does not have the expected schema."""


class OperatorPersistenceConflict(ValueError):
    """Raised when an immutable Operator action has different content."""


@dataclass(frozen=True)
class PostgresConfig:
    host: str
    port: int
    dbname: str
    user: str
    password_file: Path = field(repr=False)
    connect_timeout: int = 5

    @classmethod
    def from_environment(cls, prefix: str = "QUANT_POSTGRES_") -> PostgresConfig:
        password_file = os.environ.get(f"{prefix}PASSWORD_FILE")
        if not password_file:
            raise PersistenceUnavailableError(f"{prefix}PASSWORD_FILE is required")
        try:
            port = int(os.environ.get(f"{prefix}PORT", "5432"))
            timeout = int(os.environ.get(f"{prefix}CONNECT_TIMEOUT", "5"))
        except ValueError as exc:
            raise PersistenceUnavailableError("PostgreSQL port/timeout must be integers") from exc
        return cls(
            host=os.environ.get(f"{prefix}HOST", "postgres"),
            port=port,
            dbname=os.environ.get(f"{prefix}DATABASE", "quantresearch"),
            user=os.environ.get(f"{prefix}USER", "qr_runtime"),
            password_file=Path(password_file),
            connect_timeout=timeout,
        ).validated()

    def validated(self) -> PostgresConfig:
        if not self.host or not self.dbname or not self.user:
            raise PersistenceUnavailableError("PostgreSQL connection identity is incomplete")
        if not 1 <= self.port <= 65535 or not 1 <= self.connect_timeout <= 60:
            raise PersistenceUnavailableError("PostgreSQL port/timeout is outside its allowed range")
        try:
            metadata = os.stat(self.password_file, follow_symlinks=False)
        except OSError as exc:
            raise PersistenceUnavailableError("PostgreSQL password file is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PersistenceUnavailableError("PostgreSQL password file must be a regular file")
        return self

    def password(self) -> str:
        self.validated()
        try:
            value = self.password_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise PersistenceUnavailableError("PostgreSQL password file is unreadable") from exc
        if not value or "\0" in value or "\n" in value or "\r" in value:
            raise PersistenceUnavailableError("PostgreSQL password file has invalid content")
        return value

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        try:
            return psycopg.connect(
                host=self.host,
                port=self.port,
                dbname=self.dbname,
                user=self.user,
                password=self.password(),
                connect_timeout=self.connect_timeout,
                autocommit=autocommit,
                row_factory=dict_row,
            )
        except (psycopg.Error, OSError) as exc:
            raise PersistenceUnavailableError("PostgreSQL is unavailable") from exc


@dataclass(frozen=True)
class ArtifactInput:
    logical_name: str
    media_type: str
    payload: bytes

    def validated(self) -> ArtifactInput:
        if (
            not self.logical_name
            or self.logical_name in {".", ".."}
            or "/" in self.logical_name
            or "\\" in self.logical_name
            or "\0" in self.logical_name
        ):
            raise ValueError("artifact logical_name is invalid")
        if not self.media_type or "\0" in self.media_type:
            raise ValueError("artifact media_type is invalid")
        if not isinstance(self.payload, bytes) or len(self.payload) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact payload is invalid or exceeds its limit")
        return self


SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME};

CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.schema_identity (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    identity text NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.artifacts (
    artifact_sha256 char(64) PRIMARY KEY CHECK (artifact_sha256 ~ '^[0-9a-f]{{64}}$'),
    byte_size bigint NOT NULL CHECK (byte_size >= 0 AND byte_size <= {MAX_ARTIFACT_BYTES}),
    media_type text NOT NULL CHECK (length(media_type) > 0),
    payload bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (octet_length(payload) = byte_size),
    CHECK (encode(digest(payload, 'sha256'), 'hex') = artifact_sha256)
);

CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.artifact_sets (
    artifact_set_id char(64) PRIMARY KEY CHECK (artifact_set_id ~ '^[0-9a-f]{{64}}$'),
    kind text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    canonical_manifest jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.artifact_set_members (
    artifact_set_id char(64) NOT NULL REFERENCES {SCHEMA_NAME}.artifact_sets(artifact_set_id),
    logical_name text NOT NULL CHECK (
        length(logical_name) > 0 AND
        position('/' in logical_name) = 0 AND
        position('\\' in logical_name) = 0
    ),
    artifact_sha256 char(64) NOT NULL REFERENCES {SCHEMA_NAME}.artifacts(artifact_sha256),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (artifact_set_id, logical_name),
    UNIQUE (artifact_set_id, ordinal)
);

CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.operators (
    operator_id text PRIMARY KEY CHECK (operator_id ~ '^[a-z][a-z0-9_]{{1,63}}$'),
    slot text NOT NULL CHECK (slot IN ('fit','smoothing','statistic','decision','sizing','cost','report')),
    title_zh text NOT NULL,
    summary_zh text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.operator_versions (
    operator_id text NOT NULL REFERENCES {SCHEMA_NAME}.operators(operator_id),
    version text NOT NULL,
    action_id text NOT NULL UNIQUE,
    content_digest char(64) NOT NULL UNIQUE CHECK (content_digest ~ '^[0-9a-f]{{64}}$'),
    parameter_schema jsonb NOT NULL,
    defaults jsonb NOT NULL,
    documentation text NOT NULL,
    validation_evidence jsonb NOT NULL,
    artifact_set_id char(64) NOT NULL REFERENCES {SCHEMA_NAME}.artifact_sets(artifact_set_id),
    status text NOT NULL CHECK (status = 'PUBLISHED'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (operator_id, version),
    CHECK (action_id = operator_id || '@' || version)
);

CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.operator_current (
    operator_id text PRIMARY KEY REFERENCES {SCHEMA_NAME}.operators(operator_id),
    version text NOT NULL,
    content_digest char(64) NOT NULL,
    FOREIGN KEY (operator_id, version)
        REFERENCES {SCHEMA_NAME}.operator_versions(operator_id, version)
);

CREATE OR REPLACE FUNCTION {SCHEMA_NAME}.reject_immutable_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION {SCHEMA_NAME}.reject_member_after_publication()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM {SCHEMA_NAME}.operator_versions
        WHERE artifact_set_id = NEW.artifact_set_id
    ) THEN
        RAISE EXCEPTION 'published artifact set membership is closed' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS schema_identity_immutable ON {SCHEMA_NAME}.schema_identity;
CREATE TRIGGER schema_identity_immutable
BEFORE UPDATE OR DELETE ON {SCHEMA_NAME}.schema_identity
FOR EACH ROW EXECUTE FUNCTION {SCHEMA_NAME}.reject_immutable_change();
DROP TRIGGER IF EXISTS artifacts_immutable ON {SCHEMA_NAME}.artifacts;
CREATE TRIGGER artifacts_immutable
BEFORE UPDATE OR DELETE ON {SCHEMA_NAME}.artifacts
FOR EACH ROW EXECUTE FUNCTION {SCHEMA_NAME}.reject_immutable_change();
DROP TRIGGER IF EXISTS artifact_sets_immutable ON {SCHEMA_NAME}.artifact_sets;
CREATE TRIGGER artifact_sets_immutable
BEFORE UPDATE OR DELETE ON {SCHEMA_NAME}.artifact_sets
FOR EACH ROW EXECUTE FUNCTION {SCHEMA_NAME}.reject_immutable_change();
DROP TRIGGER IF EXISTS artifact_set_members_immutable ON {SCHEMA_NAME}.artifact_set_members;
CREATE TRIGGER artifact_set_members_immutable
BEFORE UPDATE OR DELETE ON {SCHEMA_NAME}.artifact_set_members
FOR EACH ROW EXECUTE FUNCTION {SCHEMA_NAME}.reject_immutable_change();
DROP TRIGGER IF EXISTS artifact_set_members_closed ON {SCHEMA_NAME}.artifact_set_members;
CREATE TRIGGER artifact_set_members_closed
BEFORE INSERT ON {SCHEMA_NAME}.artifact_set_members
FOR EACH ROW EXECUTE FUNCTION {SCHEMA_NAME}.reject_member_after_publication();
DROP TRIGGER IF EXISTS operators_immutable ON {SCHEMA_NAME}.operators;
CREATE TRIGGER operators_immutable
BEFORE UPDATE OR DELETE ON {SCHEMA_NAME}.operators
FOR EACH ROW EXECUTE FUNCTION {SCHEMA_NAME}.reject_immutable_change();
DROP TRIGGER IF EXISTS operator_versions_immutable ON {SCHEMA_NAME}.operator_versions;
CREATE TRIGGER operator_versions_immutable
BEFORE UPDATE OR DELETE ON {SCHEMA_NAME}.operator_versions
FOR EACH ROW EXECUTE FUNCTION {SCHEMA_NAME}.reject_immutable_change();
"""


def migrate_schema(
    migration_config: PostgresConfig,
    *,
    runtime_user: str,
    runtime_password_file: Path,
) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", runtime_user):
        raise ValueError("runtime PostgreSQL role name is invalid")
    runtime_config = PostgresConfig(
        host=migration_config.host,
        port=migration_config.port,
        dbname=migration_config.dbname,
        user=runtime_user,
        password_file=runtime_password_file,
        connect_timeout=migration_config.connect_timeout,
    )
    runtime_password = runtime_config.password()
    with migration_config.connect() as connection:
        with connection.transaction():
            role = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (runtime_user,)
            ).fetchone()
            if role is None:
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(runtime_user), sql.Literal(runtime_password)
                    )
                )
            else:
                connection.execute(
                    sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(runtime_user), sql.Literal(runtime_password)
                    )
                )
            connection.execute(SCHEMA_SQL)
            inserted = connection.execute(
                f"""
                INSERT INTO {SCHEMA_NAME}.schema_identity(singleton, identity)
                VALUES (true, %s)
                ON CONFLICT (singleton) DO NOTHING
                RETURNING identity
                """,
                (SCHEMA_IDENTITY,),
            ).fetchone()
            if inserted is None:
                existing = connection.execute(
                    f"SELECT identity FROM {SCHEMA_NAME}.schema_identity WHERE singleton"
                ).fetchone()
                if existing is None or existing["identity"] != SCHEMA_IDENTITY:
                    raise PersistenceSchemaError("PostgreSQL schema identity conflicts")
            connection.execute(f"REVOKE ALL ON SCHEMA {SCHEMA_NAME} FROM PUBLIC")
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(SCHEMA_NAME), sql.Identifier(runtime_user)
                )
            )
            immutable_tables = (
                "schema_identity",
                "artifacts",
                "artifact_sets",
                "artifact_set_members",
                "operators",
                "operator_versions",
            )
            for table in immutable_tables:
                connection.execute(
                    sql.SQL("REVOKE ALL ON {}.{} FROM {}").format(
                        sql.Identifier(SCHEMA_NAME),
                        sql.Identifier(table),
                        sql.Identifier(runtime_user),
                    )
                )
                connection.execute(
                    sql.SQL("GRANT SELECT, INSERT ON {}.{} TO {}").format(
                        sql.Identifier(SCHEMA_NAME),
                        sql.Identifier(table),
                        sql.Identifier(runtime_user),
                    )
                )
            connection.execute(
                sql.SQL("REVOKE ALL ON {}.operator_current FROM {}").format(
                    sql.Identifier(SCHEMA_NAME), sql.Identifier(runtime_user)
                )
            )
            connection.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE ON {}.operator_current TO {}"
                ).format(sql.Identifier(SCHEMA_NAME), sql.Identifier(runtime_user))
            )


def _canonical_time(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be an unambiguous ISO-8601 timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    return value.astimezone(UTC)


def _time_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _artifact_manifest(artifacts: Iterable[ArtifactInput]) -> tuple[list[ArtifactInput], dict[str, Any], str]:
    ordered = sorted((artifact.validated() for artifact in artifacts), key=lambda item: item.logical_name)
    if not ordered or len({item.logical_name for item in ordered}) != len(ordered):
        raise ValueError("artifact set must be non-empty with unique logical names")
    members = [
        {
            "logical_name": item.logical_name,
            "artifact_sha256": hashlib.sha256(item.payload).hexdigest(),
            "byte_size": len(item.payload),
            "media_type": item.media_type,
        }
        for item in ordered
    ]
    manifest = {"schema_version": 1, "kind": "OPERATOR_BUNDLE", "members": members}
    set_id = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return ordered, manifest, set_id


class PostgresOperatorPersistence:
    """Deep PostgreSQL Operator persistence module.

    The interface exposes domain publication/read operations. Connection handling,
    transactions, schema admission, artifact hashing, closed membership, immutable
    records, current selection, and exact-byte retrieval remain implementation details.
    """

    def __init__(self, config: PostgresConfig, *, admit_schema: bool = True):
        self.config = config.validated()
        if admit_schema:
            self.verify_schema()

    @classmethod
    def from_environment(cls) -> PostgresOperatorPersistence:
        return cls(PostgresConfig.from_environment())

    def verify_schema(self) -> None:
        try:
            with self.config.connect() as connection:
                row = connection.execute(
                    f"SELECT identity FROM {SCHEMA_NAME}.schema_identity WHERE singleton"
                ).fetchone()
        except PersistenceUnavailableError:
            raise
        except psycopg.Error as exc:
            raise PersistenceSchemaError("PostgreSQL schema is unavailable") from exc
        if row is None or row["identity"] != SCHEMA_IDENTITY:
            raise PersistenceSchemaError("PostgreSQL schema identity is unavailable or unexpected")

    def is_empty(self) -> bool:
        with self.config.connect() as connection:
            row = connection.execute(
                f"SELECT count(*) AS count FROM {SCHEMA_NAME}.operator_versions"
            ).fetchone()
        return row["count"] == 0

    def publish_operator(
        self,
        *,
        operator_id: str,
        slot: str,
        version: str,
        title_zh: str,
        summary_zh: str,
        content_digest: str,
        parameter_schema: dict[str, Any],
        defaults: dict[str, Any],
        documentation: str,
        validation_evidence: dict[str, Any],
        artifacts: Iterable[ArtifactInput],
        created_at: datetime | str,
        operator_created_at: datetime | str | None = None,
    ) -> dict[str, str]:
        parse_semantic_version(version)
        if SHA256_RE.fullmatch(content_digest) is None:
            raise ValueError("content_digest must be lowercase SHA-256")
        action_id = f"{operator_id}@{version}"
        timestamp = _canonical_time(created_at)
        operator_timestamp = _canonical_time(operator_created_at or created_at)
        ordered, manifest, artifact_set_id = _artifact_manifest(artifacts)
        try:
            with self.config.connect() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (action_id,)
                    )
                    existing = connection.execute(
                        f"""
                        SELECT content_digest, artifact_set_id
                        FROM {SCHEMA_NAME}.operator_versions
                        WHERE action_id = %s
                        """,
                        (action_id,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            existing["content_digest"].strip() != content_digest
                            or existing["artifact_set_id"].strip() != artifact_set_id
                        ):
                            raise OperatorPersistenceConflict(
                                f"{action_id} already exists with different content"
                            )
                        return {
                            "status": "NO_CHANGE",
                            "operator_id": operator_id,
                            "version": version,
                            "content_digest": content_digest,
                        }
                    operator = connection.execute(
                        f"SELECT slot, title_zh, summary_zh FROM {SCHEMA_NAME}.operators WHERE operator_id = %s",
                        (operator_id,),
                    ).fetchone()
                    if operator is not None and operator["slot"] != slot:
                        raise OperatorPersistenceConflict(
                            f"operator {operator_id} immutable metadata conflicts"
                        )
                    for item, member in zip(ordered, manifest["members"], strict=True):
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA_NAME}.artifacts(
                                artifact_sha256, byte_size, media_type, payload
                            ) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (artifact_sha256) DO NOTHING
                            """,
                            (
                                member["artifact_sha256"],
                                member["byte_size"],
                                member["media_type"],
                                item.payload,
                            ),
                        )
                        stored = connection.execute(
                            f"""
                            SELECT byte_size, media_type, payload
                            FROM {SCHEMA_NAME}.artifacts WHERE artifact_sha256 = %s
                            """,
                            (member["artifact_sha256"],),
                        ).fetchone()
                        if (
                            stored is None
                            or stored["byte_size"] != member["byte_size"]
                            or stored["media_type"] != member["media_type"]
                            or bytes(stored["payload"]) != item.payload
                        ):
                            raise OperatorPersistenceConflict("artifact identity conflicts")
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.artifact_sets(
                            artifact_set_id, kind, schema_version, canonical_manifest
                        ) VALUES (%s, 'OPERATOR_BUNDLE', 1, %s)
                        ON CONFLICT (artifact_set_id) DO NOTHING
                        """,
                        (artifact_set_id, Jsonb(manifest)),
                    )
                    stored_set = connection.execute(
                        f"""
                        SELECT kind, schema_version, canonical_manifest
                        FROM {SCHEMA_NAME}.artifact_sets WHERE artifact_set_id = %s
                        """,
                        (artifact_set_id,),
                    ).fetchone()
                    if (
                        stored_set is None
                        or stored_set["kind"] != "OPERATOR_BUNDLE"
                        or stored_set["schema_version"] != 1
                        or stored_set["canonical_manifest"] != manifest
                    ):
                        raise OperatorPersistenceConflict("artifact set identity conflicts")
                    for ordinal, member in enumerate(manifest["members"]):
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA_NAME}.artifact_set_members(
                                artifact_set_id, logical_name, artifact_sha256, ordinal
                            ) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (artifact_set_id, logical_name) DO NOTHING
                            """,
                            (
                                artifact_set_id,
                                member["logical_name"],
                                member["artifact_sha256"],
                                ordinal,
                            ),
                        )
                    members = connection.execute(
                        f"""
                        SELECT logical_name, artifact_sha256, ordinal
                        FROM {SCHEMA_NAME}.artifact_set_members
                        WHERE artifact_set_id = %s ORDER BY ordinal
                        """,
                        (artifact_set_id,),
                    ).fetchall()
                    expected_members = [
                        {
                            "logical_name": member["logical_name"],
                            "artifact_sha256": member["artifact_sha256"],
                            "ordinal": ordinal,
                        }
                        for ordinal, member in enumerate(manifest["members"])
                    ]
                    normalized_members = [
                        row | {"artifact_sha256": row["artifact_sha256"].strip()}
                        for row in members
                    ]
                    if normalized_members != expected_members:
                        raise OperatorPersistenceConflict("artifact set membership is not closed")
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.operators(
                            operator_id, slot, title_zh, summary_zh, created_at
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (operator_id) DO NOTHING
                        """,
                        (operator_id, slot, title_zh, summary_zh, operator_timestamp),
                    )
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.operator_versions(
                            operator_id, version, action_id, content_digest,
                            parameter_schema, defaults, documentation,
                            validation_evidence, artifact_set_id, status, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PUBLISHED', %s)
                        """,
                        (
                            operator_id,
                            version,
                            action_id,
                            content_digest,
                            Jsonb(parameter_schema),
                            Jsonb(defaults),
                            documentation,
                            Jsonb(validation_evidence),
                            artifact_set_id,
                            timestamp,
                        ),
                    )
                    current = connection.execute(
                        f"""
                        SELECT version FROM {SCHEMA_NAME}.operator_current
                        WHERE operator_id = %s FOR UPDATE
                        """,
                        (operator_id,),
                    ).fetchone()
                    if current is None:
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA_NAME}.operator_current(
                                operator_id, version, content_digest
                            ) VALUES (%s, %s, %s)
                            """,
                            (operator_id, version, content_digest),
                        )
                    elif parse_semantic_version(version) > parse_semantic_version(
                        current["version"]
                    ):
                        connection.execute(
                            f"""
                            UPDATE {SCHEMA_NAME}.operator_current
                            SET version = %s, content_digest = %s
                            WHERE operator_id = %s
                            """,
                            (version, content_digest, operator_id),
                        )
        except OperatorPersistenceConflict:
            raise
        except PersistenceUnavailableError:
            raise
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("PostgreSQL Operator publication failed") from exc
        return {
            "status": "CREATED",
            "operator_id": operator_id,
            "version": version,
            "content_digest": content_digest,
        }

    def list_operators(self) -> list[dict[str, Any]]:
        try:
            with self.config.connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT o.operator_id, o.slot, o.title_zh, o.summary_zh,
                           c.version AS latest_version, c.content_digest
                    FROM {SCHEMA_NAME}.operators AS o
                    LEFT JOIN {SCHEMA_NAME}.operator_current AS c USING (operator_id)
                    ORDER BY o.slot, o.operator_id
                    """
                ).fetchall()
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("PostgreSQL Operator list failed") from exc
        return [
            row
            | {
                "content_digest": (
                    row["content_digest"].strip() if row["content_digest"] else None
                )
            }
            for row in rows
        ]

    def operator_detail(self, operator_id: str, version: str | None = None) -> dict[str, Any]:
        params: tuple[Any, ...]
        selector_sql: str
        if version is None:
            selector_sql = (
                f"JOIN {SCHEMA_NAME}.operator_current AS c "
                "ON c.operator_id = v.operator_id AND c.version = v.version "
                "WHERE v.operator_id = %s"
            )
            params = (operator_id,)
        else:
            selector_sql = "WHERE v.operator_id = %s AND v.version = %s"
            params = (operator_id, version)
        try:
            with self.config.connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT o.operator_id, o.slot, o.title_zh, o.summary_zh,
                           o.created_at AS operator_created_at,
                           v.version, v.content_digest, v.parameter_schema, v.defaults,
                           v.documentation, v.validation_evidence, v.artifact_set_id,
                           v.status, v.created_at
                    FROM {SCHEMA_NAME}.operator_versions AS v
                    JOIN {SCHEMA_NAME}.operators AS o USING (operator_id)
                    {selector_sql}
                    """,
                    params,
                ).fetchone()
                if row is None:
                    selector = "latest" if version is None else version
                    raise ValueError(f"unknown published operator: {operator_id}@{selector}")
                members = connection.execute(
                    f"""
                    SELECT m.logical_name, a.artifact_sha256, a.byte_size, a.media_type
                    FROM {SCHEMA_NAME}.artifact_set_members AS m
                    JOIN {SCHEMA_NAME}.artifacts AS a USING (artifact_sha256)
                    WHERE m.artifact_set_id = %s ORDER BY m.ordinal
                    """,
                    (row["artifact_set_id"],),
                ).fetchall()
        except ValueError:
            raise
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("PostgreSQL Operator detail failed") from exc
        return {
            "operator_id": row["operator_id"],
            "slot": row["slot"],
            "version": row["version"],
            "content_digest": row["content_digest"].strip(),
            "parameter_schema": row["parameter_schema"],
            "defaults": row["defaults"],
            "title_zh": row["title_zh"],
            "summary_zh": row["summary_zh"],
            "operator_created_at": _time_text(row["operator_created_at"]),
            "documentation": row["documentation"],
            "validation_evidence": row["validation_evidence"],
            "artifact_set_id": row["artifact_set_id"].strip(),
            "artifacts": [
                member | {"artifact_sha256": member["artifact_sha256"].strip()}
                for member in members
            ],
            "status": row["status"],
            "created_at": _time_text(row["created_at"]),
        }

    def list_operator_versions(self, operator_id: str) -> list[dict[str, Any]]:
        try:
            with self.config.connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT version FROM {SCHEMA_NAME}.operator_versions
                    WHERE operator_id = %s AND status = 'PUBLISHED'
                    """,
                    (operator_id,),
                ).fetchall()
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("PostgreSQL Operator versions failed") from exc
        return [
            self.operator_detail(operator_id, version)
            for version in sorted(
                (row["version"] for row in rows),
                key=parse_semantic_version,
                reverse=True,
            )
        ]

    def read_artifact(self, operator_id: str, version: str, logical_name: str) -> tuple[bytes, str, str]:
        try:
            with self.config.connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT a.payload, a.media_type, a.artifact_sha256
                    FROM {SCHEMA_NAME}.operator_versions AS v
                    JOIN {SCHEMA_NAME}.artifact_set_members AS m
                      ON m.artifact_set_id = v.artifact_set_id
                    JOIN {SCHEMA_NAME}.artifacts AS a USING (artifact_sha256)
                    WHERE v.operator_id = %s AND v.version = %s AND m.logical_name = %s
                    """,
                    (operator_id, version, logical_name),
                ).fetchone()
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("PostgreSQL Operator artifact read failed") from exc
        if row is None:
            raise ValueError(
                f"unknown Operator artifact: {operator_id}@{version}/{logical_name}"
            )
        payload = bytes(row["payload"])
        digest = row["artifact_sha256"].strip()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise PersistenceUnavailableError("PostgreSQL Operator artifact hash mismatch")
        return payload, row["media_type"], digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the QuantResearch PostgreSQL schema")
    parser.add_argument("command", choices=("migrate", "admit"))
    args = parser.parse_args()
    if args.command == "migrate":
        runtime_password_file = os.environ.get("QUANT_POSTGRES_PASSWORD_FILE")
        runtime_user = os.environ.get("QUANT_POSTGRES_USER", "qr_runtime")
        if not runtime_password_file:
            raise PersistenceUnavailableError("QUANT_POSTGRES_PASSWORD_FILE is required")
        migrate_schema(
            PostgresConfig.from_environment("QUANT_POSTGRES_MIGRATION_"),
            runtime_user=runtime_user,
            runtime_password_file=Path(runtime_password_file),
        )
    else:
        PostgresOperatorPersistence.from_environment().verify_schema()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
