from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .datasets import (
    DatasetValidationError,
    _atomic_json,
    _canonical_json,
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


def _validate_update_store_path(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"update provenance path cannot be a symlink: {path}")
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise RuntimeError(f"update provenance path cannot be resolved: {path}: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"update provenance path escapes configured root: {path}")


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_absolute_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def _open_directory_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    create: bool = False,
    mode: int = 0o755,
) -> tuple[int | None, bool]:
    created = False
    if create:
        try:
            os.mkdir(name, mode=mode, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(name, flags, dir_fd=parent_fd), created
    except FileNotFoundError:
        if not create:
            return None, False
        raise
    except OSError as exc:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            metadata = None
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(
                f"update provenance {label} cannot be a symlink: {name}"
            ) from exc
        raise RuntimeError(
            f"update provenance {label} is not a safe directory: {name}: {exc}"
        ) from exc


def _require_pinned_entry(
    parent_fd: int,
    name: str,
    pinned_fd: int,
    label: str,
    *,
    directory: bool,
) -> None:
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        pinned = os.fstat(pinned_fd)
    except OSError as exc:
        raise RuntimeError(f"update provenance {label} changed during use: {name}") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(entry.st_mode) or not _same_inode(entry, pinned):
        raise RuntimeError(f"update provenance {label} changed during use: {name}")


def _create_staging_directory(updates_fd: int, update_id: str) -> tuple[str, int]:
    for _ in range(100):
        name = f".{update_id}.{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=updates_fd)
        except FileExistsError:
            continue
        staging_fd, _ = _open_directory_at(
            updates_fd, name, "staging directory", mode=0o700
        )
        if staging_fd is None:
            raise RuntimeError("update provenance staging directory disappeared")
        return name, staging_fd
    raise FileExistsError("could not allocate a unique update provenance staging directory")


def _write_update_record(directory_fd: int, record: dict[str, Any]) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("update.json", flags, 0o600, dir_fd=directory_fd)
    try:
        payload = (
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode()
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError("short write while publishing update provenance")
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        return os.dup(descriptor)
    finally:
        os.close(descriptor)


def _read_update_record(directory_fd: int) -> tuple[dict[str, Any], int]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open("update.json", flags, dir_fd=directory_fd)
    except OSError as exc:
        try:
            metadata = os.stat(
                "update.json", dir_fd=directory_fd, follow_symlinks=False
            )
        except OSError:
            metadata = None
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(
                "update provenance update.json cannot be a symlink"
            ) from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("update.json is not a regular file")
        chunks = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return json.loads(b"".join(chunks).decode("utf-8")), os.dup(descriptor)
    finally:
        os.close(descriptor)


def _discard_staging_directory(
    updates_fd: int, staging_name: str, staging_fd: int
) -> None:
    try:
        entry = os.stat(staging_name, dir_fd=updates_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(entry.st_mode) or not _same_inode(entry, os.fstat(staging_fd)):
        return
    try:
        os.unlink("update.json", dir_fd=staging_fd)
    except FileNotFoundError:
        pass
    os.rmdir(staging_name, dir_fd=updates_fd)


def _publish_update_record(
    root: Path, instrument: str, identity: dict[str, Any]
) -> tuple[str, Path]:
    root = root.resolve()
    update_id = _sha256(_canonical_json(identity))
    target = root / "updates" / instrument / update_id
    record_path = target / "update.json"
    record = identity | {
        "update_id": update_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    root_fd = _open_absolute_directory(root)
    try:
        store_fd, store_created = _open_directory_at(
            root_fd, "updates", "store root", create=True
        )
        if store_fd is None:
            raise RuntimeError("update provenance store root disappeared")
        try:
            if store_created:
                os.fsync(root_fd)
            updates_fd, updates_created = _open_directory_at(
                store_fd, instrument, "instrument directory", create=True
            )
            if updates_fd is None:
                raise RuntimeError("update provenance instrument directory disappeared")
            try:
                if updates_created:
                    os.fsync(store_fd)
                os.fchmod(updates_fd, 0o755)
                target_fd, _ = _open_directory_at(
                    updates_fd, update_id, "target directory"
                )
                published_inode: os.stat_result | None = None
                if target_fd is None:
                    staging_name, staging_fd = _create_staging_directory(
                        updates_fd, update_id
                    )
                    published = False
                    try:
                        # Diagnostic preflight only; all access remains relative to staging_fd.
                        _validate_update_store_path(
                            root
                            / "updates"
                            / instrument
                            / staging_name
                            / "update.json",
                            root,
                        )
                        record_fd = _write_update_record(staging_fd, record)
                        try:
                            _require_pinned_entry(
                                staging_fd,
                                "update.json",
                                record_fd,
                                "staged record",
                                directory=False,
                            )
                        finally:
                            os.close(record_fd)
                        os.fchmod(staging_fd, 0o755)
                        os.fsync(staging_fd)
                        _require_pinned_entry(
                            updates_fd,
                            staging_name,
                            staging_fd,
                            "staging directory",
                            directory=True,
                        )
                        try:
                            os.rename(
                                staging_name,
                                update_id,
                                src_dir_fd=updates_fd,
                                dst_dir_fd=updates_fd,
                            )
                        except OSError as exc:
                            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                                raise
                        else:
                            published = True
                            published_inode = os.fstat(staging_fd)
                            os.fsync(updates_fd)
                    finally:
                        if not published:
                            _discard_staging_directory(
                                updates_fd, staging_name, staging_fd
                            )
                        os.close(staging_fd)
                    target_fd, _ = _open_directory_at(
                        updates_fd, update_id, "target directory"
                    )
                    if target_fd is None:
                        raise RuntimeError(
                            "update provenance target disappeared during publication"
                        )
                    if published_inode is not None and not _same_inode(
                        published_inode, os.fstat(target_fd)
                    ):
                        raise RuntimeError(
                            "update provenance target changed during publication"
                        )

                try:
                    _require_pinned_entry(
                        updates_fd,
                        update_id,
                        target_fd,
                        "target directory",
                        directory=True,
                    )
                    record_fd: int | None = None
                    try:
                        try:
                            stored, record_fd = _read_update_record(target_fd)
                            if not isinstance(stored, dict):
                                raise ValueError("record must be a JSON object")
                            expected_fields = set(identity) | {
                                "update_id",
                                "created_at",
                            }
                            if set(stored) != expected_fields:
                                raise ValueError("unexpected or missing record fields")
                            stored_identity = {
                                key: value
                                for key, value in stored.items()
                                if key not in {"update_id", "created_at"}
                            }
                            if (
                                stored.get("update_id") != update_id
                                or stored_identity != identity
                            ):
                                raise ValueError("record identity mismatch")
                            datetime.fromisoformat(stored["created_at"])
                        except (KeyError, OSError, TypeError, ValueError) as exc:
                            raise RuntimeError(
                                f"corrupt update provenance {target}: {exc}"
                            ) from exc
                        if record_fd is None:
                            raise RuntimeError(
                                "update provenance record disappeared during verification"
                            )
                        _require_pinned_entry(
                            root_fd,
                            "updates",
                            store_fd,
                            "store root",
                            directory=True,
                        )
                        _require_pinned_entry(
                            store_fd,
                            instrument,
                            updates_fd,
                            "instrument directory",
                            directory=True,
                        )
                        _require_pinned_entry(
                            updates_fd,
                            update_id,
                            target_fd,
                            "target directory",
                            directory=True,
                        )
                        _require_pinned_entry(
                            target_fd,
                            "update.json",
                            record_fd,
                            "record",
                            directory=False,
                        )
                    finally:
                        if record_fd is not None:
                            os.close(record_fd)
                finally:
                    os.close(target_fd)
            finally:
                os.close(updates_fd)
        finally:
            os.close(store_fd)
        verification_root_fd = _open_absolute_directory(root)
        try:
            if not _same_inode(os.fstat(root_fd), os.fstat(verification_root_fd)):
                raise RuntimeError("update provenance configured root changed during use")
        finally:
            os.close(verification_root_fd)
    finally:
        os.close(root_fd)
    return update_id, record_path


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
