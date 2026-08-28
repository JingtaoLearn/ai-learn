from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
from threading import Event

import pandas as pd
import pytest

import quant_platform.parameter_study as parameter_study_module
from quant_platform.catalog import (
    Catalog,
    CatalogMigration,
    CatalogVersionError,
    initialize_catalog,
)
from quant_platform.dataset_service import DatasetCatalogItem, DatasetService
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService
from quant_platform.parameter_study import (
    ParameterStudy,
    StudyNotFoundError,
    StudyValidationError,
)
from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.schemas import canonical_json_bytes
from quant_platform.seed import BUILTINS
from quant_platform.study_contracts import FOLD_WINDOW_FIELDS, normalize_fold_window
from quant_platform.study_datasets import ExecutionDatasetSliceFactory
from quant_platform.study_suggesters import (
    Exhausted,
    GridParameterSuggester,
    Suggestion,
    optuna_tpe_frozen_identity,
)


WARMUP_SESSION = "2026-01-02"
SESSIONS = pd.date_range("2026-01-05", periods=18, freq="B").strftime("%Y-%m-%d").tolist()
ALL_SESSIONS = [WARMUP_SESSION, *SESSIONS]
EXECUTION_IDENTITY = {
    "domain_schema": 1,
    "runner": "quant_platform",
    "source_sha256": "a" * 64,
    "runtime": {"python": "3.12.11", "pandas": "2.3.1"},
    "runner_image": "sha256:" + "b" * 64,
}


class FixedCalendar:
    source_identity = {
        "calendar": "XSHG",
        "library": "test-calendar",
        "version": "2026",
    }

    def sessions(self, start: str, end: str) -> list[str]:
        return [session for session in ALL_SESSIONS if start <= session <= end]


class NoFetchSource:
    provider = "synthetic"

    def latest_available_close(self, instrument: str) -> str:
        return ALL_SESSIONS[-1]

    def fetch(self, instrument: str, start: str, end: str):
        raise AssertionError("the complete synthetic snapshot must not be fetched")


def _bars(*, offset: float = 0.0) -> pd.DataFrame:
    values = [10.0 + offset + index / 10 for index in range(len(ALL_SESSIONS))]
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(ALL_SESSIONS),
            "Open": values,
            "High": [value + 0.1 for value in values],
            "Low": [value - 0.1 for value in values],
            "Close": values,
            "Volume": [1_000.0 + index for index in range(len(ALL_SESSIONS))],
        }
    )


def _study_service(
    tmp_path: Path,
    *,
    legacy_experiment: bool = False,
) -> tuple[ParameterStudy, ExperimentService]:
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    datasets = DatasetService(
        catalog,
        sources={"synthetic": NoFetchSource()},
        calendars={"XSHG": FixedCalendar()},
    )
    datasets.register(
        DatasetCatalogItem(
            dataset_id="SYNTH.SS",
            name="Synthetic daily bars",
            instrument="SYNTH.SS",
            provider="synthetic",
            market="XSHG",
            currency="CNY",
            adjustment="mixed",
            calendar="XSHG",
            default_start=WARMUP_SESSION,
        )
    )
    publish_snapshot(
        _bars(),
        root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )
    experiments = ExperimentService(
        catalog,
        execution_identity=EXECUTION_IDENTITY,
        datasets=datasets,
    )
    if legacy_experiment:
        with catalog.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO experiments(experiment_id, identity_json, created_at)
                VALUES (?, '{}', '2026-08-27T12:00:00Z')
                """,
                ("f" * 64,),
            )
    return (
        ParameterStudy(
            catalog,
            datasets=datasets,
            experiments=experiments,
            release_locator="/srv/quant/releases/154",
        ),
        experiments,
    )


def _spec() -> dict:
    operators = {
        descriptor["slot"]: {
            "operator_id": descriptor["operator_id"],
            "version": "latest",
            "parameters": {},
        }
        for descriptor in BUILTINS
    }
    return {
        "schema_version": 1,
        "dataset": {
            "dataset_id": "SYNTH.SS",
            "start": SESSIONS[0],
            "end": SESSIONS[-1],
        },
        "template": {
            "name": "single_stock_daily_causal",
            "version": "1",
            "parameters": {
                "instrument_display_name": "Synthetic daily bars",
                "evaluation_start": SESSIONS[0],
                "evaluation_end": SESSIONS[-1],
            },
        },
        "operators": operators,
        "search": {
            "suggester": "GRID",
            "suggester_version": "1.0.0",
            "seed": 17,
            "unique_trial_budget": 4,
            "max_suggestions": 8,
            "space": {
                "/operators/decision/buy_threshold_pct_per_day": {
                    "values": [1, 2.0]
                },
                "/operators/fit/window_sessions": {"values": [2, 3]},
            },
        },
        "validation": {
            "outer_folds": 2,
            "inner_folds": 2,
            "scoring_sessions": 2,
            "minimum_training_sessions": 4,
            "purge_sessions": 0,
            "outer_account_policy": "FORCE_FLAT_WITH_COST",
        },
        "evaluation": {
            "policy_id": "robust_walk_forward",
            "version": "latest",
            "parameters": {},
        },
        "holdout": {"sessions": 2, "pass_rule": "POLICY_CONSTRAINTS"},
        "lineage": {
            "parent_study_ids": [],
            "prior_unique_candidate_count": 0,
            "is_complete": True,
        },
    }


def _explicit_spec() -> dict:
    spec = _spec()
    spec["template"]["parameters"].update(
        {
            "initial_capital_cny": 100_000,
            "initial_state": "flat",
            "terminal_handling": "mark_to_market",
            "cost_assumption_label": (
                "Conservative research assumptions; not an account-specific fee schedule."
            ),
        }
    )
    for descriptor in BUILTINS:
        spec["operators"][descriptor["slot"]]["parameters"] = deepcopy(
            descriptor["defaults"]
        )
    spec["search"]["space"][
        "/operators/decision/buy_threshold_pct_per_day"
    ]["values"] = [1.0, 2]
    spec["evaluation"]["parameters"] = {
        "stability_weight": 0.5,
        "turnover_weight": 0.05,
        "minimum_trades": 1,
        "maximum_drawdown": None,
        "maximum_annual_turnover": None,
    }
    return spec


def _minimal_orchestration_spec() -> dict:
    spec = _explicit_spec()
    spec["operators"]["fit"]["parameters"]["window_sessions"] = 2
    spec["search"].update(
        {
            "unique_trial_budget": 1,
            "max_suggestions": 1,
            "space": {
                "/operators/decision/buy_threshold_pct_per_day": {
                    "values": [0.2]
                }
            },
        }
    )
    spec["validation"].update({"outer_folds": 1, "inner_folds": 1})
    spec["evaluation"]["parameters"]["minimum_trades"] = 0
    spec["operators"]["fit"]["parameters"]["price_column"] = "Close"
    return spec


class _ScriptedOptunaSuggester:
    mode = "UNIQUE"
    calls: list[tuple[str, list[dict]]] = []

    @classmethod
    def reset(cls, mode: str = "UNIQUE") -> None:
        cls.mode = mode
        cls.calls = []

    def next_suggestion(
        self,
        frozen_plan: dict,
        ordered_history: list[dict],
    ) -> Suggestion | Exhausted:
        round_identity = frozen_plan["round_identity"]
        history = deepcopy(ordered_history)
        type(self).calls.append((round_identity, history))
        proposals = [
            event
            for event in history
            if event["event_type"] in {"SUGGESTION_RECORDED", "DUPLICATE_SUGGESTION"}
        ]
        told = {
            event["candidate_digest"]
            for event in history
            if event["event_type"] == "INNER_EVALUATION_RECORDED"
        }
        pending = [
            event
            for event in proposals
            if event["disposition"] == "UNIQUE"
            and event["candidate_digest"] not in told
        ]
        if pending:
            raise AssertionError("a second Optuna Trial was asked before the first tell")

        unique_count = sum(event["disposition"] == "UNIQUE" for event in proposals)
        if unique_count >= frozen_plan["search"]["unique_trial_budget"]:
            return Exhausted(
                "UNIQUE_TRIAL_BUDGET",
                len(proposals),
                unique_count,
            )
        if len(proposals) >= frozen_plan["search"]["max_suggestions"]:
            return Exhausted("RAW_SUGGESTION_BUDGET", len(proposals), unique_count)

        grid_plan = deepcopy(frozen_plan)
        grid_plan["search"]["suggester"] = "GRID"
        baseline = GridParameterSuggester().next_suggestion(grid_plan, [])
        assert isinstance(baseline, Suggestion)
        sequence = len(proposals)
        if sequence == 0:
            return baseline
        if self.mode == "DUPLICATE" and sequence == 1:
            return Suggestion(
                proposal_sequence=sequence,
                candidate_digest=baseline.candidate_digest,
                candidate=baseline.candidate,
                classification=baseline.classification,
                disposition="DUPLICATE",
                duplicate_of_sequence=0,
            )

        candidate = baseline.candidate
        candidate["operators"]["decision"]["parameters"][
            "buy_threshold_pct_per_day"
        ] = 0.3
        return Suggestion(
            proposal_sequence=sequence,
            candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
            candidate=candidate,
            classification="IN_RANGE",
            disposition="UNIQUE",
            duplicate_of_sequence=None,
        )


def _install_optuna_contract(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "UNIQUE",
) -> None:
    original_search = parameter_study_module._search

    def freeze_test_optuna_search(value, template, operators):
        if value["suggester"] != "OPTUNA_TPE":
            return original_search(value, template, operators)
        legacy = deepcopy(value)
        legacy["suggester"] = "GRID"
        frozen = original_search(legacy, template, operators)
        frozen["suggester"] = "OPTUNA_TPE"
        return frozen

    _ScriptedOptunaSuggester.reset(mode)
    monkeypatch.setattr(parameter_study_module, "_search", freeze_test_optuna_search)
    monkeypatch.setattr(
        parameter_study_module.study_suggesters,
        "OptunaTPEParameterSuggester",
        _ScriptedOptunaSuggester,
        raising=False,
    )


def _optuna_spec(*, unique_trial_budget: int = 2, max_suggestions: int = 2) -> dict:
    spec = _minimal_orchestration_spec()
    spec["search"].update(
        {
            "suggester": "OPTUNA_TPE",
            "unique_trial_budget": unique_trial_budget,
            "max_suggestions": max_suggestions,
            "space": {
                "/operators/decision/buy_threshold_pct_per_day": {
                    "values": [0.2, 0.3]
                }
            },
        }
    )
    return spec


def _restart_studies(
    studies: ParameterStudy,
    experiments: ExperimentService,
    study_id: str,
    *,
    effect_executor=None,
) -> ParameterStudy:
    _expire_study_lease(studies, study_id)
    return ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/171",
        effect_executor=effect_executor,
    )


def _holdout_identity_digest(frozen_plan: dict) -> str:
    dataset = frozen_plan["dataset"]
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "dataset": {
                    "instrument": dataset["instrument"],
                    "snapshot_id": dataset["snapshot_id"],
                    "canonical_sha256": dataset["canonical_sha256"],
                },
                "fold_window": frozen_plan["holdout"]["fold_window"],
            }
        )
    ).hexdigest()


def _expire_study_lease(studies: ParameterStudy, study_id: str) -> None:
    current = studies.detail(study_id)["coordination"]["lease"]
    fencing_token = 1 if current is None else current["fencing_token"] + 1
    lease = {
        "owner": "expired-public-test-owner",
        "owner_nonce": "e" * 32,
        "expires_at": "2000-01-01T00:00:00.000000Z",
        "fencing_token": fencing_token,
    }
    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO parameter_study_actions(
                action_id, operation, study_id, request_digest,
                response_json, created_at
            ) VALUES (?, 'COORDINATOR_LEASE', ?, ?, ?, ?)
            """,
            (
                f"study-internal:lease:{study_id}:{fencing_token}",
                study_id,
                hashlib.sha256(
                    canonical_json_bytes({"study_id": study_id, **lease})
                ).hexdigest(),
                canonical_json_bytes(lease).decode(),
                lease["expires_at"],
            ),
        )


