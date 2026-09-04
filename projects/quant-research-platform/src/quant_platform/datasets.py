from __future__ import annotations

import ctypes
import errno
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
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .corporate_actions import (
    CorporateActionEvidence,
    admit_corporate_action_evidence,
    load_strict_json,
    project_corporate_action_evidence,
)
from .dataset_lineage import (
    SAFE_INSTRUMENT,
    DatasetValidationError,
    _canonical_json,
    _directory_identity,
    _file_identity,
    _InstrumentLock,
    _sha256,
    _validate_metadata,
    snapshot_update_lineage,
)
from .study_contracts import normalize_fold_window as _validated_fold_window


REQUIRED_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Volume")
OPTIONAL_COLUMNS = ("AdjustedClose",)
LEGACY_COLUMN_ALIASES = {"Adj Close": "AdjustedClose"}
PROJECTION_IDENTITY = {
    "name": "daily_market_data_prefix",
    "version": "1.0.0",
    "date_column": "Date",
    "boundary": "inclusive",
    "serialization": "parquet",
}
PARENT_VERIFICATION_PROTOCOL = "recursive_snapshot_verification"


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


def _scoring_mask(
    frame: pd.DataFrame, fold_window: Mapping[str, Any]
) -> dict[str, Any]:
    dates = frame["Date"].dt.strftime("%Y-%m-%d").tolist()
    return {
        "schema_version": 1,
        "date_column": "Date",
        "rows": [
            {
                "date": date,
                "scored": (
                    fold_window["scoring_start"]
                    <= date
                    <= fold_window["scoring_end"]
                ),
            }
            for date in dates
        ],
    }


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


def _write_new(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _seal_snapshot(
    directory: Path,
    expected_files: frozenset[str] = frozenset(
        {"manifest.json", "data.parquet"}
    ),
) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory_before = os.stat(directory, follow_symlinks=False)
    if not stat.S_ISDIR(directory_before.st_mode):
        raise DatasetValidationError("snapshot staging path is unsafe")
    directory_fd = os.open(directory, flags)
    try:
        directory_opened = os.fstat(directory_fd)
        if (
            directory_before.st_dev,
            directory_before.st_ino,
        ) != (
            directory_opened.st_dev,
            directory_opened.st_ino,
        ):
            raise DatasetValidationError(
                "snapshot staging directory changed while opening"
            )
        if set(os.listdir(directory_fd)) != expected_files:
            raise DatasetValidationError("snapshot file set is incomplete")
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        for name in sorted(expected_files):
            descriptor = os.open(name, file_flags, dir_fd=directory_fd)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise DatasetValidationError(
                        f"snapshot file is unsafe: {name}"
                    )
                if metadata.st_nlink != 1:
                    raise DatasetValidationError(
                        f"snapshot file has an unsafe hard link count: {name}"
                    )
                os.fchmod(descriptor, 0o444)
                os.fsync(descriptor)
                sealed = os.fstat(descriptor)
                current = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (
                    _file_identity(current) != _file_identity(sealed)
                    or stat.S_IMODE(sealed.st_mode) != 0o444
                    or sealed.st_nlink != 1
                ):
                    raise DatasetValidationError(
                        f"snapshot file changed while sealing: {name}"
                    )
            finally:
                os.close(descriptor)
        os.fchmod(directory_fd, 0o555)
        os.fsync(directory_fd)
        directory_sealed = os.fstat(directory_fd)
        directory_current = os.stat(directory, follow_symlinks=False)
        if (
            _directory_identity(directory_current)
            != _directory_identity(directory_sealed)
            or stat.S_IMODE(directory_sealed.st_mode) != 0o555
        ):
            raise DatasetValidationError(
                "snapshot staging directory changed while sealing"
            )
    finally:
        os.close(directory_fd)
    _verify_snapshot_seal(directory, expected_files)


def _verify_snapshot_seal(
    directory: Path, expected_files: frozenset[str]
) -> None:
    directory_metadata = os.stat(directory, follow_symlinks=False)
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_IMODE(directory_metadata.st_mode) != 0o555
    ):
        raise DatasetValidationError("snapshot directory seal is unsafe")
    entries = {
        entry.name: entry.stat(follow_symlinks=False)
        for entry in os.scandir(directory)
    }
    if set(entries) != expected_files:
        raise DatasetValidationError("snapshot file set is incomplete")
    for name, metadata in entries.items():
        if not stat.S_ISREG(metadata.st_mode):
            raise DatasetValidationError(f"snapshot file is unsafe: {name}")
        if metadata.st_nlink != 1:
            raise DatasetValidationError(
                f"snapshot file has an unsafe hard link count: {name}"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o444:
            raise DatasetValidationError(
                f"snapshot file has unsafe permissions: {name}"
            )


def _make_snapshot_removable(directory: Path) -> None:
    if not directory.exists() or directory.is_symlink():
        return
    directory.chmod(0o755)
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink():
            path.chmod(0o644)


def _read_snapshot_file(path: Path, label: str) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"snapshot {label} is not a regular file")
    if before.st_mode & 0o222:
        raise ValueError(f"snapshot {label} is writable")
    if stat.S_IMODE(before.st_mode) != 0o444:
        raise ValueError(f"snapshot {label} has unsafe permissions")
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


