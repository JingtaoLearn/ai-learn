from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock

import pytest

from quant_platform.parameter_study import ParameterStudy, StudyValidationError
from quant_platform.schemas import canonical_json_bytes
from quant_platform.worker import SerialStudyWorker

from test_parameter_study import _spec, _study_service


def _insert_expired_lease(studies: ParameterStudy, study_id: str) -> int:
    current = studies.detail(study_id)["coordination"]["lease"]
    fencing_token = 1 if current is None else current["fencing_token"] + 1
    lease = {
        "owner": "expired-test-owner",
        "owner_nonce": "e" * 32,
        "expires_at": "2000-01-01T00:00:00.000000Z",
        "fencing_token": fencing_token,
    }
    request_digest = hashlib.sha256(
        canonical_json_bytes({"study_id": study_id, **lease})
    ).hexdigest()
    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO parameter_study_actions(
                action_id, operation, study_id, request_digest,
                response_json, created_at
            ) VALUES (?, 'COORDINATOR_LEASE', ?, ?, ?, ?)
            """,
            (
                f"study-internal:lease:{study_id}:{fencing_token}",
                study_id,
                request_digest,
                canonical_json_bytes(lease).decode(),
                "2000-01-01T00:00:00.000000Z",
            ),
        )
    return fencing_token


def test_pause_is_idempotent_and_keeps_progress_phase_orthogonal(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-study",
    )

    first = studies.control(submitted["study_id"], "PAUSE", action_id="pause-study")
    replay = studies.control(submitted["study_id"], "PAUSE", action_id="pause-study")

    assert first == replay == {
        "status": "PAUSED",
        "study_id": submitted["study_id"],
        "control_status": "PAUSED",
    }
    detail = studies.detail(submitted["study_id"])
    assert detail["phase"] == "FROZEN"
    assert detail["control_status"] == "PAUSED"
    assert [event["event_type"] for event in detail["events"]] == [
        "STUDY_SUBMITTED",
        "STUDY_PAUSED",
    ]


def test_resume_and_cancel_are_ledgered_without_revoking_an_authorized_attempt(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-controlled-study",
    )
    normalized = preview["frozen_plan"]["normalized_request"]
    task = {
        key: normalized[key]
        for key in ("schema_version", "dataset", "template", "operators")
    }
    authorized = experiments.submit(task, action_id="authorized-before-control")

    studies.control(submitted["study_id"], "PAUSE", action_id="pause-controlled-study")
    resumed = studies.control(
        submitted["study_id"],
        "RESUME",
        action_id="resume-controlled-study",
    )
    conflict = studies.control(
        submitted["study_id"],
        "PAUSE",
        action_id="resume-controlled-study",
    )
    cancelled = studies.control(
        submitted["study_id"],
        "CANCEL",
        action_id="cancel-controlled-study",
    )
    invalid = studies.control(
        submitted["study_id"],
        "RESUME",
        action_id="resume-cancelled-study",
    )

    assert resumed["status"] == "RESUMED"
    assert conflict == {
        "status": "ACTION_CONFLICT",
        "action_id": "resume-controlled-study",
    }
    assert cancelled["status"] == "CANCELLED"
    assert invalid["status"] == "INVALID_TRANSITION"
    detail = studies.detail(submitted["study_id"])
    assert detail["phase"] == "FROZEN"
    assert detail["control_status"] == "CANCELLED"
    assert [event["event_type"] for event in detail["events"]] == [
        "STUDY_SUBMITTED",
        "STUDY_PAUSED",
        "STUDY_RESUMED",
        "STUDY_CANCELLED",
    ]
    assert experiments.attempt_detail(authorized["attempt_id"])["status"] == "PENDING"


def test_two_coordinators_create_one_monotonic_transition_and_event(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-racing-study",
    )
    now = datetime(2026, 8, 28, 6, 30, tzinfo=UTC)
    coordinators = [
        ParameterStudy(
            studies.catalog,
            datasets=studies.datasets,
            experiments=experiments,
            release_locator="/srv/quant/releases/155",
            clock=lambda: now,
            coordinator_id=owner,
            lease_duration_seconds=30,
        )
        for owner in ("coordinator-a", "coordinator-b")
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda coordinator: coordinator.advance(submitted["study_id"]),
                coordinators,
            )
        )

    assert sorted(result["status"] for result in results) == [
        "ADVANCED",
        "LEASE_BUSY",
    ]
    detail = studies.detail(submitted["study_id"])
    assert detail["phase"] == "VALIDATING_SELECTION_PROCESS"
    assert detail["coordination"]["lease"]["fencing_token"] == 1
    assert [event["event_type"] for event in detail["events"]] == [
        "STUDY_SUBMITTED",
        "STUDY_PHASE_ADVANCED",
    ]


def test_same_coordinator_label_uses_instance_nonce_to_dispatch_once(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-shared-coordinator-study",
    )
    dispatch_started = Event()
    release_dispatch = Event()
    dispatched: list[dict] = []

    def execute(effect: dict, action_id: str) -> dict:
        dispatched.append(effect)
        dispatch_started.set()
        assert release_dispatch.wait(timeout=10)
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    first = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="shared-human-coordinator",
        effect_executor=execute,
    )
    second = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="shared-human-coordinator",
        effect_executor=execute,
    )
    assert first.advance(submitted["study_id"])["status"] == "ADVANCED"
    submitted_attempt = first.advance(submitted["study_id"])

    with ThreadPoolExecutor(max_workers=1) as executor:
        committed_future = executor.submit(first.advance, submitted["study_id"])
        assert dispatch_started.wait(timeout=10)
        busy = second.advance(submitted["study_id"])
        release_dispatch.set()
        committed = committed_future.result(timeout=10)

    assert busy["status"] == "LEASE_BUSY"
    assert committed["status"] == "ATTEMPT_DISPATCHED"
    assert len(dispatched) == 1
    assert dispatched[0]["binding_id"] == submitted_attempt["binding_id"]
    assert dispatched[0]["experiment_id"] == submitted_attempt["experiment_id"]
    assert dispatched[0]["attempt_id"] == submitted_attempt["attempt_id"]


def test_lease_uses_locked_database_time_with_subsecond_precision(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-database-clock-study",
    )
    first = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        clock=lambda: datetime(1900, 1, 1, tzinfo=UTC),
        coordinator_id="database-clock-owner",
        lease_duration_seconds=1,
    )
    same_label = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        clock=lambda: datetime(2200, 1, 1, tzinfo=UTC),
        coordinator_id="database-clock-owner",
        lease_duration_seconds=1,
    )

    assert first.advance(submitted["study_id"])["status"] == "ADVANCED"
    assert same_label.advance(submitted["study_id"])["status"] == "LEASE_BUSY"
    lease = studies.detail(submitted["study_id"])["coordination"]["lease"]
    with studies.catalog.connect() as connection:
        created_at = connection.execute(
            """
            SELECT created_at
            FROM parameter_study_actions
            WHERE study_id = ? AND operation = 'COORDINATOR_LEASE'
            """,
            (submitted["study_id"],),
        ).fetchone()["created_at"]

    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    expires = datetime.fromisoformat(
        lease["expires_at"].replace("Z", "+00:00")
    )
    assert (expires - created).total_seconds() == 1
    assert "." in created_at
    assert "." in lease["expires_at"]
    assert len(lease["owner_nonce"]) == 32


def test_execution_identity_drift_fails_closed_once_before_any_effect(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-drift-study",
    )
    experiments.execution_identity = {
        **experiments.execution_identity,
        "source_sha256": "c" * 64,
    }

    first = studies.advance(submitted["study_id"])
    replay = studies.advance(submitted["study_id"])

    assert first == replay
    assert first["status"] == "EXECUTION_IDENTITY_DRIFT"
    assert experiments.list_experiments() == []
    detail = studies.detail(submitted["study_id"])
    assert detail["phase"] == "FROZEN"
    assert detail["control_status"] == "FAILED"
    assert [event["event_type"] for event in detail["events"]] == [
        "STUDY_SUBMITTED",
        "EXECUTION_IDENTITY_DRIFT",
    ]
    assert detail["events"][-1]["payload"]["code"] == "EXECUTION_IDENTITY_DRIFT"


def test_effect_intent_survives_restart_and_replays_one_deterministic_action(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-recoverable-study",
    )
    dispatched: list[tuple[str, dict]] = []

    def crash_then_acknowledge(effect: dict, action_id: str) -> dict:
        dispatched.append((action_id, effect))
        if len(dispatched) == 1:
            raise RuntimeError("coordinator stopped after dispatch")
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="owner-before-restart",
        lease_duration_seconds=10,
        effect_executor=crash_then_acknowledge,
    )
    assert coordinator.advance(submitted["study_id"])["status"] == "ADVANCED"
    submitted_attempt = coordinator.advance(submitted["study_id"])

    assert submitted_attempt["status"] == "ATTEMPT_SUBMITTED"
    with pytest.raises(RuntimeError, match="stopped after dispatch"):
        coordinator.advance(submitted["study_id"])
    assert _insert_expired_lease(studies, submitted["study_id"]) == 2

    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="owner-after-restart",
        lease_duration_seconds=10,
        effect_executor=crash_then_acknowledge,
    )
    committed = restarted.advance(submitted["study_id"])

    assert committed["status"] == "ATTEMPT_DISPATCHED"
    expected_action_id = (
        f"study-internal:effect:{submitted_attempt['binding_id']}"
    )
    assert [action_id for action_id, _ in dispatched] == [
        expected_action_id,
        expected_action_id,
    ]
    assert dispatched[0][1] == dispatched[1][1]
    assert len(experiments.list_experiments()) == 1
    assert [
        event["event_type"]
        for event in studies.detail(submitted["study_id"])["events"]
    ] == [
        "STUDY_SUBMITTED",
        "STUDY_PHASE_ADVANCED",
    ]


def test_expired_owner_cannot_commit_after_fenced_takeover(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-fenced-study",
    )
    first_dispatch_started = Event()
    allow_first_dispatch_to_return = Event()
    dispatch_lock = Lock()
    external_effects: dict[str, dict] = {}
    dispatch_count = 0

    def idempotent_dispatch(effect: dict, action_id: str) -> dict:
        nonlocal dispatch_count
        with dispatch_lock:
            dispatch_count += 1
            call_number = dispatch_count
            result = external_effects.setdefault(
                action_id,
                {
                    "experiment_id": effect["experiment_id"],
                    "attempt_id": effect["attempt_id"],
                },
            )
        if call_number == 1:
            first_dispatch_started.set()
            assert allow_first_dispatch_to_return.wait(timeout=10)
        return result

    first = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="expiring-owner",
        lease_duration_seconds=10,
        effect_executor=idempotent_dispatch,
    )
    second = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="takeover-owner",
        lease_duration_seconds=10,
        effect_executor=idempotent_dispatch,
    )
    first.advance(submitted["study_id"])
    first.advance(submitted["study_id"])

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_commit = executor.submit(first.advance, submitted["study_id"])
        assert first_dispatch_started.wait(timeout=10)
        assert _insert_expired_lease(studies, submitted["study_id"]) == 2
        takeover = second.advance(submitted["study_id"])
        allow_first_dispatch_to_return.set()
        stale = stale_commit.result(timeout=10)

    assert takeover["status"] == "ATTEMPT_DISPATCHED"
    assert stale["status"] == "LEASE_BUSY"
    assert len(external_effects) == 1
    detail = studies.detail(submitted["study_id"])
    assert detail["coordination"]["lease"]["owner"] == "takeover-owner"
    assert detail["coordination"]["lease"]["fencing_token"] == 3
    with studies.catalog.connect() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM parameter_study_actions
            WHERE study_id = ? AND operation = 'EFFECT_RECEIPT'
            """,
            (submitted["study_id"],),
        ).fetchone()[0] == 1


