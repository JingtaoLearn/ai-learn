import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.strategy_runner as runner_module
from quant_platform.datasets import publish_snapshot
from quant_platform.strategy_runner import StrategyRunError, run_strategy_config


FIXTURE = Path(__file__).parent / "fixtures" / "strategy" / "daily.csv"
REQUIRED_ARTIFACTS = {
    "config.json",
    "run_manifest.json",
    "daily_replay.csv",
    "events.csv",
    "trades.csv",
    "metrics.json",
    "cost_breakdown.json",
    "report.html",
}


def _foundation(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    snapshot = publish_snapshot(
        frame,
        state,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed-raw-and-adjusted-signal",
        },
    )
    config = {
        "schema_version": 1,
        "dataset": {
            "root": str(state),
            "instrument": "SYNTH.SS",
            "snapshot_id": snapshot["snapshot_id"],
        },
        "output_root": str(tmp_path / "runs"),
        "template": {
            "name": "single_stock_daily_causal",
            "version": "1",
            "parameters": {
                "instrument_display_name": "Synthetic Bank",
                "evaluation_start": "2026-01-06",
                "evaluation_end": "2026-01-12",
                "initial_capital_cny": 100000.0,
                "initial_state": "flat",
                "terminal_handling": "mark_to_market",
                "cost_assumption_label": "Deterministic synthetic research assumption",
            },
        },
        "operators": {
            "fit": {
                "name": "prior_log_ols",
                "version": "1",
                "parameters": {
                    "window_sessions": 2,
                    "price_column": "AdjustedClose",
                },
            },
            "smoothing": {
                "name": "recursive_log_ema",
                "version": "1",
                "parameters": {"span_sessions": 1},
            },
            "statistic": {
                "name": "adjacent_curve_pct_slope",
                "version": "1",
                "parameters": {},
            },
            "decision": {
                "name": "post_start_threshold_crossing_hysteresis",
                "version": "1",
                "parameters": {
                    "buy_threshold_pct_per_day": 1.0,
                    "sell_threshold_abs_pct_per_day": 1.0,
                },
            },
            "sizing": {
                "name": "all_in_all_out_a_share_lots",
                "version": "1",
                "parameters": {"lot_size": 100, "target_fraction": 1.0},
            },
            "cost": {
                "name": "cms_china_a_share",
                "version": "1",
                "parameters": {
                    "commission_rate": 0.0003,
                    "minimum_commission_cny": 5.0,
                    "transfer_fee_rate": 0.00001,
                    "sell_stamp_tax_rate": 0.0005,
                    "buy_slippage_bps": 2.0,
                    "sell_slippage_bps": 3.0,
                },
            },
            "report": {
                "name": "concise_chinese_causal_trade",
                "version": "1",
                "parameters": {},
            },
        },
    }
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_run_publishes_complete_atomic_read_only_artifacts(tmp_path: Path):
    config_path = _foundation(tmp_path)

    published = run_strategy_config(config_path)
    target = Path(published["path"])
    manifest = json.loads((target / "run_manifest.json").read_text())

    assert published["status"] == "CREATED"
    assert set(path.name for path in target.iterdir()) == REQUIRED_ARTIFACTS
    assert target.name == published["run_id"]
    assert manifest["run_id"] == published["run_id"]
    assert manifest["config_sha256"] == published["config_sha256"]
    assert manifest["dataset_snapshot_id"] == published["dataset_snapshot_id"]
    assert manifest["reconciliation"] == {
        "daily_equity": True,
        "event_cash": True,
        "event_positions": True,
        "event_costs": True,
        "trade_events": True,
        "profit_identity": True,
        "trade_net_pnl": True,
    }
    assert set(manifest["files"]) == REQUIRED_ARTIFACTS - {"run_manifest.json"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o444 for path in target.iterdir()
    )


def test_exact_rerun_verifies_and_returns_same_immutable_run(tmp_path: Path):
    config_path = _foundation(tmp_path)
    first = run_strategy_config(config_path)
    before = {
        path.name: path.read_bytes() for path in Path(first["path"]).iterdir()
    }

    second = run_strategy_config(config_path)

    assert second["status"] == "NO_CHANGE"
    assert second["run_id"] == first["run_id"]
    assert {
        path.name: path.read_bytes() for path in Path(second["path"]).iterdir()
    } == before


def test_concurrent_identical_publications_reuse_one_verified_run(
    tmp_path: Path, monkeypatch
):
    config_path = _foundation(tmp_path)
    monkeypatch.setattr(
        runner_module,
        "render_report",
        lambda *args, **kwargs: "<!doctype html><html></html>",
    )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run_strategy_config, [config_path] * 4))

    assert [result["status"] for result in results].count("CREATED") == 1
    assert [result["status"] for result in results].count("NO_CHANGE") == 3
    assert len({result["run_id"] for result in results}) == 1


