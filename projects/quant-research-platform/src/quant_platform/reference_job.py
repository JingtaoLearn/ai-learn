from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Volume")
EXECUTION_ENVELOPE = {
    "cap_drop": ["ALL"],
    "cpus": 1.0,
    "memory_mib": 512,
    "network": "none",
    "no_new_privileges": True,
    "pids_limit": 256,
    "read_only_root": True,
}


class ReferenceJobError(RuntimeError):
    """Raised when immutable inputs cannot produce trusted reference outputs."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (OSError, ValueError) as exc:
        raise ReferenceJobError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReferenceJobError(f"{label} must be a JSON object")
    return value


def _verify_source(workspace: Path, submission: dict[str, Any]) -> None:
    expected = submission.get("source_files")
    if not isinstance(expected, dict) or not expected:
        raise ReferenceJobError("submission source manifest is invalid")
    actual_paths: list[Path] = []
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink():
            raise ReferenceJobError(
                f"submission workspace contains symlink: {path.relative_to(workspace)}"
            )
        if path.is_file():
            actual_paths.append(path)
    digest = hashlib.sha256()
    actual: dict[str, str] = {}
    for path in sorted(actual_paths, key=lambda item: item.relative_to(workspace).as_posix()):
        relative = path.relative_to(workspace).as_posix()
        payload = path.read_bytes()
        actual[relative] = _sha256(payload)
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    if actual != expected or digest.hexdigest() != submission.get("source_sha256"):
        raise ReferenceJobError("submission source checksum mismatch")


def _verify_submission(
    submission_path: Path, workspace: Path
) -> dict[str, Any]:
    submission = _load_json(submission_path, "submission contract")
    try:
        identity = {
            "schema_version": submission["schema_version"],
            "spec": submission["spec"],
            "source_sha256": submission["source_sha256"],
            "execution_envelope": submission["execution_envelope"],
        }
        submission_id = submission["submission_id"]
        if _sha256(_canonical_json(identity)) != submission_id:
            raise ReferenceJobError("submission contract identity mismatch")
        if submission["dataset_snapshot_id"] != submission["spec"]["dataset_snapshot_id"]:
            raise ReferenceJobError("submission dataset identity mismatch")
        if submission["runner_image"] != submission["spec"]["runner_image"]:
            raise ReferenceJobError("submission runner image identity mismatch")
        if submission["execution_envelope"] != EXECUTION_ENVELOPE:
            raise ReferenceJobError("submission execution envelope mismatch")
    except KeyError as exc:
        raise ReferenceJobError(f"submission contract field is missing: {exc}") from exc
    _verify_source(workspace, submission)
    return submission


def _verify_run(
    run_path: Path, submission: dict[str, Any]
) -> dict[str, Any]:
    run = _load_json(run_path, "run contract")
    identity_fields = {
        "schema_version",
        "submission_id",
        "dataset_snapshot_id",
        "runner_image",
        "execution_envelope",
        "attempt_id",
        "artifact_path",
    }
    try:
        identity = {field: run[field] for field in identity_fields}
        if set(run) != identity_fields | {"run_id"}:
            raise ReferenceJobError("run contract fields are invalid")
        if _sha256(_canonical_json(identity)) != run["run_id"]:
            raise ReferenceJobError("run contract identity mismatch")
        bindings = {
            "submission_id": submission["submission_id"],
            "dataset_snapshot_id": submission["dataset_snapshot_id"],
            "runner_image": submission["runner_image"],
            "execution_envelope": submission["execution_envelope"],
        }
        if any(run[key] != value for key, value in bindings.items()):
            raise ReferenceJobError("run contract identity does not match submission")
    except KeyError as exc:
        raise ReferenceJobError(f"run contract field is missing: {exc}") from exc
    return run


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if tuple(frame.columns) != REQUIRED_COLUMNS:
        raise ReferenceJobError(
            f"dataset columns must be exactly {list(REQUIRED_COLUMNS)}"
        )
    clean = frame.copy()
    if clean.empty:
        raise ReferenceJobError("dataset cannot be empty")
    dates: list[pd.Timestamp] = []
    try:
        for value in clean["Date"]:
            timestamp = pd.Timestamp(value)
            if pd.isna(timestamp) or timestamp.tz is not None:
                raise ValueError("invalid daily date")
            if timestamp != timestamp.normalize():
                raise ValueError("daily date is not midnight")
            dates.append(timestamp)
        clean["Date"] = pd.DatetimeIndex(dates)
        clean[list(REQUIRED_COLUMNS[1:])] = clean[list(REQUIRED_COLUMNS[1:])].apply(
            pd.to_numeric, errors="raise"
        )
    except (TypeError, ValueError) as exc:
        raise ReferenceJobError(f"dataset values are invalid: {exc}") from exc
    if clean["Date"].duplicated().any():
        raise ReferenceJobError("dataset contains duplicate dates")
    values = clean[list(REQUIRED_COLUMNS[1:])].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ReferenceJobError("dataset contains non-finite values")
    clean[list(REQUIRED_COLUMNS[1:])] = clean[list(REQUIRED_COLUMNS[1:])].astype(
        "float64"
    )
    normalized = clean.sort_values("Date").reset_index(drop=True)
    if not normalized["Date"].equals(clean["Date"].reset_index(drop=True)):
        raise ReferenceJobError("dataset dates are not sorted")
    return normalized


def _canonical_data_bytes(frame: pd.DataFrame) -> bytes:
    dates = frame["Date"].astype("int64").to_numpy(dtype=">i8")
    numeric = frame[list(REQUIRED_COLUMNS[1:])].to_numpy(dtype=">f8")
    return (
        b"quant-platform-ohlcv-v1\0"
        + struct.pack(">Q", len(frame))
        + dates.tobytes(order="C")
        + numeric.tobytes(order="C")
    )


def _verify_dataset(
    dataset_dir: Path, submission: dict[str, Any]
) -> tuple[dict[str, Any], pd.DataFrame]:
    manifest = _load_json(dataset_dir / "manifest.json", "dataset manifest")
    parquet_path = dataset_dir / "data.parquet"
    try:
        if manifest["snapshot_id"] != submission["dataset_snapshot_id"]:
            raise ReferenceJobError("dataset identity does not match submission")
        if _sha256(parquet_path.read_bytes()) != manifest["parquet_sha256"]:
            raise ReferenceJobError("dataset Parquet checksum mismatch")
        frame = _normalize_frame(pd.read_parquet(parquet_path))
        if _sha256(_canonical_data_bytes(frame)) != manifest["canonical_sha256"]:
            raise ReferenceJobError("dataset canonical checksum mismatch")
        identity = {
            "schema_version": manifest["schema_version"],
            "metadata": manifest["metadata"],
            "canonical_sha256": manifest["canonical_sha256"],
        }
        if _sha256(_canonical_json(identity)) != manifest["snapshot_id"]:
            raise ReferenceJobError("dataset snapshot identity mismatch")
        if manifest["rows"] != len(frame):
            raise ReferenceJobError("dataset row count mismatch")
        if manifest["data_start"] != str(frame["Date"].min().date()):
            raise ReferenceJobError("dataset start date mismatch")
        if manifest["data_end"] != str(frame["Date"].max().date()):
            raise ReferenceJobError("dataset end date mismatch")
    except KeyError as exc:
        raise ReferenceJobError(f"dataset manifest field is missing: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ReferenceJobError(f"dataset checksum validation failed: {exc}") from exc
    return manifest, frame


def _write_new(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ReferenceJobError(f"cannot publish reference artifact {path.name}: {exc}") from exc


def run_reference_job(
    dataset_dir: Path | str,
    submission_path: Path | str,
    run_path: Path | str,
    artifacts_dir: Path | str,
    *,
    workspace: Path | str = "/workspace",
) -> dict[str, str]:
    """Validate immutable inputs and write deterministic snapshot-derived evidence."""

    dataset_dir = Path(dataset_dir)
    submission_path = Path(submission_path)
    run_path = Path(run_path)
    artifacts_dir = Path(artifacts_dir)
    workspace = Path(workspace)
    submission = _verify_submission(submission_path, workspace)
    run = _verify_run(run_path, submission)
    dataset, frame = _verify_dataset(dataset_dir, submission)
    if not artifacts_dir.is_dir() or artifacts_dir.is_symlink():
        raise ReferenceJobError("artifacts directory must be an existing regular directory")

    daily = pd.DataFrame(
        {
            "Date": frame["Date"].dt.strftime("%Y-%m-%d"),
            "Close": frame["Close"],
            "DailyReturn": frame["Close"].pct_change(fill_method=None),
            "NormalizedClose": frame["Close"] / frame["Close"].iloc[0],
        }
    )
    daily_payload = daily.to_csv(
        index=False, lineterminator="\n", float_format="%.17g"
    ).encode()
    result = {
        "schema_version": 1,
        "submission_id": submission["submission_id"],
        "dataset_snapshot_id": dataset["snapshot_id"],
        "run_id": run["run_id"],
        "rows": len(frame),
        "data_start": dataset["data_start"],
        "data_end": dataset["data_end"],
        "daily_sha256": _sha256(daily_payload),
    }
    result_payload = (
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    daily_path = artifacts_dir / "daily.csv"
    result_path = artifacts_dir / "result.json"
    _write_new(daily_path, daily_payload)
    _write_new(result_path, result_payload)
    return {"result": str(result_path), "daily": str(daily_path)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--submission", required=True)
    parser.add_argument("--run-contract", required=True)
    parser.add_argument("--artifacts", required=True)
    args = parser.parse_args(argv)
    run_reference_job(
        args.dataset,
        args.submission,
        args.run_contract,
        args.artifacts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
