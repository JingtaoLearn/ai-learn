from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ParameterSpec:
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] = ()
    nullable: bool = False


@dataclass(frozen=True)
class OperatorSpec:
    slot: str
    name: str
    version: str
    parameters: Mapping[str, ParameterSpec]


def validate_registry(
    registry: Mapping[tuple[str, str, str], OperatorSpec],
    template_parameter_names: set[str],
) -> None:
    owners: dict[str, str] = {
        name: "template" for name in template_parameter_names
    }
    for key, operator in registry.items():
        if key != (operator.slot, operator.name, operator.version):
            raise ValueError(f"operator registry key does not match descriptor: {key}")
        owner = f"{operator.slot}:{operator.name}:{operator.version}"
        for parameter_name in operator.parameters:
            if parameter_name in owners:
                raise ValueError(
                    "parameter name collision: "
                    f"{parameter_name} is owned by {owners[parameter_name]} and {owner}"
                )
            owners[parameter_name] = owner


OPERATOR_REGISTRY: Mapping[tuple[str, str, str], OperatorSpec] = MappingProxyType({
    ("fit", "prior_log_ols", "1"): OperatorSpec(
        "fit",
        "prior_log_ols",
        "1",
        {
            "window_sessions": ParameterSpec("integer", minimum=2),
            "price_column": ParameterSpec(
                "string", choices=("AdjustedClose", "Close")
            ),
        },
    ),
    ("smoothing", "recursive_log_ema", "1"): OperatorSpec(
        "smoothing",
        "recursive_log_ema",
        "1",
        {"span_sessions": ParameterSpec("integer", minimum=1)},
    ),
    ("statistic", "adjacent_curve_pct_slope", "1"): OperatorSpec(
        "statistic", "adjacent_curve_pct_slope", "1", {}
    ),
    (
        "decision",
        "post_start_threshold_crossing_hysteresis",
        "1",
    ): OperatorSpec(
        "decision",
        "post_start_threshold_crossing_hysteresis",
        "1",
        {
            "buy_threshold_pct_per_day": ParameterSpec("number", minimum=0),
            "sell_threshold_abs_pct_per_day": ParameterSpec(
                "number", minimum=0
            ),
        },
    ),
    ("sizing", "all_in_all_out_a_share_lots", "1"): OperatorSpec(
        "sizing",
        "all_in_all_out_a_share_lots",
        "1",
        {
            "lot_size": ParameterSpec("integer", minimum=1),
            "target_fraction": ParameterSpec(
                "number", minimum=0, maximum=1
            ),
        },
    ),
    ("cost", "cms_china_a_share", "1"): OperatorSpec(
        "cost",
        "cms_china_a_share",
        "1",
        {
            "commission_rate": ParameterSpec("number", minimum=0),
            "minimum_commission_cny": ParameterSpec("number", minimum=0),
            "transfer_fee_rate": ParameterSpec("number", minimum=0),
            "sell_stamp_tax_rate": ParameterSpec("number", minimum=0),
            "buy_slippage_bps": ParameterSpec("number", minimum=0),
            "sell_slippage_bps": ParameterSpec("number", minimum=0),
        },
    ),
    ("report", "concise_chinese_causal_trade", "1"): OperatorSpec(
        "report", "concise_chinese_causal_trade", "1", {}
    ),
})


@dataclass(frozen=True)
class DecisionResult:
    action: str
    reason: str
    position: int
    previous_statistic: float | None


class HysteresisDecision:
    def __init__(
        self,
        *,
        buy_threshold_pct_per_day: float,
        sell_threshold_abs_pct_per_day: float,
    ):
        if buy_threshold_pct_per_day < 0 or sell_threshold_abs_pct_per_day < 0:
            raise ValueError("decision thresholds must be non-negative")
        self.buy_threshold = float(buy_threshold_pct_per_day)
        self.sell_threshold = float(sell_threshold_abs_pct_per_day)
        self.position = 0
        self.previous: float | None = None

    def step(self, statistic: float) -> DecisionResult:
        previous = self.previous
        if not np.isfinite(statistic):
            return DecisionResult("HOLD", "STATISTIC_UNAVAILABLE", self.position, previous)
        statistic = float(statistic)
        if previous is None:
            self.previous = statistic
            return DecisionResult("HOLD", "INITIALIZE_ZONE", self.position, None)

        crossed_up = previous < self.buy_threshold <= statistic
        crossed_down = previous > -self.sell_threshold >= statistic
        action = "HOLD"
        reason = "NO_THRESHOLD_CROSSING"
        if self.position == 0 and crossed_up:
            self.position = 1
            action = "BUY"
            reason = "BUY_THRESHOLD_CROSSING"
        elif self.position == 1 and crossed_down:
            self.position = 0
            action = "SELL"
            reason = "SELL_THRESHOLD_CROSSING"
        elif self.position == 0 and crossed_down:
            reason = "SELL_CROSSING_IGNORED_WHILE_FLAT"
        self.previous = statistic
        return DecisionResult(action, reason, self.position, previous)