def _real_attempt_executor(
    studies: ParameterStudy,
    experiments: ExperimentService,
    output_root: Path,
    *,
    before_execution=None,
):
    runner = ResolvedAttemptExecutor(
        studies.catalog,
        output_root=output_root,
        project_root=Path(__file__).parents[1],
        attempt_controller=experiments,
        identity_provider=lambda project_root, runner_image: EXECUTION_IDENTITY,
    )

    def execute(effect: dict, action_id: str) -> dict:
        if before_execution is not None:
            before_execution(effect, action_id)
        attempt = experiments.claim_next_attempt()
        assert attempt is not None
        assert attempt["attempt_id"] == effect["attempt_id"]
        experiments.record_physical_launch(
            attempt["attempt_id"],
            container_name=f"study-{attempt['attempt_id'][:12]}",
        )
        result = runner(attempt)
        experiments.record_termination(
            attempt["attempt_id"],
            exit_status=0,
            outcome="SUCCEEDED",
        )
        experiments.finish_success(
            attempt["attempt_id"],
            result_path=result["result_path"],
            result_digest=result["result_digest"],
            logs=result["logs"],
        )
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    return execute


def test_preview_freezes_one_canonical_plan_for_semantically_equivalent_inputs(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    concise = _spec()
    explicit = _explicit_spec()

    first = studies.preview(concise)
    second = studies.preview(explicit)

    assert first == second
    assert len(first["preview_digest"]) == 64
    plan = first["frozen_plan"]
    assert plan["dataset"]["snapshot_id"]
    assert plan["template"]["parameters"]["initial_capital_cny"] == 100_000.0
    assert plan["operators"]["decision"]["parameters"][
        "buy_threshold_pct_per_day"
    ] == 0.2
    assert plan["search"]["space"][
        "/operators/decision/buy_threshold_pct_per_day"
    ] == {"values": [1.0, 2.0]}
    assert plan["execution"]["identity"] == experiments.execution_identity
    assert len(plan["validation"]["outer_rounds"]) == 2
    assert len(plan["validation"]["final_search_round"]["inner_folds"]) == 2
    assert plan["holdout"]["fold_window"]["role"] == "TERMINAL_HOLDOUT"


def test_preview_estimates_bindings_from_actual_defaults_first_suggestions(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    spec["search"].update(
        {
            "unique_trial_budget": 2,
            "max_suggestions": 2,
            "space": {
                "/operators/decision/buy_threshold_pct_per_day": {
                    "values": [0.3]
                }
            },
        }
    )

    preview = studies.preview(spec)

    assert preview["frozen_plan"]["search"]["candidate_capacity"] == 1
    assert [
        item["candidate_count"] for item in preview["execution_estimate"]["rounds"]
    ] == [2, 2]
    assert preview["execution_estimate"] == {
        "minimum_experiment_bindings": 4,
        "conditional_maximum_experiment_bindings": 6,
        "selection_dependent_bindings": 2,
        "rounds": [
            {
                "search_round": "OUTER:1",
                "candidate_count": 2,
                "minimum_binding_count": 2,
                "conditional_maximum_binding_count": 3,
            },
            {
                "search_round": "FINAL",
                "candidate_count": 2,
                "minimum_binding_count": 2,
                "conditional_maximum_binding_count": 2,
            },
        ],
        "reuse_resolution": "CANONICAL_EXPERIMENT_IDENTITY_AT_DISPATCH",
    }


def test_public_preview_freezes_typed_optuna_search_and_adapter_identity(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    spec["search"].update(
        {
            "suggester": "OPTUNA_TPE",
            "unique_trial_budget": 3,
            "max_suggestions": 6,
            "space": {
                "/operators/decision/buy_threshold_pct_per_day": {
                    "kind": "float",
                    "low": 0.1,
                    "high": 0.4,
                    "step": 0.1,
                    "log": False,
                }
            },
        }
    )

    preview = studies.preview(spec)

    frozen = preview["frozen_plan"]["search"]
    assert frozen["suggester"] == "OPTUNA_TPE"
    assert frozen["adapter_identity"] == optuna_tpe_frozen_identity()
    assert frozen["space"] == spec["search"]["space"]
    assert frozen["candidate_capacity"] == 4
    assert [item["candidate_count"] for item in preview["execution_estimate"]["rounds"]] == [
        3,
        3,
    ]
    assert preview["execution_estimate"]["minimum_experiment_bindings"] == 2
    assert preview["execution_estimate"]["conditional_maximum_experiment_bindings"] == 8
    assert all(
        item["minimum_candidate_count"] == 1
        and item["conditional_maximum_candidate_count"] == 3
        and item["candidate_count_semantics"] == "ADAPTIVE_UPPER_BOUND"
        for item in preview["execution_estimate"]["rounds"]
    )


def test_public_preview_rejects_invalid_optuna_distribution(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    spec["search"].update(
        {
            "suggester": "OPTUNA_TPE",
            "space": {
                "/operators/decision/buy_threshold_pct_per_day": {
                    "kind": "float",
                    "low": 0.1,
                    "high": 0.4,
                    "step": 0.1,
                    "log": True,
                }
            },
        }
    )

    with pytest.raises(StudyValidationError, match="step and log"):
        studies.preview(spec)


def test_public_optuna_study_runs_real_adapter_to_completion(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    spec["search"].update(
        {
            "suggester": "OPTUNA_TPE",
            "unique_trial_budget": 2,
            "max_suggestions": 4,
            "space": {
                "/operators/decision/buy_threshold_pct_per_day": {
                    "kind": "float",
                    "low": 0.1,
                    "high": 0.4,
                    "step": 0.1,
                    "log": False,
                }
            },
        }
    )
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-real-optuna-study",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/optuna-real",
        effect_executor=_real_attempt_executor(
            studies,
            experiments,
            studies.catalog.state_root / "real-optuna-runs",
        ),
    )

    for _ in range(120):
        coordinator.advance(submitted["study_id"])
        detail = coordinator.detail(submitted["study_id"])
        if detail["phase"] == "COMPLETED":
            break
    else:
        pytest.fail("real Optuna Study did not complete")

    assert detail["selection_outcome"] == "CHAMPION_SELECTED"
    assert detail["holdout"]["access"] == "ACCESSED"
    assert detail["holdout"]["outcome"] in {"PASSED", "FAILED"}
    assert detail["suggestion_journal"]
    for search_round in ("OUTER:1", "FINAL"):
        events = [
            event
            for event in detail["suggestion_journal"]
            if event["search_round"] == search_round
        ]
        event_types = [event["event_type"] for event in events]
        assert event_types[0] == "SUGGESTION_RECORDED"
        assert event_types.count("SUGGESTION_RECORDED") == 2
        assert event_types.count("INNER_EVALUATION_RECORDED") == 2
        assert set(event_types) <= {
            "SUGGESTION_RECORDED",
            "DUPLICATE_SUGGESTION",
            "INNER_EVALUATION_RECORDED",
        }
        first_tell = event_types.index("INNER_EVALUATION_RECORDED")
        assert first_tell > event_types.index("SUGGESTION_RECORDED")
        assert all(
            event.get("role") in {None, "INNER_SCORE"}
            for event in events
        )


def test_creation_options_are_supplied_by_the_parameter_study_boundary(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)

    options = studies.creation_options()

    assert options["datasets"][0]["dataset_id"] == "SYNTH.SS"
    assert options["template"]["name"] == "single_stock_daily_causal"
    assert {item["slot"] for item in options["operators"]} == {
        "fit",
        "statistic",
        "smoothing",
        "decision",
        "cost",
        "sizing",
        "report",
    }
    assert all(item["versions"] for item in options["operators"])
    assert options["evaluation"]["policy_id"] == "robust_walk_forward"


def test_resolved_dataset_dates_are_interchangeable_between_preview_and_submit(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    canonical = _spec()
    midnight = _spec()
    midnight["dataset"]["start"] = f"{SESSIONS[0]}T00:00:00"
    midnight["dataset"]["end"] = f"{SESSIONS[-1]}T00:00:00"

    canonical_preview = studies.preview(canonical)
    midnight_preview = studies.preview(midnight)
    submitted = studies.submit(
        midnight,
        expected_preview_digest=canonical_preview["preview_digest"],
        action_id="canonical-dataset-dates",
    )

    assert midnight_preview["preview_digest"] == canonical_preview["preview_digest"]
    assert submitted["status"] == "SUBMITTED"
    assert submitted["study_id"] == canonical_preview["preview_digest"]


def test_preview_does_not_expose_process_global_identity_references(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    first = studies.preview(_spec())
    expected_later = deepcopy(first)

    first["frozen_plan"]["metric_engine"]["name"] = "mutated"
    first["frozen_plan"]["evaluation"]["parameter_schema"]["properties"][
        "stability_weight"
    ]["minimum"] = 0.25
    first["frozen_plan"]["evaluation"]["defaults"]["stability_weight"] = 0.75
    first["frozen_plan"]["evaluation"]["manifest"]["direction"] = "MINIMIZE"
    first["frozen_plan"]["validation"]["outer_rounds"][0]["inner_folds"][0][
        "information_interval"
    ]["signal_time"] = "MUTATED"

    later = studies.preview(_spec())

    assert later == expected_later


def test_release_locator_is_outside_the_semantic_frozen_plan(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    other_release = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/same-content-elsewhere",
    )

    first = studies.preview(_spec())
    second = other_release.preview(_spec())

    assert first == second
    assert set(first["frozen_plan"]["execution"]) == {"identity"}
    assert first["preview_digest"] == hashlib.sha256(
        canonical_json_bytes(first["frozen_plan"])
    ).hexdigest()


def test_first_submitted_release_locator_is_immutable_operational_metadata(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="original-release",
    )
    other_release = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/same-content-elsewhere",
    )

    duplicate = other_release.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="other-release",
    )

    assert duplicate["status"] == "DUPLICATE"
    assert studies.detail(preview["preview_digest"])["operational_metadata"] == {
        "release_locator": "/srv/quant/releases/154"
    }
    with studies.catalog.transaction(immediate=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE parameter_studies
                SET operational_metadata_json = '{}'
                WHERE study_id = ?
                """,
                (preview["preview_digest"],),
            )


def test_preview_fold_windows_keep_the_parent_snapshot_warmup_boundary(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)

    plan = studies.preview(_spec())["frozen_plan"]

    assert plan["dataset"]["snapshot_data_start"] == WARMUP_SESSION
    assert plan["validation"]["outer_rounds"][0]["inner_folds"][0][
        "allowed_start"
    ] == WARMUP_SESSION
    assert plan["holdout"]["fold_window"]["allowed_start"] == WARMUP_SESSION


def test_preview_fold_windows_are_the_unchanged_shared_contract_shape(
    tmp_path: Path,
    monkeypatch,
):
    studies, _ = _study_service(tmp_path)
    normalized_windows: list[dict] = []

    def record_normalized_window(value, sessions):
        normalized = normalize_fold_window(value, sessions)
        normalized_windows.append(normalized)
        return normalized

    monkeypatch.setattr(
        parameter_study_module,
        "normalize_fold_window",
        record_normalized_window,
    )

    plan = studies.preview(_spec())["frozen_plan"]
    windows = [
        fold
        for outer_round in plan["validation"]["outer_rounds"]
        for fold in [*outer_round["inner_folds"], outer_round["outer_audit"]]
    ]
    windows.extend(plan["validation"]["final_search_round"]["inner_folds"])
    windows.append(plan["holdout"]["fold_window"])

    assert windows
    assert normalized_windows == windows
    for window in windows:
        assert set(window) == FOLD_WINDOW_FIELDS
        assert normalize_fold_window(window, ALL_SESSIONS) == window


def test_parameter_study_composes_one_dataset_slice_factory_without_materializing(
    tmp_path: Path,
    monkeypatch,
):
    studies, experiments = _study_service(tmp_path)
    factory = ExecutionDatasetSliceFactory(studies.catalog.state_root)
    materialized = False

    def unexpected_materialize(*args, **kwargs):
        nonlocal materialized
        materialized = True
        raise AssertionError("preview must not materialize Execution Dataset Slices")

    monkeypatch.setattr(factory, "materialize", unexpected_materialize)
    composed = ParameterStudy.from_experiments(
        studies.catalog,
        experiments=experiments,
        release_locator="/srv/quant/releases/158",
        dataset_slice_factory=factory,
    )

    composed.preview(_spec())

    assert composed.dataset_slice_factory is factory
    assert materialized is False
    assert not any(
        name.startswith(("dataset_view", "execution_dataset_slice"))
        for name in vars(composed)
        if name != "dataset_slice_factory"
    )


def test_parameter_study_defaults_dataset_slice_factory_to_catalog_state_root(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)

    assert isinstance(studies.dataset_slice_factory, ExecutionDatasetSliceFactory)
    assert studies.dataset_slice_factory.state_root == studies.catalog.state_root


def test_submit_persists_the_frozen_projection_and_initial_event_for_detail(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())

    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-study-154",
    )

    assert submitted == {
        "status": "SUBMITTED",
        "study_id": preview["preview_digest"],
        "preview_digest": preview["preview_digest"],
    }
    assert experiments.list_experiments() == []
    detail = studies.detail(submitted["study_id"])
    assert detail["phase"] == "FROZEN"
    assert detail["control_status"] == "ACTIVE"
    assert detail["selection_outcome"] == "NOT_DETERMINED"
    assert detail["holdout"] == {
        "access": "SEALED",
        "outcome": "NOT_RUN",
        "freshness": "LEGACY_UNKNOWN",
    }
    assert detail["frozen_plan"] == preview["frozen_plan"]
    assert detail["lineage"] == preview["frozen_plan"]["lineage"]
    assert detail["identities"]["execution"] == EXECUTION_IDENTITY
    assert detail["identities"]["dataset"]["snapshot_id"] == preview["frozen_plan"][
        "dataset"
    ]["snapshot_id"]
    assert detail["events"] == [
        {
            "sequence": 1,
            "event_type": "STUDY_SUBMITTED",
            "occurred_at": detail["created_at"],
            "payload": {
                "action_id": "submit-study-154",
                "preview_digest": preview["preview_digest"],
            },
        }
    ]


def test_legacy_experiments_make_preledger_holdout_freshness_unknown(tmp_path: Path):
    studies, _ = _study_service(tmp_path, legacy_experiment=True)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="legacy-holdout-history",
    )

    assert studies.detail(submitted["study_id"])["holdout"]["freshness"] == "LEGACY_UNKNOWN"


def test_unexposed_holdout_stays_unknown_without_platform_wide_ledger(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="incomplete-platform-ledger",
    )

    with studies.catalog.connect() as connection:
        history_complete = connection.execute(
            """
            SELECT pre_ledger_history_complete
            FROM parameter_study_holdout_history_metadata
            WHERE singleton = 1
            """
        ).fetchone()[0]

    assert history_complete == 0
    assert studies.detail(submitted["study_id"])["holdout"]["freshness"] == "LEGACY_UNKNOWN"


def test_recorded_exposure_supersedes_legacy_holdout_uncertainty(tmp_path: Path):
    studies, _ = _study_service(tmp_path, legacy_experiment=True)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="legacy-exposure",
    )
    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO parameter_study_holdout_ledger(
                study_id, sequence, holdout_identity_digest,
                event_type, occurred_at, payload_json
            ) VALUES (?, 1, ?, 'EXPOSURE_RECORDED', '2026-08-28T03:00:00Z', '{}')
            """,
            (
                submitted["study_id"],
                _holdout_identity_digest(preview["frozen_plan"]),
            ),
        )

    assert studies.detail(submitted["study_id"])["holdout"] == {
        "access": "SEALED",
        "outcome": "NOT_RUN",
        "freshness": "PREVIOUSLY_EXPOSED",
    }


def test_holdout_detail_derives_append_only_access_and_exposure_ledger(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="holdout-ledger",
    )
    study_id = submitted["study_id"]
    holdout_identity = _holdout_identity_digest(preview["frozen_plan"])

    with studies.catalog.connect() as connection:
        projection_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(parameter_studies)"
            ).fetchall()
        }
    assert projection_columns.isdisjoint({"holdout_access", "holdout_freshness"})

    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO parameter_study_holdout_ledger(
                study_id, sequence, holdout_identity_digest,
                event_type, occurred_at, payload_json
            ) VALUES (?, 1, ?, 'GRANTED', '2026-08-28T01:00:00Z', '{}')
            """,
            (study_id, holdout_identity),
        )
    with pytest.raises(RuntimeError, match="projection"):
        studies.detail(study_id)

    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO parameter_study_holdout_ledger(
                study_id, sequence, holdout_identity_digest,
                event_type, occurred_at, payload_json
            ) VALUES (?, 2, ?, 'ACCESSED', '2026-08-28T02:00:00Z', '{}')
            """,
            (study_id, holdout_identity),
        )
    with pytest.raises(RuntimeError, match="projection|claim"):
        studies.detail(study_id)

    with studies.catalog.transaction(immediate=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                UPDATE parameter_study_holdout_ledger
                SET payload_json = '{"changed":true}'
                WHERE study_id = ? AND sequence = 1
                """,
                (study_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                DELETE FROM parameter_study_holdout_ledger
                WHERE study_id = ? AND sequence = 1
                """,
                (study_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE parameter_study_holdout_history_metadata
                SET pre_ledger_history_complete = 0
                WHERE singleton = 1
                """
            )


def test_identical_study_with_new_action_persists_replayable_duplicate_response(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="first-study-action",
    )

    duplicate = studies.submit(
        _explicit_spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="duplicate-study-action",
    )
    replayed = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="duplicate-study-action",
    )

    assert duplicate == {
        "status": "DUPLICATE",
        "study_id": preview["preview_digest"],
        "preview_digest": preview["preview_digest"],
    }
    assert replayed == duplicate
    assert len(studies.detail(preview["preview_digest"])["events"]) == 1
    with studies.catalog.connect() as connection:
        assert tuple(
            connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM parameter_studies),
                    (SELECT COUNT(*) FROM parameter_study_events),
                    (SELECT COUNT(*) FROM parameter_study_actions)
                """
            ).fetchone()
        ) == (1, 1, 2)


def test_successful_action_retry_returns_its_original_response_before_freshness(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="stable-submit-action",
    )
    fit = studies.catalog.operator_detail("prior_log_ols", "1.0.0")
    studies.catalog.insert_operator_version_for_test(
        operator_id="prior_log_ols",
        slot="fit",
        version="1.1.0",
        content_digest="c" * 64,
        parameter_schema=fit["parameter_schema"],
        defaults=fit["defaults"],
    )

    retried = studies.submit(
        _explicit_spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="stable-submit-action",
    )

    assert retried == submitted
    assert len(studies.detail(submitted["study_id"])["events"]) == 1


def test_explicit_operator_version_ignores_unrelated_latest_pointer_movement(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    spec = _explicit_spec()
    for operator in spec["operators"].values():
        operator["version"] = "1.0.0"
    preview = studies.preview(spec)
    fit = studies.catalog.operator_detail("prior_log_ols", "1.0.0")
    studies.catalog.insert_operator_version_for_test(
        operator_id="prior_log_ols",
        slot="fit",
        version="1.1.0",
        content_digest="d" * 64,
        parameter_schema=fit["parameter_schema"],
        defaults=fit["defaults"],
    )

    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="explicit-operator-version",
    )

    assert submitted["status"] == "SUBMITTED"
    assert submitted["study_id"] == preview["preview_digest"]


def test_latest_operator_selector_stales_when_latest_pointer_moves(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    fit = studies.catalog.operator_detail("prior_log_ols", "1.0.0")
    studies.catalog.insert_operator_version_for_test(
        operator_id="prior_log_ols",
        slot="fit",
        version="1.1.0",
        content_digest="e" * 64,
        parameter_schema=fit["parameter_schema"],
        defaults=fit["defaults"],
    )

    stale = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="latest-operator-version",
    )

    assert stale["status"] == "PREVIEW_STALE"
    assert stale["current_preview_digest"] != preview["preview_digest"]
    with pytest.raises(StudyNotFoundError):
        studies.detail(preview["preview_digest"])


def test_action_id_cannot_be_rebound_to_a_different_study_request(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="single-request-action",
    )
    changed = _spec()
    changed["search"]["unique_trial_budget"] = 3

    conflict = studies.submit(
        changed,
        expected_preview_digest=preview["preview_digest"],
        action_id="single-request-action",
    )

    assert conflict == {
        "status": "ACTION_CONFLICT",
        "action_id": "single-request-action",
    }
    assert len(studies.detail(submitted["study_id"])["events"]) == 1


def test_stale_preview_rejects_revised_external_data_without_partial_history(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    publish_snapshot(
        _bars(offset=1.0),
        studies.catalog.state_root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )

    stale = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="reusable-after-stale",
    )

    assert stale == {
        "status": "PREVIEW_STALE",
        "expected_preview_digest": preview["preview_digest"],
        "current_preview_digest": stale["current_preview_digest"],
    }
    assert stale["current_preview_digest"] != preview["preview_digest"]
    with pytest.raises(StudyNotFoundError):
        studies.detail(preview["preview_digest"])

    fresh = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=fresh["preview_digest"],
        action_id="reusable-after-stale",
    )
    assert submitted["status"] == "SUBMITTED"
    assert len(studies.detail(submitted["study_id"])["events"]) == 1


