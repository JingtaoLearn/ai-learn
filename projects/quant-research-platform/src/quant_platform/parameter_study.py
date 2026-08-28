from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
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
    action_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    study_id TEXT NOT NULL REFERENCES parameter_studies(study_id),
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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
        self.catalog.apply_migrations([STUDY_MIGRATION])

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("ParameterStudy clock must return an aware datetime")
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

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
        if action["operation"] != "SUBMIT":
            return conflict
        frozen_plan = json.loads(action["frozen_plan_json"])
        try:
            normalized_request = _normalize_request_for_plan(
                spec,
                frozen_plan,
            )
        except StudyValidationError:
            return conflict
        request_digest = _action_request_digest(
            normalized_request,
            expected_preview_digest,
        )
        if request_digest != action["request_digest"]:
            return conflict
        return json.loads(action["response_json"])

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
        if not isinstance(action_id, str) or ACTION_ID.fullmatch(action_id) is None:
            raise StudyValidationError("action_id has invalid syntax")

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

    def detail(self, study_id: str) -> dict[str, Any]:
        if not isinstance(study_id, str) or STUDY_ID.fullmatch(study_id) is None:
            raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
        connection = self.catalog.connect()
        try:
            study = connection.execute(
                "SELECT * FROM parameter_studies WHERE study_id = ?",
                (study_id,),
            ).fetchone()
            if study is None:
                raise StudyNotFoundError(f"unknown Parameter Study: {study_id}")
            frozen_plan = json.loads(study["frozen_plan_json"])
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
        finally:
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
            "operational_metadata": json.loads(
                study["operational_metadata_json"]
            ),
            "frozen_plan": frozen_plan,
            "lineage": frozen_plan["lineage"],
            "identities": identities,
            "events": [
                {
                    "sequence": event["sequence"],
                    "event_type": event["event_type"],
                    "occurred_at": event["occurred_at"],
                    "payload": json.loads(event["payload_json"]),
                }
                for event in events
            ],
        }