def _snapshot_manifest_fields(schema_version: int) -> frozenset[str]:
    common = {
        "schema_version",
        "metadata",
        "canonical_sha256",
        "snapshot_id",
        "rows",
        "data_start",
        "data_end",
        "parquet_sha256",
        "files",
    }
    if schema_version == 1:
        return frozenset(common | {"created_at"})
    if schema_version == 2:
        return frozenset(common | {"created_at", "columns"})
    if schema_version == 3:
        return frozenset(common | {"columns", "lineage", "scoring_mask_sha256"})
    if schema_version == 4:
        return frozenset(
            common
            | {"created_at", "columns", "corporate_action_evidence_sha256"}
        )
    if schema_version == 5:
        return frozenset(
            common
            | {
                "columns",
                "lineage",
                "scoring_mask_sha256",
                "corporate_action_evidence_sha256",
            }
        )
    raise ValueError("unsupported snapshot schema")


def _action_snapshot_files(manifest: Mapping[str, Any]) -> frozenset[str]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("snapshot file map must be an object")
    artifacts = files.get("corporate_action_artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("corporate-action artifact file map is invalid")
    if not all(
        isinstance(artifact_id, str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact_id)
        and isinstance(path, str)
        and path == f"corporate-action-{artifact_id}.bin"
        for artifact_id, path in artifacts.items()
    ):
        raise ValueError("corporate-action artifact file map is invalid")
    expected_map: dict[str, Any] = {
        "data": "data.parquet",
        "corporate_actions": "corporate_actions.json",
        "corporate_action_artifacts": artifacts,
    }
    if manifest.get("schema_version") == 5:
        expected_map["scoring_mask"] = "scoring_mask.json"
    if files != expected_map:
        raise ValueError("snapshot file map is invalid")
    names = {"manifest.json"}
    names.update(value for value in files.values() if isinstance(value, str))
    names.update(artifacts.values())
    return frozenset(names)


def _verified_action_evidence(
    target: Path, manifest: Mapping[str, Any]
) -> CorporateActionEvidence:
    payload = _read_snapshot_file(
        target / "corporate_actions.json", "corporate-action evidence"
    )
    document = load_strict_json(payload)
    if payload != _canonical_json(document) + b"\n":
        raise ValueError("corporate-action evidence JSON is not canonical")
    artifact_bytes = {
        artifact_id: _read_snapshot_file(
            target / path, f"corporate-action artifact {artifact_id}"
        )
        for artifact_id, path in manifest["files"]["corporate_action_artifacts"].items()
    }
    evidence = admit_corporate_action_evidence(document, artifact_bytes)
    if not evidence.publishable:
        raise ValueError("corporate-action evidence is not publishable")
    if evidence.digest != manifest["corporate_action_evidence_sha256"]:
        raise ValueError("corporate-action evidence digest mismatch")
    return evidence


def _parent_verification_attestation(
    parent_identity: dict[str, Any],
    parent_manifest: dict[str, Any],
) -> dict[str, Any]:
    verified_manifest = json.loads(_canonical_json(parent_manifest))
    return {
        "schema_version": 1,
        "protocol": PARENT_VERIFICATION_PROTOCOL,
        "parent_identity_sha256": _sha256(_canonical_json(parent_identity)),
        "parent_manifest_sha256": _sha256(_canonical_json(verified_manifest)),
        "verified_manifest": verified_manifest,
    }


def _validate_parent_verification_attestation(
    value: Any,
    parent_identity: dict[str, Any],
    metadata: dict[str, str],
) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "protocol",
        "parent_identity_sha256",
        "parent_manifest_sha256",
        "verified_manifest",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("derived-view parent verification attestation is invalid")
    if value["schema_version"] != 1 or value["protocol"] != PARENT_VERIFICATION_PROTOCOL:
        raise ValueError("derived-view parent verification protocol is invalid")
    for field in ("parent_identity_sha256", "parent_manifest_sha256"):
        if not isinstance(value[field], str) or not re.fullmatch(
            r"[0-9a-f]{64}", value[field]
        ):
            raise ValueError(f"derived-view parent verification {field} is invalid")
    if value["parent_identity_sha256"] != _sha256(
        _canonical_json(parent_identity)
    ):
        raise ValueError("derived-view parent verification identity digest is invalid")
    verified_manifest = value["verified_manifest"]
    if not isinstance(verified_manifest, dict):
        raise ValueError("derived-view verified parent manifest is invalid")
    if value["parent_manifest_sha256"] != _sha256(
        _canonical_json(verified_manifest)
    ):
        raise ValueError("derived-view parent manifest attestation digest is invalid")
    schema_version = verified_manifest.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2, 3, 4, 5}:
        raise ValueError("derived-view verified parent schema is invalid")
    if set(verified_manifest) != _snapshot_manifest_fields(schema_version):
        raise ValueError("derived-view verified parent manifest fields are invalid")
    if (
        verified_manifest.get("snapshot_id") != parent_identity["snapshot_id"]
        or verified_manifest.get("canonical_sha256")
        != parent_identity["canonical_sha256"]
        or verified_manifest.get("metadata") != metadata
    ):
        raise ValueError("derived-view verified parent identity does not match")
    manifest_identity = {
        "schema_version": schema_version,
        "metadata": verified_manifest["metadata"],
        "canonical_sha256": verified_manifest["canonical_sha256"],
    }
    if schema_version in {3, 5}:
        if verified_manifest.get("lineage") != parent_identity["lineage"]:
            raise ValueError("derived-view verified parent lineage does not match")
        manifest_identity["lineage"] = verified_manifest["lineage"]
    if schema_version in {4, 5}:
        if (
            verified_manifest.get("corporate_action_evidence_sha256")
            != parent_identity.get("corporate_action_evidence_sha256")
        ):
            raise ValueError("derived-view verified parent action evidence does not match")
        manifest_identity["corporate_action_evidence_sha256"] = verified_manifest[
            "corporate_action_evidence_sha256"
        ]
    if _sha256(_canonical_json(manifest_identity)) != parent_identity["snapshot_id"]:
        raise ValueError("derived-view verified parent snapshot ID is invalid")
    return verified_manifest


