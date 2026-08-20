from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from gold_research.backtest import backtest, metrics


def constant_exposure_signal(candidate_signal: pd.Series) -> pd.Series:
    """Return the retrospective scored-period mean exposure on every scored row."""
    if not isinstance(candidate_signal, pd.Series) or candidate_signal.empty:
        raise ValueError("candidate signal must be a non-empty pandas Series")
    values = pd.to_numeric(candidate_signal, errors="coerce").astype(float)
    if not np.isfinite(values.to_numpy()).all() or not values.between(0.0, 1.0).all():
        raise ValueError("candidate signal must be finite and between zero and one")
    return pd.Series(
        float(values.mean()),
        index=candidate_signal.index,
        name="constant_exposure_matched",
    )


def _validate_datetime_index(index: pd.Index) -> pd.DatetimeIndex:
    if (
        not isinstance(index, pd.DatetimeIndex)
        or index.empty
        or index.hasnans
        or index.has_duplicates
        or not index.is_monotonic_increasing
    ):
        raise ValueError("index must be a non-empty DatetimeIndex that is unique and sorted")
    return index


def non_overlapping_four_year_blocks(
    index: pd.Index,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return complete four-calendar-year block boundaries for a scored index.

    Coverage in the first seven calendar days counts as a complete opening year.
    This deterministic rule accommodates New Year market holidays without assuming
    that every generic weekday, especially January 1, was a trading session.
    """
    dates = _validate_datetime_index(index)
    first_year = int(dates[0].year)
    last_date = dates[-1]
    blocks: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    timezone = dates.tz
    starts_in_opening_week = dates[0].month == 1 and dates[0].day <= 7
    start_year = first_year if starts_in_opening_week else first_year + 1
    while start_year + 3 <= int(last_date.year):
        end_year = start_year + 3
        calendar_end = pd.Timestamp(year=end_year, month=12, day=31, tz=timezone)
        last_business_day = pd.offsets.BDay().rollback(calendar_end)
        year_is_complete = end_year < int(last_date.year) or last_date >= last_business_day
        if not year_is_complete:
            break
        blocks.append(
            (
                pd.Timestamp(year=start_year, month=1, day=1, tz=timezone),
                calendar_end,
            )
        )
        start_year += 4
    return blocks


def _validated_result_returns(result: pd.DataFrame, label: str) -> pd.Series:
    if not isinstance(result, pd.DataFrame) or "net_return" not in result:
        raise ValueError(f"{label} result must contain net_return")
    _validate_datetime_index(result.index)
    values = pd.to_numeric(result["net_return"], errors="coerce").astype(float)
    if not np.isfinite(values.to_numpy()).all() or (values <= -1.0).any():
        raise ValueError(f"{label} net returns must be finite and greater than negative one")
    return values


def subperiod_stability(
    candidate_result: pd.DataFrame,
    constant_result: pd.DataFrame,
) -> dict[str, object]:
    """Compare candidate and constant exposure over predeclared calendar blocks."""
    candidate = _validated_result_returns(candidate_result, "candidate")
    constant = _validated_result_returns(constant_result, "constant")
    if not candidate.index.equals(constant.index):
        raise ValueError("candidate and constant result indexes must match exactly")

    rows: list[dict[str, object]] = []
    for start, end in non_overlapping_four_year_blocks(candidate.index):
        candidate_block = candidate.loc[start:end]
        constant_block = constant.loc[start:end]
        candidate_log_return = float(np.log1p(candidate_block).sum())
        constant_log_return = float(np.log1p(constant_block).sum())
        relative_log_return = candidate_log_return - constant_log_return
        rows.append(
            {
                "start": start,
                "end": end,
                "start_year": int(start.year),
                "end_year": int(end.year),
                "candidate_compounded_return": float(np.expm1(candidate_log_return)),
                "constant_compounded_return": float(np.expm1(constant_log_return)),
                "relative_log_return": relative_log_return,
                "pass": relative_log_return > 0.0,
            }
        )

    complete_blocks = len(rows)
    positive_blocks = sum(bool(row["pass"]) for row in rows)
    positive_fraction = positive_blocks / complete_blocks if complete_blocks else 0.0
    if complete_blocks < 3:
        status = "INSUFFICIENT"
    else:
        status = "PASS" if positive_fraction >= 0.60 else "FAIL"
    return {
        "blocks": rows,
        "complete_blocks": complete_blocks,
        "positive_relative_log_blocks": positive_blocks,
        "positive_fraction": float(positive_fraction),
        "status": status,
    }


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _nonnegative_cost(value: object) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("cost values must be finite and non-negative")
    try:
        cost = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("cost values must be finite and non-negative") from error
    if not np.isfinite(cost) or cost < 0.0:
        raise ValueError("cost values must be finite and non-negative")
    return cost


def circular_shift_timing_test(
    open_price: pd.Series,
    signal: pd.Series,
    buy_cost_bps: float,
    sell_cost_bps: float,
    samples: int = 10_000,
    min_shift: int = 60,
    seed: int = 20260820,
    family_size: int = 4,
) -> dict[str, float | int]:
    """Run a deterministic circular-shift timing placebo with asymmetric costs."""
    sample_count = _positive_integer(samples, "samples")
    minimum_shift = _positive_integer(min_shift, "min_shift")
    family_count = _positive_integer(family_size, "family_size")
    if isinstance(seed, (bool, np.bool_)) or not isinstance(seed, (int, np.integer)):
        raise ValueError("seed must be an integer")
    buy_cost = _nonnegative_cost(buy_cost_bps)
    sell_cost = _nonnegative_cost(sell_cost_bps)
    if not isinstance(open_price, pd.Series) or not isinstance(signal, pd.Series):
        raise ValueError("open prices and signal must be pandas Series")
    if not open_price.index.equals(signal.index):
        raise ValueError("open price and signal indexes must match exactly")
    dates = _validate_datetime_index(open_price.index)

    prices = pd.to_numeric(open_price, errors="coerce").to_numpy(dtype=float)
    exposures = pd.to_numeric(signal, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0.0).any():
        raise ValueError("open prices must be finite and strictly positive")
    if not np.isfinite(exposures).all() or (exposures < 0.0).any() or (exposures > 1.0).any():
        raise ValueError("signal must be finite and between zero and one")
    observations = len(prices)
    if observations < 2 * minimum_shift:
        raise ValueError("enough rows are required to permit the minimum circular shift")

    allowed_shifts = np.arange(minimum_shift, observations - minimum_shift + 1)
    rng = np.random.default_rng(int(seed))
    if sample_count >= len(allowed_shifts):
        shifts = allowed_shifts
    else:
        shifts = rng.choice(allowed_shifts, size=sample_count, replace=False)
    evaluated_count = len(shifts)
    distinct_shift_count = int(np.unique(shifts).size)
    forward_returns = np.zeros(observations, dtype=float)
    forward_returns[:-1] = prices[1:] / prices[:-1] - 1.0
    elapsed_years = max(
        (dates[-1] - dates[0]).days / 365.2425,
        max(observations - 1, 1) / 252.0,
    )

    actual_result = backtest(
        open_price,
        signal,
        buy_cost_bps=buy_cost,
        sell_cost_bps=sell_cost,
    )
    _validated_result_returns(actual_result, "actual timing")

    random_cagrs = np.empty(evaluated_count, dtype=float)
    random_drawdowns = np.empty(evaluated_count, dtype=float)
    positions = np.arange(observations)
    chunk_size = 256
    for start in range(0, evaluated_count, chunk_size):
        stop = min(start + chunk_size, evaluated_count)
        chunk_shifts = shifts[start:stop]
        shifted = exposures[(positions[None, :] - chunk_shifts[:, None]) % observations]
        delta = np.diff(shifted, axis=1, prepend=np.zeros((len(chunk_shifts), 1)))
        costs = (
            np.clip(delta, 0.0, None) * buy_cost / 10_000.0
            + np.clip(-delta, 0.0, None) * sell_cost / 10_000.0
        )
        net_returns = shifted * forward_returns - costs
        if not np.isfinite(net_returns).all() or (net_returns <= -1.0).any():
            raise ValueError("randomized net returns must be finite and greater than negative one")
        equity = np.cumprod(1.0 + net_returns, axis=1)
        terminal = equity[:, -1]
        chunk_cagrs = np.full(len(chunk_shifts), -1.0)
        positive_terminal = terminal > 0.0
        chunk_cagrs[positive_terminal] = terminal[positive_terminal] ** (1.0 / elapsed_years) - 1.0
        peaks = np.maximum.accumulate(np.maximum(equity, 1.0), axis=1)
        random_cagrs[start:stop] = chunk_cagrs
        random_drawdowns[start:stop] = np.min(equity / peaks - 1.0, axis=1)

    actual_metrics = metrics(actual_result)
    actual_cagr = float(actual_metrics["cagr"])
    raw_p_value = float((1 + np.count_nonzero(random_cagrs >= actual_cagr)) / (evaluated_count + 1))
    return {
        "observations": observations,
        "samples": evaluated_count,
        "requested_samples": sample_count,
        "evaluated_samples": evaluated_count,
        "distinct_shift_count": distinct_shift_count,
        "min_shift": minimum_shift,
        "seed": int(seed),
        "family_size": family_count,
        "actual_cagr": actual_cagr,
        "actual_max_drawdown": float(actual_metrics["max_drawdown"]),
        "random_cagr_q05": float(np.quantile(random_cagrs, 0.05)),
        "random_cagr_median": float(np.median(random_cagrs)),
        "random_cagr_q95": float(np.quantile(random_cagrs, 0.95)),
        "random_max_drawdown_q05": float(np.quantile(random_drawdowns, 0.05)),
        "random_max_drawdown_median": float(np.median(random_drawdowns)),
        "random_max_drawdown_q95": float(np.quantile(random_drawdowns, 0.95)),
        "raw_p_value": raw_p_value,
        "bonferroni_p_value": float(min(1.0, family_count * raw_p_value)),
    }


def _finite_mapping_value(metrics_map: object, key: str) -> float | None:
    if not isinstance(metrics_map, Mapping) or key not in metrics_map:
        return None
    value = metrics_map[key]
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def risk_utility_pass(
    candidate_metrics: Mapping[str, object],
    buy_hold_metrics: Mapping[str, object],
) -> bool:
    """Apply the frozen Round-2 risk-utility rule exactly."""
    candidate_sharpe = _finite_mapping_value(candidate_metrics, "sharpe")
    candidate_drawdown = _finite_mapping_value(candidate_metrics, "max_drawdown")
    candidate_calmar = _finite_mapping_value(candidate_metrics, "calmar")
    buy_hold_sharpe = _finite_mapping_value(buy_hold_metrics, "sharpe")
    buy_hold_drawdown = _finite_mapping_value(buy_hold_metrics, "max_drawdown")
    buy_hold_calmar = _finite_mapping_value(buy_hold_metrics, "calmar")
    values = (
        candidate_sharpe,
        candidate_drawdown,
        candidate_calmar,
        buy_hold_sharpe,
        buy_hold_drawdown,
        buy_hold_calmar,
    )
    if any(value is None for value in values):
        raise ValueError("risk utility metrics must be present and finite")
    assert candidate_drawdown is not None
    assert buy_hold_drawdown is not None
    if not (-1.0 <= candidate_drawdown <= 0.0 and -1.0 <= buy_hold_drawdown <= 0.0):
        raise ValueError("maximum drawdowns must be between negative one and zero")
    return bool(
        (
            candidate_sharpe > buy_hold_sharpe
            and abs(candidate_drawdown) <= 0.85 * abs(buy_hold_drawdown)
        )
        or candidate_calmar >= buy_hold_calmar + 0.10
    )


def _status_for_comparison(values: tuple[float | None, ...], passed: bool) -> str:
    if any(value is None for value in values):
        return "INSUFFICIENT"
    return "PASS" if passed else "FAIL"


def _risk_utility_status(candidate: object, buy_hold: object) -> str:
    candidate_sharpe = _finite_mapping_value(candidate, "sharpe")
    candidate_drawdown = _finite_mapping_value(candidate, "max_drawdown")
    candidate_calmar = _finite_mapping_value(candidate, "calmar")
    buy_hold_sharpe = _finite_mapping_value(buy_hold, "sharpe")
    buy_hold_drawdown = _finite_mapping_value(buy_hold, "max_drawdown")
    buy_hold_calmar = _finite_mapping_value(buy_hold, "calmar")
    if candidate_drawdown is not None and not -1.0 <= candidate_drawdown <= 0.0:
        return "INSUFFICIENT"
    if buy_hold_drawdown is not None and not -1.0 <= buy_hold_drawdown <= 0.0:
        return "INSUFFICIENT"

    branch_one_values = (
        candidate_sharpe,
        candidate_drawdown,
        buy_hold_sharpe,
        buy_hold_drawdown,
    )
    branch_two_values = (candidate_calmar, buy_hold_calmar)
    branch_one = None
    if all(value is not None for value in branch_one_values):
        branch_one = bool(
            candidate_sharpe > buy_hold_sharpe
            and abs(candidate_drawdown) <= 0.85 * abs(buy_hold_drawdown)
        )
    branch_two = None
    if all(value is not None for value in branch_two_values):
        branch_two = bool(candidate_calmar >= buy_hold_calmar + 0.10)
    if branch_one is True or branch_two is True:
        return "PASS"
    if branch_one is False and branch_two is False:
        return "FAIL"
    return "INSUFFICIENT"


def candidate_gate_decision(
    target_base_metrics: Mapping[str, object],
    target_stress_metrics: Mapping[str, object],
    target_buy_hold: Mapping[str, object],
    constant_benchmark: Mapping[str, object],
    timing_result: Mapping[str, object],
    stability_summary: Mapping[str, object],
    peer_sharpe_wins_count: int | None,
    execution_complete: bool | None,
) -> dict[str, object]:
    """Evaluate all frozen Round-2 gates independently, without selecting a winner."""
    base_cagr = _finite_mapping_value(target_base_metrics, "cagr")
    base_sharpe = _finite_mapping_value(target_base_metrics, "sharpe")
    stress_cagr = _finite_mapping_value(target_stress_metrics, "cagr")
    stress_sharpe = _finite_mapping_value(target_stress_metrics, "sharpe")
    buy_hold_cagr = _finite_mapping_value(target_buy_hold, "cagr")
    constant_cagr = _finite_mapping_value(constant_benchmark, "cagr")
    timing_p = _finite_mapping_value(timing_result, "bonferroni_p_value")
    if timing_p is not None and not 0.0 <= timing_p <= 1.0:
        timing_p = None

    base_status = _status_for_comparison(
        (base_cagr, base_sharpe),
        bool(
            base_cagr is not None and base_sharpe is not None and base_cagr > 0 and base_sharpe > 0
        ),
    )
    stress_status = _status_for_comparison(
        (stress_cagr, stress_sharpe),
        bool(
            stress_cagr is not None
            and stress_sharpe is not None
            and stress_cagr > 0
            and stress_sharpe > 0
        ),
    )
    retention_status = _status_for_comparison(
        (base_cagr, buy_hold_cagr),
        bool(
            base_cagr is not None
            and buy_hold_cagr is not None
            and base_cagr >= 0.70 * buy_hold_cagr
        ),
    )
    timing_status = _status_for_comparison(
        (base_cagr, constant_cagr, timing_p),
        bool(
            base_cagr is not None
            and constant_cagr is not None
            and timing_p is not None
            and base_cagr > constant_cagr
            and timing_p <= 0.05
        ),
    )

    stability = stability_summary.get("status") if isinstance(stability_summary, Mapping) else None
    stability_status = (
        stability if stability in {"PASS", "FAIL", "INSUFFICIENT"} else "INSUFFICIENT"
    )
    if peer_sharpe_wins_count is None:
        peer_status = "INSUFFICIENT"
    else:
        if (
            isinstance(peer_sharpe_wins_count, (bool, np.bool_))
            or not isinstance(peer_sharpe_wins_count, (int, np.integer))
            or not 0 <= peer_sharpe_wins_count <= 6
        ):
            raise ValueError("peer Sharpe wins count must be an integer from zero through six")
        peer_status = "PASS" if peer_sharpe_wins_count >= 4 else "FAIL"
    if execution_complete is None:
        execution_status = "INSUFFICIENT"
    elif isinstance(execution_complete, (bool, np.bool_)):
        execution_status = "PASS" if bool(execution_complete) else "FAIL"
    else:
        raise ValueError("execution_complete must be bool or None")

    gates = [
        {"id": "positive_base_cagr_and_sharpe", "status": base_status},
        {"id": "positive_stress_cagr_and_sharpe", "status": stress_status},
        {"id": "target_cagr_at_least_70pct_buy_hold", "status": retention_status},
        {
            "id": "risk_utility_improvement",
            "status": _risk_utility_status(target_base_metrics, target_buy_hold),
        },
        {
            "id": "beats_constant_exposure_and_familywise_random_timing_p_le_0_05",
            "status": timing_status,
        },
        {"id": "stable_across_predeclared_subperiods", "status": stability_status},
        {
            "id": "improves_risk_adjusted_objective_on_at_least_4_of_6_peers",
            "status": peer_status,
        },
        {"id": "executable_data_and_cost_model_complete", "status": execution_status},
    ]
    statuses = [str(gate["status"]) for gate in gates]
    if "FAIL" in statuses:
        overall = "REJECTED_NO_EDGE"
    elif "INSUFFICIENT" in statuses:
        overall = "INSUFFICIENT_EVIDENCE"
    else:
        overall = "HISTORICALLY_SUPPORTED_FOR_PAPER_FORWARD"
    return {"gates": gates, "overall": overall}
