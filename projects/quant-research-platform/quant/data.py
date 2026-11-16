"""Market data: daily history (Yahoo / Sina) and live quotes (Sina hq)."""
from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) quant-signal/1"}
SINA_HQ = {"Referer": "https://finance.sina.com.cn", **UA}
CN = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")

# provider -> (history fetcher, live quote code builder)
PROVIDERS = ("yahoo", "sina_index", "sina_gold")


def _get(url: str, headers: dict | None = None) -> httpx.Response:
    r = httpx.get(url, headers=headers or UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r


def _yahoo(code: str) -> list[dict]:
    # range=max silently downsamples to monthly bars; an explicit period keeps daily.
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}?period1=0"
           f"&period2={int(datetime.now(UTC).timestamp()) + 86400}"
           "&interval=1d&events=div,splits&includeAdjustedClose=true")
    res = _get(url).json()["chart"]["result"][0]
    tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "UTC")
    q = res["indicators"]["quote"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    bars = {}
    for i, ts in enumerate(res["timestamp"]):
        o, h, lo, cl, a = q["open"][i], q["high"][i], q["low"][i], q["close"][i], adj[i]
        if None in (o, cl, a) or cl <= 0 or o <= 0:
            continue
        f = a / cl  # back-adjust OHLC so dividends/splits don't create fake moves
        d = datetime.fromtimestamp(ts, tz).date().isoformat()
        bars[d] = {"date": d, "open": o * f, "high": (h or max(o, cl)) * f,
                   "low": (lo or min(o, cl)) * f, "close": a, "volume": q["volume"][i] or 0}
    return [bars[k] for k in sorted(bars)]


def _sina_index(code: str) -> list[dict]:
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=10000")
    rows = []
    for k in _get(url).json():
        o, cl = float(k["open"]), float(k["close"])
        if o <= 0 or cl <= 0:
            continue
        rows.append({"date": k["day"], "open": o, "high": float(k["high"]),
                     "low": float(k["low"]), "close": cl, "volume": float(k["volume"])})
    return rows


def _sina_gold(code: str) -> list[dict]:
    url = ("https://vip.stock.finance.sina.com.cn/q/view/download_gold_history.php"
           f"?breed={code}&start=2005-01-01&end={date.today() + timedelta(days=1)}")
    text = _get(url).content.decode("gb18030")
    bars = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        try:
            vals = [float(row[k].replace(",", "")) for k in ("开盘价", "最高价", "最低价", "收盘价")]
        except (KeyError, ValueError, AttributeError):
            continue
        if min(vals) <= 0:
            continue
        d = row["日期"].strip()
        bars[d] = {"date": d, "open": vals[0], "high": vals[1], "low": vals[2],
                   "close": vals[3], "volume": 0}
    return [bars[k] for k in sorted(bars)]


def history(provider: str, code: str) -> list[dict]:
    fetch = {"yahoo": _yahoo, "sina_index": _sina_index, "sina_gold": _sina_gold}[provider]
    bars = fetch(code)
    if len(bars) < 60:
        raise ValueError(f"{provider}:{code} returned only {len(bars)} bars")
    return bars


def sina_code(provider: str, code: str) -> str:
    if provider == "sina_gold":
        return f"SGE_{code}"
    if provider == "sina_index":
        return code
    if code.endswith(".SS"):
        return "sh" + code[:-3]
    if code.endswith(".SZ"):
        return "sz" + code[:-3]
    return "gb_" + code.lower().replace("-", "")


def vehicle_code(inst: dict) -> str:
    """Sina code of what the user actually trades (e.g. an ETF for an index signal)."""
    return inst.get("hold_code") or sina_code(inst["provider"], inst["code"])


def quotes(codes: list[str]) -> dict[str, dict]:
    """Live quotes keyed by sina code: {name, price, prev_close, open, high, low, time}."""
    if not codes:
        return {}
    text = _get("https://hq.sinajs.cn/list=" + ",".join(codes), SINA_HQ).content.decode("gb18030")
    out = {}
    for line in text.splitlines():
        if "=" not in line or '""' in line:
            continue
        key = line.split("=")[0].replace("var hq_str_", "").strip()
        f = line.split('"')[1].split(",")
        try:
            if key.startswith("fx_"):  # time,bid,ask,prev,...,latest(8)
                out[key] = {"name": key, "price": float(f[8]) or float(f[1]), "time": f[0]}
            elif key.startswith("gb_"):  # US: name,price,chg%,time,chg,open,high,low,...,prev at 26
                out[key] = {"name": f[0], "price": float(f[1]), "open": float(f[5]), "high": float(f[6]),
                            "low": float(f[7]), "prev_close": float(f[26]), "time": f[3]}
            elif key.startswith("SGE_"):  # code,name,contract,price,...,high,_,low,prev,open
                out[key] = {"name": f[1].replace(" ", ""), "price": float(f[3]), "high": float(f[6]), "low": float(f[8]),
                            "prev_close": float(f[9]), "open": float(f[10]), "time": None}
            else:  # A-share: name,open,prev,price,high,low,...,date(30),time(31)
                out[key] = {"name": f[0], "price": float(f[3]), "open": float(f[1]), "prev_close": float(f[2]),
                            "high": float(f[4]), "low": float(f[5]), "time": f"{f[30]} {f[31]}"}
        except (IndexError, ValueError):
            continue
    return out


def market_now(market: str, now: datetime | None = None) -> tuple[bool, str]:
    """(is a live trading session, session date it belongs to) for cn / us / sge."""
    now = now or datetime.now(UTC)
    if market == "us":
        t = now.astimezone(NY)
        live = t.weekday() < 5 and time(9, 30) <= t.time() <= time(16, 0)
        return live, t.date().isoformat()
    t = now.astimezone(CN)
    wd, tm = t.weekday(), t.time()
    day = t.weekday() < 5 and (time(9, 30) <= tm <= time(11, 30) or time(13, 0) <= tm <= time(15, 0))
    if market == "cn":
        return day, t.date().isoformat()
    # SGE: day 09:00-15:30; night 20:00-02:30 belongs to the next trading day.
    if wd < 5 and time(9, 0) <= tm <= time(15, 30):
        return True, t.date().isoformat()
    if (wd < 4 and tm >= time(20, 0)) or (0 < wd < 5 and tm <= time(2, 30)):
        nxt = t.date() + timedelta(days=1) if tm >= time(20, 0) else t.date()
        return True, nxt.isoformat()
    if wd == 4 and tm >= time(20, 0):
        return True, (t.date() + timedelta(days=3)).isoformat()
    if wd == 5 and tm <= time(2, 30):
        return True, (t.date() + timedelta(days=2)).isoformat()
    return False, t.date().isoformat()


def is_cn_trading_day(today: str) -> bool:
    """SSE and SGE share the holiday calendar; the CSI 300 quote date tells us."""
    q = _get("https://hq.sinajs.cn/list=sh000300", SINA_HQ).content.decode("gb18030")
    try:
        return q.split('"')[1].split(",")[30] == today
    except IndexError:
        return False


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
