from __future__ import annotations

import base64
import html
import io
import json
import math
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg", force=True)
matplotlib.rcdefaults()
matplotlib.rcParams.update(
    {
        "backend": "Agg",
        "font.family": ["sans-serif"],
        "font.sans-serif": ["DejaVu Sans"],
        "text.usetex": False,
        "figure.dpi": 100.0,
        "savefig.dpi": 130.0,
    }
)
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .backtest import backtest, metrics, trade_ledger  # noqa: E402
from .round3 import completed_signal_close  # noqa: E402
from .run import (  # noqa: E402
    _data_hash,
    capture_source_authority,
    launch_round4_worker,
    revalidate_execution_identity,
    seal_execution_identity,
    stable_run_id,
)
from .strategies import (  # noqa: E402
    absolute_momentum_signal,
    buy_hold_signal,
    donchian_signal,
    multi_horizon_momentum_signal,
    risk_managed_trend_signal,
    trend_filter_signal,
    trend_temperature_signal,
)

CANDIDATE_NAMES = [
    "buy_and_hold",
    "trend_200",
    "momentum_252",
    "donchian_55_20",
    "momentum_vote_63_126_252",
    "trend_200_vol10",
    "temperature_63",
]
REQUIRED_SYMBOLS = {"GC=F", "GLD"}
_PROVENANCE_CONTEXT = getattr(sys, "_gold_round4_provenance_context", None)


def _restrict_fonts_to_matplotlib_payload() -> None:
    if _PROVENANCE_CONTEXT is None:
        data_root = Path(matplotlib.get_data_path()).resolve()
        allowed_paths = {
            str(Path(entry.fname).resolve())
            for entry in font_manager.fontManager.ttflist
            if Path(entry.fname).resolve().is_relative_to(data_root)
        }
    else:
        allowed_paths = set(_PROVENANCE_CONTEXT["matplotlib_font_paths"])
        if not allowed_paths:
            raise RuntimeError("Matplotlib RECORD contains no usable font")
    allowed = [
        entry
        for entry in font_manager.fontManager.ttflist
        if str(Path(entry.fname).resolve()) in allowed_paths
    ]
    if not allowed:
        raise RuntimeError("Matplotlib package contains no usable RECORD-bound font")
    font_manager.fontManager.ttflist = allowed


_restrict_fonts_to_matplotlib_payload()


def _run_with_resource_audit_suspended(provenance_context: dict, operation):
    """Run provenance metadata I/O outside the separately sealed resource-open audit."""
    with provenance_context["resource_tracker"].suspended():
        return operation()


def round4_signals(close: pd.Series) -> dict[str, pd.Series]:
    """Return the fixed, transparent Round 4 candidate set."""
    return {
        "buy_and_hold": buy_hold_signal(close),
        "trend_200": trend_filter_signal(close, 200),
        "momentum_252": absolute_momentum_signal(close, 252),
        "donchian_55_20": donchian_signal(close, 55, 20),
        "momentum_vote_63_126_252": multi_horizon_momentum_signal(
            close, (63, 126, 252), 2
        ),
        "trend_200_vol10": risk_managed_trend_signal(close, 200, 60, 0.10),
        "temperature_63": trend_temperature_signal(close, 63, 1.0, 0.5),
    }


