from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
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
                "latest_content_digest_at_submission": latest["content_digest"],
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
        identity = {
            key: resolved[key]
            for key in (
                "schema_version",
                "dataset",
                "template",
                "execution_identity",
            )
        }
        identity["operators"] = {
            slot: {
                key: operator[key]
                for key in (
                    "operator_id",
                    "resolved_version",
                    "content_digest",
                    "parameters",
                )
            }
            for slot, operator in resolved["operators"].items()
        }
        return identity

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
                "SELECT attempt_id, experiment_id FROM attempts WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if repeated is not None:
                if repeated["experiment_id"] != experiment_id:
                    raise TaskValidationError(
                        "action_id is already used by another experiment"
                    )
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
            resolved = self._refresh_action_audit(
                json.loads(previous["resolved_json"])
            )
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

    def _refresh_action_audit(self, resolved: dict[str, Any]) -> dict[str, Any]:
        for operator in resolved["operators"].values():
            latest = self.catalog.operator_detail(operator["operator_id"])
            operator["latest_version_at_submission"] = latest["version"]
            operator["latest_content_digest_at_submission"] = latest[
                "content_digest"
            ]
        return resolved

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
                WHERE status = 'PENDING' AND launch_count = 0
                ORDER BY created_at, sequence LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            attempt_id = row["attempt_id"]
            control_relative = f"attempt-control/{attempt_id}"
            control_dir = self.catalog.state_root / control_relative
            control_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            control_dir.mkdir(mode=0o700)
            claimed_at = _now()
            control = {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "launch_count": 1,
                "state": "CLAIMED",
                "claimed_at": claimed_at,
                "container_name": None,
            }
            try:
                self._write_control_json(control_dir / "control.json", control)
                connection.execute(
                    """
                    UPDATE attempts
                    SET status = 'RUNNING', started_at = ?, launch_count = 1,
                        control_path = ?, control_json = ?
                    WHERE attempt_id = ? AND status = 'PENDING' AND launch_count = 0
                    """,
                    (
                        claimed_at,
                        control_relative,
                        canonical_json_bytes(control).decode(),
                        attempt_id,
                    ),
                )
            except BaseException:
                shutil.rmtree(control_dir, ignore_errors=True)
                raise
        return self.attempt_detail(row["attempt_id"])

    def _write_control_json(self, path: os.PathLike[str], value: dict[str, Any]) -> None:
        payload = canonical_json_bytes(value) + b"\n"
        with Path(path).open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        Path(path).chmod(0o600)

    def _control_directory(self, row: sqlite3.Row | dict[str, Any]) -> Path:
        expected = f"attempt-control/{row['attempt_id']}"
        if row["control_path"] != expected:
            raise InvalidAttemptTransition("attempt control path is missing or invalid")
        target = self.catalog.state_root / expected
        if target.is_symlink() or not target.is_dir():
            raise InvalidAttemptTransition("attempt control directory is unsafe")
        return target

    def record_physical_launch(
        self, attempt_id: str, *, container_name: str
    ) -> dict[str, Any]:
        if not isinstance(container_name, str) or not container_name:
            raise ValueError("container_name must be non-empty")
        with self.catalog.transaction(immediate=True) as connection:
            row = self._require_running(connection, attempt_id)
            control_dir = self._control_directory(row)
            control = json.loads(row["control_json"])
            if control["state"] != "CLAIMED":
                raise InvalidAttemptTransition("physical launch is already recorded")
            control["state"] = "LAUNCHING"
            control["container_name"] = container_name
            replacement = control_dir / "control.next.json"
            self._write_control_json(replacement, control)
            os.replace(replacement, control_dir / "control.json")
            connection.execute(
                "UPDATE attempts SET control_json = ? WHERE attempt_id = ?",
                (canonical_json_bytes(control).decode(), attempt_id),
            )
        return self.attempt_detail(attempt_id)

    def _seal_control(
        self, control_dir: Path, evidence: dict[str, Any], *, filename: str
    ) -> None:
        evidence_path = control_dir / filename
        if not evidence_path.exists():
            self._write_control_json(evidence_path, evidence)
        allowed = {
            "control.json",
            "container.cid",
            "stdout.log",
            "stderr.log",
            "recovery.json",
            "terminal.json",
        }
        for path in control_dir.iterdir():
            metadata = os.stat(path, follow_symlinks=False)
            if (
                path.name not in allowed
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o7000
            ):
                raise InvalidAttemptTransition(
                    f"attempt control evidence is unsafe: {path.name}"
                )
            path.chmod(0o444)
        control_dir.chmod(0o555)

    def recover_abandoned_attempts(
        self,
        *,
        container_reconciler: Any | None = None,
    ) -> int:
        if container_reconciler is None:
            from .runner import reconcile_container

            container_reconciler = reconcile_container
        connection = self.catalog.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE status = 'RUNNING'"
            ).fetchall()
        finally:
            connection.close()
        recovered = 0
        for row in rows:
            control_dir = self._control_directory(row)
            control = json.loads(row["control_json"])
            cidfile = control_dir / "container.cid"
            confirmed = control.get("state") == "CLAIMED" and not cidfile.exists()
            if not confirmed:
                try:
                    confirmed = bool(container_reconciler(cidfile))
                except (OSError, RuntimeError):
                    confirmed = False
            status = "INTERRUPTED" if confirmed else "TERMINATION_UNCONFIRMED"
            quarantine_relative = None
            evidence = {
                "schema_version": 1,
                "attempt_id": row["attempt_id"],
                "status": status,
                "reconciled_at": _now(),
                "termination_confirmed": confirmed,
            }
            if confirmed:
                self._seal_control(control_dir, evidence, filename="recovery.json")
            else:
                quarantine_relative = f"quarantine/attempts/{row['attempt_id']}"
                quarantine = self.catalog.state_root / quarantine_relative
                quarantine.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if quarantine.exists():
                    raise InvalidAttemptTransition(
                        "attempt termination quarantine already exists"
                    )
                os.rename(control_dir, quarantine)
                control_dir.mkdir(mode=0o700)
                evidence["quarantine_path"] = quarantine_relative
                self._seal_control(control_dir, evidence, filename="recovery.json")
            message = (
                f"Runner reconciled abandoned attempt as {status}; "
                f"termination_confirmed={str(confirmed).lower()}."
            )
            with self.catalog.transaction(immediate=True) as update:
                current = self._require_running(update, row["attempt_id"])
                if current["launch_count"] != 1:
                    raise InvalidAttemptTransition(
                        "abandoned attempt launch count is invalid"
                    )
                update.execute(
                    """
                    UPDATE attempts
                    SET status = ?, finished_at = ?, logs = ?, quarantine_path = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        status,
                        evidence["reconciled_at"],
                        message[-MAX_LOG_BYTES:],
                        quarantine_relative,
                        row["attempt_id"],
                    ),
                )
            recovered += 1
        return recovered

    def create_replacement_attempt(
        self, attempt_id: str, *, action_id: str
    ) -> dict[str, Any]:
        action_id = _validate_action_id(action_id)
        with self.catalog.transaction(immediate=True) as connection:
            prior = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if prior is None:
                raise TaskValidationError(f"unknown attempt: {attempt_id}")
            if prior["status"] == "TERMINATION_UNCONFIRMED":
                raise InvalidAttemptTransition(
                    "replacement requires confirmed prior termination"
                )
            if prior["status"] != "INTERRUPTED":
                raise InvalidAttemptTransition(
                    "replacement requires an INTERRUPTED attempt"
                )
            repeated = connection.execute(
                "SELECT attempt_id FROM attempts WHERE action_id = ?", (action_id,)
            ).fetchone()
            if repeated is not None:
                return {
                    "status": "NO_CHANGE",
                    "experiment_id": prior["experiment_id"],
                    "attempt_id": repeated["attempt_id"],
                }
            sequence = connection.execute(
                "SELECT MAX(sequence) FROM attempts WHERE experiment_id = ?",
                (prior["experiment_id"],),
            ).fetchone()[0] + 1
            replacement_id = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "experiment_id": prior["experiment_id"],
                        "action_id": action_id,
                        "sequence": sequence,
                    }
                )
            ).hexdigest()
            resolved = self._refresh_action_audit(
                json.loads(prior["resolved_json"])
            )
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, experiment_id, action_id, sequence, status,
                    requested_json, resolved_json, created_at, recovery_of_attempt_id
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    replacement_id,
                    prior["experiment_id"],
                    action_id,
                    sequence,
                    prior["requested_json"],
                    canonical_json_bytes(resolved).decode(),
                    _now(),
                    attempt_id,
                ),
            )
        return {
            "status": "CREATED",
            "experiment_id": prior["experiment_id"],
            "attempt_id": replacement_id,
        }

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

    def _finalize_terminal_control(
        self, attempt: sqlite3.Row, *, status: str, finished_at: str
    ) -> None:
        control_dir = self._control_directory(attempt)
        evidence = {
            "schema_version": 1,
            "attempt_id": attempt["attempt_id"],
            "status": status,
            "finished_at": finished_at,
            "launch_count": attempt["launch_count"],
        }
        self._seal_control(control_dir, evidence, filename="terminal.json")

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
            finished_at = _now()
            self._finalize_terminal_control(
                attempt, status="SUCCEEDED", finished_at=finished_at
            )
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
                    finished_at,
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
            attempt = self._require_running(connection, attempt_id)
            finished_at = _now()
            self._finalize_terminal_control(
                attempt, status="FAILED", finished_at=finished_at
            )
            connection.execute(
                """
                UPDATE attempts
                SET status = 'FAILED', finished_at = ?, logs = ?
                WHERE attempt_id = ?
                """,
                (finished_at, logs, attempt_id),
            )
        return self.attempt_detail(attempt_id)
