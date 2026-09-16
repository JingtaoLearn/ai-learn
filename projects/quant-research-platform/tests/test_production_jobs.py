from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from quant_platform.production_bocom import BocomProductionJob
from quant_platform.production_gold import GoldProductionJob
from quant_platform.production_contract import canonical_json_bytes
from quant_platform.production_jobs import (
    CanonicalJsonBytes,
    FormalComputation,
    ProductionInput,
    ProductionJobError,
    ProductionJobs,
    ProductionReportSpec,
    build_canonical_production_report,
    identity_canonical_bytes,
    staged_package_identity,
)
from quant_platform import production_jobs
from quant_platform import production_bocom as production_bocom_module
from quant_platform import production_gold as production_gold_module
from quant_platform.production_package_authority import (
    FilesystemPackageIdentityAuthority,
    PackageIdentityAuthorityError,
)
from quant_platform.production_schedule_client import _verify_report_document_sources
from quant_platform.production_worker import ProductionWorker


FIXTURES = Path(__file__).parent / "fixtures" / "production"
SCHEDULED = datetime(2026, 3, 9, 0, 40, tzinfo=UTC)


def assert_canonical_report_schema(document: dict) -> None:
    schema_root = Path(__file__).parent / "fixtures" / "attempt_report"
    schema = json.loads((schema_root / "REPORT-DOCUMENT.schema.json").read_text())
    resources = []
    for path in schema_root.glob("*.json"):
        value = json.loads(path.read_text())
        resources.append((f"quant-platform/report-document/{path.name}", value))
        if isinstance(value.get("$id"), str):
            resources.append((value["$id"], value))
    registry = Registry().with_contents(resources, default_specification=DRAFT202012)
    errors = list(Draft202012Validator(schema, registry=registry).iter_errors(document))
    assert not errors, "\n".join(
        f"{list(error.absolute_path)}: {error.message}" for error in errors
    )


def seal(directory: Path, payloads: dict[str, bytes]) -> str:
    directory.mkdir()
    for name, payload in payloads.items():
        member = directory / name
        member.write_bytes(payload)
        member.chmod(0o444)
    directory.chmod(0o555)
    return staged_package_identity(payloads)


def _one_entry_points(rows, _config):
    points = [
        {
            "decision_date": row["date"],
            "is_next_session": False,
            "slope_pct": -1.0 if index == 0 else 1.0,
        }
        for index, row in enumerate(rows)
    ]
    points.append(
        {
            "decision_date": None,
            "is_next_session": True,
            "slope_pct": 1.0,
            "raw_curve": rows[-1]["signal_close"],
            "smooth_curve": rows[-1]["signal_close"],
        }
    )
    return points


def formal_computation() -> FormalComputation:
    return FormalComputation(
        job_id="focus-job",
        production_manifest_sha256="1" * 64,
        operation="calibrate",
        authority_sha256="2" * 64,
        files={
            "calibration.json": b"calibration",
            "03-CALIBRATION_CLAIMED.json": b"claimed",
            "04-CALIBRATION_SEALED.json": b"sealed",
        },
        experiment_id="3" * 64,
        attempt_id="4" * 64,
    )


@pytest.mark.parametrize(
    ("job", "raw_name", "expected_name"),
    [
        (
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            "bocom-yahoo-chart.json",
            "expected-bocom-result.json",
        ),
        (
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
            "gold-au9999.tsv",
            "expected-gold-result.json",
        ),
    ],
)
def test_synthetic_jobs_preserve_frozen_action_cost_and_route(job, raw_name, expected_name) -> None:
    raw = (FIXTURES / raw_name).read_bytes()
    expected = json.loads((FIXTURES / expected_name).read_bytes())
    url = "fixture://" + raw_name

    first = job.compute(raw, url, SCHEDULED)
    second = job.compute(raw, url, SCHEDULED)

    assert first == second
    for key, value in expected.items():
        if isinstance(value, dict):
            assert all(first.action[key][nested] == nested_value for nested, nested_value in value.items())
        else:
            assert first.action[key] == value
    assert first.action["automatic_ordering"] is False
    assert first.report_uuid.encode() in first.report_html
    assert b"automatic_ordering=false" in first.report_html
    assert first.report_operator["operator_id"] == "canonical_attempt_report"
    assert first.report_operator["version"] == "1.0.0"
    assert first.report_operator["api_version"] == 2
    assert first.report_operator["source_sha256"] == (
        "11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8"
    )
    assert expected["latest_market_date"].encode() in first.report_html
    assert expected["action"].encode() in first.report_html
    assert b"Buy-and-hold is a period-dependent reference" in first.report_html
    assert b"no terminal exit or exit cost is fabricated" in first.report_html
    assert len(first.experiment_id) == len(first.attempt_id) == 64


def test_both_daily_jobs_use_the_exact_same_canonical_report_operator() -> None:
    computations = [
        BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        ),
        GoldProductionJob(FIXTURES / "gold-model-manifest.json").compute(
            (FIXTURES / "gold-au9999.tsv").read_bytes(),
            "fixture://gold-au9999.tsv",
            SCHEDULED,
        ),
    ]

    assert computations[0].report_operator == computations[1].report_operator
    assert all(b"Canonical Attempt Report" in item.report_html for item in computations)
    assert all(b'data-action="canonical"' not in item.report_html for item in computations)