def latest_three_year_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Select sessions in the open-left three-calendar-year interval ending at the last bar."""
    ordered = pd.DatetimeIndex(index).sort_values().unique()
    if ordered.empty:
        raise ValueError("at least one completed daily bar is required")
    end = ordered[-1]
    boundary = end - pd.DateOffset(years=3)
    return ordered[(ordered > boundary) & (ordered <= end)]


def anchored_walk_forward_folds(
    index: pd.DatetimeIndex,
    minimum_train_sessions: int = 252,
    test_sessions: int = 63,
) -> list[dict]:
    """Build anchored expanding folds and retain only complete test blocks."""
    ordered = pd.DatetimeIndex(index)
    if minimum_train_sessions < 252:
        raise ValueError("minimum_train_sessions must be at least 252")
    if test_sessions != 63:
        raise ValueError("test_sessions must be exactly 63")
    folds: list[dict] = []
    train_size = minimum_train_sessions
    fold_id = 1
    while train_size + test_sessions <= len(ordered):
        train_index = ordered[:train_size]
        test_index = ordered[train_size : train_size + test_sessions]
        folds.append(
            {
                "fold_id": fold_id,
                "train_index": train_index,
                "test_index": test_index,
                "train_start": train_index[0],
                "train_end": train_index[-1],
                "test_start": test_index[0],
                "test_end": test_index[-1],
            }
        )
        fold_id += 1
        train_size += test_sessions
    if not folds:
        raise ValueError("evaluation requires at least 315 common sessions")
    return folds


def marker_labels(exposure: pd.Series) -> pd.Series:
    """Classify entries, exits, and cost-bearing fractional rebalances."""
    values = exposure.astype(float).fillna(0.0).clip(0.0, 1.0)
    previous = values.shift(1, fill_value=0.0)
    delta = values - previous
    labels = pd.Series("CASH", index=values.index, dtype="string", name="marker")
    labels.loc[(previous <= 0.0) & (values > 0.0)] = "BUY"
    labels.loc[(previous > 0.0) & (values <= 0.0)] = "SELL"
    labels.loc[(previous > 0.0) & (values > 0.0) & (delta > 1e-12)] = "ADD"
    labels.loc[(previous > 0.0) & (values > 0.0) & (delta < -1e-12)] = "REDUCE"
    labels.loc[(previous > 0.0) & (values > 0.0) & (delta.abs() <= 1e-12)] = "HOLD"
    return labels


def _transition_marker(previous: float, target: float) -> str:
    if previous <= 0.0 < target:
        return "BUY"
    if previous > 0.0 >= target:
        return "SELL"
    if previous > 0.0 and target > previous + 1e-12:
        return "ADD"
    if target > 0.0 and target < previous - 1e-12:
        return "REDUCE"
    return "HOLD" if target > 0.0 else "CASH"


def _path_metrics(path: pd.DataFrame) -> dict[str, float]:
    returns = path["net_return"].astype(float)
    if returns.empty:
        raise ValueError("metric path cannot be empty")
    equity = (1.0 + returns).cumprod()
    years = max(
        (returns.index[-1] - returns.index[0]).days / 365.2425,
        max(len(returns) - 1, 1) / 252.0,
    )
    cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0) if equity.iloc[-1] > 0 else -1.0
    standard_deviation = float(returns.std(ddof=0))
    sharpe = (
        float(returns.mean() / standard_deviation * math.sqrt(252))
        if standard_deviation > 0
        else 0.0
    )
    drawdown = equity / equity.cummax().clip(lower=1.0) - 1.0
    maximum_drawdown = float(drawdown.min())
    calmar = float(cagr / abs(maximum_drawdown)) if maximum_drawdown < 0 else 0.0
    turnover = float(path["turnover"].astype(float).sum()) if "turnover" in path else 0.0
    return {
        "cagr": cagr,
        "sharpe": sharpe,
        "calmar": calmar,
        "turnover": turnover,
    }


def _aggregate_robust_rank(rows: pd.DataFrame) -> pd.DataFrame:
    """Rank each metric within each symbol/cost scenario, then aggregate ranks."""
    ranked = rows.copy()
    for metric in ("cagr", "sharpe", "calmar"):
        ranked[f"{metric}_rank"] = ranked.groupby(["symbol", "cost_bps"])[metric].rank(
            method="average", ascending=False
        )
    rank_columns = ["cagr_rank", "sharpe_rank", "calmar_rank"]
    ranked["mean_metric_rank"] = ranked[rank_columns].mean(axis=1)
    aggregate = (
        ranked.groupby("candidate", as_index=False)
        .agg(
            aggregate_mean_metric_rank=("mean_metric_rank", "mean"),
            aggregate_turnover=("turnover", "mean"),
        )
        .sort_values(
            ["aggregate_mean_metric_rank", "aggregate_turnover", "candidate"],
            kind="stable",
        )
        .reset_index(drop=True)
    )
    aggregate["aggregate_robust_rank"] = np.arange(1, len(aggregate) + 1)
    return aggregate


def select_candidate_for_fold(
    candidate_results: dict[tuple[str, float, str], pd.DataFrame],
    train_index: pd.DatetimeIndex,
) -> tuple[str, pd.DataFrame]:
    """Select using only the explicit training index; later rows are never inspected."""
    train_index = pd.DatetimeIndex(train_index)
    rows = []
    for (symbol, cost_bps, candidate), result in sorted(candidate_results.items()):
        training_path = result.reindex(train_index).copy()
        if training_path["net_return"].isna().any():
            raise ValueError("training index is not fully covered by candidate results")
        if "cost" not in training_path:
            raise ValueError("candidate results require explicit cost accounting")
        # The train-end rebalance cost is known at that open, but its forward
        # return is only revealed at the test-start open. Keep the cost and
        # remove only the unavailable return before ranking candidates.
        training_path.loc[train_index[-1], "net_return"] = -float(
            training_path.loc[train_index[-1], "cost"]
        )
        rows.append(
            {
                "symbol": symbol,
                "cost_bps": float(cost_bps),
                "candidate": candidate,
                **_path_metrics(training_path),
            }
        )
    scenario_rows = pd.DataFrame(rows)
    candidates_per_scenario = scenario_rows.groupby(["symbol", "cost_bps"])[
        "candidate"
    ].nunique()
    if candidates_per_scenario.nunique() != 1:
        raise ValueError("every training scenario must contain the same candidates")
    ranking = _aggregate_robust_rank(scenario_rows)
    return str(ranking.iloc[0]["candidate"]), ranking


def _normalize(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    if not REQUIRED_SYMBOLS <= set(data):
        missing = ", ".join(sorted(REQUIRED_SYMBOLS - set(data)))
        raise ValueError(f"Round 4 requires GC=F and GLD; missing: {missing}")
    normalized = {}
    for symbol, frame in data.items():
        item = frame.copy()
        if "Date" in item.columns:
            item["Date"] = pd.to_datetime(item["Date"])
            item = item.set_index("Date")
        item.index = pd.DatetimeIndex(pd.to_datetime(item.index))
        if item.index.tz is not None:
            item.index = item.index.tz_convert(None)
        item.index = item.index.normalize()
        if not {"Open", "Close"} <= set(item.columns):
            raise ValueError(f"{symbol} requires Open and Close columns")
        raw_prices = item.loc[:, ["Open", "Close"]]
        contains_boolean = raw_prices.map(lambda value: isinstance(value, (bool, np.bool_))).any().any()
        prices = raw_prices.apply(pd.to_numeric, errors="coerce")
        if (
            contains_boolean
            or not np.isfinite(prices.to_numpy(dtype=float)).all()
            or (prices <= 0.0).any().any()
        ):
            raise ValueError(f"{symbol} Open and Close must be finite and strictly positive")
        item.loc[:, ["Open", "Close"]] = prices
        item = item.sort_index()
        if item.index.has_duplicates:
            raise ValueError(f"{symbol} contains duplicate dates")
        normalized[symbol] = item
    return normalized


def _validate_costs(cost_grid_bps: tuple[float, ...]) -> tuple[float, ...]:
    if len(cost_grid_bps) != 2:
        raise ValueError("cost grid must contain the fixed 5 and 20 bps scenarios")
    if any(isinstance(value, (bool, np.bool_)) for value in cost_grid_bps):
        raise ValueError("cost values must be finite and non-negative")
    costs = tuple(float(value) for value in cost_grid_bps)
    if any(not np.isfinite(value) or value < 0.0 for value in costs):
        raise ValueError("cost values must be finite and non-negative")
    if costs != (5.0, 20.0):
        raise ValueError("Round 4 cost grid is fixed at 5 and 20 bps")
    return costs


def _latest_target(close: pd.Series) -> dict[str, float]:
    next_date = close.index[-1] + pd.offsets.BDay(1)
    extended = pd.concat([close, pd.Series([close.iloc[-1]], index=[next_date])])
    return {name: float(signal.iloc[-1]) for name, signal in round4_signals(extended).items()}


def _price_chart(
    evaluation_frame: pd.DataFrame,
    marker_frame: pd.DataFrame,
    symbol: str,
    best_frozen: str,
) -> str:
    fig, axis = plt.subplots(figsize=(10, 4), constrained_layout=True)
    axis.plot(evaluation_frame.index, evaluation_frame["Open"], color="#26364a", linewidth=1.2)
    chosen = marker_frame[
        (marker_frame["symbol"] == symbol) & (marker_frame["portfolio"] == best_frozen)
    ].copy()
    for label, shape, color in (("BUY", "^", "#16855b"), ("SELL", "v", "#c93f3f")):
        points = chosen[chosen["marker"] == label]
        if not points.empty:
            axis.scatter(
                pd.to_datetime(points["date"]),
                points["open"],
                marker=shape,
                color=color,
                s=32,
                label=label,
                zorder=3,
            )
    axis.set_title(f"{symbol} open price with {best_frozen} transitions")
    axis.set_ylabel("Price")
    axis.grid(alpha=0.25)
    if not chosen.empty and chosen["marker"].isin(["BUY", "SELL"]).any():
        axis.legend()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=130)
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode()


def _render_report(
    run_id: str,
    config: dict,
    manifest: dict,
    candidate_summary: pd.DataFrame,
    fold_rows: pd.DataFrame,
    pseudo_summary: pd.DataFrame,
    latest_signals: pd.DataFrame,
    chart: str,
) -> str:
    frozen = (
        pseudo_summary[pseudo_summary["portfolio_type"] == "frozen"]
        .drop_duplicates("portfolio")
        .sort_values("aggregate_robust_rank")
        [["portfolio", "aggregate_robust_rank", "aggregate_mean_metric_rank"]]
    )
    adaptive = pseudo_summary[pseudo_summary["portfolio"] == "adaptive_selector"][
        ["symbol", "cost_bps", "cagr", "sharpe", "calmar", "turnover"]
    ]
    selections = (
        fold_rows.drop_duplicates("fold_id")[
            [
                "fold_id",
                "selection_return_end",
                "selection_cost_end",
                "train_end",
                "test_start",
                "test_end",
                "selected_candidate",
            ]
        ]
        .sort_values("fold_id")
    )
    current = latest_signals[latest_signals["candidate"] == config["best_frozen_candidate"]][
        ["symbol", "candidate", "signal_as_of_date", "next_open_target_exposure", "marker"]
    ]
    provenance = html.escape(
        json.dumps(
            {
                "run_id": run_id,
                "config": config,
                "data_hash": manifest["data_hash"],
                "git": manifest["git"],
                "data_manifest": manifest["data_manifest"],
            },
            indent=2,
            default=str,
        ),
        quote=True,
    )
    best = html.escape(config["best_frozen_candidate"], quote=True)
    evaluation = candidate_summary[
        (candidate_summary["candidate"] == config["best_frozen_candidate"])
        & (candidate_summary["cost_bps"] == 5.0)
    ][["symbol", "candidate", "cagr", "sharpe", "calmar", "max_drawdown", "turnover"]]
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gold three-year walk-forward research</title><style>
:root{{--ink:#172033;--muted:#596579;--paper:#f3f5f7;--card:#fff;--gold:#9a6b12}}*{{box-sizing:border-box}}body{{font:15px/1.55 system-ui,sans-serif;max-width:1080px;margin:auto;padding:18px;color:var(--ink);background:var(--paper)}}section{{background:var(--card);padding:16px;margin:13px 0;border-radius:10px}}.decision{{border-left:5px solid var(--gold)}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:7px;border-bottom:1px solid #ddd;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}img{{max-width:100%;height:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.muted{{color:var(--muted)}}@media(max-width:390px){{body{{padding:9px}}section{{padding:11px}}h1{{font-size:24px}}}}
</style></head><body><p class="muted">RESEARCH ONLY · RUN {html.escape(run_id)}</p>
<h1>Gold three-year walk-forward research</h1>
<section class="decision"><h2>Decision</h2><p>The aggregate retrospective pseudo-OOS robust rank selected <strong>{best}</strong> as the best frozen candidate. Buy-and-hold was eligible and could win the same deterministic ranking.</p>{evaluation.to_html(index=False, border=0, escape=True, float_format=lambda value: f'{value:.4f}')}</section>
<section><h2>Design</h2><ul><li>Evaluation: {config['evaluation_start']} through {config['evaluation_end']}, exactly the latest open-left three-calendar-year interval ending at the latest common completed daily bar.</li><li>Earlier observations are used only to warm indicators; reported evaluation and selection metrics exclude them.</li><li>Anchored expanding training starts with 252 sessions; every test block has 63 sessions and starts after its training end.</li><li>Same-fold selection retains the known train-end rebalance cost but excludes the train-end forward return because it is not known until the test-start open. CAGR, Sharpe, and Calmar ranks are aggregated across GC=F, GLD, and 5/20 bps one-way costs; lower turnover and then candidate name break ties.</li><li>Prior-close signals execute at the next open and earn open-to-open returns.</li><li>Markers include fractional ADD/REDUCE rebalances that incur costs; the trade ledger separately records zero-to-positive round trips.</li></ul></section>
<section><h2>Frozen-candidate rank</h2><div class="scroll">{frozen.to_html(index=False, border=0, escape=True, float_format=lambda value: f'{value:.3f}')}</div></section>
<section><h2>Adaptive selector</h2><div class="scroll">{adaptive.to_html(index=False, border=0, escape=True, float_format=lambda value: f'{value:.4f}')}</div></section>
<section><h2>Walk-forward selections</h2><div class="scroll">{selections.to_html(index=False, border=0, escape=True)}</div></section>
<section><h2>Latest completed-bar signals</h2><div class="scroll">{current.to_html(index=False, border=0, escape=True, float_format=lambda value: f'{value:.3f}')}</div></section>
<section><h2>Price and transitions</h2><img alt="Price chart with buy and sell transitions" src="data:image/png;base64,{chart}"></section>
<section><h2>Interpretation and limitations</h2><ul><li>These stitched paths are retrospective pseudo-OOS comparisons because strategy families and the full historical archive have already been observed.</li><li>GC=F and GLD are correlated confirmations, not independent statistical samples.</li><li>Equal-weight CAGR, Sharpe, and Calmar ranks are a declared judgment; a risk-first objective or different score weights can flip the winner.</li><li>This is research, not investment advice or a live-order system.</li><li>The temperature candidate is a transparent public-formula proxy, not a reconstruction of the proprietary Trend Animal algorithm.</li><li>GC=F is a continuous-futures proxy with opaque roll construction; GLD is an ETF proxy. No roll, financing, spread, tax, impact, or opening-auction slippage is modeled.</li><li>The final partial block after the last complete 63-session test, if any, is not included in stitched pseudo-OOS metrics.</li></ul></section>
<section><h2>Provenance</h2><pre><code>{provenance}</code></pre></section></body></html>"""


