from __future__ import annotations

import errno
import copy
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
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any

import matplotlib
import numpy as np
import pandas as pd
import pyarrow as pa

from .corporate_actions import SettlementSchedule, tax_policy_identity
from .datasets import _verified_action_evidence, _verified_scoring_bounds, _verify_snapshot
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
SETTLEMENT_ARTIFACT_NAMES = ARTIFACT_NAMES | {
    "account_events.csv",
    "account_trades.csv",
}
PACKAGE_SOURCE_PATHS = (
    ("src/quant_platform/catalog.py", "catalog.py"),
    ("src/quant_platform/composition_worker.py", "composition_worker.py"),
    ("src/quant_platform/corporate_actions.py", "corporate_actions.py"),
    ("src/quant_platform/datasets.py", "datasets.py"),
    ("src/quant_platform/experiment_service.py", "experiment_service.py"),
    ("src/quant_platform/__init__.py", "__init__.py"),
    ("src/quant_platform/cli.py", "cli.py"),
    ("src/quant_platform/operator_worker.py", "operator_worker.py"),
    ("src/quant_platform/resolved_runner.py", "resolved_runner.py"),
    ("src/quant_platform/schemas.py", "schemas.py"),
    ("src/quant_platform/seed.py", "seed.py"),
    ("src/quant_platform/strategy_config.py", "strategy_config.py"),
    ("src/quant_platform/strategy_operators.py", "strategy_operators.py"),
    ("src/quant_platform/strategy_replay.py", "strategy_replay.py"),
    ("src/quant_platform/strategy_report.py", "strategy_report.py"),
    ("src/quant_platform/strategy_runner.py", "strategy_runner.py"),
    ("src/quant_platform/study_contracts.py", "study_contracts.py"),
    ("src/quant_platform/study_datasets.py", "study_datasets.py"),
    ("src/quant_platform/worker.py", "worker.py"),
)
PROJECT_SOURCE_PATHS = (
    "pyproject.toml",
    "requirements.lock",
)
SOURCE_PATHS = tuple(label for label, _ in PACKAGE_SOURCE_PATHS) + PROJECT_SOURCE_PATHS
PROJECT_ROOT_ENV = "QUANT_PLATFORM_PROJECT_ROOT"
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
SETTLEMENT_RECONCILIATION_FIELDS = RECONCILIATION_FIELDS | {
    "integer_fen",
    "settled_quantity",
    "account_isolation",
    "deferred_tax",
}
SEMANTICS = {
    "account_return_scope": "price_return_only_without_dividend_or_corporate_action_cash_flows",
    "decision_information": "signal_history_ends_before_execution_session",
    "execution_price": "raw_open",
    "terminal_mark": "raw_close",
}


def _settlement_accounting(evidence_sha256: str, schedule: SettlementSchedule) -> dict[str, Any]:
    return {
        "claim": "KNOWN_EVENT_CORRECTED_PARTIAL",
        "corporate_action_evidence_sha256": evidence_sha256,
        "tax_policy": tax_policy_identity(),
        "settlement_schedule": {
            "sha256": schedule.digest,
            "document": schedule.document,
        },
    }