def test_bocom_report_execution_and_pnl_are_not_revised_by_adjusted_close_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")

    def one_entry_points(rows, _config):
        points = [
            {
                "decision_date": row["date"],
                "is_next_session": False,
                "slope_pct": -1.0 if index == 0 else 1.0,
            }
            for index, row in enumerate(rows)
        ]
        points.append(
            {
                "decision_date": None,
                "is_next_session": True,
                "slope_pct": 1.0,
                "raw_curve": rows[-1]["signal_close"],
                "smooth_curve": rows[-1]["signal_close"],
            }
        )
        return points

    monkeypatch.setattr(production_bocom_module, "decision_points", one_entry_points)
    original_payload = json.loads((FIXTURES / "bocom-yahoo-chart.json").read_bytes())
    revised_payload = deepcopy(original_payload)
    revised = revised_payload["chart"]["result"][0]["indicators"]["adjclose"][0][
        "adjclose"
    ]
    revised_payload["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"] = [
        value * 0.5 for value in revised
    ]

    original = job.compute(
        canonical_json_bytes(original_payload), "fixture://bocom-original", SCHEDULED
    )
    rescaled = job.compute(
        canonical_json_bytes(revised_payload), "fixture://bocom-rescaled", SCHEDULED
    )
    original_config = json.loads(original.report_evidence["config.json"])["template"][
        "parameters"
    ]
    rescaled_config = json.loads(rescaled.report_evidence["config.json"])["template"][
        "parameters"
    ]

    assert original.action["action"] == rescaled.action["action"]
    assert len(original_config["event_ledger"]) == 1
    assert len(original_config["trade_ledger"]) == 1
    assert original_config["trade_ledger"][0]["status"] == "OPEN"
    assert original_config["event_ledger"][0]["price"] == 6.0
    assert original_config["trade_ledger"][0]["mark_price"] == 6.0
    assert original_config["event_ledger"] == rescaled_config["event_ledger"]
    assert original_config["trade_ledger"] == rescaled_config["trade_ledger"]
    assert original_config["performance_summary"] == rescaled_config["performance_summary"]
    assert original_config["execution_price_basis"] == (
        "next-session as-traded open reconstructed from observed Yahoo split-adjusted quote "
        "and bound split events"
    )
    assert original_config["price_unit"] == "CNY_PER_SHARE"


def test_bocom_split_and_dividend_accounting_preserves_equity_and_binds_revision_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")

    def one_entry_points(rows, _config):
        points = [
            {
                "decision_date": row["date"],
                "is_next_session": False,
                "slope_pct": -1.0 if index == 0 else 1.0,
            }
            for index, row in enumerate(rows)
        ]
        points.append(
            {
                "decision_date": None,
                "is_next_session": True,
                "slope_pct": 1.0,
                "raw_curve": rows[-1]["signal_close"],
                "smooth_curve": rows[-1]["signal_close"],
            }
        )
        return points

    monkeypatch.setattr(production_bocom_module, "decision_points", one_entry_points)
    payload = json.loads((FIXTURES / "bocom-yahoo-chart.json").read_bytes())
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    split_index = 5
    dividend_index = 7
    for name in ("open", "close"):
        quote[name] = [50.0] * len(result["timestamp"])
    quote["high"] = [value + 1.0 for value in quote["close"]]
    quote["low"] = [value - 1.0 for value in quote["close"]]
    result["events"] = {
        "splits": {
            str(result["timestamp"][split_index]): {
                "date": result["timestamp"][split_index],
                "numerator": 2.0,
                "denominator": 1.0,
                "splitRatio": "2:1",
            }
        },
        "dividends": {
            str(result["timestamp"][dividend_index]): {
                "amount": 1.0,
                "date": result["timestamp"][dividend_index],
            }
        },
    }

    computation = job.compute(
        canonical_json_bytes(payload), "fixture://bocom-corporate-actions", SCHEDULED
    )
    parameters = json.loads(computation.report_evidence["config.json"])["template"][
        "parameters"
    ]
    metrics = json.loads(computation.report_evidence["metrics.json"])
    daily = list(
        csv.DictReader(
            io.StringIO(computation.report_evidence["daily_replay.csv"].decode("utf-8"))
        )
    )
    document = json.loads(computation.report_evidence["report-document.json"])
    fields = {
        field["field_id"]: field
        for section in document["sections"]
        for field in section["fields"]
    }

    actions = parameters["corporate_action_ledger"]
    assert [item["type"] for item in actions] == ["SPLIT", "DIVIDEND"]
    assert parameters["event_ledger"][0]["price"] == 100.0
    assert actions[0]["units_before"] * 2 == actions[0]["units_after"]
    assert actions[1]["gross_cash_cny"] == pytest.approx(actions[1]["units_before"])
    assert float(daily[split_index - 1]["equity"]) == pytest.approx(
        float(daily[split_index]["equity"])
    )
    assert float(daily[dividend_index]["equity"]) == pytest.approx(
        float(daily[dividend_index - 1]["equity"]) + actions[1]["gross_cash_cny"]
    )
    assert metrics["gross_dividends_cny"] == pytest.approx(actions[1]["gross_cash_cny"])
    assert fields["gross_dividends_cny"]["availability"] == "AVAILABLE"
    assert fields["gross_dividends_cny"]["raw"] == pytest.approx(
        metrics["gross_dividends_cny"]
    )
    trade = parameters["trade_ledger"][0]
    assert trade["entry_quantity"] * 2 == trade["mark_quantity"]
    assert trade["gross_pnl_cny"] == pytest.approx(metrics["gross_dividends_cny"])
    assert metrics["final_equity_cny"] == pytest.approx(
        metrics["initial_capital_cny"] + trade["net_pnl_cny"]
    )
    provenance = json.loads(computation.report_evidence["run_manifest.json"])["runtime"]
    assert provenance["corporate_action_source"] == "Yahoo chart events=div,splits"
    assert "may revise" in provenance["corporate_action_revision_policy"]
    attestation = json.loads(computation.report_evidence["semantic-attestation.json"])
    assert attestation["authority"] == "zhlearn production report adapter"
    assert attestation["status"] == "VERIFIED"


