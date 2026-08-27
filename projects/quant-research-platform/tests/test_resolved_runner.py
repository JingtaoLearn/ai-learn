import json
import stat
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService
from quant_platform.resolved_runner import (
    ResolvedExecutionError,
    ResolvedAttemptExecutor,
    build_legacy_config,
)
from quant_platform.strategy_runner import run_strategy_config

from test_experiment_service import FIXTURE, _task


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _attempt(tmp_path: Path):
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    snapshot = publish_snapshot(
        frame,
        root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )
    service = ExperimentService(catalog, execution_identity={"runner": "test"})
    created = service.submit(_task(snapshot["snapshot_id"]), action_id="create")
    return catalog, service.attempt_detail(created["attempt_id"])


def test_resolved_builtins_adapt_to_legacy_registry_without_semantic_change(tmp_path: Path):
    catalog, attempt = _attempt(tmp_path)

    legacy = build_legacy_config(
        attempt["resolved"],
        state_root=catalog.state_root,
        output_root=tmp_path / "resolved-runs",
    )

    assert legacy["template"]["name"] == "single_stock_daily_causal"
    assert legacy["template"]["version"] == "1"
    assert legacy["operators"]["fit"] == {
        "name": "prior_log_ols",
        "version": "1",
        "parameters": {
            "price_column": "AdjustedClose",
            "window_sessions": 2,
        },
    }
    assert all(operator["version"] == "1" for operator in legacy["operators"].values())


def test_resolved_execution_matches_existing_financial_artifacts_and_writes_audit(
    tmp_path: Path,
):
    catalog, attempt = _attempt(tmp_path)
    legacy = build_legacy_config(
        attempt["resolved"],
        state_root=catalog.state_root,
        output_root=tmp_path / "legacy-runs",
    )
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    direct = run_strategy_config(legacy_path, project_root=PROJECT_ROOT)
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "resolved-runs",
        project_root=PROJECT_ROOT,
    )

    resolved = executor(attempt)

    for name in ("metrics.json", "cost_breakdown.json", "events.csv", "trades.csv"):
        assert (Path(resolved["result_path"]) / name).read_bytes() == (
            Path(direct["path"]) / name
        ).read_bytes()
    assert len(resolved["result_digest"]) == 64
    audit_path = catalog.state_root / "attempt-audit" / f"{attempt['attempt_id']}.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["attempt_id"] == attempt["attempt_id"]
    assert audit["experiment_id"] == attempt["experiment_id"]
    assert audit["operators"]["fit"]["resolved_version"] == "1.0.0"
    assert audit["result_digest"] == resolved["result_digest"]
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o444


def test_exact_resolved_rerun_verifies_and_reuses_immutable_artifacts(tmp_path: Path):
    catalog, attempt = _attempt(tmp_path)
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
    )

    first = executor(attempt)
    second = executor(attempt)

    assert first["result_path"] == second["result_path"]
    assert first["result_digest"] == second["result_digest"]
    assert second["logs"] == "Resolved strategy run status: NO_CHANGE"


def test_custom_operator_never_falls_back_to_builtin_execution(tmp_path: Path):
    catalog, attempt = _attempt(tmp_path)
    attempt["resolved"]["operators"]["fit"]["operator_id"] = "custom_fit"
    attempt["resolved"]["operators"]["fit"]["content_digest"] = "f" * 64

    with pytest.raises(ResolvedExecutionError, match="isolated custom fit"):
        build_legacy_config(
            attempt["resolved"],
            state_root=catalog.state_root,
            output_root=tmp_path / "runs",
        )