def _semantics(accounting: dict[str, Any] | None) -> dict[str, Any]:
    if accounting is None:
        return dict(SEMANTICS)
    return {
        "account_return_scope": "known_event_corrected_partial_after_tax",
        "decision_information": "signal_history_ends_before_execution_session",
        "execution_price": "raw_open",
        "terminal_mark": "raw_close",
        "money_posting": "integer_fen_round_half_up_research_assumption",
        "holding_period_endpoint": "day_before_transfer_settlement",
        "fifo": "per_securities_account_end_of_day_net_change",
        "tax_collection": "after_transfer_settlement_next_trading_day",
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


def _unavailable_git_identity() -> dict[str, Any]:
    return {"available": False, "commit": None, "dirty": None}


def _git_identity(project_root: Path | None) -> dict[str, Any]:
    if project_root is None:
        return _unavailable_git_identity()
    try:
        repository_output = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        repository = Path(repository_output).resolve(strict=True)
        project_relative = project_root.relative_to(repository)
        tracked_metadata = [
            (project_relative / relative).as_posix() for relative in PROJECT_SOURCE_PATHS
        ]
        subprocess.run(
            ["git", "-C", str(repository), "ls-files", "--error-unmatch", "--"] + tracked_metadata,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        commit = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
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
            timeout=5,
        ).stdout
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("empty Git commit")
        return {"available": True, "commit": commit, "dirty": bool(dirty_output)}
    except (OSError, subprocess.SubprocessError, ValueError):
        return _unavailable_git_identity()


def _source_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_source_payload(
    path: Path,
    label: str,
    *,
    directory_fd: int | None = None,
) -> bytes:
    target: Path | str = path.name if directory_fd is not None else path
    try:
        before = os.stat(target, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise StrategyRunError(f"effective source input is unsafe: {label}")
        if before.st_nlink != 1:
            raise StrategyRunError(f"effective source input has an unsafe hard link count: {label}")
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if _source_file_identity(opened) != _source_file_identity(before):
                raise StrategyRunError(f"effective source input changed while opening: {label}")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if _source_file_identity(after) != _source_file_identity(opened):
                raise StrategyRunError(f"effective source input changed while hashing: {label}")
            payload = b"".join(chunks)
            if len(payload) != after.st_size:
                raise StrategyRunError(f"effective source input read was incomplete: {label}")
        finally:
            os.close(descriptor)
        current = os.stat(target, dir_fd=directory_fd, follow_symlinks=False)
        if _source_file_identity(current) != _source_file_identity(after):
            raise StrategyRunError(f"effective source input path changed while hashing: {label}")
    except StrategyRunError:
        raise
    except OSError as exc:
        raise StrategyRunError(f"effective source input is unavailable: {path}") from exc
    return payload


def _absolute_path(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _directory_open_flags() -> int:
    return (
        getattr(os, "O_PATH", os.O_RDONLY)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _require_safe_directory(path: Path, label: str) -> None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise StrategyRunError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise StrategyRunError(f"{label} is unsafe: {path}")


def _require_no_symlink_components(path: Path, label: str) -> None:
    for component in reversed((path, *path.parents)):
        try:
            metadata = os.stat(component, follow_symlinks=False)
        except OSError as exc:
            raise StrategyRunError(f"{label} is unavailable: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise StrategyRunError(f"{label} contains a symlink: {component}")


@contextmanager
def _open_anchored_directory(path: Path, label: str) -> Iterator[int]:
    flags = _directory_open_flags()
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise StrategyRunError(f"{label} is unsafe: {path}")
        _require_no_symlink_components(path, label)
        current = os.stat(path, follow_symlinks=False)
        if _directory_identity(current) != _directory_identity(opened):
            raise StrategyRunError(f"{label} changed while opening: {path}")
        yield descriptor
        after = os.fstat(descriptor)
        _require_no_symlink_components(path, label)
        current = os.stat(path, follow_symlinks=False)
        if _directory_identity(after) != _directory_identity(opened) or _directory_identity(
            current
        ) != _directory_identity(opened):
            raise StrategyRunError(f"{label} changed while hashing: {path}")
    except StrategyRunError:
        raise
    except OSError as exc:
        raise StrategyRunError(f"{label} is unavailable or unsafe: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_project_layout_at(root_fd: int, label: str) -> None:
    flags = _directory_open_flags()
    src_fd = -1
    package_fd = -1
    try:
        for relative in PROJECT_SOURCE_PATHS:
            try:
                metadata = os.stat(relative, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise StrategyRunError(
                    f"{label} is missing required source input: {relative}"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise StrategyRunError(f"{label} has an unsafe required source input: {relative}")
        try:
            src_fd = os.open("src", flags, dir_fd=root_fd)
            package_fd = os.open("quant_platform", flags, dir_fd=src_fd)
        except FileNotFoundError as exc:
            raise StrategyRunError(
                f"{label} is missing required source input: src/quant_platform"
            ) from exc
        for source_label, filename in PACKAGE_SOURCE_PATHS:
            try:
                metadata = os.stat(filename, dir_fd=package_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise StrategyRunError(
                    f"{label} is missing required source input: {source_label}"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise StrategyRunError(
                    f"{label} has an unsafe required source input: {source_label}"
                )
    except StrategyRunError:
        raise
    except OSError as exc:
        raise StrategyRunError(f"{label} does not have the exact expected project layout") from exc
    finally:
        if package_fd >= 0:
            os.close(package_fd)
        if src_fd >= 0:
            os.close(src_fd)


def _validate_project_root(path: Path | str, label: str) -> Path:
    raw_path = os.fspath(path)
    if not raw_path or not Path(raw_path).is_absolute():
        raise StrategyRunError(f"{label} must be a non-empty absolute path")
    root = _absolute_path(path)
    with _open_anchored_directory(root, label) as root_fd:
        _validate_project_layout_at(root_fd, label)
    return root


def _is_recognizable_project_root(path: Path) -> bool:
    return all(
        os.path.lexists(path / relative) for relative in ("pyproject.toml", "src/quant_platform")
    )


def _discover_project_root(
    explicit_root: Path | str | None = None,
    *,
    cwd: Path | None = None,
    package_root: Path | None = None,
) -> Path:
    if explicit_root is not None:
        return _validate_project_root(explicit_root, "explicit project root")

    environment_root = os.environ.get(PROJECT_ROOT_ENV)
    if environment_root is not None:
        if not environment_root:
            raise StrategyRunError("environment project root is empty")
        return _validate_project_root(environment_root, "environment project root")

    current = _absolute_path(cwd or Path.cwd())
    _require_safe_directory(current, "current directory")
    candidates = [
        ancestor
        for ancestor in (current, *current.parents)
        if _is_recognizable_project_root(ancestor)
    ]
    validated = [
        _validate_project_root(candidate, "current-directory project root")
        for candidate in candidates
    ]
    if len(validated) > 1:
        raise StrategyRunError(
            "project root discovery is ambiguous: "
            + ", ".join(str(candidate) for candidate in validated)
        )
    if validated:
        return validated[0]

    loaded_package = _absolute_path(package_root or Path(__file__).resolve(strict=True).parent)
    if loaded_package.name == "quant_platform" and loaded_package.parent.name == "src":
        editable_root = loaded_package.parent.parent
        return _validate_project_root(editable_root, "editable-layout project root")
    raise StrategyRunError(
        "complete project source root is required; run from the project source "
        "checkout or pass --project-root (or set QUANT_PLATFORM_PROJECT_ROOT)"
    )


def _effective_source_identity(
    *,
    font_identity: dict[str, str] | None = None,
    project_root: Path | str | None = None,
    cwd: Path | None = None,
) -> tuple[str, dict[str, str], dict[str, str], dict[str, Any]]:
    try:
        package_root = Path(__file__).resolve(strict=True).parent
    except OSError as exc:
        raise StrategyRunError("loaded quant_platform package is unavailable") from exc
    _require_safe_directory(package_root, "loaded quant_platform package")
    discovered_root = _discover_project_root(
        project_root,
        cwd=cwd,
        package_root=package_root,
    )
    digest = hashlib.sha256()
    files: dict[str, str] = {}

    def bind_input(label: str, path: Path, directory_fd: int) -> None:
        payload = _read_source_payload(path, label, directory_fd=directory_fd)
        files[label] = _sha256(payload)
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")

    with _open_anchored_directory(package_root, "loaded quant_platform package") as package_fd:
        for label, filename in PACKAGE_SOURCE_PATHS:
            bind_input(label, package_root / filename, package_fd)
    with _open_anchored_directory(discovered_root, "discovered project root") as project_fd:
        _validate_project_layout_at(project_fd, "discovered project root")
        for relative in PROJECT_SOURCE_PATHS:
            bind_input(relative, discovered_root / relative, project_fd)
    runtime = _runtime_identity(font_identity) if font_identity is not None else _runtime_identity()
    digest.update(b"runtime\0")
    digest.update(_canonical_json(runtime))
    return digest.hexdigest(), files, runtime, _git_identity(discovered_root)


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
        verified = _verify_snapshot(
            target,
            snapshot_id,
            include_frame=True,
            verify_parent=False,
        )
    except RuntimeError as exc:
        raise StrategyRunError(f"dataset snapshot verification failed: {exc}") from exc
    if not isinstance(verified, tuple):
        raise StrategyRunError("dataset snapshot verifier did not return its frame")
    manifest, frame = verified
    if manifest["metadata"]["instrument"] != instrument:
        raise StrategyRunError("configured dataset instrument does not match snapshot metadata")
    if manifest["schema_version"] == 3:
        try:
            scoring_start, scoring_end = _verified_scoring_bounds(
                target,
                manifest,
                frame,
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise StrategyRunError(f"dataset scoring mask verification failed: {exc}") from exc
        parameters = config.template_parameters
        if parameters["evaluation_start"] != scoring_start:
            raise StrategyRunError(
                "template evaluation_start must exactly match derived lineage scoring_start"
            )
        if parameters["evaluation_end"] != scoring_end:
            raise StrategyRunError(
                "template evaluation_end must exactly match derived lineage scoring_end"
            )
        if (
            manifest["lineage"]["view_spec"]["account_policy"] == "FORCE_FLAT_WITH_COST"
            and parameters["terminal_handling"] != "force_liquidate"
        ):
            raise StrategyRunError(
                "FORCE_FLAT_WITH_COST requires force_liquidate terminal handling"
            )
    return target, manifest, frame


def _write_json(path: Path, value: Any) -> None:
    payload = (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
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


def _file_manifest(
    directory: Path, artifact_names: set[str] = HASHED_ARTIFACT_NAMES
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "sha256": _sha256((directory / name).read_bytes()),
            "size": (directory / name).stat().st_size,
        }
        for name in sorted(artifact_names)
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
    composition_digest: str | None = None,
    accounting: dict[str, Any] | None = None,
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
        expected_artifact_names = (
            SETTLEMENT_ARTIFACT_NAMES if accounting is not None else ARTIFACT_NAMES
        )
        actual_names = {path.name for path in target.iterdir()}
        if actual_names != expected_artifact_names:
            raise ValueError(
                f"artifact set mismatch: expected={sorted(expected_artifact_names)}, "
                f"actual={sorted(actual_names)}"
            )
        artifact_payloads = {
            name: _read_immutable_artifact(target / name)
            for name in sorted(expected_artifact_names)
        }

        manifest = _require_object(
            _load_strict_json(artifact_payloads["run_manifest.json"], "run manifest"),
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
        if accounting is not None:
            expected_fields.add("accounting")
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
        if composition_digest is not None:
            identity["composition_digest"] = composition_digest
        if accounting is not None:
            identity["accounting"] = accounting
            if manifest["accounting"] != accounting:
                raise ValueError("accounting identity inputs do not match")
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
        if manifest["dataset_canonical_sha256"] != dataset_manifest["canonical_sha256"]:
            raise ValueError("dataset canonical checksum binding mismatch")
        if manifest["source_sha256"] != source_sha256:
            raise ValueError("effective source checksum binding mismatch")
        manifest_source_files = _validate_sha_map(manifest["source_files"], "source files")
        if manifest_source_files != source_files:
            raise ValueError("effective source file checksums mismatch")
        manifest_runtime = _validate_runtime(manifest["runtime"])
        if manifest_runtime != runtime:
            raise ValueError("runtime identity mismatch")
        _validate_git(manifest["git"])
        semantics = _require_object(manifest["semantics"], "semantics")
        if semantics != _semantics(accounting):
            raise ValueError("financial semantics mismatch")
        reconciliation = _require_object(manifest["reconciliation"], "reconciliation")
        reconciliation_fields = (
            SETTLEMENT_RECONCILIATION_FIELDS if accounting is not None else RECONCILIATION_FIELDS
        )
        if set(reconciliation) != reconciliation_fields or not all(
            type(passed) is bool for passed in reconciliation.values()
        ):
            raise ValueError("stored reconciliation gates are invalid")
        if not all(reconciliation.values()):
            raise ValueError("stored reconciliation gates are not all true")

        stored_config = _load_strict_json(artifact_payloads["config.json"], "canonical config")
        if stored_config != config.canonical:
            raise ValueError("canonical config artifact mismatch")
        if _sha256(_canonical_json(stored_config)) != config.config_sha256:
            raise ValueError("canonical config artifact checksum mismatch")
        files = _require_object(manifest["files"], "artifact checksum map")
        expected_hashed_names = expected_artifact_names - {"run_manifest.json"}
        if set(files) != expected_hashed_names:
            raise ValueError("artifact checksum map is incomplete")
        for name, expected in files.items():
            expected = _require_object(expected, f"artifact checksum entry {name}")
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
        _verify_snapshot(
            dataset_path,
            dataset_manifest["snapshot_id"],
            verify_parent=False,
        )
        return manifest
    except StrategyRunError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise StrategyRunError(f"corrupt immutable strategy run {target}: {exc}") from exc


def run_strategy_config(
    config_path: Path | str,
    *,
    project_root: Path | str | None = None,
    implementations: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], Any]] | None = None,
    implementation_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    composition_digest: str | None = None,
    settlement_schedule: SettlementSchedule | None = None,
) -> dict[str, str]:
    config = load_strategy_config(config_path)
    dataset_path, dataset_manifest, frame = _bound_snapshot(config)
    action_evidence = None
    accounting = None
    if dataset_manifest["schema_version"] in {4, 5}:
        if settlement_schedule is None:
            raise StrategyRunError(
                "action-aware dataset requires an explicit transfer-settlement mapping"
            )
        action_evidence = _verified_action_evidence(dataset_path, dataset_manifest)
        accounting = _settlement_accounting(action_evidence.digest, settlement_schedule)
    elif settlement_schedule is not None:
        raise StrategyRunError(
            "settlement schedule cannot be applied without admitted corporate-action evidence"
        )
    source_identity = (
        _effective_source_identity(project_root=project_root)
        if project_root is not None
        else _effective_source_identity()
    )
    source_sha256, source_files, runtime, git = source_identity
    identity = {
        "schema_version": 1,
        "config_sha256": config.config_sha256,
        "dataset_snapshot_id": dataset_manifest["snapshot_id"],
        "source_sha256": source_sha256,
        "runtime": runtime,
    }
    if composition_digest is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", composition_digest):
            raise StrategyRunError("composition digest must be a lowercase SHA-256 value")
        identity["composition_digest"] = composition_digest
    if accounting is not None:
        identity["accounting"] = accounting
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
            composition_digest,
            accounting,
        )
        return {
            "status": "NO_CHANGE",
            "run_id": run_id,
            "path": str(target),
            "config_sha256": config.config_sha256,
            "dataset_snapshot_id": dataset_manifest["snapshot_id"],
        }

    if action_evidence is not None:
        replay = replay_strategy(
            frame,
            config,
            implementations=implementations,
            implementation_parameters=implementation_parameters,
            corporate_action_evidence=action_evidence,
            settlement_schedule=settlement_schedule,
        )
    elif implementations:
        replay = replay_strategy(
            frame,
            config,
            implementations=implementations,
            implementation_parameters=implementation_parameters,
        )
    else:
        replay = replay_strategy(frame, config)
    provenance = {
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
    }
    if accounting is not None:
        provenance["accounting"] = accounting
    if implementations is not None and "report" in implementations:
        report_payload = {
            "title": config.template_parameters["instrument_display_name"],
            "metrics": copy.deepcopy(replay.metrics),
        }
        report_payload_before = _canonical_json(report_payload)
        report = implementations["report"](
            report_payload,
            dict((implementation_parameters or {})["report"]),
        )
        if _canonical_json(report_payload) != report_payload_before:
            raise StrategyRunError("custom report operator mutated its input payload")
    else:
        report = render_report(replay, config, provenance)
    output_root.mkdir(parents=True, exist_ok=True)
    output_root.chmod(0o755)
    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_root))
    try:
        _write_json(staging / "config.json", config.canonical)
        _write_csv(staging / "daily_replay.csv", replay.daily)
        _write_csv(staging / "events.csv", replay.events)
        _write_csv(staging / "trades.csv", replay.trades)
        if accounting is not None:
            _write_csv(staging / "account_events.csv", replay.account_events)
            _write_csv(staging / "account_trades.csv", replay.account_trades)
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
            "semantics": _semantics(accounting),
            "reconciliation": replay.reconciliation,
            "files": _file_manifest(
                staging,
                (SETTLEMENT_ARTIFACT_NAMES if accounting is not None else ARTIFACT_NAMES)
                - {"run_manifest.json"},
            ),
        }
        if accounting is not None:
            manifest["accounting"] = accounting
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
            composition_digest,
            accounting,
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
                composition_digest,
                accounting,
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
            composition_digest,
            accounting,
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
