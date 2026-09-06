from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.strategy_replay as replay_module
from quant_platform.corporate_actions import CashDividend, SettlementSchedule
from quant_platform.strategy_config import validate_strategy_config
from quant_platform.strategy_operators import all_in_quantity, cms_cost_breakdown
from quant_platform.strategy_replay import ReplayError, replay_strategy
from test_corporate_actions import bocom_evidence


FIXTURE = Path(__file__).parent / "fixtures" / "strategy" / "daily.csv"


def _config(*, terminal_handling: str = "mark_to_market", capital: float = 100000.0):
    return validate_strategy_config(
        {
            "schema_version": 1,
            "dataset": {
                "root": "unused",
                "instrument": "SYNTH.SS",
                "snapshot_id": "a" * 64,
            },
            "output_root": "unused",
            "template": {
                "name": "single_stock_daily_causal",
                "version": "1",
                "parameters": {
                    "instrument_display_name": "Synthetic <Bank>",
                    "evaluation_start": "2026-01-06",
                    "evaluation_end": "2026-01-12",
                    "initial_capital_cny": capital,
                    "initial_state": "flat",
                    "terminal_handling": terminal_handling,
                    "cost_assumption_label": "Synthetic exact-cost fixture",
                },
            },
            "operators": {
                "fit": {
                    "name": "prior_log_ols",
                    "version": "1",
                    "parameters": {
                        "window_sessions": 2,
                        "price_column": "AdjustedClose",
                    },
                },
                "smoothing": {
                    "name": "recursive_log_ema",
                    "version": "1",
                    "parameters": {"span_sessions": 1},
                },
                "statistic": {
                    "name": "adjacent_curve_pct_slope",
                    "version": "1",
                    "parameters": {},
                },
                "decision": {
                    "name": "post_start_threshold_crossing_hysteresis",
                    "version": "1",
                    "parameters": {
                        "buy_threshold_pct_per_day": 1.0,
                        "sell_threshold_abs_pct_per_day": 1.0,
                    },
                },
                "sizing": {
                    "name": "all_in_all_out_a_share_lots",
                    "version": "1",
                    "parameters": {"lot_size": 100, "target_fraction": 1.0},
                },
                "cost": {
                    "name": "cms_china_a_share",
                    "version": "1",
                    "parameters": {
                        "commission_rate": 0.0003,
                        "minimum_commission_cny": 5.0,
                        "transfer_fee_rate": 0.00001,
                        "sell_stamp_tax_rate": 0.0005,
                        "buy_slippage_bps": 2.0,
                        "sell_slippage_bps": 3.0,
                    },
                },
                "report": {
                    "name": "concise_chinese_causal_trade",
                    "version": "1",
                    "parameters": {},
                },
            },
        }
    )


def _frame() -> pd.DataFrame:
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    return frame