def test_outer_worker_discovers_a_runnable_study_after_restart(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-before-worker-restart",
    )
    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="restarted-worker",
    )

    worker = SerialStudyWorker(restarted)

    assert worker.run_once() is True
    assert studies.detail(submitted["study_id"])["phase"] == (
        "VALIDATING_SELECTION_PROCESS"
    )


def test_pause_and_cancel_block_dispatch_of_a_pending_effect(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-control-boundary-study",
    )
    dispatched: list[str] = []
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="control-boundary-owner",
        effect_executor=lambda effect, action_id: (
            dispatched.append(action_id) or {"status": "ACKNOWLEDGED"}
        ),
    )
    coordinator.advance(submitted["study_id"])
    coordinator.advance(submitted["study_id"])

    studies.control(
        submitted["study_id"],
        "PAUSE",
        action_id="pause-before-dispatch",
    )
    assert coordinator.advance(submitted["study_id"])["status"] == "PAUSED"
    studies.control(
        submitted["study_id"],
        "RESUME",
        action_id="resume-before-cancel",
    )
    studies.control(
        submitted["study_id"],
        "CANCEL",
        action_id="cancel-before-dispatch",
    )

    assert coordinator.advance(submitted["study_id"])["status"] == "CANCELLED"
    assert dispatched == []
    assert studies.detail(submitted["study_id"])["phase"] == (
        "VALIDATING_SELECTION_PROCESS"
    )


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [("PAUSE", "PAUSED"), ("CANCEL", "CANCELLED")],
)
def test_control_is_busy_until_an_authorized_dispatch_submits_its_attempt(
    tmp_path: Path,
    operation: str,
    expected_status: str,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id=f"submit-{operation.lower()}-dispatch-gap-study",
    )
    executor_started = Event()
    allow_submit = Event()

    def submit_after_control_attempt(effect: dict, action_id: str) -> dict:
        executor_started.set()
        if not allow_submit.wait(timeout=5):
            raise RuntimeError("timed out waiting for concurrent control")
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id=f"{operation.lower()}-dispatch-gap-owner",
        effect_executor=submit_after_control_attempt,
    )
    coordinator.advance(submitted["study_id"])
    coordinator.advance(submitted["study_id"])
    action_id = f"{operation.lower()}-during-dispatch-gap"

    with ThreadPoolExecutor(max_workers=1) as executor:
        dispatched = executor.submit(coordinator.advance, submitted["study_id"])
        assert executor_started.wait(timeout=5)
        try:
            controlled = studies.control(
                submitted["study_id"],
                operation,
                action_id=action_id,
            )
            assert controlled["status"] == expected_status
            assert (
                studies.detail(submitted["study_id"])["control_status"]
                == expected_status
            )
        finally:
            allow_submit.set()
        committed = dispatched.result(timeout=5)

    assert committed["status"] == "ATTEMPT_DISPATCHED"
    transitioned = studies.control(
        submitted["study_id"],
        operation,
        action_id=action_id,
    )
    assert transitioned == controlled
    assert studies.detail(submitted["study_id"])["control_status"] == expected_status


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [("PAUSE", "PAUSED"), ("CANCEL", "CANCELLED")],
)
def test_control_after_attempt_submission_allows_only_effect_reconciliation(
    tmp_path: Path,
    operation: str,
    expected_status: str,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id=f"submit-{operation.lower()}-attempt-gap-study",
    )
    attempt_submitted = Event()
    allow_crash = Event()

    def submit_then_crash(effect: dict, action_id: str) -> dict:
        attempt_submitted.set()
        if not allow_crash.wait(timeout=5):
            raise RuntimeError("timed out waiting for concurrent control")
        raise RuntimeError("stopped before Study receipt")

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id=f"{operation.lower()}-attempt-gap-owner",
        effect_executor=submit_then_crash,
    )
    coordinator.advance(submitted["study_id"])
    coordinator.advance(submitted["study_id"])

    with ThreadPoolExecutor(max_workers=1) as executor:
        dispatched = executor.submit(coordinator.advance, submitted["study_id"])
        assert attempt_submitted.wait(timeout=5)
        try:
            controlled = studies.control(
                submitted["study_id"],
                operation,
                action_id=f"{operation.lower()}-after-attempt-submit",
            )
            assert controlled["status"] == expected_status
        finally:
            allow_crash.set()
        with pytest.raises(RuntimeError, match="before Study receipt"):
            dispatched.result(timeout=5)

    assert _insert_expired_lease(studies, submitted["study_id"]) == 2
    reconciliations: list[str] = []

    def reconcile(effect: dict, action_id: str) -> dict:
        reconciliations.append(action_id)
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id=f"{operation.lower()}-attempt-gap-restart",
        effect_executor=reconcile,
    )

    result = restarted.advance(submitted["study_id"])

    assert result["status"] == "ATTEMPT_DISPATCHED"
    assert restarted.advance(submitted["study_id"])["status"] == expected_status
    assert len(reconciliations) == 1
    experiment = experiments.list_experiments()[0]
    assert len(experiments.list_attempts(experiment["experiment_id"])) == 1


