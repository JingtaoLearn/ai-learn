from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import backtest, metrics
from .run import _data_hash, canonical_hash, get_git_state, stable_run_id
from .strategies import (
    absolute_momentum_signal,
    buy_hold_signal,
    donchian_signal,
    multi_horizon_momentum_signal,
    risk_managed_trend_signal,
    sma_signal,
    trend_filter_signal,
)
from .validation import calendar_year_metrics, paired_block_bootstrap


STRATEGY_NAMES = [
    "buy_and_hold",
    "sma_50_200",
    "donchian_55_20",
    "trend_200",
    "momentum_252",
    "momentum_vote_63_126_252",
    "trend_200_vol10",
]


def round2_signals(close: pd.Series) -> dict[str, pd.Series]:
    return {
        "buy_and_hold": buy_hold_signal(close),
        "sma_50_200": sma_signal(close, 50, 200),
        "donchian_55_20": donchian_signal(close, 55, 20),
        "trend_200": trend_filter_signal(close, 200),
        "momentum_252": absolute_momentum_signal(close, 252),
        "momentum_vote_63_126_252": multi_horizon_momentum_signal(close, (63, 126, 252), 2),
        "trend_200_vol10": risk_managed_trend_signal(close, 200, 60, 0.10),
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
        item = item.sort_index().dropna(subset=["Open", "Close"])
        if item.index.has_duplicates:
            raise ValueError(f"{symbol} contains duplicate dates")
        normalized[symbol] = item
    return normalized


def _parameter_signals(close: pd.Series) -> dict[str, pd.Series]:
    signals = {}
    for window in (150, 200, 250):
        signals[f"trend_{window}"] = trend_filter_signal(close, window)
    for lookback in (189, 252, 315):
        signals[f"momentum_{lookback}"] = absolute_momentum_signal(close, lookback)
    for entry in (50, 55, 60):
        signals[f"donchian_{entry}_20"] = donchian_signal(close, entry, 20)
    for target in (0.08, 0.10, 0.12):
        signals[f"trend_200_vol{int(target * 100)}"] = risk_managed_trend_signal(close, 200, 60, target)
    return signals


def _next_session_signals(close: pd.Series) -> dict[str, float]:
    next_date = close.index[-1] + pd.offsets.BDay(1)
    extended = pd.concat([close, pd.Series([close.iloc[-1]], index=[next_date])])
    return {name: float(signal.iloc[-1]) for name, signal in round2_signals(extended).items()}


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
    view = base[["symbol", "strategy", "cagr", "max_drawdown", "sharpe", "market_exposure"]].copy()
    for column in ["cagr", "max_drawdown", "market_exposure"]:
        view[column] = view[column].map(lambda value: f"{value:.1%}")
    view["sharpe"] = view["sharpe"].map(lambda value: f"{value:.2f}")
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
        .agg(years=("year", "count"), positive_year_rate=("positive", "mean"), median_year_return=("total_return", "median"))
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gold strategy robustness round 2</title><style>body{{font:15px/1.55 system-ui,sans-serif;max-width:1100px;margin:auto;padding:20px;color:#172033;background:#f4f6f8}}section{{background:white;padding:16px;margin:14px 0;border-radius:10px}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:7px;border-bottom:1px solid #ddd;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}code{{overflow-wrap:anywhere}}@media(max-width:390px){{body{{padding:10px}}section{{padding:12px}}}}</style></head><body>
<h1>Gold strategy robustness: round 2</h1><p><strong>Research only:</strong> this is not a live-trading recommendation.</p>
<section><h2>Pre-registered design</h2><ul><li>Long-only or cash; no leverage and no short selling.</li><li>Prior-close signals execute at the next daily open.</li><li>All strategies share a {config['warmup_bars']}-session warm-up.</li><li>One-way costs: {', '.join(f'{value:g}' for value in config['cost_grid_bps'])} bps.</li><li>Calendar-year pseudo-out-of-sample folds begin in {config['first_test_year']}.</li><li>Paired moving-block bootstrap: {config['bootstrap_samples']} samples, {config['bootstrap_block_size']}-session blocks.</li></ul></section>
<section><h2>Full-period comparison</h2><div class="scroll">{view.to_html(index=False, border=0)}</div></section>
<section><h2>Uncertainty versus buy-and-hold</h2><p>A confidence interval containing zero means the historical advantage is not statistically resolved by this test.</p><div class="scroll">{boot.to_html(index=False, border=0, float_format=lambda value: f'{value:.4f}')}</div></section>
<section><h2>Year-by-year consistency</h2><div class="scroll">{consistency.to_html(index=False, border=0, float_format=lambda value: f'{value:.4f}')}</div></section>
<section><h2>Next modeled session exposure</h2><div class="scroll">{current.to_html(index=False, border=0, float_format=lambda value: f'{value:.3f}')}</div></section>
<section><h2>Provenance</h2><pre><code>{json.dumps({'run_id': run_id, 'config': config, 'data_hash': manifest['data_hash'], 'git': manifest['git']}, indent=2)}</code></pre></section>
</body></html>"""


def run_round2_research(
    data: dict[str, pd.DataFrame],
    output_root: Path,
    *,
    cost_bps: float = 5.0,
    bootstrap_samples: int = 2_000,
    bootstrap_block_size: int = 20,
    first_test_year: int = 2010,
    data_manifest: dict | None = None,
) -> dict:
    data = _normalize(data)
    config = {
        "version": 2,
        "strategies": STRATEGY_NAMES,
        "symbols": sorted(data),
        "cost_bps": float(cost_bps),
        "cost_grid_bps": [float(cost_bps), float(cost_bps * 2), float(cost_bps * 4)],
        "warmup_bars": 315,
        "first_test_year": int(first_test_year),
        "bootstrap_samples": int(bootstrap_samples),
        "bootstrap_block_size": int(bootstrap_block_size),
        "bootstrap_seed": 20260816,
        "familywise_comparisons": 12,
        "bootstrap_alpha": 0.05 / 12,
        "cash_return": "zero; no interest credit",
        "execution": "prior-close signal; next-open fill; open-to-open return",
        "parameter_neighborhoods": {
            "trend_windows": [150, 200, 250],
            "momentum_lookbacks": [189, 252, 315],
            "donchian_entries": [50, 55, 60],
            "volatility_targets": [0.08, 0.10, 0.12],
        },
    }
    data_hash = _data_hash(data)
    git = get_git_state()
    run_id = stable_run_id(config, data_hash, canonical_hash(git))
    run_dir = Path(output_root) / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_hash": data_hash,
        "git": git,
        "data_manifest": data_manifest,
    }

    summary_rows: list[dict] = []
    fold_frames: list[pd.DataFrame] = []
    bootstrap_rows: list[dict] = []
    stability_rows: list[dict] = []
    signal_rows: list[dict] = []
    daily_frames: list[pd.DataFrame] = []

    for symbol, frame in data.items():
        close = frame["Close"].astype(float)
        open_price = frame["Open"].astype(float)
        if len(frame) <= config["warmup_bars"] + 2:
            raise ValueError(f"{symbol} does not have enough rows for the shared warm-up")
        evaluation_index = frame.index[config["warmup_bars"] :]
        open_eval = open_price.reindex(evaluation_index)
        base_results: dict[str, pd.DataFrame] = {}
        for strategy, full_signal in round2_signals(close).items():
            signal = full_signal.reindex(evaluation_index)
            for cost in config["cost_grid_bps"]:
                result = backtest(open_eval, signal, cost)
                summary_rows.append({"symbol": symbol, "strategy": strategy, "cost_bps": cost, **metrics(result)})
                if cost == config["cost_bps"]:
                    base_results[strategy] = result
                    daily = result.reset_index(names="date")
                    daily.insert(0, "strategy", strategy)
                    daily.insert(0, "symbol", symbol)
                    daily_frames.append(daily)
            folds = calendar_year_metrics(base_results[strategy]["net_return"], config["first_test_year"])
            folds.insert(0, "strategy", strategy)
            folds.insert(0, "symbol", symbol)
            fold_frames.append(folds)

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
            bootstrap_rows.append({"symbol": symbol, "strategy": strategy, "benchmark": "buy_and_hold", **inference})

        for strategy, full_signal in _parameter_signals(close).items():
            result = backtest(open_eval, full_signal.reindex(evaluation_index), config["cost_bps"])
            stability_rows.append({"symbol": symbol, "strategy": strategy, **metrics(result)})

        latest_date = close.index[-1]
        next_date = latest_date + pd.offsets.BDay(1)
        for strategy, exposure in _next_session_signals(close).items():
            signal_rows.append(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "latest_observed_date": latest_date,
                    "next_model_date": next_date,
                    "next_open_target_exposure": exposure,
                }
            )

    summary = pd.DataFrame(summary_rows)
    annual_folds = pd.concat(fold_frames, ignore_index=True)
    parameter_stability = pd.DataFrame(stability_rows)
    current_signals = pd.DataFrame(signal_rows)
    daily_returns = pd.concat(daily_frames, ignore_index=True)

    (run_dir / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    summary.to_csv(run_dir / "summary.csv", index=False)
    annual_folds.to_csv(run_dir / "annual_folds.csv", index=False)
    (run_dir / "bootstrap.json").write_text(json.dumps(bootstrap_rows, indent=2) + "\n")
    parameter_stability.to_csv(run_dir / "parameter_stability.csv", index=False)
    current_signals.to_csv(run_dir / "current_signals.csv", index=False)
    daily_returns.to_csv(run_dir / "daily_returns.csv", index=False)
    report = _render_report(run_id, config, manifest, summary, annual_folds, bootstrap_rows, current_signals)
    (run_dir / "report.html").write_text(report)
    return {"run_id": run_id, "run_dir": str(run_dir), "config": config}
