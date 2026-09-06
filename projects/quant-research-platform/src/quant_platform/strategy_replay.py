from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .corporate_actions import (
    CashDividend,
    CorporateActionEvidence,
    CorporateActionEvidenceError,
    SettlementSchedule,
    accounting_cash_dividends,
    cny_to_fen,
    dividend_tax_burden,
    fen_to_cny,
    tax_policy_identity,
)
from .datasets import DatasetValidationError, _normalize_frame
from .strategy_config import ValidatedStrategyConfig
from .strategy_operators import (
    DecisionResult,
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
    account_events: pd.DataFrame
    account_trades: pd.DataFrame
    account_final_states: dict[str, dict[str, int]]


class _Account:
    def __init__(
        self,
        capital: float,
        sizing: Mapping[str, Any],
        costs: Mapping[str, float],
        implementations: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], Any]]
        | None = None,
        implementation_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self.cash = float(capital)
        self.holdings = 0
        self.sizing = sizing
        self.costs = costs
        self.implementations = implementations or {}
        self.implementation_parameters = implementation_parameters or {}

    def buy(self, date: pd.Timestamp, raw_price: float, reason: str) -> dict[str, Any] | None:
        if self.holdings:
            return None
        if "sizing" in self.implementations:
            quantity = self.implementations["sizing"](
                {
                    "cash": self.cash,
                    "raw_price": raw_price,
                    "holdings": self.holdings,
                    "side": "BUY",
                },
                dict(self.implementation_parameters["sizing"]),
            )
        else:
            if "cost" in self.implementations:
                budget = self.cash * self.sizing["target_fraction"]
                lot_size = self.sizing["lot_size"]
                quantity = int(budget // (raw_price * lot_size)) * lot_size
                while quantity > 0:
                    custom_costs = self.implementations["cost"](
                        {
                            "side": "BUY",
                            "raw_price": raw_price,
                            "quantity": quantity,
                        },
                        dict(self.implementation_parameters["cost"]),
                    )
                    if raw_price * quantity + custom_costs["total_cost_cny"] <= budget:
                        break
                    quantity -= lot_size
            else:
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

    def sell(self, date: pd.Timestamp, raw_price: float, reason: str) -> dict[str, Any] | None:
        if not self.holdings:
            return None
        quantity = self.holdings
        if "sizing" in self.implementations:
            quantity = self.implementations["sizing"](
                {
                    "cash": self.cash,
                    "raw_price": raw_price,
                    "holdings": self.holdings,
                    "side": "SELL",
                },
                dict(self.implementation_parameters["sizing"]),
            )
            if quantity != self.holdings:
                raise ReplayError("custom sizing must sell all held shares")
        return self._event(date, "SELL", raw_price, quantity, reason)

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
        if "cost" in self.implementations:
            costs = self.implementations["cost"](
                {"side": side, "raw_price": raw_price, "quantity": quantity},
                dict(self.implementation_parameters["cost"]),
            )
        else:
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


def _allocate_fen(total_fen: int, quantity: int, remaining_quantity: int) -> int:
    if quantity <= 0 or quantity > remaining_quantity:
        raise ReplayError("invalid exact-fen allocation quantity")
    if quantity == remaining_quantity:
        return total_fen
    return int(
        (Decimal(total_fen) * Decimal(quantity) / Decimal(remaining_quantity)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


class _SettlementAccount:
    """Per-account FIFO settlement ledger behind the replay account interface."""

    def __init__(
        self,
        account_id: str,
        capital: float,
        sizing: Mapping[str, Any],
        costs: Mapping[str, float],
        schedule: SettlementSchedule,
        implementations: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], Any]]
        | None = None,
        implementation_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self.account_id = account_id
        self.initial_capital_fen = cny_to_fen(Decimal(str(capital)))
        self.cash_fen = self.initial_capital_fen
        self.holdings = 0
        self.settled_holdings = 0
        self.receivable_fen = 0
        self.unpaid_dividend_tax_base_fen = 0
        self.deferred_tax_base_fen = 0
        self.outstanding_tax_fen = 0
        self.sizing = sizing
        self.costs = costs
        self.schedule = schedule
        self.implementations = implementations or {}
        self.implementation_parameters = implementation_parameters or {}
        self.pending: list[dict[str, Any]] = []
        self.pending_collections: list[dict[str, Any]] = []
        self.lots: list[dict[str, Any]] = []
        self.entitlements: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.closed_trades: list[dict[str, Any]] = []
        self._sequence = 0
        self._trade_sequence = 0
        self._last_mark_fen = 0

    @property
    def cash(self) -> float:
        return fen_to_cny(self.cash_fen)

    def _state(self) -> dict[str, int]:
        market_value = self.holdings * self._last_mark_fen
        equity = self.cash_fen + market_value + self.receivable_fen - self.outstanding_tax_fen
        return {
            "cash_fen": self.cash_fen,
            "trade_holdings": self.holdings,
            "settled_holdings": self.settled_holdings,
            "receivable_fen": self.receivable_fen,
            "unpaid_dividend_tax_base_fen": self.unpaid_dividend_tax_base_fen,
            "deferred_tax_base_fen": self.deferred_tax_base_fen,
            "outstanding_tax_fen": self.outstanding_tax_fen,
            "market_price_fen": self._last_mark_fen,
            "market_value_fen": market_value,
            "equity_fen": equity,
        }

    def _post(
        self,
        event_date: date,
        event_type: str,
        *,
        trade_id: str | None = None,
        lot_id: str | None = None,
        event_revision_id: str | None = None,
        quantity: int = 0,
        trade_quantity_delta: int = 0,
        settled_quantity_delta: int = 0,
        cash_delta_fen: int = 0,
        cost_fen: int = 0,
        note: str | None = None,
    ) -> None:
        state = self._state()
        non_negative_fields = (
            "cash_fen",
            "trade_holdings",
            "settled_holdings",
            "receivable_fen",
            "unpaid_dividend_tax_base_fen",
            "deferred_tax_base_fen",
            "outstanding_tax_fen",
            "market_price_fen",
            "market_value_fen",
        )
        if any(state[field] < 0 for field in non_negative_fields):
            raise ReplayError("account event contains a negative settlement state")
        active_dividend_base = sum(
            sum(lot["dividends"].values()) for lot in self.lots if lot["remaining_quantity"] > 0
        )
        if self.unpaid_dividend_tax_base_fen + self.deferred_tax_base_fen != active_dividend_base:
            raise ReplayError("account event dividend tax-base identity failed")
        self._sequence += 1
        self.events.append(
            {
                "account": self.account_id,
                "Date": pd.Timestamp(event_date),
                "sequence": self._sequence,
                "event_type": event_type,
                "trade_id": trade_id,
                "lot_id": lot_id,
                "event_revision_id": event_revision_id,
                "quantity": quantity,
                "trade_quantity_delta": trade_quantity_delta,
                "settled_quantity_delta": settled_quantity_delta,
                "cash_delta_fen": cash_delta_fen,
                "cost_fen": cost_fen,
                "note": note,
                **state,
            }
        )

    def _cost_fen(self, side: str, raw_price: float, quantity: int) -> dict[str, int]:
        if "cost" in self.implementations:
            raw = self.implementations["cost"](
                {"side": side, "raw_price": raw_price, "quantity": quantity},
                dict(self.implementation_parameters["cost"]),
            )
            fields = {name: cny_to_fen(Decimal(str(raw[name]))) for name in COST_FIELDS[:-1]}
        else:
            price = Decimal(str(raw_price))
            notional = price * quantity
            fields = {
                "commission_cny": max(
                    cny_to_fen(notional * Decimal(str(self.costs["commission_rate"]))),
                    cny_to_fen(Decimal(str(self.costs["minimum_commission_cny"]))),
                ),
                "transfer_fee_cny": cny_to_fen(
                    notional * Decimal(str(self.costs["transfer_fee_rate"]))
                ),
                "stamp_tax_cny": (
                    cny_to_fen(notional * Decimal(str(self.costs["sell_stamp_tax_rate"])))
                    if side == "SELL"
                    else 0
                ),
                "slippage_cny": cny_to_fen(
                    notional
                    * Decimal(
                        str(
                            self.costs["buy_slippage_bps" if side == "BUY" else "sell_slippage_bps"]
                        )
                    )
                    / Decimal("10000")
                ),
            }
        fields["total_cost_cny"] = sum(fields.values())
        return fields

    def _affordable_quantity(self, raw_price: float) -> int:
        if "sizing" in self.implementations:
            value = self.implementations["sizing"](
                {
                    "cash": self.cash,
                    "raw_price": raw_price,
                    "holdings": self.holdings,
                    "side": "BUY",
                },
                dict(self.implementation_parameters["sizing"]),
            )
            if type(value) is not int or value < 0:
                raise ReplayError("custom sizing returned an invalid quantity")
            return value
        lot_size = self.sizing["lot_size"]
        budget_fen = int(
            (Decimal(self.cash_fen) * Decimal(str(self.sizing["target_fraction"]))).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        lot_notional_fen = cny_to_fen(Decimal(str(raw_price)) * lot_size)
        quantity = (budget_fen // lot_notional_fen) * lot_size
        while quantity > 0:
            notional_fen = cny_to_fen(Decimal(str(raw_price)) * quantity)
            if (
                notional_fen + self._cost_fen("BUY", raw_price, quantity)["total_cost_cny"]
                <= budget_fen
            ):
                return quantity
            quantity -= lot_size
        return 0

    def _trade(
        self, event_date: date, side: str, raw_price: float, quantity: int, reason: str
    ) -> dict[str, Any]:
        try:
            settlement_date = self.schedule.settlement_date(event_date)
        except CorporateActionEvidenceError as exc:
            raise ReplayError(f"settlement input is invalid: {exc}") from exc
        price_fen = cny_to_fen(Decimal(str(raw_price)))
        notional_fen = cny_to_fen(Decimal(str(raw_price)) * quantity)
        costs_fen = self._cost_fen(side, raw_price, quantity)
        total_cost_fen = costs_fen["total_cost_cny"]
        cash_before_fen = self.cash_fen
        holdings_before = self.holdings
        self._trade_sequence += 1
        trade_id = f"{self.account_id}-trade-{self._trade_sequence:06d}"
        direction = 1 if side == "BUY" else -1
        notional_cash_delta = -notional_fen if side == "BUY" else notional_fen
        self.cash_fen += notional_cash_delta
        self.holdings += direction * quantity
        self._last_mark_fen = price_fen
        self._post(
            event_date,
            f"TRADE_{side}",
            trade_id=trade_id,
            quantity=quantity,
            trade_quantity_delta=direction * quantity,
            cash_delta_fen=notional_cash_delta,
            note=reason,
        )
        self.cash_fen -= total_cost_fen
        self._post(
            event_date,
            "TRADE_COST",
            trade_id=trade_id,
            quantity=quantity,
            cash_delta_fen=-total_cost_fen,
            cost_fen=total_cost_fen,
            note=side,
        )
        if self.cash_fen < 0:
            raise ReplayError("account cash became negative after an exact-fen event")
        self.pending.append(
            {
                "trade_id": trade_id,
                "side": side,
                "trade_date": event_date,
                "settlement_date": settlement_date,
                "quantity": quantity,
                "price_fen": price_fen,
                "notional_fen": notional_fen,
                "cost_fen": total_cost_fen,
            }
        )
        return {
            "Date": pd.Timestamp(event_date),
            "side": side,
            "price": float(raw_price),
            "quantity": quantity,
            "notional_cny": fen_to_cny(notional_fen),
            **{name: fen_to_cny(value) for name, value in costs_fen.items()},
            "cash_before_cny": fen_to_cny(cash_before_fen),
            "cash_after_cny": self.cash,
            "holdings_before": holdings_before,
            "holdings_after": self.holdings,
            "reason": reason,
        }

    def buy(self, event_date: pd.Timestamp, raw_price: float, reason: str) -> dict[str, Any] | None:
        if self.holdings:
            return None
        quantity = self._affordable_quantity(raw_price)
        if quantity == 0:
            return None
        return self._trade(event_date.date(), "BUY", raw_price, quantity, reason)

    def sell(
        self, event_date: pd.Timestamp, raw_price: float, reason: str
    ) -> dict[str, Any] | None:
        if not self.holdings:
            return None
        quantity = self.holdings
        if "sizing" in self.implementations:
            quantity = self.implementations["sizing"](
                {
                    "cash": self.cash,
                    "raw_price": raw_price,
                    "holdings": self.holdings,
                    "side": "SELL",
                },
                dict(self.implementation_parameters["sizing"]),
            )
            if quantity != self.holdings:
                raise ReplayError("custom sizing must sell all held shares")
        return self._trade(event_date.date(), "SELL", raw_price, quantity, reason)

    def _settle_buy(self, pending: dict[str, Any], event_date: date) -> None:
        lot_id = f"{self.account_id}-lot-{len(self.lots) + 1:06d}"
        self.lots.append(
            {
                "lot_id": lot_id,
                "trade_id": pending["trade_id"],
                "trade_date": pending["trade_date"],
                "acquisition_date": event_date,
                "quantity": pending["quantity"],
                "remaining_quantity": pending["quantity"],
                "remaining_notional_fen": pending["notional_fen"],
                "remaining_cost_fen": pending["cost_fen"],
                "dividends": {},
            }
        )
        self.settled_holdings += pending["quantity"]
        self._post(
            event_date,
            "ACQUISITION_SETTLEMENT",
            trade_id=pending["trade_id"],
            lot_id=lot_id,
            quantity=pending["quantity"],
            settled_quantity_delta=pending["quantity"],
        )

    def _settle_sell(self, pending: dict[str, Any], event_date: date) -> None:
        remaining = pending["quantity"]
        sale_notional_remaining = pending["notional_fen"]
        sale_cost_remaining = pending["cost_fen"]
        chunks: list[dict[str, Any]] = []
        for lot in self.lots:
            if remaining == 0:
                break
            available = lot["remaining_quantity"]
            if available == 0:
                continue
            quantity = min(remaining, available)
            basis_notional = _allocate_fen(lot["remaining_notional_fen"], quantity, available)
            basis_cost = _allocate_fen(lot["remaining_cost_fen"], quantity, available)
            sale_notional = _allocate_fen(sale_notional_remaining, quantity, remaining)
            sale_cost = _allocate_fen(sale_cost_remaining, quantity, remaining)
            dividend_fen = 0
            for revision_id, value in list(lot["dividends"].items()):
                allocation = _allocate_fen(value, quantity, available)
                lot["dividends"][revision_id] -= allocation
                dividend_fen += allocation
                entitlement = self.entitlements.get(revision_id)
                if entitlement is None:
                    raise ReplayError("FIFO lot dividend has no entitlement state")
                if entitlement["paid"]:
                    self.deferred_tax_base_fen -= allocation
                else:
                    self.unpaid_dividend_tax_base_fen -= allocation
                    entitlement["disposed_before_payment_fen"] += allocation
            burden = dividend_tax_burden(lot["acquisition_date"], event_date)
            chunks.append(
                {
                    "account": self.account_id,
                    "lot_id": lot["lot_id"],
                    "entry_trade_id": lot["trade_id"],
                    "exit_trade_id": pending["trade_id"],
                    "entry_trade_date": pd.Timestamp(lot["trade_date"]),
                    "entry_settlement_date": pd.Timestamp(lot["acquisition_date"]),
                    "exit_trade_date": pd.Timestamp(pending["trade_date"]),
                    "exit_settlement_date": pd.Timestamp(event_date),
                    "quantity": quantity,
                    "entry_notional_fen": basis_notional,
                    "entry_cost_fen": basis_cost,
                    "exit_notional_fen": sale_notional,
                    "exit_cost_fen": sale_cost,
                    "dividend_fen": dividend_fen,
                    "tax_weight": Decimal(dividend_fen) * burden,
                    "tax_burden": format(burden, ".2f"),
                    "status": "CLOSED",
                }
            )
            lot["remaining_quantity"] -= quantity
            lot["remaining_notional_fen"] -= basis_notional
            lot["remaining_cost_fen"] -= basis_cost
            remaining -= quantity
            sale_notional_remaining -= sale_notional
            sale_cost_remaining -= sale_cost
            self.settled_holdings -= quantity
            self._post(
                event_date,
                "DISPOSAL_SETTLEMENT",
                trade_id=pending["trade_id"],
                lot_id=lot["lot_id"],
                quantity=quantity,
                settled_quantity_delta=-quantity,
            )
        if remaining:
            raise ReplayError("disposal settlement exceeds FIFO settled holdings")
        tax_fen = int(
            sum((chunk["tax_weight"] for chunk in chunks), Decimal("0")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        tax_remaining = tax_fen
        weight_remaining = sum((chunk["tax_weight"] for chunk in chunks), Decimal("0"))
        for index, chunk in enumerate(chunks):
            chunk_weight = chunk["tax_weight"]
            if index == len(chunks) - 1 or weight_remaining == 0:
                allocation = tax_remaining
            else:
                allocation = int(
                    (Decimal(tax_remaining) * chunk_weight / weight_remaining).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                allocation = min(allocation, tax_remaining)
            chunk["tax_fen"] = allocation
            chunk["net_pnl_fen"] = (
                chunk["exit_notional_fen"]
                - chunk["exit_cost_fen"]
                - chunk["entry_notional_fen"]
                - chunk["entry_cost_fen"]
                + chunk["dividend_fen"]
                - allocation
            )
            chunk.pop("tax_weight")
            self.closed_trades.append(chunk)
            tax_remaining -= allocation
            weight_remaining -= chunk_weight
        if tax_fen:
            collection_date = self.schedule.collection_date(event_date)
            self.outstanding_tax_fen += tax_fen
            self.pending_collections.append(
                {
                    "settlement_date": event_date,
                    "collection_date": collection_date,
                    "tax_fen": tax_fen,
                    "trade_id": pending["trade_id"],
                }
            )
            self._post(
                event_date,
                "TAX_LIABILITY",
                trade_id=pending["trade_id"],
                cash_delta_fen=0,
                cost_fen=tax_fen,
                note="DEFERRED_UNTIL_NEXT_TRADING_DAY_COLLECTION",
            )

    def _record_entitlement(self, action: CashDividend, event_date: date) -> None:
        entitled = [lot for lot in self.lots if lot["remaining_quantity"] > 0]
        quantity = sum(lot["remaining_quantity"] for lot in entitled)
        if quantity == 0:
            return
        gross_fen = cny_to_fen(action.gross_cash_per_share * quantity)
        remaining_fen = gross_fen
        remaining_quantity = quantity
        allocations: list[dict[str, Any]] = []
        for lot in entitled:
            lot_quantity = lot["remaining_quantity"]
            allocation = _allocate_fen(remaining_fen, lot_quantity, remaining_quantity)
            lot["dividends"][action.event_revision_id] = allocation
            allocations.append(
                {"lot_id": lot["lot_id"], "quantity": lot_quantity, "gross_fen": allocation}
            )
            remaining_fen -= allocation
            remaining_quantity -= lot_quantity
        self.receivable_fen += gross_fen
        self.unpaid_dividend_tax_base_fen += gross_fen
        self.entitlements[action.event_revision_id] = {
            "action": action,
            "gross_fen": gross_fen,
            "quantity": quantity,
            "allocations": allocations,
            "paid": False,
            "disposed_before_payment_fen": 0,
        }
        self._post(
            event_date,
            "DIVIDEND_ENTITLEMENT",
            event_revision_id=action.event_revision_id,
            quantity=quantity,
            note="RECORD_CLOSE_SETTLED_HOLDINGS",
        )

    def _pay_dividend(self, action: CashDividend, event_date: date) -> None:
        entitlement = self.entitlements.get(action.event_revision_id)
        if entitlement is None:
            return
        if entitlement["paid"]:
            raise ReplayError("dividend entitlement was paid twice")
        gross_fen = entitlement["gross_fen"]
        held_tax_base_fen = gross_fen - entitlement["disposed_before_payment_fen"]
        self.receivable_fen -= gross_fen
        self.unpaid_dividend_tax_base_fen -= held_tax_base_fen
        self.deferred_tax_base_fen += held_tax_base_fen
        self.cash_fen += gross_fen
        entitlement["paid"] = True
        self._post(
            event_date,
            "DIVIDEND_PAYMENT",
            event_revision_id=action.event_revision_id,
            quantity=entitlement["quantity"],
            cash_delta_fen=gross_fen,
            note="NO_IMMEDIATE_WITHHOLDING",
        )

    def _collect_tax(self, collection: dict[str, Any], event_date: date) -> None:
        tax_fen = collection["tax_fen"]
        if self.cash_fen >= tax_fen:
            self.cash_fen -= tax_fen
            self.outstanding_tax_fen -= tax_fen
            event_type = "TAX_COLLECTION"
            cash_delta = -tax_fen
            note = "COLLECTED_IN_FULL"
        else:
            event_type = "TAX_COLLECTION_OUTSTANDING"
            cash_delta = 0
            note = "INSUFFICIENT_FUNDS_NOT_REPRESENTED_AS_PAID"
        self._post(
            event_date,
            event_type,
            trade_id=collection["trade_id"],
            cash_delta_fen=cash_delta,
            cost_fen=tax_fen,
            note=note,
        )

    def process_day(
        self, event_date: date, actions: tuple[CashDividend, ...], mark_price: float | None
    ) -> None:
        if mark_price is not None:
            self._last_mark_fen = cny_to_fen(Decimal(str(mark_price)))
        settling = [item for item in self.pending if item["settlement_date"] == event_date]
        self.pending = [item for item in self.pending if item["settlement_date"] != event_date]
        for pending in settling:
            if pending["side"] == "BUY":
                self._settle_buy(pending, event_date)
            else:
                self._settle_sell(pending, event_date)
        for action in actions:
            if action.record_date == event_date:
                self._record_entitlement(action, event_date)
        for action in actions:
            if action.pay_date == event_date:
                self._pay_dividend(action, event_date)
        collecting = [
            item for item in self.pending_collections if item["collection_date"] == event_date
        ]
        self.pending_collections = [
            item for item in self.pending_collections if item["collection_date"] != event_date
        ]
        for collection in collecting:
            self._collect_tax(collection, event_date)
        if mark_price is not None:
            self._post(event_date, "ACCOUNT_MARK")

    def pending_dates(self, actions: tuple[CashDividend, ...], after: date) -> set[date]:
        dates = {
            item["settlement_date"] for item in self.pending if item["settlement_date"] > after
        }
        dates.update(
            item["collection_date"]
            for item in self.pending_collections
            if item["collection_date"] > after
        )
        dates.update(action.record_date for action in actions if action.record_date > after)
        dates.update(action.pay_date for action in actions if action.pay_date > after)
        return dates

    def equity(self, mark_price: float) -> float:
        self._last_mark_fen = cny_to_fen(Decimal(str(mark_price)))
        return fen_to_cny(self._state()["equity_fen"])

    def trade_ledger(self, endpoint: pd.Series) -> pd.DataFrame:
        rows = [dict(row) for row in self.closed_trades]
        mark_fen = cny_to_fen(Decimal(str(endpoint["Close"])))
        for lot in self.lots:
            quantity = lot["remaining_quantity"]
            if quantity == 0:
                continue
            dividend_fen = sum(lot["dividends"].values())
            net_pnl = (
                mark_fen * quantity
                - lot["remaining_notional_fen"]
                - lot["remaining_cost_fen"]
                + dividend_fen
            )
            rows.append(
                {
                    "account": self.account_id,
                    "lot_id": lot["lot_id"],
                    "entry_trade_id": lot["trade_id"],
                    "exit_trade_id": None,
                    "entry_trade_date": pd.Timestamp(lot["trade_date"]),
                    "entry_settlement_date": pd.Timestamp(lot["acquisition_date"]),
                    "exit_trade_date": pd.NaT,
                    "exit_settlement_date": pd.NaT,
                    "quantity": quantity,
                    "entry_notional_fen": lot["remaining_notional_fen"],
                    "entry_cost_fen": lot["remaining_cost_fen"],
                    "exit_notional_fen": mark_fen * quantity,
                    "exit_cost_fen": 0,
                    "dividend_fen": dividend_fen,
                    "tax_burden": None,
                    "tax_fen": 0,
                    "net_pnl_fen": net_pnl,
                    "status": "OPEN",
                }
            )
        return pd.DataFrame(rows)


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
        gross = (event["price"] - active["price"]) * active["quantity"]
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
        "trade_net_pnl": bool(
            np.isclose(
                trades["net_pnl_cny"].sum(),
                daily.iloc[-1]["equity"] - initial_capital,
                rtol=0,
                atol=1e-8,
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


def _reconcile_settlement(
    accounts: tuple[_SettlementAccount, ...],
    account_events: pd.DataFrame,
    account_trades: pd.DataFrame,
) -> dict[str, bool]:
    checks = {
        "daily_equity": True,
        "event_cash": True,
        "event_positions": True,
        "event_costs": True,
        "trade_events": True,
        "profit_identity": True,
        "trade_net_pnl": True,
        "integer_fen": True,
        "settled_quantity": True,
        "account_isolation": True,
        "deferred_tax": True,
    }
    integer_columns = [
        "quantity",
        "trade_quantity_delta",
        "settled_quantity_delta",
        "cash_delta_fen",
        "cost_fen",
        "cash_fen",
        "trade_holdings",
        "settled_holdings",
        "receivable_fen",
        "unpaid_dividend_tax_base_fen",
        "deferred_tax_base_fen",
        "outstanding_tax_fen",
        "market_price_fen",
        "market_value_fen",
        "equity_fen",
    ]
    checks["integer_fen"] = all(
        pd.api.types.is_integer_dtype(account_events[column]) for column in integer_columns
    ) and all(
        pd.api.types.is_integer_dtype(account_trades[column])
        for column in [
            "quantity",
            "entry_notional_fen",
            "entry_cost_fen",
            "exit_notional_fen",
            "exit_cost_fen",
            "dividend_fen",
            "tax_fen",
            "net_pnl_fen",
        ]
    )
    for account in accounts:
        events = account_events[account_events["account"] == account.account_id]
        trades = account_trades[account_trades["account"] == account.account_id]
        checks["event_cash"] &= bool(
            account.initial_capital_fen + int(events["cash_delta_fen"].sum()) == account.cash_fen
        )
        checks["event_positions"] &= bool(
            int(events["trade_quantity_delta"].sum()) == account.holdings
        )
        checks["settled_quantity"] &= bool(
            int(events["settled_quantity_delta"].sum()) == account.settled_holdings
            and account.settled_holdings == sum(lot["remaining_quantity"] for lot in account.lots)
        )
        final_state = account._state()
        checks["daily_equity"] &= bool(
            final_state["equity_fen"]
            == account.cash_fen
            + final_state["market_value_fen"]
            + account.receivable_fen
            - account.outstanding_tax_fen
        )
        trade_costs = int(events.loc[events["event_type"] == "TRADE_COST", "cost_fen"].sum())
        attributed_costs = int(trades["entry_cost_fen"].sum()) + int(trades["exit_cost_fen"].sum())
        checks["event_costs"] &= trade_costs == attributed_costs
        checks["trade_events"] &= bool(
            set(trades["entry_trade_id"].dropna()).issubset(set(events["trade_id"].dropna()))
            and set(trades["exit_trade_id"].dropna()).issubset(set(events["trade_id"].dropna()))
        )
        expected_profit = final_state["equity_fen"] - account.initial_capital_fen
        attributed_profit = int(trades["net_pnl_fen"].sum())
        checks["profit_identity"] &= expected_profit == attributed_profit
        checks["trade_net_pnl"] &= expected_profit == attributed_profit
        tax_state_columns = [
            "receivable_fen",
            "unpaid_dividend_tax_base_fen",
            "deferred_tax_base_fen",
            "outstanding_tax_fen",
        ]
        latest = events.iloc[-1]
        checks["deferred_tax"] &= bool(
            all(
                all(int(value) >= 0 for value in events[column].tolist())
                for column in tax_state_columns
            )
            and (
                events["equity_fen"]
                == events["cash_fen"]
                + events["market_value_fen"]
                + events["receivable_fen"]
                - events["outstanding_tax_fen"]
            ).all()
            and all(latest[field] == final_state[field] for field in final_state)
        )
    checks["account_isolation"] = bool(
        set(account_events["account"]) == {"strategy", "zero_cost", "buy_and_hold"}
        and all(
            str(lot_id).startswith(f"{account}-lot-")
            for account, lot_id in account_trades[["account", "lot_id"]].itertuples(
                index=False, name=None
            )
        )
    )
    if not all(checks.values()):
        failures = sorted(name for name, passed in checks.items() if not passed)
        raise ReplayError(f"settlement reconciliation failed: {failures}")
    return checks


def replay_strategy(
    frame: pd.DataFrame,
    config: ValidatedStrategyConfig,
    *,
    implementations: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], Any]] | None = None,
    implementation_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    corporate_action_evidence: CorporateActionEvidence | None = None,
    settlement_schedule: SettlementSchedule | None = None,
) -> ReplayResult:
    try:
        normalized = _normalize_frame(frame)
    except DatasetValidationError as exc:
        raise ReplayError(f"invalid replay dataset: {exc}") from exc
    canonical = config.canonical
    template = canonical["template"]["parameters"]
    operator_parameters = {
        slot: value["parameters"] for slot, value in canonical["operators"].items()
    }
    implementations = implementations or {}
    implementation_parameters = implementation_parameters or {}
    signal_column = operator_parameters["fit"]["price_column"]
    if signal_column not in normalized.columns:
        raise ReplayError(f"configured signal column {signal_column} is absent from the snapshot")

    indexed = normalized.set_index("Date")
    if "fit" in implementations:
        fit = pd.DataFrame(
            {
                "history_start": pd.Series(pd.NaT, index=indexed.index, dtype="datetime64[ns]"),
                "history_end": pd.Series(pd.NaT, index=indexed.index, dtype="datetime64[ns]"),
                "curve": np.nan,
            },
            index=indexed.index,
        )
        for position in range(1, len(indexed)):
            history = indexed[signal_column].iloc[:position]
            fit.iloc[position, fit.columns.get_loc("history_start")] = history.index[0]
            fit.iloc[position, fit.columns.get_loc("history_end")] = history.index[-1]
            fit.iloc[position, fit.columns.get_loc("curve")] = implementations["fit"](
                {"values": history.astype(float).tolist()},
                dict(implementation_parameters["fit"]),
            )
    else:
        fit = prior_log_ols(
            indexed[signal_column],
            window_sessions=operator_parameters["fit"]["window_sessions"],
        )
    if "smoothing" in implementations:
        finite_fit = fit["curve"].dropna()
        custom_smoothed = []
        for position in range(1, len(finite_fit) + 1):
            prefix = finite_fit.iloc[:position].astype(float).tolist()
            output = implementations["smoothing"](
                {"values": prefix},
                dict(implementation_parameters["smoothing"]),
            )
            custom_smoothed.append(output[-1])
        smoothed = pd.Series(custom_smoothed, index=finite_fit.index, dtype=float).reindex(
            indexed.index
        )
    else:
        smoothed = recursive_log_ema(
            fit["curve"],
            span_sessions=operator_parameters["smoothing"]["span_sessions"],
        )
    if "statistic" in implementations:
        finite_smoothed = smoothed.dropna()
        custom_statistic = []
        for position in range(1, len(finite_smoothed) + 1):
            prefix = finite_smoothed.iloc[:position].astype(float).tolist()
            output = implementations["statistic"](
                {"values": prefix},
                dict(implementation_parameters["statistic"]),
            )
            custom_statistic.append(output[-1])
        statistic = pd.Series(custom_statistic, index=finite_smoothed.index, dtype=float).reindex(
            indexed.index
        )
    else:
        statistic = adjacent_curve_pct_slope(smoothed)
    start = pd.Timestamp(template["evaluation_start"])
    end = (
        pd.Timestamp(template["evaluation_end"])
        if template["evaluation_end"] is not None
        else indexed.index.max()
    )
    evaluation = indexed.loc[(indexed.index >= start) & (indexed.index <= end)].copy()
    if evaluation.empty:
        raise ReplayError("evaluation interval contains no dataset sessions")
    if fit.loc[evaluation.index, "curve"].isna().any():
        raise ReplayError("insufficient prior history for the evaluation interval")

    action_mode = corporate_action_evidence is not None
    if action_mode != (settlement_schedule is not None):
        raise ReplayError(
            "corporate-action accounting requires both admitted evidence and an explicit "
            "settlement schedule"
        )
    actions: tuple[CashDividend, ...] = ()
    if corporate_action_evidence is not None:
        try:
            tax_policy_identity()
            actions = tuple(
                action
                for action in accounting_cash_dividends(corporate_action_evidence)
                if start.date() <= action.record_date <= end.date()
            )
        except CorporateActionEvidenceError as exc:
            raise ReplayError(f"corporate-action accounting input is invalid: {exc}") from exc

    capital = float(template["initial_capital_cny"])
    sizing = operator_parameters["sizing"]
    costs = operator_parameters["cost"]
    zero_implementations = {key: value for key, value in implementations.items() if key != "cost"}
    if settlement_schedule is not None:
        account = _SettlementAccount(
            "strategy",
            capital,
            sizing,
            costs,
            settlement_schedule,
            implementations,
            implementation_parameters,
        )
        zero_account = _SettlementAccount(
            "zero_cost",
            capital,
            sizing,
            _zero_cost_parameters(costs),
            settlement_schedule,
            zero_implementations,
            implementation_parameters,
        )
        buy_hold_account = _SettlementAccount(
            "buy_and_hold",
            capital,
            sizing,
            costs,
            settlement_schedule,
            implementations,
            implementation_parameters,
        )
    else:
        account = _Account(capital, sizing, costs, implementations, implementation_parameters)
        zero_account = _Account(
            capital,
            sizing,
            _zero_cost_parameters(costs),
            zero_implementations,
            implementation_parameters,
        )
        buy_hold_account = _Account(
            capital, sizing, costs, implementations, implementation_parameters
        )
    buy_hold_entry = buy_hold_account.buy(
        evaluation.index[0],
        float(evaluation.iloc[0]["Open"]),
        "BUY_AND_HOLD_ENTRY",
    )
    buy_hold_total_cost = (
        float(buy_hold_entry["total_cost_cny"]) if buy_hold_entry is not None else 0.0
    )
    decision = HysteresisDecision(**operator_parameters["decision"])
    event_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    cumulative_costs = 0.0

    for position, (session, bar) in enumerate(evaluation.iterrows()):
        raw_open = float(bar["Open"])
        raw_close = float(bar["Close"])
        position_before = int(account.holdings > 0)
        if "decision" not in implementations:
            decision_result = decision.step(float(statistic.loc[session]))
        else:
            statistic_prefix = [
                None if pd.isna(value) else float(value)
                for value in statistic.loc[evaluation.index[: position + 1]]
            ]
            custom_decision = implementations["decision"](
                {
                    "statistics": statistic_prefix,
                    "initial_position": int(account.holdings > 0),
                },
                dict(implementation_parameters["decision"]),
            )[-1]
            previous_statistic = (
                None if position == 0 else float(statistic.loc[evaluation.index[position - 1]])
            )
            decision_result = DecisionResult(
                custom_decision["action"],
                custom_decision["reason"],
                int(account.holdings > 0),
                previous_statistic,
            )
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
            terminal = account.sell(session, raw_open, "TERMINAL_FORCED_LIQUIDATION")
            zero_account.sell(session, raw_open, "TERMINAL_FORCED_LIQUIDATION")
            buy_hold_terminal = buy_hold_account.sell(
                session, raw_open, "TERMINAL_FORCED_LIQUIDATION"
            )
            if buy_hold_terminal is not None:
                buy_hold_total_cost += float(buy_hold_terminal["total_cost_cny"])
            if terminal is not None:
                day_events.append(terminal)
                action = "SELL"
                reason = "TERMINAL_FORCED_LIQUIDATION"

        if isinstance(account, _SettlementAccount):
            for settlement_account in (account, zero_account, buy_hold_account):
                if not isinstance(settlement_account, _SettlementAccount):
                    raise ReplayError("settlement account adapters are inconsistent")
                settlement_account.process_day(session.date(), actions, raw_close)

        event_rows.extend(day_events)
        day_costs = {name: float(sum(event[name] for event in day_events)) for name in COST_FIELDS}
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
                "close": raw_close,
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
    cost_breakdown = {name: float(events[name].sum()) for name in COST_FIELDS}
    if isinstance(account, _SettlementAccount):
        settlement_accounts = (account, zero_account, buy_hold_account)
        if not all(isinstance(item, _SettlementAccount) for item in settlement_accounts):
            raise ReplayError("settlement account adapters are inconsistent")
        cursor = evaluation.index[-1].date()
        while True:
            pending_dates = set().union(
                *(item.pending_dates(actions, cursor) for item in settlement_accounts)
            )
            if not pending_dates:
                break
            cursor = min(pending_dates)
            for settlement_account in settlement_accounts:
                settlement_account.process_day(cursor, actions, None)
        account_events = pd.DataFrame(
            [row for item in settlement_accounts for row in item.events]
        ).sort_values(["Date", "account", "sequence"], kind="stable", ignore_index=True)
        account_trades = pd.concat(
            [item.trade_ledger(evaluation.iloc[-1]) for item in settlement_accounts],
            ignore_index=True,
        )
        strategy_trades = account_trades[account_trades["account"] == "strategy"]
        trade_rows = []
        for row in strategy_trades.to_dict("records"):
            quantity = row["quantity"]
            is_open = row["status"] == "OPEN"
            basis_fen = row["entry_notional_fen"] + row["entry_cost_fen"]
            trade_rows.append(
                {
                    "entry_date": row["entry_trade_date"],
                    "entry_price": fen_to_cny(row["entry_notional_fen"]) / quantity,
                    "quantity": quantity,
                    "entry_cost_cny": fen_to_cny(row["entry_cost_fen"]),
                    "exit_date": pd.NaT if is_open else row["exit_trade_date"],
                    "exit_price": (
                        np.nan if is_open else fen_to_cny(row["exit_notional_fen"]) / quantity
                    ),
                    "exit_cost_cny": fen_to_cny(row["exit_cost_fen"]),
                    "status": row["status"],
                    "gross_pnl_cny": fen_to_cny(
                        row["exit_notional_fen"] - row["entry_notional_fen"]
                    ),
                    "net_pnl_cny": fen_to_cny(row["net_pnl_fen"]),
                    "return": row["net_pnl_fen"] / basis_fen,
                }
            )
        trades = pd.DataFrame(trade_rows, columns=TRADE_COLUMNS)
        final_states = [item._state() for item in settlement_accounts]
        account_final_states = {
            item.account_id: dict(state)
            for item, state in zip(settlement_accounts, final_states, strict=True)
        }
        daily.loc[daily.index[-1], ["cash", "market_value", "equity"]] = [
            fen_to_cny(final_states[0]["cash_fen"]),
            fen_to_cny(final_states[0]["market_value_fen"]),
            fen_to_cny(final_states[0]["equity_fen"]),
        ]
        daily.loc[daily.index[-1], "net_pnl"] = fen_to_cny(final_states[0]["equity_fen"]) - capital
        daily.loc[daily.index[-1], "gross_pnl"] = daily.iloc[-1]["net_pnl"] + cumulative_costs
        daily.loc[daily.index[-1], "zero_cost_equity"] = fen_to_cny(final_states[1]["equity_fen"])
        daily.loc[daily.index[-1], "buy_hold_equity"] = fen_to_cny(final_states[2]["equity_fen"])
    else:
        trades = _trade_ledger(events, evaluation.iloc[-1])
        account_events = events.copy()
        account_events.insert(0, "account", "strategy")
        account_events.insert(2, "event_type", "TRADE_EXECUTION")
        account_trades = trades.copy()
        account_trades.insert(0, "account", "strategy")
        account_final_states = {}
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
        "buy_hold_total_cost_cny": buy_hold_total_cost,
        "max_drawdown": float(drawdown.min()),
        "closed_trades": int(len(closed)),
        "open_trades": int((trades["status"] == "OPEN").sum()),
        "closed_trade_win_rate": (
            float((closed["net_pnl_cny"] > 0).mean()) if len(closed) else None
        ),
        "current_position": "LONG" if account.holdings else "FLAT",
        "price_return_only": not action_mode,
    }
    if isinstance(account, _SettlementAccount):
        strategy_events = account_events[account_events["account"] == "strategy"]
        source_claim = (
            "KNOWN_EVENT_CORRECTED_PARTIAL"
            if corporate_action_evidence.document["revisions"]
            else "PRICE_RETURN_ONLY"
        )
        account_facts = {}
        for settlement_account in settlement_accounts:
            events_for_account = account_events[
                account_events["account"] == settlement_account.account_id
            ]
            gross_dividend_fen = int(
                events_for_account.loc[
                    events_for_account["event_type"] == "DIVIDEND_PAYMENT",
                    "cash_delta_fen",
                ].sum()
            )
            collected_tax_fen = -int(
                events_for_account.loc[
                    events_for_account["event_type"] == "TAX_COLLECTION",
                    "cash_delta_fen",
                ].sum()
            )
            trading_cost_fen = int(
                events_for_account.loc[
                    events_for_account["event_type"] == "TRADE_COST", "cost_fen"
                ].sum()
            )
            final_state = account_final_states[settlement_account.account_id]
            account_facts[settlement_account.account_id] = {
                "initial_capital_fen": settlement_account.initial_capital_fen,
                "final_state": final_state,
                "gross_dividend_fen": gross_dividend_fen,
                "net_dividend_fen": gross_dividend_fen - collected_tax_fen,
                "deferred_tax_fen": final_state["deferred_tax_base_fen"],
                "collected_tax_fen": collected_tax_fen,
                "outstanding_tax_fen": final_state["outstanding_tax_fen"],
                "trading_cost_fen": trading_cost_fen,
                "price_profit_fen": (
                    final_state["equity_fen"]
                    - settlement_account.initial_capital_fen
                    - gross_dividend_fen
                    + collected_tax_fen
                ),
                "after_tax_profit_fen": (
                    final_state["equity_fen"] - settlement_account.initial_capital_fen
                ),
            }
        metrics.update(
            {
                "accounting_status": source_claim,
                "corporate_action_evidence_sha256": corporate_action_evidence.digest,
                "tax_policy_id": tax_policy_identity()["tax_policy_id"],
                "settlement_schedule_sha256": settlement_schedule.digest,
                "accounting_close_date": cursor.isoformat(),
                "gross_dividends_cny": fen_to_cny(
                    int(
                        strategy_events.loc[
                            strategy_events["event_type"] == "DIVIDEND_PAYMENT",
                            "cash_delta_fen",
                        ].sum()
                    )
                ),
                "dividend_tax_cny": fen_to_cny(
                    int(
                        strategy_events.loc[
                            strategy_events["event_type"] == "TAX_LIABILITY",
                            "cost_fen",
                        ].sum()
                    )
                ),
                "outstanding_tax_cny": fen_to_cny(account.outstanding_tax_fen),
                "accounting_accounts": account_facts,
            }
        )
        reconciliation = _reconcile_settlement(settlement_accounts, account_events, account_trades)
    else:
        reconciliation = _reconcile(daily, events, trades, capital)
    return ReplayResult(
        daily=daily,
        events=events,
        trades=trades,
        metrics=metrics,
        cost_breakdown=cost_breakdown,
        reconciliation=reconciliation,
        account_events=account_events,
        account_trades=account_trades,
        account_final_states=account_final_states,
    )
