"""Long/flat backtest: decide at close t, trade at open t+1, costs in bps per side."""
from __future__ import annotations

import math

import numpy as np

YEAR = 252


def daily_returns(open_: np.ndarray, close: np.ndarray, pos: np.ndarray,
                  buy_bps: float, sell_bps: float) -> np.ndarray:
    """pos: bool[n] or bool[n, k] target after close t. Returns r[n(, k)] with r[0]=0."""
    pos = pos.astype(float)
    if pos.ndim == 1:
        pos = pos[:, None]
        squeeze = True
    else:
        squeeze = False
    held = np.zeros_like(pos)  # held during session d (from open d)
    held[1:] = pos[:-1]
    overnight = np.ones(len(close))
    overnight[1:] = open_[1:] / close[:-1]
    intraday = close / open_
    prev_held = np.zeros_like(held)
    prev_held[1:] = held[:-1]
    growth = (1 + prev_held * (overnight[:, None] - 1)) * (1 + held * (intraday[:, None] - 1))
    change = held - prev_held
    cost = np.where(change > 0, buy_bps, 0.0) + np.where(change < 0, sell_bps, 0.0)
    growth *= 1 - cost / 1e4
    r = growth - 1
    return r[:, 0] if squeeze else r


def score(r: np.ndarray, objective: str = "sharpe") -> np.ndarray:
    """Column-wise objective over daily returns r[n, k]."""
    r = np.atleast_2d(r.T).T
    with np.errstate(divide="ignore", invalid="ignore"):
        if objective == "sharpe":
            s = r.mean(0) / r.std(0) * math.sqrt(YEAR)
        else:
            equity = np.cumprod(1 + r, axis=0)
            years = max(len(r) / YEAR, 1e-9)
            cagr = equity[-1] ** (1 / years) - 1
            if objective == "cagr":
                s = cagr
            else:  # calmar
                mdd = (equity / np.maximum.accumulate(equity, axis=0) - 1).min(0)
                s = cagr / np.maximum(-mdd, 0.01)
    return np.where(np.isfinite(s), s, -np.inf)


def metrics(r: np.ndarray, pos: np.ndarray | None = None) -> dict:
    equity = np.cumprod(1 + r)
    years = len(r) / YEAR
    total = equity[-1] - 1
    cagr = equity[-1] ** (1 / years) - 1 if years > 0 else 0.0
    dd = equity / np.maximum.accumulate(equity) - 1
    std = r.std()
    out = {
        "total_return": float(total),
        "cagr": float(cagr),
        "max_drawdown": float(dd.min()),
        "volatility": float(std * math.sqrt(YEAR)),
        "sharpe": float(r.mean() / std * math.sqrt(YEAR)) if std > 0 else 0.0,
    }
    if pos is not None:
        out["exposure"] = float(pos.mean())
    return out


def trades(dates: list[str], open_: np.ndarray, pos: np.ndarray,
           buy_bps: float, sell_bps: float, last_close: float) -> list[dict]:
    """Round trips executed at next open; an open trade is marked at the last close."""
    out: list[dict] = []
    entry = None
    for t in range(len(pos) - 1):
        if pos[t] and (t == 0 or not pos[t - 1]):
            entry = (dates[t + 1], open_[t + 1] * (1 + buy_bps / 1e4))
        elif not pos[t] and t > 0 and pos[t - 1] and entry:
            px = open_[t + 1] * (1 - sell_bps / 1e4)
            out.append({"entry_date": entry[0], "entry": entry[1], "exit_date": dates[t + 1],
                        "exit": px, "return": px / entry[1] - 1, "open": False})
            entry = None
    if entry:  # still holding; a buy decided at the last close is not executed yet
        out.append({"entry_date": entry[0], "entry": entry[1], "exit_date": None,
                    "exit": last_close, "return": last_close / entry[1] - 1, "open": True})
    return out


def _mdd(r: np.ndarray) -> float:
    eq = np.cumprod(1 + r)
    return float((eq / np.maximum.accumulate(np.maximum(eq, 1.0)) - 1).min())


def periods(dates: list[str], r: np.ndarray, bh: np.ndarray, pos: np.ndarray, trade_list: list[dict]) -> list[dict]:
    """Calendar-year breakdown; drawdowns are measured from each year's opening value."""
    out = []
    years = sorted({d[:4] for d in dates})
    for y in years:
        idx = [i for i, d in enumerate(dates) if d.startswith(y)]
        a, b = idx[0], idx[-1] + 1
        out.append({
            "period": y, "start": dates[a], "end": dates[b - 1],
            "strategy": float(np.prod(1 + r[a:b]) - 1), "buy_hold": float(np.prod(1 + bh[a:b]) - 1),
            "strategy_mdd": _mdd(r[a:b]), "buy_hold_mdd": _mdd(bh[a:b]),
            "exposure": float(pos[a:b].mean()),
            "trades": sum(1 for t in trade_list if not t["open"] and t["exit_date"].startswith(y)),
        })
    return out


def run(dates: list[str], open_: np.ndarray, close: np.ndarray, pos: np.ndarray,
        buy_bps: float, sell_bps: float, start_index: int = 0) -> dict:
    """Full single-strategy report from start_index onward, with buy & hold baseline."""
    sl = slice(start_index, None)
    d, o, c, p = dates[sl], open_[sl], close[sl], pos[sl]
    r = daily_returns(o, c, p, buy_bps, sell_bps)
    bh = np.zeros(len(c))
    bh[1:] = c[1:] / c[:-1] - 1
    all_trades = trades(d, o, p, buy_bps, sell_bps, float(c[-1]))
    closed = [x for x in all_trades if not x["open"]]
    wins = [x for x in closed if x["return"] > 0]
    return {
        "strategy": {**metrics(r, p), "trades": len(closed),
                     "win_rate": len(wins) / len(closed) if closed else None},
        "buy_hold": metrics(bh),
        "equity": np.cumprod(1 + r).round(5).tolist(),
        "buy_hold_equity": np.cumprod(1 + bh).round(5).tolist(),
        "dates": d,
        "trades": all_trades,
        "periods": periods(d, r, bh, p, all_trades),
    }
