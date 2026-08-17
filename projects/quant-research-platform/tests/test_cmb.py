import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from gold_research.cmb import CmbGoldDataError, append_cmb_gold_snapshot, parse_cmb_gold_payload


@pytest.fixture
def cmb_payload():
    return {
        "returnCode": "SUC0000",
        "errorMsg": None,
        "body": {
            "time": "2026-08-17 09:41",
            "data": [
                {
                    "variety": "Au(T+D)",
                    "curPrice": "952.40",
                    "upDown": "12.77",
                    "open": "947.50",
                    "preClose": "940.44",
                    "high": "956.28",
                    "low": "946.50",
                    "avePrice": "951.01",
                    "tradeCount": "13004",
                    "time": "09:41:50",
                    "goldNo": "AUTD",
                },
                {
                    "variety": "Au99.99",
                    "curPrice": "953.00",
                    "upDown": "12.28",
                    "open": "946.00",
                    "preClose": "940.72",
                    "high": "956.00",
                    "low": "946.00",
                    "avePrice": "954.13",
                    "tradeCount": "71392",
                    "time": "09:41:46",
                    "goldNo": "AU9999",
                },
            ],
        },
    }


def test_parse_cmb_gold_payload_labels_sge_reference_not_bank_execution(cmb_payload):
    retrieved_at = datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc)
    frame = parse_cmb_gold_payload(cmb_payload, retrieved_at=retrieved_at)

    assert list(frame["gold_no"]) == ["AUTD", "AU9999"]
    assert frame.loc[1, "current_price"] == pytest.approx(953.00)
    assert frame.loc[0, "previous_close"] == pytest.approx(940.44)
    assert frame.loc[0, "trade_count"] == 13004
    assert frame["source_kind"].unique().tolist() == ["sge_market_snapshot_via_cmb_public_page"]
    assert not frame["is_executable_cmb_gold_account_quote"].any()
    assert frame["payload_sha256"].str.len().unique().tolist() == [64]
    assert frame["market_timestamp"].dt.tz is not None


def test_parse_cmb_gold_payload_rejects_unsuccessful_response():
    with pytest.raises(CmbGoldDataError, match="ERR0001"):
        parse_cmb_gold_payload(
            {"returnCode": "ERR0001", "errorMsg": "unavailable", "body": None},
            retrieved_at=datetime.now(timezone.utc),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: [], "JSON object"),
        (lambda payload: {**payload, "body": {**payload["body"], "data": [{**payload["body"]["data"][0], "curPrice": "NaN"}]}}, "curPrice"),
        (lambda payload: {**payload, "body": {**payload["body"], "data": [{**payload["body"]["data"][0], "curPrice": True}]}}, "curPrice"),
        (
            lambda payload: {
                **payload,
                "body": {
                    **payload["body"],
                    "data": [{**payload["body"]["data"][0], "tradeCount": "1.5"}],
                },
            },
            "tradeCount",
        ),
        (
            lambda payload: {
                **payload,
                "body": {**payload["body"], "data": [{**payload["body"]["data"][0], "tradeCount": False}]},
            },
            "tradeCount",
        ),
        (
            lambda payload: {
                **payload,
                "body": {**payload["body"], "data": [{**payload["body"]["data"][0], "goldNo": None}]},
            },
            "goldNo",
        ),
        (
            lambda payload: {
                **payload,
                "body": {**payload["body"], "data": [{**payload["body"]["data"][0], "variety": None}]},
            },
            "variety",
        ),
    ],
)
def test_parse_cmb_gold_payload_rejects_malformed_numeric_data(cmb_payload, mutate, message):
    with pytest.raises(CmbGoldDataError, match=message):
        parse_cmb_gold_payload(
            mutate(cmb_payload),
            retrieved_at=datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc),
        )


def test_failed_parquet_write_does_not_leave_partial_snapshot_files(tmp_path, cmb_payload, monkeypatch):
    frame = parse_cmb_gold_payload(
        cmb_payload,
        retrieved_at=datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc),
    )

    def fail_parquet(*args, **kwargs):
        raise RuntimeError("simulated parquet failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_parquet)
    with pytest.raises(RuntimeError, match="simulated parquet failure"):
        append_cmb_gold_snapshot(frame, tmp_path)

    assert not (tmp_path / "cmb_sge_gold_snapshots.csv").exists()
    assert not (tmp_path / "cmb_sge_gold_snapshots.parquet").exists()
    assert not (tmp_path / "data_manifest.json").exists()


def test_append_rejects_mismatched_source_provenance(tmp_path, cmb_payload):
    frame = parse_cmb_gold_payload(
        cmb_payload,
        retrieved_at=datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc),
    )
    frame.loc[0, "source_kind"] = "unrelated_source"

    with pytest.raises(CmbGoldDataError, match="source_kind"):
        append_cmb_gold_snapshot(frame, tmp_path)


@pytest.mark.parametrize("column", ["market_timestamp", "retrieved_at_utc"])
def test_append_rejects_missing_timestamps(tmp_path, cmb_payload, column):
    frame = parse_cmb_gold_payload(
        cmb_payload,
        retrieved_at=datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc),
    )
    frame.loc[0, column] = pd.NaT

    with pytest.raises(CmbGoldDataError, match="timestamp"):
        append_cmb_gold_snapshot(frame, tmp_path)


def test_append_rejects_conflicting_duplicate_market_rows(tmp_path, cmb_payload):
    frame = parse_cmb_gold_payload(
        cmb_payload,
        retrieved_at=datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc),
    )
    conflict = frame.iloc[[0]].copy()
    conflict.loc[:, "current_price"] = 999.0

    with pytest.raises(CmbGoldDataError, match="conflicting duplicate"):
        append_cmb_gold_snapshot(pd.concat([frame, conflict], ignore_index=True), tmp_path)


def test_append_rejects_tampered_existing_artifacts(tmp_path, cmb_payload):
    frame = parse_cmb_gold_payload(
        cmb_payload,
        retrieved_at=datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc),
    )
    append_cmb_gold_snapshot(frame, tmp_path)
    csv_path = tmp_path / "cmb_sge_gold_snapshots.csv"
    csv_path.write_text(csv_path.read_text() + "tampered\n")

    with pytest.raises(CmbGoldDataError, match="integrity"):
        append_cmb_gold_snapshot(frame, tmp_path)


def test_append_cmb_gold_snapshot_is_idempotent_and_writes_manifest(tmp_path, cmb_payload):
    frame = parse_cmb_gold_payload(
        cmb_payload,
        retrieved_at=datetime(2026, 8, 17, 1, 42, tzinfo=timezone.utc),
    )
    result = append_cmb_gold_snapshot(frame, tmp_path)
    repeated = append_cmb_gold_snapshot(frame, tmp_path)

    csv_frame = pd.read_csv(tmp_path / "cmb_sge_gold_snapshots.csv")
    parquet_frame = pd.read_parquet(tmp_path / "cmb_sge_gold_snapshots.parquet")
    manifest = json.loads((tmp_path / "data_manifest.json").read_text())

    assert result["rows"] == 2
    assert repeated["rows"] == 2
    assert len(csv_frame) == len(parquet_frame) == 2
    assert manifest["source_kind"] == "sge_market_snapshot_via_cmb_public_page"
    assert manifest["is_executable_cmb_gold_account_quote"] is False
    assert manifest["rows"] == 2
    assert len(manifest["csv_sha256"]) == 64
    assert len(manifest["latest_payload_sha256"]) == 64
