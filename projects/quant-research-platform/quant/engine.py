"""Trend signal: rolling log-OLS projection -> EMA -> slope (%/day) -> hysteresis crossings.

Indexing: slope[t] uses closes up to and including t; a crossing at close t trades at open t+1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Params:
    window: int
    ema: int
    buy: float  # %/day; long when slope crosses up through it
    sell: float  # %/day; flat when slope crosses down through it

    def as_dict(self) -> dict:
        return {"window": self.window, "ema": self.ema, "buy": self.buy, "sell": self.sell}


def _projection_weights(window: int) -> np.ndarray:
    # OLS fit over x=0..w-1, evaluated one step ahead at x=w; linear in y.
    x = np.arange(window, dtype=float)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()
    return 1.0 / window + (x - xm) * (window - xm) / denom


def raw_curve(log_close: np.ndarray, window: int) -> np.ndarray:
    """Projected log price per t (NaN before window-1)."""
    out = np.full(len(log_close), np.nan)
    if len(log_close) >= window:
        w = _projection_weights(window)
        out[window - 1 :] = np.convolve(log_close, w[::-1], mode="valid")
    return out


def ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.full(len(values), np.nan)
    acc = math.nan
    for i, v in enumerate(values):
        if math.isnan(v):
            continue
        acc = v if math.isnan(acc) else alpha * v + (1 - alpha) * acc
        out[i] = acc
    return out


def smooth_curve(close: np.ndarray, window: int, span: int) -> np.ndarray:
    return ema(raw_curve(np.log(close), window), span)


def slope_from_smooth(smooth: np.ndarray) -> np.ndarray:
    out = np.full(len(smooth), np.nan)
    out[1:] = 100.0 * np.expm1(np.diff(smooth))
    return out


def slope_series(close: np.ndarray, window: int, span: int) -> np.ndarray:
    return slope_from_smooth(smooth_curve(close, window, span))


def first_eligible(slope: np.ndarray, start_index: int) -> int:
    """First t whose slope may seed the state machine (decision session t+1 >= start)."""
    t = max(start_index - 1, 0)
    while t < len(slope) and math.isnan(slope[t]):
        t += 1
    return t


def positions(slope: np.ndarray, t0: int, buys: np.ndarray, sells: np.ndarray) -> np.ndarray:
    """Target position after close t, for k threshold pairs at once -> bool[n, k]."""
    buys = np.atleast_1d(np.asarray(buys, float))
    sells = np.atleast_1d(np.asarray(sells, float))
    n, k = len(slope), len(buys)
    out = np.zeros((n, k), dtype=bool)
    if t0 >= n:
        return out
    state = np.zeros(k, dtype=bool)
    prior = slope[t0]
    for t in range(t0 + 1, n):
        s = slope[t]
        up = ~state & (prior < buys) & (buys <= s)
        down = state & (prior > sells) & (sells >= s)
        state = (state | up) & ~down
        out[t] = state
        prior = s
    return out


def single_positions(close: np.ndarray, params: Params, start_index: int = 0) -> tuple[np.ndarray, np.ndarray]:
    slope = slope_series(close, params.window, params.ema)
    t0 = first_eligible(slope, start_index)
    pos = positions(slope, t0, np.array([params.buy]), np.array([params.sell]))[:, 0]
    return slope, pos


def trigger_close(close: np.ndarray, params: Params, target_slope: float) -> float:
    """Next close at which the next slope would equal target_slope (closed form)."""
    w, alpha = params.window, 2.0 / (params.ema + 1.0)
    smooth = smooth_curve(close, w, params.ema)
    prev = smooth[-1]
    weights = _projection_weights(w)
    known = float(np.dot(weights[:-1], np.log(close[-(w - 1) :])))
    target_smooth = prev + math.log1p(target_slope / 100.0)
    target_raw = (target_smooth - (1 - alpha) * prev) / alpha
    return math.exp((target_raw - known) / weights[-1])


def projected_slope(close: np.ndarray, params: Params, candidate: float) -> float:
    return float(slope_series(np.append(close, candidate), params.window, params.ema)[-1])


def snapshot(close: np.ndarray, params: Params, start_index: int = 0) -> dict:
    """Current state after the latest close plus next-close trigger prices."""
    slope, pos = single_positions(close, params, start_index)
    held_before = bool(pos[-2]) if len(pos) > 1 else False
    held = bool(pos[-1])
    if held and not held_before:
        action = "BUY"
    elif held_before and not held:
        action = "SELL"
    else:
        action = "HOLD" if held else "WAIT"
    s = float(slope[-1])
    # Line prices ignore the model's state: a user who holds against the model still needs a sell line.
    buy_line = trigger_close(close, params, params.buy) if s < params.buy else None
    sell_line = trigger_close(close, params, params.sell) if s > params.sell else None
    t0 = first_eligible(slope, start_index)
    prev, cur = slope[t0:-1], slope[t0 + 1:]
    ups = np.nonzero((prev < params.buy) & (params.buy <= cur))[0]
    downs = np.nonzero((prev > params.sell) & (params.sell >= cur))[0]
    return {
        "action": action,
        "position": int(held),
        "slope": s,
        "prev_slope": float(slope[-2]),
        # the model's own next trigger only exists on the side its state machine is on
        "buy_trigger": buy_line if not held else None,
        "sell_trigger": sell_line if held else None,
        "buy_line_price": buy_line,
        "sell_line_price": sell_line,
        "buy_cross_index": int(t0 + 1 + ups[-1]) if len(ups) else None,
        "sell_cross_index": int(t0 + 1 + downs[-1]) if len(downs) else None,
    }
