from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
import time
from pathlib import Path

import exchange_calendars
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_platform.dataset_service import (
    MSFT_PROXY_LABEL,
    MSFT_YAHOO_BASIS_UNQUALIFIED,
    DatasetResolutionError,
    MsftDatasetIngress,
    msft_adjusted_ohlc_proxy_snapshot_from_yahoo,
    msft_snapshot_from_yahoo,
)
from quant_platform.msft_trend_study import (
    PROXY_RESULT_SCHEMA,
    SNAPSHOT_BASIS_CONTRACT,
    VERDICT_PRECEDENCE,
    MsftStudyValidationError,
    build_report_pointer,
    build_proxy_snapshot,
    build_snapshot,
    candidates,
    chinese_report,
    replay,
    run_study,
)
from quant_platform.production_web import VERIFIED_CLIENT_HEADER, create_production_app
from quant_platform.schemas import canonical_json_bytes
from quant_platform.study_remote import (
    SignedStudyClient,
    StudyValidationError,
    StudyWorkerServer,
    WorkerJobStore,
    freeze_market_request,
    validate_request,
)
from quant_platform.yahoo_proxy_sanitizer import sanitize_msft_yahoo_proxy_response


SEALED = "2026-09-10T12:00:00Z"
IMAGE = "sha256:" + "1" * 64
COMMIT = "2" * 40
TREE = "3" * 40


def _identity(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _record(date: str, open_: float, close: float, *, split: float = 1.0, dividend: float = 0.0):
    return {
        "session_date": date,
        "open": open_,
        "close": close,
        "split_factor": split,
        "cash_dividend": dividend,
        "source_record_identity": _identity(date),
        "record_sealed_at": SEALED,
    }


def _snapshot(records):
    return build_snapshot(
        records,
        source_identity={
            "provider": "synthetic-fixture",
            "response_sha256": _identity("response"),
            "basis_contract": SNAPSHOT_BASIS_CONTRACT,
        },
        sealed_at=SEALED,
    )


def test_explicit_snapshot_seals_source_actions_and_rejects_missing_or_malformed() -> None:
    snapshot = _snapshot(
        [
            _record("2025-01-02", 100.0, 102.0),
            _record("2025-01-03", 51.0, 52.0, split=2.0, dividend=0.25),
        ]
    )

    assert snapshot["schema"] == "quantresearch-xnys-total-return-snapshot/v1"
    assert snapshot["price_semantics"] == "split-adjusted-dividend-unadjusted"
    assert snapshot["record_count"] == 2
    assert snapshot["null_counts"] == {field: 0 for field in snapshot["required_fields"]}
    assert snapshot["records"][1]["split_factor"] == 2.0
    assert snapshot["records"][1]["cash_dividend"] == 0.25

    missing = dict(snapshot["records"][0])
    missing.pop("source_record_identity")
    with pytest.raises(MsftStudyValidationError, match="fields are invalid"):
        _snapshot([missing])
    malformed = dict(snapshot["records"][0], record_sealed_at="not-a-time")
    with pytest.raises(MsftStudyValidationError, match="RFC3339"):
        _snapshot([malformed])


def test_yahoo_basis_is_fail_closed_before_provider_access_or_relabeling() -> None:
    class Provider:
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return b"{}"

    class Persistence:
        @staticmethod
        def dataset_ingress_receipt(*args):
            return None

    provider = Provider()
    ingress = MsftDatasetIngress(provider, Persistence())

    with pytest.raises(DatasetResolutionError, match=MSFT_YAHOO_BASIS_UNQUALIFIED):
        ingress.ingest(
            {"start": "2025-01-02", "end": "2025-01-03", "expected_generation": 0},
            "unqualified-basis",
        )
    with pytest.raises(DatasetResolutionError, match=MSFT_YAHOO_BASIS_UNQUALIFIED):
        msft_snapshot_from_yahoo(
            b"{}",
            request_url="https://query1.finance.yahoo.com/frozen",
            sealed_at=SEALED,
        )
    assert provider.calls == 0


def _proxy_yahoo_payload(dates: list[str], *, extra_metadata: dict | None = None) -> bytes:
    timestamps = [int(pd.Timestamp(f"{date}T14:30:00Z").timestamp()) for date in dates]
    raw_close = [100.0 + index for index in range(len(dates))]
    metadata = {
        "symbol": "MSFT",
        "currency": "USD",
        "dataGranularity": "1d",
        "exchangeTimezoneName": "America/New_York",
    }
    metadata.update(extra_metadata or {})
    return json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": metadata,
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": [value - 1.0 for value in raw_close],
                                    "high": [value + 2.0 for value in raw_close],
                                    "low": [value - 2.0 for value in raw_close],
                                    "close": raw_close,
                                }
                            ],
                            "adjclose": [
                                {"adjclose": [value * 0.8 for value in raw_close]}
                            ],
                        },
                    }
                ],
            }
        },
        separators=(",", ":"),
    ).encode()


