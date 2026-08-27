from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .datasets import (
    DatasetValidationError,
    SAFE_INSTRUMENT,
    _atomic_json,
    _canonical_json,
    _fsync_directory,
    _InstrumentLock,
    _normalize_frame,
    _sha256,
    _validate_metadata,
    _verify_snapshot,
    publish_snapshot,
    snapshot_status,
)
from .schemas import SchemaValidationError, canonical_json_bytes


class ConcurrentUpdateError(RuntimeError):
    """Raised when latest changes before a reconciled update can commit."""


UPDATE_ID = re.compile(r"^[0-9a-f]{64}$")
MAX_UPDATE_RECORD_BYTES = 1_048_576
UPDATE_IDENTITY_FIELDS = {
    "schema_version",
    "metadata",
    "request",
    "expected_sessions_sha256",
    "expected_session_count",
    "fetched",
    "prior_snapshot_id",
    "result_snapshot_id",
    "revision_count",
}


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
    columns = [column for column in previous.columns if column != "Date"]
    changed = previous_by_date.loc[overlap, columns].ne(
        fetched_by_date.loc[overlap, columns]
    )
    return int(changed.any(axis=1).sum())


def _validated_provenance_identity(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise DatasetValidationError(f"{label} must be a non-empty object")
    try:
        canonical_json_bytes(value)
    except SchemaValidationError as exc:
        raise DatasetValidationError(f"{label} must contain finite JSON values") from exc
    return value


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


def _immutable_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


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
        os.fchmod(descriptor, 0o444)
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
        if metadata.st_mode & 0o222:
            raise ValueError("update.json is writable")
        if metadata.st_nlink != 1:
            raise ValueError("update.json has an unsafe hard link count")
        if metadata.st_size > MAX_UPDATE_RECORD_BYTES:
            raise ValueError("update.json exceeds the size limit")
        chunks = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise ValueError("update.json changed while reading")
        if _immutable_state(os.fstat(descriptor)) != _immutable_state(metadata):
            raise ValueError("update.json changed while reading")
        return (
            json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_strict_json_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value: {value}")
                ),
            ),
            os.dup(descriptor),
        )
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
    os.fchmod(staging_fd, 0o700)
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
                        os.fchmod(staging_fd, 0o555)
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
                            if set(os.listdir(target_fd)) != {"update.json"}:
                                raise ValueError(
                                    "target directory has unexpected entries"
                                )
                            stored, record_fd = _read_update_record(target_fd)
                            if os.fstat(target_fd).st_mode & 0o222:
                                raise ValueError("target directory is writable")
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


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate update provenance field: {key}")
        value[key] = item
    return value


def _validate_stored_update_record(
    stored: Any, update_id: str
) -> dict[str, Any]:
    if not isinstance(stored, dict):
        raise ValueError("record must be a JSON object")
    identity_fields = set(stored) - {"update_id", "created_at"}
    allowed = (
        UPDATE_IDENTITY_FIELDS,
        UPDATE_IDENTITY_FIELDS | {"source", "expected_sessions_source"},
    )
    if identity_fields not in allowed:
        raise ValueError("unexpected or missing record fields")
    if stored.get("update_id") != update_id:
        raise ValueError("record update ID mismatch")
    identity = {
        key: value
        for key, value in stored.items()
        if key not in {"update_id", "created_at"}
    }
    if _sha256(_canonical_json(identity)) != update_id:
        raise ValueError("record identity hash mismatch")
    datetime.fromisoformat(stored["created_at"])
    if stored.get("schema_version") != 1:
        raise ValueError("record schema version is invalid")
    metadata = _validate_metadata(stored["metadata"])
    if metadata != stored["metadata"]:
        raise ValueError("record metadata is not canonical")
    if "source" in stored:
        if (
            not isinstance(stored["source"], dict)
            or stored["source"].get("provider") != metadata["provider"]
            or stored["source"].get("instrument") != metadata["instrument"]
        ):
            raise ValueError("record source identity does not match metadata")
        if not isinstance(stored["expected_sessions_source"], dict):
            raise ValueError("record expected-session source is invalid")
    if (
        not isinstance(stored.get("expected_sessions_sha256"), str)
        or UPDATE_ID.fullmatch(stored["expected_sessions_sha256"]) is None
    ):
        raise ValueError("record expected-session hash is invalid")
    if (
        not isinstance(stored.get("result_snapshot_id"), str)
        or UPDATE_ID.fullmatch(stored["result_snapshot_id"]) is None
    ):
        raise ValueError("record result snapshot ID is invalid")
    return stored


