from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd
import pyarrow as pa

from .datasets import _verify_snapshot
from .strategy_config import ValidatedStrategyConfig, load_strategy_config
from .strategy_replay import replay_strategy
from .strategy_report import render_report, verified_cjk_font_identity


class StrategyRunError(RuntimeError):
    """Raised when a strategy run cannot be verified or published immutably."""


ARTIFACT_NAMES = {
    "config.json",
    "run_manifest.json",
    "daily_replay.csv",
    "events.csv",
    "trades.csv",
    "metrics.json",
    "cost_breakdown.json",
    "report.html",
}
HASHED_ARTIFACT_NAMES = ARTIFACT_NAMES - {"run_manifest.json"}
SOURCE_PATHS = (
    "src/quant_platform/datasets.py",
    "src/quant_platform/__init__.py",
    "src/quant_platform/cli.py",
    "src/quant_platform/strategy_config.py",
    "src/quant_platform/strategy_operators.py",
    "src/quant_platform/strategy_replay.py",
    "src/quant_platform/strategy_report.py",
    "src/quant_platform/strategy_runner.py",
    "pyproject.toml",
    "requirements.lock",
)
RUNTIME_FIELDS = {
    "python",
    "python_implementation",
    "pandas",
    "numpy",
    "matplotlib",
    "pyarrow",
    "cjk_font_path",
    "cjk_font_family",
    "cjk_font_sha256",
}
GIT_FIELDS = {"available", "commit", "dirty"}
RECONCILIATION_FIELDS = {
    "daily_equity",
    "event_cash",
    "event_positions",
    "event_costs",
    "trade_events",
    "profit_identity",
    "trade_net_pnl",
}
SEMANTICS = {
    "account_return_scope": "price_return_only_without_dividend_or_corporate_action_cash_flows",
    "decision_information": "signal_history_ends_before_execution_session",
    "execution_price": "raw_open",
    "terminal_mark": "raw_close",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _runtime_identity(
    font_identity: dict[str, str] | None = None,
) -> dict[str, str]:
    font = font_identity or verified_cjk_font_identity()
    return {
        "python": sys.version,
        "python_implementation": platform.python_implementation(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
        "matplotlib": matplotlib.__version__,
        "pyarrow": pa.__version__,
        "cjk_font_path": font["path"],
        "cjk_font_family": font["family"],
        "cjk_font_sha256": font["sha256"],
    }


def _git_identity(project_root: Path) -> dict[str, Any]:
    repository = project_root.parents[1]
    try:
        commit = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty_output = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if not commit:
            raise ValueError("empty Git commit")
        return {"available": True, "commit": commit, "dirty": bool(dirty_output)}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"available": False, "commit": None, "dirty": None}


def _read_source_payload(path: Path, project_root: Path) -> bytes:
    try:
        relative = path.relative_to(project_root)
        before = path.stat(follow_symlinks=False)
        if path.is_symlink() or not path.is_file():
            raise StrategyRunError(
                f"effective source input is unsafe: {relative.as_posix()}"
            )
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise StrategyRunError(f"effective source input is unavailable: {path}") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(payload) != after.st_size:
        raise StrategyRunError(f"effective source input changed while hashing: {path}")
    return payload


def _effective_source_identity(
    *,
    font_identity: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], dict[str, str], dict[str, Any]]:
    project_root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    for relative in SOURCE_PATHS:
        path = project_root / relative
        payload = _read_source_payload(path, project_root)
        files[relative] = _sha256(payload)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    runtime = (
        _runtime_identity(font_identity)
        if font_identity is not None
        else _runtime_identity()
    )
    digest.update(b"runtime\0")
    digest.update(_canonical_json(runtime))
    return digest.hexdigest(), files, runtime, _git_identity(project_root)


