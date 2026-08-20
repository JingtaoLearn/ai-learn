import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.datasets import DatasetValidationError, publish_snapshot, snapshot_status


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-17", "2026-08-18", "2026-08-19"]),
            "Open": [6.10, 6.12, 6.18],
            "High": [6.15, 6.20, 6.24],
            "Low": [6.05, 6.08, 6.14],
            "Close": [6.12, 6.18, 6.20],
            "Volume": [1000, 1200, 1100],
        }
    )


def _metadata() -> dict[str, str]:
    return {
        "instrument": "601288.SS",
        "provider": "synthetic",
        "market": "XSHG",
        "currency": "CNY",
        "adjustment": "unadjusted",
    }


def test_publish_snapshot_is_content_addressed_atomic_and_idempotent(tmp_path: Path):
    first = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    second = publish_snapshot(_daily_frame().iloc[::-1], tmp_path, _metadata())

    assert first["status"] == "CREATED"
    assert second["status"] == "NO_CHANGE"
    assert first["snapshot_id"] == second["snapshot_id"]

    snapshot_dir = Path(first["path"])
    manifest = json.loads((snapshot_dir / "manifest.json").read_text())
    assert (snapshot_dir / "data.parquet").exists()
    assert len(manifest["canonical_sha256"]) == 64
    assert len(manifest["parquet_sha256"]) == 64
    assert manifest["rows"] == 3
    assert manifest["data_start"] == "2026-08-17"
    assert manifest["data_end"] == "2026-08-19"
    assert (snapshot_dir.stat().st_mode & 0o777) == 0o755
    assert ((snapshot_dir / "manifest.json").stat().st_mode & 0o777) == 0o644
    assert ((snapshot_dir / "data.parquet").stat().st_mode & 0o777) == 0o644

    status = snapshot_status(tmp_path, "601288.SS")
    assert status["snapshot_id"] == first["snapshot_id"]
    assert status["path"] == first["path"]


def test_historical_revision_creates_new_snapshot_without_overwriting_old(tmp_path: Path):
    first = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    revised = _daily_frame()
    revised.loc[1, "Close"] = 6.17
    second = publish_snapshot(revised, tmp_path, _metadata())

    assert second["status"] == "CREATED"
    assert second["snapshot_id"] != first["snapshot_id"]
    assert Path(first["path"]).exists()
    assert Path(second["path"]).exists()
    assert snapshot_status(tmp_path, "601288.SS")["snapshot_id"] == second["snapshot_id"]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda frame: frame.drop(columns=["Open"]), "missing required columns"),
        (lambda frame: pd.concat([frame, frame.iloc[[0]]]), "duplicate dates"),
        (lambda frame: frame.assign(Close=[6.12, float("nan"), 6.20]), "non-finite"),
        (lambda frame: frame.assign(Open=[True, False, True]), "boolean"),
        (lambda frame: frame.assign(Open=[6.10, 0.0, 6.18]), "strictly positive"),
        (lambda frame: frame.assign(Volume=[1000, -1, 1100]), "non-negative"),
        (lambda frame: frame.assign(High=[6.15, 6.10, 6.24]), "High"),
        (lambda frame: frame.assign(Low=[6.05, 6.30, 6.14]), "Low"),
    ],
)
def test_snapshot_rejects_invalid_market_data(tmp_path: Path, mutator, message: str):
    with pytest.raises(DatasetValidationError, match=message):
        publish_snapshot(mutator(_daily_frame()), tmp_path, _metadata())


def test_snapshot_rejects_unsafe_instrument_and_unknown_metadata(tmp_path: Path):
    unsafe = _metadata() | {"instrument": "../601288"}
    unknown = _metadata() | {"retrieved_at": "caller-controlled"}

    with pytest.raises(DatasetValidationError, match="instrument"):
        publish_snapshot(_daily_frame(), tmp_path, unsafe)
    with pytest.raises(DatasetValidationError, match="metadata fields"):
        publish_snapshot(_daily_frame(), tmp_path, unknown)


def test_existing_snapshot_corruption_fails_closed(tmp_path: Path):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    (Path(published["path"]) / "data.parquet").write_bytes(b"corrupted")

    with pytest.raises(RuntimeError, match="corrupt snapshot"):
        publish_snapshot(_daily_frame(), tmp_path, _metadata())


def test_latest_pointer_cannot_redirect_outside_instrument_store(tmp_path: Path):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    pointer = tmp_path / "datasets" / "601288.SS" / "latest.json"
    pointer.write_text(
        json.dumps({"snapshot_id": published["snapshot_id"], "path": str(tmp_path)})
    )

    with pytest.raises(RuntimeError, match="latest snapshot pointer"):
        snapshot_status(tmp_path, "601288.SS")


def test_snapshot_pointer_survives_restore_under_a_different_root(tmp_path: Path):
    original = tmp_path / "original"
    published = publish_snapshot(_daily_frame(), original, _metadata())
    pointer = json.loads(
        (original / "datasets" / "601288.SS" / "latest.json").read_text()
    )
    assert pointer["path"] == published["snapshot_id"]

    restored = tmp_path / "restored"
    shutil.copytree(original, restored)
    status = snapshot_status(restored, "601288.SS")
    assert status["snapshot_id"] == published["snapshot_id"]
    assert Path(status["path"]).is_relative_to(restored)


def test_snapshot_identity_preserves_full_float64_precision(tmp_path: Path):
    first_frame = _daily_frame()
    first_frame.loc[2, "Close"] = 6.20000000001
    second_frame = _daily_frame()
    second_frame.loc[2, "Close"] = 6.20000000002

    first = publish_snapshot(first_frame, tmp_path, _metadata())
    second = publish_snapshot(second_frame, tmp_path, _metadata())

    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["status"] == "CREATED"


def test_daily_snapshot_rejects_timezone_aware_or_intraday_dates(tmp_path: Path):
    timezone_aware = _daily_frame()
    timezone_aware["Date"] = pd.to_datetime(timezone_aware["Date"]).dt.tz_localize(
        "Asia/Shanghai"
    )
    intraday = _daily_frame()
    intraday.loc[1, "Date"] = pd.Timestamp("2026-08-18 09:30:00")

    with pytest.raises(DatasetValidationError, match="timezone-naive"):
        publish_snapshot(timezone_aware, tmp_path, _metadata())
    with pytest.raises(DatasetValidationError, match="midnight"):
        publish_snapshot(intraday, tmp_path, _metadata())


def test_daily_snapshot_rejects_empty_data(tmp_path: Path):
    with pytest.raises(DatasetValidationError, match="at least one row"):
        publish_snapshot(_daily_frame().iloc[0:0], tmp_path, _metadata())