def _bocom_accounting_case():
    dates = [
        "2025-08-13",
        "2025-08-14",
        "2025-08-15",
        "2025-08-18",
        "2025-11-06",
        "2025-11-07",
        "2025-12-24",
        "2025-12-25",
        "2026-01-22",
        "2026-01-23",
        "2026-01-26",
        "2026-03-31",
        "2026-04-01",
        "2026-06-30",
    ]
    trade_prices = {
        "2025-08-15": 7.610000133514404,
        "2025-11-06": 7.340000152587891,
        "2026-01-22": 6.739999771118164,
        "2026-03-31": 6.989999771118164,
        "2026-06-30": 6.539999961853027,
    }
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": [trade_prices.get(value, 7.0) for value in dates],
            "High": [trade_prices.get(value, 7.0) + 0.1 for value in dates],
            "Low": [trade_prices.get(value, 7.0) - 0.1 for value in dates],
            "Close": [trade_prices.get(value, 7.0) for value in dates],
            "Volume": [10000 + index for index in range(len(dates))],
            "AdjustedClose": [100.0 + index for index in range(len(dates))],
        }
    )
    canonical = _config(terminal_handling="force_liquidate").canonical
    canonical["dataset"]["instrument"] = "601328.SS"
    parameters = canonical["template"]["parameters"]
    parameters["evaluation_start"] = "2025-08-15"
    parameters["evaluation_end"] = "2026-06-30"
    canonical["operators"]["cost"]["parameters"]["buy_slippage_bps"] = 5.0
    canonical["operators"]["cost"]["parameters"]["sell_slippage_bps"] = 5.0
    config = validate_strategy_config(canonical)
    schedule = SettlementSchedule(
        {
            "2025-08-15": "2025-08-18",
            "2025-11-06": "2025-11-07",
            "2026-01-22": "2026-01-23",
            "2026-03-31": "2026-04-01",
            "2026-06-30": "2026-07-01",
        },
        {
            "2026-01-23": "2026-01-26",
            "2026-07-01": "2026-07-02",
        },
    )
    action_by_position = {2: "BUY", 6: "SELL", 9: "BUY"}

    def decision(inputs, _parameters):
        position = len(inputs["statistics"]) - 1
        action = action_by_position.get(position, "HOLD")
        return [{"action": action, "reason": f"FIXED_{action}"}]

    return frame, config, schedule, {"decision": decision}, {"decision": {}}


def _ledger_account(schedule: SettlementSchedule, account_id: str = "strategy"):
    costs = {
        "commission_rate": 0.0,
        "minimum_commission_cny": 0.0,
        "transfer_fee_rate": 0.0,
        "sell_stamp_tax_rate": 0.0,
        "buy_slippage_bps": 0.0,
        "sell_slippage_bps": 0.0,
    }
    return replay_module._SettlementAccount(
        account_id,
        1000.0,
        {"lot_size": 1, "target_fraction": 1.0},
        costs,
        schedule,
    )


def _assert_event_state_reconciles(account) -> None:
    events = pd.DataFrame(account.events)
    state_columns = [
        "receivable_fen",
        "unpaid_dividend_tax_base_fen",
        "deferred_tax_base_fen",
        "outstanding_tax_fen",
    ]
    assert all(
        all(int(value) >= 0 for value in events[column].tolist()) for column in state_columns
    )
    assert (
        events["equity_fen"]
        == events["cash_fen"]
        + events["market_value_fen"]
        + events["receivable_fen"]
        - events["outstanding_tax_fen"]
    ).all()


def test_replay_is_prior_only_and_records_required_daily_fields():
    result = replay_strategy(_frame(), _config())
    daily = result.daily

    assert daily["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
        "2026-01-12",
    ]
    assert (daily["history_end"] < daily["Date"]).all()
    assert {
        "history_start",
        "history_end",
        "curve",
        "smoothed_curve",
        "statistic",
        "previous_statistic",
        "decision",
        "reason",
        "position_before",
        "position_after",
        "price",
        "quantity",
        "cash",
        "holdings",
        "market_value",
        "equity",
        "gross_pnl",
        "net_pnl",
        "commission_cny",
        "transfer_fee_cny",
        "stamp_tax_cny",
        "slippage_cny",
        "total_cost_cny",
        "zero_cost_equity",
        "buy_hold_equity",
    }.issubset(daily.columns)
    assert daily.iloc[0]["decision"] == "HOLD"
    assert daily.iloc[0]["reason"] == "INITIALIZE_ZONE"
    assert daily.iloc[1]["decision"] == "BUY"
    assert daily.iloc[2]["decision"] == "SELL"
    assert daily.iloc[3]["decision"] == "BUY"


def test_future_mutation_cannot_change_replay_prefix():
    original = _frame()
    first = replay_strategy(original, _config()).daily
    changed = original.copy()
    changed.loc[changed["Date"] == pd.Timestamp("2026-01-12"), "AdjustedClose"] = 500.0
    second = replay_strategy(changed, _config()).daily

    columns = [
        "curve",
        "smoothed_curve",
        "statistic",
        "decision",
        "reason",
        "position_after",
        "cash",
        "holdings",
        "equity",
    ]
    pd.testing.assert_frame_equal(first.iloc[:-1][columns], second.iloc[:-1][columns])


