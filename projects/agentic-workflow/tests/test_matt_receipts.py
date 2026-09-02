from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import agentic_workflow
from agentic_workflow import UserDecision, WorkflowError, WorkflowKernel

PROFILE = json.loads(
    (Path(__file__).parents[1] / "config" / "operating-profile.v1.json").read_text()
)

IMPLEMENT_METHOD = {
    "allowed_next_methods": [],
    "completion_criterion": "BOUNDED_IMPLEMENTATION_COMPLETED",
    "expected_artifact": "IMPLEMENTATION_RESULT",
    "gates": ["SPEC_SATISFIED", "TESTS_PASSED"],
    "skill_digest": "6d3fd9e83b8f36e5213854779db49b256a457a7ebb4a503e53fa7dcff696adc3",
    "skill_name": "mattpocock:implement",
}


class ActorAuthenticator:
    def authenticate(self, decision: UserDecision) -> bool:
        return decision.authenticated_actor == "user-1" and decision.provenance == {
            "channel": "test"
        }


def digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class AttestingExternalEffects:
    executor_id = "trusted-local-executor"

    def __init__(self) -> None:
        self.invocations: list[Any] = []

    def attempt(self, invocation: Any) -> Any:
        self.invocations.append(invocation)
        artifact = {
            "artifact_type": invocation.expected_artifact,
            "result": "bounded implementation evidence",
        }
        return {
            "invocation_digest": invocation.invocation_digest,
            "executor_id": self.executor_id,
            "run_id": invocation.run_id,
            "skill_name": invocation.skill_name,
            "skill_digest": invocation.skill_digest,
            "load_proof": {
                "proof_kind": "EXECUTOR_VERIFIED_SKILL_LOAD",
                "skill_name": invocation.skill_name,
                "skill_digest": invocation.skill_digest,
                "executor_id": self.executor_id,
                "run_id": invocation.run_id,
            },
            "gate_outcomes": {
                gate: {"status": "PASSED", "evidence_digest": digest({"gate": gate})}
                for gate in invocation.gates
            },
            "artifact": artifact,
            "artifact_digest": digest(artifact),
            "completion_classification": "COMPLETED",
        }


def decision(**changes: object) -> UserDecision:
    event = UserDecision(
        project_id="project-1",
        source="test-ui",
        source_event_id="bootstrap-1",
        authenticated_actor="user-1",
        scope="PROJECT_INTENT",
        verbatim_text="Create the workflow project.",
        nonce="nonce-bootstrap",
        replay_identity="bootstrap-project-1",
        provenance={"channel": "test"},
        decision_kind="BOOTSTRAP_PROJECT",
        complete_revision_payload={
            "project": {"name": "Research Workflow"},
            "constitution": {
                "user_sovereignty": True,
                "external_effects_require_authority": True,
            },
            "goal": {
                "outcome": "Produce evidence-backed research decisions",
                "scope": "agentic-workflow-v1",
                "success_evidence": ["replayable decision record"],
                "constraints": ["no automatic deployment"],
                "accepted_tradeoffs": [],
                "non_goals": ["manage an issue backlog"],
            },
            "operating_profile": deepcopy(PROFILE),
        },
    )
    return replace(event, **changes)


def goal_revision() -> UserDecision:
    goal = deepcopy(decision().complete_revision_payload["goal"])
    goal["outcome"] = "Produce reviewed evidence-backed research decisions"
    return decision(
        source_event_id="goal-revision-1",
        scope="GOAL",
        verbatim_text="Require reviewed evidence.",
        nonce="nonce-goal-2",
        replay_identity="goal-revision-project-1-2",
        decision_kind="REVISE_GOAL",
        complete_revision_payload={"goal": goal, "compatibility": {}},
    )


