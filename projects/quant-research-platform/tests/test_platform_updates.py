import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.datasets import DatasetValidationError
from quant_platform.updates import reconcile_daily_history


METADATA = {
    "instrument": "601288.SS",
    "provider": "synthetic",
    "market": "XSHG",
    "currency": "CNY",
    "adjustment": "unadjusted",
}


def _bars(dates: list[str], closes: list[float] | None = None) -> pd.DataFrame:
    closes = closes or [6.10 + index / 100 for index in range(len(dates))]
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": [close - 0.02 for close in closes],
            "High": [close + 0.04 for close in closes],
            "Low": [close - 0.05 for close in closes],
            "Close": closes,
            "Volume": [1000 + index for index in range(len(dates))],
        }
    )


def _reconcile(
    root: Path,
    bars: pd.DataFrame,
    sessions: list[str],
    start: str,
    end: str,
    metadata: dict[str, str] | None = None,
) -> dict[str, str | int]:
    return reconcile_daily_history(
        bars,
        sessions,
        root,
        metadata or METADATA,
        start,
        end,
    )


def _snapshot_frame(result: dict[str, str | int]) -> pd.DataFrame:
    return pd.read_parquet(Path(str(result["path"])) / "data.parquet")


def test_first_update_backfills_exact_requested_inclusive_range(tmp_path: Path):
    result = _reconcile(
        tmp_path,
        _bars(["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20"]),
        ["2026-08-18", "2026-08-19"],
        "2026-08-18",
        "2026-08-19",
    )

    assert result["status"] == "CREATED"
    assert _snapshot_frame(result)["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-18",
        "2026-08-19",
    ]


def test_later_update_merges_verified_history_with_fetched_bars(tmp_path: Path):
    first = _reconcile(
        tmp_path,
        _bars(["2026-08-18", "2026-08-19"]),
        ["2026-08-18", "2026-08-19"],
        "2026-08-18",
        "2026-08-19",
    )
    second = _reconcile(
        tmp_path,
        _bars(["2026-08-19", "2026-08-20"], [6.11, 6.12]),
        ["2026-08-19", "2026-08-20"],
        "2026-08-19",
        "2026-08-20",
    )

    assert first["snapshot_id"] != second["snapshot_id"]
    assert _snapshot_frame(second)["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
    ]


def test_identical_update_is_idempotent(tmp_path: Path):
    bars = _bars(["2026-08-18", "2026-08-19"])
    first = _reconcile(
        tmp_path, bars, ["2026-08-18", "2026-08-19"], "2026-08-18", "2026-08-19"
    )
    second = _reconcile(
        tmp_path, bars, ["2026-08-18", "2026-08-19"], "2026-08-18", "2026-08-19"
    )
    snapshots = [
        path
        for path in (tmp_path / "datasets" / METADATA["instrument"]).iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]

    assert second["status"] == "NO_CHANGE"
    assert second["snapshot_id"] == first["snapshot_id"]
    assert len(snapshots) == 1


def test_historical_revision_creates_snapshot_and_preserves_old(tmp_path: Path):
    first = _reconcile(
        tmp_path,
        _bars(["2026-08-18", "2026-08-19"]),
        ["2026-08-18", "2026-08-19"],
        "2026-08-18",
        "2026-08-19",
    )
    revised = _bars(["2026-08-18", "2026-08-19"], [6.10, 6.50])
    second = _reconcile(
        tmp_path,
        revised,
        ["2026-08-18", "2026-08-19"],
        "2026-08-18",
        "2026-08-19",
    )

    assert first["snapshot_id"] != second["snapshot_id"]
    assert Path(str(first["path"])).is_dir()
    assert _snapshot_frame(first)["Close"].tolist() != _snapshot_frame(second)["Close"].tolist()
    assert second["revision_count"] == 1