def test_events_use_raw_open_and_costs_reconcile_exactly():
    result = replay_strategy(_frame(), _config())
    events = result.events
    daily = result.daily

    assert events["side"].tolist() == ["BUY", "SELL", "BUY"]
    assert events["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ]
    assert events["price"].tolist() == [10.0, 11.0, 9.0]
    assert (events["quantity"] % 100 == 0).all()
    assert events.iloc[0]["commission_cny"] >= 5.0
    assert events.iloc[0]["stamp_tax_cny"] == 0.0
    assert events.iloc[1]["stamp_tax_cny"] > 0.0
    assert events["total_cost_cny"].sum() == pytest.approx(daily["total_cost_cny"].sum())
    assert result.cost_breakdown["total_cost_cny"] == pytest.approx(events["total_cost_cny"].sum())


def test_open_terminal_trade_has_entry_cost_only_and_is_not_a_closed_win():
    result = replay_strategy(_frame(), _config())

    assert "return" in result.trades.columns
    assert result.trades["status"].tolist() == ["CLOSED", "OPEN"]
    open_trade = result.trades.iloc[-1]
    assert pd.isna(open_trade["exit_date"])
    assert open_trade["exit_cost_cny"] == 0.0
    assert result.metrics["closed_trades"] == 1
    assert result.metrics["open_trades"] == 1
    assert result.metrics["closed_trade_win_rate"] in {0.0, 1.0}
    assert result.metrics["current_position"] == "LONG"


def test_optional_terminal_liquidation_sells_all_at_final_raw_open():
    frame = _frame()
    marked = replay_strategy(frame, _config())
    result = replay_strategy(frame, _config(terminal_handling="force_liquidate"))

    assert result.events.iloc[-1]["side"] == "SELL"
    assert result.events.iloc[-1]["Date"] == pd.Timestamp("2026-01-12")
    assert result.events.iloc[-1]["price"] == 10.5
    assert result.daily.iloc[-1]["holdings"] == 0
    assert result.trades["status"].tolist() == ["CLOSED", "CLOSED"]
    assert result.metrics["open_trades"] == 0
    assert result.metrics["current_position"] == "FLAT"

    sizing = {"lot_size": 100, "target_fraction": 1.0}
    costs = _config().canonical["operators"]["cost"]["parameters"]
    quantity = all_in_quantity(
        cash=100000.0,
        raw_price=10.0,
        cost_parameters=costs,
        **sizing,
    )
    entry_cost = cms_cost_breakdown(side="BUY", raw_price=10.0, quantity=quantity, **costs)[
        "total_cost_cny"
    ]
    exit_cost = cms_cost_breakdown(side="SELL", raw_price=10.5, quantity=quantity, **costs)[
        "total_cost_cny"
    ]
    expected_buy_hold = 100000.0 - quantity * 10.0 - entry_cost + quantity * 10.5 - exit_cost
    assert result.metrics["buy_hold_final_equity_cny"] == pytest.approx(expected_buy_hold)
    assert result.metrics["buy_hold_total_cost_cny"] == pytest.approx(entry_cost + exit_cost)
    assert result.metrics["buy_hold_final_equity_cny"] != pytest.approx(
        marked.metrics["buy_hold_final_equity_cny"]
    )
    assert result.metrics["zero_cost_final_equity_cny"] != pytest.approx(
        marked.metrics["zero_cost_final_equity_cny"]
    )


def test_ledger_equity_cost_and_benchmark_reconciliation():
    result = replay_strategy(_frame(), _config())
    metrics = result.metrics
    final = result.daily.iloc[-1]

    assert final["cash"] + final["market_value"] == pytest.approx(final["equity"])
    assert final["equity"] - metrics["initial_capital_cny"] == pytest.approx(
        metrics["net_profit_cny"]
    )
    assert final["net_pnl"] == pytest.approx(metrics["net_profit_cny"])
    assert final["gross_pnl"] - final["net_pnl"] == pytest.approx(
        result.cost_breakdown["total_cost_cny"]
    )
    assert metrics["final_equity_cny"] == pytest.approx(final["equity"])
    assert metrics["zero_cost_final_equity_cny"] == pytest.approx(final["zero_cost_equity"])
    assert metrics["buy_hold_final_equity_cny"] == pytest.approx(final["buy_hold_equity"])
    assert result.reconciliation == {
        "daily_equity": True,
        "event_cash": True,
        "event_positions": True,
        "event_costs": True,
        "trade_events": True,
        "profit_identity": True,
        "trade_net_pnl": True,
    }


def test_trade_net_pnl_reconciliation_fails_on_ledger_drift(monkeypatch):
    original = replay_module._trade_ledger

    def corrupt_trade_ledger(events, endpoint):
        trades = original(events, endpoint)
        trades.loc[0, "net_pnl_cny"] += 1.0
        return trades

    monkeypatch.setattr(replay_module, "_trade_ledger", corrupt_trade_ledger)

    with pytest.raises(ReplayError, match="trade_net_pnl"):
        replay_strategy(_frame(), _config())


def test_insufficient_cash_records_no_event_and_stays_flat():
    result = replay_strategy(_frame(), _config(capital=1005.0))

    buy_day = result.daily.loc[result.daily["decision"] == "BUY"].iloc[0]
    assert buy_day["reason"] == "INSUFFICIENT_CASH"
    assert buy_day["position_after"] == 0
    assert not (result.events["Date"] == pd.Timestamp("2026-01-07")).any()


def test_missing_explicit_signal_column_and_empty_evaluation_fail_closed():
    no_adjusted = _frame().drop(columns=["AdjustedClose"])
    with pytest.raises(ReplayError, match="AdjustedClose"):
        replay_strategy(no_adjusted, _config())

    config = _config()
    canonical = config.canonical
    canonical["template"]["parameters"]["evaluation_start"] = "2027-01-01"
    canonical["template"]["parameters"]["evaluation_end"] = None
    with pytest.raises(ReplayError, match="evaluation interval"):
        replay_strategy(_frame(), validate_strategy_config(canonical))


def test_bocom_known_event_settlement_matches_checksum_bound_integer_fen_oracle():
    frame, config, schedule, implementations, parameters = _bocom_accounting_case()
    price_only = replay_strategy(
        frame,
        config,
        implementations=implementations,
        implementation_parameters=parameters,
    )

    result = replay_strategy(
        frame,
        config,
        implementations=implementations,
        implementation_parameters=parameters,
        corporate_action_evidence=bocom_evidence(),
        settlement_schedule=schedule,
    )

    pd.testing.assert_series_equal(
        result.daily["decision"], price_only.daily["decision"], check_names=False
    )
    assert result.metrics["accounting_status"] == "KNOWN_EVENT_CORRECTED_PARTIAL"
    assert "AFTER_TAX_TOTAL_RETURN_VERIFIED" not in str(result.metrics)
    assert result.metrics["final_equity_cny"] == 87377.93
    assert result.metrics["buy_hold_final_equity_cny"] == 87632.78
    assert result.metrics["gross_dividends_cny"] == 2125.68
    assert result.metrics["dividend_tax_cny"] == 212.57
    assert result.metrics["accounting_close_date"] == "2026-07-02"
    assert all(result.reconciliation.values())
    assert set(result.account_final_states) == {"strategy", "zero_cost", "buy_and_hold"}
    assert set(result.metrics["accounting_accounts"]) == set(result.account_final_states)
    assert all(
        type(value) is int
        for account in result.metrics["accounting_accounts"].values()
        for value in (
            account["initial_capital_fen"],
            account["gross_dividend_fen"],
            account["net_dividend_fen"],
            account["deferred_tax_fen"],
            account["collected_tax_fen"],
            account["outstanding_tax_fen"],
            account["trading_cost_fen"],
            account["price_profit_fen"],
            account["after_tax_profit_fen"],
        )
    )

    event_types = set(result.account_events["event_type"])
    assert {
        "ACQUISITION_SETTLEMENT",
        "DISPOSAL_SETTLEMENT",
        "DIVIDEND_ENTITLEMENT",
        "DIVIDEND_PAYMENT",
        "TAX_LIABILITY",
        "TAX_COLLECTION",
        "TRADE_COST",
        "ACCOUNT_MARK",
    }.issubset(event_types)
    assert set(result.account_events["account"]) == {
        "strategy",
        "zero_cost",
        "buy_and_hold",
    }
    strategy_entitlement = result.account_events.loc[
        (result.account_events["account"] == "strategy")
        & (result.account_events["event_type"] == "DIVIDEND_ENTITLEMENT")
    ].iloc[0]
    assert strategy_entitlement["Date"] == pd.Timestamp("2025-12-24")
    assert strategy_entitlement["quantity"] == 13600
    assert result.account_events.loc[
        result.account_events["event_type"] == "TAX_LIABILITY", "Date"
    ].min() == pd.Timestamp("2026-01-23")
    assert result.account_events.loc[
        result.account_events["event_type"] == "TAX_COLLECTION", "Date"
    ].min() == pd.Timestamp("2026-01-26")


def test_action_accounting_fails_closed_without_exact_settlement_mapping():
    frame, config, _, implementations, parameters = _bocom_accounting_case()

    with pytest.raises(ReplayError, match="requires both"):
        replay_strategy(
            frame,
            config,
            implementations=implementations,
            implementation_parameters=parameters,
            corporate_action_evidence=bocom_evidence(),
        )

    incomplete = SettlementSchedule(
        {"2025-08-15": "2025-08-18"},
        {},
    )
    with pytest.raises(ReplayError, match="unknown for 2025-11-06"):
        replay_strategy(
            frame,
            config,
            implementations=implementations,
            implementation_parameters=parameters,
            corporate_action_evidence=bocom_evidence(),
            settlement_schedule=incomplete,
        )


def test_tax_collection_after_settlement_keeps_insufficient_amount_outstanding():
    frame, config, schedule, _, parameters = _bocom_accounting_case()
    canonical = config.canonical
    canonical["operators"]["sizing"]["parameters"]["lot_size"] = 1
    config = validate_strategy_config(canonical)
    action_by_position = {2: "BUY", 6: "SELL", 8: "BUY"}
    trade_to_settlement = dict(schedule.trade_to_settlement)
    trade_to_settlement["2026-01-26"] = "2026-01-26"
    schedule = SettlementSchedule(
        trade_to_settlement,
        schedule.settlement_to_collection,
    )

    def reinvest_before_collection(inputs, _parameters):
        action = action_by_position.get(len(inputs["statistics"]) - 1, "HOLD")
        return [{"action": action, "reason": f"FIXED_{action}"}]

    result = replay_strategy(
        frame,
        config,
        implementations={"decision": reinvest_before_collection},
        implementation_parameters=parameters,
        corporate_action_evidence=bocom_evidence(),
        settlement_schedule=schedule,
    )

    outstanding = result.account_events.loc[
        (result.account_events["account"] == "strategy")
        & (result.account_events["event_type"] == "TAX_COLLECTION_OUTSTANDING")
    ]
    assert len(outstanding) == 1
    assert outstanding.iloc[0]["cash_delta_fen"] == 0
    assert outstanding.iloc[0]["outstanding_tax_fen"] > 0


def test_not_held_account_ignores_multiple_dividend_events():
    schedule = SettlementSchedule(
        {"2026-01-01": "2026-01-02"},
        {},
    )
    account = _ledger_account(schedule)
    actions = (
        CashDividend(
            "event-a",
            date(2026, 1, 3),
            date(2026, 1, 4),
            date(2026, 1, 5),
            Decimal("0.10"),
        ),
        CashDividend(
            "event-b",
            date(2026, 1, 6),
            date(2026, 1, 7),
            date(2026, 1, 8),
            Decimal("0.20"),
        ),
    )

    for event_date in (
        date(2026, 1, 3),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 8),
    ):
        account.process_day(event_date, actions, 1.0)

    assert not any(event["event_type"].startswith("DIVIDEND_") for event in account.events)
    assert account.entitlements == {}
    assert account.cash_fen == account.initial_capital_fen
    assert account.receivable_fen == 0
    assert account.unpaid_dividend_tax_base_fen == 0
    assert account.deferred_tax_base_fen == 0
    _assert_event_state_reconciles(account)


