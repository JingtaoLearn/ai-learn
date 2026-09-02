from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pytest

import agentic_workflow
from agentic_workflow import UserDecision, WorkflowError, WorkflowKernel
from agentic_workflow.model import (
    CapabilitySnapshot,
    HandoffDeliveryCommand,
    HandoffExecutionAttestation,
    HandoffPackage,
    HandoffRetryCommand,
    HandoffSourceContext,
    RouteExecutionAttestation,
    RoutePlan,
    WatchdogAuthority,
)


def test_connector_types_and_controls_are_not_public_api() -> None:
    assert agentic_workflow.__all__ == [
        "WorkflowKernel",
        "UserDecision",
        "RecordReceipt",
        "AdvanceResult",
        "ProjectView",
        "IntentBinding",
        "WorkflowError",
    ]
    assert list(inspect.signature(WorkflowKernel).parameters) == [
        "database_path",
        "decision_authenticator",
        "external_effects",
        "clock",
    ]
    assert {
        name
        for name, member in inspect.getmembers(WorkflowKernel, predicate=callable)
        if not name.startswith("_")
    } == {"advance", "record", "view"}


PROFILE = json.loads(
    (Path(__file__).parents[1] / "config" / "operating-profile.v1.json").read_text()
)
NOW = "2026-09-02T02:00:00+00:00"
EXPIRES = "2026-09-02T03:00:00+00:00"
DIMENSIONS = ("provider", "model", "reasoning", "context", "tools")
CLAIMS = DIMENSIONS + ("usage", "parent_identity", "subagent_identity")
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "tool_calls", "elapsed_ms")
ROUTE = {
    "provider": "copilot",
    "model": "gpt-5.6-sol",
    "reasoning": "xhigh",
    "context": "long_context",
    "tools": ["read_file", "terminal"],
}
LIMITS = {
    "input_tokens": 8_000,
    "output_tokens": 4_000,
    "total_tokens": 12_000,
    "tool_calls": 8,
    "elapsed_ms": 120_000,
}
USAGE = {
    "input_tokens": 3_000,
    "output_tokens": 1_000,
    "total_tokens": 4_000,
    "tool_calls": 3,
    "elapsed_ms": 20_000,
}


class _Invocation(Protocol):
    invocation_digest: str
    expected_artifact: str
    executor_id: str
    run_id: str
    skill_name: str
    skill_digest: str
    gates: tuple[str, ...]


SOURCE_CONTEXT = HandoffSourceContext(
    exact_base_sha="a" * 40,
    expected_merge_base="b" * 40,
    expected_remote_version="origin/main@" + "c" * 40,
    owned_paths=("src/agentic_workflow/routing.py",),
    tool_policy={"allowed": ["read_file", "terminal"], "network": "forbid"},
    test_profile={"commands": ["python3.12 -m pytest -q"], "required": True},
)
WATCHDOG_AUTHORITY = WatchdogAuthority(
    authority_id="watchdog-policy-1",
    attestor_id="trusted-watchdog-1",
    provenance={"proof_kind": "TRUSTED_WATCHDOG_POLICY", "issuer": "platform-policy"},
)
WATCHDOG_AUTHORITY_PAYLOAD = {
    "authority_id": WATCHDOG_AUTHORITY.authority_id,
    "attestor_id": WATCHDOG_AUTHORITY.attestor_id,
    "provenance": dict(WATCHDOG_AUTHORITY.provenance),
}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def reseal_route_attestation(
    route: RouteExecutionAttestation,
    **changes: object,
) -> RouteExecutionAttestation:
    changed = replace(route, **changes)
    route_fields = {
        "handoff_package_digest": changed.handoff_package_digest,
        "candidate_digest": changed.candidate_digest,
        "requested_route": dict(changed.requested_route),
        "actual_route": dict(changed.actual_route),
        "requested_parent_identity": dict(changed.requested_parent_identity),
        "actual_parent_identity": dict(changed.actual_parent_identity),
        "requested_subagent_identity": dict(changed.requested_subagent_identity),
        "actual_subagent_identity": dict(changed.actual_subagent_identity),
        "usage": dict(changed.usage),
    }
    provenance = dict(changed.telemetry_provenance)
    provenance["payload_digest"] = digest(route_fields)
    return replace(changed, telemetry_provenance=provenance)


WATCHDOG_DIGEST = digest(WATCHDOG_AUTHORITY_PAYLOAD)


class FixedClock:
    def now(self) -> str:
        return NOW


class MutableClock:
    def __init__(self, current: str = NOW) -> None:
        self.current = current

    def now(self) -> str:
        return self.current


class SequenceClock:
    def __init__(self, values: list[str]) -> None:
        self.values = values
        self.last = values[-1]

    def now(self) -> str:
        return self.values.pop(0) if self.values else self.last


class ActorAuthenticator:
    def authenticate(self, decision: UserDecision) -> bool:
        return decision.authenticated_actor == "user-1" and decision.provenance == {
            "channel": "test"
        }


def decision() -> UserDecision:
    return UserDecision(
        project_id="project-1",
        source="test-ui",
        source_event_id="bootstrap-route-1",
        authenticated_actor="user-1",
        scope="PROJECT_INTENT",
        verbatim_text="Create routed workflow.",
        nonce="nonce-route-1",
        replay_identity="bootstrap-route-project-1",
        provenance={"channel": "test"},
        decision_kind="BOOTSTRAP_PROJECT",
        complete_revision_payload={
            "project": {"name": "Routed Workflow"},
            "constitution": {
                "user_sovereignty": True,
                "external_effects_require_authority": True,
            },
            "goal": {
                "outcome": "Produce a bounded routed result",
                "scope": "ticket-214",
                "success_evidence": ["attested route receipt"],
                "constraints": ["no automatic merge or deploy"],
                "accepted_tradeoffs": [],
                "non_goals": ["automatic merge", "automatic deploy"],
            },
            "operating_profile": deepcopy(PROFILE),
        },
    )


def candidate(**route_changes: object) -> dict[str, object]:
    return {
        "candidate_id": "candidate-1",
        "route": ROUTE | route_changes,
        "control": {claim: True for claim in CLAIMS},
        "attestation": {claim: True for claim in CLAIMS},
        "budget_enforcement": {"kind": "hard", "limits": deepcopy(LIMITS)},
    }


def candidate_digest(value: dict[str, object]) -> str:
    return digest(value)


