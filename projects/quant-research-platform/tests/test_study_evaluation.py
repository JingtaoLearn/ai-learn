import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.corporate_actions import admit_corporate_action_evidence
from quant_platform.datasets import publish_snapshot
from quant_platform.resolved_runner import _result_digest
from quant_platform.schemas import canonical_json_bytes
from quant_platform.strategy_runner import run_strategy_config
from quant_platform.study_contracts import INFORMATION_INTERVAL
from quant_platform.study_datasets import ExecutionDatasetSliceFactory
from quant_platform.study_evaluation import (
    EvaluationPolicyError,
    MetricDocumentFactory,
    NestedChronologicalSelection,
    RobustWalkForwardPolicy,
)

from test_corporate_actions import synthetic_complete_evidence_inputs
from test_strategy_replay import _bocom_accounting_case
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


def _trusted_attempt_and_factory(
    tmp_path: Path,
    *,
    no_action: bool = False,
    historical_exposure: str = "PRISTINE",
) -> tuple[MetricDocumentFactory, dict, dict]:
    frame, validated, schedule, implementations, parameters = _bocom_accounting_case()
    state = tmp_path / "state"
    evidence = admit_corporate_action_evidence(
        **synthetic_complete_evidence_inputs(no_action=no_action)
    )
    parent = publish_snapshot(
        frame,
        state,
        {
            "instrument": "601328.SS",
            "provider": "synthetic-complete-non-observational-fixture",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed-raw-and-adjusted-signal",
        },
        corporate_action_evidence=evidence,
    )
    derived = ExecutionDatasetSliceFactory(state).materialize(
        {"instrument": "601328.SS", "snapshot_id": parent["snapshot_id"]},
        {
            "allowed_start": "2025-08-13",
            "training_through": "2025-08-14",
            "available_through": "2026-06-30",
            "scoring_start": "2025-08-15",
            "scoring_end": "2026-06-30",
            "role": "INNER_SCORE",
            "information_interval": INFORMATION_INTERVAL,
            "account_policy": "FORCE_FLAT_WITH_COST",
        },
    )
    config = validated.canonical
    config["dataset"]["root"] = str(state)
    config["dataset"]["snapshot_id"] = derived["snapshot_id"]
    config["output_root"] = str(state / "study-runs")
    config_path = tmp_path / "trusted-action-strategy.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    implementations["report"] = lambda *_args, **_kwargs: "<!doctype html><html></html>"
    parameters["report"] = {}
    run = run_strategy_config(
        config_path,
        settlement_schedule=schedule,
        implementations=implementations,
        implementation_parameters=parameters,
    )
    dataset_path = state / "datasets" / "601328.SS" / derived["snapshot_id"]
    manifest = json.loads((dataset_path / "manifest.json").read_text(encoding="utf-8"))
    resolved = {
        "dataset": {
            "instrument": "601328.SS",
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
            for index, (slot, operator) in enumerate(sorted(config["operators"].items()))
        },
        "execution_identity": {"runner": "synthetic"},
    }
    requested = {"historical_exposure": historical_exposure}
    attempt = {
        "status": "SUCCEEDED",
        "comparison": "CANONICAL",
        "experiment_id": "e" * 64,
        "attempt_id": "a" * 64,
        "result_path": run["path"],
        "result_digest": _result_digest(Path(run["path"])),
        "requested": requested,
        "resolved": resolved,
    }
    attempt["candidate_configuration"] = {
        "schema_version": 1,
        "template": {
            "name": resolved["template"]["name"],
            "version": resolved["template"]["version"],
            "content_digest": resolved["template"]["content_digest"],
            "parameters": {
                key: value
                for key, value in resolved["template"]["parameters"].items()
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
            for slot, operator in resolved["operators"].items()
        },
    }
    audit_root = state / "attempt-audit"
    audit_root.mkdir()
    audit = {
        "schema_version": 1,
        "attempt_id": attempt["attempt_id"],
        "experiment_id": attempt["experiment_id"],
        "requested": requested,
        "template": resolved["template"],
        "dataset": resolved["dataset"],
        "operators": resolved["operators"],
        "execution_identity": resolved["execution_identity"],
        "run_id": Path(run["path"]).name,
        "result_path": run["path"],
        "result_digest": attempt["result_digest"],
    }
    audit_path = audit_root / f"{attempt['attempt_id']}.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    audit_path.chmod(0o444)
    return MetricDocumentFactory(state), attempt, manifest["lineage"]["view_spec"]


def _trusted_document(tmp_path: Path, **options) -> dict:
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path, **options)
    candidate = attempt["candidate_configuration"]
    return factory.from_attempt(
        attempt,
        candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
        candidate_configuration=candidate,
        fold_window=fold_window,
    )


