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
        return {"status": "ACKNOWLEDGED", "action_id": action_id}

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
    intent = first.advance(submitted["study_id"])

    with ThreadPoolExecutor(max_workers=1) as executor:
        committed_future = executor.submit(first.advance, submitted["study_id"])
        assert dispatch_started.wait(timeout=10)
        busy = second.advance(submitted["study_id"])
        release_dispatch.set()
        committed = committed_future.result(timeout=10)

    assert busy["status"] == "LEASE_BUSY"
    assert committed["status"] == "EFFECT_COMMITTED"
    assert len(dispatched) == 1
    coordination = dispatched[0]["coordination"]
    assert coordination["action_id"] == intent["action_id"]
    assert coordination["fencing_token"] == committed["fencing_token"]
    assert coordination["owner_nonce"] == committed["owner_nonce"]
    assert coordination["owner"] == "shared-human-coordinator"


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
        return {"status": "ACKNOWLEDGED", "effect_type": effect["effect_type"]}

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
    intent = coordinator.advance(submitted["study_id"])

    assert intent["status"] == "EFFECT_PENDING"
    assert intent["effect"]["effect_type"] == "REQUEST_TRIAL_PROPOSAL"
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

    assert committed["status"] == "EFFECT_COMMITTED"
    assert [action_id for action_id, _ in dispatched] == [
        intent["action_id"],
        intent["action_id"],
    ]
    assert [
        {
            key: value
            for key, value in effect.items()
            if key != "coordination"
        }
        for _, effect in dispatched
    ] == [intent["effect"], intent["effect"]]
    assert [
        effect["coordination"]["fencing_token"] for _, effect in dispatched
    ] == [1, 3]
    assert committed["fencing_token"] == 3
    assert experiments.list_experiments() == []
    assert [
        event["event_type"]
        for event in studies.detail(submitted["study_id"])["events"]
    ] == [
        "STUDY_SUBMITTED",
        "STUDY_PHASE_ADVANCED",
        "STUDY_EFFECT_INTENT_RECORDED",
        "STUDY_EFFECT_DISPATCH_AUTHORIZED",
        "STUDY_EFFECT_DISPATCH_AUTHORIZED",
        "STUDY_EFFECT_COMMITTED",
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
                {"status": "ACKNOWLEDGED", "effect_type": effect["effect_type"]},
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

    assert takeover["status"] == "EFFECT_COMMITTED"
    assert takeover["fencing_token"] == 3
    assert stale["status"] == "LEASE_BUSY"
    assert len(external_effects) == 1
    detail = studies.detail(submitted["study_id"])
    assert detail["coordination"]["lease"]["owner"] == "takeover-owner"
    assert detail["coordination"]["lease"]["fencing_token"] == 3
    assert (
        [event["event_type"] for event in detail["events"]].count(
            "STUDY_EFFECT_COMMITTED"
        )
        == 1
    )


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
    normalized = preview["frozen_plan"]["normalized_request"]
    task = {
        key: normalized[key]
        for key in ("schema_version", "dataset", "template", "operators")
    }
    executor_started = Event()
    allow_submit = Event()

    def submit_after_control_attempt(effect: dict, action_id: str) -> dict:
        executor_started.set()
        if not allow_submit.wait(timeout=5):
            raise RuntimeError("timed out waiting for concurrent control")
        return experiments.submit_study_effect(task, action_id=action_id)

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
            blocked = studies.control(
                submitted["study_id"],
                operation,
                action_id=action_id,
            )
            assert blocked == {
                "status": "LEASE_BUSY",
                "study_id": submitted["study_id"],
                "reason": "DISPATCH_IN_FLIGHT",
            }
            assert studies.detail(submitted["study_id"])["control_status"] == "ACTIVE"
        finally:
            allow_submit.set()
        committed = dispatched.result(timeout=5)

    assert committed["status"] == "EFFECT_COMMITTED"
    assert committed["result"]["status"] == "CREATED"
    transitioned = studies.control(
        submitted["study_id"],
        operation,
        action_id=action_id,
    )
    assert transitioned["status"] == expected_status
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
    normalized = preview["frozen_plan"]["normalized_request"]
    task = {
        key: normalized[key]
        for key in ("schema_version", "dataset", "template", "operators")
    }
    attempt_submitted = Event()
    allow_crash = Event()

    def submit_then_crash(effect: dict, action_id: str) -> dict:
        experiments.submit_study_effect(task, action_id=action_id)
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
        return experiments.submit_study_effect(task, action_id=action_id)

    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id=f"{operation.lower()}-attempt-gap-restart",
        effect_executor=reconcile,
    )

    result = restarted.advance(submitted["study_id"])

    assert result["status"] == "EFFECT_COMMITTED"
    assert result["result"]["status"] == "DUPLICATE"
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
    assert experiments.list_experiments() == []
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
    normalized = preview["frozen_plan"]["normalized_request"]
    task = {
        key: normalized[key]
        for key in ("schema_version", "dataset", "template", "operators")
    }
    dispatched: list[dict] = []

    def submit_then_stop(effect: dict, action_id: str) -> dict:
        dispatched.append(
            experiments.submit_study_effect(task, action_id=action_id)
        )
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
        effect_executor=lambda effect, action_id: experiments.submit_study_effect(
            task,
            action_id=action_id,
        ),
    )

    reconciled = restarted.advance(submitted["study_id"])

    assert reconciled["status"] == "EFFECT_COMMITTED"
    assert reconciled["result"]["status"] == "DUPLICATE"
    assert dispatched[0]["status"] == "CREATED"
    assert len(experiments.list_attempts(dispatched[0]["experiment_id"])) == 1
    detail = studies.detail(submitted["study_id"])
    assert detail["control_status"] == "CANCELLED"
    assert [event["event_type"] for event in detail["events"]].count(
        "STUDY_EFFECT_COMMITTED"
    ) == 1


