import hashlib
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.updates as updates_module
from quant_platform.datasets import (
    DatasetValidationError,
    publish_snapshot,
    snapshot_status,
)
from quant_platform.updates import (
    ConcurrentUpdateError,
    load_update_record,
    reconcile_daily_history,
    snapshot_update_lineage,
)
from quant_platform.market_sessions import (
    EXPECTED_SESSIONS_SOURCE_KIND,
    POLICY_VERSION,
)
from test_market_sessions import live_evidence


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


def test_update_preserves_and_detects_adjusted_close_revisions(tmp_path: Path):
    initial = _bars(["2026-08-18", "2026-08-19"]).assign(
        AdjustedClose=[3.01, 3.02]
    )
    first = _reconcile(
        tmp_path,
        initial,
        ["2026-08-18", "2026-08-19"],
        "2026-08-18",
        "2026-08-19",
    )
    revised = _bars(["2026-08-19", "2026-08-20"]).assign(
        AdjustedClose=[3.03, 3.04]
    )
    second = _reconcile(
        tmp_path,
        revised,
        ["2026-08-19", "2026-08-20"],
        "2026-08-19",
        "2026-08-20",
    )

    persisted = _snapshot_frame(second)
    assert persisted["AdjustedClose"].tolist() == [3.01, 3.03, 3.04]
    assert first["snapshot_id"] != second["snapshot_id"]
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
    parquet = Path(str(first["path"])) / "data.parquet"
    parquet.chmod(0o644)
    parquet.write_bytes(b"corrupt")

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
    assert stat.S_IMODE(Path(str(second["update_path"])).stat().st_mode) == 0o444
    assert (
        stat.S_IMODE(Path(str(second["update_path"])).parent.stat().st_mode)
        == 0o555
    )
    assert load_update_record(
        tmp_path, METADATA["instrument"], str(second["update_id"])
    ) == record


@pytest.mark.parametrize("writable_target", ["record", "directory"])
def test_existing_update_provenance_must_be_non_writable(
    tmp_path: Path, writable_target: str
):
    result = _reconcile(
        tmp_path,
        _bars(["2026-08-18", "2026-08-19"]),
        ["2026-08-18", "2026-08-19"],
        "2026-08-18",
        "2026-08-19",
    )
    record = Path(str(result["update_path"]))
    if writable_target == "record":
        record.chmod(0o644)
    else:
        record.parent.chmod(0o755)

    with pytest.raises(RuntimeError, match="writable"):
        load_update_record(
            tmp_path, METADATA["instrument"], str(result["update_id"])
        )


def _ordered_source_identity(
    *,
    result_snapshot_id: str,
    prior_snapshot_id: str | None,
    revision_count: int,
    predicate,
) -> tuple[dict[str, str], str]:
    expected_hash = hashlib.sha256(
        b"quant-platform-expected-sessions-v1\0" b"2026-08-18\n"
    ).hexdigest()
    for nonce in range(10_000):
        source = {
            "provider": METADATA["provider"],
            "instrument": METADATA["instrument"],
            "nonce": str(nonce),
        }
        identity = {
            "schema_version": 1,
            "metadata": dict(sorted(METADATA.items())),
            "request": {"start": "2026-08-18", "end": "2026-08-18"},
            "expected_sessions_sha256": expected_hash,
            "expected_session_count": 1,
            "fetched": {
                "start": "2026-08-18",
                "end": "2026-08-18",
                "rows": 1,
            },
            "prior_snapshot_id": prior_snapshot_id,
            "result_snapshot_id": result_snapshot_id,
            "revision_count": revision_count,
            "source": source,
            "expected_sessions_source": {
                "calendar": "XSHG",
                "library": "test",
                "version": "2026",
            },
        }
        update_id = hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        if predicate(update_id):
            return source, update_id
    raise AssertionError("could not construct adversarial update identity")


