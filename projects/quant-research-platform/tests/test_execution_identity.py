from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService
from quant_platform.operator_service import OperatorService
from quant_platform.resolved_runner import (
    ExecutionIdentityMismatch,
    ResolvedAttemptExecutor,
    effective_execution_identity,
)
from quant_platform.worker import SerialAttemptWorker

from test_experiment_service import FIXTURE, _task
from test_operator_submission import IMAGE, _passing_validator, _submission


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_IDENTITY = {
    "domain_schema": 1,
    "runner": "quant_platform",
    "source_sha256": "a" * 64,
    "runtime": {
        "python": "3.12.11",
        "numpy": "2.3.2",
        "pandas": "2.3.1",
    },
    "runner_image": IMAGE,
}


def _foundation(tmp_path: Path, identity=FROZEN_IDENTITY):
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
    service = ExperimentService(catalog, execution_identity=identity)
    return catalog, service, _task(snapshot["snapshot_id"])


def _identity_provider(identity):
    return lambda project_root, runner_image: deepcopy(identity)


def test_authoritative_identity_binds_effective_source_runtime_and_runner_image():
    identity = effective_execution_identity(
        PROJECT_ROOT,
        IMAGE,
        source_identity_provider=lambda **kwargs: (
            "a" * 64,
            {"source.py": "b" * 64},
            FROZEN_IDENTITY["runtime"],
            {"available": False, "commit": None, "dirty": None},
        ),
    )

    assert identity == FROZEN_IDENTITY


@pytest.mark.parametrize(
    "changed",
    [
        FROZEN_IDENTITY | {"source_sha256": "b" * 64},
        FROZEN_IDENTITY
        | {"runtime": FROZEN_IDENTITY["runtime"] | {"numpy": "9.9.9"}},
        FROZEN_IDENTITY | {"runner_image": "sha256:" + "c" * 64},
    ],
    ids=["source", "runtime", "runner-image"],
)
def test_builtin_identity_mismatch_fails_before_runner_or_artifact(
    tmp_path: Path, monkeypatch, changed
):
    catalog, service, task = _foundation(tmp_path)
    created = service.submit(task, action_id="create")
    attempt = service.claim_next_attempt()
    calls = []
    monkeypatch.setattr(
        "quant_platform.resolved_runner.run_strategy_config",
        lambda *args, **kwargs: calls.append(args),
    )
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
        attempt_controller=service,
        identity_provider=_identity_provider(changed),
    )

    with pytest.raises(ExecutionIdentityMismatch, match="execution identity mismatch"):
        executor(attempt)

    assert calls == []
    assert not (tmp_path / "runs").exists()
    assert service.experiment_detail(created["experiment_id"])["canonical_attempt_id"] is None


def test_all_custom_identity_mismatch_never_starts_physical_process(tmp_path: Path):
    catalog, service, task = _foundation(tmp_path)
    operators = OperatorService(
        catalog, validator=_passing_validator, runner_image=IMAGE
    )
    for slot in task["operators"]:
        operators.submit(_submission(slot=slot))
        task["operators"][slot] = {
            "operator_id": f"fixture_{slot}",
            "version": "1.0.0",
            "parameters": {"window": 2},
        }
    created = service.submit(task, action_id="custom")
    attempt = service.claim_next_attempt()
    launches = []
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
        attempt_controller=service,
        process_launcher=lambda *args, **kwargs: launches.append(args),
        identity_provider=_identity_provider(
            FROZEN_IDENTITY | {"source_sha256": "d" * 64}
        ),
    )

    with pytest.raises(ExecutionIdentityMismatch):
        executor(attempt)

    assert launches == []
    assert service.attempt_detail(created["attempt_id"])["control_json"].find(
        '"state":"CLAIMED"'
    ) >= 0


def test_worker_records_bounded_mismatch_failure_without_canonical_result(
    tmp_path: Path,
):
    catalog, service, task = _foundation(tmp_path)
    created = service.submit(task, action_id="create")
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
        attempt_controller=service,
        identity_provider=_identity_provider(
            FROZEN_IDENTITY | {"source_sha256": "e" * 64}
        ),
    )

    assert SerialAttemptWorker(service, executor=executor).run_once() is True
    attempt = service.attempt_detail(created["attempt_id"])

    assert attempt["status"] == "FAILED"
    assert "execution identity mismatch" in attempt["logs"]
    assert len(attempt["logs"]) <= 16_384
    assert service.experiment_detail(created["experiment_id"])["canonical_attempt_id"] is None


def test_old_rerun_fails_after_upgrade_but_resubmit_creates_new_experiment(
    tmp_path: Path,
):
    catalog, old_service, task = _foundation(tmp_path)
    old = old_service.submit(task, action_id="old-create")
    upgraded_identity = FROZEN_IDENTITY | {"source_sha256": "f" * 64}
    upgraded_executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
        attempt_controller=old_service,
        identity_provider=_identity_provider(upgraded_identity),
    )
    SerialAttemptWorker(old_service, executor=upgraded_executor).run_once()
    rerun = old_service.rerun(old["experiment_id"], action_id="old-rerun")
    SerialAttemptWorker(old_service, executor=upgraded_executor).run_once()

    upgraded_service = ExperimentService(
        catalog, execution_identity=upgraded_identity
    )
    resubmitted = upgraded_service.submit(task, action_id="new-submit")

    assert old_service.attempt_detail(old["attempt_id"])["status"] == "FAILED"
    assert old_service.attempt_detail(rerun["attempt_id"])["status"] == "FAILED"
    assert resubmitted["status"] == "CREATED"
    assert resubmitted["experiment_id"] != old["experiment_id"]
    assert old_service.experiment_detail(old["experiment_id"])["canonical_attempt_id"] is None


def test_matching_identity_executes_successfully(tmp_path: Path):
    catalog, service, task = _foundation(tmp_path)
    created = service.submit(task, action_id="create")
    attempt = service.claim_next_attempt()
    executor = ResolvedAttemptExecutor(
        catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        runner_image=IMAGE,
        attempt_controller=service,
        identity_provider=_identity_provider(FROZEN_IDENTITY),
    )

    result = executor(attempt)
    terminal = service.finish_success(
        attempt["attempt_id"],
        result_path=result["result_path"],
        result_digest=result["result_digest"],
    )

    assert terminal["status"] == "SUCCEEDED"
    assert service.experiment_detail(created["experiment_id"])["canonical_attempt_id"] == attempt[
        "attempt_id"
    ]
