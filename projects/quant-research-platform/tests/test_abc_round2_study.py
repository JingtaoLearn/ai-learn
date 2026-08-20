import hashlib
import json
import shutil
import stat
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gold_research.abc_round2_study import (
    CANDIDATES,
    REQUIRED_SYMBOLS,
    run_abc_round2_study,
)


ARTIFACTS = {
    "config.json",
    "run_manifest.json",
    "trial_registry.csv",
    "candidate_summary.csv",
    "benchmark_summary.csv",
    "peer_validation.csv",
    "target_daily_returns.csv",
    "target_trades.csv",
    "subperiods.csv",
    "random_timing.json",
    "gate_decision.json",
    "current_signals.json",
}


def _price_frame(index: pd.DatetimeIndex, phase: float) -> pd.DataFrame:
    position = np.arange(len(index), dtype=float)
    close = 20.0 * np.exp(0.00012 * position + 0.08 * np.sin(position / 37.0 + phase))
    open_price = close * (1.0 + 0.003 * np.sin(position / 11.0 + phase))
    high = np.maximum(open_price, close) * 1.01
    low = np.minimum(open_price, close) * 0.99
    adjustment = 0.55 + 0.45 * position / max(len(index) - 1, 1)
    frame = pd.DataFrame(
        {
            "Open": open_price,
            "Volume": (1_000_000 + position).astype(int),
            "Close": close,
            "High": high,
            "Low": low,
            "Date": index.strftime("%Y-%m-%d 01:30:00"),
            "Adj Close": close * adjustment,
        }
    )
    frame.loc[frame.index[500], "Open"] = frame.loc[frame.index[500], "High"] * 1.02
    return frame


