from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import agentic_workflow
from agentic_workflow import (
    ProjectView,
    RecordReceipt,
    UserDecision,
    WorkflowError,
    WorkflowKernel,
)

PROFILE = json.loads(
    (Path(__file__).parents[1] / "config" / "operating-profile.v1.json").read_text()
)


class ActorAuthenticator:
    def __init__(self, *authenticated_actors: str) -> None:
        self.authenticated_actors = set(authenticated_actors)

    def authenticate(self, decision: UserDecision) -> bool:
        return (
            decision.authenticated_actor in self.authenticated_actors
            and decision.provenance == {"channel": "test"}
        )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "control.sqlite3"


@pytest.fixture
def kernel(database_path: Path) -> WorkflowKernel:
    return WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator("user-1"),
    )


def bootstrap_decision(**changes: object) -> UserDecision:
    event = UserDecision(
        project_id="project-1",
        source="test-ui",
        source_event_id="event-1",
        authenticated_actor="user-1",
        scope="PROJECT_INTENT",
        verbatim_text="Create the workflow project.",
        nonce="nonce-1",
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


def test_authenticated_bootstrap_creates_current_intent_atomically(kernel: WorkflowKernel) -> None:
    event = bootstrap_decision()

    receipt = kernel.record(event)

    assert receipt == RecordReceipt(
        receipt_id=receipt.receipt_id,
        project_id="project-1",
        event_type="USER_DECISION",
        outcome="PROJECT_BOOTSTRAPPED",
        event_digest=receipt.event_digest,
        active_intent_digest=receipt.active_intent_digest,
        recorded_at=receipt.recorded_at,
    )
    assert receipt.receipt_id
    assert receipt.active_intent_digest
    assert kernel.view("project-1").current_goal == event.complete_revision_payload["goal"]


def test_identical_source_event_and_nonce_replay_returns_original_receipt(
    kernel: WorkflowKernel,
) -> None:
    event = bootstrap_decision()
    original = kernel.record(event)

    replayed = kernel.record(event)

    assert replayed == original


@pytest.mark.parametrize(
    "conflict",
    [
        {"verbatim_text": "Different content."},
        {"source_event_id": "event-2", "verbatim_text": "Different content."},
    ],
    ids=["source-event", "nonce"],
)
def test_reused_source_event_or_nonce_with_different_content_is_identity_conflict(
    kernel: WorkflowKernel, conflict: dict[str, object]
) -> None:
    kernel.record(bootstrap_decision())

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(**conflict))

    assert caught.value.code == "IDENTITY_CONFLICT"


def test_reused_replay_identity_with_new_event_and_nonce_is_identity_conflict(
    kernel: WorkflowKernel,
) -> None:
    kernel.record(bootstrap_decision())

    with pytest.raises(WorkflowError) as caught:
        kernel.record(
            bootstrap_decision(
                source_event_id="event-2",
                nonce="nonce-2",
                verbatim_text="Different content.",
            )
        )

    assert caught.value.code == "IDENTITY_CONFLICT"


def test_bootstrap_rolls_back_every_record_when_a_late_insert_fails(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER inject_late_bootstrap_failure BEFORE INSERT ON daily_briefs "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision())
    assert caught.value.code == "LEDGER_ERROR"

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER inject_late_bootstrap_failure")

    receipt = kernel.record(bootstrap_decision())
    assert receipt.outcome == "PROJECT_BOOTSTRAPPED"
    assert kernel.view("project-1").current_goal["outcome"] == (
        "Produce evidence-backed research decisions"
    )


def test_unauthenticated_user_decision_is_rejected(database_path: Path) -> None:
    kernel = WorkflowKernel(
        database_path,
        decision_authenticator=ActorAuthenticator(),
    )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision())

    assert caught.value.code == "UNAUTHENTICATED_DECISION"
    with pytest.raises(WorkflowError) as absent:
        kernel.view("project-1")
    assert absent.value.code == "PROJECT_NOT_FOUND"