@pytest.mark.parametrize("artifact", ["metrics.json", "events.csv", "run_manifest.json"])
def test_existing_run_corruption_fails_closed_without_repair(
    tmp_path: Path, artifact: str
):
    config_path = _foundation(tmp_path)
    published = run_strategy_config(config_path)
    target = Path(published["path"])
    path = target / artifact
    target.chmod(0o755)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"corrupt")
    corrupted = path.read_bytes()
    path.chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(StrategyRunError, match="corrupt|immutable|checksum|JSON"):
        run_strategy_config(config_path)

    assert path.read_bytes() == corrupted


@pytest.mark.parametrize("corruption", ["schema_version", "duplicate_key"])
def test_manifest_semantic_and_duplicate_key_corruption_fails_closed(
    tmp_path: Path, corruption: str
):
    config_path = _foundation(tmp_path)
    published = run_strategy_config(config_path)
    target = Path(published["path"])
    manifest_path = target / "run_manifest.json"
    target.chmod(0o755)
    manifest_path.chmod(0o644)
    if corruption == "schema_version":
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] = 999
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        payload = manifest_path.read_text(encoding="utf-8").rstrip()
        manifest_path.write_text(
            payload[:-1] + ', "run_id": "' + published["run_id"] + '"}',
            encoding="utf-8",
        )
    manifest_path.chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(StrategyRunError, match="corrupt|schema|duplicate"):
        run_strategy_config(config_path)


def test_source_identity_changes_run_id(tmp_path: Path, monkeypatch):
    config_path = _foundation(tmp_path)
    first = run_strategy_config(config_path)
    monkeypatch.setattr(
        runner_module,
        "_effective_source_identity",
        lambda: ("f" * 64, {"synthetic.py": "e" * 64}),
    )

    second = run_strategy_config(config_path)

    assert second["run_id"] != first["run_id"]
    assert Path(first["path"]).is_dir()
    assert Path(second["path"]).is_dir()


def test_dataset_tamper_and_publication_failure_leave_no_run(tmp_path: Path, monkeypatch):
    config_path = _foundation(tmp_path)
    config = json.loads(config_path.read_text())
    snapshot = (
        Path(config["dataset"]["root"])
        / "datasets"
        / "SYNTH.SS"
        / config["dataset"]["snapshot_id"]
    )
    (snapshot / "data.parquet").chmod(0o644)
    (snapshot / "data.parquet").write_bytes(b"tampered")

    with pytest.raises(StrategyRunError, match="dataset"):
        run_strategy_config(config_path)
    assert not Path(config["output_root"]).exists()

    config_path = _foundation(tmp_path / "render-failure")
    monkeypatch.setattr(
        runner_module,
        "render_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render failed")),
    )
    with pytest.raises(RuntimeError, match="render failed"):
        run_strategy_config(config_path)
    output_root = Path(json.loads(config_path.read_text())["output_root"])
    assert not output_root.exists() or not any(output_root.iterdir())


def test_configured_instrument_must_match_snapshot_metadata(tmp_path: Path):
    config_path = _foundation(tmp_path)
    config = json.loads(config_path.read_text())
    config["dataset"]["instrument"] = "OTHER.SS"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(StrategyRunError, match="instrument"):
        run_strategy_config(config_path)


def test_runner_uses_verified_frame_and_detects_post_verify_mutation(
    tmp_path: Path, monkeypatch
):
    config_path = _foundation(tmp_path)
    original_bound_snapshot = runner_module._bound_snapshot
    original_replay = runner_module.replay_strategy
    replay_called = False

    def mutate_after_verify(config):
        path, manifest, frame = original_bound_snapshot(config)
        parquet = path / "data.parquet"
        parquet.chmod(0o644)
        parquet.write_bytes(b"mutated after verification")
        return path, manifest, frame

    def record_replay(frame, config):
        nonlocal replay_called
        replay_called = True
        return original_replay(frame, config)

    monkeypatch.setattr(runner_module, "_bound_snapshot", mutate_after_verify)
    monkeypatch.setattr(runner_module, "replay_strategy", record_replay)

    with pytest.raises(StrategyRunError, match="dataset|snapshot"):
        run_strategy_config(config_path)
    assert replay_called is True


def test_persisted_json_is_strict_finite_and_canonical_config_is_bound(tmp_path: Path):
    config_path = _foundation(tmp_path)
    published = run_strategy_config(config_path)
    target = Path(published["path"])

    def reject_constant(value: str):
        raise AssertionError(f"non-finite JSON constant: {value}")

    for name in ("config.json", "metrics.json", "cost_breakdown.json", "run_manifest.json"):
        json.loads(
            (target / name).read_text(encoding="utf-8"),
            parse_constant=reject_constant,
        )
    canonical = json.loads((target / "config.json").read_text())
    assert canonical["dataset"]["snapshot_id"] == published["dataset_snapshot_id"]