def _verified_scoring_bounds(
    target: Path,
    manifest: dict[str, Any],
    frame: pd.DataFrame,
) -> tuple[str, str]:
    if manifest.get("schema_version") not in {3, 5}:
        raise ValueError("snapshot does not have a committed scoring mask")
    lineage = manifest["lineage"]
    dates = frame["Date"].dt.strftime("%Y-%m-%d").tolist()
    view_spec = _validated_fold_window(lineage["view_spec"], dates)
    mask_payload = _read_snapshot_file(
        target / "scoring_mask.json",
        "scoring mask",
    )
    if _sha256(mask_payload) != manifest["scoring_mask_sha256"]:
        raise ValueError("scoring mask checksum mismatch")
    mask = _load_manifest(mask_payload)
    expected_mask = _scoring_mask(frame, view_spec)
    if mask != expected_mask:
        raise ValueError("derived-view scoring mask does not match projected rows")
    scored_dates = [
        row["date"]
        for row in mask["rows"]
        if row["scored"]
    ]
    if not scored_dates:
        raise ValueError("derived-view scoring mask has no scored sessions")
    expected_scoring = {
        "path": "scoring_mask.json",
        "sha256": manifest["scoring_mask_sha256"],
        "rows": len(frame),
        "scored_rows": len(scored_dates),
    }
    if lineage["scoring_mask"] != expected_scoring:
        raise ValueError("derived-view scoring mask identity is invalid")
    if (
        scored_dates[0] != view_spec["scoring_start"]
        or scored_dates[-1] != view_spec["scoring_end"]
    ):
        raise ValueError("derived-view scoring mask bounds are invalid")
    return scored_dates[0], scored_dates[-1]


