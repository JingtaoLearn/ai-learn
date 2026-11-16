"""Since the user started tracking: your actual result vs following the strategy vs holding still."""
from __future__ import annotations

import numpy as np

from .portfolio import ledger


def _index_on_or_after(dates: list[str], day: str) -> int | None:
    return next((i for i, d in enumerate(dates) if d >= day), None)


def since_start(dates: list[str], open_: np.ndarray, close: np.ndarray, model_pos: np.ndarray,
                trades: list[dict], k: float, buy_bps: float, sell_bps: float,
                full_amount: float | None) -> dict | None:
    """Three equity paths in the traded vehicle's money, all starting from the same capital N.

    k converts signal prices (index points) to the vehicle (ETF); trades are in vehicle prices.
    Start = the latest declared state, or the first trade if nothing was declared.
    """
    if not trades:
        return None
    ordered = sorted(trades, key=lambda t: (t["date"], t["id"]))
    inits = [t for t in ordered if t["side"] == "INIT"]
    start_trade = inits[-1] if inits else ordered[0]
    s = _index_on_or_after(dates, start_trade["date"])
    if s is None:  # declared after the last bar: nothing to compare yet
        return {"start": start_trade["date"], "days": 0}
    px = close * k
    before = [t for t in ordered if (t["date"], t["id"]) <= (start_trade["date"], start_trade["id"])]
    q0 = ledger(before)["qty"] if start_trade["side"] == "INIT" else 0.0
    v0 = q0 * px[s]
    n = max(v0, full_amount or 0.0) or (start_trade["qty"] or 0) * start_trade["price"]
    if n <= 0:
        return None
    later = [t for t in ordered if (t["date"], t["id"]) > (start_trade["date"], start_trade["id"])]
    if start_trade["side"] != "INIT":
        later = ordered

    span = range(s, len(dates))
    # actual: holdings from the ledger replayed day by day, cash from trades
    actual, cash, qty, j = [], n - v0, q0, 0
    for i in span:
        while j < len(later) and later[j]["date"] <= dates[i]:
            t = later[j]
            q = t["qty"] if t["side"] != "SELL" or t["qty"] else qty
            if t["side"] == "BUY":
                qty, cash = qty + q, cash - q * t["price"]
            elif t["side"] == "SELL":
                q = min(q, qty)
                qty, cash = qty - q, cash + q * t["price"]
            else:  # a later declared state replaces holdings at that day's value
                cash += qty * px[i] - (t["qty"] or 0) * px[i]
                qty = t["qty"] or 0
            j += 1
        actual.append(cash + qty * px[i])

    # hold still: the starting holding, untouched
    hold = [n - v0 + q0 * px[i] for i in span]

    # follow: start at your exposure, then the model's target from the next open
    x = v0 / n
    eq, follow = n, [n]
    for i in list(span)[1:]:
        target = float(model_pos[i - 1])
        on_night = 1 + x * (open_[i] / close[i - 1] - 1)
        cost = (target - x) * buy_bps / 1e4 if target > x else (x - target) * sell_bps / 1e4
        eq *= on_night * (1 - cost) * (1 + target * (close[i] / open_[i] - 1))
        x = target
        follow.append(eq)

    def res(path):
        return {"pnl": path[-1] - n, "pct": path[-1] / n - 1}

    switches = [dates[i] for i in list(span)[1:] if model_pos[i] != model_pos[i - 1]]
    return {"start": dates[s], "days": len(span) - 1, "capital": n,
            "actual": res(actual), "follow": res(follow), "hold": res(hold),
            "dates": [dates[i] for i in span],
            "paths": {"actual": [round(v, 2) for v in actual], "follow": [round(v, 2) for v in follow],
                      "hold": [round(v, 2) for v in hold]},
            "model_switches": switches}
