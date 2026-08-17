from __future__ import annotations

import html
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import backtest, metrics, trade_ledger
from .run import _data_hash, canonical_hash, get_git_state, stable_run_id
from .strategies import (
    absolute_momentum_signal,
    buy_hold_signal,
    donchian_signal,
    risk_managed_trend_temperature_signal,
    trend_filter_signal,
    trend_temperature,
    trend_temperature_score,
    trend_temperature_signal,
)
from .validation import calendar_year_metrics, paired_block_bootstrap


STRATEGY_NAMES = [
    "buy_and_hold",
    "donchian_55_20",
    "trend_200",
    "momentum_252",
    "temperature_63",
    "temperature_63_vol10",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def temperature_research_signals(close: pd.Series) -> dict[str, pd.Series]:
    return {
        "buy_and_hold": buy_hold_signal(close),
        "donchian_55_20": donchian_signal(close, 55, 20),
        "trend_200": trend_filter_signal(close, 200),
        "momentum_252": absolute_momentum_signal(close, 252),
        "temperature_63": trend_temperature_signal(close, 63, 1.0, 0.5),
        "temperature_63_vol10": risk_managed_trend_temperature_signal(
            close, 63, 1.0, 0.5, 60, 0.10
        ),
    }


def _normalize(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    normalized = {}
    for symbol, frame in data.items():
        item = frame.copy()
        if "Date" in item.columns:
            item["Date"] = pd.to_datetime(item["Date"])
            item = item.set_index("Date")
        item.index = pd.to_datetime(item.index)
        if not {"Open", "Close"} <= set(item.columns):
            raise ValueError(f"{symbol} requires Open and Close columns")
        raw_prices = item.loc[:, ["Open", "Close"]]
        contains_boolean = raw_prices.map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).any().any()
        prices = raw_prices.apply(pd.to_numeric, errors="coerce")
        if (
            contains_boolean
            or not np.isfinite(prices.to_numpy(dtype=float)).all()
            or (prices <= 0).any().any()
        ):
            raise ValueError(f"{symbol} Open and Close must be finite and strictly positive")
        item.loc[:, ["Open", "Close"]] = prices
        item = item.sort_index()
        if item.index.has_duplicates:
            raise ValueError(f"{symbol} contains duplicate dates")
        normalized[symbol] = item
    return normalized


def _parameter_signals(close: pd.Series) -> dict[str, pd.Series]:
    signals: dict[str, pd.Series] = {}
    for lookback in (42, 63, 84):
        signals[f"temperature_lookback_{lookback}"] = trend_temperature_signal(
            close, lookback, 1.0, 0.5
        )
    for entry in (0.8, 1.0, 1.2):
        signals[f"temperature_entry_{entry:.1f}"] = trend_temperature_signal(
            close, 63, entry, 0.5
        )
    for exit_ in (0.25, 0.5, 0.75):
        signals[f"temperature_exit_{exit_:.2f}"] = trend_temperature_signal(
            close, 63, 1.0, exit_
        )
    for target in (0.08, 0.10, 0.12):
        signals[f"temperature_vol_target_{target:.2f}"] = (
            risk_managed_trend_temperature_signal(close, 63, 1.0, 0.5, 60, target)
        )
    return signals


def _next_session_signals(close: pd.Series) -> dict[str, float]:
    next_date = close.index[-1] + pd.offsets.BDay(1)
    extended = pd.concat([close, pd.Series([close.iloc[-1]], index=[next_date])])
    return {
        name: float(signal.iloc[-1])
        for name, signal in temperature_research_signals(extended).items()
    }


def completed_signal_close(
    close: pd.Series, analysis_date: pd.Timestamp | datetime
) -> tuple[pd.Series, bool]:
    """Exclude a same-date daily bar that may still be forming."""
    analysis_day = pd.Timestamp(analysis_date)
    if analysis_day.tzinfo is not None:
        analysis_day = analysis_day.tz_convert(None)
    analysis_day = analysis_day.normalize()
    observed_days = pd.DatetimeIndex(close.index)
    if observed_days.tz is not None:
        observed_days = observed_days.tz_convert(None)
    completed = close.loc[observed_days.normalize() < analysis_day]
    if completed.empty:
        raise ValueError("no completed daily bar is available for the current signal")
    return completed, len(completed) < len(close)


def measured_conclusion(comparison: pd.DataFrame) -> str:
    """Describe only performance differences that are present in the supplied results."""
    supported = bool(
        ((comparison["cagr_difference"] > 0) & (comparison["sharpe_difference"] > 0)).any()
    )
    if supported:
        return (
            "At least one frozen temperature variant beat buy-and-hold on both CAGR and "
            "Sharpe for an evaluated proxy. Treat that as a candidate for forward paper "
            "observation, not proof of a durable edge."
        )
    drawdown_improvement = comparison["drawdown_improvement"] > 0
    if bool(drawdown_improvement.all()):
        risk_clause = "All evaluated variants reduced historical maximum drawdown"
    elif bool(drawdown_improvement.any()):
        risk_clause = "Some variants reduced drawdown, but the result was not consistent"
    else:
        risk_clause = "The variants did not consistently reduce drawdown"
    return (
        "The frozen temperature variants did not beat buy-and-hold on both CAGR and Sharpe "
        f"for any evaluated proxy. {risk_clause}; no edge is established by this historical test."
    )


def _render_report(
    run_id: str,
    config: dict,
    manifest: dict,
    summary: pd.DataFrame,
    folds: pd.DataFrame,
    bootstrap: list[dict],
    current: pd.DataFrame,
) -> str:
    base = summary[summary["cost_bps"] == config["cost_bps"]].copy()
    view = base[
        [
            "symbol",
            "strategy",
            "cagr",
            "max_drawdown",
            "sharpe",
            "calmar",
            "win_rate",
            "trade_count",
            "market_exposure",
        ]
    ].copy()
    for column in ["cagr", "max_drawdown", "win_rate", "market_exposure"]:
        view[column] = view[column].map(lambda value: f"{value:.1%}")
    for column in ["sharpe", "calmar"]:
        view[column] = view[column].map(lambda value: f"{value:.2f}")
    boot = pd.DataFrame(bootstrap)[
        [
            "symbol",
            "strategy",
            "annual_return_diff",
            "annual_return_diff_ci_low",
            "annual_return_diff_ci_high",
            "sharpe_diff",
            "sharpe_diff_ci_low",
            "sharpe_diff_ci_high",
        ]
    ]
    consistency = (
        folds.assign(positive=lambda frame: frame["total_return"] > 0)
        .groupby(["symbol", "strategy"], as_index=False)
        .agg(
            years=("year", "count"),
            positive_year_rate=("positive", "mean"),
            median_year_return=("total_return", "median"),
        )
    )
    benchmark = base.loc[base["strategy"] == "buy_and_hold", [
        "symbol", "cagr", "max_drawdown", "sharpe"
    ]].rename(columns={
        "cagr": "benchmark_cagr",
        "max_drawdown": "benchmark_max_drawdown",
        "sharpe": "benchmark_sharpe",
    })
    temperature = base[base["strategy"].str.startswith("temperature")].merge(
        benchmark, on="symbol", validate="many_to_one"
    )
    temperature["cagr_difference"] = temperature["cagr"] - temperature["benchmark_cagr"]
    temperature["sharpe_difference"] = (
        temperature["sharpe"] - temperature["benchmark_sharpe"]
    )
    temperature["drawdown_improvement"] = (
        temperature["max_drawdown"] - temperature["benchmark_max_drawdown"]
    )
    conclusion = measured_conclusion(temperature)
    temperature_view = temperature[[
        "symbol", "strategy", "cagr_difference", "sharpe_difference", "drawdown_improvement"
    ]].copy()
    formula = config["temperature_model"]["score"]
    provenance = html.escape(
        json.dumps(
            {
                "run_id": run_id,
                "config": config,
                "data_hash": manifest["data_hash"],
                "git": manifest["git"],
            },
            indent=2,
        ),
        quote=True,
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trend Animal-inspired gold strategy research</title><style>
:root{{--ink:#172033;--muted:#596579;--paper:#f3f5f7;--card:#fff;--gold:#9a6b12}}
*{{box-sizing:border-box}}body{{font:15px/1.55 system-ui,sans-serif;max-width:1100px;margin:auto;padding:20px;color:var(--ink);background:var(--paper)}}section{{background:var(--card);padding:16px;margin:14px 0;border-radius:10px}}.decision{{border-left:5px solid var(--gold)}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:7px;border-bottom:1px solid #ddd;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}code{{overflow-wrap:anywhere}}.muted{{color:var(--muted)}}@media(max-width:390px){{body{{padding:10px}}section{{padding:12px}}}}
</style></head><body>
<p class="muted">REPRODUCIBLE RESEARCH · RUN {run_id}</p><h1>Trend Animal-inspired gold strategy</h1>
<section class="decision"><h2>Measured conclusion</h2><p><strong>{conclusion}</strong></p><div class="scroll">{temperature_view.to_html(index=False, border=0, float_format=lambda value: f'{value:.4f}')}</div></section>
<section class="decision"><h2>Decision boundary</h2><p>This is a transparent, testable interpretation of public trend-following ideas. It is <strong>not a reconstruction of the proprietary model</strong> and not a live-trading recommendation.</p><p>The model buys only after trend strength reaches “hot”, stays invested while it remains “warm” or “hot”, and exits after it cools below the frozen exit threshold. A second version caps exposure to a 10% annualized volatility target.</p></section>
<section><h2>Frozen model before inspecting results</h2><ul><li>Temperature score: <code>{formula}</code>.</li><li>States: cold &lt; -0.5; flat &lt; 0.5; warm &lt; 1.0; hot ≥ 1.0.</li><li>Enter at score ≥ 1.0; exit at score &lt; 0.5; signals execute at the following daily open.</li><li>Lookback: 63 sessions; risk window: 60 sessions; exposure capped at 1.0.</li><li>Shared warm-up: {config['warmup_bars']} sessions; one-way cost grid: {', '.join(f'{value:g}' for value in config['cost_grid_bps'])} bps.</li></ul></section>
<section><h2>Full-period comparison</h2><div class="scroll">{view.to_html(index=False, border=0)}</div></section>
<section><h2>Uncertainty versus buy-and-hold</h2><p>A confidence interval containing zero means this historical test does not resolve an advantage.</p><div class="scroll">{boot.to_html(index=False, border=0, float_format=lambda value: f'{value:.4f}')}</div></section>
<section><h2>Year-by-year consistency</h2><div class="scroll">{consistency.to_html(index=False, border=0, float_format=lambda value: f'{value:.4f}')}</div></section>
<section><h2>Next modeled session</h2><div class="scroll">{current.to_html(index=False, border=0, float_format=lambda value: f'{value:.3f}')}</div></section>
<section><h2>Limitations</h2><ul><li><code>GC=F</code> is a continuous-futures research proxy with opaque roll construction; <code>GLD</code> is an ETF proxy.</li><li>No futures margin, contract roll, opening-auction slippage, bid/ask spread, financing, tax, or market impact is modeled.</li><li>Current signals conservatively exclude any daily bar dated on the UTC analysis date because it may still be forming.</li><li>The full historical path has already been inspected, so year folds are retrospective stress tests rather than a pristine holdout.</li><li>Public source context: <a href="https://www.xiaoyuzhoufm.com/episode/6890c2138e06fe8de74c266b">trend methodology</a> and <a href="https://www.xiaoyuzhoufm.com/episode/6981c3cc65d831df72398b63">statistical framing</a>.</li></ul></section>
<section><h2>Provenance</h2><pre><code>{provenance}</code></pre></section>
</body></html>"""


def run_trend_temperature_research(
    data: dict[str, pd.DataFrame],
    output_root: Path,
    *,
    cost_bps: float = 5.0,
    bootstrap_samples: int = 2_000,
    bootstrap_block_size: int = 20,
    first_test_year: int = 2010,
    data_manifest: dict | None = None,
    analysis_date: str | pd.Timestamp | datetime | None = None,
) -> dict:
    cost_bps = float(cost_bps)
    if not np.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps must be finite and non-negative")
    data = _normalize(data)
    created_at = datetime.now(timezone.utc)
    analysis_day = (
        pd.Timestamp(analysis_date) if analysis_date is not None else pd.Timestamp(created_at)
    )
    if analysis_day.tzinfo is not None:
        analysis_day = analysis_day.tz_convert(None)
    analysis_day = analysis_day.normalize()
    config = {
        "version": 3,
        "analysis_date": analysis_day.date().isoformat(),
        "strategies": STRATEGY_NAMES,
        "symbols": sorted(data),
        "cost_bps": cost_bps,
        "cost_grid_bps": [cost_bps, cost_bps * 2, cost_bps * 4],
        "warmup_bars": 315,
        "first_test_year": int(first_test_year),
        "bootstrap_samples": int(bootstrap_samples),
        "bootstrap_block_size": int(bootstrap_block_size),
        "bootstrap_seed": 20260817,
        "familywise_comparisons": len(data) * (len(STRATEGY_NAMES) - 1),
        "cash_return": "zero; no interest credit",
        "execution": "prior-close signal; next-open fill; open-to-open return",
        "current_signal_policy": "exclude any daily bar dated on the UTC analysis date",
        "temperature_model": {
            "score": "log return / (daily log-return volatility * sqrt(lookback))",
            "lookback": 63,
            "entry_threshold": 1.0,
            "exit_threshold": 0.5,
            "state_thresholds": {"cold": -0.5, "flat": 0.5, "hot": 1.0},
            "volatility_window": 60,
            "target_volatility": 0.10,
        },
        "parameter_neighborhoods": {
            "lookbacks": [42, 63, 84],
            "entry_thresholds": [0.8, 1.0, 1.2],
            "exit_thresholds": [0.25, 0.5, 0.75],
            "volatility_targets": [0.08, 0.10, 0.12],
        },
    }
    config["bootstrap_alpha"] = 0.05 / config["familywise_comparisons"]
    data_hash = _data_hash(data)
    git = get_git_state(PROJECT_ROOT)
    run_id = stable_run_id(config, data_hash, canonical_hash(git))
    output_root = Path(output_root)
    run_dir = output_root / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    manifest = {
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "data_hash": data_hash,
        "git": git,
        "data_manifest": data_manifest,
    }

    summary_rows: list[dict] = []
    fold_frames: list[pd.DataFrame] = []
    bootstrap_rows: list[dict] = []
    stability_rows: list[dict] = []
    current_rows: list[dict] = []
    state_frames: list[pd.DataFrame] = []
    trade_frames: list[pd.DataFrame] = []

    for symbol, frame in data.items():
        close = frame["Close"].astype(float)
        open_price = frame["Open"].astype(float)
        if len(frame) <= config["warmup_bars"] + 2:
            raise ValueError(f"{symbol} does not have enough rows for the shared warm-up")
        evaluation_index = frame.index[config["warmup_bars"] :]
        open_eval = open_price.reindex(evaluation_index)
        base_results: dict[str, pd.DataFrame] = {}

        score = trend_temperature_score(close, config["temperature_model"]["lookback"])
        state = trend_temperature(score=score)
        history = pd.DataFrame(
            {
                "date": evaluation_index,
                "symbol": symbol,
                "close": close.reindex(evaluation_index).to_numpy(),
                "temperature_score": score.reindex(evaluation_index).to_numpy(),
                "state": state.reindex(evaluation_index).to_numpy(),
            }
        )
        state_frames.append(history)

        for strategy, full_signal in temperature_research_signals(close).items():
            signal = full_signal.reindex(evaluation_index)
            for cost in config["cost_grid_bps"]:
                result = backtest(open_eval, signal, cost)
                summary_rows.append(
                    {"symbol": symbol, "strategy": strategy, "cost_bps": cost, **metrics(result)}
                )
                if cost == config["cost_bps"]:
                    base_results[strategy] = result
            folds = calendar_year_metrics(
                base_results[strategy]["net_return"], config["first_test_year"]
            )
            folds.insert(0, "strategy", strategy)
            folds.insert(0, "symbol", symbol)
            fold_frames.append(folds)
            ledger = trade_ledger(base_results[strategy])
            if not ledger.empty:
                ledger.insert(0, "strategy", strategy)
                ledger.insert(0, "symbol", symbol)
                trade_frames.append(ledger)

        benchmark = base_results["buy_and_hold"]["net_return"]
        bootstrap_start = pd.Timestamp(f"{config['first_test_year']}-01-01")
        for strategy in STRATEGY_NAMES[1:]:
            inference = paired_block_bootstrap(
                base_results[strategy].loc[bootstrap_start:, "net_return"],
                benchmark.loc[bootstrap_start:],
                samples=config["bootstrap_samples"],
                block_size=config["bootstrap_block_size"],
                seed=config["bootstrap_seed"],
                alpha=config["bootstrap_alpha"],
            )
            bootstrap_rows.append(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "benchmark": "buy_and_hold",
                    **inference,
                }
            )

        for strategy, full_signal in _parameter_signals(close).items():
            result = backtest(
                open_eval, full_signal.reindex(evaluation_index), config["cost_bps"]
            )
            stability_rows.append({"symbol": symbol, "strategy": strategy, **metrics(result)})

        signal_close, excluded_latest_bar = completed_signal_close(close, analysis_day)
        signal_score = trend_temperature_score(
            signal_close, config["temperature_model"]["lookback"]
        )
        signal_state = trend_temperature(score=signal_score)
        latest_observed_date = close.index[-1]
        signal_as_of_date = signal_close.index[-1]
        next_date = signal_as_of_date + pd.offsets.BDay(1)
        latest_score = (
            float(signal_score.iloc[-1]) if pd.notna(signal_score.iloc[-1]) else None
        )
        latest_state = (
            None if pd.isna(signal_state.iloc[-1]) else str(signal_state.iloc[-1])
        )
        for strategy, exposure in _next_session_signals(signal_close).items():
            current_rows.append(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "latest_observed_date": latest_observed_date,
                    "signal_as_of_date": signal_as_of_date,
                    "latest_bar_status": (
                        "same_date_excluded" if excluded_latest_bar else "completed"
                    ),
                    "latest_temperature_score": latest_score,
                    "latest_temperature_state": latest_state,
                    "next_model_date": next_date,
                    "next_open_target_exposure": exposure,
                }
            )

    summary = pd.DataFrame(summary_rows)
    annual_folds = pd.concat(fold_frames, ignore_index=True)
    parameter_stability = pd.DataFrame(stability_rows)
    current_signals = pd.DataFrame(current_rows)
    state_history = pd.concat(state_frames, ignore_index=True)
    trades = (
        pd.concat(trade_frames, ignore_index=True)
        if trade_frames
        else pd.DataFrame(
            columns=[
                "symbol",
                "strategy",
                "entry_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "bars",
                "net_return",
                "is_open",
            ]
        )
    )

    output_root.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_root))
    try:
        (temporary_dir / "config.json").write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n"
        )
        (temporary_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
        )
        summary.to_csv(temporary_dir / "summary.csv", index=False)
        annual_folds.to_csv(temporary_dir / "annual_folds.csv", index=False)
        (temporary_dir / "bootstrap.json").write_text(
            json.dumps(bootstrap_rows, indent=2) + "\n"
        )
        parameter_stability.to_csv(temporary_dir / "parameter_stability.csv", index=False)
        current_signals.to_csv(temporary_dir / "current_signals.csv", index=False)
        state_history.to_csv(temporary_dir / "state_history.csv", index=False)
        trades.to_csv(temporary_dir / "trades.csv", index=False)
        report = _render_report(
            run_id, config, manifest, summary, annual_folds, bootstrap_rows, current_signals
        )
        (temporary_dir / "report.html").write_text(report)
        for artifact in temporary_dir.iterdir():
            artifact.chmod(0o644)
        temporary_dir.chmod(0o755)
        temporary_dir.rename(run_dir)
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    return {"run_id": run_id, "run_dir": str(run_dir), "config": config}
