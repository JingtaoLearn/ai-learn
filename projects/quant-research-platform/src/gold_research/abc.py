from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

from .backtest import backtest, metrics, trade_ledger
from .round3 import completed_signal_close
from .run import _data_hash, canonical_hash, get_git_state, stable_run_id
from .strategies import buy_hold_signal, donchian_signal
from .validation import paired_block_bootstrap

ABC_CANDIDATES = [
    "buy_and_hold",
    "breakout_20_10",
    "breakout_40_20",
    "breakout_60_20",
    "breakout_120_40",
]
ABC_PARAMETERS = {
    "breakout_20_10": (20, 10),
    "breakout_40_20": (40, 20),
    "breakout_60_20": (60, 20),
    "breakout_120_40": (120, 40),
}
ABC_SYMBOL = "601288.SS"
ABC_HOLDOUT_START = pd.Timestamp("2023-01-01")
CJK_FONT_PATH = Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
if CJK_FONT_PATH.is_file():
    font_manager.fontManager.addfont(str(CJK_FONT_PATH))
    CJK_FONT_FAMILY = font_manager.FontProperties(fname=str(CJK_FONT_PATH)).get_name()
else:
    CJK_FONT_FAMILY = None


STRATEGY_LABELS_ZH = {
    "buy_and_hold": "买入并持有",
    "breakout_20_10": "20日突破 / 10日退出",
    "breakout_40_20": "40日突破 / 20日退出",
    "breakout_60_20": "60日突破 / 20日退出",
    "breakout_120_40": "120日突破 / 40日退出",
}
ACTION_LABELS_ZH = {"BUY": "买入", "SELL": "卖出", "HOLD": "继续持有", "CASH": "空仓等待"}


def _session_index(index: pd.Index) -> pd.DatetimeIndex:
    """Normalize daily bars while preserving their exchange-local calendar date."""
    parsed = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce"))
    if parsed.isna().any():
        raise ValueError("session dates must be valid and non-missing")
    if parsed.tz is not None:
        parsed = parsed.tz_localize(None)
    return parsed.normalize()


def _verify_source_artifact(frame: pd.DataFrame, manifest: dict, symbol: str) -> None:
    required = {"symbol", "url", "csv", "csv_sha256", "rows", "data_start", "data_end"}
    if not required <= manifest.keys():
        raise ValueError("data manifest is missing required source artifact fields")
    parsed_url = urlparse(str(manifest["url"]))
    expected_path = f"/v8/finance/chart/{symbol}"
    try:
        valid_port = parsed_url.port in (None, 443)
    except ValueError:
        valid_port = False
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "query1.finance.yahoo.com"
        or not valid_port
        or parsed_url.username is not None
        or parsed_url.password is not None
        or unquote(parsed_url.path) != expected_path
        or parsed_url.fragment
    ):
        raise ValueError("source artifact URL does not identify the canonical Yahoo chart endpoint")
    csv_path = Path(str(manifest["csv"]))
    if not csv_path.is_file():
        raise ValueError("source artifact CSV is missing")
    csv_digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    if csv_digest != manifest["csv_sha256"]:
        raise ValueError("source artifact CSV checksum does not match the manifest")
    try:
        source = pd.read_csv(csv_path, float_precision="round_trip")
    except Exception as exc:
        raise ValueError("source artifact CSV cannot be read") from exc
    if "Date" not in source.columns:
        raise ValueError("source artifact CSV is missing Date")
    source = source.set_index("Date")
    canonical = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    source_columns = [column for column in canonical if column in source.columns]
    frame_columns = [column for column in canonical if column in frame.columns]
    if source_columns != frame_columns:
        raise ValueError("source artifact columns do not match the supplied frame")
    source.index = _session_index(source.index)
    candidate = frame.loc[:, frame_columns].copy()
    candidate.index = _session_index(candidate.index)
    source = source.loc[:, source_columns].sort_index()
    candidate = candidate.sort_index()
    try:
        pd.testing.assert_frame_equal(
            candidate,
            source,
            check_exact=True,
            check_dtype=False,
            check_names=False,
        )
    except AssertionError as exc:
        raise ValueError("source artifact contents do not match the supplied frame") from exc
    if int(manifest["rows"]) != len(source):
        raise ValueError("source artifact row count does not match the manifest")
    data_start = source.index.min().date().isoformat()
    data_end = source.index.max().date().isoformat()
    if manifest["data_start"] != data_start or manifest["data_end"] != data_end:
        raise ValueError("source artifact date range does not match the manifest")


