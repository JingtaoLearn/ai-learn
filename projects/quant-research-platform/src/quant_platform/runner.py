from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .datasets import _atomic_json, _fsync_directory
from .isolation import (
    RUNNER_CONTROL_FILENAMES,
    SAFE_ATTEMPT_NAME,
    _prepare_run_manifest,
    build_docker_command,
)
from .submissions import EXECUTION_ENVELOPE, _bound_snapshot, _verify_submission


class RunnerIntegrityError(RuntimeError):
    """Raised after sealing an attempt containing unsafe or unstable artifacts."""


class RunnerCallbackError(RuntimeError):
    """Raised when terminal record delivery fails after an attempt is sealed."""


class RunnerTerminationError(RuntimeError):
    """Raised when Docker container termination cannot be confirmed."""


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


def _walk_tree(directory: Path) -> list[Path]:
    paths: list[Path] = []

    def visit(current: Path) -> None:
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise RunnerIntegrityError(
                f"attempt tree is inaccessible: {current.relative_to(directory)}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            paths.append(path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RunnerIntegrityError(
                    f"attempt entry is inaccessible: {path.relative_to(directory)}"
                ) from exc
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                visit(path)

    visit(directory)
    return paths


def _prepare_payload_for_validation(payload_dir: Path) -> None:
    def visit(directory: Path) -> None:
        try:
            directory.chmod(0o700)
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as exc:
            raise RunnerIntegrityError(
                f"artifact payload is inaccessible: {directory}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RunnerIntegrityError(
                    f"artifact payload is inaccessible: {path}"
                ) from exc
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                try:
                    path.chmod(0o600)
                except OSError as exc:
                    raise RunnerIntegrityError(
                        f"artifact payload is inaccessible: {path}"
                    ) from exc

    visit(payload_dir)


def _hash_attempt_files(attempt_dir: Path) -> dict[str, dict[str, str | int]]:
    files: dict[str, dict[str, str | int]] = {}
    for path in _walk_tree(attempt_dir):
        relative = path.relative_to(attempt_dir)
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if relative.as_posix() == "attempt.json":
            continue
        digest, size = _read_regular_file(path, attempt_dir)
        files[relative.as_posix()] = {"sha256": digest, "size": size}
    return files


def _special_file_reason(mode: int) -> str | None:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
        return "special"
    return None


def _remove_rejected_payload_entries(
    payload_dir: Path, attempt_dir: Path
) -> list[dict[str, str]]:
    rejected: list[dict[str, str]] = []
    paths = sorted(_walk_tree(payload_dir), key=lambda item: len(item.parts), reverse=True)
    metadata_by_path = {
        path: path.stat(follow_symlinks=False)
        for path in paths
    }
    multiply_linked = {
        (metadata.st_dev, metadata.st_ino)
        for metadata in metadata_by_path.values()
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1
    }
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        metadata = metadata_by_path[path]
        reason = "reserved" if path.name in RUNNER_CONTROL_FILENAMES else _special_file_reason(
            metadata.st_mode
        )
        if reason is None and (metadata.st_dev, metadata.st_ino) in multiply_linked:
            reason = "hardlink"
        if reason is None:
            continue
        rejected.append(
            {"path": path.relative_to(attempt_dir).as_posix(), "reason": reason}
        )
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    return sorted(rejected, key=lambda value: value["path"])


def _replace_payload_with_empty(payload_dir: Path) -> None:
    def repair(function, path, exc_info) -> None:
        candidate = Path(path)
        if candidate.is_symlink():
            candidate.unlink()
            return
        candidate.chmod(0o700 if candidate.is_dir() else 0o600)
        function(path)

    try:
        payload_dir.chmod(0o700)
        shutil.rmtree(payload_dir, onexc=repair)
        payload_dir.mkdir(mode=0o700)
    except OSError as exc:
        raise RunnerIntegrityError(
            "artifact payload could not be safely replaced"
        ) from exc


def _seal_attempt(attempt_dir: Path) -> None:
    for path in sorted(_walk_tree(attempt_dir), key=lambda item: len(item.parts), reverse=True):
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            path.chmod(0o444)
        elif stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o555)
    attempt_dir.chmod(0o555)
    _fsync_directory(attempt_dir)