def test_drift_after_intent_is_checked_before_dispatch(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-drift-before-effect-study",
    )
    dispatched: list[str] = []
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="drift-before-effect-owner",
        effect_executor=lambda effect, action_id: (
            dispatched.append(action_id) or {"status": "ACKNOWLEDGED"}
        ),
    )
    coordinator.advance(submitted["study_id"])
    coordinator.advance(submitted["study_id"])
    experiments.execution_identity = {
        **experiments.execution_identity,
        "source_sha256": "d" * 64,
    }

    drift = coordinator.advance(submitted["study_id"])

    assert drift["status"] == "EXECUTION_IDENTITY_DRIFT"
    assert dispatched == []
    assert len(experiments.list_experiments()) == 1
    assert [
        event["event_type"]
        for event in studies.detail(submitted["study_id"])["events"]
    ][-1] == "EXECUTION_IDENTITY_DRIFT"


def test_cancelled_study_reconciles_an_attempt_submitted_before_receipt(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-cancel-recovery-study",
    )
    dispatched: list[dict] = []

    def submit_then_stop(effect: dict, action_id: str) -> dict:
        dispatched.append(effect)
        raise RuntimeError("stopped before Study receipt")

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="owner-before-cancel",
        lease_duration_seconds=10,
        effect_executor=submit_then_stop,
    )
    coordinator.advance(submitted["study_id"])
    coordinator.advance(submitted["study_id"])
    with pytest.raises(RuntimeError, match="before Study receipt"):
        coordinator.advance(submitted["study_id"])
    studies.control(
        submitted["study_id"],
        "CANCEL",
        action_id="cancel-after-attempt-submit",
    )
    assert _insert_expired_lease(studies, submitted["study_id"]) == 2
    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="owner-after-cancel",
        lease_duration_seconds=10,
        effect_executor=lambda effect, action_id: {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        },
    )

    reconciled = restarted.advance(submitted["study_id"])

    assert reconciled["status"] == "ATTEMPT_DISPATCHED"
    assert len(experiments.list_attempts(dispatched[0]["experiment_id"])) == 1
    detail = studies.detail(submitted["study_id"])
    assert detail["control_status"] == "CANCELLED"
    with studies.catalog.connect() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM parameter_study_actions
            WHERE study_id = ? AND operation = 'EFFECT_RECEIPT'
            """,
            (submitted["study_id"],),
        ).fetchone()[0] == 1


def test_restart_rejects_unrelated_attempt_identity_during_reconciliation(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-unrelated-preclaim-study",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="unrelated-preclaim-owner",
        effect_executor=lambda effect, action_id: (_ for _ in ()).throw(
            RuntimeError("stopped before attempt submission")
        ),
    )
    coordinator.advance(submitted["study_id"])
    submitted_attempt = coordinator.advance(submitted["study_id"])
    with pytest.raises(RuntimeError, match="before attempt submission"):
        coordinator.advance(submitted["study_id"])

    assert _insert_expired_lease(studies, submitted["study_id"]) == 2
    reconciliations: list[str] = []
    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="unrelated-preclaim-restart",
        effect_executor=lambda effect, action_id: (
            reconciliations.append(action_id)
            or {
                "experiment_id": effect["experiment_id"],
                "attempt_id": "f" * 64,
            }
        ),
    )

    with pytest.raises(RuntimeError, match="authorized binding"):
        restarted.advance(submitted["study_id"])
    assert reconciliations == [
        f"study-internal:effect:{submitted_attempt['binding_id']}"
    ]


def test_internal_attempt_receipt_must_match_experiment_and_attempt_identity(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-mismatched-receipt-study",
    )
    def submit_with_mismatched_receipt(effect: dict, action_id: str) -> dict:
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": "f" * 64,
        }

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="mismatched-receipt-owner",
        effect_executor=submit_with_mismatched_receipt,
    )
    coordinator.advance(submitted["study_id"])
    coordinator.advance(submitted["study_id"])

    with pytest.raises(RuntimeError, match="authorized binding"):
        coordinator.advance(submitted["study_id"])
    with studies.catalog.connect() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM parameter_study_actions
            WHERE study_id = ? AND operation = 'EFFECT_RECEIPT'
            """,
            (submitted["study_id"],),
        ).fetchone()[0] == 0