class ScriptedExternalEffects:
    adapter_id = "trusted-route-adapter"
    executor_id = "trusted-subagent-executor"

    def __init__(
        self,
        selected: dict[str, object] | None = None,
        *,
        source_context: HandoffSourceContext | None = SOURCE_CONTEXT,
        watchdog_authorities: tuple[WatchdogAuthority, ...] = (),
        watchdog_proof_verifier: object | None = None,
        max_route_attempts: int = 1,
    ) -> None:
        self.selected = selected or candidate()
        self.source_context = source_context
        self.watchdog_authorities = watchdog_authorities
        self.watchdog_proof_verifier = watchdog_proof_verifier
        self.max_route_attempts = max_route_attempts
        self.capability_requests: list[object] = []
        self.route_requests: list[object] = []
        self.handoffs: list[HandoffPackage] = []
        self.controls: list[HandoffDeliveryCommand | HandoffRetryCommand] = []

    def attempt(self, invocation: _Invocation) -> object:
        artifact = {
            "artifact_type": invocation.expected_artifact,
            "result": "bounded routed implementation",
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

    def queue_control(self, control: HandoffDeliveryCommand | HandoffRetryCommand) -> None:
        self.controls.append(control)

    def _next_handoff_control(
        self, _project_id: str
    ) -> HandoffDeliveryCommand | HandoffRetryCommand | None:
        return self.controls.pop(0) if self.controls else None

    def observe_capabilities(self, request: object) -> CapabilitySnapshot:
        self.capability_requests.append(request)
        return CapabilitySnapshot(
            snapshot_id="snapshot-1",
            project_id="project-1",
            adapter_id=self.adapter_id,
            observed_at=NOW,
            accepted_at=NOW,
            expires_at=EXPIRES,
            provenance={
                "proof_kind": "AUTHENTICATED_CAPABILITY_OBSERVATION",
                "adapter_id": self.adapter_id,
                "evidence_digest": digest({"observation": "snapshot-1"}),
            },
            candidates=(deepcopy(self.selected),),
        )

    def plan_route(self, request: object) -> RoutePlan:
        self.route_requests.append(request)
        matrix_digest = request.matrix_digest
        return RoutePlan(
            plan_id="plan-1",
            mode="exact",
            matrix_digest=matrix_digest,
            fallback="forbid",
            requested=deepcopy(ROUTE),
            required_capabilities={},
            target_identity={
                "parent_executor_id": "workflow-kernel",
                "subagent_executor_id": self.executor_id,
            },
            budget=deepcopy(LIMITS),
            exact_candidate_digest=candidate_digest(self.selected),
            allowed_candidate_digests=(),
            approved_watchdog_digests=(),
        )

    def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
        assert isinstance(handoff, HandoffPackage)
        self.handoffs.append(handoff)
        matt = self.attempt(invocation)
        parent = {
            "executor_id": "workflow-kernel",
            "run_id": handoff.action_id,
        }
        subagent = {
            "executor_id": self.executor_id,
            "run_id": handoff.run_id,
        }
        requested_route = deepcopy(self.selected["route"])
        route_fields = {
            "handoff_package_digest": handoff.handoff_package_digest,
            "candidate_digest": candidate_digest(self.selected),
            "requested_route": requested_route,
            "actual_route": deepcopy(requested_route),
            "requested_parent_identity": parent,
            "actual_parent_identity": deepcopy(parent),
            "requested_subagent_identity": subagent,
            "actual_subagent_identity": deepcopy(subagent),
            "usage": deepcopy(USAGE),
        }
        observed_paths = [
            *(f"actual_route.{dimension}" for dimension in DIMENSIONS),
            "actual_parent_identity.executor_id",
            "actual_parent_identity.run_id",
            "actual_subagent_identity.executor_id",
            "actual_subagent_identity.run_id",
            *(f"usage.{field}" for field in USAGE_FIELDS),
        ]
        route = RouteExecutionAttestation(
            **route_fields,
            telemetry_provenance={
                "proof_kind": "AUTHENTICATED_ROUTE_TELEMETRY",
                "adapter_id": self.adapter_id,
                "observed_paths": observed_paths,
                "payload_digest": digest(route_fields),
            },
        )
        return HandoffExecutionAttestation(
            acceptance={
                "status": "ACCEPTED",
                "handoff_package_digest": handoff.handoff_package_digest,
                "delivery_id": handoff.delivery_id,
                "idempotency_key": handoff.idempotency_key,
            },
            route=route,
            matt=matt,
        )


def make_kernel(
    database_path: Path,
    adapter: ScriptedExternalEffects,
    *,
    watchdog_authorities: tuple[WatchdogAuthority, ...] = (),
    watchdog_proof_verifier: object | None = None,
    max_route_attempts: int = 1,
    clock: object | None = None,
) -> WorkflowKernel:
    adapter.watchdog_authorities = watchdog_authorities
    adapter.watchdog_proof_verifier = watchdog_proof_verifier
    adapter.max_route_attempts = max_route_attempts
    return WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
        external_effects=adapter,
        clock=clock or FixedClock(),
    )


def test_accepted_unexpired_snapshot_becomes_versioned_matrix_and_frozen_route_envelope(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "control.sqlite3"
    adapter = ScriptedExternalEffects()
    kernel = make_kernel(database_path, adapter)
    kernel.record(decision())

    result = kernel.advance("project-1")

    assert result.outcome == "ACTION_ENVELOPED"
    assert result.capability_matrix_digest
    assert result.route_envelope_digest
    assert len(adapter.capability_requests) == 1
    assert len(adapter.route_requests) == 1
    with sqlite3.connect(database_path) as connection:
        snapshot_json, snapshot_digest = connection.execute(
            "SELECT snapshot_json, snapshot_digest FROM capability_snapshots"
        ).fetchone()
        matrix_json, matrix_digest, version = connection.execute(
            "SELECT matrix_json, matrix_digest, matrix_version FROM capability_matrices"
        ).fetchone()
        plan_json, plan_digest = connection.execute(
            "SELECT plan_json, plan_digest FROM route_plans"
        ).fetchone()
        envelope_json = connection.execute("SELECT envelope_json FROM action_envelopes").fetchone()[
            0
        ]

    snapshot = json.loads(snapshot_json)
    matrix = json.loads(matrix_json)
    plan = json.loads(plan_json)
    envelope = json.loads(envelope_json)
    assert snapshot_digest == digest(snapshot)
    assert snapshot["accepted_at"] == NOW
    assert snapshot["expires_at"] == EXPIRES
    assert snapshot["provenance"]["proof_kind"] == "AUTHENTICATED_CAPABILITY_OBSERVATION"
    assert version == 1
    assert matrix_digest == digest(matrix) == result.capability_matrix_digest
    assert matrix["snapshot_digests"] == [snapshot_digest]
    assert matrix["candidates"][0]["candidate_digest"] == candidate_digest(adapter.selected)
    assert plan_digest == digest(plan)
    assert plan["matrix_digest"] == matrix_digest
    assert envelope["route"]["plan_digest"] == plan_digest
    assert envelope["route"]["matrix_digest"] == matrix_digest
    assert envelope["route"]["route_envelope_digest"] == result.route_envelope_digest
    assert envelope["route"]["fallback"] == "forbid"


@pytest.mark.parametrize(
    ("accepted_at", "expires_at"),
    [("2026-09-02T02:00:01+00:00", EXPIRES), (NOW, NOW)],
    ids=["not-yet-accepted", "expired"],
)
def test_unaccepted_or_expired_snapshot_never_enters_a_matrix(
    tmp_path: Path, accepted_at: str, expires_at: str
) -> None:
    class InvalidSnapshotAdapter(ScriptedExternalEffects):
        def observe_capabilities(self, request: object) -> CapabilitySnapshot:
            snapshot = super().observe_capabilities(request)
            return replace(snapshot, accepted_at=accepted_at, expires_at=expires_at)

    database_path = tmp_path / "control.sqlite3"
    kernel = make_kernel(database_path, InvalidSnapshotAdapter())
    kernel.record(decision())

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "CAPABILITY_SNAPSHOT_REJECTED"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM capability_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM capability_matrices").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


@pytest.mark.parametrize("claim", CLAIMS)
@pytest.mark.parametrize("claim_group", ["control", "attestation"])
def test_exact_route_requires_every_control_and_attestation_claim(
    tmp_path: Path, claim_group: str, claim: str
) -> None:
    selected = candidate()
    selected[claim_group][claim] = False  # type: ignore[index]
    database_path = tmp_path / f"{claim_group}-{claim}.sqlite3"
    kernel = make_kernel(database_path, ScriptedExternalEffects(selected))
    kernel.record(decision())

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "ROUTE_PLAN_REJECTED"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_plans").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM action_envelopes").fetchone()[0] == 0


def test_exact_route_forbids_fallback_and_soft_or_none_budget_enforcement(tmp_path: Path) -> None:
    for variant in ("fallback", "soft", "none"):
        selected = candidate()

        class IneligibleAdapter(ScriptedExternalEffects):
            _variant = variant

            def plan_route(self, request: object) -> RoutePlan:
                plan = super().plan_route(request)
                return replace(plan, fallback="allow") if self._variant == "fallback" else plan

        if variant != "fallback":
            selected["budget_enforcement"] = {
                "kind": variant,
                "limits": deepcopy(LIMITS),
            }
        database_path = tmp_path / f"{variant}.sqlite3"
        kernel = make_kernel(database_path, IneligibleAdapter(selected))
        kernel.record(decision())

        with pytest.raises(WorkflowError) as caught:
            kernel.advance("project-1")

        assert caught.value.code == "ROUTE_PLAN_REJECTED"


class CapabilityClassAdapter(ScriptedExternalEffects):
    def plan_route(self, request: object) -> RoutePlan:
        self.route_requests.append(request)
        return RoutePlan(
            plan_id="plan-capability-class",
            mode="capability_class",
            matrix_digest=request.matrix_digest,
            fallback="forbid",
            requested={},
            required_capabilities={
                "providers": ["copilot"],
                "contexts": ["long_context"],
                "tools": ["read_file"],
            },
            target_identity={
                "parent_executor_id": "workflow-kernel",
                "subagent_executor_id": self.executor_id,
            },
            budget=deepcopy(LIMITS),
            exact_candidate_digest=None,
            allowed_candidate_digests=(candidate_digest(self.selected),),
            approved_watchdog_digests=(),
        )


