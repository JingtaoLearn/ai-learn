"""The user's real holdings and the advice derived from them.

State = latest declared state (INIT) + later trades. Advice compares that state with the model's
target (long/flat). A "didn't do it" answer is respected until a fresh line crossing (or a change
in holdings) gives a new reason to act.
"""
from __future__ import annotations

import math

from .store import Store

ACTIONS = ("BUY", "SELL")
REBALANCE_GAP = 0.10  # a position within 10% of the target counts as on target


def ledger(trades: list[dict]) -> dict:
    """Average-cost ledger. INIT declares the holding outright; a SELL without qty closes it all."""
    qty = cost = 0.0
    opened = changed = None
    realized = []
    for t in sorted(trades, key=lambda x: (x["date"], x["id"])):
        changed = t["date"]
        if t["side"] == "INIT":
            qty = t["qty"] or 0.0
            cost = qty * t["price"]
            opened = t["date"] if qty else None
        elif t["side"] == "BUY":
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
            "opened": opened if qty else None, "changed": changed, "realized": realized}


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


def advise(*, held: bool, ratio: float | None, target: int, today: str, changed: str | None,
           skips: dict, crosses: dict) -> dict:
    """What the user should do now, given their real position.

    skips: latest "didn't do it" date per side; crosses: latest slope crossing date per side
    (BUY = up through the buy line, SELL = down through the sell line).
    """
    if target and (not held or (ratio is not None and ratio < 1 - REBALANCE_GAP)):
        side = "BUY"
    elif not target and held:
        side = "SELL"
    else:
        return {"action": "HOLD" if held else "WAIT"}
    skip, cross = skips.get(side), crosses.get(side)
    dismissed = bool(skip) and skip >= (changed or "") and (not cross or cross <= skip)
    return {"action": ("HOLD" if held else "WAIT") if dismissed else side, "side": side,
            "dismissed": dismissed, "skipped_on": skip if dismissed else None,
            "topup": side == "BUY" and held, "new": cross == today}


def latest_skips(store: Store, symbol: str) -> dict:
    out: dict = {}
    for f in store.feedback(symbol):
        if f["response"] == "SKIPPED" and f["signal_date"] > out.get(f["action"], ""):
            out[f["action"]] = f["signal_date"]
    return out


def me(store: Store, inst: dict, signal: dict | None, fx: float = 1.0,
       total_capital: float | None = None) -> dict:
    """User-facing state and advice for one instrument; prices are in the traded vehicle's units."""
    trades = store.trades(inst["symbol"])
    led = ledger(trades)
    close = (signal or {}).get("close")
    price = (inst.get("hold_price") if inst.get("hold_code") else close) or close
    out = {"tracked": bool(trades), "trades": trades, "realized": led["realized"],
           "qty": led["qty"], "held": led["qty"] > 0, "avg_cost": led["avg_cost"], "opened": led["opened"],
           "changed": led["changed"], "price": price, "value": led["qty"] * price if price else None,
           "cost": led["cost"], "pnl": price / led["avg_cost"] - 1 if led["qty"] and price else None, "fx": fx}
    full = full_amount(inst, price, total_capital, fx)
    out["full_amount"] = full
    out["ratio"] = out["value"] / full if full and out["value"] is not None else None
    if not signal:
        return out
    out["target"] = signal["position"]
    if not out["tracked"]:  # unknown holdings: relay the model's own signal
        out["advice"] = {"action": signal["action"], "basis": "model", "new": signal["action"] in ACTIONS}
        return out
    adv = advise(held=out["held"], ratio=out["ratio"], target=signal["position"], today=signal["date"],
                 changed=led["changed"], skips=latest_skips(store, inst["symbol"]),
                 crosses={"BUY": signal.get("buy_cross"), "SELL": signal.get("sell_cross")})
    adv["basis"] = "you"
    if adv.get("side") == "SELL":
        adv["qty"] = led["qty"]
    elif adv.get("side") == "BUY" and full and price:
        lot = inst.get("lot") or 1
        adv["qty"] = math.floor((full - (out["value"] or 0)) / price / lot) * lot or None
    if adv.get("qty") and price:
        adv["amount"] = adv["qty"] * price
    # Your trigger watches the side you are on: holders wait for the sell line, others for the buy line.
    k = price / close if price and close else 1.0
    if out["held"] and signal.get("sell_line_price"):
        adv["trigger"] = {"side": "SELL", "price": signal["sell_line_price"], "vehicle": signal["sell_line_price"] * k}
    elif not out["held"] and signal.get("buy_line_price"):
        adv["trigger"] = {"side": "BUY", "price": signal["buy_line_price"], "vehicle": signal["buy_line_price"] * k}
    out["advice"] = adv
    return out
