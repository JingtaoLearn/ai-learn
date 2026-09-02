from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_type_hints

import pytest
from test_intent_fencing import (
    ActorAuthenticator,
    TrustedFakeExternalEffects,
    decision,
    digest,
)

import agentic_workflow.kernel as kernel_module
import agentic_workflow.model as model
import agentic_workflow.operations as operations
from agentic_workflow import UserDecision, WorkflowError, WorkflowKernel

NOW = "2026-09-02T15:59:00+00:00"
NEXT_DAY = "2026-09-02T16:01:00+00:00"
TARGET = {"issue": 215, "repository": "JingtaoLearn/ai-learn"}
SPEND_CAP = {"currency": "USD", "minor_units": 0}


class MutableClock:
    def __init__(self, value: str = NOW) -> None:
        self.value = value

    def now(self) -> str:
        return self.value


class ScriptedTracerEffects(TrustedFakeExternalEffects):
    operation_mode = "shadow"

    def __init__(
        self,
        *,
        approval_required: bool = False,
        observation: str = "APPLIED",
        transport_failures: int = 0,
        database_path: Path | None = None,
    ) -> None:
        self.approval_required = approval_required
        self.observation = observation
        self.transport_failures = transport_failures
        self.database_path = database_path
        self.attempts: list[Any] = []
        self.probes: list[Any] = []
        self.deliveries: list[Any] = []

    def operation_policy(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        assert context["project_id"] == "project-1"
        return {
            "approval_required": self.approval_required,
            "approval_expires_at": "2026-09-03T00:00:00+00:00",
            "exact_spend_cap": SPEND_CAP,
            "expected_target_version": "issue-215-v1",
            "side_effect_class": "SCRIPTED_WRITE",
            "target_identity": TARGET,
        }

    def attempt_operation(self, operation: Any) -> Mapping[str, Any]:
        assert operation.mode == "shadow"
        assert operation.physical_apply_authorized is False
        if self.database_path is not None:
            with sqlite3.connect(
                self.database_path, timeout=0.1, isolation_level=None
            ) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("COMMIT")
        self.attempts.append(operation)
        return {
            "attempted": True,
            "idempotency_identity": operation.idempotency_identity,
            "operation_digest": operation.operation_digest,
        }

    def observe_operation(self, probe: Any) -> Mapping[str, Any]:
        assert probe.target_identity == TARGET
        assert probe.expected_target_version == "issue-215-v1"
        self.probes.append(probe)
        return {
            "classification": self.observation,
            "evidence": {"observed_version": "issue-215-v1"},
            "operation_digest": probe.operation_digest,
        }

    def deliver_outbox(self, message: Any) -> Mapping[str, Any]:
        self.deliveries.append(message)
        if len(self.deliveries) <= self.transport_failures:
            raise RuntimeError("scripted transport failure")
        return {
            "acknowledged": True,
            "logical_outbox_identity": message.logical_outbox_identity,
            "transport_receipt_id": f"transport-{len(self.deliveries)}",
        }


class ReplayTracerEffects(ScriptedTracerEffects):
    operation_mode = "replay"

    def attempt_operation(self, operation: Any) -> Mapping[str, Any]:
        raise AssertionError("Replay must not attempt an effect")

    def observe_operation(self, probe: Any) -> Mapping[str, Any]:
        raise AssertionError("Replay must not observe an external target")


class DurableCrashEffects(ScriptedTracerEffects):
    def __init__(self, state_path: Path) -> None:
        super().__init__()
        self.state_path = state_path
        with sqlite3.connect(state_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS remote_state ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "attempt_calls INTEGER NOT NULL, readback_calls INTEGER NOT NULL, "
                "applied INTEGER NOT NULL)"
            )
            connection.execute("INSERT OR IGNORE INTO remote_state VALUES (1, 0, 0, 0)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS transport_calls ("
                "call_number INTEGER PRIMARY KEY AUTOINCREMENT, logical_identity TEXT NOT NULL)"
            )

    def attempt_operation(self, operation: Any) -> Mapping[str, Any]:
        with sqlite3.connect(self.state_path) as connection:
            connection.execute(
                "UPDATE remote_state SET attempt_calls = attempt_calls + 1, applied = 1 "
                "WHERE singleton = 1"
            )
        return {
            "attempted": True,
            "idempotency_identity": operation.idempotency_identity,
            "operation_digest": operation.operation_digest,
        }

    def observe_operation(self, probe: Any) -> Mapping[str, Any]:
        with sqlite3.connect(self.state_path) as connection:
            connection.execute(
                "UPDATE remote_state SET readback_calls = readback_calls + 1 WHERE singleton = 1"
            )
            applied = connection.execute(
                "SELECT applied FROM remote_state WHERE singleton = 1"
            ).fetchone()[0]
        return {
            "classification": "APPLIED" if applied else "NOT_APPLIED",
            "evidence": {"applied": bool(applied)},
            "operation_digest": probe.operation_digest,
        }

    def deliver_outbox(self, message: Any) -> Mapping[str, Any]:
        with sqlite3.connect(self.state_path) as connection:
            cursor = connection.execute(
                "INSERT INTO transport_calls (logical_identity) VALUES (?)",
                (message.logical_outbox_identity,),
            )
            call_number = cursor.lastrowid
        return {
            "acknowledged": True,
            "logical_outbox_identity": message.logical_outbox_identity,
            "transport_receipt_id": f"transport-{call_number}",
        }



def make_kernel(
    database_path: Path,
    effects: ScriptedTracerEffects,
    clock: MutableClock | None = None,
    authenticator: Any = None,
) -> WorkflowKernel:
    return WorkflowKernel(
        database_path,
        decision_authenticator=authenticator or ActorAuthenticator(),
        external_effects=effects,
        clock=clock or MutableClock(),
    )



def approval_for(request: Mapping[str, Any], **changes: object) -> UserDecision:
    approval = UserDecision(
        project_id="project-1",
        source="test-ui",
        source_event_id="approval-1",
        authenticated_actor="user-1",
        scope="EXACT_OPERATION",
        verbatim_text="Approve the exact scripted operation.",
        nonce="approval-nonce-1",
        replay_identity="approval-replay-1",
        provenance={"channel": "test"},
        decision_kind="APPROVE_EXACT_OPERATION",
        complete_revision_payload=request["complete_revision_payload"],
    )
    return replace(approval, **changes)



def prepare_operation(kernel: WorkflowKernel) -> Any:
    kernel.record(decision())
    assert kernel.advance("project-1").outcome == "ACTION_ENVELOPED"
    return kernel.advance("project-1")



def test_scripted_effect_seam_has_typed_private_request_response_contracts() -> None:
    protocol = operations._ScriptedOperationEffects
    method_contracts = {
        "operation_policy": (
            operations._OperationPolicyRequest,
            operations._OperationPolicyResponse,
        ),
        "attempt_operation": (operations.FrozenOperation, operations._AttemptResponse),
        "observe_operation": (operations.EffectProbe, operations._ObservationResponse),
        "deliver_outbox": (operations.OutboxMessage, operations._DeliveryResponse),
    }

    assert get_type_hints(operations.OperationLifecycle.__init__)["effects"] == protocol | None
    for method_name, expected in method_contracts.items():
        annotations = get_type_hints(getattr(protocol, method_name))
        assert tuple(annotations.values()) == expected
    assert "# type: ignore" not in Path(operations.__file__).read_text()


def test_decision_json_validation_has_one_private_source_of_truth() -> None:
    model_validator = getattr(model, "_validate_json_value", None)

    assert model_validator is not None
    assert kernel_module._validate_json_value is model_validator
    assert operations._validate_json_value is model_validator


def test_scripted_response_payload_annotations_use_strict_json_domain() -> None:
    strict_json_object = getattr(operations, "_StrictJsonObject", None)

    assert strict_json_object is not None
    assert get_type_hints(operations._OperationPolicyResponse)["exact_spend_cap"] \
        is strict_json_object
    assert get_type_hints(operations._OperationPolicyResponse)["target_identity"] \
        is strict_json_object
    assert get_type_hints(operations._ObservationResponse)["evidence"] is strict_json_object


def test_shadow_operation_uses_exact_approval_and_one_boundary_per_advance(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    effects = ScriptedTracerEffects(approval_required=True, database_path=database_path)
    kernel = make_kernel(database_path, effects)

    waiting = prepare_operation(kernel)

    assert waiting.outcome == "OPERATION_AWAITING_APPROVAL"
    assert effects.attempts == []
    request = kernel.view("project-1").pending_decisions[0]
    original = kernel.record(approval_for(request))
    replay = kernel.record(approval_for(request))
    assert replay == original
    assert original.outcome == "OPERATION_APPROVAL_RECORDED"
    assert effects.attempts == []

    prepared = kernel.advance("project-1")
    attempted = kernel.advance("project-1")
    observed = kernel.advance("project-1")
    concluded = kernel.advance("project-1")

    assert [prepared.outcome, attempted.outcome, observed.outcome, concluded.outcome] == [
        "OPERATION_PREPARED",
        "OPERATION_ATTEMPT_RETURNED",
        "OPERATION_READBACK_RECORDED",
        "OPERATION_APPLIED",
    ]
    assert len(effects.attempts) == 1
    assert len(effects.probes) == 1
    assert effects.attempts[0].target_identity == effects.probes[0].target_identity
    assert effects.attempts[0].expected_target_version == effects.probes[0].expected_target_version
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT event_type FROM operation_events ORDER BY event_number"
        ).fetchall() == [
            ("AWAITING_APPROVAL",),
            ("PREPARED",),
            ("ATTEMPT_INTENT",),
            ("ATTEMPT_RETURNED",),
            ("READBACK_RECORDED",),
            ("APPLIED",),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM operation_approval_consumptions"
        ).fetchone() == (1,)


def test_approval_rejects_numeric_provenance_key_before_string_key_identity(
    tmp_path: Path,
) -> None:
    class ActorIdentityAuthenticator:
        def authenticate(self, decision: UserDecision) -> bool:
            return decision.authenticated_actor == "user-1"

    database_path = tmp_path / "control.sqlite3"
    effects = ScriptedTracerEffects(approval_required=True)
    kernel = make_kernel(database_path, effects, authenticator=ActorIdentityAuthenticator())
    prepare_operation(kernel)
    request = kernel.view("project-1").pending_decisions[0]

    with pytest.raises(WorkflowError) as caught:
        kernel.record(approval_for(request, provenance={1: "same"}))

    assert caught.value.code == "INVALID_EVENT"
    accepted = kernel.record(approval_for(request, provenance={"1": "same"}))
    assert accepted.outcome == "OPERATION_APPROVAL_RECORDED"


def test_approval_rejects_mixed_string_and_numeric_provenance_keys(
    tmp_path: Path,
) -> None:
    class ActorIdentityAuthenticator:
        def authenticate(self, decision: UserDecision) -> bool:
            return decision.authenticated_actor == "user-1"

    database_path = tmp_path / "control.sqlite3"
    effects = ScriptedTracerEffects(approval_required=True)
    kernel = make_kernel(database_path, effects, authenticator=ActorIdentityAuthenticator())
    prepare_operation(kernel)
    request = kernel.view("project-1").pending_decisions[0]

    with pytest.raises(WorkflowError) as caught:
        kernel.record(
            approval_for(request, provenance={1: "numeric", "1": "string"})
        )

    assert caught.value.code == "INVALID_EVENT"


@pytest.mark.parametrize(
    "provenance",
    [
        [],
        MappingProxyType({"channel": "test"}),
        {"channel": "test", "steps": ("one", "two")},
    ],
    ids=["non-object", "non-json-mapping", "tuple"],
)
def test_approval_rejects_every_non_strict_json_provenance_shape(
    tmp_path: Path, provenance: object
) -> None:
    database_path = tmp_path / "control.sqlite3"
    effects = ScriptedTracerEffects(approval_required=True)
    kernel = make_kernel(database_path, effects)
    prepare_operation(kernel)
    request = kernel.view("project-1").pending_decisions[0]

    with pytest.raises(WorkflowError) as caught:
        kernel.record(approval_for(request, provenance=provenance))

    assert caught.value.code == "INVALID_EVENT"


@pytest.mark.parametrize("classification", ["APPLIED", "NOT_APPLIED", "AMBIGUOUS"])
def test_stable_target_readback_has_exactly_three_terminal_classifications(
    tmp_path: Path, classification: str
) -> None:
    effects = ScriptedTracerEffects(observation=classification)
    kernel = make_kernel(tmp_path / "control.sqlite3", effects)

    assert prepare_operation(kernel).outcome == "OPERATION_PREPARED"
    assert kernel.advance("project-1").outcome == "OPERATION_ATTEMPT_RETURNED"
    assert kernel.advance("project-1").outcome == "OPERATION_READBACK_RECORDED"
    assert kernel.advance("project-1").outcome == f"OPERATION_{classification}"

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")
    assert caught.value.code == "NO_ACTION"
    assert len(effects.attempts) == 1
    assert len(effects.probes) == 1



def test_replay_goal_to_brief_tracer_is_side_effect_free(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    clock = MutableClock()
    effects = ReplayTracerEffects()
    kernel = make_kernel(database_path, effects, clock)

    prepared = prepare_operation(kernel)
    replayed = kernel.advance("project-1")
    concluded = kernel.advance("project-1")

    assert prepared.outcome == "OPERATION_PREPARED"
    assert replayed.outcome == "OPERATION_READBACK_RECORDED"
    assert concluded.outcome == "OPERATION_NOT_APPLIED"
    assert effects.attempts == []
    assert effects.probes == []
    clock.value = NEXT_DAY
    assert kernel.advance("project-1").outcome == "DAILY_BRIEF_READY"
    brief = kernel.view("project-1").daily_brief
    assert brief["local_day"] == "2026-09-02"
    assert brief["mode"] == "replay"
    assert brief["material_changes"][0]["outcome"] == "NOT_APPLIED"
    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")
    assert caught.value.code == "NO_ACTION"
    assert effects.deliveries == []



def test_day_close_atomically_commits_evidence_brief_and_one_logical_outbox(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "control.sqlite3"
    clock = MutableClock()
    effects = ScriptedTracerEffects()
    kernel = make_kernel(database_path, effects, clock)
    prepare_operation(kernel)
    kernel.advance("project-1")
    kernel.advance("project-1")
    kernel.advance("project-1")
    assert kernel.view("project-1").daily_brief == {
        "material_changes": [],
        "status": "INITIAL",
    }
    clock.value = NEXT_DAY
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER inject_outbox_failure BEFORE INSERT ON outbox_events "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")
    assert caught.value.code == "LEDGER_ERROR"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM operation_evidence WHERE evidence_kind = 'OUTCOME'"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_briefs WHERE local_day IS NOT NULL"
        ).fetchone() == (0,)
        connection.execute("DROP TRIGGER inject_outbox_failure")

    ready = kernel.advance("project-1")
    same = kernel.advance("project-1")

    assert ready.outcome == "DAILY_BRIEF_READY"
    assert same.outcome == "OUTBOX_DELIVERED"
    assert len(effects.deliveries) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM operation_evidence WHERE evidence_kind = 'OUTCOME'"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_briefs WHERE local_day IS NOT NULL"
        ).fetchone() == (1,)