def test_zhlearn_semantic_attestation_fails_closed_on_inconsistent_financial_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = production_jobs._equity_path

    def inconsistent_equity(*args, **kwargs):
        path, metrics = original(*args, **kwargs)
        return path, {**metrics, "final_equity_cny": metrics["final_equity_cny"] + 1.0}

    monkeypatch.setattr(production_jobs, "_equity_path", inconsistent_equity)
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")

    with pytest.raises(ProductionJobError, match="semantic attestation"):
        job.compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )


def test_zhlearn_semantic_attestation_recomputes_transaction_closing_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = production_jobs._account_events

    def inconsistent_account(*args, **kwargs):
        events, actions = original(*args, **kwargs)
        events = deepcopy(events)
        events[0]["cash_after_cny"] += 1.0
        return events, actions

    monkeypatch.setattr(production_jobs, "_account_events", inconsistent_account)
    monkeypatch.setattr(production_bocom_module, "decision_points", _one_entry_points)
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")

    with pytest.raises(ProductionJobError, match="semantic attestation"):
        job.compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )


def test_zhlearn_semantic_attestation_recomputes_trade_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = production_jobs._trade_and_holding_ledgers

    def inconsistent_trades(*args, **kwargs):
        trades, spans = original(*args, **kwargs)
        trades = deepcopy(trades)
        trades[0]["gross_pnl_cny"] += 1.0
        trades[0]["net_pnl_cny"] += 1.0
        return trades, spans

    monkeypatch.setattr(
        production_jobs, "_trade_and_holding_ledgers", inconsistent_trades
    )
    monkeypatch.setattr(production_bocom_module, "decision_points", _one_entry_points)
    job = BocomProductionJob(FIXTURES / "bocom-model-manifest.json")

    with pytest.raises(ProductionJobError, match="semantic attestation"):
        job.compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )


@pytest.mark.parametrize(
    ("module", "job", "raw_name"),
    [
        (
            production_bocom_module,
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            "bocom-yahoo-chart.json",
        ),
        (
            production_gold_module,
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
            "gold-au9999.tsv",
        ),
    ],
)
def test_both_daily_jobs_build_formally_canonical_report_documents(
    monkeypatch: pytest.MonkeyPatch, module, job, raw_name: str
) -> None:
    captured = []
    original = module.build_canonical_production_report

    def capture(**kwargs):
        report = original(**kwargs)
        captured.append((report, kwargs["normalized_bytes"]))
        return report

    monkeypatch.setattr(module, "build_canonical_production_report", capture)
    job.compute((FIXTURES / raw_name).read_bytes(), f"fixture://{raw_name}", SCHEDULED)

    assert len(captured) == 1
    report, normalized = captured[0]
    assert_canonical_report_schema(report.document)
    _verify_report_document_sources(
        report.document,
        {**report.evidence_files, "normalized-snapshot.json": normalized},
    )