def test_multiple_dividends_remain_separate_and_reconcile_exactly():
    schedule = SettlementSchedule(
        {
            "2026-01-01": "2026-01-02",
            "2026-01-10": "2026-01-11",
        },
        {"2026-01-11": "2026-01-12"},
    )
    account = _ledger_account(schedule)
    actions = (
        CashDividend(
            "event-a",
            date(2026, 1, 3),
            date(2026, 1, 4),
            date(2026, 1, 5),
            Decimal("0.10"),
        ),
        CashDividend(
            "event-b",
            date(2026, 1, 6),
            date(2026, 1, 7),
            date(2026, 1, 8),
            Decimal("0.20"),
        ),
    )

    account._trade(date(2026, 1, 1), "BUY", 1.0, 2, "FIRST_LOT")
    for event_date in (
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 8),
    ):
        account.process_day(event_date, actions, None)

    assert account.deferred_tax_base_fen == 60
    assert [
        event["event_revision_id"]
        for event in account.events
        if event["event_type"] == "DIVIDEND_PAYMENT"
    ] == ["event-a", "event-b"]
    account._trade(date(2026, 1, 10), "SELL", 1.0, 2, "CLOSE")
    account.process_day(date(2026, 1, 11), actions, None)
    account.process_day(date(2026, 1, 12), actions, None)

    assert account.closed_trades[0]["dividend_fen"] == 60
    assert account.closed_trades[0]["tax_fen"] == 12
    assert account.closed_trades[0]["net_pnl_fen"] == 48
    assert account.deferred_tax_base_fen == 0
    assert account.outstanding_tax_fen == 0
    _assert_event_state_reconciles(account)


