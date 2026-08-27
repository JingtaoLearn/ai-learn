from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .datasets import DatasetValidationError, _normalize_frame
from .strategy_config import ValidatedStrategyConfig
from .strategy_operators import (
    HysteresisDecision,
    adjacent_curve_pct_slope,
    all_in_quantity,
    cms_cost_breakdown,
    prior_log_ols,
    recursive_log_ema,
)


class ReplayError(ValueError):
    """Raised when a validated strategy cannot produce a reconciled replay."""


EVENT_COLUMNS = [
    "Date",
    "side",
    "price",
    "quantity",
    "notional_cny",
    "commission_cny",
    "transfer_fee_cny",
    "stamp_tax_cny",
    "slippage_cny",
    "total_cost_cny",
    "cash_before_cny",
    "cash_after_cny",
    "holdings_before",
    "holdings_after",
    "reason",
]
TRADE_COLUMNS = [
    "entry_date",
    "entry_price",
    "quantity",
    "entry_cost_cny",
    "exit_date",
    "exit_price",
    "exit_cost_cny",
    "status",
    "gross_pnl_cny",
    "net_pnl_cny",
    "return",
]
COST_FIELDS = [
    "commission_cny",
    "transfer_fee_cny",
    "stamp_tax_cny",
    "slippage_cny",
    "total_cost_cny",
]


@dataclass(frozen=True)
class ReplayResult:
    daily: pd.DataFrame
    events: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, Any]
    cost_breakdown: dict[str, float]
    reconciliation: dict[str, bool]


class _Account:
    def __init__(
        self,
        capital: float,
        sizing: Mapping[str, Any],
        costs: Mapping[str, float],
    ):
        self.cash = float(capital)
        self.holdings = 0
        self.sizing = sizing
        self.costs = costs

    def buy(
        self, date: pd.Timestamp, raw_price: float, reason: str
    ) -> dict[str, Any] | None:
        if self.holdings:
            return None
        quantity = all_in_quantity(
            cash=self.cash,
            raw_price=raw_price,
            lot_size=self.sizing["lot_size"],
            target_fraction=self.sizing["target_fraction"],
            cost_parameters=self.costs,
        )
        if quantity == 0:
            return None
        return self._event(date, "BUY", raw_price, quantity, reason)

    def sell(
        self, date: pd.Timestamp, raw_price: float, reason: str
    ) -> dict[str, Any] | None:
        if not self.holdings:
            return None
        return self._event(date, "SELL", raw_price, self.holdings, reason)

    def _event(
        self,
        date: pd.Timestamp,
        side: str,
        raw_price: float,
        quantity: int,
        reason: str,
    ) -> dict[str, Any]:
        cash_before = self.cash
        holdings_before = self.holdings
        notional = raw_price * quantity
        costs = cms_cost_breakdown(
            side=side,
            raw_price=raw_price,
            quantity=quantity,
            **self.costs,
        )
        if side == "BUY":
            self.cash -= notional + costs["total_cost_cny"]
            self.holdings += quantity
        else:
            self.cash += notional - costs["total_cost_cny"]
            self.holdings -= quantity
        if self.cash < -1e-8:
            raise ReplayError("account cash became negative after an event")
        if abs(self.cash) < 1e-10:
            self.cash = 0.0
        return {
            "Date": date,
            "side": side,
            "price": float(raw_price),
            "quantity": quantity,
            "notional_cny": float(notional),
            **costs,
            "cash_before_cny": cash_before,
            "cash_after_cny": self.cash,
            "holdings_before": holdings_before,
            "holdings_after": self.holdings,
            "reason": reason,
        }

    def equity(self, mark_price: float) -> float:
        return self.cash + self.holdings * mark_price


def _zero_cost_parameters(costs: Mapping[str, float]) -> dict[str, float]:
    return {name: 0.0 for name in costs}


