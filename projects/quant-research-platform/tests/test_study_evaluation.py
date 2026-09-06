import hashlib
import json
from pathlib import Path

import pytest

from quant_platform.resolved_runner import _result_digest
from quant_platform.schemas import canonical_json_bytes
from quant_platform.strategy_runner import run_strategy_config
from quant_platform.study_evaluation import (
    EvaluationPolicyError,
    MetricDocumentFactory,
    NestedChronologicalSelection,
    RobustWalkForwardPolicy,
)

from test_strategy_runner import _derived_foundation


def _attempt_and_factory(
    tmp_path: Path,
    *,
    outside_state_root: bool = False,
) -> tuple[MetricDocumentFactory, dict, dict]:
    config_path, _, derived = _derived_foundation(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    state = Path(config["dataset"]["root"])
    if not outside_state_root:
        config["output_root"] = str(state / "study-runs")
        config_path.write_text(json.dumps(config), encoding="utf-8")
    run = run_strategy_config(config_path)
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
        "requested": {},
        "resolved": {
            "dataset": {
                "instrument": config["dataset"]["instrument"],
                "snapshot_id": derived["snapshot_id"],
                "canonical_sha256": manifest["canonical_sha256"],
                "lineage": manifest["lineage"],
            },
            "schema_version": 1,
            "template": {
                "name": config["template"]["name"],
                "version": config["template"]["version"],
                "content_digest": "1" * 64,
                "parameters": config["template"]["parameters"],
            },
            "operators": {
                slot: {
                    "operator_id": operator["name"],
                    "resolved_version": "1.0.0",
                    "content_digest": str(index + 2) * 64,
                    "parameters": operator["parameters"],
                }
                for index, (slot, operator) in enumerate(
                    sorted(config["operators"].items())
                )
            },
            "execution_identity": {"runner": "synthetic"},
        },
    }
    attempt["candidate_configuration"] = {
        "schema_version": 1,
        "template": {
            "name": attempt["resolved"]["template"]["name"],
            "version": attempt["resolved"]["template"]["version"],
            "content_digest": attempt["resolved"]["template"]["content_digest"],
            "parameters": {
                key: value
                for key, value in attempt["resolved"]["template"]["parameters"].items()
                if key not in {"evaluation_start", "evaluation_end"}
            },
        },
        "operators": {
            slot: {
                "operator_id": operator["operator_id"],
                "version": operator["resolved_version"],
                "content_digest": operator["content_digest"],
                "parameters": operator["parameters"],
            }
            for slot, operator in attempt["resolved"]["operators"].items()
        },
    }
    audit_root = state / "attempt-audit"
    audit_root.mkdir()
    audit = {
        "schema_version": 1,
        "attempt_id": attempt["attempt_id"],
        "experiment_id": attempt["experiment_id"],
        "requested": attempt["requested"],
        "template": attempt["resolved"]["template"],
        "dataset": attempt["resolved"]["dataset"],
        "operators": attempt["resolved"]["operators"],
        "execution_identity": attempt["resolved"]["execution_identity"],
        "run_id": Path(run["path"]).name,
        "result_path": run["path"],
        "result_digest": attempt["result_digest"],
    }
    audit_path = audit_root / f"{attempt['attempt_id']}.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    audit_path.chmod(0o444)
    return MetricDocumentFactory(state), attempt, manifest["lineage"]["view_spec"]


def _verified_document(tmp_path: Path) -> dict:
    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    return factory.from_attempt(
        attempt,
        candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
        candidate_configuration=candidate,
        fold_window=fold_window,
    )


def test_metric_document_factory_verifies_and_recomputes_account_evidence(
    tmp_path: Path,
):
    document = _verified_document(tmp_path)

    assert document["candidate_digest"] == document["candidate_binding"][
        "strategy_configuration_digest"
    ]
    assert document["fold_window"]["account_policy"] == "FORCE_FLAT_WITH_COST"
    assert document["scored_dates"] == ["2026-01-06", "2026-01-07"]
    assert set(document["reconciliation"].values()) == {True}
    assert document["metrics"]["closed_trades"] >= 0
    assert len(document["document_digest"]) == 64


def test_metric_document_factory_rejects_a_digest_not_bound_to_artifacts(
    tmp_path: Path,
):
    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    attempt["result_digest"] = "d" * 64

    with pytest.raises(RuntimeError, match="result digest"):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(candidate)
            ).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def test_robust_policy_is_transparent_and_uses_all_deterministic_tie_breaks(
    tmp_path: Path,
):
    first_document = _verified_document(tmp_path / "first")
    parameters = {
        "stability_weight": 0.5,
        "turnover_weight": 0.05,
        "minimum_trades": 0,
        "maximum_drawdown": None,
        "maximum_annual_turnover": None,
    }
    policy = RobustWalkForwardPolicy()
    candidate_digest = first_document["candidate_digest"]

    first = policy.evaluate(candidate_digest, [first_document], parameters)
    second = json.loads(json.dumps(first))
    second["candidate_digest"] = "f" * 64
    second["tie_break"]["strategy_configuration_digest"] = "f" * 64
    selected = policy.select([second, first])
    ranked = sorted([second, first], key=policy.ranking_key)

    assert first["eligible"] is True
    assert first["constraints"]["trusted_total_return"]["passed"] is True
    assert first["total_return_qualifications"] == []
    assert first["validation_score"] == (
        first["independent_metrics"]["median_fold_net_sharpe"]
        - 0.5 * first["independent_metrics"]["mad_fold_net_sharpe"]
        - 0.05 * first["independent_metrics"]["annual_turnover"]
    )
    assert selected["candidate_digest"] == min(candidate_digest, "f" * 64)
    assert ranked[0] == selected
    assert first["explanation"]["formula"]