def test_submit_rejects_latest_movement_between_resolution_and_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    original_resolve = studies.datasets.resolve
    resolved_before_guard = Event()
    allow_guard = Event()
    resolve_calls = 0

    def resolve_with_race(dataset_id: str, start: str, end: str) -> dict:
        nonlocal resolve_calls
        result = original_resolve(dataset_id, start, end)
        resolve_calls += 1
        if resolve_calls == 1:
            resolved_before_guard.set()
            assert allow_guard.wait(timeout=10)
        return result

    monkeypatch.setattr(studies.datasets, "resolve", resolve_with_race)
    with ThreadPoolExecutor(max_workers=1) as executor:
        submission = executor.submit(
            studies.submit,
            _spec(),
            expected_preview_digest=preview["preview_digest"],
            action_id="dataset-race",
        )
        assert resolved_before_guard.wait(timeout=10)
        publish_snapshot(
            _bars(offset=2.0),
            studies.catalog.state_root,
            {
                "instrument": "SYNTH.SS",
                "provider": "synthetic",
                "market": "XSHG",
                "currency": "CNY",
                "adjustment": "mixed",
            },
        )
        allow_guard.set()
        stale = submission.result(timeout=10)

    assert stale["status"] == "PREVIEW_STALE"
    assert stale["expected_preview_digest"] == preview["preview_digest"]
    assert stale["current_preview_digest"] != preview["preview_digest"]
    with studies.catalog.connect() as connection:
        assert tuple(
            connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM parameter_studies),
                    (SELECT COUNT(*) FROM parameter_study_events),
                    (SELECT COUNT(*) FROM parameter_study_actions)
                """
            ).fetchone()
        ) == (0, 0, 0)


def test_preview_rejects_template_dates_outside_the_selected_dataset_range(
    tmp_path: Path,
):
    studies, _ = _study_service(tmp_path)
    spec = _spec()
    spec["template"]["parameters"]["evaluation_end"] = SESSIONS[-2]

    with pytest.raises(StudyValidationError, match="evaluation dates"):
        studies.preview(spec)


def test_preview_rejects_cost_parameters_from_the_search_space(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    spec = _spec()
    spec["search"]["space"]["/operators/cost/commission_rate"] = {
        "values": [0, 0.001]
    }

    with pytest.raises(StudyValidationError, match="cannot search cost"):
        studies.preview(spec)


def test_submit_failure_rolls_back_projection_event_and_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_spec())
    transaction = studies.catalog.transaction
    injected = False

    @contextmanager
    def fail_first_immediate_transaction(*, immediate: bool = False):
        nonlocal injected
        with transaction(immediate=immediate) as connection:
            yield connection
            if immediate and not injected:
                injected = True
                raise RuntimeError("injected commit-boundary failure")

    monkeypatch.setattr(studies.catalog, "transaction", fail_first_immediate_transaction)

    with pytest.raises(RuntimeError, match="injected commit-boundary failure"):
        studies.submit(
            _spec(),
            expected_preview_digest=preview["preview_digest"],
            action_id="retry-after-rollback",
        )

    with pytest.raises(StudyNotFoundError):
        studies.detail(preview["preview_digest"])
    retried = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="retry-after-rollback",
    )
    assert retried["status"] == "SUBMITTED"
    assert len(studies.detail(retried["study_id"])["events"]) == 1


def test_catalog_initialization_rejects_unknown_future_schema_versions(
    tmp_path: Path,
):
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    with catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (10, '2026-08-29T00:00:00Z')
            """
        )

    with pytest.raises(CatalogVersionError, match="newer than supported: 10"):
        Catalog(root).initialize()