def _write_manifest(data_dir: Path, frames: dict[str, pd.DataFrame]) -> None:
    manifest = {}
    for symbol, frame in frames.items():
        csv_path = data_dir / f"{symbol}.csv"
        frame.to_csv(csv_path, index=False)
        dates = pd.to_datetime(frame["Date"])
        manifest[symbol] = {
            "symbol": symbol,
            "url": (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                "?period1=1&period2=2&interval=1d&events=history"
            ),
            "csv": str(csv_path),
            "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            "rows": len(frame),
            "data_start": dates.min().date().isoformat(),
            "data_end": dates.max().date().isoformat(),
        }
    (data_dir / "data_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


@pytest.fixture(scope="session")
def round2_inputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("abc-round2-inputs")
    data_dir = root / "data"
    data_dir.mkdir()
    analysis_date = pd.Timestamp("2026-08-20 13:00:00", tz="Asia/Shanghai")
    full_index = pd.bdate_range(end=analysis_date.tz_localize(None).normalize(), periods=3_900)
    frames = {}
    for position, symbol in enumerate(REQUIRED_SYMBOLS):
        index = full_index
        if symbol == "601658.SS":
            index = full_index[full_index >= pd.Timestamp("2019-12-10")]
        frames[symbol] = _price_frame(index, position / 3.0)
    _write_manifest(data_dir, frames)
    protocol = root / "protocol.yaml"
    protocol.write_text("version: 1\nprotocol_id: synthetic-round2\n")
    return data_dir, protocol, analysis_date


def _clone_data_dir(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    manifest_path = destination / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for symbol, entry in manifest.items():
        entry["csv"] = str(destination / f"{symbol}.csv")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return destination


@pytest.mark.parametrize("tamper", ["checksum", "url", "rows"])
def test_runner_rejects_tampered_manifest_evidence(round2_inputs, tmp_path, tamper):
    source, protocol, analysis_date = round2_inputs
    data_dir = _clone_data_dir(source, tmp_path / "data")
    manifest_path = data_dir / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    target = manifest["601288.SS"]
    if tamper == "checksum":
        target["csv_sha256"] = "0" * 64
    elif tamper == "url":
        target["url"] = target["url"].replace("601288.SS", "601398.SS")
    else:
        target["rows"] += 1
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    with pytest.raises(ValueError, match="checksum|canonical Yahoo|row count"):
        run_abc_round2_study(
            data_dir=data_dir,
            output_root=tmp_path / "runs",
            analysis_date=analysis_date,
            protocol_path=protocol,
            timing_samples=3,
        )


def test_runner_writes_complete_non_html_evidence_and_independent_gates(round2_inputs, tmp_path):
    data_dir, protocol, analysis_date = round2_inputs

    result = run_abc_round2_study(
        data_dir=data_dir,
        output_root=tmp_path / "runs",
        analysis_date=analysis_date,
        protocol_path=protocol,
        timing_samples=7,
    )
    run_dir = Path(result["run_dir"])

    assert {path.name for path in run_dir.iterdir()} == ARTIFACTS
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o755
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in run_dir.iterdir())
    assert not list(run_dir.glob("*.html"))

    config = json.loads((run_dir / "config.json").read_text())
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    timing = json.loads((run_dir / "random_timing.json").read_text())
    decisions = json.loads((run_dir / "gate_decision.json").read_text())
    current = json.loads((run_dir / "current_signals.json").read_text())
    for path in run_dir.glob("*.json"):
        text = path.read_text()
        assert "NaN" not in text
        assert "Infinity" not in text

    assert config["symbols"] == list(REQUIRED_SYMBOLS)
    assert config["candidates"] == list(CANDIDATES)
    assert config["warmup_sessions"] == 315
    assert config["execution_complete"] is False
    assert config["excluded_analysis_date_or_later"] is True
    assert manifest["run_id"] == result["run_id"]
    assert manifest["protocol_sha256"] == hashlib.sha256(protocol.read_bytes()).hexdigest()
    assert "winner" not in json.dumps(decisions).lower()
    assert "best" not in json.dumps(decisions).lower()

    registry = pd.read_csv(run_dir / "trial_registry.csv")
    candidates = pd.read_csv(run_dir / "candidate_summary.csv")
    benchmarks = pd.read_csv(run_dir / "benchmark_summary.csv")
    peers = pd.read_csv(run_dir / "peer_validation.csv")
    daily = pd.read_csv(run_dir / "target_daily_returns.csv")
    trades = pd.read_csv(run_dir / "target_trades.csv")
    subperiods = pd.read_csv(run_dir / "subperiods.csv")

    assert registry["candidate"].tolist() == list(CANDIDATES)
    assert len(registry) == 4
    assert set(candidates["candidate"]) == set(CANDIDATES)
    assert set(candidates["scenario"]) == {"base", "stress"}
    assert len(candidates) == 8
    assert set(benchmarks["benchmark"]) == {
        "buy_and_hold",
        "cash_zero",
        "constant_exposure_matched",
    }
    assert len(peers) == 24
    assert set(peers["peer_symbol"]) == set(REQUIRED_SYMBOLS[1:])
    assert set(daily["candidate"]) == set(CANDIDATES)
    assert set(daily["scenario"]) == {"base", "stress"}
    assert set(trades["candidate"]).issubset(CANDIDATES)
    assert set(trades["scenario"]).issubset({"base", "stress"})
    assert set(subperiods["candidate"]) == set(CANDIDATES)

    late_peer = peers.loc[peers["peer_symbol"] == "601658.SS"]
    source_dates = pd.to_datetime(pd.read_csv(data_dir / "601658.SS.csv")["Date"])
    assert late_peer["evaluation_start"].nunique() == 1
    assert late_peer["evaluation_start"].iloc[0] == source_dates.iloc[315].date().isoformat()

    assert set(timing) == set(CANDIDATES)
    assert all(item["requested_samples"] == 7 for item in timing.values())
    assert all(item["evaluated_samples"] == 7 for item in timing.values())
    assert all(item["distinct_shift_count"] == 7 for item in timing.values())
    assert set(current) == set(CANDIDATES)
    assert all(
        item["signal_as_of_date"] < analysis_date.date().isoformat() for item in current.values()
    )
    assert all(item["target_exposure"] in {0.0, 1.0} for item in current.values())

    decision_rows = decisions["candidates"]
    assert [row["candidate"] for row in decision_rows] == list(CANDIDATES)
    assert all(len(row["gates"]) == 8 for row in decision_rows)
    assert all(row["gates"][-1]["status"] == "FAIL" for row in decision_rows)
    for row in decision_rows:
        candidate = row["candidate"]
        wins = int(peers.loc[peers["candidate"] == candidate, "sharpe_improved"].sum())
        peer_gate = next(
            gate
            for gate in row["gates"]
            if gate["id"] == "improves_risk_adjusted_objective_on_at_least_4_of_6_peers"
        )
        assert peer_gate["status"] == ("PASS" if wins >= 4 else "FAIL")
        stability_gate = next(
            gate for gate in row["gates"] if gate["id"] == "stable_across_predeclared_subperiods"
        )
        candidate_blocks = subperiods.loc[subperiods["candidate"] == candidate]
        assert stability_gate["status"] == candidate_blocks["stability_status"].iloc[0]


def test_partial_analysis_day_cannot_change_scored_data_or_current_target(round2_inputs, tmp_path):
    source, protocol, analysis_date = round2_inputs
    changed = _clone_data_dir(source, tmp_path / "changed")
    target_path = changed / "601288.SS.csv"
    lines = target_path.read_text().splitlines()
    columns = lines[0].split(",")
    values = lines[-1].split(",")
    replacements = {
        "Open": "1000.0",
        "High": "1100.0",
        "Low": "900.0",
        "Close": "1050.0",
        "Adj Close": "1050.0",
    }
    for column, value in replacements.items():
        values[columns.index(column)] = value
    lines[-1] = ",".join(values)
    target_path.write_text("\n".join(lines) + "\n")
    manifest_path = changed / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["601288.SS"]["csv_sha256"] = hashlib.sha256(target_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    original_result = run_abc_round2_study(
        data_dir=source,
        output_root=tmp_path / "original-runs",
        analysis_date=analysis_date,
        protocol_path=protocol,
        timing_samples=3,
    )
    changed_result = run_abc_round2_study(
        data_dir=changed,
        output_root=tmp_path / "changed-runs",
        analysis_date=analysis_date,
        protocol_path=protocol,
        timing_samples=3,
    )

    assert original_result["run_id"] == changed_result["run_id"]
    original_dir = Path(original_result["run_dir"])
    changed_dir = Path(changed_result["run_dir"])
    for artifact in (
        "candidate_summary.csv",
        "target_daily_returns.csv",
        "current_signals.json",
        "random_timing.json",
        "gate_decision.json",
    ):
        assert (original_dir / artifact).read_bytes() == (changed_dir / artifact).read_bytes()


def test_run_id_and_artifacts_are_reproducible_and_existing_run_is_immutable(
    round2_inputs, tmp_path
):
    data_dir, protocol, analysis_date = round2_inputs
    kwargs = {
        "data_dir": data_dir,
        "analysis_date": analysis_date,
        "protocol_path": protocol,
        "timing_samples": 3,
    }
    first = run_abc_round2_study(output_root=tmp_path / "one", **kwargs)
    second = run_abc_round2_study(output_root=tmp_path / "two", **kwargs)

    assert first["run_id"] == second["run_id"]
    first_dir = Path(first["run_dir"])
    second_dir = Path(second["run_dir"])
    assert {path.name: path.read_bytes() for path in first_dir.iterdir()} == {
        path.name: path.read_bytes() for path in second_dir.iterdir()
    }

    with pytest.raises(FileExistsError, match="immutable run already exists"):
        run_abc_round2_study(output_root=tmp_path / "one", **kwargs)
    assert not list((tmp_path / "one").glob(f".{first['run_id']}*"))