def test_canonical_production_adapter_preserves_next_open_events_and_open_trade() -> None:
    rows = [
        {"date": datetime(2024, 1, 2, tzinfo=UTC).date(), "open": 100.0, "close": 101.0, "signal_close": 101.0},
        {"date": datetime(2024, 1, 3, tzinfo=UTC).date(), "open": 105.0, "close": 106.0, "signal_close": 106.0},
        {"date": datetime(2024, 1, 4, tzinfo=UTC).date(), "open": 110.0, "close": 111.0, "signal_close": 111.0},
        {"date": datetime(2024, 1, 5, tzinfo=UTC).date(), "open": 120.0, "close": 121.0, "signal_close": 121.0},
        {"date": datetime(2024, 1, 8, tzinfo=UTC).date(), "open": 130.0, "close": 132.0, "signal_close": 132.0},
    ]
    points = [
        {"decision_date": rows[1]["date"], "is_next_session": False, "slope_pct": -1.0},
        {"decision_date": rows[2]["date"], "is_next_session": False, "slope_pct": 1.0},
        {"decision_date": rows[3]["date"], "is_next_session": False, "slope_pct": -1.0},
        {"decision_date": rows[4]["date"], "is_next_session": False, "slope_pct": 1.0},
        {
            "decision_date": None,
            "is_next_session": True,
            "slope_pct": 0.0,
            "raw_curve": 132.0,
            "smooth_curve": 132.0,
        },
    ]
    action = {
        "action": "HOLD",
        "reason": "signal did not cross the frozen sell line",
        "state_before_next": 1,
        "target_state": 1,
        "generated_at": "2024-01-08T00:00:00Z",
        "latest_market_date": "2024-01-08",
        "next_session_date_estimate": "2024-01-09",
        "job_id": "fixture-job",
        "model_version": "fixture-model",
        "production_manifest_sha256": "1" * 64,
        "report_uuid": "fixture-report",
        "automatic_ordering": False,
    }
    spec = ProductionReportSpec(
        display_name="Fixture",
        qualification="DESCRIPTIVE_ONLY",
        execution_price_key="open",
        mark_price_key="signal_close",
        execution_price_basis="observed next-session raw open",
        price_unit="CNY_PER_UNIT",
        buy_cost_bps=8.0,
        sell_cost_bps=13.0,
        completed_roundtrip_cost_per_unit=0.0,
        cost_description="buy 8 bps; sell 13 bps",
        limitations=("fixture limitation",),
    )
    report = build_canonical_production_report(
        rows=rows,
        points=points,
        config=production_jobs.TrendConfig(2, 1, 0.5, -0.5, rows[1]["date"]),
        action=action,
        experiment_id="2" * 64,
        attempt_id="3" * 64,
        provider_url="fixture://prices",
        normalized_bytes=production_jobs.normalized_rows(rows).value,
        spec=spec,
    )
    assert_canonical_report_schema(report.document)
    assert frozenset(report.evidence_files) == production_jobs.REPORT_EVIDENCE_FILE_NAMES
    assert report.evidence_files["report-document.json"] == canonical_json_bytes(
        report.document
    )
    assert b"2024-01-03" in report.evidence_files["events.csv"]
    assert json.loads(report.evidence_files["config.json"])["template"]["parameters"][
        "current_action"
    ] == action

    fields = {
        field["field_id"]: field
        for section in report.document["sections"]
        for field in section["fields"]
    }
    events = fields["events"]["raw"]
    trades = fields["trades"]["raw"]
    holdings = fields["holdings"]["raw"]
    price_equity = fields["price_equity_rows"]["raw"]
    configuration = fields["template_parameters"]["raw"]

    assert [event["side"] for event in events] == ["BUY", "SELL", "BUY"]
    first_event_detail = json.loads(events[0]["reason"])
    assert first_event_detail == {
        **first_event_detail,
        "signal_date": "2024-01-03",
        "action_date": "2024-01-04",
        "position_before": 0,
        "position_after": 1,
    }
    assert events[0]["price"] == 110.0
    assert trades[0]["status"] == "CLOSED"
    assert trades[0]["quantity"] > 1
    assert trades[0]["gross_pnl_cny"] == pytest.approx(
        10.0 * trades[0]["quantity"]
    )
    assert trades[0]["net_pnl_cny"] == pytest.approx(
        trades[0]["gross_pnl_cny"]
        - trades[0]["entry_cost_cny"]
        - trades[0]["exit_cost_cny"]
    )
    assert trades[1]["status"] == "OPEN"
    assert trades[1]["exit_date"] is None
    assert trades[1]["exit_price"] is None
    assert configuration["trade_ledger"][1]["mark_date"] == "2024-01-08"
    assert configuration["trade_ledger"][1]["mark_price"] == 132.0
    assert trades[1]["net_pnl_cny"] == pytest.approx(
        trades[1]["gross_pnl_cny"] - trades[1]["entry_cost_cny"]
    )
    assert configuration["holding_spans"][-1]["status"] == "OPEN"
    assert holdings[-1]["position_after"] == 1
    assert len(price_equity) == len(rows)
    assert price_equity[0]["date"] == "2024-01-02"
    assert price_equity[0]["equity"] == 1_000_000.0
    assert price_equity[3]["equity"] == pytest.approx(
        1_000_000.0 + trades[0]["net_pnl_cny"]
    )
    assert price_equity[-1]["equity"] == pytest.approx(
        price_equity[3]["equity"] + trades[1]["net_pnl_cny"]
    )
    assert fields["final_equity_cny"]["raw"] == pytest.approx(price_equity[-1]["equity"])
    assert fields["net_profit_cny"]["raw"] == pytest.approx(
        fields["final_equity_cny"]["raw"] - fields["initial_capital_cny"]["raw"]
    )
    assert fields["total_cost_cny"]["raw"] == pytest.approx(
        sum(event["total_cost_cny"] for event in events)
    )
    assert fields["period_start"]["raw"] == "2024-01-03"
    assert configuration["current_action"] == action
    assert configuration["performance_summary"]["transitions"] == 3
    assert configuration["performance_summary"]["turnover"] == 3
    assert configuration["performance_summary"]["turnover_definition"] == (
        "sum absolute long/cash position changes; each full transition equals 1.0"
    )
    assert configuration["performance_summary"]["exposure"] == pytest.approx(0.5)
    assert "buy_and_hold_return" in configuration["performance_summary"]
    assert fields["current_position"]["raw"] == "LONG"
    assert fields["closed_trades"]["raw"] == 1
    assert fields["open_trades"]["raw"] == 1
    assert fields["operator_id"]["raw"] == "canonical_attempt_report"
    assert fields["operator_version"]["raw"] == "1.0.0"
    assert b"2024-01-03" in report.html and b"2024-01-04" in report.html
    assert b"fixture limitation" in report.html

    pending_sell_points = [*points[:-1], {**points[-1], "slope_pct": -1.0}]
    pending_sell_action = {
        **action,
        "action": "SELL",
        "reason": "signal crossed downward through the frozen sell line",
        "target_state": 0,
    }
    pending_sell = build_canonical_production_report(
        rows=rows,
        points=pending_sell_points,
        config=production_jobs.TrendConfig(2, 1, 0.5, -0.5, rows[1]["date"]),
        action=pending_sell_action,
        experiment_id="2" * 64,
        attempt_id="3" * 64,
        provider_url="fixture://prices",
        normalized_bytes=production_jobs.normalized_rows(rows).value,
        spec=spec,
    )
    pending_fields = {
        field["field_id"]: field
        for section in pending_sell.document["sections"]
        for field in section["fields"]
    }
    assert len(pending_fields["events"]["raw"]) == 3
    assert pending_fields["trades"]["raw"][-1]["status"] == "OPEN"
    assert pending_fields["trades"]["raw"][-1]["exit_date"] is None
    assert pending_fields["template_parameters"]["raw"]["current_action"]["action"] == "SELL"