def adjusted_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert Yahoo OHLC rows to the same total-return scale as adjusted close."""
    required = {"Open", "Close", "Adj Close"}
    missing = required - set(frame)
    if missing:
        raise ValueError(f"missing required price columns: {', '.join(sorted(missing))}")
    result = frame.copy()
    price_columns = [column for column in ("Open", "High", "Low", "Close", "Adj Close") if column in result]
    raw = result.loc[:, price_columns]
    contains_boolean = raw.map(lambda value: isinstance(value, (bool, np.bool_))).any().any()
    numeric = raw.apply(pd.to_numeric, errors="coerce")
    if (
        contains_boolean
        or not np.isfinite(numeric.to_numpy(dtype=float)).all()
        or (numeric <= 0.0).any().any()
    ):
        raise ValueError("prices must be finite and strictly positive")
    factor = numeric["Adj Close"] / numeric["Close"]
    for column in ("Open", "High", "Low"):
        if column in numeric:
            result[column] = numeric[column] * factor
    result["Close"] = numeric["Adj Close"]
    result["Adj Close"] = numeric["Adj Close"]
    result.index = _session_index(result.index)
    result = result.sort_index()
    if result.index.has_duplicates:
        raise ValueError("price data contains duplicate dates")
    return result


def abc_candidate_signals(close: pd.Series) -> dict[str, pd.Series]:
    """Fixed long-or-cash breakout family: buy strength and sell weakness."""
    signals = {"buy_and_hold": buy_hold_signal(close)}
    for name, (entry, exit_) in ABC_PARAMETERS.items():
        signals[name] = donchian_signal(close, entry, exit_)
    return signals


def select_frozen_candidate(summary: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    """Select one candidate by balanced historical rank, then lower turnover."""
    required = {"candidate", "cagr", "sharpe", "calmar", "turnover"}
    if not required <= set(summary):
        raise ValueError("candidate summary is missing required selection metrics")
    ranking = summary.copy()
    for name in ("cagr", "sharpe", "calmar"):
        ranking[f"{name}_rank"] = ranking[name].rank(method="average", ascending=False)
    ranking["mean_metric_rank"] = ranking[
        ["cagr_rank", "sharpe_rank", "calmar_rank"]
    ].mean(axis=1)
    ranking = ranking.sort_values(
        ["mean_metric_rank", "turnover", "candidate"], kind="stable"
    ).reset_index(drop=True)
    return str(ranking.iloc[0]["candidate"]), ranking


def _metric_row(candidate: str, result: pd.DataFrame) -> dict:
    return {"candidate": candidate, **metrics(result)}


def _annual_returns(results: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for candidate, result in results.items():
        returns = result["net_return"].astype(float)
        for year, values in returns.groupby(returns.index.year):
            rows.append(
                {
                    "candidate": candidate,
                    "year": int(year),
                    "observations": int(len(values)),
                    "total_return": float((1.0 + values).prod() - 1.0),
                }
            )
    return pd.DataFrame(rows)


def _trade_path(trades: pd.DataFrame, result: pd.DataFrame) -> pd.DataFrame:
    """Attach strategy equity after every completed or marked-open trade."""
    columns = [
        "entry_date",
        "entry_price",
        "exit_date",
        "exit_price",
        "bars",
        "trade_return",
        "cumulative_return",
        "is_open",
    ]
    if trades.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for _, trade in trades.iterrows():
        is_open = bool(trade["is_open"])
        event_date = result.index[-1] if is_open else pd.Timestamp(trade["exit_date"])
        rows.append(
            {
                "entry_date": pd.Timestamp(trade["entry_date"]),
                "entry_price": float(trade["entry_price"]),
                "exit_date": pd.NaT if is_open else event_date,
                "exit_price": np.nan if is_open else float(trade["exit_price"]),
                "bars": int(trade["bars"]),
                "trade_return": float(trade["net_return"]),
                "cumulative_return": float(result.loc[event_date, "equity_net"] - 1.0),
                "is_open": is_open,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _strategy_chart(
    holdout: pd.DataFrame,
    strategy_result: pd.DataFrame,
    benchmark_result: pd.DataFrame,
    trade_path: pd.DataFrame,
    strategy_name: str,
) -> str:
    """Render a Chinese-labeled chart of transactions and cumulative returns."""
    if CJK_FONT_FAMILY is None:
        raise RuntimeError(f"CJK font is required to render the Chinese chart: {CJK_FONT_PATH}")
    entry_window, exit_window = ABC_PARAMETERS[strategy_name]
    chart_context = {
        "svg.hashsalt": "abc-breakout-trend-v1",
        "font.family": CJK_FONT_FAMILY,
        "axes.unicode_minus": False,
    }
    with plt.rc_context(chart_context):
        figure, axes = plt.subplots(2, 1, figsize=(11, 7.5), sharex=True, constrained_layout=True)
        price_axis, return_axis = axes
        price_axis.plot(
            holdout.index,
            holdout["Close"],
            color="#25364d",
            linewidth=1.35,
            label="农业银行复权收盘价",
        )
        if not trade_path.empty:
            price_axis.scatter(
                pd.to_datetime(trade_path["entry_date"]),
                trade_path["entry_price"],
                marker="^",
                s=42,
                color="#16855b",
                edgecolors="white",
                linewidths=0.6,
                label="买入（下一交易日开盘）",
                zorder=3,
            )
            closed = trade_path.loc[~trade_path["is_open"].astype(bool)]
            if not closed.empty:
                price_axis.scatter(
                    pd.to_datetime(closed["exit_date"]),
                    closed["exit_price"],
                    marker="v",
                    s=42,
                    color="#c93f3f",
                    edgecolors="white",
                    linewidths=0.6,
                    label="卖出（下一交易日开盘）",
                    zorder=3,
                )
        price_axis.set_title("农业银行复权股价与策略买卖点")
        price_axis.set_ylabel("复权价格（元）")
        price_axis.grid(alpha=0.22)
        price_axis.legend(loc="upper left", ncols=3, frameon=False)
        price_axis.text(
            0.985,
            0.035,
            f"买入：收盘价突破前{entry_window}日最高收盘价\n"
            "→ 下一交易日开盘，按策略资金100%买入\n"
            f"卖出：收盘价跌破前{exit_window}日最低收盘价\n"
            "→ 下一交易日开盘，全部卖出",
            transform=price_axis.transAxes,
            ha="right",
            va="bottom",
            fontsize=10.5,
            color="#172033",
            bbox={"boxstyle": "round,pad=0.55", "facecolor": "#f7f9fc", "edgecolor": "#b8c2d1", "alpha": 0.96},
        )

        strategy_curve = (strategy_result["equity_net"] - 1.0) * 100.0
        benchmark_curve = (benchmark_result["equity_net"] - 1.0) * 100.0
        return_axis.plot(
            strategy_curve.index,
            strategy_curve,
            color="#405cf5",
            linewidth=1.7,
            label=f"{STRATEGY_LABELS_ZH[strategy_name]}（已扣交易成本）",
        )
        return_axis.plot(
            benchmark_curve.index,
            benchmark_curve,
            color="#8a94a6",
            linewidth=1.25,
            label="买入并持有（已扣交易成本）",
        )
        return_axis.axhline(0.0, color="#98a2b3", linewidth=0.8)
        return_axis.set_title("累计收益率")
        return_axis.set_ylabel("累计收益率（%）")
        return_axis.set_xlabel("日期")
        return_axis.grid(alpha=0.22)
        return_axis.legend(loc="upper left", frameon=False)
        for curve, color in ((strategy_curve, "#405cf5"), (benchmark_curve, "#687386")):
            return_axis.annotate(
                f"{curve.iloc[-1]:+.1f}%",
                xy=(curve.index[-1], curve.iloc[-1]),
                xytext=(-4, 8),
                textcoords="offset points",
                ha="right",
                color=color,
                fontsize=9,
                fontweight="bold",
            )
        buffer = io.StringIO()
        figure.savefig(buffer, format="svg", metadata={"Date": None})
        plt.close(figure)
    svg = buffer.getvalue()
    svg_start = svg.find("<svg")
    svg_open_end = svg.find(">", svg_start) + 1
    description = (
        "<title>农业银行趋势交易买卖点与累计收益率</title>"
        f"<desc>买入：收盘价突破前{entry_window}日最高收盘价，下一交易日开盘按策略资金100%买入；"
        f"卖出：收盘价跌破前{exit_window}日最低收盘价，下一交易日开盘全部卖出。</desc>"
    )
    return svg[:svg_open_end] + description + svg[svg_open_end:]


def _latest_action(close: pd.Series, candidate: str) -> dict:
    next_date = close.index[-1] + pd.offsets.BDay(1)
    extended = pd.concat([close, pd.Series([close.iloc[-1]], index=[next_date])])
    signal = abc_candidate_signals(extended)[candidate]
    previous = float(signal.iloc[-2])
    target = float(signal.iloc[-1])
    if previous <= 0.0 < target:
        action = "BUY"
    elif previous > 0.0 >= target:
        action = "SELL"
    else:
        action = "HOLD" if target > 0.0 else "CASH"
    return {
        "candidate": candidate,
        "signal_as_of_date": close.index[-1].date().isoformat(),
        "next_model_date": next_date.date().isoformat(),
        "previous_exposure": previous,
        "target_exposure": target,
        "action": action,
    }


def _format_percent(value: float) -> str:
    return f"{value:.1%}"


def _render_report(
    run_id: str,
    config: dict,
    manifest: dict,
    development: pd.DataFrame,
    holdout: pd.DataFrame,
    annual: pd.DataFrame,
    bootstrap: dict,
    latest: dict,
    trade_path: pd.DataFrame,
    chart_svg: str,
) -> str:
    best = config["best_frozen_candidate"]
    entry_window, exit_window = ABC_PARAMETERS[best]
    holdout_view = holdout.loc[
        :, ["candidate", "cumulative_return", "cagr", "max_drawdown", "sharpe", "calmar"]
    ].copy()
    for column in ("cumulative_return", "cagr", "max_drawdown"):
        holdout_view[column] = holdout_view[column].map(_format_percent)
    for column in ("sharpe", "calmar"):
        holdout_view[column] = holdout_view[column].map(lambda value: f"{value:.2f}")
    holdout_view["candidate"] = holdout_view["candidate"].map(STRATEGY_LABELS_ZH)
    holdout_view = holdout_view.rename(
        columns={
            "candidate": "策略",
            "cumulative_return": "累计收益率",
            "cagr": "年化收益率",
            "max_drawdown": "最大回撤",
            "sharpe": "夏普比率",
            "calmar": "卡玛比率",
        }
    )
    development_view = development.loc[
        :, ["candidate", "cagr", "max_drawdown", "sharpe", "calmar", "turnover"]
    ].copy()
    for column in ("cagr", "max_drawdown"):
        development_view[column] = development_view[column].map(_format_percent)
    for column in ("sharpe", "calmar", "turnover"):
        development_view[column] = development_view[column].map(lambda value: f"{value:.2f}")
    development_view["candidate"] = development_view["candidate"].map(STRATEGY_LABELS_ZH)
    development_view = development_view.rename(
        columns={
            "candidate": "策略",
            "cagr": "年化收益率",
            "max_drawdown": "最大回撤",
            "sharpe": "夏普比率",
            "calmar": "卡玛比率",
            "turnover": "换手次数",
        }
    )
    annual_view = annual.pivot(index="year", columns="candidate", values="total_return").reset_index()
    for column in annual_view.columns[1:]:
        annual_view[column] = annual_view[column].map(_format_percent)
    annual_view = annual_view.rename(columns={"year": "年份", **STRATEGY_LABELS_ZH})
    trade_view = trade_path.loc[
        :, ["entry_date", "entry_price", "exit_date", "exit_price", "trade_return", "cumulative_return"]
    ].copy()
    trade_view["entry_date"] = pd.to_datetime(trade_view["entry_date"]).dt.date.astype(str)
    trade_view["exit_date"] = pd.to_datetime(trade_view["exit_date"]).dt.date.astype("string").fillna("持仓中")
    for column in ("entry_price", "exit_price"):
        trade_view[column] = trade_view[column].map(
            lambda value: "—" if pd.isna(value) else f"{float(value):.3f}"
        )
    for column in ("trade_return", "cumulative_return"):
        trade_view[column] = trade_view[column].map(_format_percent)
    trade_view = trade_view.rename(
        columns={
            "entry_date": "买入日期",
            "entry_price": "买入价",
            "exit_date": "卖出日期",
            "exit_price": "卖出价",
            "trade_return": "单笔收益率",
            "cumulative_return": "卖出后累计收益率",
        }
    )
    chart_data = base64.b64encode(chart_svg.encode()).decode()
    strategy_row = holdout.loc[holdout["candidate"] == best].iloc[0]
    benchmark_row = holdout.loc[holdout["candidate"] == "buy_and_hold"].iloc[0]
    lead = (
        f"冻结后的{STRATEGY_LABELS_ZH[best]}策略，在回顾性留出期内年化收益率为"
        f"{_format_percent(strategy_row['cagr'])}，最大回撤为"
        f"{_format_percent(strategy_row['max_drawdown'])}；同期买入并持有的年化收益率为"
        f"{_format_percent(benchmark_row['cagr'])}，最大回撤为"
        f"{_format_percent(benchmark_row['max_drawdown'])}。"
    )
    escaped_manifest = html.escape(json.dumps(manifest, indent=2, default=str))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>农业银行趋势交易研究</title>
<style>
:root{{--ink:#172033;--muted:#617087;--line:#dfe5ec;--brand:#405cf5;--good:#147d55;--bad:#bd3f3f}}
*{{box-sizing:border-box}}body{{margin:0;background:#f5f7fa;color:var(--ink);font:15px/1.6 system-ui,sans-serif}}
main{{max-width:1080px;margin:auto;padding:24px}}section{{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0}}
h1{{font-size:clamp(28px,5vw,48px);line-height:1.08;margin:.2em 0}}h2{{margin-top:0}}.lead{{font-size:18px;color:#344158}}
.pills{{display:flex;gap:8px;flex-wrap:wrap}}.pill{{background:#eef1ff;color:#3146b8;padding:4px 10px;border-radius:999px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card{{border:1px solid var(--line);border-radius:10px;padding:14px}}
.card b{{display:block;font-size:24px}}.scroll-shell{{position:relative}}.scroll{{overflow-x:auto;scrollbar-width:thin;scrollbar-color:#5968d8 #e8ebf5}}
.scroll::-webkit-scrollbar{{height:6px;-webkit-appearance:none}}.scroll::-webkit-scrollbar-track{{background:#e8ebf5}}.scroll::-webkit-scrollbar-thumb{{background:#5968d8;border-radius:999px}}.scroll-hint{{display:none}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
.chart{{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:10px;background:white}}
th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}
.warning{{border-left:4px solid #d68a00;background:#fff8e8;padding:12px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f7f8fa;padding:12px;border-radius:8px}}
@media(max-width:640px){{main{{padding:12px}}section{{padding:14px}}.grid{{grid-template-columns:1fr}}
.scroll-shell.overflows .scroll-hint{{display:block;background:#eef1ff;color:#3146b8;text-align:center;padding:6px 10px;border-radius:8px 8px 0 0;font-size:12px;font-weight:650}}
.scroll-shell.overflows:not(.at-end)::after{{content:"";position:absolute;right:0;bottom:6px;width:34px;height:calc(100% - 32px);pointer-events:none;background:linear-gradient(90deg,transparent,rgba(89,104,216,.28))}}}}
</style></head><body><main>
<p class="pills"><span class="pill">601288.SS</span><span class="pill">只做多或空仓</span><span class="pill">下一交易日开盘执行</span></p>
<h1>农业银行：突破后买入，趋势破坏后卖出</h1>
<p class="lead">{html.escape(lead)}</p>
<section><h2>结论</h2><div class="grid">
<div class="card">冻结策略<b>{html.escape(STRATEGY_LABELS_ZH[best])}</b><span>仅使用2022年及以前数据选定</span></div>
<div class="card">当前动作<b>{html.escape(ACTION_LABELS_ZH[latest['action']])}</b><span>策略资金目标仓位 {latest['target_exposure']:.0%}</span></div>
<div class="card">相对收益为正的重采样占比<b>{bootstrap['probability_annual_return_diff_positive']:.1%}</b><span>成对区块重采样中，策略平均收益差大于零的比例</span></div>
</div><p class="warning"><strong>仅供研究。</strong> 2023年后的留出期已经被查看，因此属于回顾性检验，不是真正未见的未来数据；本研究不连接券商，也不会自动下单。</p></section>
<section><h2>什么时候买，什么时候卖</h2>
<p><strong>买入：</strong>当日收盘价突破此前{entry_window}个交易日的最高收盘价，收盘后确认信号；<strong>下一交易日开盘，按策略资金100%买入。</strong></p>
<p><strong>卖出：</strong>持仓后，当日收盘价跌破此前{exit_window}个交易日的最低收盘价，收盘后确认信号；<strong>下一交易日开盘全部卖出。</strong></p>
<ul><li>仓位只有两种：100%持有农业银行，或者0%空仓；不分批、不做空、不加杠杆。</li><li>使用总回报复权价格，持仓期间的分红影响计入收益。</li><li>基础交易成本：买入{config['buy_cost_bps']:g}个基点，卖出{config['sell_cost_bps']:g}个基点。</li><li>候选参数只使用截至{config['selection_end']}的数据选择，之后参数冻结。</li></ul></section>
<section><h2>买卖点与累计收益率</h2>
<p>绿色上三角表示下一交易日开盘买入，红色下三角表示下一交易日开盘卖出；图内规则框直接写明触发条件。下半图展示扣除交易成本后的累计收益率。</p>
<img class="chart" src="data:image/svg+xml;base64,{chart_data}" alt="农业银行复权股价、买卖点，以及趋势策略与买入持有的累计收益率">
<div class="scroll">{trade_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>回顾性留出期</h2><div class="scroll">{holdout_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>开发期候选策略</h2><div class="scroll">{development_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>分年度收益率</h2><div class="scroll">{annual_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>不确定性</h2><p>成对移动区块自助法估计的“日均收益差年化值”为 {bootstrap['annual_return_diff']:.2%}，95%区间为 {bootstrap['annual_return_diff_ci_low']:.2%} 至 {bootstrap['annual_return_diff_ci_high']:.2%}。这不是年化复合收益率之差，也不是策略存在优势的后验概率；区间跨过零，说明历史相对收益差尚无定论。</p></section>
<section><h2>策略可能失效的情况</h2><ul><li>横盘震荡会反复出现假突破，造成连续小亏。</li><li>跳空低开时，实际卖出价可能明显差于发出信号时的收盘价。</li><li>单押一只银行股，仍承担政策、信用周期和公司个体风险。</li><li>如果未来模拟结果持续偏离本报告的成本与成交假设，应停止使用该规则并重新评估。</li></ul></section>
<section><h2>运行溯源</h2><p>运行编号：<code>{html.escape(run_id)}</code></p><p>以下为机器可读字段，字段名保留英文以便复现。</p><pre>{escaped_manifest}</pre></section>
<script>(()=>{{
  const update=(shell,scroll)=>{{
    const overflow=scroll.scrollWidth>scroll.clientWidth+1;
    shell.classList.toggle('overflows',overflow);
    shell.classList.toggle('at-end',!overflow||scroll.scrollLeft+scroll.clientWidth>=scroll.scrollWidth-2);
  }};
  const pairs=[];
  document.querySelectorAll('.scroll').forEach(scroll=>{{
    const shell=document.createElement('div');
    shell.className='scroll-shell';
    scroll.parentNode.insertBefore(shell,scroll);
    const hint=document.createElement('div');
    hint.className='scroll-hint';
    hint.textContent='← 横向滑动查看完整表格 →';
    shell.append(hint,scroll);
    pairs.push([shell,scroll]);
    scroll.addEventListener('scroll',()=>update(shell,scroll),{{passive:true}});
  }});
  const refreshAll=()=>pairs.forEach(([shell,scroll])=>update(shell,scroll));
  requestAnimationFrame(refreshAll);
  window.addEventListener('load', refreshAll);
  window.addEventListener('resize', refreshAll,{{passive:true}});
}})();</script>
</main></body></html>"""


