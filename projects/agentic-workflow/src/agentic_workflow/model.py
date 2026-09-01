"""Public value objects for the workflow kernel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

JsonObject = Mapping[str, Any]


@dataclass(frozen=True)
class UserDecision:
    project_id: str
    source: str
    source_event_id: str
    authenticated_actor: str
    scope: str
    verbatim_text: str
    nonce: str
    replay_identity: str
    provenance: JsonObject
    decision_kind: str
    complete_revision_payload: JsonObject


@dataclass(frozen=True)
class RecordReceipt:
    receipt_id: str
    project_id: str
    event_type: str
    outcome: str
    event_digest: str
    active_intent_digest: str | None
    recorded_at: str


@dataclass(frozen=True)
class IntentBinding:
    constitution_revision: int
    goal_revision: int
    operating_profile_revision: int
    active_intent_digest: str


@dataclass(frozen=True)
class AdvanceResult:
    project_id: str
    outcome: str
    intent_binding: IntentBinding
    action_id: str
    action_envelope_id: str
    action_envelope_digest: str
    predecessor_action_envelope_id: str | None
    operation_id: str | None
    operation_digest: str | None
    action_class: str = "cognitive"
    matt_invocation_id: str | None = None
    matt_invocation_digest: str | None = None
    matt_receipt_id: str | None = None
    matt_receipt_digest: str | None = None


@dataclass(frozen=True)
class ProjectView:
    current_goal: dict[str, Any]
    daily_brief: dict[str, Any]
    pending_decisions: tuple[dict[str, Any], ...]


class WorkflowError(Exception):
    """A fail-closed public kernel error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
