import json
import os

import numpy as np
import pandas as pd
import pytest

from gold_research.round3 import (
    completed_signal_close,
    measured_conclusion,
    run_trend_temperature_research,
)


def synthetic_gold(seed: int, periods: int = 2400) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2005-01-03", periods=periods)
    regime = np.where((np.arange(periods) // 300) % 2 == 0, 0.0007, -0.0002)
    close = 100 * np.exp(np.cumsum(regime + rng.normal(0, 0.008, periods)))
    open_price = close * np.exp(rng.normal(0, 0.001, periods))
    return pd.DataFrame({"Open": open_price, "Close": close}, index=index)


def test_current_signal_excludes_a_same_date_potentially_incomplete_daily_bar():
    close = pd.Series(
        [100.0, 110.0],
        index=pd.to_datetime(["2026-08-14 13:30:00", "2026-08-17 13:30:00"]),
    )
    completed, excluded = completed_signal_close(close, pd.Timestamp("2026-08-17"))
    assert completed.index.tolist() == [pd.Timestamp("2026-08-14 13:30:00")]
    assert excluded is True


def test_measured_conclusion_does_not_claim_unobserved_drawdown_reduction():
    comparison = pd.DataFrame(
        {
            "cagr_difference": [-0.02],
            "sharpe_difference": [-0.10],
            "drawdown_improvement": [-0.05],
        }
    )
    conclusion = measured_conclusion(comparison)
    assert "did not beat" in conclusion
    assert "did not consistently reduce drawdown" in conclusion


def test_failed_round3_run_leaves_no_partial_immutable_directory(tmp_path):
    short = synthetic_gold(3, periods=100)
    with pytest.raises(ValueError, match="enough rows"):
        run_trend_temperature_research({"GC=F": short}, tmp_path, bootstrap_samples=10)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad_price", [0.0, -1.0, np.nan, np.inf, True, False])
def test_round3_rejects_non_finite_or_non_positive_prices(tmp_path, bad_price):
    frame = synthetic_gold(5, periods=400)
    if isinstance(bad_price, bool):
        frame["Open"] = frame["Open"].astype(object)
    frame.iloc[0, frame.columns.get_loc("Open")] = bad_price
    with pytest.raises(ValueError, match="finite and strictly positive"):
        run_trend_temperature_research({"GC=F": frame}, tmp_path, bootstrap_samples=10)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad_cost", [-1.0, np.nan, np.inf])
def test_round3_rejects_invalid_transaction_costs(tmp_path, bad_cost):
    with pytest.raises(ValueError, match="cost_bps"):
        run_trend_temperature_research(
            {"GC=F": synthetic_gold(6, periods=400)},
            tmp_path,
            cost_bps=bad_cost,
            bootstrap_samples=10,
        )
    assert not list(tmp_path.iterdir())


def test_round3_emits_pre_registered_temperature_research_artifacts(tmp_path):
    data = {"GC=F": synthetic_gold(1), "GLD": synthetic_gold(2)}
    previous_umask = os.umask(0o077)
    try:
        result = run_trend_temperature_research(
            data,
            tmp_path,
            cost_bps=5.0,
            bootstrap_samples=100,
            first_test_year=2010,
            analysis_date="2026-08-17",
        )
    finally:
        os.umask(previous_umask)
    run_dir = tmp_path / result["run_id"]
    assert run_dir.stat().st_mode & 0o777 == 0o755
    required = {
        "config.json",
        "run_manifest.json",
        "summary.csv",
        "annual_folds.csv",
        "bootstrap.json",
        "parameter_stability.csv",
        "current_signals.csv",
        "state_history.csv",
        "trades.csv",
        "report.html",
    }
    assert required <= {path.name for path in run_dir.iterdir()}
    assert all(path.stat().st_mode & 0o777 == 0o644 for path in run_dir.iterdir())
    config = json.loads((run_dir / "config.json").read_text())
    assert config["analysis_date"] == "2026-08-17"
    assert config["temperature_model"] == {
        "entry_threshold": 1.0,
        "exit_threshold": 0.5,
        "lookback": 63,
        "score": "log return / (daily log-return volatility * sqrt(lookback))",
        "state_thresholds": {"cold": -0.5, "flat": 0.5, "hot": 1.0},
        "target_volatility": 0.1,
        "volatility_window": 60,
    }
    assert config["strategies"] == [
        "buy_and_hold",
        "donchian_55_20",
        "trend_200",
        "momentum_252",
        "temperature_63",
        "temperature_63_vol10",
    ]
    summary = pd.read_csv(run_dir / "summary.csv")
    assert len(summary) == 2 * 6 * 3
    assert set(summary["cost_bps"]) == {5.0, 10.0, 20.0}
    states = pd.read_csv(run_dir / "state_history.csv")
    assert set(states["state"].dropna().unique()) <= {"cold", "flat", "warm", "hot"}
    assert set(states["symbol"]) == {"GC=F", "GLD"}
    bootstrap = json.loads((run_dir / "bootstrap.json").read_text())
    assert len(bootstrap) == 2 * 5
    report = (run_dir / "report.html").read_text()
    assert "Measured conclusion" in report
    assert "Trend Animal-inspired" in report
    assert "not a reconstruction of the proprietary model" in report
    assert "not a live-trading recommendation" in report


def test_analysis_date_changes_run_identity_and_provenance_is_cwd_independent(
    tmp_path, monkeypatch
):
    malicious_symbol = "<script id='probe'>bad()</script>"
    data = {malicious_symbol: synthetic_gold(4)}
    monkeypatch.chdir(tmp_path)
    first = run_trend_temperature_research(
        data,
        tmp_path / "first",
        bootstrap_samples=10,
        first_test_year=2010,
        analysis_date="2026-08-17",
    )
    second = run_trend_temperature_research(
        data,
        tmp_path / "second",
        bootstrap_samples=10,
        first_test_year=2010,
        analysis_date="2026-08-18",
    )
    assert first["run_id"] != second["run_id"]
    manifest = json.loads(
        (tmp_path / "first" / first["run_id"] / "run_manifest.json").read_text()
    )
    assert manifest["git"]["source_hash"] != "unavailable"
    report = (tmp_path / "first" / first["run_id"] / "report.html").read_text()
    assert malicious_symbol not in report
    assert "&lt;script" in report