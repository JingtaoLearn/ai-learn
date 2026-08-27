from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path
from typing import Any, Mapping

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager

from .strategy_config import ValidatedStrategyConfig
from .strategy_replay import ReplayResult


CJK_FONT_PATH = Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
EVENT_DISPLAY_TRANSLATIONS = {
    "side": {
        "BUY": "买入",
        "SELL": "卖出",
    },
    "reason": {
        "BUY_THRESHOLD_CROSSING": "向上穿越买入阈值",
        "SELL_THRESHOLD_CROSSING": "向下穿越卖出阈值",
        "INITIALIZE_ZONE": "初始化阈值区域",
        "NO_THRESHOLD_CROSSING": "未穿越阈值",
        "SELL_CROSSING_IGNORED_WHILE_FLAT": "空仓时忽略卖出穿越",
        "STATISTIC_UNAVAILABLE": "斜率暂不可用",
        "INSUFFICIENT_CASH": "现金不足",
        "SELL_SIGNAL_WHILE_NO_HOLDINGS": "无持仓可卖",
        "TERMINAL_FORCED_LIQUIDATION": "期末强制平仓",
    },
}
TRADE_DISPLAY_TRANSLATIONS = {
    "status": {
        "CLOSED": "已平仓",
        "OPEN": "未平仓",
    }
}


class ReportError(RuntimeError):
    """Raised when the required Chinese report cannot be rendered safely."""


def _verified_font_family() -> str:
    if not CJK_FONT_PATH.is_file():
        raise ReportError(f"verified CJK font is unavailable: {CJK_FONT_PATH}")
    try:
        font = font_manager.get_font(str(CJK_FONT_PATH))
        if 0x4E2D not in font.get_charmap():
            raise ReportError(f"CJK font lacks required Chinese glyphs: {CJK_FONT_PATH}")
        font_manager.fontManager.addfont(str(CJK_FONT_PATH))
        family = font_manager.FontProperties(fname=str(CJK_FONT_PATH)).get_name()
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, ReportError):
            raise
        raise ReportError(f"could not register CJK font: {CJK_FONT_PATH}") from exc
    if not family:
        raise ReportError(f"CJK font family could not be verified: {CJK_FONT_PATH}")
    return family


