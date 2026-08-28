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
INFORMATION_INTERVAL = {
    "signal_time": "SESSION_CLOSE",
    "earliest_execution_time": "NEXT_SESSION_OPEN",
    "return_or_label_end_time": "EXECUTION_SESSION_CLOSE",
}
EVALUATION_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "stability_weight": {"type": "number", "minimum": 0},
        "turnover_weight": {"type": "number", "minimum": 0},
        "minimum_trades": {"type": "integer", "minimum": 0},
        "maximum_drawdown": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "nullable": True,
        },
        "maximum_annual_turnover": {
            "type": "number",
            "minimum": 0,
            "nullable": True,
        },
    },
    "required": [
        "maximum_annual_turnover",
        "maximum_drawdown",
        "minimum_trades",
        "stability_weight",
        "turnover_weight",
    ],
    "additionalProperties": False,
}
EVALUATION_DEFAULTS = {
    "stability_weight": 0.5,
    "turnover_weight": 0.05,
    "minimum_trades": 1,
    "maximum_drawdown": None,
    "maximum_annual_turnover": None,
}
METRIC_ENGINE_IDENTITY = {
    "name": "account_daily_equity",
    "version": "1.0.0",
    "semantics": "net-account-daily-equity-force-terminal-policy",
}
EVALUATION_POLICY_IDENTITY = {
    "policy_id": "robust_walk_forward",
    "version": "1.0.0",
    "direction": "MAXIMIZE",
    "validation_score": (
        "median(fold_net_sharpe)"
        "-stability_weight*MAD(fold_net_sharpe)"
        "-turnover_weight*annual_turnover"
    ),
    "tie_break": [
        "lower_maximum_drawdown",
        "lower_annual_turnover",
        "strategy_configuration_digest",
    ],
    "parameter_schema": EVALUATION_PARAMETER_SCHEMA,
    "defaults": EVALUATION_DEFAULTS,
    "metric_engine": METRIC_ENGINE_IDENTITY,
}
EVALUATION_POLICY_DIGEST = hashlib.sha256(
    canonical_json_bytes(EVALUATION_POLICY_IDENTITY)
).hexdigest()
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
    return {
        "allowed_start": allowed_start,
        "training_through": sessions[training_end],
        "available_through": sessions[scoring_end],
        "scoring_start": sessions[scoring_start],
        "scoring_end": sessions[scoring_end],
        "role": role,
        "information_interval": deepcopy(INFORMATION_INTERVAL),
        "account_policy": account_policy,
    }


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
        self._instance_nonce = uuid.uuid4().hex
        self._advance_lock = threading.Lock()
        self.catalog.apply_migrations([STUDY_MIGRATION])

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
            return self._freeze_resolved_plan(resolved, connection)

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

    def _classify_readiness(
        self,
        connection: sqlite3.Connection,
        study_id: str,
    ) -> _StudyReadiness:
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

        if phase == "VALIDATING_SELECTION_PROCESS":
            effect = self._trial_proposal_effect(study_id, frozen_plan)
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
        if phase != "VALIDATING_SELECTION_PROCESS":
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
        if type(result) is not dict:
            raise RuntimeError("Parameter Study effect executor must return an object")
        canonical_json_bytes(result)
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

    def detail(self, study_id: str) -> dict[str, Any]:
        if not isinstance(study_id, str) or STUDY_ID.fullmatch(study_id) is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        connection = self.catalog.connect()
        try:
            connection.execute("BEGIN")
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
                SELECT holdout_identity_digest, event_type
                FROM parameter_study_holdout_ledger
                WHERE study_id = ?
                ORDER BY sequence
                """,
                (study_id,),
            ).fetchall()
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
        return {
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
            "operational_metadata": _strict_json_object(
                study["operational_metadata_json"],
                "Parameter Study operational metadata",
            ),
            "coordination": {"lease": lease},
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
        }