def test_unrelated_internal_attempt_preclaim_fails_closed_during_reconciliation(
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
    intent = coordinator.advance(submitted["study_id"])
    with pytest.raises(RuntimeError, match="before attempt submission"):
        coordinator.advance(submitted["study_id"])

    unrelated_task = {
        key: preview["frozen_plan"]["normalized_request"][key]
        for key in ("schema_version", "dataset", "template", "operators")
    }
    unrelated_task["template"]["parameters"]["initial_capital_cny"] = 200000.0
    unrelated = experiments.submit(
        unrelated_task,
        action_id="unrelated-public-attempt",
    )
    forged_attempt_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "experiment_id": unrelated["experiment_id"],
                "action_id": intent["action_id"],
                "sequence": 1,
            }
        )
    ).hexdigest()
    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE attempts
            SET attempt_id = ?, action_id = ?
            WHERE attempt_id = ?
            """,
            (forged_attempt_id, intent["action_id"], unrelated["attempt_id"]),
        )
    assert _insert_expired_lease(studies, submitted["study_id"]) == 2
    reconciliations: list[str] = []
    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="unrelated-preclaim-restart",
        effect_executor=lambda effect, action_id: (
            reconciliations.append(action_id) or {"status": "UNREACHABLE"}
        ),
    )

    result = restarted.advance(submitted["study_id"])

    assert result == {
        "status": "ACTION_CONFLICT",
        "action_id": intent["action_id"],
    }
    assert reconciliations == []


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
    task = {
        key: preview["frozen_plan"]["normalized_request"][key]
        for key in ("schema_version", "dataset", "template", "operators")
    }

    def submit_with_mismatched_receipt(effect: dict, action_id: str) -> dict:
        result = experiments.submit_study_effect(task, action_id=action_id)
        return {**result, "attempt_id": "f" * 64}

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id="mismatched-receipt-owner",
        effect_executor=submit_with_mismatched_receipt,
    )
    coordinator.advance(submitted["study_id"])
    intent = coordinator.advance(submitted["study_id"])

    result = coordinator.advance(submitted["study_id"])

    assert result == {
        "status": "ACTION_CONFLICT",
        "action_id": intent["action_id"],
    }
    assert [
        event["event_type"]
        for event in studies.detail(submitted["study_id"])["events"]
    ].count("STUDY_EFFECT_COMMITTED") == 0


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
def test_malformed_receipt_never_suppresses_executor(
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
        return {"status": "ACKNOWLEDGED", "action_id": action_id}

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/155",
        coordinator_id=f"malformed-receipt-{malformed_kind}",
        effect_executor=stop_then_acknowledge,
    )
    coordinator.advance(submitted["study_id"])
    intent = coordinator.advance(submitted["study_id"])
    with pytest.raises(RuntimeError, match="external dispatch"):
        coordinator.advance(submitted["study_id"])

    effect_digest = intent["action_id"].rsplit(":", 1)[1]
    receipt_action_id = f"study-internal:receipt:{effect_digest}"
    coordination = dispatched[0]["coordination"]
    if malformed_kind == "forged":
        response_json = json.dumps(
            {
                "status": "EFFECT_COMMITTED",
                "study_id": submitted["study_id"],
                "action_id": f"study-internal:effect:{'f' * 64}",
                "dispatch_action_id": coordination["dispatch_action_id"],
                "effect": intent["effect"],
                "result": {"status": "FORGED"},
                "fencing_token": coordination["fencing_token"],
                "owner": coordination["owner"],
                "owner_nonce": coordination["owner_nonce"],
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

    result = coordinator.advance(submitted["study_id"])

    assert result == {
        "status": "ACTION_CONFLICT",
        "action_id": receipt_action_id,
    }
    assert len(dispatched) == 2
    assert dispatched[0]["coordination"] == dispatched[1]["coordination"]


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
