from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .schemas import SchemaValidationError, canonical_json_bytes


METADATA_FIELDS = {"instrument", "provider", "market", "currency", "adjustment"}
SAFE_INSTRUMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,63}$")
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


class DatasetValidationError(ValueError):
    """Raised when data or metadata cannot form a trustworthy snapshot."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_metadata(metadata: dict[str, str]) -> dict[str, str]:
    fields = set(metadata)
    if fields != METADATA_FIELDS:
        missing = sorted(METADATA_FIELDS - fields)
        unknown = sorted(fields - METADATA_FIELDS)
        raise DatasetValidationError(
            f"metadata fields must be exactly {sorted(METADATA_FIELDS)}; "
            f"missing={missing}, unknown={unknown}"
        )
    normalized = {key: str(metadata[key]).strip() for key in sorted(metadata)}
    if any(not value for value in normalized.values()):
        raise DatasetValidationError("metadata fields cannot be empty")
    instrument = normalized["instrument"]
    if not SAFE_INSTRUMENT.fullmatch(instrument):
        raise DatasetValidationError(f"unsafe instrument: {instrument!r}")
    return normalized


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_lock_directory(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise DatasetValidationError(
            f"latest lock directory is unsafe or symlinked: {name}"
        ) from exc
    try:
        if created:
            os.fchmod(descriptor, 0o700)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
        ):
            raise DatasetValidationError(
                f"latest lock directory has unsafe ownership or permissions: {name}"
            )
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _directory_identity(current) != _directory_identity(metadata):
            raise DatasetValidationError(
                f"latest lock directory changed while opening: {name}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_lock_file(parent_fd: int, name: str) -> int:
    flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise DatasetValidationError(
                f"latest lock file cannot be created safely: {name}"
            ) from exc
        existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if existing.st_nlink != 1:
            raise DatasetValidationError(
                f"latest lock file has an unsafe hard link count: {name}"
            )
        if (
            not stat.S_ISREG(existing.st_mode)
            or stat.S_IMODE(existing.st_mode) != 0o600
            or existing.st_uid != os.geteuid()
        ):
            raise DatasetValidationError(
                f"latest lock file has unsafe type, ownership, or permissions: {name}"
            )
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as open_exc:
            raise DatasetValidationError(
                f"latest lock file is unsafe or symlinked: {name}"
            ) from open_exc
    else:
        os.fchmod(descriptor, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_nlink != 1:
            raise DatasetValidationError(
                f"latest lock file has an unsafe hard link count: {name}"
            )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise DatasetValidationError(
                f"latest lock file has unsafe type, ownership, or permissions: {name}"
            )
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _file_identity(current) != _file_identity(metadata):
            raise DatasetValidationError(
                f"latest lock file changed while opening: {name}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _InstrumentLock:
    def __init__(self, root: Path, instrument: str):
        self.root = Path(root).absolute()
        self.path = self.root / ".locks" / "latest" / f"{instrument}.lock"
        self._root_fd: int | None = None
        self._locks_fd: int | None = None
        self._latest_fd: int | None = None
        self._lock_fd: int | None = None

    def __enter__(self) -> None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._root_fd = os.open(self.root, flags)
            self._locks_fd = _open_lock_directory(self._root_fd, ".locks")
            self._latest_fd = _open_lock_directory(self._locks_fd, "latest")
            self._lock_fd = _open_lock_file(
                self._latest_fd, f"{self.path.stem}.lock"
            )
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            self._validate_path_bindings()
        except BaseException:
            self._close()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        assert self._lock_fd is not None
        try:
            self._validate_path_bindings()
        finally:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                self._close()

    def _validate_path_bindings(self) -> None:
        assert self._root_fd is not None
        assert self._locks_fd is not None
        assert self._latest_fd is not None
        assert self._lock_fd is not None
        if _directory_identity(
            os.stat(self.root, follow_symlinks=False)
        ) != _directory_identity(os.fstat(self._root_fd)):
            raise DatasetValidationError("latest lock state-root binding changed")
        directories = (
            (self._root_fd, ".locks", self._locks_fd),
            (self._locks_fd, "latest", self._latest_fd),
        )
        for parent_fd, name, descriptor in directories:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != os.geteuid()
            ):
                raise DatasetValidationError(
                    f"latest lock directory became unsafe: {name}"
                )
            if _directory_identity(current) != _directory_identity(opened):
                raise DatasetValidationError(
                    f"latest lock directory binding changed: {name}"
                )
        current_lock = os.stat(
            f"{self.path.stem}.lock",
            dir_fd=self._latest_fd,
            follow_symlinks=False,
        )
        opened_lock = os.fstat(self._lock_fd)
        if (
            not stat.S_ISREG(opened_lock.st_mode)
            or opened_lock.st_nlink != 1
            or stat.S_IMODE(opened_lock.st_mode) != 0o600
            or opened_lock.st_uid != os.geteuid()
        ):
            raise DatasetValidationError("latest lock file became unsafe")
        if _file_identity(current_lock) != _file_identity(opened_lock):
            raise DatasetValidationError("latest lock file binding changed")

    def _close(self) -> None:
        for attribute in ("_lock_fd", "_latest_fd", "_locks_fd", "_root_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)


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


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate update provenance field: {key}")
        value[key] = item
    return value


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


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
