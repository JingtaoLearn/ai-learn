from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import prod
from typing import Any, Callable

from .catalog import Catalog, CatalogMigration
from .dataset_service import DatasetResolutionError, DatasetService
from .experiment_service import ExperimentService
from .schemas import (
    MAX_WEB_SAFE_INTEGER,
    SchemaValidationError,
    canonical_json_bytes,
    validate_parameters,
)
from .study_contracts import INFORMATION_INTERVAL, normalize_fold_window
from .study_datasets import ExecutionDatasetSliceFactory
from .study_evaluation import (
    EVALUATION_DEFAULTS,
    EVALUATION_PARAMETER_SCHEMA,
    EVALUATION_POLICY_DIGEST,
    EVALUATION_POLICY_IDENTITY,
    METRIC_ENGINE_IDENTITY,
    MetricDocumentFactory,
    RobustWalkForwardPolicy,
)
from .study_suggesters import (
    Exhausted,
    GridParameterSuggester,
    SeededRandomParameterSuggester,
    Suggestion,
)


class StudyValidationError(ValueError):
    """Raised when a Study specification cannot be frozen safely."""


class StudyNotFoundError(LookupError):
    """Raised when a Study ID has no durable projection."""


STUDY_FIELDS = {
    "schema_version",
    "dataset",
    "template",
    "operators",
    "search",
    "validation",
    "evaluation",
    "holdout",
    "lineage",
}
DATASET_FIELDS = {"dataset_id", "start", "end"}
TEMPLATE_FIELDS = {"name", "version", "parameters"}
OPERATOR_FIELDS = {"operator_id", "version", "parameters"}
SEARCH_FIELDS = {
    "suggester",
    "suggester_version",
    "seed",
    "unique_trial_budget",
    "max_suggestions",
    "space",
}
VALIDATION_FIELDS = {
    "outer_folds",
    "inner_folds",
    "scoring_sessions",
    "minimum_training_sessions",
    "purge_sessions",
    "outer_account_policy",
}
EVALUATION_FIELDS = {"policy_id", "version", "parameters"}
HOLDOUT_FIELDS = {"sessions", "pass_rule"}
LINEAGE_FIELDS = {
    "parent_study_ids",
    "prior_unique_candidate_count",
    "is_complete",
}
STUDY_ID = re.compile(r"^[0-9a-f]{64}$")
ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
OWNER_NONCE = re.compile(r"^[0-9a-f]{32}$")
INTERNAL_ACTION_PREFIX = "study-internal:"
INTERNAL_EFFECT_ACTION_ID = re.compile(r"^study-internal:effect:[0-9a-f]{64}$")
ACTION_OPERATIONS = {
    "SUBMIT",
    "CONTROL_PAUSE",
    "CONTROL_RESUME",
    "CONTROL_CANCEL",
    "COORDINATOR_LEASE",
    "EXECUTION_IDENTITY_DRIFT",
    "EFFECT_INTENT",
    "EFFECT_DISPATCH_AUTHORIZATION",
    "EFFECT_RECEIPT",
}
MAX_DATASET_RESOLUTION_ATTEMPTS = 2
MAX_STUDY_SESSIONS = 100_000
MAX_STUDY_EVENTS = 10_000
MAX_STUDY_EVIDENCE = 10_000
MAX_STUDY_BINDINGS = 10_000
MAX_STUDY_DETAIL_BYTES = 64 * 1_048_576
MAX_STUDY_DOCUMENT_BYTES = 4 * 1_048_576
STUDY_MIGRATION = CatalogMigration(
    version=5,
    applied_at="2026-08-28T00:00:00Z",
    sql="""
CREATE TABLE parameter_studies (
    study_id TEXT PRIMARY KEY,
    preview_digest TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    frozen_plan_json TEXT NOT NULL,
    operational_metadata_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (
        phase IN (
            'FROZEN', 'VALIDATING_SELECTION_PROCESS',
            'SELECTING_FINAL_CANDIDATE', 'HOLDOUT_READY',
            'HOLDOUT_RUNNING', 'COMPLETED'
        )
    ),
    control_status TEXT NOT NULL CHECK (
        control_status IN ('ACTIVE', 'PAUSED', 'CANCELLED', 'FAILED')
    ),
    selection_outcome TEXT NOT NULL CHECK (
        selection_outcome IN (
            'NOT_DETERMINED', 'CHAMPION_SELECTED', 'NO_ELIGIBLE_CANDIDATE'
        )
    ),
    holdout_outcome TEXT NOT NULL CHECK (
        holdout_outcome IN ('NOT_RUN', 'PASSED', 'FAILED')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE parameter_study_events (
    study_id TEXT NOT NULL REFERENCES parameter_studies(study_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (study_id, sequence)
);

CREATE TABLE parameter_study_actions (
    action_id TEXT PRIMARY KEY CHECK (
        length(action_id) BETWEEN 1 AND 128
        AND substr(action_id, 1, 1) GLOB '[A-Za-z0-9]'
        AND action_id NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    operation TEXT NOT NULL CHECK (
        operation IN (
            'SUBMIT', 'CONTROL_PAUSE', 'CONTROL_RESUME', 'CONTROL_CANCEL',
            'COORDINATOR_LEASE', 'EXECUTION_IDENTITY_DRIFT',
            'EFFECT_INTENT', 'EFFECT_DISPATCH_AUTHORIZATION',
            'EFFECT_RECEIPT'
        )
    ),
    study_id TEXT NOT NULL REFERENCES parameter_studies(study_id) CHECK (
        length(study_id) = 64 AND study_id NOT GLOB '*[^0-9a-f]*'
    ),
    request_digest TEXT NOT NULL CHECK (
        length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    response_json TEXT NOT NULL CHECK (
        json_valid(response_json) AND json_type(response_json) = 'object'
    ),
    created_at TEXT NOT NULL,
    CHECK (
        (
            operation IN (
                'SUBMIT', 'CONTROL_PAUSE', 'CONTROL_RESUME', 'CONTROL_CANCEL'
            )
            AND action_id NOT GLOB 'study-internal:*'
        )
        OR (
            operation = 'COORDINATOR_LEASE'
            AND length(action_id) >= 87
            AND substr(action_id, 1, 21) = 'study-internal:lease:'
            AND substr(action_id, 22, 64) = study_id
            AND substr(action_id, 86, 1) = ':'
            AND substr(action_id, 87, 1) GLOB '[1-9]'
            AND substr(action_id, 87) NOT GLOB '*[^0-9]*'
        )
        OR (
            operation = 'EXECUTION_IDENTITY_DRIFT'
            AND length(action_id) = 85
            AND substr(action_id, 1, 21) = 'study-internal:drift:'
            AND substr(action_id, 22, 64) = study_id
        )
        OR (
            operation = 'EFFECT_INTENT'
            AND length(action_id) = 86
            AND substr(action_id, 1, 22) = 'study-internal:effect:'
            AND substr(action_id, 23, 64) NOT GLOB '*[^0-9a-f]*'
        )
        OR (
            operation = 'EFFECT_DISPATCH_AUTHORIZATION'
            AND length(action_id) >= 90
            AND substr(action_id, 1, 24) = 'study-internal:dispatch:'
            AND substr(action_id, 25, 64) NOT GLOB '*[^0-9a-f]*'
            AND substr(action_id, 89, 1) = ':'
            AND substr(action_id, 90, 1) GLOB '[1-9]'
            AND substr(action_id, 90) NOT GLOB '*[^0-9]*'
        )
        OR (
            operation = 'EFFECT_RECEIPT'
            AND length(action_id) = 87
            AND substr(action_id, 1, 23) = 'study-internal:receipt:'
            AND substr(action_id, 24, 64) NOT GLOB '*[^0-9a-f]*'
        )
    )
);

CREATE TABLE parameter_study_holdout_history_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    pre_ledger_history_complete INTEGER NOT NULL CHECK (
        pre_ledger_history_complete IN (0, 1)
    ),
    pre_ledger_experiment_count INTEGER NOT NULL CHECK (
        pre_ledger_experiment_count >= 0
    ),
    assessed_at TEXT NOT NULL
);

INSERT INTO parameter_study_holdout_history_metadata(
    singleton, pre_ledger_history_complete,
    pre_ledger_experiment_count, assessed_at
)
SELECT
    1,
    0,
    COUNT(*),
    '2026-08-28T00:00:00Z'
FROM experiments;

CREATE TABLE parameter_study_holdout_ledger (
    study_id TEXT NOT NULL REFERENCES parameter_studies(study_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    holdout_identity_digest TEXT NOT NULL CHECK (
        length(holdout_identity_digest) = 64
        AND holdout_identity_digest NOT GLOB '*[^0-9a-f]*'
    ),
    event_type TEXT NOT NULL CHECK (
        event_type IN ('GRANTED', 'ACCESSED', 'EXPOSURE_RECORDED')
    ),
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (study_id, sequence)
);

CREATE INDEX idx_parameter_study_events_order
ON parameter_study_events(study_id, sequence);

CREATE INDEX idx_parameter_study_holdout_exposure
ON parameter_study_holdout_ledger(holdout_identity_digest, event_type);

CREATE TRIGGER immutable_parameter_study_plan
BEFORE UPDATE OF
    study_id, preview_digest, request_digest, frozen_plan_json,
    operational_metadata_json, created_at
ON parameter_studies BEGIN
    SELECT RAISE(ABORT, 'frozen Study plan is immutable');
END;

CREATE TRIGGER append_only_parameter_study_events_update
BEFORE UPDATE ON parameter_study_events BEGIN
    SELECT RAISE(ABORT, 'Study events are append-only');
END;

CREATE TRIGGER append_only_parameter_study_events_delete
BEFORE DELETE ON parameter_study_events BEGIN
    SELECT RAISE(ABORT, 'Study events are append-only');
END;

CREATE TRIGGER immutable_parameter_study_actions_update
BEFORE UPDATE ON parameter_study_actions BEGIN
    SELECT RAISE(ABORT, 'Study actions are immutable');
END;

CREATE TRIGGER immutable_parameter_study_actions_delete
BEFORE DELETE ON parameter_study_actions BEGIN
    SELECT RAISE(ABORT, 'Study actions are immutable');
END;

CREATE TRIGGER immutable_parameter_study_holdout_history_update
BEFORE UPDATE ON parameter_study_holdout_history_metadata BEGIN
    SELECT RAISE(ABORT, 'holdout history metadata is immutable');
END;

CREATE TRIGGER immutable_parameter_study_holdout_history_delete
BEFORE DELETE ON parameter_study_holdout_history_metadata BEGIN
    SELECT RAISE(ABORT, 'holdout history metadata is immutable');
END;

CREATE TRIGGER append_only_parameter_study_holdout_ledger_update
BEFORE UPDATE ON parameter_study_holdout_ledger BEGIN
    SELECT RAISE(ABORT, 'holdout ledger is append-only');
END;

CREATE TRIGGER append_only_parameter_study_holdout_ledger_delete
BEFORE DELETE ON parameter_study_holdout_ledger BEGIN
    SELECT RAISE(ABORT, 'holdout ledger is append-only');
END;
""",
)

STUDY_EVALUATION_MIGRATION = CatalogMigration(
    version=6,
    applied_at="2026-08-28T00:00:00Z",
    sql="""
CREATE TABLE parameter_study_evidence (
    study_id TEXT NOT NULL REFERENCES parameter_studies(study_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    evidence_type TEXT NOT NULL CHECK (
        evidence_type IN (
            'METRIC_DOCUMENT_VERIFIED', 'CANDIDATE_EVALUATED',
            'OUTER_SELECTION_RECORDED', 'CHAMPION_FROZEN',
            'HOLDOUT_OUTCOME_RECORDED', 'EVIDENCE_CONTESTED'
        )
    ),
    candidate_digest TEXT CHECK (
        candidate_digest IS NULL OR (
            length(candidate_digest) = 64
            AND candidate_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    payload_json TEXT NOT NULL CHECK (
        json_valid(payload_json) AND json_type(payload_json) = 'object'
    ),
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (study_id, sequence)
);

CREATE UNIQUE INDEX one_frozen_champion_per_study
ON parameter_study_evidence(study_id)
WHERE evidence_type = 'CHAMPION_FROZEN';

CREATE TRIGGER append_only_parameter_study_evidence_update
BEFORE UPDATE ON parameter_study_evidence BEGIN
    SELECT RAISE(ABORT, 'Study evidence is append-only');
END;

CREATE TRIGGER append_only_parameter_study_evidence_delete
BEFORE DELETE ON parameter_study_evidence BEGIN
    SELECT RAISE(ABORT, 'Study evidence is append-only');
END;
""",
)

STUDY_ORCHESTRATION_MIGRATION = CatalogMigration(
    version=7,
    applied_at="2026-08-28T00:00:00Z",
    sql="""
CREATE TABLE parameter_study_trials (
    study_id TEXT NOT NULL REFERENCES parameter_studies(study_id),
    candidate_digest TEXT NOT NULL CHECK (
        length(candidate_digest) = 64
        AND candidate_digest NOT GLOB '*[^0-9a-f]*'
    ),
    configuration_json TEXT NOT NULL CHECK (
        json_valid(configuration_json) AND json_type(configuration_json) = 'object'
    ),
    first_search_round TEXT NOT NULL,
    proposal_sequence INTEGER NOT NULL CHECK (proposal_sequence >= 0),
    classification TEXT NOT NULL CHECK (
        classification IN ('IN_RANGE', 'BASELINE_ONLY')
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (study_id, candidate_digest)
);

CREATE TABLE parameter_study_bindings (
    binding_id TEXT PRIMARY KEY CHECK (
        length(binding_id) = 64 AND binding_id NOT GLOB '*[^0-9a-f]*'
    ),
    study_id TEXT NOT NULL REFERENCES parameter_studies(study_id),
    search_round TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('INNER_SCORE', 'OUTER_AUDIT', 'TERMINAL_HOLDOUT')
    ),
    fold_sequence INTEGER NOT NULL CHECK (fold_sequence >= 1),
    fold_window_json TEXT NOT NULL CHECK (
        json_valid(fold_window_json) AND json_type(fold_window_json) = 'object'
    ),
    task_json TEXT NOT NULL CHECK (
        json_valid(task_json) AND json_type(task_json) = 'object'
    ),
    task_digest TEXT NOT NULL CHECK (
        length(task_digest) = 64 AND task_digest NOT GLOB '*[^0-9a-f]*'
    ),
    dataset_snapshot_id TEXT NOT NULL CHECK (
        length(dataset_snapshot_id) = 64
        AND dataset_snapshot_id NOT GLOB '*[^0-9a-f]*'
    ),
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    submitted_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    state TEXT NOT NULL CHECK (
        state IN ('SUBMITTED', 'VERIFIED', 'FAILED', 'CONTESTED')
    ),
    metric_document_json TEXT CHECK (
        metric_document_json IS NULL OR (
            json_valid(metric_document_json)
            AND json_type(metric_document_json) = 'object'
        )

    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (study_id, candidate_digest)
        REFERENCES parameter_study_trials(study_id, candidate_digest),
    UNIQUE (study_id, search_round, candidate_digest, role, fold_sequence)
);

CREATE TABLE parameter_study_attempt_candidate_claims (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
    candidate_digest TEXT NOT NULL CHECK (
        length(candidate_digest) = 64
        AND candidate_digest NOT GLOB '*[^0-9a-f]*'
    ),
    configuration_json TEXT NOT NULL CHECK (
        json_valid(configuration_json) AND json_type(configuration_json) = 'object'
    ),
    claimed_at TEXT NOT NULL
);

CREATE INDEX idx_parameter_study_bindings_progress
ON parameter_study_bindings(study_id, search_round, role, state);

CREATE TRIGGER immutable_parameter_study_trials_update
BEFORE UPDATE ON parameter_study_trials BEGIN
    SELECT RAISE(ABORT, 'Study Trials are immutable');
END;

CREATE TRIGGER immutable_parameter_study_trials_delete
BEFORE DELETE ON parameter_study_trials BEGIN
    SELECT RAISE(ABORT, 'Study Trials are immutable');
END;

CREATE TRIGGER immutable_parameter_study_binding_identity
BEFORE UPDATE OF
    binding_id, study_id, search_round, candidate_digest, role, fold_sequence,
    fold_window_json, task_json, task_digest, dataset_snapshot_id,
    experiment_id, submitted_attempt_id, created_at
ON parameter_study_bindings BEGIN
    SELECT RAISE(ABORT, 'Study binding identity is immutable');
END;

CREATE TRIGGER immutable_parameter_study_bindings_delete
BEFORE DELETE ON parameter_study_bindings BEGIN
    SELECT RAISE(ABORT, 'Study bindings cannot be deleted');
END;

CREATE TRIGGER immutable_parameter_study_attempt_claims_update
BEFORE UPDATE ON parameter_study_attempt_candidate_claims BEGIN
    SELECT RAISE(ABORT, 'Attempt candidate claims are immutable');
END;

CREATE TRIGGER immutable_parameter_study_attempt_claims_delete
BEFORE DELETE ON parameter_study_attempt_candidate_claims BEGIN
    SELECT RAISE(ABORT, 'Attempt candidate claims are immutable');
END;
""",
)

STUDY_HOLDOUT_MIGRATION = CatalogMigration(
    version=8,
    applied_at="2026-08-28T00:00:00Z",
    sql="""
CREATE TABLE parameter_study_holdout_claims (
    study_id TEXT PRIMARY KEY REFERENCES parameter_studies(study_id),
    holdout_identity_digest TEXT NOT NULL CHECK (
        length(holdout_identity_digest) = 64
        AND holdout_identity_digest NOT GLOB '*[^0-9a-f]*'
    ),
    candidate_digest TEXT NOT NULL CHECK (
        length(candidate_digest) = 64
        AND candidate_digest NOT GLOB '*[^0-9a-f]*'
    ),
    binding_id TEXT NOT NULL UNIQUE CHECK (
        length(binding_id) = 64 AND binding_id NOT GLOB '*[^0-9a-f]*'
    ),
    effect_action_id TEXT NOT NULL UNIQUE CHECK (
        length(effect_action_id) = 86
        AND substr(effect_action_id, 1, 22) = 'study-internal:effect:'
        AND substr(effect_action_id, 23, 64) NOT GLOB '*[^0-9a-f]*'
    ),
    claimed_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_holdout_grant_per_study
ON parameter_study_holdout_ledger(study_id)
WHERE event_type = 'GRANTED';

CREATE UNIQUE INDEX one_holdout_access_per_study
ON parameter_study_holdout_ledger(study_id)
WHERE event_type = 'ACCESSED';

CREATE TRIGGER immutable_parameter_study_holdout_claims_update
BEFORE UPDATE ON parameter_study_holdout_claims BEGIN
    SELECT RAISE(ABORT, 'holdout claims are immutable');
END;

CREATE TRIGGER immutable_parameter_study_holdout_claims_delete
BEFORE DELETE ON parameter_study_holdout_claims BEGIN
    SELECT RAISE(ABORT, 'holdout claims are immutable');
END;
""",
)


