"""The public workflow kernel."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .model import (
    ProjectView,
    RecordReceipt,
    UserDecision,
    WorkflowError,
)
from .store import ControlStore


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
            receipt_id=str(uuid.uuid4()),
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
                    "SELECT e.event_digest, e.event_json, e.receipt_json, e.receipt_digest "
                    ", n.project_id AS nonce_project_id, n.actor_id AS nonce_actor_id, "
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
                            existing_rows[0]["receipt_json"],
                            existing_rows[0]["receipt_digest"],
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
        WorkflowKernel._validate_bootstrap_decision(stored_event)
        WorkflowKernel._validate_bootstrap_payload(stored_event.complete_revision_payload)
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


def _verified_receipt(receipt_json: str, receipt_digest: str) -> RecordReceipt:
    if _digest(receipt_json) != receipt_digest:
        raise WorkflowError("LEDGER_INTEGRITY", "record receipt digest mismatch")
    try:
        payload = json.loads(receipt_json)
        if not isinstance(payload, dict):
            raise TypeError
        return RecordReceipt(**payload)
    except (json.JSONDecodeError, TypeError) as error:
        raise WorkflowError("LEDGER_INTEGRITY", "record receipt is not valid JSON") from error


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