def test_three_lot_fifo_tax_allocation_uses_advancing_remainder_state():
    schedule = SettlementSchedule(
        {
            "2026-01-01": "2026-01-02",
            "2026-01-02": "2026-01-03",
            "2026-01-03": "2026-01-04",
            "2026-01-07": "2026-01-08",
        },
        {"2026-01-08": "2026-01-09"},
    )
    account = _ledger_account(schedule)
    action = CashDividend(
        "three-lot-event",
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 6),
        Decimal("1.00"),
    )

    account._trade(date(2026, 1, 1), "BUY", 1.0, 1, "LOT_1")
    account.process_day(date(2026, 1, 2), (action,), None)
    account._trade(date(2026, 1, 2), "BUY", 1.0, 3, "LOT_2")
    account.process_day(date(2026, 1, 3), (action,), None)
    account._trade(date(2026, 1, 3), "BUY", 1.0, 6, "LOT_3")
    account.process_day(date(2026, 1, 4), (action,), None)
    account.process_day(date(2026, 1, 5), (action,), None)
    account.process_day(date(2026, 1, 6), (action,), None)
    account._trade(date(2026, 1, 7), "SELL", 1.0, 10, "CLOSE_ALL")
    account.process_day(date(2026, 1, 8), (action,), None)

    assert [trade["quantity"] for trade in account.closed_trades] == [1, 3, 6]
    assert [trade["dividend_fen"] for trade in account.closed_trades] == [100, 300, 600]
    assert [trade["tax_fen"] for trade in account.closed_trades] == [20, 60, 120]
    assert [trade["net_pnl_fen"] for trade in account.closed_trades] == [80, 240, 480]
    assert sum(trade["tax_fen"] for trade in account.closed_trades) == 200
    account.process_day(date(2026, 1, 9), (action,), None)
    _assert_event_state_reconciles(account)