def test_parameter_study_migration_rejects_noncontiguous_recorded_history(
    tmp_path: Path,
):
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    with catalog.transaction(immediate=True) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 2")
    datasets = DatasetService(
        catalog,
        sources={"synthetic": NoFetchSource()},
        calendars={"XSHG": FixedCalendar()},
    )
    experiments = ExperimentService(
        catalog,
        execution_identity=EXECUTION_IDENTITY,
        datasets=datasets,
    )

    with pytest.raises(CatalogVersionError, match="not contiguous"):
        ParameterStudy(
            catalog,
            datasets=datasets,
            experiments=experiments,
            release_locator="/srv/quant/releases/154",
        )

    with catalog.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 5"
        ).fetchone() is None


def test_parameter_study_migration_upgrades_v4_without_losing_catalog_data(
    tmp_path: Path,
):
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    template_before = catalog.template_detail("single_stock_daily_causal", "1")
    operators_before = catalog.list_operators()
    with catalog.connect() as connection:
        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3, 4]
    datasets = DatasetService(
        catalog,
        sources={"synthetic": NoFetchSource()},
        calendars={"XSHG": FixedCalendar()},
    )
    experiments = ExperimentService(
        catalog,
        execution_identity=EXECUTION_IDENTITY,
        datasets=datasets,
    )

    ParameterStudy(
        catalog,
        datasets=datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/154",
    )
    restarted = Catalog(root).initialize()

    assert restarted.template_detail("single_stock_daily_causal", "1") == template_before
    assert restarted.list_operators() == operators_before
    with restarted.connect() as connection:
        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_parameter_study_migration_is_safe_under_concurrent_initialization(
    tmp_path: Path,
):
    root = tmp_path / "state"
    initialize_catalog(root)

    def initialize_studies(_: int) -> ParameterStudy:
        catalog = Catalog(root).initialize()
        datasets = DatasetService(
            catalog,
            sources={"synthetic": NoFetchSource()},
            calendars={"XSHG": FixedCalendar()},
        )
        experiments = ExperimentService(
            catalog,
            execution_identity=EXECUTION_IDENTITY,
            datasets=datasets,
        )
        return ParameterStudy(
            catalog,
            datasets=datasets,
            experiments=experiments,
            release_locator="/srv/quant/releases/154",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        services = list(executor.map(initialize_studies, range(8)))

    assert len(services) == 8
    with Catalog(root).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 7"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 8"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 9"
        ).fetchone()[0] == 1


