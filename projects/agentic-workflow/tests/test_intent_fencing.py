from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

import pytest

from agentic_workflow import UserDecision, WorkflowError, WorkflowKernel

PROFILE = json.loads(
    (Path(__file__).parents[1] / "config" / "operating-profile.v1.json").read_text()
)


class ActorAuthenticator:
    def authenticate(self, decision: UserDecision) -> bool:
        return decision.authenticated_actor == "user-1" and decision.provenance == {
            "channel": "test"
        }


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "control.sqlite3"


@pytest.fixture
def kernel(database_path: Path) -> WorkflowKernel:
    return WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
    )


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


def revised_goal() -> dict[str, object]:
    return {
        "outcome": "Produce reviewed evidence-backed research decisions",
        "scope": "agentic-workflow-v1",
        "success_evidence": ["replayable decision record", "independent review"],
        "constraints": ["no automatic deployment"],
        "accepted_tradeoffs": ["invalidate stale work"],
        "non_goals": ["manage an issue backlog"],
    }


def goal_revision(compatibility: dict[str, str]) -> UserDecision:
    return decision(
        source_event_id="goal-revision-1",
        scope="GOAL",
        verbatim_text="Require independent review.",
        nonce="nonce-goal-2",
        replay_identity="goal-revision-project-1-2",
        decision_kind="REVISE_GOAL",
        complete_revision_payload={"goal": revised_goal(), "compatibility": compatibility},
    )


def profile_revision(compatibility: dict[str, str]) -> UserDecision:
    profile = deepcopy(PROFILE)
    profile["profile_id"] = "agentic-workflow-v1-revised"
    return decision(
        source_event_id="profile-revision-1",
        scope="OPERATING_PROFILE",
        verbatim_text="Use the revised execution profile.",
        nonce="nonce-profile-2",
        replay_identity="profile-revision-project-1-2",
        decision_kind="REVISE_OPERATING_PROFILE",
        complete_revision_payload={
            "operating_profile": profile,
            "compatibility": compatibility,
        },
    )


