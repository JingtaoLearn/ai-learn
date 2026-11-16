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


def test_advice_follows_real_holdings_and_respects_didnt_do_it(tmp_path, monkeypatch):
    svc, dates, close = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(svc, "update_vehicle_prices", lambda force=False: None)
    sym = "601328"
    view = lambda: svc.view(svc.store.instrument(sym), with_backtest=False)
    sig = view()["signal"]
    assert view()["me"]["advice"]["basis"] == "model"           # nothing declared yet

    # Declare the opposite of the model: holding while it is flat (or flat while it is long).
    against = "SELL" if not sig["position"] else "BUY"
    svc.declare_state(sym, 1000 if against == "SELL" else 0, 10.0, 100 if against == "SELL" else None, sig["date"])
    adv = view()["me"]["advice"]
    assert adv["basis"] == "you" and adv["action"] == against
    if against == "SELL":
        assert adv["qty"] == 1000

    # "Didn't do it": the advice turns into HOLD/WAIT and stays quiet.
    svc.store.answer(sym, sig["date"], against, "SKIPPED")
    adv = view()["me"]["advice"]
    assert adv["dismissed"] and adv["action"] == ("HOLD" if against == "SELL" else "WAIT")

    # A trade changes the state, so the old answer stops applying.
    if against == "SELL":
        svc.store.add_trade(sym, sig["date"], "SELL", 11.0, None, None, None)
        assert view()["me"]["advice"]["action"] == "WAIT"
    else:
        svc.store.add_trade(sym, sig["date"], "BUY", 11.0, 100, None, None)
        assert view()["me"]["advice"]["action"] == "HOLD"


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
    adv = me["advice"]
    if me["target"] == 1:
        assert adv["action"] == "BUY" and adv["topup"] and adv["qty"] == 1100  # 100-share lots
    else:
        assert adv["action"] == "SELL" and adv["qty"] == 900

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


def test_declared_state_is_the_starting_point(tmp_path, monkeypatch):
    svc, _, close = setup(tmp_path, monkeypatch)
    sym = "AU9999"
    monkeypatch.setattr(svc, "update_vehicle_prices", lambda force=False: None)
    svc.store.add_trade(sym, "2020-01-02", "BUY", 5.0, 999, None, None)  # superseded history
    svc.declare_state(sym, 200, 800.0, 100, "2021-01-04")
    me = svc.view(svc.store.instrument(sym), with_backtest=False)["me"]
    assert me["qty"] == 200 and me["avg_cost"] == 800.0 and me["ratio"] == 1.0
    assert me["opened"] == "2021-01-04"
    svc.store.add_trade(sym, "2021-02-01", "SELL", 900.0, 50, None, None)
    me = svc.view(svc.store.instrument(sym), with_backtest=False)["me"]
    assert me["qty"] == 150 and me["ratio"] == 0.75 and me["realized"][-1]["pnl"] == 50 * 100

    svc.declare_state("601328", 3000, None, 50, None)  # no cost: today's price is the basis
    me = svc.view(svc.store.instrument("601328"), with_backtest=False)["me"]
    assert me["avg_cost"] == float(close[-1]) and me["pnl"] == 0 and abs(me["ratio"] - 0.5) < 1e-12