def load_update_record(
    root: Path | str, instrument: str, update_id: str
) -> dict[str, Any]:
    """Load one sealed content-addressed update record after topology verification."""

    if not SAFE_INSTRUMENT.fullmatch(instrument):
        raise DatasetValidationError(f"unsafe instrument: {instrument!r}")
    if not isinstance(update_id, str) or UPDATE_ID.fullmatch(update_id) is None:
        raise DatasetValidationError("invalid update ID")
    root = Path(root).resolve()
    target = root / "updates" / instrument / update_id
    root_fd = _open_absolute_directory(root)
    store_fd: int | None = None
    updates_fd: int | None = None
    target_fd: int | None = None
    record_fd: int | None = None
    try:
        store_fd, _ = _open_directory_at(root_fd, "updates", "store root")
        if store_fd is None:
            raise FileNotFoundError("update provenance store does not exist")
        updates_fd, _ = _open_directory_at(
            store_fd, instrument, "instrument directory"
        )
        if updates_fd is None:
            raise FileNotFoundError(
                f"update provenance does not exist for {instrument}"
            )
        target_fd, _ = _open_directory_at(
            updates_fd, update_id, "target directory"
        )
        if target_fd is None:
            raise FileNotFoundError(f"update provenance does not exist: {update_id}")
        try:
            target_metadata = os.fstat(target_fd)
            if set(os.listdir(target_fd)) != {"update.json"}:
                raise ValueError("target directory has unexpected entries")
            stored, record_fd = _read_update_record(target_fd)
            if target_metadata.st_mode & 0o222:
                raise ValueError("target directory is writable")
            stored = _validate_stored_update_record(stored, update_id)
            _require_pinned_entry(
                root_fd, "updates", store_fd, "store root", directory=True
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
            if _immutable_state(os.fstat(target_fd)) != _immutable_state(
                target_metadata
            ):
                raise ValueError("target directory changed while reading")
            if stored["metadata"]["instrument"] != instrument:
                raise ValueError(
                    "record instrument does not match provenance directory"
                )
            return stored
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError(f"corrupt update provenance {target}: {exc}") from exc
    finally:
        if record_fd is not None:
            os.close(record_fd)
        if target_fd is not None:
            os.close(target_fd)
        if updates_fd is not None:
            os.close(updates_fd)
        if store_fd is not None:
            os.close(store_fd)
        os.close(root_fd)


def snapshot_update_lineage(
    root: Path | str, instrument: str, snapshot_id: str
) -> dict[str, Any]:
    """Return the immutable first lineage claim for a snapshot."""

    if not SAFE_INSTRUMENT.fullmatch(instrument):
        raise DatasetValidationError(f"unsafe instrument: {instrument!r}")
    if not isinstance(snapshot_id, str) or UPDATE_ID.fullmatch(snapshot_id) is None:
        raise DatasetValidationError("invalid snapshot ID")
    root = Path(root).resolve()
    with _InstrumentLock(root, f"lineage-{instrument}"):
        claimed = _load_snapshot_lineage_claim(root, instrument, snapshot_id)
        if claimed is not None:
            return claimed
        lineage = _candidate_snapshot_lineage(root, instrument, snapshot_id)
        _publish_snapshot_lineage_claim(root, instrument, snapshot_id, lineage)
        claimed = _load_snapshot_lineage_claim(root, instrument, snapshot_id)
        if claimed is None:
            raise RuntimeError("snapshot lineage claim disappeared after publication")
        return claimed


def _candidate_snapshot_lineage(
    root: Path, instrument: str, snapshot_id: str
) -> dict[str, Any]:
    instrument_root = root / "updates" / instrument
    if not instrument_root.exists():
        return {"kind": "legacy_snapshot"}
    if instrument_root.is_symlink() or not instrument_root.is_dir():
        raise RuntimeError(
            f"update provenance instrument directory is unsafe: {instrument_root}"
        )
    candidates: list[dict[str, Any]] = []
    for entry in sorted(instrument_root.iterdir(), key=lambda path: path.name):
        if entry.name.startswith("."):
            continue
        if UPDATE_ID.fullmatch(entry.name) is None:
            raise RuntimeError(f"unexpected update provenance entry: {entry}")
        record = load_update_record(root, instrument, entry.name)
        if (
            record["result_snapshot_id"] == snapshot_id
            and record["prior_snapshot_id"] != snapshot_id
            and "source" in record
        ):
            candidates.append(record)
    if not candidates:
        return {"kind": "legacy_snapshot"}
    record = min(candidates, key=lambda value: value["update_id"])
    return {
        "kind": "verified_update",
        "update_id": record["update_id"],
        "source": record["source"],
        "expected_sessions_source": record["expected_sessions_source"],
        "expected_sessions_sha256": record["expected_sessions_sha256"],
        "expected_session_count": record["expected_session_count"],
    }


def _lineage_claim_identity(
    instrument: str, snapshot_id: str, lineage: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "instrument": instrument,
        "snapshot_id": snapshot_id,
        "lineage": lineage,
    }


def _validate_lineage(lineage: Any) -> dict[str, Any]:
    if lineage == {"kind": "legacy_snapshot"}:
        return lineage
    expected = {
        "kind",
        "update_id",
        "source",
        "expected_sessions_source",
        "expected_sessions_sha256",
        "expected_session_count",
    }
    if not isinstance(lineage, dict) or set(lineage) != expected:
        raise ValueError("snapshot lineage fields are invalid")
    if lineage["kind"] != "verified_update":
        raise ValueError("snapshot lineage kind is invalid")
    if UPDATE_ID.fullmatch(str(lineage["update_id"])) is None:
        raise ValueError("snapshot lineage update ID is invalid")
    canonical_json_bytes(lineage)
    return lineage


def _load_snapshot_lineage_claim(
    root: Path, instrument: str, snapshot_id: str
) -> dict[str, Any] | None:
    target = root / "snapshot-lineage" / instrument / snapshot_id
    if not target.exists():
        return None
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError(f"snapshot lineage claim is unsafe: {target}")
    target_metadata = target.stat()
    if target_metadata.st_mode & 0o222:
        raise RuntimeError(f"snapshot lineage claim directory is writable: {target}")
    if {path.name for path in target.iterdir()} != {"lineage.json"}:
        raise RuntimeError(f"snapshot lineage claim topology is invalid: {target}")
    claim_path = target / "lineage.json"
    descriptor = os.open(
        claim_path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("lineage.json is not a regular file")
        if metadata.st_mode & 0o222:
            raise ValueError("lineage.json is writable")
        if metadata.st_nlink != 1:
            raise ValueError("lineage.json has an unsafe hard link count")
        if metadata.st_size > MAX_UPDATE_RECORD_BYTES:
            raise ValueError("lineage.json exceeds the size limit")
        payload = b""
        while chunk := os.read(descriptor, 64 * 1024):
            payload += chunk
        if len(payload) != metadata.st_size:
            raise ValueError("lineage.json changed while reading")
        if _immutable_state(os.fstat(descriptor)) != _immutable_state(metadata):
            raise ValueError("lineage.json changed while reading")
        claim = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {value}")
            ),
        )
    except (OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"corrupt snapshot lineage claim {target}: {exc}") from exc
    finally:
        os.close(descriptor)
    if not isinstance(claim, dict) or set(claim) != {
        "schema_version",
        "instrument",
        "snapshot_id",
        "lineage",
        "claim_sha256",
    }:
        raise RuntimeError(f"snapshot lineage claim fields are invalid: {target}")
    identity = _lineage_claim_identity(instrument, snapshot_id, claim["lineage"])
    if (
        claim["schema_version"] != 1
        or claim["instrument"] != instrument
        or claim["snapshot_id"] != snapshot_id
        or claim["claim_sha256"] != _sha256(_canonical_json(identity))
    ):
        raise RuntimeError(f"snapshot lineage claim identity is invalid: {target}")
    try:
        lineage = _validate_lineage(claim["lineage"])
    except (SchemaValidationError, TypeError, ValueError) as exc:
        raise RuntimeError(f"snapshot lineage claim is invalid: {target}: {exc}") from exc
    if _immutable_state(target.stat()) != _immutable_state(target_metadata):
        raise RuntimeError(f"snapshot lineage claim changed while reading: {target}")
    return lineage