@pytest.mark.parametrize(
    "event",
    [
        bootstrap_decision(scope="OTHER"),
        bootstrap_decision(nonce=""),
        bootstrap_decision(replay_identity=""),
        bootstrap_decision(complete_revision_payload={"x": 1}),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "constitution": {"x": 1},
            }
        ),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "goal": {"x": 1},
            }
        ),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "goal": {
                    key: value
                    for key, value in bootstrap_decision().complete_revision_payload["goal"].items()
                    if key != "scope"
                },
            }
        ),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "goal": {
                    **deepcopy(bootstrap_decision().complete_revision_payload["goal"]),
                    "scope": "",
                },
            }
        ),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "operating_profile": {"x": 1},
            }
        ),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "constitution": {
                    "user_sovereignty": False,
                    "external_effects_require_authority": True,
                },
            }
        ),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "operating_profile": {
                    **deepcopy(PROFILE),
                    "autonomy": {
                        **deepcopy(PROFILE["autonomy"]),
                        "automatic_merge": True,
                    },
                },
            }
        ),
        bootstrap_decision(
            complete_revision_payload={
                **deepcopy(bootstrap_decision().complete_revision_payload),
                "operating_profile": {
                    **deepcopy(PROFILE),
                    "autonomy": {
                        **deepcopy(PROFILE["autonomy"]),
                        "enabled_modes": ["autonomous-deploy"],
                    },
                },
            }
        ),
    ],
    ids=[
        "wrong-scope",
        "empty-nonce",
        "empty-replay-identity",
        "missing-complete-snapshots",
        "invalid-constitution",
        "invalid-goal",
        "goal-missing-scope",
        "goal-empty-scope",
        "invalid-operating-profile",
        "constitution-disables-sovereignty",
        "operating-profile-enables-merge",
        "operating-profile-unknown-mode",
    ],
)
def test_bootstrap_schema_and_authority_fields_fail_closed(
    kernel: WorkflowKernel, event: UserDecision
) -> None:
    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code in {"INVALID_DECISION", "INVALID_BOOTSTRAP"}


@pytest.mark.parametrize(
    "missing_field",
    [
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
    ],
)
def test_operating_profile_requires_every_v1_top_level_field(
    kernel: WorkflowKernel, missing_field: str
) -> None:
    profile = deepcopy(PROFILE)
    del profile[missing_field]
    payload = deepcopy(bootstrap_decision().complete_revision_payload)
    payload["operating_profile"] = profile

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(complete_revision_payload=payload))

    assert caught.value.code == "INVALID_BOOTSTRAP"


@pytest.mark.parametrize(
    ("object_path", "extra_key", "extra_value"),
    [
        (("project",), "automatic_deploy", True),
        (("constitution",), "delegated_external_authority", True),
        (("goal",), "automatic_deploy", True),
        (("operating_profile",), "automatic_deploy", True),
        (("operating_profile", "autonomy"), "unreviewed_external_effects", True),
        (
            ("operating_profile", "method_policy"),
            "cognitive_actions_may_skip_receipts",
            True,
        ),
        (("operating_profile", "synchronization"), "silence_grants_authority", True),
        (("operating_profile", "routing"), "fallback_for_low_risk", True),
        (("operating_profile", "venues"), "unbounded_cloud", {}),
        (("operating_profile", "venues", "local_hermes"), "automatic_deploy", True),
        (("operating_profile", "venues", "local_copilot"), "automatic_deploy", True),
        (
            ("operating_profile", "venues", "github_copilot_cloud"),
            "automatic_deploy",
            True,
        ),
        (("operating_profile", "venues", "feng"), "automatic_deploy", True),
        (("operating_profile", "budgets"), "unbounded_cloud", {}),
        (("operating_profile", "budgets", "local_hermes"), "allow_overrun", True),
        (("operating_profile", "budgets", "local_copilot"), "allow_overrun", True),
        (
            ("operating_profile", "budgets", "github_copilot_cloud"),
            "allow_overrun",
            True,
        ),
        (("operating_profile", "budgets", "feng"), "allow_overrun", True),
    ],
    ids=lambda value: ".".join(value) if isinstance(value, tuple) else str(value),
)
def test_bootstrap_v1_rejects_unknown_authority_bearing_keys_at_every_object_level(
    kernel: WorkflowKernel,
    object_path: tuple[str, ...],
    extra_key: str,
    extra_value: object,
) -> None:
    payload = deepcopy(bootstrap_decision().complete_revision_payload)
    target: object = payload
    for component in object_path:
        assert isinstance(target, dict)
        target = target[component]
    assert isinstance(target, dict)
    target[extra_key] = extra_value

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(complete_revision_payload=payload))

    assert caught.value.code == "INVALID_BOOTSTRAP"


def test_placeholder_budget_and_missing_venue_fail_closed(kernel: WorkflowKernel) -> None:
    for mutation in ("placeholder-budget", "missing-venue"):
        profile = deepcopy(PROFILE)
        if mutation == "placeholder-budget":
            profile["budgets"] = {"placeholder": {}}
        else:
            del profile["venues"]["feng"]
        payload = deepcopy(bootstrap_decision().complete_revision_payload)
        payload["operating_profile"] = profile

        with pytest.raises(WorkflowError) as caught:
            kernel.record(bootstrap_decision(complete_revision_payload=payload))

        assert caught.value.code == "INVALID_BOOTSTRAP"