def test_delivery_retry_reuses_logical_identity_and_never_reruns_work(tmp_path: Path) -> None:
    clock = MutableClock()
    effects = ScriptedTracerEffects(transport_failures=1)
    kernel = make_kernel(tmp_path / "control.sqlite3", effects, clock)
    prepare_operation(kernel)
    kernel.advance("project-1")
    kernel.advance("project-1")
    kernel.advance("project-1")
    clock.value = NEXT_DAY
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")
    assert caught.value.code == "OUTBOX_DELIVERY_FAILED"
    delivered = kernel.advance("project-1")

    assert delivered.outcome == "OUTBOX_DELIVERED"
    assert len(effects.attempts) == 1
    assert len(effects.probes) == 1
    assert len(effects.deliveries) == 2
    assert {
        message.logical_outbox_identity for message in effects.deliveries
    } == {effects.deliveries[0].logical_outbox_identity}


def create_closed_v6_ledger(
    database_path: Path, source_path: Path, *, concluded: bool
) -> tuple[str, str]:
    source = WorkflowKernel(
        source_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=TrustedFakeExternalEffects(),
        clock=MutableClock(),
    )
    source.record(decision())
    source.advance("project-1")
    reserved = source.advance("project-1")
    if concluded:
        source.advance("project-1")

    migrations = Path(__file__).parents[1] / "src" / "agentic_workflow" / "migrations"
    with sqlite3.connect(database_path) as destination:
        destination.execute("PRAGMA foreign_keys = OFF")
        destination.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        for version in range(1, 7):
            migration = next(migrations.glob(f"{version:04d}_*.sql"))
            destination.executescript(migration.read_text())
            destination.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
        destination.execute("ATTACH DATABASE ? AS source", (str(source_path),))
        tables = [
            row[0]
            for row in destination.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
            ).fetchall()
        ]
        for table in tables:
            columns = [
                row[1] for row in destination.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            names = ", ".join(columns)
            destination.execute(
                f"INSERT INTO main.{table} ({names}) SELECT {names} FROM source.{table}"
            )
    return reserved.operation_id, reserved.operation_digest


@pytest.mark.parametrize("concluded", [False, True], ids=["reserved", "concluded"])
def test_every_valid_closed_v6_operation_lifecycle_migrates(
    tmp_path: Path, concluded: bool
) -> None:
    database_path = tmp_path / "v6.sqlite3"
    operation_id, legacy_digest = create_closed_v6_ledger(
        database_path, tmp_path / "source.sqlite3", concluded=concluded
    )
    effects = ScriptedTracerEffects()

    kernel = make_kernel(database_path, effects)

    assert kernel.view("project-1").current_goal
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT operation_id, operation_digest, legacy_operation_digest "
            "FROM operation_records"
        ).fetchone()
        events = connection.execute(
            "SELECT event_type FROM operation_events ORDER BY event_number"
        ).fetchall()
    assert row == (operation_id, legacy_digest, None)
    assert events == (
        [("RESERVED",), ("CONCLUDED",)] if concluded else [("RESERVED",)]
    )
    if concluded:
        with pytest.raises(WorkflowError) as caught:
            kernel.advance("project-1")
        assert caught.value.code == "NO_ACTION"
        assert effects.attempts == []
    else:
        assert kernel.advance("project-1").outcome == "OPERATION_CONCLUDED"
        assert effects.attempts == []