def test_catalog_extension_migration_rolls_back_an_injected_failure(tmp_path: Path):
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    broken = CatalogMigration(
        version=5,
        applied_at="2026-08-28T00:00:00Z",
        sql="""
CREATE TABLE migration_rollback_probe(value TEXT);
INSERT INTO missing_migration_target(value) VALUES ('fail');
""",
    )

    with pytest.raises(sqlite3.OperationalError, match="missing_migration_target"):
        catalog.apply_migrations([broken])

    with catalog.connect() as connection:
        assert connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'migration_rollback_probe'
            """
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 5"
        ).fetchone() is None

    datasets = DatasetService(
        catalog,
        sources={"synthetic": NoFetchSource()},
        calendars={"XSHG": FixedCalendar()},
    )
    experiments = ExperimentService(
        catalog,
        execution_identity=EXECUTION_IDENTITY,
        datasets=datasets,
    )
    ParameterStudy(
        catalog,
        datasets=datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/154",
    )


def test_list_returns_study_views_in_reverse_creation_order(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    first_spec = _spec()
    first_preview = studies.preview(first_spec)
    first = studies.submit(
        first_spec,
        expected_preview_digest=first_preview["preview_digest"],
        action_id="list-first",
    )
    second_spec = _spec()
    second_spec["search"]["unique_trial_budget"] = 3
    second_preview = studies.preview(second_spec)
    second = studies.submit(
        second_spec,
        expected_preview_digest=second_preview["preview_digest"],
        action_id="list-second",
    )

    listed = studies.list()

    assert [item["study_id"] for item in listed] == [
        second["study_id"],
        first["study_id"],
    ]
    assert all(item["phase"] == "FROZEN" for item in listed)


def test_executor_cannot_fabricate_all_ineligible_conclusion(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-no-eligible-study",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/158",
        effect_executor=lambda effect, action_id: {
            "status": "NO_ELIGIBLE_CANDIDATE",
            "explanation": "Every candidate failed its declared constraints.",
        },
    )

    coordinator.advance(submitted["study_id"])
    assert coordinator.advance(submitted["study_id"])["status"] == "ATTEMPT_SUBMITTED"
    with pytest.raises(RuntimeError, match="Experiment/Attempt identifiers"):
        coordinator.advance(submitted["study_id"])
    detail = coordinator.detail(submitted["study_id"])

    assert detail["phase"] == "VALIDATING_SELECTION_PROCESS"
    assert detail["selection_outcome"] == "NOT_DETERMINED"
    assert detail["holdout"] == {
        "access": "SEALED",
        "outcome": "NOT_RUN",
        "freshness": "LEGACY_UNKNOWN",
    }


def test_duplicate_experiment_is_not_success_and_divergence_is_contested(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    candidate = studies._round_candidates(
        preview["preview_digest"],
        preview["frozen_plan"],
        "OUTER:1",
    )[0]["configuration"]
    task = {
        key: preview["frozen_plan"]["normalized_request"][key]
        for key in ("schema_version", "dataset", "template", "operators")
    }
    created = experiments.submit(task, action_id="canonical-observation")
    duplicate = experiments.submit(task, action_id="duplicate-observation")

    assert duplicate["status"] == "DUPLICATE"
    assert (
        studies._canonical_verified_metric_document(
            experiment_id=created["experiment_id"],
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(candidate)
            ).hexdigest(),
            candidate_configuration=candidate,
            fold_window=preview["frozen_plan"]["validation"]["outer_rounds"][0][
                "inner_folds"
            ][0],
        )
        is None
    )

    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE experiments
            SET canonical_attempt_id = ?, canonical_result_digest = ?
            WHERE experiment_id = ?
            """,
            (created["attempt_id"], "a" * 64, created["experiment_id"]),
        )
        connection.execute(
            """
            UPDATE attempts
            SET status = 'SUCCEEDED', result_path = 'canonical',
                result_digest = ?, comparison = 'CANONICAL'
            WHERE attempt_id = ?
            """,
            ("a" * 64, created["attempt_id"]),
        )
        connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, experiment_id, action_id, sequence, status,
                requested_json, resolved_json, created_at, result_path,
                result_digest, comparison
            )
            SELECT ?, experiment_id, ?, 2, 'SUCCEEDED',
                   requested_json, resolved_json, created_at, 'divergent',
                   ?, 'DIVERGENT'
            FROM attempts WHERE attempt_id = ?
            """,
            ("d" * 64, "divergent-observation", "b" * 64, created["attempt_id"]),
        )

    with pytest.raises(RuntimeError, match="CONTESTED"):
        studies._canonical_verified_metric_document(
            experiment_id=created["experiment_id"],
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(candidate)
            ).hexdigest(),
            candidate_configuration=candidate,
            fold_window=preview["frozen_plan"]["validation"]["outer_rounds"][0][
                "inner_folds"
            ][0],
        )


def test_executor_cannot_fabricate_champion_or_holdout_outcome(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    preview = studies.preview(_spec())
    submitted = studies.submit(
        _spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-one-champion-study",
    )
    champion = "c" * 64

    def execute(effect: dict, action_id: str) -> dict:
        return {
            "status": "CHAMPION_SELECTED",
            "candidate_digest": champion,
            "outer_evidence_digest": "d" * 64,
            "evaluation": {
                "candidate_digest": champion,
                "eligible": True,
                "evaluation_digest": "e" * 64,
            },
        }

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/158",
        effect_executor=execute,
    )
    coordinator.advance(submitted["study_id"])
    assert coordinator.advance(submitted["study_id"])["status"] == "ATTEMPT_SUBMITTED"
    with pytest.raises(RuntimeError, match="Experiment/Attempt identifiers"):
        coordinator.advance(submitted["study_id"])
    detail = coordinator.detail(submitted["study_id"])

    assert detail["phase"] == "VALIDATING_SELECTION_PROCESS"
    assert detail["selection_outcome"] == "NOT_DETERMINED"
    assert detail["holdout"]["access"] == "SEALED"
    assert detail["holdout"]["outcome"] == "NOT_RUN"
    assert not any(
        item["evidence_type"] == "CHAMPION_FROZEN"
        for item in detail["evidence"]
    )


def test_public_study_rejects_arbitrary_executor_conclusions(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-no-executor-conclusions",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=lambda effect, action_id: {
            "status": "CHAMPION_SELECTED",
            "candidate_digest": "c" * 64,
            "evaluation": {"eligible": True},
        },
    )

    assert coordinator.advance(submitted["study_id"])["status"] == "ADVANCED"
    assert coordinator.advance(submitted["study_id"])["status"] == "ATTEMPT_SUBMITTED"
    with pytest.raises(RuntimeError, match="Experiment/Attempt identifiers"):
        coordinator.advance(submitted["study_id"])

    detail = coordinator.detail(submitted["study_id"])
    assert detail["selection_outcome"] == "NOT_DETERMINED"
    assert not any(
        item["evidence_type"] == "CHAMPION_FROZEN"
        for item in detail["evidence"]
    )


def test_public_study_owns_tasks_bindings_metrics_policy_and_outer_evidence(
    tmp_path: Path,
):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-public-orchestration",
    )
    output_root = studies.catalog.state_root / "study-runs"
    runner = ResolvedAttemptExecutor(
        studies.catalog,
        output_root=output_root,
        project_root=Path(__file__).parents[1],
        attempt_controller=experiments,
        identity_provider=lambda project_root, runner_image: EXECUTION_IDENTITY,
    )

    def execute(effect: dict, action_id: str) -> dict:
        assert set(effect) >= {
            "candidate_digest",
            "experiment_id",
            "attempt_id",
            "fold_window",
            "role",
        }
        assert "task" not in effect
        attempt = experiments.claim_next_attempt()
        assert attempt is not None
        assert attempt["attempt_id"] == effect["attempt_id"]
        experiments.record_physical_launch(
            attempt["attempt_id"],
            container_name=f"study-{attempt['attempt_id'][:12]}",
        )
        result = runner(attempt)
        experiments.record_termination(
            attempt["attempt_id"],
            exit_status=0,
            outcome="SUCCEEDED",
        )
        experiments.finish_success(
            attempt["attempt_id"],
            result_path=result["result_path"],
            result_digest=result["result_digest"],
            logs=result["logs"],
        )
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=execute,
    )
    for _ in range(40):
        result = coordinator.advance(submitted["study_id"])
        if coordinator.detail(submitted["study_id"])["phase"] == "HOLDOUT_READY":
            break
    else:
        pytest.fail(f"selection orchestration did not complete: {result}")

    detail = coordinator.detail(submitted["study_id"])
    assert detail["selection_outcome"] == "CHAMPION_SELECTED"
    candidate = next(
        trial for trial in detail["trials"] if trial["classification"] == "IN_RANGE"
    )
    assert candidate["candidate_digest"] == hashlib.sha256(
        canonical_json_bytes(candidate["configuration"])
    ).hexdigest()
    assert {binding["role"] for binding in detail["bindings"]} == {
        "INNER_SCORE",
        "OUTER_AUDIT",
    }
    assert all(binding["state"] == "VERIFIED" for binding in detail["bindings"])
    assert all(binding["experiment_id"] for binding in detail["bindings"])
    assert all(binding["attempt_id"] for binding in detail["bindings"])
    assert all(
        binding["candidate_digest"] == candidate["candidate_digest"]
        for binding in detail["bindings"]
    )
    assert detail["rankings"][0]["candidate_digest"] == candidate["candidate_digest"]
    assert detail["outer_evidence"]["account_policy"] == "FORCE_FLAT_WITH_COST"
    assert detail["outer_evidence"]["ordered_net_daily_returns"]
    assert {
        "METRIC_DOCUMENT_VERIFIED",
        "CANDIDATE_EVALUATED",
        "OUTER_SELECTION_RECORDED",
        "CHAMPION_FROZEN",
    } <= {item["evidence_type"] for item in detail["evidence"]}
    for experiment in experiments.list_experiments():
        assert experiment["dataset"]["lineage"]["kind"] == "derived_view"
        assert "evaluation" not in experiment
        assert "robust_walk_forward" not in json.dumps(experiment, sort_keys=True)


def test_public_study_rejects_mismatched_executor_attempt_identity(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-mismatched-effect-identity",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=lambda effect, action_id: {
            "experiment_id": effect["experiment_id"],
            "attempt_id": "f" * 64,
        },
    )

    assert coordinator.advance(submitted["study_id"])["status"] == "ADVANCED"
    assert coordinator.advance(submitted["study_id"])["status"] == "ATTEMPT_SUBMITTED"
    with pytest.raises(RuntimeError, match="does not match the authorized binding"):
        coordinator.advance(submitted["study_id"])

    detail = coordinator.detail(submitted["study_id"])
    assert detail["selection_outcome"] == "NOT_DETERMINED"
    assert detail["bindings"][0]["state"] == "SUBMITTED"


def test_public_study_records_divergent_attempts_as_contested(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-contested-public-study",
    )
    assert studies.advance(submitted["study_id"])["status"] == "ADVANCED"
    created = studies.advance(submitted["study_id"])
    assert created["status"] == "ATTEMPT_SUBMITTED"

    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE experiments
            SET canonical_attempt_id = ?, canonical_result_digest = ?
            WHERE experiment_id = ?
            """,
            (created["attempt_id"], "a" * 64, created["experiment_id"]),
        )
        connection.execute(
            """
            UPDATE attempts
            SET status = 'SUCCEEDED', result_path = 'canonical',
                result_digest = ?, comparison = 'CANONICAL'
            WHERE attempt_id = ?
            """,
            ("a" * 64, created["attempt_id"]),
        )
        connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, experiment_id, action_id, sequence, status,
                requested_json, resolved_json, created_at, result_path,
                result_digest, comparison
            )
            SELECT ?, experiment_id, ?, 2, 'SUCCEEDED',
                   requested_json, resolved_json, created_at, 'divergent',
                   ?, 'DIVERGENT'
            FROM attempts WHERE attempt_id = ?
            """,
            ("d" * 64, "public-divergent-attempt", "b" * 64, created["attempt_id"]),
        )

    contested = studies.advance(submitted["study_id"])

    assert contested["status"] == "EVIDENCE_CONTESTED"
    detail = studies.detail(submitted["study_id"])
    assert detail["control_status"] == "FAILED"
    assert detail["bindings"][0]["state"] == "CONTESTED"
    assert [
        item["evidence_type"] for item in detail["evidence"]
    ] == ["EVIDENCE_CONTESTED"]


def test_late_divergent_attempt_invalidates_a_selected_study(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-late-divergence-study",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=_real_attempt_executor(
            studies,
            experiments,
            studies.catalog.state_root / "study-runs",
        ),
    )
    for _ in range(40):
        coordinator.advance(submitted["study_id"])
        detail = coordinator.detail(submitted["study_id"])
        if detail["phase"] == "HOLDOUT_READY":
            break
    else:
        pytest.fail("selection did not reach HOLDOUT_READY")
    binding = next(item for item in detail["bindings"] if item["state"] == "VERIFIED")
    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, experiment_id, action_id, sequence, status,
                requested_json, resolved_json, created_at, result_path,
                result_digest, comparison
            )
            SELECT ?, experiment_id, ?, 2, 'SUCCEEDED',
                   requested_json, resolved_json, created_at, 'late-divergent',
                   ?, 'DIVERGENT'
            FROM attempts WHERE attempt_id = ?
            """,
            (
                "d" * 64,
                "late-divergent-attempt",
                "b" * 64,
                binding["attempt_id"],
            ),
        )

    result = coordinator.advance(submitted["study_id"])
    detail = coordinator.detail(submitted["study_id"])

    assert result["status"] == "EVIDENCE_CONTESTED"
    assert detail["control_status"] == "FAILED"
    assert any(item["state"] == "CONTESTED" for item in detail["bindings"])
    assert any(
        item["evidence_type"] == "EVIDENCE_CONTESTED"
        for item in detail["evidence"]
    )


