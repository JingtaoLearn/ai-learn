import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quant_platform.full_persistence import FullPostgresPersistence
from quant_platform.gold_evaluation_report import (
    DISCLAIMER,
    PROXY_LABEL,
    SPREAD_LABEL,
    GoldEvaluationReportError,
    load_gold_evaluation_artifact,
    load_native_gold_evaluation_package,
    render_gold_evaluation_report,
)
from quant_platform.postgres_persistence import PostgresOperatorPersistence

from test_web_api import authenticate, make_app


def _metric(availability, value, unit):
    return {"availability": availability, "value": value, "unit": unit}


def _pending_evaluation():
    return {
        "schema_id": "quant-platform/gold-evaluation-report/v1",
        "labels": {
            "market_proxy": PROXY_LABEL,
            "cost_assumption": SPREAD_LABEL,
            "disclaimer": DISCLAIMER,
        },
        "evidence": {
            "status": "PENDING",
            "verdict": "PENDING",
            "verdict_line": "Evaluation evidence is pending; no result is claimed.",
            "effectiveness_basis": "PENDING",
            "evaluation_run_id": "TEST_ONLY_NOT_RESEARCH_EVIDENCE",
            "generated_at": "2026-09-11T17:04:08+00:00",
        },
        "current_state": {"position": "PENDING", "label": "Pending"},
        "period": {"start": "2023-09-01", "end": None, "recent_start": None},
        "fit": {
            "method": "PENDING_IMMUTABLE_EVALUATION",
            "causal": True,
            "description": "Pending; no fit values are present.",
        },
        "metrics": {
            "initial_wealth": _metric("PENDING", None, "CNY"),
            "final_wealth": _metric("PENDING", None, "CNY"),
            "strategy_return": _metric("PENDING", None, "PERCENT"),
            "buy_hold_return": _metric("PENDING", None, "PERCENT"),
            "excess_return": _metric("PENDING", None, "PERCENT"),
            "max_drawdown": _metric("PENDING", None, "CNY"),
            "transaction_count": _metric("PENDING", None, "COUNT"),
            "full_spread": _metric("AVAILABLE", 5, "CNY_PER_G"),
        },
        "series": [],
        "trades": [],
        "limitations": ["Test fixture only; no research result is represented."],
    }


def _rendering_contract_fixture():
    """Synthetic values exercise rendering only and are never persisted as evidence."""

    value = _pending_evaluation()
    value["evidence"] = {
        "status": "DESCRIPTIVE_ONLY",
        "verdict": "NOT_SUPPORTED",
        "verdict_line": "Test-only rendering fixture; not research evidence.",
        "effectiveness_basis": "RETROSPECTIVE_DESCRIPTIVE",
        "evaluation_run_id": "TEST_ONLY_NOT_RESEARCH_EVIDENCE",
        "generated_at": "2026-09-11T17:04:08+00:00",
    }
    value["current_state"] = {"position": "LONG", "label": "Long (test only)"}
    value["period"] = {
        "start": "2023-09-01",
        "end": "2023-09-29",
        "recent_start": "2023-09-01",
    }
    value["fit"] = {
        "method": "TEST_ONLY_CAUSAL_FIXTURE",
        "causal": True,
        "description": "Synthetic rendering-contract fixture; not research evidence.",
    }
    value["metrics"] = {
        "initial_wealth": _metric("AVAILABLE", 100, "CNY"),
        "final_wealth": _metric("AVAILABLE", 101, "CNY"),
        "strategy_return": _metric("AVAILABLE", 1, "PERCENT"),
        "buy_hold_return": _metric("AVAILABLE", 2, "PERCENT"),
        "excess_return": _metric("AVAILABLE", -1, "PERCENT"),
        "max_drawdown": _metric("AVAILABLE", 0.5, "CNY"),
        "transaction_count": _metric("AVAILABLE", 1, "COUNT"),
        "full_spread": _metric("AVAILABLE", 5, "CNY_PER_G"),
    }
    value["series"] = [
        {
            "date": "2023-09-01",
            "official_close": 100,
            "fitted_level": 99,
            "fitted_trend": 0.1,
            "fit_information_cutoff": "2023-09-01",
            "position": "CASH",
        },
        {
            "date": "2023-09-04",
            "official_close": 101,
            "fitted_level": 100,
            "fitted_trend": 0.2,
            "fit_information_cutoff": "2023-09-04",
            "position": "LONG",
        },
        {
            "date": "2023-09-29",
            "official_close": 102,
            "fitted_level": 101,
            "fitted_trend": 0.3,
            "fit_information_cutoff": "2023-09-29",
            "position": "LONG",
        },
    ]
    value["trades"] = [
        {
            "trade_id": "test-buy",
            "signal_date": "2023-09-01",
            "execution_date": "2023-09-04",
            "kind": "SIGNAL_BUY",
            "side": "BUY",
            "position_before": "CASH",
            "position_after": "LONG",
            "execution_price": 101,
            "spread_cny_per_g": 5,
        }
    ]
    return value


