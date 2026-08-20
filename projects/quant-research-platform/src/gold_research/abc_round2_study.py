from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
import pandas as pd

from .abc import adjusted_ohlc
from .abc_round2 import abc_round2_signals
from .abc_round2_validation import (
    candidate_gate_decision,
    circular_shift_timing_test,
    constant_exposure_signal,
    subperiod_stability,
)
from .backtest import backtest, metrics, trade_ledger
from .round3 import completed_signal_close
from .run import _data_hash, canonical_hash, get_git_state, stable_run_id

TARGET_SYMBOL = "601288.SS"
REQUIRED_SYMBOLS = (
    TARGET_SYMBOL,
    "601398.SS",
    "601939.SS",
    "601988.SS",
    "601328.SS",
    "601658.SS",
    "600036.SS",
)
CANDIDATES = (
    "d55_20_close",
    "ma_hys_1_200_b1",
    "mom_12m_monthly",
    "dmi_adx_14_25_20",
)
WARMUP_SESSIONS = 315
ARTIFACT_NAMES = (
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
)
SCENARIOS = {
    "base": (8.0, 13.0),
    "stress": (20.0, 25.0),
}
_TRIALS = (
    {
        "candidate": "d55_20_close",
        "family": "donchian",
        "parameters": {"entry_sessions": 55, "exit_sessions": 20},
        "formula": "Enter when close exceeds the prior 55-session high; exit below the prior 20-session low.",
    },
    {
        "candidate": "ma_hys_1_200_b1",
        "family": "moving-average-hysteresis",
        "parameters": {"sma_sessions": 200, "entry_band": 0.01, "exit_band": 0.01},
        "formula": "Enter above 1.01 times SMA(200); exit below 0.99 times SMA(200).",
    },
    {
        "candidate": "mom_12m_monthly",
        "family": "monthly-time-series-momentum",
        "parameters": {"lookback_month_ends": 12, "rebalance": "monthly"},
        "formula": "At each new month, hold when the prior month-end exceeds the month-end 12 months earlier.",
    },
    {
        "candidate": "dmi_adx_14_25_20",
        "family": "directional-movement-trend-strength",
        "parameters": {"wilder_period": 14, "entry_adx": 25, "exit_adx": 20},
        "formula": "Enter on +DI above -DI with ADX above 25; exit on direction reversal or ADX below 20.",
    },
)


def _json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float) and not np.isfinite(value):
        raise ValueError("JSON evidence values must be finite")
    return value


def _json_text(value: object) -> str:
    return (
        json.dumps(
            _json_value(value),
            allow_nan=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    )


def _analysis_day(value: pd.Timestamp | datetime | str) -> tuple[pd.Timestamp, str]:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("Asia/Shanghai")
    else:
        timestamp = timestamp.tz_convert("Asia/Shanghai")
    return timestamp.tz_localize(None).normalize(), timestamp.isoformat()


def _canonical_yahoo_url(url: object, symbol: str) -> bool:
    parsed = urlparse(str(url))
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "query1.finance.yahoo.com"
        and valid_port
        and parsed.username is None
        and parsed.password is None
        and unquote(parsed.path) == f"/v8/finance/chart/{symbol}"
        and not parsed.fragment
    )


def _session_dates(values: pd.Series) -> pd.DatetimeIndex:
    parsed = pd.to_datetime(values, errors="coerce")
    dates = pd.DatetimeIndex(parsed)
    if dates.hasnans:
        raise ValueError("source artifact dates must be valid and non-missing")
    if dates.tz is not None:
        dates = dates.tz_convert("Asia/Shanghai").tz_localize(None)
    return dates.normalize()


def _manifest_csv_path(data_dir: Path, value: object) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = data_dir / path
    return path.resolve()


