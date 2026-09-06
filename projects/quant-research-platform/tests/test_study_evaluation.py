import copy
import hashlib
import json
import math
import shutil
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.total_return_claims as total_return_claims
from quant_platform.corporate_actions import (
    CorporateActionEvidenceError,
    admit_corporate_action_evidence,
    identity_digest,
)
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


def _seal_accounting_outcome(
    state: Path,
    result_path: Path,
    result_digest: str,
    execution_view_snapshot_id: str,
    evidence,
) -> None:
    document = copy.deepcopy(evidence.document)
    for revision in document["revisions"]:
        revision["use_role"] = "ACCOUNTING_OUTCOME"
    outcome = admit_corporate_action_evidence(document, evidence.artifact_bytes)
    package = state / "accounting-outcomes" / result_digest
    package.mkdir(parents=True)
    payloads = {
        "corporate_actions.json": (
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode(),
        **{
            descriptor["path"]: outcome.artifact_bytes[descriptor["artifact_id"]]
            for descriptor in document["artifacts"]
        },
    }
    for name, payload in payloads.items():
        path = package / name
        path.write_bytes(payload)
        path.chmod(0o444)
    result_payloads = {path.name: path.read_bytes() for path in result_path.iterdir()}
    manifest = {
        "schema_version": 1,
        "use_role": "ACCOUNTING_OUTCOME",
        "checked_as_of": document["coverage"]["payload"]["checked_as_of"],
        "attached_after_result_digest": result_digest,
        "execution_view_snapshot_id": execution_view_snapshot_id,
        "result_artifact_set_sha256": total_return_claims._artifact_set_digest(result_payloads),
        "corporate_action_evidence_sha256": outcome.digest,
        "files": {
            name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
            for name, payload in sorted(payloads.items())
        },
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o444)
    package.chmod(0o555)


def _trusted_attempt_and_factory(
    tmp_path: Path,
    *,
    no_action: bool = False,
    historical_exposure: str = "PRISTINE",
    candidate_variant: int = 0,
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
    config["operators"]["decision"]["parameters"][
        "buy_threshold_pct_per_day"
    ] += candidate_variant
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
    _seal_accounting_outcome(
        state,
        Path(run["path"]),
        attempt["result_digest"],
        derived["snapshot_id"],
        evidence,
    )
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


_POLICY_PARAMETERS = {
    "stability_weight": 0.5,
    "turnover_weight": 0.05,
    "minimum_trades": 0,
    "maximum_drawdown": None,
    "maximum_annual_turnover": None,
}


_FACTORY_MATRIX_FAILURES = {
    "C1": "quarantined action evidence cannot be accounted",
    "C2": "accounting outcome evidence is invalid",
    "C3": "accounting outcome evidence is invalid",
    "C4": "coverage or terminal event identity differs",
    "C5": "accounting outcome evidence is invalid",
    "A1": "coverage or terminal event identity differs",
    "T1": "tax or rounding policy identity is invalid",
    "T2": "settlement policy digest is invalid",
    "T3": "trade settlement posting is incomplete",
    "T4": "account settlement or tax remains open",
    "T5": "account ledger contains a negative state",
    "L1": "account trade ledger lacks the three accounts",
    "L2": "account event ledger lacks the three accounts",
    "L3": "control initial capital differs",
    "L4": "strategy and zero-cost control parity failed",
    "L5": "account cash or quantity does not reconcile",
    "I1": "account frozen metrics are not bounded integers",
    "I2": "account event ledger quantity is not an exact integer",
    "I3": "non-finite value in metrics",
    "R1": "account frozen metric fields are invalid",
    "R2": "account components do not reconcile",
}


def _load_outcome_evidence(factory: MetricDocumentFactory, result_digest: str):
    package = factory.state_root / "accounting-outcomes" / result_digest
    document = json.loads((package / "corporate_actions.json").read_text(encoding="utf-8"))
    artifacts = {
        descriptor["artifact_id"]: (package / descriptor["path"]).read_bytes()
        for descriptor in document["artifacts"]
    }
    return admit_corporate_action_evidence(document, artifacts)


def _rewrite_outcome_package(
    factory: MetricDocumentFactory,
    attempt: dict,
    case: str,
) -> None:
    package = factory.state_root / "accounting-outcomes" / attempt["result_digest"]
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    document = json.loads((package / "corporate_actions.json").read_text(encoding="utf-8"))
    artifacts = {
        descriptor["artifact_id"]: (package / descriptor["path"]).read_bytes()
        for descriptor in document["artifacts"]
    }
    if case == "C1":
        conflict = copy.deepcopy(document["revisions"][0])
        conflict["payload"]["gross_cash_per_share"] = "0.1663"
        conflict_id = identity_digest(
            "quant-platform/corporate-action-revision/v1", conflict["payload"]
        )
        conflict["event_revision_id"] = conflict_id
        conflict["normalization_digest"] = conflict_id
        document["revisions"].append(conflict)
        document["coverage"]["payload"]["event_revision_ids"].append(conflict_id)
        document["coverage"]["coverage_id"] = identity_digest(
            "quant-platform/corporate-action-coverage/v1",
            document["coverage"]["payload"],
        )
        manifest["corporate_action_evidence_sha256"] = admit_corporate_action_evidence(
            document, artifacts
        ).digest
    elif case == "C2":
        document["coverage"]["payload"]["interval_start"] = "2025-12-25"
        document["coverage"]["coverage_id"] = identity_digest(
            "quant-platform/corporate-action-coverage/v1",
            document["coverage"]["payload"],
        )
    elif case == "C3":
        artifact_id = document["artifacts"][0]["artifact_id"]
        artifacts[artifact_id] += b"corrupt"
    elif case == "C5":
        document["coverage"]["payload"]["coverage_state"] = "STALE_COMPLETE"
        document["coverage"]["coverage_id"] = identity_digest(
            "quant-platform/corporate-action-coverage/v1",
            document["coverage"]["payload"],
        )
    else:
        raise AssertionError(case)

    shutil.rmtree(package)
    package.mkdir()
    payloads = {
        "corporate_actions.json": (
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode(),
        **{
            descriptor["path"]: artifacts[descriptor["artifact_id"]]
            for descriptor in document["artifacts"]
        },
    }
    for name, payload in payloads.items():
        path = package / name
        path.write_bytes(payload)
        path.chmod(0o444)
    manifest["files"] = {
        name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in sorted(payloads.items())
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o444)
    package.chmod(0o555)


def _mutate_and_reseal_trusted_run(
    factory: MetricDocumentFactory,
    attempt: dict,
    case: str,
) -> None:
    prior_result_digest = attempt["result_digest"]
    root = Path(attempt["result_path"])
    if case in {"C1", "C2", "C3", "C5"}:
        _rewrite_outcome_package(factory, attempt, case)
        return
    outcome_evidence = _load_outcome_evidence(factory, prior_result_digest)
    root.chmod(0o755)
    for path in root.iterdir():
        path.chmod(0o644)
    if case in {
        "L1",
        "L2",
        "L3",
        "L4",
        "L5",
        "I1",
        "I2",
        "I3",
        "T3",
        "T4",
        "T5",
        "F1_MISSING",
        "F1_EXTRA",
    }:
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
        elif case == "L3":
            metrics_path = root / "metrics.json"
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            metrics["accounting_accounts"]["zero_cost"]["initial_capital_fen"] += 1
            metrics_path.write_text(
                json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            target = None
            frame = None
        elif case == "L4":
            selected = events["account"] == "zero_cost"
            events.loc[selected.idxmax(), "note"] += "-undeclared"
            target = root / "account_events.csv"
            frame = events
        elif case == "F1_MISSING":
            selected = (events["account"] == "strategy") & (
                events["event_type"] == "DIVIDEND_PAYMENT"
            )
            events = events[~selected]
            events.loc[events["account"] == "strategy", "sequence"] = range(
                1, int((events["account"] == "strategy").sum()) + 1
            )
            target = root / "account_events.csv"
            frame = events
        elif case == "F1_EXTRA":
            selected = events[
                (events["account"] == "strategy")
                & (events["event_type"] == "DIVIDEND_PAYMENT")
            ].iloc[[0]].copy()
            events = pd.concat([events, selected], ignore_index=True)
            events = events.sort_values(["account", "Date", "sequence"], kind="stable")
            events.loc[events["account"] == "strategy", "sequence"] = range(
                1, int((events["account"] == "strategy").sum()) + 1
            )
            target = root / "account_events.csv"
            frame = events
        elif case == "L5":
            events.loc[0, "cash_delta_fen"] = str(int(events.loc[0, "cash_delta_fen"]) + 1)
            target = root / "account_events.csv"
            frame = events
        elif case == "I1":
            metrics_path = root / "metrics.json"
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            metrics["accounting_accounts"]["strategy"]["gross_dividend_fen"] = True
            metrics_path.write_text(
                json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            target = None
            frame = None
        elif case == "I2":
            events.loc[0, "quantity"] = "1.0"
            target = root / "account_events.csv"
            frame = events
        elif case == "I3":
            metrics_path = root / "metrics.json"
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            metrics["period_start"] = math.nan
            metrics_path.write_text(
                json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            target = None
            frame = None
        elif case == "T3":
            selected = (events["account"] == "strategy") & (
                events["event_type"] == "ACQUISITION_SETTLEMENT"
            )
            events = events.drop(events[selected].index[0])
            events.loc[events["account"] == "strategy", "sequence"] = range(
                1, int((events["account"] == "strategy").sum()) + 1
            )
            target = root / "account_events.csv"
            frame = events
        elif case == "T4":
            final_index = events[events["account"] == "strategy"].index[-1]
            events.loc[final_index, "outstanding_tax_fen"] = "1"
            events.loc[final_index, "equity_fen"] = str(
                int(events.loc[final_index, "equity_fen"]) - 1
            )
            metrics_path = root / "metrics.json"
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            metrics["accounting_accounts"]["strategy"]["final_state"][
                "outstanding_tax_fen"
            ] = 1
            metrics["accounting_accounts"]["strategy"]["final_state"]["equity_fen"] -= 1
            metrics_path.write_text(
                json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            target = root / "account_events.csv"
            frame = events
        elif case == "T5":
            events.loc[0, "outstanding_tax_fen"] = "-1"
            target = root / "account_events.csv"
            frame = events
        else:
            raise AssertionError(case)
        if target is not None:
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
            accounting["tax_policy"].pop("tax_policy_id")
        elif case == "T2":
            accounting["settlement_schedule"]["policy_id"] = "0" * 64
        elif case == "C4":
            accounting["coverage_id"] = "0" * 64
        elif case == "A1":
            accounting["claim"] = "AFTER_TAX_TOTAL_RETURN_VERIFIED"
        elif case == "N_PRICE_ONLY":
            manifest.pop("accounting")
            manifest["identity"].pop("accounting")
        else:
            raise AssertionError(case)
        if case != "N_PRICE_ONLY":
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
    prior_package = factory.state_root / "accounting-outcomes" / prior_result_digest
    shutil.rmtree(prior_package)
    if case != "N_PRICE_ONLY":
        _seal_accounting_outcome(
            factory.state_root,
            root,
            attempt["result_digest"],
            attempt["resolved"]["dataset"]["snapshot_id"],
            outcome_evidence,
        )
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
    ids=["P1-D5-separate-outcome", "P2-D5-separate-outcome"],
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


@pytest.mark.parametrize(
    ("case", "failure"),
    sorted(_FACTORY_MATRIX_FAILURES.items()),
)
def test_adversarial_accounting_rows_reject_through_metric_document_factory(
    tmp_path: Path,
    case: str,
    failure: str,
):
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    _mutate_and_reseal_trusted_run(factory, attempt, case)

    with pytest.raises((RuntimeError, CorporateActionEvidenceError), match=failure):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


@pytest.mark.parametrize("case", ["F1_MISSING", "F1_EXTRA", "L4"])
def test_terminal_action_postings_reject_missing_extra_and_control_divergence(
    tmp_path: Path,
    case: str,
):
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path)
    candidate = attempt["candidate_configuration"]
    _mutate_and_reseal_trusted_run(factory, attempt, case)

    with pytest.raises(RuntimeError, match="account|action|posting|reconcil|settlement"):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def test_accounting_outcome_package_is_distinct_post_result_and_operator_inaccessible(
    tmp_path: Path,
):
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path)
    result = Path(attempt["result_path"])
    package = factory.state_root / "accounting-outcomes" / attempt["result_digest"]
    package_manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    run_manifest = json.loads((result / "run_manifest.json").read_text(encoding="utf-8"))
    view_digest = run_manifest["accounting"]["corporate_action_evidence_sha256"]

    assert package.is_dir()
    assert package_manifest["use_role"] == "ACCOUNTING_OUTCOME"
    assert package_manifest["attached_after_result_digest"] == attempt["result_digest"]
    assert package_manifest["corporate_action_evidence_sha256"] != view_digest
    assert package.name not in (result / "config.json").read_text(encoding="utf-8")
    assert package_manifest["corporate_action_evidence_sha256"] not in (
        result / "run_manifest.json"
    ).read_text(encoding="utf-8")

    candidate = attempt["candidate_configuration"]
    document = factory.from_attempt(
        attempt,
        candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
        candidate_configuration=candidate,
        fold_window=fold_window,
    )
    binding = document["total_return_qualification"]["bindings"]
    assert binding["corporate_action_evidence_sha256"] == package_manifest[
        "corporate_action_evidence_sha256"
    ]
    assert binding["view_corporate_action_evidence_sha256"] == view_digest


@pytest.mark.parametrize("case", ["D3", "D4"])
def test_accounting_outcome_role_and_post_result_binding_fail_closed(
    tmp_path: Path,
    case: str,
):
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path)
    package = factory.state_root / "accounting-outcomes" / attempt["result_digest"]
    package.chmod(0o755)
    manifest_path = package / "manifest.json"
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if case == "D3":
        manifest["use_role"] = "CAUSAL_FEATURE"
    else:
        manifest["execution_view_snapshot_id"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o444)
    package.chmod(0o555)
    candidate = attempt["candidate_configuration"]

    with pytest.raises(RuntimeError, match="outcome|bound|role"):
        factory.from_attempt(
            attempt,
            candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
            candidate_configuration=candidate,
            fold_window=fold_window,
        )


def _candidate_digest(attempt: dict) -> str:
    return hashlib.sha256(
        canonical_json_bytes(attempt["candidate_configuration"])
    ).hexdigest()


def _policy_evaluation(document: dict) -> dict:
    return RobustWalkForwardPolicy().evaluate(
        document["candidate_digest"], [document], _POLICY_PARAMETERS
    )


def _replace_qualification_and_reseal_document(document: dict, qualification: dict) -> None:
    document["total_return_qualification"] = qualification
    document["document_digest"] = hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in document.items() if key != "document_digest"}
        )
    ).hexdigest()


def _partial_record(record: dict) -> dict:
    partial = copy.deepcopy(record)
    partial["claim_state"] = "KNOWN_EVENT_CORRECTED_PARTIAL"
    partial["coverage_state"] = "VERIFIED_EVENTS"
    partial["coverage_basis"] = "NONE"
    partial["complete_contract_id"] = None
    partial["checks"]["coverage_complete"] = False
    partial["ranking"] = {
        "eligible_for_ranking": False,
        "eligible_for_promotion": False,
        "historical_exposure": "PRISTINE",
        "reason_codes": ["KNOWN_EVENT_PARTIAL"],
    }
    partial["transition"] = {
        "prior_qualification_id": None,
        "from_state": None,
        "to_state": "KNOWN_EVENT_CORRECTED_PARTIAL",
        "same_corporate_action_evidence": False,
    }
    partial["qualification_id"] = total_return_claims.qualification_id(
        {key: value for key, value in partial.items() if key != "qualification_id"}
    )
    total_return_claims._validate_record(partial, None)
    return partial


@pytest.mark.parametrize("case", ["A2", "A4"])
def test_study_or_plain_dictionary_cannot_reseal_factory_authority(
    tmp_path: Path,
    case: str,
):
    document = _trusted_document(tmp_path / case)
    if case == "A2":
        forged = copy.deepcopy(document["total_return_qualification"])
        forged["claim_state"] = "PRICE_RETURN_ONLY"
        forged["qualification_id"] = total_return_claims.qualification_id(
            {key: value for key, value in forged.items() if key != "qualification_id"}
        )
        _replace_qualification_and_reseal_document(document, forged)
        supplied = document
    else:
        supplied = dict(document)

    with pytest.raises(EvaluationPolicyError, match="not pristine MetricDocumentFactory-issued"):
        RobustWalkForwardPolicy().evaluate(
            supplied["candidate_digest"], [supplied], _POLICY_PARAMETERS
        )


def test_a3_request_payload_cannot_inject_verified_state_into_factory(tmp_path: Path):
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path)
    attempt["requested"]["claim_state"] = "AFTER_TAX_TOTAL_RETURN_VERIFIED"

    with pytest.raises(RuntimeError, match="Attempt audit does not match canonical execution identity"):
        factory.from_attempt(
            attempt,
            candidate_digest=_candidate_digest(attempt),
            candidate_configuration=attempt["candidate_configuration"],
            fold_window=fold_window,
        )


