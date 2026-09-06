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

from .dataset_lineage import (
    DatasetValidationError,
    _canonical_json,
    _InstrumentLock,
    _open_absolute_directory,
    _open_directory_at,
    _read_update_record,
    _require_pinned_entry,
    _same_inode,
    _sha256,
    _validate_metadata,
    _verified_snapshot_update_lineage,
    load_update_record as load_update_record,
    snapshot_update_lineage,
)
from .datasets import (
    _atomic_json,
    _normalize_frame,
    _verify_snapshot,
    _verified_action_evidence,
    publish_snapshot,
    snapshot_status,
)
from .market_sessions import (
    EXPECTED_SESSIONS_SOURCE_KIND,
    POLICY_VERSION,
    MarketSessionEvidence,
)
from .schemas import SchemaValidationError, canonical_json_bytes


class ConcurrentUpdateError(RuntimeError):
    """Raised when latest changes before a reconciled update can commit."""


_UNSET = object()


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


def _write_update_file(directory_fd: int, name: str, payload: bytes) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError(f"short write while publishing {name}")
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        return os.dup(descriptor)
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
    for name in os.listdir(staging_fd):
        try:
            os.unlink(name, dir_fd=staging_fd)
        except FileNotFoundError:
            pass
    os.rmdir(staging_name, dir_fd=updates_fd)