def _mutate_and_reseal_trusted_run(
    factory: MetricDocumentFactory,
    attempt: dict,
    case: str,
) -> None:
    root = Path(attempt["result_path"])
    root.chmod(0o755)
    for path in root.iterdir():
        path.chmod(0o644)
    if case in {"L1", "L2", "L5", "I2"}:
        events = pd.read_csv(root / "account_events.csv", dtype=str, keep_default_na=False)
        trades = pd.read_csv(root / "account_trades.csv", dtype=str, keep_default_na=False)
        if case == "L1":
            trades = trades[trades["account"] != "buy_and_hold"]
            target = root / "account_trades.csv"
            frame = trades
        elif case == "L2":
            events.loc[0, "account"] = "rogue"
            target = root / "account_events.csv"
            frame = events
        elif case == "L5":
            events.loc[0, "cash_delta_fen"] = str(int(events.loc[0, "cash_delta_fen"]) + 1)
            target = root / "account_events.csv"
            frame = events
        else:
            events.loc[0, "quantity"] = "1.0"
            target = root / "account_events.csv"
            frame = events
        target.write_text(frame.to_csv(index=False, lineterminator="\n"), encoding="utf-8")
    elif case in {"R1", "R2"}:
        metrics_path = root / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        strategy = metrics["accounting_accounts"]["strategy"]
        if case == "R1":
            strategy.pop("gross_dividend_fen")
        else:
            strategy["after_tax_profit_fen"] += 1
        metrics_path.write_text(
            json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    else:
        manifest_path = root / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        accounting = manifest["accounting"]
        if case == "T1":
            accounting["tax_policy"]["sha256"] = "0" * 64
        elif case == "T2":
            accounting["settlement_schedule"]["sha256"] = "0" * 64
        elif case == "C4":
            accounting["coverage_id"] = "0" * 64
        else:
            raise AssertionError(case)
        manifest["identity"]["accounting"] = copy.deepcopy(accounting)
        manifest["run_id"] = hashlib.sha256(
            canonical_json_bytes(manifest["identity"])
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in manifest["files"]:
        payload = (root / name).read_bytes()
        manifest["files"][name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if root.name != manifest["run_id"]:
        renamed = root.parent / manifest["run_id"]
        root.rename(renamed)
        root = renamed
    for path in root.iterdir():
        path.chmod(0o444)
    root.chmod(0o555)
    attempt["result_path"] = str(root)
    attempt["result_digest"] = _result_digest(root)
    audit_path = factory.state_root / "attempt-audit" / f"{attempt['attempt_id']}.json"
    audit_path.chmod(0o644)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["run_id"] = root.name
    audit["result_path"] = str(root)
    audit["result_digest"] = attempt["result_digest"]
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    audit_path.chmod(0o444)


@pytest.mark.parametrize(
    ("no_action", "coverage_state"),
    [
        (False, "VERIFIED_COMPLETE_INTERVAL"),
        (True, "VERIFIED_NO_ACTION"),
    ],
)
def test_metric_document_factory_issues_only_from_complete_verified_graph(
    tmp_path: Path,
    no_action: bool,
    coverage_state: str,
):
    document = _trusted_document(tmp_path, no_action=no_action)
    qualification = document["total_return_qualification"]

    assert qualification["claim_state"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
    assert qualification["coverage_state"] == coverage_state
    assert set(qualification["checks"].values()) == {True}
    assert qualification["ranking"]["eligible_for_ranking"] is True
    evaluation = RobustWalkForwardPolicy().evaluate(
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
    assert evaluation["constraints"]["trusted_total_return"]["passed"] is True


@pytest.mark.parametrize("case", ["C4", "T1", "T2", "L1", "L2", "L5", "I2", "R1", "R2"])
def test_adversarial_accounting_rows_reject_through_metric_document_factory(
    tmp_path: Path,
    case: str,
):
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    _mutate_and_reseal_trusted_run(factory, attempt, case)

    with pytest.raises(RuntimeError, match="account|policy|coverage|integer|reconcil|digest"):
        factory.from_attempt(
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
