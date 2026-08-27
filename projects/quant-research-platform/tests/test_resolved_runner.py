import json
import hashlib
import stat
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService
from quant_platform.operator_service import OperatorService
from quant_platform.operator_worker import load_published_operator
from quant_platform.resolved_runner import ResolvedAttemptExecutor, build_legacy_config
from quant_platform.schemas import canonical_json_bytes
from quant_platform.strategy_runner import run_strategy_config
from quant_platform.strategy_runner import StrategyRunError

from test_experiment_service import FIXTURE, _task
from test_operator_submission import IMAGE, _passing_validator, _submission


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


def test_custom_operator_uses_a_validated_builtin_placeholder_config(tmp_path: Path):
    catalog, attempt = _attempt(tmp_path)
    attempt["resolved"]["operators"]["fit"]["operator_id"] = "custom_fit"
    attempt["resolved"]["operators"]["fit"]["content_digest"] = "f" * 64

    legacy = build_legacy_config(
        attempt["resolved"],
        state_root=catalog.state_root,
        output_root=tmp_path / "runs",
    )

    assert legacy["operators"]["fit"]["name"] == "prior_log_ols"
    assert legacy["operators"]["fit"]["parameters"]["window_sessions"] == 20


def test_all_seven_custom_implementations_run_through_one_composed_replay_call(
    tmp_path: Path, monkeypatch
):
    catalog, original_attempt = _attempt(tmp_path)
    operator_service = OperatorService(
        catalog, validator=_passing_validator, runner_image=IMAGE
    )
    task = original_attempt["requested"]
    implementations = {}
    implementation_parameters = {}
    for slot in task["operators"]:
        operator_service.submit(_submission(slot=slot))
        task["operators"][slot] = {
            "operator_id": f"fixture_{slot}",
            "version": "1.0.0",
            "parameters": {"window": 2},
        }
        detail = operator_service.detail(f"fixture_{slot}", "1.0.0")
        loaded_slot, implementation = load_published_operator(
            catalog.state_root / detail["bundle_path"]
        )
        assert loaded_slot == slot
        implementations[slot] = implementation
        implementation_parameters[slot] = {"window": 2}
    service = ExperimentService(catalog, execution_identity={"runner": "test"})
    created = service.submit(task, action_id="custom-create")
    attempt = service.attempt_detail(created["attempt_id"])
    legacy = build_legacy_config(
        attempt["resolved"],
        state_root=catalog.state_root,
        output_root=tmp_path / "custom-runs",
    )
    config_path = tmp_path / "custom.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    composition_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "experiment_id": attempt["experiment_id"],
                "operators": attempt["resolved"]["operators"],
                "execution_identity": attempt["resolved"]["execution_identity"],
            }
        )
    ).hexdigest()
    result = run_strategy_config(
        config_path,
        project_root=PROJECT_ROOT,
        implementations=implementations,
        implementation_parameters=implementation_parameters,
        composition_digest=composition_digest,
    )

    report = (Path(result["path"]) / "report.html").read_text(encoding="utf-8")
    assert "<h1>Synthetic Bank</h1>" in report
    manifest = json.loads((Path(result["path"]) / "run_manifest.json").read_text())
    assert manifest["identity"]["composition_digest"] == composition_digest

    launches = []

    def launch(command, **kwargs):
        launches.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "CREATED",
                    "run_id": result["run_id"],
                    "path": f"/artifacts/{result['run_id']}",
                }
            )
            + "\n",
            stderr="",
        )

    monkeypatch.setattr("quant_platform.resolved_runner.subprocess.run", launch)
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "custom-runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
    )
    executed = executor(attempt)

    assert executed["result_path"] == result["path"]
    assert len(launches) == 1
    assert launches[0].count("docker") == 1
    assert sum(
        f"dst=/operators/{slot},readonly" in argument
        for argument in launches[0]
        for slot in task["operators"]
    ) == 7


def test_custom_series_operators_receive_only_growing_causal_prefixes(tmp_path: Path):
    catalog, attempt = _attempt(tmp_path)
    legacy = build_legacy_config(
        attempt["resolved"],
        state_root=catalog.state_root,
        output_root=tmp_path / "prefix-runs",
    )
    config_path = tmp_path / "prefix.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    observed = {"smoothing": [], "statistic": [], "decision": []}

    def fit(payload, parameters):
        return payload["values"][-1]

    def smoothing(payload, parameters):
        observed["smoothing"].append(len(payload["values"]))
        return payload["values"]

    def statistic(payload, parameters):
        observed["statistic"].append(len(payload["values"]))
        return [None] + [0.0] * (len(payload["values"]) - 1)

    def decision(payload, parameters):
        observed["decision"].append(len(payload["statistics"]))
        return [
            {"action": "HOLD", "reason": "PREFIX_ONLY"}
            for _ in payload["statistics"]
        ]

    run_strategy_config(
        config_path,
        project_root=PROJECT_ROOT,
        implementations={
            "fit": fit,
            "smoothing": smoothing,
            "statistic": statistic,
            "decision": decision,
        },
        implementation_parameters={
            slot: {} for slot in ("fit", "smoothing", "statistic", "decision")
        },
        composition_digest="b" * 64,
    )

    for lengths in observed.values():
        assert lengths == sorted(lengths)
        assert len(lengths) > 1
        assert len(set(lengths)) == len(lengths)


def test_custom_report_cannot_mutate_canonical_metrics(tmp_path: Path):
    catalog, attempt = _attempt(tmp_path)
    legacy = build_legacy_config(
        attempt["resolved"],
        state_root=catalog.state_root,
        output_root=tmp_path / "mutation-runs",
    )
    config_path = tmp_path / "mutation.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    def mutating_report(payload, parameters):
        payload["metrics"].clear()
        return "<!doctype html><html></html>"

    with pytest.raises(StrategyRunError, match="mutated"):
        run_strategy_config(
            config_path,
            project_root=PROJECT_ROOT,
            implementations={"report": mutating_report},
            implementation_parameters={"report": {}},
            composition_digest="c" * 64,
        )