def test_capability_class_freezes_only_digest_pinned_candidates_with_specific_limits(
    tmp_path: Path,
) -> None:
    selected = candidate(model="gpt-5.4-mini", reasoning="high")
    selected["budget_enforcement"] = {
        "kind": "hard",
        "limits": {**deepcopy(LIMITS), "total_tokens": 6_000},
    }
    database_path = tmp_path / "control.sqlite3"
    kernel = make_kernel(database_path, CapabilityClassAdapter(selected))
    kernel.record(decision())

    result = kernel.advance("project-1")

    assert result.route_envelope_digest
    with sqlite3.connect(database_path) as connection:
        envelope = json.loads(
            connection.execute("SELECT envelope_json FROM action_envelopes").fetchone()[0]
        )
    route = envelope["route"]
    selected_digest = candidate_digest(selected)
    assert route["allowed_candidate_digests"] == [selected_digest]
    assert route["candidate_limits"] == {selected_digest: selected["budget_enforcement"]}
    assert route["requested"] == {}
    assert route["required_capabilities"]["tools"] == ["read_file"]


def test_capability_class_rejects_unpinned_or_looser_candidate_limits(tmp_path: Path) -> None:
    for variant in ("unpinned", "too-loose"):
        selected = candidate(model="gpt-5.4-mini", reasoning="high")
        if variant == "too-loose":
            selected["budget_enforcement"] = {
                "kind": "hard",
                "limits": {**deepcopy(LIMITS), "total_tokens": LIMITS["total_tokens"] + 1},
            }

        class InvalidCapabilityClassAdapter(CapabilityClassAdapter):
            _variant = variant

            def plan_route(self, request: object) -> RoutePlan:
                plan = super().plan_route(request)
                if self._variant == "unpinned":
                    return replace(plan, allowed_candidate_digests=("0" * 64,))
                return plan

        database_path = tmp_path / f"{variant}.sqlite3"
        kernel = make_kernel(database_path, InvalidCapabilityClassAdapter(selected))
        kernel.record(decision())

        with pytest.raises(WorkflowError) as caught:
            kernel.advance("project-1")

        assert caught.value.code == "ROUTE_PLAN_REJECTED"


def test_freezes_handoff_before_dispatch_and_persists_attested_route_receipt(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "control.sqlite3"
    adapter = ScriptedExternalEffects()
    kernel = make_kernel(database_path, adapter)
    bootstrap = kernel.record(decision())
    envelope = kernel.advance("project-1")

    result = kernel.advance("project-1")

    assert result.outcome == "OPERATION_RESERVED"
    assert result.handoff_id
    assert result.handoff_package_digest
    assert result.attempt_id
    assert result.run_id
    assert result.route_receipt_id
    assert result.route_receipt_digest
    assert len(adapter.handoffs) == 1
    dispatched = adapter.handoffs[0]
    assert dispatched.handoff_id == result.handoff_id
    assert dispatched.handoff_package_digest == result.handoff_package_digest
    assert dispatched.attempt_id == result.attempt_id
    assert dispatched.run_id == result.run_id
    assert dispatched.action_envelope_digest == envelope.action_envelope_digest
    assert dispatched.intent_binding.active_intent_digest == bootstrap.active_intent_digest

    with sqlite3.connect(database_path) as connection:
        package_json, package_digest = connection.execute(
            "SELECT package_json, handoff_package_digest FROM handoffs"
        ).fetchone()
        events = connection.execute(
            "SELECT event_type FROM handoff_events ORDER BY event_number"
        ).fetchall()
        receipt_json, receipt_digest = connection.execute(
            "SELECT receipt_json, receipt_digest FROM route_receipts"
        ).fetchone()
    package = json.loads(package_json)
    receipt = json.loads(receipt_json)
    unsigned_package = dict(package)
    assert unsigned_package.pop("handoff_package_digest") == package_digest
    assert digest(unsigned_package) == package_digest
    assert package["intent_binding"]["active_intent_digest"] == bootstrap.active_intent_digest
    assert package["action_envelope_digest"] == envelope.action_envelope_digest
    assert package["route_envelope_digest"] == envelope.route_envelope_digest
    assert package["matt_invocation_digest"] == result.matt_invocation_digest
    assert package["attempt_id"] == result.attempt_id
    assert package["run_id"] == result.run_id
    assert package["delivery_id"]
    assert package["idempotency_key"]
    assert package["fencing_epoch"] == 1
    assert package["executor_id"] == adapter.executor_id
    assert package["expires_at"] == EXPIRES
    assert package["limits"]["route_budget"] == LIMITS
    assert package["acceptance"] == ["attested route receipt"]
    assert package["source_identity"] == {
        "exact_base_sha": "a" * 40,
        "expected_merge_base": "b" * 40,
        "expected_remote_version": "origin/main@" + "c" * 40,
        "owned_paths": ["src/agentic_workflow/routing.py"],
        "non_goals": ["automatic merge", "automatic deploy"],
    }
    assert package["tool_policy"] == SOURCE_CONTEXT.tool_policy
    assert package["test_profile"] == SOURCE_CONTEXT.test_profile
    assert package["side_effect_policy"] == "NO_AUTOMATIC_MERGE_OR_DEPLOY"
    assert events == [
        ("OFFERED",),
        ("ACCEPTED",),
        ("RUNNING",),
        ("RESULT_RECORDED",),
        ("VERIFIED",),
    ]
    assert receipt_digest == result.route_receipt_digest == digest(receipt)
    assert receipt["requested_route"] == ROUTE
    assert receipt["actual_route"] == ROUTE
    assert receipt["requested_parent_identity"] == receipt["actual_parent_identity"]
    assert receipt["requested_subagent_identity"] == receipt["actual_subagent_identity"]
    assert receipt["handoff_id"] == result.handoff_id
    assert receipt["attempt_id"] == result.attempt_id
    assert receipt["run_id"] == result.run_id
    assert receipt["action_envelope_digest"] == envelope.action_envelope_digest
    assert receipt["intent_binding"]["active_intent_digest"] == bootstrap.active_intent_digest


def test_routed_handoff_fails_closed_without_authoritative_source_context(tmp_path: Path) -> None:
    adapter = ScriptedExternalEffects(source_context=None)
    kernel = WorkflowKernel(
        tmp_path / "control.sqlite3",
        decision_authenticator=ActorAuthenticator(),
        external_effects=adapter,
        clock=FixedClock(),
    )
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "SOURCE_CONTEXT_UNAVAILABLE"
    assert adapter.handoffs == []


class WatchdogAdapter(ScriptedExternalEffects):
    def __init__(self, *, proof_variant: str = "valid", planner_approves: bool = False) -> None:
        selected = candidate()
        selected["budget_enforcement"] = {
            "kind": "external_watchdog",
            "limits": deepcopy(LIMITS),
            "watchdog_digest": WATCHDOG_DIGEST,
        }
        super().__init__(selected)
        self.proof_variant = proof_variant
        self.planner_approves = planner_approves

    def plan_route(self, request: object) -> RoutePlan:
        plan = super().plan_route(request)
        return replace(
            plan,
            approved_watchdog_digests=(WATCHDOG_DIGEST,) if self.planner_approves else (),
        )

    def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
        assert isinstance(handoff, HandoffPackage)
        attestation = super().dispatch(handoff, invocation)
        if self.proof_variant == "prose":
            proof = {"worker_summary": "trust me"}
        else:
            claims = {
                "schema_version": 1,
                "proof_kind": "AUTHENTICATED_WATCHDOG_ENFORCEMENT",
                "watchdog_digest": WATCHDOG_DIGEST,
                "candidate_digest": candidate_digest(self.selected),
                "limits": deepcopy(LIMITS),
                "usage": deepcopy(USAGE),
                "enforcement_outcome": "WITHIN_LIMITS",
                "attestor_id": WATCHDOG_AUTHORITY.attestor_id,
                "authority_provenance": dict(WATCHDOG_AUTHORITY.provenance),
                "route_envelope_digest": handoff.route_envelope_digest,
                "handoff_id": handoff.handoff_id,
                "handoff_package_digest": handoff.handoff_package_digest,
                "run_id": handoff.run_id,
            }
            proof = {**claims, "proof_digest": digest(claims)}
            if self.proof_variant == "wrong-run":
                proof["run_id"] = "worker-selected-run"
        return replace(attestation, route=replace(attestation.route, watchdog_proof=proof))


class IndependentWatchdogVerifier:
    verifier_id = "trusted-watchdog-verifier-1"
    provenance = {
        "proof_kind": "TRUSTED_WATCHDOG_VERIFIER",
        "issuer": "platform-security",
    }

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[dict[str, object], dict[str, object], dict[str, object]]] = []

    def verify(
        self,
        *,
        authority: object,
        expected_claims: object,
        proof: object,
    ) -> bool:
        authority_copy = json.loads(canonical(authority))
        claims_copy = json.loads(canonical(expected_claims))
        proof_copy = json.loads(canonical(proof))
        self.calls.append((authority_copy, claims_copy, proof_copy))
        return self.accept and proof_copy == {
            **claims_copy,
            "proof_digest": digest(claims_copy),
        }


