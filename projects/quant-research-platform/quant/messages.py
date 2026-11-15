"""Plain-text Feishu messages (Hermes delivers script stdout; empty output = no message)."""
from __future__ import annotations

URL = "https://quant.ai.jingtao.fun"


def _px(v: float | None) -> str:
    return "—" if v is None else (f"{v:,.2f}" if v >= 100 else f"{v:.3f}")


def _todo(r: dict) -> list[str]:
    """Things the user should act on for one instrument, written from their position."""
    i, s, me = r["instrument"], r["signal"], r.get("me") or {}
    name, out = i["name"], []
    if s["action"] == "BUY":
        mine = "（你已持有，可不动）" if me.get("held") else ""
        out.append(f"🔴 {name}：下一交易日开盘买入{mine}")
    elif s["action"] == "SELL":
        pnl = f"，浮盈 {me['pnl']:+.1%}" if me.get("held") and me.get("pnl") is not None else ""
        mine = f"（你持有{pnl}）" if me.get("held") else ("（你已空仓，可不动）" if me.get("tracked") else "")
        out.append(f"🟢 {name}：下一交易日开盘卖出{mine}")
    if me.get("divergence") == "MODEL_FLAT":
        out.append(f"⚠️ {name}：模型已离场，你仍持有（浮盈 {me.get('pnl', 0):+.1%}），考虑卖出")
    elif me.get("divergence") == "MODEL_LONG":
        out.append(f"⚠️ {name}：模型持有中，你空仓；等下一个买点或自行决定")
    for p in me.get("pending", []):
        if p["date"] != s["date"]:  # today's signal is listed above
            verb = "买" if p["action"] == "BUY" else "卖"
            out.append(f"❓ {name}：{p['date'][5:]} 的{verb}出信号你执行了吗？请到网页确认")
    return out


def _line(r: dict) -> str:
    i, s, me = r["instrument"], r["signal"], r.get("me") or {}
    head = f"{i['name']} {_px(s['close'])}（{s['change']:+.2%}）"
    held = me["held"] if me.get("tracked") else bool(s["position"])
    if held:
        pnl = f" 浮盈 {me['pnl']:+.1%}" if me.get("pnl") is not None else ""
        trig = s["sell_trigger"]
        tail = (f"｜收盘 ≤ {_px(trig)}（{trig / s['close'] - 1:+.1%}）卖出" if trig
                else "｜模型已离场" if not s["position"] else "")
        return f"  {head}{pnl}{tail}"
    trig = s["buy_trigger"]
    tail = f"｜收盘 ≥ {_px(trig)}（{trig / s['close'] - 1:+.1%}）买入" if trig else ""
    return f"  {head}{tail}"


def daily(results: list[dict]) -> str:
    """Only new market sessions are reported; what to do first, then holdings, then watchlist."""
    fresh = [r for r in results if r.get("new")]
    errors = [r for r in results if r.get("error")]
    if not fresh and not errors:
        return ""
    todo = [t for r in fresh for t in _todo(r)]
    date = fresh[0]["signal"]["date"] if fresh else ""
    lines = [f"📊 趋势信号 {date} 收盘｜" + (f"{len(todo)} 件事要处理" if todo else "今天不用操作")]
    if todo:
        lines += ["", "【要处理】", *todo]

    def held(r):
        me = r.get("me") or {}
        return me["held"] if me.get("tracked") else bool(r["signal"]["position"])
    holding = [r for r in fresh if held(r)]
    watching = [r for r in fresh if not held(r)]
    if holding:
        lines += ["", "【持仓】", *map(_line, holding)]
    if watching:
        lines += ["", "【观察】", *map(_line, watching)]
    for r in errors:
        lines.append(f"\n❗{r['instrument']['name']} 数据刷新失败：{r['error'][:120]}")
    lines.append(f"\n{URL}　执行后请在网页确认，仅供参考")
    return "\n".join(lines)


def alerts(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["⚡ 盘中预警"]
    for a in items:
        i = a["instrument"]
        verb = "突破买入触发价" if a["side"] == "BUY" else "跌破卖出触发价"
        then = "买入" if a["side"] == "BUY" else "卖出"
        lines.append(
            f"\n{i['name']} 现价 {_px(a['price'])}（{a['price'] / a['prev_close'] - 1:+.2%}）"
            f"已{verb} {_px(a['trigger'])}\n"
            f"  若按现价收盘：斜率 {a['slope_if_close']:+.3f}%/日 → 产生{then}信号，下一交易日开盘执行"
        )
    lines.append(f"\n{URL}")
    return "\n".join(lines)