def _verified_symbol_frame(
    data_dir: Path,
    symbol: str,
    entry: object,
    analysis_date: pd.Timestamp | datetime | str,
) -> tuple[pd.DataFrame, dict[str, object]]:
    required_fields = {
        "symbol",
        "url",
        "csv",
        "csv_sha256",
        "rows",
        "data_start",
        "data_end",
    }
    if not isinstance(entry, dict) or not required_fields <= set(entry):
        raise ValueError(f"data manifest entry for {symbol} is missing required fields")
    if entry["symbol"] != symbol:
        raise ValueError(f"data manifest symbol does not match {symbol}")
    if not _canonical_yahoo_url(entry["url"], symbol):
        raise ValueError(
            f"source artifact URL for {symbol} is not the canonical Yahoo chart endpoint"
        )

    csv_path = _manifest_csv_path(data_dir, entry["csv"])
    if not csv_path.is_file():
        raise ValueError(f"source artifact CSV for {symbol} is missing")
    csv_bytes = csv_path.read_bytes()
    digest = hashlib.sha256(csv_bytes).hexdigest()
    if digest != entry["csv_sha256"]:
        raise ValueError(f"source artifact CSV checksum does not match the manifest for {symbol}")
    try:
        source = pd.read_csv(csv_path, float_precision="round_trip")
    except Exception as error:
        raise ValueError(f"source artifact CSV for {symbol} cannot be read") from error
    if isinstance(entry["rows"], bool) or not isinstance(entry["rows"], int):
        raise ValueError(f"source artifact row count is invalid for {symbol}")
    if len(source) != entry["rows"]:
        raise ValueError(f"source artifact row count does not match the manifest for {symbol}")
    required_columns = {"Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"}
    if not required_columns <= set(source) or source.columns.has_duplicates:
        raise ValueError(f"source artifact columns are incomplete or duplicated for {symbol}")

    dates = _session_dates(source["Date"])
    if dates.has_duplicates:
        raise ValueError(f"source artifact dates contain duplicates for {symbol}")
    if not dates.is_monotonic_increasing:
        raise ValueError(f"source artifact dates are not strictly increasing for {symbol}")
    if (
        entry["data_start"] != dates[0].date().isoformat()
        or entry["data_end"] != dates[-1].date().isoformat()
    ):
        raise ValueError(f"source artifact date range does not match the manifest for {symbol}")

    canonical = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    frame = source.loc[:, canonical].copy()
    frame.index = dates
    adjusted = adjusted_ohlc(frame)
    prices = adjusted.loc[:, ["Open", "High", "Low", "Close", "Adj Close"]]
    numeric = prices.apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all() or (numeric <= 0.0).any().any():
        raise ValueError(f"normalized prices must be finite and strictly positive for {symbol}")
    impossible_signal_geometry = (numeric["High"] < numeric["Low"]) | (
        (numeric["Close"] < numeric["Low"]) | (numeric["Close"] > numeric["High"])
    )
    if impossible_signal_geometry.any():
        raise ValueError(f"normalized signal OHLC relationships are invalid for {symbol}")
    open_outside_range = (numeric["Open"] < numeric["Low"]) | (numeric["Open"] > numeric["High"])

    completed_close, excluded = completed_signal_close(
        adjusted["Close"],
        analysis_date,
        exchange_timezone="Asia/Shanghai",
    )
    completed = adjusted.reindex(completed_close.index)
    if len(completed) <= WARMUP_SESSIONS + 120:
        raise ValueError(f"{symbol} does not have enough completed sessions after warm-up")
    verification = {
        "symbol": symbol,
        "url": str(entry["url"]),
        "csv_sha256": digest,
        "manifest_rows": int(entry["rows"]),
        "manifest_data_start": str(entry["data_start"]),
        "manifest_data_end": str(entry["data_end"]),
        "completed_rows": len(completed),
        "completed_data_end": completed.index[-1].date().isoformat(),
        "open_outside_reported_range_rows": int(open_outside_range.sum()),
        "excluded_analysis_date_or_later": bool(excluded),
    }
    return completed, verification


def _load_verified_data(
    data_dir: Path,
    analysis_date: pd.Timestamp | datetime | str,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, object]]]:
    manifest_path = data_dir / "data_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("data_manifest.json is missing")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("data_manifest.json cannot be read") from error
    if not isinstance(manifest, dict) or set(manifest) != set(REQUIRED_SYMBOLS):
        raise ValueError("data manifest must contain exactly the required seven-symbol universe")
    frames: dict[str, pd.DataFrame] = {}
    evidence = []
    for symbol in REQUIRED_SYMBOLS:
        frame, verification = _verified_symbol_frame(
            data_dir, symbol, manifest[symbol], analysis_date
        )
        frames[symbol] = frame
        evidence.append(verification)
    return frames, evidence


def _metric_row(
    symbol: str,
    candidate: str,
    scenario: str,
    result: pd.DataFrame,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "candidate": candidate,
        "scenario": scenario,
        **metrics(result),
    }


def _trial_registry() -> pd.DataFrame:
    rows = []
    for order, trial in enumerate(_TRIALS, start=1):
        rows.append(
            {
                "trial_order": order,
                "candidate": trial["candidate"],
                "family": trial["family"],
                "parameters_json": json.dumps(trial["parameters"], sort_keys=True),
                "formula": trial["formula"],
                "eligibility": "eligible_complete_config",
            }
        )
    return pd.DataFrame(rows)