def rewrite_action_kind(database_path: Path, action_kind: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER actions_no_update")
        connection.execute("DROP TRIGGER action_envelopes_no_update")
        action_json = connection.execute("SELECT action_json FROM actions").fetchone()[0]
        action = json.loads(action_json)
        action["action_kind"] = action_kind
        replacement_action_json = json.dumps(action, sort_keys=True, separators=(",", ":"))
        replacement_action_digest = hashlib.sha256(replacement_action_json.encode()).hexdigest()
        connection.execute(
            "UPDATE actions SET action_kind = ?, action_json = ?, action_digest = ?",
            (action_kind, replacement_action_json, replacement_action_digest),
        )
        envelope_json = connection.execute("SELECT envelope_json FROM action_envelopes").fetchone()[
            0
        ]
        envelope = json.loads(envelope_json)
        envelope["action_digest"] = replacement_action_digest
        envelope["method"] = IMPLEMENT_METHOD if action_kind == "GOAL_WORK" else None
        replacement_envelope_json = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        replacement_envelope_digest = hashlib.sha256(replacement_envelope_json.encode()).hexdigest()
        connection.execute(
            "UPDATE action_envelopes SET envelope_json = ?, action_envelope_digest = ?",
            (replacement_envelope_json, replacement_envelope_digest),
        )


def test_goal_work_is_cognitive_and_fails_closed_without_a_trusted_executor(
    tmp_path: Path,
) -> None:
    kernel = WorkflowKernel(
        tmp_path / "control.sqlite3",
        decision_authenticator=ActorAuthenticator(),
    )
    kernel.record(decision())
    envelope = kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert envelope.action_class == "cognitive"
    assert caught.value.code == "MATT_EXECUTOR_UNAVAILABLE"


def test_goal_work_freezes_its_applicable_method_in_the_action_envelope(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(database_path, decision_authenticator=ActorAuthenticator())
    kernel.record(decision())

    result = kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        envelope = json.loads(
            connection.execute("SELECT envelope_json FROM action_envelopes").fetchone()[0]
        )
    assert result.action_class == "cognitive"
    assert envelope["method"] == IMPLEMENT_METHOD


def test_known_mechanical_action_bypasses_matt_deterministically(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(database_path, decision_authenticator=ActorAuthenticator())
    kernel.record(decision())
    kernel.advance("project-1")
    rewrite_action_kind(database_path, "FROZEN_TEST_COMMAND")

    reservation = kernel.advance("project-1")

    assert reservation.outcome == "OPERATION_RESERVED"
    assert reservation.action_class == "mechanical"
    assert reservation.matt_invocation_id is None
    assert reservation.matt_receipt_id is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matt_invocations").fetchone()[0] == 0


def test_unknown_action_kind_is_cognitive_but_has_no_implicit_matt_method(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    executor = AttestingExternalEffects()
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=executor,
    )
    kernel.record(decision())
    kernel.advance("project-1")
    rewrite_action_kind(database_path, "UNRECOGNIZED_FUTURE_ACTION")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "MATT_METHOD_UNAVAILABLE"
    assert executor.invocations == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matt_invocations").fetchone()[0] == 0


def test_cognitive_action_accepts_executor_attested_invocation_and_receipt(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "control.sqlite3"
    executor = AttestingExternalEffects()
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=executor,
    )
    bootstrap = kernel.record(decision())
    envelope = kernel.advance("project-1")

    reservation = kernel.advance("project-1")

    assert reservation.outcome == "OPERATION_RESERVED"
    assert reservation.action_class == "cognitive"
    assert reservation.matt_invocation_id
    assert reservation.matt_invocation_digest
    assert reservation.matt_receipt_id
    assert reservation.matt_receipt_digest
    assert len(executor.invocations) == 1
    invocation = executor.invocations[0]
    assert invocation.skill_name == "mattpocock:implement"
    assert invocation.skill_digest == (
        "6d3fd9e83b8f36e5213854779db49b256a457a7ebb4a503e53fa7dcff696adc3"
    )
    assert invocation.executor_id == executor.executor_id
    assert invocation.input_evidence_digest
    assert invocation.gates == ("SPEC_SATISFIED", "TESTS_PASSED")
    assert invocation.completion_criterion == "BOUNDED_IMPLEMENTATION_COMPLETED"
    assert invocation.expected_artifact == "IMPLEMENTATION_RESULT"
    assert invocation.intent_binding == envelope.intent_binding
    assert invocation.action_envelope_digest == envelope.action_envelope_digest
    assert invocation.run_id

    with sqlite3.connect(database_path) as connection:
        envelope_json = connection.execute("SELECT envelope_json FROM action_envelopes").fetchone()[
            0
        ]
        invocation_json, stored_invocation_digest, created_at = connection.execute(
            "SELECT invocation_json, invocation_digest, created_at FROM matt_invocations"
        ).fetchone()
        attestation_json, recorded_at = connection.execute(
            "SELECT attestation_json, recorded_at FROM matt_executor_attestations"
        ).fetchone()
        receipt_json, stored_receipt_digest, accepted_at = connection.execute(
            "SELECT receipt_json, receipt_digest, accepted_at FROM matt_receipts"
        ).fetchone()

    stored_envelope = json.loads(envelope_json)
    stored_invocation = json.loads(invocation_json)
    stored_attestation = json.loads(attestation_json)
    stored_receipt = json.loads(receipt_json)
    assert stored_invocation_digest == reservation.matt_invocation_digest
    assert stored_receipt_digest == reservation.matt_receipt_digest
    assert stored_invocation["intent_binding"]["active_intent_digest"] == (
        bootstrap.active_intent_digest
    )
    assert stored_invocation["action_envelope_digest"] == envelope.action_envelope_digest
    assert stored_invocation["skill_name"] == stored_envelope["method"]["skill_name"]
    assert stored_invocation["skill_digest"] == stored_envelope["method"]["skill_digest"]
    assert stored_attestation["load_proof"]["proof_kind"] == ("EXECUTOR_VERIFIED_SKILL_LOAD")
    assert stored_receipt["actual_skill_digest"] == invocation.skill_digest
    assert stored_receipt["artifact_digest"] == stored_attestation["artifact_digest"]
    assert stored_receipt["completion_classification"] == "COMPLETED"
    assert stored_receipt["allowed_next_methods"] == []
    assert stored_receipt["intent_binding"] == stored_invocation["intent_binding"]
    assert stored_receipt["action_envelope_digest"] == envelope.action_envelope_digest
    assert stored_receipt["route"] == {
        "executor_id": executor.executor_id,
        "kind": "LOCAL_TRUSTED_EXECUTOR",
        "run_id": invocation.run_id,
    }
    assert stored_receipt["route"] == stored_invocation["route"]
    assert stored_invocation["created_at"] == created_at
    assert stored_attestation["recorded_at"] == recorded_at
    assert stored_receipt["accepted_at"] == accepted_at

    conclusion = kernel.advance("project-1")
    assert conclusion.outcome == "OPERATION_CONCLUDED"
    assert conclusion.matt_invocation_id == reservation.matt_invocation_id
    assert conclusion.matt_receipt_id == reservation.matt_receipt_id


def test_artifact_mismatched_attestation_fails_closed_without_a_receipt(
    tmp_path: Path,
) -> None:
    class ArtifactMismatchExecutor(AttestingExternalEffects):
        def attempt(self, invocation: Any) -> dict[str, object]:
            attestation = super().attempt(invocation)
            attestation["artifact_digest"] = "0" * 64
            return attestation

    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=ArtifactMismatchExecutor(),
    )
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "MATT_RECEIPT_REJECTED"
    with sqlite3.connect(database_path) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM matt_invocations), "
            "(SELECT COUNT(*) FROM matt_executor_attestations), "
            "(SELECT COUNT(*) FROM matt_receipts), "
            "(SELECT COUNT(*) FROM operation_records)"
        ).fetchone()
    assert counts == (1, 0, 0, 0)


