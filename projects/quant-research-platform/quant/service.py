"""Orchestration: refresh data, compute views, daily signals, intraday alerts, optimization."""
from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime

import httpx
import numpy as np

from . import backtest, data, engine, optimize, portfolio
from .engine import Params
from .store import Store

log = logging.getLogger("quant")
ACTION_CN = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有", "WAIT": "观望"}


def params_of(inst: dict) -> Params:
    return Params(int(inst["window"]), int(inst["ema"]), float(inst["buy"]), float(inst["sell"]))


def start_index(dates: list[str], start: str) -> int:
    return next((i for i, d in enumerate(dates) if d >= start), len(dates))


class Service:
    def __init__(self, store: Store, worker_url: str | None = None, worker_token: str | None = None):
        self.store = store
        self.worker_url = worker_url
        self.worker_token = worker_token

    # data
    def refresh(self, inst: dict) -> int:
        try:
            bars = data.history(inst["provider"], inst["code"])
        except Exception as exc:  # keep the previous bars; surface the error in the UI
            self.store.update_instrument(inst["symbol"], refresh_error=str(exc)[:300])
            raise
        self.store.replace_bars(inst["symbol"], bars)
        return len(bars)

    def refresh_all(self) -> dict:
        out = {}
        for inst in self.store.instruments(enabled_only=True):
            try:
                out[inst["symbol"]] = self.refresh(inst)
            except Exception as exc:
                log.warning("refresh %s failed: %s", inst["symbol"], exc)
                out[inst["symbol"]] = f"error: {exc}"
        return out

    def _arrays(self, symbol: str, upto: str | None = None):
        bars = [b for b in self.store.bars(symbol) if upto is None or b["date"] < upto]
        dates = [b["date"] for b in bars]
        o = np.array([b["open"] for b in bars], float)
        c = np.array([b["close"] for b in bars], float)
        return bars, dates, o, c

    # views
    def view(self, inst: dict, with_backtest: bool = True) -> dict:
        bars, dates, o, c = self._arrays(inst["symbol"])
        p = params_of(inst)
        if len(c) < p.window + 3:
            return {"instrument": inst, "ready": False}
        si = start_index(dates, inst["start_date"])
        snap = engine.snapshot(c, p, si)
        snap.update(date=dates[-1], close=float(c[-1]), change=float(c[-1] / c[-2] - 1),
                    provisional=bool(bars[-1]["provisional"]))
        out = {"instrument": inst, "ready": True, "signal": snap, "me": self.me(inst, snap, bars)}
        if with_backtest:
            slope, pos = engine.single_positions(c, p, si)
            smooth = np.exp(engine.smooth_curve(c, p.window, p.ema))
            bt = backtest.run(dates, o, c, pos, inst["buy_bps"], inst["sell_bps"], si)
            out.update(
                bars=[[b["date"], round(b["open"], 4), round(b["high"], 4), round(b["low"], 4),
                       round(b["close"], 4)] for b in bars],
                trend=[None if np.isnan(v) else round(float(v), 4) for v in smooth],
                slope=[None if np.isnan(v) else round(float(v), 4) for v in slope],
                position=pos.astype(int).tolist(),
                start_index=si,
                backtest=bt,
            )
        return out

    def me(self, inst: dict, snap: dict, bars: list[dict]) -> dict:
        m = portfolio.me(self.store, inst, snap)
        for p in m["pending"]:  # suggest the next-open fill the model assumed
            nxt = next((b for b in bars if b["date"] > p["date"]), None)
            p["fill"] = {"date": nxt["date"], "price": nxt["open"]} if nxt else None
        return m

    def overview(self) -> list[dict]:
        out = []
        for inst in self.store.instruments():
            v = self.view(inst, with_backtest=False)
            if v["ready"]:
                _, dates, _, c = self._arrays(inst["symbol"])
                v["spark"] = [round(float(x), 4) for x in c[-120:]]
            out.append(v)
        return out

    # daily signal after the close
    def daily(self, markets: list[str], refresh: bool = True) -> list[dict]:
        today_cn = datetime.now(data.CN).date().isoformat()
        results = []
        for inst in self.store.instruments(enabled_only=True):
            if inst["market"] not in markets:
                continue
            if refresh:
                try:
                    self.refresh(inst)
                    self._append_live_close(inst, today_cn)
                except Exception as exc:
                    results.append({"instrument": inst, "error": str(exc)})
                    continue
            v = self.view(inst, with_backtest=False)
            if not v["ready"]:
                continue
            s = v["signal"]
            new = self.store.add_event(inst["symbol"], "signal", s["date"], s["action"], s)
            results.append({**v, "new": new})
        return results

    def _append_live_close(self, inst: dict, today: str) -> None:
        """Sina gold history can lag the 15:30 close; complete today from the live quote."""
        if inst["provider"] != "sina_gold":
            return
        bars = self.store.bars(inst["symbol"])
        if bars and bars[-1]["date"] >= today or not data.is_cn_trading_day(today):
            return
        q = data.quotes([data.sina_code(inst["provider"], inst["code"])])
        q = next(iter(q.values()), None)
        if q and q["price"] > 0:
            bars.append({"date": today, "open": q["open"] or q["price"], "high": q["high"],
                         "low": q["low"], "close": q["price"], "volume": 0, "provisional": 1})
            self.store.replace_bars(inst["symbol"], bars)

    # intraday alerts
    def check_alerts(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        live = []
        for inst in self.store.instruments(enabled_only=True):
            is_live, session = data.market_now(inst["market"], now)
            if is_live:
                live.append((inst, session))
        if not live:
            return []
        quotes = data.quotes([data.sina_code(i["provider"], i["code"]) for i, _ in live])
        alerts = []
        for inst, session in live:
            q = quotes.get(data.sina_code(inst["provider"], inst["code"]))
            _, dates, _, c = self._arrays(inst["symbol"], upto=session)
            p = params_of(inst)
            if not q or q["price"] <= 0 or len(c) < p.window + 3:
                continue
            snap = engine.snapshot(c, p, start_index(dates, inst["start_date"]))
            price = q["price"]
            for side, trigger, hit in (("BUY", snap["buy_trigger"], lambda t: price >= t),
                                       ("SELL", snap["sell_trigger"], lambda t: price <= t)):
                if trigger and hit(trigger):
                    detail = {"price": price, "trigger": trigger, "prev_close": float(c[-1]),
                              "slope_if_close": engine.projected_slope(c, p, price),
                              "threshold": p.buy if side == "BUY" else p.sell}
                    if self.store.add_event(inst["symbol"], "alert", session, side, detail):
                        alerts.append({"instrument": inst, "side": side, **detail})
        return alerts

    # optimization (training VM first, local fallback)
    def start_optimization(self, symbol: str, objective: str = "sharpe") -> int:
        run_id = self.store.create_optimization(symbol, objective)
        threading.Thread(target=self._optimize, args=(run_id, symbol, objective), daemon=True).start()
        return run_id

    def _optimize(self, run_id: int, symbol: str, objective: str) -> None:
        inst = self.store.instrument(symbol)
        _, dates, o, c = self._arrays(symbol)
        job = {"dates": dates, "open_": o.tolist(), "close": c.tolist(),
               "start_index": start_index(dates, inst["start_date"]),
               "buy_bps": inst["buy_bps"], "sell_bps": inst["sell_bps"],
               "objective": objective, "current": params_of(inst).as_dict()}
        runner = "local"
        try:
            if self.worker_url:
                try:
                    self.store.set_optimization_status(run_id, "running", "feng")
                    r = httpx.post(f"{self.worker_url}/optimize", json=job, timeout=900,
                                   headers={"Authorization": f"Bearer {self.worker_token}"})
                    r.raise_for_status()
                    self.store.finish_optimization(run_id, "feng", r.json(), None)
                    return
                except httpx.HTTPError as exc:
                    log.warning("training worker unavailable, running locally: %s", exc)
            self.store.set_optimization_status(run_id, "running", runner)
            self.store.finish_optimization(run_id, runner, optimize.run(**job), None)
        except Exception as exc:
            log.exception("optimization %s failed", run_id)
            self.store.finish_optimization(run_id, runner, None, str(exc)[:500])

    def apply_params(self, symbol: str, params: dict) -> None:
        p = Params(int(params["window"]), int(params["ema"]), float(params["buy"]), float(params["sell"]))
        if not (2 <= p.window <= 500 and 1 <= p.ema <= 100 and p.buy > p.sell):
            raise ValueError("invalid parameters")
        old = params_of(self.store.instrument(symbol)).as_dict()
        self.store.update_instrument(symbol, **p.as_dict())
        self.store.add_event(symbol, "params", datetime.now(UTC).isoformat(timespec="seconds"),
                             "UPDATE", {"from": old, "to": p.as_dict()})


def from_env() -> Service:
    store = Store(os.environ.get("QUANT_DB", "/data/quant.db"))
    return Service(store, os.environ.get("QUANT_WORKER_URL") or None,
                   os.environ.get("QUANT_WORKER_TOKEN") or None)