@pytest.mark.parametrize(
    ("case", "failure"),
    [
        ("A5", "same evidence cannot upgrade to verified"),
        ("A6", "qualification transition is illegal"),
        ("A7", "transition target is invalid"),
        ("A8", "source issuer/claim combination is forbidden"),
    ],
)
def test_transition_and_source_authority_rows_reject_before_policy(
    tmp_path: Path,
    case: str,
    failure: str,
):
    document = _trusted_document(tmp_path / case)
    verified = copy.deepcopy(document["total_return_qualification"])
    prior = None
    if case == "A5":
        prior = _partial_record(verified)
        forged = copy.deepcopy(verified)
        forged["transition"] = {
            "prior_qualification_id": prior["qualification_id"],
            "from_state": "KNOWN_EVENT_CORRECTED_PARTIAL",
            "to_state": "AFTER_TAX_TOTAL_RETURN_VERIFIED",
            "same_corporate_action_evidence": True,
        }
    elif case == "A6":
        prior = verified
        forged = _partial_record(verified)
        forged["transition"] = {
            "prior_qualification_id": prior["qualification_id"],
            "from_state": "AFTER_TAX_TOTAL_RETURN_VERIFIED",
            "to_state": "KNOWN_EVENT_CORRECTED_PARTIAL",
            "same_corporate_action_evidence": False,
        }
    elif case == "A7":
        forged = copy.deepcopy(verified)
        forged["transition"]["to_state"] = "PRICE_RETURN_ONLY"
    else:
        forged = copy.deepcopy(verified)
        forged["source_issuer"] = "CORPORATE_ACTION_COLLECTOR"
        forged["source_total_return_claim"] = "PRICE_RETURN_ONLY"
    forged["qualification_id"] = total_return_claims.qualification_id(
        {key: value for key, value in forged.items() if key != "qualification_id"}
    )
    prior_bytes = canonical_json_bytes(prior) if prior is not None else None

    with pytest.raises(total_return_claims.TotalReturnQualificationError, match=failure):
        total_return_claims._validate_record(forged, prior_bytes)
    _replace_qualification_and_reseal_document(document, forged)
    with pytest.raises(EvaluationPolicyError, match="not pristine MetricDocumentFactory-issued"):
        RobustWalkForwardPolicy().evaluate(
            document["candidate_digest"], [document], _POLICY_PARAMETERS
        )


