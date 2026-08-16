import numpy as np
import pandas as pd

from gold_research.strategies import (
    absolute_momentum_signal,
    buy_hold_signal,
    donchian_signal,
    multi_horizon_momentum_signal,
    risk_managed_trend_signal,
    sma_signal,
    trend_filter_signal,
)


def prices(n=260):
    return pd.Series(100 + np.linspace(0, 30, n) + np.sin(np.arange(n) / 5), name="Close")


def test_buy_hold_enters_at_the_first_available_open():
    s = buy_hold_signal(prices(5))
    assert (s == 1).all()


def test_sma_signal_is_shifted_and_prefix_stable():
    p = prices()
    signal = sma_signal(p, 50, 200)
    expected = (p.rolling(50).mean() > p.rolling(200).mean()).astype(float).shift(1).fillna(0)
    pd.testing.assert_series_equal(signal, expected)
    pd.testing.assert_series_equal(signal.iloc[:230], sma_signal(p.iloc[:230], 50, 200))


def test_donchian_uses_prior_channels_and_delayed_execution():
    p = prices(120)
    signal = donchian_signal(p, 55, 20)
    assert signal.index.equals(p.index)
    assert set(signal.unique()) <= {0.0, 1.0}
    pd.testing.assert_series_equal(signal.iloc[:100], donchian_signal(p.iloc[:100], 55, 20))


def test_round2_signals_are_delayed_and_prefix_stable():
    p = prices(420)
    signals = [
        trend_filter_signal(p, 200),
        absolute_momentum_signal(p, 252),
        multi_horizon_momentum_signal(p, (63, 126, 252), 2),
        risk_managed_trend_signal(p, 200, 60, 0.10),
    ]
    prefixes = [
        trend_filter_signal(p.iloc[:360], 200),
        absolute_momentum_signal(p.iloc[:360], 252),
        multi_horizon_momentum_signal(p.iloc[:360], (63, 126, 252), 2),
        risk_managed_trend_signal(p.iloc[:360], 200, 60, 0.10),
    ]
    for signal, prefix in zip(signals, prefixes, strict=True):
        assert signal.index.equals(p.index)
        assert signal.between(0.0, 1.0).all()
        pd.testing.assert_series_equal(signal.iloc[:360], prefix)


def test_trend_and_momentum_do_not_use_current_close_for_current_open():
    p = prices(320)
    altered = p.copy()
    altered.iloc[-1] *= 100
    for function in [trend_filter_signal, absolute_momentum_signal]:
        original = function(p)
        changed = function(altered)
        assert changed.iloc[-1] == original.iloc[-1]


def test_risk_managed_trend_reduces_exposure_when_volatility_rises():
    idx = pd.bdate_range("2020-01-01", periods=360)
    calm = pd.Series(100 * np.cumprod(np.repeat(1.0002, 360)), index=idx)
    volatile = calm.copy()
    volatile.iloc[-80:] *= np.cumprod(np.tile([1.04, 0.97], 40))
    calm_signal = risk_managed_trend_signal(calm, 100, 60, 0.10)
    volatile_signal = risk_managed_trend_signal(volatile, 100, 60, 0.10)
    assert volatile_signal.iloc[-1] < calm_signal.iloc[-1]
