import json

import numpy as np
import pandas as pd

from gold_research.round2 import run_round2_research


def synthetic_gold(seed: int, periods: int = 2400) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2005-01-03", periods=periods)
    regime = np.where((np.arange(periods) // 300) % 2 == 0, 0.0005, -0.0001)
    close = 100 * np.exp(np.cumsum(regime + rng.normal(0, 0.008, periods)))
    overnight = rng.normal(0, 0.001, periods)
    open_price = close * np.exp(overnight)
    return pd.DataFrame({"Open": open_price, "Close": close}, index=index)


def test_round2_run_emits_pre_registered_robustness_artifacts(tmp_path):
    data = {"GC=F": synthetic_gold(1), "GLD": synthetic_gold(2)}
    result = run_round2_research(
        data,
        tmp_path,
        cost_bps=5.0,
        bootstrap_samples=100,
        first_test_year=2010,
    )
    run_dir = tmp_path / result["run_id"]
    required = {
        "config.json",
        "run_manifest.json",
        "summary.csv",
        "annual_folds.csv",
        "bootstrap.json",
        "parameter_stability.csv",
        "current_signals.csv",
        "report.html",
    }
    assert required <= {path.name for path in run_dir.iterdir()}
    config = json.loads((run_dir / "config.json").read_text())
    assert config["strategies"] == [
        "buy_and_hold",
        "sma_50_200",
        "donchian_55_20",
        "trend_200",
        "momentum_252",
        "momentum_vote_63_126_252",
        "trend_200_vol10",
    ]
    summary = pd.read_csv(run_dir / "summary.csv")
    assert len(summary) == 2 * 7 * 3
    assert set(summary["cost_bps"]) == {5.0, 10.0, 20.0}
    folds = pd.read_csv(run_dir / "annual_folds.csv")
    assert folds["year"].min() >= 2010
    assert set(folds["strategy"]) == set(config["strategies"])
    bootstrap = json.loads((run_dir / "bootstrap.json").read_text())
    assert len(bootstrap) == 2 * 6
    assert all(item["benchmark"] == "buy_and_hold" for item in bootstrap)
    report = (run_dir / "report.html").read_text()
    assert "Paired moving-block bootstrap" in report
    assert "not a live-trading recommendation" in report