def _payload(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _native_package():
    labels = [PROXY_LABEL, SPREAD_LABEL, DISCLAIMER]
    result = {
        "schema": "quantresearch-gold-rls-retrospective-result/v1",
        "verdict": "SUPPORTED_FOR_FURTHER_RESEARCH",
        "classification": "USER_SELECTED_HISTORICALLY_EXPOSED_RETROSPECTIVE",
        "untouched_confirmation": False,
        "production_or_trading_authority": "NOT_AUTHORIZED",
        "labels": labels,
        "metrics": {
            "first_evaluation_trading_date": "2023-09-01",
            "last_evaluation_trading_date": "2023-09-29",
            "strategy_final_wealth_cny": "101000.00",
            "buy_and_hold_final_wealth_cny": "102000.00",
            "strategy_excess_vs_buy_and_hold_cny": "-1000.00",
            "strategy_max_drawdown_cny": "500.00",
        },
        "implication": "Retrospective market-proxy evidence only; test fixture.",
    }
    observations = [
        {
            "trading_date": "2023-09-01",
            "close_decimal": "100.00",
            "fitted_level_after_current_close": 99.0,
            "fitted_slope_after_current_close": 0.1,
            "gold_grams": 975,
        },
        {
            "trading_date": "2023-09-04",
            "close_decimal": "101.00",
            "fitted_level_after_current_close": 100.0,
            "fitted_slope_after_current_close": 0.2,
            "gold_grams": 975,
        },
        {
            "trading_date": "2023-09-29",
            "close_decimal": "102.00",
            "fitted_level_after_current_close": 101.0,
            "fitted_slope_after_current_close": 0.3,
            "gold_grams": 0,
        },
    ]
    ledger = {
        "schema": "quantresearch-gold-trade-ledger/v1",
        "initial_cash_cny": "100000.00",
        "initial_gold_grams": 0,
        "trades": [
            {
                "date": "2023-09-01",
                "kind": "SIGNAL_BUY",
                "price_cny_per_g": "102.50",
                "signal_source_date": "2023-08-31",
            },
            {
                "date": "2023-09-29",
                "kind": "FORCED_TERMINAL_SELL",
                "price_cny_per_g": "99.50",
                "signal_source_date": None,
            },
        ],
        "final_cash_cny": "101000.00",
        "final_gold_grams": 0,
        "labels": labels,
    }
    comparators = {
        "schema": "quantresearch-gold-comparators/v1",
        "BUY_AND_HOLD": {"final_wealth_cny": "102000.00"},
    }
    return {
        "RESULT.json": _payload(result),
        "TRADE-LEDGER.json": _payload(ledger),
        "COMPARATORS.json": _payload(comparators),
        "OBSERVATIONS.jsonl": b"".join(_payload(item) for item in observations),
    }


def test_pending_report_is_concise_and_never_invents_values():
    payload = _payload(_pending_evaluation())
    evidence_id = "a" * 64
    value, artifact_sha = load_gold_evaluation_artifact(payload, evidence_id=evidence_id)
    rendered = render_gold_evaluation_report(
        value,
        evidence_id=evidence_id,
        artifact_sha256=artifact_sha,
    ).decode()

    assert rendered.index('data-testid="report-summary"') < rendered.index('data-testid="recent-chart"')
    assert rendered.index('data-testid="recent-chart"') < rendered.index('aria-label="核心指标"')
    assert rendered.index('aria-label="核心指标"') < rendered.index('data-testid="panorama-chart"')
    assert rendered.count("待评估：权威不可变评估产物尚未提供。") == 2
    assert "@media(max-width:640px)" in rendered
    assert PROXY_LABEL in rendered and SPREAD_LABEL in rendered and DISCLAIMER in rendered
    assert evidence_id in rendered and artifact_sha in rendered
    assert "<polyline" not in rendered


def test_pending_report_rejects_invented_results_and_outcome_claim():
    schema_path = (
        Path(__file__).parents[1]
        / "src"
        / "quant_platform"
        / "gold_evaluation_report.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(_pending_evaluation()))

    reviewer_payload = _pending_evaluation()
    reviewer_payload["metrics"]["initial_wealth"] = _metric(
        "AVAILABLE", 123456, "CNY"
    )
    reviewer_payload["metrics"]["strategy_return"] = _metric(
        "AVAILABLE", 42, "PERCENT"
    )
    reviewer_payload["evidence"]["verdict_line"] = "PASS — invented pending result"

    metrics_only = _pending_evaluation()
    metrics_only["metrics"] = reviewer_payload["metrics"]
    outcome_only = _pending_evaluation()
    outcome_only["evidence"]["verdict_line"] = reviewer_payload["evidence"]["verdict_line"]

    for value in (reviewer_payload, metrics_only, outcome_only):
        with pytest.raises(GoldEvaluationReportError, match="pending evidence"):
            load_gold_evaluation_artifact(_payload(value), evidence_id="0" * 64)
        assert list(validator.iter_errors(value))


def test_chart_markers_are_derived_from_the_validated_trade_ledger():
    value = _rendering_contract_fixture()
    payload = _payload(value)
    loaded, artifact_sha = load_gold_evaluation_artifact(payload, evidence_id="b" * 64)
    rendered = render_gold_evaluation_report(
        loaded,
        evidence_id="b" * 64,
        artifact_sha256=artifact_sha,
    ).decode()

    assert rendered.count('class="marker buy"') == 2
    assert rendered.count('class="marker sell"') == 0
    assert "2023-09-04 · BUY · 101.00 CNY/g" in rendered
    assert "Synthetic rendering-contract fixture" in rendered


def test_native_p0_package_renders_without_manual_copy_or_pass_promotion():
    view, package_sha = load_native_gold_evaluation_package(
        _native_package(), evidence_id="f" * 64
    )
    rendered = render_gold_evaluation_report(
        view, evidence_id="f" * 64, artifact_sha256=package_sha
    ).decode()

    assert view["evidence"]["status"] == "DESCRIPTIVE_ONLY"
    assert view["evidence"]["effectiveness_basis"] == "RETROSPECTIVE_DESCRIPTIVE"
    assert view["evidence"]["verdict"] == "SUPPORTED_FOR_FURTHER_RESEARCH"
    assert view["metrics"]["strategy_return"]["value"] == 1.0
    assert view["metrics"]["buy_hold_return"]["value"] == 2.0
    assert view["metrics"]["excess_return"]["value"] == -1.0
    assert view["metrics"]["transaction_count"]["value"] == 2
    assert rendered.count('class="marker buy"') == 2
    assert rendered.count('class="marker sell"') == 2
    assert "<strong>PASS</strong>" not in rendered


def test_future_informed_fit_and_ledger_drift_fail_closed():
    future_fit = _rendering_contract_fixture()
    future_fit["series"][1]["fit_information_cutoff"] = "2023-09-05"
    try:
        load_gold_evaluation_artifact(_payload(future_fit), evidence_id="c" * 64)
    except GoldEvaluationReportError as exc:
        assert "causal period" in str(exc)
    else:
        raise AssertionError("future-informed fit was accepted")

    drift = _rendering_contract_fixture()
    drift["series"][1]["position"] = "CASH"
    try:
        load_gold_evaluation_artifact(_payload(drift), evidence_id="d" * 64)
    except GoldEvaluationReportError as exc:
        assert "trade ledger" in str(exc)
    else:
        raise AssertionError("position/ledger drift was accepted")


def test_authenticated_route_reads_only_postgresql_accepted_evidence(monkeypatch, tmp_path):
    class OperatorPersistence:
        def verify_schema(self):
            return None

        def list_operators(self):
            return []

    monkeypatch.setattr(
        PostgresOperatorPersistence,
        "from_environment",
        classmethod(lambda cls: OperatorPersistence()),
    )
    app, client = make_app(tmp_path)
    evidence_id = "e" * 64
    native = _native_package()

    class FakePostgres:
        def accepted_evidence_package(self, requested_id, *, expected_class):
            assert requested_id == evidence_id
            assert expected_class == "VALIDATION_EVIDENCE"
            return {
                "evidence_id": evidence_id,
                "evidence_class": expected_class,
                "source_path": "platform/validation-evidence/test-only",
                "members": native,
            }

    unauthenticated = client.get(f"/reports/gold/{evidence_id}", follow_redirects=False)
    assert unauthenticated.status_code in {302, 303, 307}

    authenticate(app, client)
    monkeypatch.setenv("QUANT_POSTGRES_PASSWORD_FILE", "/run/secrets/postgres-runtime")
    monkeypatch.setattr(
        FullPostgresPersistence,
        "from_environment",
        classmethod(lambda cls: FakePostgres()),
    )
    response = client.get(f"/reports/gold/{evidence_id}")

    assert response.status_code == 200
    assert response.headers["x-quantresearch-evidence-id"] == evidence_id
    assert len(response.headers["x-quantresearch-evaluation-sha256"]) == 64
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "Retrospective market-proxy evidence only; test fixture." in response.text
    assert "SUPPORTED_FOR_FURTHER_RESEARCH" in response.text
    assert "<strong>PASS</strong>" not in response.text
