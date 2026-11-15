from datetime import date, timedelta

import numpy as np
import pytest

from quant import backtest, engine, optimize
from quant.engine import Params

from . import reference_v1 as v1


def synthetic(n=900, seed=7):
    rng = np.random.default_rng(seed)
    drift = np.sin(np.arange(n) / 60) * 0.004
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.012, n)))
    open_ = close * np.exp(rng.normal(0, 0.004, n))
    dates = [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)]
    return dates, open_, close


@pytest.mark.parametrize("params,start", [(Params(55, 1, 0.175, -0.275), 300), (Params(20, 5, 0.2, -0.2), 120),
                                          (Params(30, 3, 0.05, -0.1), 0)])
def test_matches_retired_production_signal(params, start):
    dates, _, close = synthetic()
    cfg = v1.TrendConfig(params.window, params.ema, params.buy, params.sell, date.fromisoformat(dates[start]))
    ends = list(range(max(params.window, start) + 5, len(close), 23))
    for end in ends:
        c = close[:end]
        rows = [{"date": date.fromisoformat(d), "signal_close": x, "close": x} for d, x in zip(dates, c)]
        old = v1.evaluate(v1.decision_points(rows, cfg), cfg)
        new = engine.snapshot(c, params, start)
        assert new["action"] == old["action"]
        assert new["position"] == old["target_state"]
        assert new["slope"] == pytest.approx(old["next_slope_pct"], abs=1e-9)
        if end not in ends[-3:]:  # v1 bisection is slow; spot-check triggers
            continue
        if new["buy_trigger"]:
            assert new["buy_trigger"] == pytest.approx(v1.close_for_slope(rows, cfg, params.buy), rel=1e-7)
        if new["sell_trigger"]:
            assert new["sell_trigger"] == pytest.approx(v1.close_for_slope(rows, cfg, params.sell), rel=1e-7)


def test_trigger_price_flips_projected_slope():
    _, _, close = synthetic()
    p = Params(20, 5, 0.2, -0.2)
    px = engine.trigger_close(close, p, p.buy)
    assert engine.projected_slope(close, p, px * 1.0001) > p.buy > engine.projected_slope(close, p, px * 0.9999)


def test_backtest_trades_at_next_open_with_costs():
    open_ = np.array([10, 10, 11, 12, 12, 11.0])
    close = np.array([10, 10, 12, 12, 11, 11.0])
    pos = np.array([0, 1, 1, 0, 0, 0], bool)  # buy decided at close 1 -> open 2; sell at close 3 -> open 4
    r = backtest.daily_returns(open_, close, pos, 10, 10)
    assert np.prod(1 + r) == pytest.approx((12 / 11) * (12 / 12) * (12 / 12) * (1 - 1e-3) ** 2)
    t = backtest.trades([str(i) for i in range(6)], open_, pos, 10, 10, 11)
    assert t == [{"entry_date": "2", "entry": 11 * 1.001, "exit_date": "4", "exit": 12 * 0.999,
                  "return": pytest.approx(12 * 0.999 / (11 * 1.001) - 1), "open": False}]


def test_vectorised_positions_equal_single_runs():
    _, _, close = synthetic()
    slope = engine.slope_series(close, 25, 3)
    buys, sells = np.array([0.05, 0.2, 0.3]), np.array([-0.1, -0.2, -0.05])
    many = engine.positions(slope, engine.first_eligible(slope, 50), buys, sells)
    for k in range(3):
        _, one = engine.single_positions(close, Params(25, 3, buys[k], sells[k]), 50)
        assert (many[:, k] == one).all()


def test_optimizer_walk_forward_shape():
    dates, open_, close = synthetic(1200)
    grid = {"window": [15, 30], "ema": [1, 3], "buy": [0.05, 0.15], "sell": [-0.05, -0.15]}
    res = optimize.run(dates, open_.tolist(), close.tolist(), 100, 5, 5, grid, "sharpe", 4,
                       {"window": 20, "ema": 5, "buy": 0.2, "sell": -0.2})
    assert res["combos"] == 16 and len(res["walk_forward"]["folds"]) == 4
    assert len(res["walk_forward"]["equity"]) == len(res["walk_forward"]["dates"])
    assert res["recommended"]["window"] in (15, 30)