def test_public_action_cannot_claim_the_internal_coordination_namespace(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())

    with pytest.raises(StudyValidationError, match="reserved"):
        studies.submit(
            _spec(),
            expected_preview_digest=preview["preview_digest"],
            action_id="study-internal:lease:reserved",
        )


@pytest.mark.parametrize("malformed_kind", ["forged", "duplicate-key"])
def test_malformed_binding_receipt_fails_closed(
    tmp_path: Path,
    malformed_kind: str,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id=f"submit-malformed-receipt-{malformed_kind}",
    )
    dispatched: list[dict] = []

    def stop_then_acknowledge(effect: dict, action_id: str) -> dict:
        dispatched.append(effect)
        if len(dispatched) == 1:
            raise RuntimeError("stop after external dispatch")
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id=f"malformed-receipt-{malformed_kind}",
        effect_executor=stop_then_acknowledge,
    )
    coordinator.advance(submitted["study_id"])
    submitted_attempt = coordinator.advance(submitted["study_id"])
    with pytest.raises(RuntimeError, match="external dispatch"):
        coordinator.advance(submitted["study_id"])

    effect_digest = submitted_attempt["binding_id"]
    receipt_action_id = f"study-internal:receipt:{effect_digest}"
    if malformed_kind == "forged":
        response_json = json.dumps(
            {
                "status": "EFFECT_COMMITTED",
                "study_id": submitted["study_id"],
                "action_id": f"study-internal:effect:{'f' * 64}",
                "effect": dispatched[0],
                "result": {"status": "FORGED"},
            }
        )
    else:
        response_json = (
            '{"status":"EFFECT_COMMITTED","status":"FORGED"}'
        )
    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO parameter_study_actions(
                action_id, operation, study_id, request_digest,
                response_json, created_at
            ) VALUES (?, 'EFFECT_RECEIPT', ?, ?, ?, ?)
            """,
            (
                receipt_action_id,
                submitted["study_id"],
                "0" * 64,
                response_json,
                "2026-08-28T00:00:00.000000Z",
            ),
        )

    with pytest.raises(RuntimeError, match="receipt|strict JSON"):
        coordinator.advance(submitted["study_id"])
    assert len(dispatched) == 1


def test_action_ledger_constraints_reject_invalid_operations_namespaces_and_digests(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-ledger-constraint-study",
    )
    invalid_rows = (
        ("invalid-operation", "NOT_ALLOWED", "0" * 64),
        ("study-internal:effect:public", "SUBMIT", "0" * 64),
        ("invalid-request-digest", "CONTROL_PAUSE", "not-a-digest"),
        (
            "study-internal:receipt:not-a-digest",
            "EFFECT_RECEIPT",
            "0" * 64,
        ),
    )

    for action_id, operation, request_digest in invalid_rows:
        with pytest.raises(sqlite3.IntegrityError):
            with studies.catalog.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO parameter_study_actions(
                        action_id, operation, study_id, request_digest,
                        response_json, created_at
                    ) VALUES (?, ?, ?, ?, '{}', ?)
                    """,
                    (
                        action_id,
                        operation,
                        submitted["study_id"],
                        request_digest,
                        "2026-08-28T00:00:00.000000Z",
                    ),
                )