def test_executor_runs_outside_the_sqlite_write_transaction(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"

    class LockCheckingExecutor(AttestingExternalEffects):
        def attempt(self, invocation: Any) -> dict[str, object]:
            with sqlite3.connect(database_path, timeout=0.1, isolation_level=None) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("COMMIT")
            return super().attempt(invocation)

    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=LockCheckingExecutor(),
    )
    kernel.record(decision())
    kernel.advance("project-1")

    assert kernel.advance("project-1").outcome == "OPERATION_RESERVED"


def test_intent_change_during_execution_rejects_the_stale_receipt(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    revision_kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
    )

    class RevisingExecutor(AttestingExternalEffects):
        def attempt(self, invocation: Any) -> dict[str, object]:
            revision_kernel.record(goal_revision())
            return super().attempt(invocation)

    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=RevisingExecutor(),
    )
    kernel.record(decision())
    stale_envelope = kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "STALE_INTENT"
    with sqlite3.connect(database_path) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM matt_invocations), "
            "(SELECT COUNT(*) FROM matt_executor_attestations), "
            "(SELECT COUNT(*) FROM matt_receipts), "
            "(SELECT COUNT(*) FROM operation_records)"
        ).fetchone()
    assert counts == (1, 0, 0, 0)
    fresh = kernel.advance("project-1")
    assert fresh.outcome == "ACTION_ENVELOPED"
    assert fresh.action_envelope_id != stale_envelope.action_envelope_id