def test_empty_venue_definition_fails_closed(kernel: WorkflowKernel) -> None:
    profile = deepcopy(PROFILE)
    profile["venues"]["feng"] = {}
    payload = deepcopy(bootstrap_decision().complete_revision_payload)
    payload["operating_profile"] = profile

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(complete_revision_payload=payload))

    assert caught.value.code == "INVALID_BOOTSTRAP"


@pytest.mark.parametrize(
    ("venue_name", "required_field"),
    [
        ("local_hermes", "role"),
        ("local_hermes", "heavy_tests_allowed"),
        ("local_copilot", "role"),
        ("local_copilot", "enabled_mode"),
        ("local_copilot", "requires_resource_isolation"),
        ("github_copilot_cloud", "role"),
        ("github_copilot_cloud", "enabled_mode"),
        ("github_copilot_cloud", "requires_custom_agent"),
        ("feng", "role"),
        ("feng", "required_tests_authoritative"),
        ("feng", "max_concurrency"),
    ],
)
def test_every_required_venue_requires_its_complete_v1_definition(
    kernel: WorkflowKernel, venue_name: str, required_field: str
) -> None:
    profile = deepcopy(PROFILE)
    del profile["venues"][venue_name][required_field]
    payload = deepcopy(bootstrap_decision().complete_revision_payload)
    payload["operating_profile"] = profile

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(complete_revision_payload=payload))

    assert caught.value.code == "INVALID_BOOTSTRAP"


def test_authenticator_receives_and_can_bind_the_complete_decision(database_path: Path) -> None:
    expected = bootstrap_decision()

    class ExactAuthenticator:
        def authenticate(self, decision: UserDecision) -> bool:
            return decision == expected

    kernel = WorkflowKernel(database_path, decision_authenticator=ExactAuthenticator())

    with pytest.raises(WorkflowError) as caught:
        kernel.record(replace(expected, verbatim_text="Altered instruction"))

    assert caught.value.code == "UNAUTHENTICATED_DECISION"


def test_non_string_json_keys_are_rejected(kernel: WorkflowKernel) -> None:
    event = bootstrap_decision(provenance={1: "ambiguous", "1": "collision"})

    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code == "INVALID_EVENT"


def test_strict_json_rejects_tuples(kernel: WorkflowKernel) -> None:
    event = bootstrap_decision(provenance={"channel": "test", "steps": ("one", "two")})

    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code == "INVALID_EVENT"


def test_provenance_must_be_a_json_object(kernel: WorkflowKernel) -> None:
    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(provenance=[]))

    assert caught.value.code == "INVALID_EVENT"