def test_proxy_parser_applies_one_factor_to_ohlc_and_rejects_2025_before_publication() -> None:
    payload = _proxy_yahoo_payload(["2022-02-01", "2024-12-31"])
    source_artifact = sanitize_msft_yahoo_proxy_response(
        payload,
        start="2022-02-01",
        end="2024-12-31",
    )
    snapshot = msft_adjusted_ohlc_proxy_snapshot_from_yahoo(
        source_artifact,
        request_url="https://query1.finance.yahoo.com/frozen-proxy",
        sealed_at=SEALED,
    )

    assert snapshot["classification"] == MSFT_PROXY_LABEL
    assert snapshot["data_start"] == "2022-02-01"
    assert snapshot["data_end"] == "2024-12-31"
    assert snapshot["source_identity"]["response_sha256"] == hashlib.sha256(payload).hexdigest()
    assert snapshot["source_identity"]["source_artifact_sha256"] == hashlib.sha256(
        source_artifact
    ).hexdigest()
    assert snapshot["source_identity"]["separate_corporate_action_postings"] == 0
    row = snapshot["records"][0]
    assert row["adjustment_factor"] == pytest.approx(0.8)
    assert row["open"] == pytest.approx(row["raw_open"] * 0.8)
    assert row["high"] == pytest.approx(row["raw_high"] * 0.8)
    assert row["low"] == pytest.approx(row["raw_low"] * 0.8)
    assert row["close"] == pytest.approx(row["raw_close"] * 0.8)

    filtered = sanitize_msft_yahoo_proxy_response(
        _proxy_yahoo_payload(["2022-02-01", "2025-01-02"]),
        start="2022-02-01",
        end="2024-12-31",
    )
    filtered_snapshot = msft_adjusted_ohlc_proxy_snapshot_from_yahoo(
        filtered,
        request_url="https://query1.finance.yahoo.com/frozen-proxy",
        sealed_at=SEALED,
    )
    assert filtered_snapshot["record_count"] == 1
    assert filtered_snapshot["data_end"] == "2022-02-01"


