from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
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
POLICY_IDENTITY = {
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
}


class MetricDocumentValidationError(RuntimeError):
    """Raised when immutable strategy evidence cannot be trusted."""


class EvaluationPolicyError(ValueError):
    """Raised when evaluation evidence or policy parameters are invalid."""


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


def _immutable_file(path: Path, root: Path, label: str) -> bytes:
    try:
        before = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o222
            or before.st_nlink != 1
        ):
            raise MetricDocumentValidationError(f"{label} is not an immutable regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise MetricDocumentValidationError(f"{label} changed while opening")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
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
        if path.parent != root:
            raise MetricDocumentValidationError(f"{label} is outside the result directory")
        return b"".join(chunks)
    except MetricDocumentValidationError:
        raise
    except OSError as exc:
        raise MetricDocumentValidationError(f"{label} cannot be read safely") from exc


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


class MetricDocumentFactory:
    """Verify immutable run artifacts and derive policy-safe account metrics."""

    def __init__(self, state_root: Path | str):
        self.state_root = Path(state_root).absolute()

    def from_attempt(
        self,
        attempt: Mapping[str, Any],
        *,
        candidate_digest: str,
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
            dataset=dataset,
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
        dataset: Mapping[str, Any],
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

        root = Path(result_path).absolute() if isinstance(result_path, (str, Path)) else None
        try:
            root_metadata = os.stat(root, follow_symlinks=False) if root is not None else None
        except OSError as exc:
            raise MetricDocumentValidationError("result directory is unavailable") from exc
        if (
            root is None
            or root_metadata is None
            or not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_IMODE(root_metadata.st_mode) & 0o222
        ):
            raise MetricDocumentValidationError("result directory is not immutable")
        if any(
            component.is_symlink()
            for component in (root, *root.parents)
            if component.exists()
        ):
            raise MetricDocumentValidationError("result directory contains a symlink")
        names = {entry.name for entry in os.scandir(root)}
        required = {*RESULT_ARTIFACTS, "run_manifest.json", "config.json", "report.html"}
        if names != required:
            raise MetricDocumentValidationError("result artifact set is incomplete or unexpected")
        payloads = {
            name: _immutable_file(root / name, root, f"result artifact {name}")
            for name in sorted(required)
        }
        if _result_digest(payloads) != result_digest:
            raise MetricDocumentValidationError("Attempt result digest does not match artifacts")

        run_manifest = _strict_json(payloads["run_manifest.json"], "run manifest")
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
                "net_pnl",
                "total_cost_cny",
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
        if (
            initial <= 0
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
        for event in events.itertuples(index=False):
            if (
                not _close(float(event.cash_before_cny), expected_cash, scale=initial)
                or int(event.holdings_before) != expected_holdings
            ):
                raise MetricDocumentValidationError(
                    "event ledger opening state does not reconcile"
                )
            if event.side == "BUY":
                expected_cash -= float(event.notional_cny) + float(event.total_cost_cny)
                expected_holdings += int(event.quantity)
            elif event.side == "SELL":
                expected_cash += float(event.notional_cny) - float(event.total_cost_cny)
                expected_holdings -= int(event.quantity)
            else:
                raise MetricDocumentValidationError("event ledger side is invalid")
            if (
                not _close(float(event.cash_after_cny), expected_cash, scale=initial)
                or int(event.holdings_after) != expected_holdings
            ):
                raise MetricDocumentValidationError(
                    "event ledger closing state does not reconcile"
                )
        if (
            not _close(expected_cash, float(daily["cash"].iloc[-1]), scale=initial)
            or expected_holdings != int(daily["holdings"].iloc[-1])
        ):
            raise MetricDocumentValidationError(
                "event ledger terminal state does not match daily equity"
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
            "metric_engine": {
                "name": "account_daily_equity",
                "version": "1.0.0",
                "semantics": "net-account-daily-equity-force-terminal-policy",
            },
            "candidate_digest": candidate_digest,
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
        return document


class RobustWalkForwardPolicy:
    """Built-in transparent, deterministic robust walk-forward policy."""

    identity = deepcopy(POLICY_IDENTITY)

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

        documents = [dict(document) for document in metric_documents]
        for index, document in enumerate(documents):
            if document.get("candidate_digest") != candidate_digest:
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] belongs to another candidate"
                )
            if set(document.get("reconciliation", {}).values()) != {True}:
                raise EvaluationPolicyError(
                    f"metric_documents[{index}] is not verified evidence"
                )
            role = document.get("fold_window", {}).get("role")
            if role not in {"INNER_SCORE", "OUTER_AUDIT", "TERMINAL_HOLDOUT"}:
                raise EvaluationPolicyError(f"metric_documents[{index}] has an invalid role")
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
            "policy_id": "robust_walk_forward",
            "version": "1.0.0",
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
                "formula": POLICY_IDENTITY["validation_score"],
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

    def select(self, evaluations: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        eligible = [dict(value) for value in evaluations if value.get("eligible") is True]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda value: (
                -float(value["validation_score"]),
                float(value["tie_break"]["lower_maximum_drawdown"]),
                float(value["tie_break"]["lower_annual_turnover"]),
                value["tie_break"]["strategy_configuration_digest"],
            ),
        )

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
