import numpy as np
import pandas as pd
import pytest

from gold_research.backtest import backtest, metrics, chronological_split


def sample(n=300):
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(100 * np.cumprod(np.repeat(1.001, n)), index=idx, name="Close")


def test_costs_are_reported_before_and_after_and_reduce_return():
    p = sample()
    sig = pd.Series(np.tile([0.0, 1.0], len(p) // 2), index=p.index)
    result = backtest(p, sig, cost_bps=5)
    assert {"gross_return", "net_return", "equity_gross", "equity_net", "cost"} <= set(result)
    assert result["net_return"].sum() < result["gross_return"].sum()
    assert result["cost"].sum() > 0


def test_asymmetric_costs_charge_buy_and_sell_turnover_separately():
    index = pd.bdate_range("2024-01-02", periods=5)
    opens = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0], index=index)
    signal = pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=index)
    result = backtest(opens, signal, buy_cost_bps=8.0, sell_cost_bps=13.0)
    assert result["buy_turnover"].sum() == pytest.approx(1.0)
    assert result["sell_turnover"].sum() == pytest.approx(1.0)
    assert result["cost"].sum() == pytest.approx(0.0021)


@pytest.mark.parametrize(
    ("buy_cost_bps", "sell_cost_bps"),
    [(-1.0, 5.0), (5.0, -1.0), (np.nan, 5.0), (5.0, np.inf), (True, 5.0)],
)
def test_backtest_rejects_invalid_asymmetric_costs(buy_cost_bps, sell_cost_bps):
    p = sample(10)
    with pytest.raises(ValueError, match="cost"):
        backtest(
            p,
            pd.Series(1.0, index=p.index),
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
        )


def test_trade_ledger_uses_attainable_next_open_execution():
    from gold_research.backtest import trade_ledger

    idx = pd.bdate_range("2024-01-01", periods=5)
    opens = pd.Series([100.0, 110.0, 121.0, 133.1, 146.41], index=idx)
    signal = pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=idx)
    ledger = trade_ledger(backtest(opens, signal, cost_bps=0))
    trade = ledger.iloc[0]
    assert trade["entry_date"] == idx[1]
    assert trade["entry_price"] == 110.0
    assert trade["exit_date"] == idx[3]
    assert trade["exit_price"] == 133.1
    assert trade["net_return"] == pytest.approx(0.21)
    assert not bool(trade["is_open"])


def test_open_trade_is_not_counted_as_closed_or_charged_exit_cost():
    from gold_research.backtest import trade_ledger

    idx = pd.bdate_range("2024-01-01", periods=4)
    opens = pd.Series([100.0, 110.0, 121.0, 133.1], index=idx)
    signal = pd.Series([0.0, 1.0, 1.0, 1.0], index=idx)
    result = backtest(opens, signal, cost_bps=5)
    ledger = trade_ledger(result)
    assert len(ledger) == 1 and bool(ledger.iloc[0]["is_open"])
    assert ledger.iloc[0]["bars"] == 2
    got = metrics(result)
    assert got["trade_count"] == 0
    assert got["open_trade_count"] == 1
    assert result["cost"].sum() == pytest.approx(0.0005)


def test_fractional_rebalancing_does_not_create_phantom_round_trips():
    from gold_research.backtest import trade_ledger

    idx = pd.bdate_range("2024-01-01", periods=7)
    opens = pd.Series([100, 101, 102, 103, 104, 105, 106], index=idx, dtype=float)
    signal = pd.Series([0.0, 0.4, 0.6, 0.5, 0.8, 0.0, 0.0], index=idx)
    ledger = trade_ledger(backtest(opens, signal, cost_bps=0))
    assert len(ledger) == 1
    assert ledger.iloc[0]["entry_date"] == idx[1]
    assert ledger.iloc[0]["exit_date"] == idx[5]
    assert not bool(ledger.iloc[0]["is_open"])


def test_max_drawdown_includes_starting_capital_and_sortino_uses_target_downside():
    idx = pd.bdate_range("2024-01-01", periods=3)
    opens = pd.Series([100.0, 80.0, 80.0], index=idx)
    got = metrics(backtest(opens, pd.Series(1.0, index=idx), cost_bps=0))
    assert got["max_drawdown"] == pytest.approx(-0.20)

    returns = pd.Series([0.01, -0.02, 0.03], index=idx)
    result = pd.DataFrame({
        "open": [100.0, 101.0, 99.0],
        "signal": [0.0, 0.0, 0.0],
        "net_return": returns,
        "equity_net": (1 + returns).cumprod(),
    }, index=idx)
    downside_deviation = np.sqrt(np.mean(np.minimum(returns, 0.0) ** 2))
    expected = returns.mean() / downside_deviation * np.sqrt(252)
    assert metrics(result)["sortino"] == pytest.approx(expected)


def test_metrics_include_required_fields():
    p = sample()
    result = backtest(p, pd.Series(1.0, index=p.index), cost_bps=5)
    got = metrics(result)
    required = {"cagr", "cumulative_return", "max_drawdown", "annual_volatility", "sharpe", "sortino", "calmar", "trade_count", "win_rate", "market_exposure"}
    assert required <= set(got)
    assert 0 <= got["market_exposure"] <= 1


def test_split_is_chronological_70_30():
    p = sample(100)
    research, out = chronological_split(p, 0.7)
    assert len(research) == 70 and len(out) == 30
    assert research.index.max() < out.index.min()
