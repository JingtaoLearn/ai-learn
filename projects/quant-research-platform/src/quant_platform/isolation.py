from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from .datasets import _verify_snapshot
from .submissions import EXECUTION_ENVELOPE
from .submissions import _verify_submission
from .submissions import is_immutable_runner_image


PROTECTED_FENG_PATHS = (
    Path("/home/feng/abc-trend-strategy"),
    Path("/home/feng/quant-research"),
    Path("/var/run/docker.sock"),
    Path("/run/docker.sock"),
)
SAFE_ATTEMPT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class IsolationError(ValueError):
    """Raised when a run would escape the fixed research sandbox."""


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _reject_protected(path: Path) -> None:
    for protected in PROTECTED_FENG_PATHS:
        protected = protected.resolve(strict=False)
        if (
            path == protected
            or path.is_relative_to(protected)
            or protected.is_relative_to(path)
        ):
            raise IsolationError(f"protected Feng path cannot be mounted: {path}")


def _reject_unsafe_mount_syntax(path: Path) -> None:
    value = str(path)
    if "," in value or "\n" in value or "\r" in value:
        raise IsolationError(f"path cannot be represented safely as a Docker mount: {path}")


def _canonical_json(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_run_manifest(
    platform_root: Path,
    submission_id: str,
    dataset_snapshot_id: str,
    runner_image: str,
    artifact_dir: Path,
) -> Path:
    identity = {
        "schema_version": 1,
        "submission_id": submission_id,
        "dataset_snapshot_id": dataset_snapshot_id,
        "runner_image": runner_image,
        "execution_envelope": EXECUTION_ENVELOPE,
        "attempt_id": artifact_dir.name,
        "artifact_path": artifact_dir.relative_to(platform_root).as_posix(),
    }
    run_id = hashlib.sha256(_canonical_json(identity)).hexdigest()
    manifest = identity | {"run_id": run_id}
    runs_root = platform_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    runs_root.chmod(0o755)
    target = runs_root / run_id
    manifest_path = target / "run.json"
    if target.exists():
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IsolationError(f"existing run manifest is corrupt: {run_id}") from exc
        if current != manifest:
            raise IsolationError(f"existing run manifest identity mismatch: {run_id}")
        return manifest_path

    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=runs_root))
    try:
        temporary_manifest = temporary / "run.json"
        with temporary_manifest.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        temporary_manifest.chmod(0o644)
        _fsync_directory(temporary)
        try:
            os.rename(temporary, target)
        except OSError:
            if not target.exists():
                raise
        _fsync_directory(runs_root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    try:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IsolationError(f"published run manifest is corrupt: {run_id}") from exc
    if current != manifest:
        raise IsolationError(f"published run manifest identity mismatch: {run_id}")
    return manifest_path


def build_docker_command(
    submission_dir: Path | str,
    dataset_dir: Path | str,
    artifact_dir: Path | str,
) -> list[str]:
    """Build, but do not execute, the only allowed research-container command."""

    raw_paths = (Path(submission_dir), Path(dataset_dir), Path(artifact_dir))
    for raw_path in raw_paths:
        if raw_path.is_symlink():
            raise IsolationError(f"sandbox mount path cannot be a symlink: {raw_path}")
    submission_dir = _resolved(raw_paths[0])
    dataset_dir = _resolved(raw_paths[1])
    artifact_dir = _resolved(raw_paths[2])
    for path in (submission_dir, dataset_dir, artifact_dir):
        _reject_protected(path)
        _reject_unsafe_mount_syntax(path)

    if not submission_dir.is_dir() or not (submission_dir / "source").is_dir():
        raise IsolationError("submission source is incomplete")
    if not dataset_dir.is_dir():
        raise IsolationError("dataset directory does not exist")
    if not artifact_dir.is_dir():
        raise IsolationError("artifact directory does not exist")

    if submission_dir.parent.name != "submissions":
        raise IsolationError("submission directory is outside the platform store")
    platform_root = submission_dir.parent.parent
    if dataset_dir.parent.parent.parent != platform_root or dataset_dir.parents[1].name != "datasets":
        raise IsolationError("dataset directory is outside the submission platform store")
    artifact_root = platform_root / "artifacts"
    if (
        artifact_dir.parents[1] != artifact_root
        or artifact_dir.parent.name != submission_dir.name
        or not SAFE_ATTEMPT_NAME.fullmatch(artifact_dir.name)
    ):
        raise IsolationError(
            "artifact directory must be <platform>/artifacts/<submission-id>/<attempt-id>"
        )
    if any(artifact_dir.iterdir()):
        raise IsolationError("artifact attempt directory must be empty")

    try:
        manifest = _verify_submission(submission_dir, submission_dir.name)
    except RuntimeError as exc:
        raise IsolationError(f"submission integrity check failed: {exc}") from exc
    expected_snapshot_id = manifest["dataset_snapshot_id"]
    if dataset_dir.name != expected_snapshot_id:
        raise IsolationError(
            f"dataset binding mismatch: expected {expected_snapshot_id}, got {dataset_dir.name}"
        )
    try:
        _verify_snapshot(dataset_dir, expected_snapshot_id)
    except RuntimeError as exc:
        raise IsolationError(f"dataset binding integrity check failed: {exc}") from exc
    if manifest.get("execution_envelope") != EXECUTION_ENVELOPE:
        raise IsolationError("submission integrity check failed: execution envelope was modified")
    image = manifest.get("runner_image")
    if not isinstance(image, str) or not is_immutable_runner_image(image):
        raise IsolationError("submission integrity check failed: runner image is not digest pinned")
    entrypoint = manifest.get("spec", {}).get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise IsolationError("submission entrypoint is missing")
    pure_entrypoint = PurePosixPath(entrypoint)
    if pure_entrypoint.is_absolute() or ".." in pure_entrypoint.parts:
        raise IsolationError("submission entrypoint is unsafe")
    source_entrypoint = submission_dir / "source" / Path(*pure_entrypoint.parts)
    if not source_entrypoint.is_file() or source_entrypoint.is_symlink():
        raise IsolationError("submission entrypoint is unavailable")
    run_manifest_path = _prepare_run_manifest(
        platform_root,
        submission_dir.name,
        expected_snapshot_id,
        image,
        artifact_dir,
    )

    return [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--cpus",
        "1.0",
        "--memory",
        "512m",
        "--pids-limit",
        "256",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "1000:1000",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--mount",
        f"type=bind,src={submission_dir / 'source'},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={dataset_dir},dst=/data,readonly",
        "--mount",
        f"type=bind,src={artifact_dir},dst=/artifacts",
        "--mount",
        f"type=bind,src={submission_dir / 'submission.json'},dst=/run-contract/submission.json,readonly",
        "--mount",
        f"type=bind,src={run_manifest_path},dst=/run-contract/run.json,readonly",
        "--workdir",
        "/workspace",
        image,
        "python",
        f"/workspace/{pure_entrypoint.as_posix()}",
        "--dataset=/data",
        "--submission=/run-contract/submission.json",
        "--run-contract=/run-contract/run.json",
        "--artifacts=/artifacts",
    ]