def _publish_update_record(
    root: Path,
    instrument: str,
    identity: dict[str, Any],
    market_session_evidence: MarketSessionEvidence | None = None,
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
                        if market_session_evidence is not None:
                            evidence_files = {
                                "market_sessions.json": market_session_evidence.json_bytes(),
                                **{
                                    f"market-session-{artifact_id}.bin": payload
                                    for artifact_id, payload in market_session_evidence.artifact_bytes.items()
                                },
                            }
                            for name, payload in evidence_files.items():
                                evidence_fd = _write_update_file(staging_fd, name, payload)
                                try:
                                    _require_pinned_entry(
                                        staging_fd,
                                        name,
                                        evidence_fd,
                                        f"staged {name}",
                                        directory=False,
                                    )
                                finally:
                                    os.close(evidence_fd)
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
                            expected_entries = {"update.json"}
                            if market_session_evidence is not None:
                                expected_entries |= {
                                    "market_sessions.json",
                                    *(
                                        f"market-session-{artifact_id}.bin"
                                        for artifact_id in market_session_evidence.artifact_bytes
                                    ),
                                }
                            if set(os.listdir(target_fd)) != expected_entries:
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
    load_update_record(root, instrument, update_id)
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


def _normalized_overlap_row(frame: pd.DataFrame, session: pd.Timestamp) -> bytes:
    row = frame.loc[
        frame["Date"] == session,
        ["Date", "Open", "High", "Low", "Close", "Volume"],
    ]
    if len(row) != 1:
        raise DatasetValidationError("SOURCE_CONFLICT: protected overlap row is not unique")
    values = row.iloc[0]
    return canonical_json_bytes(
        {
            "Date": str(values["Date"].date()),
            "Open": float(values["Open"]),
            "High": float(values["High"]),
            "Low": float(values["Low"]),
            "Close": float(values["Close"]),
            "Volume": float(values["Volume"]),
        }
    )


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
    expected_prior_snapshot_id: str | None | object = _UNSET,
    protected_overlap_dates: tuple[str, ...] = (),
    market_session_evidence: MarketSessionEvidence | None = None,
) -> dict[str, str | int]:
    """Reconcile requested daily bars into a verified immutable history snapshot."""

    root = Path(root).resolve()
    normalized_metadata = _validate_metadata(metadata)
    if (source_identity is None) != (expected_sessions_source is None):
        raise DatasetValidationError(
            "source and expected-session provenance must be supplied together"
        )
    if source_identity is not None:
        source_identity = _validated_provenance_identity(source_identity, "source identity")
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

    is_v2 = market_session_evidence is not None
    if is_v2:
        if source_identity is None or expected_sessions_source is None:
            raise DatasetValidationError(
                "UPDATE_EVIDENCE_PUBLICATION_FAILED: v2 provenance is required"
            )
        if not isinstance(market_session_evidence, MarketSessionEvidence):
            raise DatasetValidationError(
                "UPDATE_EVIDENCE_PUBLICATION_FAILED: market-session evidence type is invalid"
            )
        if not market_session_evidence.publishable:
            raise DatasetValidationError(
                "UPDATE_EVIDENCE_PUBLICATION_FAILED: market-session evidence is not publishable"
            )
        evidence_scope = market_session_evidence.document.get("scope")
        if evidence_scope != {
            "market": "XSHG",
            "instrument": normalized_metadata["instrument"],
            "start": str(request_start.date()),
            "end": str(request_end.date()),
            "timezone": "Asia/Shanghai",
        }:
            raise DatasetValidationError(
                "UPDATE_EVIDENCE_PUBLICATION_FAILED: market-session evidence scope mismatch"
            )
        if tuple(market_session_evidence.document.get("eligible_sessions", ())) != tuple(
            str(value.date()) for value in sessions
        ):
            raise DatasetValidationError(
                "UPDATE_EVIDENCE_PUBLICATION_FAILED: eligible sessions mismatch"
            )
        expected_projection = {
            "kind": EXPECTED_SESSIONS_SOURCE_KIND,
            "market": "XSHG",
            "instrument": normalized_metadata["instrument"],
            "start": str(request_start.date()),
            "end": str(request_end.date()),
            "evidence_sha256": market_session_evidence.digest,
            "policy_version": POLICY_VERSION,
        }
        if expected_sessions_source != expected_projection:
            raise DatasetValidationError(
                "UPDATE_EVIDENCE_PUBLICATION_FAILED: expected-session source mismatch"
            )
        if expected_prior_snapshot_id is _UNSET:
            raise DatasetValidationError(
                "CONCURRENT_UPDATE: v2 reconciliation requires a prior-generation token"
            )

    instrument = normalized_metadata["instrument"]
    with _InstrumentLock(root, instrument):
        prior_snapshot_id: str | None = None
        prior_manifest: dict[str, Any] | None = None
        prior_action_evidence = None
        previous = pd.DataFrame(columns=normalized_fetched.columns)
        latest_pointer = root / "datasets" / instrument / "latest.json"
        if latest_pointer.exists():
            prior = snapshot_status(root, instrument)
            prior_snapshot_id = prior["snapshot_id"]
        if expected_prior_snapshot_id is not _UNSET and prior_snapshot_id != expected_prior_snapshot_id:
            raise ConcurrentUpdateError("CONCURRENT_UPDATE: latest snapshot generation changed")
        if prior_snapshot_id is not None:
            prior_path = root / "datasets" / instrument / prior_snapshot_id
            verified = _verify_snapshot(prior_path, prior_snapshot_id, include_frame=True)
            if not isinstance(verified, tuple):
                raise RuntimeError("snapshot verifier did not return the prior frame")
            prior_manifest, previous = verified
            if prior_manifest["schema_version"] in {3, 5}:
                raise DatasetValidationError(
                    "UPDATE_PRIOR_IS_DERIVED_VIEW: latest snapshot is not an update root"
                )
            if prior_manifest["schema_version"] not in {1, 2, 4}:
                raise DatasetValidationError("UPDATE_PRIOR_IS_DERIVED_VIEW: unsupported prior root")
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
            if is_v2:
                _verified_snapshot_update_lineage(root, instrument, prior_snapshot_id)

        if is_v2:
            fetched_order = tuple(normalized_fetched["Date"])
            prior_dates = set(previous["Date"])
            recomputed_overlap = tuple(
                str(value.date()) for value in fetched_order if value in prior_dates
            )
            if tuple(protected_overlap_dates) != recomputed_overlap:
                raise DatasetValidationError(
                    "SOURCE_CONFLICT: protected overlap set does not match selected generation"
                )
            for value in fetched_order:
                rendered = str(value.date())
                if rendered not in protected_overlap_dates:
                    continue
                if _normalized_overlap_row(previous, value) != _normalized_overlap_row(
                    normalized_fetched, value
                ):
                    assert prior_snapshot_id is not None
                    prior_lineage = _verified_snapshot_update_lineage(
                        root, instrument, prior_snapshot_id
                    )
                    raise DatasetValidationError(
                        "SOURCE_CONFLICT: overlap differs; "
                        f"prior_snapshot_id={prior_snapshot_id}; "
                        f"prior_source={prior_lineage.get('source')}; "
                        f"fetched_source={source_identity}"
                    )
            if prior_manifest is not None and prior_manifest["schema_version"] == 4:
                assert prior_snapshot_id is not None
                try:
                    prior_action_evidence = _verified_action_evidence(
                        root / "datasets" / instrument / prior_snapshot_id,
                        prior_manifest,
                    )
                except (KeyError, OSError, RuntimeError, ValueError) as exc:
                    raise DatasetValidationError(
                        f"CORPORATE_ACTION_EVIDENCE_INVALID: {exc}"
                    ) from exc

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
            raise DatasetValidationError(f"missing expected sessions after merge: {rendered}")
        merged = _normalize_frame(merged_input)

        snapshot = publish_snapshot(
            merged,
            root,
            normalized_metadata,
            update_latest=False,
            corporate_action_evidence=prior_action_evidence,
        )
        result_manifest = _verify_snapshot(Path(snapshot["path"]), snapshot["snapshot_id"])
        if isinstance(result_manifest, tuple):
            result_manifest = result_manifest[0]
        prior_action_digest = (
            prior_manifest.get("corporate_action_evidence_sha256")
            if prior_manifest is not None
            else None
        )
        result_action_digest = result_manifest.get("corporate_action_evidence_sha256")
        if is_v2 and prior_action_digest != result_action_digest:
            raise DatasetValidationError(
                "CORPORATE_ACTION_EVIDENCE_DOWNGRADE: result action evidence changed"
            )
        identity: dict[str, Any] = {
            "schema_version": 2 if is_v2 else 1,
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
        if market_session_evidence is not None:
            artifact_files = {
                artifact_id: f"market-session-{artifact_id}.bin"
                for artifact_id in sorted(market_session_evidence.artifact_bytes)
            }
            identity |= {
                "market_session_evidence_sha256": market_session_evidence.digest,
                "market_session_evidence_files": {
                    "document": "market_sessions.json",
                    "artifacts": artifact_files,
                },
                "prior_corporate_action_evidence_sha256": prior_action_digest,
                "result_corporate_action_evidence_sha256": result_action_digest,
            }
        if market_session_evidence is None:
            update_id, update_path = _publish_update_record(root, instrument, identity)
        else:
            update_id, update_path = _publish_update_record(
                root, instrument, identity, market_session_evidence
            )
        snapshot_update_lineage(root, instrument, snapshot["snapshot_id"])
        _commit_latest(root, instrument, snapshot["snapshot_id"], prior_snapshot_id)
        loaded = load_update_record(root, instrument, update_id)
        if loaded["result_snapshot_id"] != snapshot["snapshot_id"]:
            raise RuntimeError("published update does not bind the result snapshot")
        latest = snapshot_status(root, instrument)
        if latest["snapshot_id"] != snapshot["snapshot_id"]:
            raise ConcurrentUpdateError("CONCURRENT_UPDATE: latest readback mismatch")
        return snapshot | {
            "update_id": update_id,
            "update_path": str(update_path),
            "revision_count": revision_count,
        }
