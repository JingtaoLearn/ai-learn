from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from .postgres_persistence import (
    ArtifactInput,
    PersistenceSchemaError,
    PersistenceUnavailableError,
    PostgresConfig,
    PostgresOperatorPersistence,
    migrate_schema,
)
from .schemas import canonical_json_bytes

FULL_SCHEMA_IDENTITY = "quantresearch-postgresql-full-persistence-v4"
MIGRATION_MANIFEST_SCHEMA = "quantresearch-full-migration-manifest/v1"
PARITY_RECEIPT_SCHEMA = "quantresearch-full-migration-parity/v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MAX_BYTEA_ARTIFACT = 16 * 1024 * 1024
RESIDUAL_CLASSES = frozenset({"DATASET_PARQUET", "SOURCE_TREE"})
EPHEMERAL_PREFIXES = (
    "work/",
    "platform/work/",
    "platform/attempt-control/",
    "platform/quarantine/",
)


class FullPersistenceError(RuntimeError):
    """A PostgreSQL-only persistence invariant could not be satisfied."""


class MigrationRejected(FullPersistenceError):
    """The frozen source cannot be mapped without ambiguity."""


class PersistenceConflict(FullPersistenceError):
    """An immutable identity already exists with different content."""


# Existing domain code stores canonical JSON and UTC timestamps as TEXT. Keeping those
# representations in PostgreSQL makes the migration byte-semantic rather than coercive.
CATALOG_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS qr_catalog;