def test_public_study_does_not_evaluate_partial_inner_folds(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    spec["validation"]["inner_folds"] = 2
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-partial-fold-study",
    )

    assert studies.advance(submitted["study_id"])["status"] == "ADVANCED"
    assert studies.advance(submitted["study_id"])["status"] == "ATTEMPT_SUBMITTED"

    detail = studies.detail(submitted["study_id"])
    assert len(detail["bindings"]) == 1
    assert detail["evaluations"] == []
    assert detail["rankings"] == []
    assert detail["outer_evidence"] is None
    assert detail["selection_outcome"] == "NOT_DETERMINED"


def test_failed_attempts_progress_to_no_eligible_candidate(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-all-failed-study",
    )

    for _ in range(30):
        result = studies.advance(submitted["study_id"])
        if result["status"] == "ATTEMPT_SUBMITTED":
            attempt = experiments.claim_next_attempt()
            assert attempt is not None
            assert attempt["attempt_id"] == result["attempt_id"]
            experiments.finish_failure(attempt["attempt_id"], "synthetic failure")
        detail = studies.detail(submitted["study_id"])
        if detail["phase"] == "COMPLETED":
            break
    else:
        pytest.fail("failed Study Attempts did not reach a terminal conclusion")

    assert detail["selection_outcome"] == "NO_ELIGIBLE_CANDIDATE"
    assert detail["holdout"] == {
        "access": "SEALED",
        "outcome": "NOT_RUN",
        "freshness": "LEGACY_UNKNOWN",
    }
    assert {binding["state"] for binding in detail["bindings"]} == {"FAILED"}


def test_failed_binding_follows_a_replacement_attempt(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-replacement-following-study",
    )
    assert studies.advance(submitted["study_id"])["status"] == "ADVANCED"
    dispatched = studies.advance(submitted["study_id"])
    attempt = experiments.claim_next_attempt()
    assert attempt is not None
    assert attempt["attempt_id"] == dispatched["attempt_id"]
    assert experiments.recover_abandoned_attempts(
        container_reconciler=lambda cidfile: True
    ) == 1
    assert studies.advance(submitted["study_id"])["status"] == "ATTEMPT_FAILED"
    replacement = experiments.create_replacement_attempt(
        attempt["attempt_id"],
        action_id="replacement-for-study-binding",
    )

    result = studies.advance(submitted["study_id"])
    detail = studies.detail(submitted["study_id"])
    binding = detail["bindings"][0]

    assert result["status"] == "ATTEMPT_PENDING"
    assert result["attempt_id"] == replacement["attempt_id"]
    assert binding["attempt_id"] == replacement["attempt_id"]
    assert binding["attempt"]["status"] == "PENDING"
    assert binding["state"] == "SUBMITTED"