def test_failed_execution_is_ambiguous_and_is_never_retried(tmp_path: Path) -> None:
    class FailingExternalEffects(AttestingExternalEffects):
        def attempt(self, invocation: Any) -> dict[str, object]:
            self.invocations.append(invocation)
            raise RuntimeError("injected executor failure")

    database_path = tmp_path / "control.sqlite3"
    executor = FailingExternalEffects()
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=executor,
    )
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")
    with pytest.raises(WorkflowError) as replay:
        kernel.advance("project-1")

    assert caught.value.code == "MATT_EXECUTION_AMBIGUOUS"
    assert replay.value.code == "MATT_EXECUTION_AMBIGUOUS"
    assert len(executor.invocations) == 1
    with sqlite3.connect(database_path) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM matt_invocations), "
            "(SELECT COUNT(*) FROM matt_execution_attempts), "
            "(SELECT COUNT(*) FROM matt_execution_observations), "
            "(SELECT COUNT(*) FROM matt_receipts)"
        ).fetchone()
    assert counts == (1, 1, 1, 0)


def test_crash_gap_after_durable_attempt_is_ambiguous_and_never_retried(tmp_path: Path) -> None:
    class CrashingExternalEffects(AttestingExternalEffects):
        def attempt(self, invocation: Any) -> Any:
            self.invocations.append(invocation)
            raise KeyboardInterrupt("simulated process loss")

    database_path = tmp_path / "control.sqlite3"
    effects = CrashingExternalEffects()
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=effects,
    )
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(KeyboardInterrupt):
        kernel.advance("project-1")
    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "MATT_EXECUTION_AMBIGUOUS"
    assert len(effects.invocations) == 1
    with sqlite3.connect(database_path) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM matt_execution_attempts), "
            "(SELECT COUNT(*) FROM matt_execution_observations)"
        ).fetchone()
    assert counts == (1, 0)


def test_concurrent_advance_deduplicates_one_durable_external_attempt(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingExternalEffects(AttestingExternalEffects):
        def attempt(self, invocation: Any) -> Any:
            entered.set()
            assert release.wait(timeout=5)
            return super().attempt(invocation)

    database_path = tmp_path / "control.sqlite3"
    effects = BlockingExternalEffects()
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=effects,
    )
    kernel.record(decision())
    kernel.advance("project-1")
    results: list[object] = []

    def advance() -> None:
        try:
            results.append(kernel.advance("project-1"))
        except Exception as error:
            results.append(error)

    first = threading.Thread(target=advance)
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=advance)
    second.start()
    second.join(timeout=5)
    release.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(effects.invocations) == 1
    assert sum(getattr(result, "outcome", None) == "OPERATION_RESERVED" for result in results) == 1
    errors = [result for result in results if isinstance(result, WorkflowError)]
    assert len(errors) == 1
    assert errors[0].code == "PULSE_BUSY"
    with sqlite3.connect(database_path) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM matt_execution_attempts), "
            "(SELECT COUNT(*) FROM matt_execution_observations), "
            "(SELECT COUNT(*) FROM matt_receipts)"
        ).fetchone()
    assert counts == (1, 1, 1)


