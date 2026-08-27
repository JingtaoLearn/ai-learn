from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService, TaskValidationError


FIXTURE = Path(__file__).parent / "fixtures" / "strategy" / "daily.csv"


def _service(tmp_path: Path) -> tuple[ExperimentService, str]:
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    snapshot = publish_snapshot(
        frame,
        root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )
    return (
        ExperimentService(
            catalog,
            execution_identity={
                "runner": "quant-platform",
                "source_digest": "e" * 64,
                "runtime_digest": "f" * 64,
            },
        ),
        snapshot["snapshot_id"],
    )


def _task(snapshot_id: str) -> dict:
    return {
        "schema_version": 1,
        "dataset": {
            "instrument": "SYNTH.SS",
            "snapshot_id": snapshot_id,
        },
        "template": {
            "name": "single_stock_daily_causal",
            "version": "1",
            "parameters": {
                "instrument_display_name": "Synthetic Bank",
                "evaluation_start": "2026-01-06",
                "evaluation_end": "2026-01-12",
            },
        },
        "operators": {
            "fit": {
                "operator_id": "prior_log_ols",
                "parameters": {"window_sessions": 2},
            },
            "smoothing": {
                "operator_id": "recursive_log_ema",
                "version": "1.0.0",
                "parameters": {"span_sessions": 1},
            },
            "statistic": {
                "operator_id": "adjacent_curve_pct_slope",
                "parameters": {},
            },
            "decision": {
                "operator_id": "post_start_threshold_crossing_hysteresis",
                "parameters": {
                    "buy_threshold_pct_per_day": 1.0,
                    "sell_threshold_abs_pct_per_day": 1.0,
                },
            },
            "sizing": {
                "operator_id": "all_in_all_out_a_share_lots",
                "parameters": {},
            },
            "cost": {"operator_id": "cms_china_a_share", "parameters": {}},
            "report": {
                "operator_id": "concise_chinese_causal_trade",
                "parameters": {},
            },
        },
    }


def test_latest_and_explicit_resolution_records_complete_audit(tmp_path: Path):
    service, snapshot_id = _service(tmp_path)

    resolved = service.resolve_task(_task(snapshot_id))

    fit = resolved["operators"]["fit"]
    smoothing = resolved["operators"]["smoothing"]
    assert fit["selector_mode"] == "latest"
    assert fit["requested_version"] == "latest"
    assert fit["latest_version_at_submission"] == "1.0.0"
    assert fit["resolved_version"] == "1.0.0"
    assert fit["parameters"] == {
        "price_column": "AdjustedClose",
        "window_sessions": 2,
    }
    assert smoothing["selector_mode"] == "explicit"
    assert smoothing["requested_version"] == "1.0.0"
    assert len(fit["content_digest"]) == 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda task: task.update(source="print('no')"), "unknown"),
        (
            lambda task: task["operators"]["fit"].update(source="print('no')"),
            "unknown",
        ),
        (
            lambda task: task["operators"]["fit"].update(version="9.9.9"),
            "unknown published",
        ),
        (
            lambda task: task["operators"]["fit"].update(operator_id="cms_china_a_share"),
            "slot",
        ),
        (
            lambda task: task["operators"]["fit"]["parameters"].update(undeclared=1),
            "unknown",
        ),
        (
            lambda task: task["operators"].pop("report"),
            "missing",
        ),
    ],
)
def test_task_fails_closed_on_source_unknown_versions_slots_and_params(
    tmp_path: Path, mutation, message
):
    service, snapshot_id = _service(tmp_path)
    task = _task(snapshot_id)
    mutation(task)

    with pytest.raises(TaskValidationError, match=message):
        service.resolve_task(task)


def test_exact_duplicate_returns_existing_experiment_without_new_attempt(tmp_path: Path):
    service, snapshot_id = _service(tmp_path)
    task = _task(snapshot_id)

    first = service.submit(task, action_id="create-1")
    duplicate = service.submit(task, action_id="duplicate-click")

    assert first["status"] == "CREATED"
    assert first["attempt_created"] is True
    assert duplicate == {
        "status": "DUPLICATE",
        "experiment_id": first["experiment_id"],
        "attempt_created": False,
        "attempt_id": first["attempt_id"],
    }
    assert len(service.list_attempts(first["experiment_id"])) == 1


