import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.updates as updates_module
from quant_platform.datasets import (
    DatasetValidationError,
    snapshot_status,
)
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


def test_provenance_failure_does_not_move_latest_pointer(tmp_path: Path, monkeypatch):
    first = _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    latest = tmp_path / "datasets" / METADATA["instrument"] / "latest.json"
    before = latest.read_bytes()

    def fail_provenance(*args, **kwargs):
        raise OSError("provenance store unavailable")

    monkeypatch.setattr(updates_module, "_publish_update_record", fail_provenance)

    with pytest.raises(OSError, match="provenance store unavailable"):
        _reconcile(
            tmp_path,
            _bars(["2026-08-19"]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
        )

    assert latest.read_bytes() == before
    assert snapshot_status(tmp_path, METADATA["instrument"])["snapshot_id"] == first["snapshot_id"]


def _seed_update_and_mirror(tmp_path: Path) -> tuple[Path, dict[str, str | int]]:
    root = tmp_path / "state"
    mirror = tmp_path / "mirror"
    for store in (root, mirror):
        _reconcile(
            store,
            _bars(["2026-08-18"]),
            ["2026-08-18"],
            "2026-08-18",
            "2026-08-18",
        )
    next_update = _reconcile(
        mirror,
        _bars(["2026-08-19"]),
        ["2026-08-19"],
        "2026-08-19",
        "2026-08-19",
    )
    return root, next_update


@pytest.mark.parametrize(
    "symlinked_component",
    ["updates", "instrument", "target", "update.json"],
)
def test_update_provenance_rejects_symlinked_store_components_without_moving_latest(
    tmp_path: Path, symlinked_component: str
):
    root, mirrored = _seed_update_and_mirror(tmp_path)
    instrument = METADATA["instrument"]
    updates_root = root / "updates"
    instrument_root = updates_root / instrument
    target = instrument_root / str(mirrored["update_id"])
    mirrored_target = Path(str(mirrored["update_path"])).parent
    latest = root / "datasets" / instrument / "latest.json"
    before = latest.read_bytes()

    if symlinked_component == "updates":
        updates_root.rename(root / "updates-original")
        updates_root.symlink_to(mirrored_target.parent.parent, target_is_directory=True)
    elif symlinked_component == "instrument":
        instrument_root.rename(updates_root / f"{instrument}-original")
        instrument_root.symlink_to(mirrored_target.parent, target_is_directory=True)
    elif symlinked_component == "target":
        target.symlink_to(mirrored_target, target_is_directory=True)
    else:
        target.mkdir()
        (target / "update.json").symlink_to(mirrored_target / "update.json")

    with pytest.raises(RuntimeError, match="symlink"):
        _reconcile(
            root,
            _bars(["2026-08-19"]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
        )

    assert latest.read_bytes() == before


def test_update_provenance_pins_staging_directory_during_record_creation(
    tmp_path: Path, monkeypatch
):
    root, mirrored = _seed_update_and_mirror(tmp_path)
    instrument = METADATA["instrument"]
    latest = root / "datasets" / instrument / "latest.json"
    before = latest.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    original_validate = updates_module._validate_update_store_path
    swapped = False

    def swap_staging_after_validation(path, configured_root):
        nonlocal swapped
        original_validate(path, configured_root)
        staging = path.parent
        if (
            not swapped
            and path.name == "update.json"
            and staging.name.startswith(f".{mirrored['update_id']}.")
        ):
            displaced = staging.with_name(f"{staging.name}.pinned")
            staging.rename(displaced)
            staging.symlink_to(outside, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(
        updates_module, "_validate_update_store_path", swap_staging_after_validation
    )

    with pytest.raises(RuntimeError, match="symlink|changed"):
        _reconcile(
            root,
            _bars(["2026-08-19"]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
        )

    assert swapped is True
    assert (
        outside.stat().st_mode & 0o777,
        sorted(path.name for path in outside.iterdir()),
    ) == (0o700, [])
    assert latest.read_bytes() == before


@pytest.mark.parametrize("corrupt_record", ["{", "{}", "[]"])
def test_corrupt_existing_update_record_does_not_move_latest(
    tmp_path: Path, corrupt_record: str
):
    root, mirrored = _seed_update_and_mirror(tmp_path)
    instrument = METADATA["instrument"]
    target = root / "updates" / instrument / str(mirrored["update_id"])
    target.mkdir()
    (target / "update.json").write_text(corrupt_record, encoding="utf-8")
    latest = root / "datasets" / instrument / "latest.json"
    before = latest.read_bytes()

    with pytest.raises(RuntimeError, match="corrupt update provenance"):
        _reconcile(
            root,
            _bars(["2026-08-19"]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
        )

    assert latest.read_bytes() == before


def test_concurrent_reconciliations_serialize_without_losing_updates(
    tmp_path: Path, monkeypatch
):
    _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    original_publish = updates_module._publish_update_record
    first_reached_provenance = threading.Event()
    release_first = threading.Event()

    def pause_first_update(root, instrument, identity):
        result = original_publish(root, instrument, identity)
        if identity["request"]["end"] == "2026-08-19":
            first_reached_provenance.set()
            assert release_first.wait(timeout=5)
        return result

    monkeypatch.setattr(updates_module, "_publish_update_record", pause_first_update)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _reconcile,
            tmp_path,
            _bars(["2026-08-19"], [6.50]),
            ["2026-08-19"],
            "2026-08-19",
            "2026-08-19",
        )
        assert first_reached_provenance.wait(timeout=5)
        second = executor.submit(
            _reconcile,
            tmp_path,
            _bars(["2026-08-20"], [6.60]),
            ["2026-08-20"],
            "2026-08-20",
            "2026-08-20",
        )
        assert not second.done()
        release_first.set()
        first.result(timeout=5)
        final = second.result(timeout=5)

    assert _snapshot_frame(final)["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
    ]