def test_view_returns_only_current_projections_without_writing(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    event = bootstrap_decision()
    kernel.record(event)
    with sqlite3.connect(database_path) as observer:
        before = observer.execute("PRAGMA data_version").fetchone()[0]

        projected = kernel.view("project-1")

        after = observer.execute("PRAGMA data_version").fetchone()[0]

    assert projected == ProjectView(
        current_goal=dict(event.complete_revision_payload["goal"]),
        daily_brief={"status": "INITIAL", "material_changes": []},
        pending_decisions=(),
    )
    assert after == before


def test_concurrent_kernel_initialization_serializes_migrations(database_path: Path) -> None:
    def initialize(_: int) -> None:
        WorkflowKernel(database_path, decision_authenticator=ActorAuthenticator("user-1"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(initialize, range(16)))

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]


def test_revision_history_is_immutable_and_view_verifies_digests(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(bootstrap_decision())

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE goal_revisions SET payload_json = '{}' WHERE project_id = 'project-1'"
            )

        connection.execute("DROP TRIGGER goal_revisions_no_update")
        connection.execute(
            "UPDATE goal_revisions SET payload_json = '{}' WHERE project_id = 'project-1'"
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.view("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


def test_projects_are_append_only(kernel: WorkflowKernel, database_path: Path) -> None:
    kernel.record(bootstrap_decision())

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE projects SET name = 'changed' WHERE project_id = 'project-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM projects WHERE project_id = 'project-1'")


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE projects SET project_id = 'changed' WHERE project_id = 'project-1'",
        "UPDATE projects SET name = 'changed' WHERE project_id = 'project-1'",
        "UPDATE projects SET project_json = '{}' WHERE project_id = 'project-1'",
        "UPDATE projects SET project_digest = 'changed' WHERE project_id = 'project-1'",
        "UPDATE projects SET created_at = 'changed' WHERE project_id = 'project-1'",
    ],
    ids=["project-id", "name", "payload", "digest", "created-at"],
)
def test_view_fails_closed_when_authoritative_project_row_is_tampered(
    kernel: WorkflowKernel, database_path: Path, tamper_sql: str
) -> None:
    kernel.record(bootstrap_decision())

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER projects_no_update")
        connection.execute(tamper_sql)

    with pytest.raises(WorkflowError) as caught:
        kernel.view("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize(
    ("update_sql", "delete_sql"),
    [
        (
            "UPDATE inbox_events SET receipt_json = '{}' WHERE project_id = 'project-1'",
            "DELETE FROM inbox_events WHERE project_id = 'project-1'",
        ),
        (
            "UPDATE daily_briefs SET projection_json = '{}' WHERE project_id = 'project-1'",
            "DELETE FROM daily_briefs WHERE project_id = 'project-1'",
        ),
    ],
    ids=["receipt", "projection"],
)
def test_receipts_and_projections_are_append_only(
    kernel: WorkflowKernel,
    database_path: Path,
    update_sql: str,
    delete_sql: str,
) -> None:
    kernel.record(bootstrap_decision())

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(update_sql)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(delete_sql)


def test_replay_fails_closed_when_stored_receipt_is_tampered(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    event = bootstrap_decision()
    kernel.record(event)

    with sqlite3.connect(database_path) as connection:
        stored_receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM inbox_events WHERE project_id = 'project-1'"
            ).fetchone()[0]
        )
        stored_receipt["receipt_id"] = "tampered"
        connection.execute("DROP TRIGGER IF EXISTS inbox_events_no_update")
        connection.execute(
            "UPDATE inbox_events SET receipt_json = ? WHERE project_id = 'project-1'",
            (json.dumps(stored_receipt),),
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code == "LEDGER_INTEGRITY"


def test_view_fails_closed_when_stored_projection_is_tampered(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    kernel.record(bootstrap_decision())

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS daily_briefs_no_update")
        connection.execute(
            "UPDATE daily_briefs SET projection_json = '{}' WHERE project_id = 'project-1'"
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.view("project-1")

    assert caught.value.code == "LEDGER_INTEGRITY"


def test_internal_seams_are_not_exported_from_the_package() -> None:
    assert not hasattr(agentic_workflow, "Clock")
    assert not hasattr(agentic_workflow, "DecisionAuthenticator")


def test_mutation_during_authentication_is_rejected(database_path: Path) -> None:
    event = bootstrap_decision()

    class MutatingAuthenticator:
        def authenticate(self, decision: UserDecision) -> bool:
            decision.complete_revision_payload["goal"]["outcome"] = "Mutated after hashing"
            return True

    kernel = WorkflowKernel(database_path, decision_authenticator=MutatingAuthenticator())

    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code == "INVALID_EVENT"
    with pytest.raises(WorkflowError) as absent:
        kernel.view("project-1")
    assert absent.value.code == "PROJECT_NOT_FOUND"


@pytest.mark.parametrize(
    ("venue_name", "required_field"),
    [
        ("local_hermes", "max_turns"),
        ("feng", "max_memory_mb"),
        ("feng", "max_disk_mb"),
    ],
)
def test_operating_profile_requires_resource_limit_fields(
    kernel: WorkflowKernel, venue_name: str, required_field: str
) -> None:
    profile = deepcopy(PROFILE)
    del profile["budgets"][venue_name][required_field]
    payload = deepcopy(bootstrap_decision().complete_revision_payload)
    payload["operating_profile"] = profile

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(complete_revision_payload=payload))

    assert caught.value.code == "INVALID_BOOTSTRAP"


@pytest.mark.parametrize(
    ("path", "boolean_value"),
    [
        (("schema_version",), True),
        (("synchronization", "primary_briefs_per_day"), True),
        (("venues", "feng", "max_concurrency"), True),
        (("budgets", "local_hermes", "max_wall_seconds"), True),
        (("budgets", "local_hermes", "max_turns"), True),
        (("budgets", "local_hermes", "max_concurrency"), True),
        (("budgets", "local_hermes", "max_paid_units"), False),
        (("budgets", "local_copilot", "max_wall_seconds"), True),
        (("budgets", "local_copilot", "max_concurrency"), True),
        (("budgets", "local_copilot", "max_paid_units"), False),
        (("budgets", "github_copilot_cloud", "max_concurrency"), True),
        (("budgets", "github_copilot_cloud", "max_paid_units"), False),
        (("budgets", "feng", "max_wall_seconds"), True),
        (("budgets", "feng", "max_concurrency"), True),
        (("budgets", "feng", "max_memory_mb"), True),
        (("budgets", "feng", "max_disk_mb"), True),
        (("budgets", "feng", "max_paid_units"), False),
    ],
    ids=lambda value: ".".join(value) if isinstance(value, tuple) else str(value),
)
def test_operating_profile_rejects_booleans_for_every_integer_field(
    kernel: WorkflowKernel, path: tuple[str, ...], boolean_value: bool
) -> None:
    profile = deepcopy(PROFILE)
    target = profile
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = boolean_value
    payload = deepcopy(bootstrap_decision().complete_revision_payload)
    payload["operating_profile"] = profile

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(complete_revision_payload=payload))

    assert caught.value.code == "INVALID_BOOTSTRAP"


def test_external_watchdog_policy_identity_must_not_be_blank(kernel: WorkflowKernel) -> None:
    profile = deepcopy(PROFILE)
    profile["budgets"]["local_copilot"]["watchdog_policy_id"] = "  "
    payload = deepcopy(bootstrap_decision().complete_revision_payload)
    payload["operating_profile"] = profile

    with pytest.raises(WorkflowError) as caught:
        kernel.record(bootstrap_decision(complete_revision_payload=payload))

    assert caught.value.code == "INVALID_BOOTSTRAP"


@pytest.mark.parametrize(
    ("update_sql", "delete_sql"),
    [
        (
            "UPDATE decision_nonces SET nonce = 'changed' WHERE project_id = 'project-1'",
            "DELETE FROM decision_nonces WHERE project_id = 'project-1'",
        ),
        (
            "UPDATE active_intents SET activated_at = 'changed' WHERE project_id = 'project-1'",
            "DELETE FROM active_intents WHERE project_id = 'project-1'",
        ),
    ],
    ids=["decision-nonce", "active-intent-history"],
)
def test_decision_nonces_and_active_intent_history_are_append_only(
    kernel: WorkflowKernel,
    database_path: Path,
    update_sql: str,
    delete_sql: str,
) -> None:
    kernel.record(bootstrap_decision())

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(update_sql)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(delete_sql)


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "DELETE FROM decision_nonces WHERE project_id = 'project-1'",
        "UPDATE decision_nonces SET project_id = 'changed' WHERE project_id = 'project-1'",
        "UPDATE decision_nonces SET actor_id = 'changed' WHERE project_id = 'project-1'",
        "UPDATE decision_nonces SET nonce = 'changed' WHERE project_id = 'project-1'",
        "UPDATE decision_nonces SET replay_identity = 'changed' WHERE project_id = 'project-1'",
        "UPDATE decision_nonces SET source = 'changed' WHERE project_id = 'project-1'",
        "UPDATE decision_nonces SET source_event_id = 'changed' WHERE project_id = 'project-1'",
    ],
    ids=[
        "missing",
        "project-id",
        "actor-id",
        "nonce",
        "replay-identity",
        "source",
        "source-event-id",
    ],
)
def test_source_identity_replay_requires_its_exact_decision_nonce_history(
    kernel: WorkflowKernel, database_path: Path, tamper_sql: str
) -> None:
    event = bootstrap_decision()
    kernel.record(event)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER decision_nonces_no_update")
        connection.execute("DROP TRIGGER decision_nonces_no_delete")
        connection.execute(tamper_sql)

    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code == "LEDGER_INTEGRITY"


def test_replay_fails_closed_when_stored_event_json_is_tampered(
    kernel: WorkflowKernel, database_path: Path
) -> None:
    event = bootstrap_decision()
    kernel.record(event)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS inbox_events_no_update")
        connection.execute(
            "UPDATE inbox_events SET event_json = '{}' WHERE project_id = 'project-1'"
        )

    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code == "LEDGER_INTEGRITY"


@pytest.mark.parametrize("number", [1.0, -0.0], ids=["integral-float", "negative-zero"])
def test_content_addressed_event_rejects_all_floats(kernel: WorkflowKernel, number: float) -> None:
    event = bootstrap_decision(provenance={"channel": "test", "number": number})

    with pytest.raises(WorkflowError) as caught:
        kernel.record(event)

    assert caught.value.code == "INVALID_EVENT"


def test_content_addressed_event_allows_json_integers(database_path: Path) -> None:
    event = bootstrap_decision(provenance={"channel": "test", "number": 1})

    class ExactAuthenticator:
        def authenticate(self, decision: UserDecision) -> bool:
            return decision == event

    kernel = WorkflowKernel(database_path, decision_authenticator=ExactAuthenticator())

    assert kernel.record(event).outcome == "PROJECT_BOOTSTRAPPED"