def test_route_planner_cannot_self_approve_watchdog_policy(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path / "control.sqlite3", WatchdogAdapter(planner_approves=True))
    kernel.record(decision())

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "ROUTE_PLAN_REJECTED"


def test_injected_watchdog_authority_and_canonical_proof_are_required(tmp_path: Path) -> None:
    for variant, authorities, expected in (
        ("valid", (WATCHDOG_AUTHORITY,), None),
        ("valid", (), "ROUTE_PLAN_REJECTED"),
        ("prose", (WATCHDOG_AUTHORITY,), "ROUTE_RECEIPT_REJECTED"),
        ("wrong-run", (WATCHDOG_AUTHORITY,), "ROUTE_RECEIPT_REJECTED"),
    ):
        adapter = WatchdogAdapter(proof_variant=variant)
        kernel = make_kernel(
            tmp_path / f"{variant}-{len(authorities)}.sqlite3",
            adapter,
            watchdog_authorities=authorities,
            watchdog_proof_verifier=IndependentWatchdogVerifier(),
        )
        kernel.record(decision())
        if expected == "ROUTE_PLAN_REJECTED":
            with pytest.raises(WorkflowError) as caught:
                kernel.advance("project-1")
            assert caught.value.code == expected
            continue
        kernel.advance("project-1")
        if expected is None:
            result = kernel.advance("project-1")
            assert result.route_receipt_id
        else:
            with pytest.raises(WorkflowError) as caught:
                kernel.advance("project-1")
            assert caught.value.code == expected


def test_route_adapter_cannot_self_authenticate_a_fabricated_watchdog_proof(
    tmp_path: Path,
) -> None:
    verifier = IndependentWatchdogVerifier(accept=False)
    adapter = WatchdogAdapter()
    database_path = tmp_path / "malicious-watchdog.sqlite3"
    kernel = make_kernel(
        database_path,
        adapter,
        watchdog_authorities=(WATCHDOG_AUTHORITY,),
        watchdog_proof_verifier=verifier,
    )
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "ROUTE_RECEIPT_REJECTED"
    assert len(verifier.calls) == 1
    assert len(adapter.handoffs) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_receipts").fetchone()[0] == 0


def test_route_adapter_object_cannot_also_be_the_watchdog_verifier(tmp_path: Path) -> None:
    class SelfVerifyingAdapter(WatchdogAdapter):
        verifier_id = "apparently-independent-verifier"
        provenance = {
            "proof_kind": "TRUSTED_WATCHDOG_VERIFIER",
            "issuer": "same-route-adapter-object",
        }

        def verify(self, **_kwargs: object) -> bool:
            return True

    adapter = SelfVerifyingAdapter()
    with pytest.raises(WorkflowError) as caught:
        make_kernel(
            tmp_path / "same-object-watchdog.sqlite3",
            adapter,
            watchdog_authorities=(WATCHDOG_AUTHORITY,),
            watchdog_proof_verifier=adapter,
        )

    assert caught.value.code == "INVALID_WATCHDOG_VERIFIER"


def test_independent_watchdog_verifier_accepts_and_is_persisted_in_receipt(
    tmp_path: Path,
) -> None:
    verifier = IndependentWatchdogVerifier()
    database_path = tmp_path / "verified-watchdog.sqlite3"
    kernel = make_kernel(
        database_path,
        WatchdogAdapter(),
        watchdog_authorities=(WATCHDOG_AUTHORITY,),
        watchdog_proof_verifier=verifier,
    )
    kernel.record(decision())
    kernel.advance("project-1")

    result = kernel.advance("project-1")

    assert result.route_receipt_id
    assert len(verifier.calls) == 1
    with sqlite3.connect(database_path) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM route_receipts").fetchone()[0]
        )
    assert receipt["watchdog_verification"] == {
        "authority_id": WATCHDOG_AUTHORITY.authority_id,
        "authority_provenance": WATCHDOG_AUTHORITY.provenance,
        "attestor_id": WATCHDOG_AUTHORITY.attestor_id,
        "verifier_id": verifier.verifier_id,
        "verifier_provenance": verifier.provenance,
        "watchdog_digest": WATCHDOG_DIGEST,
    }


def test_external_watchdog_route_without_independent_verifier_fails_closed(tmp_path: Path) -> None:
    kernel = make_kernel(
        tmp_path / "missing-watchdog-verifier.sqlite3",
        WatchdogAdapter(),
        watchdog_authorities=(WATCHDOG_AUTHORITY,),
    )
    kernel.record(decision())

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "WATCHDOG_VERIFIER_UNAVAILABLE"


def test_expiry_is_enforced_before_claim_and_before_dispatch(tmp_path: Path) -> None:
    for phase in ("claim", "dispatch"):
        adapter = ScriptedExternalEffects()
        clock: MutableClock | SequenceClock = (
            MutableClock() if phase == "claim" else SequenceClock([NOW, NOW, NOW, NOW, EXPIRES])
        )
        database_path = tmp_path / f"{phase}.sqlite3"
        kernel = make_kernel(database_path, adapter, clock=clock)
        kernel.record(decision())
        kernel.advance("project-1")
        if isinstance(clock, MutableClock):
            clock.current = EXPIRES

        with pytest.raises(WorkflowError) as caught:
            kernel.advance("project-1")

        assert caught.value.code == "ROUTE_EXPIRED"
        assert adapter.handoffs == []
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM route_receipts").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM operation_records").fetchone()[0] == 0


def test_result_completing_after_expiry_is_rejected_without_receipt_or_operation(
    tmp_path: Path,
) -> None:
    clock = MutableClock()

    class ExpiringResultAdapter(ScriptedExternalEffects):
        def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
            assert isinstance(handoff, HandoffPackage)
            result = super().dispatch(handoff, invocation)
            clock.current = EXPIRES
            return result

    database_path = tmp_path / "control.sqlite3"
    kernel = make_kernel(database_path, ExpiringResultAdapter(), clock=clock)
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "ROUTE_EXPIRED"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operation_records").fetchone()[0] == 0
        assert connection.execute(
            "SELECT event_type FROM handoff_events ORDER BY event_number"
        ).fetchall() == [("OFFERED",), ("ACCEPTED",), ("RUNNING",), ("EXPIRED",)]


class FailFirstDispatchAdapter(ScriptedExternalEffects):
    def __init__(self) -> None:
        super().__init__()
        self.dispatch_calls = 0

    def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
        assert isinstance(handoff, HandoffPackage)
        self.dispatch_calls += 1
        if self.dispatch_calls == 1:
            self.handoffs.append(handoff)
            raise RuntimeError("response lost")
        return super().dispatch(handoff, invocation)


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterRunningAdapter(ScriptedExternalEffects):
    def __init__(self) -> None:
        super().__init__()
        self.dispatch_calls = 0

    def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
        assert isinstance(handoff, HandoffPackage)
        self.dispatch_calls += 1
        self.handoffs.append(handoff)
        raise SimulatedProcessCrash