def test_matt_uses_only_the_external_effects_attempt_seam(tmp_path: Path) -> None:
    class SingleSeamExternalEffects(AttestingExternalEffects):
        def execute(self, invocation: object) -> object:
            raise AssertionError("executor-specific seam must not be used")

    effects = SingleSeamExternalEffects()
    kernel = WorkflowKernel(
        tmp_path / "control.sqlite3",
        decision_authenticator=ActorAuthenticator(),
        external_effects=effects,
    )
    kernel.record(decision())
    kernel.advance("project-1")

    assert kernel.advance("project-1").outcome == "OPERATION_RESERVED"
    assert len(effects.invocations) == 1


def test_connector_contracts_are_not_public_api() -> None:
    assert not hasattr(agentic_workflow, "MattInvocation")
    assert not hasattr(agentic_workflow, "MattExecutionAttestation")


def test_frozen_invocation_cannot_be_executed_by_a_different_adapter(tmp_path: Path) -> None:
    class FreezingExecutor(AttestingExternalEffects):
        executor_id = "trusted-executor-a"

        def attempt(self, invocation: Any) -> dict[str, object]:
            self.invocations.append(invocation)
            raise RuntimeError("leave the invocation frozen")

    class ImpersonatingExecutor(AttestingExternalEffects):
        executor_id = "trusted-executor-b"

        def attempt(self, invocation: Any) -> dict[str, object]:
            attestation = super().attempt(invocation)
            attestation["executor_id"] = invocation.executor_id
            attestation["load_proof"] = {
                **attestation["load_proof"],  # type: ignore[misc]
                "executor_id": invocation.executor_id,
            }
            return attestation

    database_path = tmp_path / "control.sqlite3"
    executor_a = FreezingExecutor()
    kernel_a = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=executor_a,
    )
    kernel_a.record(decision())
    kernel_a.advance("project-1")
    with pytest.raises(WorkflowError, match="ambiguous"):
        kernel_a.advance("project-1")

    executor_b = ImpersonatingExecutor()
    kernel_b = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=executor_b,
    )
    with pytest.raises(WorkflowError) as caught:
        kernel_b.advance("project-1")

    assert caught.value.code == "MATT_EXECUTOR_MISMATCH"
    assert executor_b.invocations == []


@pytest.mark.parametrize(
    "mismatch",
    [
        "self-declared-receipt",
        "invocation",
        "skill",
        "executor",
        "run",
        "load-proof",
        "missing-gate",
        "failed-gate",
        "artifact-type",
        "completion",
    ],
)
def test_forged_or_mismatched_executor_evidence_never_becomes_a_receipt(
    tmp_path: Path, mismatch: str
) -> None:
    class MismatchingExecutor(AttestingExternalEffects):
        def attempt(self, invocation: Any) -> object:
            attestation = super().attempt(invocation)
            if mismatch == "self-declared-receipt":
                return {"receipt_id": "worker-authored", "outcome": "COMPLETED"}
            if mismatch == "invocation":
                attestation["invocation_digest"] = "0" * 64
            if mismatch == "skill":
                attestation["skill_digest"] = "0" * 64
            if mismatch == "executor":
                attestation["executor_id"] = "untrusted-worker"
            if mismatch == "run":
                attestation["run_id"] = "other-run"
            if mismatch == "load-proof":
                attestation["load_proof"] = {"proof_kind": "SELF_DECLARED"}
            if mismatch == "missing-gate":
                outcomes = dict(attestation["gate_outcomes"])  # type: ignore[arg-type]
                outcomes.pop(invocation.gates[0])
                attestation["gate_outcomes"] = outcomes
            if mismatch == "failed-gate":
                outcomes = dict(attestation["gate_outcomes"])  # type: ignore[arg-type]
                outcomes[invocation.gates[0]] = {
                    "status": "FAILED",
                    "evidence_digest": digest({"gate": invocation.gates[0]}),
                }
                attestation["gate_outcomes"] = outcomes
            if mismatch == "artifact-type":
                artifact = {"artifact_type": "WORKER_PROSE", "result": "not evidence"}
                attestation["artifact"] = artifact
                attestation["artifact_digest"] = digest(artifact)
            if mismatch == "completion":
                attestation["completion_classification"] = "SELF_DECLARED_DONE"
            return attestation

    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=MismatchingExecutor(),  # type: ignore[arg-type]
    )
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "MATT_RECEIPT_REJECTED"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matt_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operation_records").fetchone()[0] == 0


