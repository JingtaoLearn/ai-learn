"""Serving app (zhlearn): dashboard, JSON API, automation endpoints for Hermes cron."""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import jwt
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import data, messages
from .auth import COOKIE, SESSION_TTL, Auth
from .service import from_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
STATIC = Path(__file__).parent / "static"

svc = from_env()
auth = Auth()
app = FastAPI(title="Quant Signal", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path in ("/health", "/auth/login", "/auth/callback") or path.startswith("/static/"):
        return await call_next(request)
    if path.startswith("/api/jobs/"):
        if not auth.token_ok(request.headers.get("authorization")):
            return JSONResponse({"detail": "unauthorized"}, 401)
        return await call_next(request)
    user = auth.session_user(request.cookies.get(COOKIE))
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "login required"}, 401)
        return RedirectResponse("/auth/login", 303)
    # Cross-site forms cannot set custom headers, so this blocks CSRF on mutations.
    if request.method != "GET" and request.headers.get("x-quant") != "1":
        return JSONResponse({"detail": "missing x-quant header"}, 403)
    return await call_next(request)


@app.on_event("startup")
def warm() -> None:
    empty = [i for i in svc.store.instruments(enabled_only=True) if not svc.store.bars(i["symbol"])]
    if empty:
        threading.Thread(target=lambda: [svc.refresh(i) for i in empty], daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/auth/login")
def login():
    return RedirectResponse(auth.login_redirect(), 303)


@app.post("/auth/callback")
def callback(token: str = Form(...)):
    try:
        session = auth.accept_sso(token)
    except PermissionError:
        raise HTTPException(403, "该账号无权访问")
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"登录失败：{exc}")
    resp = RedirectResponse("/", 303)
    resp.set_cookie(COOKIE, session, max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# ---- dashboard API
def _inst(symbol: str) -> dict:
    inst = svc.store.instrument(symbol)
    if not inst:
        raise HTTPException(404, "unknown symbol")
    return inst


@app.get("/api/overview")
def overview():
    return {"items": svc.overview(), "events": svc.store.events(limit=30)}


@app.get("/api/instruments/{symbol}")
def detail(symbol: str):
    inst = _inst(symbol)
    v = svc.view(inst)
    v["events"] = svc.store.events(symbol, 50)
    v["optimizations"] = svc.store.optimizations(symbol, 5)
    return v


class PreviewIn(BaseModel):
    window: int = Field(ge=2, le=500)
    ema: int = Field(ge=1, le=100)
    buy: float
    sell: float
    start_date: str | None = None


@app.post("/api/instruments/{symbol}/preview")
def preview(symbol: str, body: PreviewIn):
    """What-if backtest for unsaved parameters."""
    inst = {**_inst(symbol), **body.model_dump(exclude_none=True)}
    v = svc.view(inst)
    return {k: v[k] for k in ("signal", "trend", "slope", "position", "start_index", "backtest") if k in v}


class InstrumentIn(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9.\-]{1,20}$")
    name: str = Field(min_length=1, max_length=40)
    provider: str
    code: str = Field(min_length=1, max_length=20)
    market: str
    currency: str = "CNY"
    unit: str = ""
    window: int = Field(20, ge=2, le=500)
    ema: int = Field(5, ge=1, le=100)
    buy: float = 0.2
    sell: float = -0.2
    start_date: str = Field("2018-01-02", pattern=r"^\d{4}-\d{2}-\d{2}$")
    buy_bps: float = Field(5, ge=0, le=500)
    sell_bps: float = Field(5, ge=0, le=500)


def guess(code: str) -> dict:
    """Map a user-typed code to provider/market: 600519, 000001, sh000300, AU9999, MSFT."""
    c = code.strip()
    if c.upper() in ("AU9999", "AU9995", "AGT+D", "AUT+D"):
        return {"provider": "sina_gold", "code": c.upper(), "market": "sge", "currency": "CNY", "unit": "元/克"}
    if re.fullmatch(r"(sh|sz)\d{6}", c.lower()):
        return {"provider": "sina_index", "code": c.lower(), "market": "cn", "currency": "CNY", "unit": "点"}
    if re.fullmatch(r"\d{6}", c):
        suffix = ".SS" if c[0] in "569" else ".SZ"
        return {"provider": "yahoo", "code": c + suffix, "market": "cn", "currency": "CNY", "unit": "元"}
    return {"provider": "yahoo", "code": c.upper(), "market": "us", "currency": "USD", "unit": "$"}


@app.get("/api/lookup")
def lookup(code: str):
    g = guess(code)
    q = next(iter(data.quotes([data.sina_code(g["provider"], g["code"])]).values()), None)
    return {**g, "symbol": code.strip().upper(), "name": q and q["name"], "quote": q}


@app.post("/api/instruments")
def add(body: InstrumentIn):
    if body.provider not in data.PROVIDERS or body.market not in ("cn", "sge", "us"):
        raise HTTPException(400, "unsupported provider/market")
    inst = body.model_dump()
    try:
        bars = data.history(inst["provider"], inst["code"])
    except Exception as exc:
        raise HTTPException(400, f"无法获取行情：{exc}")
    svc.store.upsert_instrument(inst, sort=len(svc.store.instruments()))
    svc.store.replace_bars(inst["symbol"], bars)
    return {"ok": True, "bars": len(bars)}


class PatchIn(BaseModel):
    name: str | None = None
    start_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    buy_bps: float | None = Field(None, ge=0, le=500)
    sell_bps: float | None = Field(None, ge=0, le=500)
    enabled: bool | None = None


@app.patch("/api/instruments/{symbol}")
def patch(symbol: str, body: PatchIn):
    _inst(symbol)
    fields = body.model_dump(exclude_none=True)
    if fields:
        svc.store.update_instrument(symbol, **fields)
    return {"ok": True}


@app.delete("/api/instruments/{symbol}")
def remove(symbol: str):
    _inst(symbol)
    svc.store.delete_instrument(symbol)
    return {"ok": True}


@app.post("/api/instruments/{symbol}/refresh")
def refresh(symbol: str):
    try:
        return {"bars": svc.refresh(_inst(symbol))}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"刷新失败：{exc}")