def test_snapshot_lineage_first_claim_survives_smaller_reversion_update_id(
    tmp_path: Path,
):
    root = tmp_path / "state"
    calendar = {"calendar": "XSHG", "library": "test", "version": "2026"}
    snapshot_a = publish_snapshot(
        _bars(["2026-08-18"], [6.10]),
        root,
        METADATA,
        update_latest=False,
    )
    first_source, first_id = _ordered_source_identity(
        result_snapshot_id=snapshot_a["snapshot_id"],
        prior_snapshot_id=None,
        revision_count=0,
        predicate=lambda value: value.startswith(("d", "e", "f")),
    )
    first = reconcile_daily_history(
        _bars(["2026-08-18"], [6.10]),
        ["2026-08-18"],
        root,
        METADATA,
        "2026-08-18",
        "2026-08-18",
        source_identity=first_source,
        expected_sessions_source=calendar,
    )
    claimed = snapshot_update_lineage(
        root, METADATA["instrument"], snapshot_a["snapshot_id"]
    )

    snapshot_b = publish_snapshot(
        _bars(["2026-08-18"], [6.20]),
        root,
        METADATA,
        update_latest=False,
    )
    middle_source, _ = _ordered_source_identity(
        result_snapshot_id=snapshot_b["snapshot_id"],
        prior_snapshot_id=snapshot_a["snapshot_id"],
        revision_count=1,
        predicate=lambda value: True,
    )
    reconcile_daily_history(
        _bars(["2026-08-18"], [6.20]),
        ["2026-08-18"],
        root,
        METADATA,
        "2026-08-18",
        "2026-08-18",
        source_identity=middle_source,
        expected_sessions_source=calendar,
    )
    reverted_source, reverted_id = _ordered_source_identity(
        result_snapshot_id=snapshot_a["snapshot_id"],
        prior_snapshot_id=snapshot_b["snapshot_id"],
        revision_count=1,
        predicate=lambda value: value < first_id,
    )
    reverted = reconcile_daily_history(
        _bars(["2026-08-18"], [6.10]),
        ["2026-08-18"],
        root,
        METADATA,
        "2026-08-18",
        "2026-08-18",
        source_identity=reverted_source,
        expected_sessions_source=calendar,
    )

    assert first["update_id"] == first_id
    assert reverted["update_id"] == reverted_id
    assert reverted_id < first_id
    assert snapshot_update_lineage(
        root, METADATA["instrument"], snapshot_a["snapshot_id"]
    ) == claimed
    assert claimed["update_id"] == first_id
    claim = (
        root
        / "snapshot-lineage"
        / METADATA["instrument"]
        / snapshot_a["snapshot_id"]
    )
    assert stat.S_IMODE(claim.stat().st_mode) == 0o555
    assert stat.S_IMODE((claim / "lineage.json").stat().st_mode) == 0o444