def _publish_snapshot_lineage_claim(
    root: Path,
    instrument: str,
    snapshot_id: str,
    lineage: dict[str, Any],
) -> None:
    lineage = _validate_lineage(lineage)
    instrument_root = root / "snapshot-lineage" / instrument
    for directory in (root / "snapshot-lineage", instrument_root):
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise RuntimeError(f"snapshot lineage store is unsafe: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
    target = instrument_root / snapshot_id
    if target.exists():
        return
    identity = _lineage_claim_identity(instrument, snapshot_id, lineage)
    claim = identity | {"claim_sha256": _sha256(_canonical_json(identity))}
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=instrument_root)
    )
    try:
        claim_path = temporary / "lineage.json"
        with claim_path.open("xb") as stream:
            stream.write(
                json.dumps(
                    claim,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        claim_path.chmod(0o444)
        temporary.chmod(0o555)
        _fsync_directory(temporary)
        try:
            os.rename(temporary, target)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
        else:
            _fsync_directory(instrument_root)
    finally:
        if temporary.exists():
            temporary.chmod(0o755)
            claim_path = temporary / "lineage.json"
            if claim_path.exists() and not claim_path.is_symlink():
                claim_path.chmod(0o644)
            shutil.rmtree(temporary)


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
    *,
    source_identity: dict[str, Any] | None = None,
    expected_sessions_source: dict[str, Any] | None = None,
) -> dict[str, str | int]:
    """Reconcile requested daily bars into a verified immutable history snapshot."""

    root = Path(root).resolve()
    normalized_metadata = _validate_metadata(metadata)
    if (source_identity is None) != (expected_sessions_source is None):
        raise DatasetValidationError(
            "source and expected-session provenance must be supplied together"
        )
    if source_identity is not None:
        source_identity = _validated_provenance_identity(
            source_identity, "source identity"
        )
        expected_sessions_source = _validated_provenance_identity(
            expected_sessions_source, "expected sessions source"
        )
        if source_identity.get("provider") != normalized_metadata["provider"]:
            raise DatasetValidationError(
                "source identity provider does not match dataset metadata"
            )
        if source_identity.get("instrument") != normalized_metadata["instrument"]:
            raise DatasetValidationError(
                "source identity instrument does not match dataset metadata"
            )
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
            verified = _verify_snapshot(
                prior_path, prior_snapshot_id, include_frame=True
            )
            if not isinstance(verified, tuple):
                raise RuntimeError("snapshot verifier did not return the prior frame")
            prior_manifest, previous = verified
            mismatches = [
                field
                for field, value in normalized_metadata.items()
                if prior_manifest["metadata"].get(field) != value
            ]
            if mismatches:
                raise DatasetValidationError(
                    f"latest snapshot metadata mismatch: {', '.join(sorted(mismatches))}"
                )
            if list(previous.columns) != list(normalized_fetched.columns):
                raise DatasetValidationError(
                    "fetched column schema must match the latest snapshot schema"
                )

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
        if source_identity is not None:
            identity["source"] = source_identity
            identity["expected_sessions_source"] = expected_sessions_source
        update_id, update_path = _publish_update_record(root, instrument, identity)
        snapshot_update_lineage(root, instrument, snapshot["snapshot_id"])
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