class OptimizeIn(BaseModel):
    objective: str = Field("sharpe", pattern="^(sharpe|calmar|cagr)$")


@app.post("/api/instruments/{symbol}/optimize")
def run_optimize(symbol: str, body: OptimizeIn):
    _inst(symbol)
    return {"id": svc.start_optimization(symbol, body.objective)}


class ApplyIn(BaseModel):
    window: int
    ema: int
    buy: float
    sell: float


@app.post("/api/instruments/{symbol}/apply")
def apply(symbol: str, body: ApplyIn):
    _inst(symbol)
    try:
        svc.apply_params(symbol, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


class TradeIn(BaseModel):
    side: str = Field(pattern="^(BUY|SELL)$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    price: float = Field(gt=0)
    qty: float | None = Field(None, gt=0)
    note: str | None = Field(None, max_length=200)
    signal_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.post("/api/instruments/{symbol}/trades")
def add_trade(symbol: str, body: TradeIn):
    """Record what the user actually did (optionally answering a model signal)."""
    _inst(symbol)
    return {"id": svc.store.add_trade(symbol, **body.model_dump())}


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int):
    svc.store.delete_trade(trade_id)
    return {"ok": True}


class SkipIn(BaseModel):
    signal_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    action: str = Field(pattern="^(BUY|SELL)$")


@app.post("/api/instruments/{symbol}/skip")
def skip(symbol: str, body: SkipIn):
    """The user saw the signal and did not act on it."""
    _inst(symbol)
    svc.store.answer(symbol, body.signal_date, body.action, "SKIPPED")
    return {"ok": True}


# ---- automation (Hermes cron on ailearn, bearer token)
@app.post("/api/jobs/daily")
def job_daily(markets: str = "cn,sge"):
    results = svc.daily(markets.split(","))
    return {"text": messages.daily(results)}


@app.post("/api/jobs/alerts")
def job_alerts():
    return {"text": messages.alerts(svc.check_alerts())}


@app.post("/api/jobs/refresh")
def job_refresh():
    return svc.refresh_all()
