from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .schemas import canonical_json_bytes

MARKET_JOB_TYPE = "xnys-msft-trend-study-v1"
SNAPSHOT_SCHEMA = "quantresearch-xnys-total-return-snapshot/v1"
KERNEL_IDENTITY = "quant_platform.msft_trend_study@1.0.0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_RECORD_FIELDS = (
    "session_date",
    "open",
    "close",
    "split_factor",
    "cash_dividend",
    "source_record_identity",
    "record_sealed_at",
)
PHASES = {
    "TRAIN": ("2022-02-01", "2023-12-29"),
    "VALIDATION": ("2024-01-02", "2024-12-31"),
    "FINAL": ("2025-01-02", None),
}
OPERATOR_IDENTITIES = {
    "SMA_CROSS": "msft_sma_cross@1.0.0",
    "BREAKOUT_TRAILING": "msft_breakout_trailing@1.0.0",
    "OLS_SLOPE_HYSTERESIS": "annualized_log_ols_slope@1.0.0+long_cash_level_hysteresis@1.0.0",
    "sizing": "whole_share_long_cash@1.0.0",
    "cost": "one_way_bps@1.0.0",
}


class MsftStudyValidationError(ValueError):
    """Frozen MSFT Study input or synthetic evidence is invalid."""


def _finite_number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise MsftStudyValidationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise MsftStudyValidationError(f"{label} must be {'positive ' if positive else ''}finite")
    return result