def prepare_undispatched_handoff_chain(
    database_path: Path,
    through_event: str,
) -> None:
    adapter = ScriptedExternalEffects()
    clock = SequenceClock([NOW, NOW, NOW, EXPIRES])
    kernel = make_kernel(database_path, adapter, clock=clock)
    kernel.record(decision())
    kernel.advance("project-1")
    with pytest.raises(WorkflowError) as expired:
        kernel.advance("project-1")
    assert expired.value.code == "ROUTE_EXPIRED"
    assert adapter.handoffs == []

    if through_event == "OFFERED":
        return
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        offered = connection.execute("SELECT * FROM handoff_events").fetchone()
        assert offered is not None
        binding = {
            "constitution_revision": offered["constitution_revision"],
            "goal_revision": offered["goal_revision"],
            "operating_profile_revision": offered["operating_profile_revision"],
            "active_intent_digest": offered["active_intent_digest"],
        }
        for event_number, event_type in ((2, "ACCEPTED"), (3, "RUNNING")):
            payload = {
                "event_number": event_number,
                "event_type": event_type,
                "handoff_id": offered["handoff_id"],
                "handoff_package_digest": json.loads(offered["event_json"])[
                    "handoff_package_digest"
                ],
                "intent_binding": binding,
                "recorded_at": NOW,
            }
            event_json = canonical(payload)
            connection.execute(
                "INSERT INTO handoff_events "
                "(handoff_id, event_number, event_type, event_json, event_digest, "
                "constitution_revision, goal_revision, operating_profile_revision, "
                "active_intent_digest, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    offered["handoff_id"],
                    event_number,
                    event_type,
                    event_json,
                    hashlib.sha256(event_json.encode()).hexdigest(),
                    *binding.values(),
                    NOW,
                ),
            )
            if event_type == through_event:
                break


def tamper_handoff_event(connection: sqlite3.Connection, variant: str) -> None:
    if variant.startswith("payload-") or variant == "noncanonical":
        event_type, field = {
            "payload-package": ("OFFERED", "handoff_package_digest"),
            "payload-handoff": ("OFFERED", "handoff_id"),
            "payload-binding": ("ACCEPTED", "intent_binding"),
            "noncanonical": ("RUNNING", None),
        }[variant]
        event_json = connection.execute(
            "SELECT event_json FROM handoff_events WHERE event_type = ?", (event_type,)
        ).fetchone()[0]
        payload = json.loads(event_json)
        if field == "intent_binding":
            payload[field]["constitution_revision"] += 1
        elif field is not None:
            payload[field] = "0" * 64 if field.endswith("digest") else "other-handoff"
        resealed = canonical(payload) + (" " if variant == "noncanonical" else "")
        connection.execute(
            "UPDATE handoff_events SET event_json = ?, event_digest = ? WHERE event_type = ?",
            (resealed, hashlib.sha256(resealed.encode()).hexdigest(), event_type),
        )
    elif variant == "indexed-binding":
        connection.execute(
            "UPDATE handoff_events SET constitution_revision = constitution_revision + 1 "
            "WHERE event_type = 'ACCEPTED'"
        )
    elif variant == "indexed-handoff":
        connection.execute(
            "UPDATE handoff_events SET handoff_id = 'other-handoff' WHERE event_type = 'ACCEPTED'"
        )
    elif variant == "digest":
        connection.execute(
            "UPDATE handoff_events SET event_digest = ? WHERE event_type = 'RUNNING'",
            ("0" * 64,),
        )
    elif variant == "event-type":
        connection.execute(
            "UPDATE handoff_events SET event_type = 'FAILED' WHERE event_type = 'RUNNING'"
        )
    elif variant == "recorded-at":
        connection.execute(
            "UPDATE handoff_events SET recorded_at = ? WHERE event_type = 'RUNNING'",
            (EXPIRES,),
        )
    elif variant == "order":
        connection.execute(
            "UPDATE handoff_events SET event_number = 99 WHERE event_type = 'ACCEPTED'"
        )
        connection.execute(
            "UPDATE handoff_events SET event_number = 2 WHERE event_type = 'RUNNING'"
        )
        connection.execute(
            "UPDATE handoff_events SET event_number = 3 WHERE event_type = 'ACCEPTED'"
        )
    else:  # pragma: no cover - test parameter guard
        raise AssertionError(f"unknown Handoff event tamper variant: {variant}")


def reseal_handoff_event_timestamp(
    connection: sqlite3.Connection,
    event_type: str,
    recorded_at: str,
) -> None:
    event_json = connection.execute(
        "SELECT event_json FROM handoff_events WHERE event_type = ?", (event_type,)
    ).fetchone()[0]
    payload = json.loads(event_json)
    payload["recorded_at"] = recorded_at
    resealed = canonical(payload)
    updated = connection.execute(
        "UPDATE handoff_events SET recorded_at = ?, event_json = ?, event_digest = ? "
        "WHERE event_type = ?",
        (recorded_at, resealed, hashlib.sha256(resealed.encode()).hexdigest(), event_type),
    )
    assert updated.rowcount == 1


def assert_resealed_verified_event_timestamps_rejected(
    database_path: Path,
    mutations: tuple[tuple[str, str], ...],
) -> None:
    adapter = ScriptedExternalEffects()
    kernel = make_kernel(database_path, adapter)
    kernel.record(decision())
    kernel.advance("project-1")
    original = kernel.advance("project-1")
    handoff = adapter.handoffs[0]

    with sqlite3.connect(database_path) as connection:
        before = connection.execute(
            "SELECT (SELECT COUNT(*) FROM handoffs), (SELECT COUNT(*) FROM handoff_events), "
            "(SELECT COUNT(*) FROM handoff_retry_commands), (SELECT COUNT(*) FROM attempts), "
            "(SELECT COUNT(*) FROM runs), (SELECT COUNT(*) FROM route_receipts), "
            "(SELECT COUNT(*) FROM operation_records)"
        ).fetchone()
        connection.execute("DROP TRIGGER handoff_events_no_update")
        for event_type, recorded_at in mutations:
            reseal_handoff_event_timestamp(connection, event_type, recorded_at)

    adapter.queue_control(
        HandoffDeliveryCommand(
            project_id="project-1",
            delivery_id=handoff.delivery_id,
            idempotency_key=handoff.idempotency_key,
        )
    )
    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"
    assert len(adapter.handoffs) == 1
    with sqlite3.connect(database_path) as connection:
        after = connection.execute(
            "SELECT (SELECT COUNT(*) FROM handoffs), (SELECT COUNT(*) FROM handoff_events), "
            "(SELECT COUNT(*) FROM handoff_retry_commands), (SELECT COUNT(*) FROM attempts), "
            "(SELECT COUNT(*) FROM runs), (SELECT COUNT(*) FROM route_receipts), "
            "(SELECT COUNT(*) FROM operation_records)"
        ).fetchone()
    assert after == before
    assert original.route_receipt_id


@pytest.mark.parametrize(
    ("through_event", "variant"),
    [
        ("OFFERED", "payload-package"),
        ("OFFERED", "payload-handoff"),
        ("RUNNING", "payload-binding"),
        ("RUNNING", "indexed-binding"),
        ("RUNNING", "indexed-handoff"),
        ("RUNNING", "digest"),
        ("RUNNING", "event-type"),
        ("RUNNING", "recorded-at"),
        ("RUNNING", "order"),
        ("RUNNING", "noncanonical"),
    ],
)
def test_advance_rejects_tampered_existing_handoff_event_chain_before_dispatch(
    tmp_path: Path,
    through_event: str,
    variant: str,
) -> None:
    database_path = tmp_path / f"{through_event.lower()}-{variant}.sqlite3"
    prepare_undispatched_handoff_chain(database_path, through_event)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER handoff_events_no_update")
        tamper_handoff_event(connection, variant)

    adapter = ScriptedExternalEffects()
    restarted = make_kernel(database_path, adapter)
    with pytest.raises(WorkflowError) as caught:
        restarted.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"
    assert adapter.handoffs == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operation_records").fetchone()[0] == 0


@pytest.mark.parametrize(
    "recorded_at",
    [
        "not-a-timestamp",
        "2026-09-02T02:00:00",
        "2026-09-02T10:00:00+08:00",
        "2026-09-02T02:00:00Z",
    ],
    ids=["malformed", "naive", "non-utc", "noncanonical-utc"],
)
def test_duplicate_advance_rejects_resealed_noncanonical_handoff_event_timestamp(
    tmp_path: Path,
    recorded_at: str,
) -> None:
    assert_resealed_verified_event_timestamps_rejected(
        tmp_path / f"event-time-{recorded_at[-5:]}.sqlite3",
        (("VERIFIED", recorded_at),),
    )


