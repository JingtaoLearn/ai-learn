import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gold_research.round4 import (
    CANDIDATE_NAMES,
    _aggregate_robust_rank,
    anchored_walk_forward_folds,
    latest_three_year_index,
    marker_labels,
    round4_signals,
    run_round4_research,
    select_candidate_for_fold,
)
from gold_research.run import _canonical_source_identity, _enumerate_release_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def release_provenance() -> dict[str, str]:
    files, _, _ = _enumerate_release_files(PROJECT_ROOT)
    return {
        "mode": "release",
        "source_root": str(PROJECT_ROOT),
        "expected_source_sha256": str(_canonical_source_identity(files)["sha256"]),
    }


def synthetic_gold(seed: int, periods: int = 1200) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2021-01-04", periods=periods)
    phase = np.arange(periods)
    regime = np.where((phase // 140) % 3 == 0, 0.0010, np.where((phase // 140) % 3 == 1, -0.0005, 0.0003))
    close = 100.0 * np.exp(np.cumsum(regime + rng.normal(0.0, 0.007, periods)))
    open_price = close * np.exp(rng.normal(0.0, 0.001, periods))
    return pd.DataFrame({"Open": open_price, "Close": close}, index=index)


def result_path(index: pd.DatetimeIndex, daily_return: float, turnover: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "net_return": daily_return,
            "turnover": turnover,
            "cost": 0.0,
        },
        index=index,
    )


def test_round4_uses_a_fixed_modest_transparent_candidate_set():
    expected = {
        "buy_and_hold",
        "trend_200",
        "momentum_252",
        "donchian_55_20",
        "momentum_vote_63_126_252",
        "trend_200_vol10",
        "temperature_63",
    }
    assert set(CANDIDATE_NAMES) == expected
    assert len(CANDIDATE_NAMES) == 7
    signals = round4_signals(synthetic_gold(1, 400)["Close"])
    assert list(signals) == CANDIDATE_NAMES
    assert all(signal.index.equals(next(iter(signals.values())).index) for signal in signals.values())


def test_evaluation_window_is_exactly_latest_three_calendar_years():
    index = pd.bdate_range("2019-01-01", "2026-08-17")
    selected = latest_three_year_index(index)
    end = pd.Timestamp("2026-08-17")
    boundary = end - pd.DateOffset(years=3)
    assert selected[-1] == end
    assert selected[0] > boundary
    assert index[index.get_loc(selected[0]) - 1] <= boundary


def test_anchored_folds_expand_training_and_use_disjoint_63_session_tests():
    index = pd.bdate_range("2023-01-02", periods=756)
    folds = anchored_walk_forward_folds(index)
    assert folds
    for number, fold in enumerate(folds):
        assert len(fold["train_index"]) == 252 + number * 63
        assert len(fold["test_index"]) == 63
        assert fold["train_index"][0] == index[0]
        assert fold["train_end"] < fold["test_start"]
        assert set(fold["train_index"]).isdisjoint(fold["test_index"])


def test_fold_selection_uses_only_training_returns_and_has_low_turnover_tie_break():
    index = pd.bdate_range("2024-01-02", periods=315)
    train = index[:252]
    paths = {}
    for symbol in ("GC=F", "GLD"):
        for cost in (5.0, 20.0):
            paths[(symbol, cost, "steady")] = result_path(index, 0.001, 0.02)
            paths[(symbol, cost, "weak")] = result_path(index, -0.001, 0.01)
    selected, ranking = select_candidate_for_fold(paths, train)
    assert selected == "steady"
    assert ranking.iloc[0]["candidate"] == "steady"

    changed_test = {key: value.copy() for key, value in paths.items()}
    for key, path in changed_test.items():
        candidate = key[2]
        path.loc[index[251:], "net_return"] = -0.5 if candidate == "steady" else 0.5
    assert select_candidate_for_fold(changed_test, train)[0] == "steady"

    tied = {}
    for symbol in ("GC=F", "GLD"):
        for cost in (5.0, 20.0):
            tied[(symbol, cost, "high_turnover")] = result_path(index, 0.0, 0.2)
            tied[(symbol, cost, "low_turnover")] = result_path(index, 0.0, 0.1)
    assert select_candidate_for_fold(tied, train)[0] == "low_turnover"

    cost_sensitive = {key: value.copy() for key, value in tied.items()}
    for key, path in cost_sensitive.items():
        if key[2] == "low_turnover":
            path.loc[index[251], "cost"] = 0.5
    assert select_candidate_for_fold(cost_sensitive, train)[0] == "high_turnover"


def test_robust_rank_aggregates_all_three_metrics_before_turnover_tie_break():
    rows = []
    for symbol in ("GC=F", "GLD"):
        for cost in (5.0, 20.0):
            rows.extend(
                [
                    {"symbol": symbol, "cost_bps": cost, "candidate": "return_only", "cagr": 3.0, "sharpe": 1.0, "calmar": 1.0, "turnover": 0.1},
                    {"symbol": symbol, "cost_bps": cost, "candidate": "balanced", "cagr": 2.0, "sharpe": 2.0, "calmar": 2.0, "turnover": 0.1},
                    {"symbol": symbol, "cost_bps": cost, "candidate": "risk_only", "cagr": 1.0, "sharpe": 3.0, "calmar": 3.0, "turnover": 0.1},
                ]
            )
    ranking = _aggregate_robust_rank(pd.DataFrame(rows))
    assert ranking.iloc[0]["candidate"] == "risk_only"
    assert ranking.iloc[0]["aggregate_mean_metric_rank"] == pytest.approx(5.0 / 3.0)


def test_marker_labels_cover_exposure_transitions_and_states():
    index = pd.bdate_range("2026-01-02", periods=6)
    exposure = pd.Series([0.0, 0.7, 1.0, 0.4, 0.0, 0.5], index=index)
    assert marker_labels(exposure).tolist() == ["CASH", "BUY", "ADD", "REDUCE", "SELL", "BUY"]


def test_round4_aligns_daily_proxies_by_session_date_not_vendor_timestamp(tmp_path):
    gc = synthetic_gold(8)
    gld = synthetic_gold(9)
    gc.index = gc.index + pd.Timedelta(hours=4)
    gld.index = gld.index + pd.Timedelta(hours=13, minutes=30)
    result = run_round4_research(
        {"GC=F": gc, "GLD": gld},
        tmp_path,
        analysis_date=gc.index[-1].normalize(),
        source_provenance=release_provenance(),
    )
    config = json.loads((tmp_path / result["run_id"] / "config.json").read_text())
    assert config["evaluation_sessions"] > 700


@pytest.mark.parametrize("bad_price", [0.0, -1.0, np.nan, np.inf, True, False])
def test_round4_rejects_invalid_prices_without_partial_output(tmp_path, bad_price):
    frame = synthetic_gold(2)
    if isinstance(bad_price, bool):
        frame["Open"] = frame["Open"].astype(object)
    frame.iloc[0, frame.columns.get_loc("Open")] = bad_price
    with pytest.raises(ValueError, match="finite and strictly positive"):
        run_round4_research(
            {"GC=F": frame, "GLD": synthetic_gold(3)},
            tmp_path,
            source_provenance=release_provenance(),
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad_costs", [(-1.0, 20.0), (np.nan, 20.0), (5.0, np.inf), (True, 20.0)])
def test_round4_rejects_invalid_costs_without_partial_output(tmp_path, bad_costs):
    with pytest.raises(ValueError, match="cost"):
        run_round4_research(
            {"GC=F": synthetic_gold(4), "GLD": synthetic_gold(5)},
            tmp_path,
            cost_grid_bps=bad_costs,
            source_provenance=release_provenance(),
        )
    assert not list(tmp_path.iterdir())


def test_round4_emits_immutable_walk_forward_artifacts_and_escaped_mobile_report(tmp_path):
    data = {"GC=F": synthetic_gold(6), "GLD": synthetic_gold(7)}
    analysis_date = data["GC=F"].index[-1].normalize()
    previous_umask = os.umask(0o077)
    try:
        result = run_round4_research(
            data,
            tmp_path,
            analysis_date=analysis_date,
            data_manifest={"source": "<script id='probe'>bad()</script>"},
            source_provenance=release_provenance(),
        )
    finally:
        os.umask(previous_umask)

    run_dir = tmp_path / result["run_id"]
    required = {
        "config.json",
        "run_manifest.json",
        "candidate_summary.csv",
        "walk_forward_folds.csv",
        "walk_forward_daily.csv",
        "pseudo_oos_summary.csv",
        "latest_signals.csv",
        "markers.csv",
        "trades.csv",
        "report.html",
    }
    assert required == {path.name for path in run_dir.iterdir()}
    assert run_dir.stat().st_mode & 0o777 == 0o755
    assert all(path.stat().st_mode & 0o777 == 0o644 for path in run_dir.iterdir())

    config = json.loads((run_dir / "config.json").read_text())
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    render_identity = manifest["execution_identity"]["render_identity"]
    assert render_identity["resources"]
    assert any(
        resource["environment_member"].endswith("matplotlibrc")
        and any(owner["distribution_name"] == "matplotlib" for owner in resource["owners"])
        for resource in render_identity["resources"]
    )
    assert all(resource["owners"] for resource in render_identity["resources"])
    assert config["candidate_names"] == CANDIDATE_NAMES
    assert config["cost_grid_bps"] == [5.0, 20.0]
    assert config["evaluation_policy"] == "latest three calendar years ending at latest completed daily bar"
    assert config["selection_metrics"] == ["cagr", "sharpe", "calmar"]
    assert config["minimum_train_sessions"] == 252
    assert config["test_sessions"] == 63
    assert config["evaluation_end"] == data["GC=F"].index[-2].date().isoformat()
    assert config["best_frozen_candidate"] in CANDIDATE_NAMES

    candidate_summary = pd.read_csv(run_dir / "candidate_summary.csv")
    assert len(candidate_summary) == 2 * 2 * len(CANDIDATE_NAMES)
    assert set(candidate_summary["candidate"]) == set(CANDIDATE_NAMES)
    assert set(candidate_summary["cost_bps"]) == {5.0, 20.0}

    folds = pd.read_csv(
        run_dir / "walk_forward_folds.csv",
        parse_dates=[
            "selection_return_end",
            "selection_cost_end",
            "train_end",
            "test_start",
            "test_end",
        ],
    )
    assert (folds["selection_return_end"] < folds["train_end"]).all()
    assert (folds["selection_cost_end"] == folds["train_end"]).all()
    assert (folds["train_end"] < folds["test_start"]).all()
    assert (folds["test_sessions"] == 63).all()
    assert folds.groupby("fold_id")["selected_candidate"].nunique().eq(1).all()

    daily = pd.read_csv(run_dir / "walk_forward_daily.csv")
    assert set(daily["portfolio"]) == {"adaptive_selector", *CANDIDATE_NAMES}
    assert set(daily["cost_bps"]) == {5.0, 20.0}

    pseudo = pd.read_csv(run_dir / "pseudo_oos_summary.csv")
    assert set(pseudo["portfolio"]) == {"adaptive_selector", *CANDIDATE_NAMES}
    assert pseudo[pseudo["portfolio_type"] == "frozen"]["aggregate_robust_rank"].notna().all()
    assert pseudo["is_best_frozen"].sum() == 4

    latest = pd.read_csv(run_dir / "latest_signals.csv")
    assert len(latest) == 2 * len(CANDIDATE_NAMES)
    assert set(latest["marker"]) <= {"BUY", "SELL", "ADD", "REDUCE", "HOLD", "CASH"}
    assert set(latest["signal_as_of_date"]) == {config["evaluation_end"]}

    markers = pd.read_csv(run_dir / "markers.csv", parse_dates=["date"])
    assert set(markers["marker"]) <= {"BUY", "SELL", "ADD", "REDUCE", "HOLD", "CASH"}
    if config["best_frozen_candidate"] == "trend_200_vol10":
        assert {"ADD", "REDUCE"} <= set(
            markers[markers["portfolio"] == "trend_200_vol10"]["marker"]
        )
    assert set(markers["portfolio"]) == {"adaptive_selector", config["best_frozen_candidate"]}
    best_markers = markers[markers["portfolio"] == config["best_frozen_candidate"]]
    assert best_markers["date"].min().date().isoformat() == config["evaluation_start"]

    trades = pd.read_csv(run_dir / "trades.csv")
    assert {"portfolio", "symbol", "cost_bps", "entry_date", "is_open"} <= set(trades)

    report = (run_dir / "report.html").read_text()
    assert "retrospective pseudo-OOS" in report
    assert "pristine holdout" not in report.lower()
    assert "data:image/png;base64," in report
    assert "width=device-width" in report
    assert "<script id='probe'>" not in report
    assert "&lt;script" in report
    assert "not a reconstruction of the proprietary Trend Animal algorithm" in report
    assert "correlated confirmations" in report
    assert "risk-first objective" in report
    assert "fractional ADD/REDUCE" in report

    with pytest.raises(FileExistsError, match="immutable run already exists"):
        run_round4_research(
            data,
            tmp_path,
            analysis_date=analysis_date,
            data_manifest={"source": "<script id='probe'>bad()</script>"},
            source_provenance=release_provenance(),
        )