def _bound_snapshot(
    config: ValidatedStrategyConfig,
) -> tuple[Path, dict[str, Any], pd.DataFrame]:
    dataset = config.canonical["dataset"]
    root = Path(dataset["root"]).resolve()
    instrument = dataset["instrument"]
    snapshot_id = dataset["snapshot_id"]
    target = root / "datasets" / instrument / snapshot_id
    if target.is_symlink() or not target.is_dir():
        raise StrategyRunError(
            f"dataset snapshot is not available for instrument {instrument}: {snapshot_id}"
        )
    try:
        verified = _verify_snapshot(target, snapshot_id, include_frame=True)
    except RuntimeError as exc:
        raise StrategyRunError(f"dataset snapshot verification failed: {exc}") from exc
    if not isinstance(verified, tuple):
        raise StrategyRunError("dataset snapshot verifier did not return its frame")
    manifest, frame = verified
    if manifest["metadata"]["instrument"] != instrument:
        raise StrategyRunError(
            "configured dataset instrument does not match snapshot metadata"
        )
    return target, manifest, frame


def _write_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    payload = frame.to_csv(
        index=False,
        date_format="%Y-%m-%d",
        float_format="%.17g",
        lineterminator="\n",
    ).encode("utf-8")
    _write_bytes(path, payload)


def _file_manifest(directory: Path) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "sha256": _sha256((directory / name).read_bytes()),
            "size": (directory / name).stat().st_size,
        }
        for name in sorted(HASHED_ARTIFACT_NAMES)
    }


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal(directory: Path) -> None:
    for path in directory.iterdir():
        if not path.is_file() or path.is_symlink():
            raise StrategyRunError(f"run artifact is not a regular file: {path.name}")
        path.chmod(0o444)
    directory.chmod(0o555)


def _make_removable(directory: Path) -> None:
    if not directory.exists() or directory.is_symlink():
        return
    directory.chmod(0o755)
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink():
            path.chmod(0o644)