def _trade_ledger(events: pd.DataFrame, endpoint: pd.Series) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    for event in events.to_dict("records"):
        if event["side"] == "BUY":
            if active is not None:
                raise ReplayError("trade ledger encountered overlapping buys")
            active = event
            continue
        if active is None:
            raise ReplayError("trade ledger encountered a sell without an entry")
        gross = (
            event["price"] - active["price"]
        ) * active["quantity"]
        net = gross - active["total_cost_cny"] - event["total_cost_cny"]
        basis = active["price"] * active["quantity"] + active["total_cost_cny"]
        rows.append(
            {
                "entry_date": active["Date"],
                "entry_price": active["price"],
                "quantity": active["quantity"],
                "entry_cost_cny": active["total_cost_cny"],
                "exit_date": event["Date"],
                "exit_price": event["price"],
                "exit_cost_cny": event["total_cost_cny"],
                "status": "CLOSED",
                "gross_pnl_cny": gross,
                "net_pnl_cny": net,
                "return": net / basis,
            }
        )
        active = None
    if active is not None:
        mark_price = float(endpoint["Close"])
        gross = (mark_price - active["price"]) * active["quantity"]
        net = gross - active["total_cost_cny"]
        basis = active["price"] * active["quantity"] + active["total_cost_cny"]
        rows.append(
            {
                "entry_date": active["Date"],
                "entry_price": active["price"],
                "quantity": active["quantity"],
                "entry_cost_cny": active["total_cost_cny"],
                "exit_date": pd.NaT,
                "exit_price": np.nan,
                "exit_cost_cny": 0.0,
                "status": "OPEN",
                "gross_pnl_cny": gross,
                "net_pnl_cny": net,
                "return": net / basis,
            }
        )
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def _reconcile(
    daily: pd.DataFrame,
    events: pd.DataFrame,
    trades: pd.DataFrame,
    initial_capital: float,
) -> dict[str, bool]:
    checks = {
        "daily_equity": bool(
            np.allclose(
                daily["cash"] + daily["market_value"],
                daily["equity"],
                rtol=0,
                atol=1e-7,
            )
        ),
        "event_cash": True,
        "event_positions": True,
        "event_costs": bool(
            np.isclose(
                events["total_cost_cny"].sum(),
                daily["total_cost_cny"].sum(),
                rtol=0,
                atol=1e-7,
            )
        ),
        "trade_events": bool(
            np.isclose(
                trades["entry_cost_cny"].sum() + trades["exit_cost_cny"].sum(),
                events["total_cost_cny"].sum(),
                rtol=0,
                atol=1e-7,
            )
        ),
        "profit_identity": bool(
            np.isclose(
                daily.iloc[-1]["equity"] - initial_capital,
                daily.iloc[-1]["net_pnl"],
                rtol=0,
                atol=1e-7,
            )
        ),
    }
    expected_cash = initial_capital
    expected_holdings = 0
    for event in events.to_dict("records"):
        if not np.isclose(event["cash_before_cny"], expected_cash, rtol=0, atol=1e-7):
            checks["event_cash"] = False
        if event["holdings_before"] != expected_holdings:
            checks["event_positions"] = False
        if event["side"] == "BUY":
            expected_cash -= event["notional_cny"] + event["total_cost_cny"]
            expected_holdings += event["quantity"]
        else:
            expected_cash += event["notional_cny"] - event["total_cost_cny"]
            expected_holdings -= event["quantity"]
        if not np.isclose(event["cash_after_cny"], expected_cash, rtol=0, atol=1e-7):
            checks["event_cash"] = False
        if event["holdings_after"] != expected_holdings:
            checks["event_positions"] = False
    if not all(checks.values()):
        failures = sorted(name for name, passed in checks.items() if not passed)
        raise ReplayError(f"replay reconciliation failed: {failures}")
    return checks