def prior_log_ols(
    prices: pd.Series, *, window_sessions: int
) -> pd.DataFrame:
    if isinstance(window_sessions, bool) or window_sessions < 2:
        raise ValueError("window_sessions must be an integer of at least 2")
    numeric = prices.astype(float)
    if (numeric <= 0).any() or not np.isfinite(numeric.to_numpy()).all():
        raise ValueError("fit prices must be finite and strictly positive")
    result = pd.DataFrame(
        {
            "history_start": pd.Series(pd.NaT, index=numeric.index, dtype="datetime64[ns]"),
            "history_end": pd.Series(pd.NaT, index=numeric.index, dtype="datetime64[ns]"),
            "curve": np.nan,
        },
        index=numeric.index,
    )
    x = np.arange(window_sessions, dtype=float)
    design = np.column_stack([x, np.ones(window_sessions)])
    projection = np.array([float(window_sessions), 1.0]) @ np.linalg.pinv(design)
    log_prices = np.log(numeric.to_numpy())
    for position in range(window_sessions, len(numeric)):
        start = position - window_sessions
        history = log_prices[start:position]
        result.iloc[position, result.columns.get_loc("history_start")] = numeric.index[start]
        result.iloc[position, result.columns.get_loc("history_end")] = numeric.index[position - 1]
        result.iloc[position, result.columns.get_loc("curve")] = float(
            np.exp(projection @ history)
        )
    return result


def recursive_log_ema(values: pd.Series, *, span_sessions: int) -> pd.Series:
    if isinstance(span_sessions, bool) or span_sessions < 1:
        raise ValueError("span_sessions must be a positive integer")
    numeric = values.astype(float)
    finite = numeric.dropna()
    if (finite <= 0).any() or not np.isfinite(finite.to_numpy()).all():
        raise ValueError("smoothing values must be finite and strictly positive")
    return np.exp(np.log(numeric).ewm(span=span_sessions, adjust=False).mean())


def adjacent_curve_pct_slope(values: pd.Series) -> pd.Series:
    numeric = values.astype(float)
    return numeric.pct_change(fill_method=None) * 100.0


def cms_cost_breakdown(
    *,
    side: str,
    raw_price: float,
    quantity: int,
    commission_rate: float,
    minimum_commission_cny: float,
    transfer_fee_rate: float,
    sell_stamp_tax_rate: float,
    buy_slippage_bps: float,
    sell_slippage_bps: float,
) -> dict[str, float]:
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported cost side: {side}")
    if quantity < 0 or isinstance(quantity, bool):
        raise ValueError("quantity must be a non-negative integer")
    if raw_price <= 0 or not np.isfinite(raw_price):
        raise ValueError("raw_price must be finite and strictly positive")
    values = (
        commission_rate,
        minimum_commission_cny,
        transfer_fee_rate,
        sell_stamp_tax_rate,
        buy_slippage_bps,
        sell_slippage_bps,
    )
    if any(value < 0 or not np.isfinite(value) for value in values):
        raise ValueError("cost parameters must be finite and non-negative")
    notional = float(raw_price) * quantity
    if quantity == 0:
        commission = 0.0
    else:
        commission = max(notional * commission_rate, minimum_commission_cny)
    transfer = notional * transfer_fee_rate
    stamp = notional * sell_stamp_tax_rate if side == "SELL" else 0.0
    slippage_bps = buy_slippage_bps if side == "BUY" else sell_slippage_bps
    slippage = notional * slippage_bps / 10_000.0
    return {
        "commission_cny": commission,
        "transfer_fee_cny": transfer,
        "stamp_tax_cny": stamp,
        "slippage_cny": slippage,
        "total_cost_cny": commission + transfer + stamp + slippage,
    }


def all_in_quantity(
    *,
    cash: float,
    raw_price: float,
    lot_size: int,
    target_fraction: float,
    cost_parameters: Mapping[str, float],
) -> int:
    if cash < 0 or not np.isfinite(cash):
        raise ValueError("cash must be finite and non-negative")
    if raw_price <= 0 or not np.isfinite(raw_price):
        raise ValueError("raw_price must be finite and strictly positive")
    if isinstance(lot_size, bool) or lot_size < 1:
        raise ValueError("lot_size must be a positive integer")
    if not 0 <= target_fraction <= 1 or not np.isfinite(target_fraction):
        raise ValueError("target_fraction must be between zero and one")
    budget = cash * target_fraction
    quantity = int(budget // (raw_price * lot_size)) * lot_size
    while quantity > 0:
        costs = cms_cost_breakdown(
            side="BUY",
            raw_price=raw_price,
            quantity=quantity,
            **cost_parameters,
        )
        if raw_price * quantity + costs["total_cost_cny"] <= budget:
            return quantity
        quantity -= lot_size
    return 0
