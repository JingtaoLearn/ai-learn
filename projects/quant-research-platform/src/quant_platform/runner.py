from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .datasets import _atomic_json, _fsync_directory
from .isolation import SAFE_ATTEMPT_NAME, _prepare_run_manifest, build_docker_command
from .submissions import EXECUTION_ENVELOPE, _bound_snapshot, _verify_submission


class RunnerIntegrityError(RuntimeError):
    """Raised after sealing an attempt containing unsafe or unstable artifacts."""


class RunnerCallbackError(RuntimeError):
    """Raised when terminal record delivery fails after an attempt is sealed."""


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


def _read_regular_file(path: Path, attempt_dir: Path) -> tuple[str, int]:
    relative = path.relative_to(attempt_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise RunnerIntegrityError(f"artifact path traversal is not allowed: {relative}")
    try:
        before = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise RunnerIntegrityError(f"artifact symlink is not allowed: {relative}")
        if not stat.S_ISREG(before.st_mode):
            raise RunnerIntegrityError(f"artifact must be a regular file: {relative}")
        if before.st_nlink != 1:
            raise RunnerIntegrityError(f"artifact hard link is not allowed: {relative}")
        if not path.resolve(strict=True).is_relative_to(attempt_dir):
            raise RunnerIntegrityError(f"artifact escapes attempt directory: {relative}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            digest = hashlib.sha256()
            size = 0
            while payload := os.read(descriptor, 1024 * 1024):
                digest.update(payload)
                size += len(payload)
        finally:
            os.close(descriptor)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RunnerIntegrityError(f"artifact changed while hashing: {relative}") from exc

    def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
        )

    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise RunnerIntegrityError(f"artifact changed while hashing: {relative}")
    if size != opened.st_size:
        raise RunnerIntegrityError(f"artifact size changed while hashing: {relative}")
    return digest.hexdigest(), size


def _hash_attempt_files(attempt_dir: Path) -> dict[str, dict[str, str | int]]:
    files: dict[str, dict[str, str | int]] = {}
    for path in sorted(attempt_dir.rglob("*")):
        relative = path.relative_to(attempt_dir)
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if relative.as_posix() == "attempt.json":
            continue
        digest, size = _read_regular_file(path, attempt_dir)
        files[relative.as_posix()] = {"sha256": digest, "size": size}
    return files


def _seal_attempt(attempt_dir: Path) -> None:
    for path in sorted(attempt_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            path.chmod(0o444)
        elif stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o555)
    attempt_dir.chmod(0o555)
    _fsync_directory(attempt_dir)


def _terminate_process_group(process: Any) -> int | None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait()


def _validate_timeout(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")
    return float(timeout_seconds)


def run_submission(
    root: Path | str,
    submission_id: str,
    attempt_id: str,
    timeout_seconds: float,
    *,
    process_launcher: Callable[..., Any] = subprocess.Popen,
    clock: Clock | None = None,
    callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute one verified submission and seal its terminal attempt evidence."""

    timeout = _validate_timeout(timeout_seconds)
    if not isinstance(attempt_id, str) or not SAFE_ATTEMPT_NAME.fullmatch(attempt_id):
        raise ValueError("attempt_id must be a safe 1-64 character identifier")
    root = Path(root).resolve()
    submission_dir = root / "submissions" / submission_id
    manifest = _verify_submission(submission_dir, submission_id)
    dataset_dir = _bound_snapshot(root, manifest["dataset_snapshot_id"])

    attempt_dir = root / "artifacts" / submission_id / attempt_id
    attempt_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        attempt_dir.mkdir()
    except FileExistsError as exc:
        raise FileExistsError(f"attempt already exists: {attempt_id}") from exc
    attempt_dir.chmod(0o755)
    command = build_docker_command(submission_dir, dataset_dir, attempt_dir)
    run_contract_path = _prepare_run_manifest(
        root,
        submission_id,
        manifest["dataset_snapshot_id"],
        manifest["runner_image"],
        attempt_dir,
    )
    run_contract = json.loads(run_contract_path.read_text(encoding="utf-8"))

    active_clock = clock or SystemClock()
    started_at = active_clock.now()
    started_monotonic = active_clock.monotonic()
    outcome = "LAUNCH_FAILED"
    exit_status: int | None = None
    error_type: str | None = None
    stdout_path = attempt_dir / "stdout.log"
    stderr_path = attempt_dir / "stderr.log"
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        try:
            process = process_launcher(
                command,
                shell=False,
                stdout=stdout,
                stderr=stderr,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                start_new_session=True,
                close_fds=True,
            )
            try:
                exit_status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                exit_status = _terminate_process_group(process)
                outcome = "TIMED_OUT"
            else:
                outcome = "SUCCESS" if exit_status == 0 else "FAILED"
        except OSError as exc:
            error_type = type(exc).__name__
        finally:
            for stream in (stdout, stderr):
                stream.flush()
                os.fsync(stream.fileno())

    finished_at = active_clock.now()
    duration_seconds = active_clock.monotonic() - started_monotonic
    integrity_error: RunnerIntegrityError | None = None
    files: dict[str, dict[str, str | int]]
    try:
        files = _hash_attempt_files(attempt_dir)
        if _hash_attempt_files(attempt_dir) != files:
            raise RunnerIntegrityError("artifact files changed after the process exited")
    except RunnerIntegrityError as exc:
        integrity_error = exc
        outcome = "ARTIFACT_REJECTED"
        error_type = type(exc).__name__
        try:
            files = _hash_attempt_files(attempt_dir)
        except RunnerIntegrityError:
            files = {}

    attempt_manifest = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "run_id": run_contract["run_id"],
        "submission_id": submission_id,
        "dataset_snapshot_id": manifest["dataset_snapshot_id"],
        "runner_image": manifest["runner_image"],
        "execution_envelope": EXECUTION_ENVELOPE,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
        "exit_status": exit_status,
        "outcome": outcome,
        "error_type": error_type,
        "files": files,
    }
    _atomic_json(attempt_dir / "attempt.json", attempt_manifest)
    _seal_attempt(attempt_dir)

    terminal_record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "run_id": run_contract["run_id"],
        "submission_id": submission_id,
        "dataset_snapshot_id": manifest["dataset_snapshot_id"],
        "outcome": outcome,
        "path": str(attempt_dir),
    }
    if callback is not None:
        try:
            callback(terminal_record.copy())
        except Exception as exc:
            raise RunnerCallbackError(
                f"terminal callback failed after attempt {attempt_id} was sealed"
            ) from exc
    if integrity_error is not None:
        raise integrity_error
    return terminal_record
