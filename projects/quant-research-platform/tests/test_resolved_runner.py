import json
import hashlib
import shutil
import stat
from pathlib import Path

import pandas as pd
import pytest
import quant_platform.resolved_runner as resolved_runner_module
from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService
from quant_platform.operator_service import OperatorService
from quant_platform.operator_worker import load_published_operator
from quant_platform.resolved_runner import (
    ResolvedAttemptExecutor,
    ResolvedExecutionError,
    build_legacy_config,
)
from quant_platform.schemas import canonical_json_bytes
from quant_platform.strategy_runner import run_strategy_config
from quant_platform.strategy_runner import StrategyRunError
from quant_platform.study_datasets import ExecutionDatasetSliceFactory

from test_experiment_service import FIXTURE, _task
from test_operator_submission import IMAGE, _passing_validator, _submission


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_EXECUTION_IDENTITY = {"runner": "test"}


def _test_identity_provider(project_root, runner_image):
    return TEST_EXECUTION_IDENTITY


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
    service = ExperimentService(catalog, execution_identity=TEST_EXECUTION_IDENTITY)
    created = service.submit(_task(snapshot["snapshot_id"]), action_id="create")
    return catalog, service.attempt_detail(created["attempt_id"])


def _derived_attempt(tmp_path: Path, *, custom: bool):
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    parent = publish_snapshot(
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
    parent_manifest = json.loads(
        (Path(parent["path"]) / "manifest.json").read_text(encoding="utf-8")
    )
    derived = ExecutionDatasetSliceFactory(root).materialize(
        {
            "instrument": "SYNTH.SS",
            "snapshot_id": parent["snapshot_id"],
            "canonical_sha256": parent_manifest["canonical_sha256"],
            "lineage": {"kind": "legacy_snapshot"},
        },
        {
            "allowed_start": "2026-01-01",
            "training_through": "2026-01-05",
            "available_through": "2026-01-12",
            "scoring_start": "2026-01-06",
            "scoring_end": "2026-01-12",
            "role": "INNER_SCORE",
            "information_interval": {
                "signal_time": "SESSION_CLOSE",
                "earliest_execution_time": "NEXT_SESSION_OPEN",
                "return_or_label_end_time": "EXECUTION_SESSION_CLOSE",
            },
            "account_policy": "FORCE_FLAT_WITH_COST",
        },
    )
    task = _task(derived["snapshot_id"])
    service = ExperimentService(catalog, execution_identity=TEST_EXECUTION_IDENTITY)
    if custom:
        operators = OperatorService(
            catalog,
            validator=_passing_validator,
            runner_image=IMAGE,
        )
        operators.submit(_submission(slot="fit"))
        task["operators"]["fit"] = {
            "operator_id": "fixture_fit",
            "version": "1.0.0",
            "parameters": {"window": 2},
        }
    service.submit(task, action_id="derived-custom" if custom else "derived-builtin")
    return catalog, service, service.claim_next_attempt(), Path(parent["path"])


def _remove_snapshot(path: Path) -> None:
    path.chmod(0o755)
    for child in path.iterdir():
        child.chmod(0o644)
    shutil.rmtree(path)


def _tamper_snapshot_data(path: Path) -> None:
    parquet_path = path / "data.parquet"
    path.chmod(0o755)
    parquet_path.chmod(0o644)
    parquet_path.write_bytes(parquet_path.read_bytes() + b"tampered")
    parquet_path.chmod(0o444)
    path.chmod(0o555)


def test_builtin_study_attempt_reverifies_tampered_parent_before_runner_launch(
    tmp_path: Path,
    monkeypatch,
):
    catalog, service, attempt, parent_path = _derived_attempt(tmp_path, custom=False)
    _tamper_snapshot_data(parent_path)
    launches = []

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("built-in runner launched before parent preflight")

    monkeypatch.setattr(resolved_runner_module, "run_strategy_config", launch)
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        attempt_controller=service,
        identity_provider=_test_identity_provider,
    )

    with pytest.raises(
        ResolvedExecutionError,
        match="validated Study dataset preflight",
    ):
        executor(attempt)

    assert launches == []


def test_custom_study_attempt_reverifies_missing_parent_before_docker_launch(
    tmp_path: Path,
):
    catalog, service, attempt, parent_path = _derived_attempt(tmp_path, custom=True)
    _remove_snapshot(parent_path)
    launches = []

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("Docker launched before parent preflight")

    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
        attempt_controller=service,
        process_launcher=launch,
        identity_provider=_test_identity_provider,
    )

    with pytest.raises(
        ResolvedExecutionError,
        match="validated Study dataset preflight",
    ):
        executor(attempt)

    assert launches == []
    detail = service.attempt_detail(attempt["attempt_id"])
    assert json.loads(detail["control_json"])["state"] == "CLAIMED"
    assert {
        path.name for path in (catalog.state_root / detail["control_path"]).iterdir()
    } == {"control.json"}


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
        identity_provider=_test_identity_provider,
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
        identity_provider=_test_identity_provider,
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
    tmp_path: Path,
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
    service = ExperimentService(catalog, execution_identity=TEST_EXECUTION_IDENTITY)
    stale = service.claim_next_attempt()
    service.finish_failure(stale["attempt_id"], "superseded test setup")
    service.submit(task, action_id="custom-create")
    attempt = service.claim_next_attempt()
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

    class Process:
        pid = 999999

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def launch(command, **kwargs):
        launches.append(command)
        kwargs["stdout"].write(
            (
                json.dumps(
                {
                    "ok": True,
                    "status": "CREATED",
                    "run_id": result["run_id"],
                    "path": f"/artifacts/{result['run_id']}",
                }
            )
                + "\n"
            ).encode()
        )
        Path(command[command.index("--cidfile") + 1]).write_text(
            "e" * 64, encoding="ascii"
        )
        return Process()

    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "custom-runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
        attempt_controller=service,
        process_launcher=launch,
        container_reconciler=lambda cidfile: True,
        identity_provider=_test_identity_provider,
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