def _run_round4_worker(
    data: dict[str, pd.DataFrame],
    output_root: Path,
    *,
    cost_grid_bps: tuple[float, ...] = (5.0, 20.0),
    analysis_date: str | pd.Timestamp | datetime | None = None,
    data_manifest: dict | None = None,
    _provenance_context: dict,
) -> dict:
    """Calculate, attest, and atomically publish inside the isolated worker."""
    source_capture = _run_with_resource_audit_suspended(
        _provenance_context,
        lambda: capture_source_authority(_provenance_context["provenance"]),
    )
    if dict(source_capture.source_identity) != _provenance_context["source_identity"]:
        raise RuntimeError("SOURCE_CHANGED_DURING_RUN: worker source differs from bootstrap capture")
    costs = _validate_costs(cost_grid_bps)
    normalized = _normalize(data)
    created_at = datetime.now(timezone.utc)
    analysis_day = pd.Timestamp(analysis_date if analysis_date is not None else created_at)
    if analysis_day.tzinfo is not None:
        analysis_day = analysis_day.tz_convert(None)
    analysis_day = analysis_day.normalize()

    completed: dict[str, pd.DataFrame] = {}
    for symbol, frame in normalized.items():
        close, _ = completed_signal_close(frame["Close"].astype(float), analysis_day)
        completed[symbol] = frame.loc[close.index]
    common_index = completed["GC=F"].index.intersection(completed["GLD"].index).sort_values()
    evaluation_index = latest_three_year_index(common_index)
    folds = anchored_walk_forward_folds(evaluation_index)
    evaluation_end = evaluation_index[-1]
    for symbol in completed:
        completed[symbol] = completed[symbol].loc[:evaluation_end]

    config = {
        "version": 4,
        "analysis_date": analysis_day.date().isoformat(),
        "symbols": sorted(normalized),
        "candidate_names": CANDIDATE_NAMES,
        "cost_grid_bps": list(costs),
        "evaluation_policy": "latest three calendar years ending at latest completed daily bar",
        "evaluation_start": evaluation_index[0].date().isoformat(),
        "evaluation_end": evaluation_end.date().isoformat(),
        "evaluation_sessions": len(evaluation_index),
        "warmup_policy": "all earlier bars are indicator warmup only",
        "minimum_train_sessions": 252,
        "test_sessions": 63,
        "selection_metrics": ["cagr", "sharpe", "calmar"],
        "selection_method": (
            "mean within-scenario metric ranks across symbols and costs; "
            "lower turnover then candidate name tie-break"
        ),
        "execution": "prior-close signal; next-open fill; open-to-open return",
        "cash_return": "zero; no interest credit",
        "study_label": "retrospective pseudo-OOS",
    }
    data_hash = _data_hash(normalized)

    candidate_results: dict[tuple[str, float, str], pd.DataFrame] = {}
    summary_rows: list[dict] = []
    signal_maps: dict[str, dict[str, pd.Series]] = {}
    for symbol in sorted(REQUIRED_SYMBOLS):
        frame = completed[symbol]
        signals = round4_signals(frame["Close"].astype(float))
        signal_maps[symbol] = signals
        open_eval = frame["Open"].astype(float).reindex(evaluation_index)
        for candidate in CANDIDATE_NAMES:
            signal = signals[candidate].reindex(evaluation_index)
            for cost in costs:
                result = backtest(open_eval, signal, cost)
                candidate_results[(symbol, cost, candidate)] = result
                summary_rows.append(
                    {
                        "symbol": symbol,
                        "cost_bps": cost,
                        "candidate": candidate,
                        **metrics(result),
                    }
                )
    candidate_summary = pd.DataFrame(summary_rows)
    full_rank = _aggregate_robust_rank(
        candidate_summary[["symbol", "cost_bps", "candidate", "cagr", "sharpe", "calmar", "turnover"]]
    )
    candidate_summary = candidate_summary.merge(full_rank, on="candidate", validate="many_to_one")

    fold_rows: list[dict] = []
    selected_by_fold: dict[int, str] = {}
    fold_for_date: dict[pd.Timestamp, int] = {}
    for fold in folds:
        selected, ranking = select_candidate_for_fold(candidate_results, fold["train_index"])
        selected_by_fold[fold["fold_id"]] = selected
        fold_for_date.update({date: fold["fold_id"] for date in fold["test_index"]})
        for row in ranking.itertuples(index=False):
            fold_rows.append(
                {
                    "fold_id": fold["fold_id"],
                    "train_start": fold["train_start"],
                    "train_end": fold["train_end"],
                    "train_sessions": len(fold["train_index"]),
                    "selection_return_end": fold["train_index"][-2],
                    "selection_cost_end": fold["train_index"][-1],
                    "selection_metric_rows": len(fold["train_index"]),
                    "test_start": fold["test_start"],
                    "test_end": fold["test_end"],
                    "test_sessions": len(fold["test_index"]),
                    "candidate": row.candidate,
                    "training_aggregate_mean_metric_rank": row.aggregate_mean_metric_rank,
                    "training_aggregate_turnover": row.aggregate_turnover,
                    "training_robust_rank": row.aggregate_robust_rank,
                    "selected_candidate": selected,
                    "is_selected": row.candidate == selected,
                }
            )
    walk_forward_folds = pd.DataFrame(fold_rows)
    pseudo_index = pd.DatetimeIndex([date for fold in folds for date in fold["test_index"]])

    stitched_results: dict[tuple[str, float, str], pd.DataFrame] = {}
    daily_frames: list[pd.DataFrame] = []
    pseudo_rows: list[dict] = []
    for symbol in sorted(REQUIRED_SYMBOLS):
        open_pseudo = completed[symbol]["Open"].astype(float).reindex(pseudo_index)
        adaptive_exposure = pd.Series(index=pseudo_index, dtype=float, name="signal")
        for date in pseudo_index:
            selected = selected_by_fold[fold_for_date[date]]
            adaptive_exposure.loc[date] = signal_maps[symbol][selected].loc[date]
        for cost in costs:
            portfolio_signals = {
                candidate: signal_maps[symbol][candidate].reindex(pseudo_index)
                for candidate in CANDIDATE_NAMES
            }
            portfolio_signals["adaptive_selector"] = adaptive_exposure
            for portfolio, exposure in portfolio_signals.items():
                result = backtest(open_pseudo, exposure, cost)
                stitched_results[(symbol, cost, portfolio)] = result
                frame = result.reset_index(names="date")
                frame.insert(1, "symbol", symbol)
                frame.insert(2, "cost_bps", cost)
                frame.insert(3, "portfolio", portfolio)
                frame.insert(4, "fold_id", frame["date"].map(fold_for_date))
                frame.insert(
                    5,
                    "selected_candidate",
                    frame["fold_id"].map(selected_by_fold)
                    if portfolio == "adaptive_selector"
                    else portfolio,
                )
                daily_frames.append(frame)
                pseudo_rows.append(
                    {
                        "symbol": symbol,
                        "cost_bps": cost,
                        "portfolio": portfolio,
                        "portfolio_type": "adaptive" if portfolio == "adaptive_selector" else "frozen",
                        **metrics(result),
                    }
                )
    walk_forward_daily = pd.concat(daily_frames, ignore_index=True)
    pseudo_summary = pd.DataFrame(pseudo_rows)
    frozen_rows = pseudo_summary[pseudo_summary["portfolio_type"] == "frozen"].rename(
        columns={"portfolio": "candidate"}
    )
    frozen_rank = _aggregate_robust_rank(
        frozen_rows[["symbol", "cost_bps", "candidate", "cagr", "sharpe", "calmar", "turnover"]]
    )
    best_frozen = str(frozen_rank.iloc[0]["candidate"])
    config["best_frozen_candidate"] = best_frozen
    config["complete_walk_forward_folds"] = len(folds)
    config["pseudo_oos_sessions"] = len(pseudo_index)
    pseudo_summary = pseudo_summary.merge(
        frozen_rank.rename(columns={"candidate": "portfolio"}),
        on="portfolio",
        how="left",
        validate="many_to_one",
    )
    pseudo_summary["is_best_frozen"] = pseudo_summary["portfolio"].eq(best_frozen)

    latest_rows: list[dict] = []
    for symbol in sorted(REQUIRED_SYMBOLS):
        close = completed[symbol]["Close"].astype(float)
        targets = _latest_target(close)
        for candidate in CANDIDATE_NAMES:
            current_exposure = float(signal_maps[symbol][candidate].loc[evaluation_end])
            target = targets[candidate]
            latest_rows.append(
                {
                    "symbol": symbol,
                    "candidate": candidate,
                    "signal_as_of_date": evaluation_end,
                    "next_model_date": evaluation_end + pd.offsets.BDay(1),
                    "current_open_exposure": current_exposure,
                    "next_open_target_exposure": target,
                    "marker": _transition_marker(current_exposure, target),
                }
            )
    latest_signals = pd.DataFrame(latest_rows)

    marker_frames: list[pd.DataFrame] = []
    trade_frames: list[pd.DataFrame] = []
    base_cost = costs[0]

    def portfolio_result(symbol: str, cost: float, portfolio: str) -> pd.DataFrame:
        if portfolio == "adaptive_selector":
            return stitched_results[(symbol, cost, portfolio)]
        return candidate_results[(symbol, cost, portfolio)]

    for symbol in sorted(REQUIRED_SYMBOLS):
        for portfolio in ("adaptive_selector", best_frozen):
            base = portfolio_result(symbol, base_cost, portfolio)
            marked = pd.DataFrame(
                {
                    "date": base.index,
                    "symbol": symbol,
                    "portfolio": portfolio,
                    "open": base["open"].to_numpy(),
                    "exposure": base["signal"].to_numpy(),
                    "marker": marker_labels(base["signal"]).to_numpy(),
                }
            )
            marker_frames.append(marked)
        for cost in costs:
            for portfolio in ("adaptive_selector", best_frozen):
                ledger = trade_ledger(portfolio_result(symbol, cost, portfolio))
                if not ledger.empty:
                    ledger.insert(0, "cost_bps", cost)
                    ledger.insert(0, "symbol", symbol)
                    ledger.insert(0, "portfolio", portfolio)
                    trade_frames.append(ledger)
    markers = pd.concat(marker_frames, ignore_index=True)
    trade_columns = [
        "portfolio",
        "symbol",
        "cost_bps",
        "entry_date",
        "entry_price",
        "exit_date",
        "exit_price",
        "bars",
        "net_return",
        "is_open",
    ]
    trades = (
        pd.concat(trade_frames, ignore_index=True).reindex(columns=trade_columns)
        if trade_frames
        else pd.DataFrame(columns=trade_columns)
    )

    chart = _price_chart(completed["GC=F"].reindex(evaluation_index), markers, "GC=F", best_frozen)
    source_observation = dict(source_capture.observation)
    preflight_manifest = {
        "created_at": created_at.isoformat(),
        "data_hash": data_hash,
        "git": source_observation["git"],
        "data_manifest": data_manifest,
    }
    _render_report(
        "preflight",
        config,
        preflight_manifest,
        candidate_summary,
        walk_forward_folds,
        pseudo_summary,
        latest_signals,
        chart,
    )
    execution_identity = seal_execution_identity(_provenance_context)
    config_for_identity = dict(config)
    run_id = stable_run_id(config_for_identity, data_hash, execution_identity)
    _run_with_resource_audit_suspended(_provenance_context, source_capture.revalidate)
    source_observation["publication_sha256"] = source_capture.source_identity["sha256"]
    manifest = {
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "data_hash": data_hash,
        "git": source_observation["git"],
        "source_identity": dict(source_capture.source_identity),
        "execution_identity": execution_identity,
        "source_observation": source_observation,
        "data_manifest": data_manifest,
    }
    report = _render_report(
        run_id,
        config,
        manifest,
        candidate_summary,
        walk_forward_folds,
        pseudo_summary,
        latest_signals,
        chart,
    )

    output_root = Path(output_root)
    run_dir = output_root / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_root))
    try:
        (temporary_dir / "config.json").write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n"
        )
        (temporary_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
        )
        candidate_summary.to_csv(temporary_dir / "candidate_summary.csv", index=False)
        walk_forward_folds.to_csv(temporary_dir / "walk_forward_folds.csv", index=False)
        walk_forward_daily.to_csv(temporary_dir / "walk_forward_daily.csv", index=False)
        pseudo_summary.to_csv(temporary_dir / "pseudo_oos_summary.csv", index=False)
        latest_signals.to_csv(temporary_dir / "latest_signals.csv", index=False)
        markers.to_csv(temporary_dir / "markers.csv", index=False)
        trades.to_csv(temporary_dir / "trades.csv", index=False)
        (temporary_dir / "report.html").write_text(report)
        for artifact in temporary_dir.iterdir():
            artifact.chmod(0o644)
        temporary_dir.chmod(0o755)
        revalidate_execution_identity(_provenance_context, execution_identity)
        _run_with_resource_audit_suspended(_provenance_context, source_capture.revalidate)
        temporary_dir.rename(run_dir)
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    return {"run_id": run_id, "run_dir": str(run_dir), "config": config}


def run_round4_research(
    data: dict[str, pd.DataFrame],
    output_root: Path,
    *,
    cost_grid_bps: tuple[float, ...] = (5.0, 20.0),
    analysis_date: str | pd.Timestamp | datetime | None = None,
    data_manifest: dict | None = None,
    source_provenance: dict | None = None,
) -> dict:
    """Launch one fresh, provenance-bound Round 4 worker."""
    return launch_round4_worker(
        data,
        output_root,
        cost_grid_bps=cost_grid_bps,
        analysis_date=analysis_date,
        data_manifest=data_manifest,
        source_provenance=source_provenance,
    )