def test_public_detail_rejects_projection_only_holdout_pass(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_minimal_orchestration_spec())
    submitted = studies.submit(
        _minimal_orchestration_spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-projection-only-pass",
    )
    with studies.catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE parameter_studies
            SET holdout_outcome = 'PASSED'
            WHERE study_id = ?
            """,
            (submitted["study_id"],),
        )

    with pytest.raises(RuntimeError, match="projection"):
        studies.detail(submitted["study_id"])


def test_holdout_access_is_recorded_before_dataset_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-holdout-materialization-crash",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=_real_attempt_executor(
            studies,
            experiments,
            studies.catalog.state_root / "study-runs",
        ),
    )
    for _ in range(40):
        coordinator.advance(submitted["study_id"])
        if coordinator.detail(submitted["study_id"])["phase"] == "HOLDOUT_READY":
            break
    else:
        pytest.fail("selection did not reach HOLDOUT_READY")
    assert coordinator.advance(submitted["study_id"])["status"] == "HOLDOUT_CLAIMED"
    original_materialize = coordinator.dataset_slice_factory.materialize

    def crash_after_materialization(*args, **kwargs):
        assert coordinator.detail(submitted["study_id"])["holdout"]["access"] == (
            "ACCESSED"
        )
        original_materialize(*args, **kwargs)
        raise RuntimeError("crash during holdout materialization")

    monkeypatch.setattr(
        coordinator.dataset_slice_factory,
        "materialize",
        crash_after_materialization,
    )
    with pytest.raises(RuntimeError, match="holdout materialization"):
        coordinator.advance(submitted["study_id"])

    detail = coordinator.detail(submitted["study_id"])
    assert detail["phase"] == "HOLDOUT_RUNNING"
    assert detail["holdout"]["access"] == "ACCESSED"
    assert not any(binding["role"] == "TERMINAL_HOLDOUT" for binding in detail["bindings"])
    _expire_study_lease(studies, submitted["study_id"])
    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=lambda effect, action_id: (_ for _ in ()).throw(
            AssertionError("terminal holdout was redispatched")
        ),
    )

    result = restarted.advance(submitted["study_id"])

    assert result["status"] == "HOLDOUT_EXECUTION_AMBIGUOUS"
    assert restarted.detail(submitted["study_id"])["control_status"] == "FAILED"


def _interrupted_terminal_holdout(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-interrupted-terminal-holdout",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=_real_attempt_executor(
            studies,
            experiments,
            studies.catalog.state_root / "study-runs",
        ),
    )
    for _ in range(40):
        coordinator.advance(submitted["study_id"])
        if coordinator.detail(submitted["study_id"])["phase"] == "HOLDOUT_READY":
            break
    else:
        pytest.fail("selection did not reach HOLDOUT_READY")
    assert coordinator.advance(submitted["study_id"])["status"] == "HOLDOUT_CLAIMED"
    dispatched = coordinator.advance(submitted["study_id"])
    assert dispatched["status"] == "ATTEMPT_SUBMITTED"
    attempt = experiments.claim_next_attempt()
    assert attempt is not None
    assert attempt["attempt_id"] == dispatched["attempt_id"]
    assert experiments.recover_abandoned_attempts(
        container_reconciler=lambda cidfile: True
    ) == 1
    assert coordinator.advance(submitted["study_id"])["status"] == "ATTEMPT_FAILED"
    return studies, experiments, coordinator, submitted["study_id"], attempt


def test_failed_terminal_holdout_stays_accessed_without_research_outcome(
    tmp_path: Path,
):
    studies, _, coordinator, study_id, attempt = _interrupted_terminal_holdout(
        tmp_path
    )

    result = coordinator.advance(study_id)
    detail = studies.detail(study_id)

    assert result == {
        "status": "HOLDOUT_EXECUTION_FAILED",
        "study_id": study_id,
        "binding_id": result["binding_id"],
        "experiment_id": result["experiment_id"],
        "attempt_id": attempt["attempt_id"],
        "access": "ACCESSED",
        "outcome": "NOT_RUN",
    }
    assert detail["control_status"] == "FAILED"
    assert detail["holdout"]["access"] == "ACCESSED"
    assert detail["holdout"]["outcome"] == "NOT_RUN"
    assert any(
        event["event_type"] == "HOLDOUT_EXECUTION_FAILED"
        for event in detail["events"]
    )


def test_failed_terminal_holdout_follows_a_replacement_attempt(tmp_path: Path):
    studies, experiments, coordinator, study_id, attempt = (
        _interrupted_terminal_holdout(tmp_path)
    )
    replacement = experiments.create_replacement_attempt(
        attempt["attempt_id"],
        action_id="replacement-terminal-holdout",
    )

    result = coordinator.advance(study_id)
    detail = studies.detail(study_id)
    binding = next(
        item for item in detail["bindings"] if item["role"] == "TERMINAL_HOLDOUT"
    )

    assert result["status"] == "ATTEMPT_PENDING"
    assert result["attempt_id"] == replacement["attempt_id"]
    assert binding["attempt_id"] == replacement["attempt_id"]
    assert binding["attempt"]["status"] == "PENDING"
    assert binding["state"] == "SUBMITTED"


def test_terminal_holdout_crash_after_access_never_redispatches(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-holdout-access-crash",
    )
    holdout_calls: list[str] = []
    coordinator: ParameterStudy

    def crash_after_access(effect: dict, action_id: str) -> None:
        if effect["role"] != "TERMINAL_HOLDOUT":
            return
        holdout_calls.append(action_id)
        assert coordinator.detail(submitted["study_id"])["holdout"]["access"] == (
            "ACCESSED"
        )
        raise RuntimeError("crash after holdout access")

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=_real_attempt_executor(
            studies,
            experiments,
            studies.catalog.state_root / "study-runs",
            before_execution=crash_after_access,
        ),
    )
    for _ in range(40):
        coordinator.advance(submitted["study_id"])
        if coordinator.detail(submitted["study_id"])["phase"] == "HOLDOUT_READY":
            break
    else:
        pytest.fail("selection did not reach HOLDOUT_READY")

    assert coordinator.advance(submitted["study_id"])["status"] == "HOLDOUT_CLAIMED"
    assert coordinator.advance(submitted["study_id"])["status"] == "ATTEMPT_SUBMITTED"
    with pytest.raises(RuntimeError, match="crash after holdout access"):
        coordinator.advance(submitted["study_id"])

    _expire_study_lease(studies, submitted["study_id"])
    restarted = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=lambda effect, action_id: (_ for _ in ()).throw(
            AssertionError("terminal holdout was redispatched")
        ),
    )
    result = restarted.advance(submitted["study_id"])

    assert result["status"] == "HOLDOUT_EXECUTION_AMBIGUOUS"
    assert holdout_calls == [
        f"study-internal:effect:{result['binding_id']}"
    ]
    detail = restarted.detail(submitted["study_id"])
    assert detail["holdout"]["access"] == "ACCESSED"
    assert detail["holdout"]["outcome"] == "NOT_RUN"


def test_lease_expiry_never_duplicates_terminal_holdout_dispatch(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-holdout-lease-expiry",
    )
    selection_executor = _real_attempt_executor(
        studies,
        experiments,
        studies.catalog.state_root / "study-runs",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        coordinator_id="holdout-first-owner",
        effect_executor=selection_executor,
    )
    for _ in range(40):
        coordinator.advance(submitted["study_id"])
        if coordinator.detail(submitted["study_id"])["phase"] == "HOLDOUT_READY":
            break
    coordinator.advance(submitted["study_id"])
    coordinator.advance(submitted["study_id"])
    dispatch_started = Event()
    release_dispatch = Event()
    holdout_calls: list[str] = []

    def blocking_executor(effect: dict, action_id: str) -> dict:
        holdout_calls.append(action_id)
        dispatch_started.set()
        assert release_dispatch.wait(timeout=10)
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    coordinator.effect_executor = blocking_executor
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(coordinator.advance, submitted["study_id"])
        assert dispatch_started.wait(timeout=10)
        _expire_study_lease(studies, submitted["study_id"])
        takeover = ParameterStudy(
            studies.catalog,
            datasets=studies.datasets,
            experiments=experiments,
            release_locator="/srv/quant/releases/161",
            coordinator_id="holdout-takeover-owner",
            effect_executor=lambda effect, action_id: (_ for _ in ()).throw(
                AssertionError("terminal holdout was dispatched twice")
            ),
        ).advance(submitted["study_id"])
        release_dispatch.set()
        stale = first.result(timeout=10)

    assert takeover["status"] == "HOLDOUT_EXECUTION_AMBIGUOUS"
    assert stale["status"] == "LEASE_BUSY"
    assert len(holdout_calls) == 1


def test_terminal_holdout_records_one_verified_champion_outcome(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-successful-terminal-holdout",
    )
    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/161",
        effect_executor=_real_attempt_executor(
            studies,
            experiments,
            studies.catalog.state_root / "study-runs",
        ),
    )
    for _ in range(60):
        result = coordinator.advance(submitted["study_id"])
        if coordinator.detail(submitted["study_id"])["phase"] == "COMPLETED":
            break
    else:
        pytest.fail(f"terminal holdout did not complete: {result}")

    detail = coordinator.detail(submitted["study_id"])
    assert result["status"] == "HOLDOUT_PASSED"
    assert detail["selection_outcome"] == "CHAMPION_SELECTED"
    assert detail["holdout"]["access"] == "ACCESSED"
    assert detail["holdout"]["outcome"] == "PASSED"
    assert detail["holdout_claim"]["candidate_digest"] == detail[
        "champion_evidence"
    ]["candidate_digest"]
    holdout_binding = next(
        binding
        for binding in detail["bindings"]
        if binding["role"] == "TERMINAL_HOLDOUT"
    )
    assert holdout_binding["state"] == "VERIFIED"
    assert detail["holdout_claim"]["binding_id"] == holdout_binding["binding_id"]
    assert detail["holdout_evidence"]["attempt_id"] == holdout_binding["attempt_id"]
    assert [
        event["event_type"] for event in detail["holdout_ledger"]
    ] == ["GRANTED", "ACCESSED"]
    assert coordinator.advance(submitted["study_id"])["status"] == "NO_CHANGE"


def test_projection_disagreement_fails_all_study_entry_points(tmp_path: Path):
    for operation in ("detail", "list", "advance", "control", "runnable"):
        studies, _ = _study_service(tmp_path / operation)
        spec = _minimal_orchestration_spec()
        preview = studies.preview(spec)
        submitted = studies.submit(
            spec,
            expected_preview_digest=preview["preview_digest"],
            action_id=f"submit-invalid-projection-{operation}",
        )
        with studies.catalog.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE parameter_studies
                SET phase = 'COMPLETED', holdout_outcome = 'PASSED'
                WHERE study_id = ?
                """,
                (submitted["study_id"],),
            )

        with pytest.raises(RuntimeError, match="projection|ledger"):
            if operation == "detail":
                studies.detail(submitted["study_id"])
            elif operation == "list":
                studies.list()
            elif operation == "advance":
                studies.advance(submitted["study_id"])
            elif operation == "control":
                studies.control(
                    submitted["study_id"],
                    "PAUSE",
                    action_id=f"pause-invalid-{operation}",
                )
            else:
                studies._advance_next_runnable()


def test_selection_restarts_after_every_durable_round_step(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-restart-every-selection-step",
    )
    effect_executor = _real_attempt_executor(
        studies,
        experiments,
        studies.catalog.state_root / "study-runs",
    )

    for restart in range(40):
        coordinator = ParameterStudy(
            studies.catalog,
            datasets=studies.datasets,
            experiments=experiments,
            release_locator="/srv/quant/releases/161",
            coordinator_id=f"selection-restart-{restart}",
            effect_executor=effect_executor,
        )
        coordinator.advance(submitted["study_id"])
        if coordinator.detail(submitted["study_id"])["phase"] == "HOLDOUT_READY":
            break
        _expire_study_lease(studies, submitted["study_id"])
    else:
        pytest.fail("restarted selection did not reach HOLDOUT_READY")

    detail = studies.detail(submitted["study_id"])
    assert detail["selection_outcome"] == "CHAMPION_SELECTED"
    assert all(binding["state"] == "VERIFIED" for binding in detail["bindings"])
    assert len(
        {
            (
                binding["search_round"],
                binding["role"],
                binding["fold_sequence"],
                binding["candidate_digest"],
            )
            for binding in detail["bindings"]
        }
    ) == len(detail["bindings"])


def test_legacy_study_detail_has_an_empty_suggestion_journal(tmp_path: Path):
    studies, _ = _study_service(tmp_path)
    preview = studies.preview(_minimal_orchestration_spec())
    submitted = studies.submit(
        _minimal_orchestration_spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-legacy-empty-journal",
    )

    assert studies.detail(submitted["study_id"])["suggestion_journal"] == []


def test_optuna_ask_is_journaled_before_dispatch_and_replays_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_optuna_contract(monkeypatch)
    studies, experiments = _study_service(tmp_path)
    spec = _optuna_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-optuna-ask-before-dispatch",
    )
    study_id = submitted["study_id"]

    assert studies.advance(study_id)["status"] == "ADVANCED"
    asked = studies.advance(study_id)
    detail = studies.detail(study_id)

    assert asked["status"] == "SUGGESTION_RECORDED"
    assert len(detail["trials"]) == 1
    assert detail["bindings"] == []
    assert [
        {
            key: event[key]
            for key in (
                "search_round",
                "sequence",
                "event_type",
                "candidate_digest",
                "sampled_parameters",
                "tell_state",
                "objective",
            )
        }
        for event in detail["suggestion_journal"]
    ] == [
        {
            "search_round": "OUTER:1",
            "sequence": 1,
            "event_type": "SUGGESTION_RECORDED",
            "candidate_digest": detail["trials"][0]["candidate_digest"],
            "sampled_parameters": {
                "/operators/decision/buy_threshold_pct_per_day": 0.2
            },
            "tell_state": None,
            "objective": None,
        }
    ]

    restarted = _restart_studies(studies, experiments, study_id)
    original_materialize = restarted.dataset_slice_factory.materialize

    def assert_ask_committed_before_materialize(*args, **kwargs):
        committed = restarted.detail(study_id)["suggestion_journal"]
        assert [event["event_type"] for event in committed] == ["SUGGESTION_RECORDED"]
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(
        restarted.dataset_slice_factory,
        "materialize",
        assert_ask_committed_before_materialize,
    )
    dispatched = restarted.advance(study_id)

    assert dispatched["status"] == "ATTEMPT_SUBMITTED"
    assert len(_ScriptedOptunaSuggester.calls) == 1
    assert restarted.advance(study_id)["status"] == "ATTEMPT_PENDING"
    assert len(_ScriptedOptunaSuggester.calls) == 1