def _exact(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StudyValidationError(f"{path} must be an object")
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise StudyValidationError(f"{path} has missing fields: {missing}")
    if unknown:
        raise StudyValidationError(f"{path} has unknown fields: {unknown}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise StudyValidationError(f"{path} must be a non-empty trimmed string")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_WEB_SAFE_INTEGER
    ):
        raise StudyValidationError(
            f"{path} must be an integer from {minimum} to {MAX_WEB_SAFE_INTEGER}"
        )
    return value


def _parameters(
    schema: dict[str, Any],
    defaults: dict[str, Any],
    supplied: Any,
    path: str,
) -> dict[str, Any]:
    if not isinstance(supplied, dict):
        raise StudyValidationError(f"{path} must be an object")
    unknown = sorted(set(supplied) - set(schema["properties"]))
    if unknown:
        raise StudyValidationError(f"{path} has unknown parameters: {unknown}")
    try:
        return validate_parameters(schema, defaults | supplied)
    except SchemaValidationError as exc:
        raise StudyValidationError(f"{path} is invalid: {exc}") from exc


def _scalar(
    property_schema: dict[str, Any],
    value: Any,
    path: str,
) -> Any:
    schema = {
        "type": "object",
        "properties": {"value": property_schema},
        "required": ["value"],
        "additionalProperties": False,
    }
    try:
        return validate_parameters(schema, {"value": value})["value"]
    except SchemaValidationError as exc:
        raise StudyValidationError(f"{path} is invalid: {exc}") from exc


def _fold_window(
    sessions: list[str],
    scoring_start: int,
    scoring_end: int,
    *,
    allowed_start: str,
    purge_sessions: int,
    role: str,
    account_policy: str,
) -> dict[str, Any]:
    training_end = scoring_start - purge_sessions - 1
    contract_sessions = (
        sessions if sessions[0] == allowed_start else [allowed_start, *sessions]
    )
    return normalize_fold_window(
        {
            "allowed_start": allowed_start,
            "training_through": sessions[training_end],
            "available_through": sessions[scoring_end],
            "scoring_start": sessions[scoring_start],
            "scoring_end": sessions[scoring_end],
            "role": role,
            "information_interval": INFORMATION_INTERVAL,
            "account_policy": account_policy,
        },
        contract_sessions,
    )


def _inner_folds(
    sessions: list[str],
    *,
    count: int,
    scoring_sessions: int,
    minimum_training_sessions: int,
    purge_sessions: int,
    account_policy: str,
    allowed_start: str,
) -> list[dict[str, Any]]:
    first_scoring = len(sessions) - count * scoring_sessions
    if first_scoring - purge_sessions < minimum_training_sessions:
        raise StudyValidationError(
            "validation range cannot contain the requested inner folds"
        )
    return [
        _fold_window(
            sessions,
            first_scoring + index * scoring_sessions,
            first_scoring + (index + 1) * scoring_sessions - 1,
            allowed_start=allowed_start,
            purge_sessions=purge_sessions,
            role="INNER_SCORE",
            account_policy=account_policy,
        )
        for index in range(count)
    ]


def _validation_plan(
    value: Any,
    holdout_value: Any,
    sessions: list[str],
    *,
    allowed_start: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = _exact(value, VALIDATION_FIELDS, "study.validation")
    holdout = _exact(holdout_value, HOLDOUT_FIELDS, "study.holdout")
    outer_folds = _integer(validation["outer_folds"], "study.validation.outer_folds", minimum=1)
    inner_folds = _integer(validation["inner_folds"], "study.validation.inner_folds", minimum=1)
    scoring_sessions = _integer(
        validation["scoring_sessions"],
        "study.validation.scoring_sessions",
        minimum=1,
    )
    minimum_training_sessions = _integer(
        validation["minimum_training_sessions"],
        "study.validation.minimum_training_sessions",
        minimum=1,
    )
    purge_sessions = _integer(
        validation["purge_sessions"],
        "study.validation.purge_sessions",
    )
    account_policy = _string(
        validation["outer_account_policy"],
        "study.validation.outer_account_policy",
    )
    if account_policy != "FORCE_FLAT_WITH_COST":
        raise StudyValidationError(
            "study.validation.outer_account_policy must be FORCE_FLAT_WITH_COST"
        )
    holdout_sessions = _integer(
        holdout["sessions"], "study.holdout.sessions", minimum=1
    )
    if holdout["pass_rule"] != "POLICY_CONSTRAINTS":
        raise StudyValidationError(
            "study.holdout.pass_rule must be POLICY_CONSTRAINTS"
        )
    development_count = len(sessions) - holdout_sessions
    first_outer_scoring = development_count - outer_folds * scoring_sessions
    if first_outer_scoring - purge_sessions < minimum_training_sessions:
        raise StudyValidationError(
            "dataset range cannot contain the requested outer folds and holdout"
        )

    outer_rounds: list[dict[str, Any]] = []
    for index in range(outer_folds):
        scoring_start = first_outer_scoring + index * scoring_sessions
        scoring_end = scoring_start + scoring_sessions - 1
        training = sessions[: scoring_start - purge_sessions]
        outer_rounds.append(
            {
                "round": index + 1,
                "inner_folds": _inner_folds(
                    training,
                    count=inner_folds,
                    scoring_sessions=scoring_sessions,
                    minimum_training_sessions=minimum_training_sessions,
                    purge_sessions=purge_sessions,
                    account_policy=account_policy,
                    allowed_start=allowed_start,
                ),
                "outer_audit": _fold_window(
                    sessions,
                    scoring_start,
                    scoring_end,
                    allowed_start=allowed_start,
                    purge_sessions=purge_sessions,
                    role="OUTER_AUDIT",
                    account_policy=account_policy,
                ),
            }
        )

    development = sessions[:development_count]
    final_search_round = {
        "inner_folds": _inner_folds(
            development,
            count=inner_folds,
            scoring_sessions=scoring_sessions,
            minimum_training_sessions=minimum_training_sessions,
            purge_sessions=purge_sessions,
            account_policy=account_policy,
            allowed_start=allowed_start,
        )
    }
    normalized_validation = {
        "rules": {
            "outer_folds": outer_folds,
            "inner_folds": inner_folds,
            "scoring_sessions": scoring_sessions,
            "minimum_training_sessions": minimum_training_sessions,
            "purge_sessions": purge_sessions,
            "outer_account_policy": account_policy,
        },
        "outer_rounds": outer_rounds,
        "final_search_round": final_search_round,
    }
    normalized_holdout = {
        "sessions": holdout_sessions,
        "pass_rule": "POLICY_CONSTRAINTS",
        "fold_window": _fold_window(
            sessions,
            development_count,
            len(sessions) - 1,
            allowed_start=allowed_start,
            purge_sessions=purge_sessions,
            role="TERMINAL_HOLDOUT",
            account_policy=account_policy,
        ),
    }
    return normalized_validation, normalized_holdout


def _resolve_template(
    connection: sqlite3.Connection,
    selector_value: Any,
) -> dict[str, Any]:
    selector = _exact(selector_value, TEMPLATE_FIELDS, "study.template")
    name = _string(selector["name"], "study.template.name")
    version = _string(selector["version"], "study.template.version")
    row = connection.execute(
        "SELECT * FROM templates WHERE name = ? AND version = ?",
        (name, version),
    ).fetchone()
    if row is None:
        raise StudyValidationError(f"unknown template: {name}@{version}")
    schema = json.loads(row["parameter_schema_json"])
    defaults = json.loads(row["defaults_json"])
    parameters = _parameters(
        schema,
        defaults,
        selector["parameters"],
        "study.template.parameters",
    )
    return {
        "name": name,
        "version": version,
        "content_digest": row["content_digest"],
        "slots": json.loads(row["slots_json"]),
        "parameter_schema": schema,
        "defaults": defaults,
        "parameters": parameters,
    }


def _resolve_operator(
    connection: sqlite3.Connection,
    selector_value: Any,
    slot: str,
) -> dict[str, Any]:
    selector = _exact(selector_value, OPERATOR_FIELDS, f"study.operators.{slot}")
    operator_id = _string(
        selector["operator_id"], f"study.operators.{slot}.operator_id"
    )
    requested_version = _string(
        selector["version"], f"study.operators.{slot}.version"
    )
    latest = None
    if requested_version == "latest":
        latest = connection.execute(
            """
            SELECT v.*
            FROM operator_latest AS l
            JOIN operator_versions AS v
              ON v.operator_id = l.operator_id AND v.version = l.version
            WHERE l.operator_id = ? AND v.status = 'PUBLISHED'
            """,
            (operator_id,),
        ).fetchone()
        if latest is None:
            raise StudyValidationError(f"unknown published operator: {operator_id}@latest")
        selected = latest
    else:
        selected = connection.execute(
            """
            SELECT * FROM operator_versions
            WHERE operator_id = ? AND version = ? AND status = 'PUBLISHED'
            """,
            (operator_id, requested_version),
        ).fetchone()
        if selected is None:
            raise StudyValidationError(
                f"unknown published operator: {operator_id}@{requested_version}"
            )
    operator = connection.execute(
        "SELECT slot FROM operators WHERE operator_id = ?",
        (operator_id,),
    ).fetchone()
    if operator is None or operator["slot"] != slot:
        actual = "unknown" if operator is None else operator["slot"]
        raise StudyValidationError(
            f"operator {operator_id} belongs to slot {actual}, not {slot}"
        )
    schema = json.loads(selected["parameter_schema_json"])
    defaults = json.loads(selected["defaults_json"])
    parameters = _parameters(
        schema,
        defaults,
        selector["parameters"],
        f"study.operators.{slot}.parameters",
    )
    frozen = {
        "operator_id": operator_id,
        "slot": slot,
        "selector_mode": "LATEST" if requested_version == "latest" else "EXPLICIT",
        "requested_version": requested_version,
        "resolved_version": selected["version"],
        "content_digest": selected["content_digest"],
        "parameter_schema": schema,
        "defaults": defaults,
        "parameters": parameters,
    }
    if latest is not None:
        frozen["latest_version_at_preview"] = latest["version"]
        frozen["latest_content_digest_at_preview"] = latest["content_digest"]
    return frozen


def _search(
    value: Any,
    template: dict[str, Any],
    operators: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    search = _exact(value, SEARCH_FIELDS, "study.search")
    suggester = _string(search["suggester"], "study.search.suggester")
    if suggester not in {"GRID", "SEEDED_RANDOM"}:
        raise StudyValidationError(
            "study.search.suggester must be GRID or SEEDED_RANDOM"
        )
    suggester_version = _string(
        search["suggester_version"], "study.search.suggester_version"
    )
    if suggester_version != "1.0.0":
        raise StudyValidationError(
            "study.search.suggester_version must be 1.0.0"
        )
    seed = _integer(search["seed"], "study.search.seed")
    unique_trial_budget = _integer(
        search["unique_trial_budget"],
        "study.search.unique_trial_budget",
        minimum=1,
    )
    max_suggestions = _integer(
        search["max_suggestions"],
        "study.search.max_suggestions",
        minimum=1,
    )
    if max_suggestions < unique_trial_budget:
        raise StudyValidationError(
            "study.search.max_suggestions cannot be less than unique_trial_budget"
        )
    if not isinstance(search["space"], dict) or not search["space"]:
        raise StudyValidationError("study.search.space must be a non-empty object")

    normalized_space: dict[str, dict[str, list[Any]]] = {}
    for path, definition_value in sorted(search["space"].items()):
        if not isinstance(path, str):
            raise StudyValidationError("study.search.space paths must be strings")
        definition = _exact(
            definition_value,
            {"values"},
            f"study.search.space.{path}",
        )
        values = definition["values"]
        if not isinstance(values, list) or not values:
            raise StudyValidationError(
                f"study.search.space.{path}.values must be a non-empty array"
            )
        parts = path.split("/")
        if len(parts) == 4 and parts[:2] == ["", "operators"]:
            slot, parameter = parts[2:]
            if slot == "cost":
                raise StudyValidationError(
                    "study.search.space cannot search cost parameters"
                )
            if slot == "report":
                raise StudyValidationError(
                    "study.search.space cannot search report parameters"
                )
            try:
                property_schema = operators[slot]["parameter_schema"]["properties"][
                    parameter
                ]
            except KeyError as exc:
                raise StudyValidationError(
                    f"study.search.space path is not owned by a frozen operator: {path}"
                ) from exc
        elif len(parts) == 3 and parts[:2] == ["", "template"]:
            raise StudyValidationError(
                "study.search.space cannot search template protocol parameters"
            )
        else:
            raise StudyValidationError(
                f"study.search.space path has invalid syntax: {path}"
            )
        normalized_values = [
            _scalar(
                property_schema,
                item,
                f"study.search.space.{path}.values[{index}]",
            )
            for index, item in enumerate(values)
        ]
        identities = [canonical_json_bytes(item) for item in normalized_values]
        if len(set(identities)) != len(identities):
            raise StudyValidationError(
                f"study.search.space.{path}.values must be unique after normalization"
            )
        normalized_space[path] = {"values": normalized_values}

    candidate_capacity = prod(
        len(definition["values"]) for definition in normalized_space.values()
    )
    return {
        "suggester": suggester,
        "suggester_version": suggester_version,
        "seed": seed,
        "unique_trial_budget": unique_trial_budget,
        "max_suggestions": max_suggestions,
        "space": normalized_space,
        "candidate_capacity": candidate_capacity,
    }


def _evaluation(value: Any) -> dict[str, Any]:
    evaluation = _exact(value, EVALUATION_FIELDS, "study.evaluation")
    policy_id = _string(evaluation["policy_id"], "study.evaluation.policy_id")
    requested_version = _string(
        evaluation["version"], "study.evaluation.version"
    )
    if policy_id != "robust_walk_forward":
        raise StudyValidationError(f"unknown evaluation policy: {policy_id}")
    if requested_version not in {"latest", "1.0.0"}:
        raise StudyValidationError(
            f"unknown evaluation policy: {policy_id}@{requested_version}"
        )
    parameter_schema = deepcopy(EVALUATION_PARAMETER_SCHEMA)
    defaults = deepcopy(EVALUATION_DEFAULTS)
    parameters = _parameters(
        parameter_schema,
        defaults,
        evaluation["parameters"],
        "study.evaluation.parameters",
    )
    return {
        "policy_id": policy_id,
        "selector_mode": "LATEST" if requested_version == "latest" else "EXPLICIT",
        "requested_version": requested_version,
        "resolved_version": "1.0.0",
        "content_digest": EVALUATION_POLICY_DIGEST,
        "parameter_schema": parameter_schema,
        "defaults": defaults,
        "parameters": parameters,
        "manifest": deepcopy(EVALUATION_POLICY_IDENTITY),
    }


def _lineage(value: Any) -> dict[str, Any]:
    lineage = _exact(value, LINEAGE_FIELDS, "study.lineage")
    parents = lineage["parent_study_ids"]
    if not isinstance(parents, list) or any(
        not isinstance(parent, str) or STUDY_ID.fullmatch(parent) is None
        for parent in parents
    ):
        raise StudyValidationError(
            "study.lineage.parent_study_ids must contain Study digests"
        )
    if len(set(parents)) != len(parents):
        raise StudyValidationError(
            "study.lineage.parent_study_ids must be unique"
        )
    prior_count = _integer(
        lineage["prior_unique_candidate_count"],
        "study.lineage.prior_unique_candidate_count",
    )
    if type(lineage["is_complete"]) is not bool:
        raise StudyValidationError("study.lineage.is_complete must be boolean")
    return {
        "parent_study_ids": sorted(parents),
        "prior_unique_candidate_count": prior_count,
        "is_complete": lineage["is_complete"],
    }


def _normalize_request_for_plan(
    spec: Any,
    frozen_plan: dict[str, Any],
) -> dict[str, Any]:
    study = _exact(spec, STUDY_FIELDS, "study")
    if type(study["schema_version"]) is not int or study["schema_version"] != 1:
        raise StudyValidationError("study.schema_version must be integer 1")

    dataset = _exact(study["dataset"], DATASET_FIELDS, "study.dataset")
    _string(dataset["start"], "study.dataset.start")
    _string(dataset["end"], "study.dataset.end")
    normalized_dataset = {
        "dataset_id": _string(dataset["dataset_id"], "study.dataset.dataset_id"),
        "start": frozen_plan["dataset"]["requested_start"],
        "end": frozen_plan["dataset"]["requested_end"],
    }

    template_selector = _exact(
        study["template"], TEMPLATE_FIELDS, "study.template"
    )
    normalized_template = {
        "name": _string(template_selector["name"], "study.template.name"),
        "version": _string(template_selector["version"], "study.template.version"),
        "parameters": _parameters(
            frozen_plan["template"]["parameter_schema"],
            frozen_plan["template"]["defaults"],
            template_selector["parameters"],
            "study.template.parameters",
        ),
    }

    expected_slots = set(frozen_plan["template"]["slots"])
    operator_selectors = _exact(
        study["operators"], expected_slots, "study.operators"
    )
    normalized_operators: dict[str, dict[str, Any]] = {}
    for slot in frozen_plan["template"]["slots"]:
        selector = _exact(
            operator_selectors[slot],
            OPERATOR_FIELDS,
            f"study.operators.{slot}",
        )
        operator = frozen_plan["operators"][slot]
        normalized_operators[slot] = {
            "operator_id": _string(
                selector["operator_id"],
                f"study.operators.{slot}.operator_id",
            ),
            "version": _string(
                selector["version"],
                f"study.operators.{slot}.version",
            ),
            "parameters": _parameters(
                operator["parameter_schema"],
                operator["defaults"],
                selector["parameters"],
                f"study.operators.{slot}.parameters",
            ),
        }

    search = _search(
        study["search"],
        frozen_plan["template"],
        frozen_plan["operators"],
    )
    validation = _exact(
        study["validation"], VALIDATION_FIELDS, "study.validation"
    )
    normalized_validation = {
        "outer_folds": _integer(
            validation["outer_folds"],
            "study.validation.outer_folds",
            minimum=1,
        ),
        "inner_folds": _integer(
            validation["inner_folds"],
            "study.validation.inner_folds",
            minimum=1,
        ),
        "scoring_sessions": _integer(
            validation["scoring_sessions"],
            "study.validation.scoring_sessions",
            minimum=1,
        ),
        "minimum_training_sessions": _integer(
            validation["minimum_training_sessions"],
            "study.validation.minimum_training_sessions",
            minimum=1,
        ),
        "purge_sessions": _integer(
            validation["purge_sessions"],
            "study.validation.purge_sessions",
        ),
        "outer_account_policy": _string(
            validation["outer_account_policy"],
            "study.validation.outer_account_policy",
        ),
    }
    if normalized_validation["outer_account_policy"] != "FORCE_FLAT_WITH_COST":
        raise StudyValidationError(
            "study.validation.outer_account_policy must be FORCE_FLAT_WITH_COST"
        )

    evaluation = _exact(
        study["evaluation"], EVALUATION_FIELDS, "study.evaluation"
    )
    normalized_evaluation = {
        "policy_id": _string(
            evaluation["policy_id"], "study.evaluation.policy_id"
        ),
        "version": _string(
            evaluation["version"], "study.evaluation.version"
        ),
        "parameters": _parameters(
            frozen_plan["evaluation"]["parameter_schema"],
            frozen_plan["evaluation"]["defaults"],
            evaluation["parameters"],
            "study.evaluation.parameters",
        ),
    }
    holdout = _exact(study["holdout"], HOLDOUT_FIELDS, "study.holdout")
    normalized_holdout = {
        "sessions": _integer(
            holdout["sessions"], "study.holdout.sessions", minimum=1
        ),
        "pass_rule": _string(
            holdout["pass_rule"], "study.holdout.pass_rule"
        ),
    }
    return {
        "schema_version": 1,
        "dataset": normalized_dataset,
        "template": normalized_template,
        "operators": normalized_operators,
        "search": {key: search[key] for key in SEARCH_FIELDS},
        "validation": normalized_validation,
        "evaluation": normalized_evaluation,
        "holdout": normalized_holdout,
        "lineage": _lineage(study["lineage"]),
    }


def _action_request_digest(
    normalized_request: dict[str, Any],
    expected_preview_digest: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "expected_preview_digest": expected_preview_digest,
                "study": normalized_request,
            }
        )
    ).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _strict_json_value(value: Any, path: str) -> Any:
    if not isinstance(value, str):
        raise RuntimeError(f"{path} is not JSON text")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    try:
        return json.loads(
            value,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {constant}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"{path} is not strict JSON") from exc


def _strict_json_object(value: Any, path: str) -> dict[str, Any]:
    parsed = _strict_json_value(value, path)
    if type(parsed) is not dict:
        raise RuntimeError(f"{path} must be a JSON object")
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _utc_datetime(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RuntimeError(f"{path} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeError(f"{path} must be a UTC timestamp") from exc
    if parsed.tzinfo != UTC:
        raise RuntimeError(f"{path} must be a UTC timestamp")
    return parsed


@dataclass(frozen=True)
class _StudyReadiness:
    classification: str
    response: dict[str, Any] | None
    frozen_plan: dict[str, Any]
    effect: dict[str, Any] | None = None
    effect_digest: str | None = None
    requires_lease: bool = False
    discoverable: bool = False
    reconciliation_only: bool = False
    dispatch_in_flight: bool = False


def _validate_public_action_id(action_id: str) -> None:
    if not isinstance(action_id, str) or ACTION_ID.fullmatch(action_id) is None:
        raise StudyValidationError("action_id has invalid syntax")
    if action_id.startswith(INTERNAL_ACTION_PREFIX):
        raise StudyValidationError("action_id uses a reserved internal namespace")


def _holdout_identity_digest(frozen_plan: dict[str, Any]) -> str:
    dataset = frozen_plan["dataset"]
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "dataset": {
                    "instrument": dataset["instrument"],
                    "snapshot_id": dataset["snapshot_id"],
                    "canonical_sha256": dataset["canonical_sha256"],
                },
                "fold_window": frozen_plan["holdout"]["fold_window"],
            }
        )
    ).hexdigest()