def execution_marker_points(
    events: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    buys = events.loc[events["side"] == "BUY", ["Date", "price"]].copy()
    sells = events.loc[events["side"] == "SELL", ["Date", "price"]].copy()
    return buys, sells


def _rule_lines(config: ValidatedStrategyConfig) -> list[str]:
    operators = config.canonical["operators"]
    template = config.canonical["template"]["parameters"]
    decision = operators["decision"]["parameters"]
    sizing = operators["sizing"]["parameters"]
    target = sizing["target_fraction"] * 100
    return [
        f"买入：斜率从下向上穿越 +{decision['buy_threshold_pct_per_day']:.2f}%/日",
        f"卖出：持仓后斜率从上向下穿越 -{decision['sell_threshold_abs_pct_per_day']:.2f}%/日",
        "时点：仅使用前一交易日及更早的收盘信号；下一交易日原始开盘价成交",
        "启动：初始空仓，首个有效斜率只初始化区域",
        f"仓位：目标仓位 {target:.0f}%，按 {sizing['lot_size']} 股整手向下取整",
        f"终端：{'按期末收盘价盯市，不虚构卖出成本' if template['terminal_handling'] == 'mark_to_market' else '最后评估日按原始开盘价强制平仓'}",
    ]


def _holding_spans(
    events: pd.DataFrame, endpoint: pd.Timestamp
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    spans: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start: pd.Timestamp | None = None
    for event in events.itertuples(index=False):
        if event.side == "BUY":
            start = event.Date
        elif event.side == "SELL" and start is not None:
            spans.append((start, event.Date))
            start = None
    if start is not None:
        spans.append((start, endpoint))
    return spans


def _chart_png(
    result: ReplayResult, config: ValidatedStrategyConfig, family: str
) -> bytes:
    daily = result.daily
    events = result.events
    buys, sells = execution_marker_points(events)
    decision = config.canonical["operators"]["decision"]["parameters"]
    plt.rcParams.update(
        {
            "font.family": family,
            "axes.unicode_minus": False,
            "figure.facecolor": "#f4f0e7",
            "axes.facecolor": "#fffdf8",
        }
    )
    figure, axes = plt.subplots(
        3,
        1,
        figsize=(12.8, 12.2),
        sharex=True,
        gridspec_kw={"height_ratios": [1.8, 0.85, 1.0]},
        constrained_layout=True,
    )
    price_axis, slope_axis, equity_axis = axes
    price_axis.plot(
        daily["Date"], daily["close"], color="#8f969e", lw=1.1, label="原始收盘价"
    )
    price_axis.plot(
        daily["Date"], daily["curve"], color="#c17345", lw=1.5, label="因果拟合曲线"
    )
    price_axis.plot(
        daily["Date"],
        daily["smoothed_curve"],
        color="#2f7356",
        lw=2.2,
        label="平滑趋势",
    )
    if not buys.empty:
        price_axis.scatter(
            buys["Date"],
            buys["price"],
            marker="^",
            s=80,
            color="#16784b",
            edgecolor="white",
            zorder=5,
            label="实际买入（开盘）",
        )
    if not sells.empty:
        price_axis.scatter(
            sells["Date"],
            sells["price"],
            marker="v",
            s=80,
            color="#b5483f",
            edgecolor="white",
            zorder=5,
            label="实际卖出（开盘）",
        )
    for start, end in _holding_spans(events, daily["Date"].iloc[-1]):
        price_axis.axvspan(start, end, color="#66a381", alpha=0.12)
    price_axis.set_title("原始价格、因果趋势与实际成交", fontsize=17)
    price_axis.set_ylabel("人民币 / 股")
    price_axis.legend(frameon=False, ncols=3, fontsize=9, loc="upper left")
    price_axis.grid(alpha=0.25)
    price_axis.text(
        0.99,
        0.03,
        "\n".join(_rule_lines(config)),
        transform=price_axis.transAxes,
        ha="right",
        va="bottom",
        fontsize=8.5,
        bbox={
            "boxstyle": "round,pad=0.55",
            "fc": "#fff4e7",
            "ec": "#d6aa87",
            "alpha": 0.96,
        },
    )

    slope_axis.plot(
        daily["Date"], daily["statistic"], color="#4f66a1", lw=1.4, label="曲线斜率"
    )
    slope_axis.axhline(
        decision["buy_threshold_pct_per_day"],
        color="#16784b",
        ls="--",
        lw=1.2,
        label="买入阈值",
    )
    slope_axis.axhline(
        -decision["sell_threshold_abs_pct_per_day"],
        color="#b5483f",
        ls="--",
        lw=1.2,
        label="卖出阈值",
    )
    slope_axis.set_title("斜率与配置阈值", fontsize=15)
    slope_axis.set_ylabel("% / 日")
    slope_axis.legend(frameon=False, ncols=3, fontsize=9)
    slope_axis.grid(alpha=0.25)

    equity_axis.plot(
        daily["Date"], daily["equity"], color="#2f7356", lw=2.2, label="策略净权益"
    )
    equity_axis.plot(
        daily["Date"],
        daily["zero_cost_equity"],
        color="#6b6ca6",
        lw=1.4,
        label="策略零成本权益",
    )
    equity_axis.plot(
        daily["Date"],
        daily["buy_hold_equity"],
        color="#9a6b20",
        lw=1.4,
        label="买入并持有权益",
    )
    equity_axis.set_title("累计权益", fontsize=15)
    equity_axis.set_ylabel("人民币")
    equity_axis.legend(frameon=False, ncols=3, fontsize=9)
    equity_axis.grid(alpha=0.25)
    equity_axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    equity_axis.tick_params(axis="x", rotation=25)

    stream = io.BytesIO()
    figure.savefig(
        stream,
        format="png",
        dpi=150,
        metadata={"Software": "quant-platform"},
    )
    plt.close(figure)
    return stream.getvalue()


def _format_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _table(
    frame: pd.DataFrame,
    columns: list[tuple[str, str]],
    translations: Mapping[str, Mapping[Any, str]] | None = None,
) -> str:
    translations = translations or {}
    headings = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    rows = []
    for record in frame.to_dict("records"):
        cells = "".join(
            "<td>"
            + html.escape(
                _format_value(translations.get(name, {}).get(record[name], record[name])),
                quote=True,
            )
            + "</td>"
            for name, _ in columns
        )
        rows.append(f"<tr>{cells}</tr>")
    if not rows:
        rows.append(f'<tr><td colspan="{len(columns)}">无</td></tr>')
    return f"<table><thead><tr>{headings}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_report(
    result: ReplayResult,
    config: ValidatedStrategyConfig,
    provenance: Mapping[str, Any],
) -> str:
    dataset_instrument = config.canonical["dataset"]["instrument"]
    if provenance.get("dataset_instrument") != dataset_instrument:
        raise ReportError(
            "report dataset instrument provenance does not match configuration"
        )
    family = _verified_font_family()
    chart = base64.b64encode(_chart_png(result, config, family)).decode("ascii")
    template = config.canonical["template"]["parameters"]
    metrics = result.metrics
    costs = result.cost_breakdown
    instrument = html.escape(template["instrument_display_name"], quote=True)
    assumption = html.escape(template["cost_assumption_label"], quote=True)
    rules = "".join(f"<li>{html.escape(line)}</li>" for line in _rule_lines(config))
    event_table = _table(
        result.events,
        [
            ("Date", "日期"),
            ("side", "方向"),
            ("price", "原始开盘价"),
            ("quantity", "股数"),
            ("commission_cny", "佣金"),
            ("transfer_fee_cny", "过户费"),
            ("stamp_tax_cny", "印花税"),
            ("slippage_cny", "滑点"),
            ("total_cost_cny", "总成本"),
            ("reason", "原因"),
        ],
        EVENT_DISPLAY_TRANSLATIONS,
    )
    trade_table = _table(
        result.trades,
        [
            ("entry_date", "买入日"),
            ("status", "状态"),
            ("entry_price", "买入开盘价"),
            ("exit_date", "卖出日"),
            ("exit_price", "卖出开盘价"),
            ("net_pnl_cny", "净损益"),
            ("return", "收益率"),
        ],
        TRADE_DISPLAY_TRANSLATIONS,
    )
    provenance_rows = "".join(
        "<tr>"
        f"<th>{html.escape(str(key), quote=True)}</th>"
        f"<td><code>{html.escape(_format_value(value), quote=True)}</code></td>"
        "</tr>"
        for key, value in sorted(provenance.items())
    )
    current = "持仓" if metrics["current_position"] == "LONG" else "空仓"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{instrument} 因果交易研究</title>
<style>
:root{{--paper:#f4f0e7;--card:#fffdf8;--ink:#202822;--muted:#667069;--line:#ddd3c4;--green:#2f7356;--red:#a9473e;--scroll-cue:#c67b36}}
*{{box-sizing:border-box}}html,body{{max-width:100%;overflow-x:hidden}}body{{width:100%;margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 sans-serif}}
main{{width:min(1080px,calc(100% - 24px));max-width:100%;margin:auto;padding:18px 0 48px}}header,section{{min-width:0;max-width:100%;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;margin:12px 0}}
h1{{font-size:clamp(29px,6vw,50px);line-height:1.12;margin:.2em 0}}h2{{margin-top:0}}.lead,.muted{{color:var(--muted)}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;min-width:0}}.kpi{{min-width:0;border:1px solid var(--line);border-radius:12px;padding:12px;background:white}}
.kpi span{{display:block;color:var(--muted);font-size:12px}}.kpi b{{display:block;font-size:21px;overflow-wrap:anywhere}}img{{display:block;width:100%;height:auto}}
.table-shell{{max-width:100%;min-width:0;border-right:4px solid var(--scroll-cue);border-radius:8px}}.scroll{{position:relative;max-width:100%;min-width:0;overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--green) #efe7dc}}
.scroll::-webkit-scrollbar{{height:8px}}.scroll::-webkit-scrollbar-track{{background:#efe7dc}}.scroll::-webkit-scrollbar-thumb{{background:var(--green);border-radius:999px}}
.scroll-hint{{display:none;margin:0 0 6px;padding:6px 9px;border-radius:7px;background:#fff0df;color:#75421f;text-align:center;font-size:12px;font-weight:700}}
table{{border-collapse:collapse;width:100%;min-width:680px}}th,td{{padding:8px;border-bottom:1px solid #e8e0d5;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}
code{{overflow-wrap:anywhere;white-space:normal}}pre{{max-width:100%;overflow-x:auto}}.warning{{border-left:4px solid #c67b36;background:#fff3e5;padding:12px}}
.chart-zoom{{position:relative;display:block;width:100%;max-width:100%;padding:0;border:0;background:transparent;cursor:zoom-in;color:var(--ink)}}.chart-zoom:focus-visible{{outline:3px solid var(--green);outline-offset:3px}}.zoom-hint{{position:absolute;right:9px;bottom:9px;padding:6px 9px;border-radius:999px;background:#202822dd;color:white;font-size:12px}}
[hidden]{{display:none!important}}.lightbox{{position:fixed;inset:0;z-index:1000;display:flex;align-items:flex-start;overflow:auto;padding:64px 16px 24px;background:#111b;overscroll-behavior:contain}}.lightbox img{{width:max(1200px,95vw);max-width:none;height:auto;margin:auto}}.lightbox-close{{position:fixed;top:12px;right:12px;z-index:1001;width:44px;height:44px;border:0;border-radius:50%;background:white;color:#202822;font-size:28px;line-height:1;cursor:pointer}}body.zoom-open{{overflow:hidden}}
@media(max-width:700px){{main{{width:calc(100% - 12px);padding-top:4px}}header,section{{padding:15px;border-radius:13px}}.kpis{{grid-template-columns:1fr 1fr}}h1{{font-size:32px}}.scroll-hint{{display:block}}}}
@media(max-width:390px){{.kpis{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header><p class="muted">单股日线 · 严格因果 · 研究用途</p><h1>{instrument}</h1>
<p class="lead">{metrics["period_start"]} 至 {metrics["period_end"]}。信号在开盘前冻结，成交只使用当日原始开盘价。</p></header>
<section><h2>账户摘要</h2><div class="kpis">
<div class="kpi"><span>初始资金</span><b>¥{metrics["initial_capital_cny"]:,.2f}</b></div>
<div class="kpi"><span>最终权益</span><b>¥{metrics["final_equity_cny"]:,.2f}</b></div>
<div class="kpi"><span>净损益 / 收益</span><b>¥{metrics["net_profit_cny"]:,.2f} / {_percent(metrics["net_return"])}</b></div>
<div class="kpi"><span>最大回撤</span><b>{_percent(metrics["max_drawdown"])}</b></div>
<div class="kpi"><span>买入并持有收益</span><b>{_percent(metrics["buy_hold_return"])}</b></div>
<div class="kpi"><span>零成本策略收益</span><b>{_percent(metrics["zero_cost_return"])}</b></div>
<div class="kpi"><span>完整 / 未平交易</span><b>{metrics["closed_trades"]} / {metrics["open_trades"]}</b></div>
<div class="kpi"><span>当前仓位</span><b>{current}</b></div>
</div></section>
<section><h2>配置规则</h2><ul>{rules}</ul><p>成本标签：{assumption}</p></section>
<section><h2>原始价格、因果趋势与实际成交</h2><p class="muted">图内另含“斜率与配置阈值”和“累计权益”两个独立面板。买入和卖出标记来自事件账本的真实开盘成交日和原始开盘价；阴影是实际持仓区间。</p>
<button type="button" class="chart-zoom" id="chart-open" aria-label="放大图表"><img id="chart-image" src="data:image/png;base64,{chart}" alt="价格趋势、斜率阈值和累计权益三面板图"><span class="zoom-hint">点击放大图表</span></button></section>
<section><h2>成本拆分</h2><div class="kpis">
<div class="kpi"><span>佣金</span><b>¥{costs["commission_cny"]:,.2f}</b></div>
<div class="kpi"><span>过户费</span><b>¥{costs["transfer_fee_cny"]:,.2f}</b></div>
<div class="kpi"><span>印花税</span><b>¥{costs["stamp_tax_cny"]:,.2f}</b></div>
<div class="kpi"><span>滑点</span><b>¥{costs["slippage_cny"]:,.2f}</b></div>
</div><p>总成本 ¥{costs["total_cost_cny"]:,.2f}。成本只在实际事件发生时计提。</p></section>
<section><h2>事件明细</h2><p class="scroll-hint">← 左右滑动查看完整表格 →</p><div class="table-shell"><div class="scroll">{event_table}</div></div></section>
<section><h2>交易明细</h2><p class="scroll-hint">← 左右滑动查看完整表格 →</p><div class="table-shell"><div class="scroll">{trade_table}</div></div><p class="muted">未平交易只含买入成本，不计虚构卖出成本，也不纳入完整交易胜率。</p></section>
<section><h2>可复现来源</h2><p class="scroll-hint">← 左右滑动查看完整表格 →</p><div class="table-shell"><div class="scroll"><table>{provenance_rows}</table></div></div></section>
<section><h2>口径限制</h2><p class="warning">这是价格收益账户。除非数据和账本另行提供分红及公司行动现金流，否则不代表包含分红现金流的股东总回报，也不构成投资建议。</p>
<pre>{html.escape(json.dumps(result.reconciliation, sort_keys=True, ensure_ascii=False))}</pre></section>
</main>
<div class="lightbox" id="chart-lightbox" role="dialog" aria-modal="true" aria-label="放大的三面板策略图" hidden><button type="button" class="lightbox-close" id="chart-close" aria-label="关闭放大图表">×</button><img id="chart-enlarged" alt="放大的价格趋势、斜率阈值和累计权益三面板图"></div>
<script>
const opener=document.getElementById('chart-open'),source=document.getElementById('chart-image'),lightbox=document.getElementById('chart-lightbox'),enlarged=document.getElementById('chart-enlarged'),closer=document.getElementById('chart-close');
let previousFocus;
function openChart(){{previousFocus=document.activeElement;enlarged.src=source.src;lightbox.hidden=false;document.body.classList.add('zoom-open');closer.focus()}}
function closeChart(){{lightbox.hidden=true;enlarged.removeAttribute('src');document.body.classList.remove('zoom-open');if(previousFocus)previousFocus.focus()}}
opener.addEventListener('click',openChart);closer.addEventListener('click',closeChart);
lightbox.addEventListener('click',event=>{{if(event.target===lightbox)closeChart()}});
document.addEventListener('keydown',event=>{{if(event.key==='Escape'&&!lightbox.hidden)closeChart()}});
</script></body></html>"""
