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
class CapabilitySnapshot:
    snapshot_id: str
    project_id: str
    adapter_id: str
    observed_at: str
    accepted_at: str
    expires_at: str
    provenance: JsonObject
    candidates: tuple[JsonObject, ...]


@dataclass(frozen=True)
class CapabilityRequest:
    project_id: str
    action_id: str
    action_kind: str
    objective: str
    acceptance: tuple[str, ...]
    constraints: tuple[str, ...]
    intent_binding: IntentBinding
    requested_at: str


@dataclass(frozen=True)
class RouteRequest:
    project_id: str
    action_id: str
    action_kind: str
    objective: str
    acceptance: tuple[str, ...]
    constraints: tuple[str, ...]
    intent_binding: IntentBinding
    matrix_id: str
    matrix_version: int
    matrix_digest: str
    candidates: tuple[JsonObject, ...]


@dataclass(frozen=True)
class RoutePlan:
    plan_id: str
    mode: str
    matrix_digest: str
    fallback: str
    requested: JsonObject
    required_capabilities: JsonObject
    target_identity: JsonObject
    budget: JsonObject
    exact_candidate_digest: str | None
    allowed_candidate_digests: tuple[str, ...]
    approved_watchdog_digests: tuple[str, ...]


@dataclass(frozen=True)
class WatchdogAuthority:
    authority_id: str
    attestor_id: str
    provenance: JsonObject


@dataclass(frozen=True)
class HandoffSourceContext:
    exact_base_sha: str
    expected_merge_base: str
    expected_remote_version: str
    owned_paths: tuple[str, ...]
    tool_policy: JsonObject
    test_profile: JsonObject


@dataclass(frozen=True)
class HandoffRetryCommand:
    command_id: str
    project_id: str
    handoff_id: str
    expected_attempt_number: int
    max_attempts: int
    reason: str


@dataclass(frozen=True)
class HandoffPackage:
    handoff_id: str
    handoff_package_digest: str
    project_id: str
    action_id: str
    action_envelope_id: str
    action_envelope_digest: str
    route_envelope_digest: str
    invocation_digest: str
    attempt_id: str
    run_id: str
    delivery_id: str
    idempotency_key: str
    fencing_epoch: int
    executor_id: str
    expires_at: str
    parent_handoff_id: str | None
    intent_binding: IntentBinding
    payload: JsonObject


@dataclass(frozen=True)
class RouteExecutionAttestation:
    handoff_package_digest: str
    candidate_digest: str
    requested_route: JsonObject
    actual_route: JsonObject
    requested_parent_identity: JsonObject
    actual_parent_identity: JsonObject
    requested_subagent_identity: JsonObject
    actual_subagent_identity: JsonObject
    usage: JsonObject
    telemetry_provenance: JsonObject
    watchdog_proof: JsonObject | None = None


@dataclass(frozen=True)
class HandoffExecutionAttestation:
    acceptance: JsonObject
    route: RouteExecutionAttestation
    matt: JsonObject


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
    capability_matrix_digest: str | None = None
    route_envelope_digest: str | None = None
    handoff_id: str | None = None
    handoff_package_digest: str | None = None
    attempt_id: str | None = None
    run_id: str | None = None
    route_receipt_id: str | None = None
    route_receipt_digest: str | None = None


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
