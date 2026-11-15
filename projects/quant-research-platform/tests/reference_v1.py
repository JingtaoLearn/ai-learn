"""Verbatim core of the retired production signal (v1), kept only to prove engine equivalence."""
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence


class ProductionJobError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrendConfig:
    window_sessions: int
    ema_span: int
    buy_threshold_pct_per_day: float
    sell_threshold_pct_per_day: float
    anchor_date: date


def fit_next_log(values: Sequence[float], window: int) -> float:
    if len(values) != window or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ProductionJobError("trend window must contain finite positive values")
    logs = [math.log(value) for value in values]
    x_mean = (window - 1) / 2.0
    y_mean = sum(logs) / window
    denominator = sum((index - x_mean) ** 2 for index in range(window))
    slope = sum((index - x_mean) * (logs[index] - y_mean) for index in range(window))
    return y_mean + slope / denominator * (window - x_mean)


def decision_points(rows: Sequence[Mapping[str, Any]], config: TrendConfig) -> list[dict[str, Any]]:
    values = [float(row["signal_close"]) for row in rows]
    alpha = 2.0 / (config.ema_span + 1.0)
    smoothed: float | None = None
    previous: float | None = None
    points: list[dict[str, Any]] = []
    for position in range(config.window_sessions, len(rows) + 1):
        raw = fit_next_log(values[position - config.window_sessions : position], config.window_sessions)
        smoothed = raw if smoothed is None else alpha * raw + (1 - alpha) * smoothed
        slope = None if previous is None else 100.0 * math.expm1(smoothed - previous)
        points.append(
            {
                "decision_date": rows[position]["date"] if position < len(rows) else None,
                "is_next_session": position == len(rows),
                "raw_curve": math.exp(raw),
                "smooth_curve": math.exp(smoothed),
                "slope_pct": slope,
            }
        )
        previous = smoothed
    return points


def evaluate(points: Sequence[Mapping[str, Any]], config: TrendConfig) -> dict[str, Any]:
    eligible = [
        point
        for point in points
        if point["is_next_session"]
        or (
            isinstance(point["decision_date"], date)
            and point["decision_date"] >= config.anchor_date
        )
    ]
    state = 0
    prior_slope: float | None = None
    state_before = 0
    action = "WAIT"
    reason = "no upward crossing; remain flat"
    for point in eligible:
        value = point["slope_pct"]
        if value is None:
            continue
        slope = float(value)
        if point["is_next_session"]:
            state_before = state
            if prior_slope is not None:
                if state == 0 and prior_slope < config.buy_threshold_pct_per_day <= slope:
                    state, action, reason = 1, "BUY", "signal crossed upward through the frozen buy line"
                elif state == 1 and prior_slope > config.sell_threshold_pct_per_day >= slope:
                    state, action, reason = 0, "SELL", "signal crossed downward through the frozen sell line"
                elif state == 1:
                    action, reason = "HOLD", "signal did not cross the frozen sell line"
            break
        if prior_slope is not None:
            if state == 0 and prior_slope < config.buy_threshold_pct_per_day <= slope:
                state = 1
            elif state == 1 and prior_slope > config.sell_threshold_pct_per_day >= slope:
                state = 0
        prior_slope = slope
    if not eligible or eligible[-1]["is_next_session"] is not True or prior_slope is None:
        raise ProductionJobError("history cannot produce the next-session decision")
    point = eligible[-1]
    return {
        "action": action,
        "reason": reason,
        "state_before_next": state_before,
        "target_state": state,
        "previous_slope_pct": prior_slope,
        "next_slope_pct": float(point["slope_pct"]),
        "next_raw_curve": float(point["raw_curve"]),
        "next_smooth_curve": float(point["smooth_curve"]),
    }


def projected_slope(
    rows: Sequence[Mapping[str, Any]], config: TrendConfig, candidate_raw_close: float
) -> float:
    if not math.isfinite(candidate_raw_close) or candidate_raw_close <= 0:
        raise ProductionJobError("candidate close must be finite and positive")
    points = decision_points(rows, config)
    current = math.log(float(points[-1]["smooth_curve"]))
    factor = float(rows[-1]["signal_close"]) / float(rows[-1]["close"])
    values = [float(row["signal_close"]) for row in rows[-(config.window_sessions - 1) :]]
    values.append(candidate_raw_close * factor)
    next_raw = fit_next_log(values, config.window_sessions)
    alpha = 2.0 / (config.ema_span + 1.0)
    next_smooth = alpha * next_raw + (1 - alpha) * current
    return 100.0 * math.expm1(next_smooth - current)


def close_for_slope(
    rows: Sequence[Mapping[str, Any]], config: TrendConfig, target: float
) -> float:
    if not math.isfinite(target) or target <= -100:
        raise ProductionJobError("target slope is invalid")
    low = math.log(float(rows[-1]["close"])) - 50
    high = math.log(float(rows[-1]["close"])) + 50
    for _ in range(180):
        middle = (low + high) / 2
        if projected_slope(rows, config, math.exp(middle)) < target:
            low = middle
        else:
            high = middle
    return math.exp((low + high) / 2)

