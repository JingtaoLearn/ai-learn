from datetime import UTC, datetime

from quant import data, engine, messages
from quant.service import Service, params_of, start_index
from quant.store import Store

from .test_engine import synthetic


def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "q.db")
    dates, open_, close = synthetic(600)
    bars = [{"date": d, "open": o, "high": max(o, c), "low": min(o, c), "close": c}
            for d, o, c in zip(dates, open_, close)]
    for inst in store.instruments():
        store.replace_bars(inst["symbol"], bars)
        store.update_instrument(inst["symbol"], start_date=dates[100])
    monkeypatch.setattr(data, "history", lambda provider, code: bars)
    return Service(store), dates, close


def test_daily_reports_each_session_once(tmp_path, monkeypatch):
    svc, _, _ = setup(tmp_path, monkeypatch)
    first = messages.daily(svc.daily(["cn"], refresh=False))
    assert "趋势信号" in first and "交通银行" in first and "微软" not in first
    assert messages.daily(svc.daily(["cn"], refresh=False)) == ""


def test_intraday_alert_fires_once_when_price_crosses_trigger(tmp_path, monkeypatch):
    svc, dates, close = setup(tmp_path, monkeypatch)
    inst = svc.store.instrument("MSFT")
    snap = engine.snapshot(close, params_of(inst), start_index(dates, inst["start_date"]))
    side, trigger = ("BUY", snap["buy_trigger"]) if snap["buy_trigger"] else ("SELL", snap["sell_trigger"])
    price = trigger * (1.01 if side == "BUY" else 0.99)
    monkeypatch.setattr(data, "quotes", lambda codes: {c: {"price": price} for c in codes})
    monkeypatch.setattr(data, "market_now",
                        lambda m, now=None: (m == "us", "2099-01-01"))  # session after all bars
    alerts = svc.check_alerts(datetime.now(UTC))
    assert [(a["instrument"]["symbol"], a["side"]) for a in alerts] == [("MSFT", side)]
    assert "盘中预警" in messages.alerts(alerts)
    assert svc.check_alerts(datetime.now(UTC)) == []


def test_user_feedback_drives_position_pending_and_divergence(tmp_path, monkeypatch):
    svc, dates, close = setup(tmp_path, monkeypatch)
    inst = svc.store.instrument("601328")
    sig = svc.view(inst, with_backtest=False)["signal"]
    other = "SELL" if sig["position"] else "BUY"
    svc.store.add_event("601328", "signal", "2020-03-02", other, {})
    svc.store.add_event("601328", "signal", "2020-04-01", "BUY", {})

    me = svc.view(inst, with_backtest=False)["me"]
    assert [p["date"] for p in me["pending"]] == ["2020-04-01", "2020-03-02"] and not me["tracked"]
    assert me["pending"][0]["fill"]["date"] == "2020-04-02"  # next session's open is the suggested fill

    svc.store.answer("601328", "2020-03-02", other, "SKIPPED")
    svc.store.add_trade("601328", "2020-04-02", "BUY", 10.0, 1000, None, "2020-04-01")
    me = svc.view(inst, with_backtest=False)["me"]
    assert me["pending"] == [] and me["held"] and me["avg_cost"] == 10.0 and me["qty"] == 1000
    assert me["pnl"] == close[-1] / 10.0 - 1
    if sig["action"] not in ("BUY", "SELL"):
        assert me.get("divergence") == (None if sig["position"] else "MODEL_FLAT")

    tid = svc.store.add_trade("601328", "2020-05-06", "SELL", 11.0, None, None, None)  # no qty = sell all
    me = svc.view(inst, with_backtest=False)["me"]
    assert not me["held"] and abs(me["realized"][0]["return"] - 0.1) < 1e-12 and me["realized"][0]["pnl"] == 1000
    svc.store.delete_trade(tid)
    assert svc.view(inst, with_backtest=False)["me"]["held"]


def test_partial_trades_average_cost_and_position_size(tmp_path, monkeypatch):
    svc, _, close = setup(tmp_path, monkeypatch)
    sym = "601328"
    svc.store.add_trade(sym, "2020-04-01", "BUY", 10.0, 1000, None, None)
    svc.store.add_trade(sym, "2020-05-01", "BUY", 13.0, 500, None, None)
    svc.store.add_trade(sym, "2020-06-01", "SELL", 12.0, 600, None, None)
    price = float(close[-1])
    full = 2000 * price  # a full position is worth 2,000 shares at today's price
    svc.store.update_instrument(sym, full_mode="amount", full_value=full)
    me = svc.view(svc.store.instrument(sym), with_backtest=False)["me"]
    assert me["qty"] == 900 and abs(me["avg_cost"] - 11.0) < 1e-9          # (10000 + 6500) / 1500
    assert abs(me["realized"][0]["pnl"] - 600 * (12 - 11)) < 1e-9
    assert abs(me["ratio"] - 0.45) < 1e-9
    if me["target"] == 1:
        assert me["suggest"] == {"side": "BUY", "qty": 1100, "amount": 1100 * price}  # 100-share lots
    else:
        assert me["suggest"]["side"] == "SELL" and me["suggest"]["qty"] == 900

    svc.store.update_instrument(sym, full_mode="qty", full_value=1800)
    assert abs(svc.view(svc.store.instrument(sym), with_backtest=False)["me"]["ratio"] - 0.5) < 1e-9
    svc.store.set_setting("total_capital", str(full * 4))
    svc.store.update_instrument(sym, full_mode="weight", full_value=25)
    assert abs(svc.view(svc.store.instrument(sym), with_backtest=False)["me"]["ratio"] - 0.45) < 1e-9


def test_daily_message_is_written_from_the_users_position(tmp_path, monkeypatch):
    svc, _, close = setup(tmp_path, monkeypatch)
    svc.store.add_trade("601328", "2020-04-02", "BUY", 10.0, 1000, None, None)
    svc.store.update_instrument("601328", full_mode="qty", full_value=1000)
    text = messages.daily(svc.daily(["cn"], refresh=False))
    held = text.split("【持仓】")[1]
    assert "交通银行" in held and "1,000 股" in held and "仓位 100%" in held
    assert f"浮盈 {close[-1] / 10.0 - 1:+.1%}" in held