def test_robust_policy_returns_no_candidate_when_every_candidate_is_ineligible(
    tmp_path: Path,
):
    document = _verified_document(tmp_path)
    result = RobustWalkForwardPolicy().evaluate(
        document["candidate_digest"],
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
            final_inner_evidence={document["candidate_digest"]: [document]},
            parameters={
                "stability_weight": 0.5,
                "turnover_weight": 0.05,
                "minimum_trades": 0,
                "maximum_drawdown": None,
                "maximum_annual_turnover": None,
            },
        )


def test_policy_rejects_forged_plain_metric_document(tmp_path: Path):
    document = _verified_document(tmp_path)

    with pytest.raises(EvaluationPolicyError, match="MetricDocumentFactory"):
        RobustWalkForwardPolicy().evaluate(
            document["candidate_digest"],
            [dict(document)],
            {
                "stability_weight": 0.5,
                "turnover_weight": 0.05,
                "minimum_trades": 0,
                "maximum_drawdown": None,
                "maximum_annual_turnover": None,
            },
        )


def test_policy_rejects_mutated_factory_document_with_recomputed_digest(
    tmp_path: Path,
):
    document = _verified_document(tmp_path)
    with pytest.raises(AttributeError):
        document._issued_canonical_bytes = canonical_json_bytes(document)
    document["metrics"]["net_sharpe"] = 1_000_000.0
    document["document_digest"] = hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in document.items() if key != "document_digest"}
        )
    ).hexdigest()

    with pytest.raises(EvaluationPolicyError, match="not pristine"):
        RobustWalkForwardPolicy().evaluate(
            document["candidate_digest"],
            [document],
            {
                "stability_weight": 0.5,
                "turnover_weight": 0.05,
                "minimum_trades": 0,
                "maximum_drawdown": None,
                "maximum_annual_turnover": None,
            },
        )


def test_metric_factory_rejects_candidate_relabeling(tmp_path: Path):
    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    candidate = json.loads(json.dumps(attempt["candidate_configuration"]))
    candidate["operators"]["fit"]["parameters"]["window_sessions"] += 1
    candidate_digest = hashlib.sha256(canonical_json_bytes(candidate)).hexdigest()

    with pytest.raises(RuntimeError, match="does not match the Attempt"):
        factory.from_attempt(
            attempt,
            candidate_digest=candidate_digest,
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def test_metric_factory_rejects_artifacts_outside_state_root(tmp_path: Path):
    factory, attempt, fold_window = _attempt_and_factory(
        tmp_path,
        outside_state_root=True,
    )
    candidate = attempt["candidate_configuration"]

    with pytest.raises(RuntimeError, match="state root"):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(candidate)
            ).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def test_metric_factory_enforces_total_artifact_byte_bound(
    tmp_path: Path,
    monkeypatch,
):
    import quant_platform.study_evaluation as evaluation_module

    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    monkeypatch.setattr(
        evaluation_module,
        "MAX_TOTAL_RESULT_BYTES",
        1,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="total byte"):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(candidate)
            ).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def test_metric_factory_rejects_hardlinked_artifact(tmp_path: Path):
    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    result_root = Path(attempt["result_path"])
    hardlink = factory.state_root / "hardlinked-metrics.json"
    hardlink.hardlink_to(result_root / "metrics.json")

    with pytest.raises(RuntimeError, match="immutable regular file"):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(candidate)
            ).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def test_metric_factory_detects_state_root_swap_while_reading(
    tmp_path: Path,
    monkeypatch,
):
    import quant_platform.study_evaluation as evaluation_module

    factory, attempt, fold_window = _attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    original_stat = evaluation_module.os.stat
    root_stats = 0

    def swapped_stat(path, *args, **kwargs):
        nonlocal root_stats
        result = original_stat(path, *args, **kwargs)
        if Path(path) == factory.state_root and kwargs.get("follow_symlinks") is False:
            root_stats += 1
            if root_stats == 2:
                values = list(result)
                values[1] += 1
                return evaluation_module.os.stat_result(values)
        return result

    monkeypatch.setattr(evaluation_module.os, "stat", swapped_stat)

    with pytest.raises(RuntimeError, match="changed"):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(candidate)
            ).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def test_policy_enforces_metric_document_count_bound(tmp_path: Path, monkeypatch):
    import quant_platform.study_evaluation as evaluation_module

    document = _verified_document(tmp_path)
    monkeypatch.setattr(
        evaluation_module,
        "MAX_METRIC_DOCUMENTS_PER_EVALUATION",
        0,
    )

    with pytest.raises(EvaluationPolicyError, match="count"):
        RobustWalkForwardPolicy().evaluate(
            document["candidate_digest"],
            [document],
            {
                "stability_weight": 0.5,
                "turnover_weight": 0.05,
                "minimum_trades": 0,
                "maximum_drawdown": None,
                "maximum_annual_turnover": None,
            },
        )