def _date(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise MsftStudyValidationError(f"{label} must be canonical YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise MsftStudyValidationError(f"{label} must be a real date") from exc
    return parsed.date().isoformat()


def _time(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise MsftStudyValidationError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MsftStudyValidationError(f"{label} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.astimezone(UTC) != parsed:
        raise MsftStudyValidationError(f"{label} must be an RFC3339 UTC timestamp")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_snapshot_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)) or not records:
        raise MsftStudyValidationError("Snapshot records must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        if not isinstance(item, Mapping) or set(item) != set(REQUIRED_RECORD_FIELDS):
            raise MsftStudyValidationError(f"Snapshot record {index} fields are invalid")
        source_identity = item["source_record_identity"]
        if not isinstance(source_identity, str) or SHA256.fullmatch(source_identity) is None:
            raise MsftStudyValidationError("source_record_identity must be lowercase SHA-256")
        split = _finite_number(item["split_factor"], "split_factor", positive=True)
        dividend = _finite_number(item["cash_dividend"], "cash_dividend")
        if dividend < 0:
            raise MsftStudyValidationError("cash_dividend must be non-negative")
        normalized.append(
            {
                "session_date": _date(item["session_date"], "session_date"),
                "open": _finite_number(item["open"], "open", positive=True),
                "close": _finite_number(item["close"], "close", positive=True),
                "split_factor": split,
                "cash_dividend": dividend,
                "source_record_identity": source_identity,
                "record_sealed_at": _time(item["record_sealed_at"], "record_sealed_at"),
            }
        )
    dates = [row["session_date"] for row in normalized]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise MsftStudyValidationError("Snapshot sessions must be unique and increasing")
    return normalized


def build_snapshot(
    records: Sequence[Mapping[str, Any]],
    *,
    source_identity: Mapping[str, Any],
    sealed_at: str,
) -> dict[str, Any]:
    normalized = normalize_snapshot_records(records)
    sealed = _time(sealed_at, "sealed_at")
    if any(row["record_sealed_at"] > sealed for row in normalized):
        raise MsftStudyValidationError("record seal cannot be later than Snapshot seal")
    if not isinstance(source_identity, Mapping):
        raise MsftStudyValidationError("source_identity must be an object")
    try:
        source = dict(source_identity)
        source_digest = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise MsftStudyValidationError("source_identity must be finite canonical JSON") from exc
    core = {
        "schema": SNAPSHOT_SCHEMA,
        "instrument": "MSFT",
        "market": "XNYS",
        "currency": "USD",
        "price_semantics": "split-adjusted-dividend-unadjusted",
        "dividend_semantics": "explicit-cash-credit-in-adjusted-share-units",
        "share_semantics": "whole-split-adjusted-research-shares",
        "source_identity": source,
        "source_identity_sha256": source_digest,
        "sealed_at": sealed,
        "record_count": len(normalized),
        "data_start": normalized[0]["session_date"],
        "data_end": normalized[-1]["session_date"],
        "required_fields": list(REQUIRED_RECORD_FIELDS),
        "null_counts": {field: 0 for field in REQUIRED_RECORD_FIELDS},
        "records_sha256": hashlib.sha256(canonical_json_bytes(normalized)).hexdigest(),
    }
    return {
        **core,
        "snapshot_id": hashlib.sha256(
            b"quantresearch-xnys-snapshot/v1\0" + canonical_json_bytes(core)
        ).hexdigest(),
        "records": normalized,
    }


def validate_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MsftStudyValidationError("Snapshot must be an object")
    expected = {
        "schema",
        "instrument",
        "market",
        "currency",
        "price_semantics",
        "dividend_semantics",
        "share_semantics",
        "source_identity",
        "source_identity_sha256",
        "sealed_at",
        "record_count",
        "data_start",
        "data_end",
        "required_fields",
        "null_counts",
        "records_sha256",
        "snapshot_id",
        "records",
    }
    if set(value) != expected:
        raise MsftStudyValidationError("Snapshot fields are invalid")
    rebuilt = build_snapshot(
        value["records"], source_identity=value["source_identity"], sealed_at=value["sealed_at"]
    )
    if dict(value) != rebuilt:
        raise MsftStudyValidationError("Snapshot identity or metadata does not match its records")
    return rebuilt


def candidates() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for fast in (20, 50, 100):
        for slow in (100, 150, 200):
            if fast < slow:
                result.append({"family": "SMA_CROSS", "fast_sessions": fast, "slow_sessions": slow})
    for entry in (63, 126, 252):
        for exit_ in (20, 63, 126):
            if exit_ < entry:
                result.append(
                    {
                        "family": "BREAKOUT_TRAILING",
                        "entry_sessions": entry,
                        "exit_sessions": exit_,
                    }
                )
    result.append(
        {
            "family": "OLS_SLOPE_HYSTERESIS",
            "window_sessions": 126,
            "entry_threshold": 0.05,
            "exit_threshold": 0.0,
        }
    )
    if len(result) != 15:
        raise AssertionError("frozen candidate enumeration changed")
    return result


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        b"quantresearch-msft-candidate/v1\0" + canonical_json_bytes(dict(candidate))
    ).hexdigest()


def _signals(rows: Sequence[Mapping[str, Any]], candidate: Mapping[str, Any]) -> list[int]:
    close = np.asarray([row["close"] for row in rows], dtype=float)
    positions: list[int] = []
    prior = 0
    family = candidate["family"]
    for index in range(len(rows)):
        history = close[:index]
        target = prior
        if family == "SMA_CROSS":
            slow = int(candidate["slow_sessions"])
            if len(history) >= slow:
                fast = int(candidate["fast_sessions"])
                target = int(float(np.mean(history[-fast:])) > float(np.mean(history[-slow:])))
            else:
                target = 0
        elif family == "BREAKOUT_TRAILING":
            entry = int(candidate["entry_sessions"])
            exit_ = int(candidate["exit_sessions"])
            if len(history) >= entry + 1:
                previous = float(history[-1])
                if previous >= float(np.max(history[-entry - 1 : -1])):
                    target = 1
                elif previous <= float(np.min(history[-exit_ - 1 : -1])):
                    target = 0
            else:
                target = 0
        elif family == "OLS_SLOPE_HYSTERESIS":
            window = int(candidate["window_sessions"])
            if len(history) >= window:
                slope = float(np.polyfit(np.arange(window), np.log(history[-window:]), 1)[0] * 252.0)
                if slope > float(candidate["entry_threshold"]):
                    target = 1
                elif slope <= float(candidate["exit_threshold"]):
                    target = 0
            else:
                target = 0
        else:
            raise MsftStudyValidationError("candidate family is unsupported")
        positions.append(target)
        prior = target
    return positions


@dataclass(frozen=True)
class Replay:
    metrics: dict[str, Any]
    ledger: list[dict[str, Any]]
    trades: list[dict[str, Any]]


def _period_rows(
    rows: Sequence[Mapping[str, Any]], signals: Sequence[int], start: str, end: str
) -> tuple[list[Mapping[str, Any]], list[int]]:
    selected = [(row, signal) for row, signal in zip(rows, signals, strict=True) if start <= row["session_date"] <= end]
    if not selected:
        raise MsftStudyValidationError(f"no sessions are available for {start} through {end}")
    return [item[0] for item in selected], [item[1] for item in selected]


def replay(
    rows: Sequence[Mapping[str, Any]],
    signals: Sequence[int],
    *,
    start: str,
    end: str,
    one_way_bps: int,
    cash_annual_rate: float = 0.0,
    initial_capital: float = 100_000.0,
) -> Replay:
    period, positions = _period_rows(rows, signals, start, end)
    if len(signals) != len(rows) or one_way_bps not in {10, 25}:
        raise MsftStudyValidationError("replay inputs do not match the frozen contract")
    cash = float(initial_capital)
    shares = 0
    broker_shares = 0.0
    prior_date: datetime | None = None
    total_cost = 0.0
    total_notional = 0.0
    ledger: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None
    equities: list[float] = []
    exposures: list[int] = []
    for row, target in zip(period, positions, strict=True):
        session = datetime.strptime(row["session_date"], "%Y-%m-%d")
        days = 0 if prior_date is None else (session - prior_date).days
        cash_accrual = cash * cash_annual_rate * days / 365.0
        cash += cash_accrual
        split = float(row["split_factor"])
        broker_before = broker_shares
        if split != 1.0:
            broker_shares *= split
        dividend = shares * float(row["cash_dividend"])
        cash += dividend
        price = float(row["open"])
        side = None
        quantity = 0
        notional = 0.0
        cost = 0.0
        if target == 1 and shares == 0:
            quantity = math.floor(cash / (price * (1.0 + one_way_bps / 10_000.0)))
            if quantity > 0:
                notional = quantity * price
                cost = notional * one_way_bps / 10_000.0
                cash -= notional + cost
                shares = quantity
                broker_shares = float(quantity)
                side = "BUY"
                open_trade = {
                    "entry_date": row["session_date"],
                    "entry_price": price,
                    "quantity": quantity,
                    "entry_cost": cost,
                }
        elif target == 0 and shares > 0:
            quantity = shares
            notional = quantity * price
            cost = notional * one_way_bps / 10_000.0
            cash += notional - cost
            shares = 0
            broker_shares = 0.0
            side = "SELL"
            if open_trade is None:
                raise MsftStudyValidationError("ledger sell has no open trade")
            gross = (price - float(open_trade["entry_price"])) * quantity
            trades.append(
                {
                    **open_trade,
                    "exit_date": row["session_date"],
                    "exit_price": price,
                    "exit_cost": cost,
                    "gross_pnl": gross,
                    "net_pnl": gross - float(open_trade["entry_cost"]) - cost,
                }
            )
            open_trade = None
        total_cost += cost
        total_notional += notional
        equity = cash + shares * float(row["close"])
        equities.append(equity)
        exposures.append(int(shares > 0))
        ledger.append(
            {
                "session_date": row["session_date"],
                "signal_information_through": None if row is rows[0] else "t-1",
                "fill_timing": "open-t",
                "side": side,
                "quantity": quantity,
                "open": price,
                "close": float(row["close"]),
                "cost": cost,
                "cash_accrual": cash_accrual,
                "dividend_credit": dividend,
                "split_factor": split,
                "split_adjusted_shares": shares,
                "broker_shares_before_split": broker_before,
                "broker_shares_after_split": broker_shares,
                "cash": cash,
                "equity": equity,
            }
        )
        prior_date = session
    daily_returns = np.diff(np.concatenate(([initial_capital], np.asarray(equities)))) / np.concatenate(
        ([initial_capital], np.asarray(equities[:-1]))
    )
    elapsed_days = max(1, (datetime.strptime(period[-1]["session_date"], "%Y-%m-%d") - datetime.strptime(period[0]["session_date"], "%Y-%m-%d")).days)
    cumulative = equities[-1] / initial_capital - 1.0
    annualized = (equities[-1] / initial_capital) ** (365.0 / elapsed_days) - 1.0
    volatility = float(np.std(daily_returns, ddof=1) * math.sqrt(252.0)) if len(daily_returns) > 1 else 0.0
    mean = float(np.mean(daily_returns))
    std = float(np.std(daily_returns, ddof=1)) if len(daily_returns) > 1 else 0.0
    downside = np.minimum(daily_returns, 0.0)
    downside_std = float(np.sqrt(np.mean(np.square(downside))))
    sharpe = mean / std * math.sqrt(252.0) if std > 0 else 0.0
    sortino = mean / downside_std * math.sqrt(252.0) if downside_std > 0 else 0.0
    peaks = np.maximum.accumulate(np.concatenate(([initial_capital], np.asarray(equities))))[1:]
    drawdown = 1.0 - np.asarray(equities) / peaks
    maximum_drawdown = float(max(0.0, np.max(drawdown)))
    calmar = annualized / maximum_drawdown if maximum_drawdown > 0 else 0.0
    wins = [trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0]
    losses = [-trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0]
    profit_factor = sum(wins) / sum(losses) if losses else (0.0 if not wins else 1_000_000.0)
    years: dict[str, float] = {}
    start_equity = initial_capital
    for year in sorted({row["session_date"][:4] for row in period}):
        year_values = [equity for row, equity in zip(period, equities, strict=True) if row["session_date"].startswith(year)]
        years[year] = year_values[-1] / start_equity - 1.0
        start_equity = year_values[-1]
    metrics = {
        "period_start": period[0]["session_date"],
        "period_end": period[-1]["session_date"],
        "cumulative_return": cumulative,
        "annualized_return": annualized,
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "maximum_drawdown": maximum_drawdown,
        "trade_count": sum(1 for item in ledger if item["side"] is not None),
        "completed_round_trips": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor,
        "turnover": total_notional / initial_capital,
        "average_exposure": float(np.mean(exposures)),
        "cost_drag": total_cost / initial_capital,
        "final_equity": equities[-1],
        "yearly_returns": years,
        "terminal_position": {
            "state": "OPEN" if shares else "FLAT",
            "split_adjusted_whole_shares": shares,
            "broker_shares": broker_shares,
            "marked_at": "final-close",
            "fabricated_exit": False,
            "unrealized_pnl_included": shares > 0,
        },
    }
    if not all(math.isfinite(float(value)) for key, value in metrics.items() if isinstance(value, (int, float))):
        raise MsftStudyValidationError("replay produced a non-finite metric")
    return Replay(metrics=metrics, ledger=ledger, trades=trades)


def _rank(metrics: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(metrics["calmar"]),
        float(metrics["sharpe"]),
        float(metrics["annualized_return"]),
        -float(metrics["turnover"]),
    )


def _validation_pass(metrics: Mapping[str, Any], matched: Mapping[str, Any]) -> bool:
    return (
        metrics["annualized_return"] >= 0.0
        and metrics["sharpe"] >= 0.5
        and metrics["maximum_drawdown"] <= 0.40
        and metrics["annualized_return"] - matched["annualized_return"] >= 0.0
    )


def _matched_exposure(rows: Sequence[Mapping[str, Any]], start: str, end: str, exposure: float) -> dict[str, Any]:
    selected = [row for row in rows if start <= row["session_date"] <= end]
    first = float(selected[0]["open"])
    last = float(selected[-1]["close"])
    elapsed = max(1, (datetime.strptime(selected[-1]["session_date"], "%Y-%m-%d") - datetime.strptime(selected[0]["session_date"], "%Y-%m-%d")).days)
    total = exposure * (last / first - 1.0)
    return {
        "classification": "ANALYTICAL_NONTRADABLE_NO_REBALANCING_COSTS",
        "average_exposure": exposure,
        "cumulative_return": total,
        "annualized_return": (1.0 + total) ** (365.0 / elapsed) - 1.0 if 1.0 + total > 0 else -1.0,
        "sharpe": 0.0,
    }


def _neighbors(selected: Mapping[str, Any], population: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    family = selected["family"]
    fields = {
        "SMA_CROSS": ("fast_sessions", "slow_sessions"),
        "BREAKOUT_TRAILING": ("entry_sessions", "exit_sessions"),
        "OLS_SLOPE_HYSTERESIS": (),
    }[family]
    if not fields:
        return []
    family_population = [item for item in population if item["family"] == family]
    neighbors: list[Mapping[str, Any]] = []
    for item in family_population:
        differences = sum(item[field] != selected[field] for field in fields)
        if differences == 1:
            neighbors.append(item)
    return neighbors


def run_study(
    snapshot: Mapping[str, Any],
    *,
    checkpoint: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    frozen = validate_snapshot(snapshot)
    rows = frozen["records"]
    final_end = rows[-1]["session_date"]
    if final_end < PHASES["FINAL"][0]:
        raise MsftStudyValidationError("Snapshot does not contain the frozen FINAL period")
    population = candidates()
    train_results: list[tuple[dict[str, Any], Replay]] = []
    for index, candidate in enumerate(population, 1):
        signals = _signals(rows, candidate)
        trained = replay(
            rows,
            signals,
            start=PHASES["TRAIN"][0],
            end=PHASES["TRAIN"][1] or final_end,
            one_way_bps=10,
        )
        train_results.append((candidate, trained))
        if checkpoint is not None and index in {5, 10, 15}:
            checkpoint(
                {"completed_candidates": index, "total_candidates": 15, "checkpoint_sequence": index // 5},
                {"sequence": index // 5, "completed_candidates": index},
            )
    family_winners: list[tuple[dict[str, Any], Replay]] = []
    for family in ("SMA_CROSS", "BREAKOUT_TRAILING", "OLS_SLOPE_HYSTERESIS"):
        family_winners.append(max((item for item in train_results if item[0]["family"] == family), key=lambda item: _rank(item[1].metrics)))
    validation_rows: list[dict[str, Any]] = []
    passing: list[tuple[dict[str, Any], Replay]] = []
    for candidate, train in family_winners:
        result = replay(
            rows,
            _signals(rows, candidate),
            start=PHASES["VALIDATION"][0],
            end=PHASES["VALIDATION"][1] or final_end,
            one_way_bps=10,
        )
        matched = _matched_exposure(
            rows, PHASES["VALIDATION"][0], PHASES["VALIDATION"][1] or final_end, result.metrics["average_exposure"]
        )
        passed = _validation_pass(result.metrics, matched)
        validation_rows.append(
            {
                "family": candidate["family"],
                "candidate_id": _candidate_id(candidate),
                "train_metrics": train.metrics,
                "validation_metrics": result.metrics,
                "matched_exposure": matched,
                "passed": passed,
            }
        )
        if passed:
            passing.append((candidate, result))
    if not passing:
        return {
            "schema": "quantresearch-msft-study-result/v1",
            "kernel": KERNEL_IDENTITY,
            "snapshot_id": frozen["snapshot_id"],
            "trial_count": 15,
            "family_winners": validation_rows,
            "selection": None,
            "final": None,
            "verdict": "REJECTED_VALIDATION",
            "conclusion": "REJECTED_VALIDATION",
            "verdict_precedence": [
                "INVALID_OR_CONTAMINATED",
                "INCONCLUSIVE_DATA_OR_EXECUTION",
                "REJECTED_VALIDATION",
                "REJECTED_NO_EDGE",
                "QUALIFIED_FOR_PAPER",
            ],
            "per_trial_attempt_rows": 0,
            "terminal_exit_fabricated": False,
        }
    selected, selected_validation = max(passing, key=lambda item: _rank(item[1].metrics))
    final = replay(rows, _signals(rows, selected), start=PHASES["FINAL"][0], end=final_end, one_way_bps=10)
    stress = replay(rows, _signals(rows, selected), start=PHASES["FINAL"][0], end=final_end, one_way_bps=25)
    buy_hold = replay(rows, [1] * len(rows), start=PHASES["FINAL"][0], end=final_end, one_way_bps=10)
    cash = replay(rows, [0] * len(rows), start=PHASES["FINAL"][0], end=final_end, one_way_bps=10)
    cash3 = replay(rows, [0] * len(rows), start=PHASES["FINAL"][0], end=final_end, one_way_bps=10, cash_annual_rate=0.03)
    simple = {"family": "SMA_CROSS", "fast_sessions": 1, "slow_sessions": 200}
    simple_trend = replay(rows, _signals(rows, simple), start=PHASES["FINAL"][0], end=final_end, one_way_bps=10)
    matched = _matched_exposure(rows, PHASES["FINAL"][0], final_end, final.metrics["average_exposure"])
    neighbor_results = [
        replay(rows, _signals(rows, item), start=PHASES["FINAL"][0], end=final_end, one_way_bps=10).metrics
        for item in _neighbors(selected, population)
    ]
    stability = {
        "neighbor_count": len(neighbor_results),
        "positive_net_return_fraction": (
            sum(item["cumulative_return"] > 0 for item in neighbor_results) / len(neighbor_results)
            if neighbor_results
            else 1.0
        ),
        "median_sharpe": float(np.median([item["sharpe"] for item in neighbor_results])) if neighbor_results else 0.0,
    }
    risk_improvement = (
        final.metrics["sharpe"] - buy_hold.metrics["sharpe"] >= 0.20
        or (
            buy_hold.metrics["maximum_drawdown"] > 0
            and (buy_hold.metrics["maximum_drawdown"] - final.metrics["maximum_drawdown"])
            / buy_hold.metrics["maximum_drawdown"]
            >= 0.20
        )
    )
    gates = {
        "net_cagr": final.metrics["annualized_return"] >= 0.05,
        "sharpe": final.metrics["sharpe"] >= 0.70,
        "sortino": final.metrics["sortino"] >= 0.90,
        "calmar": final.metrics["calmar"] >= 0.35,
        "maximum_drawdown": final.metrics["maximum_drawdown"] <= 0.30,
        "completed_round_trips": final.metrics["completed_round_trips"] >= 3,
        "buy_hold_shortfall": final.metrics["annualized_return"] >= buy_hold.metrics["annualized_return"] - 0.03,
        "risk_improvement": risk_improvement,
        "timing_return": final.metrics["annualized_return"] - matched["annualized_return"] >= 0.02,
        "timing_sharpe": final.metrics["sharpe"] - matched["sharpe"] >= 0.15,
        "stress_cagr": stress.metrics["annualized_return"] >= 0.0,
        "stress_sharpe": stress.metrics["sharpe"] >= 0.50,
        "neighbor_positive": stability["positive_net_return_fraction"] >= 0.50,
        "neighbor_sharpe": stability["median_sharpe"] >= 0.0,
        "validation_passed": True,
    }
    verdict = "QUALIFIED_FOR_PAPER" if all(gates.values()) else "REJECTED_NO_EDGE"
    return {
        "schema": "quantresearch-msft-study-result/v1",
        "kernel": KERNEL_IDENTITY,
        "snapshot_id": frozen["snapshot_id"],
        "trial_count": 15,
        "family_winners": validation_rows,
        "selection": {
            "candidate_id": _candidate_id(selected),
            "rule": selected,
            "operator_identities": OPERATOR_IDENTITIES,
            "validation_metrics": selected_validation.metrics,
            "no_final_reselection": True,
        },
        "final": {
            "baseline_cost_metrics": final.metrics,
            "stress_cost_metrics": stress.metrics,
            "baselines": {
                "BUY_AND_HOLD_MSFT": buy_hold.metrics,
                "CASH_BASE": cash.metrics,
                "CASH_3PCT_SENSITIVITY": cash3.metrics,
                "PRICE_ABOVE_SMA200_SIMPLE_TREND": simple_trend.metrics,
                "MATCHED_AVERAGE_EXPOSURE_ANALYTICAL_NONTRADABLE": matched,
            },
            "neighborhood_stability": stability,
            "qualification_gates": gates,
            "ledger_digest": hashlib.sha256(canonical_json_bytes(final.ledger)).hexdigest(),
        },
        "verdict": verdict,
        "conclusion": verdict,
        "verdict_precedence": [
            "INVALID_OR_CONTAMINATED",
            "INCONCLUSIVE_DATA_OR_EXECUTION",
            "REJECTED_VALIDATION",
            "REJECTED_NO_EDGE",
            "QUALIFIED_FOR_PAPER",
        ],
        "per_trial_attempt_rows": 0,
        "terminal_exit_fabricated": False,
    }


def chinese_report(result: Mapping[str, Any], provenance: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    allowed_verdicts = {
        "INCONCLUSIVE_DATA_OR_EXECUTION",
        "REJECTED_VALIDATION",
        "REJECTED_NO_EDGE",
        "QUALIFIED_FOR_PAPER",
    }
    if (
        result.get("schema") != "quantresearch-msft-study-result/v1"
        or result.get("verdict") not in allowed_verdicts
        or not isinstance(result.get("snapshot_id"), str)
        or SHA256.fullmatch(result["snapshot_id"]) is None
        or type(result.get("trial_count")) is not int
        or not 0 <= result["trial_count"] <= 15
    ):
        raise MsftStudyValidationError("Study result cannot be rendered")
    document = {
        "schema": "quantresearch-msft-study-report/v1",
        "language": "zh-CN",
        "title": "微软（MSFT）趋势研究结论",
        "verdict": result["verdict"],
        "research_only": True,
        "not_a_trade_recommendation": True,
        "snapshot_id": result["snapshot_id"],
        "trial_count": result["trial_count"],
        "selection": result["selection"],
        "final": result["final"],
        "provenance": dict(provenance),
        "limitations": [
            "不含个人资本利得税；没有账户特定税务合同。",
            "匹配暴露控制为不可交易分析控制，未建模再平衡成本。",
            "终点无下一交易日开盘时按最后完整交易日收盘估值，不虚构平仓。",
            "整股口径为拆股调整后的研究份额；拆股事件单独对账，不改变经济权益。",
        ],
    }
    document_id = hashlib.sha256(
        b"quantresearch-msft-study-report/v1\0" + canonical_json_bytes(document)
    ).hexdigest()
    document["report_artifact_id"] = document_id
    html = (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        "<title>MSFT 趋势研究结论</title><main>"
        f"<h1>{document['title']}</h1><p><strong>{document['verdict']}</strong></p>"
        "<p>仅供研究，不构成交易建议。</p>"
        f"<p>Snapshot: <code>{document['snapshot_id']}</code></p>"
        f"<p>候选数量: {document['trial_count']}</p>"
        "<h2>限制</h2><ul>"
        + "".join(f"<li>{item}</li>" for item in document["limitations"])
        + "</ul></main></html>"
    ).encode("utf-8")
    return document, html


def build_report_pointer(
    study_id: str,
    report_artifact_id: str,
    current: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if SHA256.fullmatch(study_id) is None or SHA256.fullmatch(report_artifact_id) is None:
        raise MsftStudyValidationError("Study report pointer identity is invalid")
    if current is not None:
        if (
            not isinstance(current.get("report_artifact_id"), str)
            or SHA256.fullmatch(current["report_artifact_id"]) is None
            or type(current.get("sequence")) is not int
            or current["sequence"] < 1
        ):
            raise MsftStudyValidationError("Current Study report pointer is invalid")
        if current["report_artifact_id"] == report_artifact_id:
            return None
    return {
        "schema_version": 1,
        "study_id": study_id,
        "sequence": 1 if current is None else current["sequence"] + 1,
        "report_artifact_id": report_artifact_id,
        "prior_report_artifact_id": (
            None if current is None else current["report_artifact_id"]
        ),
    }
