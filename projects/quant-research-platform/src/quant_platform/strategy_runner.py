from __future__ import annotations

import hashlib
import errno
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from .datasets import _verify_snapshot
from .strategy_config import ValidatedStrategyConfig, load_strategy_config
from .strategy_replay import replay_strategy
from .strategy_report import render_report


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
SOURCE_MODULES = (
    "strategy_config.py",
    "strategy_operators.py",
    "strategy_replay.py",
    "strategy_report.py",
    "strategy_runner.py",
)
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


def _effective_source_identity() -> tuple[str, dict[str, str]]:
    source_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    for name in SOURCE_MODULES:
        path = source_root / name
        if not path.is_file() or path.is_symlink():
            raise StrategyRunError(f"effective source module is unavailable: {name}")
        payload = path.read_bytes()
        files[name] = _sha256(payload)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest(), files


def _bound_snapshot(config: ValidatedStrategyConfig) -> tuple[Path, dict[str, Any]]:
    dataset = config.canonical["dataset"]
    root = Path(dataset["root"]).resolve()
    snapshot_id = dataset["snapshot_id"]
    matches = sorted((root / "datasets").glob(f"*/{snapshot_id}"))
    if (
        len(matches) != 1
        or matches[0].is_symlink()
        or not matches[0].is_dir()
    ):
        raise StrategyRunError(
            f"dataset snapshot is not uniquely available: {snapshot_id}"
        )
    target = matches[0]
    try:
        manifest = _verify_snapshot(target, snapshot_id)
    except RuntimeError as exc:
        raise StrategyRunError(f"dataset snapshot verification failed: {exc}") from exc
    return target, manifest


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
        float_format="%.12g",
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


def _load_strict_json(path: Path, label: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON object key: {key}")
            value[key] = item
        return value

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise StrategyRunError(f"corrupt {label} JSON: {exc}") from exc


def _verify_run(
    target: Path,
    run_id: str,
    config: ValidatedStrategyConfig,
    dataset_path: Path,
    dataset_manifest: dict[str, Any],
    source_sha256: str,
    source_files: dict[str, str],
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
        for path in target.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"artifact is not a regular file: {path.name}")
            if stat.S_IMODE(path.stat().st_mode) & 0o222:
                raise ValueError(f"artifact is not immutable: {path.name}")

        manifest = _load_strict_json(target / "run_manifest.json", "run manifest")
        expected_fields = {
            "schema_version",
            "run_id",
            "identity",
            "config_sha256",
            "dataset_snapshot_id",
            "dataset_canonical_sha256",
            "source_sha256",
            "source_files",
            "semantics",
            "reconciliation",
            "files",
        }
        if set(manifest) != expected_fields:
            raise ValueError("run manifest fields are invalid")
        if manifest["schema_version"] != 1:
            raise ValueError("run manifest schema_version must be 1")
        identity = {
            "schema_version": 1,
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
            "source_sha256": source_sha256,
        }
        if manifest["identity"] != identity:
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
        if manifest["source_files"] != source_files:
            raise ValueError("effective source file checksums mismatch")
        if manifest["semantics"] != SEMANTICS:
            raise ValueError("financial semantics mismatch")
        if not all(manifest["reconciliation"].values()):
            raise ValueError("stored reconciliation gates are not all true")

        stored_config = _load_strict_json(target / "config.json", "canonical config")
        if stored_config != config.canonical:
            raise ValueError("canonical config artifact mismatch")
        if _sha256(_canonical_json(stored_config)) != config.config_sha256:
            raise ValueError("canonical config artifact checksum mismatch")
        files = manifest["files"]
        if set(files) != HASHED_ARTIFACT_NAMES:
            raise ValueError("artifact checksum map is incomplete")
        for name, expected in files.items():
            path = target / name
            payload = path.read_bytes()
            if expected != {"sha256": _sha256(payload), "size": len(payload)}:
                raise ValueError(f"artifact checksum or size mismatch: {name}")
        _verify_snapshot(dataset_path, dataset_manifest["snapshot_id"])
        return manifest
    except (KeyError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, StrategyRunError):
            raise
        raise StrategyRunError(f"corrupt immutable strategy run {target}: {exc}") from exc


def run_strategy_config(config_path: Path | str) -> dict[str, str]:
    config = load_strategy_config(config_path)
    dataset_path, dataset_manifest = _bound_snapshot(config)
    source_sha256, source_files = _effective_source_identity()
    identity = {
        "schema_version": 1,
        "config_sha256": config.config_sha256,
        "dataset_snapshot_id": dataset_manifest["snapshot_id"],
        "source_sha256": source_sha256,
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
        )
        return {
            "status": "NO_CHANGE",
            "run_id": run_id,
            "path": str(target),
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
        }

    frame = pd.read_parquet(dataset_path / "data.parquet")
    replay = replay_strategy(frame, config)
    report = render_report(
        replay,
        config,
        {
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
            "dataset_canonical_sha256": dataset_manifest["canonical_sha256"],
            "source_sha256": source_sha256,
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
