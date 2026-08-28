from __future__ import annotations

import hashlib
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
from quant_platform.schemas import canonical_json_bytes
from quant_platform.seed import BUILTINS
from quant_platform.study_contracts import FOLD_WINDOW_FIELDS, normalize_fold_window
from quant_platform.study_datasets import ExecutionDatasetSliceFactory


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
    assert studies.detail(study_id)["holdout"] == {
        "access": "GRANTED",
        "outcome": "NOT_RUN",
        "freshness": "LEGACY_UNKNOWN",
    }

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
    assert studies.detail(study_id)["holdout"] == {
        "access": "ACCESSED",
        "outcome": "NOT_RUN",
        "freshness": "PREVIOUSLY_EXPOSED",
    }

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
            VALUES (6, '2026-08-29T00:00:00Z')
            """
        )

    with pytest.raises(CatalogVersionError, match="newer than supported: 6"):
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
        ] == [1, 2, 3, 4, 5]


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
