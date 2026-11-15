"""Grid search with neighbour-smoothed scores and expanding walk-forward validation."""
from __future__ import annotations

import itertools
import time

import numpy as np

from . import backtest, engine
from .engine import Params

DEFAULT_GRID = {
    "window": [10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120],
    "ema": [1, 2, 3, 5, 8, 13],
    "buy": [0.02, 0.04, 0.06, 0.08, 0.1, 0.125, 0.15, 0.175, 0.2, 0.25, 0.3, 0.4],
    "sell": [-0.02, -0.04, -0.06, -0.08, -0.1, -0.125, -0.15, -0.175, -0.2, -0.25, -0.3, -0.4],
}


def _prefix_scores(r: np.ndarray, ends: list[int], objective: str) -> np.ndarray:
    """Objective of r[:end] for each end -> [len(ends), k]."""
    out = np.empty((len(ends), r.shape[1]))
    if objective == "sharpe":
        c1, c2 = np.cumsum(r, 0), np.cumsum(r * r, 0)
        for j, e in enumerate(ends):
            mean = c1[e - 1] / e
            std = np.sqrt(np.maximum(c2[e - 1] / e - mean**2, 0))
            with np.errstate(divide="ignore", invalid="ignore"):
                out[j] = mean / std * np.sqrt(backtest.YEAR)
    else:
        eq = np.cumprod(1 + r, 0)
        mdd = np.minimum.accumulate(eq / np.maximum.accumulate(eq, 0) - 1, 0)
        for j, e in enumerate(ends):
            cagr = eq[e - 1] ** (backtest.YEAR / e) - 1
            out[j] = cagr if objective == "cagr" else cagr / np.maximum(-mdd[e - 1], 0.01)
    return np.where(np.isfinite(out), out, np.nan)


def _smooth(scores: np.ndarray) -> np.ndarray:
    """Mean over the +-1 neighbourhood on every parameter axis (last axis = boundary)."""
    axes = scores.ndim - 1
    padded = np.pad(scores, [(1, 1)] * axes + [(0, 0)], constant_values=np.nan)
    total = np.zeros_like(scores)
    count = np.zeros_like(scores)
    for offset in itertools.product(range(3), repeat=axes):
        view = padded[tuple(slice(o, o + s) for o, s in zip(offset, scores.shape[:-1]))]
        valid = ~np.isnan(view)
        total += np.where(valid, view, 0)
        count += valid
    with np.errstate(invalid="ignore"):
        return np.where(count > 0, total / count, np.nan)


def _argbest(grid: dict, smoothed: np.ndarray) -> Params:
    filled = np.where(np.isnan(smoothed), -np.inf, smoothed)
    i, j, b, s = np.unravel_index(int(np.argmax(filled)), filled.shape)
    return Params(int(grid["window"][i]), int(grid["ema"][j]),
                  float(grid["buy"][b]), float(grid["sell"][s]))


