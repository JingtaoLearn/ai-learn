import math

import numpy as np
import pandas as pd
import pytest

from quant_platform.strategy_operators import (
    HysteresisDecision,
    adjacent_curve_pct_slope,
    all_in_quantity,
    cms_cost_breakdown,
    prior_log_ols,
    recursive_log_ema,
)


def test_prior_log_ols_uses_only_prior_window_and_extrapolates_one_session():
    index = pd.date_range("2026-01-01", periods=6, freq="D")
    prices = pd.Series(np.exp(np.arange(6) * 0.1), index=index)

    result = prior_log_ols(prices, window_sessions=3)

    assert result.loc[index[3], "history_start"] == index[0]
    assert result.loc[index[3], "history_end"] == index[2]
    assert result.loc[index[3], "curve"] == pytest.approx(math.exp(0.3))
    assert pd.isna(result.loc[index[2], "curve"])


def test_recursive_log_ema_and_adjacent_percent_slope_are_causal():
    values = pd.Series([100.0, 121.0, 144.0])

    smooth = recursive_log_ema(values, span_sessions=3)
    slope = adjacent_curve_pct_slope(smooth)

    assert smooth.iloc[0] == pytest.approx(100.0)
    assert smooth.iloc[1] == pytest.approx(110.0)
    assert smooth.iloc[2] == pytest.approx(math.sqrt(110.0 * 144.0))
    assert pd.isna(slope.iloc[0])
    assert slope.iloc[1] == pytest.approx(10.0)


def test_hysteresis_waits_for_crossings_and_ignores_sell_while_flat():
    decision = HysteresisDecision(
        buy_threshold_pct_per_day=0.2,
        sell_threshold_abs_pct_per_day=0.2,
    )

    first = decision.step(0.3)
    ignored_sell = decision.step(-0.3)
    no_buy = decision.step(0.1)
    buy = decision.step(0.2)
    hold = decision.step(-0.1)
    sell = decision.step(-0.2)

    assert (first.action, first.reason, first.position) == (
        "HOLD",
        "INITIALIZE_ZONE",
        0,
    )
    assert (ignored_sell.action, ignored_sell.position) == ("HOLD", 0)
    assert (no_buy.action, no_buy.position) == ("HOLD", 0)
    assert (buy.action, buy.position) == ("BUY", 1)
    assert (hold.action, hold.position) == ("HOLD", 1)
    assert (sell.action, sell.position) == ("SELL", 0)


def test_cms_cost_breakdown_is_itemized_and_exact():
    buy = cms_cost_breakdown(
        side="BUY",
        raw_price=10.0,
        quantity=1000,
        commission_rate=0.0003,
        minimum_commission_cny=5.0,
        transfer_fee_rate=0.00001,
        sell_stamp_tax_rate=0.0005,
        buy_slippage_bps=2.0,
        sell_slippage_bps=3.0,
    )
    sell = cms_cost_breakdown(
        side="SELL",
        raw_price=10.0,
        quantity=1000,
        commission_rate=0.0003,
        minimum_commission_cny=5.0,
        transfer_fee_rate=0.00001,
        sell_stamp_tax_rate=0.0005,
        buy_slippage_bps=2.0,
        sell_slippage_bps=3.0,
    )

    assert buy == {
        "commission_cny": 5.0,
        "transfer_fee_cny": 0.1,
        "stamp_tax_cny": 0.0,
        "slippage_cny": 2.0,
        "total_cost_cny": 7.1,
    }
    assert sell == {
        "commission_cny": 5.0,
        "transfer_fee_cny": 0.1,
        "stamp_tax_cny": 5.0,
        "slippage_cny": 3.0,
        "total_cost_cny": 13.1,
    }


def test_all_in_quantity_floors_board_lots_and_accounts_for_minimum_fee():
    cost_parameters = {
        "commission_rate": 0.0003,
        "minimum_commission_cny": 5.0,
        "transfer_fee_rate": 0.00001,
        "sell_stamp_tax_rate": 0.0005,
        "buy_slippage_bps": 2.0,
        "sell_slippage_bps": 2.0,
    }

    quantity = all_in_quantity(
        cash=10000.0,
        raw_price=10.0,
        lot_size=100,
        target_fraction=1.0,
        cost_parameters=cost_parameters,
    )
    insufficient = all_in_quantity(
        cash=1005.0,
        raw_price=10.0,
        lot_size=100,
        target_fraction=1.0,
        cost_parameters=cost_parameters,
    )

    assert quantity == 900
    assert insufficient == 0