def test_record_never_accepts_a_worker_authored_receipt(tmp_path: Path) -> None:
    kernel = WorkflowKernel(
        tmp_path / "control.sqlite3",
        decision_authenticator=ActorAuthenticator(),
    )

    with pytest.raises(WorkflowError) as caught:
        kernel.record({"receipt_id": "forged", "completion": "COMPLETED"})  # type: ignore[arg-type]

    assert caught.value.code == "INVALID_EVENT"


@pytest.mark.parametrize(
    ("table", "payload_column", "trigger"),
    [
        ("matt_invocations", "invocation_json", "matt_invocations_no_update"),
        (
            "matt_executor_attestations",
            "attestation_json",
            "matt_executor_attestations_no_update",
        ),
        ("matt_receipts", "receipt_json", "matt_receipts_no_update"),
    ],
)
def test_tampered_matt_history_fails_closed_on_the_public_advance_seam(
    tmp_path: Path, table: str, payload_column: str, trigger: str
) -> None:
    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=AttestingExternalEffects(),
    )
    kernel.record(decision())
    kernel.advance("project-1")
    kernel.advance("project-1")
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET {payload_column} = '{{}}'")  # noqa: S608

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    ("table", "column", "trigger"),
    [
        ("matt_invocations", "project_id", "matt_invocations_no_update"),
        ("matt_invocations", "action_id", "matt_invocations_no_update"),
        (
            "matt_executor_attestations",
            "attestation_id",
            "matt_executor_attestations_no_update",
        ),
        ("matt_receipts", "receipt_id", "matt_receipts_no_update"),
        ("matt_receipts", "project_id", "matt_receipts_no_update"),
        ("matt_receipts", "attestation_id", "matt_receipts_no_update"),
        ("matt_receipts", "action_envelope_id", "matt_receipts_no_update"),
    ],
)
def test_tampered_matt_indexed_identity_fails_closed_on_the_public_advance_seam(
    tmp_path: Path, table: str, column: str, trigger: str
) -> None:
    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=AttestingExternalEffects(),
    )
    kernel.record(decision())
    kernel.advance("project-1")
    kernel.advance("project-1")
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET {column} = 'tampered-index'")  # noqa: S608

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    ("table", "column", "trigger"),
    [
        ("matt_invocations", "created_at", "matt_invocations_no_update"),
        (
            "matt_executor_attestations",
            "recorded_at",
            "matt_executor_attestations_no_update",
        ),
        ("matt_receipts", "accepted_at", "matt_receipts_no_update"),
    ],
)
def test_tampered_matt_evidence_timestamp_fails_closed_on_advance(
    tmp_path: Path, table: str, column: str, trigger: str
) -> None:
    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=AttestingExternalEffects(),
    )
    kernel.record(decision())
    kernel.advance("project-1")
    kernel.advance("project-1")
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET {column} = 'tampered-time'")  # noqa: S608

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("matt_invocations", "invocation_json"),
        ("matt_execution_attempts", "attempt_json"),
        ("matt_execution_observations", "observation_json"),
        ("matt_executor_attestations", "attestation_json"),
        ("matt_receipts", "receipt_json"),
    ],
)
def test_matt_evidence_history_is_append_only(tmp_path: Path, table: str, column: str) -> None:
    database_path = tmp_path / "control.sqlite3"
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=AttestingExternalEffects(),
    )
    kernel.record(decision())
    kernel.advance("project-1")
    kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"UPDATE {table} SET {column} = '{{}}'")  # noqa: S608
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"DELETE FROM {table}")  # noqa: S608