def _verify_snapshot(
    target: Path,
    expected_snapshot_id: str,
    *,
    include_frame: bool = False,
    require_name: bool = True,
    verify_parent: bool = True,
    _ancestors: frozenset[str] = frozenset(),
) -> dict[str, Any] | tuple[dict[str, Any], pd.DataFrame]:
    try:
        target = Path(target)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot_id):
            raise ValueError("expected snapshot ID is invalid")
        if expected_snapshot_id in _ancestors:
            raise ValueError("derived-view parent lineage contains a cycle")
        ancestors = _ancestors | {expected_snapshot_id}
        topology = (
            (target.parent.parent, "dataset store"),
            (target.parent, "instrument directory"),
            (target, "snapshot directory"),
        )
        topology_identities: list[tuple[Path, tuple[int, ...]]] = []
        for path, label in topology:
            metadata = os.stat(path, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"unsafe snapshot topology: {label} is not a directory")
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"unsafe snapshot topology: {label} is a symlink")
            topology_identities.append((path, _directory_identity(metadata)))
        if not SAFE_INSTRUMENT.fullmatch(target.parent.name):
            raise ValueError("unsafe snapshot instrument directory")
        target_metadata = os.stat(target, follow_symlinks=False)
        if target_metadata.st_mode & 0o222:
            raise ValueError("snapshot directory is writable")
        if stat.S_IMODE(target_metadata.st_mode) != 0o555:
            raise ValueError("snapshot directory has unsafe permissions")
        entries = {entry.name for entry in os.scandir(target)}
        if not {"manifest.json", "data.parquet"}.issubset(entries):
            raise ValueError(f"snapshot file set is invalid: actual={sorted(entries)}")
        manifest = _load_manifest(
            _read_snapshot_file(target / "manifest.json", "manifest")
        )
        schema_version = manifest.get("schema_version")
        if type(schema_version) is not int or schema_version not in {1, 2, 3, 4, 5}:
            raise ValueError("unsupported snapshot schema")
        if schema_version in {4, 5}:
            expected_files = _action_snapshot_files(manifest)
        elif schema_version == 3:
            expected_files = {"manifest.json", "data.parquet", "scoring_mask.json"}
        else:
            expected_files = {"manifest.json", "data.parquet"}
        if entries != expected_files:
            raise ValueError("snapshot file set does not match its schema")
        parquet_path = target / "data.parquet"
        parquet_payload = _read_snapshot_file(parquet_path, "Parquet data")
        if set(manifest) != _snapshot_manifest_fields(schema_version):
            raise ValueError("unexpected or missing manifest fields")
        if (
            manifest.get("snapshot_id") != expected_snapshot_id
            or (require_name and target.name != expected_snapshot_id)
        ):
            raise ValueError("snapshot identity mismatch")
        metadata = manifest["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("snapshot metadata must be an object")
        if metadata != _validate_metadata(metadata):
            raise ValueError("snapshot metadata is not canonical")
        if metadata["instrument"] != target.parent.name:
            raise ValueError("instrument directory does not match snapshot metadata")
        if not isinstance(manifest["files"], dict):
            raise ValueError("snapshot file map must be an object")
        if schema_version not in {4, 5}:
            expected_file_map = (
                {"data": "data.parquet", "scoring_mask": "scoring_mask.json"}
                if schema_version == 3
                else {"data": "data.parquet"}
            )
            if manifest["files"] != expected_file_map:
                raise ValueError("snapshot file map is invalid")
        if schema_version in {1, 2, 4}:
            if not isinstance(manifest["created_at"], str):
                raise ValueError("created_at must be a string")
            datetime.fromisoformat(manifest["created_at"])
        digest_fields = ["canonical_sha256", "parquet_sha256"]
        if schema_version in {3, 5}:
            digest_fields.append("scoring_mask_sha256")
        if schema_version in {4, 5}:
            digest_fields.append("corporate_action_evidence_sha256")
        for field in digest_fields:
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
        if schema_version in {3, 5}:
            identity["lineage"] = manifest["lineage"]
        action_evidence: CorporateActionEvidence | None = None
        if schema_version in {4, 5}:
            identity["corporate_action_evidence_sha256"] = manifest[
                "corporate_action_evidence_sha256"
            ]
            action_evidence = _verified_action_evidence(target, manifest)
        if _sha256(_canonical_json(identity)) != expected_snapshot_id:
            raise ValueError("snapshot ID does not match its identity inputs")
        if _sha256(parquet_payload) != manifest["parquet_sha256"]:
            raise ValueError("Parquet checksum mismatch")
        normalized = _normalize_frame(pd.read_parquet(io.BytesIO(parquet_payload)))
        if schema_version == 1 and tuple(normalized.columns) != REQUIRED_COLUMNS:
            raise ValueError("v1 snapshot must contain exactly the legacy OHLCV schema")
        if schema_version in {2, 3, 4, 5}:
            if not isinstance(manifest["columns"], list) or not all(
                isinstance(column, str) for column in manifest["columns"]
            ):
                raise ValueError("snapshot columns must be a string array")
            if manifest["columns"] != list(normalized.columns):
                raise ValueError("snapshot column schema mismatch")
        if schema_version in {3, 5}:
            lineage = manifest["lineage"]
            expected_lineage_fields = {
                "kind",
                "parent",
                "parent_verification",
                "view_spec",
                "readable_range",
                "scoring_mask",
                "projection_identity",
                "projected_bytes_sha256",
                "access_boundary_digest",
            }
            if schema_version == 5:
                expected_lineage_fields.add("projected_action_evidence_sha256")
            if (
                not isinstance(lineage, dict)
                or set(lineage) != expected_lineage_fields
            ):
                raise ValueError("derived-view lineage fields are invalid")
            if lineage["kind"] != "derived_view":
                raise ValueError("derived snapshot lineage kind is invalid")
            parent = lineage["parent"]
            expected_parent_fields = {
                "instrument",
                "snapshot_id",
                "canonical_sha256",
                "lineage",
            }
            if schema_version == 5:
                expected_parent_fields.add("corporate_action_evidence_sha256")
            if not isinstance(parent, dict) or set(parent) != expected_parent_fields:
                raise ValueError("derived-view parent identity is invalid")
            if parent["instrument"] != metadata["instrument"]:
                raise ValueError("derived-view parent instrument is invalid")
            for field in ("snapshot_id", "canonical_sha256"):
                if not isinstance(parent[field], str) or not re.fullmatch(
                    r"[0-9a-f]{64}", parent[field]
                ):
                    raise ValueError(f"derived-view parent {field} is invalid")
            if not isinstance(parent["lineage"], dict):
                raise ValueError("derived-view parent lineage is invalid")
            attested_parent_manifest = _validate_parent_verification_attestation(
                lineage["parent_verification"],
                parent,
                metadata,
            )
            verified_parent_frame: pd.DataFrame | None = None
            if verify_parent:
                parent_target = target.parent / parent["snapshot_id"]
                parent_verification = _verify_snapshot(
                    parent_target,
                    parent["snapshot_id"],
                    include_frame=True,
                    verify_parent=True,
                    _ancestors=ancestors,
                )
                if not isinstance(parent_verification, tuple):
                    raise ValueError(
                        "parent snapshot verifier did not return market data"
                    )
                parent_manifest, verified_parent_frame = parent_verification
                if parent_manifest != attested_parent_manifest:
                    raise ValueError(
                        "derived-view parent manifest does not match its attestation"
                    )
                if (
                    parent_manifest["metadata"] != metadata
                    or parent_manifest["canonical_sha256"]
                    != parent["canonical_sha256"]
                ):
                    raise ValueError(
                        "derived-view parent snapshot identity does not match"
                    )
                if parent_manifest["schema_version"] in {3, 5}:
                    parent_lineage = parent_manifest["lineage"]
                else:
                    parent_lineage = snapshot_update_lineage(
                        target.parent.parent.parent,
                        metadata["instrument"],
                        parent["snapshot_id"],
                    )
                if parent_lineage != parent["lineage"]:
                    raise ValueError("derived-view parent lineage does not match")
            dates = normalized["Date"].dt.strftime("%Y-%m-%d").tolist()
            view_spec = _validated_fold_window(lineage["view_spec"], dates)
            readable_range = {
                "start": view_spec["allowed_start"],
                "end": view_spec["available_through"],
            }
            if lineage["readable_range"] != readable_range:
                raise ValueError("derived-view readable range is invalid")
            if (
                dates[0] != readable_range["start"]
                or dates[-1] != readable_range["end"]
            ):
                raise ValueError("derived-view bytes exceed their readable range")
            if verified_parent_frame is not None:
                expected_projection = verified_parent_frame.loc[
                    (
                        verified_parent_frame["Date"]
                        >= pd.Timestamp(view_spec["allowed_start"])
                    )
                    & (
                        verified_parent_frame["Date"]
                        <= pd.Timestamp(view_spec["available_through"])
                    )
                ].reset_index(drop=True)
                if (
                    list(expected_projection.columns) != list(normalized.columns)
                    or _canonical_data_bytes(expected_projection)
                    != _canonical_data_bytes(normalized)
                ):
                    raise ValueError(
                        "derived-view bytes do not match the verified parent projection"
                    )
            if lineage["projection_identity"] != PROJECTION_IDENTITY:
                raise ValueError("derived-view projection identity is invalid")
            if (
                lineage["projected_bytes_sha256"]
                != manifest["parquet_sha256"]
            ):
                raise ValueError("derived-view projected bytes digest is invalid")
            _verified_scoring_bounds(target, manifest, normalized)
            access_boundary = {
                "schema_version": 1,
                "parent": parent,
                "parent_verification": lineage["parent_verification"],
                "view_spec": view_spec,
                "projection_identity": PROJECTION_IDENTITY,
                "projected_bytes_sha256": manifest["parquet_sha256"],
                "scoring_mask_sha256": manifest["scoring_mask_sha256"],
            }
            if schema_version == 5:
                if action_evidence is None:
                    raise ValueError("derived-view action evidence is missing")
                if (
                    lineage["projected_action_evidence_sha256"]
                    != manifest["corporate_action_evidence_sha256"]
                ):
                    raise ValueError("derived-view projected action evidence is invalid")
                projection = action_evidence.document.get("projection")
                if (
                    not isinstance(projection, dict)
                    or projection.get("parent_evidence_sha256")
                    != parent["corporate_action_evidence_sha256"]
                    or projection.get("available_through")
                    != view_spec["available_through"]
                ):
                    raise ValueError("derived-view action evidence projection is invalid")
                if verify_parent:
                    if parent_manifest["schema_version"] not in {4, 5}:
                        raise ValueError(
                            "derived-view action evidence parent is not action-aware"
                        )
                    verified_parent_action_evidence = _verified_action_evidence(
                        parent_target,
                        parent_manifest,
                    )
                    expected_action_evidence = project_corporate_action_evidence(
                        verified_parent_action_evidence,
                        view_spec["available_through"],
                    )
                    if (
                        action_evidence.digest != expected_action_evidence.digest
                        or action_evidence.json_bytes()
                        != expected_action_evidence.json_bytes()
                        or dict(action_evidence.artifact_bytes)
                        != dict(expected_action_evidence.artifact_bytes)
                    ):
                        raise ValueError(
                            "derived-view action evidence does not match the verified "
                            "parent projection"
                        )
                access_boundary["projected_action_evidence_sha256"] = manifest[
                    "corporate_action_evidence_sha256"
                ]
            if lineage["access_boundary_digest"] != _sha256(
                _canonical_json(access_boundary)
            ):
                raise ValueError(
                    "derived-view access-boundary digest is invalid"
                )
        if _sha256(_canonical_data_bytes(normalized)) != manifest["canonical_sha256"]:
            raise ValueError("canonical data checksum mismatch")
        if manifest["rows"] != len(normalized):
            raise ValueError("row count mismatch")
        if manifest["data_start"] != str(normalized["Date"].min().date()):
            raise ValueError("data_start mismatch")
        if manifest["data_end"] != str(normalized["Date"].max().date()):
            raise ValueError("data_end mismatch")
        for path, identity in topology_identities:
            current = os.stat(path, follow_symlinks=False)
            if _directory_identity(current) != identity:
                raise ValueError(
                    f"snapshot topology changed while verifying: {path}"
                )
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


def _rename_noreplace(source: Path, target: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace rename is unavailable",
            str(target),
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def publish_snapshot(
    frame: pd.DataFrame,
    root: Path | str,
    metadata: dict[str, str],
    *,
    update_latest: bool = True,
    corporate_action_evidence: CorporateActionEvidence | None = None,
) -> dict[str, str]:
    """Validate and atomically publish one immutable market-data snapshot."""

    normalized_metadata = _validate_metadata(metadata)
    normalized = _normalize_frame(frame)
    canonical_bytes = _canonical_data_bytes(normalized)
    canonical_sha256 = _sha256(canonical_bytes)
    if corporate_action_evidence is not None:
        if not isinstance(corporate_action_evidence, CorporateActionEvidence):
            raise DatasetValidationError("corporate-action evidence type is invalid")
        if not corporate_action_evidence.publishable:
            raise DatasetValidationError("corporate-action evidence is not publishable")
        revisions = corporate_action_evidence.document["revisions"]
        coverage = corporate_action_evidence.document["coverage"]["payload"]
        if (
            coverage["instrument"] != normalized_metadata["instrument"]
            or coverage["market"] != normalized_metadata["market"]
            or any(
                revision["payload"]["instrument"] != normalized_metadata["instrument"]
                or revision["payload"]["market"] != normalized_metadata["market"]
                for revision in revisions
            )
        ):
            raise DatasetValidationError(
                "corporate-action evidence source does not match snapshot metadata"
            )
        schema_version = 4
    else:
        schema_version = 1 if tuple(normalized.columns) == REQUIRED_COLUMNS else 2
    identity = {
        "schema_version": schema_version,
        "metadata": normalized_metadata,
        "canonical_sha256": canonical_sha256,
    }
    if corporate_action_evidence is not None:
        identity["corporate_action_evidence_sha256"] = corporate_action_evidence.digest
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
            files: dict[str, Any] = {"data": "data.parquet"}
            expected_files = {"manifest.json", "data.parquet"}
            if corporate_action_evidence is not None:
                _write_new(
                    temporary / "corporate_actions.json",
                    corporate_action_evidence.json_bytes(),
                )
                artifact_files: dict[str, str] = {}
                for artifact in corporate_action_evidence.document["artifacts"]:
                    artifact_id = artifact["artifact_id"]
                    artifact_path = artifact["path"]
                    _write_new(
                        temporary / artifact_path,
                        corporate_action_evidence.artifact_bytes[artifact_id],
                    )
                    artifact_files[artifact_id] = artifact_path
                    expected_files.add(artifact_path)
                files |= {
                    "corporate_actions": "corporate_actions.json",
                    "corporate_action_artifacts": artifact_files,
                }
                expected_files.add("corporate_actions.json")
            manifest = identity | {
                "snapshot_id": snapshot_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "rows": int(len(normalized)),
                "data_start": str(normalized["Date"].min().date()),
                "data_end": str(normalized["Date"].max().date()),
                "parquet_sha256": parquet_sha256,
                "files": files,
            }
            if schema_version in {2, 4}:
                manifest["columns"] = list(normalized.columns)
            with (temporary / "manifest.json").open("x", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            _seal_snapshot(temporary, frozenset(expected_files))
            _verify_snapshot_seal(temporary, frozenset(expected_files))
            _fsync_directory(temporary)
            _verify_snapshot(
                temporary,
                snapshot_id,
                require_name=False,
            )
            try:
                _rename_noreplace(temporary, target)
            except FileExistsError:
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
    _verify_snapshot(target, snapshot_id)
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
