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


def test_restart_marks_abandoned_running_attempt_interrupted_and_never_relaunches_it(
    tmp_path: Path,
):
    service, created = _created(tmp_path)
    running = service.claim_next_attempt()

    recovered = service.recover_abandoned_attempts(
        container_reconciler=lambda cidfile: True
    )
    reclaimed = service.claim_next_attempt()

    assert recovered == 1
    assert reclaimed is None
    recovered_attempt = service.attempt_detail(created["attempt_id"])
    assert recovered_attempt["attempt_id"] == running["attempt_id"]
    assert recovered_attempt["status"] == "INTERRUPTED"
    assert recovered_attempt["launch_count"] == 1
    assert "abandoned" in recovered_attempt["logs"].lower()
    assert recovered_attempt["control_path"]
    assert len(service.list_attempts(created["experiment_id"])) == 1


def test_claim_atomically_records_the_only_allowed_launch(tmp_path: Path):
    service, created = _created(tmp_path)

    claimed = service.claim_next_attempt()

    assert claimed["attempt_id"] == created["attempt_id"]
    assert claimed["launch_count"] == 1
    control = service.catalog.state_root / claimed["control_path"]
    assert control.is_dir()
    assert (control / "control.json").is_file()
    assert service.claim_next_attempt() is None


def test_physical_launch_control_is_recorded_once_and_terminal_evidence_is_sealed(
    tmp_path: Path,
):
    service, created = _created(tmp_path)
    claimed = service.claim_next_attempt()

    launched = service.record_physical_launch(
        claimed["attempt_id"], container_name="quant-attempt-test"
    )
    with pytest.raises(InvalidAttemptTransition, match="already"):
        service.record_physical_launch(
            claimed["attempt_id"], container_name="quant-attempt-test-2"
        )
    service.finish_failure(launched["attempt_id"], "synthetic")
    control = service.catalog.state_root / launched["control_path"]

    assert (control / "terminal.json").is_file()
    assert not control.stat().st_mode & 0o222
    assert all(not path.stat().st_mode & 0o222 for path in control.iterdir())


def test_unconfirmed_restart_quarantines_control_and_blocks_replacement(tmp_path: Path):
    service, created = _created(tmp_path)
    running = service.claim_next_attempt()
    control = service.catalog.state_root / running["control_path"]
    (control / "container.cid").write_text("f" * 64, encoding="ascii")

    recovered = service.recover_abandoned_attempts(
        container_reconciler=lambda cidfile: False
    )
    attempt = service.attempt_detail(created["attempt_id"])

    assert recovered == 1
    assert attempt["status"] == "TERMINATION_UNCONFIRMED"
    assert attempt["control_path"].startswith("attempt-control/")
    assert attempt["quarantine_path"].startswith("quarantine/attempts/")
    with pytest.raises(InvalidAttemptTransition, match="confirmed"):
        service.create_replacement_attempt(
            created["attempt_id"], action_id="replacement-blocked"
        )


def test_explicit_recovery_creates_distinct_attempt_with_frozen_resolution(
    tmp_path: Path,
):
    service, created = _created(tmp_path)
    original = service.claim_next_attempt()
    service.recover_abandoned_attempts(container_reconciler=lambda cidfile: True)

    replacement = service.create_replacement_attempt(
        original["attempt_id"], action_id="replacement"
    )

    assert replacement["attempt_id"] != original["attempt_id"]
    replacement_detail = service.attempt_detail(replacement["attempt_id"])
    assert replacement_detail["recovery_of_attempt_id"] == original["attempt_id"]
    assert replacement_detail["launch_count"] == 0
    assert replacement_detail["resolved"]["operators"] == original["resolved"]["operators"]


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