def test_duplicate_advance_rejects_resealed_handoff_event_before_previous_event(
    tmp_path: Path,
) -> None:
    assert_resealed_verified_event_timestamps_rejected(
        tmp_path / "event-time-regression.sqlite3",
        (("RUNNING", "2026-09-02T01:59:59+00:00"),),
    )


def test_duplicate_advance_rejects_resealed_offered_event_time_different_from_handoff(
    tmp_path: Path,
) -> None:
    assert_resealed_verified_event_timestamps_rejected(
        tmp_path / "offered-time-mismatch.sqlite3",
        (("OFFERED", "2026-09-02T01:59:59+00:00"),),
    )


@pytest.mark.parametrize(
    ("first_terminal_event", "mutated_events"),
    [
        ("RESULT_RECORDED", ("RESULT_RECORDED", "VERIFIED")),
        ("VERIFIED", ("VERIFIED",)),
    ],
)
def test_duplicate_advance_rejects_resealed_terminal_event_time_different_from_receipt(
    tmp_path: Path,
    first_terminal_event: str,
    mutated_events: tuple[str, ...],
) -> None:
    assert_resealed_verified_event_timestamps_rejected(
        tmp_path / f"{first_terminal_event.lower()}-time-mismatch.sqlite3",
        tuple((event_type, EXPIRES) for event_type in mutated_events),
    )


class RejectFirstAttestationAdapter(ScriptedExternalEffects):
    def __init__(self, rejection: str) -> None:
        super().__init__()
        self.rejection = rejection
        self.dispatch_calls = 0

    def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
        self.dispatch_calls += 1
        attestation = super().dispatch(handoff, invocation)
        if self.dispatch_calls == 1 and self.rejection == "route":
            actual = dict(attestation.route.actual_route)
            actual["model"] = "drifted-after-dispatch"
            return replace(
                attestation,
                route=reseal_route_attestation(attestation.route, actual_route=actual),
            )
        if self.dispatch_calls == 1:
            return replace(
                attestation,
                matt={**attestation.matt, "executor_id": "drifted-matt-executor"},
            )
        return attestation


class AttestationMutationAdapter(ScriptedExternalEffects):
    def __init__(
        self,
        mutation: Callable[
            [HandoffExecutionAttestation, HandoffPackage, _Invocation],
            HandoffExecutionAttestation,
        ],
        selected: dict[str, object] | None = None,
    ) -> None:
        super().__init__(selected)
        self.mutation = mutation

    def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
        assert isinstance(handoff, HandoffPackage)
        attestation = super().dispatch(handoff, invocation)
        return self.mutation(attestation, handoff, invocation)


class LedgerDriftAdapter(ScriptedExternalEffects):
    def __init__(self, database_path: Path, artifact: str) -> None:
        super().__init__()
        self.database_path = database_path
        self.artifact = artifact

    def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
        attestation = super().dispatch(handoff, invocation)
        table, trigger, column = {
            "snapshot": (
                "capability_snapshots",
                "capability_snapshots_no_update",
                "snapshot_json",
            ),
            "matrix": (
                "capability_matrices",
                "capability_matrices_no_update",
                "matrix_json",
            ),
            "plan": ("route_plans", "route_plans_no_update", "plan_json"),
            "route": (
                "action_envelopes",
                "action_envelopes_no_update",
                "envelope_json",
            ),
        }[self.artifact]
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute(f"UPDATE {table} SET {column} = {column} || ' '")
        return attestation


def assert_post_dispatch_rejected(
    database_path: Path,
    adapter: ScriptedExternalEffects,
    *,
    expected_code: str = "ROUTE_RECEIPT_REJECTED",
) -> dict[str, object]:
    kernel = make_kernel(database_path, adapter, max_route_attempts=2)
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == expected_code
    with sqlite3.connect(database_path) as connection:
        events = connection.execute(
            "SELECT event_type, event_json FROM handoff_events ORDER BY event_number"
        ).fetchall()
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM route_executor_attestations), "
            "(SELECT COUNT(*) FROM route_receipts), "
            "(SELECT COUNT(*) FROM matt_executor_attestations), "
            "(SELECT COUNT(*) FROM matt_receipts), "
            "(SELECT COUNT(*) FROM operation_records)"
        ).fetchone()
    assert [row[0] for row in events] == ["OFFERED", "ACCEPTED", "RUNNING", "FAILED"]
    assert counts == (0, 0, 0, 0, 0)
    failure = json.loads(events[-1][1])["failure"]
    assert failure["error_code"] == expected_code
    if expected_code in {"ROUTE_RECEIPT_REJECTED", "MATT_RECEIPT_REJECTED"}:
        with pytest.raises(WorkflowError) as retry_required:
            kernel.advance("project-1")
        assert retry_required.value.code == "HANDOFF_RETRY_REQUIRED"
        assert len(adapter.handoffs) == 1
    return failure


def test_duplicate_delivery_returns_original_receipt_without_new_attempt(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    adapter = ScriptedExternalEffects()
    kernel = make_kernel(database_path, adapter)
    kernel.record(decision())
    kernel.advance("project-1")
    original = kernel.advance("project-1")
    handoff = adapter.handoffs[0]

    adapter.queue_control(
        HandoffDeliveryCommand(
            project_id="project-1",
            delivery_id=handoff.delivery_id,
            idempotency_key=handoff.idempotency_key,
        )
    )
    duplicate = kernel.advance("project-1")

    assert duplicate.route_receipt_id == original.route_receipt_id
    assert duplicate.route_receipt_digest == original.route_receipt_digest
    assert duplicate.attempt_id == original.attempt_id
    assert duplicate.run_id == original.run_id
    assert len(adapter.handoffs) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_restart_recovers_running_handoff_as_ambiguous_once_without_redispatch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "running-crash.sqlite3"
    crashing = CrashAfterRunningAdapter()
    kernel = make_kernel(database_path, crashing, max_route_attempts=2)
    kernel.record(decision())
    kernel.advance("project-1")

    with pytest.raises(SimulatedProcessCrash):
        kernel.advance("project-1")
    original = crashing.handoffs[0]

    restarted_adapter = ScriptedExternalEffects()
    restarted = make_kernel(database_path, restarted_adapter, max_route_attempts=2)
    for _ in range(2):
        with pytest.raises(WorkflowError) as caught:
            restarted.advance("project-1")
        assert caught.value.code == "HANDOFF_RETRY_REQUIRED"
        assert restarted_adapter.handoffs == []

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT event_type FROM handoff_events ORDER BY event_number"
        ).fetchall() == [
            ("OFFERED",),
            ("ACCEPTED",),
            ("RUNNING",),
            ("AMBIGUOUS",),
        ]
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    restarted_adapter.queue_control(
        HandoffRetryCommand(
            command_id="retry-ambiguous-command-1",
            project_id="project-1",
            handoff_id=original.handoff_id,
            expected_attempt_number=1,
            max_attempts=2,
            reason="original external effect outcome is unknown after process crash",
        )
    )
    retried = restarted.advance("project-1")

    assert retried.outcome == "OPERATION_RESERVED"
    assert retried.attempt_id != original.attempt_id
    assert len(restarted_adapter.handoffs) == 1