def test_malformed_v6_operation_lifecycle_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "malformed-v6.sqlite3"
    create_closed_v6_ledger(database_path, tmp_path / "source.sqlite3", concluded=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER operation_events_no_delete")
        connection.execute("DELETE FROM operation_events WHERE event_type = 'RESERVED'")

    with pytest.raises(sqlite3.IntegrityError, match="legacy v6 Operation event is invalid"):
        make_kernel(database_path, ScriptedTracerEffects())


def replace_migration_history(database_path: Path, versions: list[int]) -> None:
    make_kernel(database_path, ScriptedTracerEffects())
    with sqlite3.connect(database_path) as connection:
        connection.execute("ALTER TABLE schema_migrations RENAME TO previous_schema_migrations")
        connection.execute("CREATE TABLE schema_migrations (version INTEGER)")
        connection.executemany(
            "INSERT INTO schema_migrations VALUES (?)", ((version,) for version in versions)
        )
        connection.execute("DROP TABLE previous_schema_migrations")


def test_schema_migration_history_rejects_an_ahead_version(tmp_path: Path) -> None:
    database_path = tmp_path / "ahead.sqlite3"
    replace_migration_history(database_path, list(range(1, 9)))

    with pytest.raises(sqlite3.IntegrityError, match="schema migration history is invalid"):
        make_kernel(database_path, ScriptedTracerEffects())


def test_schema_migration_history_rejects_a_gap(tmp_path: Path) -> None:
    database_path = tmp_path / "gap.sqlite3"
    replace_migration_history(database_path, [1, 2, 4, 5, 6, 7])

    with pytest.raises(sqlite3.IntegrityError, match="schema migration history is invalid"):
        make_kernel(database_path, ScriptedTracerEffects())


def test_schema_migration_history_rejects_a_duplicate_version(tmp_path: Path) -> None:
    database_path = tmp_path / "duplicate.sqlite3"
    replace_migration_history(database_path, [1, 2, 3, 3, 4, 5, 6, 7])

    with pytest.raises(sqlite3.IntegrityError, match="schema migration history is invalid"):
        make_kernel(database_path, ScriptedTracerEffects())


def test_schema_migration_history_rejects_an_unknown_version(tmp_path: Path) -> None:
    database_path = tmp_path / "unknown.sqlite3"
    replace_migration_history(database_path, [0, 1, 2, 3, 4, 5, 6, 7])

    with pytest.raises(sqlite3.IntegrityError, match="schema migration history is invalid"):
        make_kernel(database_path, ScriptedTracerEffects())


def run_crash_probe(
    database_path: Path,
    state_path: Path,
    fault_point: str,
    clock_value: str = NOW,
) -> subprocess.CompletedProcess[str]:
    script = """
import os
import sys

import agentic_workflow.operations as operations
from agentic_workflow import WorkflowKernel
from test_intent_fencing import ActorAuthenticator
from test_replay_shadow_tracer import DurableCrashEffects, MutableClock

def crash(point):
    if point == sys.argv[3]:
        os._exit(91)

operations._PRIVATE_FAULT_HOOK = crash
WorkflowKernel(
    sys.argv[1],
    decision_authenticator=ActorAuthenticator(),
    external_effects=DurableCrashEffects(sys.argv[2]),
    clock=MutableClock(sys.argv[4]),
).advance("project-1")
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(Path(__file__).parent),
            str(Path(__file__).parents[1] / "src"),
            environment.get("PYTHONPATH", ""),
        )
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(database_path),
            str(state_path),
            fault_point,
            clock_value,
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize(
    ("fault_point", "attempts_after_crash", "expected_outcome"),
    [
        ("operation_before_attempt", 0, "NOT_APPLIED"),
        ("operation_after_attempt", 1, "APPLIED"),
    ],
)
def test_attempt_crash_recovers_by_readback_without_blind_retry(
    tmp_path: Path,
    fault_point: str,
    attempts_after_crash: int,
    expected_outcome: str,
) -> None:
    database_path = tmp_path / "control.sqlite3"
    state_path = tmp_path / "remote.sqlite3"
    effects = DurableCrashEffects(state_path)
    kernel = make_kernel(database_path, effects)
    assert prepare_operation(kernel).outcome == "OPERATION_PREPARED"

    crashed = run_crash_probe(database_path, state_path, fault_point)

    assert crashed.returncode == 91, crashed.stderr
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            "SELECT attempt_calls FROM remote_state WHERE singleton = 1"
        ).fetchone() == (attempts_after_crash,)
    restarted = make_kernel(database_path, DurableCrashEffects(state_path))
    assert restarted.advance("project-1").outcome == "OPERATION_READBACK_RECORDED"
    assert restarted.advance("project-1").outcome == f"OPERATION_{expected_outcome}"
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            "SELECT attempt_calls, readback_calls FROM remote_state WHERE singleton = 1"
        ).fetchone() == (attempts_after_crash, 1)


@pytest.mark.parametrize(
    ("fault_point", "readbacks_after_crash"),
    [("operation_before_readback", 0), ("operation_after_readback", 1)],
)
def test_readback_crash_repeats_only_stable_observation(
    tmp_path: Path, fault_point: str, readbacks_after_crash: int
) -> None:
    database_path = tmp_path / "control.sqlite3"
    state_path = tmp_path / "remote.sqlite3"
    kernel = make_kernel(database_path, DurableCrashEffects(state_path))
    prepare_operation(kernel)
    assert kernel.advance("project-1").outcome == "OPERATION_ATTEMPT_RETURNED"

    crashed = run_crash_probe(database_path, state_path, fault_point)

    assert crashed.returncode == 91, crashed.stderr
    restarted = make_kernel(database_path, DurableCrashEffects(state_path))
    assert restarted.advance("project-1").outcome == "OPERATION_READBACK_RECORDED"
    assert restarted.advance("project-1").outcome == "OPERATION_APPLIED"
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            "SELECT attempt_calls, readback_calls FROM remote_state WHERE singleton = 1"
        ).fetchone() == (1, readbacks_after_crash + 1)


@pytest.mark.parametrize(
    ("fault_point", "deliveries_after_crash"),
    [("outbox_before_delivery", 0), ("outbox_after_delivery", 1)],
)
def test_transport_crash_retries_only_one_logical_outbox_identity(
    tmp_path: Path, fault_point: str, deliveries_after_crash: int
) -> None:
    database_path = tmp_path / "control.sqlite3"
    state_path = tmp_path / "remote.sqlite3"
    clock = MutableClock()
    kernel = make_kernel(database_path, DurableCrashEffects(state_path), clock)
    prepare_operation(kernel)
    kernel.advance("project-1")
    kernel.advance("project-1")
    kernel.advance("project-1")
    clock.value = NEXT_DAY
    assert kernel.advance("project-1").outcome == "DAILY_BRIEF_READY"

    crashed = run_crash_probe(database_path, state_path, fault_point, NEXT_DAY)

    assert crashed.returncode == 91, crashed.stderr
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transport_calls").fetchone() == (
            deliveries_after_crash,
        )
    restarted = make_kernel(
        database_path, DurableCrashEffects(state_path), MutableClock(NEXT_DAY)
    )
    assert restarted.advance("project-1").outcome == "OUTBOX_DELIVERED"
    with sqlite3.connect(state_path) as connection:
        identities = connection.execute(
            "SELECT logical_identity FROM transport_calls ORDER BY call_number"
        ).fetchall()
    assert len(identities) == deliveries_after_crash + 1
    assert len(set(identities)) == 1


def test_overlapping_advance_cannot_misclassify_a_live_attempt_as_crashed(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingEffects(ScriptedTracerEffects):
        def attempt_operation(self, operation: Any) -> Mapping[str, Any]:
            started.set()
            assert release.wait(timeout=5)
            return super().attempt_operation(operation)

    database_path = tmp_path / "control.sqlite3"
    effects = BlockingEffects()
    first = make_kernel(database_path, effects)
    second = make_kernel(database_path, effects)
    prepare_operation(first)
    results: list[Any] = []

    thread = threading.Thread(target=lambda: results.append(first.advance("project-1")))
    thread.start()
    assert started.wait(timeout=5)
    try:
        with pytest.raises(WorkflowError) as caught:
            second.advance("project-1")
        assert caught.value.code == "PULSE_BUSY"
        assert effects.probes == []
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert [result.outcome for result in results] == ["OPERATION_ATTEMPT_RETURNED"]
    assert second.advance("project-1").outcome == "OPERATION_READBACK_RECORDED"


def test_readback_must_bind_the_exact_operation_digest(tmp_path: Path) -> None:
    class UnboundObservationEffects(ScriptedTracerEffects):
        def observe_operation(self, probe: Any) -> Mapping[str, Any]:
            return {"classification": "APPLIED", "evidence": {"applied": True}}

    kernel = make_kernel(tmp_path / "control.sqlite3", UnboundObservationEffects())
    prepare_operation(kernel)
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "INVALID_EFFECT_OBSERVATION"


def test_approval_expiry_policy_requires_an_aware_timestamp(tmp_path: Path) -> None:
    class NaiveExpiryEffects(ScriptedTracerEffects):
        def operation_policy(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
            return {
                **super().operation_policy(context),
                "approval_expires_at": "2026-09-03T00:00:00",
            }

    kernel = make_kernel(tmp_path / "control.sqlite3", NaiveExpiryEffects())
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "INVALID_OPERATION_POLICY"


def test_view_rejects_a_forged_terminal_without_readback_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    kernel = make_kernel(
        database_path, ScriptedTracerEffects(approval_required=True)
    )
    waiting = prepare_operation(kernel)
    with sqlite3.connect(database_path) as connection:
        operation = json.loads(
            connection.execute("SELECT operation_json FROM operation_records").fetchone()[0]
        )
        forged = {
            "event_type": "APPLIED",
            "intent_binding": operation["intent_binding"],
            "operation_digest": waiting.operation_digest,
        }
        payload_json = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO operation_events VALUES (?, 2, 'APPLIED', ?, ?, ?, ?, ?, ?, ?)",
            (
                waiting.operation_id,
                payload_json,
                digest(forged),
                1,
                1,
                1,
                waiting.intent_binding.active_intent_digest,
                NOW,
            ),
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.view("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"
