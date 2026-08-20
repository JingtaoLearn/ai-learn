import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gold_research.abc import (
    ABC_CANDIDATES,
    adjusted_ohlc,
    abc_candidate_signals,
    run_abc_trend_research,
    select_frozen_candidate,
)
from gold_research.round3 import completed_signal_close


def synthetic_abc(seed: int = 1, periods: int = 1800) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2017-01-02", periods=periods)
    phase = np.arange(periods)
    regime = np.where(
        (phase // 180) % 3 == 0,
        0.0012,
        np.where((phase // 180) % 3 == 1, -0.0007, 0.0002),
    )
    close = 3.0 * np.exp(np.cumsum(regime + rng.normal(0.0, 0.009, periods)))
    open_price = close * np.exp(rng.normal(0.0, 0.002, periods))
    factor = np.linspace(0.72, 1.0, periods)
    return pd.DataFrame(
        {
            "Open": open_price,
            "High": np.maximum(open_price, close) * 1.005,
            "Low": np.minimum(open_price, close) * 0.995,
            "Close": close,
            "Adj Close": close * factor,
            "Volume": 1_000_000,
        },
        index=index,
    )


def verified_manifest(root: Path, frame: pd.DataFrame, name: str = "source") -> dict:
    root.mkdir(parents=True, exist_ok=True)
    csv_path = root / f"{name}.csv"
    clean = frame.reset_index(names="Date")
    clean.to_csv(csv_path, index=False, float_format="%.17g")
    normalized = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
    return {
        "symbol": "601288.SS",
        "url": "https://query1.finance.yahoo.com/v8/finance/chart/601288.SS?interval=1d",
        "csv": str(csv_path),
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "rows": len(frame),
        "data_start": normalized.min().date().isoformat(),
        "data_end": normalized.max().date().isoformat(),
    }


def test_adjusted_ohlc_uses_vendor_adjustment_factor_for_total_return_prices():
    frame = pd.DataFrame(
        {
            "Open": [100.0, 52.0],
            "High": [102.0, 53.0],
            "Low": [99.0, 51.0],
            "Close": [100.0, 52.0],
            "Adj Close": [50.0, 52.0],
        },
        index=pd.bdate_range("2024-01-02", periods=2),
    )
    got = adjusted_ohlc(frame)
    assert got.loc[frame.index[0], "Open"] == pytest.approx(50.0)
    assert got.loc[frame.index[0], "High"] == pytest.approx(51.0)
    assert got.loc[frame.index[0], "Low"] == pytest.approx(49.5)
    assert got["Close"].tolist() == [50.0, 52.0]


@pytest.mark.parametrize("column", ["Open", "Close", "Adj Close"])
def test_adjusted_ohlc_rejects_non_positive_or_non_finite_prices(column):
    frame = synthetic_abc(periods=20)
    frame.iloc[0, frame.columns.get_loc(column)] = 0.0
    with pytest.raises(ValueError, match="finite and strictly positive"):
        adjusted_ohlc(frame)


def test_adjusted_ohlc_preserves_exchange_local_dates_for_timezone_aware_rows():
    frame = synthetic_abc(periods=3)
    frame.index = pd.DatetimeIndex(frame.index).tz_localize("Asia/Shanghai")
    got = adjusted_ohlc(frame)
    assert got.index[0] == frame.index[0].tz_localize(None)


def test_adjusted_ohlc_rejects_nat_session_dates():
    frame = synthetic_abc(periods=3)
    frame.index = pd.DatetimeIndex([frame.index[0], pd.NaT, frame.index[2]])
    with pytest.raises(ValueError, match="session dates"):
        adjusted_ohlc(frame)


def test_completed_signal_close_uses_shanghai_calendar_day_for_aware_cutoff():
    close = pd.Series(
        [1.0, 2.0, 3.0],
        index=pd.DatetimeIndex(["2024-01-01", "2024-01-02", "2024-01-03"]),
    )
    local_cutoff = pd.Timestamp("2024-01-04 00:30", tz="Asia/Shanghai")
    utc_cutoff = local_cutoff.tz_convert("UTC")
    for cutoff in (local_cutoff, utc_cutoff):
        completed, excluded = completed_signal_close(
            close,
            cutoff,
            exchange_timezone="Asia/Shanghai",
        )
        assert completed.index[-1] == pd.Timestamp("2024-01-03")
        assert not excluded


def test_candidate_set_is_small_fixed_and_expresses_buy_strength_sell_weakness():
    expected = {
        "buy_and_hold",
        "breakout_20_10",
        "breakout_40_20",
        "breakout_60_20",
        "breakout_120_40",
    }
    assert set(ABC_CANDIDATES) == expected
    assert len(ABC_CANDIDATES) == 5
    close = synthetic_abc(periods=500)["Adj Close"]
    signals = abc_candidate_signals(close)
    assert list(signals) == ABC_CANDIDATES
    assert all(signal.index.equals(close.index) for signal in signals.values())
    assert all(set(signal.unique()) <= {0.0, 1.0} for signal in signals.values())


def test_breakout_candidate_is_prefix_stable_and_executes_after_signal_close():
    close = synthetic_abc(periods=500)["Adj Close"]
    original = abc_candidate_signals(close)["breakout_40_20"]
    altered = close.copy()
    altered.iloc[-1] *= 100.0
    changed = abc_candidate_signals(altered)["breakout_40_20"]
    assert changed.iloc[-1] == original.iloc[-1]
    pd.testing.assert_series_equal(
        original.iloc[:420],
        abc_candidate_signals(close.iloc[:420])["breakout_40_20"],
    )


def test_candidate_selection_uses_development_rows_only_and_prefers_robust_rank():
    index = pd.bdate_range("2018-01-02", periods=600)
    development = index[:400]
    rows = []
    for candidate, cagr, sharpe, calmar, turnover in [
        ("return_only", 0.30, 0.5, 0.5, 2.0),
        ("balanced", 0.20, 1.5, 1.5, 1.0),
        ("risk_only", 0.10, 2.0, 2.0, 0.5),
    ]:
        rows.append(
            {
                "candidate": candidate,
                "cagr": cagr,
                "sharpe": sharpe,
                "calmar": calmar,
                "turnover": turnover,
            }
        )
    selected, ranking = select_frozen_candidate(pd.DataFrame(rows))
    assert selected == "risk_only"
    assert ranking.iloc[0]["candidate"] == "risk_only"
    assert development.max() < index[400]


def test_run_hash_tracks_intraday_vendor_rows(tmp_path):
    frame = synthetic_abc(periods=2400)
    frame.index = frame.index + pd.Timedelta(hours=1, minutes=30)
    analysis_date = frame.index[-1] + pd.Timedelta(days=1)
    first = run_abc_trend_research(
        frame,
        tmp_path / "first",
        symbol="601288.SS",
        data_manifest=verified_manifest(tmp_path / "manifest-first", frame, "first"),
        holdout_start="2023-01-01",
        analysis_date=analysis_date,
        bootstrap_samples=20,
    )
    changed = frame.copy()
    changed.iloc[200, changed.columns.get_loc("Close")] *= 1.01
    changed.iloc[200, changed.columns.get_loc("Adj Close")] *= 1.01
    second = run_abc_trend_research(
        changed,
        tmp_path / "second",
        symbol="601288.SS",
        data_manifest=verified_manifest(tmp_path / "manifest-second", changed, "second"),
        holdout_start="2023-01-01",
        analysis_date=analysis_date,
        bootstrap_samples=20,
    )
    assert first["run_id"] != second["run_id"]


def test_abc_research_rejects_unverified_or_mislabeled_instrument(tmp_path):
    frame = synthetic_abc(periods=20)
    with pytest.raises(ValueError, match="601288.SS"):
        run_abc_trend_research(
            frame,
            tmp_path,
            symbol="NOT_601288.SS",
            data_manifest={"symbol": "NOT_601288.SS"},
        )
    with pytest.raises(ValueError, match="data manifest"):
        run_abc_trend_research(
            frame,
            tmp_path,
            symbol="601288.SS",
            data_manifest={"symbol": "NOT_601288.SS"},
        )
    with pytest.raises(ValueError, match="source artifact"):
        run_abc_trend_research(
            frame,
            tmp_path,
            symbol="601288.SS",
            data_manifest={
                "symbol": "601288.SS",
                "url": "https://query1.finance.yahoo.com/v8/finance/chart/601288.SS",
                "csv": str(tmp_path / "missing.csv"),
                "csv_sha256": "0" * 64,
                "rows": len(frame),
                "data_start": frame.index.min().date().isoformat(),
                "data_end": frame.index.max().date().isoformat(),
            },
        )
    bad_url_manifest = verified_manifest(tmp_path / "manifest-bad-url", frame, "bad-url")
    bad_url_manifest["url"] = "https://query1.finance.yahoo.com/not-the-api/chart/601288.SS"
    with pytest.raises(ValueError, match="Yahoo"):
        run_abc_trend_research(
            frame,
            tmp_path,
            symbol="601288.SS",
            data_manifest=bad_url_manifest,
        )


def test_abc_research_rejects_noncanonical_holdout_boundary(tmp_path):
    frame = synthetic_abc(periods=2400)
    with pytest.raises(ValueError, match="holdout"):
        run_abc_trend_research(
            frame,
            tmp_path,
            symbol="601288.SS",
            data_manifest=verified_manifest(tmp_path / "manifest-holdout", frame, "holdout"),
            holdout_start="2024-01-01",
        )


def test_research_freezes_candidate_before_holdout_and_emits_immutable_artifacts(tmp_path):
    frame = synthetic_abc(periods=2400)
    holdout_start = pd.Timestamp("2023-01-01")
    analysis_date = frame.index[-1] + pd.Timedelta(days=1)
    manifest = verified_manifest(tmp_path / "manifest-main", frame, "main")
    manifest["source"] = "<script>bad()</script>"
    previous_umask = os.umask(0o077)
    try:
        result = run_abc_trend_research(
            frame,
            tmp_path,
            symbol="601288.SS",
            data_manifest=manifest,
            holdout_start="2023-01-01",
            analysis_date=analysis_date,
            bootstrap_samples=200,
        )
    finally:
        os.umask(previous_umask)

    run_dir = tmp_path / result["run_id"]
    required = {
        "config.json",
        "run_manifest.json",
        "development_summary.csv",
        "holdout_summary.csv",
        "cost_stress_summary.csv",
        "annual_returns.csv",
        "bootstrap.json",
        "daily_returns.csv",
        "trades.csv",
        "trade_path.csv",
        "strategy_chart.svg",
        "latest_signal.json",
        "report.html",
    }
    assert required == {path.name for path in run_dir.iterdir()}
    assert run_dir.stat().st_mode & 0o777 == 0o755
    assert all(path.stat().st_mode & 0o777 == 0o644 for path in run_dir.iterdir())

    config = json.loads((run_dir / "config.json").read_text())
    assert config["symbol"] == "601288.SS"
    assert config["candidate_names"] == ABC_CANDIDATES
    assert config["holdout_start"] == holdout_start.date().isoformat()
    assert config["selection_end"] < config["holdout_start"]
    assert config["development_warmup_sessions"] == 120
    assert config["development_evaluation_start"] > frame.index[0].date().isoformat()
    assert config["execution"] == "completed close signal; next-session adjusted open fill"
    assert config["buy_cost_bps"] == 8.0
    assert config["sell_cost_bps"] == 13.0
    assert config["best_frozen_candidate"] in ABC_CANDIDATES[1:]

    holdout = pd.read_csv(run_dir / "holdout_summary.csv")
    assert set(holdout["candidate"]) == {"buy_and_hold", config["best_frozen_candidate"]}
    assert {"cagr", "cumulative_return", "max_drawdown", "sharpe", "calmar"} <= set(holdout)

    bootstrap = json.loads((run_dir / "bootstrap.json").read_text())
    assert bootstrap["strategy"] == config["best_frozen_candidate"]
    assert bootstrap["benchmark"] == "buy_and_hold"
    assert bootstrap["samples"] == 200

    latest = json.loads((run_dir / "latest_signal.json").read_text())
    assert latest["candidate"] == config["best_frozen_candidate"]
    assert latest["target_exposure"] in {0.0, 1.0}
    assert latest["action"] in {"BUY", "SELL", "HOLD", "CASH"}

    trade_path = pd.read_csv(run_dir / "trade_path.csv")
    assert {"entry_date", "exit_date", "trade_return", "cumulative_return"} <= set(trade_path)
    assert trade_path["cumulative_return"].notna().all()
    chart = (run_dir / "strategy_chart.svg").read_text()
    assert "<svg" in chart
    assert "BUY" in chart and "SELL" in chart

    report = (run_dir / "report.html").read_text()
    assert "Agricultural Bank of China" in report
    assert "retrospective holdout" in report
    assert "CAGR" in report
    assert "Maximum drawdown" in report
    assert "Buy / sell points and cumulative return" in report
    assert "data:image/svg+xml;base64," in report
    assert "Swipe horizontally for the full table" in report
    assert "window.addEventListener('load', refreshAll)" in report
    assert "&lt;script&gt;bad()&lt;/script&gt;" in report
    assert "<script>bad()</script>" not in report