def test_explicit_bounded_retry_creates_new_attempt_run_and_invocation(tmp_path: Path) -> None:
    database_path = tmp_path / "control.sqlite3"
    adapter = FailFirstDispatchAdapter()
    kernel = make_kernel(database_path, adapter, max_route_attempts=2)
    kernel.record(decision())
    envelope = kernel.advance("project-1")

    with pytest.raises(WorkflowError) as failed:
        kernel.advance("project-1")
    assert failed.value.code == "ROUTE_EXECUTION_FAILED"
    first = adapter.handoffs[0]

    with pytest.raises(WorkflowError) as implicit:
        kernel.advance("project-1")
    assert implicit.value.code == "HANDOFF_RETRY_REQUIRED"
    assert adapter.dispatch_calls == 1

    command = HandoffRetryCommand(
        command_id="retry-command-1",
        project_id="project-1",
        handoff_id=first.handoff_id,
        expected_attempt_number=1,
        max_attempts=2,
        reason="durable dispatch failed before receipt",
    )
    adapter.queue_control(command)
    retried = kernel.advance("project-1")

    assert retried.outcome == "OPERATION_RESERVED"
    assert retried.action_id == envelope.action_id
    assert retried.attempt_id != first.attempt_id
    assert retried.run_id != first.run_id
    second = adapter.handoffs[1]
    assert second.parent_handoff_id == first.handoff_id
    assert second.invocation_digest != first.invocation_digest
    assert second.fencing_epoch == 2

    adapter.queue_control(command)
    duplicate_command = kernel.advance("project-1")
    assert duplicate_command.route_receipt_id == retried.route_receipt_id
    assert adapter.dispatch_calls == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM matt_invocations").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("rejection", "expected_code"),
    [
        ("route", "ROUTE_RECEIPT_REJECTED"),
        ("matt", "MATT_RECEIPT_REJECTED"),
    ],
)
def test_rejected_attestation_is_durable_and_requires_explicit_bounded_retry(
    tmp_path: Path,
    rejection: str,
    expected_code: str,
) -> None:
    database_path = tmp_path / f"{rejection}.sqlite3"
    adapter = RejectFirstAttestationAdapter(rejection)
    kernel = make_kernel(database_path, adapter, max_route_attempts=2)
    kernel.record(decision())
    envelope = kernel.advance("project-1")

    with pytest.raises(WorkflowError) as rejected:
        kernel.advance("project-1")
    assert rejected.value.code == expected_code
    first = adapter.handoffs[0]

    with sqlite3.connect(database_path) as connection:
        event_rows = connection.execute(
            "SELECT event_type, event_json FROM handoff_events ORDER BY event_number"
        ).fetchall()
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM route_executor_attestations), "
            "(SELECT COUNT(*) FROM route_receipts), "
            "(SELECT COUNT(*) FROM matt_executor_attestations), "
            "(SELECT COUNT(*) FROM matt_receipts), "
            "(SELECT COUNT(*) FROM operation_records)"
        ).fetchone()
    assert [row[0] for row in event_rows] == ["OFFERED", "ACCEPTED", "RUNNING", "FAILED"]
    failure = json.loads(event_rows[-1][1])["failure"]
    assert failure["error_code"] == expected_code
    assert failure["phase"] == "POST_DISPATCH_ATTESTATION_VALIDATION"
    if rejection == "route":
        assert failure["rejected_attestation"]["route"]["actual_route"]["model"] == (
            "drifted-after-dispatch"
        )
        assert (
            failure["rejected_attestation"]["route"]["telemetry_provenance"]["proof_kind"]
            == "AUTHENTICATED_ROUTE_TELEMETRY"
        )
    else:
        assert failure["rejected_attestation"]["matt"]["executor_id"] == ("drifted-matt-executor")
    assert counts == (0, 0, 0, 0, 0)

    with pytest.raises(WorkflowError) as implicit:
        kernel.advance("project-1")
    assert implicit.value.code == "HANDOFF_RETRY_REQUIRED"
    assert adapter.dispatch_calls == 1

    command = HandoffRetryCommand(
        command_id="retry-rejected-command-1",
        project_id="project-1",
        handoff_id=first.handoff_id,
        expected_attempt_number=1,
        max_attempts=2,
        reason="attested actual route was rejected",
    )
    adapter.queue_control(command)
    retried = kernel.advance("project-1")

    assert retried.outcome == "OPERATION_RESERVED"
    assert retried.action_id == envelope.action_id
    assert retried.attempt_id != first.attempt_id
    assert retried.run_id != first.run_id
    second = adapter.handoffs[1]
    assert second.parent_handoff_id == first.handoff_id
    assert second.invocation_digest != first.invocation_digest
    assert second.fencing_epoch == 2

    adapter.queue_control(command)
    duplicate = kernel.advance("project-1")
    assert duplicate.route_receipt_id == retried.route_receipt_id
    assert duplicate.route_receipt_digest == retried.route_receipt_digest
    assert adapter.dispatch_calls == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM matt_invocations").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("dimension", "replacement"),
    [
        ("provider", "other-provider"),
        ("model", "drifted-model"),
        ("reasoning", "low"),
        ("context", "short-context"),
        ("tools", ["read_file"]),
    ],
)
def test_production_seam_rejects_each_requested_route_dimension_drift(
    tmp_path: Path,
    dimension: str,
    replacement: object,
) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        requested = dict(attestation.route.requested_route)
        requested[dimension] = replacement
        return replace(
            attestation,
            route=reseal_route_attestation(attestation.route, requested_route=requested),
        )

    assert_post_dispatch_rejected(
        tmp_path / f"requested-{dimension}.sqlite3",
        AttestationMutationAdapter(mutate),
    )


@pytest.mark.parametrize(
    ("dimension", "replacement"),
    [
        ("provider", "other-provider"),
        ("model", "drifted-model"),
        ("reasoning", "low"),
        ("context", "short-context"),
        ("tools", ["read_file"]),
    ],
)
def test_production_seam_rejects_each_actual_route_dimension_drift(
    tmp_path: Path,
    dimension: str,
    replacement: object,
) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        actual = dict(attestation.route.actual_route)
        actual[dimension] = replacement
        return replace(
            attestation,
            route=reseal_route_attestation(attestation.route, actual_route=actual),
        )

    assert_post_dispatch_rejected(
        tmp_path / f"actual-{dimension}.sqlite3",
        AttestationMutationAdapter(mutate),
    )


@pytest.mark.parametrize(
    ("identity_field", "component"),
    [
        (identity_field, component)
        for identity_field in (
            "requested_parent_identity",
            "actual_parent_identity",
            "requested_subagent_identity",
            "actual_subagent_identity",
        )
        for component in ("executor_id", "run_id")
    ],
)
def test_production_seam_rejects_parent_and_subagent_identity_drift(
    tmp_path: Path,
    identity_field: str,
    component: str,
) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        identity = dict(getattr(attestation.route, identity_field))
        identity[component] = "drifted-identity"
        return replace(
            attestation,
            route=reseal_route_attestation(
                attestation.route,
                **{identity_field: identity},
            ),
        )

    assert_post_dispatch_rejected(
        tmp_path / f"{identity_field}-{component}.sqlite3",
        AttestationMutationAdapter(mutate),
    )


REQUIRED_TELEMETRY_PATHS = (
    *(f"actual_route.{dimension}" for dimension in DIMENSIONS),
    "actual_parent_identity.executor_id",
    "actual_parent_identity.run_id",
    "actual_subagent_identity.executor_id",
    "actual_subagent_identity.run_id",
    *(f"usage.{field}" for field in USAGE_FIELDS),
)


@pytest.mark.parametrize("missing_path", REQUIRED_TELEMETRY_PATHS)
def test_production_seam_rejects_each_missing_telemetry_provenance_path(
    tmp_path: Path,
    missing_path: str,
) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        provenance = dict(attestation.route.telemetry_provenance)
        observed = list(provenance["observed_paths"])
        observed.remove(missing_path)
        provenance["observed_paths"] = observed
        return replace(
            attestation,
            route=replace(attestation.route, telemetry_provenance=provenance),
        )

    assert_post_dispatch_rejected(
        tmp_path / (missing_path.replace(".", "-") + ".sqlite3"),
        AttestationMutationAdapter(mutate),
    )


def test_production_seam_rejects_telemetry_payload_digest_drift(tmp_path: Path) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        provenance = dict(attestation.route.telemetry_provenance)
        provenance["payload_digest"] = "0" * 64
        return replace(
            attestation,
            route=replace(attestation.route, telemetry_provenance=provenance),
        )

    assert_post_dispatch_rejected(
        tmp_path / "telemetry-payload-digest.sqlite3",
        AttestationMutationAdapter(mutate),
    )


@pytest.mark.parametrize("field", USAGE_FIELDS)
def test_production_seam_rejects_each_runtime_budget_overrun(
    tmp_path: Path,
    field: str,
) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        usage = dict(attestation.route.usage)
        usage[field] = LIMITS[field] + 1
        if field in {"input_tokens", "output_tokens"}:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        elif field == "total_tokens":
            usage["input_tokens"] = LIMITS["input_tokens"]
            usage["output_tokens"] = LIMITS["output_tokens"] + 1
        return replace(
            attestation,
            route=reseal_route_attestation(attestation.route, usage=usage),
        )

    assert_post_dispatch_rejected(
        tmp_path / f"budget-{field}.sqlite3",
        AttestationMutationAdapter(mutate),
    )


