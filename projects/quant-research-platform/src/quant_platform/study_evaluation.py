from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import weakref
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
import pandas as pd

from .datasets import _verify_snapshot
from .schemas import canonical_json_bytes
from .strategy_replay import COST_FIELDS, EVENT_COLUMNS, TRADE_COLUMNS
from .strategy_runner import RECONCILIATION_FIELDS
from .study_contracts import normalize_fold_window


SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESULT_ARTIFACTS = (
    "daily_replay.csv",
    "events.csv",
    "trades.csv",
    "metrics.json",
    "cost_breakdown.json",
)
MAX_ARTIFACT_BYTES = {
    "config.json": 1_048_576,
    "run_manifest.json": 1_048_576,
    "daily_replay.csv": 64 * 1_048_576,
    "events.csv": 64 * 1_048_576,
    "trades.csv": 64 * 1_048_576,
    "metrics.json": 1_048_576,
    "cost_breakdown.json": 1_048_576,
    "report.html": 16 * 1_048_576,
}
MAX_ATTEMPT_AUDIT_BYTES = 4 * 1_048_576
MAX_TOTAL_RESULT_BYTES = 128 * 1_048_576
MAX_SCORED_SESSIONS = 100_000
MAX_LEDGER_ROWS = 200_000
MAX_METRIC_DOCUMENTS_PER_EVALUATION = 256
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
EVALUATION_POLICY_SOURCE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
EVALUATION_POLICY_IDENTITY = {
    "policy_id": "robust_walk_forward",
    "version": "1.0.0",
    "source_digest": EVALUATION_POLICY_SOURCE_DIGEST,
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


class MetricDocumentValidationError(RuntimeError):
    """Raised when immutable strategy evidence cannot be trusted."""


class EvaluationPolicyError(ValueError):
    """Raised when evaluation evidence or policy parameters are invalid."""


def _metric_document_capability():
    issued: dict[int, tuple[weakref.ReferenceType, bytes]] = {}

    class VerifiedMetricDocument(dict[str, Any]):
        """Opaque evidence capability issued only by MetricDocumentFactory."""

        __slots__ = ("__weakref__",)

    def issue(value: Mapping[str, Any]) -> VerifiedMetricDocument:
        document = VerifiedMetricDocument(deepcopy(dict(value)))
        identifier = id(document)

        def discard(reference: weakref.ReferenceType) -> None:
            current = issued.get(identifier)
            if current is not None and current[0] is reference:
                issued.pop(identifier, None)

        reference = weakref.ref(document, discard)
        issued[identifier] = (reference, canonical_json_bytes(document))
        return document

    def is_pristine(value: Any) -> bool:
        record = issued.get(id(value))
        if record is None or record[0]() is not value:
            return False
        try:
            return canonical_json_bytes(value) == record[1]
        except (TypeError, ValueError):
            return False

    return VerifiedMetricDocument, issue, is_pristine


(
    VerifiedMetricDocument,
    _issue_verified_metric_document,
    _is_pristine_verified_metric_document,
) = _metric_document_capability()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _strict_json(payload: bytes, label: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MetricDocumentValidationError(
                    f"{label} contains duplicate object key: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise MetricDocumentValidationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise MetricDocumentValidationError(f"{label} must be an object")
    return value


@contextmanager
def _root_relative_directory(
    state_root: Path,
    target: Path,
    label: str,
) -> Any:
    state_root = state_root.absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(state_root)
    except ValueError as exc:
        raise MetricDocumentValidationError(
            f"{label} is outside the state root"
        ) from exc
    if not relative.parts:
        raise MetricDocumentValidationError(f"{label} cannot be the state root")
    descriptors: list[int] = []
    try:
        root_before = os.stat(state_root, follow_symlinks=False)
        if not stat.S_ISDIR(root_before.st_mode) or stat.S_ISLNK(
            root_before.st_mode
        ):
            raise MetricDocumentValidationError("state root is not an immutable locator")
        root_descriptor = os.open(
            state_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        descriptors.append(root_descriptor)
        opened_root = os.fstat(root_descriptor)
        if (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != (
            root_before.st_dev,
            root_before.st_ino,
        ):
            raise MetricDocumentValidationError("state root changed while opening")
        parent_descriptor = root_descriptor
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise MetricDocumentValidationError(
                    f"{label} contains an unsafe path component"
                )
            descriptor = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            descriptors.append(descriptor)
            parent_descriptor = descriptor
        metadata = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise MetricDocumentValidationError(f"{label} is not a directory")
        yield parent_descriptor
        root_after = os.stat(state_root, follow_symlinks=False)
        if (
            root_after.st_dev,
            root_after.st_ino,
        ) != (
            opened_root.st_dev,
            opened_root.st_ino,
        ):
            raise MetricDocumentValidationError("state root changed while reading")
    except MetricDocumentValidationError:
        raise
    except OSError as exc:
        raise MetricDocumentValidationError(
            f"{label} cannot be opened relative to the state root"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _immutable_file_at(
    directory_descriptor: int,
    name: str,
    label: str,
    *,
    maximum_bytes: int,
) -> bytes:
    if "/" in name or name in {"", ".", ".."}:
        raise MetricDocumentValidationError(f"{label} has an unsafe name")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o222
            or before.st_nlink != 1
            or before.st_size > maximum_bytes
        ):
            raise MetricDocumentValidationError(
                f"{label} is not a bounded immutable regular file"
            )
        payload = bytearray()
        while chunk := os.read(descriptor, min(1024 * 1024, maximum_bytes + 1)):
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise MetricDocumentValidationError(
                    f"{label} exceeds its byte bound"
                )
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise MetricDocumentValidationError(f"{label} changed while reading")
        return bytes(payload)
    except MetricDocumentValidationError:
        raise
    except OSError as exc:
        raise MetricDocumentValidationError(f"{label} cannot be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _finite(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise MetricDocumentValidationError(f"{path} must be finite")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _finite(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _finite(item, f"{path}.{key}")
        return
    raise MetricDocumentValidationError(f"{path} contains an unsupported value")


def _close(left: float, right: float, *, scale: float = 1.0) -> bool:
    return bool(np.isclose(left, right, rtol=1e-12, atol=1e-8 * max(1.0, scale)))


def _date_series(frame: pd.DataFrame, column: str, label: str) -> pd.Series:
    if column not in frame:
        raise MetricDocumentValidationError(f"{label} is missing {column}")
    try:
        dates = pd.to_datetime(frame[column], format="%Y-%m-%d", errors="raise")
    except (TypeError, ValueError) as exc:
        raise MetricDocumentValidationError(f"{label}.{column} contains invalid dates") from exc
    if dates.isna().any() or dates.dt.strftime("%Y-%m-%d").tolist() != frame[column].tolist():
        raise MetricDocumentValidationError(
            f"{label}.{column} must use canonical YYYY-MM-DD dates"
        )
    return dates


def _numeric(frame: pd.DataFrame, columns: Sequence[str], label: str) -> None:
    for column in columns:
        if column not in frame:
            raise MetricDocumentValidationError(f"{label} is missing {column}")
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
            raise MetricDocumentValidationError(f"{label}.{column} must be finite numeric data")


def _result_digest(payloads: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in RESULT_ARTIFACTS:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payloads[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _candidate_attempt_binding(
    candidate: Mapping[str, Any],
    resolved: Mapping[str, Any],
    *,
    candidate_digest: str,
    experiment_id: str,
    attempt_id: str,
    result_digest: str,
    attempt_audit_digest: str,
) -> dict[str, Any]:
    if not isinstance(candidate, Mapping) or set(candidate) != {
        "schema_version",
        "template",
        "operators",
    }:
        raise MetricDocumentValidationError("candidate configuration shape is invalid")
    if candidate.get("schema_version") != 1:
        raise MetricDocumentValidationError("candidate configuration schema is invalid")
    canonical_candidate = deepcopy(dict(candidate))
    if _sha256(canonical_json_bytes(canonical_candidate)) != candidate_digest:
        raise MetricDocumentValidationError(
            "candidate_digest does not match the canonical candidate configuration"
        )
    resolved_template = resolved.get("template")
    candidate_template = candidate.get("template")
    resolved_operators = resolved.get("operators")
    candidate_operators = candidate.get("operators")
    if (
        not isinstance(resolved_template, Mapping)
        or not isinstance(candidate_template, Mapping)
        or set(candidate_template)
        != {"name", "version", "content_digest", "parameters"}
        or not isinstance(resolved_operators, Mapping)
        or not isinstance(candidate_operators, Mapping)
        or set(resolved_operators) != set(candidate_operators)
    ):
        raise MetricDocumentValidationError(
            "candidate configuration does not match Attempt configuration"
        )
    if any(
        candidate_template.get(field) != resolved_template.get(field)
        for field in ("name", "version", "content_digest")
    ):
        raise MetricDocumentValidationError(
            "candidate template identity does not match the Attempt"
        )
    candidate_parameters = candidate_template.get("parameters")
    resolved_parameters = resolved_template.get("parameters")
    if not isinstance(candidate_parameters, Mapping) or not isinstance(
        resolved_parameters, Mapping
    ):
        raise MetricDocumentValidationError("candidate template parameters are invalid")
    protocol_parameters = {
        key: value
        for key, value in resolved_parameters.items()
        if key not in {"evaluation_start", "evaluation_end", "terminal_handling"}
    }
    candidate_protocol_parameters = {
        key: value
        for key, value in candidate_parameters.items()
        if key != "terminal_handling"
    }
    fold_window = resolved.get("dataset", {}).get("lineage", {}).get("view_spec", {})
    if (
        protocol_parameters != candidate_protocol_parameters
        or resolved_parameters.get("evaluation_start") != fold_window.get("scoring_start")
        or resolved_parameters.get("evaluation_end") != fold_window.get("scoring_end")
        or (
            fold_window.get("account_policy") == "FORCE_FLAT_WITH_COST"
            and resolved_parameters.get("terminal_handling") != "force_liquidate"
        )
    ):
        raise MetricDocumentValidationError(
            "candidate template parameters do not match the Attempt protocol"
        )
    for slot, candidate_operator_value in candidate_operators.items():
        resolved_operator = resolved_operators[slot]
        if (
            not isinstance(candidate_operator_value, Mapping)
            or not isinstance(resolved_operator, Mapping)
            or set(candidate_operator_value)
            != {"operator_id", "version", "content_digest", "parameters"}
            or candidate_operator_value.get("operator_id")
            != resolved_operator.get("operator_id")
            or candidate_operator_value.get("version")
            != resolved_operator.get("resolved_version")
            or candidate_operator_value.get("content_digest")
            != resolved_operator.get("content_digest")
            or candidate_operator_value.get("parameters")
            != resolved_operator.get("parameters")
        ):
            raise MetricDocumentValidationError(
                f"candidate operator {slot} does not match the Attempt"
            )
    experiment_configuration = {
        key: deepcopy(resolved[key])
        for key in ("schema_version", "dataset", "template", "operators", "execution_identity")
    }
    return {
        "strategy_configuration": canonical_candidate,
        "strategy_configuration_digest": candidate_digest,
        "experiment_configuration_digest": _sha256(
            canonical_json_bytes(experiment_configuration)
        ),
        "attempt_audit_digest": attempt_audit_digest,
    }


class MetricDocumentFactory:
    """Verify immutable run artifacts and derive policy-safe account metrics."""

    def __init__(self, state_root: Path | str):
        self.state_root = Path(state_root).absolute()

    def from_attempt(
        self,
        attempt: Mapping[str, Any],
        *,
        candidate_digest: str,
        candidate_configuration: Mapping[str, Any],
        fold_window: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(attempt, Mapping):
            raise MetricDocumentValidationError("attempt must be an object")
        if attempt.get("status") != "SUCCEEDED":
            raise MetricDocumentValidationError("attempt is not successful")
        if attempt.get("comparison") not in {"CANONICAL", "EQUAL"}:
            raise MetricDocumentValidationError("attempt is not canonical Experiment evidence")
        resolved = attempt.get("resolved")
        dataset = resolved.get("dataset") if isinstance(resolved, Mapping) else None
        if not isinstance(dataset, Mapping):
            raise MetricDocumentValidationError("attempt dataset identity is missing")
        return self.create(
            result_path=attempt.get("result_path"),
            result_digest=attempt.get("result_digest"),
            experiment_id=attempt.get("experiment_id"),
            attempt_id=attempt.get("attempt_id"),
            candidate_digest=candidate_digest,
            candidate_configuration=candidate_configuration,
            dataset=dataset,
            resolved=resolved,
            requested=attempt.get("requested"),
            fold_window=fold_window,
        )

    def create(
        self,
        *,
        result_path: Path | str,
        result_digest: str,
        experiment_id: str,
        attempt_id: str,
        candidate_digest: str,
        candidate_configuration: Mapping[str, Any],
        dataset: Mapping[str, Any],
        resolved: Mapping[str, Any],
        requested: Mapping[str, Any],
        fold_window: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        identities = {
            "result_digest": result_digest,
            "experiment_id": experiment_id,
            "attempt_id": attempt_id,
            "candidate_digest": candidate_digest,
        }
        for label, value in identities.items():
            if not isinstance(value, str) or SHA256.fullmatch(value) is None:
                raise MetricDocumentValidationError(f"{label} must be a lowercase SHA-256 digest")
        if not isinstance(dataset, Mapping):
            raise MetricDocumentValidationError("dataset must be an object")
        if not isinstance(resolved, Mapping):
            raise MetricDocumentValidationError("resolved Attempt must be an object")
        if not isinstance(requested, Mapping):
            raise MetricDocumentValidationError("requested Attempt must be an object")
        instrument = dataset.get("instrument")
        snapshot_id = dataset.get("snapshot_id")
        lineage = dataset.get("lineage")
        if (
            not isinstance(instrument, str)
            or not isinstance(snapshot_id, str)
            or SHA256.fullmatch(snapshot_id) is None
            or not isinstance(lineage, Mapping)
            or lineage.get("kind") != "derived_view"
        ):
            raise MetricDocumentValidationError(
                "Metric Documents require an access-bounded derived dataset"
            )
        dataset_path = self.state_root / "datasets" / instrument / snapshot_id
        try:
            verified = _verify_snapshot(
                dataset_path,
                snapshot_id,
                include_frame=True,
                verify_parent=True,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise MetricDocumentValidationError(
                f"execution dataset failed verification: {exc}"
            ) from exc
        if not isinstance(verified, tuple):
            raise MetricDocumentValidationError("execution dataset did not return verified rows")
        manifest, dataset_frame = verified
        if (
            manifest.get("canonical_sha256") != dataset.get("canonical_sha256")
            or manifest.get("lineage") != lineage
        ):
            raise MetricDocumentValidationError(
                "execution dataset does not match the Attempt identity"
            )
        expected_window = normalize_fold_window(
            lineage["view_spec"],
            dataset_frame["Date"].dt.strftime("%Y-%m-%d").tolist(),
        )
        if fold_window is not None and dict(fold_window) != expected_window:
            raise MetricDocumentValidationError("fold window does not match dataset scoring identity")

        if not isinstance(result_path, (str, Path)):
            raise MetricDocumentValidationError("result directory is unavailable")
        root = Path(result_path).absolute()
        required = {*RESULT_ARTIFACTS, "run_manifest.json", "config.json", "report.html"}
        with _root_relative_directory(
            self.state_root,
            root,
            "result directory",
        ) as result_descriptor:
            root_metadata = os.fstat(result_descriptor)
            if stat.S_IMODE(root_metadata.st_mode) & 0o222:
                raise MetricDocumentValidationError(
                    "result directory is not immutable"
                )
            names = set(os.listdir(result_descriptor))
            if names != required:
                raise MetricDocumentValidationError(
                    "result artifact set is incomplete or unexpected"
                )
            payloads = {
                name: _immutable_file_at(
                    result_descriptor,
                    name,
                    f"result artifact {name}",
                    maximum_bytes=MAX_ARTIFACT_BYTES[name],
                )
                for name in sorted(required)
            }
        if sum(len(payload) for payload in payloads.values()) > MAX_TOTAL_RESULT_BYTES:
            raise MetricDocumentValidationError(
                "result artifacts exceed the total byte bound"
            )
        if _result_digest(payloads) != result_digest:
            raise MetricDocumentValidationError("Attempt result digest does not match artifacts")

        run_manifest = _strict_json(payloads["run_manifest.json"], "run manifest")
        audit_root = self.state_root / "attempt-audit"
        with _root_relative_directory(
            self.state_root,
            audit_root,
            "Attempt audit directory",
        ) as audit_descriptor:
            audit_payload = _immutable_file_at(
                audit_descriptor,
                f"{attempt_id}.json",
                "Attempt audit",
                maximum_bytes=MAX_ATTEMPT_AUDIT_BYTES,
            )
        attempt_audit = _strict_json(audit_payload, "Attempt audit")
        expected_audit_fields = {
            "schema_version",
            "attempt_id",
            "experiment_id",
            "requested",
            "template",
            "dataset",
            "operators",
            "execution_identity",
            "run_id",
            "result_path",
            "result_digest",
        }
        if (
            set(attempt_audit) != expected_audit_fields
            or attempt_audit["schema_version"] != 1
            or attempt_audit["attempt_id"] != attempt_id
            or attempt_audit["experiment_id"] != experiment_id
            or attempt_audit["requested"] != dict(requested)
            or attempt_audit["template"] != resolved.get("template")
            or attempt_audit["dataset"] != resolved.get("dataset")
            or attempt_audit["operators"] != resolved.get("operators")
            or attempt_audit["execution_identity"]
            != resolved.get("execution_identity")
            or attempt_audit["run_id"] != root.name
            or Path(attempt_audit["result_path"]).absolute() != root
            or attempt_audit["result_digest"] != result_digest
        ):
            raise MetricDocumentValidationError(
                "Attempt audit does not match canonical execution identity"
            )
        candidate_binding = _candidate_attempt_binding(
            candidate_configuration,
            resolved,
            candidate_digest=candidate_digest,
            experiment_id=experiment_id,
            attempt_id=attempt_id,
            result_digest=result_digest,
            attempt_audit_digest=_sha256(audit_payload),
        )
        files = run_manifest.get("files")
        if not isinstance(files, dict) or set(files) != required - {"run_manifest.json"}:
            raise MetricDocumentValidationError("run manifest artifact digest map is invalid")
        artifact_digests: dict[str, str] = {}
        for name, descriptor in files.items():
            payload = payloads[name]
            if not isinstance(descriptor, dict) or descriptor != {
                "sha256": _sha256(payload),
                "size": len(payload),
            }:
                raise MetricDocumentValidationError(
                    f"run manifest artifact digest mismatch: {name}"
                )
            artifact_digests[name] = descriptor["sha256"]
        if (
            not isinstance(run_manifest.get("run_id"), str)
            or run_manifest["run_id"] != root.name
            or SHA256.fullmatch(run_manifest["run_id"]) is None
            or run_manifest.get("dataset_snapshot_id") != snapshot_id
            or run_manifest.get("dataset_canonical_sha256")
            != dataset.get("canonical_sha256")
        ):
            raise MetricDocumentValidationError("run manifest dataset identity mismatch")

        metrics = _strict_json(payloads["metrics.json"], "metrics")
        costs = _strict_json(payloads["cost_breakdown.json"], "cost breakdown")
        _finite(metrics, "metrics")
        _finite(costs, "cost_breakdown")
        try:
            daily = pd.read_csv(BytesIO(payloads["daily_replay.csv"]))
            events = pd.read_csv(BytesIO(payloads["events.csv"]))
            trades = pd.read_csv(BytesIO(payloads["trades.csv"]))
        except (OSError, pd.errors.ParserError, UnicodeDecodeError) as exc:
            raise MetricDocumentValidationError("account CSV artifacts are invalid") from exc
        if daily.empty:
            raise MetricDocumentValidationError("daily replay cannot be empty")
        if (
            len(daily) > MAX_SCORED_SESSIONS
            or len(events) > MAX_LEDGER_ROWS
            or len(trades) > MAX_LEDGER_ROWS
        ):
            raise MetricDocumentValidationError("account evidence exceeds bounded row limits")
        daily_dates = _date_series(daily, "Date", "daily replay")
        if not daily_dates.is_monotonic_increasing or daily_dates.duplicated().any():
            raise MetricDocumentValidationError("daily replay dates must be unique and ordered")
        scored_dates = dataset_frame.loc[
            (dataset_frame["Date"] >= pd.Timestamp(expected_window["scoring_start"]))
            & (dataset_frame["Date"] <= pd.Timestamp(expected_window["scoring_end"])),
            "Date",
        ].dt.strftime("%Y-%m-%d").tolist()
        if daily["Date"].tolist() != scored_dates:
            raise MetricDocumentValidationError(
                "daily replay dates do not exactly match the committed scoring mask"
            )
        if (
            metrics.get("period_start") != scored_dates[0]
            or metrics.get("period_end") != scored_dates[-1]
        ):
            raise MetricDocumentValidationError("metric dates do not match scored sessions")

        _numeric(
            daily,
            (
                "cash",
                "holdings",
                "market_value",
                "equity",
                "price",
                "close",
                "quantity",
                "position_before",
                "position_after",
                "gross_pnl",
                "net_pnl",
                *COST_FIELDS,
            ),
            "daily replay",
        )
        if set(events) != set(EVENT_COLUMNS):
            raise MetricDocumentValidationError("event ledger columns are invalid")
        if set(trades) != set(TRADE_COLUMNS):
            raise MetricDocumentValidationError("trade ledger columns are invalid")
        _numeric(
            events,
            (
                "price",
                "quantity",
                "notional_cny",
                *COST_FIELDS,
                "cash_before_cny",
                "cash_after_cny",
                "holdings_before",
                "holdings_after",
            ),
            "event ledger",
        )
        if not events.empty:
            event_dates = _date_series(events, "Date", "event ledger")
            if (
                not event_dates.is_monotonic_increasing
                or event_dates.min() < daily_dates.min()
                or event_dates.max() > daily_dates.max()
            ):
                raise MetricDocumentValidationError("event ledger dates are invalid")
        _numeric(
            trades,
            (
                "entry_price",
                "quantity",
                "entry_cost_cny",
                "exit_cost_cny",
                "gross_pnl_cny",
                "net_pnl_cny",
                "return",
            ),
            "trade ledger",
        )
        if not trades.empty:
            for column in ("entry_date", "exit_date"):
                trade_dates = _date_series(trades, column, "trade ledger")
                if (
                    trade_dates.min() < daily_dates.min()
                    or trade_dates.max() > daily_dates.max()
                ):
                    raise MetricDocumentValidationError("trade ledger dates are invalid")

        required_metrics = {
            "initial_capital_cny",
            "final_equity_cny",
            "net_profit_cny",
            "net_return",
            "max_drawdown",
            "closed_trades",
            "open_trades",
            "current_position",
        }
        if not required_metrics <= set(metrics):
            raise MetricDocumentValidationError("metrics are incomplete")
        initial = float(metrics["initial_capital_cny"])
        final = float(daily["equity"].iloc[-1])
        total_cost = float(events["total_cost_cny"].sum())
        scored_market = dataset_frame.loc[
            dataset_frame["Date"].dt.strftime("%Y-%m-%d").isin(scored_dates),
            ["Date", "Open", "Close"],
        ].copy()
        scored_market["Date"] = scored_market["Date"].dt.strftime("%Y-%m-%d")
        scored_market = scored_market.set_index("Date")
        if (
            initial <= 0
            or not np.allclose(
                daily["price"],
                scored_market.loc[daily["Date"], "Open"].to_numpy(dtype=float),
                rtol=1e-12,
                atol=1e-8,
            )
            or not np.allclose(
                daily["close"],
                scored_market.loc[daily["Date"], "Close"].to_numpy(dtype=float),
                rtol=1e-12,
                atol=1e-8,
            )
            or not np.allclose(
                daily["cash"] + daily["market_value"],
                daily["equity"],
                rtol=1e-12,
                atol=1e-8,
            )
            or not _close(final, float(metrics["final_equity_cny"]), scale=initial)
            or not _close(final - initial, float(metrics["net_profit_cny"]), scale=initial)
            or not _close(final / initial - 1.0, float(metrics["net_return"]))
            or not _close(total_cost, float(daily["total_cost_cny"].sum()), scale=initial)
            or not _close(total_cost, float(costs.get("total_cost_cny", math.nan)), scale=initial)
            or not _close(
                total_cost,
                float(trades["entry_cost_cny"].sum() + trades["exit_cost_cny"].sum()),
                scale=initial,
            )
            or not _close(float(trades["net_pnl_cny"].sum()), final - initial, scale=initial)
            or not _close(float(daily["net_pnl"].iloc[-1]), final - initial, scale=initial)
        ):
            raise MetricDocumentValidationError(
                "ledger, equity, cost, and metric artifacts do not reconcile"
            )
        stored_reconciliation = run_manifest.get("reconciliation")
        if (
            not isinstance(stored_reconciliation, dict)
            or set(stored_reconciliation) != RECONCILIATION_FIELDS
            or any(value is not True for value in stored_reconciliation.values())
        ):
            raise MetricDocumentValidationError("run manifest reconciliation is not successful")
        if (
            int(daily["holdings"].iloc[-1]) != 0
            or metrics["current_position"] != "FLAT"
            or metrics["open_trades"] != 0
            or (not trades.empty and set(trades["status"]) != {"CLOSED"})
        ):
            raise MetricDocumentValidationError(
                "FORCE_FLAT_WITH_COST evidence did not close its terminal position"
            )
        if len(events):
            if not all(
                _close(
                    float(row.total_cost_cny),
                    sum(float(getattr(row, name)) for name in COST_FIELDS[:-1]),
                )
                for row in events.itertuples(index=False)
            ):
                raise MetricDocumentValidationError("event cost components do not reconcile")
        expected_cash = initial
        expected_holdings = 0
        cumulative_cost = 0.0
        for daily_row in daily.itertuples(index=False):
            date = daily_row.Date
            day_events = events.loc[events["Date"] == date]
            if (
                int(daily_row.position_before) != int(expected_holdings > 0)
                or not _close(
                    float(daily_row.price),
                    float(scored_market.loc[date, "Open"]),
                )
                or not _close(
                    float(daily_row.close),
                    float(scored_market.loc[date, "Close"]),
                )
            ):
                raise MetricDocumentValidationError(
                    "daily opening state or prices do not reconcile"
                )
            signed_quantity = 0
            day_cost = 0.0
            for event in day_events.itertuples(index=False):
                quantity = int(event.quantity)
                if (
                    quantity <= 0
                    or float(event.quantity) != quantity
                    or not _close(float(event.price), float(daily_row.price))
                    or not _close(
                        float(event.notional_cny),
                        float(event.price) * quantity,
                        scale=initial,
                    )
                    or not _close(
                        float(event.cash_before_cny),
                        expected_cash,
                        scale=initial,
                    )
                    or int(event.holdings_before) != expected_holdings
                ):
                    raise MetricDocumentValidationError(
                        "event ledger opening state, price, or notional does not reconcile"
                    )
                if event.side == "BUY":
                    expected_cash -= float(event.notional_cny) + float(
                        event.total_cost_cny
                    )
                    expected_holdings += quantity
                    signed_quantity += quantity
                elif event.side == "SELL":
                    expected_cash += float(event.notional_cny) - float(
                        event.total_cost_cny
                    )
                    expected_holdings -= quantity
                    signed_quantity -= quantity
                else:
                    raise MetricDocumentValidationError("event ledger side is invalid")
                day_cost += float(event.total_cost_cny)
                if (
                    not _close(
                        float(event.cash_after_cny),
                        expected_cash,
                        scale=initial,
                    )
                    or int(event.holdings_after) != expected_holdings
                    or expected_holdings < 0
                ):
                    raise MetricDocumentValidationError(
                        "event ledger closing state does not reconcile"
                    )
            cumulative_cost += day_cost
            close = float(daily_row.close)
            expected_market_value = expected_holdings * close
            expected_equity = expected_cash + expected_market_value
            if (
                float(daily_row.holdings) != expected_holdings
                or int(daily_row.position_after) != int(expected_holdings > 0)
                or float(daily_row.quantity) != signed_quantity
                or not _close(float(daily_row.cash), expected_cash, scale=initial)
                or not _close(
                    float(daily_row.market_value),
                    expected_market_value,
                    scale=initial,
                )
                or not _close(
                    float(daily_row.equity),
                    expected_equity,
                    scale=initial,
                )
                or not _close(
                    float(daily_row.total_cost_cny),
                    day_cost,
                    scale=initial,
                )
                or not _close(
                    float(daily_row.net_pnl),
                    expected_equity - initial,
                    scale=initial,
                )
                or not _close(
                    float(daily_row.gross_pnl),
                    expected_equity - initial + cumulative_cost,
                    scale=initial,
                )
                or any(
                    not _close(
                        float(getattr(daily_row, field)),
                        float(day_events[field].sum()),
                        scale=initial,
                    )
                    for field in COST_FIELDS
                )
            ):
                raise MetricDocumentValidationError(
                    "daily holdings, cash, equity, costs, or PnL do not reconcile"
                )
        for field in COST_FIELDS:
            if not _close(
                float(costs.get(field, math.nan)),
                float(events[field].sum()),
                scale=initial,
            ):
                raise MetricDocumentValidationError(
                    f"cost breakdown does not reconcile: {field}"
                )
        for trade in trades.to_dict("records"):
            gross = (float(trade["exit_price"]) - float(trade["entry_price"])) * int(
                trade["quantity"]
            )
            net = gross - float(trade["entry_cost_cny"]) - float(
                trade["exit_cost_cny"]
            )
            basis = float(trade["entry_price"]) * int(trade["quantity"]) + float(
                trade["entry_cost_cny"]
            )
            if (
                trade["status"] != "CLOSED"
                or not _close(gross, float(trade["gross_pnl_cny"]), scale=initial)
                or not _close(net, float(trade["net_pnl_cny"]), scale=initial)
                or not _close(net / basis, float(trade["return"]))
            ):
                raise MetricDocumentValidationError("trade ledger does not reconcile")
        expected_trades: list[tuple[dict[str, Any], dict[str, Any]]] = []
        open_event: dict[str, Any] | None = None
        for event in events.to_dict("records"):
            if event["side"] == "BUY":
                if open_event is not None:
                    raise MetricDocumentValidationError(
                        "event ledger opens overlapping positions"
                    )
                open_event = event
            else:
                if open_event is None:
                    raise MetricDocumentValidationError(
                        "event ledger closes a position that is not open"
                    )
                expected_trades.append((open_event, event))
                open_event = None
        if open_event is not None or len(expected_trades) != len(trades):
            raise MetricDocumentValidationError(
                "trade ledger does not match ordered execution events"
            )
        for trade, (entry, exit_) in zip(
            trades.to_dict("records"),
            expected_trades,
            strict=True,
        ):
            if (
                trade["entry_date"] != entry["Date"]
                or trade["exit_date"] != exit_["Date"]
                or not _close(float(trade["entry_price"]), float(entry["price"]))
                or not _close(float(trade["exit_price"]), float(exit_["price"]))
                or int(trade["quantity"]) != int(entry["quantity"])
                or int(trade["quantity"]) != int(exit_["quantity"])
                or not _close(
                    float(trade["entry_cost_cny"]),
                    float(entry["total_cost_cny"]),
                    scale=initial,
                )
                or not _close(
                    float(trade["exit_cost_cny"]),
                    float(exit_["total_cost_cny"]),
                    scale=initial,
                )
            ):
                raise MetricDocumentValidationError(
                    "trade ledger identity does not reconcile with events"
                )

        equity = daily["equity"].to_numpy(dtype=float)
        returns = np.diff(np.concatenate(([initial], equity))) / np.concatenate(
            ([initial], equity[:-1])
        )
        if not np.isfinite(returns).all():
            raise MetricDocumentValidationError("derived daily returns are not finite")
        standard_deviation = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
        net_sharpe = (
            float(np.sqrt(252.0) * np.mean(returns) / standard_deviation)
            if standard_deviation > 0
            else 0.0
        )
        drawdown = equity / np.maximum.accumulate(np.concatenate(([initial], equity)))[1:] - 1.0
        maximum_drawdown = max(0.0, -float(drawdown.min()))
        annual_turnover = (
            float(events["notional_cny"].sum()) / initial * 252.0 / len(daily)
        )
        independent = {
            "net_return": float(equity[-1] / initial - 1.0),
            "net_sharpe": net_sharpe,
            "maximum_drawdown": maximum_drawdown,
            "annual_turnover": annual_turnover,
            "closed_trades": int((trades["status"] == "CLOSED").sum()),
            "total_cost_cny": total_cost,
            "final_equity_cny": final,
        }
        if (
            not _close(-maximum_drawdown, float(metrics["max_drawdown"]))
            or int(metrics["closed_trades"]) != independent["closed_trades"]
        ):
            raise MetricDocumentValidationError(
                "reported drawdown or trade metrics do not reconcile"
            )
        _finite(independent, "independent_metrics")
        document = {
                "schema_version": 1,
                "metric_engine": deepcopy(METRIC_ENGINE_IDENTITY),
                "candidate_digest": candidate_digest,
                "candidate_binding": candidate_binding,
                "experiment_id": experiment_id,
                "attempt_id": attempt_id,
                "result_digest": result_digest,
                "dataset_snapshot_id": snapshot_id,
                "scoring_mask_sha256": manifest["scoring_mask_sha256"],
                "fold_window": expected_window,
                "artifact_digests": dict(sorted(artifact_digests.items())),
                "scored_dates": scored_dates,
                "net_daily_returns": [
                    {"date": date, "return": float(value)}
                    for date, value in zip(scored_dates, returns, strict=True)
                ],
                "metrics": independent,
                "reported_metrics": metrics,
                "reconciliation": {
                    "immutable_artifacts": True,
                    "scoring_mask": True,
                    "finite_values": True,
                    "dates": True,
                    "ledger_equity_cost": True,
                    "force_flat_with_cost": True,
                },
            }
        document["document_digest"] = _sha256(canonical_json_bytes(document))
        return _issue_verified_metric_document(document)


class RobustWalkForwardPolicy:
    """Built-in transparent, deterministic robust walk-forward policy."""

    identity = deepcopy(EVALUATION_POLICY_IDENTITY)

    def evaluate(
        self,
        candidate_digest: str,
        metric_documents: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(candidate_digest, str) or SHA256.fullmatch(candidate_digest) is None:
            raise EvaluationPolicyError("candidate_digest must be a lowercase SHA-256 digest")
        if not metric_documents:
            raise EvaluationPolicyError("at least one Metric Document is required")
        if len(metric_documents) > MAX_METRIC_DOCUMENTS_PER_EVALUATION:
            raise EvaluationPolicyError(
                "Metric Document count exceeds the evaluation bound"
            )
        required_parameters = {
            "stability_weight",
            "turnover_weight",
            "minimum_trades",
            "maximum_drawdown",
            "maximum_annual_turnover",
        }
        if not isinstance(parameters, Mapping) or set(parameters) != required_parameters:
            raise EvaluationPolicyError("policy parameters are invalid")
        stability_weight = self._nonnegative(parameters["stability_weight"], "stability_weight")
        turnover_weight = self._nonnegative(parameters["turnover_weight"], "turnover_weight")
        minimum_trades = parameters["minimum_trades"]
        if isinstance(minimum_trades, bool) or not isinstance(minimum_trades, int) or minimum_trades < 0:
            raise EvaluationPolicyError("minimum_trades must be a non-negative integer")
        maximum_drawdown = self._optional_nonnegative(
            parameters["maximum_drawdown"], "maximum_drawdown"
        )
        maximum_turnover = self._optional_nonnegative(
            parameters["maximum_annual_turnover"], "maximum_annual_turnover"
        )

        required_document_fields = {
            "schema_version",
            "metric_engine",
            "candidate_digest",
            "candidate_binding",
            "experiment_id",
            "attempt_id",
            "result_digest",
            "dataset_snapshot_id",
            "scoring_mask_sha256",
            "fold_window",
            "artifact_digests",
            "scored_dates",
            "net_daily_returns",
            "metrics",
            "reported_metrics",
            "reconciliation",
            "document_digest",
        }
        documents: list[dict[str, Any]] = []
        for index, factory_document in enumerate(metric_documents):
            if (
                not isinstance(factory_document, VerifiedMetricDocument)
                or not _is_pristine_verified_metric_document(factory_document)
            ):
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] is not pristine "
                    "MetricDocumentFactory-issued evidence"
                )
            document = dict(factory_document)
            if (
                set(document) != required_document_fields
                or document.get("schema_version") != 1
                or document.get("metric_engine") != METRIC_ENGINE_IDENTITY
                or not isinstance(document.get("document_digest"), str)
                or _sha256(
                    canonical_json_bytes(
                        {
                            key: value
                            for key, value in document.items()
                            if key != "document_digest"
                        }
                    )
                )
                != document["document_digest"]
            ):
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] schema or digest is invalid"
                )
            if document.get("candidate_digest") != candidate_digest:
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] belongs to another candidate"
                )
            candidate_binding = document.get("candidate_binding")
            if (
                not isinstance(candidate_binding, Mapping)
                or set(candidate_binding)
                != {
                    "strategy_configuration",
                    "strategy_configuration_digest",
                    "experiment_configuration_digest",
                    "attempt_audit_digest",
                }
                or candidate_binding.get("strategy_configuration_digest")
                != candidate_digest
                or _sha256(
                    canonical_json_bytes(candidate_binding.get("strategy_configuration"))
                )
                != candidate_digest
                or any(
                    not isinstance(candidate_binding.get(key), str)
                    or SHA256.fullmatch(candidate_binding[key]) is None
                    for key in (
                        "experiment_configuration_digest",
                        "attempt_audit_digest",
                    )
                )
            ):
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] candidate binding is invalid"
                )
            reconciliation = document.get("reconciliation")
            if (
                not isinstance(reconciliation, Mapping)
                or set(reconciliation)
                != {
                    "immutable_artifacts",
                    "scoring_mask",
                    "finite_values",
                    "dates",
                    "ledger_equity_cost",
                    "force_flat_with_cost",
                }
                or any(value is not True for value in reconciliation.values())
            ):
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] is not verified evidence"
                )
            expected_metrics = {
                "net_return",
                "net_sharpe",
                "maximum_drawdown",
                "annual_turnover",
                "closed_trades",
                "total_cost_cny",
                "final_equity_cny",
            }
            if (
                not isinstance(document.get("metrics"), Mapping)
                or set(document["metrics"]) != expected_metrics
                or not isinstance(document.get("scored_dates"), list)
                or not document["scored_dates"]
                or not isinstance(document.get("net_daily_returns"), list)
                or len(document["net_daily_returns"]) != len(document["scored_dates"])
                or [
                    item.get("date") if isinstance(item, Mapping) else None
                    for item in document["net_daily_returns"]
                ]
                != document["scored_dates"]
            ):
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] metric evidence is invalid"
                )
            role = document.get("fold_window", {}).get("role")
            if role not in {"INNER_SCORE", "OUTER_AUDIT", "TERMINAL_HOLDOUT"}:
                raise EvaluationPolicyError(f"metric_documents[{index}] has an invalid role")
            documents.append(document)
        if len({document["attempt_id"] for document in documents}) != len(documents):
            raise EvaluationPolicyError("one Attempt cannot supply multiple Metric Documents")
        roles = {document["fold_window"]["role"] for document in documents}
        if len(roles) != 1:
            raise EvaluationPolicyError("one policy evaluation cannot mix evidence roles")
        ordered = sorted(
            documents,
            key=lambda document: (
                document["fold_window"]["scoring_start"],
                document["document_digest"],
            ),
        )
        for left, right in zip(ordered, ordered[1:]):
            if left["fold_window"]["scoring_end"] >= right["fold_window"]["scoring_start"]:
                raise EvaluationPolicyError("Metric Document scoring windows overlap")
        fold_sharpes = [float(document["metrics"]["net_sharpe"]) for document in ordered]
        fold_median = float(median(fold_sharpes))
        fold_mad = float(median(abs(value - fold_median) for value in fold_sharpes))
        total_sessions = sum(len(document["scored_dates"]) for document in ordered)
        if total_sessions > MAX_SCORED_SESSIONS:
            raise EvaluationPolicyError(
                "Metric Document sessions exceed the evaluation bound"
            )
        annual_turnover = sum(
            float(document["metrics"]["annual_turnover"]) * len(document["scored_dates"])
            for document in ordered
        ) / total_sessions
        maximum_drawdown_value = max(
            float(document["metrics"]["maximum_drawdown"]) for document in ordered
        )
        closed_trades = sum(int(document["metrics"]["closed_trades"]) for document in ordered)
        validation_score = (
            fold_median
            - stability_weight * fold_mad
            - turnover_weight * annual_turnover
        )
        constraints = {
            "minimum_trades": {
                "actual": closed_trades,
                "limit": minimum_trades,
                "passed": closed_trades >= minimum_trades,
            },
            "maximum_drawdown": {
                "actual": maximum_drawdown_value,
                "limit": maximum_drawdown,
                "passed": (
                    maximum_drawdown is None
                    or maximum_drawdown_value <= maximum_drawdown
                ),
            },
            "maximum_annual_turnover": {
                "actual": annual_turnover,
                "limit": maximum_turnover,
                "passed": maximum_turnover is None or annual_turnover <= maximum_turnover,
            },
        }
        eligible = all(item["passed"] for item in constraints.values())
        result = {
            "policy_identity": deepcopy(EVALUATION_POLICY_IDENTITY),
            "policy_digest": EVALUATION_POLICY_DIGEST,
            "policy_id": EVALUATION_POLICY_IDENTITY["policy_id"],
            "version": EVALUATION_POLICY_IDENTITY["version"],
            "candidate_digest": candidate_digest,
            "evidence_role": next(iter(roles)),
            "eligibility": "ELIGIBLE" if eligible else "INELIGIBLE",
            "eligible": eligible,
            "validation_score": validation_score,
            "independent_metrics": {
                "fold_net_sharpe": fold_sharpes,
                "median_fold_net_sharpe": fold_median,
                "mad_fold_net_sharpe": fold_mad,
                "maximum_drawdown": maximum_drawdown_value,
                "annual_turnover": annual_turnover,
                "closed_trades": closed_trades,
                "net_return_by_fold": [
                    float(document["metrics"]["net_return"]) for document in ordered
                ],
            },
            "constraints": constraints,
            "tie_break": {
                "lower_maximum_drawdown": maximum_drawdown_value,
                "lower_annual_turnover": annual_turnover,
                "strategy_configuration_digest": candidate_digest,
            },
            "explanation": {
                "formula": EVALUATION_POLICY_IDENTITY["validation_score"],
                "components": {
                    "median_fold_net_sharpe": fold_median,
                    "stability_weight": stability_weight,
                    "mad_fold_net_sharpe": fold_mad,
                    "turnover_weight": turnover_weight,
                    "annual_turnover": annual_turnover,
                },
                "constraint_failures": [
                    name for name, value in constraints.items() if not value["passed"]
                ],
            },
            "metric_document_digests": [
                document["document_digest"] for document in ordered
            ],
        }
        if not math.isfinite(validation_score):
            raise EvaluationPolicyError("validation score is not finite")
        result["evaluation_digest"] = _sha256(canonical_json_bytes(result))
        return result

    @staticmethod
    def ranking_key(value: Mapping[str, Any]) -> tuple[float, float, float, str]:
        return (
            -float(value["validation_score"]),
            float(value["tie_break"]["lower_maximum_drawdown"]),
            float(value["tie_break"]["lower_annual_turnover"]),
            str(value["tie_break"]["strategy_configuration_digest"]),
        )

    def select(self, evaluations: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        eligible = [dict(value) for value in evaluations if value.get("eligible") is True]
        if not eligible:
            return None
        return min(eligible, key=self.ranking_key)

    @staticmethod
    def _nonnegative(value: Any, label: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise EvaluationPolicyError(f"{label} must be a finite non-negative number")
        return float(value)

    @classmethod
    def _optional_nonnegative(cls, value: Any, label: str) -> float | None:
        return None if value is None else cls._nonnegative(value, label)


class NestedChronologicalSelection:
    """Evaluate nested inner selection, ordered outer OOS, and one holdout."""

    def __init__(self, policy: RobustWalkForwardPolicy | None = None):
        self.policy = policy or RobustWalkForwardPolicy()

    def evaluate(
        self,
        *,
        outer_rounds: Sequence[Mapping[str, Any]],
        final_inner_evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        parameters: Mapping[str, Any],
        holdout_document: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ordered_outer: list[dict[str, Any]] = []
        previous_outer_end: str | None = None
        for expected_round, round_value in enumerate(outer_rounds, start=1):
            if not isinstance(round_value, Mapping) or set(round_value) != {
                "round",
                "inner_evidence",
                "outer_document",
            }:
                raise EvaluationPolicyError("outer round shape is invalid")
            if round_value["round"] != expected_round:
                raise EvaluationPolicyError("outer rounds must be contiguous and ordered")
            inner_evidence = round_value["inner_evidence"]
            if not isinstance(inner_evidence, Mapping):
                raise EvaluationPolicyError("outer round inner_evidence must be an object")
            evaluations = self._candidate_evaluations(
                inner_evidence,
                parameters,
                required_role="INNER_SCORE",
            )
            selected = self.policy.select(evaluations)
            outer_document = round_value["outer_document"]
            if selected is None:
                if outer_document is not None:
                    raise EvaluationPolicyError(
                        "an outer run cannot exist without an eligible inner selection"
                    )
                ordered_outer.append(
                    {
                        "round": expected_round,
                        "selection_outcome": "NO_ELIGIBLE_CANDIDATE",
                        "candidate_evaluations": evaluations,
                        "selected_candidate_digest": None,
                        "metric_document_digest": None,
                    }
                )
                continue
            if (
                not isinstance(outer_document, Mapping)
                or outer_document.get("candidate_digest")
                != selected["candidate_digest"]
                or outer_document.get("fold_window", {}).get("role") != "OUTER_AUDIT"
            ):
                raise EvaluationPolicyError(
                    "outer evidence must evaluate only the inner-selected candidate"
                )
            start = outer_document["fold_window"]["scoring_start"]
            end = outer_document["fold_window"]["scoring_end"]
            if previous_outer_end is not None and start <= previous_outer_end:
                raise EvaluationPolicyError("outer OOS evidence must be chronological")
            previous_outer_end = end
            ordered_outer.append(
                {
                    "round": expected_round,
                    "selection_outcome": "CHAMPION_SELECTED",
                    "candidate_evaluations": evaluations,
                    "selected_candidate_digest": selected["candidate_digest"],
                    "metric_document_digest": outer_document["document_digest"],
                    "net_daily_returns": deepcopy(outer_document["net_daily_returns"]),
                }
            )

        final_evaluations = self._candidate_evaluations(
            final_inner_evidence,
            parameters,
            required_role="INNER_SCORE",
        )
        champion = self.policy.select(final_evaluations)
        stitched_returns = [
            value
            for outer in ordered_outer
            for value in outer.get("net_daily_returns", [])
        ]
        if champion is None:
            if holdout_document is not None:
                raise EvaluationPolicyError(
                    "holdout evidence cannot exist without an eligible champion"
                )
            return {
                "selection_outcome": "NO_ELIGIBLE_CANDIDATE",
                "holdout_outcome": "NOT_RUN",
                "champion": None,
                "outer_rounds": ordered_outer,
                "outer_selection_process": {
                    "account_policy": "FORCE_FLAT_WITH_COST",
                    "ordered_net_daily_returns": stitched_returns,
                },
                "final_candidate_evaluations": final_evaluations,
            }

        champion_digest = champion["candidate_digest"]
        holdout_outcome = "NOT_RUN"
        holdout_evaluation = None
        if holdout_document is not None:
            if (
                holdout_document.get("candidate_digest") != champion_digest
                or holdout_document.get("fold_window", {}).get("role")
                != "TERMINAL_HOLDOUT"
            ):
                raise EvaluationPolicyError(
                    "holdout evidence must belong to the single frozen champion"
                )
            holdout_evaluation = self.policy.evaluate(
                champion_digest,
                [holdout_document],
                parameters,
            )
            holdout_outcome = (
                "PASSED" if holdout_evaluation["eligible"] else "FAILED"
            )
        return {
            "selection_outcome": "CHAMPION_SELECTED",
            "holdout_outcome": holdout_outcome,
            "champion": champion,
            "outer_rounds": ordered_outer,
            "outer_selection_process": {
                "account_policy": "FORCE_FLAT_WITH_COST",
                "ordered_net_daily_returns": stitched_returns,
            },
            "final_candidate_evaluations": final_evaluations,
            "holdout_evaluation": holdout_evaluation,
        }

    def _candidate_evaluations(
        self,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        parameters: Mapping[str, Any],
        *,
        required_role: str,
    ) -> list[dict[str, Any]]:
        evaluations: list[dict[str, Any]] = []
        for candidate_digest in sorted(evidence):
            documents = evidence[candidate_digest]
            if not documents:
                continue
            if any(
                document.get("fold_window", {}).get("role") != required_role
                for document in documents
            ):
                raise EvaluationPolicyError(
                    "outer or holdout evidence cannot feed inner candidate selection"
                )
            evaluations.append(
                self.policy.evaluate(candidate_digest, documents, parameters)
            )
        return evaluations


def robust_walk_forward(
    candidate_digest: str,
    metric_documents: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one candidate with the built-in versioned policy."""

    return RobustWalkForwardPolicy().evaluate(
        candidate_digest,
        metric_documents,
        parameters,
    )
