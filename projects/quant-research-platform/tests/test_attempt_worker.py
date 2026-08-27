from pathlib import Path

import pandas as pd
import pytest

from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService, InvalidAttemptTransition
from quant_platform.worker import SerialAttemptWorker

from test_experiment_service import FIXTURE, _task


def _created(tmp_path: Path):
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
    return service, created


def test_attempt_transitions_pending_running_succeeded_and_sets_first_canonical(tmp_path: Path):
    service, created = _created(tmp_path)

    claimed = service.claim_next_attempt()
    finished = service.finish_success(
        claimed["attempt_id"],
        result_path="/immutable/run-a",
        result_digest="a" * 64,
        logs="completed",
    )

    assert claimed["status"] == "RUNNING"
    assert finished["status"] == "SUCCEEDED"
    assert finished["comparison"] == "CANONICAL"
    assert service.experiment_detail(created["experiment_id"])["canonical_attempt_id"] == created[
        "attempt_id"
    ]


def test_equal_rerun_points_to_canonical_artifact_and_divergence_is_preserved(tmp_path: Path):
    service, created = _created(tmp_path)
    first = service.claim_next_attempt()
    service.finish_success(
        first["attempt_id"], result_path="/immutable/run-a", result_digest="a" * 64
    )

    equal = service.rerun(created["experiment_id"], action_id="equal")
    service.claim_next_attempt()
    equal_finished = service.finish_success(
        equal["attempt_id"], result_path="/duplicate/run", result_digest="a" * 64
    )
    divergent = service.rerun(created["experiment_id"], action_id="divergent")
    service.claim_next_attempt()
    divergent_finished = service.finish_success(
        divergent["attempt_id"], result_path="/immutable/run-b", result_digest="b" * 64
    )

    assert equal_finished["comparison"] == "EQUAL"
    assert equal_finished["result_path"] == "/immutable/run-a"
    assert divergent_finished["comparison"] == "DIVERGENT"
    assert divergent_finished["result_path"] == "/immutable/run-b"
    detail = service.experiment_detail(created["experiment_id"])
    assert detail["canonical_attempt_id"] == first["attempt_id"]
    assert detail["has_divergent_attempt"] is True


def test_failure_never_becomes_canonical_and_illegal_transition_fails(tmp_path: Path):
    service, created = _created(tmp_path)

    failed = service.claim_next_attempt()
    result = service.finish_failure(failed["attempt_id"], "synthetic failure")

    assert result["status"] == "FAILED"
    assert service.experiment_detail(created["experiment_id"])["canonical_attempt_id"] is None
    with pytest.raises(InvalidAttemptTransition, match="RUNNING"):
        service.finish_success(
            failed["attempt_id"], result_path="/bad", result_digest="c" * 64
        )


def test_restart_marks_abandoned_running_attempt_failed_and_never_relaunches_it(
    tmp_path: Path,
):
    service, created = _created(tmp_path)
    running = service.claim_next_attempt()

    recovered = service.recover_abandoned_attempts()
    reclaimed = service.claim_next_attempt()

    assert recovered == 1
    assert reclaimed is None
    recovered_attempt = service.attempt_detail(created["attempt_id"])
    assert recovered_attempt["attempt_id"] == running["attempt_id"]
    assert recovered_attempt["status"] == "FAILED"
    assert recovered_attempt["launch_count"] == 1
    assert "abandoned" in recovered_attempt["logs"].lower()
    assert len(service.list_attempts(created["experiment_id"])) == 1


def test_claim_atomically_records_the_only_allowed_launch(tmp_path: Path):
    service, created = _created(tmp_path)

    claimed = service.claim_next_attempt()

    assert claimed["attempt_id"] == created["attempt_id"]
    assert claimed["launch_count"] == 1
    assert service.claim_next_attempt() is None


def test_serial_worker_claims_one_and_records_bounded_failure(tmp_path: Path):
    service, created = _created(tmp_path)
    worker = SerialAttemptWorker(
        service,
        executor=lambda attempt: (_ for _ in ()).throw(RuntimeError("x" * 20000)),
    )

    assert worker.run_once() is True
    attempt = service.attempt_detail(created["attempt_id"])
    assert attempt["status"] == "FAILED"
    assert len(attempt["logs"]) <= 16384
    assert worker.run_once() is False