def _artifact_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_immutable_artifact(path: Path) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"artifact is not a regular file: {path.name}")
    if before.st_mode & 0o222:
        raise ValueError(f"artifact is not immutable: {path.name}")
    if before.st_nlink != 1:
        raise ValueError(f"artifact has an unsafe hard link count: {path.name}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if _artifact_identity(opened) != _artifact_identity(before):
            raise ValueError(f"artifact changed while opening: {path.name}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _artifact_identity(after) != _artifact_identity(opened):
            raise ValueError(f"artifact changed while reading: {path.name}")
        payload = b"".join(chunks)
        if len(payload) != after.st_size:
            raise ValueError(f"artifact read was incomplete: {path.name}")
    finally:
        os.close(descriptor)
    current = os.stat(path, follow_symlinks=False)
    if _artifact_identity(current) != _artifact_identity(after):
        raise ValueError(f"artifact path changed while reading: {path.name}")
    return payload


def _load_strict_json(payload: bytes, label: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON object key: {key}")
            value[key] = item
        return value

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, ValueError) as exc:
        raise StrategyRunError(f"corrupt {label} JSON: {exc}") from exc


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return value


def _validate_sha_map(value: Any, label: str) -> dict[str, str]:
    mapping = _require_object(value, label)
    if not all(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in mapping.values()
    ):
        raise ValueError(f"{label} values must be SHA-256 strings")
    return mapping


def _validate_runtime(value: Any) -> dict[str, str]:
    runtime = _require_object(value, "runtime")
    if set(runtime) != RUNTIME_FIELDS or not all(
        isinstance(item, str) and item for item in runtime.values()
    ):
        raise ValueError("runtime identity is invalid")
    return runtime


def _validate_git(value: Any) -> dict[str, Any]:
    git = _require_object(value, "git")
    if set(git) != GIT_FIELDS or type(git["available"]) is not bool:
        raise ValueError("Git provenance is invalid")
    if git["available"]:
        if (
            not isinstance(git["commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", git["commit"])
            or type(git["dirty"]) is not bool
        ):
            raise ValueError("available Git provenance is invalid")
    elif git["commit"] is not None or git["dirty"] is not None:
        raise ValueError("unavailable Git provenance must use null values")
    return git


def _verify_run(
    target: Path,
    run_id: str,
    config: ValidatedStrategyConfig,
    dataset_path: Path,
    dataset_manifest: dict[str, Any],
    source_sha256: str,
    source_files: dict[str, str],
    runtime: dict[str, str],
    *,
    require_name: bool = True,
) -> dict[str, Any]:
    try:
        if target.is_symlink() or not target.is_dir():
            raise ValueError("run path is not a safe directory")
        if require_name and target.name != run_id:
            raise ValueError("run directory name does not match run ID")
        if stat.S_IMODE(target.stat().st_mode) & 0o222:
            raise ValueError("run directory is not immutable")
        actual_names = {path.name for path in target.iterdir()}
        if actual_names != ARTIFACT_NAMES:
            raise ValueError(
                f"artifact set mismatch: expected={sorted(ARTIFACT_NAMES)}, "
                f"actual={sorted(actual_names)}"
            )
        artifact_payloads = {
            name: _read_immutable_artifact(target / name)
            for name in sorted(ARTIFACT_NAMES)
        }

        manifest = _require_object(
            _load_strict_json(
                artifact_payloads["run_manifest.json"], "run manifest"
            ),
            "run manifest",
        )
        expected_fields = {
            "schema_version",
            "run_id",
            "identity",
            "config_sha256",
            "dataset_snapshot_id",
            "dataset_canonical_sha256",
            "source_sha256",
            "source_files",
            "runtime",
            "git",
            "semantics",
            "reconciliation",
            "files",
        }
        if set(manifest) != expected_fields:
            raise ValueError("run manifest fields are invalid")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise ValueError("run manifest schema_version must be 1")
        identity = {
            "schema_version": 1,
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
            "source_sha256": source_sha256,
            "runtime": runtime,
        }
        manifest_identity = _require_object(manifest["identity"], "run identity")
        if set(manifest_identity) != set(identity) or manifest_identity != identity:
            raise ValueError("run identity inputs do not match")
        if _sha256(_canonical_json(identity)) != run_id:
            raise ValueError("run ID does not match identity inputs")
        if manifest["run_id"] != run_id:
            raise ValueError("manifest run ID mismatch")
        if manifest["config_sha256"] != config.config_sha256:
            raise ValueError("config checksum binding mismatch")
        if manifest["dataset_snapshot_id"] != dataset_manifest["snapshot_id"]:
            raise ValueError("dataset snapshot binding mismatch")
        if (
            manifest["dataset_canonical_sha256"]
            != dataset_manifest["canonical_sha256"]
        ):
            raise ValueError("dataset canonical checksum binding mismatch")
        if manifest["source_sha256"] != source_sha256:
            raise ValueError("effective source checksum binding mismatch")
        manifest_source_files = _validate_sha_map(
            manifest["source_files"], "source files"
        )
        if manifest_source_files != source_files:
            raise ValueError("effective source file checksums mismatch")
        manifest_runtime = _validate_runtime(manifest["runtime"])
        if manifest_runtime != runtime:
            raise ValueError("runtime identity mismatch")
        _validate_git(manifest["git"])
        semantics = _require_object(manifest["semantics"], "semantics")
        if semantics != SEMANTICS:
            raise ValueError("financial semantics mismatch")
        reconciliation = _require_object(
            manifest["reconciliation"], "reconciliation"
        )
        if set(reconciliation) != RECONCILIATION_FIELDS or not all(
            type(passed) is bool for passed in reconciliation.values()
        ):
            raise ValueError("stored reconciliation gates are invalid")
        if not all(reconciliation.values()):
            raise ValueError("stored reconciliation gates are not all true")

        stored_config = _load_strict_json(
            artifact_payloads["config.json"], "canonical config"
        )
        if stored_config != config.canonical:
            raise ValueError("canonical config artifact mismatch")
        if _sha256(_canonical_json(stored_config)) != config.config_sha256:
            raise ValueError("canonical config artifact checksum mismatch")
        files = _require_object(manifest["files"], "artifact checksum map")
        if set(files) != HASHED_ARTIFACT_NAMES:
            raise ValueError("artifact checksum map is incomplete")
        for name, expected in files.items():
            expected = _require_object(
                expected, f"artifact checksum entry {name}"
            )
            if (
                set(expected) != {"sha256", "size"}
                or not isinstance(expected["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])
                or type(expected["size"]) is not int
                or expected["size"] < 0
            ):
                raise ValueError(f"artifact checksum entry is invalid: {name}")
            payload = artifact_payloads[name]
            if expected != {"sha256": _sha256(payload), "size": len(payload)}:
                raise ValueError(f"artifact checksum or size mismatch: {name}")
        _verify_snapshot(dataset_path, dataset_manifest["snapshot_id"])
        return manifest
    except StrategyRunError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise StrategyRunError(f"corrupt immutable strategy run {target}: {exc}") from exc


def run_strategy_config(config_path: Path | str) -> dict[str, str]:
    config = load_strategy_config(config_path)
    dataset_path, dataset_manifest, frame = _bound_snapshot(config)
    source_sha256, source_files, runtime, git = _effective_source_identity()
    identity = {
        "schema_version": 1,
        "config_sha256": config.config_sha256,
        "dataset_snapshot_id": dataset_manifest["snapshot_id"],
        "source_sha256": source_sha256,
        "runtime": runtime,
    }
    run_id = _sha256(_canonical_json(identity))
    output_root = Path(config.canonical["output_root"]).resolve()
    target = output_root / run_id
    if target.exists():
        _verify_run(
            target,
            run_id,
            config,
            dataset_path,
            dataset_manifest,
            source_sha256,
            source_files,
            runtime,
        )
        return {
            "status": "NO_CHANGE",
            "run_id": run_id,
            "path": str(target),
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
        }

    replay = replay_strategy(frame, config)
    report = render_report(
        replay,
        config,
        {
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
            "dataset_instrument": dataset_manifest["metadata"]["instrument"],
            "dataset_canonical_sha256": dataset_manifest["canonical_sha256"],
            "source_sha256": source_sha256,
            "runtime": runtime,
            "cjk_font_identity": {
                "path": runtime["cjk_font_path"],
                "family": runtime["cjk_font_family"],
                "sha256": runtime["cjk_font_sha256"],
            },
            "git_commit": git["commit"],
            "git_dirty": git["dirty"],
        },
    )
    output_root.mkdir(parents=True, exist_ok=True)
    output_root.chmod(0o755)
    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_root))
    try:
        _write_json(staging / "config.json", config.canonical)
        _write_csv(staging / "daily_replay.csv", replay.daily)
        _write_csv(staging / "events.csv", replay.events)
        _write_csv(staging / "trades.csv", replay.trades)
        _write_json(staging / "metrics.json", replay.metrics)
        _write_json(staging / "cost_breakdown.json", replay.cost_breakdown)
        _write_bytes(staging / "report.html", report.encode("utf-8"))
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "identity": identity,
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
            "dataset_canonical_sha256": dataset_manifest["canonical_sha256"],
            "source_sha256": source_sha256,
            "source_files": source_files,
            "runtime": runtime,
            "git": git,
            "semantics": SEMANTICS,
            "reconciliation": replay.reconciliation,
            "files": _file_manifest(staging),
        }
        _write_json(staging / "run_manifest.json", manifest)
        _fsync_directory(staging)
        _seal(staging)
        _verify_run(
            staging,
            run_id,
            config,
            dataset_path,
            dataset_manifest,
            source_sha256,
            source_files,
            runtime,
            require_name=False,
        )
        try:
            os.rename(staging, target)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not target.exists():
                raise
            _verify_run(
                target,
                run_id,
                config,
                dataset_path,
                dataset_manifest,
                source_sha256,
                source_files,
                runtime,
            )
            status = "NO_CHANGE"
        else:
            _fsync_directory(output_root)
            status = "CREATED"
            staging = target
        _verify_run(
            target,
            run_id,
            config,
            dataset_path,
            dataset_manifest,
            source_sha256,
            source_files,
            runtime,
        )
        return {
            "status": status,
            "run_id": run_id,
            "path": str(target),
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
        }
    finally:
        if staging.exists() and staging != target:
            _make_removable(staging)
            shutil.rmtree(staging)
