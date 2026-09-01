"""Private capability, route, and receipt policy.

Adapters may observe and execute routes, but only this module turns their data
into authority-bearing canonical artifacts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .model import CapabilitySnapshot, RoutePlan, WorkflowError

ROUTE_DIMENSIONS = ("provider", "model", "reasoning", "context", "tools")
ROUTE_CLAIMS = ROUTE_DIMENSIONS + ("usage", "parent_identity", "subagent_identity")
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "tool_calls", "elapsed_ms")


def canonical_json(value: object, *, error_code: str = "ROUTE_REJECTED") -> str:
    _validate_json(value, error_code)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise WorkflowError(error_code, "route artifact is not canonical JSON") from error


def canonical_digest(value: object, *, error_code: str = "ROUTE_REJECTED") -> str:
    return hashlib.sha256(canonical_json(value, error_code=error_code).encode()).hexdigest()


def freeze_capability_snapshot(
    snapshot: object,
    *,
    adapter_id: str,
    project_id: str,
    accepted_now: str,
) -> tuple[dict[str, Any], str]:
    if not isinstance(snapshot, CapabilitySnapshot):
        raise WorkflowError(
            "CAPABILITY_SNAPSHOT_REJECTED", "trusted adapter did not return a Capability Snapshot"
        )
    payload = {
        "schema_version": 1,
        "snapshot_id": snapshot.snapshot_id,
        "project_id": snapshot.project_id,
        "adapter_id": snapshot.adapter_id,
        "observed_at": snapshot.observed_at,
        "accepted_at": snapshot.accepted_at,
        "expires_at": snapshot.expires_at,
        "provenance": dict(snapshot.provenance),
        "candidates": [dict(candidate) for candidate in snapshot.candidates],
    }
    try:
        canonical_json(payload, error_code="CAPABILITY_SNAPSHOT_REJECTED")
    except (TypeError, ValueError) as error:
        raise WorkflowError(
            "CAPABILITY_SNAPSHOT_REJECTED", "Capability Snapshot is not canonical"
        ) from error
    if (
        not _nonempty(snapshot.snapshot_id)
        or snapshot.project_id != project_id
        or snapshot.adapter_id != adapter_id
        or not _nonempty(adapter_id)
    ):
        raise WorkflowError(
            "CAPABILITY_SNAPSHOT_REJECTED", "Capability Snapshot identity is not trusted"
        )
    provenance = payload["provenance"]
    if (
        set(provenance) != {"proof_kind", "adapter_id", "evidence_digest"}
        or provenance.get("proof_kind") != "AUTHENTICATED_CAPABILITY_OBSERVATION"
        or provenance.get("adapter_id") != adapter_id
        or not _is_sha256(provenance.get("evidence_digest"))
    ):
        raise WorkflowError(
            "CAPABILITY_SNAPSHOT_REJECTED", "Capability Snapshot provenance is not authenticated"
        )
    observed_at = _timestamp(snapshot.observed_at, "CAPABILITY_SNAPSHOT_REJECTED")
    accepted_at = _timestamp(snapshot.accepted_at, "CAPABILITY_SNAPSHOT_REJECTED")
    expires_at = _timestamp(snapshot.expires_at, "CAPABILITY_SNAPSHOT_REJECTED")
    now = _timestamp(accepted_now, "CAPABILITY_SNAPSHOT_REJECTED")
    if observed_at > accepted_at or accepted_at > now or now >= expires_at:
        raise WorkflowError(
            "CAPABILITY_SNAPSHOT_REJECTED", "Capability Snapshot is unaccepted or expired"
        )
    if not payload["candidates"]:
        raise WorkflowError("CAPABILITY_SNAPSHOT_REJECTED", "Capability Snapshot has no candidates")
    seen: set[str] = set()
    for candidate in payload["candidates"]:
        _validate_candidate(candidate, "CAPABILITY_SNAPSHOT_REJECTED")
        candidate_digest = canonical_digest(candidate, error_code="CAPABILITY_SNAPSHOT_REJECTED")
        if candidate_digest in seen:
            raise WorkflowError(
                "CAPABILITY_SNAPSHOT_REJECTED", "Capability Snapshot repeats a candidate"
            )
        seen.add(candidate_digest)
    return payload, canonical_digest(payload, error_code="CAPABILITY_SNAPSHOT_REJECTED")


def build_capability_matrix(
    *,
    project_id: str,
    matrix_version: int,
    snapshot_payloads: Sequence[tuple[Mapping[str, Any], str]],
    created_at: str,
) -> tuple[dict[str, Any], str]:
    if type(matrix_version) is not int or matrix_version <= 0 or not snapshot_payloads:
        raise WorkflowError("CAPABILITY_MATRIX_REJECTED", "Capability Matrix input is invalid")
    entries: list[dict[str, Any]] = []
    snapshot_digests: list[str] = []
    now = _timestamp(created_at, "CAPABILITY_MATRIX_REJECTED")
    for snapshot, snapshot_digest in snapshot_payloads:
        if canonical_digest(snapshot, error_code="CAPABILITY_MATRIX_REJECTED") != snapshot_digest:
            raise WorkflowError("CAPABILITY_MATRIX_REJECTED", "snapshot digest changed")
        accepted = _timestamp(snapshot["accepted_at"], "CAPABILITY_MATRIX_REJECTED")
        expires = _timestamp(snapshot["expires_at"], "CAPABILITY_MATRIX_REJECTED")
        if accepted > now or now >= expires:
            raise WorkflowError(
                "CAPABILITY_MATRIX_REJECTED", "only accepted unexpired snapshots enter the Matrix"
            )
        snapshot_digests.append(snapshot_digest)
        for candidate in snapshot["candidates"]:
            candidate_value = dict(candidate)
            entries.append(
                {
                    "candidate": candidate_value,
                    "candidate_digest": canonical_digest(
                        candidate_value, error_code="CAPABILITY_MATRIX_REJECTED"
                    ),
                    "snapshot_digest": snapshot_digest,
                }
            )
    entries.sort(key=lambda entry: entry["candidate_digest"])
    if len({entry["candidate_digest"] for entry in entries}) != len(entries):
        raise WorkflowError("CAPABILITY_MATRIX_REJECTED", "Matrix candidate digest is ambiguous")
    matrix_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"agentic-workflow:capability-matrix:{project_id}:{matrix_version}"
        )
    )
    payload = {
        "schema_version": 1,
        "matrix_id": matrix_id,
        "matrix_version": matrix_version,
        "project_id": project_id,
        "snapshot_digests": sorted(snapshot_digests),
        "candidates": entries,
        "created_at": created_at,
    }
    return payload, canonical_digest(payload, error_code="CAPABILITY_MATRIX_REJECTED")


def freeze_route_plan(
    plan: object, *, project_id: str, action_id: str
) -> tuple[dict[str, Any], str]:
    if not isinstance(plan, RoutePlan):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "trusted router did not return a Route Plan")
    payload = {
        "schema_version": 1,
        "plan_id": plan.plan_id,
        "project_id": project_id,
        "action_id": action_id,
        "mode": plan.mode,
        "matrix_digest": plan.matrix_digest,
        "fallback": plan.fallback,
        "requested": dict(plan.requested),
        "required_capabilities": dict(plan.required_capabilities),
        "target_identity": dict(plan.target_identity),
        "budget": dict(plan.budget),
        "exact_candidate_digest": plan.exact_candidate_digest,
        "allowed_candidate_digests": list(plan.allowed_candidate_digests),
        "approved_watchdog_digests": list(plan.approved_watchdog_digests),
    }
    canonical_json(payload, error_code="ROUTE_PLAN_REJECTED")
    return payload, canonical_digest(payload, error_code="ROUTE_PLAN_REJECTED")


def validate_and_freeze_route_envelope(
    plan: Mapping[str, Any],
    plan_digest: str,
    matrix: Mapping[str, Any],
    matrix_digest: str,
    *,
    route_adapter_executor_id: str,
    watchdog_authorities: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    if canonical_digest(plan, error_code="ROUTE_PLAN_REJECTED") != plan_digest:
        raise WorkflowError("ROUTE_PLAN_REJECTED", "Route Plan digest mismatch")
    if canonical_digest(matrix, error_code="ROUTE_PLAN_REJECTED") != matrix_digest:
        raise WorkflowError("ROUTE_PLAN_REJECTED", "Capability Matrix digest mismatch")
    expected_plan_fields = {
        "schema_version",
        "plan_id",
        "project_id",
        "action_id",
        "mode",
        "matrix_digest",
        "fallback",
        "requested",
        "required_capabilities",
        "target_identity",
        "budget",
        "exact_candidate_digest",
        "allowed_candidate_digests",
        "approved_watchdog_digests",
    }
    if (
        set(plan) != expected_plan_fields
        or plan.get("schema_version") != 1
        or not _nonempty(plan.get("plan_id"))
        or not _nonempty(plan.get("project_id"))
        or not _nonempty(plan.get("action_id"))
        or plan.get("project_id") != matrix.get("project_id")
        or plan.get("matrix_digest") != matrix_digest
        or plan.get("fallback") != "forbid"
        or plan.get("mode") not in {"exact", "capability_class"}
    ):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "Route Plan authority is invalid")
    _validate_limits(plan.get("budget"), "ROUTE_PLAN_REJECTED")
    target = plan.get("target_identity")
    if (
        not isinstance(target, Mapping)
        or set(target) != {"parent_executor_id", "subagent_executor_id"}
        or target.get("parent_executor_id") != "workflow-kernel"
        or target.get("subagent_executor_id") != route_adapter_executor_id
    ):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "Route Plan target identity is invalid")
    approved = plan.get("approved_watchdog_digests")
    allowed = plan.get("allowed_candidate_digests")
    if approved != []:
        raise WorkflowError("ROUTE_PLAN_REJECTED", "Route Plan cannot grant watchdog authority")
    if not _unique_sha_list(allowed, allow_empty=True):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "Route Plan digest allowlist is invalid")
    trusted_watchdogs = _validate_watchdog_authorities(watchdog_authorities)
    candidates = {
        entry["candidate_digest"]: entry["candidate"] for entry in matrix.get("candidates", [])
    }
    selected_digests: list[str]
    if plan["mode"] == "exact":
        _validate_route(plan.get("requested"), "ROUTE_PLAN_REJECTED")
        if plan.get("required_capabilities") != {} or allowed != []:
            raise WorkflowError(
                "ROUTE_PLAN_REJECTED", "exact Route Plan widens candidate authority"
            )
        exact_digest = plan.get("exact_candidate_digest")
        if not _is_sha256(exact_digest) or exact_digest not in candidates:
            raise WorkflowError("ROUTE_PLAN_REJECTED", "exact candidate is not Matrix-pinned")
        selected_digests = [exact_digest]
        if not _routes_equal(plan["requested"], candidates[exact_digest]["route"]):
            raise WorkflowError(
                "ROUTE_PLAN_REJECTED", "exact candidate does not match requested route"
            )
    else:
        if plan.get("requested") != {} or plan.get("exact_candidate_digest") is not None:
            raise WorkflowError(
                "ROUTE_PLAN_REJECTED", "capability-class Route Plan claims exact route"
            )
        _validate_requirements(plan.get("required_capabilities"))
        if not allowed or any(item not in candidates for item in allowed):
            raise WorkflowError(
                "ROUTE_PLAN_REJECTED", "capability-class candidate is not Matrix-pinned"
            )
        selected_digests = list(allowed)
        for candidate_digest in selected_digests:
            if not _meets_requirements(
                candidates[candidate_digest]["route"], plan["required_capabilities"]
            ):
                raise WorkflowError(
                    "ROUTE_PLAN_REJECTED", "capability-class candidate misses a requirement"
                )
    candidate_limits: dict[str, Any] = {}
    for candidate_digest in selected_digests:
        selected = candidates[candidate_digest]
        _require_route_assurance(selected)
        enforcement = selected["budget_enforcement"]
        _validate_enforcement(enforcement, plan["budget"], trusted_watchdogs)
        candidate_limits[candidate_digest] = dict(enforcement)
    route_payload = {
        "schema_version": 1,
        "project_id": plan["project_id"],
        "action_id": plan["action_id"],
        "plan_id": plan["plan_id"],
        "matrix_id": matrix["matrix_id"],
        "plan_digest": plan_digest,
        "matrix_digest": matrix_digest,
        "mode": plan["mode"],
        "fallback": "forbid",
        "requested": dict(plan["requested"]),
        "required_capabilities": dict(plan["required_capabilities"]),
        "target_identity": dict(target),
        "budget": dict(plan["budget"]),
        "exact_candidate_digest": plan["exact_candidate_digest"],
        "allowed_candidate_digests": list(allowed),
        "approved_watchdog_digests": sorted(trusted_watchdogs),
        "watchdog_authorities": trusted_watchdogs,
        "candidate_limits": candidate_limits,
    }
    route_digest = canonical_digest(route_payload, error_code="ROUTE_PLAN_REJECTED")
    route_envelope = {**route_payload, "route_envelope_digest": route_digest}
    return route_envelope, route_digest


def validate_route_envelope(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise WorkflowError("LEDGER_INTEGRITY", "Route Envelope is missing")
    route = dict(payload)
    route_digest = route.pop("route_envelope_digest", None)
    if (
        not _is_sha256(route_digest)
        or canonical_digest(route, error_code="LEDGER_INTEGRITY") != route_digest
    ):
        raise WorkflowError("LEDGER_INTEGRITY", "Route Envelope digest mismatch")
    return route_digest


def _validate_candidate(candidate: object, error_code: str) -> None:
    if not isinstance(candidate, Mapping) or set(candidate) != {
        "candidate_id",
        "route",
        "control",
        "attestation",
        "budget_enforcement",
    }:
        raise WorkflowError(error_code, "candidate schema is invalid")
    if not _nonempty(candidate.get("candidate_id")):
        raise WorkflowError(error_code, "candidate identity is invalid")
    _validate_route(candidate.get("route"), error_code)
    for field in ("control", "attestation"):
        claims = candidate.get(field)
        if (
            not isinstance(claims, Mapping)
            or set(claims) != set(ROUTE_CLAIMS)
            or any(type(value) is not bool for value in claims.values())
        ):
            raise WorkflowError(error_code, f"candidate {field} claims are invalid")
    enforcement = candidate.get("budget_enforcement")
    if not isinstance(enforcement, Mapping):
        raise WorkflowError(error_code, "candidate budget enforcement is invalid")
    kind = enforcement.get("kind")
    expected = (
        {"kind", "limits"}
        if kind != "external_watchdog"
        else {
            "kind",
            "limits",
            "watchdog_digest",
        }
    )
    if set(enforcement) != expected or kind not in {
        "hard",
        "external_watchdog",
        "soft",
        "none",
    }:
        raise WorkflowError(error_code, "candidate budget enforcement kind is invalid")
    if kind == "external_watchdog" and not _is_sha256(enforcement.get("watchdog_digest")):
        raise WorkflowError(error_code, "candidate watchdog digest is invalid")
    _validate_limits(enforcement.get("limits"), error_code)


def _validate_route(route: object, error_code: str) -> None:
    if not isinstance(route, Mapping) or set(route) != set(ROUTE_DIMENSIONS):
        raise WorkflowError(error_code, "route dimensions are incomplete")
    if any(not _nonempty(route.get(field)) for field in ROUTE_DIMENSIONS[:-1]):
        raise WorkflowError(error_code, "route dimension is blank")
    tools = route.get("tools")
    if not _unique_strings(tools, allow_empty=True):
        raise WorkflowError(error_code, "route tools are invalid")


def _validate_limits(limits: object, error_code: str) -> None:
    if (
        not isinstance(limits, Mapping)
        or set(limits) != set(USAGE_FIELDS)
        or any(type(limits[field]) is not int or limits[field] < 0 for field in USAGE_FIELDS)
    ):
        raise WorkflowError(error_code, "route budget limits are invalid")


def _require_route_assurance(candidate: Mapping[str, Any]) -> None:
    if any(candidate["control"][claim] is not True for claim in ROUTE_CLAIMS):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "candidate route is not fully controlled")
    if any(candidate["attestation"][claim] is not True for claim in ROUTE_CLAIMS):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "candidate route is not fully attestable")


def _validate_enforcement(
    enforcement: Mapping[str, Any], budget: Mapping[str, Any], approved: Mapping[str, Any]
) -> None:
    kind = enforcement["kind"]
    if kind == "hard":
        eligible = True
    elif kind == "external_watchdog":
        eligible = enforcement["watchdog_digest"] in approved
    else:
        eligible = False
    if not eligible:
        raise WorkflowError("ROUTE_PLAN_REJECTED", "candidate budget enforcement is ineligible")
    if any(enforcement["limits"][field] > budget[field] for field in USAGE_FIELDS):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "candidate-specific limits exceed route budget")


def _validate_watchdog_authorities(
    authorities: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(authorities, Mapping):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "watchdog authority input is invalid")
    frozen: dict[str, dict[str, Any]] = {}
    for authority_digest, authority in authorities.items():
        if not _is_sha256(authority_digest) or not isinstance(authority, Mapping):
            raise WorkflowError("ROUTE_PLAN_REJECTED", "watchdog authority input is invalid")
        payload = dict(authority)
        provenance = payload.get("provenance")
        if (
            set(payload) != {"authority_id", "attestor_id", "provenance"}
            or not _nonempty(payload.get("authority_id"))
            or not _nonempty(payload.get("attestor_id"))
            or not isinstance(provenance, Mapping)
            or provenance.get("proof_kind") != "TRUSTED_WATCHDOG_POLICY"
            or canonical_digest(payload, error_code="ROUTE_PLAN_REJECTED") != authority_digest
        ):
            raise WorkflowError("ROUTE_PLAN_REJECTED", "watchdog authority input is invalid")
        frozen[authority_digest] = json.loads(
            canonical_json(payload, error_code="ROUTE_PLAN_REJECTED")
        )
    return frozen


def _validate_requirements(requirements: object) -> None:
    if not isinstance(requirements, Mapping) or set(requirements) != {
        "providers",
        "contexts",
        "tools",
    }:
        raise WorkflowError("ROUTE_PLAN_REJECTED", "capability requirements are invalid")
    if (
        not _unique_strings(requirements["providers"])
        or not _unique_strings(requirements["contexts"])
        or not _unique_strings(requirements["tools"], allow_empty=True)
    ):
        raise WorkflowError("ROUTE_PLAN_REJECTED", "capability requirements are invalid")


def _meets_requirements(route: Mapping[str, Any], requirements: Mapping[str, Any]) -> bool:
    return (
        route["provider"] in requirements["providers"]
        and route["context"] in requirements["contexts"]
        and set(requirements["tools"]).issubset(route["tools"])
    )


def _routes_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        sorted(left[field]) == sorted(right[field])
        if field == "tools"
        else left[field] == right[field]
        for field in ROUTE_DIMENSIONS
    )


def _timestamp(value: object, error_code: str) -> datetime:
    if not _nonempty(value):
        raise WorkflowError(error_code, "route timestamp is invalid")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise WorkflowError(error_code, "route timestamp is invalid") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise WorkflowError(error_code, "route timestamp must be timezone-aware")
    return result


def _unique_strings(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list | tuple)
        and (allow_empty or bool(value))
        and all(_nonempty(item) for item in value)
        and len(value) == len(set(value))
    )


def _unique_sha_list(value: object, *, allow_empty: bool) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(_is_sha256(item) for item in value)
        and len(value) == len(set(value))
    )


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_json(value: object, error_code: str) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        raise WorkflowError(error_code, "route artifact contains a float")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowError(error_code, "route artifact key is not a string")
            _validate_json(item, error_code)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, error_code)
        return
    raise WorkflowError(error_code, "route artifact contains a non-JSON value")