def test_duplicate_or_conflicting_fetched_rows_fail_closed(tmp_path: Path):
    fetched = _bars(["2026-08-18", "2026-08-18"], [6.10, 6.20])

    with pytest.raises(DatasetValidationError, match="duplicate"):
        _reconcile(
            tmp_path,
            fetched,
            ["2026-08-18"],
            "2026-08-18",
            "2026-08-18",
        )

    assert not (tmp_path / "datasets").exists()


def test_missing_expected_session_keeps_latest_pointer_unchanged(tmp_path: Path):
    _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    latest = tmp_path / "datasets" / METADATA["instrument"] / "latest.json"
    before = latest.read_bytes()

    with pytest.raises(DatasetValidationError, match="missing expected sessions.*2026-08-20"):
        _reconcile(
            tmp_path,
            _bars(["2026-08-19"]),
            ["2026-08-19", "2026-08-20"],
            "2026-08-19",
            "2026-08-20",
        )

    assert latest.read_bytes() == before


def test_dates_outside_request_cannot_satisfy_completeness(tmp_path: Path):
    with pytest.raises(DatasetValidationError, match="missing expected sessions.*2026-08-19"):
        _reconcile(
            tmp_path,
            _bars(["2026-08-17", "2026-08-18"]),
            ["2026-08-18", "2026-08-19"],
            "2026-08-18",
            "2026-08-19",
        )

    assert not (tmp_path / "datasets").exists()


@pytest.mark.parametrize("field", ["provider", "market", "currency", "adjustment"])
def test_update_rejects_metadata_mismatch_with_latest(tmp_path: Path, field: str):
    _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    mismatched = METADATA | {field: "different"}

    with pytest.raises(DatasetValidationError, match=f"metadata mismatch.*{field}"):
        _reconcile(
            tmp_path,
            _bars(["2026-08-19"]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
            mismatched,
        )


def test_update_rejects_instrument_mismatch_in_latest_pointer(tmp_path: Path):
    first = _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    other_root = tmp_path / "datasets" / "OTHER"
    other_root.mkdir()
    (other_root / "latest.json").write_text(
        json.dumps({"snapshot_id": first["snapshot_id"], "path": first["snapshot_id"]})
    )

    with pytest.raises(RuntimeError, match="latest snapshot pointer is invalid"):
        _reconcile(
            tmp_path,
            _bars(["2026-08-19"]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
            METADATA | {"instrument": "OTHER"},
        )


def test_update_cannot_use_corrupt_latest_snapshot(tmp_path: Path):
    first = _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    (Path(str(first["path"])) / "data.parquet").write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="latest snapshot pointer is invalid"):
        _reconcile(
            tmp_path,
            _bars(["2026-08-19"]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
        )


def test_update_provenance_is_content_addressed_and_complete(tmp_path: Path):
    first = _reconcile(
        tmp_path,
        _bars(["2026-08-18", "2026-08-19"]),
        ["2026-08-18", "2026-08-19"],
        "2026-08-18",
        "2026-08-19",
    )
    second = _reconcile(
        tmp_path,
        _bars(["2026-08-19", "2026-08-20"], [6.50, 6.20]),
        ["2026-08-19", "2026-08-20"],
        "2026-08-19",
        "2026-08-20",
    )
    record = json.loads(Path(str(second["update_path"])).read_text())
    expected_hash = hashlib.sha256(
        b"quant-platform-expected-sessions-v1\0"
        b"2026-08-19\n2026-08-20\n"
    ).hexdigest()

    assert Path(str(second["update_path"])).name == "update.json"
    assert Path(str(second["update_path"])).parent.name == second["update_id"]
    assert record["request"] == {"start": "2026-08-19", "end": "2026-08-20"}
    assert record["expected_sessions_sha256"] == expected_hash
    assert record["fetched"] == {
        "start": "2026-08-19",
        "end": "2026-08-20",
        "rows": 2,
    }
    assert record["prior_snapshot_id"] == first["snapshot_id"]
    assert record["result_snapshot_id"] == second["snapshot_id"]
    assert record["revision_count"] == 1
    assert record["update_id"] == second["update_id"]