def _current_signals(frame: pd.DataFrame) -> dict[str, dict[str, object]]:
    next_date = frame.index[-1] + pd.offsets.BDay(1)
    synthetic = frame.iloc[[-1]].copy()
    synthetic.index = pd.DatetimeIndex([next_date])
    extended = pd.concat([frame, synthetic])
    signals = abc_round2_signals(extended)
    current: dict[str, dict[str, object]] = {}
    for candidate in CANDIDATES:
        previous = float(signals[candidate].iloc[-2])
        target = float(signals[candidate].iloc[-1])
        if previous <= 0.0 < target:
            action = "BUY"
        elif previous > 0.0 >= target:
            action = "SELL"
        else:
            action = "HOLD" if target > 0.0 else "CASH"
        current[candidate] = {
            "signal_as_of_date": frame.index[-1].date().isoformat(),
            "next_model_date": next_date.date().isoformat(),
            "previous_exposure": previous,
            "target_exposure": target,
            "action": action,
            "basis": "last completed real close; synthetic row only exposes next-open target",
        }
    return current


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, lineterminator="\n")


def _publish_artifacts(
    output_root: Path,
    run_id: str,
    json_artifacts: dict[str, object],
    csv_artifacts: dict[str, pd.DataFrame],
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=output_root))
    try:
        for name, value in json_artifacts.items():
            (temporary / name).write_text(_json_text(value))
        for name, frame in csv_artifacts.items():
            _write_csv(temporary / name, frame)
        written = {path.name for path in temporary.iterdir()}
        if written != set(ARTIFACT_NAMES):
            raise RuntimeError("artifact set does not match the frozen contract")
        for path in temporary.iterdir():
            path.chmod(0o644)
        temporary.chmod(0o755)
        os.rename(temporary, run_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return run_dir


def run_abc_round2_study(
    *,
    data_dir: Path | str,
    output_root: Path | str,
    analysis_date: pd.Timestamp | datetime | str,
    protocol_path: Path | str,
    timing_samples: int = 10_000,
    execution_complete: bool = False,
) -> dict[str, object]:
    """Run and atomically publish the frozen seven-symbol Round-2 evidence study."""
    if (
        isinstance(timing_samples, bool)
        or not isinstance(timing_samples, int)
        or timing_samples < 1
    ):
        raise ValueError("timing_samples must be a positive integer")
    if not isinstance(execution_complete, bool):
        raise ValueError("execution_complete must be bool")
    data_dir = Path(data_dir).resolve()
    protocol_path = Path(protocol_path).resolve()
    if not protocol_path.is_file():
        raise ValueError("protocol file is missing")
    protocol_bytes = protocol_path.read_bytes()
    protocol_hash = hashlib.sha256(protocol_bytes).hexdigest()
    analysis_day, analysis_timestamp = _analysis_day(analysis_date)
    frames, source_evidence = _load_verified_data(data_dir, analysis_date)

    config = {
        "version": 1,
        "protocol_sha256": protocol_hash,
        "analysis_timestamp": analysis_timestamp,
        "analysis_date_shanghai": analysis_day.date().isoformat(),
        "symbols": list(REQUIRED_SYMBOLS),
        "target_symbol": TARGET_SYMBOL,
        "peer_symbols": list(REQUIRED_SYMBOLS[1:]),
        "candidates": list(CANDIDATES),
        "warmup_sessions": WARMUP_SESSIONS,
        "timing_samples": timing_samples,
        "timing_minimum_shift_sessions": 60,
        "timing_seed": 20260820,
        "timing_family_size": 4,
        "cost_scenarios_bps": {
            name: {"buy": costs[0], "sell": costs[1]} for name, costs in SCENARIOS.items()
        },
        "scenario_interpretation": "optimistic comparability scenarios, not verified real fills",
        "execution_complete": execution_complete,
        "excluded_analysis_date_or_later": any(
            bool(item["excluded_analysis_date_or_later"]) for item in source_evidence
        ),
        "execution": "completed close information; next available adjusted open; open-to-open return",
        "cash_return": 0.0,
    }

    candidate_results: dict[str, dict[str, dict[str, pd.DataFrame]]] = {}
    benchmark_results: dict[str, dict[str, dict[str, pd.DataFrame]]] = {}
    signals_by_symbol: dict[str, dict[str, pd.Series]] = {}
    candidate_rows: list[dict[str, object]] = []
    benchmark_rows: list[dict[str, object]] = []

    for symbol in REQUIRED_SYMBOLS:
        frame = frames[symbol]
        full_signals = abc_round2_signals(frame)
        scored = frame.iloc[WARMUP_SESSIONS:]
        signals = {name: full_signals[name].reindex(scored.index) for name in CANDIDATES}
        signals_by_symbol[symbol] = signals
        candidate_results[symbol] = {}
        benchmark_results[symbol] = {}
        for scenario, (buy_cost, sell_cost) in SCENARIOS.items():
            candidate_results[symbol][scenario] = {}
            benchmark_results[symbol][scenario] = {}
            buy_hold = backtest(
                scored["Open"],
                pd.Series(1.0, index=scored.index),
                buy_cost_bps=buy_cost,
                sell_cost_bps=sell_cost,
            )
            cash = backtest(
                scored["Open"],
                pd.Series(0.0, index=scored.index),
                buy_cost_bps=buy_cost,
                sell_cost_bps=sell_cost,
            )
            benchmark_results[symbol][scenario]["buy_and_hold"] = buy_hold
            benchmark_results[symbol][scenario]["cash_zero"] = cash
            for benchmark, result in (("buy_and_hold", buy_hold), ("cash_zero", cash)):
                benchmark_rows.append(
                    {
                        "symbol": symbol,
                        "scenario": scenario,
                        "candidate": "",
                        "benchmark": benchmark,
                        **metrics(result),
                    }
                )
            for candidate in CANDIDATES:
                result = backtest(
                    scored["Open"],
                    signals[candidate],
                    buy_cost_bps=buy_cost,
                    sell_cost_bps=sell_cost,
                )
                candidate_results[symbol][scenario][candidate] = result
                constant_signal = constant_exposure_signal(signals[candidate])
                constant = backtest(
                    scored["Open"],
                    constant_signal,
                    buy_cost_bps=buy_cost,
                    sell_cost_bps=sell_cost,
                )
                benchmark_results[symbol][scenario][candidate] = constant
                benchmark_rows.append(
                    {
                        "symbol": symbol,
                        "scenario": scenario,
                        "candidate": candidate,
                        "benchmark": "constant_exposure_matched",
                        **metrics(constant),
                    }
                )
                if symbol == TARGET_SYMBOL:
                    row = _metric_row(symbol, candidate, scenario, result)
                    row["buy_hold_cagr_difference"] = float(row["cagr"] - metrics(buy_hold)["cagr"])
                    row["constant_cagr_difference"] = float(row["cagr"] - metrics(constant)["cagr"])
                    candidate_rows.append(row)

    candidate_summary = pd.DataFrame(candidate_rows)
    benchmark_summary = pd.DataFrame(benchmark_rows)

    peer_rows = []
    peer_wins: dict[str, int] = {}
    for candidate in CANDIDATES:
        wins = 0
        for peer in REQUIRED_SYMBOLS[1:]:
            candidate_metric = metrics(candidate_results[peer]["base"][candidate])
            buy_hold_metric = metrics(benchmark_results[peer]["base"]["buy_and_hold"])
            improved = bool(candidate_metric["sharpe"] > buy_hold_metric["sharpe"])
            wins += int(improved)
            scored_index = candidate_results[peer]["base"][candidate].index
            peer_rows.append(
                {
                    "candidate": candidate,
                    "peer_symbol": peer,
                    "evaluation_start": scored_index[0].date().isoformat(),
                    "evaluation_end": scored_index[-1].date().isoformat(),
                    "observations": len(scored_index),
                    "candidate_sharpe": candidate_metric["sharpe"],
                    "buy_hold_sharpe": buy_hold_metric["sharpe"],
                    "sharpe_difference": candidate_metric["sharpe"] - buy_hold_metric["sharpe"],
                    "sharpe_improved": improved,
                }
            )
        peer_wins[candidate] = wins
    peer_validation = pd.DataFrame(peer_rows)

    stability: dict[str, dict[str, object]] = {}
    subperiod_rows = []
    for candidate in CANDIDATES:
        summary = subperiod_stability(
            candidate_results[TARGET_SYMBOL]["base"][candidate],
            benchmark_results[TARGET_SYMBOL]["base"][candidate],
        )
        stability[candidate] = summary
        for block in summary["blocks"]:
            subperiod_rows.append(
                {
                    "candidate": candidate,
                    "block_start": pd.Timestamp(block["start"]).date().isoformat(),
                    "block_end": pd.Timestamp(block["end"]).date().isoformat(),
                    "start_year": block["start_year"],
                    "end_year": block["end_year"],
                    "candidate_compounded_return": block["candidate_compounded_return"],
                    "constant_compounded_return": block["constant_compounded_return"],
                    "relative_log_return": block["relative_log_return"],
                    "block_pass": block["pass"],
                    "complete_blocks": summary["complete_blocks"],
                    "positive_relative_log_blocks": summary["positive_relative_log_blocks"],
                    "positive_fraction": summary["positive_fraction"],
                    "stability_status": summary["status"],
                }
            )
    subperiods = pd.DataFrame(subperiod_rows)

    timing = {}
    target_scored_open = frames[TARGET_SYMBOL].iloc[WARMUP_SESSIONS:]["Open"]
    for candidate in CANDIDATES:
        timing[candidate] = circular_shift_timing_test(
            target_scored_open,
            signals_by_symbol[TARGET_SYMBOL][candidate],
            buy_cost_bps=SCENARIOS["base"][0],
            sell_cost_bps=SCENARIOS["base"][1],
            samples=timing_samples,
            min_shift=60,
            seed=20260820,
            family_size=4,
        )

    decisions = []
    for candidate in CANDIDATES:
        decision = candidate_gate_decision(
            metrics(candidate_results[TARGET_SYMBOL]["base"][candidate]),
            metrics(candidate_results[TARGET_SYMBOL]["stress"][candidate]),
            metrics(benchmark_results[TARGET_SYMBOL]["base"]["buy_and_hold"]),
            metrics(benchmark_results[TARGET_SYMBOL]["base"][candidate]),
            timing[candidate],
            stability[candidate],
            peer_wins[candidate],
            execution_complete,
        )
        decisions.append(
            {
                "candidate": candidate,
                **decision,
                "peer_sharpe_wins_count": peer_wins[candidate],
                "execution_complete": execution_complete,
            }
        )
    gate_decision = {"candidates": decisions}

    daily_frames = []
    trade_frames = []
    for scenario in SCENARIOS:
        for candidate in CANDIDATES:
            result = candidate_results[TARGET_SYMBOL][scenario][candidate]
            daily = result.reset_index(names="date")
            daily.insert(0, "scenario", scenario)
            daily.insert(0, "candidate", candidate)
            daily_frames.append(daily)
            ledger = trade_ledger(result)
            if not ledger.empty:
                ledger.insert(0, "scenario", scenario)
                ledger.insert(0, "candidate", candidate)
                trade_frames.append(ledger)
    target_daily = pd.concat(daily_frames, ignore_index=True)
    target_trades = (
        pd.concat(trade_frames, ignore_index=True)
        if trade_frames
        else pd.DataFrame(
            columns=[
                "candidate",
                "scenario",
                "entry_date",
                "entry_price",
                "exit_date",
                "exit_price",
                "bars",
                "net_return",
                "is_open",
            ]
        )
    )
    current = _current_signals(frames[TARGET_SYMBOL])

    data_hash = _data_hash(frames)
    project_root = Path(__file__).resolve().parents[2]
    git = get_git_state(project_root)
    identity_state = canonical_hash(
        {
            "git": git,
            "protocol_sha256": protocol_hash,
            "protocol_bytes_size": len(protocol_bytes),
        }
    )
    run_id = stable_run_id(config, data_hash, identity_state)
    run_manifest = {
        "run_id": run_id,
        "data_hash": data_hash,
        "protocol_sha256": protocol_hash,
        "protocol_bytes_size": len(protocol_bytes),
        "git": git,
        "source_artifacts": source_evidence,
        "artifact_names": list(ARTIFACT_NAMES),
        "research_only": True,
    }

    run_dir = _publish_artifacts(
        Path(output_root).resolve(),
        run_id,
        {
            "config.json": config,
            "run_manifest.json": run_manifest,
            "random_timing.json": timing,
            "gate_decision.json": gate_decision,
            "current_signals.json": current,
        },
        {
            "trial_registry.csv": _trial_registry(),
            "candidate_summary.csv": candidate_summary,
            "benchmark_summary.csv": benchmark_summary,
            "peer_validation.csv": peer_validation,
            "target_daily_returns.csv": target_daily,
            "target_trades.csv": target_trades,
            "subperiods.csv": subperiods,
        },
    )
    return {"run_id": run_id, "run_dir": str(run_dir), "config": config}
