"""The user's real holdings: quantity ledger, position size vs. the model target, signal feedback."""
from __future__ import annotations

import math

from .store import Store

ACTIONS = ("BUY", "SELL")
REBALANCE_GAP = 0.10  # ignore position gaps smaller than 10% of a full position


def ledger(trades: list[dict]) -> dict:
    """Average-cost ledger. A SELL without qty closes the whole position."""
    qty = cost = 0.0
    opened = None
    realized = []
    for t in sorted(trades, key=lambda x: (x["date"], x["id"])):
        if t["side"] == "BUY":
            q = t["qty"] or 0.0
            if qty < 1e-9:
                opened = t["date"]
            qty, cost = qty + q, cost + q * t["price"]
        elif qty > 0:
            q = min(t["qty"] or qty, qty)
            avg = cost / qty
            realized.append({"date": t["date"], "qty": q, "price": t["price"], "avg_cost": avg,
                             "return": t["price"] / avg - 1, "pnl": q * (t["price"] - avg), "opened": opened})
            qty, cost = qty - q, cost - q * avg
            if qty < 1e-9:
                qty = cost = 0.0
    return {"qty": qty, "cost": cost, "avg_cost": cost / qty if qty else None,
            "opened": opened if qty else None, "realized": realized}


def full_amount(inst: dict, price: float | None, total_capital: float | None, fx: float) -> float | None:
    """Value of a 100% position in the instrument's currency."""
    v = inst.get("full_value")
    if not v:
        return None
    mode = inst.get("full_mode") or "amount"
    if mode == "qty":
        return v * price if price else None
    if mode == "weight":
        return total_capital * v / 100 / fx if total_capital else None
    return v


def pending(store: Store, symbol: str) -> list[dict]:
    """Model BUY/SELL signals the user has not answered yet (newest first)."""
    answered = {(f["signal_date"], f["action"]) for f in store.feedback(symbol)}
    return [e for e in store.events(symbol, 200)
            if e["kind"] == "signal" and e["action"] in ACTIONS and (e["date"], e["action"]) not in answered]


def me(store: Store, inst: dict, signal: dict | None, fx: float = 1.0,
       total_capital: float | None = None) -> dict:
    """User-facing state for one instrument; prices are in the traded vehicle's units."""
    trades = store.trades(inst["symbol"])
    led = ledger(trades)
    close = (signal or {}).get("close")
    price = (inst.get("hold_price") if inst.get("hold_code") else close) or close
    out = {"tracked": bool(trades or store.feedback(inst["symbol"])), "trades": trades,
           "realized": led["realized"], "pending": pending(store, inst["symbol"]),
           "qty": led["qty"], "held": led["qty"] > 0, "avg_cost": led["avg_cost"], "opened": led["opened"],
           "price": price, "value": led["qty"] * price if price else None, "cost": led["cost"],
           "pnl": price / led["avg_cost"] - 1 if led["qty"] and price else None, "fx": fx}
    full = full_amount(inst, price, total_capital, fx)
    out["full_amount"] = full
    out["ratio"] = out["value"] / full if full and out["value"] is not None else None
    if not signal:
        return out
    target = 1.0 if signal["position"] else 0.0
    out["target"] = target
    ratio = out["ratio"] if out["ratio"] is not None else (1.0 if out["held"] else 0.0)
    gap = target - ratio
    if out["tracked"] and abs(gap) >= REBALANCE_GAP and price:
        lot = inst.get("lot") or 1
        if target == 0:
            q = led["qty"]  # model is flat: sell everything
        elif full:
            q = math.floor(abs(gap) * full / price / lot) * lot  # never overshoot a full position
        else:
            q = None  # full position not defined yet: can say "buy", not how much
        out["suggest"] = {"side": "BUY" if gap > 0 else "SELL", "qty": q,
                          "amount": q * price if q is not None else None}
    # A signal issued at the last close is tomorrow's to-do, not yet a divergence.
    if out["tracked"] and signal["action"] not in ACTIONS and "suggest" in out:
        out["divergence"] = "MODEL_LONG" if target else "MODEL_FLAT"
    return out
