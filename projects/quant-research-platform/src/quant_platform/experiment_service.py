from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .catalog import Catalog
from .datasets import SAFE_INSTRUMENT, _verify_snapshot
from .schemas import (
    SchemaValidationError,
    canonical_json_bytes,
    validate_parameters,
)


class TaskValidationError(ValueError):
    """Raised when a task cannot resolve entirely to published catalog entries."""


class InvalidAttemptTransition(RuntimeError):
    """Raised when an attempt state transition violates the lifecycle."""


TASK_FIELDS = {"schema_version", "dataset", "template", "operators"}
DATASET_FIELDS = {"instrument", "snapshot_id"}
TEMPLATE_FIELDS = {"name", "version", "parameters"}
OPERATOR_REQUIRED_FIELDS = {"operator_id", "parameters"}
OPERATOR_OPTIONAL_FIELDS = {"version"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_LOG_BYTES = 16_384


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _exact_fields(
    value: Any,
    required: set[str],
    path: str,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskValidationError(f"{path} must be an object")
    optional = optional or set()
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        raise TaskValidationError(f"{path} has missing fields: {missing}")
    if unknown:
        raise TaskValidationError(f"{path} has unknown fields: {unknown}")
    return value


def _with_defaults(
    schema: dict[str, Any], defaults: dict[str, Any], supplied: Any, path: str
) -> dict[str, Any]:
    if not isinstance(supplied, dict):
        raise TaskValidationError(f"{path} must be an object")
    unknown = sorted(set(supplied) - set(schema["properties"]))
    if unknown:
        raise TaskValidationError(f"{path} has unknown parameters: {unknown}")
    try:
        return validate_parameters(schema, defaults | supplied)
    except SchemaValidationError as exc:
        raise TaskValidationError(f"{path} is invalid: {exc}") from exc


def _validate_action_id(action_id: str) -> str:
    if not isinstance(action_id, str) or ACTION_ID.fullmatch(action_id) is None:
        raise TaskValidationError("action_id has invalid syntax")
    return action_id


class ExperimentService:
    def __init__(self, catalog: Catalog, *, execution_identity: dict[str, Any]):
        self.catalog = catalog
        if not isinstance(execution_identity, dict) or not execution_identity:
            raise ValueError("execution_identity must be a non-empty object")
        canonical_json_bytes(execution_identity)
        self.execution_identity = execution_identity

    def resolve_task(self, value: Any) -> dict[str, Any]:
        task = _exact_fields(value, TASK_FIELDS, "task")
        if type(task["schema_version"]) is not int or task["schema_version"] != 1:
            raise TaskValidationError("task.schema_version must be integer 1")

        dataset = _exact_fields(task["dataset"], DATASET_FIELDS, "task.dataset")
        instrument = dataset["instrument"]
        snapshot_id = dataset["snapshot_id"]
        if not isinstance(instrument, str) or SAFE_INSTRUMENT.fullmatch(instrument) is None:
            raise TaskValidationError("task.dataset.instrument has invalid syntax")
        if not isinstance(snapshot_id, str) or SHA256.fullmatch(snapshot_id) is None:
            raise TaskValidationError("task.dataset.snapshot_id must be a SHA-256 value")
        snapshot_path = (
            self.catalog.state_root / "datasets" / instrument / snapshot_id
        )
        if snapshot_path.is_symlink() or not snapshot_path.is_dir():
            raise TaskValidationError(
                f"unknown immutable dataset snapshot: {instrument}@{snapshot_id}"
            )
        try:
            snapshot_manifest = _verify_snapshot(snapshot_path, snapshot_id)
        except RuntimeError as exc:
            raise TaskValidationError(f"dataset snapshot failed verification: {exc}") from exc
        if snapshot_manifest["metadata"]["instrument"] != instrument:
            raise TaskValidationError("dataset snapshot instrument does not match")

        template_selector = _exact_fields(
            task["template"], TEMPLATE_FIELDS, "task.template"
        )
        name = template_selector["name"]
        version = template_selector["version"]
        if not isinstance(name, str) or not isinstance(version, str):
            raise TaskValidationError("task.template name and version must be strings")
        try:
            template = self.catalog.template_detail(name, version)
        except ValueError as exc:
            raise TaskValidationError(str(exc)) from exc
        template_parameters = _with_defaults(
            template["parameter_schema"],
            template["defaults"],
            template_selector["parameters"],
            "task.template.parameters",
        )
        if template_parameters["initial_capital_cny"] <= 0:
            raise TaskValidationError(
                "task.template.parameters.initial_capital_cny must be positive"
            )
        if (
            template_parameters["evaluation_end"] is not None
            and template_parameters["evaluation_end"]
            < template_parameters["evaluation_start"]
        ):
            raise TaskValidationError("evaluation_end cannot precede evaluation_start")

        operators = _exact_fields(
            task["operators"], set(template["slots"]), "task.operators"
        )
        resolved_operators: dict[str, Any] = {}
        requested_operators: dict[str, Any] = {}
        for slot in template["slots"]:
            selector = _exact_fields(
                operators[slot],
                OPERATOR_REQUIRED_FIELDS,
                f"task.operators.{slot}",
                OPERATOR_OPTIONAL_FIELDS,
            )
            operator_id = selector["operator_id"]
            requested_version = selector.get("version", "latest")
            if not isinstance(operator_id, str) or not isinstance(requested_version, str):
                raise TaskValidationError(
                    f"task.operators.{slot} selector values must be strings"
                )
            try:
                latest = self.catalog.operator_detail(operator_id)
                selected = (
                    latest
                    if requested_version == "latest"
                    else self.catalog.operator_detail(operator_id, requested_version)
                )
            except ValueError as exc:
                raise TaskValidationError(str(exc)) from exc
            if selected["slot"] != slot:
                raise TaskValidationError(
                    f"operator {operator_id} belongs to slot {selected['slot']}, not {slot}"
                )
            parameters = _with_defaults(
                selected["parameter_schema"],
                selected["defaults"],
                selector["parameters"],
                f"task.operators.{slot}.parameters",
            )
            mode = "latest" if requested_version == "latest" else "explicit"
            resolved_operators[slot] = {
                "operator_id": operator_id,
                "selector_mode": mode,
                "requested_version": requested_version,
                "latest_version_at_submission": latest["version"],
                "resolved_version": selected["version"],
                "content_digest": selected["content_digest"],
                "parameters": parameters,
            }
            requested_operators[slot] = {
                "operator_id": operator_id,
                "version": requested_version,
                "parameters": selector["parameters"],
            }

        return {
            "schema_version": 1,
            "dataset": {
                "instrument": instrument,
                "snapshot_id": snapshot_id,
                "canonical_sha256": snapshot_manifest["canonical_sha256"],
            },
            "template": {
                "name": template["name"],
                "version": template["version"],
                "content_digest": template["content_digest"],
                "parameters": template_parameters,
            },
            "operators": resolved_operators,
            "execution_identity": self.execution_identity,
            "requested": {
                "schema_version": 1,
                "dataset": {"instrument": instrument, "snapshot_id": snapshot_id},
                "template": {
                    "name": name,
                    "version": version,
                    "parameters": template_selector["parameters"],
                },
                "operators": requested_operators,
            },
        }

    def _identity(self, resolved: dict[str, Any]) -> dict[str, Any]:
        return {
            key: resolved[key]
            for key in (
                "schema_version",
                "dataset",
                "template",
                "operators",
                "execution_identity",
            )
        }

    def submit(self, task: Any, *, action_id: str) -> dict[str, Any]:
        action_id = _validate_action_id(action_id)
        resolved = self.resolve_task(task)
        identity = self._identity(resolved)
        experiment_id = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
        now = _now()
        with self.catalog.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT experiment_id FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if existing is not None:
                first = connection.execute(
                    """
                    SELECT attempt_id FROM attempts
                    WHERE experiment_id = ? ORDER BY sequence LIMIT 1
                    """,
                    (experiment_id,),
                ).fetchone()
                return {
                    "status": "DUPLICATE",
                    "experiment_id": experiment_id,
                    "attempt_created": False,
                    "attempt_id": first["attempt_id"],
                }
            attempt_id = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "experiment_id": experiment_id,
                        "action_id": action_id,
                        "sequence": 1,
                    }
                )
            ).hexdigest()
            try:
                connection.execute(
                    """
                    INSERT INTO experiments(experiment_id, identity_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (experiment_id, canonical_json_bytes(identity).decode(), now),
                )
                connection.execute(
                    """
                    INSERT INTO attempts(
                        attempt_id, experiment_id, action_id, sequence, status,
                        requested_json, resolved_json, created_at
                    ) VALUES (?, ?, ?, 1, 'PENDING', ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        experiment_id,
                        action_id,
                        canonical_json_bytes(resolved["requested"]).decode(),
                        canonical_json_bytes(resolved).decode(),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TaskValidationError(f"action_id is already in use: {action_id}") from exc
        return {
            "status": "CREATED",
            "experiment_id": experiment_id,
            "attempt_created": True,
            "attempt_id": attempt_id,
        }

    def rerun(self, experiment_id: str, *, action_id: str) -> dict[str, Any]:
        action_id = _validate_action_id(action_id)
        with self.catalog.transaction(immediate=True) as connection:
            experiment = connection.execute(
                "SELECT identity_json FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if experiment is None:
                raise TaskValidationError(f"unknown experiment: {experiment_id}")
            repeated = connection.execute(
                "SELECT attempt_id FROM attempts WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if repeated is not None:
                return {
                    "status": "NO_CHANGE",
                    "experiment_id": experiment_id,
                    "attempt_id": repeated["attempt_id"],
                }
            previous = connection.execute(
                """
                SELECT sequence, requested_json, resolved_json FROM attempts
                WHERE experiment_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (experiment_id,),
            ).fetchone()
            resolved = json.loads(previous["resolved_json"])
            for operator in resolved["operators"].values():
                latest = self.catalog.operator_detail(operator["operator_id"])
                operator["latest_version_at_submission"] = latest["version"]
            sequence = previous["sequence"] + 1
            attempt_id = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "experiment_id": experiment_id,
                        "action_id": action_id,
                        "sequence": sequence,
                    }
                )
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, experiment_id, action_id, sequence, status,
                    requested_json, resolved_json, created_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    attempt_id,
                    experiment_id,
                    action_id,
                    sequence,
                    previous["requested_json"],
                    canonical_json_bytes(resolved).decode(),
                    _now(),
                ),
            )
        return {
            "status": "CREATED",
            "experiment_id": experiment_id,
            "attempt_id": attempt_id,
        }

    def list_attempts(self, experiment_id: str) -> list[dict[str, Any]]:
        connection = self.catalog.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM attempts WHERE experiment_id = ?
                ORDER BY sequence
                """,
                (experiment_id,),
            ).fetchall()
        finally:
            connection.close()
        return [self._attempt_row(row) for row in rows]

    def _attempt_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["requested"] = json.loads(result.pop("requested_json"))
        result["resolved"] = json.loads(result.pop("resolved_json"))
        return result

    def attempt_detail(self, attempt_id: str) -> dict[str, Any]:
        connection = self.catalog.connect()
        try:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise TaskValidationError(f"unknown attempt: {attempt_id}")
        return self._attempt_row(row)

    def _operator_drift(self, operator: dict[str, Any]) -> dict[str, Any]:
        try:
            latest = self.catalog.operator_detail(operator["operator_id"])
            drifted = (
                latest["version"] != operator["resolved_version"]
                or latest["content_digest"] != operator["content_digest"]
            )
            latest_version = latest["version"]
        except ValueError:
            drifted = True
            latest_version = None
        return operator | {
            "current_latest_version": latest_version,
            "drifted": drifted,
        }

    def experiment_detail(self, experiment_id: str) -> dict[str, Any]:
        connection = self.catalog.connect()
        try:
            row = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            divergent = connection.execute(
                """
                SELECT COUNT(*) FROM attempts
                WHERE experiment_id = ? AND comparison = 'DIVERGENT'
                """,
                (experiment_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        if row is None:
            raise TaskValidationError(f"unknown experiment: {experiment_id}")
        identity = json.loads(row["identity_json"])
        operators = {
            slot: self._operator_drift(operator)
            for slot, operator in identity["operators"].items()
        }
        attempts = self.list_attempts(experiment_id)
        return dict(row) | identity | {
            "operators": operators,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "has_drift": any(item["drifted"] for item in operators.values()),
            "has_divergent_attempt": bool(divergent),
        }

    def list_experiments(self) -> list[dict[str, Any]]:
        connection = self.catalog.connect()
        try:
            rows = connection.execute(
                "SELECT experiment_id FROM experiments ORDER BY created_at DESC"
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                key: detail[key]
                for key in (
                    "experiment_id",
                    "created_at",
                    "attempt_count",
                    "has_drift",
                    "canonical_attempt_id",
                    "has_divergent_attempt",
                )
            }
            for detail in (
                self.experiment_detail(row["experiment_id"]) for row in rows
            )
        ]

    def claim_next_attempt(self) -> dict[str, Any] | None:
        with self.catalog.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT attempt_id FROM attempts
                WHERE status = 'PENDING' ORDER BY created_at, sequence LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE attempts SET status = 'RUNNING', started_at = ?
                WHERE attempt_id = ? AND status = 'PENDING'
                """,
                (_now(), row["attempt_id"]),
            )
        return self.attempt_detail(row["attempt_id"])

    def recover_abandoned_attempts(self) -> int:
        with self.catalog.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE attempts
                SET status = 'PENDING', started_at = NULL,
                    logs = CASE
                        WHEN logs IS NULL THEN 'Recovered abandoned RUNNING attempt.'
                        ELSE substr(logs || '\nRecovered abandoned RUNNING attempt.', 1, ?)
                    END
                WHERE status = 'RUNNING'
                """,
                (MAX_LOG_BYTES,),
            )
            return cursor.rowcount

    def _require_running(
        self, connection: sqlite3.Connection, attempt_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise TaskValidationError(f"unknown attempt: {attempt_id}")
        if row["status"] != "RUNNING":
            raise InvalidAttemptTransition(
                f"attempt {attempt_id} must be RUNNING, not {row['status']}"
            )
        return row

    def finish_success(
        self,
        attempt_id: str,
        *,
        result_path: str,
        result_digest: str,
        logs: str = "",
    ) -> dict[str, Any]:
        if not isinstance(result_path, str) or not result_path:
            raise ValueError("result_path must be non-empty")
        if not isinstance(result_digest, str) or SHA256.fullmatch(result_digest) is None:
            raise ValueError("result_digest must be a SHA-256 value")
        logs = str(logs)[-MAX_LOG_BYTES:]
        with self.catalog.transaction(immediate=True) as connection:
            attempt = self._require_running(connection, attempt_id)
            experiment = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?",
                (attempt["experiment_id"],),
            ).fetchone()
            if experiment["canonical_attempt_id"] is None:
                comparison = "CANONICAL"
                canonical_path = result_path
                connection.execute(
                    """
                    UPDATE experiments
                    SET canonical_attempt_id = ?, canonical_result_digest = ?
                    WHERE experiment_id = ?
                    """,
                    (attempt_id, result_digest, attempt["experiment_id"]),
                )
            elif experiment["canonical_result_digest"] == result_digest:
                comparison = "EQUAL"
                canonical = connection.execute(
                    "SELECT result_path FROM attempts WHERE attempt_id = ?",
                    (experiment["canonical_attempt_id"],),
                ).fetchone()
                canonical_path = canonical["result_path"]
            else:
                comparison = "DIVERGENT"
                canonical_path = result_path
            connection.execute(
                """
                UPDATE attempts
                SET status = 'SUCCEEDED', finished_at = ?, logs = ?,
                    result_path = ?, result_digest = ?, comparison = ?
                WHERE attempt_id = ?
                """,
                (
                    _now(),
                    logs,
                    canonical_path,
                    result_digest,
                    comparison,
                    attempt_id,
                ),
            )
        return self.attempt_detail(attempt_id)

    def finish_failure(self, attempt_id: str, logs: str) -> dict[str, Any]:
        logs = str(logs)[-MAX_LOG_BYTES:]
        with self.catalog.transaction(immediate=True) as connection:
            self._require_running(connection, attempt_id)
            connection.execute(
                """
                UPDATE attempts
                SET status = 'FAILED', finished_at = ?, logs = ?
                WHERE attempt_id = ?
                """,
                (_now(), logs, attempt_id),
            )
        return self.attempt_detail(attempt_id)
