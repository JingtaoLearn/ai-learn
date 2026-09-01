"""The public workflow kernel."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .model import (
    AdvanceResult,
    IntentBinding,
    ProjectView,
    RecordReceipt,
    UserDecision,
    WorkflowError,
)
from .store import ControlStore

_PRIVATE_FAULT_HOOK: Callable[[str], None] | None = None


def _private_fault(point: str) -> None:
    if _PRIVATE_FAULT_HOOK is not None:
        _PRIVATE_FAULT_HOOK(point)


class DecisionAuthenticator(Protocol):
    def authenticate(self, decision: UserDecision) -> bool: ...


class Clock(Protocol):
    def now(self) -> str: ...


class SystemClock:
    def now(self) -> str:
        return datetime.now(UTC).isoformat()


class RejectingAuthenticator:
    def authenticate(self, decision: UserDecision) -> bool:
        return False


class WorkflowKernel:
    """Deep public seam for durable workflow state transitions and projections."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        decision_authenticator: DecisionAuthenticator | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = ControlStore(database_path)
        self._authenticator = decision_authenticator or RejectingAuthenticator()
        self._clock = clock or SystemClock()

    def record(self, event: UserDecision) -> RecordReceipt:
        if not isinstance(event, UserDecision):
            raise WorkflowError("INVALID_EVENT", "record accepts only a UserDecision")
        if event.decision_kind != "BOOTSTRAP_PROJECT":
            return self._record_revision(event)
        _validate_json_value(event.provenance)
        _validate_json_value(event.complete_revision_payload)
        event_json = _canonical_json(asdict(event))
        authenticated_event = _decision_from_canonical_json(event_json)
        self._validate_bootstrap_decision(authenticated_event)
        if not self._authenticator.authenticate(authenticated_event):
            raise WorkflowError("UNAUTHENTICATED_DECISION", "decision authentication failed")
        if (
            _canonical_json(asdict(event)) != event_json
            or _canonical_json(asdict(authenticated_event)) != event_json
        ):
            raise WorkflowError("INVALID_EVENT", "decision mutated during authentication")
        event = _decision_from_canonical_json(event_json)
        self._validate_bootstrap_decision(event)
        payload = self._validate_bootstrap_payload(event.complete_revision_payload)
        event_digest = _digest(event_json)
        project_json = _canonical_json(payload["project"])
        constitution_json = _canonical_json(payload["constitution"])
        goal_json = _canonical_json(payload["goal"])
        profile_json = _canonical_json(payload["operating_profile"])
        constitution_digest = _digest(constitution_json)
        goal_digest = _digest(goal_json)
        profile_digest = _digest(profile_json)
        active_intent_digest = _digest(
            _canonical_json(
                {
                    "constitution_revision": 1,
                    "constitution_digest": constitution_digest,
                    "goal_revision": 1,
                    "goal_digest": goal_digest,
                    "operating_profile_revision": 1,
                    "operating_profile_digest": profile_digest,
                }
            )
        )
        recorded_at = self._clock.now()
        project_name = payload["project"]["name"]
        project_digest = _project_digest(event.project_id, project_name, project_json, recorded_at)
        receipt = RecordReceipt(
            receipt_id=_decision_receipt_id(event_digest),
            project_id=event.project_id,
            event_type="USER_DECISION",
            outcome="PROJECT_BOOTSTRAPPED",
            event_digest=event_digest,
            active_intent_digest=active_intent_digest,
            recorded_at=recorded_at,
        )
        receipt_json = _canonical_json(asdict(receipt))
        receipt_digest = _digest(receipt_json)
        projection_json = _canonical_json({"status": "INITIAL", "material_changes": []})
        projection_digest = _digest(projection_json)

        try:
            with self._store.writer() as connection:
                existing_rows = connection.execute(
                    "SELECT e.rowid AS event_rowid, e.project_id AS event_project_id, "
                    "e.event_type AS indexed_event_type, e.event_digest, e.event_json, "
                    "e.receipt_json, e.receipt_digest, e.recorded_at AS event_recorded_at, "
                    "n.project_id AS nonce_project_id, n.actor_id AS nonce_actor_id, "
                    "n.nonce AS nonce_value, n.replay_identity AS nonce_replay_identity, "
                    "n.source AS nonce_source, n.source_event_id AS nonce_source_event_id "
                    "FROM inbox_events AS e "
                    "LEFT JOIN decision_nonces AS n "
                    "ON n.project_id = e.project_id AND n.source = e.source "
                    "AND n.source_event_id = e.source_event_id "
                    "WHERE (e.project_id = ? AND e.source = ? AND e.source_event_id = ?) "
                    "OR (n.project_id = ? AND n.actor_id = ? AND n.nonce = ?) "
                    "OR (n.project_id = ? AND n.actor_id = ? AND n.replay_identity = ?)",
                    (
                        event.project_id,
                        event.source,
                        event.source_event_id,
                        event.project_id,
                        event.authenticated_actor,
                        event.nonce,
                        event.project_id,
                        event.authenticated_actor,
                        event.replay_identity,
                    ),
                ).fetchall()
                if existing_rows:
                    if any(
                        _digest(row["event_json"]) != row["event_digest"] for row in existing_rows
                    ):
                        raise WorkflowError("LEDGER_INTEGRITY", "record event digest mismatch")
                    for row in existing_rows:
                        _verify_decision_nonce_identity(row)
                    if all(row["event_digest"] == event_digest for row in existing_rows):
                        return _verified_receipt(
                            existing_rows[0],
                            expected_outcome="PROJECT_BOOTSTRAPPED",
                            expected_active_intent_digest=active_intent_digest,
                        )
                    raise WorkflowError(
                        "IDENTITY_CONFLICT",
                        "source-event or nonce identity was reused with different content",
                    )
                project_exists = connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (event.project_id,)
                ).fetchone()
                if project_exists:
                    raise WorkflowError("PROJECT_EXISTS", "workflow project already exists")
                connection.execute(
                    "INSERT INTO projects "
                    "(project_id, name, project_json, project_digest, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        event.project_id,
                        project_name,
                        project_json,
                        project_digest,
                        recorded_at,
                    ),
                )
                revisions = (
                    ("constitution_revisions", constitution_json, constitution_digest),
                    ("goal_revisions", goal_json, goal_digest),
                    ("operating_profile_revisions", profile_json, profile_digest),
                )
                for table, payload_json, payload_digest in revisions:
                    connection.execute(
                        f"INSERT INTO {table} "  # noqa: S608 - table comes from fixed literals
                        "(project_id, revision_number, payload_json, payload_digest) "
                        "VALUES (?, 1, ?, ?)",
                        (event.project_id, payload_json, payload_digest),
                    )
                connection.execute(
                    "INSERT INTO active_intents "
                    "(project_id, intent_number, constitution_revision, goal_revision, "
                    "operating_profile_revision, active_intent_digest, activated_at) "
                    "VALUES (?, 1, 1, 1, 1, ?, ?)",
                    (event.project_id, active_intent_digest, recorded_at),
                )
                connection.execute(
                    "INSERT INTO active_intent_current (project_id, intent_number) VALUES (?, 1)",
                    (event.project_id,),
                )
                connection.execute(
                    "INSERT INTO inbox_events "
                    "(project_id, source, source_event_id, event_type, event_digest, event_json, "
                    "receipt_json, receipt_digest, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.project_id,
                        event.source,
                        event.source_event_id,
                        "USER_DECISION",
                        event_digest,
                        event_json,
                        receipt_json,
                        receipt_digest,
                        recorded_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO decision_nonces "
                    "(project_id, actor_id, nonce, replay_identity, source, source_event_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.project_id,
                        event.authenticated_actor,
                        event.nonce,
                        event.replay_identity,
                        event.source,
                        event.source_event_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO daily_briefs "
                    "(project_id, brief_number, projection_json, projection_digest, projected_at) "
                    "VALUES (?, 1, ?, ?, ?)",
                    (event.project_id, projection_json, projection_digest, recorded_at),
                )
        except sqlite3.IntegrityError as error:
            raise WorkflowError("LEDGER_ERROR", "bootstrap transaction failed") from error
        except sqlite3.DatabaseError as error:
            raise WorkflowError("LEDGER_ERROR", "bootstrap transaction failed") from error
        return receipt

    def _record_revision(self, event: UserDecision) -> RecordReceipt:
        _validate_json_value(event.provenance)
        _validate_json_value(event.complete_revision_payload)
        event_json = _canonical_json(asdict(event))
        authenticated_event = _decision_from_canonical_json(event_json)
        self._validate_revision_decision(authenticated_event)
        if not self._authenticator.authenticate(authenticated_event):
            raise WorkflowError("UNAUTHENTICATED_DECISION", "decision authentication failed")
        if (
            _canonical_json(asdict(event)) != event_json
            or _canonical_json(asdict(authenticated_event)) != event_json
        ):
            raise WorkflowError("INVALID_EVENT", "decision mutated during authentication")
        event = _decision_from_canonical_json(event_json)
        self._validate_revision_decision(event)
        revision_kind, revision_payload, compatibility = self._validate_revision_payload(event)
        event_digest = _digest(event_json)
        recorded_at = self._clock.now()
        receipt_id = _decision_receipt_id(event_digest)

        try:
            with self._store.writer() as connection:
                existing_rows = _matching_decision_rows(connection, event)
                if existing_rows:
                    return _resolve_decision_replay(
                        connection, existing_rows, event_digest, event.decision_kind
                    )
                current = _current_intent_row(connection, event.project_id)
                if current is None:
                    raise WorkflowError("PROJECT_NOT_FOUND", "workflow project does not exist")
                _verify_current_intent(current)

                if revision_kind == "goal":
                    self._validate_goal_payload(revision_payload)
                    table = "goal_revisions"
                    next_revision = current["goal_revision"] + 1
                    constitution_revision = current["constitution_revision"]
                    goal_revision = next_revision
                    profile_revision = current["operating_profile_revision"]
                    constitution_digest = current["constitution_digest"]
                    goal_digest = _digest(_canonical_json(revision_payload))
                    profile_digest = current["profile_digest"]
                    outcome = "GOAL_REVISED"
                else:
                    self._validate_bootstrap_payload(
                        {
                            "project": {"name": current["project_name"]},
                            "constitution": json.loads(current["constitution_json"]),
                            "goal": json.loads(current["goal_json"]),
                            "operating_profile": revision_payload,
                        }
                    )
                    table = "operating_profile_revisions"
                    next_revision = current["operating_profile_revision"] + 1
                    constitution_revision = current["constitution_revision"]
                    goal_revision = current["goal_revision"]
                    profile_revision = next_revision
                    constitution_digest = current["constitution_digest"]
                    goal_digest = current["goal_digest"]
                    profile_digest = _digest(_canonical_json(revision_payload))
                    outcome = "OPERATING_PROFILE_REVISED"

                payload_json = _canonical_json(revision_payload)
                payload_digest = _digest(payload_json)
                connection.execute(
                    f"INSERT INTO {table} "  # noqa: S608 - table comes from fixed literals
                    "(project_id, revision_number, payload_json, payload_digest) "
                    "VALUES (?, ?, ?, ?)",
                    (event.project_id, next_revision, payload_json, payload_digest),
                )
                intent_number = current["intent_number"] + 1
                active_intent_digest = _active_intent_digest(
                    constitution_revision,
                    constitution_digest,
                    goal_revision,
                    goal_digest,
                    profile_revision,
                    profile_digest,
                )
                connection.execute(
                    "INSERT INTO active_intents "
                    "(project_id, intent_number, constitution_revision, goal_revision, "
                    "operating_profile_revision, active_intent_digest, activated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.project_id,
                        intent_number,
                        constitution_revision,
                        goal_revision,
                        profile_revision,
                        active_intent_digest,
                        recorded_at,
                    ),
                )
                new_binding = IntentBinding(
                    constitution_revision=constitution_revision,
                    goal_revision=goal_revision,
                    operating_profile_revision=profile_revision,
                    active_intent_digest=active_intent_digest,
                )
                for source_envelope_digest, verdict in sorted(compatibility.items()):
                    source = connection.execute(
                        _ENVELOPE_SELECT + "WHERE e.action_envelope_digest = ?",
                        (source_envelope_digest,),
                    ).fetchone()
                    source_binding = _verify_action_envelope(source) if source is not None else None
                    if (
                        source is None
                        or source["project_id"] != event.project_id
                        or source_binding != _binding_from_row(current)
                    ):
                        raise WorkflowError(
                            "INVALID_REVISION",
                            "compatibility source Action Envelope is not from the preceding intent",
                        )
                    decision_json = _canonical_json(
                        {
                            "intent_binding": asdict(new_binding),
                            "source_action_envelope_digest": source_envelope_digest,
                            "verdict": verdict,
                        }
                    )
                    connection.execute(
                        "INSERT INTO compatibility_decisions "
                        "(project_id, active_intent_digest, source_action_envelope_digest, "
                        "verdict, decision_json, decision_digest, constitution_revision, "
                        "goal_revision, operating_profile_revision, recorded_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            event.project_id,
                            active_intent_digest,
                            source_envelope_digest,
                            verdict,
                            decision_json,
                            _digest(decision_json),
                            constitution_revision,
                            goal_revision,
                            profile_revision,
                            recorded_at,
                        ),
                    )
                _private_fault("revision_after_history_append")
                connection.execute(
                    "UPDATE active_intent_current SET intent_number = ? WHERE project_id = ?",
                    (intent_number, event.project_id),
                )
                _private_fault("revision_after_pointer_swap")
                receipt = RecordReceipt(
                    receipt_id=receipt_id,
                    project_id=event.project_id,
                    event_type="USER_DECISION",
                    outcome=outcome,
                    event_digest=event_digest,
                    active_intent_digest=active_intent_digest,
                    recorded_at=recorded_at,
                )
                receipt_json = _canonical_json(asdict(receipt))
                connection.execute(
                    "INSERT INTO inbox_events "
                    "(project_id, source, source_event_id, event_type, event_digest, event_json, "
                    "receipt_json, receipt_digest, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.project_id,
                        event.source,
                        event.source_event_id,
                        "USER_DECISION",
                        event_digest,
                        event_json,
                        receipt_json,
                        _digest(receipt_json),
                        recorded_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO decision_nonces "
                    "(project_id, actor_id, nonce, replay_identity, source, source_event_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.project_id,
                        event.authenticated_actor,
                        event.nonce,
                        event.replay_identity,
                        event.source,
                        event.source_event_id,
                    ),
                )
        except WorkflowError:
            raise
        except sqlite3.DatabaseError as error:
            raise WorkflowError("LEDGER_ERROR", "revision transaction failed") from error
        return receipt

    def advance(self, project_id: str) -> AdvanceResult:
        """Run one bounded lifecycle transition for the Workflow Project."""
        recorded_at = self._clock.now()
        action_id = str(uuid.uuid4())
        action_envelope_id = str(uuid.uuid4())
        try:
            with self._store.writer() as connection:
                current = _current_intent_row(connection, project_id)
                if current is None:
                    raise WorkflowError("PROJECT_NOT_FOUND", "workflow project does not exist")
                _verify_current_intent(current)
                existing = _latest_live_envelope(connection, current)
                if existing is not None:
                    return self._advance_existing(connection, current, existing, recorded_at)
                compatible = _compatible_source_envelope(connection, current)
                if compatible is not None:
                    return self._reenvelope_compatible(connection, current, compatible, recorded_at)
                binding = _binding_from_row(current)
                goal = json.loads(current["goal_json"])
                action_json = _canonical_json(
                    {
                        "action_id": action_id,
                        "action_kind": "GOAL_WORK",
                        "intent_binding": asdict(binding),
                        "objective": goal["outcome"],
                    }
                )
                action_digest = _digest(action_json)
                connection.execute(
                    "INSERT INTO actions "
                    "(action_id, project_id, action_kind, action_json, action_digest, "
                    "constitution_revision, goal_revision, operating_profile_revision, "
                    "active_intent_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        action_id,
                        project_id,
                        "GOAL_WORK",
                        action_json,
                        action_digest,
                        binding.constitution_revision,
                        binding.goal_revision,
                        binding.operating_profile_revision,
                        binding.active_intent_digest,
                        recorded_at,
                    ),
                )
                envelope_json = _canonical_json(
                    {
                        "acceptance": goal["success_evidence"],
                        "action_digest": action_digest,
                        "action_envelope_id": action_envelope_id,
                        "constraints": goal["constraints"],
                        "intent_binding": asdict(binding),
                        "predecessor_action_envelope_id": None,
                        "stop_conditions": ["ACTIVE_INTENT_CHANGED"],
                    }
                )
                envelope_digest = _digest(envelope_json)
                connection.execute(
                    "INSERT INTO action_envelopes "
                    "(action_envelope_id, project_id, action_id, "
                    "predecessor_action_envelope_id, envelope_json, action_envelope_digest, "
                    "constitution_revision, goal_revision, operating_profile_revision, "
                    "active_intent_digest, created_at) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        action_envelope_id,
                        project_id,
                        action_id,
                        envelope_json,
                        envelope_digest,
                        binding.constitution_revision,
                        binding.goal_revision,
                        binding.operating_profile_revision,
                        binding.active_intent_digest,
                        recorded_at,
                    ),
                )
        except WorkflowError:
            raise
        except sqlite3.DatabaseError as error:
            raise WorkflowError("LEDGER_ERROR", "advance transaction failed") from error
        return AdvanceResult(
            project_id=project_id,
            outcome="ACTION_ENVELOPED",
            intent_binding=binding,
            action_id=action_id,
            action_envelope_id=action_envelope_id,
            action_envelope_digest=envelope_digest,
            predecessor_action_envelope_id=None,
            operation_id=None,
            operation_digest=None,
        )

    def _reenvelope_compatible(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        source: Mapping[str, Any],
        recorded_at: str,
    ) -> AdvanceResult:
        _verify_action_envelope(source)
        compatibility_payload = _verify_canonical_artifact(
            source["decision_json"], source["decision_digest"], "compatibility decision"
        )
        compatibility_binding = IntentBinding(
            constitution_revision=source["decision_constitution_revision"],
            goal_revision=source["decision_goal_revision"],
            operating_profile_revision=source["decision_profile_revision"],
            active_intent_digest=source["decision_active_intent_digest"],
        )
        if compatibility_binding != _binding_from_row(current):
            raise WorkflowError("LEDGER_INTEGRITY", "compatibility binding mismatch")
        if compatibility_payload != {
            "intent_binding": asdict(compatibility_binding),
            "source_action_envelope_digest": source["action_envelope_digest"],
            "verdict": "compatible",
        }:
            raise WorkflowError("LEDGER_INTEGRITY", "compatibility decision fields mismatch")

        binding = _binding_from_row(current)
        action_id = str(uuid.uuid4())
        action_envelope_id = str(uuid.uuid4())
        goal = json.loads(current["goal_json"])
        action_json = _canonical_json(
            {
                "action_id": action_id,
                "action_kind": "GOAL_WORK",
                "intent_binding": asdict(binding),
                "objective": goal["outcome"],
            }
        )
        action_digest = _digest(action_json)
        connection.execute(
            "INSERT INTO actions "
            "(action_id, project_id, action_kind, action_json, action_digest, "
            "constitution_revision, goal_revision, operating_profile_revision, "
            "active_intent_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                current["project_id"],
                "GOAL_WORK",
                action_json,
                action_digest,
                binding.constitution_revision,
                binding.goal_revision,
                binding.operating_profile_revision,
                binding.active_intent_digest,
                recorded_at,
            ),
        )
        envelope_json = _canonical_json(
            {
                "acceptance": goal["success_evidence"],
                "action_digest": action_digest,
                "action_envelope_id": action_envelope_id,
                "constraints": goal["constraints"],
                "intent_binding": asdict(binding),
                "predecessor_action_envelope_id": source["action_envelope_id"],
                "stop_conditions": ["ACTIVE_INTENT_CHANGED"],
            }
        )
        envelope_digest = _digest(envelope_json)
        connection.execute(
            "INSERT INTO action_envelopes "
            "(action_envelope_id, project_id, action_id, predecessor_action_envelope_id, "
            "envelope_json, action_envelope_digest, constitution_revision, goal_revision, "
            "operating_profile_revision, active_intent_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_envelope_id,
                current["project_id"],
                action_id,
                source["action_envelope_id"],
                envelope_json,
                envelope_digest,
                binding.constitution_revision,
                binding.goal_revision,
                binding.operating_profile_revision,
                binding.active_intent_digest,
                recorded_at,
            ),
        )
        return AdvanceResult(
            project_id=current["project_id"],
            outcome="WORK_REENVELOPED",
            intent_binding=binding,
            action_id=action_id,
            action_envelope_id=action_envelope_id,
            action_envelope_digest=envelope_digest,
            predecessor_action_envelope_id=source["action_envelope_id"],
            operation_id=None,
            operation_digest=None,
        )

    def _advance_existing(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        envelope: sqlite3.Row,
        recorded_at: str,
    ) -> AdvanceResult:
        envelope_binding = _verify_action_envelope(envelope)
        if envelope_binding != _binding_from_row(current):
            raise WorkflowError("LEDGER_INTEGRITY", "live Action Envelope binding is not current")
        if envelope["operation_id"] is not None:
            return self._conclude_existing(connection, current, envelope, recorded_at)

        operation_id = str(uuid.uuid4())
        operation_json = _canonical_json(
            {
                "action_envelope_digest": envelope["action_envelope_digest"],
                "effect_kind": "BOUNDED_WORK",
                "intent_binding": asdict(envelope_binding),
                "operation_id": operation_id,
            }
        )
        operation_digest = _digest(operation_json)
        _private_fault("operation_before_record_append")
        connection.execute(
            "INSERT INTO operation_records "
            "(operation_id, project_id, action_envelope_id, operation_json, operation_digest, "
            "constitution_revision, goal_revision, "
            "operating_profile_revision, active_intent_digest, reserved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                current["project_id"],
                envelope["action_envelope_id"],
                operation_json,
                operation_digest,
                envelope_binding.constitution_revision,
                envelope_binding.goal_revision,
                envelope_binding.operating_profile_revision,
                envelope_binding.active_intent_digest,
                recorded_at,
            ),
        )
        _private_fault("operation_after_record_append")
        event_json = _canonical_json(
            {
                "event_type": "RESERVED",
                "intent_binding": asdict(envelope_binding),
                "operation_digest": operation_digest,
            }
        )
        connection.execute(
            "INSERT INTO operation_events "
            "(operation_id, event_number, event_type, payload_json, payload_digest, "
            "constitution_revision, goal_revision, operating_profile_revision, "
            "active_intent_digest, recorded_at) VALUES (?, 1, 'RESERVED', ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                event_json,
                _digest(event_json),
                envelope_binding.constitution_revision,
                envelope_binding.goal_revision,
                envelope_binding.operating_profile_revision,
                envelope_binding.active_intent_digest,
                recorded_at,
            ),
        )
        _private_fault("operation_after_event_append")
        return AdvanceResult(
            project_id=current["project_id"],
            outcome="OPERATION_RESERVED",
            intent_binding=envelope_binding,
            action_id=envelope["action_id"],
            action_envelope_id=envelope["action_envelope_id"],
            action_envelope_digest=envelope["action_envelope_digest"],
            predecessor_action_envelope_id=envelope["predecessor_action_envelope_id"],
            operation_id=operation_id,
            operation_digest=operation_digest,
        )

    def _conclude_existing(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        envelope: sqlite3.Row,
        recorded_at: str,
    ) -> AdvanceResult:
        operation_binding = _verify_operation(envelope)
        envelope_binding = _binding_from_artifact_row(envelope)
        if operation_binding != envelope_binding or operation_binding != _binding_from_row(current):
            raise WorkflowError("LEDGER_INTEGRITY", "Operation binding is not current")
        event_types = _verify_operation_events(connection, envelope, operation_binding)
        if event_types == ("RESERVED", "CONCLUDED"):
            raise WorkflowError("NO_ACTION", "Operation is already concluded")
        if event_types != ("RESERVED",):
            raise WorkflowError("LEDGER_INTEGRITY", "Operation reservation is missing")
        event_json = _canonical_json(
            {
                "event_type": "CONCLUDED",
                "intent_binding": asdict(operation_binding),
                "operation_digest": envelope["operation_digest"],
                "result": "NO_EXTERNAL_EFFECT",
            }
        )
        _private_fault("operation_before_conclusion_event_append")
        connection.execute(
            "INSERT INTO operation_events "
            "(operation_id, event_number, event_type, payload_json, payload_digest, "
            "constitution_revision, goal_revision, operating_profile_revision, "
            "active_intent_digest, recorded_at) VALUES (?, 2, 'CONCLUDED', ?, ?, ?, ?, ?, ?, ?)",
            (
                envelope["operation_id"],
                event_json,
                _digest(event_json),
                operation_binding.constitution_revision,
                operation_binding.goal_revision,
                operation_binding.operating_profile_revision,
                operation_binding.active_intent_digest,
                recorded_at,
            ),
        )
        return AdvanceResult(
            project_id=current["project_id"],
            outcome="OPERATION_CONCLUDED",
            intent_binding=operation_binding,
            action_id=envelope["action_id"],
            action_envelope_id=envelope["action_envelope_id"],
            action_envelope_digest=envelope["action_envelope_digest"],
            predecessor_action_envelope_id=envelope["predecessor_action_envelope_id"],
            operation_id=envelope["operation_id"],
            operation_digest=envelope["operation_digest"],
        )

    def view(self, project_id: str) -> ProjectView:
        with self._store.reader() as connection:
            project_row = connection.execute(
                "SELECT project_id, name, project_json, project_digest, created_at "
                "FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            has_project_history = connection.execute(
                "SELECT 1 FROM active_intent_current WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            row = connection.execute(
                "SELECT c.payload_json AS constitution_json, "
                "c.payload_digest AS constitution_digest, "
                "g.payload_json AS goal_json, g.payload_digest AS goal_digest, "
                "o.payload_json AS profile_json, o.payload_digest AS profile_digest, "
                "current.intent_number, "
                "(SELECT MAX(history.intent_number) FROM active_intents AS history "
                "WHERE history.project_id = current.project_id) AS latest_intent_number, "
                "i.constitution_revision, i.goal_revision, i.operating_profile_revision, "
                "i.active_intent_digest, d.projection_json, d.projection_digest "
                "FROM active_intent_current AS current "
                "JOIN active_intents AS i ON i.project_id = current.project_id "
                "AND i.intent_number = current.intent_number "
                "JOIN constitution_revisions AS c ON c.project_id = i.project_id "
                "AND c.revision_number = i.constitution_revision "
                "JOIN goal_revisions AS g ON g.project_id = i.project_id "
                "AND g.revision_number = i.goal_revision "
                "JOIN operating_profile_revisions AS o ON o.project_id = i.project_id "
                "AND o.revision_number = i.operating_profile_revision "
                "JOIN daily_briefs AS d ON d.project_id = i.project_id "
                "WHERE current.project_id = ? "
                "ORDER BY d.brief_number DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if project_row is None:
            if has_project_history is not None:
                raise WorkflowError("LEDGER_INTEGRITY", "authoritative project row is missing")
            raise WorkflowError("PROJECT_NOT_FOUND", "workflow project does not exist")
        _verify_project_row(project_row)
        if row is None:
            raise WorkflowError("LEDGER_INTEGRITY", "project projection is incomplete")
        _verify_current_intent_position(row)
        try:
            for payload_field, digest_field in (
                ("constitution_json", "constitution_digest"),
                ("goal_json", "goal_digest"),
                ("profile_json", "profile_digest"),
            ):
                if _digest(row[payload_field]) != row[digest_field]:
                    raise WorkflowError("LEDGER_INTEGRITY", "revision payload digest mismatch")
            if _digest(row["projection_json"]) != row["projection_digest"]:
                raise WorkflowError("LEDGER_INTEGRITY", "daily brief projection digest mismatch")
            expected_intent_digest = _digest(
                _canonical_json(
                    {
                        "constitution_revision": row["constitution_revision"],
                        "constitution_digest": row["constitution_digest"],
                        "goal_revision": row["goal_revision"],
                        "goal_digest": row["goal_digest"],
                        "operating_profile_revision": row["operating_profile_revision"],
                        "operating_profile_digest": row["profile_digest"],
                    }
                )
            )
            if expected_intent_digest != row["active_intent_digest"]:
                raise WorkflowError("LEDGER_INTEGRITY", "active intent digest mismatch")
            goal = json.loads(row["goal_json"])
            daily_brief = json.loads(row["projection_json"])
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise WorkflowError(
                "LEDGER_INTEGRITY", "ledger payload is not canonical JSON"
            ) from error
        return ProjectView(
            current_goal=goal,
            daily_brief=daily_brief,
            pending_decisions=(),
        )

    @staticmethod
    def _validate_revision_decision(event: UserDecision) -> None:
        required_strings = (
            event.project_id,
            event.source,
            event.source_event_id,
            event.authenticated_actor,
            event.verbatim_text,
            event.nonce,
            event.replay_identity,
        )
        WorkflowKernel._validate_provenance(event.provenance)
        expected_scope = {
            "REVISE_GOAL": "GOAL",
            "REVISE_OPERATING_PROFILE": "OPERATING_PROFILE",
        }.get(event.decision_kind)
        if expected_scope is None:
            raise WorkflowError("DECISION_NOT_IMPLEMENTED", "decision kind is not implemented")
        if event.scope != expected_scope or any(
            not isinstance(value, str) or not value.strip() for value in required_strings
        ):
            raise WorkflowError(
                "INVALID_DECISION", "revision decision identity or scope is invalid"
            )

    @staticmethod
    def _validate_revision_payload(
        event: UserDecision,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        payload = event.complete_revision_payload
        if not isinstance(payload, Mapping):
            raise WorkflowError("INVALID_REVISION", "revision payload must be an object")
        revision_kind = "goal" if event.decision_kind == "REVISE_GOAL" else "operating_profile"
        if set(payload) != {revision_kind, "compatibility"}:
            raise WorkflowError(
                "INVALID_REVISION", "revision requires one complete payload and compatibility"
            )
        revision_payload = payload[revision_kind]
        compatibility = payload["compatibility"]
        if not isinstance(revision_payload, Mapping) or not revision_payload:
            raise WorkflowError("INVALID_REVISION", "complete revision payload must be non-empty")
        if not isinstance(compatibility, Mapping) or any(
            not _is_sha256(envelope_digest)
            or verdict not in {"compatible", "incompatible", "unknown"}
            for envelope_digest, verdict in compatibility.items()
        ):
            raise WorkflowError("INVALID_REVISION", "compatibility decisions are invalid")
        return revision_kind, dict(revision_payload), dict(compatibility)

    @staticmethod
    def _validate_goal_payload(goal: Mapping[str, Any]) -> None:
        expected_fields = {
            "outcome",
            "scope",
            "success_evidence",
            "constraints",
            "accepted_tradeoffs",
            "non_goals",
        }
        if set(goal) != expected_fields:
            raise WorkflowError("INVALID_REVISION", "goal revision is incomplete")
        for field in ("outcome", "scope"):
            if not isinstance(goal.get(field), str) or not goal[field].strip():
                raise WorkflowError("INVALID_REVISION", f"goal.{field} must be non-empty")
        for field in ("success_evidence", "constraints", "accepted_tradeoffs", "non_goals"):
            value = goal.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise WorkflowError("INVALID_REVISION", f"goal.{field} must be a string list")

    @staticmethod
    def _validate_bootstrap_decision(event: UserDecision) -> None:
        required_strings = (
            event.project_id,
            event.source,
            event.source_event_id,
            event.authenticated_actor,
            event.verbatim_text,
            event.nonce,
            event.replay_identity,
        )
        WorkflowKernel._validate_provenance(event.provenance)
        if event.decision_kind != "BOOTSTRAP_PROJECT":
            raise WorkflowError("DECISION_NOT_IMPLEMENTED", "decision kind is not implemented")
        if event.scope != "PROJECT_INTENT" or any(
            not isinstance(value, str) or not value.strip() for value in required_strings
        ):
            raise WorkflowError(
                "INVALID_DECISION", "bootstrap decision identity or scope is invalid"
            )

    @staticmethod
    def _validate_provenance(provenance: object) -> None:
        if not isinstance(provenance, Mapping):
            raise WorkflowError("INVALID_EVENT", "provenance must be a JSON object")
        _validate_json_value(provenance)

    @staticmethod
    def _validate_bootstrap_payload(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, Mapping):
            raise WorkflowError("INVALID_BOOTSTRAP", "revision payload must be an object")
        required = {"project", "constitution", "goal", "operating_profile"}
        if set(payload) != required:
            raise WorkflowError("INVALID_BOOTSTRAP", "bootstrap requires four complete payloads")
        result: dict[str, dict[str, Any]] = {}
        for field in required:
            value = payload[field]
            if not isinstance(value, Mapping) or not value:
                raise WorkflowError("INVALID_BOOTSTRAP", f"{field} must be a non-empty object")
            result[field] = dict(value)
        if (
            set(result["project"]) != {"name"}
            or not isinstance(result["project"].get("name"), str)
            or not result["project"]["name"]
        ):
            raise WorkflowError("INVALID_BOOTSTRAP", "project.name must be non-empty")
        constitution = result["constitution"]
        if not all(
            constitution.get(field) is True
            for field in ("user_sovereignty", "external_effects_require_authority")
        ) or set(constitution) != {"user_sovereignty", "external_effects_require_authority"}:
            raise WorkflowError("INVALID_BOOTSTRAP", "constitution snapshot is incomplete")
        goal = result["goal"]
        expected_goal_fields = {
            "outcome",
            "scope",
            "success_evidence",
            "constraints",
            "accepted_tradeoffs",
            "non_goals",
        }
        if set(goal) != expected_goal_fields:
            raise WorkflowError("INVALID_BOOTSTRAP", "goal snapshot is incomplete")
        for field in ("outcome", "scope"):
            if not isinstance(goal.get(field), str) or not goal[field].strip():
                raise WorkflowError("INVALID_BOOTSTRAP", f"goal.{field} must be non-empty")
        for field in ("success_evidence", "constraints", "accepted_tradeoffs", "non_goals"):
            value = goal.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise WorkflowError("INVALID_BOOTSTRAP", f"goal.{field} must be a string list")
        profile = result["operating_profile"]
        expected_profile_fields = {
            "schema_version",
            "artifact_role",
            "activation",
            "active_by_file_presence",
            "immutable_revision_payload",
            "profile_id",
            "status",
            "autonomy",
            "method_policy",
            "synchronization",
            "venues",
            "routing",
            "budgets",
        }
        if (
            set(profile) != expected_profile_fields
            or not _is_exact_json_integer(profile.get("schema_version"), 1)
            or profile.get("artifact_role") != "bootstrap_revision_payload"
            or profile.get("activation") != "authenticated_bootstrap_user_decision"
            or profile.get("active_by_file_presence") is not False
            or profile.get("immutable_revision_payload") is not True
            or not isinstance(profile.get("profile_id"), str)
            or not profile["profile_id"]
            or profile.get("status") != "provisional"
        ):
            raise WorkflowError("INVALID_BOOTSTRAP", "operating profile is not a bootstrap payload")
        autonomy = profile.get("autonomy")
        if not isinstance(autonomy, Mapping):
            raise WorkflowError("INVALID_BOOTSTRAP", "operating profile autonomy is invalid")
        modes = autonomy.get("enabled_modes")
        if (
            set(autonomy)
            != {"enabled_modes", "user_manages_execution", "automatic_merge", "automatic_deploy"}
            or not isinstance(modes, list)
            or not modes
            or any(mode not in {"replay", "shadow"} for mode in modes)
            or autonomy.get("user_manages_execution") is not False
            or autonomy.get("automatic_merge") is not False
            or autonomy.get("automatic_deploy") is not False
        ):
            raise WorkflowError("INVALID_BOOTSTRAP", "operating profile autonomy is unsafe")
        method_policy = profile.get("method_policy")
        synchronization = profile.get("synchronization")
        venues = profile.get("venues")
        routing = profile.get("routing")
        budgets = profile.get("budgets")
        expected_method_policy_fields = {
            "cognitive_actions_require_matt_receipt",
            "unknown_action_class",
            "fixed_global_skill_sequence",
        }
        expected_synchronization_fields = {
            "primary_briefs_per_day",
            "interrupt_only_for_material_harm",
            "silence_is_approval",
        }
        expected_routing_fields = {
            "exact_allows_fallback",
            "capability_class_requires_pinned_candidates",
            "actual_route_receipt_required",
        }
        expected_venues = {
            "local_hermes": {
                "role": "control-plane",
                "heavy_tests_allowed": False,
            },
            "local_copilot": {
                "role": "planning",
                "enabled_mode": "shadow",
                "requires_resource_isolation": True,
            },
            "github_copilot_cloud": {
                "role": "bounded-builder",
                "enabled_mode": "shadow",
                "requires_custom_agent": "matt-builder",
            },
            "feng": {
                "role": "authoritative-verification",
                "required_tests_authoritative": True,
                "max_concurrency": 1,
            },
        }
        if (
            not isinstance(method_policy, Mapping)
            or set(method_policy) != expected_method_policy_fields
            or method_policy.get("cognitive_actions_require_matt_receipt") is not True
            or method_policy.get("unknown_action_class") != "cognitive"
            or method_policy.get("fixed_global_skill_sequence") is not False
            or not isinstance(synchronization, Mapping)
            or set(synchronization) != expected_synchronization_fields
            or not _is_exact_json_integer(synchronization.get("primary_briefs_per_day"), 1)
            or synchronization.get("interrupt_only_for_material_harm") is not True
            or synchronization.get("silence_is_approval") is not False
            or not isinstance(venues, Mapping)
            or venues != expected_venues
            or not isinstance(venues.get("feng"), Mapping)
            or not _is_exact_json_integer(venues["feng"].get("max_concurrency"), 1)
            or not isinstance(routing, Mapping)
            or set(routing) != expected_routing_fields
            or routing.get("exact_allows_fallback") is not False
            or routing.get("capability_class_requires_pinned_candidates") is not True
            or routing.get("actual_route_receipt_required") is not True
            or not isinstance(budgets, Mapping)
            or set(budgets) != set(venues)
        ):
            raise WorkflowError("INVALID_BOOTSTRAP", "operating profile policy is incomplete")
        expected_budget_fields = {
            "local_hermes": {
                "enforcement",
                "max_wall_seconds",
                "max_turns",
                "max_concurrency",
                "max_paid_units",
                "exact_eligible",
            },
            "local_copilot": {
                "enforcement",
                "watchdog_policy_id",
                "max_wall_seconds",
                "max_concurrency",
                "max_paid_units",
                "exact_eligible",
            },
            "github_copilot_cloud": {
                "enforcement",
                "max_concurrency",
                "max_paid_units",
                "exact_eligible",
            },
            "feng": {
                "enforcement",
                "max_wall_seconds",
                "max_concurrency",
                "max_memory_mb",
                "max_disk_mb",
                "max_paid_units",
                "exact_eligible",
            },
        }
        for venue_name, budget in budgets.items():
            if not isinstance(budget, Mapping):
                raise WorkflowError(
                    "INVALID_BOOTSTRAP", f"operating profile budget is invalid: {venue_name}"
                )
            enforcement = budget.get("enforcement")
            if (
                set(budget) != expected_budget_fields[venue_name]
                or enforcement not in {"hard", "external_watchdog", "none"}
                or not _is_exact_json_integer(budget.get("max_concurrency"), 1)
                or not _is_exact_json_integer(budget.get("max_paid_units"), 0)
                or not isinstance(budget.get("exact_eligible"), bool)
                or (enforcement == "none" and budget.get("exact_eligible") is not False)
                or (
                    enforcement != "none"
                    and not _is_positive_json_integer(budget.get("max_wall_seconds"))
                )
                or (
                    enforcement == "external_watchdog"
                    and (
                        not isinstance(budget.get("watchdog_policy_id"), str)
                        or not budget["watchdog_policy_id"].strip()
                    )
                )
                or (
                    venue_name == "local_hermes"
                    and not _is_positive_json_integer(budget.get("max_turns"))
                )
                or (
                    venue_name == "feng"
                    and any(
                        not _is_positive_json_integer(budget.get(field))
                        for field in ("max_memory_mb", "max_disk_mb")
                    )
                )
            ):
                raise WorkflowError(
                    "INVALID_BOOTSTRAP", f"operating profile budget is invalid: {venue_name}"
                )
        return result


def _verify_decision_nonce_identity(row: sqlite3.Row) -> None:
    try:
        payload = json.loads(row["event_json"])
        if not isinstance(payload, dict) or _canonical_json(payload) != row["event_json"]:
            raise TypeError
        stored_event = UserDecision(**payload)
        if stored_event.decision_kind == "BOOTSTRAP_PROJECT":
            WorkflowKernel._validate_bootstrap_decision(stored_event)
            WorkflowKernel._validate_bootstrap_payload(stored_event.complete_revision_payload)
        else:
            WorkflowKernel._validate_revision_decision(stored_event)
            WorkflowKernel._validate_revision_payload(stored_event)
    except (json.JSONDecodeError, TypeError, ValueError, WorkflowError) as error:
        raise WorkflowError(
            "LEDGER_INTEGRITY", "stored decision event cannot verify nonce identity"
        ) from error
    expected_identity = (
        stored_event.project_id,
        stored_event.authenticated_actor,
        stored_event.nonce,
        stored_event.replay_identity,
        stored_event.source,
        stored_event.source_event_id,
    )
    stored_identity = (
        row["nonce_project_id"],
        row["nonce_actor_id"],
        row["nonce_value"],
        row["nonce_replay_identity"],
        row["nonce_source"],
        row["nonce_source_event_id"],
    )
    if stored_identity != expected_identity:
        raise WorkflowError("LEDGER_INTEGRITY", "decision nonce identity mismatch")


def _decision_receipt_id(event_digest: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"agentic-workflow:decision-receipt:{event_digest}",
        )
    )


def _verified_receipt(
    row: sqlite3.Row,
    *,
    expected_outcome: str,
    expected_active_intent_digest: str,
) -> RecordReceipt:
    receipt_json = row["receipt_json"]
    if _digest(receipt_json) != row["receipt_digest"]:
        raise WorkflowError("LEDGER_INTEGRITY", "record receipt digest mismatch")
    try:
        payload = json.loads(receipt_json)
        if not isinstance(payload, dict) or _canonical_json(payload) != receipt_json:
            raise TypeError
        receipt = RecordReceipt(**payload)
    except (json.JSONDecodeError, TypeError, ValueError, WorkflowError) as error:
        raise WorkflowError("LEDGER_INTEGRITY", "record receipt is not valid JSON") from error
    if (
        receipt.receipt_id != _decision_receipt_id(row["event_digest"])
        or receipt.project_id != row["event_project_id"]
        or row["indexed_event_type"] != "USER_DECISION"
        or receipt.event_type != "USER_DECISION"
        or receipt.event_digest != row["event_digest"]
        or receipt.recorded_at != row["event_recorded_at"]
        or receipt.outcome != expected_outcome
        or receipt.active_intent_digest != expected_active_intent_digest
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "record receipt fields mismatch")
    return receipt


def _matching_decision_rows(
    connection: sqlite3.Connection, event: UserDecision
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT e.rowid AS event_rowid, e.project_id AS event_project_id, "
        "e.event_type AS indexed_event_type, e.event_digest, e.event_json, "
        "e.receipt_json, e.receipt_digest, e.recorded_at AS event_recorded_at, "
        "n.project_id AS nonce_project_id, n.actor_id AS nonce_actor_id, "
        "n.nonce AS nonce_value, n.replay_identity AS nonce_replay_identity, "
        "n.source AS nonce_source, n.source_event_id AS nonce_source_event_id "
        "FROM inbox_events AS e "
        "LEFT JOIN decision_nonces AS n "
        "ON n.project_id = e.project_id AND n.source = e.source "
        "AND n.source_event_id = e.source_event_id "
        "WHERE (e.project_id = ? AND e.source = ? AND e.source_event_id = ?) "
        "OR (n.project_id = ? AND n.actor_id = ? AND n.nonce = ?) "
        "OR (n.project_id = ? AND n.actor_id = ? AND n.replay_identity = ?)",
        (
            event.project_id,
            event.source,
            event.source_event_id,
            event.project_id,
            event.authenticated_actor,
            event.nonce,
            event.project_id,
            event.authenticated_actor,
            event.replay_identity,
        ),
    ).fetchall()


def _resolve_decision_replay(
    connection: sqlite3.Connection,
    existing_rows: list[sqlite3.Row],
    event_digest: str,
    decision_kind: str,
) -> RecordReceipt:
    if any(_digest(row["event_json"]) != row["event_digest"] for row in existing_rows):
        raise WorkflowError("LEDGER_INTEGRITY", "record event digest mismatch")
    for row in existing_rows:
        _verify_decision_nonce_identity(row)
    if all(row["event_digest"] == event_digest for row in existing_rows):
        expected_outcome = {
            "REVISE_GOAL": "GOAL_REVISED",
            "REVISE_OPERATING_PROFILE": "OPERATING_PROFILE_REVISED",
        }[decision_kind]
        receipts = [
            _verified_receipt(
                row,
                expected_outcome=expected_outcome,
                expected_active_intent_digest=_activated_intent_digest_for_event(connection, row),
            )
            for row in existing_rows
        ]
        return receipts[0]
    raise WorkflowError(
        "IDENTITY_CONFLICT", "source-event or nonce identity was reused with different content"
    )


def _activated_intent_digest_for_event(connection: sqlite3.Connection, event: sqlite3.Row) -> str:
    intent_number = connection.execute(
        "SELECT COUNT(*) FROM inbox_events WHERE project_id = ? AND rowid <= ?",
        (event["event_project_id"], event["event_rowid"]),
    ).fetchone()[0]
    intent = connection.execute(
        "SELECT active_intent_digest, activated_at FROM active_intents "
        "WHERE project_id = ? AND intent_number = ?",
        (event["event_project_id"], intent_number),
    ).fetchone()
    if intent is None or intent["activated_at"] != event["event_recorded_at"]:
        raise WorkflowError("LEDGER_INTEGRITY", "revision event activation mismatch")
    return intent["active_intent_digest"]


def _decision_from_canonical_json(event_json: str) -> UserDecision:
    payload = json.loads(event_json)
    return UserDecision(**payload)


def _canonical_json(value: object) -> str:
    _validate_json_value(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise WorkflowError("INVALID_EVENT", "event must contain canonical JSON values") from error


def _project_digest(project_id: str, name: str, project_json: str, created_at: str) -> str:
    return _digest(
        _canonical_json(
            {
                "created_at": created_at,
                "name": name,
                "project_id": project_id,
                "project_json": project_json,
            }
        )
    )


def _active_intent_digest(
    constitution_revision: int,
    constitution_digest: str,
    goal_revision: int,
    goal_digest: str,
    operating_profile_revision: int,
    operating_profile_digest: str,
) -> str:
    return _digest(
        _canonical_json(
            {
                "constitution_revision": constitution_revision,
                "constitution_digest": constitution_digest,
                "goal_revision": goal_revision,
                "goal_digest": goal_digest,
                "operating_profile_revision": operating_profile_revision,
                "operating_profile_digest": operating_profile_digest,
            }
        )
    )


_ENVELOPE_SELECT = (
    "SELECT e.action_envelope_id, e.project_id, e.action_id AS envelope_action_id, "
    "e.predecessor_action_envelope_id, e.envelope_json, e.action_envelope_digest, "
    "e.constitution_revision, e.goal_revision, e.operating_profile_revision, "
    "e.active_intent_digest, a.action_id, a.action_kind, a.action_json, a.action_digest, "
    "a.constitution_revision AS action_constitution_revision, "
    "a.goal_revision AS action_goal_revision, "
    "a.operating_profile_revision AS action_profile_revision, "
    "a.active_intent_digest AS action_intent_digest, "
    "o.operation_id, o.operation_json, o.operation_digest, "
    "o.constitution_revision AS operation_constitution_revision, "
    "o.goal_revision AS operation_goal_revision, "
    "o.operating_profile_revision AS operation_profile_revision, "
    "o.active_intent_digest AS operation_intent_digest "
    "FROM action_envelopes AS e JOIN actions AS a ON a.action_id = e.action_id "
    "LEFT JOIN operation_records AS o ON o.action_envelope_id = e.action_envelope_id "
)


def _latest_live_envelope(
    connection: sqlite3.Connection, current: sqlite3.Row
) -> sqlite3.Row | None:
    envelope = connection.execute(
        _ENVELOPE_SELECT + "WHERE e.project_id = ? ORDER BY e.rowid DESC LIMIT 1",
        (current["project_id"],),
    ).fetchone()
    if envelope is None:
        return None
    envelope_binding = _verify_action_envelope(envelope)
    if envelope_binding != _binding_from_row(current):
        return None
    return envelope


def _compatible_source_envelope(
    connection: sqlite3.Connection, current: sqlite3.Row
) -> Mapping[str, Any] | None:
    rows = connection.execute(
        _ENVELOPE_SELECT + "JOIN compatibility_decisions AS decision "
        "ON decision.project_id = e.project_id "
        "AND decision.source_action_envelope_digest = e.action_envelope_digest "
        "WHERE decision.project_id = ? AND decision.active_intent_digest = ? "
        "ORDER BY decision.rowid DESC",
        (current["project_id"], current["active_intent_digest"]),
    ).fetchall()
    compatible: Mapping[str, Any] | None = None
    for row in rows:
        decision = connection.execute(
            "SELECT verdict, decision_json, decision_digest, constitution_revision, "
            "goal_revision, operating_profile_revision, active_intent_digest "
            "FROM compatibility_decisions WHERE project_id = ? "
            "AND active_intent_digest = ? AND source_action_envelope_digest = ?",
            (
                current["project_id"],
                current["active_intent_digest"],
                row["action_envelope_digest"],
            ),
        ).fetchone()
        _verify_compatibility_decision(decision, row, current)
        if decision["verdict"] == "compatible" and compatible is None:
            values = dict(row)
            values.update(
                {
                    "decision_json": decision["decision_json"],
                    "decision_digest": decision["decision_digest"],
                    "decision_constitution_revision": decision["constitution_revision"],
                    "decision_goal_revision": decision["goal_revision"],
                    "decision_profile_revision": decision["operating_profile_revision"],
                    "decision_active_intent_digest": decision["active_intent_digest"],
                }
            )
            compatible = values
    return compatible


def _verify_compatibility_decision(
    decision: sqlite3.Row, envelope: sqlite3.Row, current: sqlite3.Row
) -> None:
    payload = _verify_canonical_artifact(
        decision["decision_json"], decision["decision_digest"], "compatibility decision"
    )
    binding = IntentBinding(
        constitution_revision=decision["constitution_revision"],
        goal_revision=decision["goal_revision"],
        operating_profile_revision=decision["operating_profile_revision"],
        active_intent_digest=decision["active_intent_digest"],
    )
    if binding != _binding_from_row(current) or payload != {
        "intent_binding": asdict(binding),
        "source_action_envelope_digest": envelope["action_envelope_digest"],
        "verdict": decision["verdict"],
    }:
        raise WorkflowError("LEDGER_INTEGRITY", "compatibility decision fields mismatch")


def _current_intent_row(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT current.project_id, current.intent_number, "
        "(SELECT MAX(history.intent_number) FROM active_intents AS history "
        "WHERE history.project_id = current.project_id) AS latest_intent_number, "
        "i.constitution_revision, "
        "i.goal_revision, i.operating_profile_revision, i.active_intent_digest, "
        "c.payload_json AS constitution_json, c.payload_digest AS constitution_digest, "
        "g.payload_json AS goal_json, g.payload_digest AS goal_digest, "
        "o.payload_json AS profile_json, o.payload_digest AS profile_digest, "
        "p.name AS project_name "
        "FROM active_intent_current AS current "
        "JOIN active_intents AS i ON i.project_id = current.project_id "
        "AND i.intent_number = current.intent_number "
        "JOIN projects AS p ON p.project_id = current.project_id "
        "JOIN constitution_revisions AS c ON c.project_id = i.project_id "
        "AND c.revision_number = i.constitution_revision "
        "JOIN goal_revisions AS g ON g.project_id = i.project_id "
        "AND g.revision_number = i.goal_revision "
        "JOIN operating_profile_revisions AS o ON o.project_id = i.project_id "
        "AND o.revision_number = i.operating_profile_revision "
        "WHERE current.project_id = ?",
        (project_id,),
    ).fetchone()


def _verify_current_intent(row: sqlite3.Row) -> None:
    _verify_current_intent_position(row)
    try:
        constitution = _verify_canonical_artifact(
            row["constitution_json"], row["constitution_digest"], "Constitution revision"
        )
        goal = _verify_canonical_artifact(row["goal_json"], row["goal_digest"], "Goal revision")
        profile = _verify_canonical_artifact(
            row["profile_json"], row["profile_digest"], "Operating Profile revision"
        )
        WorkflowKernel._validate_bootstrap_payload(
            {
                "project": {"name": row["project_name"]},
                "constitution": constitution,
                "goal": goal,
                "operating_profile": profile,
            }
        )
    except WorkflowError as error:
        if error.code == "LEDGER_INTEGRITY":
            raise
        raise WorkflowError(
            "LEDGER_INTEGRITY", "current intent payload is not a complete V1 snapshot"
        ) from error
    expected = _active_intent_digest(
        row["constitution_revision"],
        row["constitution_digest"],
        row["goal_revision"],
        row["goal_digest"],
        row["operating_profile_revision"],
        row["profile_digest"],
    )
    if expected != row["active_intent_digest"]:
        raise WorkflowError("LEDGER_INTEGRITY", "active intent digest mismatch")


def _verify_current_intent_position(row: sqlite3.Row) -> None:
    if (
        not _is_positive_json_integer(row["intent_number"])
        or row["intent_number"] != row["latest_intent_number"]
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "current intent is not latest")


def _binding_from_row(row: sqlite3.Row) -> IntentBinding:
    return IntentBinding(
        constitution_revision=row["constitution_revision"],
        goal_revision=row["goal_revision"],
        operating_profile_revision=row["operating_profile_revision"],
        active_intent_digest=row["active_intent_digest"],
    )


def _binding_from_artifact_row(
    row: Mapping[str, Any] | sqlite3.Row, prefix: str = ""
) -> IntentBinding:
    profile_key = f"{prefix}profile_revision" if prefix else "operating_profile_revision"
    digest_key = f"{prefix}intent_digest" if prefix else "active_intent_digest"
    binding = IntentBinding(
        constitution_revision=row[f"{prefix}constitution_revision"],
        goal_revision=row[f"{prefix}goal_revision"],
        operating_profile_revision=row[profile_key],
        active_intent_digest=row[digest_key],
    )
    _validate_intent_binding(binding, "indexed artifact")
    return binding


def _binding_from_payload(payload: Mapping[str, Any], name: str) -> IntentBinding:
    raw = payload.get("intent_binding")
    if not isinstance(raw, Mapping) or set(raw) != {
        "constitution_revision",
        "goal_revision",
        "operating_profile_revision",
        "active_intent_digest",
    }:
        raise WorkflowError("LEDGER_INTEGRITY", f"{name} Intent Binding is incomplete")
    binding = IntentBinding(
        constitution_revision=raw["constitution_revision"],
        goal_revision=raw["goal_revision"],
        operating_profile_revision=raw["operating_profile_revision"],
        active_intent_digest=raw["active_intent_digest"],
    )
    _validate_intent_binding(binding, name)
    return binding


def _validate_intent_binding(binding: IntentBinding, name: str) -> None:
    if (
        not _is_positive_json_integer(binding.constitution_revision)
        or not _is_positive_json_integer(binding.goal_revision)
        or not _is_positive_json_integer(binding.operating_profile_revision)
        or not _is_sha256(binding.active_intent_digest)
    ):
        raise WorkflowError("LEDGER_INTEGRITY", f"{name} Intent Binding is invalid")


def _verify_action_envelope(row: Mapping[str, Any] | sqlite3.Row) -> IntentBinding:
    action = _verify_canonical_artifact(row["action_json"], row["action_digest"], "Action")
    if (
        set(action) != {"action_id", "action_kind", "intent_binding", "objective"}
        or action["action_id"] != row["action_id"]
        or action["action_kind"] != row["action_kind"]
        or action["action_kind"] != "GOAL_WORK"
        or not isinstance(action["objective"], str)
        or not action["objective"].strip()
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "Action fields do not match indexed fields")
    action_binding = _binding_from_payload(action, "Action")
    indexed_action_binding = _binding_from_artifact_row(row, "action_")
    if action_binding != indexed_action_binding:
        raise WorkflowError("LEDGER_INTEGRITY", "Action Intent Binding mismatch")

    envelope = _verify_canonical_artifact(
        row["envelope_json"], row["action_envelope_digest"], "Action Envelope"
    )
    if (
        set(envelope)
        != {
            "acceptance",
            "action_digest",
            "action_envelope_id",
            "constraints",
            "intent_binding",
            "predecessor_action_envelope_id",
            "stop_conditions",
        }
        or row["envelope_action_id"] != row["action_id"]
        or envelope["action_digest"] != row["action_digest"]
        or envelope["action_envelope_id"] != row["action_envelope_id"]
        or envelope["predecessor_action_envelope_id"] != row["predecessor_action_envelope_id"]
        or envelope["stop_conditions"] != ["ACTIVE_INTENT_CHANGED"]
        or any(
            not isinstance(items, list) or any(not isinstance(item, str) for item in items)
            for items in (envelope["acceptance"], envelope["constraints"])
        )
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "Action Envelope fields are invalid")
    envelope_binding = _binding_from_payload(envelope, "Action Envelope")
    indexed_envelope_binding = _binding_from_artifact_row(row)
    if envelope_binding != indexed_envelope_binding or envelope_binding != action_binding:
        raise WorkflowError("LEDGER_INTEGRITY", "Action Envelope Intent Binding mismatch")
    return envelope_binding


def _verify_operation(row: Mapping[str, Any] | sqlite3.Row) -> IntentBinding:
    operation = _verify_canonical_artifact(
        row["operation_json"], row["operation_digest"], "Operation Record"
    )
    if set(operation) != {
        "action_envelope_digest",
        "effect_kind",
        "intent_binding",
        "operation_id",
    }:
        raise WorkflowError("LEDGER_INTEGRITY", "Operation Record schema is invalid")
    operation_binding = _binding_from_payload(operation, "Operation Record")
    indexed_binding = _binding_from_artifact_row(row, "operation_")
    if (
        operation_binding != indexed_binding
        or operation["action_envelope_digest"] != row["action_envelope_digest"]
        or operation["effect_kind"] != "BOUNDED_WORK"
        or operation["operation_id"] != row["operation_id"]
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "Operation Record fields are inconsistent")
    return operation_binding


def _verify_operation_events(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any] | sqlite3.Row,
    operation_binding: IntentBinding,
) -> tuple[str, ...]:
    events = connection.execute(
        "SELECT event_number, event_type, payload_json, payload_digest, "
        "constitution_revision, goal_revision, operating_profile_revision, "
        "active_intent_digest FROM operation_events "
        "WHERE operation_id = ? ORDER BY event_number",
        (operation["operation_id"],),
    ).fetchall()
    event_types: list[str] = []
    for expected_number, event in enumerate(events, 1):
        payload = _verify_canonical_artifact(
            event["payload_json"], event["payload_digest"], "Operation event"
        )
        event_type = event["event_type"]
        expected_fields = {"event_type", "intent_binding", "operation_digest"}
        if event_type == "CONCLUDED":
            expected_fields.add("result")
        if (
            event["event_number"] != expected_number
            or event_type not in {"RESERVED", "CONCLUDED"}
            or set(payload) != expected_fields
            or payload["event_type"] != event_type
            or payload["operation_digest"] != operation["operation_digest"]
            or (event_type == "CONCLUDED" and payload["result"] != "NO_EXTERNAL_EFFECT")
        ):
            raise WorkflowError("LEDGER_INTEGRITY", "Operation event fields are inconsistent")
        payload_binding = _binding_from_payload(payload, "Operation event")
        indexed_binding = _binding_from_artifact_row(event)
        if payload_binding != indexed_binding or payload_binding != operation_binding:
            raise WorkflowError("LEDGER_INTEGRITY", "Operation event Intent Binding mismatch")
        event_types.append(event_type)
    result = tuple(event_types)
    if result not in {("RESERVED",), ("RESERVED", "CONCLUDED")}:
        raise WorkflowError("LEDGER_INTEGRITY", "Operation event lifecycle is invalid")
    return result


def _verify_canonical_artifact(payload_json: str, payload_digest: str, name: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError) as error:
        raise WorkflowError("LEDGER_INTEGRITY", f"{name} is not valid JSON") from error
    if (
        not isinstance(payload, dict)
        or _canonical_json(payload) != payload_json
        or _digest(payload_json) != payload_digest
    ):
        raise WorkflowError("LEDGER_INTEGRITY", f"{name} digest mismatch")
    return payload


def _verify_project_row(row: sqlite3.Row) -> None:
    try:
        project_payload = json.loads(row["project_json"])
        canonical_project_json = _canonical_json(project_payload)
    except (json.JSONDecodeError, TypeError, ValueError, WorkflowError) as error:
        raise WorkflowError(
            "LEDGER_INTEGRITY", "authoritative project payload is not canonical JSON"
        ) from error
    if (
        not isinstance(project_payload, dict)
        or canonical_project_json != row["project_json"]
        or project_payload.get("name") != row["name"]
        or _project_digest(row["project_id"], row["name"], row["project_json"], row["created_at"])
        != row["project_digest"]
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "authoritative project digest mismatch")


def _digest(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        raise WorkflowError("INVALID_EVENT", "event contains a float")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowError("INVALID_EVENT", "JSON object keys must be strings")
            _validate_json_value(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    raise WorkflowError("INVALID_EVENT", "event contains a non-JSON value")


def _is_positive_json_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _is_exact_json_integer(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
