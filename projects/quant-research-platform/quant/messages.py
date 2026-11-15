"""Plain-text Feishu messages (Hermes delivers script stdout; empty output = no message)."""
from __future__ import annotations

from .service import ACTION_CN

URL = "https://quant.ai.jingtao.fun"


def _px(v: float | None) -> str:
    return "—" if v is None else (f"{v:,.2f}" if v >= 100 else f"{v:.3f}")


def daily(results: list[dict]) -> str:
    """Only new market sessions are reported; actions first."""
    fresh = [r for r in results if r.get("new")]
    errors = [r for r in results if r.get("error")]
    if not fresh and not errors:
        return ""
    order = {"BUY": 0, "SELL": 1, "HOLD": 2, "WAIT": 3}
    fresh.sort(key=lambda r: order[r["signal"]["action"]])
    acts = [r for r in fresh if r["signal"]["action"] in ("BUY", "SELL")]
    date = fresh[0]["signal"]["date"] if fresh else ""
    head = f"📊 趋势信号 {date} 收盘"
    head += f"｜⚠️ {len(acts)} 个需操作" if acts else "｜无需操作"
    lines = [head]
    for r in fresh:
        i, s = r["instrument"], r["signal"]
        a = s["action"]
        tag = {"BUY": "🔴【买入】", "SELL": "🟢【卖出】", "HOLD": "【持有】", "WAIT": "【观望】"}[a]
        lines.append(f"\n{tag}{i['name']}  {_px(s['close'])}（{s['change']:+.2%}）")
        if a in ("BUY", "SELL"):
            lines.append(f"  → 下一交易日开盘{ACTION_CN[a]}")
        lines.append(f"  斜率 {s['slope']:+.3f}%/日｜买线 {i['buy']:+.3f} 卖线 {i['sell']:+.3f}")
        if s["buy_trigger"]:
            gap = s["buy_trigger"] / s["close"] - 1
            lines.append(f"  下次收盘 ≥ {_px(s['buy_trigger'])}（{gap:+.2%}）将触发买入")
        if s["sell_trigger"]:
            gap = s["sell_trigger"] / s["close"] - 1
            lines.append(f"  下次收盘 ≤ {_px(s['sell_trigger'])}（{gap:+.2%}）将触发卖出")
    for r in errors:
        lines.append(f"\n❗{r['instrument']['name']} 数据刷新失败：{r['error'][:120]}")
    lines.append(f"\n详情 {URL}　仅供参考，不自动下单")
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