def test_latest_and_explicit_selector_audit_share_one_canonical_experiment(
    tmp_path: Path,
):
    service, snapshot_id = _service(tmp_path)
    latest_task = _task(snapshot_id)
    explicit_task = _task(snapshot_id)
    for operator in explicit_task["operators"].values():
        operator["version"] = "1.0.0"

    latest = service.submit(latest_task, action_id="latest")
    explicit = service.submit(explicit_task, action_id="explicit")

    assert explicit["status"] == "DUPLICATE"
    assert explicit["experiment_id"] == latest["experiment_id"]


def test_concurrent_identical_submissions_converge_to_one_experiment_and_attempt(
    tmp_path: Path,
):
    service, snapshot_id = _service(tmp_path)
    task = _task(snapshot_id)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda action: service.submit(task, action_id=action),
                ["click-1", "click-2", "click-3", "click-4"],
            )
        )

    assert [result["status"] for result in results].count("CREATED") == 1
    assert [result["status"] for result in results].count("DUPLICATE") == 3
    assert len({result["experiment_id"] for result in results}) == 1
    assert len(service.list_attempts(results[0]["experiment_id"])) == 1


def test_explicit_rerun_adds_attempt_under_same_experiment_and_action_retry_converges(
    tmp_path: Path,
):
    service, snapshot_id = _service(tmp_path)
    created = service.submit(_task(snapshot_id), action_id="create")

    rerun = service.rerun(created["experiment_id"], action_id="rerun-1")
    repeated = service.rerun(created["experiment_id"], action_id="rerun-1")

    assert rerun["status"] == "CREATED"
    assert rerun["experiment_id"] == created["experiment_id"]
    assert rerun["attempt_id"] != created["attempt_id"]
    assert repeated["status"] == "NO_CHANGE"
    assert repeated["attempt_id"] == rerun["attempt_id"]
    assert [attempt["sequence"] for attempt in service.list_attempts(created["experiment_id"])] == [
        1,
        2,
    ]


def test_rerun_action_id_cannot_cross_experiment_boundaries(tmp_path: Path):
    service, snapshot_id = _service(tmp_path)
    first = service.submit(_task(snapshot_id), action_id="create-first")
    changed = _task(snapshot_id)
    changed["template"]["parameters"]["initial_capital_cny"] = 200000.0
    second = service.submit(changed, action_id="create-second")
    service.rerun(first["experiment_id"], action_id="shared-rerun")

    with pytest.raises(TaskValidationError, match="another experiment"):
        service.rerun(second["experiment_id"], action_id="shared-rerun")


def test_history_contains_unique_experiments_attempt_count_and_current_drift(tmp_path: Path):
    service, snapshot_id = _service(tmp_path)
    created = service.submit(_task(snapshot_id), action_id="create")
    service.rerun(created["experiment_id"], action_id="rerun")

    history = service.list_experiments()

    assert len(history) == 1
    assert history[0]["attempt_count"] == 2
    assert history[0]["has_drift"] is False
    detail = service.experiment_detail(created["experiment_id"])
    assert detail["attempt_count"] == 2
    assert all(not operator["drifted"] for operator in detail["operators"].values())


def test_rerun_keeps_the_experiment_resolution_frozen_while_detail_reports_drift(
    tmp_path: Path,
):
    service, snapshot_id = _service(tmp_path)
    created = service.submit(_task(snapshot_id), action_id="create")
    original = service.experiment_detail(created["experiment_id"])["operators"]["fit"]
    service.catalog.insert_operator_version_for_test(
        operator_id="prior_log_ols",
        slot="fit",
        version="1.1.0",
        content_digest="9" * 64,
        parameter_schema=service.catalog.operator_detail(
            "prior_log_ols", "1.0.0"
        )["parameter_schema"],
    )

    rerun = service.rerun(created["experiment_id"], action_id="after-drift")
    attempt = service.attempt_detail(rerun["attempt_id"])

    assert attempt["resolved"]["operators"]["fit"]["resolved_version"] == "1.0.0"
    assert attempt["resolved"]["operators"]["fit"]["latest_version_at_submission"] == "1.0.0"
    assert original["resolved_version"] == "1.0.0"
    assert service.experiment_detail(created["experiment_id"])["has_drift"] is True
