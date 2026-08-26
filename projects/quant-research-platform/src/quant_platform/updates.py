from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .datasets import (
    DatasetValidationError,
    _atomic_json,
    _canonical_json,
    _fsync_directory,
    _InstrumentLock,
    _normalize_frame,
    _sha256,
    _validate_metadata,
    publish_snapshot,
    snapshot_status,
)


class ConcurrentUpdateError(RuntimeError):
    """Raised when latest changes before a reconciled update can commit."""


def _session_date(value: Any, label: str) -> pd.Timestamp:
    try:
        date = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise DatasetValidationError(f"invalid {label}: {value!r}") from exc
    if pd.isna(date) or date.tz is not None or date != date.normalize():
        raise DatasetValidationError(f"{label} must be a timezone-naive daily date")
    return date


def _normalize_expected_sessions(
    values: Iterable[Any], start: pd.Timestamp, end: pd.Timestamp
) -> tuple[pd.DatetimeIndex, str]:
    sessions = [_session_date(value, "expected session") for value in values]
    if not sessions:
        raise DatasetValidationError("expected sessions cannot be empty")
    if len(set(sessions)) != len(sessions):
        raise DatasetValidationError("duplicate expected sessions are not allowed")
    normalized = pd.DatetimeIndex(sorted(sessions))
    outside = normalized[(normalized < start) | (normalized > end)]
    if len(outside):
        rendered = ", ".join(str(value.date()) for value in outside)
        raise DatasetValidationError(
            f"expected sessions must be inside the requested range: {rendered}"
        )
    payload = (
        b"quant-platform-expected-sessions-v1\0"
        + "".join(f"{value.date()}\n" for value in normalized).encode()
    )
    return normalized, _sha256(payload)


def _revision_count(previous: pd.DataFrame, fetched: pd.DataFrame) -> int:
    if previous.empty or fetched.empty:
        return 0
    previous_by_date = previous.set_index("Date")
    fetched_by_date = fetched.set_index("Date")
    overlap = previous_by_date.index.intersection(fetched_by_date.index)
    if overlap.empty:
        return 0
    columns = ["Open", "High", "Low", "Close", "Volume"]
    changed = previous_by_date.loc[overlap, columns].ne(
        fetched_by_date.loc[overlap, columns]
    )
    return int(changed.any(axis=1).sum())