def test_legacy_snapshot_lineage_claim_is_permanent_after_reversion(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_a = publish_snapshot(
        _bars(["2026-08-18"], [6.10]), root, METADATA
    )
    assert snapshot_update_lineage(
        root, METADATA["instrument"], snapshot_a["snapshot_id"]
    ) == {"kind": "legacy_snapshot"}

    snapshot_b = publish_snapshot(
        _bars(["2026-08-18"], [6.20]), root, METADATA
    )
    source, _ = _ordered_source_identity(
        result_snapshot_id=snapshot_a["snapshot_id"],
        prior_snapshot_id=snapshot_b["snapshot_id"],
        revision_count=1,
        predicate=lambda value: True,
    )
    reconcile_daily_history(
        _bars(["2026-08-18"], [6.10]),
        ["2026-08-18"],
        root,
        METADATA,
        "2026-08-18",
        "2026-08-18",
        source_identity=source,
        expected_sessions_source={
            "calendar": "XSHG",
            "library": "test",
            "version": "2026",
        },
    )

    assert snapshot_update_lineage(
        root, METADATA["instrument"], snapshot_a["snapshot_id"]
    ) == {"kind": "legacy_snapshot"}


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


def _audited_metadata(instrument: str = "601288.SS") -> dict[str, str]:
    return METADATA | {"instrument": instrument, "provider": "xshg-audited-daily-v1"}


def _v2_reconcile(
    root: Path,
    bars: pd.DataFrame,
    sessions: list[str],
    evidence,
    *,
    prior: str | None,
    overlap: tuple[str, ...] = (),
    metadata: dict[str, str] | None = None,
):
    metadata = metadata or _audited_metadata()
    start, end = sessions[0], sessions[-1]
    return reconcile_daily_history(
        bars,
        sessions,
        root,
        metadata,
        start,
        end,
        source_identity={
            "provider": "xshg-audited-daily-v1",
            "instrument": metadata["instrument"],
            "generation": "test-live",
        },
        expected_sessions_source={
            "kind": EXPECTED_SESSIONS_SOURCE_KIND,
            "market": "XSHG",
            "instrument": metadata["instrument"],
            "start": start,
            "end": end,
            "evidence_sha256": evidence.digest,
            "policy_version": POLICY_VERSION,
        },
        expected_prior_snapshot_id=prior,
        protected_overlap_dates=overlap,
        market_session_evidence=evidence,
    )


def test_v2_first_backfill_seals_official_evidence_and_lineage(tmp_path: Path, monkeypatch):
    fetched, _, _ = live_evidence(
        monkeypatch, instrument="601288.SS", start="2026-08-31", end="2026-08-31"
    )
    result = _v2_reconcile(
        tmp_path, _bars(["2026-08-31"]), ["2026-08-31"], fetched.evidence, prior=None
    )
    record = load_update_record(tmp_path, "601288.SS", str(result["update_id"]))
    target = Path(str(result["update_path"])).parent

    assert record["schema_version"] == 2
    assert record["market_session_evidence_sha256"] == fetched.evidence.digest
    assert record["expected_sessions_source"]["kind"] == EXPECTED_SESSIONS_SOURCE_KIND
    assert set(path.name for path in target.iterdir()) == {
        "update.json",
        "market_sessions.json",
        *record["market_session_evidence_files"]["artifacts"].values(),
    }
    assert all(path.stat().st_mode & 0o222 == 0 for path in target.iterdir())
    assert snapshot_update_lineage(
        tmp_path, "601288.SS", str(result["snapshot_id"])
    )["market_session_evidence_sha256"] == fetched.evidence.digest


@pytest.mark.parametrize(
    "v2_field",
    [
        "market_session_evidence_sha256",
        "market_session_evidence_files",
        "prior_corporate_action_evidence_sha256",
        "result_corporate_action_evidence_sha256",
    ],
)
def test_loader_rejects_each_v1_record_v2_only_field(
    tmp_path: Path, monkeypatch, v2_field: str
):
    fetched, _, _ = live_evidence(monkeypatch, instrument="601288.SS")
    result = _v2_reconcile(
        tmp_path, _bars(["2026-08-31"]), ["2026-08-31"], fetched.evidence, prior=None
    )
    original_path = Path(str(result["update_path"]))
    original = json.loads(original_path.read_text())
    identity = {
        key: value
        for key, value in original.items()
        if key not in {"update_id", "created_at"}
    }
    identity["schema_version"] = 1
    identity["expected_sessions_source"] = {"bogus": True}
    for field in {
        "market_session_evidence_sha256",
        "market_session_evidence_files",
        "prior_corporate_action_evidence_sha256",
        "result_corporate_action_evidence_sha256",
    } - {v2_field}:
        identity.pop(field)
    if v2_field == "market_session_evidence_files":
        identity[v2_field] = {"bogus": True}
    update_id = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    target = tmp_path / "updates" / "601288.SS" / update_id
    target.mkdir()
    record = identity | {
        "update_id": update_id,
        "created_at": original["created_at"],
    }
    (target / "update.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    (target / "update.json").chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(RuntimeError, match="unexpected or missing record fields"):
        load_update_record(tmp_path, "601288.SS", update_id)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_loader_requires_exact_integer_update_schema_version(
    tmp_path: Path, schema_version
):
    result = _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    original = json.loads(Path(str(result["update_path"])).read_text())
    identity = {
        key: value
        for key, value in original.items()
        if key not in {"update_id", "created_at"}
    }
    identity["schema_version"] = schema_version
    update_id = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    target = tmp_path / "updates" / "601288.SS" / update_id
    target.mkdir()
    record = identity | {
        "update_id": update_id,
        "created_at": original["created_at"],
    }
    (target / "update.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    (target / "update.json").chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(RuntimeError, match="schema version"):
        load_update_record(tmp_path, "601288.SS", update_id)


@pytest.mark.parametrize(
    "location",
    ["expected_session_count", "fetched_rows", "revision_count"],
)
@pytest.mark.parametrize("value", [True, 1.0])
def test_loader_requires_exact_integer_update_count_fields(
    tmp_path: Path, location: str, value
):
    result = _reconcile(
        tmp_path,
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        "2026-08-18",
        "2026-08-18",
    )
    original = json.loads(Path(str(result["update_path"])).read_text())
    identity = {
        key: item
        for key, item in original.items()
        if key not in {"update_id", "created_at"}
    }
    if location == "fetched_rows":
        identity["fetched"]["rows"] = value
    else:
        identity[location] = value
    update_id = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    target = tmp_path / "updates" / "601288.SS" / update_id
    target.mkdir()
    record = identity | {
        "update_id": update_id,
        "created_at": original["created_at"],
    }
    (target / "update.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    (target / "update.json").chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(RuntimeError, match="count fields"):
        load_update_record(tmp_path, "601288.SS", update_id)


@pytest.mark.parametrize("location", ["claim_schema", "expected_session_count"])
@pytest.mark.parametrize("value", [True, 1.0])
def test_lineage_loader_requires_exact_integer_identity_fields(
    tmp_path: Path, location: str, value
):
    result = reconcile_daily_history(
        _bars(["2026-08-18"]),
        ["2026-08-18"],
        tmp_path,
        METADATA,
        "2026-08-18",
        "2026-08-18",
        source_identity={
            "provider": METADATA["provider"],
            "instrument": METADATA["instrument"],
        },
        expected_sessions_source={"calendar": "XSHG"},
    )
    claim_path = (
        tmp_path
        / "snapshot-lineage"
        / METADATA["instrument"]
        / str(result["snapshot_id"])
        / "lineage.json"
    )
    claim = json.loads(claim_path.read_text())
    if location == "claim_schema":
        claim["schema_version"] = value
    else:
        claim["lineage"]["expected_session_count"] = value
    identity = {
        key: item for key, item in claim.items() if key != "claim_sha256"
    }
    claim["claim_sha256"] = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    claim_path.parent.chmod(0o755)
    claim_path.chmod(0o644)
    claim_path.write_text(
        json.dumps(claim, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    claim_path.chmod(0o444)
    claim_path.parent.chmod(0o555)

    with pytest.raises(RuntimeError, match="identity|expected-session count"):
        snapshot_update_lineage(
            tmp_path, METADATA["instrument"], str(result["snapshot_id"])
        )


def test_v2_rejects_wrong_expected_source_kind_before_publication(tmp_path: Path, monkeypatch):
    fetched, _, _ = live_evidence(monkeypatch, instrument="601288.SS")
    with pytest.raises(DatasetValidationError, match="expected-session source mismatch"):
        reconcile_daily_history(
            _bars(["2026-08-31"]),
            ["2026-08-31"],
            tmp_path,
            _audited_metadata(),
            "2026-08-31",
            "2026-08-31",
            source_identity={
                "provider": "xshg-audited-daily-v1",
                "instrument": "601288.SS",
            },
            expected_sessions_source={
                "kind": "XSHG_OFFICIAL_ELIGIBLE_SESSIONS_V1",
                "market": "XSHG",
                "instrument": "601288.SS",
                "start": "2026-08-31",
                "end": "2026-08-31",
                "evidence_sha256": fetched.evidence.digest,
                "policy_version": POLICY_VERSION,
            },
            expected_prior_snapshot_id=None,
            market_session_evidence=fetched.evidence,
        )
    assert not (tmp_path / "updates").exists()


def test_v2_tampered_or_writable_evidence_fails_verification(tmp_path: Path, monkeypatch):
    fetched, _, _ = live_evidence(monkeypatch, instrument="601288.SS")
    result = _v2_reconcile(
        tmp_path, _bars(["2026-08-31"]), ["2026-08-31"], fetched.evidence, prior=None
    )
    record_path = Path(str(result["update_path"]))
    record = json.loads(record_path.read_text())
    artifact = record_path.parent / next(
        iter(record["market_session_evidence_files"]["artifacts"].values())
    )
    artifact.chmod(0o644)

    with pytest.raises(RuntimeError, match="writable"):
        load_update_record(tmp_path, "601288.SS", str(result["update_id"]))


@pytest.mark.parametrize(
    "tamper",
    ["undeclared", "missing", "symlink", "hardlink", "document", "directory"],
)
def test_v2_evidence_topology_and_tampering_fail_closed(
    tmp_path: Path, monkeypatch, tamper: str
):
    fetched, _, _ = live_evidence(monkeypatch, instrument="601288.SS")
    result = _v2_reconcile(
        tmp_path, _bars(["2026-08-31"]), ["2026-08-31"], fetched.evidence, prior=None
    )
    target = Path(str(result["update_path"])).parent
    record = json.loads(Path(str(result["update_path"])).read_text())
    artifact = target / next(
        iter(record["market_session_evidence_files"]["artifacts"].values())
    )
    target.chmod(0o755)
    if tamper == "undeclared":
        (target / "extra.bin").write_bytes(b"x")
    elif tamper == "missing":
        artifact.unlink()
    elif tamper == "symlink":
        artifact.unlink()
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"x")
        artifact.symlink_to(outside)
    elif tamper == "hardlink":
        os.link(artifact, tmp_path / "hardlink.bin")
    elif tamper == "document":
        document = target / "market_sessions.json"
        document.chmod(0o644)
        document.write_bytes(b"{}\n")
        document.chmod(0o444)
    else:
        pass

    with pytest.raises(RuntimeError, match="corrupt|symlink|hard link|writable"):
        load_update_record(tmp_path, "601288.SS", str(result["update_id"]))


def test_v2_non_publishable_synthetic_evidence_rejects_before_writes(
    tmp_path: Path, monkeypatch
):
    fetched, _, _ = live_evidence(monkeypatch, instrument="601288.SS")
    synthetic = replace(fetched.evidence, publishable=False)
    with pytest.raises(DatasetValidationError, match="not publishable"):
        _v2_reconcile(
            tmp_path,
            _bars(["2026-08-31"]),
            ["2026-08-31"],
            synthetic,
            prior=None,
        )
    assert not (tmp_path / "updates").exists()


def test_v2_generation_token_rejects_stale_prior_before_publication(tmp_path: Path, monkeypatch):
    prior = publish_snapshot(_bars(["2026-08-31"]), tmp_path, _audited_metadata())
    snapshot_update_lineage(tmp_path, "601288.SS", str(prior["snapshot_id"]))
    fetched, _, _ = live_evidence(monkeypatch, instrument="601288.SS")
    latest = tmp_path / "datasets" / "601288.SS" / "latest.json"
    before = latest.read_bytes()

    with pytest.raises(ConcurrentUpdateError, match="CONCURRENT_UPDATE"):
        _v2_reconcile(
            tmp_path,
            _bars(["2026-08-31"]),
            ["2026-08-31"],
            fetched.evidence,
            prior="0" * 64,
            overlap=("2026-08-31",),
        )
    assert latest.read_bytes() == before
    assert not (tmp_path / "updates" / "601288.SS").exists()


def test_v2_cross_source_overlap_conflict_preserves_latest(tmp_path: Path, monkeypatch):
    prior = publish_snapshot(_bars(["2026-08-31"], [6.10]), tmp_path, _audited_metadata())
    snapshot_update_lineage(tmp_path, "601288.SS", str(prior["snapshot_id"]))
    fetched, _, _ = live_evidence(monkeypatch, instrument="601288.SS")
    latest = tmp_path / "datasets" / "601288.SS" / "latest.json"
    before = latest.read_bytes()

    with pytest.raises(DatasetValidationError, match="SOURCE_CONFLICT.*prior_snapshot_id"):
        _v2_reconcile(
            tmp_path,
            _bars(["2026-08-31"], [6.20]),
            ["2026-08-31"],
            fetched.evidence,
            prior=str(prior["snapshot_id"]),
            overlap=("2026-08-31",),
        )
    assert latest.read_bytes() == before


def test_v2_schema4_carries_exact_corporate_action_evidence(tmp_path: Path, monkeypatch):
    from test_corporate_actions import bocom_evidence

    metadata = _audited_metadata("601328.SS")
    action_evidence = bocom_evidence()
    prior = publish_snapshot(
        _bars(["2026-08-31"]),
        tmp_path,
        metadata,
        corporate_action_evidence=action_evidence,
    )
    snapshot_update_lineage(tmp_path, "601328.SS", str(prior["snapshot_id"]))
    fetched, _, _ = live_evidence(monkeypatch)
    result = _v2_reconcile(
        tmp_path,
        _bars(["2026-08-31"]),
        ["2026-08-31"],
        fetched.evidence,
        prior=str(prior["snapshot_id"]),
        overlap=("2026-08-31",),
        metadata=metadata,
    )
    prior_target = Path(str(prior["path"]))
    result_target = Path(str(result["path"]))
    prior_manifest = json.loads((prior_target / "manifest.json").read_text())
    result_manifest = json.loads((result_target / "manifest.json").read_text())

    assert result_manifest["schema_version"] == 4
    assert result_manifest["corporate_action_evidence_sha256"] == prior_manifest[
        "corporate_action_evidence_sha256"
    ]
    for name in set(prior_manifest["files"]["corporate_action_artifacts"].values()) | {
        "corporate_actions.json"
    }:
        assert (result_target / name).read_bytes() == (prior_target / name).read_bytes()