def replay_strategy(
    frame: pd.DataFrame, config: ValidatedStrategyConfig
) -> ReplayResult:
    try:
        normalized = _normalize_frame(frame)
    except DatasetValidationError as exc:
        raise ReplayError(f"invalid replay dataset: {exc}") from exc
    canonical = config.canonical
    template = canonical["template"]["parameters"]
    operator_parameters = {
        slot: value["parameters"]
        for slot, value in canonical["operators"].items()
    }
    signal_column = operator_parameters["fit"]["price_column"]
    if signal_column not in normalized.columns:
        raise ReplayError(
            f"configured signal column {signal_column} is absent from the snapshot"
        )

    indexed = normalized.set_index("Date")
    fit = prior_log_ols(
        indexed[signal_column],
        window_sessions=operator_parameters["fit"]["window_sessions"],
    )
    smoothed = recursive_log_ema(
        fit["curve"],
        span_sessions=operator_parameters["smoothing"]["span_sessions"],
    )
    statistic = adjacent_curve_pct_slope(smoothed)
    start = pd.Timestamp(template["evaluation_start"])
    end = (
        pd.Timestamp(template["evaluation_end"])
        if template["evaluation_end"] is not None
        else indexed.index.max()
    )
    evaluation = indexed.loc[
        (indexed.index >= start) & (indexed.index <= end)
    ].copy()
    if evaluation.empty:
        raise ReplayError("evaluation interval contains no dataset sessions")
    if fit.loc[evaluation.index, "curve"].isna().any():
        raise ReplayError("insufficient prior history for the evaluation interval")

    capital = float(template["initial_capital_cny"])
    sizing = operator_parameters["sizing"]
    costs = operator_parameters["cost"]
    account = _Account(capital, sizing, costs)
    zero_account = _Account(capital, sizing, _zero_cost_parameters(costs))
    buy_hold_account = _Account(capital, sizing, costs)
    buy_hold_account.buy(
        evaluation.index[0],
        float(evaluation.iloc[0]["Open"]),
        "BUY_AND_HOLD_ENTRY",
    )
    decision = HysteresisDecision(**operator_parameters["decision"])
    event_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    cumulative_costs = 0.0

    for position, (session, bar) in enumerate(evaluation.iterrows()):
        raw_open = float(bar["Open"])
        raw_close = float(bar["Close"])
        position_before = int(account.holdings > 0)
        decision_result = decision.step(float(statistic.loc[session]))
        action = decision_result.action
        reason = decision_result.reason
        day_events: list[dict[str, Any]] = []
        if action == "BUY":
            event = account.buy(session, raw_open, reason)
            zero_account.buy(session, raw_open, reason)
            if event is None:
                reason = "INSUFFICIENT_CASH"
            else:
                day_events.append(event)
        elif action == "SELL":
            event = account.sell(session, raw_open, reason)
            zero_account.sell(session, raw_open, reason)
            if event is None:
                reason = "SELL_SIGNAL_WHILE_NO_HOLDINGS"
            else:
                day_events.append(event)

        is_terminal = position == len(evaluation) - 1
        if is_terminal and template["terminal_handling"] == "force_liquidate":
            terminal = account.sell(
                session, raw_open, "TERMINAL_FORCED_LIQUIDATION"
            )
            zero_account.sell(
                session, raw_open, "TERMINAL_FORCED_LIQUIDATION"
            )
            if terminal is not None:
                day_events.append(terminal)
                action = "SELL"
                reason = "TERMINAL_FORCED_LIQUIDATION"

        event_rows.extend(day_events)
        day_costs = {
            name: float(sum(event[name] for event in day_events))
            for name in COST_FIELDS
        }
        cumulative_costs += day_costs["total_cost_cny"]
        equity = account.equity(raw_close)
        market_value = account.holdings * raw_close
        net_pnl = equity - capital
        gross_pnl = net_pnl + cumulative_costs
        daily_rows.append(
            {
                "Date": session,
                "history_start": fit.loc[session, "history_start"],
                "history_end": fit.loc[session, "history_end"],
                "curve": float(fit.loc[session, "curve"]),
                "smoothed_curve": float(smoothed.loc[session]),
                "statistic": float(statistic.loc[session]),
                "previous_statistic": decision_result.previous_statistic,
                "decision": action,
                "reason": reason,
                "position_before": position_before,
                "position_after": int(account.holdings > 0),
                "price": raw_open,
                "quantity": sum(
                    event["quantity"] * (1 if event["side"] == "BUY" else -1)
                    for event in day_events
                ),
                "cash": account.cash,
                "holdings": account.holdings,
                "market_value": market_value,
                "equity": equity,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                **day_costs,
                "zero_cost_equity": zero_account.equity(raw_close),
                "buy_hold_equity": buy_hold_account.equity(raw_close),
            }
        )

    daily = pd.DataFrame(daily_rows)
    events = pd.DataFrame(event_rows, columns=EVENT_COLUMNS)
    trades = _trade_ledger(events, evaluation.iloc[-1])
    cost_breakdown = {
        name: float(events[name].sum()) for name in COST_FIELDS
    }
    equity = daily["equity"]
    drawdown = equity / equity.cummax() - 1.0
    closed = trades[trades["status"] == "CLOSED"]
    metrics: dict[str, Any] = {
        "period_start": str(daily["Date"].iloc[0].date()),
        "period_end": str(daily["Date"].iloc[-1].date()),
        "initial_capital_cny": capital,
        "final_equity_cny": float(equity.iloc[-1]),
        "net_profit_cny": float(equity.iloc[-1] - capital),
        "net_return": float(equity.iloc[-1] / capital - 1.0),
        "zero_cost_final_equity_cny": float(daily["zero_cost_equity"].iloc[-1]),
        "zero_cost_return": float(daily["zero_cost_equity"].iloc[-1] / capital - 1.0),
        "buy_hold_final_equity_cny": float(daily["buy_hold_equity"].iloc[-1]),
        "buy_hold_return": float(daily["buy_hold_equity"].iloc[-1] / capital - 1.0),
        "max_drawdown": float(drawdown.min()),
        "closed_trades": int(len(closed)),
        "open_trades": int((trades["status"] == "OPEN").sum()),
        "closed_trade_win_rate": (
            float((closed["net_pnl_cny"] > 0).mean()) if len(closed) else None
        ),
        "current_position": "LONG" if account.holdings else "FLAT",
        "price_return_only": True,
    }
    reconciliation = _reconcile(
        daily, events, trades, capital
    )
    return ReplayResult(
        daily=daily,
        events=events,
        trades=trades,
        metrics=metrics,
        cost_breakdown=cost_breakdown,
        reconciliation=reconciliation,
    )
