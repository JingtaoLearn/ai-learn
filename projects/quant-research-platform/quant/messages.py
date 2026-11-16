"""Plain-text Feishu messages (Hermes delivers script stdout; empty output = no message)."""
from __future__ import annotations

URL = "https://quant.ai.jingtao.fun"


def _px(v: float | None) -> str:
    return "—" if v is None else (f"{v:,.2f}" if v >= 100 else f"{v:.3f}")


def _qty(q: float | None, unit: str) -> str:
    if q is None:
        return ""
    return f"{q:,.0f} {unit}" if abs(q - round(q)) < 1e-9 else f"{q:,.2f} {unit}"


def _money(v: float | None, cur: str) -> str:
    return "" if v is None else f"≈{'$' if cur == 'USD' else '¥'}{v:,.0f}"


def _pos(me: dict) -> str:
    return f"仓位 {me['ratio']:.0%}" if me.get("ratio") is not None else ""


def _why(i: dict, s: dict, side: str) -> str:
    line = i["sell"] if side == "SELL" else i["buy"]
    rel = "低于卖线" if side == "SELL" else "高于买线"
    if (side == "SELL" and s["slope"] <= line) or (side == "BUY" and s["slope"] >= line):
        return f"斜率 {s['slope']:+.3f}%/日 {rel} {line:+.3f}"
    return f"模型{'空仓' if side == 'SELL' else '满仓'}中"


def _todo(r: dict) -> list[str]:
    """What to do now, written from the user's real holdings."""
    i, s, me = r["instrument"], r["signal"], r.get("me") or {}
    a = me.get("advice") or {}
    name, unit, cur = i["name"], i.get("hold_unit") or "", i["currency"]
    new = "🆕" if a.get("new") else ""
    if a.get("basis") == "model":
        if a["action"] in ("BUY", "SELL"):
            return [f"📌 {name}：模型{'买入' if a['action'] == 'BUY' else '卖出'}信号（还没登记你的持仓，登记后会按你的实际情况提醒）"]
        return []
    size = f" {_qty(a['qty'], unit)}{_money(a.get('amount'), cur)}" if a.get("qty") else ""
    if a["action"] == "SELL":
        pnl = f"，浮盈 {me['pnl']:+.1%}" if me.get("pnl") is not None else ""
        return [f"🟢{new} {name}：卖出{size}（你持有{pnl}｜{_why(i, s, 'SELL')}）"]
    if a["action"] == "BUY":
        verb = "补仓" if a.get("topup") else "买入"
        mine = _pos(me) if a.get("topup") else "你空仓"
        hint = "" if size else "，设置满仓额度后给出数量"
        return [f"🔴{new} {name}：{verb}{size}（{mine}｜{_why(i, s, 'BUY')}{hint}）"]
    return []


def _line(r: dict) -> str:
    i, s, me = r["instrument"], r["signal"], r.get("me") or {}
    a = me.get("advice") or {}
    head = f"{i['name']} {_px(s['close'])}（{s['change']:+.2%}）"
    if not me.get("tracked"):
        trig = s["sell_trigger"] or s["buy_trigger"]
        side = "卖出" if s["sell_trigger"] else "买入"
        return f"  {head}｜未登记持仓，模型{'持有' if s['position'] else '空仓'}" + (
            f"，收盘{'≤' if s['sell_trigger'] else '≥'} {_px(trig)} {side}" if trig else "")
    tail = ""
    t = a.get("trigger")
    if a.get("dismissed"):
        tail = (f"｜你 {a['skipped_on'][5:]} 选择不{'卖' if a['side'] == 'SELL' else '买'}，"
                f"斜率重新{'下穿卖线' if a['side'] == 'SELL' else '上穿买线'}时再提醒")
    elif t and a["action"] not in ("BUY", "SELL"):
        tail = f"｜收盘 {'≤' if t['side'] == 'SELL' else '≥'} {_px(t['price'])}（{t['price'] / s['close'] - 1:+.1%}）{'卖出' if t['side'] == 'SELL' else '买入'}"
    if me.get("held"):
        size = " ".join(x for x in (_qty(me["qty"], i.get("hold_unit") or ""), _pos(me)) if x)
        pnl = f" 浮盈 {me['pnl']:+.1%}" if me.get("pnl") is not None else ""
        return f"  {head} {size}{pnl}{tail}"
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
        return bool((r.get("me") or {}).get("held"))
    holding = [r for r in fresh if held(r)]
    watching = [r for r in fresh if not held(r)]
    if holding:
        lines += ["", "【持仓】", *map(_line, holding)]
    if watching:
        lines += ["", "【观察】", *map(_line, watching)]
    since = [r for r in fresh if ((r.get("me") or {}).get("since") or {}).get("days")]
    if since:
        tot = {k: sum(r["me"]["since"][k]["pnl"] * r["me"]["fx"] for r in since) for k in ("actual", "follow", "hold")}
        lines += ["", f"【自登记以来】你的实际 {tot['actual']:+,.0f} 元｜完全跟随策略 {tot['follow']:+,.0f} 元"
                      f"｜一直持有 {tot['hold']:+,.0f} 元"]
    for r in errors:
        lines.append(f"\n❗{r['instrument']['name']} 数据刷新失败：{r['error'][:120]}")
    lines.append(f"\n{URL}　执行后请在网页确认，仅供参考")
    return "\n".join(lines)


def alerts(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["⚡ 盘中预警"]
    for a in items:
        i, unit = a["instrument"], a["instrument"].get("hold_unit") or ""
        sell = a["side"] == "SELL"
        mine = f"（你持有 {_qty(a['held_qty'], unit)}）" if sell and a.get("held_qty") else (
            "（你空仓）" if not sell and a.get("held_qty") is not None else "")
        lines.append(
            f"\n{i['name']}{mine} 现价 {_px(a['price'])}（{a['price'] / a['prev_close'] - 1:+.2%}）"
            f"已{'跌破卖出' if sell else '突破买入'}触发价 {_px(a['trigger'])}\n"
            f"  若按现价收盘：斜率 {a['slope_if_close']:+.3f}%/日 → {'卖出' if sell else '买入'}信号，下一交易日开盘执行"
        )
    lines.append(f"\n{URL}")
    return "\n".join(lines)