def test_proxy_sanitizer_removes_live_contaminant_from_all_downstream_surfaces() -> None:
    sentinel = "POST_CUTOFF_LIVE_SENTINEL_987654321"
    sessions = exchange_calendars.get_calendar("XNYS").sessions_in_range(
        "2022-02-01", "2024-12-31"
    )
    dates = [str(session.date()) for session in sessions]
    assert len(dates) == 733
    payload = _proxy_yahoo_payload(
        [*dates, "2025-01-02"],
        extra_metadata={
            "regularMarketTime": 1735828200,
            "regularMarketPrice": sentinel,
            "regularMarketDayHigh": 999999.0,
            "fiftyTwoWeekHigh": 999998.0,
            "currentTradingPeriod": {"regular": {"start": 1735828200}},
        },
    )

    source_artifact = sanitize_msft_yahoo_proxy_response(
        payload,
        start="2022-02-01",
        end="2024-12-31",
    )
    snapshot = msft_adjusted_ohlc_proxy_snapshot_from_yahoo(
        source_artifact,
        request_url="https://query1.finance.yahoo.com/frozen-proxy",
        sealed_at=SEALED,
    )
    remote_request = freeze_market_request(
        snapshot=snapshot,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    result = run_study(snapshot)
    _document, report_html = chinese_report(
        result,
        {"study_id": "a" * 64, "source_commit": COMMIT, "source_tree": TREE, "worker_image": IMAGE},
    )

    assert snapshot["record_count"] == 733
    assert snapshot["data_end"] == "2024-12-31"
    for surface in (
        source_artifact,
        canonical_json_bytes(snapshot),
        canonical_json_bytes(remote_request),
        report_html,
    ):
        assert sentinel.encode() not in surface
        assert b"regularMarketTime" not in surface
        assert b"currentTradingPeriod" not in surface


def _proxy_snapshot():
    records = []
    for index, session in enumerate(pd.bdate_range("2022-02-01", "2024-12-31")):
        raw_close = 100.0 * math.exp(index * 0.0007) * (1.0 + 0.04 * math.sin(index / 19.0))
        raw_open = raw_close * (1.0 + 0.002 * math.sin(index / 7.0))
        factor = 0.8
        records.append(
            {
                "session_date": str(session.date()),
                "raw_open": raw_open,
                "raw_high": max(raw_open, raw_close) * 1.01,
                "raw_low": min(raw_open, raw_close) * 0.99,
                "raw_close": raw_close,
                "adjustment_factor": factor,
                "open": raw_open * factor,
                "high": max(raw_open, raw_close) * 1.01 * factor,
                "low": min(raw_open, raw_close) * 0.99 * factor,
                "close": raw_close * factor,
                "source_record_identity": _identity(str(session.date())),
                "record_sealed_at": SEALED,
            }
        )
    return build_proxy_snapshot(
        records,
        source_identity={
            "provider": "synthetic-proxy-fixture",
            "classification": MSFT_PROXY_LABEL,
            "separate_corporate_action_postings": 0,
        },
        sealed_at=SEALED,
    )


def test_proxy_study_stops_after_validation_and_renders_required_chinese_warning() -> None:
    snapshot = _proxy_snapshot()
    result = run_study(snapshot)

    assert result["schema"] == PROXY_RESULT_SCHEMA
    assert result["classification"] == MSFT_PROXY_LABEL
    assert result["candidate_family_counts"] == {"SMA_CROSS": 8, "BREAKOUT_TRAILING": 6, "OLS": 1}
    assert result["data_end"] == "2024-12-31"
    assert result["final_created"] is False
    assert result["final_window_queried"] is False
    assert "final" not in result
    assert result["selection"] is None or result["selection"]["validation_baseline_10bp"]
    assert all(item["train_metrics"] and item["validation_metrics"] for item in result["family_winners"])

    document, html = chinese_report(
        result,
        {"study_id": "a" * 64, "source_commit": COMMIT, "source_tree": TREE, "worker_image": IMAGE},
    )
    warning = "探索性代理结果，不是最终评估，不代表真实可成交收益或生产信号"
    assert document["first_line"] == warning
    assert document["classification"] == MSFT_PROXY_LABEL
    assert warning.encode() in html
    assert MSFT_PROXY_LABEL.encode() in html


def test_t_minus_one_open_t_ledger_cost_cash_dividend_split_and_terminal_open() -> None:
    rows = [
        _record("2025-01-02", 100.0, 101.0),
        _record("2025-01-06", 102.0, 103.0, split=2.0, dividend=0.50),
        _record("2025-01-07", 104.0, 103.0),
        _record("2025-01-08", 102.0, 105.0),
    ]
    result = replay(
        rows,
        [1, 1, 0, 1],
        start="2025-01-02",
        end="2025-01-08",
        one_way_bps=10,
        cash_annual_rate=0.03,
    )

    assert [item["side"] for item in result.ledger] == ["BUY", None, "SELL", "BUY"]
    assert result.ledger[0]["quantity"] == math.floor(100_000 / 100.1)
    assert result.ledger[0]["cost"] > 0
    assert result.ledger[1]["cash_accrual"] > 0
    assert result.ledger[1]["dividend_credit"] > 0
    assert result.ledger[1]["broker_shares_after_split"] == 2 * result.ledger[1]["broker_shares_before_split"]
    assert result.ledger[2]["cost"] > 0
    assert result.metrics["completed_round_trips"] == 1
    assert result.metrics["terminal_position"] == {
        "state": "OPEN",
        "split_adjusted_whole_shares": result.ledger[-1]["split_adjusted_shares"],
        "broker_shares": result.ledger[-1]["broker_shares_after_split"],
        "marked_at": "final-close",
        "fabricated_exit": False,
        "unrealized_pnl_included": True,
        "unrealized_terminal_pnl": pytest.approx(
            result.ledger[-1]["split_adjusted_shares"]
            * (result.ledger[-1]["close"] - result.ledger[-1]["open"])
            - result.ledger[-1]["cost"]
        ),
    }


def test_split_and_dividend_reconcile_on_stable_adjusted_share_basis() -> None:
    rows = [
        _record("2025-01-02", 100.0, 100.0),
        _record("2025-01-03", 100.0, 100.0, split=2.0),
        _record("2025-01-06", 100.0, 100.0, dividend=0.50),
        _record("2025-01-07", 100.0, 100.0),
    ]
    result = replay(
        rows,
        [1, 1, 1, 1],
        start="2025-01-02",
        end="2025-01-07",
        one_way_bps=10,
    )

    assert result.ledger[1]["open_equity"] == pytest.approx(result.ledger[0]["open_equity"])
    assert result.ledger[1]["split_adjusted_shares"] == result.ledger[0][
        "split_adjusted_shares"
    ]
    assert result.ledger[1]["broker_shares_after_split"] == 2 * result.ledger[1][
        "broker_shares_before_split"
    ]
    research_units = result.ledger[2]["split_adjusted_shares"]
    broker_units = result.ledger[2]["broker_shares_after_split"]
    assert result.ledger[2]["dividend_credit"] == pytest.approx(research_units * 0.50)
    assert result.ledger[2]["dividend_credit"] == pytest.approx(broker_units * 0.25)


def test_interval_metrics_ignore_intermediate_closes_and_emit_structural_nulls() -> None:
    rows = [
        _record("2025-01-02", 100.0, 101.0),
        _record("2025-01-03", 110.0, 109.0),
        _record("2025-01-06", 90.0, 91.0),
        _record("2025-01-07", 120.0, 121.0),
    ]
    changed_closes = [
        dict(row, close=close)
        for row, close in zip(rows, [500.0, 1.0, 900.0, 121.0], strict=True)
    ]
    first = replay(rows, [1, 1, 1, 1], start="2025-01-02", end="2025-01-07", one_way_bps=10)
    second = replay(
        changed_closes,
        [1, 1, 1, 1],
        start="2025-01-02",
        end="2025-01-07",
        one_way_bps=10,
    )

    assert first.interval_returns == second.interval_returns
    for metric in ("volatility", "sharpe", "sortino"):
        assert first.metrics[metric] == second.metrics[metric]
    assert first.metrics["final_equity"] == second.metrics["final_equity"]
    assert first.metrics["interval_return_count"] == 3

    cash = replay(rows, [0, 0, 0, 0], start="2025-01-02", end="2025-01-07", one_way_bps=10)
    assert cash.metrics["sharpe"] is None
    assert cash.metrics["sortino"] is None
    assert cash.metrics["calmar"] is None
    assert cash.metrics["metric_null_reasons"] == {
        "sharpe": "ZERO_SAMPLE_STANDARD_DEVIATION",
        "sortino": "ZERO_DOWNSIDE_DEVIATION",
        "calmar": "ZERO_MAXIMUM_DRAWDOWN",
    }


def _long_synthetic_snapshot():
    sessions = pd.bdate_range("2021-01-04", "2025-06-30")
    records = []
    for index, session in enumerate(sessions):
        trend = 80.0 * math.exp(index * 0.0008)
        wave = 1.0 + 0.06 * math.sin(index / 17.0)
        open_ = trend * wave
        close = open_ * (1.0 + 0.002 * math.sin(index / 5.0))
        records.append(
            _record(
                str(session.date()),
                open_,
                close,
                dividend=0.10 if index in {300, 600, 900} else 0.0,
            )
        )
    return _snapshot(records)


def _flat_synthetic_snapshot():
    sessions = pd.bdate_range("2021-01-04", "2025-06-30")
    return _snapshot([_record(str(session.date()), 100.0, 100.0) for session in sessions])


def test_required_candidate_metric_null_is_inconclusive() -> None:
    result = run_study(_flat_synthetic_snapshot())

    assert result["verdict"] == "INCONCLUSIVE_DATA_OR_EXECUTION"
    assert result["selection"] is None
    assert result["final_evaluation_counts"] == {
        "primary_candidate": 0,
        "neighbors": 0,
        "alternatives": 0,
        "reselection": 0,
    }


def test_frozen_candidate_selection_and_final_boundary_are_aggregate_only() -> None:
    population = candidates()
    assert len(population) == 15
    assert sum(item["family"] == "SMA_CROSS" for item in population) == 8
    assert sum(item["family"] == "BREAKOUT_TRAILING" for item in population) == 6
    assert sum(item["family"] == "OLS_SLOPE_HYSTERESIS" for item in population) == 1

    checkpoints = []
    result = run_study(
        _long_synthetic_snapshot(),
        checkpoint=lambda progress, evidence: checkpoints.append((progress, evidence)),
    )

    assert result["trial_count"] == 15
    assert len(result["family_winners"]) == 3
    assert result["per_trial_attempt_rows"] == 0
    assert result["terminal_exit_fabricated"] is False
    assert result["verdict"] in {
        "REJECTED_VALIDATION",
        "REJECTED_NO_EDGE",
        "QUALIFIED_FOR_PAPER",
    }
    assert [item[0]["completed_candidates"] for item in checkpoints] == [5, 10, 15]
    ols = next(item for item in result["family_winners"] if item["family"] == "OLS_SLOPE_HYSTERESIS")
    assert ols["neighborhood_stability"]["phase"] == "VALIDATION"
    assert ols["neighborhood_stability"]["neighbor_count"] == 0
    assert ols["eligible_for_final_ranking"] is False
    assert "ZERO_PREDECLARED_NEIGHBORS" in ols["ineligibility_reasons"]
    assert all(
        neighbor["metrics"]["period_start"] == "2024-01-02"
        for winner in result["family_winners"]
        for neighbor in winner["validation_neighbors"]
    )
    if result["selection"] is not None:
        assert result["selection"]["no_final_reselection"] is True
        assert result["final_evaluation_counts"] == {
            "primary_candidate": 1,
            "neighbors": 0,
            "alternatives": 0,
            "reselection": 0,
        }
        assert result["final"]["baseline_cost_metrics"]["period_start"] == "2025-01-02"
        assert result["final"]["baseline_cost_metrics"]["period_end"] == "2025-06-30"


def test_market_job_identity_is_signed_seam_compatible_and_tamper_evident() -> None:
    snapshot = _long_synthetic_snapshot()
    request = freeze_market_request(
        snapshot=snapshot,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )

    assert validate_request(request) == request
    assert request["job_type"] == "xnys-msft-trend-study-v1"
    assert request["snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
    assert "attempts" not in request and "trials" not in request
    tampered = dict(request, source_tree="4" * 40)
    with pytest.raises(StudyValidationError, match="identity"):
        validate_request(tampered)


def test_signed_market_job_returns_one_aggregate_without_trial_materialization(
    tmp_path: Path,
) -> None:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "private.pub.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    state_root = tmp_path / "worker-state"
    server = StudyWorkerServer(
        ("127.0.0.1", 0), WorkerJobStore(state_root, IMAGE), public_key
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SignedStudyClient(
        f"http://127.0.0.1:{server.server_port}", private_key
    )
    request = freeze_market_request(
        snapshot=_long_synthetic_snapshot(),
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    try:
        first = client.submit(request)
        duplicate = client.submit(request)
        deadline = time.monotonic() + 30
        while True:
            job = client.read(request["job_id"]).value["job"]
            if job["status"] in {"SUCCEEDED", "FAILED"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert job["status"] == "SUCCEEDED"
    assert job["result"]["trial_count"] == 15
    assert job["result"]["per_trial_attempt_rows"] == 0
    assert "trials" not in job and "attempts" not in job
    assert len(job["checkpoints"]) == 3
    assert [path.name for path in state_root.iterdir()] == [f"{request['job_id']}.json"]


def test_no_qualified_result_renders_deterministic_immutable_chinese_report() -> None:
    snapshot = _long_synthetic_snapshot()
    result = {
        "schema": "quantresearch-msft-study-result/v1",
        "snapshot_id": snapshot["snapshot_id"],
        "trial_count": 15,
        "selection": None,
        "final": None,
        "verdict": "REJECTED_VALIDATION",
    }
    provenance = {
        "source_commit": COMMIT,
        "source_tree": TREE,
        "worker_image": IMAGE,
        "snapshot_id": snapshot["snapshot_id"],
    }

    first_document, first_html = chinese_report(result, provenance)
    second_document, second_html = chinese_report(result, provenance)

    assert first_document == second_document
    assert first_html == second_html
    assert first_document["language"] == "zh-CN"
    assert first_document["verdict"] == "REJECTED_VALIDATION"
    assert first_document["not_a_trade_recommendation"] is True
    assert first_document["report_artifact_id"] == second_document["report_artifact_id"]
    assert "不构成交易建议".encode() in first_html
    first_pointer = build_report_pointer(
        "b" * 64, first_document["report_artifact_id"], None
    )
    assert first_pointer is not None and first_pointer["sequence"] == 1
    assert (
        build_report_pointer(
            "b" * 64,
            first_document["report_artifact_id"],
            {
                "report_artifact_id": first_document["report_artifact_id"],
                "sequence": 1,
            },
        )
        is None
    )
    replacement = build_report_pointer(
        "b" * 64,
        "c" * 64,
        {
            "report_artifact_id": first_document["report_artifact_id"],
            "sequence": 1,
        },
    )
    assert replacement is not None and replacement["sequence"] == 2


@pytest.mark.parametrize("verdict", VERDICT_PRECEDENCE)
def test_machine_and_chinese_report_share_total_verdict_vocabulary(verdict: str) -> None:
    snapshot = _long_synthetic_snapshot()
    result = {
        "schema": "quantresearch-msft-study-result/v1",
        "snapshot_id": snapshot["snapshot_id"],
        "trial_count": 15,
        "selection": None,
        "final": None,
        "verdict": verdict,
    }

    document, html = chinese_report(result, {"source": "synthetic"})

    assert document["verdict"] == verdict
    assert verdict.encode() in html


def test_production_api_authenticates_and_reads_back_idempotent_msft_ingress() -> None:
    class Policy:
        @staticmethod
        def ready():
            return True

    class Service:
        policy = Policy()

    class Ingress:
        def __init__(self):
            self.calls = []

        def ingest(self, request, key):
            self.calls.append((request, key))
            return {
                "snapshot_id": "a" * 64,
                "schema_version": 6,
                "generation": 1,
                "record_count": 2,
            }

    ingress = Ingress()
    client = TestClient(
        create_production_app(
            Service(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            verified_client_identity="CN=ailearn",
            msft_ingress=ingress,  # type: ignore[arg-type]
        )
    )
    body = {"start": "2025-01-02", "end": "2025-01-03", "expected_generation": 0}

    denied = client.post("/api/v1/datasets/msft/snapshots", json=body)
    first = client.post(
        "/api/v1/datasets/msft/snapshots",
        json=body,
        headers={VERIFIED_CLIENT_HEADER: "CN=ailearn", "Idempotency-Key": "snapshot-1"},
    )
    duplicate = client.post(
        "/api/v1/datasets/msft/snapshots",
        json=body,
        headers={VERIFIED_CLIENT_HEADER: "CN=ailearn", "Idempotency-Key": "snapshot-1"},
    )

    assert denied.status_code == 403
    assert first.status_code == duplicate.status_code == 201
    assert first.json() == duplicate.json()
    assert first.json()["snapshot"]["snapshot_id"] == "a" * 64
    assert ingress.calls == [(body, "snapshot-1"), (body, "snapshot-1")]
