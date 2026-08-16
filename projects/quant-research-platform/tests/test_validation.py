import numpy as np
import pandas as pd

from gold_research.validation import calendar_year_metrics, paired_block_bootstrap


def test_calendar_year_metrics_preserve_continuous_return_path():
    idx = pd.bdate_range("2019-01-01", "2022-12-31")
    returns = pd.Series(0.0004, index=idx)
    folds = calendar_year_metrics(returns, first_test_year=2020)
    assert list(folds["year"]) == [2020, 2021, 2022]
    assert (folds["observations"] > 250).all()
    assert (folds["total_return"] > 0).all()


def test_paired_block_bootstrap_is_deterministic_and_detects_clear_advantage():
    idx = pd.bdate_range("2018-01-01", periods=1200)
    benchmark = pd.Series(np.tile([0.001, -0.001], 600), index=idx)
    strategy = benchmark + 0.0005
    first = paired_block_bootstrap(strategy, benchmark, samples=500, block_size=20, seed=7)
    second = paired_block_bootstrap(strategy, benchmark, samples=500, block_size=20, seed=7)
    assert first == second
    assert first["annual_return_diff_ci_low"] > 0
    assert first["probability_annual_return_diff_positive"] > 0.99


def test_paired_block_bootstrap_reports_uncertainty_for_identical_paths():
    idx = pd.bdate_range("2018-01-01", periods=800)
    returns = pd.Series(np.tile([0.01, -0.01], 400), index=idx)
    result = paired_block_bootstrap(returns, returns, samples=200, block_size=20, seed=9)
    assert result["annual_return_diff"] == 0
    assert result["annual_return_diff_ci_low"] == 0
    assert result["annual_return_diff_ci_high"] == 0


def test_lower_familywise_alpha_produces_a_wider_interval():
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2018-01-01", periods=1200)
    benchmark = pd.Series(rng.normal(0.0002, 0.01, len(idx)), index=idx)
    strategy = benchmark + pd.Series(rng.normal(0.0001, 0.003, len(idx)), index=idx)
    nominal = paired_block_bootstrap(strategy, benchmark, samples=500, block_size=20, seed=5, alpha=0.05)
    adjusted = paired_block_bootstrap(strategy, benchmark, samples=500, block_size=20, seed=5, alpha=0.005)
    nominal_width = nominal["annual_return_diff_ci_high"] - nominal["annual_return_diff_ci_low"]
    adjusted_width = adjusted["annual_return_diff_ci_high"] - adjusted["annual_return_diff_ci_low"]
    assert adjusted_width > nominal_width
    assert adjusted["confidence_level"] == 0.995