from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Volume")
OPTIONAL_COLUMNS = ("AdjustedClose",)
LEGACY_COLUMN_ALIASES = {"Adj Close": "AdjustedClose"}
METADATA_FIELDS = {"instrument", "provider", "market", "currency", "adjustment"}
SAFE_INSTRUMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,63}$")


class DatasetValidationError(ValueError):
    """Raised when data or metadata cannot form a trustworthy snapshot."""


class _InstrumentLock:
    def __init__(self, root: Path, instrument: str):
        lock_root = root / ".locks" / "latest"
        lock_root.mkdir(parents=True, exist_ok=True)
        self.path = lock_root / f"{instrument}.lock"
        self.stream = None

    def __enter__(self) -> None:
        self.stream = self.path.open("a+b")
        self.path.chmod(0o600)
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        assert self.stream is not None
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


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


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if "AdjustedClose" in frame.columns and "Adj Close" in frame.columns:
        raise DatasetValidationError(
            "market data cannot contain both AdjustedClose and Adj Close"
        )
    frame = frame.rename(columns=LEGACY_COLUMN_ALIASES)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise DatasetValidationError(f"missing required columns: {missing}")

    columns = [*REQUIRED_COLUMNS]
    columns.extend(column for column in OPTIONAL_COLUMNS if column in frame.columns)
    clean = frame.loc[:, columns].copy()
    if clean.empty:
        raise DatasetValidationError("daily market data must contain at least one row")
    dates = []
    try:
        for value in clean["Date"]:
            timestamp = pd.Timestamp(value)
            if pd.isna(timestamp):
                raise ValueError("missing date")
            if timestamp.tz is not None:
                raise DatasetValidationError("daily Date values must be timezone-naive")
            if timestamp != timestamp.normalize():
                raise DatasetValidationError("daily Date values must be midnight session labels")
            dates.append(timestamp)
    except (TypeError, ValueError) as exc:
        raise DatasetValidationError(f"invalid Date values: {exc}") from exc
    clean["Date"] = pd.DatetimeIndex(dates)
    if clean["Date"].duplicated().any():
        raise DatasetValidationError("duplicate dates are not allowed")

    numeric_columns = columns[1:]
    boolean_columns = [
        column for column in numeric_columns if pd.api.types.is_bool_dtype(clean[column])
    ]
    if boolean_columns:
        raise DatasetValidationError(
            f"boolean market data are not allowed: {boolean_columns}"
        )
    try:
        clean[numeric_columns] = clean[numeric_columns].apply(pd.to_numeric, errors="raise")
    except (TypeError, ValueError) as exc:
        raise DatasetValidationError(f"non-numeric market data: {exc}") from exc
    values = clean[numeric_columns].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise DatasetValidationError("non-finite market data are not allowed")
    if (clean[["Open", "High", "Low", "Close"]] <= 0).any().any():
        raise DatasetValidationError("OHLC values must be strictly positive")
    if (clean["Volume"] < 0).any():
        raise DatasetValidationError("Volume must be non-negative")
    if "AdjustedClose" in clean and (clean["AdjustedClose"] <= 0).any():
        raise DatasetValidationError("AdjustedClose values must be strictly positive")
    if (clean["High"] < clean[["Open", "Low", "Close"]].max(axis=1)).any():
        raise DatasetValidationError("High must be at least Open, Low, and Close")
    if (clean["Low"] > clean[["Open", "High", "Close"]].min(axis=1)).any():
        raise DatasetValidationError("Low must be at most Open, High, and Close")
    clean[numeric_columns] = clean[numeric_columns].astype("float64")
    clean.loc[:, "Volume"] = clean["Volume"].mask(clean["Volume"] == 0.0, 0.0)
    return clean.sort_values("Date").reset_index(drop=True)


def _canonical_data_bytes(frame: pd.DataFrame) -> bytes:
    dates = frame["Date"].astype("int64").to_numpy(dtype=">i8")
    numeric_columns = list(frame.columns[1:])
    numeric = frame[numeric_columns].to_numpy(dtype=">f8")
    if tuple(frame.columns) == REQUIRED_COLUMNS:
        prefix = b"quant-platform-ohlcv-v1\0"
    else:
        column_schema = _canonical_json(list(frame.columns))
        prefix = (
            b"quant-platform-daily-v2\0"
            + struct.pack(">Q", len(column_schema))
            + column_schema
        )
    return (
        prefix
        + struct.pack(">Q", len(frame))
        + dates.tobytes(order="C")
        + numeric.tobytes(order="C")
    )


def _seal_snapshot(directory: Path) -> None:
    for name in ("manifest.json", "data.parquet"):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise DatasetValidationError(f"snapshot file is unsafe: {name}")
        path.chmod(0o444)
    directory.chmod(0o555)