def run_abc_trend_research(
    frame: pd.DataFrame,
    output_root: Path,
    *,
    symbol: str,
    data_manifest: dict,
    holdout_start: pd.Timestamp | str = "2023-01-01",
    analysis_date: pd.Timestamp | datetime | None = None,
    buy_cost_bps: float = 8.0,
    sell_cost_bps: float = 13.0,
    stress_buy_cost_bps: float = 20.0,
    stress_sell_cost_bps: float = 25.0,
    bootstrap_samples: int = 10_000,
) -> dict:
    """Run the canonical frozen breakout-family study for Agricultural Bank."""
    if symbol != ABC_SYMBOL:
        raise ValueError(f"this study only accepts {ABC_SYMBOL}")
    if not isinstance(data_manifest, dict) or data_manifest.get("symbol") != symbol:
        raise ValueError("data manifest must identify the verified 601288.SS input")
    _verify_source_artifact(frame, data_manifest, symbol)
    frame_symbol = frame.attrs.get("symbol")
    if frame_symbol is not None and frame_symbol != symbol:
        raise ValueError("frame symbol conflicts with the verified data manifest")
    adjusted = adjusted_ohlc(frame)
    analysis_date = analysis_date or datetime.now(timezone.utc)
    completed_close, excluded_partial = completed_signal_close(
        adjusted["Close"],
        analysis_date,
        exchange_timezone="Asia/Shanghai",
    )
    adjusted = adjusted.reindex(completed_close.index)
    if len(adjusted) < 500:
        raise ValueError("at least 500 completed sessions are required")
    holdout_start = pd.Timestamp(holdout_start).tz_localize(None).normalize()
    if holdout_start != ABC_HOLDOUT_START:
        raise ValueError("canonical Agricultural Bank study holdout must start on 2023-01-01")
    development = adjusted.loc[adjusted.index < holdout_start]
    holdout = adjusted.loc[adjusted.index >= holdout_start]
    development_warmup_sessions = max(entry for entry, _ in ABC_PARAMETERS.values())
    if len(development) < development_warmup_sessions + 252 or len(holdout) < 252:
        raise ValueError("development and holdout periods each require enough evaluated sessions")
    development_evaluation = development.iloc[development_warmup_sessions:]
    if any(
        isinstance(value, (bool, np.bool_))
        or not np.isfinite(float(value))
        or float(value) < 0.0
        for value in (buy_cost_bps, sell_cost_bps, stress_buy_cost_bps, stress_sell_cost_bps)
    ):
        raise ValueError("cost values must be finite and non-negative")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")

    config = {
        "version": 1,
        "symbol": symbol,
        "candidate_names": ABC_CANDIDATES,
        "holdout_start": holdout_start.date().isoformat(),
        "selection_end": development.index[-1].date().isoformat(),
        "development_warmup_sessions": development_warmup_sessions,
        "development_evaluation_start": development_evaluation.index[0].date().isoformat(),
        "analysis_date": pd.Timestamp(analysis_date).date().isoformat(),
        "latest_completed_bar": adjusted.index[-1].date().isoformat(),
        "excluded_partial_bar": bool(excluded_partial),
        "execution": "completed close signal; next-session adjusted open fill",
        "price_basis": "Yahoo adjusted-close factor applied to OHLC; total-return scale",
        "cash_return": "zero",
        "buy_cost_bps": float(buy_cost_bps),
        "sell_cost_bps": float(sell_cost_bps),
        "stress_buy_cost_bps": float(stress_buy_cost_bps),
        "stress_sell_cost_bps": float(stress_sell_cost_bps),
        "bootstrap_samples": int(bootstrap_samples),
        "bootstrap_block_size": 20,
        "selection_metrics": ["cagr", "sharpe", "calmar"],
    }

    all_signals = abc_candidate_signals(adjusted["Close"])
    development_summary_rows = []
    for candidate in ABC_CANDIDATES:
        result = backtest(
            development_evaluation["Open"],
            all_signals[candidate].reindex(development_evaluation.index),
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
        )
        development_summary_rows.append(_metric_row(candidate, result))
    development_summary = pd.DataFrame(development_summary_rows)
    selectable = development_summary[development_summary["candidate"] != "buy_and_hold"].copy()
    best, ranking = select_frozen_candidate(selectable)
    config["best_frozen_candidate"] = best
    config["selection_ranking"] = ranking[
        ["candidate", "mean_metric_rank", "turnover"]
    ].to_dict(orient="records")

    holdout_results = {}
    for candidate in ("buy_and_hold", best):
        holdout_results[candidate] = backtest(
            holdout["Open"],
            all_signals[candidate].reindex(holdout.index),
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
        )
    holdout_summary = pd.DataFrame(
        [_metric_row(candidate, result) for candidate, result in holdout_results.items()]
    )

    cost_rows = []
    for label, buy_cost, sell_cost in (
        ("base", buy_cost_bps, sell_cost_bps),
        ("stress", stress_buy_cost_bps, stress_sell_cost_bps),
    ):
        for candidate in ("buy_and_hold", best):
            result = backtest(
                holdout["Open"],
                all_signals[candidate].reindex(holdout.index),
                buy_cost_bps=buy_cost,
                sell_cost_bps=sell_cost,
            )
            cost_rows.append(
                {
                    "scenario": label,
                    "buy_cost_bps": buy_cost,
                    "sell_cost_bps": sell_cost,
                    **_metric_row(candidate, result),
                }
            )
    cost_stress = pd.DataFrame(cost_rows)
    annual = _annual_returns(holdout_results)
    bootstrap = {
        "strategy": best,
        "benchmark": "buy_and_hold",
        **paired_block_bootstrap(
            holdout_results[best]["net_return"],
            holdout_results["buy_and_hold"]["net_return"],
            samples=bootstrap_samples,
            block_size=20,
            seed=20260820,
        ),
    }
    latest = _latest_action(adjusted["Close"], best)

    daily_frames = []
    trade_frames = []
    for candidate, result in holdout_results.items():
        daily = result.reset_index(names="date")
        daily.insert(0, "candidate", candidate)
        daily_frames.append(daily)
        trades = trade_ledger(result)
        if not trades.empty:
            trades.insert(0, "candidate", candidate)
            trade_frames.append(trades)
    daily_returns = pd.concat(daily_frames, ignore_index=True)
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame(
        columns=["candidate", "entry_date", "entry_price", "exit_date", "exit_price", "bars", "net_return", "is_open"]
    )
    strategy_ledger = trade_ledger(holdout_results[best])
    trade_path = _trade_path(strategy_ledger, holdout_results[best])
    chart_svg = _strategy_chart(
        holdout,
        holdout_results[best],
        holdout_results["buy_and_hold"],
        trade_path,
        best,
    )

    hash_frame = frame.copy()
    hash_frame.index = _session_index(hash_frame.index)
    hash_frame = hash_frame.sort_index().reindex(adjusted.index)
    data_hash = _data_hash({symbol: hash_frame})
    project_root = Path(__file__).resolve().parents[2]
    git = get_git_state(project_root)
    run_id = stable_run_id(config, data_hash, canonical_hash(git))
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_hash": data_hash,
        "git": git,
        "data_manifest": data_manifest,
    }
    report = _render_report(
        run_id,
        config,
        manifest,
        development_summary,
        holdout_summary,
        annual,
        bootstrap,
        latest,
        trade_path,
        chart_svg,
    )

    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=output_root))
    try:
        (temporary / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
        (temporary / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
        )
        development_summary.to_csv(temporary / "development_summary.csv", index=False)
        holdout_summary.to_csv(temporary / "holdout_summary.csv", index=False)
        cost_stress.to_csv(temporary / "cost_stress_summary.csv", index=False)
        annual.to_csv(temporary / "annual_returns.csv", index=False)
        (temporary / "bootstrap.json").write_text(json.dumps(bootstrap, indent=2) + "\n")
        daily_returns.to_csv(temporary / "daily_returns.csv", index=False)
        trades.to_csv(temporary / "trades.csv", index=False)
        trade_path.to_csv(temporary / "trade_path.csv", index=False)
        (temporary / "strategy_chart.svg").write_text(chart_svg)
        (temporary / "latest_signal.json").write_text(json.dumps(latest, indent=2) + "\n")
        (temporary / "report.html").write_text(report)
        for path in temporary.iterdir():
            path.chmod(0o644)
        temporary.chmod(0o755)
        os.replace(temporary, run_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"run_id": run_id, "run_dir": str(run_dir), "config": config}