CREATE TABLE IF NOT EXISTS qr.full_schema_identity (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    identity text NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS qr_catalog.schema_migrations (
    version bigint PRIMARY KEY,
    applied_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_catalog.templates (
    name text NOT NULL, version text NOT NULL, slots_json text NOT NULL,
    parameter_schema_json text NOT NULL, defaults_json text NOT NULL,
    content_digest text NOT NULL UNIQUE, created_at text NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS qr_catalog.experiments (
    experiment_id text PRIMARY KEY, identity_json text NOT NULL, created_at text NOT NULL,
    canonical_attempt_id text, canonical_result_digest text
);
CREATE TABLE IF NOT EXISTS qr_catalog.attempts (
    attempt_id text PRIMARY KEY, experiment_id text NOT NULL,
    action_id text NOT NULL UNIQUE, sequence bigint NOT NULL, status text NOT NULL,
    requested_json text NOT NULL, resolved_json text NOT NULL, created_at text NOT NULL,
    started_at text, finished_at text, logs text, result_path text, result_digest text,
    comparison text, launch_count bigint NOT NULL DEFAULT 0, control_path text,
    control_json text, quarantine_path text, recovery_of_attempt_id text,
    UNIQUE (experiment_id, sequence)
);
CREATE TABLE IF NOT EXISTS qr_catalog.replay_tokens (
    token_hash text PRIMARY KEY, expires_at bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_catalog.dataset_catalog (
    dataset_id text PRIMARY KEY, name text NOT NULL, instrument text NOT NULL UNIQUE,
    provider text NOT NULL, market text NOT NULL, currency text NOT NULL,
    adjustment text NOT NULL, calendar text NOT NULL, default_start text NOT NULL,
    created_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_studies (
    study_id text PRIMARY KEY CHECK (study_id ~ '^[0-9a-f]{64}$'),
    preview_digest text NOT NULL UNIQUE CHECK (preview_digest ~ '^[0-9a-f]{64}$'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    frozen_plan_json text NOT NULL CHECK (jsonb_typeof(frozen_plan_json::jsonb) = 'object'),
    operational_metadata_json text NOT NULL CHECK (
        jsonb_typeof(operational_metadata_json::jsonb) = 'object'
    ),
    phase text NOT NULL CHECK (phase IN (
        'FROZEN', 'VALIDATING_SELECTION_PROCESS', 'SELECTING_FINAL_CANDIDATE',
        'HOLDOUT_READY', 'HOLDOUT_RUNNING', 'COMPLETED'
    )),
    control_status text NOT NULL CHECK (
        control_status IN ('ACTIVE', 'PAUSED', 'CANCELLED', 'FAILED')
    ),
    selection_outcome text NOT NULL CHECK (
        selection_outcome IN ('NOT_DETERMINED', 'CHAMPION_SELECTED', 'NO_ELIGIBLE_CANDIDATE')
    ),
    holdout_outcome text NOT NULL CHECK (holdout_outcome IN ('NOT_RUN', 'PASSED', 'FAILED')),
    created_at text NOT NULL, updated_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_events (
    study_id text NOT NULL REFERENCES qr_catalog.parameter_studies(study_id),
    sequence bigint NOT NULL CHECK (sequence > 0), event_type text NOT NULL,
    occurred_at text NOT NULL, payload_json text NOT NULL,
    PRIMARY KEY (study_id, sequence)
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_actions (
    action_id text PRIMARY KEY CHECK (
        action_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    ),
    operation text NOT NULL CHECK (operation IN (
        'SUBMIT', 'CONTROL_PAUSE', 'CONTROL_RESUME', 'CONTROL_CANCEL',
        'COORDINATOR_LEASE', 'EXECUTION_IDENTITY_DRIFT',
        'EFFECT_INTENT', 'EFFECT_DISPATCH_AUTHORIZATION', 'EFFECT_RECEIPT'
    )),
    study_id text NOT NULL CHECK (study_id ~ '^[0-9a-f]{64}$'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    response_json text NOT NULL CHECK (jsonb_typeof(response_json::jsonb) = 'object'),
    created_at text NOT NULL,
    FOREIGN KEY (study_id) REFERENCES qr_catalog.parameter_studies(study_id),
    CHECK (
        (operation IN ('SUBMIT', 'CONTROL_PAUSE', 'CONTROL_RESUME', 'CONTROL_CANCEL')
            AND action_id !~ '^study-internal:')
        OR (operation = 'COORDINATOR_LEASE'
            AND action_id ~ ('^study-internal:lease:' || study_id || ':[1-9][0-9]*$'))
        OR (operation = 'EXECUTION_IDENTITY_DRIFT'
            AND action_id = 'study-internal:drift:' || study_id)
        OR (operation = 'EFFECT_INTENT'
            AND action_id ~ '^study-internal:effect:[0-9a-f]{64}$')
        OR (operation = 'EFFECT_DISPATCH_AUTHORIZATION'
            AND action_id ~ '^study-internal:dispatch:[0-9a-f]{64}:[1-9][0-9]*$')
        OR (operation = 'EFFECT_RECEIPT'
            AND action_id ~ '^study-internal:receipt:[0-9a-f]{64}$')
    )
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_holdout_history_metadata (
    singleton bigint PRIMARY KEY CHECK (singleton = 1),
    pre_ledger_history_complete bigint NOT NULL CHECK (
        pre_ledger_history_complete IN (0, 1)
    ),
    pre_ledger_experiment_count bigint NOT NULL CHECK (pre_ledger_experiment_count >= 0),
    assessed_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_holdout_ledger (
    study_id text NOT NULL REFERENCES qr_catalog.parameter_studies(study_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    holdout_identity_digest text NOT NULL CHECK (
        holdout_identity_digest ~ '^[0-9a-f]{64}$'
    ),
    event_type text NOT NULL CHECK (event_type IN ('GRANTED', 'ACCESSED', 'EXPOSURE_RECORDED')),
    occurred_at text NOT NULL, payload_json text NOT NULL,
    PRIMARY KEY (study_id, sequence)
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_evidence (
    study_id text NOT NULL REFERENCES qr_catalog.parameter_studies(study_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    evidence_type text NOT NULL CHECK (evidence_type IN (
        'METRIC_DOCUMENT_VERIFIED', 'CANDIDATE_EVALUATED',
        'OUTER_SELECTION_RECORDED', 'CHAMPION_FROZEN',
        'HOLDOUT_OUTCOME_RECORDED', 'EVIDENCE_CONTESTED'
    )),
    candidate_digest text CHECK (
        candidate_digest IS NULL OR candidate_digest ~ '^[0-9a-f]{64}$'
    ),
    payload_json text NOT NULL CHECK (jsonb_typeof(payload_json::jsonb) = 'object'),
    occurred_at text NOT NULL,
    PRIMARY KEY (study_id, sequence)
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_trials (
    study_id text NOT NULL REFERENCES qr_catalog.parameter_studies(study_id),
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    configuration_json text NOT NULL CHECK (
        jsonb_typeof(configuration_json::jsonb) = 'object'
    ),
    first_search_round text NOT NULL,
    proposal_sequence bigint NOT NULL CHECK (proposal_sequence >= 0),
    classification text NOT NULL CHECK (classification IN ('IN_RANGE', 'BASELINE_ONLY')),
    created_at text NOT NULL, PRIMARY KEY (study_id, candidate_digest)
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_bindings (
    binding_id text PRIMARY KEY CHECK (binding_id ~ '^[0-9a-f]{64}$'),
    study_id text NOT NULL REFERENCES qr_catalog.parameter_studies(study_id),
    search_round text NOT NULL,
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    role text NOT NULL CHECK (role IN ('INNER_SCORE', 'OUTER_AUDIT', 'TERMINAL_HOLDOUT')),
    fold_sequence bigint NOT NULL CHECK (fold_sequence >= 1),
    fold_window_json text NOT NULL CHECK (jsonb_typeof(fold_window_json::jsonb) = 'object'),
    task_json text NOT NULL CHECK (jsonb_typeof(task_json::jsonb) = 'object'),
    task_digest text NOT NULL CHECK (task_digest ~ '^[0-9a-f]{64}$'),
    dataset_snapshot_id text NOT NULL CHECK (dataset_snapshot_id ~ '^[0-9a-f]{64}$'),
    experiment_id text NOT NULL REFERENCES qr_catalog.experiments(experiment_id),
    submitted_attempt_id text NOT NULL REFERENCES qr_catalog.attempts(attempt_id),
    attempt_id text NOT NULL REFERENCES qr_catalog.attempts(attempt_id),
    state text NOT NULL CHECK (state IN ('SUBMITTED', 'VERIFIED', 'FAILED', 'CONTESTED')),
    metric_document_json text CHECK (
        metric_document_json IS NULL OR jsonb_typeof(metric_document_json::jsonb) = 'object'
    ),
    created_at text NOT NULL, updated_at text NOT NULL,
    FOREIGN KEY (study_id, candidate_digest)
        REFERENCES qr_catalog.parameter_study_trials(study_id, candidate_digest),
    UNIQUE (study_id, search_round, candidate_digest, role, fold_sequence)
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_attempt_candidate_claims (
    attempt_id text PRIMARY KEY REFERENCES qr_catalog.attempts(attempt_id),
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    configuration_json text NOT NULL CHECK (
        jsonb_typeof(configuration_json::jsonb) = 'object'
    ),
    claimed_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_holdout_claims (
    study_id text PRIMARY KEY REFERENCES qr_catalog.parameter_studies(study_id),
    holdout_identity_digest text NOT NULL CHECK (
        holdout_identity_digest ~ '^[0-9a-f]{64}$'
    ),
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    binding_id text NOT NULL UNIQUE CHECK (binding_id ~ '^[0-9a-f]{64}$'),
    effect_action_id text NOT NULL UNIQUE CHECK (
        effect_action_id ~ '^study-internal:effect:[0-9a-f]{64}$'
    ),
    claimed_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_catalog.parameter_study_suggestion_journal (
    study_id text NOT NULL REFERENCES qr_catalog.parameter_studies(study_id),
    search_round text NOT NULL CHECK (length(search_round) BETWEEN 1 AND 128),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL CHECK (event_type IN (
        'SUGGESTION_RECORDED', 'DUPLICATE_SUGGESTION', 'INNER_EVALUATION_RECORDED'
    )),
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    event_json text NOT NULL CHECK (jsonb_typeof(event_json::jsonb) = 'object'),
    occurred_at text NOT NULL, PRIMARY KEY (study_id, search_round, sequence)
);
CREATE INDEX IF NOT EXISTS catalog_attempts_status_created
    ON qr_catalog.attempts(status, created_at);
CREATE INDEX IF NOT EXISTS catalog_parameter_events_order
    ON qr_catalog.parameter_study_events(study_id, sequence);
CREATE INDEX IF NOT EXISTS catalog_parameter_bindings_progress
    ON qr_catalog.parameter_study_bindings(study_id, search_round, role, state);
CREATE UNIQUE INDEX IF NOT EXISTS catalog_one_champion
    ON qr_catalog.parameter_study_evidence(study_id)
    WHERE evidence_type = 'CHAMPION_FROZEN';
CREATE UNIQUE INDEX IF NOT EXISTS catalog_one_holdout_grant
    ON qr_catalog.parameter_study_holdout_ledger(study_id)
    WHERE event_type = 'GRANTED';
CREATE UNIQUE INDEX IF NOT EXISTS catalog_one_holdout_access
    ON qr_catalog.parameter_study_holdout_ledger(study_id)
    WHERE event_type = 'ACCESSED';
CREATE UNIQUE INDEX IF NOT EXISTS catalog_one_inner_tell
    ON qr_catalog.parameter_study_suggestion_journal(study_id, search_round, candidate_digest)
    WHERE event_type = 'INNER_EVALUATION_RECORDED';

CREATE OR REPLACE VIEW qr_catalog.operators AS
SELECT operator_id, slot, title_zh, summary_zh,
       to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS created_at
FROM qr.operators;
CREATE OR REPLACE VIEW qr_catalog.operator_versions AS
SELECT operator_id, version, content_digest,
       parameter_schema::text AS parameter_schema_json,
       defaults::text AS defaults_json, documentation,
       ''::text AS bundle_path, validation_evidence::text AS validation_evidence_json,
       status, to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS created_at
FROM qr.operator_versions;
CREATE OR REPLACE VIEW qr_catalog.operator_latest AS
SELECT operator_id, version, content_digest FROM qr.operator_current;

CREATE TABLE IF NOT EXISTS qr.production_schema_migrations (
    version bigint PRIMARY KEY, applied_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr.production_requests (
    request_id text PRIMARY KEY, request_digest text NOT NULL,
    request_body_json text NOT NULL, job_id text NOT NULL, scheduled_for text NOT NULL,
    production_manifest_sha256 text NOT NULL, production_release_id text NOT NULL,
    production_run_id text NOT NULL UNIQUE, status text NOT NULL,
    lease_owner text, lease_expires_at text, experiment_id text, attempt_id text,
    result_id text, result_manifest_json text, failure_reason text,
    created_at text NOT NULL, updated_at text NOT NULL,
    UNIQUE(job_id, scheduled_for)
);
CREATE TABLE IF NOT EXISTS qr.validation_invocations (
    validation_id text PRIMARY KEY, request_id text NOT NULL UNIQUE,
    job_id text NOT NULL, validation_for text NOT NULL, created_at text NOT NULL,
    UNIQUE(job_id, validation_for)
);
CREATE INDEX IF NOT EXISTS production_claimable
    ON qr.production_requests(status, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS qr.residual_artifacts (
    artifact_sha256 char(64) PRIMARY KEY CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    media_type text NOT NULL, residual_class text NOT NULL CHECK (
        residual_class IN ('DATASET_PARQUET', 'SOURCE_TREE')
    ),
    residual_key text NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS qr.source_files (
    relative_path text PRIMARY KEY, file_class text NOT NULL,
    mode integer NOT NULL, byte_size bigint NOT NULL,
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    artifact_sha256 char(64), residual_sha256 char(64),
    classification text NOT NULL,
    CHECK ((artifact_sha256 IS NULL) <> (residual_sha256 IS NULL))
);
CREATE TABLE IF NOT EXISTS qr.dataset_snapshots (
    snapshot_id char(64) PRIMARY KEY CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
    instrument text NOT NULL, schema_version bigint NOT NULL,
    canonical_sha256 char(64) NOT NULL, parquet_sha256 char(64) NOT NULL,
    manifest_sha256 char(64) NOT NULL REFERENCES qr.artifacts(artifact_sha256),
    parquet_artifact_sha256 char(64) NOT NULL REFERENCES qr.residual_artifacts(artifact_sha256),
    manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(instrument, snapshot_id)
);
CREATE TABLE IF NOT EXISTS qr.dataset_current (
    instrument text PRIMARY KEY, snapshot_id char(64) NOT NULL REFERENCES qr.dataset_snapshots(snapshot_id),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0)
);
CREATE TABLE IF NOT EXISTS qr.msft_snapshot_payloads (
    snapshot_id char(64) PRIMARY KEY REFERENCES qr.dataset_snapshots(snapshot_id),
    artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id)
);
CREATE TABLE IF NOT EXISTS qr.dataset_ingress_actions (
    idempotency_key text PRIMARY KEY, request_digest char(64) NOT NULL,
    snapshot_id char(64) NOT NULL REFERENCES qr.dataset_snapshots(snapshot_id),
    response jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS qr.dataset_updates (
    update_id text PRIMARY KEY, instrument text NOT NULL, snapshot_id char(64),
    artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    document jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS qr.dataset_lineage_claims (
    instrument text NOT NULL, snapshot_id char(64) NOT NULL,
    artifact_sha256 char(64) NOT NULL REFERENCES qr.artifacts(artifact_sha256),
    document jsonb NOT NULL, PRIMARY KEY (instrument, snapshot_id)
);
CREATE TABLE IF NOT EXISTS qr.attempt_evidence_packages (
    attempt_id text PRIMARY KEY, artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    result_digest char(64), state text NOT NULL,
    canonical boolean NOT NULL DEFAULT false, imported_path text NOT NULL
);
CREATE TABLE IF NOT EXISTS qr.attempt_events (
    attempt_id text NOT NULL, sequence bigint NOT NULL, event_type text NOT NULL,
    payload jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (attempt_id, sequence)
);
CREATE TABLE IF NOT EXISTS qr.report_artifacts (
    report_artifact_id char(64) PRIMARY KEY,
    attempt_id text NOT NULL, artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS qr.report_pointer_events (
    attempt_id text NOT NULL, sequence bigint NOT NULL,
    report_artifact_id char(64) NOT NULL REFERENCES qr.report_artifacts(report_artifact_id),
    pointer jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (attempt_id, sequence)
);
CREATE TABLE IF NOT EXISTS qr.report_current (
    attempt_id text PRIMARY KEY, report_artifact_id char(64) NOT NULL REFERENCES qr.report_artifacts(report_artifact_id),
    sequence bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS qr.study_report_artifacts (
    report_artifact_id char(64) PRIMARY KEY,
    study_id char(64) NOT NULL,
    artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(study_id, report_artifact_id)
);
CREATE TABLE IF NOT EXISTS qr.study_report_pointer_events (
    study_id char(64) NOT NULL, sequence bigint NOT NULL CHECK (sequence > 0),
    report_artifact_id char(64) NOT NULL REFERENCES qr.study_report_artifacts(report_artifact_id),
    pointer jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(study_id, sequence)
);
CREATE TABLE IF NOT EXISTS qr.study_report_current (
    study_id char(64) PRIMARY KEY,
    report_artifact_id char(64) NOT NULL REFERENCES qr.study_report_artifacts(report_artifact_id),
    sequence bigint NOT NULL CHECK (sequence > 0)
);
CREATE TABLE IF NOT EXISTS qr.production_results (
    result_id char(64) PRIMARY KEY, request_id text,
    artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS qr.formal_calibration_claims (
    claim_id text PRIMARY KEY, artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    document jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS qr.accounting_outcome_references (
    result_digest char(64) PRIMARY KEY, artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    document jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS qr.accepted_evidence_packages (
    evidence_id char(64) PRIMARY KEY, evidence_class text NOT NULL,
    artifact_set_id char(64) NOT NULL REFERENCES qr.artifact_sets(artifact_set_id),
    source_path text NOT NULL UNIQUE
);

CREATE OR REPLACE FUNCTION qr.reject_member_after_publication()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM qr.operator_versions WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.attempt_evidence_packages WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.report_artifacts WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.study_report_artifacts WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.msft_snapshot_payloads WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.production_results WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.dataset_updates WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.formal_calibration_claims WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.accounting_outcome_references WHERE artifact_set_id = NEW.artifact_set_id)
       OR EXISTS (SELECT 1 FROM qr.accepted_evidence_packages WHERE artifact_set_id = NEW.artifact_set_id)
    THEN
        RAISE EXCEPTION 'published artifact set membership is closed' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION qr.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is immutable', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$;
CREATE OR REPLACE FUNCTION qr.reject_terminal_production_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('SUCCEEDED', 'FAILED') THEN
        RAISE EXCEPTION 'terminal production row is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END; $$;
"""

IMMUTABLE_TABLES = (
    ("qr", "schema_identity"),
    ("qr", "full_schema_identity"),
    ("qr", "artifacts"),
    ("qr", "artifact_sets"),
    ("qr", "artifact_set_members"),
    ("qr", "operators"),
    ("qr", "operator_versions"),
    ("qr", "production_schema_migrations"),
    ("qr", "validation_invocations"),
    ("qr_catalog", "schema_migrations"),
    ("qr_catalog", "templates"),
    ("qr_catalog", "dataset_catalog"),
    ("qr_catalog", "parameter_study_events"),
    ("qr_catalog", "parameter_study_actions"),
    ("qr_catalog", "parameter_study_holdout_history_metadata"),
    ("qr_catalog", "parameter_study_holdout_ledger"),
    ("qr_catalog", "parameter_study_evidence"),
    ("qr_catalog", "parameter_study_trials"),
    ("qr_catalog", "parameter_study_attempt_candidate_claims"),
    ("qr_catalog", "parameter_study_holdout_claims"),
    ("qr_catalog", "parameter_study_suggestion_journal"),
    ("qr", "residual_artifacts"),
    ("qr", "source_files"),
    ("qr", "dataset_snapshots"),
    ("qr", "msft_snapshot_payloads"),
    ("qr", "dataset_ingress_actions"),
    ("qr", "dataset_updates"),
    ("qr", "dataset_lineage_claims"),
    ("qr", "attempt_evidence_packages"),
    ("qr", "attempt_events"),
    ("qr", "report_artifacts"),
    ("qr", "report_pointer_events"),
    ("qr", "study_report_artifacts"),
    ("qr", "study_report_pointer_events"),
    ("qr", "production_results"),
    ("qr", "formal_calibration_claims"),
    ("qr", "accounting_outcome_references"),
    ("qr", "accepted_evidence_packages"),
)

CATALOG_TABLES = (
    "schema_migrations",
    "templates",
    "experiments",
    "attempts",
    "replay_tokens",
    "dataset_catalog",
    "parameter_studies",
    "parameter_study_events",
    "parameter_study_actions",
    "parameter_study_holdout_history_metadata",
    "parameter_study_holdout_ledger",
    "parameter_study_evidence",
    "parameter_study_trials",
    "parameter_study_bindings",
    "parameter_study_attempt_candidate_claims",
    "parameter_study_holdout_claims",
    "parameter_study_suggestion_journal",
)
PRODUCTION_TABLES = ("schema_migrations", "production_requests", "validation_invocations")


@dataclass(frozen=True)
class FrozenFile:
    relative_path: str
    mode: int
    byte_size: int
    sha256: str
    file_class: str
    classification: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _strict_json(payload: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise MigrationRejected(f"duplicate {label} field: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationRejected(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise MigrationRejected(f"{label} must be an object")
    return value


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationRejected(f"unsafe source path: {value!r}")
    return path.as_posix()


def _read_regular(path: Path, *, maximum: int | None = None) -> tuple[bytes, os.stat_result]:
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise MigrationRejected(f"source member is not a regular single-link file: {path}")
    if maximum is not None and before.st_size > maximum:
        raise MigrationRejected(f"source member exceeds bytea limit: {path}")
    payload = path.read_bytes()
    after = os.stat(path, follow_symlinks=False)
    def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
        return (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(after) or len(payload) != after.st_size:
        raise MigrationRejected(f"source member changed while reading: {path}")
    return payload, after


def _classify(relative: str) -> tuple[str, str]:
    if relative in {"catalog.sqlite3", "production.sqlite3", "platform/catalog.sqlite3"}:
        return "SQLITE_SOURCE", "STRUCTURED_IMPORTED"
    if relative.endswith(("-wal", "-shm")):
        return "SQLITE_SIDECAR", "EPHEMERAL_EXCLUDED"
    if (
        relative.startswith(EPHEMERAL_PREFIXES)
        or relative.endswith(("container.cid", ".lock"))
        or "/.incoming." in relative
        or "/.staging." in relative
    ):
        return "EPHEMERAL_CONTROL", "EPHEMERAL_EXCLUDED"
    if re.fullmatch(r"(?:platform/)?datasets/[^/]+/[0-9a-f]{64}/data\.parquet", relative):
        return "DATASET_PARQUET", "RESIDUAL"
    if relative.startswith(("platform/operators/", "platform/submissions/")):
        return "SOURCE_TREE", "RESIDUAL"
    prefixes = {
        "platform/datasets/": "DATASET_METADATA",
        "platform/updates/": "DATASET_UPDATE",
        "platform/snapshot-lineage/": "DATASET_LINEAGE",
        "platform/experiment-runs/": "ATTEMPT_EVIDENCE",
        "platform/attempt-audit/": "ATTEMPT_AUDIT",
        "platform/attempt-reports/": "ATTEMPT_REPORT",
        "platform/attempt-report-authority/": "REPORT_AUTHORITY",
        "platform/accounting-outcomes/": "ACCOUNTING_EVIDENCE",
        "platform/validation-evidence/": "VALIDATION_EVIDENCE",
        "results/": "PRODUCTION_RESULT",
        "publication/": "REPORT_PROJECTION",
        "focus-calibration/": "FORMAL_CALIBRATION",
    }
    for prefix, file_class in prefixes.items():
        if relative.startswith(prefix):
            return file_class, "BYTEA_IMPORTED"
    raise MigrationRejected(f"unknown source evidence class: {relative}")


def inventory_source(source_root: Path) -> dict[str, Any]:
    source_root = source_root.absolute()
    metadata = os.stat(source_root, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise MigrationRejected("frozen source root is unsafe")
    files: list[dict[str, Any]] = []
    for base, directories, names in os.walk(source_root, followlinks=False):
        base_path = Path(base)
        for name in [*directories, *names]:
            path = base_path / name
            entry = os.stat(path, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                raise MigrationRejected(f"frozen source contains a symlink: {path}")
        for name in sorted(names):
            path = base_path / name
            relative = _safe_relative(path.relative_to(source_root).as_posix())
            payload, entry = _read_regular(path)
            file_class, classification = _classify(relative)
            files.append(
                {
                    "relative_path": relative,
                    "mode": stat.S_IMODE(entry.st_mode),
                    "byte_size": len(payload),
                    "sha256": _sha256_bytes(payload),
                    "file_class": file_class,
                    "classification": classification,
                }
            )
    files.sort(key=lambda item: item["relative_path"])
    return {
        "schema": MIGRATION_MANIFEST_SCHEMA,
        "source_root_class": "NON_AUTHORITATIVE_FROZEN_COPY",
        "files": files,
        "file_count": len(files),
        "byte_count": sum(item["byte_size"] for item in files),
        "inventory_digest": _sha256_bytes(canonical_json_bytes(files)),
    }


def install_full_schema(
    migration_config: PostgresConfig,
    *,
    runtime_user: str,
    runtime_password_file: Path,
) -> None:
    migrate_schema(
        migration_config,
        runtime_user=runtime_user,
        runtime_password_file=runtime_password_file,
    )
    with migration_config.connect() as connection:
        with connection.transaction():
            connection.execute(CATALOG_SCHEMA_SQL)
            inserted = connection.execute(
                """
                INSERT INTO qr.full_schema_identity(singleton, identity)
                VALUES (true, %s) ON CONFLICT (singleton) DO NOTHING
                RETURNING identity
                """,
                (FULL_SCHEMA_IDENTITY,),
            ).fetchone()
            if inserted is None:
                current = connection.execute(
                    "SELECT identity FROM qr.full_schema_identity WHERE singleton"
                ).fetchone()
                if current is not None and current["identity"] == (
                    "quantresearch-postgresql-full-persistence-v3"
                ):
                    connection.execute(
                        'DROP TRIGGER IF EXISTS "full_schema_identity_immutable" '
                        "ON qr.full_schema_identity"
                    )
                    connection.execute(
                        "UPDATE qr.full_schema_identity SET identity=%s WHERE singleton",
                        (FULL_SCHEMA_IDENTITY,),
                    )
                    current = {"identity": FULL_SCHEMA_IDENTITY}
                if current is None or current["identity"] != FULL_SCHEMA_IDENTITY:
                    raise PersistenceSchemaError("full PostgreSQL schema identity conflicts")
            for schema, table in IMMUTABLE_TABLES:
                trigger = f"{table}_immutable"
                connection.execute(
                    f'DROP TRIGGER IF EXISTS "{trigger}" ON "{schema}"."{table}"'
                )
                connection.execute(
                    f'CREATE TRIGGER "{trigger}" BEFORE UPDATE OR DELETE ON '
                    f'"{schema}"."{table}" FOR EACH ROW EXECUTE FUNCTION qr.reject_change()'
                )
            connection.execute(
                'DROP TRIGGER IF EXISTS "replay_tokens_immutable" '
                "ON qr_catalog.replay_tokens"
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS parameter_studies_identity_immutable "
                "ON qr_catalog.parameter_studies"
            )
            connection.execute(
                "CREATE TRIGGER parameter_studies_identity_immutable BEFORE UPDATE OF "
                "study_id, preview_digest, request_digest, frozen_plan_json, "
                "operational_metadata_json, created_at ON qr_catalog.parameter_studies "
                "FOR EACH ROW EXECUTE FUNCTION qr.reject_change()"
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS parameter_study_bindings_identity_immutable "
                "ON qr_catalog.parameter_study_bindings"
            )
            connection.execute(
                "CREATE TRIGGER parameter_study_bindings_identity_immutable BEFORE UPDATE OF "
                "binding_id, study_id, search_round, candidate_digest, role, fold_sequence, "
                "fold_window_json, task_json, task_digest, dataset_snapshot_id, experiment_id, "
                "submitted_attempt_id, created_at ON qr_catalog.parameter_study_bindings "
                "FOR EACH ROW EXECUTE FUNCTION qr.reject_change()"
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS production_terminal_immutable ON qr.production_requests"
            )
            connection.execute(
                "CREATE TRIGGER production_terminal_immutable BEFORE UPDATE OR DELETE "
                "ON qr.production_requests FOR EACH ROW "
                "EXECUTE FUNCTION qr.reject_terminal_production_change()"
            )
            connection.execute("REVOKE ALL ON SCHEMA qr_catalog FROM PUBLIC")
            connection.execute("REVOKE ALL ON SCHEMA qr FROM PUBLIC")
            for schema in ("qr", "qr_catalog"):
                connection.execute(
                    f'GRANT USAGE ON SCHEMA "{schema}" TO "{runtime_user}"'
                )
                connection.execute(
                    f'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA "{schema}" '
                    f'TO "{runtime_user}"'
                )
                connection.execute(
                    f'REVOKE DELETE ON ALL TABLES IN SCHEMA "{schema}" FROM "{runtime_user}"'
                )
            for schema, table in IMMUTABLE_TABLES:
                connection.execute(
                    f'REVOKE UPDATE, DELETE ON "{schema}"."{table}" FROM "{runtime_user}"'
                )
            connection.execute(
                f'REVOKE UPDATE ON qr_catalog.replay_tokens FROM "{runtime_user}"'
            )
            connection.execute(
                f'GRANT DELETE ON qr_catalog.replay_tokens TO "{runtime_user}"'
            )
            for view in ("operators", "operator_versions", "operator_latest"):
                connection.execute(
                    f'REVOKE INSERT, UPDATE, DELETE ON qr_catalog."{view}" FROM "{runtime_user}"'
                )


class HybridRow(dict[str, Any]):
    """Mapping row retaining sqlite3.Row-compatible positional reads."""

    def __init__(self, value: Mapping[str, Any]):
        super().__init__(value)
        self._values = tuple(value.values())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self):
        return iter(self._values)


class CompatCursor:
    def __init__(self, cursor: psycopg.Cursor[Any]):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> HybridRow | None:
        row = self._cursor.fetchone()
        return None if row is None else HybridRow(row)

    def fetchall(self) -> list[HybridRow]:
        return [HybridRow(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield HybridRow(row)


def _postgres_sql(statement: str) -> str:
    value = statement.strip()
    if value == "BEGIN IMMEDIATE":
        return "BEGIN"
    if value.startswith("PRAGMA "):
        raise FullPersistenceError("SQLite PRAGMA is unavailable in PostgreSQL runtime")
    value = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", value, flags=re.I)
    if "INSERT OR IGNORE" in statement.upper() and "ON CONFLICT" not in value.upper():
        value = value.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    value = value.replace("?", "%s")
    value = value.replace(
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
    )
    value = value.replace(
        "json_extract(payload_json, '$.experiment_id')",
        "(payload_json::jsonb ->> 'experiment_id')",
    )
    value = value.replace("ORDER BY created_at DESC, rowid DESC", "ORDER BY created_at DESC, study_id DESC")
    return value


class PostgresCompatConnection:
    """Small DB-API adapter used by existing domain modules during direct migration."""

    def __init__(self, config: PostgresConfig, *, search_path: str):
        self._connection = config.connect()
        self._connection.execute(f"SET search_path TO {search_path}")
        self._connection.commit()

    def execute(self, statement: str, parameters: Sequence[Any] | None = None) -> CompatCursor:
        try:
            cursor = self._connection.execute(_postgres_sql(statement), parameters or ())
            return CompatCursor(cursor)
        except psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("PostgreSQL domain query failed") from exc

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> PostgresCompatConnection:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


class FullPostgresPersistence(PostgresOperatorPersistence):
    """Deep PostgreSQL interface for migrated structured state and immutable evidence."""

    @classmethod
    def from_environment(cls) -> FullPostgresPersistence:
        return cls(PostgresConfig.from_environment())

    def verify_schema(self) -> None:
        super().verify_schema()
        try:
            with self.config.connect() as connection:
                row = connection.execute(
                    "SELECT identity FROM qr.full_schema_identity WHERE singleton"
                ).fetchone()
        except (psycopg.Error, PersistenceUnavailableError) as exc:
            raise PersistenceSchemaError("full PostgreSQL schema is unavailable") from exc
        if row is None or row["identity"] != FULL_SCHEMA_IDENTITY:
            raise PersistenceSchemaError("full PostgreSQL schema identity is unexpected")

    def catalog_connection(self) -> PostgresCompatConnection:
        return PostgresCompatConnection(self.config, search_path="qr_catalog, qr")

    def production_connection(self) -> PostgresCompatConnection:
        return PostgresCompatConnection(self.config, search_path="qr")

    @staticmethod
    def _publish_artifact_set_in_transaction(
        connection: psycopg.Connection[Any] | PostgresCompatConnection,
        *,
        kind: str,
        members: Mapping[str, tuple[str, bytes]],
    ) -> str:
        if not members:
            raise ValueError("artifact set cannot be empty")
        ordered: list[dict[str, Any]] = []
        for name in sorted(members):
            media_type, payload = members[name]
            ArtifactInput(name, media_type, payload).validated()
            ordered.append(
                {
                    "logical_name": name,
                    "artifact_sha256": _sha256_bytes(payload),
                    "byte_size": len(payload),
                    "media_type": media_type,
                }
            )
        manifest = {"schema_version": 1, "kind": kind, "members": ordered}
        artifact_set_id = _sha256_bytes(canonical_json_bytes(manifest))
        for ordinal, member in enumerate(ordered):
            payload = members[member["logical_name"]][1]
            connection.execute(
                "INSERT INTO qr.artifacts(artifact_sha256, byte_size, media_type, payload) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (artifact_sha256) DO NOTHING",
                (member["artifact_sha256"], len(payload), member["media_type"], payload),
            )
            stored = connection.execute(
                "SELECT byte_size, media_type, payload FROM qr.artifacts WHERE artifact_sha256=%s",
                (member["artifact_sha256"],),
            ).fetchone()
            if stored is None or bytes(stored["payload"]) != payload:
                raise PersistenceConflict("artifact digest collision or byte mismatch")
        connection.execute(
            "INSERT INTO qr.artifact_sets(artifact_set_id, kind, schema_version, canonical_manifest) "
            "VALUES (%s, %s, 1, %s) ON CONFLICT (artifact_set_id) DO NOTHING",
            (artifact_set_id, kind, Jsonb(manifest)),
        )
        for ordinal, member in enumerate(ordered):
            connection.execute(
                "INSERT INTO qr.artifact_set_members(artifact_set_id, logical_name, artifact_sha256, ordinal) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (artifact_set_id, logical_name) DO NOTHING",
                (artifact_set_id, member["logical_name"], member["artifact_sha256"], ordinal),
            )
        actual = connection.execute(
            "SELECT logical_name, artifact_sha256 FROM qr.artifact_set_members "
            "WHERE artifact_set_id=%s ORDER BY ordinal",
            (artifact_set_id,),
        ).fetchall()
        expected = [(item["logical_name"], item["artifact_sha256"]) for item in ordered]
        if [(row["logical_name"], row["artifact_sha256"].strip()) for row in actual] != expected:
            raise PersistenceConflict("artifact set membership conflicts")
        return artifact_set_id

    def publish_artifact_set(
        self,
        *,
        kind: str,
        members: Mapping[str, tuple[str, bytes]],
    ) -> str:
        with self.config.connect() as connection:
            with connection.transaction():
                return self._publish_artifact_set_in_transaction(
                    connection, kind=kind, members=members
                )

    def read_artifact_set(self, artifact_set_id: str) -> dict[str, bytes]:
        if SHA256.fullmatch(artifact_set_id) is None:
            raise ValueError("artifact_set_id must be lowercase SHA-256")
        with self.config.connect() as connection:
            rows = connection.execute(
                "SELECT m.logical_name, a.artifact_sha256, a.byte_size, a.payload "
                "FROM qr.artifact_set_members m JOIN qr.artifacts a USING (artifact_sha256) "
                "WHERE m.artifact_set_id=%s ORDER BY m.ordinal",
                (artifact_set_id,),
            ).fetchall()
        if not rows:
            raise ValueError("unknown artifact set")
        result: dict[str, bytes] = {}
        for row in rows:
            payload = bytes(row["payload"])
            if len(payload) != row["byte_size"] or _sha256_bytes(payload) != row["artifact_sha256"].strip():
                raise PersistenceUnavailableError("stored artifact verification failed")
            result[row["logical_name"]] = payload
        return result

    def dataset_ingress_receipt(
        self, idempotency_key: str, request_digest: str
    ) -> dict[str, Any] | None:
        if not idempotency_key or len(idempotency_key) > 128 or SHA256.fullmatch(request_digest) is None:
            raise ValueError("dataset ingress identity is invalid")
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT request_digest, response FROM qr.dataset_ingress_actions "
                "WHERE idempotency_key=%s",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_digest"].strip() != request_digest:
            raise PersistenceConflict("dataset ingress idempotency key conflicts")
        return dict(row["response"])

    def publish_msft_snapshot(
        self,
        *,
        snapshot: Mapping[str, Any],
        idempotency_key: str,
        request_digest: str,
        expected_generation: int,
    ) -> dict[str, Any]:
        """Publish one explicit XNYS/MSFT Snapshot and generation-checked pointer."""

        from .msft_trend_study import REQUIRED_RECORD_FIELDS, validate_snapshot

        frozen = validate_snapshot(snapshot)
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or SHA256.fullmatch(request_digest) is None
            or type(expected_generation) is not int
            or expected_generation < 0
        ):
            raise ValueError("dataset ingress identity or generation is invalid")
        existing = self.dataset_ingress_receipt(idempotency_key, request_digest)
        if existing is not None:
            return existing
        frame = pd.DataFrame(frozen["records"])[list(REQUIRED_RECORD_FIELDS)]
        parquet_buffer = io.BytesIO()
        frame.to_parquet(parquet_buffer, index=False)
        parquet = parquet_buffer.getvalue()
        parquet_sha256, residual_key = self._runtime_residual(parquet)
        snapshot_payload = canonical_json_bytes(frozen) + b"\n"
        manifest = {
            key: value for key, value in frozen.items() if key != "records"
        } | {
            "schema_version": 6,
            "columns": list(REQUIRED_RECORD_FIELDS),
            "parquet_sha256": parquet_sha256,
        }
        manifest_payload = canonical_json_bytes(manifest) + b"\n"
        lineage = {
            "schema_version": 1,
            "kind": "authenticated_provider_ingress",
            "instrument": "MSFT",
            "snapshot_id": frozen["snapshot_id"],
            "source_identity_sha256": frozen["source_identity_sha256"],
            "request_digest": request_digest,
        }
        lineage_payload = canonical_json_bytes(lineage) + b"\n"
        try:
            with self.config.connect() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"dataset-ingress:{idempotency_key}",),
                    )
                    action = connection.execute(
                        "SELECT request_digest, response FROM qr.dataset_ingress_actions "
                        "WHERE idempotency_key=%s",
                        (idempotency_key,),
                    ).fetchone()
                    if action is not None:
                        if action["request_digest"].strip() != request_digest:
                            raise PersistenceConflict("dataset ingress idempotency key conflicts")
                        return dict(action["response"])
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('MSFT', 0))"
                    )
                    current = connection.execute(
                        "SELECT snapshot_id, generation FROM qr.dataset_current WHERE instrument='MSFT'"
                    ).fetchone()
                    generation = 0 if current is None else int(current["generation"])
                    if generation != expected_generation:
                        raise PersistenceConflict("dataset current generation changed")
                    artifact_set_id = self._publish_artifact_set_in_transaction(
                        connection,
                        kind="XNYS_MSFT_SNAPSHOT",
                        members={
                            "manifest.json": ("application/json", manifest_payload),
                            "snapshot.json": ("application/json", snapshot_payload),
                            "lineage.json": ("application/json", lineage_payload),
                        },
                    )
                    connection.execute(
                        "INSERT INTO qr.residual_artifacts(artifact_sha256, byte_size, media_type, "
                        "residual_class, residual_key) VALUES (%s,%s,'application/x-parquet',"
                        "'DATASET_PARQUET',%s) ON CONFLICT (artifact_sha256) DO NOTHING",
                        (parquet_sha256, len(parquet), residual_key),
                    )
                    connection.execute(
                        "INSERT INTO qr_catalog.dataset_catalog(dataset_id,name,instrument,provider,"
                        "market,currency,adjustment,calendar,default_start,created_at) VALUES "
                        "('MSFT-XNYS-TOTAL-RETURN','Microsoft (MSFT)','MSFT','yahoo-chart-api',"
                        "'XNYS','USD','split-adjusted-dividend-unadjusted','XNYS',%s,%s) "
                        "ON CONFLICT (dataset_id) DO NOTHING",
                        (frozen["data_start"], frozen["sealed_at"]),
                    )
                    connection.execute(
                        "INSERT INTO qr.dataset_snapshots(snapshot_id,instrument,schema_version,"
                        "canonical_sha256,parquet_sha256,manifest_sha256,parquet_artifact_sha256,manifest) "
                        "VALUES (%s,'MSFT',6,%s,%s,%s,%s,%s) ON CONFLICT (snapshot_id) DO NOTHING",
                        (
                            frozen["snapshot_id"],
                            frozen["records_sha256"],
                            parquet_sha256,
                            _sha256_bytes(manifest_payload),
                            parquet_sha256,
                            Jsonb(manifest),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO qr.msft_snapshot_payloads(snapshot_id,artifact_set_id) "
                        "VALUES (%s,%s) ON CONFLICT (snapshot_id) DO NOTHING",
                        (frozen["snapshot_id"], artifact_set_id),
                    )
                    lineage_sha = _sha256_bytes(lineage_payload)
                    connection.execute(
                        "INSERT INTO qr.dataset_lineage_claims(instrument,snapshot_id,artifact_sha256,document) "
                        "VALUES ('MSFT',%s,%s,%s) ON CONFLICT (instrument,snapshot_id) DO NOTHING",
                        (frozen["snapshot_id"], lineage_sha, Jsonb(lineage)),
                    )
                    update_id = hashlib.sha256(
                        b"quantresearch-msft-ingress/v1\0" + request_digest.encode("ascii")
                    ).hexdigest()
                    connection.execute(
                        "INSERT INTO qr.dataset_updates(update_id,instrument,snapshot_id,artifact_set_id,document) "
                        "VALUES (%s,'MSFT',%s,%s,%s) ON CONFLICT (update_id) DO NOTHING",
                        (update_id, frozen["snapshot_id"], artifact_set_id, Jsonb(lineage)),
                    )
                    new_generation = generation + 1
                    connection.execute(
                        "INSERT INTO qr.dataset_current(instrument,snapshot_id,generation) "
                        "VALUES ('MSFT',%s,%s) ON CONFLICT (instrument) DO UPDATE SET "
                        "snapshot_id=EXCLUDED.snapshot_id,generation=EXCLUDED.generation",
                        (frozen["snapshot_id"], new_generation),
                    )
                    response = {
                        "dataset_id": "MSFT-XNYS-TOTAL-RETURN",
                        "snapshot_id": frozen["snapshot_id"],
                        "schema_version": 6,
                        "record_count": frozen["record_count"],
                        "data_start": frozen["data_start"],
                        "data_end": frozen["data_end"],
                        "required_fields": frozen["required_fields"],
                        "null_counts": frozen["null_counts"],
                        "source_identity_sha256": frozen["source_identity_sha256"],
                        "records_sha256": frozen["records_sha256"],
                        "parquet_sha256": parquet_sha256,
                        "manifest_sha256": _sha256_bytes(manifest_payload),
                        "generation": new_generation,
                    }
                    connection.execute(
                        "INSERT INTO qr.dataset_ingress_actions(idempotency_key,request_digest,"
                        "snapshot_id,response) VALUES (%s,%s,%s,%s)",
                        (idempotency_key, request_digest, frozen["snapshot_id"], Jsonb(response)),
                    )
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("MSFT Snapshot publication failed") from exc
        readback = self.dataset_ingress_receipt(idempotency_key, request_digest)
        if readback is None or readback["snapshot_id"] != frozen["snapshot_id"]:
            raise PersistenceUnavailableError("MSFT Snapshot read-back mismatch")
        return readback

    def msft_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        if SHA256.fullmatch(snapshot_id) is None:
            raise ValueError("snapshot_id must be lowercase SHA-256")
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT artifact_set_id FROM qr.msft_snapshot_payloads WHERE snapshot_id=%s",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown MSFT Snapshot")
        members = self.read_artifact_set(row["artifact_set_id"].strip())
        from .msft_trend_study import validate_snapshot

        snapshot = _strict_json(members["snapshot.json"], "MSFT Snapshot")
        return validate_snapshot(snapshot)

    def publish_msft_study_report(
        self,
        *,
        study_id: str,
        result: Mapping[str, Any],
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        if SHA256.fullmatch(study_id) is None:
            raise ValueError("study_id must be lowercase SHA-256")
        from .msft_trend_study import build_report_pointer, chinese_report

        document, html = chinese_report(result, provenance)
        report_id = document["report_artifact_id"]
        members = {
            "report-document.json": (
                "application/json",
                canonical_json_bytes(document) + b"\n",
            ),
            "report.html": ("text/html", html),
        }
        try:
            with self.config.connect() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"study-report:{study_id}",),
                    )
                    artifact_set_id = self._publish_artifact_set_in_transaction(
                        connection, kind="MSFT_STUDY_REPORT", members=members
                    )
                    connection.execute(
                        "INSERT INTO qr.study_report_artifacts(report_artifact_id,study_id,artifact_set_id) "
                        "VALUES (%s,%s,%s) ON CONFLICT (report_artifact_id) DO NOTHING",
                        (report_id, study_id, artifact_set_id),
                    )
                    current = connection.execute(
                        "SELECT report_artifact_id,sequence FROM qr.study_report_current WHERE study_id=%s",
                        (study_id,),
                    ).fetchone()
                    current_pointer = (
                        None
                        if current is None
                        else {
                            "report_artifact_id": current["report_artifact_id"].strip(),
                            "sequence": int(current["sequence"]),
                        }
                    )
                    pointer = build_report_pointer(study_id, report_id, current_pointer)
                    if pointer is not None:
                        sequence = pointer["sequence"]
                        connection.execute(
                            "INSERT INTO qr.study_report_pointer_events(study_id,sequence,"
                            "report_artifact_id,pointer) VALUES (%s,%s,%s,%s)",
                            (study_id, sequence, report_id, Jsonb(pointer)),
                        )
                        connection.execute(
                            "INSERT INTO qr.study_report_current(study_id,report_artifact_id,sequence) "
                            "VALUES (%s,%s,%s) ON CONFLICT (study_id) DO UPDATE SET "
                            "report_artifact_id=EXCLUDED.report_artifact_id,sequence=EXCLUDED.sequence",
                            (study_id, report_id, sequence),
                        )
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("MSFT Study report publication failed") from exc
        return self.current_msft_study_report(study_id)

    def current_msft_study_report(self, study_id: str) -> dict[str, Any]:
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT c.report_artifact_id,c.sequence,a.artifact_set_id,e.pointer "
                "FROM qr.study_report_current c JOIN qr.study_report_artifacts a "
                "USING (report_artifact_id) JOIN qr.study_report_pointer_events e "
                "ON e.study_id=c.study_id AND e.sequence=c.sequence WHERE c.study_id=%s",
                (study_id,),
            ).fetchone()
        if row is None:
            raise ValueError("MSFT Study report is unavailable")
        members = self.read_artifact_set(row["artifact_set_id"].strip())
        document = _strict_json(members["report-document.json"], "MSFT Study report")
        report_id = row["report_artifact_id"].strip()
        if document.get("report_artifact_id") != report_id or row["pointer"].get(
            "report_artifact_id"
        ) != report_id:
            raise PersistenceUnavailableError("MSFT Study report binding is invalid")
        return {
            "study_id": study_id,
            "report_artifact_id": report_id,
            "sequence": int(row["sequence"]),
            "canonical_url": f"https://quant.ai.jingtao.fun/studies/{study_id}/report",
            "document": document,
            "html": members["report.html"],
        }

    def attempt_report(self, attempt_id: str) -> bytes:
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT r.artifact_set_id FROM qr.report_current c "
                "JOIN qr.report_artifacts r USING (report_artifact_id) WHERE c.attempt_id=%s",
                (attempt_id,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT artifact_set_id FROM qr.attempt_evidence_packages WHERE attempt_id=%s",
                    (attempt_id,),
                ).fetchone()
        if row is None:
            raise ValueError("attempt report is unavailable")
        members = self.read_artifact_set(row["artifact_set_id"].strip())
        try:
            return members["report.html"]
        except KeyError as exc:
            raise PersistenceUnavailableError("attempt evidence has no report.html") from exc

    def current_report_identity(self, attempt_id: str) -> str | None:
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT report_artifact_id FROM qr.report_current WHERE attempt_id=%s",
                (attempt_id,),
            ).fetchone()
        return None if row is None else row["report_artifact_id"].strip()

    def current_report_evidence(self, attempt_id: str) -> dict[str, Any] | None:
        """Read and verify the current immutable report identity and document."""

        if SHA256.fullmatch(attempt_id) is None:
            raise ValueError("attempt_id must be lowercase SHA-256")
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT c.report_artifact_id, c.sequence, e.pointer, r.artifact_set_id "
                "FROM qr.report_current c "
                "JOIN qr.report_pointer_events e "
                "ON e.attempt_id=c.attempt_id AND e.sequence=c.sequence "
                "JOIN qr.report_artifacts r "
                "ON r.report_artifact_id=c.report_artifact_id "
                "WHERE c.attempt_id=%s",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None

        from .attempt_report import (
            _validate_pointer_record,
            validate_report_document,
            validate_report_manifest,
        )

        report_artifact_id = row["report_artifact_id"].strip()
        pointer = _validate_pointer_record(row["pointer"])
        if (
            pointer["attempt_id"] != attempt_id
            or pointer["report_artifact_id"] != report_artifact_id
            or pointer["sequence"] != row["sequence"]
        ):
            raise PersistenceUnavailableError("current report pointer binding is invalid")
        members = self.read_artifact_set(row["artifact_set_id"].strip())
        if set(members) != {
            "report-document.json",
            "report.html",
            "report-manifest.json",
        }:
            raise PersistenceUnavailableError("current report artifact membership is invalid")
        manifest = validate_report_manifest(
            _strict_json(members["report-manifest.json"], "report manifest")
        )
        document = validate_report_document(
            _strict_json(members["report-document.json"], "ReportDocument")
        )
        fields = {
            field["field_id"]: field
            for section in document["sections"]
            for field in section["fields"]
        }
        expected_files = [
            {
                "path": "report-document.json",
                "size": len(members["report-document.json"]),
                "sha256": _sha256_bytes(members["report-document.json"]),
            },
            {
                "path": "report.html",
                "size": len(members["report.html"]),
                "sha256": _sha256_bytes(members["report.html"]),
            },
        ]
        if (
            manifest["attempt_id"] != attempt_id
            or manifest["report_artifact_id"] != report_artifact_id
            or manifest["document_id"] != document["document_id"]
            or manifest["files"] != expected_files
            or pointer["report_manifest_sha256"]
            != _sha256_bytes(canonical_json_bytes(manifest) + b"\n")
            or pointer["report_document_sha256"] != manifest["report_document_sha256"]
            or fields["attempt_id"]["raw"] != attempt_id
            or fields["bundle_id"]["raw"] != manifest["bundle_id"]
        ):
            raise PersistenceUnavailableError("current report artifact binding is invalid")
        return {
            "report_artifact_id": report_artifact_id,
            "document": document,
        }

    def publish_attempt_completion(
        self,
        connection: Any,
        *,
        attempt_id: str,
        result_digest: str,
        publication: Mapping[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        """Publish one completed Attempt package and canonical report in its catalog transaction."""

        if SHA256.fullmatch(attempt_id) is None or SHA256.fullmatch(result_digest) is None:
            raise ValueError("Attempt completion identities must be lowercase SHA-256")
        descriptor = publication.get("descriptor")
        evidence = publication.get("evidence")
        report = publication.get("report")
        if not isinstance(descriptor, Mapping) or not isinstance(evidence, Mapping):
            raise ValueError("Attempt completion evidence is incomplete")
        if descriptor.get("attempt_id") != attempt_id or descriptor.get("core_result_digest") != result_digest:
            raise PersistenceConflict("Attempt evidence identity conflicts with completion")
        evidence_members = {
            **{
                name: (_media_type(name), bytes(payload))
                for name, payload in evidence.items()
            },
            "attempt-audit.json": (
                "application/json",
                canonical_json_bytes(publication["audit"]) + b"\n",
            ),
            "bundle.json": ("application/json", canonical_json_bytes(descriptor) + b"\n"),
        }
        evidence_set_id = self._publish_artifact_set_in_transaction(
            connection, kind="ATTEMPT_EVIDENCE", members=evidence_members
        )
        connection.execute(
            "INSERT INTO qr.attempt_evidence_packages(attempt_id, artifact_set_id, result_digest, "
            "state, canonical, imported_path) VALUES (%s,%s,%s,'SUCCEEDED',true,%s) "
            "ON CONFLICT (attempt_id) DO NOTHING",
            (attempt_id, evidence_set_id, result_digest, f"postgresql:attempt:{attempt_id}"),
        )
        stored_evidence = connection.execute(
            "SELECT artifact_set_id, result_digest, state, canonical "
            "FROM qr.attempt_evidence_packages WHERE attempt_id=%s",
            (attempt_id,),
        ).fetchone()
        if stored_evidence is None or (
            stored_evidence["artifact_set_id"].strip(),
            stored_evidence["result_digest"].strip(),
            stored_evidence["state"],
            stored_evidence["canonical"],
        ) != (evidence_set_id, result_digest, "SUCCEEDED", True):
            raise PersistenceConflict("Attempt evidence publication conflicts")

        result: dict[str, Any] = {"artifact_set_id": evidence_set_id}
        if report is not None:
            if not isinstance(report, Mapping):
                raise ValueError("Attempt report publication is invalid")
            from .attempt_report import build_latest_pointer, validate_report_manifest

            manifest = validate_report_manifest(report["manifest"])
            if manifest["attempt_id"] != attempt_id:
                raise PersistenceConflict("Attempt report identity conflicts with completion")
            report_members = {
                "report-document.json": (
                    "application/json",
                    canonical_json_bytes(report["document"]) + b"\n",
                ),
                "report.html": ("text/html", bytes(report["html"])),
                "report-manifest.json": (
                    "application/json",
                    canonical_json_bytes(manifest) + b"\n",
                ),
            }
            report_set_id = self._publish_artifact_set_in_transaction(
                connection, kind="ATTEMPT_REPORT", members=report_members
            )
            report_id = manifest["report_artifact_id"]
            connection.execute(
                "INSERT INTO qr.report_artifacts(report_artifact_id, attempt_id, artifact_set_id) "
                "VALUES (%s,%s,%s) ON CONFLICT (report_artifact_id) DO NOTHING",
                (report_id, attempt_id, report_set_id),
            )
            stored_report = connection.execute(
                "SELECT attempt_id, artifact_set_id FROM qr.report_artifacts "
                "WHERE report_artifact_id=%s",
                (report_id,),
            ).fetchone()
            if stored_report is None or (
                stored_report["attempt_id"],
                stored_report["artifact_set_id"].strip(),
            ) != (attempt_id, report_set_id):
                raise PersistenceConflict("Attempt report artifact identity conflicts")
            current = connection.execute(
                "SELECT c.report_artifact_id, c.sequence, e.pointer "
                "FROM qr.report_current c JOIN qr.report_pointer_events e "
                "ON e.attempt_id=c.attempt_id AND e.sequence=c.sequence "
                "WHERE c.attempt_id=%s",
                (attempt_id,),
            ).fetchone()
            if current is None or current["report_artifact_id"].strip() != report_id:
                prior = None if current is None else current["pointer"]
                pointer = build_latest_pointer(manifest, prior=prior)
                connection.execute(
                    "INSERT INTO qr.report_pointer_events(attempt_id, sequence, report_artifact_id, pointer) "
                    "VALUES (%s,%s,%s,%s)",
                    (attempt_id, pointer["sequence"], report_id, Jsonb(pointer)),
                )
                connection.execute(
                    "INSERT INTO qr.report_current(attempt_id, report_artifact_id, sequence) "
                    "VALUES (%s,%s,%s) ON CONFLICT (attempt_id) DO UPDATE SET "
                    "report_artifact_id=EXCLUDED.report_artifact_id, sequence=EXCLUDED.sequence",
                    (attempt_id, report_id, pointer["sequence"]),
                )
            result["report_artifact_id"] = report_id
        connection.execute(
            "INSERT INTO qr.attempt_events(attempt_id, sequence, event_type, payload, occurred_at) "
            "VALUES (%s, COALESCE((SELECT max(sequence)+1 FROM qr.attempt_events WHERE attempt_id=%s),1), "
            "'SUCCEEDED', %s, %s::timestamptz)",
            (attempt_id, attempt_id, Jsonb({"status": "SUCCEEDED", "result_digest": result_digest}), occurred_at),
        )
        return result

    def report_artifact(self, attempt_id: str, report_artifact_id: str) -> bytes:
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT artifact_set_id FROM qr.report_artifacts "
                "WHERE attempt_id=%s AND report_artifact_id=%s",
                (attempt_id, report_artifact_id),
            ).fetchone()
        if row is None:
            raise ValueError("unknown report artifact")
        members = self.read_artifact_set(row["artifact_set_id"].strip())
        if "report.html" not in members:
            raise PersistenceUnavailableError("report artifact has no report.html")
        return members["report.html"]

    def production_result(self, result_id: str) -> dict[str, Any]:
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT artifact_set_id, manifest FROM qr.production_results WHERE result_id=%s",
                (result_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown production result")
        members = self.read_artifact_set(row["artifact_set_id"].strip())
        manifest = row["manifest"]
        for name, descriptor in manifest["files"].items():
            payload = members.get(name)
            if payload is None or descriptor != {"sha256": _sha256_bytes(payload), "size": len(payload)}:
                raise PersistenceUnavailableError("production result member verification failed")
        return {"manifest": manifest, "members": members}

    def publish_production_result(
        self, manifest: Mapping[str, Any], payloads: Mapping[str, bytes]
    ) -> dict[str, Any]:
        result_id = manifest.get("result_id")
        if not isinstance(result_id, str) or SHA256.fullmatch(result_id) is None:
            raise ValueError("result_id must be lowercase SHA-256")
        core = {key: value for key, value in manifest.items() if key != "result_id"}
        if _sha256_bytes(canonical_json_bytes(core)) != result_id:
            raise ValueError("production result identity is invalid")
        members = {
            name: (_media_type(name), payload) for name, payload in payloads.items()
        }
        artifact_set_id = self.publish_artifact_set(
            kind="PRODUCTION_RESULT", members=members
        )
        try:
            with self.config.connect() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (result_id,),
                    )
                    existing = connection.execute(
                        "SELECT artifact_set_id, manifest FROM qr.production_results WHERE result_id=%s",
                        (result_id,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            existing["artifact_set_id"].strip() != artifact_set_id
                            or existing["manifest"] != dict(manifest)
                        ):
                            raise PersistenceConflict("production result identity conflicts")
                    else:
                        connection.execute(
                            "INSERT INTO qr.production_results(result_id, request_id, artifact_set_id, manifest) "
                            "VALUES (%s,%s,%s,%s)",
                            (result_id, manifest.get("request_id"), artifact_set_id, Jsonb(dict(manifest))),
                        )
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("production result publication failed") from exc
        return self.production_result(result_id)["manifest"]

    def dataset_current_snapshot(self, instrument: str) -> str | None:
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM qr.dataset_current WHERE instrument=%s",
                (instrument,),
            ).fetchone()
        return None if row is None else row["snapshot_id"].strip()

    def dataset_snapshot_lineage(self, instrument: str, snapshot_id: str) -> dict[str, Any]:
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT c.document, c.artifact_sha256, a.payload "
                "FROM qr.dataset_lineage_claims AS c "
                "JOIN qr.artifacts AS a USING (artifact_sha256) "
                "WHERE c.instrument=%s AND c.snapshot_id=%s",
                (instrument, snapshot_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown dataset lineage: {instrument}@{snapshot_id}")
        artifact_sha256 = row["artifact_sha256"].strip()
        payload = bytes(row["payload"])
        if _sha256_bytes(payload) != artifact_sha256:
            raise PersistenceUnavailableError("stored dataset lineage artifact is invalid")
        payload_document = _strict_json(payload, "dataset lineage")
        document = row["document"]
        if document != payload_document:
            migrated_index = {"member_sha256": {"lineage.json": artifact_sha256}}
            if document != migrated_index:
                raise PersistenceUnavailableError("stored dataset lineage index is invalid")
            document = payload_document
        if (
            not isinstance(document, dict)
            or document.get("instrument") != instrument
            or document.get("snapshot_id") != snapshot_id
            or not isinstance(document.get("lineage"), dict)
        ):
            raise PersistenceUnavailableError("stored dataset lineage is invalid")
        return document["lineage"]

    @contextmanager
    def materialize_dataset_update_state(
        self, instrument: str
    ) -> Iterator[tuple[Path, str | None]]:
        """Provide a transient legacy-shaped workspace, never a runtime authority."""

        current = self.dataset_current_snapshot(instrument)
        with tempfile.TemporaryDirectory(prefix="quant-dataset-update-") as temporary:
            root = Path(temporary)
            if current is not None:
                with self.materialize_dataset_snapshot(instrument, current) as source:
                    target = root / "datasets" / instrument / current
                    target.parent.mkdir(parents=True, mode=0o755)
                    shutil.copytree(source, target)
                pointer = target.parent / "latest.json"
                pointer.write_bytes(
                    canonical_json_bytes({"snapshot_id": current, "path": current}) + b"\n"
                )
                lineage_relative = (
                    f"platform/snapshot-lineage/{instrument}/{current}/lineage.json"
                )
                with self.config.connect() as connection:
                    row = connection.execute(
                        "SELECT a.payload FROM qr.source_files f JOIN qr.artifacts a "
                        "ON a.artifact_sha256=f.artifact_sha256 WHERE f.relative_path=%s",
                        (lineage_relative,),
                    ).fetchone()
                if row is not None:
                    lineage_path = root / "snapshot-lineage" / instrument / current / "lineage.json"
                    lineage_path.parent.mkdir(parents=True, mode=0o755)
                    lineage_path.write_bytes(bytes(row["payload"]))
                    lineage_path.chmod(0o444)
                    lineage_path.parent.chmod(0o555)
            yield root, current

    @staticmethod
    def _runtime_residual(payload: bytes) -> tuple[str, str]:
        residual_root = os.environ.get("QUANT_RESIDUAL_ROOT")
        if not residual_root:
            raise PersistenceUnavailableError("QUANT_RESIDUAL_ROOT is required")
        root = Path(residual_root).absolute()
        try:
            metadata = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise PersistenceUnavailableError("residual root is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise PersistenceUnavailableError("residual root is unsafe")
        digest = _sha256_bytes(payload)
        relative = f"runtime/{digest[:2]}/{digest}"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            stored, _ = _read_regular(target)
            if stored != payload:
                raise PersistenceConflict("runtime residual digest path conflicts")
        else:
            with target.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o444)
        stored, _ = _read_regular(target)
        if stored != payload:
            raise PersistenceUnavailableError("runtime residual read-back mismatch")
        return digest, relative

    @staticmethod
    def _insert_runtime_file(
        connection: Any,
        *,
        relative_path: str,
        file_class: str,
        payload: bytes,
        residual_sha256: str | None = None,
    ) -> str:
        digest = _sha256_bytes(payload)
        artifact_sha256 = None if residual_sha256 is not None else digest
        if artifact_sha256 is not None:
            connection.execute(
                "INSERT INTO qr.artifacts(artifact_sha256, byte_size, media_type, payload) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (artifact_sha256) DO NOTHING",
                (artifact_sha256, len(payload), _media_type(relative_path), payload),
            )
        connection.execute(
            "INSERT INTO qr.source_files(relative_path, file_class, mode, byte_size, sha256, "
            "artifact_sha256, residual_sha256, classification) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (relative_path) DO NOTHING",
            (
                relative_path,
                file_class,
                0o444,
                len(payload),
                digest,
                artifact_sha256,
                residual_sha256,
                "RESIDUAL" if residual_sha256 is not None else "BYTEA_RUNTIME",
            ),
        )
        stored = connection.execute(
            "SELECT file_class, byte_size, sha256, artifact_sha256, residual_sha256, classification "
            "FROM qr.source_files "
            "WHERE relative_path=%s",
            (relative_path,),
        ).fetchone()
        expected = (
            file_class,
            len(payload),
            digest,
            artifact_sha256,
            residual_sha256,
            "RESIDUAL" if residual_sha256 is not None else "BYTEA_RUNTIME",
        )
        actual = None if stored is None else (
            stored["file_class"],
            stored["byte_size"],
            stored["sha256"].strip(),
            None if stored["artifact_sha256"] is None else stored["artifact_sha256"].strip(),
            None if stored["residual_sha256"] is None else stored["residual_sha256"].strip(),
            stored["classification"],
        )
        if actual != expected:
            raise PersistenceConflict("runtime file identity conflicts")
        return digest

    def publish_dataset_update(
        self,
        *,
        instrument: str,
        snapshot_path: Path,
        update_path: Path,
        expected_prior_snapshot_id: str | None,
    ) -> dict[str, Any]:
        """Commit one verified Dataset Snapshot/update/lineage graph to PostgreSQL."""

        from .dataset_lineage import load_update_record, snapshot_update_lineage
        from .datasets import _verify_snapshot

        verified = _verify_snapshot(
            snapshot_path, snapshot_path.name, include_frame=False, require_name=True
        )
        if not isinstance(verified, dict) or verified["metadata"]["instrument"] != instrument:
            raise ValueError("dataset snapshot publication identity is invalid")
        update = load_update_record(update_path.parents[3], instrument, update_path.parent.name)
        if update["result_snapshot_id"] != verified["snapshot_id"]:
            raise ValueError("dataset update does not bind the snapshot")
        lineage = snapshot_update_lineage(
            update_path.parents[3], instrument, verified["snapshot_id"]
        )
        lineage_path = (
            update_path.parents[3]
            / "snapshot-lineage"
            / instrument
            / verified["snapshot_id"]
            / "lineage.json"
        )
        snapshot_members = {
            path.name: _read_regular(path, maximum=MAX_BYTEA_ARTIFACT)[0]
            for path in snapshot_path.iterdir()
            if path.name != "data.parquet"
        }
        parquet, _ = _read_regular(snapshot_path / "data.parquet")
        parquet_sha256, residual_key = self._runtime_residual(parquet)
        update_members = {
            path.name: (_media_type(path.name), _read_regular(path, maximum=MAX_BYTEA_ARTIFACT)[0])
            for path in update_path.parent.iterdir()
        }
        lineage_payload, _ = _read_regular(lineage_path, maximum=MAX_BYTEA_ARTIFACT)
        try:
            with self.config.connect() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (instrument,)
                    )
                    current = connection.execute(
                        "SELECT snapshot_id FROM qr.dataset_current WHERE instrument=%s",
                        (instrument,),
                    ).fetchone()
                    current_id = None if current is None else current["snapshot_id"].strip()
                    if current_id != expected_prior_snapshot_id:
                        from .updates import ConcurrentUpdateError

                        raise ConcurrentUpdateError("dataset current generation changed")
                    connection.execute(
                        "INSERT INTO qr.residual_artifacts(artifact_sha256, byte_size, media_type, "
                        "residual_class, residual_key) VALUES (%s,%s,'application/octet-stream',"
                        "'DATASET_PARQUET',%s) ON CONFLICT (artifact_sha256) DO NOTHING",
                        (parquet_sha256, len(parquet), residual_key),
                    )
                    stored_residual = connection.execute(
                        "SELECT byte_size, residual_class, residual_key FROM qr.residual_artifacts "
                        "WHERE artifact_sha256=%s",
                        (parquet_sha256,),
                    ).fetchone()
                    if stored_residual is None or (
                        stored_residual["byte_size"],
                        stored_residual["residual_class"],
                        stored_residual["residual_key"],
                    ) != (len(parquet), "DATASET_PARQUET", residual_key):
                        raise PersistenceConflict("dataset residual registry conflicts")
                    prefix = f"platform/datasets/{instrument}/{verified['snapshot_id']}"
                    self._insert_runtime_file(
                        connection,
                        relative_path=f"{prefix}/data.parquet",
                        file_class="DATASET_PARQUET",
                        payload=parquet,
                        residual_sha256=parquet_sha256,
                    )
                    for name, payload in snapshot_members.items():
                        self._insert_runtime_file(
                            connection,
                            relative_path=f"{prefix}/{name}",
                            file_class="DATASET_METADATA",
                            payload=payload,
                        )
                    lineage_sha256 = self._insert_runtime_file(
                        connection,
                        relative_path=(
                            f"platform/snapshot-lineage/{instrument}/"
                            f"{verified['snapshot_id']}/lineage.json"
                        ),
                        file_class="DATASET_LINEAGE",
                        payload=lineage_payload,
                    )
                    update_set_id = self._publish_artifact_set_in_transaction(
                        connection, kind="DATASET_UPDATE", members=update_members
                    )
                    connection.execute(
                        "INSERT INTO qr.dataset_snapshots(snapshot_id, instrument, schema_version, "
                        "canonical_sha256, parquet_sha256, manifest_sha256, "
                        "parquet_artifact_sha256, manifest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (snapshot_id) DO NOTHING",
                        (
                            verified["snapshot_id"], instrument, verified["schema_version"],
                            verified["canonical_sha256"], verified["parquet_sha256"],
                            _sha256_bytes(snapshot_members["manifest.json"]), parquet_sha256,
                            Jsonb(verified),
                        ),
                    )
                    stored_snapshot = connection.execute(
                        "SELECT instrument, canonical_sha256, parquet_sha256, manifest "
                        "FROM qr.dataset_snapshots WHERE snapshot_id=%s",
                        (verified["snapshot_id"],),
                    ).fetchone()
                    if stored_snapshot is None or (
                        stored_snapshot["instrument"],
                        stored_snapshot["canonical_sha256"].strip(),
                        stored_snapshot["parquet_sha256"].strip(),
                        stored_snapshot["manifest"],
                    ) != (
                        instrument,
                        verified["canonical_sha256"],
                        verified["parquet_sha256"],
                        verified,
                    ):
                        raise PersistenceConflict("dataset snapshot identity conflicts")
                    connection.execute(
                        "INSERT INTO qr.dataset_updates(update_id, instrument, snapshot_id, "
                        "artifact_set_id, document) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (update_id) DO NOTHING",
                        (
                            update["update_id"], instrument, verified["snapshot_id"],
                            update_set_id, Jsonb(update),
                        ),
                    )
                    lineage_document = _strict_json(lineage_payload, "dataset lineage")
                    if lineage_document.get("lineage") != lineage:
                        raise PersistenceConflict("dataset lineage bytes conflict")
                    connection.execute(
                        "INSERT INTO qr.dataset_lineage_claims(instrument, snapshot_id, "
                        "artifact_sha256, document) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (instrument, snapshot_id) DO NOTHING",
                        (instrument, verified["snapshot_id"], lineage_sha256, Jsonb(lineage_document)),
                    )
                    connection.execute(
                        "INSERT INTO qr.dataset_current(instrument, snapshot_id, generation) "
                        "VALUES (%s,%s,1) ON CONFLICT (instrument) DO UPDATE SET "
                        "snapshot_id=EXCLUDED.snapshot_id, generation=qr.dataset_current.generation+1",
                        (instrument, verified["snapshot_id"]),
                    )
        except psycopg.Error as exc:
            raise PersistenceUnavailableError("dataset publication failed") from exc
        if self.dataset_current_snapshot(instrument) != verified["snapshot_id"]:
            raise PersistenceUnavailableError("dataset current read-back mismatch")
        return {
            "snapshot_id": verified["snapshot_id"],
            "update_id": update["update_id"],
            "lineage": self.dataset_snapshot_lineage(instrument, verified["snapshot_id"]),
        }

    @contextmanager
    def materialize_dataset_snapshot(
        self, instrument: str, snapshot_id: str
    ) -> Iterator[Path]:
        if SHA256.fullmatch(snapshot_id) is None:
            raise ValueError("snapshot_id must be lowercase SHA-256")
        prefix = f"platform/datasets/{instrument}/{snapshot_id}/"
        with self.config.connect() as connection:
            snapshot = connection.execute(
                "SELECT parquet_artifact_sha256 FROM qr.dataset_snapshots "
                "WHERE instrument=%s AND snapshot_id=%s",
                (instrument, snapshot_id),
            ).fetchone()
            rows = connection.execute(
                "SELECT relative_path, sha256, byte_size, artifact_sha256, residual_sha256 "
                "FROM qr.source_files WHERE relative_path LIKE %s ORDER BY relative_path",
                (prefix + "%",),
            ).fetchall()
        if snapshot is None or not rows:
            raise ValueError(f"unknown dataset snapshot: {instrument}@{snapshot_id}")
        with tempfile.TemporaryDirectory(prefix="quant-dataset-snapshot-") as temporary:
            state_root = Path(temporary)
            root = state_root / "datasets" / instrument / snapshot_id
            root.mkdir(parents=True, mode=0o700)
            for row in rows:
                name = PurePosixPath(row["relative_path"]).name
                if row["artifact_sha256"] is not None:
                    with self.config.connect() as connection:
                        artifact = connection.execute(
                            "SELECT payload FROM qr.artifacts WHERE artifact_sha256=%s",
                            (row["artifact_sha256"],),
                        ).fetchone()
                    if artifact is None:
                        raise PersistenceUnavailableError("dataset bytea artifact is missing")
                    payload = bytes(artifact["payload"])
                else:
                    digest = row["residual_sha256"].strip()
                    with self.config.connect() as connection:
                        residual = connection.execute(
                            "SELECT residual_key FROM qr.residual_artifacts WHERE artifact_sha256=%s",
                            (digest,),
                        ).fetchone()
                    if residual is None:
                        raise PersistenceUnavailableError("dataset residual artifact is missing")
                    residual_root = os.environ.get("QUANT_RESIDUAL_ROOT")
                    if not residual_root:
                        raise PersistenceUnavailableError("QUANT_RESIDUAL_ROOT is required")
                    source = Path(residual_root) / residual["residual_key"]
                    payload, _ = _read_regular(source)
                if len(payload) != row["byte_size"] or _sha256_bytes(payload) != row["sha256"].strip():
                    raise PersistenceUnavailableError("dataset artifact identity mismatch")
                target = root / name
                target.write_bytes(payload)
                target.chmod(0o444)
            root.chmod(0o555)
            lineage_relative = (
                f"platform/snapshot-lineage/{instrument}/{snapshot_id}/lineage.json"
            )
            with self.config.connect() as connection:
                lineage = connection.execute(
                    "SELECT a.payload FROM qr.source_files f JOIN qr.artifacts a "
                    "ON a.artifact_sha256=f.artifact_sha256 WHERE f.relative_path=%s",
                    (lineage_relative,),
                ).fetchone()
            if lineage is not None:
                lineage_root = state_root / "snapshot-lineage" / instrument / snapshot_id
                lineage_root.mkdir(parents=True, mode=0o700)
                lineage_path = lineage_root / "lineage.json"
                lineage_path.write_bytes(bytes(lineage["payload"]))
                lineage_path.chmod(0o444)
                lineage_root.chmod(0o555)
            yield root


class FullStateImporter:
    """Create-only importer for one byte-frozen legacy state copy."""

    def __init__(self, persistence: FullPostgresPersistence, residual_root: Path):
        self.persistence = persistence
        self.residual_root = residual_root.absolute()

    @staticmethod
    def _database(source_root: Path, candidates: Sequence[str], label: str) -> Path:
        found = [source_root / value for value in candidates if (source_root / value).is_file()]
        if len(found) != 1:
            raise MigrationRejected(f"{label} must resolve to exactly one frozen file")
        return found[0]

    @staticmethod
    @contextmanager
    def _sqlite(path: Path) -> Iterator[sqlite3.Connection]:
        _read_regular(path)
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _table_rows(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[dict[str, Any]]]:
        if SAFE_TABLE.fullmatch(table) is None:
            raise MigrationRejected("unsafe source table identity")
        columns = [row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            raise MigrationRejected(f"required source table is absent: {table}")
        order = ", ".join(f'"{column}"' for column in columns)
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
        return columns, rows

    @staticmethod
    def _row_digest(rows: list[dict[str, Any]]) -> str:
        return _sha256_bytes(canonical_json_bytes(rows))

    def _import_table(
        self,
        source: sqlite3.Connection,
        target: psycopg.Connection[Any],
        *,
        source_table: str,
        target_schema: str,
        target_table: str | None = None,
    ) -> dict[str, Any]:
        table = target_table or source_table
        columns, rows = self._table_rows(source, source_table)
        target_count = target.execute(
            f'SELECT count(*) AS count FROM "{target_schema}"."{table}"'
        ).fetchone()["count"]
        if target_count:
            raise MigrationRejected(f"target table is not empty: {target_schema}.{table}")
        names = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("%s" for _ in columns)
        for row in rows:
            target.execute(
                f'INSERT INTO "{target_schema}"."{table}" ({names}) VALUES ({placeholders})',
                tuple(row[column] for column in columns),
            )
        selected = [
            dict(row)
            for row in target.execute(
                f'SELECT {names} FROM "{target_schema}"."{table}" ORDER BY {names}'
            ).fetchall()
        ]
        if selected != rows:
            raise MigrationRejected(f"table read-back parity mismatch: {source_table}")
        return {
            "source_table": source_table,
            "target_table": f"{target_schema}.{table}",
            "row_count": len(rows),
            "ordered_row_digest": self._row_digest(rows),
        }

    def _store_residual(self, relative: str, payload: bytes, file_class: str) -> str:
        digest = _sha256_bytes(payload)
        target = self.residual_root / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            existing, _ = _read_regular(target)
            if existing != payload:
                raise PersistenceConflict("residual digest path conflicts")
        else:
            with target.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o444)
        with self.persistence.config.connect() as connection:
            connection.execute(
                "INSERT INTO qr.residual_artifacts(artifact_sha256, byte_size, media_type, residual_class, residual_key) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (artifact_sha256) DO NOTHING",
                (digest, len(payload), "application/octet-stream", file_class, f"{digest[:2]}/{digest}"),
            )
        return digest

    def _import_files(self, source_root: Path, inventory: dict[str, Any]) -> dict[str, Any]:
        bytea_count = residual_count = excluded_count = migrated_bytes = 0
        grouped: dict[tuple[str, str], dict[str, tuple[str, bytes]]] = {}
        with self.persistence.config.connect() as connection:
            if connection.execute("SELECT count(*) AS count FROM qr.source_files").fetchone()["count"]:
                raise MigrationRejected("target file inventory is not empty")
        for item in inventory["files"]:
            relative = item["relative_path"]
            payload, metadata = _read_regular(source_root / relative)
            if (
                len(payload) != item["byte_size"]
                or _sha256_bytes(payload) != item["sha256"]
                or stat.S_IMODE(metadata.st_mode) != item["mode"]
            ):
                raise MigrationRejected(f"frozen source changed after inventory: {relative}")
            if item["classification"] == "STRUCTURED_IMPORTED":
                continue
            if item["classification"] == "EPHEMERAL_EXCLUDED":
                excluded_count += 1
                continue
            artifact_sha: str | None = None
            residual_sha: str | None = None
            if item["classification"] == "RESIDUAL":
                residual_sha = self._store_residual(relative, payload, item["file_class"])
                residual_count += 1
            else:
                if len(payload) > MAX_BYTEA_ARTIFACT:
                    raise MigrationRejected(f"non-residual source member exceeds bytea limit: {relative}")
                artifact_sha = _sha256_bytes(payload)
                with self.persistence.config.connect() as connection:
                    connection.execute(
                        "INSERT INTO qr.artifacts(artifact_sha256, byte_size, media_type, payload) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (artifact_sha256) DO NOTHING",
                        (artifact_sha, len(payload), _media_type(relative), payload),
                    )
                bytea_count += 1
            with self.persistence.config.connect() as connection:
                connection.execute(
                    "INSERT INTO qr.source_files(relative_path, file_class, mode, byte_size, sha256, "
                    "artifact_sha256, residual_sha256, classification) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        relative,
                        item["file_class"],
                        item["mode"],
                        item["byte_size"],
                        item["sha256"],
                        artifact_sha,
                        residual_sha,
                        item["classification"],
                    ),
                )
            migrated_bytes += len(payload)
            key = _package_key(relative, item["file_class"])
            if artifact_sha is not None and key is not None:
                grouped.setdefault(key, {})[PurePosixPath(relative).name] = (
                    _media_type(relative),
                    payload,
                )
        self._publish_domain_packages(grouped, source_root)
        return {
            "bytea_file_count": bytea_count,
            "residual_file_count": residual_count,
            "ephemeral_excluded_count": excluded_count,
            "migrated_byte_count": migrated_bytes,
            "residual_classes": sorted(RESIDUAL_CLASSES),
        }

    def _publish_domain_packages(
        self,
        grouped: Mapping[tuple[str, str], Mapping[str, tuple[str, bytes]]],
        source_root: Path,
    ) -> None:
        with self.persistence.config.connect() as connection:
            attempt_rows = {
                Path(row["result_path"]).name: dict(row)
                for row in connection.execute(
                    "SELECT attempt_id, result_path, result_digest, status FROM qr_catalog.attempts "
                    "WHERE result_path IS NOT NULL"
                ).fetchall()
            }
        for (file_class, identity), members in sorted(grouped.items()):
            artifact_set_id = self.persistence.publish_artifact_set(kind=file_class, members=members)
            with self.persistence.config.connect() as connection:
                if file_class == "ATTEMPT_EVIDENCE":
                    attempt = attempt_rows.get(identity)
                    if attempt is None:
                        raise MigrationRejected(f"attempt evidence has no owning row: {identity}")
                    connection.execute(
                        "INSERT INTO qr.attempt_evidence_packages(attempt_id, artifact_set_id, result_digest, state, canonical, imported_path) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (
                            attempt["attempt_id"], artifact_set_id, attempt["result_digest"],
                            attempt["status"], attempt["status"] == "SUCCEEDED",
                            f"platform/experiment-runs/{identity}",
                        ),
                    )
                elif file_class == "ATTEMPT_REPORT":
                    report_id = identity
                    attempt_id = _attempt_from_report_path(grouped, file_class, identity)
                    connection.execute(
                        "INSERT INTO qr.report_artifacts(report_artifact_id, attempt_id, artifact_set_id) "
                        "VALUES (%s,%s,%s)",
                        (report_id, attempt_id, artifact_set_id),
                    )
                elif file_class == "PRODUCTION_RESULT":
                    manifest_payload = members.get("result-manifest.json")
                    if manifest_payload is None:
                        raise MigrationRejected(f"production result lacks manifest: {identity}")
                    manifest = _strict_json(manifest_payload[1], "production result manifest")
                    if manifest.get("result_id") != identity:
                        raise MigrationRejected("production result identity mismatch")
                    connection.execute(
                        "INSERT INTO qr.production_results(result_id, request_id, artifact_set_id, manifest) "
                        "VALUES (%s,%s,%s,%s)",
                        (identity, manifest.get("request_id"), artifact_set_id, Jsonb(manifest)),
                    )
                elif file_class == "ACCOUNTING_EVIDENCE":
                    document = _package_document(members)
                    connection.execute(
                        "INSERT INTO qr.accounting_outcome_references(result_digest, artifact_set_id, document) "
                        "VALUES (%s,%s,%s)", (identity, artifact_set_id, Jsonb(document))
                    )
                elif file_class == "FORMAL_CALIBRATION":
                    document = _package_document(members)
                    connection.execute(
                        "INSERT INTO qr.formal_calibration_claims(claim_id, artifact_set_id, document) "
                        "VALUES (%s,%s,%s)", (identity, artifact_set_id, Jsonb(document))
                    )
                elif file_class == "DATASET_UPDATE":
                    parts = PurePosixPath(identity).parts
                    if len(parts) != 4:
                        raise MigrationRejected("dataset update package path is invalid")
                    document = _package_document(members)
                    connection.execute(
                        "INSERT INTO qr.dataset_updates(update_id, instrument, snapshot_id, artifact_set_id, document) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (
                            parts[3],
                            parts[2],
                            document.get("snapshot_id"),
                            artifact_set_id,
                            Jsonb(document),
                        ),
                    )
                elif file_class == "DATASET_LINEAGE":
                    parts = PurePosixPath(identity).parts
                    if len(parts) != 4 or SHA256.fullmatch(parts[3]) is None:
                        raise MigrationRejected("dataset lineage package path is invalid")
                    document = _package_document(members)
                    payload = next(iter(members.values()))[1]
                    connection.execute(
                        "INSERT INTO qr.dataset_lineage_claims(instrument, snapshot_id, artifact_sha256, document) "
                        "VALUES (%s,%s,%s,%s)",
                        (parts[2], parts[3], _sha256_bytes(payload), Jsonb(document)),
                    )
                else:
                    evidence_id = _sha256_bytes(
                        canonical_json_bytes({"class": file_class, "identity": identity, "set": artifact_set_id})
                    )
                    connection.execute(
                        "INSERT INTO qr.accepted_evidence_packages(evidence_id, evidence_class, artifact_set_id, source_path) "
                        "VALUES (%s,%s,%s,%s)",
                        (evidence_id, file_class, artifact_set_id, identity),
                    )
        self._import_dataset_graph(source_root)
        self._import_report_pointers(source_root)

    def _import_dataset_graph(self, source_root: Path) -> None:
        datasets = source_root / "platform/datasets"
        if not datasets.exists():
            return
        with self.persistence.config.connect() as connection:
            for instrument_dir in sorted(datasets.iterdir()):
                if not instrument_dir.is_dir() or instrument_dir.is_symlink():
                    continue
                for snapshot_dir in sorted(instrument_dir.iterdir()):
                    if not snapshot_dir.is_dir() or SHA256.fullmatch(snapshot_dir.name) is None:
                        continue
                    manifest_bytes, _ = _read_regular(snapshot_dir / "manifest.json", maximum=MAX_BYTEA_ARTIFACT)
                    manifest = _strict_json(manifest_bytes, "dataset manifest")
                    parquet_bytes, _ = _read_regular(snapshot_dir / "data.parquet")
                    parquet_sha = _sha256_bytes(parquet_bytes)
                    if manifest.get("snapshot_id") != snapshot_dir.name or manifest.get("parquet_sha256") != parquet_sha:
                        raise MigrationRejected("dataset snapshot identity mismatch")
                    connection.execute(
                        "INSERT INTO qr.dataset_snapshots(snapshot_id, instrument, schema_version, canonical_sha256, "
                        "parquet_sha256, manifest_sha256, parquet_artifact_sha256, manifest) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            snapshot_dir.name, instrument_dir.name, manifest["schema_version"],
                            manifest["canonical_sha256"], parquet_sha, _sha256_bytes(manifest_bytes),
                            parquet_sha, Jsonb(manifest),
                        ),
                    )
                pointer = instrument_dir / "latest.json"
                if pointer.is_file():
                    value = _strict_json(_read_regular(pointer, maximum=MAX_BYTEA_ARTIFACT)[0], "dataset current pointer")
                    snapshot_id = value.get("snapshot_id")
                    if not isinstance(snapshot_id, str) or SHA256.fullmatch(snapshot_id) is None:
                        raise MigrationRejected("dataset current pointer is invalid")
                    connection.execute(
                        "INSERT INTO qr.dataset_current(instrument, snapshot_id) VALUES (%s,%s)",
                        (instrument_dir.name, snapshot_id),
                    )

    def _import_report_pointers(self, source_root: Path) -> None:
        reports = source_root / "platform/attempt-reports"
        if not reports.exists():
            return
        with self.persistence.config.connect() as connection:
            for attempt_dir in sorted(reports.iterdir()):
                pointer = attempt_dir / "latest.json"
                if not pointer.is_file():
                    continue
                value = _strict_json(_read_regular(pointer, maximum=MAX_BYTEA_ARTIFACT)[0], "report pointer")
                report_id = value.get("report_artifact_id") or value.get("artifact_id")
                if not isinstance(report_id, str) or SHA256.fullmatch(report_id) is None:
                    raise MigrationRejected("report pointer identity is invalid")
                connection.execute(
                    "INSERT INTO qr.report_pointer_events(attempt_id, sequence, report_artifact_id, pointer) "
                    "VALUES (%s,1,%s,%s)", (attempt_dir.name, report_id, Jsonb(value))
                )
                connection.execute(
                    "INSERT INTO qr.report_current(attempt_id, report_artifact_id, sequence) VALUES (%s,%s,1)",
                    (attempt_dir.name, report_id),
                )

    def import_frozen(self, source_root: Path, expected_manifest: Mapping[str, Any]) -> dict[str, Any]:
        source_root = source_root.absolute()
        actual = inventory_source(source_root)
        if actual != expected_manifest:
            raise MigrationRejected("frozen source inventory does not match admission manifest")
        catalog_path = self._database(source_root, ("platform/catalog.sqlite3", "catalog.sqlite3"), "catalog SQLite")
        production_path = self._database(source_root, ("production.sqlite3",), "production SQLite")
        table_receipts: list[dict[str, Any]] = []
        with self._sqlite(catalog_path) as source, self.persistence.config.connect() as target:
            with target.transaction():
                available = {
                    row["name"] for row in source.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if available != set(CATALOG_TABLES) | {"operators", "operator_versions", "operator_latest"}:
                    raise MigrationRejected(
                        f"catalog table set is unsupported: {sorted(available)}"
                    )
                for table in CATALOG_TABLES:
                    table_receipts.append(
                        self._import_table(source, target, source_table=table, target_schema="qr_catalog")
                    )
        from .operator_importer import _import_record

        with self._sqlite(catalog_path) as source:
            rows = [
                dict(row) for row in source.execute(
                    "SELECT o.operator_id, o.slot, o.title_zh, o.summary_zh, "
                    "o.created_at AS operator_created_at, v.version, v.content_digest, "
                    "v.parameter_schema_json, v.defaults_json, v.documentation, v.bundle_path, "
                    "v.validation_evidence_json, v.status, v.created_at "
                    "FROM operators o JOIN operator_versions v USING(operator_id) "
                    "ORDER BY o.operator_id, v.version"
                )
            ]
        if not self.persistence.is_empty():
            raise MigrationRejected("Operator target is not empty")
        operator_projection: list[dict[str, Any]] = []
        for row in rows:
            projected, artifacts = _import_record(row, catalog_path.parent)
            self.persistence.publish_operator(
                operator_id=projected["operator_id"], slot=projected["slot"],
                version=projected["version"], title_zh=projected["title_zh"],
                summary_zh=projected["summary_zh"], content_digest=projected["content_digest"],
                parameter_schema=projected["parameter_schema"], defaults=projected["defaults"],
                documentation=projected["documentation"],
                validation_evidence=projected["validation_evidence"], artifacts=artifacts,
                created_at=projected["created_at"],
                operator_created_at=projected["operator_created_at"],
            )
            operator_projection.append(projected)
        with self._sqlite(production_path) as source, self.persistence.config.connect() as target:
            with target.transaction():
                available = {
                    row["name"] for row in source.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if available != set(PRODUCTION_TABLES):
                    raise MigrationRejected(f"production table set is unsupported: {sorted(available)}")
                for table in PRODUCTION_TABLES:
                    table_receipts.append(
                        self._import_table(
                            source,
                            target,
                            source_table=table,
                            target_schema="qr",
                            target_table="production_schema_migrations" if table == "schema_migrations" else table,
                        )
                    )
        with self.persistence.config.connect() as connection:
            attempts = connection.execute(
                "SELECT attempt_id, status, created_at, started_at, finished_at, result_digest, comparison "
                "FROM qr_catalog.attempts ORDER BY attempt_id"
            ).fetchall()
            for attempt in attempts:
                payload = {
                    "status": attempt["status"],
                    "result_digest": attempt["result_digest"],
                    "comparison": attempt["comparison"],
                }
                occurred_at = (
                    attempt["finished_at"] or attempt["started_at"] or attempt["created_at"]
                )
                connection.execute(
                    "INSERT INTO qr.attempt_events(attempt_id, sequence, event_type, payload, occurred_at) "
                    "VALUES (%s,1,%s,%s,%s::timestamptz)",
                    (attempt["attempt_id"], attempt["status"], Jsonb(payload), occurred_at),
                )
        file_receipt = self._import_files(source_root, actual)
        after = inventory_source(source_root)
        if after != actual:
            raise MigrationRejected("source state changed during import")
        receipt = {
            "schema": PARITY_RECEIPT_SCHEMA,
            "status": "PASS",
            "source_inventory_digest": actual["inventory_digest"],
            "source_file_count": actual["file_count"],
            "source_byte_count": actual["byte_count"],
            "tables": table_receipts,
            "operator_version_count": len(operator_projection),
            "ordered_operator_digest": _sha256_bytes(canonical_json_bytes(operator_projection)),
            "files": file_receipt,
            "source_unchanged": True,
        }
        return receipt


def _media_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".json": "application/json",
        ".html": "text/html; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".py": "text/x-python; charset=utf-8",
    }.get(suffix, "application/octet-stream")


def _package_key(relative: str, file_class: str) -> tuple[str, str] | None:
    parts = PurePosixPath(relative).parts
    if file_class == "ATTEMPT_EVIDENCE" and len(parts) >= 4:
        return file_class, parts[2]
    if file_class == "ATTEMPT_REPORT" and "artifacts" in parts:
        index = parts.index("artifacts")
        if len(parts) > index + 1:
            return file_class, parts[index + 1]
    if file_class == "PRODUCTION_RESULT" and len(parts) >= 3:
        return file_class, parts[1]
    if file_class == "ACCOUNTING_EVIDENCE" and len(parts) >= 4:
        return file_class, parts[2]
    if file_class == "FORMAL_CALIBRATION" and len(parts) >= 3:
        return file_class, parts[1]
    if file_class in {"DATASET_UPDATE", "DATASET_LINEAGE", "ATTEMPT_AUDIT", "REPORT_AUTHORITY", "VALIDATION_EVIDENCE"}:
        return file_class, "/".join(parts[:-1])
    return None


def _attempt_from_report_path(
    grouped: Mapping[tuple[str, str], Mapping[str, tuple[str, bytes]]],
    file_class: str,
    report_id: str,
) -> str:
    # The grouping identity does not retain the attempt directory, so recover it from
    # the canonical report document/manifest rather than guessing from path ordering.
    members = grouped[(file_class, report_id)]
    for name in ("report-manifest.json", "report-document.json"):
        if name in members:
            document = _strict_json(members[name][1], name)
            attempt_id = document.get("attempt_id")
            if isinstance(attempt_id, str) and attempt_id:
                return attempt_id
    raise MigrationRejected(f"report artifact has no bound attempt identity: {report_id}")


def _package_document(members: Mapping[str, tuple[str, bytes]]) -> dict[str, Any]:
    for name in ("manifest.json", "result-manifest.json", "evidence.json"):
        if name in members:
            return _strict_json(members[name][1], name)
    return {
        "member_sha256": {
            name: _sha256_bytes(payload) for name, (_, payload) in sorted(members.items())
        }
    }


def _load_json_file(path: Path) -> dict[str, Any]:
    payload, _ = _read_regular(path)
    return _strict_json(payload, path.name)


def _write_create_only(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(dict(value)) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description="Complete PostgreSQL persistence migration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--source-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("import")
    run.add_argument("--source-root", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--residual-root", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--source-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "migrate":
        runtime_password = os.environ.get("QUANT_POSTGRES_PASSWORD_FILE")
        if not runtime_password:
            raise PersistenceUnavailableError("QUANT_POSTGRES_PASSWORD_FILE is required")
        install_full_schema(
            PostgresConfig.from_environment("QUANT_POSTGRES_MIGRATION_"),
            runtime_user=os.environ.get("QUANT_POSTGRES_USER", "qr_runtime"),
            runtime_password_file=Path(runtime_password),
        )
    elif args.command == "inventory":
        _write_create_only(args.output, inventory_source(args.source_root))
    elif args.command == "import":
        persistence = FullPostgresPersistence.from_environment()
        receipt = FullStateImporter(persistence, args.residual_root).import_frozen(
            args.source_root, _load_json_file(args.manifest)
        )
        _write_create_only(args.receipt, receipt)
    else:
        manifest = _load_json_file(args.manifest)
        if inventory_source(args.source_root) != manifest:
            raise MigrationRejected("frozen source inventory mismatch")
        FullPostgresPersistence.from_environment().verify_schema()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