def _make_snapshot_removable(directory: Path) -> None:
    if not directory.exists() or directory.is_symlink():
        return
    directory.chmod(0o755)
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink():
            path.chmod(0o644)


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


def _read_snapshot_file(path: Path, label: str) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"snapshot {label} is not a regular file")
    if before.st_mode & 0o222:
        raise ValueError(f"snapshot {label} is writable")
    if before.st_nlink != 1:
        raise ValueError(f"snapshot {label} has an unsafe hard link count")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise ValueError(f"snapshot {label} changed while opening")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(opened):
            raise ValueError(f"snapshot {label} changed while reading")
        payload = b"".join(chunks)
        if len(payload) != after.st_size:
            raise ValueError(f"snapshot {label} read was incomplete")
    finally:
        os.close(descriptor)
    current = os.stat(path, follow_symlinks=False)
    if _file_identity(current) != _file_identity(after):
        raise ValueError(f"snapshot {label} path changed while reading")
    return payload


def _load_manifest(payload: bytes) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate manifest key: {key}")
            value[key] = item
        return value

    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
    )
    if not isinstance(value, dict):
        raise ValueError("snapshot manifest must be a JSON object")
    return value


def _verify_snapshot(
    target: Path,
    expected_snapshot_id: str,
    *,
    include_frame: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], pd.DataFrame]:
    try:
        target = Path(target)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot_id):
            raise ValueError("expected snapshot ID is invalid")
        for path, label in (
            (target.parent.parent, "dataset store"),
            (target.parent, "instrument directory"),
            (target, "snapshot directory"),
        ):
            metadata = os.stat(path, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"unsafe snapshot topology: {label} is not a directory")
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"unsafe snapshot topology: {label} is a symlink")
        if not SAFE_INSTRUMENT.fullmatch(target.parent.name):
            raise ValueError("unsafe snapshot instrument directory")
        target_metadata = os.stat(target, follow_symlinks=False)
        if target_metadata.st_mode & 0o222:
            raise ValueError("snapshot directory is writable")
        entries = {entry.name for entry in os.scandir(target)}
        if entries != {"manifest.json", "data.parquet"}:
            raise ValueError(
                "snapshot file set is invalid: "
                f"expected=['data.parquet', 'manifest.json'], actual={sorted(entries)}"
            )
        manifest = _load_manifest(
            _read_snapshot_file(target / "manifest.json", "manifest")
        )
        parquet_path = target / "data.parquet"
        parquet_payload = _read_snapshot_file(parquet_path, "Parquet data")
        common_manifest_fields = {
            "schema_version",
            "metadata",
            "canonical_sha256",
            "snapshot_id",
            "created_at",
            "rows",
            "data_start",
            "data_end",
            "parquet_sha256",
            "files",
        }
        schema_version = manifest.get("schema_version")
        expected_manifest_fields = common_manifest_fields | (
            {"columns"} if schema_version == 2 else set()
        )
        if set(manifest) != expected_manifest_fields:
            raise ValueError("unexpected or missing manifest fields")
        if manifest.get("snapshot_id") != expected_snapshot_id or target.name != expected_snapshot_id:
            raise ValueError("snapshot identity mismatch")
        if type(schema_version) is not int or schema_version not in {1, 2}:
            raise ValueError("unsupported snapshot schema")
        metadata = manifest["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("snapshot metadata must be an object")
        if metadata != _validate_metadata(metadata):
            raise ValueError("snapshot metadata is not canonical")
        if metadata["instrument"] != target.parent.name:
            raise ValueError("instrument directory does not match snapshot metadata")
        if not isinstance(manifest["files"], dict):
            raise ValueError("snapshot file map must be an object")
        if manifest["files"] != {"data": "data.parquet"}:
            raise ValueError("snapshot file map is invalid")
        if not isinstance(manifest["created_at"], str):
            raise ValueError("created_at must be a string")
        datetime.fromisoformat(manifest["created_at"])
        for field in ("canonical_sha256", "parquet_sha256"):
            if not isinstance(manifest[field], str) or not re.fullmatch(
                r"[0-9a-f]{64}", manifest[field]
            ):
                raise ValueError(f"{field} must be a SHA-256 value")
        if type(manifest["rows"]) is not int or manifest["rows"] < 1:
            raise ValueError("rows must be a positive integer")
        if not isinstance(manifest["data_start"], str) or not isinstance(
            manifest["data_end"], str
        ):
            raise ValueError("data range fields must be strings")
        identity = {
            "schema_version": manifest["schema_version"],
            "metadata": manifest["metadata"],
            "canonical_sha256": manifest["canonical_sha256"],
        }
        if _sha256(_canonical_json(identity)) != expected_snapshot_id:
            raise ValueError("snapshot ID does not match its identity inputs")
        if _sha256(parquet_payload) != manifest["parquet_sha256"]:
            raise ValueError("Parquet checksum mismatch")
        normalized = _normalize_frame(pd.read_parquet(io.BytesIO(parquet_payload)))
        if schema_version == 1 and tuple(normalized.columns) != REQUIRED_COLUMNS:
            raise ValueError("v1 snapshot must contain exactly the legacy OHLCV schema")
        if schema_version == 2:
            if not isinstance(manifest["columns"], list) or not all(
                isinstance(column, str) for column in manifest["columns"]
            ):
                raise ValueError("snapshot columns must be a string array")
            if manifest["columns"] != list(normalized.columns):
                raise ValueError("snapshot column schema mismatch")
        if _sha256(_canonical_data_bytes(normalized)) != manifest["canonical_sha256"]:
            raise ValueError("canonical data checksum mismatch")
        if manifest["rows"] != len(normalized):
            raise ValueError("row count mismatch")
        if manifest["data_start"] != str(normalized["Date"].min().date()):
            raise ValueError("data_start mismatch")
        if manifest["data_end"] != str(normalized["Date"].max().date()):
            raise ValueError("data_end mismatch")
        if include_frame:
            return manifest, normalized
        return manifest
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"corrupt snapshot {target}: {exc}") from exc


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_snapshot(
    frame: pd.DataFrame,
    root: Path | str,
    metadata: dict[str, str],
    *,
    update_latest: bool = True,
) -> dict[str, str]:
    """Validate and atomically publish one immutable market-data snapshot."""

    normalized_metadata = _validate_metadata(metadata)
    normalized = _normalize_frame(frame)
    canonical_bytes = _canonical_data_bytes(normalized)
    canonical_sha256 = _sha256(canonical_bytes)
    schema_version = 1 if tuple(normalized.columns) == REQUIRED_COLUMNS else 2
    identity = {
        "schema_version": schema_version,
        "metadata": normalized_metadata,
        "canonical_sha256": canonical_sha256,
    }
    snapshot_id = _sha256(_canonical_json(identity))

    root = Path(root).resolve()
    instrument_root = root / "datasets" / normalized_metadata["instrument"]
    instrument_root.mkdir(parents=True, exist_ok=True)
    instrument_root.chmod(0o755)
    target = instrument_root / snapshot_id
    status = "NO_CHANGE" if target.exists() else "CREATED"

    if target.exists():
        _verify_snapshot(target, snapshot_id)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=instrument_root))
        try:
            parquet_path = temporary / "data.parquet"
            normalized.to_parquet(parquet_path, index=False)
            with parquet_path.open("rb") as stream:
                os.fsync(stream.fileno())
            parquet_sha256 = _sha256(parquet_path.read_bytes())
            manifest = identity | {
                "snapshot_id": snapshot_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "rows": int(len(normalized)),
                "data_start": str(normalized["Date"].min().date()),
                "data_end": str(normalized["Date"].max().date()),
                "parquet_sha256": parquet_sha256,
                "files": {"data": "data.parquet"},
            }
            if schema_version == 2:
                manifest["columns"] = list(normalized.columns)
            with (temporary / "manifest.json").open("x", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            _seal_snapshot(temporary)
            _fsync_directory(temporary)
            try:
                os.rename(temporary, target)
            except OSError:
                if not target.exists():
                    raise
                status = "NO_CHANGE"
            else:
                _fsync_directory(instrument_root)
        finally:
            if temporary.exists():
                _make_snapshot_removable(temporary)
                shutil.rmtree(temporary)
        _verify_snapshot(target, snapshot_id)

    if update_latest:
        latest = {"snapshot_id": snapshot_id, "path": snapshot_id}
        with _InstrumentLock(root, normalized_metadata["instrument"]):
            _atomic_json(instrument_root / "latest.json", latest)
    return {"status": status, "snapshot_id": snapshot_id, "path": str(target)}


def snapshot_status(root: Path | str, instrument: str) -> dict[str, str]:
    """Return the latest immutable snapshot pointer for an instrument."""

    if not SAFE_INSTRUMENT.fullmatch(instrument):
        raise DatasetValidationError(f"unsafe instrument: {instrument!r}")
    pointer = Path(root).resolve() / "datasets" / instrument / "latest.json"
    if not pointer.is_file():
        raise FileNotFoundError(f"no snapshot exists for {instrument}")
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
        snapshot_id = str(value["snapshot_id"])
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
            raise ValueError("invalid snapshot ID")
        target = pointer.parent / snapshot_id
        if value["path"] != snapshot_id:
            raise ValueError("path does not match the content-addressed target")
        _verify_snapshot(target, snapshot_id)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"latest snapshot pointer is invalid for {instrument}: {exc}") from exc
    return {"snapshot_id": snapshot_id, "path": str(target)}
