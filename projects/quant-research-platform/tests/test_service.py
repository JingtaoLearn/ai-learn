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
