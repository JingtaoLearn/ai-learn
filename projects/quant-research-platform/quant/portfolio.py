"""The user's real positions: confirmed trades, signal feedback, divergence from the model."""
from __future__ import annotations

from .store import Store

ACTIONS = ("BUY", "SELL")


def position(trades: list[dict]) -> dict:
    """Trades are full-position in/out (the strategy is long/flat); qty is informational."""
    held, entry = False, None
    for t in sorted(trades, key=lambda x: (x["date"], x["id"])):
        if t["side"] == "BUY":
            held, entry = True, t
        else:
            held, entry = False, None
    return {"held": held, "entry": entry}


def realized(trades: list[dict]) -> list[dict]:
    out, entry = [], None
    for t in sorted(trades, key=lambda x: (x["date"], x["id"])):
        if t["side"] == "BUY":
            entry = t
        elif entry:
            out.append({"entry_date": entry["date"], "entry": entry["price"], "exit_date": t["date"],
                        "exit": t["price"], "return": t["price"] / entry["price"] - 1})
            entry = None
    return out


def pending(store: Store, symbol: str) -> list[dict]:
    """Model BUY/SELL signals the user has not answered yet (newest first)."""
    answered = {(f["signal_date"], f["action"]) for f in store.feedback(symbol)}
    return [e for e in store.events(symbol, 200)
            if e["kind"] == "signal" and e["action"] in ACTIONS and (e["date"], e["action"]) not in answered]


def me(store: Store, inst: dict, signal: dict | None) -> dict:
    """User-facing state for one instrument."""
    trades = store.trades(inst["symbol"])
    pos = position(trades)
    out = {"held": pos["held"], "trades": trades, "realized": realized(trades),
           "pending": pending(store, inst["symbol"]),
           "tracked": bool(trades or store.feedback(inst["symbol"]))}
    if pos["held"] and signal:
        e = pos["entry"]
        out.update(entry_date=e["date"], entry_price=e["price"], qty=e["qty"],
                   pnl=signal["close"] / e["price"] - 1)
    # A signal issued at the last close is a to-do for tomorrow's open, not a divergence yet.
    if signal and out["tracked"] and signal["action"] not in ACTIONS:
        model = bool(signal["position"])
        if model and not pos["held"]:
            out["divergence"] = "MODEL_LONG"   # model holds, you are flat
        elif pos["held"] and not model:
            out["divergence"] = "MODEL_FLAT"   # model exited, you still hold
    return out