def test_canonical_production_report_keeps_a_complete_large_price_path() -> None:
    start = datetime(2010, 1, 1, tzinfo=UTC).date()
    rows = [
        {
            "date": start + timedelta(days=index),
            "open": 100.0 + index / 100,
            "close": 100.5 + index / 100,
            "signal_close": 100.5 + index / 100,
        }
        for index in range(4_500)
    ]
    points = [
        {"decision_date": rows[1]["date"], "is_next_session": False, "slope_pct": None},
        {"decision_date": rows[2]["date"], "is_next_session": False, "slope_pct": 0.0},
        {
            "decision_date": None,
            "is_next_session": True,
            "slope_pct": 0.0,
            "raw_curve": rows[-1]["signal_close"],
            "smooth_curve": rows[-1]["signal_close"],
        },
    ]
    action = {
        "action": "WAIT",
        "reason": "no upward crossing; remain flat",
        "state_before_next": 0,
        "target_state": 0,
        "generated_at": "2026-01-01T00:00:00Z",
        "latest_market_date": rows[-1]["date"].isoformat(),
        "next_session_date_estimate": (rows[-1]["date"] + timedelta(days=1)).isoformat(),
        "job_id": "large-fixture-job",
        "model_version": "large-fixture-model",
        "production_manifest_sha256": "4" * 64,
        "report_uuid": "large-fixture-report",
        "automatic_ordering": False,
    }
    report = build_canonical_production_report(
        rows=rows,
        points=points,
        config=production_jobs.TrendConfig(2, 1, 0.5, -0.5, rows[1]["date"]),
        action=action,
        experiment_id="5" * 64,
        attempt_id="6" * 64,
        provider_url="fixture://large-prices",
        normalized_bytes=production_jobs.normalized_rows(rows).value,
        spec=ProductionReportSpec(
            display_name="Large fixture",
            qualification="DESCRIPTIVE_ONLY",
            execution_price_key="open",
            mark_price_key="signal_close",
            execution_price_basis="observed next-session open",
            price_unit="CNY_PER_UNIT",
            buy_cost_bps=0.0,
            sell_cost_bps=0.0,
            completed_roundtrip_cost_per_unit=0.0,
            cost_description="no fixture costs",
            limitations=("large fixture limitation",),
        ),
    )
    fields = {
        field["field_id"]: field
        for section in report.document["sections"]
        for field in section["fields"]
    }

    assert len(fields["price_equity_rows"]["raw"]) == 4_500
    assert b"QR-HOLDINGS-TSV-1" in report.html
    assert f"{rows[-1]['date'].isoformat()}\t0\t0".encode() in report.html
    assert rows[0]["date"].isoformat().encode() in report.html
    assert rows[-1]["date"].isoformat().encode() in report.html
    assert len(report.html) < 700_000


@pytest.mark.parametrize(
    ("job", "raw_name", "actions", "required"),
    [
        (
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
            "gold-au9999.tsv",
            (
                ("WAIT", 0, 0, "买入"),
                ("BUY", 0, 1, "卖出"),
                ("HOLD", 1, 1, "卖出"),
                ("SELL", 1, 0, "买入"),
            ),
            (
                "黄金生产信号",
                "完成收盘",
                "日涨跌",
                "仓位：当前",
                "斜率：上一",
                "执行时点",
                "SGE_AU9999_PROXY",
                "FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G",
                "市场代理评估，不代表招行实际可成交收益",
                "automatic_ordering=false",
            ),
        ),
        (
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            "bocom-yahoo-chart.json",
            (
                ("WAIT", 0, 0, "买入"),
                ("BUY", 0, 1, "卖出"),
                ("HOLD", 1, 1, "卖出"),
                ("SELL", 1, 0, "买入"),
            ),
            (
                "交通银行生产信号",
                "完成收盘",
                "日涨跌",
                "仓位：当前",
                "斜率：上一",
                "执行时点",
                "假设无公司行动的原始收盘价",
                "automatic_ordering=false",
            ),
        ),
    ],
)
def test_frozen_actions_render_substantive_deterministic_notifications(
    job, raw_name, actions, required
) -> None:
    computation = job.compute(
        (FIXTURES / raw_name).read_bytes(), f"fixture://{raw_name}", SCHEDULED
    )
    for action_name, current_state, target_state, boundary_name in actions:
        action = deepcopy(computation.action)
        action.update(
            {
                "action": action_name,
                "state_before_next": current_state,
                "target_state": target_state,
            }
        )

        first = job.render_notification(action)
        second = job.render_notification(action)
        text = first.decode("utf-8")

        assert first == second
        assert f"动作：{action_name}" in text
        assert f"下一完整收盘{boundary_name}边界" in text
        assert all(marker in text for marker in required)
        assert len(first) > 300
        assert not any(
            secret in text.casefold()
            for secret in ("authorization:", "cookie:", "private key")
        )