def run(dates: list[str], open_: list[float], close: list[float], start_index: int,
        buy_bps: float, sell_bps: float, grid: dict | None = None,
        objective: str = "sharpe", folds: int = 5, current: dict | None = None) -> dict:
    t_start = time.time()
    grid = {k: list(v) for k, v in (grid or DEFAULT_GRID).items()}
    o, c = np.asarray(open_, float), np.asarray(close, float)
    n_eval = len(c) - start_index
    if n_eval < 250:
        raise ValueError("need at least one year of history after the start date")
    # Expanding train / rolling test: the last 60% is split into `folds` test blocks.
    first_test = int(n_eval * 0.4)
    edges = np.linspace(first_test, n_eval, folds + 1).astype(int).tolist()
    ends = edges[:-1] + [n_eval]  # train ends per fold, then full sample

    pairs = list(itertools.product(grid["buy"], grid["sell"]))
    buys = np.array([p[0] for p in pairs])
    sells = np.array([p[1] for p in pairs])
    shape = (len(grid["window"]), len(grid["ema"]), len(grid["buy"]), len(grid["sell"]), len(ends))
    scores = np.full(shape, np.nan)
    for i, w in enumerate(grid["window"]):
        for j, e in enumerate(grid["ema"]):
            slope = engine.slope_series(c, w, e)
            pos = engine.positions(slope, engine.first_eligible(slope, start_index), buys, sells)
            r = backtest.daily_returns(o, c, pos, buy_bps, sell_bps)[start_index:]
            ps = _prefix_scores(r, ends, objective)  # [len(ends), k]
            scores[i, j] = ps.T.reshape(len(grid["buy"]), len(grid["sell"]), len(ends))
    smoothed = _smooth(scores)

    def returns_for(p: Params) -> np.ndarray:
        _, pos = engine.single_positions(c, p, start_index)
        return backtest.daily_returns(o, c, pos, buy_bps, sell_bps)[start_index:]

    bh = np.zeros(n_eval)
    bh[1:] = c[start_index + 1:] / c[start_index:-1] - 1
    cur = Params(**current) if current else None
    cur_r = returns_for(cur) if cur else None
    fold_rows, oos = [], []
    for f in range(folds):
        a, b = edges[f], edges[f + 1]
        chosen = _argbest(grid, smoothed[..., f])
        _, pos = engine.single_positions(c, chosen, start_index)
        seg = backtest.daily_returns(o, c, pos, buy_bps, sell_bps)[start_index:][a:b]
        flips = int(np.abs(np.diff(pos[start_index:][a:b].astype(int))).sum())
        oos.append(seg)
        fold_rows.append({
            "test_start": dates[start_index + a], "test_end": dates[start_index + b - 1],
            "params": chosen.as_dict(),
            "strategy": backtest.metrics(seg), "buy_hold": backtest.metrics(bh[a:b]),
            "current": backtest.metrics(cur_r[a:b]) if cur else None,
            "trades": flips,  # buy + sell executions within the block
        })
    oos_r = np.concatenate(oos)
    best = _argbest(grid, smoothed[..., -1])
    full = smoothed[..., -1]
    order = np.argsort(np.where(np.isnan(full), -np.inf, full), axis=None)[::-1][:15]
    top = []
    for flat in order:
        i, j, b, s = np.unravel_index(int(flat), full.shape)
        top.append({"params": Params(int(grid["window"][i]), int(grid["ema"][j]),
                                     float(grid["buy"][b]), float(grid["sell"][s])).as_dict(),
                    "smoothed": float(full[i, j, b, s]), "raw": float(scores[i, j, b, s, -1])})
    heat = np.nanmax(np.where(np.isnan(full), -np.inf, full), axis=(2, 3))
    oos_dates = dates[start_index + edges[0]: start_index + n_eval]
    return {
        "objective": objective,
        "combos": int(np.prod(shape[:-1])),
        "seconds": round(time.time() - t_start, 2),
        "recommended": best.as_dict(),
        "recommended_full": backtest.metrics(returns_for(best)),
        "current_full": backtest.metrics(cur_r) if cur else None,
        "buy_hold_full": backtest.metrics(bh),
        "walk_forward": {
            "folds": fold_rows,
            "strategy": backtest.metrics(oos_r),
            "buy_hold": backtest.metrics(bh[edges[0]:]),
            "current": backtest.metrics(cur_r[edges[0]:]) if cur else None,
            "dates": oos_dates,
            "equity": np.cumprod(1 + oos_r).round(5).tolist(),
            "buy_hold_equity": np.cumprod(1 + bh[edges[0]:]).round(5).tolist(),
            "current_equity": np.cumprod(1 + cur_r[edges[0]:]).round(5).tolist() if cur else None,
        },
        "heatmap": {"window": grid["window"], "ema": grid["ema"],
                    "score": [[None if not np.isfinite(v) else round(float(v), 4) for v in row]
                              for row in heat]},
        "top": top,
    }