def test_partial_disposals_consume_multiple_fifo_lots_exactly():
    schedule = SettlementSchedule(
        {
            "2026-01-01": "2026-01-02",
            "2026-01-02": "2026-01-03",
            "2026-01-07": "2026-01-08",
            "2026-01-09": "2026-01-10",
        },
        {
            "2026-01-08": "2026-01-09",
            "2026-01-10": "2026-01-11",
        },
    )
    account = _ledger_account(schedule)
    action = CashDividend(
        "partial-event",
        date(2026, 1, 4),
        date(2026, 1, 5),
        date(2026, 1, 5),
        Decimal("1.00"),
    )

    account._trade(date(2026, 1, 1), "BUY", 1.0, 4, "LOT_1")
    account.process_day(date(2026, 1, 2), (action,), None)
    account._trade(date(2026, 1, 2), "BUY", 1.0, 6, "LOT_2")
    account.process_day(date(2026, 1, 3), (action,), None)
    account.process_day(date(2026, 1, 4), (action,), None)
    account.process_day(date(2026, 1, 5), (action,), None)
    account._trade(date(2026, 1, 7), "SELL", 1.0, 5, "PARTIAL_CLOSE")
    account.process_day(date(2026, 1, 8), (action,), None)

    assert [(trade["lot_id"], trade["quantity"]) for trade in account.closed_trades] == [
        ("strategy-lot-000001", 4),
        ("strategy-lot-000002", 1),
    ]
    assert [trade["tax_fen"] for trade in account.closed_trades] == [80, 20]
    assert account.lots[1]["remaining_quantity"] == 5
    assert account.deferred_tax_base_fen == 500
    account.process_day(date(2026, 1, 9), (action,), None)
    account._trade(date(2026, 1, 9), "SELL", 1.0, 5, "FINAL_CLOSE")
    account.process_day(date(2026, 1, 10), (action,), None)
    account.process_day(date(2026, 1, 11), (action,), None)

    assert [(trade["lot_id"], trade["quantity"]) for trade in account.closed_trades] == [
        ("strategy-lot-000001", 4),
        ("strategy-lot-000002", 1),
        ("strategy-lot-000002", 5),
    ]
    assert [trade["tax_fen"] for trade in account.closed_trades] == [80, 20, 100]
    assert account.deferred_tax_base_fen == 0
    _assert_event_state_reconciles(account)