def test_registry_injects_provider_and_never_constructs_local_fallback() -> None:
    jobs = ProductionJobs(
        [
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
        ]
    )

    class Provider:
        def __init__(self):
            self.urls = []

        def get(self, url, *, headers, maximum_bytes):
            self.urls.append(url)
            fixture = "bocom-yahoo-chart.json" if "yahoo" in url else "gold-au9999.tsv"
            return (FIXTURES / fixture).read_bytes()

    provider = Provider()
    bocom_url, _ = jobs.acquire("297c11cad0dc", provider, SCHEDULED)
    gold_url, _ = jobs.acquire("1cd5557264db", provider, SCHEDULED)

    assert bocom_url.startswith("https://query1.finance.yahoo.com/")
    assert gold_url.startswith("https://vip.stock.finance.sina.com.cn/")
    assert provider.urls == [bocom_url, gold_url]
    assert all("flearn" not in url and "localhost" not in url for url in provider.urls)


@pytest.mark.parametrize(
    ("job", "raw_name"),
    [
        (BocomProductionJob(FIXTURES / "bocom-model-manifest.json"), "bocom-yahoo-chart.json"),
        (GoldProductionJob(FIXTURES / "gold-model-manifest.json"), "gold-au9999.tsv"),
    ],
)
def test_dataset_identity_hashes_canonical_bytes_exactly_once(job, raw_name) -> None:
    computation = job.compute((FIXTURES / raw_name).read_bytes(), f"fixture://{raw_name}", SCHEDULED)
    dataset_domain = b"quantresearch-production-dataset/v1\0"
    snapshot = hashlib.sha256(dataset_domain + computation.normalized_bytes).hexdigest()
    experiment_preimage = canonical_json_bytes(
        {
            "job_id": job.job_id,
            "model": job.production_manifest_sha256,
            "snapshot": snapshot,
        }
    )
    assert computation.experiment_id == hashlib.sha256(
        b"quantresearch-production-experiment/v1\0" + experiment_preimage
    ).hexdigest()

    with pytest.raises(TypeError, match="CanonicalJsonBytes"):
        identity_canonical_bytes(dataset_domain, computation.normalized_bytes)  # type: ignore[arg-type]
    with pytest.raises(ProductionJobError, match="not canonical"):
        CanonicalJsonBytes(b'{"value": 1}')


