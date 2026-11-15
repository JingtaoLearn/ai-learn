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
    svc.store.add_trade("601328", "2020-04-02", "BUY", 10.0, None, None, "2020-04-01")
    me = svc.view(inst, with_backtest=False)["me"]
    assert me["pending"] == [] and me["held"] and me["entry_price"] == 10.0
    assert me["pnl"] == close[-1] / 10.0 - 1
    if sig["action"] not in ("BUY", "SELL"):
        assert me.get("divergence") == (None if sig["position"] else "MODEL_FLAT")

    tid = svc.store.add_trade("601328", "2020-05-06", "SELL", 11.0, None, None, None)
    me = svc.view(inst, with_backtest=False)["me"]
    assert not me["held"] and abs(me["realized"][0]["return"] - 0.1) < 1e-12
    svc.store.delete_trade(tid)
    assert svc.view(inst, with_backtest=False)["me"]["held"]


def test_daily_message_is_written_from_the_users_position(tmp_path, monkeypatch):
    svc, _, close = setup(tmp_path, monkeypatch)
    svc.store.add_trade("601328", "2020-04-02", "BUY", 10.0, None, None, None)
    text = messages.daily(svc.daily(["cn"], refresh=False))
    assert "【持仓】" in text and "交通银行" in text.split("【持仓】")[1]
    assert f"浮盈 {close[-1] / 10.0 - 1:+.1%}" in text