def test_disposal_before_payment_never_creates_negative_tax_state():
    schedule = SettlementSchedule(
        {
            "2026-01-01": "2026-01-02",
            "2026-01-04": "2026-01-05",
        },
        {"2026-01-05": "2026-01-06"},
    )
    account = _ledger_account(schedule)
    action = CashDividend(
        "pre-payment-event",
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 10),
        Decimal("1.00"),
    )

    account._trade(date(2026, 1, 1), "BUY", 1.0, 1, "ENTRY")
    account.process_day(date(2026, 1, 2), (action,), None)
    account.process_day(date(2026, 1, 3), (action,), None)
    account._trade(date(2026, 1, 4), "SELL", 1.0, 1, "PRE_PAYMENT_EXIT")
    account.process_day(date(2026, 1, 5), (action,), None)

    assert account.receivable_fen == 100
    assert account.unpaid_dividend_tax_base_fen == 0
    assert account.deferred_tax_base_fen == 0
    assert account.outstanding_tax_fen == 20
    account.process_day(date(2026, 1, 6), (action,), None)
    account.process_day(date(2026, 1, 10), (action,), None)

    assert account.receivable_fen == 0
    assert account.unpaid_dividend_tax_base_fen == 0
    assert account.deferred_tax_base_fen == 0
    assert account.outstanding_tax_fen == 0
    assert account.closed_trades[0]["net_pnl_fen"] == 80
    _assert_event_state_reconciles(account)