def test_staged_reconstruction_preserves_regular_input_daily_and_formal_bytes(tmp_path) -> None:
    production_input = ProductionInput(
        "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
    )
    input_dir = tmp_path / "input"
    input_identity = seal(input_dir, ProductionJobs.input_payloads(production_input))
    assert (
        ProductionJobs.read_input(input_dir, expected_package_identity=input_identity)
        == production_input
    )

    daily = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
        (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
        "fixture://bocom-yahoo-chart.json",
        SCHEDULED,
    )
    daily_dir = tmp_path / "daily"
    daily_identity = seal(daily_dir, ProductionJobs.computation_payloads(daily))
    assert (
        ProductionJobs.read_computation(daily_dir, expected_package_identity=daily_identity)
        == daily
    )

    formal = formal_computation()
    formal_dir = tmp_path / "formal"
    formal_identity = seal(formal_dir, ProductionJobs.computation_payloads(formal))
    assert (
        ProductionJobs.read_computation(formal_dir, expected_package_identity=formal_identity)
        == formal
    )


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        ("input", "raw.bin"),
        ("daily", "action.json"),
        ("daily", "notification.txt"),
        ("daily", "normalized.json"),
        ("daily", "raw.bin"),
        ("daily", "report.html"),
        ("formal", "03-CALIBRATION_CLAIMED.json"),
        ("formal", "04-CALIBRATION_SEALED.json"),
        ("formal", "calibration.json"),
    ],
)
def test_actual_producer_external_identity_rejects_post_seal_mutation_and_reseal(
    tmp_path, stage: str, member: str
) -> None:
    work_root = tmp_path / "work"
    run_id = "a" * 64
    generation_stage = "acquisition" if stage == "input" else "computation"
    target = work_root / run_id / generation_stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://old"}, b"old"
            )
        )
        read = ProductionJobs.read_input
    elif stage == "daily":
        computation = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )
        payloads = ProductionJobs.computation_payloads(computation)
        read = ProductionJobs.read_computation
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        read = ProductionJobs.read_computation

    worker = object.__new__(ProductionWorker)
    worker.work_root = work_root
    worker.package_identity_authority = FilesystemPackageIdentityAuthority(
        work_root, tmp_path / "package-identities"
    )
    package_identity = worker._write_generation(target, payloads)
    identity_bytes = (target / "identity.json").read_bytes()
    external_identity_path = (
        tmp_path / "package-identities" / run_id / f"{generation_stage}.sha256"
    )
    external_identity_bytes = external_identity_path.read_bytes()

    member_path = target / member
    old_payload = member_path.read_bytes()
    if member == "action.json":
        changed_action = json.loads(old_payload)
        assert changed_action["action"] == "WAIT"
        changed_action["action"] = "HOLD"
        new_payload = canonical_json_bytes(changed_action)
    else:
        new_payload = bytes([old_payload[0] ^ 1]) + old_payload[1:]
    assert len(new_payload) == len(old_payload)
    assert new_payload != old_payload

    target.chmod(0o755)
    member_path.chmod(0o644)
    member_path.write_bytes(new_payload)
    member_path.chmod(0o444)
    target.chmod(0o555)

    assert (target / "identity.json").read_bytes() == identity_bytes
    assert external_identity_path.read_bytes() == external_identity_bytes
    changed_payloads = dict(payloads)
    changed_payloads[member] = new_payload
    with pytest.raises(PackageIdentityAuthorityError, match="conflicts"):
        worker._write_generation(target, changed_payloads)
    with pytest.raises(ProductionJobError, match="package identity mismatch"):
        read(target, expected_package_identity=package_identity)
    target.chmod(0o700)
    shutil.rmtree(target)
    with pytest.raises(PackageIdentityAuthorityError, match="conflicts"):
        worker._write_generation(target, changed_payloads)


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        ("input", "raw.bin"),
        ("daily", "action.json"),
        ("daily", "notification.txt"),
        ("daily", "normalized.json"),
        ("daily", "raw.bin"),
        ("daily", "report.html"),
        ("formal", "03-CALIBRATION_CLAIMED.json"),
        ("formal", "04-CALIBRATION_SEALED.json"),
        ("formal", "calibration.json"),
    ],
)
def test_staged_reconstruction_rejects_member_mutated_before_its_first_open(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stage: str, member: str
) -> None:
    target = tmp_path / stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://old"}, b"old"
            )
        )
        read = ProductionJobs.read_input
    elif stage == "daily":
        computation = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )
        payloads = ProductionJobs.computation_payloads(computation)
        read = ProductionJobs.read_computation
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        read = ProductionJobs.read_computation
    package_identity = seal(target, payloads)
    original_open = os.open
    identity_opened = False
    mutated = False

    def mutate_member_before_open(path, flags, *args, **kwargs):
        nonlocal identity_opened, mutated
        name = os.fspath(path)
        if name == "identity.json":
            opened = original_open(path, flags, *args, **kwargs)
            identity_opened = True
            return opened
        if identity_opened and name == member and not mutated:
            mutated = True
            member_path = target / member
            member_path.chmod(0o644)
            member_path.write_bytes(b'{"changed":true}' if member == "action.json" else b"changed")
            member_path.chmod(0o444)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(production_jobs.os, "open", mutate_member_before_open)
    with pytest.raises(ProductionJobError, match="changed during read"):
        read(target, expected_package_identity=package_identity)
    assert identity_opened is True
    assert mutated is True


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        ("input", "raw.bin"),
        ("daily", "action.json"),
        ("daily", "notification.txt"),
        ("daily", "normalized.json"),
        ("daily", "raw.bin"),
        ("daily", "report.html"),
        ("formal", "03-CALIBRATION_CLAIMED.json"),
        ("formal", "04-CALIBRATION_SEALED.json"),
        ("formal", "calibration.json"),
    ],
)
def test_staged_reconstruction_rejects_member_mutated_during_baseline_acquisition(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stage: str, member: str
) -> None:
    target = tmp_path / stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://old"}, b"old"
            )
        )
        read = ProductionJobs.read_input
    elif stage == "daily":
        computation = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )
        payloads = ProductionJobs.computation_payloads(computation)
        read = ProductionJobs.read_computation
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        read = ProductionJobs.read_computation
    package_identity = seal(target, payloads)
    original_stat = os.stat
    identity_baselined = False
    mutated = False

    def mutate_member_before_baseline(path, *args, **kwargs):
        nonlocal identity_baselined, mutated
        name = os.fspath(path)
        if name == "identity.json":
            result = original_stat(path, *args, **kwargs)
            identity_baselined = True
            return result
        if identity_baselined and name == member and not mutated:
            mutated = True
            member_path = target / member
            old_payload = member_path.read_bytes()
            if member == "action.json":
                changed_action = json.loads(old_payload)
                assert changed_action["action"] == "WAIT"
                changed_action["action"] = "HOLD"
                new_payload = canonical_json_bytes(changed_action)
            else:
                new_payload = bytes([old_payload[0] ^ 1]) + old_payload[1:]
            assert len(new_payload) == len(old_payload)
            assert new_payload != old_payload
            member_path.chmod(0o644)
            member_path.write_bytes(new_payload)
            member_path.chmod(0o444)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(production_jobs.os, "stat", mutate_member_before_baseline)
    with pytest.raises(ProductionJobError, match="changed during read"):
        read(target, expected_package_identity=package_identity)
    assert identity_baselined is True
    assert mutated is True


