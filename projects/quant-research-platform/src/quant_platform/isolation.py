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
RUNNER_CONTROL_FILENAMES = {
    "attempt.json",
    "container.cid",
    "stderr.log",
    "stdout.log",
}


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def build_operator_validation_command(
    candidate_dir: Path | str,
    cidfile: Path | str,
    runner_image: str,
) -> list[str]:
    """Build the fixed command that validates an operator only inside Docker."""

    raw_candidate = Path(candidate_dir)
    raw_cidfile = Path(cidfile)
    for path in (raw_candidate, raw_cidfile):
        if path.is_symlink():
            raise IsolationError(f"operator validation path cannot be a symlink: {path}")
    candidate = _resolved(raw_candidate)
    cidfile = _resolved(raw_cidfile)
    for path in (candidate, cidfile):
        _reject_protected(path)
        _reject_unsafe_mount_syntax(path)
    if not candidate.is_dir():
        raise IsolationError(f"operator candidate directory does not exist: {candidate}")
    if not cidfile.parent.is_dir() or cidfile.exists():
        raise IsolationError("operator validation cidfile location is unsafe")
    if not is_immutable_runner_image(runner_image):
        raise IsolationError("operator validator image must be pinned by SHA-256")
    container_name = (
        "quant-operator-validation-"
        + hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:24]
    )
    return [
        "docker",
        "run",
        "--rm",
        "--cidfile",
        str(cidfile),
        "--name",
        container_name,
        "--pull",
        "never",
        "--network",
        "none",
        "--cpus",
        str(EXECUTION_ENVELOPE["cpus"]),
        "--memory",
        f"{EXECUTION_ENVELOPE['memory_mib']}m",
        "--pids-limit",
        str(EXECUTION_ENVELOPE["pids_limit"]),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "1000:1000",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--env",
        "MPLCONFIGDIR=/tmp/matplotlib",
        "--env",
        "XDG_CACHE_HOME=/tmp/cache",
        "--mount",
        f"type=bind,src={candidate},dst=/operator,readonly",
        runner_image,
        "python",
        "-m",
        "quant_platform.operator_worker",
        "validate",
        "/operator",
    ]


def build_composed_execution_command(
    *,
    dataset_dir: Path | str,
    output_root: Path | str,
    composition_file: Path | str,
    config_file: Path | str,
    cidfile: Path | str,
    operator_bundles: dict[str, Path],
    runner_image: str,
) -> list[str]:
    raw_paths = [
        Path(dataset_dir),
        Path(output_root),
        Path(composition_file),
        Path(config_file),
        Path(cidfile),
        *operator_bundles.values(),
    ]
    for path in raw_paths:
        if path.is_symlink():
            raise IsolationError(f"composed execution path cannot be a symlink: {path}")
    paths = [_resolved(path) for path in raw_paths]
    for path in paths:
        _reject_protected(path)
        _reject_unsafe_mount_syntax(path)
    if not paths[0].is_dir() or not paths[1].is_dir():
        raise IsolationError("composed dataset and output paths must be directories")
    if not paths[2].is_file() or not paths[3].is_file():
        raise IsolationError("composed execution contracts must be regular files")
    if paths[4].exists():
        raise IsolationError("composed execution cidfile must not already exist")
    dataset_store = paths[0].parents[1]
    resolved_bundles = {
        slot: _resolved(bundle) for slot, bundle in operator_bundles.items()
    }
    for slot, bundle in resolved_bundles.items():
        if _paths_overlap(bundle, dataset_store):
            raise IsolationError(
                f"operator bundle for slot {slot} must not overlap the dataset store"
            )
        if not bundle.is_dir():
            raise IsolationError(f"operator bundle does not exist for slot {slot}")
    output_protected_paths = [
        ("the dataset store", dataset_store),
        ("the dataset", paths[0]),
        ("the composition contract", paths[2]),
        ("the config contract", paths[3]),
        ("runner CID/control evidence", paths[4]),
        *(
            (f"operator bundle for slot {slot}", bundle)
            for slot, bundle in resolved_bundles.items()
        ),
    ]
    for label, protected_path in output_protected_paths:
        if _paths_overlap(paths[1], protected_path):
            raise IsolationError(
                f"composed writable output mount must not overlap {label}"
            )
    try:
        _verify_snapshot(
            paths[0],
            paths[0].name,
            verify_parent=False,
        )
    except RuntimeError as exc:
        raise IsolationError(
            f"composed dataset binding integrity check failed: {exc}"
        ) from exc
    if not is_immutable_runner_image(runner_image):
        raise IsolationError("composed runner image must be pinned by SHA-256")
    container_name = (
        "quant-composition-"
        + hashlib.sha256(str(paths[2]).encode("utf-8")).hexdigest()[:32]
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--cidfile",
        str(paths[4]),
        "--name",
        container_name,
        "--pull",
        "never",
        "--network",
        "none",
        "--cpus",
        str(EXECUTION_ENVELOPE["cpus"]),
        "--memory",
        f"{EXECUTION_ENVELOPE['memory_mib']}m",
        "--pids-limit",
        str(EXECUTION_ENVELOPE["pids_limit"]),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "1000:1000",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--env",
        "MPLCONFIGDIR=/tmp/matplotlib",
        "--env",
        "XDG_CACHE_HOME=/tmp/cache",
        "--mount",
        (
            f"type=bind,src={paths[0]},"
            f"dst=/platform/datasets/{paths[0].parent.name}/{paths[0].name},readonly"
        ),
        "--mount",
        f"type=bind,src={paths[1]},dst=/artifacts",
        "--mount",
        f"type=bind,src={paths[2]},dst=/run-contract/composition.json,readonly",
        "--mount",
        f"type=bind,src={paths[3]},dst=/run-contract/config.json,readonly",
    ]
    for slot in sorted(operator_bundles):
        bundle = resolved_bundles[slot]
        command.extend(
            [
                "--mount",
                f"type=bind,src={bundle},dst=/operators/{slot},readonly",
            ]
        )
    command.extend(
        [
            runner_image,
            "python",
            "-m",
            "quant_platform.composition_worker",
            "/run-contract/composition.json",
            "/run-contract/config.json",
        ]
    )
    return command

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
    existing = {path.name for path in artifact_dir.iterdir()}
    controls = sorted(existing & RUNNER_CONTROL_FILENAMES)
    if controls:
        raise IsolationError(f"artifact directory contains runner control files: {controls}")
    if existing != {"payload"}:
        raise IsolationError("artifact attempt directory must contain only an empty payload")
    payload_dir = artifact_dir / "payload"
    if payload_dir.is_symlink() or not payload_dir.is_dir():
        raise IsolationError("artifact payload directory must be a regular directory")
    if any(payload_dir.iterdir()):
        raise IsolationError("artifact payload directory must be empty")

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
        _verify_snapshot(
            dataset_dir,
            expected_snapshot_id,
            verify_parent=False,
        )
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
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    container_name = f"quant-research-{run_manifest['run_id'][:32]}"

    return [
        "docker",
        "run",
        "--rm",
        "--cidfile",
        str(artifact_dir / "container.cid"),
        "--name",
        container_name,
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
        f"type=bind,src={payload_dir},dst=/artifacts",
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
