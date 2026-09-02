"""Private Replay/Shadow Operation lifecycle behind ``WorkflowKernel``."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from threading import Lock
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypedDict
from zoneinfo import ZoneInfo

from .model import (
    AdvanceResult,
    IntentBinding,
    RecordReceipt,
    UserDecision,
    WorkflowError,
    _StrictJsonObject,
    _validate_json_value,
)
from .store import ControlStore

_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_TERMINAL = {"APPLIED", "NOT_APPLIED", "AMBIGUOUS"}


class _Clock(Protocol):
    def now(self) -> str: ...


class _Authenticator(Protocol):
    def authenticate(self, decision: UserDecision) -> bool: ...


@dataclass(frozen=True)
class FrozenOperation:
    operation_id: str
    operation_digest: str
    project_id: str
    action_envelope_digest: str
    target_identity: Mapping[str, Any]
    expected_target_version: str
    side_effect_class: str
    exact_spend_cap: Mapping[str, Any]
    idempotency_identity: str
    mode: str
    physical_apply_authorized: bool = False


@dataclass(frozen=True)
class EffectProbe:
    operation_id: str
    operation_digest: str
    target_identity: Mapping[str, Any]
    expected_target_version: str
    idempotency_identity: str


@dataclass(frozen=True)
class OutboxMessage:
    outbox_event_id: str
    logical_outbox_identity: str
    project_id: str
    local_day: str
    brief: Mapping[str, Any]
    event_digest: str


@dataclass(frozen=True)
class _OperationPolicyRequest(Mapping[str, Any]):
    action_envelope_digest: str
    action_envelope_id: str
    action_id: str
    action_kind: str
    active_intent_digest: str
    project_id: str

    def __getitem__(self, key: str) -> Any:
        if key not in self.__dataclass_fields__:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)


class _OperationPolicyResponse(TypedDict):
    approval_required: bool
    approval_expires_at: str
    exact_spend_cap: _StrictJsonObject
    expected_target_version: str
    side_effect_class: str
    target_identity: _StrictJsonObject


class _AttemptResponse(TypedDict):
    attempted: bool
    idempotency_identity: str
    operation_digest: str


class _ObservationResponse(TypedDict):
    classification: Literal["APPLIED", "NOT_APPLIED", "AMBIGUOUS"]
    evidence: _StrictJsonObject
    operation_digest: str


class _DeliveryResponse(TypedDict):
    acknowledged: bool
    logical_outbox_identity: str
    transport_receipt_id: str


class _ScriptedOperationEffects(Protocol):
    operation_mode: str | None

    def operation_policy(self, request: _OperationPolicyRequest) -> _OperationPolicyResponse: ...

    def attempt_operation(self, operation: FrozenOperation) -> _AttemptResponse: ...

    def observe_operation(self, probe: EffectProbe) -> _ObservationResponse: ...

    def deliver_outbox(self, message: OutboxMessage) -> _DeliveryResponse: ...


@dataclass(frozen=True)
class _PreparedOperation:
    envelope_digest: str
    mode: str
    policy: Mapping[str, Any]
    policy_digest: str


@dataclass(frozen=True)
class _AttemptCommand:
    operation: FrozenOperation


@dataclass(frozen=True)
class _ObserveCommand:
    operation: FrozenOperation


@dataclass(frozen=True)
class _DeliveryCommand:
    message: OutboxMessage
    attempt_number: int


_PRIVATE_FAULT_HOOK: Any = None
_PULSE_LOCKS: dict[tuple[str, str], Lock] = {}
_PULSE_LOCKS_GUARD = Lock()


def _fault(point: str) -> None:
    if _PRIVATE_FAULT_HOOK is not None:
        _PRIVATE_FAULT_HOOK(point)


class OperationLifecycle:
    """Own journal, approval, recovery, day-close, and transport policy."""

    def __init__(
        self,
        store: ControlStore,
        effects: _ScriptedOperationEffects | None,
        clock: _Clock,
        authenticator: _Authenticator,
    ) -> None:
        self._store = store
        self._effects = effects
        self._clock = clock
        self._authenticator = authenticator
        self._plans: dict[str, _PreparedOperation] = {}
        self._mode = getattr(effects, "operation_mode", None) if effects is not None else None
        if self._mode is None:
            return
        if self._mode not in {"replay", "shadow"}:
            raise WorkflowError("INVALID_OPERATION_MODE", "scripted Operation mode is invalid")
        required = ["operation_policy", "deliver_outbox"]
        if self._mode == "shadow":
            required.extend(("attempt_operation", "observe_operation"))
        if any(not callable(getattr(effects, name, None)) for name in required):
            raise WorkflowError(
                "INVALID_OPERATION_ADAPTER", "scripted ExternalEffects capabilities are incomplete"
            )

    @property
    def enabled(self) -> bool:
        return self._mode is not None

    @property
    def _adapter(self) -> _ScriptedOperationEffects:
        if self._effects is None:
            raise WorkflowError("LEDGER_INTEGRITY", "scripted Operation adapter is unavailable")
        return self._effects

    @contextmanager
    def pulse(self, project_id: str) -> Iterator[None]:
        """Serialize one process-local Pulse while allowing post-crash recovery."""
        key = (str(self._store.database_path), project_id)
        with _PULSE_LOCKS_GUARD:
            lock = _PULSE_LOCKS.setdefault(key, Lock())
        if not lock.acquire(blocking=False):
            raise WorkflowError("PULSE_BUSY", "another Pulse owns this Workflow Project")
        try:
            yield
        finally:
            lock.release()

    def plan(self, project_id: str) -> None:
        """Read policy outside a writer transaction and freeze it for the live envelope."""
        mode = self._mode
        if mode is None:
            return
        with self._store.reader() as connection:
            row = connection.execute(
                "SELECT e.action_envelope_digest, e.action_envelope_id, e.action_id, "
                "e.active_intent_digest, a.action_kind FROM active_intent_current AS c "
                "JOIN active_intents AS i ON i.project_id = c.project_id "
                "AND i.intent_number = c.intent_number "
                "JOIN action_envelopes AS e ON e.project_id = i.project_id "
                "AND e.active_intent_digest = i.active_intent_digest "
                "JOIN actions AS a ON a.action_id = e.action_id "
                "LEFT JOIN operation_records AS o ON o.action_envelope_id = e.action_envelope_id "
                "WHERE i.project_id = ? AND o.operation_id IS NULL "
                "ORDER BY e.created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if row is None:
            return
        context = _OperationPolicyRequest(
            action_envelope_digest=row["action_envelope_digest"],
            action_envelope_id=row["action_envelope_id"],
            action_id=row["action_id"],
            action_kind=row["action_kind"],
            active_intent_digest=row["active_intent_digest"],
            project_id=project_id,
        )
        try:
            returned = self._adapter.operation_policy(context)
            policy = _freeze_policy(returned)
        except WorkflowError:
            raise
        except Exception as error:
            raise WorkflowError(
                "OPERATION_POLICY_FAILED", "scripted Operation policy failed"
            ) from error
        prepared = _PreparedOperation(
            envelope_digest=row["action_envelope_digest"],
            mode=mode,
            policy=MappingProxyType(policy),
            policy_digest=_digest(_canonical_json(policy)),
        )
        existing = self._plans.setdefault(row["action_envelope_digest"], prepared)
        if existing != prepared:
            raise WorkflowError("OPERATION_POLICY_DRIFT", "scripted Operation policy changed")

    def prepare(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        envelope: Mapping[str, Any] | sqlite3.Row,
        recorded_at: str,
    ) -> AdvanceResult | None:
        if not self.enabled:
            return None
        envelope_digest = envelope["action_envelope_digest"]
        plan = self._plans.get(envelope_digest)
        if plan is None:
            raise WorkflowError(
                "OPERATION_POLICY_UNAVAILABLE", "scripted Operation policy was not frozen"
            )
        binding = _binding(envelope, prefix="")
        if binding != _binding(current):
            raise WorkflowError("STALE_INTENT", "Operation is not bound to current authority")
        operation_id = _stable_id("operation", envelope_digest)
        policy = plan.policy
        target_json = _canonical_json(policy["target_identity"])
        spend_json = _canonical_json(policy["exact_spend_cap"])
        idempotency_identity = _digest(
            _canonical_json(
                {
                    "action_envelope_digest": envelope_digest,
                    "expected_target_version": policy["expected_target_version"],
                    "target_identity": policy["target_identity"],
                }
            )
        )
        payload = {
            "action_envelope_digest": envelope_digest,
            "approval_required": policy["approval_required"],
            "exact_spend_cap": policy["exact_spend_cap"],
            "expected_target_version": policy["expected_target_version"],
            "idempotency_identity": idempotency_identity,
            "intent_binding": asdict(binding),
            "mode": plan.mode,
            "operation_id": operation_id,
            "physical_apply_authorized": False,
            "side_effect_class": policy["side_effect_class"],
            "target_identity": policy["target_identity"],
        }
        operation_json = _canonical_json(payload)
        operation_digest = _digest(operation_json)
        connection.execute(
            "INSERT INTO operation_records "
            "(operation_id, project_id, action_envelope_id, operation_json, operation_digest, "
            "constitution_revision, goal_revision, operating_profile_revision, "
            "active_intent_digest, reserved_at, target_identity_json, target_identity_digest, "
            "expected_target_version, side_effect_class, idempotency_identity, "
            "exact_spend_cap_json, exact_spend_cap_digest, approval_required, "
            "approval_expires_at, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                operation_id,
                current["project_id"],
                envelope["action_envelope_id"],
                operation_json,
                operation_digest,
                binding.constitution_revision,
                binding.goal_revision,
                binding.operating_profile_revision,
                binding.active_intent_digest,
                recorded_at,
                target_json,
                _digest(target_json),
                policy["expected_target_version"],
                policy["side_effect_class"],
                idempotency_identity,
                spend_json,
                _digest(spend_json),
                int(policy["approval_required"]),
                policy["approval_expires_at"],
                plan.mode,
            ),
        )
        event_type = "AWAITING_APPROVAL" if policy["approval_required"] else "PREPARED"
        _append_event(connection, payload, binding, event_type, recorded_at)
        self._plans.pop(envelope_digest, None)
        stored = _operation_by_id(connection, operation_id)
        return _result(stored, binding, f"OPERATION_{event_type}")

    def record_approval(self, event: UserDecision) -> RecordReceipt:
        if not self.enabled:
            raise WorkflowError("DECISION_NOT_IMPLEMENTED", "decision kind is not implemented")
        _validate_json_value(event.provenance)
        if not isinstance(event.provenance, dict):
            raise WorkflowError("INVALID_EVENT", "event provenance must be a JSON object")
        _validate_json_value(event.complete_revision_payload)
        _validate_approval_identity(event)
        event_json = _canonical_json(asdict(event))
        frozen = UserDecision(**json.loads(event_json))
        if not self._authenticator.authenticate(frozen):
            raise WorkflowError("UNAUTHENTICATED_DECISION", "decision authentication failed")
        if (
            _canonical_json(asdict(event)) != event_json
            or _canonical_json(asdict(frozen)) != event_json
        ):
            raise WorkflowError("INVALID_EVENT", "decision mutated during authentication")
        event = frozen
        event_digest = _digest(event_json)
        with self._store.writer() as connection:
            matches = connection.execute(
                "SELECT e.*, n.actor_id, n.nonce, n.replay_identity FROM inbox_events AS e "
                "LEFT JOIN decision_nonces AS n ON n.project_id = e.project_id "
                "AND n.source = e.source AND n.source_event_id = e.source_event_id "
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
            if matches:
                if any(row["event_digest"] != event_digest for row in matches):
                    raise WorkflowError("IDENTITY_CONFLICT", "approval identity was reused")
                receipt = _stored_receipt(matches[0])
                if (
                    receipt.event_digest != event_digest
                    or receipt.project_id != event.project_id
                    or receipt.event_type != "APPROVAL_DECISION"
                    or receipt.outcome != "OPERATION_APPROVAL_RECORDED"
                ):
                    raise WorkflowError("LEDGER_INTEGRITY", "approval receipt fields changed")
                return receipt
            operation = _operation_by_id(
                connection, event.complete_revision_payload.get("operation_id")
            )
            if operation["project_id"] != event.project_id:
                raise WorkflowError("INVALID_APPROVAL", "approval names another project")
            expected = self._approval_request(operation)
            if event.complete_revision_payload != expected["complete_revision_payload"]:
                raise WorkflowError(
                    "INVALID_APPROVAL", "approval does not bind the exact Operation"
                )
            events = _events(connection, operation)
            if events != ("AWAITING_APPROVAL",):
                raise WorkflowError("INVALID_APPROVAL", "Operation is not awaiting approval")
            approved_at = self._clock.now()
            if _timestamp(approved_at, "Clock") >= _timestamp(
                operation["approval_expires_at"], "approval expiry"
            ):
                raise WorkflowError("APPROVAL_EXPIRED", "approval has expired")
            current = _current(connection, event.project_id)
            if operation["active_intent_digest"] != current["active_intent_digest"]:
                raise WorkflowError("STALE_INTENT", "approval is not bound to current authority")
            receipt = RecordReceipt(
                receipt_id=_stable_id("record-receipt", event_digest),
                project_id=event.project_id,
                event_type="APPROVAL_DECISION",
                outcome="OPERATION_APPROVAL_RECORDED",
                event_digest=event_digest,
                active_intent_digest=operation["active_intent_digest"],
                recorded_at=approved_at,
            )
            receipt_json = _canonical_json(asdict(receipt))
            connection.execute(
                "INSERT INTO inbox_events (project_id, source, source_event_id, event_type, "
                "event_digest, event_json, receipt_json, receipt_digest, recorded_at) "
                "VALUES (?, ?, ?, 'APPROVAL_DECISION', ?, ?, ?, ?, ?)",
                (
                    event.project_id,
                    event.source,
                    event.source_event_id,
                    event_digest,
                    event_json,
                    receipt_json,
                    _digest(receipt_json),
                    approved_at,
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
            approval_json = _canonical_json(
                {
                    "decision_event_digest": event_digest,
                    "operation_digest": operation["operation_digest"],
                    "operation_id": operation["operation_id"],
                }
            )
            connection.execute(
                "INSERT INTO operation_approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _stable_id("approval", event_digest),
                    event.project_id,
                    operation["operation_id"],
                    event_digest,
                    approval_json,
                    _digest(approval_json),
                    operation["approval_expires_at"],
                    approved_at,
                ),
            )
            return receipt

    def pending_decisions(
        self, connection: sqlite3.Connection, project_id: str
    ) -> tuple[dict[str, Any], ...]:
        if not self.enabled:
            return ()
        rows = connection.execute(
            _OPERATION_SELECT
            + " WHERE o.project_id = ? AND o.mode IS NOT NULL "
            "ORDER BY o.reserved_at, o.operation_id",
            (project_id,),
        ).fetchall()
        current = _current(connection, project_id)
        pending = []
        for row in rows:
            if row["active_intent_digest"] != current["active_intent_digest"]:
                continue
            if _events(connection, row) != ("AWAITING_APPROVAL",):
                continue
            if connection.execute(
                "SELECT 1 FROM operation_approvals WHERE operation_id = ?", (row["operation_id"],)
            ).fetchone() is None:
                pending.append(self._approval_request(row))
        return tuple(pending)

    def control(self, project_id: str) -> AdvanceResult | None:
        if not self.enabled:
            return None
        command: _AttemptCommand | _ObserveCommand | _DeliveryCommand | None = None
        with self._store.writer() as connection:
            current = _current(connection, project_id)
            operation = connection.execute(
                _OPERATION_SELECT
                + " WHERE o.project_id = ? AND o.mode IS NOT NULL "
                "ORDER BY o.reserved_at DESC, o.operation_id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if operation is None:
                command = self._pending_delivery(connection, project_id)
                if command is None:
                    return None
            else:
                events = _events(connection, operation)
                if events == ("AWAITING_APPROVAL",):
                    if operation["active_intent_digest"] != current["active_intent_digest"]:
                        return None
                    approval = connection.execute(
                        "SELECT * FROM operation_approvals WHERE operation_id = ?",
                        (operation["operation_id"],),
                    ).fetchone()
                    if approval is None:
                        raise WorkflowError("APPROVAL_REQUIRED", "Operation awaits exact approval")
                    now = self._clock.now()
                    if _timestamp(now, "Clock") >= _timestamp(
                        approval["expires_at"], "approval expiry"
                    ):
                        raise WorkflowError("APPROVAL_EXPIRED", "approval has expired")
                    if operation["active_intent_digest"] != current["active_intent_digest"]:
                        raise WorkflowError("STALE_INTENT", "approval is not current")
                    consumption_json = _canonical_json(
                        {
                            "approval_digest": approval["approval_digest"],
                            "operation_digest": operation["operation_digest"],
                        }
                    )
                    connection.execute(
                        "INSERT INTO operation_approval_consumptions VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            _stable_id("approval-consumption", approval["approval_digest"]),
                            approval["approval_id"],
                            operation["operation_id"],
                            consumption_json,
                            _digest(consumption_json),
                            now,
                        ),
                    )
                    payload = _operation_payload(operation)
                    _append_event(
                        connection,
                        payload,
                        _binding(operation, "operation_"),
                        "PREPARED",
                        now,
                    )
                    return _result(
                        operation,
                        _binding(operation, "operation_"),
                        "OPERATION_PREPARED",
                    )
                if events in {("PREPARED",), ("AWAITING_APPROVAL", "PREPARED")}:
                    if operation["active_intent_digest"] != current["active_intent_digest"]:
                        return None
                    if operation["mode"] == "replay":
                        self._record_readback(
                            connection,
                            operation,
                            {"classification": "NOT_APPLIED", "evidence": {"reason": "REPLAY"}},
                            self._clock.now(),
                        )
                        return _result(
                            operation,
                            _binding(operation, "operation_"),
                            "OPERATION_READBACK_RECORDED",
                        )
                    command = self._claim_attempt(connection, operation, self._clock.now())
                elif events in {
                    ("PREPARED", "ATTEMPT_INTENT"),
                    ("PREPARED", "ATTEMPT_INTENT", "ATTEMPT_RETURNED"),
                    ("AWAITING_APPROVAL", "PREPARED", "ATTEMPT_INTENT"),
                    (
                        "AWAITING_APPROVAL",
                        "PREPARED",
                        "ATTEMPT_INTENT",
                        "ATTEMPT_RETURNED",
                    ),
                }:
                    command = _ObserveCommand(_frozen_operation(operation))
                elif events[-1] == "READBACK_RECORDED":
                    evidence = _evidence(connection, operation["operation_id"], "READBACK")
                    classification = json.loads(evidence["evidence_json"])["evidence"][
                        "classification"
                    ]
                    _append_event(
                        connection,
                        _operation_payload(operation),
                        _binding(operation, "operation_"),
                        classification,
                        self._clock.now(),
                    )
                    return _result(
                        operation,
                        _binding(operation, "operation_"),
                        f"OPERATION_{classification}",
                    )
                elif events[-1] in _TERMINAL or events == ("PREPARED", "CONCLUDED"):
                    ready = self._close_day(connection, project_id, self._clock.now())
                    if ready is not None:
                        return ready
                    command = self._pending_delivery(connection, project_id)
                    if command is None:
                        if operation["active_intent_digest"] != current["active_intent_digest"]:
                            return None
                        raise WorkflowError("NO_ACTION", "Operation has no pending transition")
                else:
                    raise WorkflowError("LEDGER_INTEGRITY", "Operation lifecycle is invalid")
        if isinstance(command, _AttemptCommand):
            return self._attempt(command)
        if isinstance(command, _ObserveCommand):
            return self._observe(command)
        if isinstance(command, _DeliveryCommand):
            return self._deliver(command)
        raise WorkflowError("LEDGER_INTEGRITY", "Operation control produced no transition")

    def _claim_attempt(
        self, connection: sqlite3.Connection, operation: sqlite3.Row, attempted_at: str
    ) -> _AttemptCommand:
        frozen = _frozen_operation(operation)
        attempt_json = _canonical_json(
            {
                "idempotency_identity": frozen.idempotency_identity,
                "operation_digest": frozen.operation_digest,
            }
        )
        connection.execute(
            "INSERT INTO operation_attempts VALUES (?, ?, ?, ?, ?)",
            (
                _stable_id("operation-attempt", frozen.operation_digest),
                frozen.operation_id,
                attempt_json,
                _digest(attempt_json),
                attempted_at,
            ),
        )
        _append_event(
            connection,
            _operation_payload(operation),
            _binding(operation, "operation_"),
            "ATTEMPT_INTENT",
            attempted_at,
        )
        return _AttemptCommand(frozen)

    def _attempt(self, command: _AttemptCommand) -> AdvanceResult:
        _fault("operation_before_attempt")
        try:
            returned = self._adapter.attempt_operation(command.operation)
            result = _freeze_mapping(returned, "Operation attempt result")
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as error:
            result = {"attempted": "UNKNOWN", "error_type": type(error).__name__}
        _fault("operation_after_attempt")
        now = self._clock.now()
        with self._store.writer() as connection:
            operation = _operation_by_id(connection, command.operation.operation_id)
            events = _events(connection, operation)
            returned_histories = {
                ("PREPARED", "ATTEMPT_INTENT", "ATTEMPT_RETURNED"),
                (
                    "AWAITING_APPROVAL",
                    "PREPARED",
                    "ATTEMPT_INTENT",
                    "ATTEMPT_RETURNED",
                ),
            }
            intent_histories = {
                ("PREPARED", "ATTEMPT_INTENT"),
                ("AWAITING_APPROVAL", "PREPARED", "ATTEMPT_INTENT"),
            }
            if events in returned_histories:
                return _result(
                    operation,
                    _binding(operation, "operation_"),
                    "OPERATION_ATTEMPT_RETURNED",
                )
            if events not in intent_histories:
                raise WorkflowError("LEDGER_INTEGRITY", "Operation attempt lineage changed")
            _append_evidence(connection, operation, "ATTEMPT_RESULT", result, now)
            _append_event(
                connection,
                _operation_payload(operation),
                _binding(operation, "operation_"),
                "ATTEMPT_RETURNED",
                now,
            )
            return _result(
                operation,
                _binding(operation, "operation_"),
                "OPERATION_ATTEMPT_RETURNED",
            )

    def _observe(self, command: _ObserveCommand) -> AdvanceResult:
        _fault("operation_before_readback")
        try:
            returned = self._adapter.observe_operation(_probe(command.operation))
            observation = _freeze_mapping(returned, "Operation observation")
            if (
                set(observation) != {"classification", "evidence", "operation_digest"}
                or observation["classification"] not in _TERMINAL
                or observation["operation_digest"] != command.operation.operation_digest
                or not isinstance(observation["evidence"], dict)
            ):
                raise WorkflowError(
                    "INVALID_EFFECT_OBSERVATION", "readback classification is invalid"
                )
        except (SystemExit, KeyboardInterrupt):
            raise
        except WorkflowError:
            raise
        except Exception as error:
            observation = {
                "classification": "AMBIGUOUS",
                "evidence": {"error_type": type(error).__name__},
            }
        _fault("operation_after_readback")
        now = self._clock.now()
        with self._store.writer() as connection:
            operation = _operation_by_id(connection, command.operation.operation_id)
            if _events(connection, operation)[-1] == "READBACK_RECORDED":
                return _result(
                    operation,
                    _binding(operation, "operation_"),
                    "OPERATION_READBACK_RECORDED",
                )
            self._record_readback(connection, operation, observation, now)
            return _result(
                operation,
                _binding(operation, "operation_"),
                "OPERATION_READBACK_RECORDED",
            )

    @staticmethod
    def _record_readback(
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
        observation: Mapping[str, Any],
        recorded_at: str,
    ) -> None:
        classification = observation["classification"]
        if classification not in _TERMINAL:
            raise WorkflowError("INVALID_EFFECT_OBSERVATION", "readback classification is invalid")
        _append_evidence(connection, operation, "READBACK", dict(observation), recorded_at)
        _append_event(
            connection,
            _operation_payload(operation),
            _binding(operation, "operation_"),
            "READBACK_RECORDED",
            recorded_at,
        )

    def _close_day(
        self, connection: sqlite3.Connection, project_id: str, now: str
    ) -> AdvanceResult | None:
        today = _local_day(now)
        terminal_rows = connection.execute(
            _TERMINAL_OPERATION_SELECT
            + " JOIN operation_events AS terminal ON terminal.operation_id = o.operation_id "
            "AND terminal.event_type IN ('APPLIED', 'NOT_APPLIED', 'AMBIGUOUS') "
            "WHERE o.project_id = ? AND o.mode IS NOT NULL ORDER BY terminal.recorded_at",
            (project_id,),
        ).fetchall()
        by_day: dict[str, list[sqlite3.Row]] = {}
        for row in terminal_rows:
            day = _local_day(row["terminal_recorded_at"])
            if day < today:
                by_day.setdefault(day, []).append(row)
        for local_day, rows in sorted(by_day.items()):
            if connection.execute(
                "SELECT 1 FROM outbox_events WHERE project_id = ? AND local_day = ?",
                (project_id, local_day),
            ).fetchone():
                continue
            changes = []
            outcome_digests = []
            modes = set()
            for row in rows:
                events = _events(connection, row)
                outcome = events[-1]
                existing = connection.execute(
                    "SELECT * FROM operation_evidence WHERE operation_id = ? "
                    "AND evidence_kind = 'OUTCOME'",
                    (row["operation_id"],),
                ).fetchone()
                if existing is None:
                    outcome_payload = {
                        "operation_digest": row["operation_digest"],
                        "operation_id": row["operation_id"],
                        "outcome": outcome,
                    }
                    evidence_digest = _append_evidence(
                        connection, row, "OUTCOME", outcome_payload, now
                    )
                else:
                    evidence_digest = existing["evidence_digest"]
                changes.append(
                    {
                        "evidence_digest": evidence_digest,
                        "operation_id": row["operation_id"],
                        "outcome": outcome,
                    }
                )
                outcome_digests.append(evidence_digest)
                modes.add(row["mode"])
            source_digest = _digest(_canonical_json(outcome_digests))
            brief_id = _stable_id("daily-brief", f"{project_id}:{local_day}")
            brief = {
                "brief_id": brief_id,
                "local_day": local_day,
                "material_changes": changes,
                "mode": next(iter(modes)) if len(modes) == 1 else "mixed",
                "status": "MATERIAL_CHANGES",
                "timezone": "Asia/Shanghai",
            }
            brief_json = _canonical_json(brief)
            brief_number = connection.execute(
                "SELECT COALESCE(MAX(brief_number), 0) + 1 FROM daily_briefs WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO daily_briefs (project_id, brief_number, projection_json, "
                "projection_digest, projected_at, brief_id, local_day, timezone, "
                "source_evidence_digest) VALUES (?, ?, ?, ?, ?, ?, ?, 'Asia/Shanghai', ?)",
                (
                    project_id,
                    brief_number,
                    brief_json,
                    _digest(brief_json),
                    now,
                    brief_id,
                    local_day,
                    source_digest,
                ),
            )
            logical_identity = f"{project_id}:{local_day}"
            outbox_event_id = _stable_id("outbox", logical_identity)
            event = {
                "brief": brief,
                "brief_digest": _digest(brief_json),
                "brief_id": brief_id,
                "local_day": local_day,
                "logical_outbox_identity": logical_identity,
                "outbox_event_id": outbox_event_id,
                "project_id": project_id,
            }
            event_json = _canonical_json(event)
            connection.execute(
                "INSERT INTO outbox_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    outbox_event_id,
                    logical_identity,
                    project_id,
                    brief_id,
                    local_day,
                    event_json,
                    _digest(event_json),
                    now,
                ),
            )
            return _result(rows[-1], _binding(rows[-1], "operation_"), "DAILY_BRIEF_READY")
        return None

    def _pending_delivery(
        self, connection: sqlite3.Connection, project_id: str
    ) -> _DeliveryCommand | None:
        row = connection.execute(
            "SELECT e.* FROM outbox_events AS e "
            "LEFT JOIN outbox_acknowledgements AS a ON a.outbox_event_id = e.outbox_event_id "
            "WHERE e.project_id = ? AND a.outbox_event_id IS NULL "
            "ORDER BY e.local_day LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        event = _verified_json(row["event_json"], row["event_digest"], "Outbox Event")
        if event["brief"].get("mode") == "replay":
            return None
        message = OutboxMessage(
            outbox_event_id=row["outbox_event_id"],
            logical_outbox_identity=row["logical_outbox_identity"],
            project_id=row["project_id"],
            local_day=row["local_day"],
            brief=MappingProxyType(event["brief"]),
            event_digest=row["event_digest"],
        )
        attempt_number = connection.execute(
            "SELECT COUNT(*) + 1 FROM outbox_delivery_attempts WHERE outbox_event_id = ?",
            (row["outbox_event_id"],),
        ).fetchone()[0]
        return _DeliveryCommand(message, attempt_number)

    def _deliver(self, command: _DeliveryCommand) -> AdvanceResult:
        _fault("outbox_before_delivery")
        try:
            returned = self._adapter.deliver_outbox(command.message)
            result = _freeze_mapping(returned, "Outbox transport result")
            acknowledged = (
                result.get("acknowledged") is True
                and result.get("logical_outbox_identity")
                == command.message.logical_outbox_identity
                and isinstance(result.get("transport_receipt_id"), str)
                and bool(result["transport_receipt_id"])
            )
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as error:
            result = {"error_type": type(error).__name__}
            acknowledged = False
        _fault("outbox_after_delivery")
        now = self._clock.now()
        result_json = _canonical_json(
            {"attempt_number": command.attempt_number, "transport_result": result}
        )
        attempt_id = _stable_id(
            "outbox-attempt",
            f"{command.message.outbox_event_id}:{command.attempt_number}",
        )
        with self._store.writer() as connection:
            connection.execute(
                "INSERT INTO outbox_delivery_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    command.message.outbox_event_id,
                    command.attempt_number,
                    "ACKNOWLEDGED" if acknowledged else "DELIVERY_FAILED",
                    result_json,
                    _digest(result_json),
                    now,
                ),
            )
            operation = connection.execute(
                _OPERATION_SELECT
                + " WHERE o.project_id = ? AND o.mode IS NOT NULL "
                "ORDER BY o.reserved_at DESC LIMIT 1",
                (command.message.project_id,),
            ).fetchone()
            if operation is None:
                raise WorkflowError("LEDGER_INTEGRITY", "Outbox Event has no Operation")
            if acknowledged:
                acknowledgement_json = _canonical_json(
                    {
                        "attempt_id": attempt_id,
                        "logical_outbox_identity": command.message.logical_outbox_identity,
                        "transport_receipt_id": result["transport_receipt_id"],
                    }
                )
                connection.execute(
                    "INSERT INTO outbox_acknowledgements VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _stable_id("outbox-ack", command.message.outbox_event_id),
                        attempt_id,
                        command.message.outbox_event_id,
                        acknowledgement_json,
                        _digest(acknowledgement_json),
                        result["transport_receipt_id"],
                        now,
                    ),
                )
                return _result(
                    operation, _binding(operation, "operation_"), "OUTBOX_DELIVERED"
                )
        raise WorkflowError("OUTBOX_DELIVERY_FAILED", "scripted transport was not acknowledged")

    @staticmethod
    def _approval_request(operation: sqlite3.Row) -> dict[str, Any]:
        payload = _operation_payload(operation)
        return {
            "complete_revision_payload": {
                "action_envelope_digest": payload["action_envelope_digest"],
                "action_envelope_id": operation["action_envelope_id"],
                "exact_spend_cap": payload["exact_spend_cap"],
                "expected_target_version": payload["expected_target_version"],
                "expires_at": operation["approval_expires_at"],
                "intent_binding": payload["intent_binding"],
                "operation_digest": operation["operation_digest"],
                "operation_id": operation["operation_id"],
                "project_id": operation["project_id"],
                "side_effect_class": payload["side_effect_class"],
                "target_identity": payload["target_identity"],
            },
            "decision_kind": "APPROVE_EXACT_OPERATION",
            "scope": "EXACT_OPERATION",
        }


_OPERATION_SELECT = (
    "SELECT o.*, o.constitution_revision AS operation_constitution_revision, "
    "o.goal_revision AS operation_goal_revision, "
    "o.operating_profile_revision AS operation_operating_profile_revision, "
    "o.active_intent_digest AS operation_active_intent_digest, "
    "e.action_id, e.action_envelope_digest, e.predecessor_action_envelope_id, "
    "a.action_kind, mi.invocation_id AS matt_invocation_id, "
    "mi.invocation_digest, mr.receipt_id AS matt_receipt_id, "
    "mr.receipt_digest AS matt_receipt_digest FROM operation_records AS o "
    "JOIN action_envelopes AS e ON e.action_envelope_id = o.action_envelope_id "
    "JOIN actions AS a ON a.action_id = e.action_id "
    "LEFT JOIN matt_invocations AS mi ON mi.action_envelope_id = e.action_envelope_id "
    "LEFT JOIN matt_receipts AS mr ON mr.action_envelope_id = e.action_envelope_id"
)
_TERMINAL_OPERATION_SELECT = _OPERATION_SELECT.replace(
    "SELECT o.*", "SELECT terminal.recorded_at AS terminal_recorded_at, o.*"
)


def _current(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT i.* FROM active_intent_current AS c JOIN active_intents AS i "
        "ON i.project_id = c.project_id AND i.intent_number = c.intent_number "
        "WHERE c.project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise WorkflowError("PROJECT_NOT_FOUND", "workflow project does not exist")
    return row


def _operation_by_id(connection: sqlite3.Connection, operation_id: object) -> sqlite3.Row:
    if not isinstance(operation_id, str):
        raise WorkflowError("INVALID_APPROVAL", "approval Operation identity is invalid")
    row = connection.execute(
        _OPERATION_SELECT + " WHERE o.operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None or row["mode"] is None:
        raise WorkflowError("INVALID_APPROVAL", "approval Operation does not exist")
    _operation_payload(row)
    return row


def _operation_payload(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    payload = _verified_json(row["operation_json"], row["operation_digest"], "Operation")
    required = {
        "action_envelope_digest",
        "approval_required",
        "exact_spend_cap",
        "expected_target_version",
        "idempotency_identity",
        "intent_binding",
        "mode",
        "operation_id",
        "physical_apply_authorized",
        "side_effect_class",
        "target_identity",
    }
    if (
        set(payload) != required
        or payload["operation_id"] != row["operation_id"]
        or payload["action_envelope_digest"] != row["action_envelope_digest"]
        or payload["physical_apply_authorized"] is not False
        or payload["mode"] != row["mode"]
        or _canonical_json(payload["target_identity"]) != row["target_identity_json"]
        or _digest(row["target_identity_json"]) != row["target_identity_digest"]
        or payload["expected_target_version"] != row["expected_target_version"]
        or payload["side_effect_class"] != row["side_effect_class"]
        or payload["idempotency_identity"] != row["idempotency_identity"]
        or _canonical_json(payload["exact_spend_cap"]) != row["exact_spend_cap_json"]
        or _digest(row["exact_spend_cap_json"]) != row["exact_spend_cap_digest"]
        or int(payload["approval_required"]) != row["approval_required"]
        or _binding(payload) != _binding(row, "operation_")
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "Operation fields are inconsistent")
    return payload


def _events(connection: sqlite3.Connection, operation: sqlite3.Row) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT * FROM operation_events WHERE operation_id = ? ORDER BY event_number",
        (operation["operation_id"],),
    ).fetchall()
    types = []
    binding = _binding(operation, "operation_")
    for number, row in enumerate(rows, 1):
        payload = _verified_json(row["payload_json"], row["payload_digest"], "Operation event")
        if (
            row["event_number"] != number
            or payload["event_type"] != row["event_type"]
            or payload["operation_digest"] != operation["operation_digest"]
            or _binding(payload) != binding
            or _binding(row) != binding
            or not isinstance(row["recorded_at"], str)
            or not row["recorded_at"]
        ):
            raise WorkflowError("LEDGER_INTEGRITY", "Operation event fields are inconsistent")
        types.append(row["event_type"])
    result = tuple(types)
    in_progress = {
        ("AWAITING_APPROVAL",),
        ("AWAITING_APPROVAL", "PREPARED"),
        ("PREPARED",),
        ("PREPARED", "ATTEMPT_INTENT"),
        ("PREPARED", "ATTEMPT_INTENT", "ATTEMPT_RETURNED"),
        ("PREPARED", "ATTEMPT_INTENT", "READBACK_RECORDED"),
        ("PREPARED", "READBACK_RECORDED"),
        ("AWAITING_APPROVAL", "PREPARED", "READBACK_RECORDED"),
        ("AWAITING_APPROVAL", "PREPARED", "ATTEMPT_INTENT"),
        ("AWAITING_APPROVAL", "PREPARED", "ATTEMPT_INTENT", "READBACK_RECORDED"),
        ("AWAITING_APPROVAL", "PREPARED", "ATTEMPT_INTENT", "ATTEMPT_RETURNED"),
        ("PREPARED", "ATTEMPT_INTENT", "ATTEMPT_RETURNED", "READBACK_RECORDED"),
        (
            "AWAITING_APPROVAL",
            "PREPARED",
            "ATTEMPT_INTENT",
            "ATTEMPT_RETURNED",
            "READBACK_RECORDED",
        ),
    }
    readback_histories = {
        history for history in in_progress if history[-1] == "READBACK_RECORDED"
    }
    valid = result in in_progress or (
        bool(result) and result[-1] in _TERMINAL and result[:-1] in readback_histories
    )
    if not valid:
        raise WorkflowError("LEDGER_INTEGRITY", "Operation lifecycle is invalid")
    return result


def _append_event(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    binding: IntentBinding,
    event_type: str,
    recorded_at: str,
) -> None:
    number = connection.execute(
        "SELECT COUNT(*) + 1 FROM operation_events WHERE operation_id = ?",
        (operation["operation_id"],),
    ).fetchone()[0]
    event = {
        "event_type": event_type,
        "intent_binding": asdict(binding),
        "operation_digest": _digest(_canonical_json(operation)),
    }
    event_json = _canonical_json(event)
    connection.execute(
        "INSERT INTO operation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation["operation_id"],
            number,
            event_type,
            event_json,
            _digest(event_json),
            binding.constitution_revision,
            binding.goal_revision,
            binding.operating_profile_revision,
            binding.active_intent_digest,
            recorded_at,
        ),
    )


def _append_evidence(
    connection: sqlite3.Connection,
    operation: sqlite3.Row,
    kind: str,
    evidence: Mapping[str, Any],
    recorded_at: str,
) -> str:
    number = connection.execute(
        "SELECT COUNT(*) + 1 FROM operation_evidence WHERE operation_id = ?",
        (operation["operation_id"],),
    ).fetchone()[0]
    payload = {
        "evidence": dict(evidence),
        "evidence_kind": kind,
        "operation_digest": operation["operation_digest"],
        "operation_id": operation["operation_id"],
    }
    evidence_json = _canonical_json(payload)
    evidence_digest = _digest(evidence_json)
    connection.execute(
        "INSERT INTO operation_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _stable_id("operation-evidence", f"{operation['operation_id']}:{kind}"),
            operation["operation_id"],
            number,
            kind,
            evidence_json,
            evidence_digest,
            recorded_at,
        ),
    )
    return evidence_digest


def _evidence(connection: sqlite3.Connection, operation_id: str, kind: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM operation_evidence WHERE operation_id = ? AND evidence_kind = ?",
        (operation_id, kind),
    ).fetchone()
    if row is None:
        raise WorkflowError("LEDGER_INTEGRITY", "Operation evidence is missing")
    _verified_json(row["evidence_json"], row["evidence_digest"], "Operation evidence")
    return row


def _frozen_operation(row: sqlite3.Row) -> FrozenOperation:
    payload = _operation_payload(row)
    return FrozenOperation(
        operation_id=row["operation_id"],
        operation_digest=row["operation_digest"],
        project_id=row["project_id"],
        action_envelope_digest=row["action_envelope_digest"],
        target_identity=MappingProxyType(payload["target_identity"]),
        expected_target_version=payload["expected_target_version"],
        side_effect_class=payload["side_effect_class"],
        exact_spend_cap=MappingProxyType(payload["exact_spend_cap"]),
        idempotency_identity=payload["idempotency_identity"],
        mode=payload["mode"],
    )


def _probe(operation: FrozenOperation) -> EffectProbe:
    return EffectProbe(
        operation_id=operation.operation_id,
        operation_digest=operation.operation_digest,
        target_identity=operation.target_identity,
        expected_target_version=operation.expected_target_version,
        idempotency_identity=operation.idempotency_identity,
    )


def _result(
    row: Mapping[str, Any] | sqlite3.Row, binding: IntentBinding, outcome: str
) -> AdvanceResult:
    return AdvanceResult(
        project_id=row["project_id"],
        outcome=outcome,
        intent_binding=binding,
        action_id=row["action_id"],
        action_envelope_id=row["action_envelope_id"],
        action_envelope_digest=row["action_envelope_digest"],
        predecessor_action_envelope_id=row["predecessor_action_envelope_id"],
        operation_id=row["operation_id"],
        operation_digest=row["operation_digest"],
        action_class="cognitive" if row["action_kind"] == "GOAL_WORK" else "mechanical",
        matt_invocation_id=row["matt_invocation_id"],
        matt_invocation_digest=row["invocation_digest"],
        matt_receipt_id=row["matt_receipt_id"],
        matt_receipt_digest=row["matt_receipt_digest"],
    )


def _binding(row: Mapping[str, Any] | sqlite3.Row, prefix: str = "") -> IntentBinding:
    if "intent_binding" in row:
        value = row["intent_binding"]
        if isinstance(value, Mapping):
            return IntentBinding(**value)
    return IntentBinding(
        constitution_revision=row[f"{prefix}constitution_revision"],
        goal_revision=row[f"{prefix}goal_revision"],
        operating_profile_revision=row[f"{prefix}operating_profile_revision"],
        active_intent_digest=row[f"{prefix}active_intent_digest"],
    )


def _freeze_policy(value: object) -> dict[str, Any]:
    policy = _freeze_mapping(value, "Operation policy")
    required = {
        "approval_required",
        "approval_expires_at",
        "exact_spend_cap",
        "expected_target_version",
        "side_effect_class",
        "target_identity",
    }
    if (
        set(policy) != required
        or type(policy["approval_required"]) is not bool
        or not isinstance(policy["approval_expires_at"], str)
        or not isinstance(policy["exact_spend_cap"], dict)
        or not isinstance(policy["target_identity"], dict)
        or any(
            not isinstance(policy[field], str) or not policy[field]
            for field in ("expected_target_version", "side_effect_class")
        )
    ):
        raise WorkflowError("INVALID_OPERATION_POLICY", "Operation policy is incomplete")
    _timestamp(policy["approval_expires_at"], "approval expiry", policy_error=True)
    return policy


def _validate_approval_identity(event: UserDecision) -> None:
    if (
        event.decision_kind != "APPROVE_EXACT_OPERATION"
        or event.scope != "EXACT_OPERATION"
        or any(
            not isinstance(value, str) or not value
            for value in (
                event.project_id,
                event.source,
                event.source_event_id,
                event.authenticated_actor,
                event.verbatim_text,
                event.nonce,
                event.replay_identity,
            )
        )
        or not isinstance(event.provenance, Mapping)
        or not isinstance(event.complete_revision_payload, Mapping)
    ):
        raise WorkflowError("INVALID_APPROVAL", "approval identity or scope is invalid")


def _stored_receipt(row: sqlite3.Row) -> RecordReceipt:
    payload = _verified_json(row["receipt_json"], row["receipt_digest"], "record receipt")
    try:
        return RecordReceipt(**payload)
    except TypeError as error:
        raise WorkflowError("LEDGER_INTEGRITY", "record receipt schema is invalid") from error


def _freeze_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowError("INVALID_EFFECT_RESULT", f"{name} must be an object")
    try:
        return json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError) as error:
        raise WorkflowError("INVALID_EFFECT_RESULT", f"{name} is not canonical JSON") from error


def _verified_json(payload_json: str, digest: str, name: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError) as error:
        raise WorkflowError("LEDGER_INTEGRITY", f"{name} is not valid JSON") from error
    if (
        not isinstance(payload, dict)
        or _canonical_json(payload) != payload_json
        or _digest(payload_json) != digest
    ):
        raise WorkflowError("LEDGER_INTEGRITY", f"{name} digest mismatch")
    return payload


def _local_day(timestamp: str) -> str:
    return _timestamp(timestamp, "Clock").astimezone(_LOCAL_TIMEZONE).date().isoformat()


def _timestamp(value: str, name: str, *, policy_error: bool = False) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        code = "INVALID_OPERATION_POLICY" if policy_error else "INVALID_TIME"
        raise WorkflowError(code, f"{name} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        code = "INVALID_OPERATION_POLICY" if policy_error else "INVALID_TIME"
        raise WorkflowError(code, f"{name} timestamp must include an offset")
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stable_id(kind: str, identity: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentic-workflow:{kind}:{identity}"))