@pytest.mark.parametrize("stage", ["input", "formal"])
def test_staged_reconstruction_rejects_symlinked_members(tmp_path, stage) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-bytes")
    target = tmp_path / stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
            )
        )
        package_identity = seal(target, payloads)
        target.chmod(0o755)
        (target / "raw.bin").unlink()
        (target / "raw.bin").symlink_to(outside)
        target.chmod(0o555)
        read = ProductionJobs.read_input
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        package_identity = seal(target, payloads)
        target.chmod(0o755)
        for name in formal_computation().files:
            (target / name).unlink()
            (target / name).symlink_to(outside)
        target.chmod(0o555)
        read = ProductionJobs.read_computation

    with pytest.raises(ProductionJobError, match="unsafe"):
        read(target, expected_package_identity=package_identity)


def test_staged_reconstruction_rejects_unsafe_formal_name_before_lookup(tmp_path) -> None:
    target = tmp_path / "formal"
    payloads = ProductionJobs.computation_payloads(formal_computation())
    identity = json.loads(payloads["identity.json"])
    identity["files"] = ["../outside", *identity["files"][1:]]
    payloads["identity.json"] = canonical_json_bytes(identity)
    package_identity = seal(target, payloads)

    with pytest.raises(ProductionJobError, match="member set"):
        ProductionJobs.read_computation(
            target, expected_package_identity=package_identity
        )


def test_staged_reconstruction_rejects_hard_link_alias(tmp_path) -> None:
    target = tmp_path / "input"
    target.mkdir()
    identity = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )["identity.json"]
    (target / "identity.json").write_bytes(identity)
    (target / "identity.json").chmod(0o444)
    outside = tmp_path / "outside"
    outside.write_bytes(b"raw")
    os.link(outside, target / "raw.bin")
    (target / "raw.bin").chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(ProductionJobError, match="unsafe"):
        ProductionJobs.read_input(
            target,
            expected_package_identity=staged_package_identity(
                {"identity.json": identity, "raw.bin": b"raw"}
            ),
        )


@pytest.mark.parametrize("member_kind", ["fifo", "writable"])
def test_staged_reconstruction_rejects_non_regular_or_writable_member(
    tmp_path, member_kind
) -> None:
    target = tmp_path / "input"
    payloads = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )
    package_identity = seal(target, payloads)
    target.chmod(0o755)
    raw = target / "raw.bin"
    if member_kind == "fifo":
        raw.unlink()
        os.mkfifo(raw, 0o444)
    else:
        raw.chmod(0o644)
    target.chmod(0o555)

    with pytest.raises(ProductionJobError, match="unsafe"):
        ProductionJobs.read_input(target, expected_package_identity=package_identity)


def test_staged_reconstruction_rejects_bytes_mutated_between_reads(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "input"
    payloads = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )
    package_identity = seal(target, payloads)
    raw = target / "raw.bin"
    raw_inode = raw.stat().st_ino
    original_read = os.read
    mutated = False

    def mutate_after_first_read(fd, count):
        nonlocal mutated
        chunk = original_read(fd, count)
        if not chunk and not mutated and os.fstat(fd).st_ino == raw_inode:
            mutated = True
            raw.chmod(0o644)
            raw.write_bytes(b"new")
            raw.chmod(0o444)
        return chunk

    monkeypatch.setattr(production_jobs.os, "read", mutate_after_first_read)
    with pytest.raises(ProductionJobError, match="changed during read"):
        ProductionJobs.read_input(target, expected_package_identity=package_identity)
    assert mutated is True


def test_staged_input_rejects_identity_mutated_while_raw_is_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "input"
    payloads = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )
    package_identity = seal(target, payloads)
    identity = target / "identity.json"
    raw_inode = (target / "raw.bin").stat().st_ino
    original_read = os.read
    mutated = False

    def mutate_identity_when_raw_is_read(fd, count):
        nonlocal mutated
        chunk = original_read(fd, count)
        if not mutated and os.fstat(fd).st_ino == raw_inode:
            mutated = True
            identity.chmod(0o644)
            identity.write_bytes(
                canonical_json_bytes(
                    {
                        "kind": "provider-get",
                        "method": "GET",
                        "provider_url": "fixture://changed",
                    }
                )
            )
            identity.chmod(0o444)
        return chunk

    monkeypatch.setattr(production_jobs.os, "read", mutate_identity_when_raw_is_read)
    with pytest.raises(ProductionJobError, match="changed during read"):
        ProductionJobs.read_input(target, expected_package_identity=package_identity)
    assert mutated is True


def test_staged_computation_rejects_member_mutated_while_another_is_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "formal"
    package_identity = seal(
        target, ProductionJobs.computation_payloads(formal_computation())
    )
    claimed = target / "03-CALIBRATION_CLAIMED.json"
    sealed_inode = (target / "04-CALIBRATION_SEALED.json").stat().st_ino
    original_read = os.read
    mutated = False

    def mutate_claimed_when_sealed_is_read(fd, count):
        nonlocal mutated
        chunk = original_read(fd, count)
        if not mutated and os.fstat(fd).st_ino == sealed_inode:
            mutated = True
            claimed.chmod(0o644)
            claimed.write_bytes(b"changed-claimed")
            claimed.chmod(0o444)
        return chunk

    monkeypatch.setattr(production_jobs.os, "read", mutate_claimed_when_sealed_is_read)
    with pytest.raises(ProductionJobError, match="changed during read"):
        ProductionJobs.read_computation(
            target, expected_package_identity=package_identity
        )
    assert mutated is True