def test_optuna_restarts_between_persisted_evaluation_tell_and_next_ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_optuna_contract(monkeypatch)
    studies, experiments = _study_service(tmp_path)
    spec = _optuna_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-optuna-restart-boundaries",
    )
    study_id = submitted["study_id"]
    effect_executor = _real_attempt_executor(
        studies,
        experiments,
        studies.catalog.state_root / "study-runs",
    )
    assert studies.advance(study_id)["status"] == "ADVANCED"
    assert studies.advance(study_id)["status"] == "SUGGESTION_RECORDED"

    for _ in range(12):
        studies = _restart_studies(
            studies,
            experiments,
            study_id,
            effect_executor=effect_executor,
        )
        studies.advance(study_id)
        detail = studies.detail(study_id)
        evaluated = next(
            (
                item
                for item in detail["evidence"]
                if item["evidence_type"] == "CANDIDATE_EVALUATED"
                and item["payload"].get("search_round") == "OUTER:1"
            ),
            None,
        )
        if evaluated is not None:
            break
    else:
        pytest.fail("the first Optuna candidate was not independently evaluated")

    assert [item["event_type"] for item in detail["suggestion_journal"]] == [
        "SUGGESTION_RECORDED"
    ]
    studies = _restart_studies(studies, experiments, study_id)
    told = studies.advance(study_id)
    detail = studies.detail(study_id)

    assert told["status"] == "INNER_EVALUATION_RECORDED"
    assert [item["event_type"] for item in detail["suggestion_journal"]] == [
        "SUGGESTION_RECORDED",
        "INNER_EVALUATION_RECORDED",
    ]
    assert detail["suggestion_journal"][1]["tell_state"] == "COMPLETE"
    assert detail["suggestion_journal"][1]["objective"] == evaluated["payload"][
        "evaluation"
    ]["validation_score"]
    assert detail["suggestion_journal"][1]["sampled_parameters"] == {
        "/operators/decision/buy_threshold_pct_per_day": 0.2
    }

    studies = _restart_studies(studies, experiments, study_id)
    second = studies.advance(study_id)
    detail = studies.detail(study_id)

    assert second["status"] == "SUGGESTION_RECORDED"
    assert len(detail["trials"]) == 2
    assert [item["event_type"] for item in detail["suggestion_journal"]] == [
        "SUGGESTION_RECORDED",
        "INNER_EVALUATION_RECORDED",
        "SUGGESTION_RECORDED",
    ]
    _, replayed_history = _ScriptedOptunaSuggester.calls[-1]
    assert replayed_history == [
        item["event"]
        for item in detail["suggestion_journal"][:2]
    ]


def test_optuna_failed_candidate_records_fail_without_score_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_optuna_contract(monkeypatch)
    studies, experiments = _study_service(tmp_path)
    spec = _optuna_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-optuna-failure-continuation",
    )
    study_id = submitted["study_id"]
    assert studies.advance(study_id)["status"] == "ADVANCED"
    assert studies.advance(study_id)["status"] == "SUGGESTION_RECORDED"
    dispatched = studies.advance(study_id)
    attempt = experiments.claim_next_attempt()
    assert attempt is not None
    assert attempt["attempt_id"] == dispatched["attempt_id"]
    experiments.finish_failure(attempt["attempt_id"], "synthetic Optuna candidate failure")
    assert studies.advance(study_id)["status"] == "ATTEMPT_FAILED"

    studies = _restart_studies(studies, experiments, study_id)
    assert studies.advance(study_id)["status"] == "INNER_EVALUATION_RECORDED"
    studies = _restart_studies(studies, experiments, study_id)
    assert studies.advance(study_id)["status"] == "SUGGESTION_RECORDED"
    detail = studies.detail(study_id)
    failed_tell = detail["suggestion_journal"][1]

    assert failed_tell["tell_state"] == "FAIL"
    assert failed_tell["objective"] is None
    assert len(detail["trials"]) == 2
    with studies.catalog.connect() as connection:
        raw_tell = connection.execute(
            """
            SELECT event_json
            FROM parameter_study_suggestion_journal
            WHERE study_id = ? AND search_round = 'OUTER:1' AND sequence = 2
            """,
            (study_id,),
        ).fetchone()["event_json"]
    assert "validation_score" not in raw_tell


def test_optuna_duplicate_is_journaled_without_creating_a_duplicate_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_optuna_contract(monkeypatch, mode="DUPLICATE")
    studies, experiments = _study_service(tmp_path)
    spec = _optuna_spec(unique_trial_budget=2, max_suggestions=3)
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-optuna-duplicate",
    )
    study_id = submitted["study_id"]
    assert studies.advance(study_id)["status"] == "ADVANCED"
    assert studies.advance(study_id)["status"] == "SUGGESTION_RECORDED"
    dispatched = studies.advance(study_id)
    attempt = experiments.claim_next_attempt()
    assert attempt is not None
    assert attempt["attempt_id"] == dispatched["attempt_id"]
    experiments.finish_failure(attempt["attempt_id"], "synthetic duplicate setup")
    assert studies.advance(study_id)["status"] == "ATTEMPT_FAILED"
    assert studies.advance(study_id)["status"] == "INNER_EVALUATION_RECORDED"

    duplicate = studies.advance(study_id)
    after_duplicate = studies.detail(study_id)
    unique = studies.advance(study_id)
    detail = studies.detail(study_id)

    assert duplicate["status"] == "DUPLICATE_SUGGESTION"
    assert len(after_duplicate["trials"]) == 1
    assert unique["status"] == "SUGGESTION_RECORDED"
    assert len(detail["trials"]) == 2
    assert [
        item["event_type"] for item in detail["suggestion_journal"]
    ] == [
        "SUGGESTION_RECORDED",
        "INNER_EVALUATION_RECORDED",
        "DUPLICATE_SUGGESTION",
        "SUGGESTION_RECORDED",
    ]
    assert detail["suggestion_journal"][0]["candidate_digest"] == detail[
        "suggestion_journal"
    ][2]["candidate_digest"]


def test_optuna_histories_are_round_local_and_exclude_outer_and_holdout_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_optuna_contract(monkeypatch)
    studies, experiments = _study_service(tmp_path)
    spec = _optuna_spec(unique_trial_budget=1, max_suggestions=1)
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-optuna-no-feedback-leakage",
    )
    study_id = submitted["study_id"]
    studies.effect_executor = _real_attempt_executor(
        studies,
        experiments,
        studies.catalog.state_root / "study-runs",
    )

    for _ in range(80):
        studies.advance(study_id)
        detail = studies.detail(study_id)
        if detail["phase"] == "HOLDOUT_READY":
            break
    else:
        pytest.fail("Optuna selection did not reach HOLDOUT_READY")

    call_count_before_holdout = len(_ScriptedOptunaSuggester.calls)
    for _ in range(20):
        studies.advance(study_id)
        detail = studies.detail(study_id)
        if detail["phase"] == "COMPLETED":
            break
    else:
        pytest.fail("the governed holdout did not complete")

    assert len(_ScriptedOptunaSuggester.calls) == call_count_before_holdout
    assert [
        (item["search_round"], item["sequence"], item["event_type"])
        for item in detail["suggestion_journal"]
    ] == [
        ("OUTER:1", 1, "SUGGESTION_RECORDED"),
        ("OUTER:1", 2, "INNER_EVALUATION_RECORDED"),
        ("FINAL", 1, "SUGGESTION_RECORDED"),
        ("FINAL", 2, "INNER_EVALUATION_RECORDED"),
    ]
    round_histories: dict[str, list[list[dict]]] = {}
    for round_identity, history in _ScriptedOptunaSuggester.calls:
        search_round = round_identity.rsplit("/", 1)[-1]
        round_histories.setdefault(search_round, []).append(history)
        assert all(
            event["event_type"]
            in {
                "SUGGESTION_RECORDED",
                "DUPLICATE_SUGGESTION",
                "INNER_EVALUATION_RECORDED",
            }
            for event in history
        )
        assert all(
            event.get("role", "INNER_SCORE") == "INNER_SCORE"
            for event in history
        )
    assert set(round_histories) == {"OUTER:1", "FINAL"}
    assert round_histories["OUTER:1"][0] == []
    assert round_histories["FINAL"][0] == []


def test_optuna_journal_is_append_only_bounded_and_rejects_non_inner_tells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_optuna_contract(monkeypatch)
    studies, _ = _study_service(tmp_path)
    spec = _optuna_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-optuna-journal-validation",
    )
    study_id = submitted["study_id"]
    assert studies.advance(study_id)["status"] == "ADVANCED"
    assert studies.advance(study_id)["status"] == "SUGGESTION_RECORDED"
    detail = studies.detail(study_id)
    candidate_digest = detail["suggestion_journal"][0]["candidate_digest"]

    with studies.catalog.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                UPDATE parameter_study_suggestion_journal
                SET event_type = 'DUPLICATE_SUGGESTION'
                WHERE study_id = ?
                """,
                (study_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM parameter_study_suggestion_journal WHERE study_id = ?",
                (study_id,),
            )

        connection.execute("BEGIN")
        invalid = {
            "event_type": "INNER_EVALUATION_RECORDED",
            "role": "OUTER_AUDIT",
            "candidate_digest": candidate_digest,
            "evaluation": {"validation_score": 999.0},
        }
        connection.execute(
            """
            INSERT INTO parameter_study_suggestion_journal(
                study_id, search_round, sequence, event_type,
                candidate_digest, event_json, occurred_at
            ) VALUES (?, 'OUTER:1', 2, 'INNER_EVALUATION_RECORDED', ?, ?, ?)
            """,
            (
                study_id,
                candidate_digest,
                canonical_json_bytes(invalid).decode(),
                studies._now(),
            ),
        )
        with pytest.raises(RuntimeError, match="Suggestion Journal"):
            studies._validate_study_projection(connection, study_id)
        connection.rollback()

    monkeypatch.setattr(parameter_study_module, "MAX_STUDY_SUGGESTION_EVENTS", 0)
    with pytest.raises(RuntimeError, match="Suggestion Journal"):
        studies.detail(study_id)


def test_public_study_enforces_session_and_detail_bounds(
    tmp_path: Path,
    monkeypatch,
):
    studies, _ = _study_service(tmp_path)
    monkeypatch.setattr(parameter_study_module, "MAX_STUDY_SESSIONS", 1)
    with pytest.raises(StudyValidationError, match="sessions"):
        studies.preview(_minimal_orchestration_spec())

    monkeypatch.setattr(parameter_study_module, "MAX_STUDY_SESSIONS", 100_000)
    preview = studies.preview(_minimal_orchestration_spec())
    submitted = studies.submit(
        _minimal_orchestration_spec(),
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-bounded-detail",
    )
    monkeypatch.setattr(parameter_study_module, "MAX_STUDY_DETAIL_BYTES", 1)
    with pytest.raises(RuntimeError, match="bounded"):
        studies.detail(submitted["study_id"])
