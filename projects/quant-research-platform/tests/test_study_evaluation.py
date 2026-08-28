import json
from pathlib import Path

import pytest

from quant_platform.resolved_runner import _result_digest
from quant_platform.strategy_runner import run_strategy_config
from quant_platform.study_evaluation import (
    EvaluationPolicyError,
    MetricDocumentFactory,
    NestedChronologicalSelection,
    RobustWalkForwardPolicy,
)

from test_strategy_runner import _derived_foundation


def _attempt_and_factory(tmp_path: Path) -> tuple[MetricDocumentFactory, dict, dict]:
    config_path, _, derived = _derived_foundation(tmp_path)
    run = run_strategy_config(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    state = Path(config["dataset"]["root"])
    manifest = json.loads(
        (
            state
            / "datasets"
            / config["dataset"]["instrument"]
            / derived["snapshot_id"]
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    attempt = {
        "status": "SUCCEEDED",
        "comparison": "CANONICAL",
        "experiment_id": "e" * 64,
        "attempt_id": "a" * 64,
        "result_path": run["path"],
        "result_digest": _result_digest(Path(run["path"])),
        "resolved": {
            "dataset": {
                "instrument": config["dataset"]["instrument"],
                "snapshot_id": derived["snapshot_id"],
                "canonical_sha256": manifest["canonical_sha256"],
                "lineage": manifest["lineage"],
            }
        },
    }
    return MetricDocumentFactory(state), attempt, manifest["lineage"]["view_spec"]


def _verified_document(tmp_path: Path) -> dict:
    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    return factory.from_attempt(
        attempt,
        candidate_digest="c" * 64,
        fold_window=fold_window,
    )


def test_metric_document_factory_verifies_and_recomputes_account_evidence(
    tmp_path: Path,
):
    document = _verified_document(tmp_path)

    assert document["candidate_digest"] == "c" * 64
    assert document["fold_window"]["account_policy"] == "FORCE_FLAT_WITH_COST"
    assert document["scored_dates"] == ["2026-01-06", "2026-01-07"]
    assert set(document["reconciliation"].values()) == {True}
    assert document["metrics"]["closed_trades"] >= 0
    assert len(document["document_digest"]) == 64


def test_metric_document_factory_rejects_a_digest_not_bound_to_artifacts(
    tmp_path: Path,
):
    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    attempt["result_digest"] = "d" * 64

    with pytest.raises(RuntimeError, match="result digest"):
        factory.from_attempt(
            attempt,
            candidate_digest="c" * 64,
            fold_window=fold_window,
        )


def test_robust_policy_is_transparent_and_uses_all_deterministic_tie_breaks(
    tmp_path: Path,
):
    first_document = _verified_document(tmp_path / "first")
    second_document = _verified_document(tmp_path / "second")
    second_document["candidate_digest"] = "d" * 64
    second_document["document_digest"] = "f" * 64
    parameters = {
        "stability_weight": 0.5,
        "turnover_weight": 0.05,
        "minimum_trades": 0,
        "maximum_drawdown": None,
        "maximum_annual_turnover": None,
    }
    policy = RobustWalkForwardPolicy()

    first = policy.evaluate("c" * 64, [first_document], parameters)
    second = policy.evaluate("d" * 64, [second_document], parameters)
    selected = policy.select([second, first])

    assert first["eligible"] is True
    assert first["validation_score"] == (
        first["independent_metrics"]["median_fold_net_sharpe"]
        - 0.5 * first["independent_metrics"]["mad_fold_net_sharpe"]
        - 0.05 * first["independent_metrics"]["annual_turnover"]
    )
    assert selected["candidate_digest"] == "c" * 64
    assert first["explanation"]["formula"]


def test_robust_policy_returns_no_candidate_when_every_candidate_is_ineligible(
    tmp_path: Path,
):
    document = _verified_document(tmp_path)
    result = RobustWalkForwardPolicy().evaluate(
        "c" * 64,
        [document],
        {
            "stability_weight": 0.5,
            "turnover_weight": 0.05,
            "minimum_trades": 10_000,
            "maximum_drawdown": None,
            "maximum_annual_turnover": None,
        },
    )

    assert result["eligibility"] == "INELIGIBLE"
    assert RobustWalkForwardPolicy().select([result]) is None


def test_nested_selection_rejects_outer_evidence_in_inner_search(tmp_path: Path):
    document = _verified_document(tmp_path)
    document["fold_window"]["role"] = "OUTER_AUDIT"

    with pytest.raises(EvaluationPolicyError, match="cannot feed inner"):
        NestedChronologicalSelection().evaluate(
            outer_rounds=[],
            final_inner_evidence={"c" * 64: [document]},
            parameters={
                "stability_weight": 0.5,
                "turnover_weight": 0.05,
                "minimum_trades": 0,
                "maximum_drawdown": None,
                "maximum_annual_turnover": None,
            },
        )
