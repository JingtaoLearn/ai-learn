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
    """Render adjusted price transitions and costed cumulative-return paths."""
    with plt.rc_context({"svg.hashsalt": "abc-breakout-trend-v1"}):
        figure, axes = plt.subplots(2, 1, figsize=(11, 7.5), sharex=True, constrained_layout=True)
        price_axis, return_axis = axes
        price_axis.plot(
            holdout.index,
            holdout["Close"],
            color="#25364d",
            linewidth=1.35,
            label="Adjusted close",
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
                label="BUY",
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
                    label="SELL",
                    zorder=3,
                )
        price_axis.set_title("Agricultural Bank adjusted price with modeled transitions")
        price_axis.set_ylabel("Adjusted price (CNY)")
        price_axis.grid(alpha=0.22)
        price_axis.legend(loc="upper left", ncols=3, frameon=False)

        strategy_curve = (strategy_result["equity_net"] - 1.0) * 100.0
        benchmark_curve = (benchmark_result["equity_net"] - 1.0) * 100.0
        return_axis.plot(
            strategy_curve.index,
            strategy_curve,
            color="#405cf5",
            linewidth=1.7,
            label=f"{strategy_name} after costs",
        )
        return_axis.plot(
            benchmark_curve.index,
            benchmark_curve,
            color="#8a94a6",
            linewidth=1.25,
            label="Buy-and-hold after costs",
        )
        return_axis.axhline(0.0, color="#98a2b3", linewidth=0.8)
        return_axis.set_title("Cumulative return")
        return_axis.set_ylabel("Return (%)")
        return_axis.set_xlabel("Date")
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
    return buffer.getvalue()


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
    holdout_view = holdout.loc[
        :, ["candidate", "cumulative_return", "cagr", "max_drawdown", "sharpe", "calmar"]
    ].copy()
    for column in ("cumulative_return", "cagr", "max_drawdown"):
        holdout_view[column] = holdout_view[column].map(_format_percent)
    for column in ("sharpe", "calmar"):
        holdout_view[column] = holdout_view[column].map(lambda value: f"{value:.2f}")
    holdout_view = holdout_view.rename(
        columns={
            "candidate": "Strategy",
            "cumulative_return": "Cumulative return",
            "cagr": "CAGR",
            "max_drawdown": "Maximum drawdown",
            "sharpe": "Sharpe",
            "calmar": "Calmar",
        }
    )
    development_view = development.loc[
        :, ["candidate", "cagr", "max_drawdown", "sharpe", "calmar", "turnover"]
    ].copy()
    for column in ("cagr", "max_drawdown"):
        development_view[column] = development_view[column].map(_format_percent)
    for column in ("sharpe", "calmar", "turnover"):
        development_view[column] = development_view[column].map(lambda value: f"{value:.2f}")
    development_view = development_view.rename(
        columns={
            "candidate": "Strategy",
            "cagr": "CAGR",
            "max_drawdown": "Maximum drawdown",
            "sharpe": "Sharpe",
            "calmar": "Calmar",
            "turnover": "Turnover",
        }
    )
    annual_view = annual.pivot(index="year", columns="candidate", values="total_return").reset_index()
    for column in annual_view.columns[1:]:
        annual_view[column] = annual_view[column].map(_format_percent)
    trade_view = trade_path.loc[
        :, ["entry_date", "entry_price", "exit_date", "exit_price", "trade_return", "cumulative_return"]
    ].copy()
    trade_view["entry_date"] = pd.to_datetime(trade_view["entry_date"]).dt.date.astype(str)
    trade_view["exit_date"] = pd.to_datetime(trade_view["exit_date"]).dt.date.astype("string").fillna("OPEN")
    for column in ("entry_price", "exit_price"):
        trade_view[column] = trade_view[column].map(
            lambda value: "—" if pd.isna(value) else f"{float(value):.3f}"
        )
    for column in ("trade_return", "cumulative_return"):
        trade_view[column] = trade_view[column].map(_format_percent)
    trade_view = trade_view.rename(
        columns={
            "entry_date": "Buy date",
            "entry_price": "Buy price",
            "exit_date": "Sell date",
            "exit_price": "Sell price",
            "trade_return": "Trade return",
            "cumulative_return": "Cumulative return",
        }
    )
    chart_data = base64.b64encode(chart_svg.encode()).decode()
    strategy_row = holdout.loc[holdout["candidate"] == best].iloc[0]
    benchmark_row = holdout.loc[holdout["candidate"] == "buy_and_hold"].iloc[0]
    lead = (
        f"The frozen {best} rule produced {_format_percent(strategy_row['cagr'])} annualized "
        f"with {_format_percent(strategy_row['max_drawdown'])} maximum drawdown in the retrospective "
        f"holdout, versus {_format_percent(benchmark_row['cagr'])} and "
        f"{_format_percent(benchmark_row['max_drawdown'])} for buy-and-hold."
    )
    escaped_manifest = html.escape(json.dumps(manifest, indent=2, default=str))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agricultural Bank of China breakout trend research</title>
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
<p class="pills"><span class="pill">601288.SS</span><span class="pill">long or cash</span><span class="pill">next-open execution</span></p>
<h1>Agricultural Bank of China: buy strength, sell weakness</h1>
<p class="lead">{html.escape(lead)}</p>
<section><h2>Decision</h2><div class="grid">
<div class="card">Frozen rule<b>{html.escape(best)}</b><span>selected before the holdout</span></div>
<div class="card">Next action<b>{html.escape(latest['action'])}</b><span>target exposure {latest['target_exposure']:.0%}</span></div>
<div class="card">Bootstrap resamples above zero<b>{bootstrap['probability_annual_return_diff_positive']:.1%}</b><span>fraction of paired block resamples with a positive mean-return difference</span></div>
</div><p class="warning"><strong>Research only.</strong> The holdout is retrospective, not genuinely unseen future data. No broker connection or automatic order path exists.</p></section>
<section><h2>Rule</h2><p>Enter after the completed close breaks above the prior N-session high; exit after it breaks below the prior M-session low. The order is modeled at the next adjusted open. This is the literal "chase strength and cut weakness" interpretation.</p>
<ul><li>Long-only; no short selling or leverage.</li><li>Total-return adjusted prices include dividend effects while held.</li><li>Base friction: {config['buy_cost_bps']:g} bps on buys and {config['sell_cost_bps']:g} bps on sells.</li><li>Candidate selection used only dates through {config['selection_end']}.</li></ul></section>
<section><h2>Buy / sell points and cumulative return</h2>
<p>Green triangles are modeled next-open buys; red triangles are modeled next-open sells. The lower panel shows cumulative return after transaction costs.</p>
<img class="chart" src="data:image/svg+xml;base64,{chart_data}" alt="Agricultural Bank adjusted price with buy and sell points, plus cumulative strategy and buy-and-hold returns">
<div class="scroll">{trade_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>Retrospective holdout</h2><div class="scroll">{holdout_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>Development candidates</h2><div class="scroll">{development_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>Annual return path</h2><div class="scroll">{annual_view.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>Uncertainty</h2><p>Annualized mean daily return difference from the paired moving-block bootstrap: {bootstrap['annual_return_diff']:.2%}; confidence interval {bootstrap['annual_return_diff_ci_low']:.2%} to {bootstrap['annual_return_diff_ci_high']:.2%}. This is not a CAGR difference or a posterior probability of an edge. An interval crossing zero means the historical difference is unresolved.</p></section>
<section><h2>Failure conditions</h2><ul><li>Sideways markets can cause repeated false breakouts and small losses.</li><li>Gap-down opens can make the modeled exit materially worse than the signal close.</li><li>A single bank stock carries policy, credit-cycle, and company-specific concentration risk.</li><li>Stop using the rule if future paper results materially diverge from the recorded cost and execution assumptions.</li></ul></section>
<section><h2>Provenance</h2><p>Run ID: <code>{html.escape(run_id)}</code></p><pre>{escaped_manifest}</pre></section>
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
    hint.textContent='← Swipe horizontally for the full table →';
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