def _verify_sealed_attempt(
    attempt_dir: Path,
    expected_files: dict[str, dict[str, str | int]],
    expected_manifest: dict[str, Any],
) -> None:
    if attempt_dir.stat().st_mode & 0o222:
        raise RunnerIntegrityError("sealed attempt directory remains writable")
    for path in _walk_tree(attempt_dir):
        metadata = path.stat(follow_symlinks=False)
        relative = path.relative_to(attempt_dir)
        if stat.S_ISLNK(metadata.st_mode) or (
            not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(metadata.st_mode)
        ):
            raise RunnerIntegrityError(f"sealed attempt contains unsafe entry: {relative}")
        if metadata.st_mode & 0o222:
            raise RunnerIntegrityError(f"sealed attempt remains writable: {relative}")
        if relative.parts[0] == "payload" and path.name in RUNNER_CONTROL_FILENAMES:
            raise RunnerIntegrityError(
                f"sealed payload contains reserved control filename: {relative}"
            )
    if _hash_attempt_files(attempt_dir) != expected_files:
        raise RunnerIntegrityError("sealed attempt checksums do not match its manifest")
    manifest_path = attempt_dir / "attempt.json"
    expected_payload = (
        json.dumps(
            expected_manifest,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode()
    try:
        before = manifest_path.stat(follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise RunnerIntegrityError("sealed attempt manifest is not a regular file")
        if before.st_nlink != 1:
            raise RunnerIntegrityError("sealed attempt manifest has a hard link")
        descriptor = os.open(
            manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        after = manifest_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RunnerIntegrityError("sealed attempt manifest is unreadable") from exc

    def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
        )

    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise RunnerIntegrityError("sealed attempt manifest changed while reading")
    if b"".join(chunks) != expected_payload:
        raise RunnerIntegrityError("sealed attempt manifest identity changed")


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


def _docker_control(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=False,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )


def _authoritative_absence(stderr: str) -> bool:
    normalized = stderr.lower()
    return "no such object" in normalized or "no such container" in normalized


def _terminate_container(
    cidfile: Path, container_name: str, process: Any
) -> int | None:
    container_id: str | None = None
    try:
        candidate = cidfile.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{64}", candidate):
            container_id = candidate
    except OSError:
        pass
    identifier = container_id or container_name
    control_error: Exception | None = None
    try:
        _docker_control(["docker", "kill", identifier])
    except (OSError, subprocess.TimeoutExpired) as exc:
        control_error = exc
    if container_id is None or control_error is not None:
        exit_status = _terminate_process_group(process)
    else:
        try:
            exit_status = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            exit_status = _terminate_process_group(process)

    if container_id is None:
        raise RunnerTerminationError(
            "Docker did not provide an immutable container ID; removal is ambiguous"
        )

    absent_checks = 0
    for _ in range(50):
        try:
            inspected = _docker_control(["docker", "inspect", container_id])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunnerTerminationError(
                "Docker could not confirm container removal"
            ) from exc
        if inspected.returncode == 0:
            absent_checks = 0
            _docker_control(["docker", "kill", container_id])
        elif _authoritative_absence(inspected.stderr):
            absent_checks += 1
            if absent_checks >= 3:
                return exit_status
        else:
            raise RunnerTerminationError(
                "Docker could not confirm container removal: "
                f"{inspected.stderr.strip() or 'unknown inspect error'}"
            )
        time.sleep(0.1)
    raise RunnerTerminationError(
        f"Docker container termination was not confirmed: {container_id}"
    )


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
    container_terminator: Callable[[Path, str, Any], int | None] = _terminate_container,
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
    payload_dir = attempt_dir / "payload"
    payload_dir.mkdir()
    command = build_docker_command(submission_dir, dataset_dir, attempt_dir)
    container_name = command[command.index("--name") + 1]
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
                exit_status = container_terminator(
                    attempt_dir / "container.cid", container_name, process
                )
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
    rejected_entries: list[dict[str, str]] = []
    try:
        _prepare_payload_for_validation(payload_dir)
        rejected_entries = _remove_rejected_payload_entries(payload_dir, attempt_dir)
    except RunnerIntegrityError as exc:
        integrity_error = exc
        rejected_entries = [{"path": "payload", "reason": "inaccessible"}]
        _replace_payload_with_empty(payload_dir)
    if rejected_entries:
        rendered = ", ".join(
            f"{entry['path']} ({entry['reason']})" for entry in rejected_entries
        )
        if integrity_error is None:
            integrity_error = RunnerIntegrityError(
                f"artifact payload contains rejected entries: {rendered}"
            )
        outcome = "ARTIFACT_REJECTED"
        error_type = type(integrity_error).__name__
    try:
        files = _hash_attempt_files(attempt_dir)
        verified_files = _hash_attempt_files(attempt_dir)
        if verified_files != files:
            integrity_error = RunnerIntegrityError(
                "artifact files changed after the process exited"
            )
            rejected_entries.append({"path": "payload", "reason": "changed"})
            files = verified_files
            outcome = "ARTIFACT_REJECTED"
            error_type = type(integrity_error).__name__
    except RunnerIntegrityError as exc:
        integrity_error = exc
        outcome = "ARTIFACT_REJECTED"
        error_type = type(exc).__name__
        _replace_payload_with_empty(payload_dir)
        rejected_entries.append({"path": "payload", "reason": "inaccessible"})
        files = _hash_attempt_files(attempt_dir)

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
        "rejected_entries": rejected_entries,
    }
    _atomic_json(attempt_dir / "attempt.json", attempt_manifest)
    _seal_attempt(attempt_dir)
    _verify_sealed_attempt(attempt_dir, files, attempt_manifest)

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