def _publish_update_record(
    root: Path, instrument: str, identity: dict[str, Any]
) -> tuple[str, Path]:
    update_id = _sha256(_canonical_json(identity))
    updates_root = root / "updates" / instrument
    updates_root.mkdir(parents=True, exist_ok=True)
    updates_root.chmod(0o755)
    target = updates_root / update_id
    record = identity | {
        "update_id": update_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if not target.exists():
        temporary = Path(tempfile.mkdtemp(prefix=f".{update_id}.", dir=updates_root))
        try:
            _atomic_json(temporary / "update.json", record)
            temporary.chmod(0o755)
            _fsync_directory(temporary)
            try:
                os.rename(temporary, target)
            except FileExistsError:
                pass
            else:
                _fsync_directory(updates_root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    try:
        stored = json.loads((target / "update.json").read_text(encoding="utf-8"))
        stored_identity = {
            key: value for key, value in stored.items() if key not in {"update_id", "created_at"}
        }
        if stored.get("update_id") != update_id or stored_identity != identity:
            raise ValueError("record identity mismatch")
        datetime.fromisoformat(stored["created_at"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"corrupt update provenance {target}: {exc}") from exc
    return update_id, target / "update.json"


def _commit_latest(
    root: Path,
    instrument: str,
    snapshot_id: str,
    expected_prior_snapshot_id: str | None,
) -> None:
    pointer = root / "datasets" / instrument / "latest.json"
    current_snapshot_id = (
        snapshot_status(root, instrument)["snapshot_id"] if pointer.exists() else None
    )
    if current_snapshot_id != expected_prior_snapshot_id:
        raise ConcurrentUpdateError(
            "latest snapshot changed while the daily update was being committed"
        )
    _atomic_json(pointer, {"snapshot_id": snapshot_id, "path": snapshot_id})


def reconcile_daily_history(
    fetched_bars: pd.DataFrame,
    expected_sessions: Iterable[Any],
    root: Path | str,
    metadata: dict[str, str],
    start: Any,
    end: Any,
) -> dict[str, str | int]:
    """Reconcile requested daily bars into a verified immutable history snapshot."""

    root = Path(root).resolve()
    normalized_metadata = _validate_metadata(metadata)
    request_start = _session_date(start, "request start")
    request_end = _session_date(end, "request end")
    if request_start > request_end:
        raise DatasetValidationError("request start must not be after request end")
    sessions, expected_sessions_sha256 = _normalize_expected_sessions(
        expected_sessions, request_start, request_end
    )
    normalized_fetched = _normalize_frame(fetched_bars)
    fetched_start = normalized_fetched["Date"].min()
    fetched_end = normalized_fetched["Date"].max()
    requested_fetched = normalized_fetched[
        normalized_fetched["Date"].between(request_start, request_end)
    ].reset_index(drop=True)

    instrument = normalized_metadata["instrument"]
    with _InstrumentLock(root, instrument):
        prior_snapshot_id: str | None = None
        previous = pd.DataFrame(columns=normalized_fetched.columns)
        latest_pointer = root / "datasets" / instrument / "latest.json"
        if latest_pointer.exists():
            prior = snapshot_status(root, instrument)
            prior_snapshot_id = prior["snapshot_id"]
            prior_path = Path(prior["path"])
            prior_manifest = json.loads(
                (prior_path / "manifest.json").read_text(encoding="utf-8")
            )
            mismatches = [
                field
                for field, value in normalized_metadata.items()
                if prior_manifest["metadata"].get(field) != value
            ]
            if mismatches:
                raise DatasetValidationError(
                    f"latest snapshot metadata mismatch: {', '.join(sorted(mismatches))}"
                )
            previous = _normalize_frame(pd.read_parquet(prior_path / "data.parquet"))

        revision_count = _revision_count(previous, requested_fetched)
        fetched_dates = set(requested_fetched["Date"])
        preserved = previous[~previous["Date"].isin(fetched_dates)]
        if preserved.empty:
            merged_input = requested_fetched
        elif requested_fetched.empty:
            merged_input = preserved
        else:
            merged_input = pd.concat([preserved, requested_fetched], ignore_index=True)
        merged_dates = set(merged_input["Date"])
        missing = [session for session in sessions if session not in merged_dates]
        if missing:
            rendered = ", ".join(str(value.date()) for value in missing)
            raise DatasetValidationError(
                f"missing expected sessions after merge: {rendered}"
            )
        merged = _normalize_frame(merged_input)

        snapshot = publish_snapshot(
            merged, root, normalized_metadata, update_latest=False
        )
        identity = {
            "schema_version": 1,
            "metadata": normalized_metadata,
            "request": {
                "start": str(request_start.date()),
                "end": str(request_end.date()),
            },
            "expected_sessions_sha256": expected_sessions_sha256,
            "expected_session_count": len(sessions),
            "fetched": {
                "start": str(fetched_start.date()),
                "end": str(fetched_end.date()),
                "rows": len(normalized_fetched),
            },
            "prior_snapshot_id": prior_snapshot_id,
            "result_snapshot_id": snapshot["snapshot_id"],
            "revision_count": revision_count,
        }
        update_id, update_path = _publish_update_record(root, instrument, identity)
        _commit_latest(
            root,
            instrument,
            snapshot["snapshot_id"],
            prior_snapshot_id,
        )
        return snapshot | {
            "update_id": update_id,
            "update_path": str(update_path),
            "revision_count": revision_count,
        }