def test_production_seam_rejects_inconsistent_usage_total(tmp_path: Path) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        usage = dict(attestation.route.usage)
        usage["total_tokens"] += 1
        return replace(
            attestation,
            route=reseal_route_attestation(attestation.route, usage=usage),
        )

    assert_post_dispatch_rejected(
        tmp_path / "usage-total.sqlite3",
        AttestationMutationAdapter(mutate),
    )


def test_production_seam_rejects_candidate_digest_tampering(tmp_path: Path) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        return replace(
            attestation,
            route=reseal_route_attestation(attestation.route, candidate_digest="0" * 64),
        )

    assert_post_dispatch_rejected(
        tmp_path / "candidate.sqlite3",
        AttestationMutationAdapter(mutate),
    )


def test_production_seam_rejects_matrix_candidate_index_tampering(tmp_path: Path) -> None:
    database_path = tmp_path / "candidate-index.sqlite3"

    class CandidateIndexDriftAdapter(ScriptedExternalEffects):
        def dispatch(self, handoff: object, invocation: _Invocation) -> HandoffExecutionAttestation:
            attestation = super().dispatch(handoff, invocation)
            with sqlite3.connect(database_path) as connection:
                matrix_json = connection.execute(
                    "SELECT matrix_json FROM capability_matrices"
                ).fetchone()[0]
                matrix = json.loads(matrix_json)
                matrix["candidates"][0]["candidate_digest"] = "0" * 64
                connection.execute("DROP TRIGGER capability_matrices_no_update")
                connection.execute(
                    "UPDATE capability_matrices SET matrix_json = ?",
                    (canonical(matrix),),
                )
            return attestation

    assert_post_dispatch_rejected(
        database_path,
        CandidateIndexDriftAdapter(),
        expected_code="LEDGER_INTEGRITY",
    )


@pytest.mark.parametrize(
    ("table", "trigger", "column", "replacement"),
    [
        (
            "capability_snapshots",
            "capability_snapshots_no_update",
            "snapshot_id",
            "changed-snapshot",
        ),
        ("capability_snapshots", "capability_snapshots_no_update", "project_id", "changed-project"),
        ("capability_snapshots", "capability_snapshots_no_update", "adapter_id", "changed-adapter"),
        ("capability_matrices", "capability_matrices_no_update", "matrix_id", "changed-matrix"),
        ("capability_matrices", "capability_matrices_no_update", "project_id", "changed-project"),
        ("capability_matrices", "capability_matrices_no_update", "matrix_version", 2),
        (
            "capability_matrix_candidates",
            "capability_matrix_candidates_no_update",
            "candidate_digest",
            "0" * 64,
        ),
        (
            "capability_matrix_candidates",
            "capability_matrix_candidates_no_update",
            "snapshot_digest",
            "0" * 64,
        ),
        ("route_plans", "route_plans_no_update", "plan_id", "changed-plan"),
        ("route_plans", "route_plans_no_update", "project_id", "changed-project"),
        ("route_plans", "route_plans_no_update", "action_id", "changed-action"),
        ("route_plans", "route_plans_no_update", "matrix_digest", "0" * 64),
        (
            "route_envelopes",
            "route_envelopes_no_update",
            "action_envelope_id",
            "changed-action-envelope",
        ),
        ("route_envelopes", "route_envelopes_no_update", "plan_digest", "0" * 64),
        ("route_envelopes", "route_envelopes_no_update", "matrix_digest", "0" * 64),
    ],
)
def test_indexed_route_chain_tampering_fails_integrity_without_dispatch(
    tmp_path: Path,
    table: str,
    trigger: str,
    column: str,
    replacement: object,
) -> None:
    database_path = tmp_path / f"indexed-{table}-{column}.sqlite3"
    adapter = ScriptedExternalEffects()
    kernel = make_kernel(database_path, adapter)
    kernel.record(decision())
    kernel.advance("project-1")

    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET {column} = ?", (replacement,))

    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"
    assert adapter.handoffs == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operation_records").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("table", "trigger", "column", "replacement"),
    [
        ("handoffs", "handoffs_no_update", "action_id", "changed-action"),
        (
            "route_executor_attestations",
            "route_executor_attestations_no_update",
            "project_id",
            "changed-project",
        ),
        (
            "route_receipts",
            "route_receipts_no_update",
            "action_envelope_id",
            "changed-action-envelope",
        ),
    ],
)
def test_indexed_downstream_handoff_and_receipt_tampering_fails_integrity(
    tmp_path: Path,
    table: str,
    trigger: str,
    column: str,
    replacement: object,
) -> None:
    database_path = tmp_path / f"downstream-{table}-{column}.sqlite3"
    adapter = ScriptedExternalEffects()
    kernel = make_kernel(database_path, adapter)
    kernel.record(decision())
    kernel.advance("project-1")
    kernel.advance("project-1")
    handoff = adapter.handoffs[0]

    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET {column} = ?", (replacement,))

    adapter.queue_control(
        HandoffDeliveryCommand(
            project_id="project-1",
            delivery_id=handoff.delivery_id,
            idempotency_key=handoff.idempotency_key,
        )
    )
    with pytest.raises(WorkflowError) as caught:
        kernel.advance("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize("artifact", ["snapshot", "matrix", "plan", "route"])
def test_production_seam_rejects_snapshot_matrix_plan_and_route_digest_drift(
    tmp_path: Path,
    artifact: str,
) -> None:
    database_path = tmp_path / f"{artifact}-digest.sqlite3"
    assert_post_dispatch_rejected(
        database_path,
        LedgerDriftAdapter(database_path, artifact),
        expected_code="LEDGER_INTEGRITY",
    )


@pytest.mark.parametrize(
    "malformation",
    ["missing-usage", "actual-route-type", "route-type", "provenance-type", "acceptance-type"],
)
def test_production_seam_rejects_malformed_attestation_payloads(
    tmp_path: Path,
    malformation: str,
) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        if malformation == "missing-usage":
            return replace(attestation, route=replace(attestation.route, usage={}))
        if malformation == "actual-route-type":
            return replace(attestation, route=replace(attestation.route, actual_route=None))
        if malformation == "route-type":
            return replace(attestation, route=None)
        if malformation == "provenance-type":
            return replace(
                attestation,
                route=replace(attestation.route, telemetry_provenance=None),
            )
        return replace(attestation, acceptance=[])

    assert_post_dispatch_rejected(
        tmp_path / f"malformed-{malformation}.sqlite3",
        AttestationMutationAdapter(mutate),
    )


def test_recorded_requested_parent_route_and_actual_child_drift_fails_closed(
    tmp_path: Path,
) -> None:
    def mutate(
        attestation: HandoffExecutionAttestation,
        _handoff: HandoffPackage,
        _invocation: _Invocation,
    ) -> HandoffExecutionAttestation:
        child_actual = dict(attestation.route.actual_route)
        child_actual.update({"model": "child-model", "reasoning": "child-effort"})
        return replace(
            attestation,
            route=reseal_route_attestation(attestation.route, actual_route=child_actual),
        )

    failure = assert_post_dispatch_rejected(
        tmp_path / "requested-parent-actual-child.sqlite3",
        AttestationMutationAdapter(mutate),
    )
    rejected = failure["rejected_attestation"]
    assert isinstance(rejected, dict)
    rejected_route = rejected["route"]
    assert isinstance(rejected_route, dict)
    assert rejected_route["requested_route"] == ROUTE
    assert rejected_route["actual_route"]["model"] == "child-model"
    assert rejected_route["actual_route"]["reasoning"] == "child-effort"


def test_capability_class_route_accepts_a_digest_pinned_actual_candidate(
    tmp_path: Path,
) -> None:
    selected = candidate(model="gpt-5.4-mini", reasoning="high")
    adapter = CapabilityClassAdapter(selected)
    kernel = make_kernel(tmp_path / "capability-class-receipt.sqlite3", adapter)
    kernel.record(decision())
    kernel.advance("project-1")

    result = kernel.advance("project-1")

    assert result.outcome == "OPERATION_RESERVED"
    assert result.route_receipt_id
    assert result.route_receipt_digest