class ParameterStudy:
    """Freeze and persist Parameter Studies behind one public behavior seam."""

    def __init__(
        self,
        catalog: Catalog,
        *,
        datasets: DatasetService,
        experiments: ExperimentService,
        release_locator: str,
        clock: Callable[[], datetime] | None = None,
        coordinator_id: str | None = None,
        lease_duration_seconds: int = 30,
        dataset_slice_factory: ExecutionDatasetSliceFactory | None = None,
        effect_executor: (
            Callable[[dict[str, Any], str], dict[str, Any]] | None
        ) = None,
    ):
        if datasets.catalog is not catalog or experiments.catalog is not catalog:
            raise ValueError("ParameterStudy dependencies must share one Catalog")
        if experiments.datasets is not datasets:
            raise ValueError(
                "ParameterStudy and ExperimentService must share one DatasetService"
            )
        self.catalog = catalog
        self.datasets = datasets
        self.experiments = experiments
        self.dataset_slice_factory = (
            ExecutionDatasetSliceFactory(catalog.state_root)
            if dataset_slice_factory is None
            else dataset_slice_factory
        )
        self.release_locator = _string(release_locator, "release_locator")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.coordinator_id = (
            f"coordinator-{uuid.uuid4().hex}"
            if coordinator_id is None
            else coordinator_id
        )
        if (
            not isinstance(self.coordinator_id, str)
            or ACTION_ID.fullmatch(self.coordinator_id) is None
        ):
            raise ValueError("coordinator_id has invalid syntax")
        if type(lease_duration_seconds) is not int or lease_duration_seconds < 1:
            raise ValueError("lease_duration_seconds must be a positive integer")
        if effect_executor is not None and not callable(effect_executor):
            raise ValueError("effect_executor must be callable")
        self.lease_duration_seconds = lease_duration_seconds
        self.effect_executor = effect_executor
        self.metric_documents = MetricDocumentFactory(catalog.state_root)
        self.evaluation_policy = RobustWalkForwardPolicy()
        self._instance_nonce = uuid.uuid4().hex
        self._advance_lock = threading.Lock()
        self.catalog.apply_migrations(
            [
                STUDY_MIGRATION,
                STUDY_EVALUATION_MIGRATION,
                STUDY_ORCHESTRATION_MIGRATION,
                STUDY_HOLDOUT_MIGRATION,
            ]
        )

    @classmethod
    def from_experiments(
        cls,
        catalog: Catalog,
        *,
        experiments: ExperimentService,
        release_locator: str,
        clock: Callable[[], datetime] | None = None,
        coordinator_id: str | None = None,
        lease_duration_seconds: int = 30,
        dataset_slice_factory: ExecutionDatasetSliceFactory | None = None,
        effect_executor: (
            Callable[[dict[str, Any], str], dict[str, Any]] | None
        ) = None,
    ) -> ParameterStudy:
        """Compose a Study service from the Experiment service's shared graph."""

        if experiments.datasets is None:
            raise ValueError(
                "ExperimentService must have a DatasetService for ParameterStudy"
            )
        return cls(
            catalog,
            datasets=experiments.datasets,
            experiments=experiments,
            release_locator=release_locator,
            clock=clock,
            coordinator_id=coordinator_id,
            lease_duration_seconds=lease_duration_seconds,
            dataset_slice_factory=dataset_slice_factory,
            effect_executor=effect_executor,
        )

    def _clock_now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("ParameterStudy clock must return an aware datetime")
        return value.astimezone(UTC)

    def _now(self) -> str:
        return (
            self._clock_now()
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _database_now(connection: sqlite3.Connection) -> datetime:
        value = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]
        return _utc_datetime(value, "database clock")

    def _owner_nonce(self, study_id: str, fencing_token: int) -> str:
        return _digest(
            {
                "fencing_token": fencing_token,
                "instance_nonce": self._instance_nonce,
                "study_id": study_id,
            }
        )[:32]

    def _resolve_external_inputs(self, spec: Any) -> dict[str, Any]:
        study = _exact(spec, STUDY_FIELDS, "study")
        if type(study["schema_version"]) is not int or study["schema_version"] != 1:
            raise StudyValidationError("study.schema_version must be integer 1")
        dataset_selector = _exact(study["dataset"], DATASET_FIELDS, "study.dataset")
        dataset_id = _string(
            dataset_selector["dataset_id"], "study.dataset.dataset_id"
        )
        start = _string(dataset_selector["start"], "study.dataset.start")
        end = _string(dataset_selector["end"], "study.dataset.end")
        try:
            dataset = self.datasets.resolve(dataset_id, start, end)
            sessions = self.datasets.sessions(
                dataset_id,
                dataset["effective_start"],
                dataset["effective_end"],
            )
        except DatasetResolutionError as exc:
            raise StudyValidationError(f"study.dataset is invalid: {exc}") from exc
        if not sessions:
            raise StudyValidationError("study.dataset contains no trading sessions")
        if len(sessions) > MAX_STUDY_SESSIONS:
            raise StudyValidationError(
                f"study.dataset exceeds {MAX_STUDY_SESSIONS} sessions"
            )

        execution_identity = deepcopy(self.experiments.execution_identity)
        try:
            canonical_json_bytes(execution_identity)
        except SchemaValidationError as exc:
            raise StudyValidationError("execution identity is invalid") from exc

        return {
            "study": study,
            "dataset": dataset,
            "sessions": sessions,
            "execution_identity": execution_identity,
        }

    def _freeze_resolved_plan(
        self,
        resolved: dict[str, Any],
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        study = resolved["study"]
        template = _resolve_template(connection, study["template"])
        template_parameters = template["parameters"]
        if template_parameters["initial_capital_cny"] <= 0:
            raise StudyValidationError(
                "study.template.parameters.initial_capital_cny must be positive"
            )
        if (
            template_parameters["evaluation_start"]
            != resolved["dataset"]["requested_start"]
            or template_parameters["evaluation_end"]
            != resolved["dataset"]["requested_end"]
        ):
            raise StudyValidationError(
                "study.template evaluation dates must match the selected dataset range"
            )
        expected_slots = set(template["slots"])
        operator_selectors = _exact(
            study["operators"], expected_slots, "study.operators"
        )
        operators: dict[str, dict[str, Any]] = {}
        for slot in template["slots"]:
            operators[slot] = _resolve_operator(
                connection,
                operator_selectors[slot],
                slot,
            )
        search = _search(study["search"], template, operators)
        evaluation = _evaluation(study["evaluation"])
        validation, holdout = _validation_plan(
            study["validation"],
            study["holdout"],
            resolved["sessions"],
            allowed_start=resolved["dataset"]["snapshot_data_start"],
        )
        lineage = _lineage(study["lineage"])

        dataset = resolved["dataset"]
        frozen_dataset = {
            key: dataset[key]
            for key in (
                "dataset_id",
                "name",
                "instrument",
                "provider",
                "market",
                "currency",
                "adjustment",
                "requested_start",
                "requested_end",
                "effective_start",
                "effective_end",
                "snapshot_id",
                "canonical_sha256",
                "snapshot_data_start",
                "snapshot_data_end",
                "lineage",
            )
        }
        frozen_plan = {
            "schema_version": 1,
            "dataset": frozen_dataset,
            "template": template,
            "operators": operators,
            "search": search,
            "validation": validation,
            "evaluation": evaluation,
            "metric_engine": deepcopy(METRIC_ENGINE_IDENTITY),
            "holdout": holdout,
            "lineage": lineage,
            "execution": {"identity": resolved["execution_identity"]},
        }
        frozen_plan["normalized_request"] = _normalize_request_for_plan(
            study,
            frozen_plan,
        )
        preview_digest = hashlib.sha256(
            canonical_json_bytes(frozen_plan)
        ).hexdigest()
        return {
            "preview_digest": preview_digest,
            "frozen_plan": frozen_plan,
        }

    def preview(self, spec: Any) -> dict[str, Any]:
        resolved = self._resolve_external_inputs(spec)
        with self.catalog.transaction() as connection:
            preview = self._freeze_resolved_plan(resolved, connection)
        minimum_bindings = 0
        selection_dependent_bindings = 1  # One terminal holdout, if authorized.
        round_counts = []
        for search_round, inner_folds, outer_audit in self._selection_rounds(
            preview["frozen_plan"]
        ):
            candidate_count = len(
                self._round_candidates(
                    preview["preview_digest"],
                    preview["frozen_plan"],
                    search_round,
                )
            )
            minimum_round_bindings = candidate_count * len(inner_folds)
            conditional_round_bindings = minimum_round_bindings
            if outer_audit is not None:
                conditional_round_bindings += 1
                selection_dependent_bindings += 1
            minimum_bindings += minimum_round_bindings
            round_counts.append(
                {
                    "search_round": search_round,
                    "candidate_count": candidate_count,
                    "minimum_binding_count": minimum_round_bindings,
                    "conditional_maximum_binding_count": conditional_round_bindings,
                }
            )
        preview["execution_estimate"] = {
            "minimum_experiment_bindings": minimum_bindings,
            "conditional_maximum_experiment_bindings": (
                minimum_bindings + selection_dependent_bindings
            ),
            "selection_dependent_bindings": selection_dependent_bindings,
            "rounds": round_counts,
            "reuse_resolution": "CANONICAL_EXPERIMENT_IDENTITY_AT_DISPATCH",
        }
        return preview

    def creation_options(self) -> dict[str, Any]:
        """Return the published inputs accepted by the Study creation boundary."""
        template = self.catalog.template_detail("single_stock_daily_causal", "1")
        operators = []
        for summary in self.catalog.list_operators():
            if summary["latest_version"] is None:
                continue
            latest = self.catalog.operator_detail(
                summary["operator_id"], summary["latest_version"]
            )
            operators.append(
                latest
                | {
                    "latest_version": summary["latest_version"],
                    "versions": self.catalog.list_operator_versions(
                        summary["operator_id"]
                    ),
                }
            )
        result = {
            "datasets": self.datasets.list_available(),
            "template": template,
            "operators": operators,
            "evaluation": deepcopy(EVALUATION_POLICY_IDENTITY),
        }
        if len(canonical_json_bytes(result)) > MAX_STUDY_DOCUMENT_BYTES:
            raise RuntimeError("Parameter Study creation options exceed bounded size")
        return result

    def _load_action(
        self,
        action_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | None:
        owns_connection = connection is None
        connection = connection or self.catalog.connect()
        try:
            return connection.execute(
                """
                SELECT a.*, s.frozen_plan_json
                FROM parameter_study_actions AS a
                JOIN parameter_studies AS s USING (study_id)
                WHERE a.action_id = ?
                """,
                (action_id,),
            ).fetchone()
        finally:
            if owns_connection:
                connection.close()

    @staticmethod
    def _action_header_matches(
        action: sqlite3.Row,
        *,
        operation: str,
        study_id: str,
        action_id: str,
        request_digest: str | None = None,
    ) -> bool:
        stored_digest = action["request_digest"]
        return (
            operation in ACTION_OPERATIONS
            and action["operation"] == operation
            and action["study_id"] == study_id
            and action["action_id"] == action_id
            and isinstance(stored_digest, str)
            and STUDY_ID.fullmatch(stored_digest) is not None
            and (request_digest is None or stored_digest == request_digest)
        )

    def _replay_submit_action(
        self,
        action: sqlite3.Row,
        spec: Any,
        expected_preview_digest: str,
    ) -> dict[str, Any]:
        conflict = {
            "status": "ACTION_CONFLICT",
            "action_id": action["action_id"],
        }
        study_id = action["study_id"]
        if (
            not isinstance(study_id, str)
            or STUDY_ID.fullmatch(study_id) is None
            or not self._action_header_matches(
                action,
                operation="SUBMIT",
                study_id=study_id,
                action_id=action["action_id"],
            )
        ):
            return conflict
        try:
            frozen_plan = _strict_json_object(
                action["frozen_plan_json"],
                "Parameter Study frozen plan",
            )
            normalized_request = _normalize_request_for_plan(
                spec,
                frozen_plan,
            )
            response = _strict_json_object(
                action["response_json"],
                "Parameter Study submit response",
            )
        except (RuntimeError, StudyValidationError):
            return conflict
        request_digest = _action_request_digest(
            normalized_request,
            expected_preview_digest,
        )
        if request_digest != action["request_digest"]:
            return conflict
        if (
            set(response) != {"status", "study_id", "preview_digest"}
            or response["status"] not in {"SUBMITTED", "DUPLICATE"}
            or response["study_id"] != study_id
            or response["preview_digest"] != study_id
        ):
            return conflict
        return response

    def submit(
        self,
        spec: Any,
        *,
        expected_preview_digest: str,
        action_id: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(expected_preview_digest, str)
            or STUDY_ID.fullmatch(expected_preview_digest) is None
        ):
            raise StudyValidationError(
                "expected_preview_digest must be a SHA-256 digest"
            )
        _validate_public_action_id(action_id)

        existing_action = self._load_action(action_id)
        if existing_action is not None:
            return self._replay_submit_action(
                existing_action,
                spec,
                expected_preview_digest,
            )

        for _ in range(MAX_DATASET_RESOLUTION_ATTEMPTS):
            resolved = self._resolve_external_inputs(spec)
            with self.datasets.guard_latest_resolution(
                resolved["dataset"]
            ) as resolution_is_current:
                if not resolution_is_current:
                    continue
                with self.catalog.transaction(immediate=True) as connection:
                    existing_action = self._load_action(action_id, connection)
                    if existing_action is not None:
                        return self._replay_submit_action(
                            existing_action,
                            spec,
                            expected_preview_digest,
                        )
                    preview = self._freeze_resolved_plan(resolved, connection)
                    if preview["preview_digest"] != expected_preview_digest:
                        return {
                            "status": "PREVIEW_STALE",
                            "expected_preview_digest": expected_preview_digest,
                            "current_preview_digest": preview["preview_digest"],
                        }
                    study_id = preview["preview_digest"]
                    request_digest = _action_request_digest(
                        preview["frozen_plan"]["normalized_request"],
                        expected_preview_digest,
                    )
                    now = self._now()
                    study_exists = connection.execute(
                        "SELECT 1 FROM parameter_studies WHERE study_id = ?",
                        (study_id,),
                    ).fetchone()
                    response = {
                        "status": "DUPLICATE" if study_exists else "SUBMITTED",
                        "study_id": study_id,
                        "preview_digest": preview["preview_digest"],
                    }
                    if study_exists is None:
                        connection.execute(
                            """
                            INSERT INTO parameter_studies(
                                study_id, preview_digest, request_digest, frozen_plan_json,
                                operational_metadata_json, phase, control_status,
                                selection_outcome, holdout_outcome, created_at, updated_at
                            ) VALUES (
                                ?, ?, ?, ?, ?, 'FROZEN', 'ACTIVE', 'NOT_DETERMINED',
                                'NOT_RUN', ?, ?
                            )
                            """,
                            (
                                study_id,
                                preview["preview_digest"],
                                request_digest,
                                canonical_json_bytes(preview["frozen_plan"]).decode(),
                                canonical_json_bytes(
                                    {"release_locator": self.release_locator}
                                ).decode(),
                                now,
                                now,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO parameter_study_events(
                                study_id, sequence, event_type, occurred_at, payload_json
                            ) VALUES (?, 1, 'STUDY_SUBMITTED', ?, ?)
                            """,
                            (
                                study_id,
                                now,
                                canonical_json_bytes(
                                    {
                                        "action_id": action_id,
                                        "preview_digest": preview["preview_digest"],
                                    }
                                ).decode(),
                            ),
                        )
                    connection.execute(
                        """
                        INSERT INTO parameter_study_actions(
                            action_id, operation, study_id, request_digest,
                            response_json, created_at
                        ) VALUES (?, 'SUBMIT', ?, ?, ?, ?)
                        """,
                        (
                            action_id,
                            study_id,
                            request_digest,
                            canonical_json_bytes(response).decode(),
                            now,
                        ),
                    )
                    return response

        preview = self.preview(spec)
        return {
            "status": "PREVIEW_STALE",
            "expected_preview_digest": expected_preview_digest,
            "current_preview_digest": preview["preview_digest"],
        }

    @staticmethod
    def _lease_action_id(study_id: str, fencing_token: int) -> str:
        return (
            f"{INTERNAL_ACTION_PREFIX}lease:{study_id}:{fencing_token}"
        )

    @staticmethod
    def _effect_action_id(effect_digest: str) -> str:
        return f"{INTERNAL_ACTION_PREFIX}effect:{effect_digest}"

    @staticmethod
    def _dispatch_action_id(effect_digest: str, fencing_token: int) -> str:
        return (
            f"{INTERNAL_ACTION_PREFIX}dispatch:"
            f"{effect_digest}:{fencing_token}"
        )

    @staticmethod
    def _receipt_action_id(effect_digest: str) -> str:
        return f"{INTERNAL_ACTION_PREFIX}receipt:{effect_digest}"

    @staticmethod
    def _lease_request_digest(
        study_id: str,
        lease: dict[str, Any],
    ) -> str:
        return _digest({"study_id": study_id, **lease})

    @staticmethod
    def _dispatch_request_digest(
        *,
        effect_action_id: str,
        effect_digest: str,
        dispatch_action_id: str,
        lease: dict[str, Any],
    ) -> str:
        return _digest(
            {
                "action_id": effect_action_id,
                "dispatch_action_id": dispatch_action_id,
                "effect_digest": effect_digest,
                "fencing_token": lease["fencing_token"],
                "owner": lease["owner"],
                "owner_nonce": lease["owner_nonce"],
            }
        )

    @staticmethod
    def _receipt_request_digest(response: dict[str, Any]) -> str:
        return _digest(
            {
                "action_id": response["action_id"],
                "dispatch_action_id": response["dispatch_action_id"],
                "effect_digest": _digest(response["effect"]),
                "fencing_token": response["fencing_token"],
                "owner": response["owner"],
                "owner_nonce": response["owner_nonce"],
                "result": response["result"],
            }
        )

    def _expected_effect_experiment_id(
        self,
        frozen_plan: dict[str, Any],
    ) -> str:
        try:
            normalized = frozen_plan["normalized_request"]
            task = {
                key: normalized[key]
                for key in ("schema_version", "dataset", "template", "operators")
            }
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Parameter Study frozen plan is invalid") from exc
        return self.experiments.preview_task(task)["experiment_id"]

    def _canonical_verified_metric_document(
        self,
        *,
        experiment_id: str,
        candidate_digest: str,
        candidate_configuration: dict[str, Any],
        fold_window: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Observe one Experiment without treating identity reuse as success."""

        detail = self.experiments.experiment_detail(experiment_id)
        if detail["has_divergent_attempt"]:
            raise RuntimeError(
                f"CONTESTED Experiment evidence cannot be evaluated: {experiment_id}"
            )
        canonical_attempt_id = detail["canonical_attempt_id"]
        if canonical_attempt_id is None:
            return None
        canonical = self.experiments.attempt_detail(canonical_attempt_id)
        if canonical["status"] != "SUCCEEDED":
            return None
        self.experiments.require_validated_study_dataset(experiment_id)
        return self.metric_documents.from_attempt(
            canonical,
            candidate_digest=candidate_digest,
            candidate_configuration=candidate_configuration,
            fold_window=fold_window,
        )

    def _valid_effect_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        action_id: str,
        frozen_plan: dict[str, Any],
    ) -> sqlite3.Row | None:
        if INTERNAL_EFFECT_ACTION_ID.fullmatch(action_id) is None:
            return None
        attempt = connection.execute(
            """
            SELECT attempt_id, experiment_id, action_id, sequence
            FROM attempts WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if attempt is None:
            return None
        expected_experiment_id = self._expected_effect_experiment_id(frozen_plan)
        expected_attempt_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "experiment_id": expected_experiment_id,
                    "action_id": action_id,
                    "sequence": 1,
                }
            )
        ).hexdigest()
        if (
            attempt["experiment_id"] != expected_experiment_id
            or attempt["sequence"] != 1
            or attempt["attempt_id"] != expected_attempt_id
        ):
            return None
        return attempt

    def _valid_lease_action(
        self,
        action: sqlite3.Row,
        study_id: str,
    ) -> dict[str, Any] | None:
        try:
            lease = _strict_json_object(
                action["response_json"],
                "Parameter Study lease response",
            )
            if set(lease) != {
                "owner",
                "owner_nonce",
                "expires_at",
                "fencing_token",
            }:
                return None
            fencing_token = lease["fencing_token"]
            if type(fencing_token) is not int or fencing_token < 1:
                return None
            if (
                not isinstance(lease["owner"], str)
                or ACTION_ID.fullmatch(lease["owner"]) is None
                or not isinstance(lease["owner_nonce"], str)
                or OWNER_NONCE.fullmatch(lease["owner_nonce"]) is None
            ):
                return None
            _utc_datetime(lease["expires_at"], "lease.expires_at")
            action_id = self._lease_action_id(study_id, fencing_token)
            request_digest = self._lease_request_digest(study_id, lease)
            if not self._action_header_matches(
                action,
                operation="COORDINATOR_LEASE",
                study_id=study_id,
                action_id=action_id,
                request_digest=request_digest,
            ):
                return None
        except RuntimeError:
            return None
        return lease

    def _latest_lease(
        self,
        connection: sqlite3.Connection,
        study_id: str,
    ) -> dict[str, Any] | None:
        rows = connection.execute(
            """
            SELECT *
            FROM parameter_study_actions
            WHERE study_id = ? AND operation = 'COORDINATOR_LEASE'
            """,
            (study_id,),
        ).fetchall()
        leases: list[dict[str, Any]] = []
        for row in rows:
            lease = self._valid_lease_action(row, study_id)
            if lease is None:
                raise RuntimeError("Parameter Study lease ledger is invalid")
            leases.append(lease)
        if not leases:
            return None
        return max(leases, key=lambda lease: lease["fencing_token"])

    @staticmethod
    def _has_binding_reconciliation(
        connection: sqlite3.Connection,
        study_id: str,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1
                FROM parameter_study_bindings AS b
                JOIN parameter_study_actions AS intent
                  ON intent.action_id = 'study-internal:effect:' || b.binding_id
                 AND intent.operation = 'EFFECT_INTENT'
                WHERE b.study_id = ? AND b.state = 'SUBMITTED'
                  AND NOT EXISTS (
                      SELECT 1 FROM parameter_study_actions AS receipt
                      WHERE receipt.action_id =
                            'study-internal:receipt:' || b.binding_id
                        AND receipt.operation = 'EFFECT_RECEIPT'
                  )
                LIMIT 1
                """,
                (study_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _trial_proposal_effect(
        study_id: str,
        frozen_plan: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            round_number = frozen_plan["validation"]["outer_rounds"][0][
                "round"
            ]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Parameter Study frozen plan is invalid") from exc
        if type(round_number) is not int or round_number < 1:
            raise RuntimeError("Parameter Study frozen plan is invalid")
        return {
            "schema_version": 1,
            "effect_type": "REQUEST_TRIAL_PROPOSAL",
            "study_id": study_id,
            "frozen_plan_digest": study_id,
            "search_round": {
                "kind": "OUTER",
                "round": round_number,
            },
        }

    @staticmethod
    def _holdout_effect(
        study_id: str,
        frozen_plan: dict[str, Any],
        candidate_digest: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "effect_type": "REQUEST_TERMINAL_HOLDOUT",
            "study_id": study_id,
            "frozen_plan_digest": study_id,
            "candidate_digest": candidate_digest,
            "fold_window": deepcopy(frozen_plan["holdout"]["fold_window"]),
            "authorization": "ONE_FROZEN_CHAMPION",
        }

    def _valid_intent_action(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        *,
        study_id: str,
        effect: dict[str, Any],
        effect_digest: str,
    ) -> dict[str, Any] | None:
        effect_action_id = self._effect_action_id(effect_digest)
        if not self._action_header_matches(
            action,
            operation="EFFECT_INTENT",
            study_id=study_id,
            action_id=effect_action_id,
            request_digest=effect_digest,
        ):
            return None
        try:
            response = _strict_json_object(
                action["response_json"],
                "Parameter Study effect intent response",
            )
        except RuntimeError:
            return None
        if (
            set(response)
            != {
                "status",
                "study_id",
                "action_id",
                "effect",
                "fencing_token",
                "owner",
                "owner_nonce",
            }
            or response["status"] != "EFFECT_PENDING"
            or response["study_id"] != study_id
            or response["action_id"] != effect_action_id
            or response["effect"] != effect
            or type(response["fencing_token"]) is not int
            or response["fencing_token"] < 1
            or not isinstance(response["owner"], str)
            or not isinstance(response["owner_nonce"], str)
            or OWNER_NONCE.fullmatch(response["owner_nonce"]) is None
        ):
            return None
        lease_action = self._load_action(
            self._lease_action_id(study_id, response["fencing_token"]),
            connection,
        )
        if lease_action is None:
            return None
        lease = self._valid_lease_action(lease_action, study_id)
        if (
            lease is None
            or lease["owner"] != response["owner"]
            or lease["owner_nonce"] != response["owner_nonce"]
        ):
            return None
        return response

    def _valid_dispatch_action(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        *,
        study_id: str,
        effect: dict[str, Any],
        effect_digest: str,
    ) -> dict[str, Any] | None:
        try:
            response = _strict_json_object(
                action["response_json"],
                "Parameter Study dispatch response",
            )
        except RuntimeError:
            return None
        if (
            set(response)
            != {
                "status",
                "study_id",
                "action_id",
                "dispatch_action_id",
                "effect",
                "fencing_token",
                "owner",
                "owner_nonce",
            }
            or response["status"] != "EFFECT_AUTHORIZED"
            or response["study_id"] != study_id
            or response["action_id"] != self._effect_action_id(effect_digest)
            or response["effect"] != effect
            or type(response["fencing_token"]) is not int
            or response["fencing_token"] < 1
            or not isinstance(response["owner"], str)
            or not isinstance(response["owner_nonce"], str)
            or OWNER_NONCE.fullmatch(response["owner_nonce"]) is None
        ):
            return None
        expected_action_id = self._dispatch_action_id(
            effect_digest,
            response["fencing_token"],
        )
        if response["dispatch_action_id"] != expected_action_id:
            return None
        lease = {
            "owner": response["owner"],
            "owner_nonce": response["owner_nonce"],
            "fencing_token": response["fencing_token"],
        }
        request_digest = self._dispatch_request_digest(
            effect_action_id=response["action_id"],
            effect_digest=effect_digest,
            dispatch_action_id=expected_action_id,
            lease=lease,
        )
        if not self._action_header_matches(
            action,
            operation="EFFECT_DISPATCH_AUTHORIZATION",
            study_id=study_id,
            action_id=expected_action_id,
            request_digest=request_digest,
        ):
            return None
        lease_action = self._load_action(
            self._lease_action_id(study_id, response["fencing_token"]),
            connection,
        )
        if lease_action is None:
            return None
        stored_lease = self._valid_lease_action(lease_action, study_id)
        if (
            stored_lease is None
            or stored_lease["owner"] != response["owner"]
            or stored_lease["owner_nonce"] != response["owner_nonce"]
        ):
            return None
        return response

    def _valid_receipt_action(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        *,
        study_id: str,
        effect: dict[str, Any],
        effect_digest: str,
        frozen_plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        receipt_action_id = self._receipt_action_id(effect_digest)
        try:
            response = _strict_json_object(
                action["response_json"],
                "Parameter Study effect receipt",
            )
        except RuntimeError:
            return None
        if (
            set(response)
            != {
                "status",
                "study_id",
                "action_id",
                "dispatch_action_id",
                "effect",
                "result",
                "fencing_token",
                "owner",
                "owner_nonce",
            }
            or response["status"] != "EFFECT_COMMITTED"
            or response["study_id"] != study_id
            or response["action_id"] != self._effect_action_id(effect_digest)
            or response["effect"] != effect
            or type(response["result"]) is not dict
            or type(response["fencing_token"]) is not int
            or response["fencing_token"] < 1
            or not isinstance(response["owner"], str)
            or not isinstance(response["owner_nonce"], str)
            or OWNER_NONCE.fullmatch(response["owner_nonce"]) is None
            or response["dispatch_action_id"]
            != self._dispatch_action_id(
                effect_digest,
                response["fencing_token"],
            )
        ):
            return None
        try:
            request_digest = self._receipt_request_digest(response)
        except SchemaValidationError:
            return None
        if not self._action_header_matches(
            action,
            operation="EFFECT_RECEIPT",
            study_id=study_id,
            action_id=receipt_action_id,
            request_digest=request_digest,
        ):
            return None
        dispatch_action = self._load_action(
            response["dispatch_action_id"],
            connection,
        )
        if dispatch_action is None:
            return None
        dispatch = self._valid_dispatch_action(
            connection,
            dispatch_action,
            study_id=study_id,
            effect=effect,
            effect_digest=effect_digest,
        )
        if (
            dispatch is None
            or dispatch["fencing_token"] != response["fencing_token"]
            or dispatch["owner"] != response["owner"]
            or dispatch["owner_nonce"] != response["owner_nonce"]
        ):
            return None
        attempt = connection.execute(
            """
            SELECT attempt_id, experiment_id, action_id, sequence
            FROM attempts WHERE action_id = ?
            """,
            (response["action_id"],),
        ).fetchone()
        if attempt is not None:
            valid_attempt = self._valid_effect_attempt(
                connection,
                action_id=response["action_id"],
                frozen_plan=frozen_plan,
            )
            result = response["result"]
            if (
                valid_attempt is None
                or result.get("experiment_id") != valid_attempt["experiment_id"]
                or result.get("attempt_id") != valid_attempt["attempt_id"]
            ):
                return None
        return response

    def _valid_drift_action(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        study_id: str,
    ) -> dict[str, Any] | None:
        action_id = f"{INTERNAL_ACTION_PREFIX}drift:{study_id}"
        try:
            response = _strict_json_object(
                action["response_json"],
                "Parameter Study drift response",
            )
        except RuntimeError:
            return None
        if (
            set(response)
            != {
                "status",
                "study_id",
                "frozen_execution_identity_digest",
                "current_execution_identity_digest",
                "fencing_token",
                "owner",
                "owner_nonce",
            }
            or response["status"] != "EXECUTION_IDENTITY_DRIFT"
            or response["study_id"] != study_id
            or not isinstance(
                response["frozen_execution_identity_digest"], str
            )
            or STUDY_ID.fullmatch(
                response["frozen_execution_identity_digest"]
            )
            is None
            or not isinstance(
                response["current_execution_identity_digest"], str
            )
            or STUDY_ID.fullmatch(
                response["current_execution_identity_digest"]
            )
            is None
            or type(response["fencing_token"]) is not int
            or response["fencing_token"] < 1
            or not isinstance(response["owner"], str)
            or not isinstance(response["owner_nonce"], str)
            or OWNER_NONCE.fullmatch(response["owner_nonce"]) is None
        ):
            return None
        request_digest = _digest(
            {
                "current_execution_identity_digest": response[
                    "current_execution_identity_digest"
                ],
                "frozen_execution_identity_digest": response[
                    "frozen_execution_identity_digest"
                ],
                "study_id": study_id,
            }
        )
        if not self._action_header_matches(
            action,
            operation="EXECUTION_IDENTITY_DRIFT",
            study_id=study_id,
            action_id=action_id,
            request_digest=request_digest,
        ):
            return None
        lease_action = self._load_action(
            self._lease_action_id(study_id, response["fencing_token"]),
            connection,
        )
        if lease_action is None:
            return None
        lease = self._valid_lease_action(lease_action, study_id)
        if (
            lease is None
            or lease["owner"] != response["owner"]
            or lease["owner_nonce"] != response["owner_nonce"]
        ):
            return None
        return response

    def _validate_study_projection(
        self,
        connection: sqlite3.Connection,
        study_id: str,
    ) -> None:
        study = connection.execute(
            "SELECT * FROM parameter_studies WHERE study_id = ?",
            (study_id,),
        ).fetchone()
        if study is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        frozen_plan = _strict_json_object(
            study["frozen_plan_json"],
            "Parameter Study frozen plan",
        )
        holdout_identity = _holdout_identity_digest(frozen_plan)

        events = connection.execute(
            """
            SELECT sequence, event_type, payload_json, length(payload_json) AS bytes
            FROM parameter_study_events
            WHERE study_id = ? ORDER BY sequence
            """,
            (study_id,),
        ).fetchall()
        evidence = connection.execute(
            """
            SELECT sequence, evidence_type, candidate_digest, payload_json,
                   length(payload_json) AS bytes
            FROM parameter_study_evidence
            WHERE study_id = ? ORDER BY sequence
            """,
            (study_id,),
        ).fetchall()
        holdout = connection.execute(
            """
            SELECT sequence, holdout_identity_digest, event_type, payload_json,
                   length(payload_json) AS bytes
            FROM parameter_study_holdout_ledger
            WHERE study_id = ? ORDER BY sequence
            """,
            (study_id,),
        ).fetchall()
        bindings = connection.execute(
            """
            SELECT binding_id, search_round, candidate_digest, role,
                   fold_sequence, state, metric_document_json,
                   experiment_id, attempt_id
            FROM parameter_study_bindings
            WHERE study_id = ?
            """,
            (study_id,),
        ).fetchall()
        if (
            not events
            or len(events) > MAX_STUDY_EVENTS
            or len(evidence) > MAX_STUDY_EVIDENCE
            or len(bindings) > MAX_STUDY_BINDINGS
            or len(holdout) > 16
            or [row["sequence"] for row in events]
            != list(range(1, len(events) + 1))
            or [row["sequence"] for row in evidence]
            != list(range(1, len(evidence) + 1))
            or [row["sequence"] for row in holdout]
            != list(range(1, len(holdout) + 1))
            or events[0]["event_type"] != "STUDY_SUBMITTED"
        ):
            raise RuntimeError("Parameter Study event or evidence ledger is invalid")
        documents = [*events, *evidence, *holdout]
        if any(
            row["bytes"] > MAX_STUDY_DOCUMENT_BYTES for row in documents
        ) or sum(row["bytes"] for row in documents) > MAX_STUDY_DETAIL_BYTES:
            raise RuntimeError("Parameter Study ledger exceeds bounded detail size")
        for row in documents:
            _strict_json_object(row["payload_json"], "Parameter Study ledger payload")
        if any(
            row["holdout_identity_digest"] != holdout_identity for row in holdout
        ):
            raise RuntimeError("holdout ledger identity does not match its Study")
        granted = [row for row in holdout if row["event_type"] == "GRANTED"]
        accessed = [row for row in holdout if row["event_type"] == "ACCESSED"]
        if (
            len(granted) > 1
            or len(accessed) > 1
            or (
                accessed
                and (
                    not granted
                    or granted[0]["sequence"] >= accessed[0]["sequence"]
                )
            )
        ):
            raise RuntimeError("holdout ledger has an illegal event order")

        evidence_items = [
            {
                "sequence": row["sequence"],
                "evidence_type": row["evidence_type"],
                "candidate_digest": row["candidate_digest"],
                "payload": _strict_json_object(
                    row["payload_json"],
                    "Parameter Study evidence",
                ),
            }
            for row in evidence
        ]
        champions = [
            item
            for item in evidence_items
            if item["evidence_type"] == "CHAMPION_FROZEN"
        ]
        holdout_outcomes = [
            item
            for item in evidence_items
            if item["evidence_type"] == "HOLDOUT_OUTCOME_RECORDED"
        ]
        selection_outcome = study["selection_outcome"]
        phase = study["phase"]
        holdout_outcome = study["holdout_outcome"]
        if selection_outcome == "NOT_DETERMINED":
            legal = (
                not champions
                and phase in {"FROZEN", "VALIDATING_SELECTION_PROCESS"}
                and holdout_outcome == "NOT_RUN"
                and not granted
                and not accessed
            )
        elif selection_outcome == "NO_ELIGIBLE_CANDIDATE":
            legal = (
                not champions
                and phase == "COMPLETED"
                and holdout_outcome == "NOT_RUN"
                and not granted
                and not accessed
            )
        else:
            legal = (
                len(champions) == 1
                and phase in {"HOLDOUT_READY", "HOLDOUT_RUNNING", "COMPLETED"}
                and len(granted) == 1
                and champions[0]["candidate_digest"]
                == champions[0]["payload"].get("candidate_digest")
            )
        if not legal:
            raise RuntimeError(
                "Parameter Study phase or selection projection disagrees with evidence"
            )
        if holdout_outcome == "NOT_RUN":
            if holdout_outcomes:
                raise RuntimeError(
                    "Parameter Study holdout projection disagrees with evidence"
                )
        elif (
            phase != "COMPLETED"
            or len(holdout_outcomes) != 1
            or len(accessed) != 1
            or holdout_outcomes[0]["payload"].get("outcome")
            != holdout_outcome
        ):
            raise RuntimeError(
                "Parameter Study holdout projection disagrees with evidence"
            )
        if phase == "HOLDOUT_RUNNING" and len(accessed) != 1:
            raise RuntimeError("HOLDOUT_RUNNING requires recorded access")

        claim = connection.execute(
            """
            SELECT * FROM parameter_study_holdout_claims
            WHERE study_id = ?
            """,
            (study_id,),
        ).fetchone()
        if claim is not None:
            if (
                len(champions) != 1
                or not granted
                or claim["holdout_identity_digest"] != holdout_identity
                or claim["candidate_digest"]
                != champions[0]["candidate_digest"]
                or claim["effect_action_id"]
                != self._effect_action_id(claim["binding_id"])
            ):
                raise RuntimeError("holdout claim disagrees with frozen authorization")
        if accessed and claim is None:
            raise RuntimeError("holdout access has no durable claim")

        metric_evidence = {
            item["payload"].get("binding_id"): item["payload"]
            for item in evidence_items
            if item["evidence_type"] == "METRIC_DOCUMENT_VERIFIED"
        }
        contested = {
            item["payload"].get("experiment_id")
            for item in evidence_items
            if item["evidence_type"] == "EVIDENCE_CONTESTED"
        }
        for binding in bindings:
            metric = metric_evidence.get(binding["binding_id"])
            if binding["state"] == "VERIFIED":
                if (
                    metric is None
                    or binding["metric_document_json"] is None
                    or metric.get("attempt_id") != binding["attempt_id"]
                ):
                    raise RuntimeError(
                        "verified binding projection disagrees with evidence"
                    )
            elif (
                binding["state"] == "CONTESTED"
                and binding["experiment_id"] not in contested
            ) or (
                binding["state"] == "SUBMITTED"
                and binding["metric_document_json"] is not None
            ):
                raise RuntimeError(
                    "Study binding projection disagrees with evidence"
                )

        outer_records = [
            item["payload"]
            for item in evidence_items
            if item["evidence_type"] == "OUTER_SELECTION_RECORDED"
        ]
        for outer in outer_records:
            search_round = outer.get("search_round")
            round_bindings = [
                binding
                for binding in bindings
                if binding["search_round"] == search_round
            ]
            inner = [
                binding
                for binding in round_bindings
                if binding["role"] == "INNER_SCORE"
            ]
            selected = outer.get("selected_candidate_digest")
            if not inner or any(
                binding["state"] not in {"VERIFIED", "FAILED"}
                for binding in inner
            ):
                raise RuntimeError("outer selection has partial inner-fold evidence")
            if selected is not None and (
                any(binding["state"] != "VERIFIED" for binding in inner)
                or not any(
                    binding["role"] == "OUTER_AUDIT"
                    and binding["candidate_digest"] == selected
                    and binding["state"] == "VERIFIED"
                    for binding in round_bindings
                )
            ):
                raise RuntimeError("outer selection has no verified audit binding")
        if champions:
            expected_outer = len(frozen_plan["validation"]["outer_rounds"])
            if (
                len(outer_records) != expected_outer
                or not any(
                    item["payload"].get("search_round") == "FINAL"
                    for item in evidence_items
                    if item["evidence_type"] == "CANDIDATE_EVALUATED"
                )
            ):
                raise RuntimeError("champion evidence is based on partial folds")

    def _classify_readiness(
        self,
        connection: sqlite3.Connection,
        study_id: str,
    ) -> _StudyReadiness:
        self._validate_study_projection(connection, study_id)
        study = connection.execute(
            """
            SELECT study_id, phase, control_status, frozen_plan_json
            FROM parameter_studies
            WHERE study_id = ?
            """,
            (study_id,),
        ).fetchone()
        if study is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        frozen_plan = _strict_json_object(
            study["frozen_plan_json"],
            "Parameter Study frozen plan",
        )
        phase = study["phase"]
        control_status = study["control_status"]
        effect: dict[str, Any] | None = None
        effect_digest: str | None = None
        intent_action: sqlite3.Row | None = None
        intent_response: dict[str, Any] | None = None
        receipt_action: sqlite3.Row | None = None
        receipt_response: dict[str, Any] | None = None
        effect_attempt: sqlite3.Row | None = None
        effect_attempt_is_valid = False
        has_authorized_dispatch = False
        dispatch_in_flight = False

        if phase in {
            "VALIDATING_SELECTION_PROCESS",
            "HOLDOUT_READY",
            "HOLDOUT_RUNNING",
        }:
            if phase == "VALIDATING_SELECTION_PROCESS":
                effect = self._trial_proposal_effect(study_id, frozen_plan)
            else:
                champion = connection.execute(
                    """
                    SELECT candidate_digest
                    FROM parameter_study_evidence
                    WHERE study_id = ? AND evidence_type = 'CHAMPION_FROZEN'
                    """,
                    (study_id,),
                ).fetchone()
                if champion is None:
                    raise RuntimeError("holdout phase has no frozen champion")
                effect = self._holdout_effect(
                    study_id,
                    frozen_plan,
                    champion["candidate_digest"],
                )
            effect_digest = _digest(effect)
            intent_action = self._load_action(
                self._effect_action_id(effect_digest),
                connection,
            )
            if intent_action is not None:
                intent_response = self._valid_intent_action(
                    connection,
                    intent_action,
                    study_id=study_id,
                    effect=effect,
                    effect_digest=effect_digest,
                )
            receipt_action = self._load_action(
                self._receipt_action_id(effect_digest),
                connection,
            )
            if receipt_action is not None:
                receipt_response = self._valid_receipt_action(
                    connection,
                    receipt_action,
                    study_id=study_id,
                    effect=effect,
                    effect_digest=effect_digest,
                    frozen_plan=frozen_plan,
                )
            dispatches = connection.execute(
                """
                SELECT *
                FROM parameter_study_actions
                WHERE study_id = ?
                  AND operation = 'EFFECT_DISPATCH_AUTHORIZATION'
                """,
                (study_id,),
            ).fetchall()
            has_authorized_dispatch = (
                intent_response is not None
                and any(
                    self._valid_dispatch_action(
                        connection,
                        dispatch,
                        study_id=study_id,
                        effect=effect,
                        effect_digest=effect_digest,
                    )
                    is not None
                    for dispatch in dispatches
                )
            )
            effect_attempt = connection.execute(
                """
                SELECT attempt_id, experiment_id, action_id, sequence
                FROM attempts WHERE action_id = ?
                """,
                (self._effect_action_id(effect_digest),),
            ).fetchone()
            effect_attempt_is_valid = (
                effect_attempt is not None
                and self._valid_effect_attempt(
                    connection,
                    action_id=self._effect_action_id(effect_digest),
                    frozen_plan=frozen_plan,
                )
                is not None
            )
            dispatch_in_flight = (
                has_authorized_dispatch
                and receipt_response is None
                and effect_attempt is None
            )

        if (
            has_authorized_dispatch
            and receipt_response is None
            and effect_attempt is not None
            and not effect_attempt_is_valid
        ):
            return _StudyReadiness(
                "TERMINAL",
                {
                    "status": "ACTION_CONFLICT",
                    "action_id": self._effect_action_id(effect_digest),
                },
                frozen_plan,
            )
        if (
            has_authorized_dispatch
            and receipt_response is None
            and self.effect_executor is not None
        ):
            return _StudyReadiness(
                "RECONCILE_EFFECT",
                intent_response,
                frozen_plan,
                effect,
                effect_digest,
                requires_lease=True,
                discoverable=True,
                reconciliation_only=True,
                dispatch_in_flight=dispatch_in_flight,
            )
        if control_status != "ACTIVE" and self._has_binding_reconciliation(
            connection,
            study_id,
        ):
            return _StudyReadiness(
                "RECONCILE_BINDING",
                None,
                frozen_plan,
                requires_lease=True,
                discoverable=True,
                reconciliation_only=True,
            )
        if control_status != "ACTIVE":
            if control_status == "FAILED":
                drift_action = self._load_action(
                    f"{INTERNAL_ACTION_PREFIX}drift:{study_id}",
                    connection,
                )
                if drift_action is not None:
                    drift_response = self._valid_drift_action(
                        connection,
                        drift_action,
                        study_id,
                    )
                    if drift_response is not None:
                        return _StudyReadiness(
                            "TERMINAL",
                            drift_response,
                            frozen_plan,
                        )
            return _StudyReadiness(
                "TERMINAL",
                {
                    "status": control_status,
                    "study_id": study_id,
                    "control_status": control_status,
                },
                frozen_plan,
            )
        if phase == "COMPLETED":
            return _StudyReadiness(
                "TERMINAL",
                {
                    "status": "NO_CHANGE",
                    "study_id": study_id,
                    "phase": phase,
                },
                frozen_plan,
            )
        if phase == "FROZEN":
            return _StudyReadiness(
                "ADVANCE_PHASE",
                None,
                frozen_plan,
                requires_lease=True,
                discoverable=True,
            )
        if phase not in {
            "VALIDATING_SELECTION_PROCESS",
            "HOLDOUT_READY",
            "HOLDOUT_RUNNING",
        }:
            return _StudyReadiness(
                "TERMINAL",
                {
                    "status": "NO_CHANGE",
                    "study_id": study_id,
                    "phase": phase,
                },
                frozen_plan,
            )
        if intent_action is None:
            return _StudyReadiness(
                "RECORD_EFFECT_INTENT",
                None,
                frozen_plan,
                effect,
                effect_digest,
                requires_lease=True,
                discoverable=True,
            )
        if intent_response is None:
            return _StudyReadiness(
                "TERMINAL",
                {
                    "status": "ACTION_CONFLICT",
                    "action_id": self._effect_action_id(effect_digest),
                },
                frozen_plan,
            )
        if receipt_response is not None:
            return _StudyReadiness(
                "TERMINAL",
                receipt_response,
                frozen_plan,
            )
        if receipt_action is not None and self.effect_executor is None:
            return _StudyReadiness(
                "TERMINAL",
                {
                    "status": "ACTION_CONFLICT",
                    "action_id": self._receipt_action_id(effect_digest),
                },
                frozen_plan,
            )
        if self.effect_executor is None:
            return _StudyReadiness(
                "TERMINAL",
                intent_response,
                frozen_plan,
                dispatch_in_flight=dispatch_in_flight,
            )
        return _StudyReadiness(
            "DISPATCH_EFFECT",
            intent_response,
            frozen_plan,
            effect,
            effect_digest,
            requires_lease=True,
            discoverable=True,
        )

    def _acquire_lease(self, study_id: str) -> dict[str, Any]:
        with self.catalog.transaction(immediate=True) as connection:
            readiness = self._classify_readiness(connection, study_id)
            if not readiness.requires_lease:
                if readiness.response is None:
                    raise RuntimeError("Study readiness response is missing")
                return readiness.response

            now_value = self._database_now(connection)
            current = self._latest_lease(connection, study_id)
            if current is not None:
                expires_at = _utc_datetime(
                    current["expires_at"],
                    "lease.expires_at",
                )
                expected_nonce = self._owner_nonce(
                    study_id,
                    current["fencing_token"],
                )
                if expires_at > now_value:
                    if (
                        current["owner"] == self.coordinator_id
                        and current["owner_nonce"] == expected_nonce
                    ):
                        return {
                            "status": "ACQUIRED",
                            "lease": current,
                        }
                    return {
                        "status": "LEASE_BUSY",
                        "study_id": study_id,
                        "lease": current,
                    }

            fencing_token = (
                1 if current is None else current["fencing_token"] + 1
            )
            lease = {
                "owner": self.coordinator_id,
                "owner_nonce": self._owner_nonce(study_id, fencing_token),
                "expires_at": _utc_text(
                    now_value
                    + timedelta(seconds=self.lease_duration_seconds)
                ),
                "fencing_token": fencing_token,
            }
            action_id = self._lease_action_id(study_id, fencing_token)
            request_digest = self._lease_request_digest(study_id, lease)
            existing_action = self._load_action(action_id, connection)
            if existing_action is not None:
                return {
                    "status": "ACTION_CONFLICT",
                    "action_id": action_id,
                }
            connection.execute(
                """
                INSERT INTO parameter_study_actions(
                    action_id, operation, study_id, request_digest,
                    response_json, created_at
                ) VALUES (?, 'COORDINATOR_LEASE', ?, ?, ?, ?)
                """,
                (
                    action_id,
                    study_id,
                    request_digest,
                    canonical_json_bytes(lease).decode(),
                    _utc_text(now_value),
                ),
            )
            return {
                "status": "ACQUIRED",
                "lease": lease,
            }

    def _lease_is_current(
        self,
        connection: sqlite3.Connection,
        study_id: str,
        lease: dict[str, Any],
    ) -> tuple[bool, datetime, dict[str, Any] | None]:
        now = self._database_now(connection)
        current = self._latest_lease(connection, study_id)
        if current is None:
            return False, now, None
        expected_nonce = self._owner_nonce(
            study_id,
            current["fencing_token"],
        )
        is_current = (
            current == lease
            and current["owner"] == self.coordinator_id
            and current["owner_nonce"] == expected_nonce
            and _utc_datetime(
                current["expires_at"],
                "lease.expires_at",
            )
            > now
        )
        return is_current, now, current

    def _record_execution_identity_drift(
        self,
        connection: sqlite3.Connection,
        *,
        study_id: str,
        frozen_plan: dict[str, Any],
        lease: dict[str, Any],
        now: str,
    ) -> dict[str, Any] | None:
        frozen_identity_bytes = canonical_json_bytes(
            frozen_plan["execution"]["identity"]
        )
        current_identity_bytes = canonical_json_bytes(
            deepcopy(self.experiments.execution_identity)
        )
        if frozen_identity_bytes == current_identity_bytes:
            return None

        frozen_digest = hashlib.sha256(frozen_identity_bytes).hexdigest()
        current_digest = hashlib.sha256(current_identity_bytes).hexdigest()
        action_id = f"{INTERNAL_ACTION_PREFIX}drift:{study_id}"
        existing_drift = self._load_action(action_id, connection)
        if existing_drift is not None:
            replay = self._valid_drift_action(
                connection,
                existing_drift,
                study_id,
            )
            if replay is not None:
                return replay
            return {
                "status": "ACTION_CONFLICT",
                "action_id": action_id,
            }
        response = {
            "status": "EXECUTION_IDENTITY_DRIFT",
            "study_id": study_id,
            "frozen_execution_identity_digest": frozen_digest,
            "current_execution_identity_digest": current_digest,
            "fencing_token": lease["fencing_token"],
            "owner": lease["owner"],
            "owner_nonce": lease["owner_nonce"],
        }
        connection.execute(
            """
            UPDATE parameter_studies
            SET control_status = 'FAILED', updated_at = ?
            WHERE study_id = ?
            """,
            (now, study_id),
        )
        connection.execute(
            """
            INSERT INTO parameter_study_events(
                study_id, sequence, event_type, occurred_at, payload_json
            )
            SELECT ?, COALESCE(MAX(sequence), 0) + 1,
                   'EXECUTION_IDENTITY_DRIFT', ?, ?
            FROM parameter_study_events
            WHERE study_id = ?
            """,
            (
                study_id,
                now,
                canonical_json_bytes(
                    {
                        "action_id": action_id,
                        "code": "EXECUTION_IDENTITY_DRIFT",
                        "current_execution_identity_digest": current_digest,
                        "fencing_token": lease["fencing_token"],
                        "frozen_execution_identity_digest": frozen_digest,
                        "owner": lease["owner"],
                        "owner_nonce": lease["owner_nonce"],
                    }
                ).decode(),
                study_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO parameter_study_actions(
                action_id, operation, study_id, request_digest,
                response_json, created_at
            ) VALUES (?, 'EXECUTION_IDENTITY_DRIFT', ?, ?, ?, ?)
            """,
            (
                action_id,
                study_id,
                _digest(
                    {
                        "current_execution_identity_digest": current_digest,
                        "frozen_execution_identity_digest": frozen_digest,
                        "study_id": study_id,
                    }
                ),
                canonical_json_bytes(response).decode(),
                now,
            ),
        )
        return response

    def _apply_evaluation_result(
        self,
        connection: sqlite3.Connection,
        *,
        study_id: str,
        result: dict[str, Any],
        occurred_at: str,
    ) -> None:
        status = result.get("status")
        if status not in {
            "NO_ELIGIBLE_CANDIDATE",
            "CHAMPION_SELECTED",
            "HOLDOUT_PASSED",
            "HOLDOUT_FAILED",
        }:
            return
        study = connection.execute(
            """
            SELECT phase, selection_outcome, holdout_outcome, frozen_plan_json
            FROM parameter_studies WHERE study_id = ?
            """,
            (study_id,),
        ).fetchone()
        if study is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        frozen_plan = _strict_json_object(
            study["frozen_plan_json"], "Parameter Study frozen plan"
        )

        if status == "NO_ELIGIBLE_CANDIDATE":
            if study["selection_outcome"] != "NOT_DETERMINED":
                raise RuntimeError("Parameter Study selection is already frozen")
            payload = {
                "selection_outcome": "NO_ELIGIBLE_CANDIDATE",
                "holdout_outcome": "NOT_RUN",
                "explanation": result.get(
                    "explanation", "Every completed candidate was ineligible or failed."
                ),
            }
            connection.execute(
                """
                UPDATE parameter_studies
                SET phase = 'COMPLETED',
                    selection_outcome = 'NO_ELIGIBLE_CANDIDATE',
                    holdout_outcome = 'NOT_RUN', updated_at = ?
                WHERE study_id = ?
                """,
                (occurred_at, study_id),
            )
            self._append_evaluation_evidence(
                connection,
                study_id=study_id,
                evidence_type="CANDIDATE_EVALUATED",
                candidate_digest=None,
                payload=payload,
                occurred_at=occurred_at,
            )
            return

        candidate_digest = result.get("candidate_digest")
        if not isinstance(candidate_digest, str) or STUDY_ID.fullmatch(candidate_digest) is None:
            raise RuntimeError("evaluation result candidate_digest is invalid")
        if status == "CHAMPION_SELECTED":
            evaluation = result.get("evaluation")
            evaluation_digest = (
                evaluation.get("evaluation_digest")
                if isinstance(evaluation, dict)
                else None
            )
            outer_evidence_digest = result.get("outer_evidence_digest")
            if (
                not isinstance(evaluation, dict)
                or evaluation.get("candidate_digest") != candidate_digest
                or evaluation.get("eligible") is not True
                or not isinstance(evaluation_digest, str)
                or STUDY_ID.fullmatch(evaluation_digest) is None
                or not isinstance(outer_evidence_digest, str)
                or STUDY_ID.fullmatch(outer_evidence_digest) is None
            ):
                raise RuntimeError("champion must have one eligible verified evaluation")
            if study["selection_outcome"] != "NOT_DETERMINED":
                raise RuntimeError("Parameter Study selection is already frozen")
            holdout_identity = _holdout_identity_digest(frozen_plan)
            payload = {
                "candidate_digest": candidate_digest,
                "evaluation_digest": evaluation_digest,
                "outer_evidence_digest": outer_evidence_digest,
                "holdout_identity_digest": holdout_identity,
            }
            self._append_evaluation_evidence(
                connection,
                study_id=study_id,
                evidence_type="CHAMPION_FROZEN",
                candidate_digest=candidate_digest,
                payload=payload,
                occurred_at=occurred_at,
            )
            connection.execute(
                """
                INSERT INTO parameter_study_holdout_ledger(
                    study_id, sequence, holdout_identity_digest,
                    event_type, occurred_at, payload_json
                )
                SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?,
                       'GRANTED', ?, ?
                FROM parameter_study_holdout_ledger
                WHERE study_id = ?
                """,
                (
                    study_id,
                    holdout_identity,
                    occurred_at,
                    canonical_json_bytes(
                        {
                            "candidate_digest": candidate_digest,
                            "authorization": "ONE_FROZEN_CHAMPION",
                        }
                    ).decode(),
                    study_id,
                ),
            )
            connection.execute(
                """
                UPDATE parameter_studies
                SET phase = 'HOLDOUT_READY',
                    selection_outcome = 'CHAMPION_SELECTED', updated_at = ?
                WHERE study_id = ?
                """,
                (occurred_at, study_id),
            )
            return

        champion = connection.execute(
            """
            SELECT candidate_digest
            FROM parameter_study_evidence
            WHERE study_id = ? AND evidence_type = 'CHAMPION_FROZEN'
            """,
            (study_id,),
        ).fetchone()
        if (
            champion is None
            or champion["candidate_digest"] != candidate_digest
            or study["selection_outcome"] != "CHAMPION_SELECTED"
            or study["holdout_outcome"] != "NOT_RUN"
        ):
            raise RuntimeError("holdout result does not belong to the frozen champion")
        holdout_identity = _holdout_identity_digest(frozen_plan)
        existing_access = connection.execute(
            """
            SELECT 1 FROM parameter_study_holdout_ledger
            WHERE study_id = ? AND event_type = 'ACCESSED'
            """,
            (study_id,),
        ).fetchone()
        if existing_access is None:
            connection.execute(
                """
                INSERT INTO parameter_study_holdout_ledger(
                    study_id, sequence, holdout_identity_digest,
                    event_type, occurred_at, payload_json
                )
                SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?,
                       'ACCESSED', ?, ?
                FROM parameter_study_holdout_ledger
                WHERE study_id = ?
                """,
                (
                    study_id,
                    holdout_identity,
                    occurred_at,
                    canonical_json_bytes(
                        {"candidate_digest": candidate_digest}
                    ).decode(),
                    study_id,
                ),
            )
        outcome = "PASSED" if status == "HOLDOUT_PASSED" else "FAILED"
        metric_document_digest = result.get("metric_document_digest")
        constraints = result.get("constraints")
        if (
            not isinstance(metric_document_digest, str)
            or STUDY_ID.fullmatch(metric_document_digest) is None
            or not isinstance(constraints, dict)
        ):
            raise RuntimeError("holdout result evidence is invalid")
        self._append_evaluation_evidence(
            connection,
            study_id=study_id,
            evidence_type="HOLDOUT_OUTCOME_RECORDED",
            candidate_digest=candidate_digest,
            payload={
                "candidate_digest": candidate_digest,
                "outcome": outcome,
                "metric_document_digest": metric_document_digest,
                "constraints": constraints,
            },
            occurred_at=occurred_at,
        )
        connection.execute(
            """
            UPDATE parameter_studies
            SET phase = 'COMPLETED', holdout_outcome = ?, updated_at = ?
            WHERE study_id = ?
            """,
            (outcome, occurred_at, study_id),
        )

    @staticmethod
    def _append_evaluation_evidence(
        connection: sqlite3.Connection,
        *,
        study_id: str,
        evidence_type: str,
        candidate_digest: str | None,
        payload: dict[str, Any],
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO parameter_study_evidence(
                study_id, sequence, evidence_type, candidate_digest,
                payload_json, occurred_at
            )
            SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ?, ?
            FROM parameter_study_evidence
            WHERE study_id = ?
            """,
            (
                study_id,
                evidence_type,
                candidate_digest,
                canonical_json_bytes(payload).decode(),
                occurred_at,
                study_id,
            ),
        )

    @staticmethod
    def _selection_rounds(
        frozen_plan: dict[str, Any],
    ) -> list[tuple[str, list[dict[str, Any]], dict[str, Any] | None]]:
        rounds = [
            (
                f"OUTER:{item['round']}",
                item["inner_folds"],
                item["outer_audit"],
            )
            for item in frozen_plan["validation"]["outer_rounds"]
        ]
        rounds.append(
            (
                "FINAL",
                frozen_plan["validation"]["final_search_round"]["inner_folds"],
                None,
            )
        )
        return rounds

    @staticmethod
    def _round_candidates(
        study_id: str,
        frozen_plan: dict[str, Any],
        search_round: str,
    ) -> list[dict[str, Any]]:
        suggester = (
            GridParameterSuggester()
            if frozen_plan["search"]["suggester"] == "GRID"
            else SeededRandomParameterSuggester()
        )
        round_plan = {
            "schema_version": 1,
            "round_identity": f"{study_id}/{search_round}",
            "template": frozen_plan["template"],
            "operators": frozen_plan["operators"],
            "search": frozen_plan["search"],
        }
        history: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        while True:
            proposed = suggester.next_suggestion(round_plan, history)
            if isinstance(proposed, Exhausted):
                break
            if not isinstance(proposed, Suggestion):
                raise RuntimeError("Parameter Suggester returned an invalid outcome")
            event = proposed.as_history_event()
            history.append(event)
            if proposed.creates_trial:
                candidates.append(
                    {
                        "candidate_digest": proposed.candidate_digest,
                        "configuration": proposed.candidate,
                        "proposal_sequence": proposed.proposal_sequence,
                        "classification": proposed.classification.value,
                        "champion_eligible": proposed.champion_eligible,
                    }
                )
        if not candidates:
            raise RuntimeError("Parameter Study search round produced no Trials")
        return candidates

    def _ensure_trials(
        self,
        *,
        study_id: str,
        search_round: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            for candidate in candidates:
                configuration_json = canonical_json_bytes(
                    candidate["configuration"]
                ).decode()
                existing = connection.execute(
                    """
                    SELECT configuration_json
                    FROM parameter_study_trials
                    WHERE study_id = ? AND candidate_digest = ?
                    """,
                    (study_id, candidate["candidate_digest"]),
                ).fetchone()
                if existing is not None:
                    if existing["configuration_json"] != configuration_json:
                        raise RuntimeError(
                            "candidate digest is bound to a different configuration"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO parameter_study_trials(
                        study_id, candidate_digest, configuration_json,
                        first_search_round, proposal_sequence, classification,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        study_id,
                        candidate["candidate_digest"],
                        configuration_json,
                        search_round,
                        candidate["proposal_sequence"],
                        candidate["classification"],
                        now,
                    ),
                )

    @staticmethod
    def _binding_id(
        *,
        study_id: str,
        search_round: str,
        candidate_digest: str,
        role: str,
        fold_sequence: int,
        fold_window: dict[str, Any],
    ) -> str:
        return _digest(
            {
                "study_id": study_id,
                "search_round": search_round,
                "candidate_digest": candidate_digest,
                "role": role,
                "fold_sequence": fold_sequence,
                "fold_window": fold_window,
            }
        )

    @staticmethod
    def _task_for_candidate(
        *,
        candidate: dict[str, Any],
        instrument: str,
        snapshot_id: str,
        fold_window: dict[str, Any],
    ) -> dict[str, Any]:
        template_parameters = deepcopy(candidate["template"]["parameters"])
        template_parameters.update(
            {
                "evaluation_start": fold_window["scoring_start"],
                "evaluation_end": fold_window["scoring_end"],
            }
        )
        if fold_window["account_policy"] == "FORCE_FLAT_WITH_COST":
            template_parameters["terminal_handling"] = "force_liquidate"
        return {
            "schema_version": 1,
            "dataset": {
                "instrument": instrument,
                "snapshot_id": snapshot_id,
            },
            "template": {
                "name": candidate["template"]["name"],
                "version": candidate["template"]["version"],
                "parameters": template_parameters,
            },
            "operators": {
                slot: {
                    "operator_id": operator["operator_id"],
                    "version": operator["version"],
                    "parameters": deepcopy(operator["parameters"]),
                }
                for slot, operator in candidate["operators"].items()
            },
        }

    def _dispatch_binding(
        self,
        *,
        study_id: str,
        frozen_plan: dict[str, Any],
        search_round: str,
        candidate: dict[str, Any],
        fold_window: dict[str, Any],
        fold_sequence: int,
    ) -> dict[str, Any]:
        role = fold_window["role"]
        binding_id = self._binding_id(
            study_id=study_id,
            search_round=search_round,
            candidate_digest=candidate["candidate_digest"],
            role=role,
            fold_sequence=fold_sequence,
            fold_window=fold_window,
        )
        existing = self._binding(binding_id)
        if existing is not None:
            return {
                "status": "ATTEMPT_PENDING",
                "study_id": study_id,
                "binding_id": binding_id,
                "experiment_id": existing["experiment_id"],
                "attempt_id": existing["attempt_id"],
            }
        dataset_slice = self.dataset_slice_factory.materialize(
            frozen_plan["dataset"],
            fold_window,
        )
        task = self._task_for_candidate(
            candidate=candidate["configuration"],
            instrument=frozen_plan["dataset"]["instrument"],
            snapshot_id=dataset_slice["snapshot_id"],
            fold_window=fold_window,
        )
        preview = self.experiments.preview_task(task)
        action_id = self._effect_action_id(binding_id)
        submission = self.experiments.submit_study_effect(
            task,
            action_id=action_id,
        )
        if submission["experiment_id"] != preview["experiment_id"]:
            raise RuntimeError("Experiment submission identity changed after preview")
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM parameter_study_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO parameter_study_bindings(
                        binding_id, study_id, search_round, candidate_digest,
                        role, fold_sequence, fold_window_json, task_json,
                        task_digest, dataset_snapshot_id, experiment_id,
                        submitted_attempt_id, attempt_id, state,
                        metric_document_json, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTED',
                        NULL, ?, ?
                    )
                    """,
                    (
                        binding_id,
                        study_id,
                        search_round,
                        candidate["candidate_digest"],
                        role,
                        fold_sequence,
                        canonical_json_bytes(fold_window).decode(),
                        canonical_json_bytes(task).decode(),
                        _digest(task),
                        dataset_slice["snapshot_id"],
                        submission["experiment_id"],
                        submission["attempt_id"],
                        submission["attempt_id"],
                        now,
                        now,
                    ),
                )
            elif (
                existing["experiment_id"] != submission["experiment_id"]
                or existing["submitted_attempt_id"] != submission["attempt_id"]
            ):
                raise RuntimeError("durable Study binding identity conflicts")
        effect = {
            "schema_version": 1,
            "effect_type": "EXECUTE_EXPERIMENT_ATTEMPT",
            "study_id": study_id,
            "binding_id": binding_id,
            "candidate_digest": candidate["candidate_digest"],
            "role": role,
            "fold_window": deepcopy(fold_window),
            "experiment_id": submission["experiment_id"],
            "attempt_id": submission["attempt_id"],
        }
        return {
            "status": "ATTEMPT_SUBMITTED",
            **{
                key: effect[key]
                for key in (
                    "study_id",
                    "binding_id",
                    "candidate_digest",
                    "role",
                    "experiment_id",
                    "attempt_id",
                )
            },
        }

    def _dispatch_authorized_binding(
        self,
        *,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        if self.effect_executor is None:
            raise RuntimeError("Parameter Study effect executor is unavailable")
        fold_window = _strict_json_object(
            binding["fold_window_json"],
            "Study binding fold window",
        )
        effect = {
            "schema_version": 1,
            "effect_type": "EXECUTE_EXPERIMENT_ATTEMPT",
            "study_id": binding["study_id"],
            "binding_id": binding["binding_id"],
            "candidate_digest": binding["candidate_digest"],
            "role": binding["role"],
            "fold_window": fold_window,
            "experiment_id": binding["experiment_id"],
            "attempt_id": binding["submitted_attempt_id"],
        }
        effect_action_id = self._effect_action_id(binding["binding_id"])
        receipt_action_id = self._receipt_action_id(binding["binding_id"])
        authorized_lease: dict[str, Any] | None = None
        holdout_ambiguous = False
        with self.catalog.transaction(immediate=True) as connection:
            receipt = self._load_action(receipt_action_id, connection)
            if receipt is not None:
                stored = _strict_json_object(
                    receipt["response_json"],
                    "Study execution receipt",
                )
                result = stored.get("result")
                if (
                    receipt["operation"] != "EFFECT_RECEIPT"
                    or stored.get("effect") != effect
                    or not isinstance(result, dict)
                    or set(result) != {"experiment_id", "attempt_id"}
                    or result["experiment_id"] != binding["experiment_id"]
                    or result["attempt_id"] != binding["submitted_attempt_id"]
                ):
                    raise RuntimeError("durable Study execution receipt conflicts")
                return {
                    "status": "ATTEMPT_DISPATCHED",
                    **effect,
                }
            lease = self._latest_lease(connection, binding["study_id"])
            if lease is None:
                raise RuntimeError("Study execution dispatch has no coordinator lease")
            authorized_lease = deepcopy(lease)
            if binding["role"] == "TERMINAL_HOLDOUT":
                authorization = connection.execute(
                    """
                    SELECT c.effect_action_id
                    FROM parameter_study_holdout_claims AS c
                    JOIN parameter_study_holdout_ledger AS h
                      ON h.study_id = c.study_id
                     AND h.holdout_identity_digest =
                         c.holdout_identity_digest
                     AND h.event_type = 'ACCESSED'
                    WHERE c.study_id = ? AND c.binding_id = ?
                      AND c.candidate_digest = ?
                    """,
                    (
                        binding["study_id"],
                        binding["binding_id"],
                        binding["candidate_digest"],
                    ),
                ).fetchone()
                if (
                    authorization is None
                    or authorization["effect_action_id"] != effect_action_id
                ):
                    raise RuntimeError(
                        "terminal holdout dispatch lacks durable accessed authorization"
                    )
            intent = self._load_action(effect_action_id, connection)
            if intent is None:
                intent_response = {
                    "status": "EFFECT_PENDING",
                    "study_id": binding["study_id"],
                    "action_id": effect_action_id,
                    "effect": effect,
                    "fencing_token": lease["fencing_token"],
                    "owner": lease["owner"],
                    "owner_nonce": lease["owner_nonce"],
                }
                connection.execute(
                    """
                    INSERT INTO parameter_study_actions(
                        action_id, operation, study_id, request_digest,
                        response_json, created_at
                    ) VALUES (?, 'EFFECT_INTENT', ?, ?, ?, ?)
                    """,
                    (
                        effect_action_id,
                        binding["study_id"],
                        binding["binding_id"],
                        canonical_json_bytes(intent_response).decode(),
                        self._now(),
                    ),
                )
            else:
                stored_intent = _strict_json_object(
                    intent["response_json"],
                    "Study execution intent",
                )
                if (
                    intent["operation"] != "EFFECT_INTENT"
                    or stored_intent.get("effect") != effect
                ):
                    raise RuntimeError("durable Study execution intent conflicts")
                holdout_ambiguous = binding["role"] == "TERMINAL_HOLDOUT"
        if holdout_ambiguous:
            return self._record_holdout_ambiguous(
                study_id=binding["study_id"],
                binding_id=binding["binding_id"],
            )
        result = self.effect_executor(deepcopy(effect), effect_action_id)
        if (
            not isinstance(result, dict)
            or set(result) != {"experiment_id", "attempt_id"}
            or not all(
                isinstance(result[key], str)
                and STUDY_ID.fullmatch(result[key]) is not None
                for key in ("experiment_id", "attempt_id")
            )
        ):
            raise RuntimeError(
                "Parameter Study effects may return only durable "
                "Experiment/Attempt identifiers"
            )
        if (
            result["experiment_id"] != binding["experiment_id"]
            or result["attempt_id"] != binding["submitted_attempt_id"]
        ):
            raise RuntimeError(
                "effect result does not match the authorized binding"
            )
        response = {
            "status": "EFFECT_COMMITTED",
            "study_id": binding["study_id"],
            "action_id": effect_action_id,
            "effect": effect,
            "result": result,
        }
        with self.catalog.transaction(immediate=True) as connection:
            if authorized_lease is None:
                raise RuntimeError("Study execution dispatch lease is missing")
            lease_is_current, _, current = self._lease_is_current(
                connection,
                binding["study_id"],
                authorized_lease,
            )
            if not lease_is_current:
                return {
                    "status": "LEASE_BUSY",
                    "study_id": binding["study_id"],
                    "lease": current,
                }
            receipt = self._load_action(receipt_action_id, connection)
            if receipt is None:
                connection.execute(
                    """
                    INSERT INTO parameter_study_actions(
                        action_id, operation, study_id, request_digest,
                        response_json, created_at
                    ) VALUES (?, 'EFFECT_RECEIPT', ?, ?, ?, ?)
                    """,
                    (
                        receipt_action_id,
                        binding["study_id"],
                        _digest(response),
                        canonical_json_bytes(response).decode(),
                        self._now(),
                    ),
                )
            else:
                stored = _strict_json_object(
                    receipt["response_json"],
                    "Study execution receipt",
                )
                if stored != response:
                    raise RuntimeError("durable Study execution receipt conflicts")
        return {"status": "ATTEMPT_DISPATCHED", **effect}

    def _binding(self, binding_id: str) -> dict[str, Any] | None:
        connection = self.catalog.connect()
        try:
            row = connection.execute(
                "SELECT * FROM parameter_study_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    def _bindings(
        self,
        study_id: str,
        *,
        search_round: str | None = None,
    ) -> list[dict[str, Any]]:
        connection = self.catalog.connect()
        try:
            if search_round is None:
                rows = connection.execute(
                    """
                    SELECT * FROM parameter_study_bindings
                    WHERE study_id = ?
                    ORDER BY search_round, role, fold_sequence, candidate_digest
                    """,
                    (study_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM parameter_study_bindings
                    WHERE study_id = ? AND search_round = ?
                    ORDER BY role, fold_sequence, candidate_digest
                    """,
                    (study_id, search_round),
                ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def _record_contested_binding(
        self,
        *,
        study_id: str,
        binding: dict[str, Any],
        experiment: dict[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM parameter_study_evidence
                WHERE study_id = ? AND evidence_type = 'EVIDENCE_CONTESTED'
                  AND json_extract(payload_json, '$.experiment_id') = ?
                """,
                (study_id, binding["experiment_id"]),
            ).fetchone()
            if existing is None:
                payload = {
                    "binding_id": binding["binding_id"],
                    "candidate_digest": binding["candidate_digest"],
                    "experiment_id": binding["experiment_id"],
                    "attempts": [
                        {
                            key: attempt.get(key)
                            for key in (
                                "attempt_id",
                                "status",
                                "comparison",
                                "result_digest",
                            )
                        }
                        for attempt in experiment["attempts"]
                    ],
                }
                self._append_evaluation_evidence(
                    connection,
                    study_id=study_id,
                    evidence_type="EVIDENCE_CONTESTED",
                    candidate_digest=binding["candidate_digest"],
                    payload=payload,
                    occurred_at=now,
                )
            connection.execute(
                """
                UPDATE parameter_study_bindings
                SET state = 'CONTESTED', updated_at = ?
                WHERE study_id = ? AND experiment_id = ?
                """,
                (now, study_id, binding["experiment_id"]),
            )
            connection.execute(
                """
                UPDATE parameter_studies
                SET control_status = 'FAILED', updated_at = ?
                WHERE study_id = ?
                """,
                (now, study_id),
            )
        return {
            "status": "EVIDENCE_CONTESTED",
            "study_id": study_id,
            "binding_id": binding["binding_id"],
            "experiment_id": binding["experiment_id"],
        }

    def _factory_document_for_binding(
        self,
        binding: dict[str, Any],
        configuration: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self._canonical_verified_metric_document(
            experiment_id=binding["experiment_id"],
            candidate_digest=binding["candidate_digest"],
            candidate_configuration=configuration,
            fold_window=_strict_json_object(
                binding["fold_window_json"],
                "Study binding fold window",
            ),
        )

    def _observe_binding(
        self,
        *,
        study_id: str,
        binding: dict[str, Any],
        configuration: dict[str, Any],
    ) -> dict[str, Any]:
        experiment = self.experiments.experiment_detail(binding["experiment_id"])
        if experiment["has_divergent_attempt"]:
            return self._record_contested_binding(
                study_id=study_id,
                binding=binding,
                experiment=experiment,
            )
        latest_attempt = experiment["attempts"][-1]
        if (
            binding["state"] != "VERIFIED"
            and latest_attempt["attempt_id"] != binding["attempt_id"]
        ):
            now = self._now()
            with self.catalog.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE parameter_study_bindings
                    SET attempt_id = ?, state = 'SUBMITTED',
                        metric_document_json = NULL, updated_at = ?
                    WHERE binding_id = ?
                    """,
                    (latest_attempt["attempt_id"], now, binding["binding_id"]),
                )
            binding = self._binding(binding["binding_id"])
            if binding is None:
                raise RuntimeError("Study binding disappeared during Attempt refresh")
        document = self._factory_document_for_binding(binding, configuration)
        if document is None:
            attempt = self.experiments.attempt_detail(binding["attempt_id"])
            if attempt["status"] in {"FAILED", "INTERRUPTED"}:
                with self.catalog.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        UPDATE parameter_study_bindings
                        SET state = 'FAILED', updated_at = ?
                        WHERE binding_id = ? AND state != 'VERIFIED'
                        """,
                        (self._now(), binding["binding_id"]),
                    )
                return {
                    "status": "ATTEMPT_FAILED",
                    "study_id": study_id,
                    "binding_id": binding["binding_id"],
                    "experiment_id": binding["experiment_id"],
                    "attempt_id": binding["attempt_id"],
                    "attempt_status": attempt["status"],
                }
            if (
                self.effect_executor is not None
                and binding["attempt_id"] == binding["submitted_attempt_id"]
                and attempt["status"] == "PENDING"
            ):
                return self._dispatch_authorized_binding(binding=binding)
            return {
                "status": "ATTEMPT_PENDING",
                "study_id": study_id,
                "binding_id": binding["binding_id"],
                "experiment_id": binding["experiment_id"],
                "attempt_id": binding["attempt_id"],
                "attempt_status": attempt["status"],
            }
        now = self._now()
        configuration_json = canonical_json_bytes(configuration).decode()
        canonical_attempt_id = document["attempt_id"]
        with self.catalog.transaction(immediate=True) as connection:
            claim = connection.execute(
                """
                SELECT candidate_digest, configuration_json
                FROM parameter_study_attempt_candidate_claims
                WHERE attempt_id = ?
                """,
                (canonical_attempt_id,),
            ).fetchone()
            if claim is None:
                connection.execute(
                    """
                    INSERT INTO parameter_study_attempt_candidate_claims(
                        attempt_id, candidate_digest, configuration_json, claimed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        canonical_attempt_id,
                        binding["candidate_digest"],
                        configuration_json,
                        now,
                    ),
                )
            elif (
                claim["candidate_digest"] != binding["candidate_digest"]
                or claim["configuration_json"] != configuration_json
            ):
                raise RuntimeError(
                    "one Attempt cannot verify two candidate configurations"
                )
            current = connection.execute(
                """
                SELECT state FROM parameter_study_bindings
                WHERE binding_id = ?
                """,
                (binding["binding_id"],),
            ).fetchone()
            if current is None:
                raise RuntimeError("Study binding disappeared during verification")
            if current["state"] == "SUBMITTED":
                connection.execute(
                    """
                    UPDATE parameter_study_bindings
                    SET attempt_id = ?, state = 'VERIFIED',
                        metric_document_json = ?, updated_at = ?
                    WHERE binding_id = ?
                    """,
                    (
                        canonical_attempt_id,
                        canonical_json_bytes(dict(document)).decode(),
                        now,
                        binding["binding_id"],
                    ),
                )
                self._append_evaluation_evidence(
                    connection,
                    study_id=study_id,
                    evidence_type="METRIC_DOCUMENT_VERIFIED",
                    candidate_digest=binding["candidate_digest"],
                    payload={
                        "binding_id": binding["binding_id"],
                        "search_round": binding["search_round"],
                        "role": binding["role"],
                        "fold_sequence": binding["fold_sequence"],
                        "experiment_id": binding["experiment_id"],
                        "attempt_id": canonical_attempt_id,
                        "metric_document": dict(document),
                    },
                    occurred_at=now,
                )
        return {
            "status": "METRIC_DOCUMENT_VERIFIED",
            "study_id": study_id,
            "binding_id": binding["binding_id"],
            "candidate_digest": binding["candidate_digest"],
            "experiment_id": binding["experiment_id"],
            "attempt_id": canonical_attempt_id,
            "metric_document_digest": document["document_digest"],
        }

    def _candidate_configuration(
        self,
        study_id: str,
        candidate_digest: str,
    ) -> dict[str, Any]:
        connection = self.catalog.connect()
        try:
            row = connection.execute(
                """
                SELECT configuration_json FROM parameter_study_trials
                WHERE study_id = ? AND candidate_digest = ?
                """,
                (study_id, candidate_digest),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RuntimeError("Study binding references an unknown Trial")
        return _strict_json_object(
            row["configuration_json"],
            "Study Trial configuration",
        )

    def _evaluate_round_candidates(
        self,
        *,
        study_id: str,
        frozen_plan: dict[str, Any],
        search_round: str,
        candidates: list[dict[str, Any]],
        inner_folds: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        bindings = self._bindings(study_id, search_round=search_round)
        inner = [binding for binding in bindings if binding["role"] == "INNER_SCORE"]
        expected_count = len(candidates) * len(inner_folds)
        if len(inner) != expected_count or any(
            binding["state"] not in {"VERIFIED", "FAILED"} for binding in inner
        ):
            return None
        evaluations: list[dict[str, Any]] = []
        for candidate in candidates:
            candidate_bindings = [
                binding
                for binding in inner
                if binding["candidate_digest"] == candidate["candidate_digest"]
            ]
            if len(candidate_bindings) != len(inner_folds):
                raise RuntimeError("partial inner-fold evidence cannot be evaluated")
            if any(binding["state"] == "FAILED" for binding in candidate_bindings):
                continue
            candidate_bindings.sort(key=lambda item: item["fold_sequence"])
            if [
                _strict_json_object(item["fold_window_json"], "Study binding fold")
                for item in candidate_bindings
            ] != inner_folds:
                raise RuntimeError("inner-fold evidence does not match the frozen plan")
            documents = []
            for binding in candidate_bindings:
                experiment = self.experiments.experiment_detail(
                    binding["experiment_id"]
                )
                if experiment["has_divergent_attempt"]:
                    self._record_contested_binding(
                        study_id=study_id,
                        binding=binding,
                        experiment=experiment,
                    )
                    raise RuntimeError("CONTESTED evidence cannot feed evaluation")
                document = self._factory_document_for_binding(
                    binding,
                    candidate["configuration"],
                )
                if document is None:
                    raise RuntimeError("verified binding lost canonical evidence")
                documents.append(document)
            evaluation = self.evaluation_policy.evaluate(
                candidate["candidate_digest"],
                documents,
                frozen_plan["evaluation"]["parameters"],
            )
            evaluations.append(evaluation)

        connection = self.catalog.connect()
        try:
            existing_rows = connection.execute(
                """
                SELECT candidate_digest, payload_json
                FROM parameter_study_evidence
                WHERE study_id = ? AND evidence_type = 'CANDIDATE_EVALUATED'
                """,
                (study_id,),
            ).fetchall()
        finally:
            connection.close()
        existing = {
            row["candidate_digest"]: _strict_json_object(
                row["payload_json"],
                "candidate evaluation evidence",
            )
            for row in existing_rows
            if _strict_json_object(
                row["payload_json"],
                "candidate evaluation evidence",
            ).get("search_round")
            == search_round
        }
        now = self._now()
        evaluations_by_candidate = {
            item["candidate_digest"]: item for item in evaluations
        }
        with self.catalog.transaction(immediate=True) as connection:
            for candidate in candidates:
                evaluation = evaluations_by_candidate.get(
                    candidate["candidate_digest"]
                )
                if evaluation is None:
                    continue
                payload = {
                    "search_round": search_round,
                    "candidate_digest": candidate["candidate_digest"],
                    "champion_eligible": candidate["champion_eligible"],
                    "evaluation": evaluation,
                }
                recorded = existing.get(candidate["candidate_digest"])
                if recorded is not None:
                    if recorded != payload:
                        raise RuntimeError("candidate evaluation evidence conflicts")
                    continue
                self._append_evaluation_evidence(
                    connection,
                    study_id=study_id,
                    evidence_type="CANDIDATE_EVALUATED",
                    candidate_digest=candidate["candidate_digest"],
                    payload=payload,
                    occurred_at=now,
                )
        return evaluations

    def _outer_records(
        self,
        study_id: str,
    ) -> dict[str, dict[str, Any]]:
        connection = self.catalog.connect()
        try:
            rows = connection.execute(
                """
                SELECT payload_json FROM parameter_study_evidence
                WHERE study_id = ? AND evidence_type = 'OUTER_SELECTION_RECORDED'
                ORDER BY sequence
                """,
                (study_id,),
            ).fetchall()
        finally:
            connection.close()
        records = [
            _strict_json_object(row["payload_json"], "outer selection evidence")
            for row in rows
        ]
        if len({record["search_round"] for record in records}) != len(records):
            raise RuntimeError("outer selection evidence contains duplicate rounds")
        return {record["search_round"]: record for record in records}

    def _record_outer_selection(
        self,
        *,
        study_id: str,
        search_round: str,
        selected: dict[str, Any] | None,
        binding: dict[str, Any] | None,
        configuration: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if selected is None:
            payload = {
                "search_round": search_round,
                "selection_outcome": "NO_ELIGIBLE_CANDIDATE",
                "selected_candidate_digest": None,
                "selected_evaluation_digest": None,
                "metric_document": None,
                "net_daily_returns": [],
            }
        else:
            if binding is None or configuration is None:
                raise RuntimeError("selected outer candidate has no binding")
            document = self._factory_document_for_binding(binding, configuration)
            if document is None:
                raise RuntimeError("selected outer binding is not canonical evidence")
            payload = {
                "search_round": search_round,
                "selection_outcome": "CANDIDATE_SELECTED",
                "selected_candidate_digest": selected["candidate_digest"],
                "selected_evaluation_digest": selected["evaluation_digest"],
                "metric_document": dict(document),
                "net_daily_returns": deepcopy(document["net_daily_returns"]),
            }
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            self._append_evaluation_evidence(
                connection,
                study_id=study_id,
                evidence_type="OUTER_SELECTION_RECORDED",
                candidate_digest=(
                    None if selected is None else selected["candidate_digest"]
                ),
                payload=payload,
                occurred_at=now,
            )
        return {
            "status": "OUTER_SELECTION_RECORDED",
            "study_id": study_id,
            **{
                key: payload[key]
                for key in ("search_round", "selected_candidate_digest")
            },
        }

    def _freeze_selection(
        self,
        *,
        study_id: str,
        frozen_plan: dict[str, Any],
        candidates: list[dict[str, Any]],
        evaluations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        eligible_digests = {
            candidate["candidate_digest"]
            for candidate in candidates
            if candidate["champion_eligible"]
        }
        champion = self.evaluation_policy.select(
            [
                evaluation
                for evaluation in evaluations
                if evaluation["candidate_digest"] in eligible_digests
            ]
        )
        outer_records = self._outer_records(study_id)
        expected_rounds = [
            f"OUTER:{item['round']}"
            for item in frozen_plan["validation"]["outer_rounds"]
        ]
        if list(outer_records) != expected_rounds:
            raise RuntimeError("outer evidence is partial or not chronological")
        ordered_returns = [
            item
            for search_round in expected_rounds
            for item in outer_records[search_round]["net_daily_returns"]
        ]
        dates = [item["date"] for item in ordered_returns]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise RuntimeError("outer OOS evidence is not contiguous and ordered")
        outer_evidence = {
            "account_policy": "FORCE_FLAT_WITH_COST",
            "rounds": [outer_records[key] for key in expected_rounds],
            "ordered_net_daily_returns": ordered_returns,
        }
        outer_evidence_digest = _digest(outer_evidence)
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            if champion is None:
                connection.execute(
                    """
                    UPDATE parameter_studies
                    SET phase = 'COMPLETED',
                        selection_outcome = 'NO_ELIGIBLE_CANDIDATE',
                        holdout_outcome = 'NOT_RUN', updated_at = ?
                    WHERE study_id = ? AND selection_outcome = 'NOT_DETERMINED'
                    """,
                    (now, study_id),
                )
                return {
                    "status": "NO_ELIGIBLE_CANDIDATE",
                    "study_id": study_id,
                    "outer_evidence_digest": outer_evidence_digest,
                }
            holdout_identity = _holdout_identity_digest(frozen_plan)
            self._append_evaluation_evidence(
                connection,
                study_id=study_id,
                evidence_type="CHAMPION_FROZEN",
                candidate_digest=champion["candidate_digest"],
                payload={
                    "candidate_digest": champion["candidate_digest"],
                    "evaluation": champion,
                    "evaluation_digest": champion["evaluation_digest"],
                    "outer_evidence": outer_evidence,
                    "outer_evidence_digest": outer_evidence_digest,
                    "holdout_identity_digest": holdout_identity,
                },
                occurred_at=now,
            )
            connection.execute(
                """
                INSERT INTO parameter_study_holdout_ledger(
                    study_id, sequence, holdout_identity_digest,
                    event_type, occurred_at, payload_json
                )
                SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?,
                       'GRANTED', ?, ?
                FROM parameter_study_holdout_ledger
                WHERE study_id = ?
                """,
                (
                    study_id,
                    holdout_identity,
                    now,
                    canonical_json_bytes(
                        {
                            "candidate_digest": champion["candidate_digest"],
                            "authorization": "ONE_FROZEN_CHAMPION",
                        }
                    ).decode(),
                    study_id,
                ),
            )
            connection.execute(
                """
                UPDATE parameter_studies
                SET phase = 'HOLDOUT_READY',
                    selection_outcome = 'CHAMPION_SELECTED', updated_at = ?
                WHERE study_id = ? AND selection_outcome = 'NOT_DETERMINED'
                """,
                (now, study_id),
            )
        return {
            "status": "CHAMPION_FROZEN",
            "study_id": study_id,
            "candidate_digest": champion["candidate_digest"],
            "evaluation_digest": champion["evaluation_digest"],
            "outer_evidence_digest": outer_evidence_digest,
        }

    def _holdout_claim(
        self,
        study_id: str,
    ) -> dict[str, Any] | None:
        connection = self.catalog.connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM parameter_study_holdout_claims
                WHERE study_id = ?
                """,
                (study_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    def _ensure_holdout_claim(
        self,
        *,
        study_id: str,
        frozen_plan: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        connection = self.catalog.connect()
        try:
            champion = connection.execute(
                """
                SELECT candidate_digest, payload_json
                FROM parameter_study_evidence
                WHERE study_id = ? AND evidence_type = 'CHAMPION_FROZEN'
                """,
                (study_id,),
            ).fetchone()
        finally:
            connection.close()
        if champion is None:
            raise RuntimeError("terminal holdout requires one frozen champion")
        candidate_digest = champion["candidate_digest"]
        fold_window = deepcopy(frozen_plan["holdout"]["fold_window"])
        binding_id = self._binding_id(
            study_id=study_id,
            search_round="HOLDOUT",
            candidate_digest=candidate_digest,
            role="TERMINAL_HOLDOUT",
            fold_sequence=1,
            fold_window=fold_window,
        )
        claim = {
            "study_id": study_id,
            "holdout_identity_digest": _holdout_identity_digest(frozen_plan),
            "candidate_digest": candidate_digest,
            "binding_id": binding_id,
            "effect_action_id": self._effect_action_id(binding_id),
        }
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM parameter_study_holdout_claims
                WHERE study_id = ?
                """,
                (study_id,),
            ).fetchone()
            if existing is not None:
                stored = {
                    key: existing[key]
                    for key in claim
                }
                if stored != claim:
                    raise RuntimeError("durable holdout claim conflicts")
                return dict(existing), False
            grant = connection.execute(
                """
                SELECT holdout_identity_digest, payload_json
                FROM parameter_study_holdout_ledger
                WHERE study_id = ? AND event_type = 'GRANTED'
                """,
                (study_id,),
            ).fetchone()
            champion_payload = _strict_json_object(
                champion["payload_json"],
                "frozen champion evidence",
            )
            if (
                grant is None
                or grant["holdout_identity_digest"]
                != claim["holdout_identity_digest"]
                or champion_payload.get("candidate_digest")
                != candidate_digest
            ):
                raise RuntimeError("holdout authorization does not match champion")
            connection.execute(
                """
                INSERT INTO parameter_study_holdout_claims(
                    study_id, holdout_identity_digest, candidate_digest,
                    binding_id, effect_action_id, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    study_id,
                    claim["holdout_identity_digest"],
                    candidate_digest,
                    binding_id,
                    claim["effect_action_id"],
                    now,
                ),
            )
        return {**claim, "claimed_at": now}, True

    def _record_holdout_ambiguous(
        self,
        *,
        study_id: str,
        binding_id: str,
    ) -> dict[str, Any]:
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM parameter_study_events
                WHERE study_id = ?
                  AND event_type = 'HOLDOUT_EXECUTION_AMBIGUOUS'
                """,
                (study_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO parameter_study_events(
                        study_id, sequence, event_type, occurred_at, payload_json
                    )
                    SELECT ?, COALESCE(MAX(sequence), 0) + 1,
                           'HOLDOUT_EXECUTION_AMBIGUOUS', ?, ?
                    FROM parameter_study_events
                    WHERE study_id = ?
                    """,
                    (
                        study_id,
                        now,
                        canonical_json_bytes(
                            {
                                "binding_id": binding_id,
                                "effect_action_id": self._effect_action_id(
                                    binding_id
                                ),
                                "access": "ACCESSED",
                                "redispatch_allowed": False,
                            }
                        ).decode(),
                        study_id,
                    ),
                )
            connection.execute(
                """
                UPDATE parameter_studies
                SET control_status = 'FAILED', updated_at = ?
                WHERE study_id = ?
                """,
                (now, study_id),
            )
        return {
            "status": "HOLDOUT_EXECUTION_AMBIGUOUS",
            "study_id": study_id,
            "binding_id": binding_id,
            "access": "ACCESSED",
            "redispatch_allowed": False,
        }

    def _dispatch_holdout_binding(
        self,
        *,
        study_id: str,
        frozen_plan: dict[str, Any],
        claim: dict[str, Any],
    ) -> dict[str, Any]:
        fold_window = deepcopy(frozen_plan["holdout"]["fold_window"])
        configuration = self._candidate_configuration(
            study_id,
            claim["candidate_digest"],
        )
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            access = connection.execute(
                """
                SELECT 1 FROM parameter_study_holdout_ledger
                WHERE study_id = ? AND event_type = 'ACCESSED'
                """,
                (study_id,),
            ).fetchone()
            if access is not None:
                raise RuntimeError(
                    "holdout access already exists without a durable binding"
                )
            connection.execute(
                """
                INSERT INTO parameter_study_holdout_ledger(
                    study_id, sequence, holdout_identity_digest,
                    event_type, occurred_at, payload_json
                )
                SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?,
                       'ACCESSED', ?, ?
                FROM parameter_study_holdout_ledger
                WHERE study_id = ?
                """,
                (
                    study_id,
                    claim["holdout_identity_digest"],
                    now,
                    canonical_json_bytes(
                        {
                            "candidate_digest": claim["candidate_digest"],
                            "binding_id": claim["binding_id"],
                            "effect_action_id": claim["effect_action_id"],
                            "parent_dataset_snapshot_id": frozen_plan["dataset"][
                                "snapshot_id"
                            ],
                            "fold_window": fold_window,
                            "access_boundary": "BEFORE_DATASET_MATERIALIZATION",
                        }
                    ).decode(),
                    study_id,
                ),
            )
            connection.execute(
                """
                UPDATE parameter_studies
                SET phase = 'HOLDOUT_RUNNING', updated_at = ?
                WHERE study_id = ? AND phase = 'HOLDOUT_READY'
                """,
                (now, study_id),
            )
        dataset_slice = self.dataset_slice_factory.materialize(
            frozen_plan["dataset"],
            fold_window,
        )
        task = self._task_for_candidate(
            candidate=configuration,
            instrument=frozen_plan["dataset"]["instrument"],
            snapshot_id=dataset_slice["snapshot_id"],
            fold_window=fold_window,
        )
        preview = self.experiments.preview_task(task)
        if preview["duplicate"]:
            attempts = self.experiments.list_attempts(preview["experiment_id"])
            if not attempts:
                raise RuntimeError("duplicate holdout Experiment has no Attempt")
            expected_attempt_id = attempts[0]["attempt_id"]
        else:
            expected_attempt_id = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "experiment_id": preview["experiment_id"],
                        "action_id": claim["effect_action_id"],
                        "sequence": 1,
                    }
                )
            ).hexdigest()
        submission = self.experiments.submit_study_effect(
            task,
            action_id=claim["effect_action_id"],
        )
        if (
            submission["experiment_id"] != preview["experiment_id"]
            or submission["attempt_id"] != expected_attempt_id
        ):
            raise RuntimeError("holdout Experiment identity changed after access")
        with self.catalog.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO parameter_study_bindings(
                    binding_id, study_id, search_round, candidate_digest,
                    role, fold_sequence, fold_window_json, task_json,
                    task_digest, dataset_snapshot_id, experiment_id,
                    submitted_attempt_id, attempt_id, state,
                    metric_document_json, created_at, updated_at
                ) VALUES (
                    ?, ?, 'HOLDOUT', ?, 'TERMINAL_HOLDOUT', 1, ?, ?, ?, ?,
                    ?, ?, ?, 'SUBMITTED', NULL, ?, ?
                )
                """,
                (
                    claim["binding_id"],
                    study_id,
                    claim["candidate_digest"],
                    canonical_json_bytes(fold_window).decode(),
                    canonical_json_bytes(task).decode(),
                    _digest(task),
                    dataset_slice["snapshot_id"],
                    submission["experiment_id"],
                    submission["attempt_id"],
                    submission["attempt_id"],
                    now,
                    now,
                ),
            )
        return {
            "status": "ATTEMPT_SUBMITTED",
            "study_id": study_id,
            "binding_id": claim["binding_id"],
            "candidate_digest": claim["candidate_digest"],
            "role": "TERMINAL_HOLDOUT",
            "experiment_id": submission["experiment_id"],
            "attempt_id": submission["attempt_id"],
        }

    def _record_holdout_outcome(
        self,
        *,
        study_id: str,
        frozen_plan: dict[str, Any],
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        configuration = self._candidate_configuration(
            study_id,
            binding["candidate_digest"],
        )
        document = self._factory_document_for_binding(binding, configuration)
        if document is None:
            raise RuntimeError("verified holdout binding lost canonical evidence")
        evaluation = self.evaluation_policy.evaluate(
            binding["candidate_digest"],
            [document],
            frozen_plan["evaluation"]["parameters"],
        )
        outcome = "PASSED" if evaluation["eligible"] else "FAILED"
        now = self._now()
        with self.catalog.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM parameter_study_evidence
                WHERE study_id = ?
                  AND evidence_type = 'HOLDOUT_OUTCOME_RECORDED'
                """,
                (study_id,),
            ).fetchone()
            payload = {
                "candidate_digest": binding["candidate_digest"],
                "binding_id": binding["binding_id"],
                "experiment_id": binding["experiment_id"],
                "attempt_id": document["attempt_id"],
                "metric_document": dict(document),
                "metric_document_digest": document["document_digest"],
                "evaluation": evaluation,
                "evaluation_digest": evaluation["evaluation_digest"],
                "constraints": evaluation["constraints"],
                "outcome": outcome,
            }
            if existing is None:
                self._append_evaluation_evidence(
                    connection,
                    study_id=study_id,
                    evidence_type="HOLDOUT_OUTCOME_RECORDED",
                    candidate_digest=binding["candidate_digest"],
                    payload=payload,
                    occurred_at=now,
                )
                connection.execute(
                    """
                    UPDATE parameter_studies
                    SET phase = 'COMPLETED', holdout_outcome = ?, updated_at = ?
                    WHERE study_id = ? AND phase = 'HOLDOUT_RUNNING'
                      AND holdout_outcome = 'NOT_RUN'
                    """,
                    (outcome, now, study_id),
                )
            elif _strict_json_object(
                existing["payload_json"],
                "holdout outcome evidence",
            ) != payload:
                raise RuntimeError("holdout outcome evidence conflicts")
        return {
            "status": f"HOLDOUT_{outcome}",
            "study_id": study_id,
            "candidate_digest": binding["candidate_digest"],
            "binding_id": binding["binding_id"],
            "experiment_id": binding["experiment_id"],
            "attempt_id": document["attempt_id"],
            "metric_document_digest": document["document_digest"],
            "evaluation_digest": evaluation["evaluation_digest"],
        }

    def _advance_holdout(
        self,
        study_id: str,
        frozen_plan: dict[str, Any],
    ) -> dict[str, Any]:
        claim, created = self._ensure_holdout_claim(
            study_id=study_id,
            frozen_plan=frozen_plan,
        )
        if created:
            return {
                "status": "HOLDOUT_CLAIMED",
                "study_id": study_id,
                "binding_id": claim["binding_id"],
                "candidate_digest": claim["candidate_digest"],
                "holdout_identity_digest": claim["holdout_identity_digest"],
                "effect_action_id": claim["effect_action_id"],
            }
        binding = self._binding(claim["binding_id"])
        if binding is None:
            connection = self.catalog.connect()
            try:
                accessed = connection.execute(
                    """
                    SELECT 1 FROM parameter_study_holdout_ledger
                    WHERE study_id = ? AND event_type = 'ACCESSED'
                    """,
                    (study_id,),
                ).fetchone()
            finally:
                connection.close()
            if accessed is not None:
                return self._record_holdout_ambiguous(
                    study_id=study_id,
                    binding_id=claim["binding_id"],
                )
            return self._dispatch_holdout_binding(
                study_id=study_id,
                frozen_plan=frozen_plan,
                claim=claim,
            )
        if binding["state"] == "CONTESTED":
            return {
                "status": "EVIDENCE_CONTESTED",
                "study_id": study_id,
                "binding_id": binding["binding_id"],
                "experiment_id": binding["experiment_id"],
            }
        if binding["state"] == "SUBMITTED":
            return self._observe_binding(
                study_id=study_id,
                binding=binding,
                configuration=self._candidate_configuration(
                    study_id,
                    binding["candidate_digest"],
                ),
            )
        return self._record_holdout_outcome(
            study_id=study_id,
            frozen_plan=frozen_plan,
            binding=binding,
        )

    def _advance_selection(
        self,
        study_id: str,
        frozen_plan: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            frozen_plan["evaluation"]["content_digest"]
            != EVALUATION_POLICY_DIGEST
            or frozen_plan["evaluation"]["manifest"]
            != EVALUATION_POLICY_IDENTITY
        ):
            raise RuntimeError("frozen evaluation policy identity has drifted")
        for binding in self._bindings(study_id):
            if binding["state"] == "VERIFIED":
                experiment = self.experiments.experiment_detail(
                    binding["experiment_id"]
                )
                if experiment["has_divergent_attempt"]:
                    return self._record_contested_binding(
                        study_id=study_id,
                        binding=binding,
                        experiment=experiment,
                    )
        outer_records = self._outer_records(study_id)
        for search_round, inner_folds, outer_fold in self._selection_rounds(
            frozen_plan
        ):
            if search_round != "FINAL" and search_round in outer_records:
                continue
            proposed_candidates = self._round_candidates(
                study_id,
                frozen_plan,
                search_round,
            )
            self._ensure_trials(
                study_id=study_id,
                search_round=search_round,
                candidates=proposed_candidates,
            )
            candidates = proposed_candidates
            bindings = self._bindings(study_id, search_round=search_round)
            refreshed = next(
                (
                    binding
                    for binding in bindings
                    if binding["state"] == "FAILED"
                    and self.experiments.experiment_detail(
                        binding["experiment_id"]
                    )["attempts"][-1]["attempt_id"]
                    != binding["attempt_id"]
                ),
                None,
            )
            if refreshed is not None:
                return self._observe_binding(
                    study_id=study_id,
                    binding=refreshed,
                    configuration=self._candidate_configuration(
                        study_id,
                        refreshed["candidate_digest"],
                    ),
                )
            submitted = next(
                (
                    binding
                    for binding in bindings
                    if binding["state"] == "SUBMITTED"
                ),
                None,
            )
            if submitted is not None:
                return self._observe_binding(
                    study_id=study_id,
                    binding=submitted,
                    configuration=self._candidate_configuration(
                        study_id,
                        submitted["candidate_digest"],
                    ),
                )
            for candidate in candidates:
                for fold_sequence, fold_window in enumerate(inner_folds, start=1):
                    binding_id = self._binding_id(
                        study_id=study_id,
                        search_round=search_round,
                        candidate_digest=candidate["candidate_digest"],
                        role="INNER_SCORE",
                        fold_sequence=fold_sequence,
                        fold_window=fold_window,
                    )
                    if not any(
                        binding["binding_id"] == binding_id
                        for binding in bindings
                    ):
                        return self._dispatch_binding(
                            study_id=study_id,
                            frozen_plan=frozen_plan,
                            search_round=search_round,
                            candidate=candidate,
                            fold_window=fold_window,
                            fold_sequence=fold_sequence,
                        )
            evaluations = self._evaluate_round_candidates(
                study_id=study_id,
                frozen_plan=frozen_plan,
                search_round=search_round,
                candidates=candidates,
                inner_folds=inner_folds,
            )
            if evaluations is None:
                raise RuntimeError("partial inner evidence reached evaluation boundary")
            eligible_digests = {
                candidate["candidate_digest"]
                for candidate in candidates
                if candidate["champion_eligible"]
            }
            selected = self.evaluation_policy.select(
                [
                    evaluation
                    for evaluation in evaluations
                    if evaluation["candidate_digest"] in eligible_digests
                ]
            )
            if search_round == "FINAL":
                return self._freeze_selection(
                    study_id=study_id,
                    frozen_plan=frozen_plan,
                    candidates=candidates,
                    evaluations=evaluations,
                )
            if selected is None:
                return self._record_outer_selection(
                    study_id=study_id,
                    search_round=search_round,
                    selected=None,
                    binding=None,
                    configuration=None,
                )
            if outer_fold is None:
                raise RuntimeError("outer search round has no audit fold")
            outer_binding_id = self._binding_id(
                study_id=study_id,
                search_round=search_round,
                candidate_digest=selected["candidate_digest"],
                role="OUTER_AUDIT",
                fold_sequence=1,
                fold_window=outer_fold,
            )
            outer_binding = next(
                (
                    binding
                    for binding in bindings
                    if binding["binding_id"] == outer_binding_id
                ),
                None,
            )
            selected_candidate = next(
                candidate
                for candidate in candidates
                if candidate["candidate_digest"] == selected["candidate_digest"]
            )
            if outer_binding is None:
                return self._dispatch_binding(
                    study_id=study_id,
                    frozen_plan=frozen_plan,
                    search_round=search_round,
                    candidate=selected_candidate,
                    fold_window=outer_fold,
                    fold_sequence=1,
                )
            if outer_binding["state"] != "VERIFIED":
                raise RuntimeError("outer binding is not verified")
            return self._record_outer_selection(
                study_id=study_id,
                search_round=search_round,
                selected=selected,
                binding=outer_binding,
                configuration=selected_candidate["configuration"],
            )
        raise RuntimeError("Parameter Study selection state is inconsistent")

    def advance(self, study_id: str) -> dict[str, Any]:
        if not isinstance(study_id, str) or STUDY_ID.fullmatch(study_id) is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        with self._advance_lock:
            return self._advance_locked(study_id)

    def _advance_locked(self, study_id: str) -> dict[str, Any]:
        acquired = self._acquire_lease(study_id)
        if acquired["status"] != "ACQUIRED":
            return acquired
        lease = acquired["lease"]
        effect_to_execute: dict[str, Any] | None = None
        effect_action_id: str | None = None
        effect_digest: str | None = None
        dispatch_action_id: str | None = None

        connection = self.catalog.connect()
        try:
            study = connection.execute(
                """
                SELECT phase, control_status, frozen_plan_json
                FROM parameter_studies WHERE study_id = ?
                """,
                (study_id,),
            ).fetchone()
            binding_reconciliation = self._has_binding_reconciliation(
                connection,
                study_id,
            )
        finally:
            connection.close()
        if study is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        for binding in self._bindings(study_id):
            if binding["state"] == "CONTESTED":
                continue
            experiment = self.experiments.experiment_detail(
                binding["experiment_id"]
            )
            if experiment["has_divergent_attempt"]:
                return self._record_contested_binding(
                    study_id=study_id,
                    binding=binding,
                    experiment=experiment,
                )
        if (
            study["phase"] == "VALIDATING_SELECTION_PROCESS"
            and (
                study["control_status"] == "ACTIVE"
                or binding_reconciliation
            )
        ):
            frozen_plan = _strict_json_object(
                study["frozen_plan_json"],
                "Parameter Study frozen plan",
            )
            with self.catalog.transaction(immediate=True) as connection:
                lease_is_current, now_value, current = self._lease_is_current(
                    connection,
                    study_id,
                    lease,
                )
                if not lease_is_current:
                    return {
                        "status": "LEASE_BUSY",
                        "study_id": study_id,
                        "lease": current,
                    }
                drift = self._record_execution_identity_drift(
                    connection,
                    study_id=study_id,
                    frozen_plan=frozen_plan,
                    lease=lease,
                    now=_utc_text(now_value),
                )
                if drift is not None:
                    return drift
            return self._advance_selection(study_id, frozen_plan)
        if (
            study["phase"] in {"HOLDOUT_READY", "HOLDOUT_RUNNING"}
            and (
                study["control_status"] == "ACTIVE"
                or binding_reconciliation
            )
        ):
            frozen_plan = _strict_json_object(
                study["frozen_plan_json"],
                "Parameter Study frozen plan",
            )
            if study["phase"] == "HOLDOUT_READY":
                with self.catalog.transaction(immediate=True) as connection:
                    lease_is_current, now_value, current = self._lease_is_current(
                        connection,
                        study_id,
                        lease,
                    )
                    if not lease_is_current:
                        return {
                            "status": "LEASE_BUSY",
                            "study_id": study_id,
                            "lease": current,
                        }
                    drift = self._record_execution_identity_drift(
                        connection,
                        study_id=study_id,
                        frozen_plan=frozen_plan,
                        lease=lease,
                        now=_utc_text(now_value),
                    )
                    if drift is not None:
                        return drift
            return self._advance_holdout(study_id, frozen_plan)

        with self.catalog.transaction(immediate=True) as connection:
            lease_is_current, now_value, current = self._lease_is_current(
                connection,
                study_id,
                lease,
            )
            if not lease_is_current:
                return {
                    "status": "LEASE_BUSY",
                    "study_id": study_id,
                    "lease": current,
                }
            now = _utc_text(now_value)
            readiness = self._classify_readiness(connection, study_id)
            if not readiness.requires_lease:
                if readiness.response is None:
                    raise RuntimeError("Study readiness response is missing")
                return readiness.response
            if not readiness.reconciliation_only:
                drift = self._record_execution_identity_drift(
                    connection,
                    study_id=study_id,
                    frozen_plan=readiness.frozen_plan,
                    lease=lease,
                    now=now,
                )
                if drift is not None:
                    return drift

            if readiness.classification == "ADVANCE_PHASE":
                next_phase = "VALIDATING_SELECTION_PROCESS"
                connection.execute(
                    """
                    UPDATE parameter_studies
                    SET phase = ?, updated_at = ?
                    WHERE study_id = ?
                    """,
                    (next_phase, now, study_id),
                )
                connection.execute(
                    """
                    INSERT INTO parameter_study_events(
                        study_id, sequence, event_type, occurred_at, payload_json
                    )
                    SELECT ?, COALESCE(MAX(sequence), 0) + 1,
                           'STUDY_PHASE_ADVANCED', ?, ?
                    FROM parameter_study_events
                    WHERE study_id = ?
                    """,
                    (
                        study_id,
                        now,
                        canonical_json_bytes(
                            {
                                "fencing_token": lease["fencing_token"],
                                "from_phase": "FROZEN",
                                "owner_nonce": lease["owner_nonce"],
                                "to_phase": next_phase,
                            }
                        ).decode(),
                        study_id,
                    ),
                )
                return {
                    "status": "ADVANCED",
                    "study_id": study_id,
                    "effect": "STATE_TRANSITION",
                    "from_phase": "FROZEN",
                    "phase": next_phase,
                    "fencing_token": lease["fencing_token"],
                    "owner_nonce": lease["owner_nonce"],
                }

            if readiness.effect is None or readiness.effect_digest is None:
                raise RuntimeError("Study readiness effect is missing")
            effect_to_execute = readiness.effect
            effect_digest = readiness.effect_digest
            effect_action_id = self._effect_action_id(effect_digest)
            if readiness.classification == "RECORD_EFFECT_INTENT":
                response = {
                    "status": "EFFECT_PENDING",
                    "study_id": study_id,
                    "action_id": effect_action_id,
                    "effect": effect_to_execute,
                    "fencing_token": lease["fencing_token"],
                    "owner": lease["owner"],
                    "owner_nonce": lease["owner_nonce"],
                }
                connection.execute(
                    """
                    INSERT INTO parameter_study_actions(
                        action_id, operation, study_id, request_digest,
                        response_json, created_at
                    ) VALUES (?, 'EFFECT_INTENT', ?, ?, ?, ?)
                    """,
                    (
                        effect_action_id,
                        study_id,
                        effect_digest,
                        canonical_json_bytes(response).decode(),
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO parameter_study_events(
                        study_id, sequence, event_type, occurred_at, payload_json
                    )
                    SELECT ?, COALESCE(MAX(sequence), 0) + 1,
                           'STUDY_EFFECT_INTENT_RECORDED', ?, ?
                    FROM parameter_study_events
                    WHERE study_id = ?
                    """,
                    (
                        study_id,
                        now,
                        canonical_json_bytes(
                            {
                                "action_id": effect_action_id,
                                "effect_type": effect_to_execute["effect_type"],
                                "fencing_token": lease["fencing_token"],
                                "owner_nonce": lease["owner_nonce"],
                            }
                        ).decode(),
                        study_id,
                    ),
                )
                if effect_to_execute["effect_type"] == "REQUEST_TERMINAL_HOLDOUT":
                    connection.execute(
                        """
                        UPDATE parameter_studies
                        SET phase = 'HOLDOUT_RUNNING'
                        WHERE study_id = ? AND phase = 'HOLDOUT_READY'
                        """,
                        (study_id,),
                    )
                connection.execute(
                    """
                    UPDATE parameter_studies
                    SET updated_at = ?
                    WHERE study_id = ?
                    """,
                    (now, study_id),
                )
                return response

            if readiness.classification not in {
                "DISPATCH_EFFECT",
                "RECONCILE_EFFECT",
            }:
                raise RuntimeError(
                    "Parameter Study readiness classification is invalid"
                )
            dispatch_action_id = self._dispatch_action_id(
                effect_digest,
                lease["fencing_token"],
            )
            existing_dispatch = self._load_action(
                dispatch_action_id,
                connection,
            )
            if existing_dispatch is None:
                dispatch_response = {
                    "status": "EFFECT_AUTHORIZED",
                    "study_id": study_id,
                    "action_id": effect_action_id,
                    "dispatch_action_id": dispatch_action_id,
                    "effect": effect_to_execute,
                    "fencing_token": lease["fencing_token"],
                    "owner": lease["owner"],
                    "owner_nonce": lease["owner_nonce"],
                }
                connection.execute(
                    """
                    INSERT INTO parameter_study_actions(
                        action_id, operation, study_id, request_digest,
                        response_json, created_at
                    ) VALUES (
                        ?, 'EFFECT_DISPATCH_AUTHORIZATION', ?, ?, ?, ?
                    )
                    """,
                    (
                        dispatch_action_id,
                        study_id,
                        self._dispatch_request_digest(
                            effect_action_id=effect_action_id,
                            effect_digest=effect_digest,
                            dispatch_action_id=dispatch_action_id,
                            lease=lease,
                        ),
                        canonical_json_bytes(dispatch_response).decode(),
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO parameter_study_events(
                        study_id, sequence, event_type,
                        occurred_at, payload_json
                    )
                    SELECT ?, COALESCE(MAX(sequence), 0) + 1,
                           'STUDY_EFFECT_DISPATCH_AUTHORIZED', ?, ?
                    FROM parameter_study_events
                    WHERE study_id = ?
                    """,
                    (
                        study_id,
                        now,
                        canonical_json_bytes(
                            {
                                "action_id": effect_action_id,
                                "dispatch_action_id": dispatch_action_id,
                                "effect_type": effect_to_execute["effect_type"],
                                "fencing_token": lease["fencing_token"],
                                "owner": lease["owner"],
                                "owner_nonce": lease["owner_nonce"],
                            }
                        ).decode(),
                        study_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE parameter_studies
                    SET updated_at = ?
                    WHERE study_id = ?
                    """,
                    (now, study_id),
                )
            elif self._valid_dispatch_action(
                connection,
                existing_dispatch,
                study_id=study_id,
                effect=effect_to_execute,
                effect_digest=effect_digest,
            ) is None:
                return {
                    "status": "ACTION_CONFLICT",
                    "action_id": dispatch_action_id,
                }

        if (
            effect_to_execute is None
            or effect_action_id is None
            or effect_digest is None
            or dispatch_action_id is None
            or self.effect_executor is None
        ):
            raise RuntimeError("Parameter Study effect dispatch was not resolved")
        dispatched_effect = deepcopy(effect_to_execute)
        dispatched_effect["coordination"] = {
            "action_id": effect_action_id,
            "dispatch_action_id": dispatch_action_id,
            "fencing_token": lease["fencing_token"],
            "owner": lease["owner"],
            "owner_nonce": lease["owner_nonce"],
        }
        result = self.effect_executor(dispatched_effect, effect_action_id)
        if (
            type(result) is not dict
            or set(result) != {"experiment_id", "attempt_id"}
            or any(
                not isinstance(result[key], str)
                or STUDY_ID.fullmatch(result[key]) is None
                for key in ("experiment_id", "attempt_id")
            )
        ):
            raise RuntimeError(
                "Parameter Study effects may return only durable "
                "Experiment/Attempt identifiers"
            )
        response = {
            "status": "EFFECT_COMMITTED",
            "study_id": study_id,
            "action_id": effect_action_id,
            "dispatch_action_id": dispatch_action_id,
            "effect": effect_to_execute,
            "result": result,
            "fencing_token": lease["fencing_token"],
            "owner": lease["owner"],
            "owner_nonce": lease["owner_nonce"],
        }
        receipt_action_id = self._receipt_action_id(effect_digest)
        receipt_request_digest = self._receipt_request_digest(response)
        with self.catalog.transaction(immediate=True) as connection:
            lease_is_current, receipt_now_value, current = self._lease_is_current(
                connection,
                study_id,
                lease,
            )
            if not lease_is_current:
                return {
                    "status": "LEASE_BUSY",
                    "study_id": study_id,
                    "lease": current,
                }
            effect_attempt = connection.execute(
                """
                SELECT attempt_id, experiment_id, action_id, sequence
                FROM attempts WHERE action_id = ?
                """,
                (effect_action_id,),
            ).fetchone()
            if effect_attempt is not None:
                valid_attempt = self._valid_effect_attempt(
                    connection,
                    action_id=effect_action_id,
                    frozen_plan=readiness.frozen_plan,
                )
                if (
                    valid_attempt is None
                    or result.get("experiment_id")
                    != valid_attempt["experiment_id"]
                    or result.get("attempt_id") != valid_attempt["attempt_id"]
                ):
                    return {
                        "status": "ACTION_CONFLICT",
                        "action_id": effect_action_id,
                    }
            existing_receipt = self._load_action(
                receipt_action_id,
                connection,
            )
            if existing_receipt is not None:
                replay = self._valid_receipt_action(
                    connection,
                    existing_receipt,
                    study_id=study_id,
                    effect=effect_to_execute,
                    effect_digest=effect_digest,
                    frozen_plan=readiness.frozen_plan,
                )
                if replay is not None:
                    return replay
                return {
                    "status": "ACTION_CONFLICT",
                    "action_id": receipt_action_id,
                }
            receipt_now = _utc_text(receipt_now_value)
            connection.execute(
                """
                INSERT INTO parameter_study_actions(
                    action_id, operation, study_id, request_digest,
                    response_json, created_at
                ) VALUES (?, 'EFFECT_RECEIPT', ?, ?, ?, ?)
                """,
                (
                    receipt_action_id,
                    study_id,
                    receipt_request_digest,
                    canonical_json_bytes(response).decode(),
                    receipt_now,
                ),
            )
            connection.execute(
                """
                INSERT INTO parameter_study_events(
                    study_id, sequence, event_type, occurred_at, payload_json
                )
                SELECT ?, COALESCE(MAX(sequence), 0) + 1,
                       'STUDY_EFFECT_COMMITTED', ?, ?
                FROM parameter_study_events
                WHERE study_id = ?
                """,
                (
                    study_id,
                    receipt_now,
                    canonical_json_bytes(
                        {
                            "action_id": effect_action_id,
                            "dispatch_action_id": dispatch_action_id,
                            "effect_type": effect_to_execute["effect_type"],
                            "fencing_token": lease["fencing_token"],
                            "owner": lease["owner"],
                            "owner_nonce": lease["owner_nonce"],
                            "result": result,
                        }
                    ).decode(),
                    study_id,
                ),
            )
            connection.execute(
                """
                UPDATE parameter_studies
                SET updated_at = ?
                WHERE study_id = ?
                """,
                (receipt_now, study_id),
            )
            return response

    def _advance_next_runnable(self) -> dict[str, Any] | None:
        connection = self.catalog.connect()
        try:
            connection.execute("BEGIN")
            candidate_ids = [
                row["study_id"]
                for row in connection.execute(
                    """
                    SELECT study_id
                    FROM parameter_studies
                    ORDER BY updated_at, created_at, study_id
                    """
                ).fetchall()
            ]
            runnable = [
                study_id
                for study_id in candidate_ids
                if self._classify_readiness(
                    connection,
                    study_id,
                ).discoverable
            ]
        finally:
            connection.rollback()
            connection.close()

        for study_id in runnable:
            result = self.advance(study_id)
            if result["status"] in {"LEASE_BUSY", "NO_CHANGE"}:
                continue
            return result
        return None

    def control(
        self,
        study_id: str,
        operation: str,
        *,
        action_id: str,
    ) -> dict[str, Any]:
        if not isinstance(study_id, str) or STUDY_ID.fullmatch(study_id) is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        _validate_public_action_id(action_id)
        if not isinstance(operation, str) or operation.upper() not in {
            "PAUSE",
            "RESUME",
            "CANCEL",
        }:
            raise StudyValidationError(
                "control operation must be PAUSE, RESUME, or CANCEL"
            )
        operation = operation.upper()
        request_digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "operation": operation,
                    "study_id": study_id,
                }
            )
        ).hexdigest()

        with self.catalog.transaction(immediate=True) as connection:
            self._validate_study_projection(connection, study_id)
            existing_action = self._load_action(action_id, connection)
            if existing_action is not None:
                if (
                    not self._action_header_matches(
                        existing_action,
                        operation=f"CONTROL_{operation}",
                        study_id=study_id,
                        action_id=action_id,
                        request_digest=request_digest,
                    )
                ):
                    return {
                        "status": "ACTION_CONFLICT",
                        "action_id": action_id,
                    }
                try:
                    response = _strict_json_object(
                        existing_action["response_json"],
                        "Parameter Study control response",
                    )
                except RuntimeError:
                    return {
                        "status": "ACTION_CONFLICT",
                        "action_id": action_id,
                    }
                expected_status = {
                    "PAUSE": "PAUSED",
                    "RESUME": "RESUMED",
                    "CANCEL": "CANCELLED",
                }[operation]
                transitioned = (
                    set(response)
                    == {"status", "study_id", "control_status"}
                    and response["status"] in {expected_status, "NO_CHANGE"}
                    and response["study_id"] == study_id
                )
                invalid = (
                    set(response)
                    == {
                        "status",
                        "study_id",
                        "operation",
                        "control_status",
                    }
                    and response["status"] == "INVALID_TRANSITION"
                    and response["study_id"] == study_id
                    and response["operation"] == operation
                )
                if not transitioned and not invalid:
                    return {
                        "status": "ACTION_CONFLICT",
                        "action_id": action_id,
                    }
                return response

            study = connection.execute(
                """
                SELECT phase, control_status
                FROM parameter_studies
                WHERE study_id = ?
                """,
                (study_id,),
            ).fetchone()
            if study is None:
                raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
            if operation in {"PAUSE", "CANCEL"}:
                readiness = self._classify_readiness(connection, study_id)
                if readiness.dispatch_in_flight:
                    return {
                        "status": "LEASE_BUSY",
                        "study_id": study_id,
                        "reason": "DISPATCH_IN_FLIGHT",
                    }
            now = self._now()
            transitions = {
                "PAUSE": {"ACTIVE": "PAUSED"},
                "RESUME": {"PAUSED": "ACTIVE"},
                "CANCEL": {
                    "ACTIVE": "CANCELLED",
                    "PAUSED": "CANCELLED",
                },
            }
            response_status = {
                "PAUSE": "PAUSED",
                "RESUME": "RESUMED",
                "CANCEL": "CANCELLED",
            }
            event_type = {
                "PAUSE": "STUDY_PAUSED",
                "RESUME": "STUDY_RESUMED",
                "CANCEL": "STUDY_CANCELLED",
            }
            current_status = study["control_status"]
            target_status = transitions[operation].get(current_status)
            if target_status is None:
                if (
                    (operation == "PAUSE" and current_status == "PAUSED")
                    or (operation == "RESUME" and current_status == "ACTIVE")
                    or (operation == "CANCEL" and current_status == "CANCELLED")
                ):
                    response = {
                        "status": "NO_CHANGE",
                        "study_id": study_id,
                        "control_status": current_status,
                    }
                else:
                    response = {
                        "status": "INVALID_TRANSITION",
                        "study_id": study_id,
                        "operation": operation,
                        "control_status": current_status,
                    }
            else:
                response = {
                    "status": response_status[operation],
                    "study_id": study_id,
                    "control_status": target_status,
                }
                connection.execute(
                    """
                    UPDATE parameter_studies
                    SET control_status = ?, updated_at = ?
                    WHERE study_id = ?
                    """,
                    (target_status, now, study_id),
                )
                connection.execute(
                    """
                    INSERT INTO parameter_study_events(
                        study_id, sequence, event_type, occurred_at, payload_json
                    )
                    SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ?
                    FROM parameter_study_events
                    WHERE study_id = ?
                    """,
                    (
                        study_id,
                        event_type[operation],
                        now,
                        canonical_json_bytes(
                            {
                                "action_id": action_id,
                                "previous_control_status": current_status,
                            }
                        ).decode(),
                        study_id,
                    ),
                )
            connection.execute(
                """
                INSERT INTO parameter_study_actions(
                    action_id, operation, study_id, request_digest,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    f"CONTROL_{operation}",
                    study_id,
                    request_digest,
                    canonical_json_bytes(response).decode(),
                    now,
                ),
            )
            return response

    def list(self) -> list[dict[str, Any]]:
        connection = self.catalog.connect()
        try:
            rows = connection.execute(
                """
                SELECT study_id
                FROM parameter_studies
                ORDER BY created_at DESC, rowid DESC
                """
            ).fetchall()
        finally:
            connection.close()
        return [self.detail(row["study_id"]) for row in rows]

    def detail(self, study_id: str) -> dict[str, Any]:
        if not isinstance(study_id, str) or STUDY_ID.fullmatch(study_id) is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        connection = self.catalog.connect()
        try:
            connection.execute("BEGIN")
            self._validate_study_projection(connection, study_id)
            study = connection.execute(
                "SELECT * FROM parameter_studies WHERE study_id = ?",
                (study_id,),
            ).fetchone()
            if study is None:
                raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
            frozen_plan = _strict_json_object(
                study["frozen_plan_json"],
                "Parameter Study frozen plan",
            )
            holdout_identity = _holdout_identity_digest(frozen_plan)
            events = connection.execute(
                """
                SELECT sequence, event_type, occurred_at, payload_json
                FROM parameter_study_events
                WHERE study_id = ?
                ORDER BY sequence
                """,
                (study_id,),
            ).fetchall()
            holdout_ledger = connection.execute(
                """
                SELECT sequence, holdout_identity_digest, event_type,
                       occurred_at, payload_json
                FROM parameter_study_holdout_ledger
                WHERE study_id = ?
                ORDER BY sequence
                """,
                (study_id,),
            ).fetchall()
            holdout_claim = connection.execute(
                """
                SELECT * FROM parameter_study_holdout_claims
                WHERE study_id = ?
                """,
                (study_id,),
            ).fetchone()
            prior_exposure = connection.execute(
                """
                SELECT 1
                FROM parameter_study_holdout_ledger
                WHERE holdout_identity_digest = ?
                  AND event_type IN ('ACCESSED', 'EXPOSURE_RECORDED')
                LIMIT 1
                """,
                (holdout_identity,),
            ).fetchone()
            holdout_history = connection.execute(
                """
                SELECT pre_ledger_history_complete
                FROM parameter_study_holdout_history_metadata
                WHERE singleton = 1
                """
            ).fetchone()
            evidence = connection.execute(
                """
                SELECT sequence, evidence_type, candidate_digest,
                       payload_json, occurred_at
                FROM parameter_study_evidence
                WHERE study_id = ?
                ORDER BY sequence
                """,
                (study_id,),
            ).fetchall()
            trials = connection.execute(
                """
                SELECT candidate_digest, configuration_json, first_search_round,
                       proposal_sequence, classification, created_at
                FROM parameter_study_trials
                WHERE study_id = ?
                ORDER BY proposal_sequence, candidate_digest
                """,
                (study_id,),
            ).fetchall()
            bindings = connection.execute(
                """
                SELECT *
                FROM parameter_study_bindings
                WHERE study_id = ?
                ORDER BY search_round, role, fold_sequence, candidate_digest
                """,
                (study_id,),
            ).fetchall()
            lease = self._latest_lease(connection, study_id)
        finally:
            connection.rollback()
            connection.close()

        if holdout_history is None:
            raise RuntimeError("holdout history metadata is missing")
        if any(
            row["holdout_identity_digest"] != holdout_identity
            for row in holdout_ledger
        ):
            raise RuntimeError("holdout ledger identity does not match its Study")
        holdout_events = {row["event_type"] for row in holdout_ledger}
        if "ACCESSED" in holdout_events:
            holdout_access = "ACCESSED"
        elif "GRANTED" in holdout_events:
            holdout_access = "GRANTED"
        else:
            holdout_access = "SEALED"
        if prior_exposure is not None:
            holdout_freshness = "PREVIOUSLY_EXPOSED"
        else:
            holdout_freshness = "LEGACY_UNKNOWN"
        identities = {
            "dataset": frozen_plan["dataset"],
            "template": {
                key: frozen_plan["template"][key]
                for key in ("name", "version", "content_digest")
            },
            "operators": {
                slot: {
                    key: operator[key]
                    for key in (
                        "operator_id",
                        "resolved_version",
                        "content_digest",
                    )
                }
                for slot, operator in frozen_plan["operators"].items()
            },
            "evaluation": {
                key: frozen_plan["evaluation"][key]
                for key in ("policy_id", "resolved_version", "content_digest")
            },
            "metric_engine": frozen_plan["metric_engine"],
            "execution": frozen_plan["execution"]["identity"],
        }
        evidence_items = [
            {
                "sequence": item["sequence"],
                "evidence_type": item["evidence_type"],
                "candidate_digest": item["candidate_digest"],
                "occurred_at": item["occurred_at"],
                "payload": _strict_json_object(
                    item["payload_json"],
                    "Parameter Study evidence payload",
                ),
            }
            for item in evidence
        ]
        final_evaluations = [
            item["payload"]
            for item in evidence_items
            if item["evidence_type"] == "CANDIDATE_EVALUATED"
            and item["payload"].get("search_round") == "FINAL"
        ]
        rankings = sorted(
            [
                {
                    "candidate_digest": item["candidate_digest"],
                    "champion_eligible": item["champion_eligible"],
                    **item["evaluation"],
                }
                for item in final_evaluations
            ],
            key=lambda item: (
                not item["champion_eligible"],
                not item["eligible"],
                *self.evaluation_policy.ranking_key(item),
            ),
        )
        champion_evidence = next(
            (
                item["payload"]
                for item in evidence_items
                if item["evidence_type"] == "CHAMPION_FROZEN"
            ),
            None,
        )
        holdout_evidence = next(
            (
                item["payload"]
                for item in evidence_items
                if item["evidence_type"] == "HOLDOUT_OUTCOME_RECORDED"
            ),
            None,
        )
        recorded_outer_rounds = [
            item["payload"]
            for item in evidence_items
            if item["evidence_type"] == "OUTER_SELECTION_RECORDED"
        ]
        derived_outer_evidence = (
            None
            if not recorded_outer_rounds
            else {
                "account_policy": "FORCE_FLAT_WITH_COST",
                "rounds": recorded_outer_rounds,
                "ordered_net_daily_returns": [
                    value
                    for item in recorded_outer_rounds
                    for value in item["net_daily_returns"]
                ],
            }
        )
        drift = next(
            (
                event["payload"]
                for event in [
                    {
                        "event_type": row["event_type"],
                        "payload": _strict_json_object(
                            row["payload_json"],
                            "Parameter Study event payload",
                        ),
                    }
                    for row in events
                ]
                if event["event_type"] == "EXECUTION_IDENTITY_DRIFT"
            ),
            None,
        )
        trial_views = []
        trial_configurations: dict[str, dict[str, Any]] = {}
        for trial in trials:
            configuration = _strict_json_object(
                trial["configuration_json"],
                "Study Trial configuration",
            )
            if _digest(configuration) != trial["candidate_digest"]:
                raise RuntimeError(
                    "Study Trial projection does not match its candidate digest"
                )
            trial_configurations[trial["candidate_digest"]] = configuration
            trial_views.append(
                {
                    "candidate_digest": trial["candidate_digest"],
                    "configuration": configuration,
                    "first_search_round": trial["first_search_round"],
                    "proposal_sequence": trial["proposal_sequence"],
                    "classification": trial["classification"],
                    "created_at": trial["created_at"],
                }
            )
        metric_evidence_by_binding = {
            item["payload"]["binding_id"]: item["payload"]
            for item in evidence_items
            if item["evidence_type"] == "METRIC_DOCUMENT_VERIFIED"
        }
        contested_experiments = {
            item["payload"]["experiment_id"]
            for item in evidence_items
            if item["evidence_type"] == "EVIDENCE_CONTESTED"
        }
        binding_views = []
        for binding in bindings:
            attempt = self.experiments.attempt_detail(binding["attempt_id"])
            fold_window = _strict_json_object(
                binding["fold_window_json"],
                "Study binding fold window",
            )
            task = _strict_json_object(
                binding["task_json"],
                "Study binding task",
            )
            if (
                binding["candidate_digest"] not in trial_configurations
                or _digest(task) != binding["task_digest"]
                or task.get("dataset", {}).get("snapshot_id")
                != binding["dataset_snapshot_id"]
                or self._binding_id(
                    study_id=study_id,
                    search_round=binding["search_round"],
                    candidate_digest=binding["candidate_digest"],
                    role=binding["role"],
                    fold_sequence=binding["fold_sequence"],
                    fold_window=fold_window,
                )
                != binding["binding_id"]
                or attempt["experiment_id"] != binding["experiment_id"]
            ):
                raise RuntimeError(
                    "Study binding projection does not match durable identity"
                )
            stored_metric = (
                None
                if binding["metric_document_json"] is None
                else _strict_json_object(
                    binding["metric_document_json"],
                    "Study binding Metric Document",
                )
            )
            metric_evidence = metric_evidence_by_binding.get(
                binding["binding_id"]
            )
            if (
                binding["state"] == "VERIFIED"
                and (
                    stored_metric is None
                    or metric_evidence is None
                    or metric_evidence.get("metric_document") != stored_metric
                    or metric_evidence.get("attempt_id") != binding["attempt_id"]
                )
            ) or (
                binding["state"] == "CONTESTED"
                and binding["experiment_id"] not in contested_experiments
            ) or (
                binding["state"] == "SUBMITTED"
                and stored_metric is not None
            ):
                raise RuntimeError(
                    "Study binding state disagrees with canonical evidence"
                )
            binding_views.append(
                {
                    "binding_id": binding["binding_id"],
                    "search_round": binding["search_round"],
                    "candidate_digest": binding["candidate_digest"],
                    "role": binding["role"],
                    "fold_sequence": binding["fold_sequence"],
                    "fold_window": fold_window,
                    "task": task,
                    "task_digest": binding["task_digest"],
                    "dataset_snapshot_id": binding["dataset_snapshot_id"],
                    "experiment_id": binding["experiment_id"],
                    "submitted_attempt_id": binding["submitted_attempt_id"],
                    "attempt_id": binding["attempt_id"],
                    "state": binding["state"],
                    "attempt": {
                        key: attempt.get(key)
                        for key in (
                            "status",
                            "sequence",
                            "comparison",
                            "result_digest",
                            "created_at",
                            "started_at",
                            "finished_at",
                        )
                    },
                    "metric_document": stored_metric,
                }
            )
        if (
            study["selection_outcome"] == "CHAMPION_SELECTED"
        ) != (champion_evidence is not None):
            raise RuntimeError(
                "Study selection projection disagrees with champion evidence"
            )
        if (
            study["holdout_outcome"] != "NOT_RUN"
            and not any(
                item["evidence_type"] == "HOLDOUT_OUTCOME_RECORDED"
                for item in evidence_items
            )
        ):
            raise RuntimeError(
                "Study holdout projection disagrees with canonical evidence"
            )
        result = {
            "study_id": study["study_id"],
            "preview_digest": study["preview_digest"],
            "created_at": study["created_at"],
            "updated_at": study["updated_at"],
            "phase": study["phase"],
            "control_status": study["control_status"],
            "selection_outcome": study["selection_outcome"],
            "holdout": {
                "access": holdout_access,
                "outcome": study["holdout_outcome"],
                "freshness": holdout_freshness,
            },
            "holdout_claim": (
                None if holdout_claim is None else dict(holdout_claim)
            ),
            "holdout_ledger": [
                {
                    "sequence": item["sequence"],
                    "holdout_identity_digest": item[
                        "holdout_identity_digest"
                    ],
                    "event_type": item["event_type"],
                    "occurred_at": item["occurred_at"],
                    "payload": _strict_json_object(
                        item["payload_json"],
                        "holdout ledger payload",
                    ),
                }
                for item in holdout_ledger
            ],
            "operational_metadata": _strict_json_object(
                study["operational_metadata_json"],
                "Parameter Study operational metadata",
            ),
            "coordination": {"lease": lease},
            "drift": drift,
            "frozen_plan": frozen_plan,
            "lineage": frozen_plan["lineage"],
            "identities": identities,
            "events": [
                {
                    "sequence": event["sequence"],
                    "event_type": event["event_type"],
                    "occurred_at": event["occurred_at"],
                    "payload": _strict_json_object(
                        event["payload_json"],
                        "Parameter Study event payload",
                    ),
                }
                for event in events
            ],
            "trials": trial_views,
            "bindings": binding_views,
            "rankings": rankings,
            "verified_metrics": [
                binding["metric_document"]
                for binding in binding_views
                if binding["metric_document"] is not None
            ],
            "evaluations": final_evaluations,
            "champion_evidence": champion_evidence,
            "holdout_evidence": holdout_evidence,
            "outer_evidence": (
                champion_evidence["outer_evidence"]
                if champion_evidence is not None
                else derived_outer_evidence
            ),
            "evidence": evidence_items,
        }
        if len(canonical_json_bytes(result)) > MAX_STUDY_DETAIL_BYTES:
            raise RuntimeError("Parameter Study detail exceeds bounded size")
        return result
