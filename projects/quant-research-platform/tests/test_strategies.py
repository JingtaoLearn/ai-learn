import numpy as np
import pandas as pd

from gold_research.strategies import buy_hold_signal, donchian_signal, sma_signal


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