def run_crash_probe(
    database_path: Path, fault_point: str, revision: UserDecision | None = None
) -> subprocess.CompletedProcess[str]:
    script = """
import json
import os
import sys

import agentic_workflow.kernel as kernel_module
from agentic_workflow import UserDecision, WorkflowKernel

class Authenticator:
    def authenticate(self, decision):
        return (
            decision.authenticated_actor == "user-1"
            and decision.provenance == {"channel": "test"}
        )

def crash_at_selected_point(point):
    if point == sys.argv[2]:
        os._exit(91)

kernel_module._PRIVATE_FAULT_HOOK = crash_at_selected_point
kernel = WorkflowKernel(sys.argv[1], decision_authenticator=Authenticator())
if sys.argv[2].startswith("revision_"):
    kernel.record(UserDecision(**json.loads(os.environ["CRASH_REVISION_JSON"])))
else:
    kernel.advance("project-1")
"""
    environment = os.environ.copy()
    source = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    if revision is not None:
        environment["CRASH_REVISION_JSON"] = json.dumps(asdict(revision))
    return subprocess.run(
        [sys.executable, "-c", script, str(database_path), fault_point],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_authenticated_goal_revision_atomically_changes_active_intent(
    kernel: WorkflowKernel,
) -> None:
    original = kernel.record(decision())
    revision = decision(
        source_event_id="goal-revision-1",
        scope="GOAL",
        verbatim_text="Require independent review.",
        nonce="nonce-goal-2",
        replay_identity="goal-revision-project-1-2",
        decision_kind="REVISE_GOAL",
        complete_revision_payload={"goal": revised_goal(), "compatibility": {}},
    )

    receipt = kernel.record(revision)

    assert receipt.outcome == "GOAL_REVISED"
    assert receipt.active_intent_digest != original.active_intent_digest
    assert kernel.view("project-1").current_goal == revised_goal()


def test_authenticated_operating_profile_revision_changes_active_intent(
    kernel: WorkflowKernel,
) -> None:
    original = kernel.record(decision())
    profile = deepcopy(PROFILE)
    profile["profile_id"] = "agentic-workflow-v1-revised"
    revision = decision(
        source_event_id="profile-revision-1",
        scope="OPERATING_PROFILE",
        verbatim_text="Use the revised execution profile.",
        nonce="nonce-profile-2",
        replay_identity="profile-revision-project-1-2",
        decision_kind="REVISE_OPERATING_PROFILE",
        complete_revision_payload={"operating_profile": profile, "compatibility": {}},
    )

    receipt = kernel.record(revision)
    current_work = kernel.advance("project-1")

    assert receipt.outcome == "OPERATING_PROFILE_REVISED"
    assert receipt.active_intent_digest != original.active_intent_digest
    assert current_work.intent_binding.operating_profile_revision == 2
    assert current_work.intent_binding.active_intent_digest == receipt.active_intent_digest
    assert kernel.view("project-1").current_goal == decision().complete_revision_payload["goal"]


def test_advance_freezes_an_action_envelope_with_the_complete_intent_binding(
    kernel: WorkflowKernel,
) -> None:
    bootstrap = kernel.record(decision())

    result = kernel.advance("project-1")

    assert result.outcome == "ACTION_ENVELOPED"
    assert result.intent_binding.constitution_revision == 1
    assert result.intent_binding.goal_revision == 1
    assert result.intent_binding.operating_profile_revision == 1
    assert result.intent_binding.active_intent_digest == bootstrap.active_intent_digest
    assert result.action_id
    assert result.action_envelope_id
    assert result.action_envelope_digest
    assert result.predecessor_action_envelope_id is None
    assert result.operation_id is None


def test_operation_reservation_rechecks_and_embeds_the_envelope_intent_binding(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(decision())
    envelope = kernel.advance("project-1")

    reservation = kernel.advance("project-1")

    assert reservation.outcome == "OPERATION_RESERVED"
    assert reservation.action_id == envelope.action_id
    assert reservation.action_envelope_id == envelope.action_envelope_id
    assert reservation.action_envelope_digest == envelope.action_envelope_digest
    assert reservation.intent_binding == envelope.intent_binding
    assert reservation.operation_id
    assert reservation.operation_digest


def test_current_operation_concludes_with_an_append_only_lifecycle_event(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(decision())
    kernel.advance("project-1")
    reservation = kernel.advance("project-1")

    conclusion = kernel.advance("project-1")

    assert conclusion.outcome == "OPERATION_CONCLUDED"
    assert conclusion.operation_id == reservation.operation_id
    assert conclusion.operation_digest == reservation.operation_digest
    assert conclusion.intent_binding == reservation.intent_binding


def test_unknown_compatibility_fences_stale_work_and_starts_fresh(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(decision())
    kernel.advance("project-1")
    revision = kernel.record(goal_revision({}))

    fresh = kernel.advance("project-1")

    assert fresh.outcome == "ACTION_ENVELOPED"
    assert fresh.predecessor_action_envelope_id is None
    assert fresh.intent_binding.active_intent_digest == revision.active_intent_digest


def test_compatible_work_continues_only_through_a_new_current_envelope(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(decision())
    old = kernel.advance("project-1")
    revision = kernel.record(goal_revision({old.action_envelope_digest: "compatible"}))

    successor = kernel.advance("project-1")

    assert successor.outcome == "WORK_REENVELOPED"
    assert successor.action_id != old.action_id
    assert successor.action_envelope_id != old.action_envelope_id
    assert successor.action_envelope_digest != old.action_envelope_digest
    assert successor.predecessor_action_envelope_id == old.action_envelope_id
    assert successor.intent_binding.goal_revision == 2
    assert successor.intent_binding.active_intent_digest == revision.active_intent_digest
    assert successor.operation_id is None
    reservation = kernel.advance("project-1")
    assert reservation.outcome == "OPERATION_RESERVED"
    assert reservation.action_envelope_id == successor.action_envelope_id


def test_explicit_incompatibility_does_not_continue_stale_work(kernel: WorkflowKernel) -> None:
    kernel.record(decision())
    stale = kernel.advance("project-1")
    revision = kernel.record(goal_revision({stale.action_envelope_digest: "incompatible"}))

    fresh = kernel.advance("project-1")

    assert fresh.outcome == "ACTION_ENVELOPED"
    assert fresh.predecessor_action_envelope_id is None
    assert fresh.intent_binding.active_intent_digest == revision.active_intent_digest


def test_stale_reserved_operation_is_fenced_and_cannot_conclude(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(decision())
    kernel.advance("project-1")
    reservation = kernel.advance("project-1")
    kernel.record(goal_revision({}))

    fresh = kernel.advance("project-1")

    assert fresh.outcome == "ACTION_ENVELOPED"
    assert fresh.operation_id is None
    with sqlite3.connect(database_path) as connection:
        concluded = connection.execute(
            "SELECT 1 FROM operation_events WHERE operation_id = ? AND event_type = 'CONCLUDED'",
            (reservation.operation_id,),
        ).fetchone()
    assert concluded is None


@pytest.mark.parametrize("entrypoint", ["record", "advance", "view"])
def test_rolled_back_current_intent_pointer_cannot_restore_stale_authority(
    kernel: WorkflowKernel, database_path: Path, entrypoint: str
) -> None:
    kernel.record(decision())
    kernel.advance("project-1")
    reservation = kernel.advance("project-1")
    kernel.record(goal_revision({}))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE active_intent_current SET intent_number = 1 WHERE project_id = 'project-1'"
        )

    with pytest.raises(WorkflowError) as caught:
        if entrypoint == "record":
            kernel.record(profile_revision({}))
        elif entrypoint == "advance":
            kernel.advance("project-1")
        else:
            kernel.view("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"
    with sqlite3.connect(database_path) as connection:
        concluded = connection.execute(
            "SELECT 1 FROM operation_events WHERE operation_id = ? AND event_type = 'CONCLUDED'",
            (reservation.operation_id,),
        ).fetchone()
    assert concluded is None


def test_operating_profile_revision_also_fences_stale_envelopes(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(decision())
    kernel.advance("project-1")
    revision = kernel.record(profile_revision({}))

    fresh = kernel.advance("project-1")

    assert revision.outcome == "OPERATING_PROFILE_REVISED"
    assert fresh.outcome == "ACTION_ENVELOPED"
    assert fresh.intent_binding.active_intent_digest == revision.active_intent_digest


def test_compatibility_accepts_only_sources_from_the_immediately_preceding_intent(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(decision())
    stale = kernel.advance("project-1")
    kernel.record(goal_revision({stale.action_envelope_digest: "compatible"}))
    with pytest.raises(WorkflowError) as caught:
        kernel.record(profile_revision({stale.action_envelope_digest: "compatible"}))
    assert caught.value.code == "INVALID_REVISION"
    revision = kernel.record(profile_revision({}))

    fresh = kernel.advance("project-1")

    assert fresh.outcome == "ACTION_ENVELOPED"
    assert fresh.predecessor_action_envelope_id is None
    assert fresh.intent_binding.active_intent_digest == revision.active_intent_digest


def test_revision_activation_rejects_tampered_compatibility_source_envelope(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(decision())
    source = kernel.advance("project-1")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER action_envelopes_no_update")
        envelope = json.loads(
            connection.execute("SELECT envelope_json FROM action_envelopes").fetchone()[0]
        )
        envelope["action_envelope_id"] = "tampered-id"
        envelope_json = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        envelope_digest = hashlib.sha256(envelope_json.encode()).hexdigest()
        connection.execute(
            "UPDATE action_envelopes SET envelope_json = ?, action_envelope_digest = ?",
            (envelope_json, envelope_digest),
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(goal_revision({envelope_digest: "compatible"}))

    assert source.action_envelope_digest != envelope_digest
    assert caught.value.code == "LEDGER_INTEGRITY"


def test_revision_activation_verifies_the_current_intent_before_appending(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(decision())
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER goal_revisions_no_update")
        connection.execute(
            "UPDATE goal_revisions SET payload_json = '{}' "
            "WHERE project_id = 'project-1' AND revision_number = 1"
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(goal_revision({}))

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize("corruption", ["noncanonical", "incomplete_schema"])
def test_revision_activation_revalidates_canonical_complete_v1_intent_payloads(
    kernel: WorkflowKernel, database_path: Path, corruption: str
) -> None:
    kernel.record(decision())
    goal = decision().complete_revision_payload["goal"]
    if corruption == "noncanonical":
        goal_json = json.dumps(goal, sort_keys=False, indent=2)
    else:
        incomplete = deepcopy(goal)
        del incomplete["non_goals"]
        goal_json = json.dumps(incomplete, sort_keys=True, separators=(",", ":"))
    goal_digest = hashlib.sha256(goal_json.encode()).hexdigest()
    with sqlite3.connect(database_path) as connection:
        current = connection.execute(
            "SELECT i.constitution_revision, i.goal_revision, i.operating_profile_revision, "
            "c.payload_digest, o.payload_digest "
            "FROM active_intent_current AS current "
            "JOIN active_intents AS i ON i.project_id = current.project_id "
            "AND i.intent_number = current.intent_number "
            "JOIN constitution_revisions AS c ON c.project_id = i.project_id "
            "AND c.revision_number = i.constitution_revision "
            "JOIN operating_profile_revisions AS o ON o.project_id = i.project_id "
            "AND o.revision_number = i.operating_profile_revision"
        ).fetchone()
        intent_json = json.dumps(
            {
                "constitution_digest": current[3],
                "constitution_revision": current[0],
                "goal_digest": goal_digest,
                "goal_revision": current[1],
                "operating_profile_digest": current[4],
                "operating_profile_revision": current[2],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        active_intent_digest = hashlib.sha256(intent_json.encode()).hexdigest()
        connection.execute("DROP TRIGGER goal_revisions_no_update")
        connection.execute("DROP TRIGGER active_intents_no_update")
        connection.execute(
            "UPDATE goal_revisions SET payload_json = ?, payload_digest = ?",
            (goal_json, goal_digest),
        )
        connection.execute(
            "UPDATE active_intents SET active_intent_digest = ?",
            (active_intent_digest,),
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(goal_revision({}))

    assert caught.value.code == "LEDGER_INTEGRITY"


def test_revision_replay_returns_the_original_receipt_and_conflicts_fail_closed(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(decision())
    revision = goal_revision({})
    original = kernel.record(revision)

    assert kernel.record(revision) == original

    conflicting_goal = revised_goal()
    conflicting_goal["outcome"] = "Conflicting revision"
    conflict = replace(
        revision,
        complete_revision_payload={"goal": conflicting_goal, "compatibility": {}},
    )
    with pytest.raises(WorkflowError) as caught:
        kernel.record(conflict)

    assert caught.value.code == "IDENTITY_CONFLICT"


def test_revision_replay_rejects_noncanonical_receipt_json_with_coherent_digest(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(decision())
    revision = goal_revision({})
    kernel.record(revision)
    with sqlite3.connect(database_path) as connection:
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM inbox_events WHERE source_event_id = 'goal-revision-1'"
            ).fetchone()[0]
        )
        replacement = json.dumps(receipt, sort_keys=False, indent=2)
        replacement_digest = hashlib.sha256(replacement.encode()).hexdigest()
        connection.execute("DROP TRIGGER inbox_events_no_update")
        connection.execute(
            "UPDATE inbox_events SET receipt_json = ?, receipt_digest = ? "
            "WHERE source_event_id = 'goal-revision-1'",
            (replacement, replacement_digest),
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(revision)

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("receipt_id", ""),
        ("receipt_id", "not-a-uuid"),
        ("receipt_id", "00000000-0000-4000-8000-000000000000"),
        ("project_id", "other-project"),
        ("event_type", "OTHER"),
        ("event_digest", "0" * 64),
        ("recorded_at", "2000-01-01T00:00:00+00:00"),
        ("outcome", "OPERATING_PROFILE_REVISED"),
        ("active_intent_digest", "bootstrap"),
    ],
    ids=[
        "blank-receipt-id",
        "invalid-receipt-id",
        "different-valid-receipt-id",
        "project-id",
        "event-type",
        "event-digest",
        "recorded-at",
        "outcome",
        "activated-intent",
    ],
)
def test_revision_replay_rejects_coherently_altered_receipt_fields(
    kernel: WorkflowKernel,
    database_path: Path,
    field: str,
    replacement: str,
) -> None:
    bootstrap = kernel.record(decision())
    revision = goal_revision({})
    kernel.record(revision)
    if replacement == "bootstrap":
        replacement = bootstrap.active_intent_digest or ""
    with sqlite3.connect(database_path) as connection:
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM inbox_events WHERE source_event_id = 'goal-revision-1'"
            ).fetchone()[0]
        )
        receipt[field] = replacement
        replacement_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        replacement_digest = hashlib.sha256(replacement_json.encode()).hexdigest()
        connection.execute("DROP TRIGGER inbox_events_no_update")
        connection.execute(
            "UPDATE inbox_events SET receipt_json = ?, receipt_digest = ? "
            "WHERE source_event_id = 'goal-revision-1'",
            (replacement_json, replacement_digest),
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(revision)

    assert caught.value.code == "LEDGER_INTEGRITY"


def test_revision_requires_a_complete_authenticated_immutable_snapshot(
    database_path: Path,
) -> None:
    revision = goal_revision({})

    class MutatingAuthenticator(ActorAuthenticator):
        def authenticate(self, decision: UserDecision) -> bool:
            if decision.decision_kind == "REVISE_GOAL":
                decision.complete_revision_payload["goal"]["constraints"].append(
                    "mutated during authentication"
                )
            return super().authenticate(decision)

    mutating_kernel = WorkflowKernel(
        database_path,
        decision_authenticator=MutatingAuthenticator(),
    )
    mutating_kernel.record(decision())

    with pytest.raises(WorkflowError) as caught:
        mutating_kernel.record(revision)
    assert caught.value.code == "INVALID_EVENT"

    incomplete = deepcopy(revision.complete_revision_payload)
    del incomplete["goal"]["accepted_tradeoffs"]
    validating_kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
    )
    with pytest.raises(WorkflowError) as missing:
        validating_kernel.record(
            replace(
                revision,
                source_event_id="goal-revision-incomplete",
                nonce="nonce-goal-incomplete",
                replay_identity="goal-revision-incomplete",
                complete_revision_payload=incomplete,
            )
        )
    assert missing.value.code == "INVALID_REVISION"


def test_revision_replay_verifies_its_nonce_identity_history(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(decision())
    revision = goal_revision({})
    kernel.record(revision)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER decision_nonces_no_update")
        connection.execute(
            "UPDATE decision_nonces SET nonce = 'tampered' "
            "WHERE source_event_id = 'goal-revision-1'"
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(revision)

    assert caught.value.code == "LEDGER_INTEGRITY"


def test_concurrent_goal_revision_writers_serialize_with_linear_history(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    import agentic_workflow.kernel as kernel_module
    import agentic_workflow.store as store_module

    kernel.record(decision())

    revisions = []
    for number in (2, 3):
        goal = revised_goal()
        goal["outcome"] = f"Concurrent goal {number}"
        revisions.append(
            replace(
                goal_revision({}),
                source_event_id=f"goal-revision-{number}",
                nonce=f"nonce-goal-{number}",
                replay_identity=f"goal-revision-project-1-{number}",
                complete_revision_payload={"goal": goal, "compatibility": {}},
            )
        )

    concurrent_kernels = [
        WorkflowKernel(database_path, decision_authenticator=ActorAuthenticator())
        for _ in revisions
    ]
    holder_entered = Event()
    release_holder = Event()
    competing_begin_attempted = Event()

    def hold_first_revision(point: str) -> None:
        if point == "revision_after_history_append":
            holder_entered.set()
            if not release_holder.wait(timeout=10):
                raise TimeoutError("test did not release revision lock holder")

    kernel_module._PRIVATE_FAULT_HOOK = hold_first_revision
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            holder_future = pool.submit(concurrent_kernels[0].record, revisions[0])
            try:
                assert holder_entered.wait(timeout=10)
                store_module._PRIVATE_TRANSACTION_BEGIN_HOOK = competing_begin_attempted.set
                competing_future = pool.submit(concurrent_kernels[1].record, revisions[1])
                assert competing_begin_attempted.wait(timeout=10)
                assert not competing_future.done()
            finally:
                release_holder.set()
            digests = [
                holder_future.result(timeout=10).active_intent_digest or "",
                competing_future.result(timeout=10).active_intent_digest or "",
            ]
    finally:
        kernel_module._PRIVATE_FAULT_HOOK = None
        store_module._PRIVATE_TRANSACTION_BEGIN_HOOK = None

    with sqlite3.connect(database_path) as connection:
        history = connection.execute(
            "SELECT intent_number, goal_revision, active_intent_digest "
            "FROM active_intents WHERE project_id = 'project-1' ORDER BY intent_number"
        ).fetchall()
        current = connection.execute(
            "SELECT intent_number FROM active_intent_current WHERE project_id = 'project-1'"
        ).fetchone()

    assert [row[:2] for row in history] == [(1, 1), (2, 2), (3, 3)]
    assert len({row[2] for row in history}) == 3
    assert set(digests) == {history[1][2], history[2][2]}
    assert current == (3,)
    assert kernel.view("project-1").current_goal["outcome"] in {
        "Concurrent goal 2",
        "Concurrent goal 3",
    }


@pytest.mark.parametrize(
    ("table", "update_column"),
    [
        ("actions", "action_kind"),
        ("action_envelopes", "envelope_json"),
        ("operation_records", "operation_json"),
        ("operation_events", "payload_json"),
        ("compatibility_decisions", "decision_json"),
    ],
)
def test_new_authoritative_history_tables_reject_update_and_delete(
    kernel: WorkflowKernel,
    database_path: Path,
    table: str,
    update_column: str,
) -> None:
    kernel.record(decision())
    old = kernel.advance("project-1")
    kernel.record(goal_revision({old.action_envelope_digest: "compatible"}))
    kernel.advance("project-1")
    kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"UPDATE {table} SET {update_column} = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"DELETE FROM {table}")


def test_authoritative_work_artifacts_share_the_exact_current_intent_binding(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(decision())
    old = kernel.advance("project-1")
    revision = kernel.record(goal_revision({old.action_envelope_digest: "compatible"}))
    successor = kernel.advance("project-1")
    reservation = kernel.advance("project-1")
    conclusion = kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = {
            "action": connection.execute(
                "SELECT action_json AS payload, constitution_revision, goal_revision, "
                "operating_profile_revision, active_intent_digest FROM actions "
                "WHERE action_id = ?",
                (successor.action_id,),
            ).fetchone(),
            "envelope": connection.execute(
                "SELECT envelope_json AS payload, constitution_revision, goal_revision, "
                "operating_profile_revision, active_intent_digest FROM action_envelopes "
                "WHERE action_envelope_id = ?",
                (successor.action_envelope_id,),
            ).fetchone(),
            "compatibility": connection.execute(
                "SELECT decision_json AS payload, constitution_revision, goal_revision, "
                "operating_profile_revision, active_intent_digest "
                "FROM compatibility_decisions WHERE source_action_envelope_digest = ?",
                (old.action_envelope_digest,),
            ).fetchone(),
            "operation": connection.execute(
                "SELECT operation_json AS payload, constitution_revision, goal_revision, "
                "operating_profile_revision, active_intent_digest FROM operation_records "
                "WHERE operation_id = ?",
                (reservation.operation_id,),
            ).fetchone(),
            "event_reserved": connection.execute(
                "SELECT payload_json AS payload, constitution_revision, goal_revision, "
                "operating_profile_revision, active_intent_digest FROM operation_events "
                "WHERE operation_id = ? AND event_type = 'RESERVED'",
                (reservation.operation_id,),
            ).fetchone(),
            "event_concluded": connection.execute(
                "SELECT payload_json AS payload, constitution_revision, goal_revision, "
                "operating_profile_revision, active_intent_digest FROM operation_events "
                "WHERE operation_id = ? AND event_type = 'CONCLUDED'",
                (conclusion.operation_id,),
            ).fetchone(),
        }

    assert conclusion.operation_id == reservation.operation_id
    expected = {
        "constitution_revision": 1,
        "goal_revision": 2,
        "operating_profile_revision": 1,
        "active_intent_digest": revision.active_intent_digest,
    }
    for row in rows.values():
        assert row is not None
        assert json.loads(row["payload"])["intent_binding"] == expected
        assert {
            key: row[key]
            for key in (
                "constitution_revision",
                "goal_revision",
                "operating_profile_revision",
                "active_intent_digest",
            )
        } == expected


@pytest.mark.parametrize(
    ("artifact", "table", "payload_column", "trigger"),
    [
        ("action", "actions", "action_json", "actions_no_update"),
        (
            "envelope",
            "action_envelopes",
            "envelope_json",
            "action_envelopes_no_update",
        ),
        (
            "compatibility",
            "compatibility_decisions",
            "decision_json",
            "compatibility_decisions_no_update",
        ),
        (
            "operation",
            "operation_records",
            "operation_json",
            "operation_records_no_update",
        ),
        (
            "event",
            "operation_events",
            "payload_json",
            "operation_events_no_update",
        ),
    ],
)
def test_advance_verifies_every_authoritative_work_artifact_digest_when_read(
    kernel: WorkflowKernel,
    database_path: Path,
    artifact: str,
    table: str,
    payload_column: str,
    trigger: str,
) -> None:
    kernel.record(decision())
    envelope = kernel.advance("project-1")
    if artifact == "compatibility":
        kernel.record(goal_revision({envelope.action_envelope_digest: "compatible"}))
    elif artifact in {"operation", "event"}:
        kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(
            f"UPDATE {table} SET {payload_column} = '{{}}'"  # noqa: S608
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    ("artifact", "table", "payload_column", "digest_column", "trigger"),
    [
        ("action", "actions", "action_json", "action_digest", "actions_no_update"),
        (
            "envelope",
            "action_envelopes",
            "envelope_json",
            "action_envelope_digest",
            "action_envelopes_no_update",
        ),
        (
            "operation",
            "operation_records",
            "operation_json",
            "operation_digest",
            "operation_records_no_update",
        ),
        (
            "event",
            "operation_events",
            "payload_json",
            "payload_digest",
            "operation_events_no_update",
        ),
    ],
)
@pytest.mark.parametrize("corruption", ["indexed_binding", "json_binding", "json_schema"])
def test_advance_verifies_complete_artifact_json_and_indexed_intent_binding(
    kernel: WorkflowKernel,
    database_path: Path,
    artifact: str,
    table: str,
    payload_column: str,
    digest_column: str,
    trigger: str,
    corruption: str,
) -> None:
    kernel.record(decision())
    kernel.advance("project-1")
    if artifact in {"operation", "event"}:
        kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        if corruption == "indexed_binding":
            connection.execute(
                f"UPDATE {table} SET active_intent_digest = ?",  # noqa: S608
                ("0" * 64,),
            )
        else:
            payload_json = connection.execute(
                f"SELECT {payload_column} FROM {table}"  # noqa: S608
            ).fetchone()[0]
            payload = json.loads(payload_json)
            if corruption == "json_binding":
                payload["intent_binding"]["goal_revision"] += 1
            else:
                payload["unexpected"] = True
            replacement = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            replacement_digest = hashlib.sha256(replacement.encode()).hexdigest()
            connection.execute(
                f"UPDATE {table} SET {payload_column} = ?, {digest_column} = ?",  # noqa: S608
                (replacement, replacement_digest),
            )

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    (
        "artifact",
        "table",
        "payload_column",
        "digest_column",
        "trigger",
        "embedded_field",
    ),
    [
        (
            "action",
            "actions",
            "action_json",
            "action_digest",
            "actions_no_update",
            "action_id",
        ),
        (
            "envelope",
            "action_envelopes",
            "envelope_json",
            "action_envelope_digest",
            "action_envelopes_no_update",
            "action_envelope_id",
        ),
        (
            "envelope",
            "action_envelopes",
            "envelope_json",
            "action_envelope_digest",
            "action_envelopes_no_update",
            "action_digest",
        ),
        (
            "compatibility",
            "compatibility_decisions",
            "decision_json",
            "decision_digest",
            "compatibility_decisions_no_update",
            "source_action_envelope_digest",
        ),
        (
            "operation",
            "operation_records",
            "operation_json",
            "operation_digest",
            "operation_records_no_update",
            "operation_id",
        ),
        (
            "operation",
            "operation_records",
            "operation_json",
            "operation_digest",
            "operation_records_no_update",
            "action_envelope_digest",
        ),
        (
            "event",
            "operation_events",
            "payload_json",
            "payload_digest",
            "operation_events_no_update",
            "operation_digest",
        ),
    ],
    ids=[
        "action-id",
        "envelope-id",
        "envelope-action-link",
        "compatibility-envelope-link",
        "operation-id",
        "operation-envelope-link",
        "event-operation-link",
    ],
)
def test_advance_rejects_tampered_embedded_artifact_ids_and_links(
    kernel: WorkflowKernel,
    database_path: Path,
    artifact: str,
    table: str,
    payload_column: str,
    digest_column: str,
    trigger: str,
    embedded_field: str,
) -> None:
    kernel.record(decision())
    envelope = kernel.advance("project-1")
    if artifact == "compatibility":
        kernel.record(goal_revision({envelope.action_envelope_digest: "compatible"}))
    elif artifact in {"operation", "event"}:
        kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        payload_json = connection.execute(
            f"SELECT {payload_column} FROM {table}"  # noqa: S608
        ).fetchone()[0]
        payload = json.loads(payload_json)
        payload[embedded_field] = "0" * 64 if embedded_field.endswith("digest") else "tampered-id"
        replacement = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        replacement_digest = hashlib.sha256(replacement.encode()).hexdigest()
        connection.execute(
            f"UPDATE {table} SET {payload_column} = ?, {digest_column} = ?",  # noqa: S608
            (replacement, replacement_digest),
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    "fault_point", ["revision_after_history_append", "revision_after_pointer_swap"]
)
def test_revision_os_exit_crash_probe_preserves_atomic_history_and_retry(
    kernel: WorkflowKernel, database_path: Path, fault_point: str
) -> None:
    original = kernel.record(decision())

    crashed = run_crash_probe(database_path, fault_point, goal_revision({}))

    assert crashed.returncode == 91, crashed.stderr
    assert kernel.view("project-1").current_goal == decision().complete_revision_payload["goal"]
    with sqlite3.connect(database_path) as connection:
        counts = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM goal_revisions), "
            "(SELECT COUNT(*) FROM active_intents), "
            "(SELECT intent_number FROM active_intent_current)"
        ).fetchone()
    assert counts == (1, 1, 1)

    revised = kernel.record(goal_revision({}))

    assert revised.outcome == "GOAL_REVISED"
    assert revised.active_intent_digest != original.active_intent_digest
    assert kernel.view("project-1").current_goal == revised_goal()


@pytest.mark.parametrize(
    "fault_point", ["operation_after_record_append", "operation_after_event_append"]
)
def test_operation_os_exit_crash_probe_preserves_atomic_reservation_and_retry(
    kernel: WorkflowKernel, database_path: Path, fault_point: str
) -> None:
    kernel.record(decision())
    envelope = kernel.advance("project-1")

    crashed = run_crash_probe(database_path, fault_point)

    assert crashed.returncode == 91, crashed.stderr
    with sqlite3.connect(database_path) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM operation_records), "
            "(SELECT COUNT(*) FROM operation_events)"
        ).fetchone()
    assert counts == (0, 0)

    reservation = kernel.advance("project-1")

    assert reservation.outcome == "OPERATION_RESERVED"
    assert reservation.action_envelope_id == envelope.action_envelope_id


@pytest.mark.parametrize(
    ("transition", "fault_point"),
    [
        ("reservation", "operation_before_record_append"),
        ("conclusion", "operation_before_conclusion_event_append"),
    ],
)
def test_operation_transition_wins_lock_controlled_race_with_revision(
    kernel: WorkflowKernel,
    transition: str,
    fault_point: str,
) -> None:
    import agentic_workflow.kernel as kernel_module
    import agentic_workflow.store as store_module

    kernel.record(decision())
    kernel.advance("project-1")
    if transition == "conclusion":
        kernel.advance("project-1")
    entered = Event()
    release = Event()
    competing_begin_attempted = Event()

    def hold_transition(point: str) -> None:
        if point == fault_point:
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release operation transition")

    kernel_module._PRIVATE_FAULT_HOOK = hold_transition
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            transition_future = pool.submit(kernel.advance, "project-1")
            try:
                assert entered.wait(timeout=10)
                store_module._PRIVATE_TRANSACTION_BEGIN_HOOK = competing_begin_attempted.set
                revision_future = pool.submit(kernel.record, goal_revision({}))
                assert competing_begin_attempted.wait(timeout=10)
                assert not revision_future.done()
            finally:
                release.set()
            transitioned = transition_future.result(timeout=10)
            revised = revision_future.result(timeout=10)
    finally:
        kernel_module._PRIVATE_FAULT_HOOK = None
        store_module._PRIVATE_TRANSACTION_BEGIN_HOOK = None

    expected_outcome = {
        "reservation": "OPERATION_RESERVED",
        "conclusion": "OPERATION_CONCLUDED",
    }[transition]
    assert transitioned.outcome == expected_outcome
    fresh = kernel.advance("project-1")
    assert fresh.outcome == "ACTION_ENVELOPED"
    assert fresh.intent_binding.active_intent_digest == revised.active_intent_digest


@pytest.mark.parametrize("stale_state", ["envelope", "reserved_operation"])
def test_revision_wins_lock_controlled_race_and_stale_transition_is_not_run(
    kernel: WorkflowKernel,
    database_path: Path,
    stale_state: str,
) -> None:
    import agentic_workflow.kernel as kernel_module
    import agentic_workflow.store as store_module

    kernel.record(decision())
    stale = kernel.advance("project-1")
    reservation = None
    if stale_state == "reserved_operation":
        reservation = kernel.advance("project-1")
    entered = Event()
    release = Event()
    competing_begin_attempted = Event()

    def hold_revision(point: str) -> None:
        if point == "revision_after_history_append":
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release revision")

    kernel_module._PRIVATE_FAULT_HOOK = hold_revision
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            revision_future = pool.submit(kernel.record, goal_revision({}))
            try:
                assert entered.wait(timeout=10)
                store_module._PRIVATE_TRANSACTION_BEGIN_HOOK = competing_begin_attempted.set
                advance_future = pool.submit(kernel.advance, "project-1")
                assert competing_begin_attempted.wait(timeout=10)
                assert not advance_future.done()
            finally:
                release.set()
            revised = revision_future.result(timeout=10)
            advanced = advance_future.result(timeout=10)
    finally:
        kernel_module._PRIVATE_FAULT_HOOK = None
        store_module._PRIVATE_TRANSACTION_BEGIN_HOOK = None

    assert advanced.outcome == "ACTION_ENVELOPED"
    assert advanced.action_envelope_id != stale.action_envelope_id
    assert advanced.intent_binding.active_intent_digest == revised.active_intent_digest
    if reservation is not None:
        with sqlite3.connect(database_path) as connection:
            concluded = connection.execute(
                "SELECT 1 FROM operation_events WHERE operation_id = ? "
                "AND event_type = 'CONCLUDED'",
                (reservation.operation_id,),
            ).fetchone()
        assert concluded is None


def test_compatibility_read_verifies_embedded_fields_against_indexed_fields(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(decision())
    stale = kernel.advance("project-1")
    kernel.record(goal_revision({stale.action_envelope_digest: "compatible"}))
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER compatibility_decisions_no_update")
        connection.execute("UPDATE compatibility_decisions SET verdict = 'incompatible'")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"