@pytest.mark.parametrize(
    ("case", "mutation", "failure"),
    [
        ("D1", "root", "Metric Documents require an access-bounded derived dataset"),
        ("D2", "interval", "fold window does not match dataset scoring identity"),
    ],
)
def test_dataset_binding_rows_fail_at_the_factory_seam(
    tmp_path: Path,
    case: str,
    mutation: str,
    failure: str,
):
    factory, attempt, fold_window = _trusted_attempt_and_factory(tmp_path / case)
    supplied_window = copy.deepcopy(fold_window)
    if mutation == "root":
        attempt["resolved"]["dataset"]["lineage"]["kind"] = "root"
    else:
        supplied_window["scoring_end"] = supplied_window["scoring_start"]

    with pytest.raises(RuntimeError, match=failure):
        factory.from_attempt(
            attempt,
            candidate_digest=_candidate_digest(attempt),
            candidate_configuration=attempt["candidate_configuration"],
            fold_window=supplied_window,
        )


@pytest.mark.parametrize(
    ("matrix_case", "historical_exposure"),
    [("H1", "EXPOSED"), ("H2", "UNKNOWN")],
)
def test_historical_exposure_matrix_is_removed_before_policy_ranking(
    tmp_path: Path,
    matrix_case: str,
    historical_exposure: str,
):
    document = _trusted_document(tmp_path / matrix_case, historical_exposure=historical_exposure)
    qualification = document["total_return_qualification"]
    assert qualification["claim_state"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
    assert qualification["ranking"]["eligible_for_ranking"] is False
    expected_reason = (
        "HISTORICALLY_EXPOSED"
        if historical_exposure == "EXPOSED"
        else "HISTORICAL_EXPOSURE_UNKNOWN"
    )
    assert qualification["ranking"]["reason_codes"] == [expected_reason]
    evaluation = _policy_evaluation(document)
    assert evaluation["constraints"]["trusted_total_return"]["passed"] is False
    assert RobustWalkForwardPolicy().select([evaluation]) is None

    contradictory = copy.deepcopy(qualification)
    contradictory["ranking"]["eligible_for_ranking"] = True
    contradictory["ranking"]["eligible_for_promotion"] = True
    contradictory["qualification_id"] = total_return_claims.qualification_id(
        {key: value for key, value in contradictory.items() if key != "qualification_id"}
    )
    with pytest.raises(
        total_return_claims.TotalReturnQualificationError,
        match="ranking eligibility contradicts qualification",
    ):
        total_return_claims._validate_record(contradictory, None)
    _replace_qualification_and_reseal_document(document, contradictory)
    with pytest.raises(EvaluationPolicyError, match="not pristine MetricDocumentFactory-issued"):
        RobustWalkForwardPolicy().evaluate(
            document["candidate_digest"], [document], _POLICY_PARAMETERS
        )


def _price_only_document(tmp_path: Path, *, candidate_variant: int) -> dict:
    factory, attempt, fold_window = _trusted_attempt_and_factory(
        tmp_path, candidate_variant=candidate_variant
    )
    _mutate_and_reseal_trusted_run(factory, attempt, "N_PRICE_ONLY")
    return factory.from_attempt(
        attempt,
        candidate_digest=_candidate_digest(attempt),
        candidate_configuration=attempt["candidate_configuration"],
        fold_window=fold_window,
    )


def test_n1_heterogeneous_all_ineligible_has_no_champion_or_holdout(tmp_path: Path):
    price_only = _price_only_document(tmp_path / "price", candidate_variant=1)
    exposed = _trusted_document(
        tmp_path / "exposed", historical_exposure="EXPOSED", candidate_variant=2
    )
    unknown = _trusted_document(
        tmp_path / "unknown", historical_exposure="UNKNOWN", candidate_variant=3
    )
    documents = [price_only, exposed, unknown]
    qualifications = [document["total_return_qualification"] for document in documents]
    assert qualifications[0]["claim_state"] == "PRICE_RETURN_ONLY"
    assert qualifications[0]["ranking"]["reason_codes"] == ["PRICE_ONLY"]
    assert qualifications[1]["ranking"]["reason_codes"] == ["HISTORICALLY_EXPOSED"]
    assert qualifications[2]["ranking"]["reason_codes"] == ["HISTORICAL_EXPOSURE_UNKNOWN"]

    result = NestedChronologicalSelection().evaluate(
        outer_rounds=[],
        final_inner_evidence={document["candidate_digest"]: [document] for document in documents},
        parameters=_POLICY_PARAMETERS,
    )
    assert result["selection_outcome"] == "NO_ELIGIBLE_CANDIDATE"
    assert result["champion"] is None
    assert result["holdout_outcome"] == "NOT_RUN"
    assert all(not item["eligible"] for item in result["final_candidate_evaluations"])


def test_n2_numerically_better_partial_is_removed_before_trusted_ranking(tmp_path: Path):
    untrusted_document = _price_only_document(tmp_path / "partial", candidate_variant=4)
    trusted_document = _trusted_document(tmp_path / "trusted", candidate_variant=5)
    partial = _policy_evaluation(untrusted_document)
    trusted = _policy_evaluation(trusted_document)
    partial["total_return_qualifications"] = [
        total_return_claims.read_time_classification(
            source_issuer="STRATEGY_RUNNER",
            source_total_return_claim="KNOWN_EVENT_CORRECTED_PARTIAL",
            coverage_state="VERIFIED_EVENTS",
        )
    ]
    partial["validation_score"] = trusted["validation_score"] + 1_000.0
    assert partial["total_return_qualifications"][0]["claim_state"] == (
        "KNOWN_EVENT_CORRECTED_PARTIAL"
    )
    assert partial["eligible"] is False
    assert trusted["eligible"] is True

    selected = RobustWalkForwardPolicy().select([partial, trusted])
    assert selected is not None
    assert selected["candidate_digest"] == trusted_document["candidate_digest"]
    assert selected["validation_score"] < partial["validation_score"]


def test_n3_all_trusted_gate_failures_need_no_scalar_comparison_or_holdout(
    tmp_path: Path,
):
    documents = [
        _price_only_document(tmp_path / "price", candidate_variant=6),
        _trusted_document(
            tmp_path / "exposed", historical_exposure="EXPOSED", candidate_variant=7
        ),
    ]
    result = NestedChronologicalSelection().evaluate(
        outer_rounds=[],
        final_inner_evidence={document["candidate_digest"]: [document] for document in documents},
        parameters=_POLICY_PARAMETERS,
    )
    assert result["selection_outcome"] == "NO_ELIGIBLE_CANDIDATE"
    assert result["champion"] is None
    assert result["holdout_outcome"] == "NOT_RUN"


def test_o1_qualification_slice_has_no_prohibited_effect_surface(tmp_path: Path):
    matrix_case = "O1"
    factory, attempt, _ = _trusted_attempt_and_factory(tmp_path)
    state_entries = {path.name for path in factory.state_root.iterdir()}
    result_entries = {path.name for path in Path(attempt["result_path"]).iterdir()}
    assert state_entries <= {
        "datasets",
        "study-runs",
        "attempt-audit",
        "accounting-outcomes",
        "snapshot-lineage",
        ".locks",
    }
    assert not {
        "latest",
        "scheduler",
        "cron",
        "signals",
        "orders",
        "observations",
    }.intersection(state_entries | result_entries)
    assert matrix_case == "O1"


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
