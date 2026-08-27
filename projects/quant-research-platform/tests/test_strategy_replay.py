from pathlib import Path

import pandas as pd
import pytest

from quant_platform.strategy_config import validate_strategy_config
from quant_platform.strategy_replay import ReplayError, replay_strategy


FIXTURE = Path(__file__).parent / "fixtures" / "strategy" / "daily.csv"


def _config(*, terminal_handling: str = "mark_to_market", capital: float = 100000.0):
    return validate_strategy_config(
        {
            "schema_version": 1,
            "dataset": {"root": "unused", "snapshot_id": "a" * 64},
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
    assert events["total_cost_cny"].sum() == pytest.approx(
        daily["total_cost_cny"].sum()
    )
    assert result.cost_breakdown["total_cost_cny"] == pytest.approx(
        events["total_cost_cny"].sum()
    )


def test_open_terminal_trade_has_entry_cost_only_and_is_not_a_closed_win():
    result = replay_strategy(_frame(), _config())

    assert result.trades["status"].tolist() == ["CLOSED", "OPEN"]
    open_trade = result.trades.iloc[-1]
    assert pd.isna(open_trade["exit_date"])
    assert open_trade["exit_cost_cny"] == 0.0
    assert result.metrics["closed_trades"] == 1
    assert result.metrics["open_trades"] == 1
    assert result.metrics["closed_trade_win_rate"] in {0.0, 1.0}
    assert result.metrics["current_position"] == "LONG"


def test_optional_terminal_liquidation_sells_all_at_final_raw_open():
    result = replay_strategy(_frame(), _config(terminal_handling="force_liquidate"))

    assert result.events.iloc[-1]["side"] == "SELL"
    assert result.events.iloc[-1]["Date"] == pd.Timestamp("2026-01-12")
    assert result.events.iloc[-1]["price"] == 10.5
    assert result.daily.iloc[-1]["holdings"] == 0
    assert result.trades["status"].tolist() == ["CLOSED", "CLOSED"]
    assert result.metrics["open_trades"] == 0
    assert result.metrics["current_position"] == "FLAT"


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
    assert metrics["zero_cost_final_equity_cny"] == pytest.approx(
        final["zero_cost_equity"]
    )
    assert metrics["buy_hold_final_equity_cny"] == pytest.approx(
        final["buy_hold_equity"]
    )
    assert result.reconciliation == {
        "daily_equity": True,
        "event_cash": True,
        "event_positions": True,
        "event_costs": True,
        "trade_events": True,
        "profit_identity": True,
    }


def test_insufficient_cash_records_no_event_and_stays_flat():
    result = replay_strategy(_frame(), _config(capital=1005.0))

    buy_day = result.daily.loc[result.daily["decision"] == "BUY"].iloc[0]
    assert buy_day["reason"] == "INSUFFICIENT_CASH"
    assert buy_day["position_after"] == 0
    assert not (
        result.events["Date"] == pd.Timestamp("2026-01-07")
    ).any()


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
