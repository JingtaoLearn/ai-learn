from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _sharpe(values: np.ndarray) -> float:
    standard_deviation = float(np.std(values, ddof=0))
    if standard_deviation == 0.0:
        return 0.0
    return float(np.mean(values) / standard_deviation * math.sqrt(252))


def calendar_year_metrics(returns: pd.Series, first_test_year: int) -> pd.DataFrame:
    """Summarize a continuous, already-costed return path by calendar test year."""
    clean = returns.astype(float).dropna().sort_index()
    if not isinstance(clean.index, pd.DatetimeIndex):
        raise ValueError("returns must have a DatetimeIndex")
    rows = []
    for year, values in clean.groupby(clean.index.year):
        if int(year) < first_test_year:
            continue
        equity = (1.0 + values).cumprod()
        drawdown = equity / equity.cummax().clip(lower=1.0) - 1.0
        rows.append(
            {
                "year": int(year),
                "observations": int(len(values)),
                "total_return": float(equity.iloc[-1] - 1.0),
                "annual_volatility": float(values.std(ddof=0) * math.sqrt(252)),
                "sharpe": _sharpe(values.to_numpy()),
                "max_drawdown": float(drawdown.min()),
            }
        )
    return pd.DataFrame(rows)


def paired_block_bootstrap(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    samples: int = 2_000,
    block_size: int = 20,
    seed: int = 20260816,
    alpha: float = 0.05,
) -> dict[str, float | int]:
    """Paired moving-block bootstrap for return and Sharpe differences."""
    paired = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    values = paired.to_numpy(dtype=float)
    observations = len(values)
    if samples < 1 or block_size < 1 or observations < block_size:
        raise ValueError("samples and block_size must be positive and enough observations are required")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between zero and one")
    annual_return_diff = float(np.mean(values[:, 0] - values[:, 1]) * 252)
    sharpe_diff = _sharpe(values[:, 0]) - _sharpe(values[:, 1])
    rng = np.random.default_rng(seed)
    blocks_needed = math.ceil(observations / block_size)
    max_start = observations - block_size
    annual_diffs = np.empty(samples)
    sharpe_diffs = np.empty(samples)
    for sample in range(samples):
        starts = rng.integers(0, max_start + 1, size=blocks_needed)
        indices = np.concatenate([np.arange(start, start + block_size) for start in starts])[:observations]
        drawn = values[indices]
        annual_diffs[sample] = np.mean(drawn[:, 0] - drawn[:, 1]) * 252
        sharpe_diffs[sample] = _sharpe(drawn[:, 0]) - _sharpe(drawn[:, 1])
    return {
        "observations": observations,
        "samples": samples,
        "block_size": block_size,
        "annual_return_diff": annual_return_diff,
        "confidence_level": float(1.0 - alpha),
        "annual_return_diff_ci_low": float(np.quantile(annual_diffs, alpha / 2.0)),
        "annual_return_diff_ci_high": float(np.quantile(annual_diffs, 1.0 - alpha / 2.0)),
        "probability_annual_return_diff_positive": float(np.mean(annual_diffs > 0.0)),
        "sharpe_diff": float(sharpe_diff),
        "sharpe_diff_ci_low": float(np.quantile(sharpe_diffs, alpha / 2.0)),
        "sharpe_diff_ci_high": float(np.quantile(sharpe_diffs, 1.0 - alpha / 2.0)),
        "probability_sharpe_diff_positive": float(np.mean(sharpe_diffs > 0.0)),
    }