from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .datasets import _verify_snapshot


REQUIRED_FIELDS = {
    "name",
    "entrypoint",
    "dataset_snapshot_id",
    "runner_image",
    "config",
}
OPTIONAL_FIELDS = {"seed"}
ROOT_SOURCE_FILES = ("pyproject.toml", "requirements.in", "requirements.lock")
SOURCE_DIRECTORIES = ("src", "tests", "scripts", "web")
IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".ipynb_checkpoints",
    "runs",
    "state",
    "data",
    "build",
    "dist",
}
SNAPSHOT_ID = re.compile(r"^[0-9a-f]{64}$")
REGISTRY_DIGEST_IMAGE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:=-]*@sha256:[0-9a-f]{64}$"
)
LOCAL_DOCKER_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
SECRET_FILENAMES = {
    ".env",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
}
SECRET_DIRECTORY_NAMES = {".ssh", ".aws", ".azure", ".gnupg"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
PRIVATE_KEY_MARKERS = tuple(
    b"-----BEGIN " + suffix
    for suffix in (
        b"PRIVATE KEY-----",
        b"RSA PRIVATE KEY-----",
        b"OPENSSH PRIVATE KEY-----",
    )
)
HARDCODED_SECRET = re.compile(
    rb"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]"
)
EXECUTION_ENVELOPE = {
    "cap_drop": ["ALL"],
    "cpus": 1.0,
    "memory_mib": 512,
    "network": "none",
    "no_new_privileges": True,
    "pids_limit": 256,
    "read_only_root": True,
}


class SubmissionValidationError(ValueError):
    """Raised when an experiment cannot be submitted safely or reproducibly."""


def is_immutable_runner_image(value: str) -> bool:
    """Return whether an image is a registry digest or full local Docker image ID."""

    return bool(
        REGISTRY_DIGEST_IMAGE.fullmatch(value)
        or LOCAL_DOCKER_IMAGE_ID.fullmatch(value)
    )


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


def _validate_json_value(value: Any, path: str = "config") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SubmissionValidationError(f"{path} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SubmissionValidationError(f"{path} object keys must be strings")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise SubmissionValidationError(
        f"{path} must use standard JSON types, not {type(value).__name__}"
    )


def _is_secret_like(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in SECRET_FILENAMES
        or name in SECRET_DIRECTORY_NAMES
        or name.startswith(".env.")
        or name.startswith("secrets.")
        or path.suffix.lower() in SECRET_SUFFIXES
    )


def _scan_source_payload(relative: str, payload: bytes) -> None:
    if any(marker in payload for marker in PRIVATE_KEY_MARKERS):
        raise SubmissionValidationError(f"secret-like private key content is not allowed: {relative}")
    if HARDCODED_SECRET.search(payload):
        raise SubmissionValidationError(f"secret-like credential content is not allowed: {relative}")


def _validate_project(project_root: Path) -> None:
    if not project_root.is_dir():
        raise SubmissionValidationError(f"project root does not exist: {project_root}")
    for base, directories, files in os.walk(project_root, followlinks=False):
        base_path = Path(base)
        for name in [*directories, *files]:
            candidate = base_path / name
            if _is_secret_like(candidate):
                raise SubmissionValidationError(
                    f"secret-like file is not allowed: {candidate.relative_to(project_root)}"
                )
        directories[:] = [
            name
            for name in directories
            if name not in IGNORED_DIRECTORIES and not name.endswith(".egg-info")
        ]


def _bound_snapshot(root: Path, snapshot_id: str) -> Path:
    matches = sorted((root / "datasets").glob(f"*/{snapshot_id}"))
    if len(matches) != 1 or matches[0].is_symlink():
        raise SubmissionValidationError(f"dataset snapshot is not available: {snapshot_id}")
    snapshot = matches[0]
    try:
        _verify_snapshot(snapshot, snapshot_id)
    except RuntimeError as exc:
        raise SubmissionValidationError(f"dataset snapshot integrity check failed: {exc}") from exc
    return snapshot


def _validate_spec(spec: dict[str, Any], project_root: Path, root: Path) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise SubmissionValidationError("specification must be a JSON object")
    fields = set(spec)
    unknown = fields - REQUIRED_FIELDS - OPTIONAL_FIELDS
    missing = REQUIRED_FIELDS - fields
    if unknown:
        raise SubmissionValidationError(f"unknown specification fields: {sorted(unknown)}")
    if missing:
        raise SubmissionValidationError(f"missing specification fields: {sorted(missing)}")
    if not isinstance(spec["name"], str) or not spec["name"].strip():
        raise SubmissionValidationError("name must be a non-empty string")
    if not isinstance(spec["config"], dict):
        raise SubmissionValidationError("config must be a JSON object")
    _validate_json_value(spec["config"])
    if "seed" in spec and (isinstance(spec["seed"], bool) or not isinstance(spec["seed"], int)):
        raise SubmissionValidationError("seed must be an integer")

    entrypoint = spec["entrypoint"]
    if not isinstance(entrypoint, str) or not entrypoint:
        raise SubmissionValidationError("entrypoint must be a non-empty relative path")
    pure_entrypoint = PurePosixPath(entrypoint)
    if pure_entrypoint.is_absolute():
        raise SubmissionValidationError("entrypoint must be relative")
    if ".." in pure_entrypoint.parts:
        raise SubmissionValidationError("entrypoint path traversal is not allowed")
    entrypoint_path = project_root.joinpath(*pure_entrypoint.parts)
    if not entrypoint_path.exists():
        raise SubmissionValidationError(f"entrypoint does not exist: {entrypoint}")
    if entrypoint_path.is_symlink():
        raise SubmissionValidationError("entrypoint cannot be a symlink")
    if not entrypoint_path.is_file():
        raise SubmissionValidationError("entrypoint must be a file")
    try:
        entrypoint_path.resolve().relative_to(project_root)
    except ValueError as exc:
        raise SubmissionValidationError("entrypoint escapes project root") from exc

    snapshot_id = spec["dataset_snapshot_id"]
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise SubmissionValidationError("dataset_snapshot_id must be a SHA-256 value")
    _bound_snapshot(root, snapshot_id)

    runner_image = spec["runner_image"]
    if not isinstance(runner_image, str) or not is_immutable_runner_image(runner_image):
        raise SubmissionValidationError("runner_image must be pinned by a sha256 digest")

    normalized = {
        "name": spec["name"].strip(),
        "entrypoint": pure_entrypoint.as_posix(),
        "dataset_snapshot_id": snapshot_id,
        "runner_image": runner_image,
        "config": json.loads(_canonical_json(spec["config"])),
    }
    if "seed" in spec:
        normalized["seed"] = spec["seed"]
    _validate_json_value(normalized, "spec")
    return normalized


def _source_paths(project_root: Path) -> list[Path]:
    paths: list[Path] = []
    for relative in ROOT_SOURCE_FILES:
        path = project_root / relative
        if path.is_symlink():
            raise SubmissionValidationError(f"source bundle cannot contain symlink: {relative}")
        if not path.is_file():
            raise SubmissionValidationError(f"required replay file is missing: {relative}")
        paths.append(path)

    for directory_name in SOURCE_DIRECTORIES:
        directory = project_root / directory_name
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise SubmissionValidationError(f"source bundle cannot contain symlink: {directory_name}")
        for base, directories, files in os.walk(directory, followlinks=False):
            base_path = Path(base)
            for name in directories:
                candidate = base_path / name
                if candidate.is_symlink():
                    raise SubmissionValidationError(
                        f"source bundle cannot contain symlink: {candidate.relative_to(project_root)}"
                    )
            directories[:] = [
                name
                for name in directories
                if name not in IGNORED_DIRECTORIES and not name.endswith(".egg-info")
            ]
            for name in files:
                path = base_path / name
                relative = path.relative_to(project_root)
                if path.is_symlink():
                    raise SubmissionValidationError(
                        f"source bundle cannot contain symlink: {relative}"
                    )
                if _is_secret_like(path):
                    raise SubmissionValidationError(f"secret-like file is not allowed: {relative}")
                if path.is_file():
                    paths.append(path)
    return sorted(set(paths), key=lambda path: path.relative_to(project_root).as_posix())


def _read_source_file(path: Path, project_root: Path) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(project_root):
            raise SubmissionValidationError(
                f"source bundle path escaped or became a symlink: {path.relative_to(project_root)}"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read()
        finally:
            os.close(descriptor)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SubmissionValidationError(f"source file changed while copying: {path}") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_opened = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_opened or identity_opened != identity_after:
        raise SubmissionValidationError(f"source file changed while copying: {path}")
    _scan_source_payload(path.relative_to(project_root).as_posix(), payload)
    return payload


def _copy_source_bundle(project_root: Path, paths: list[Path], source_root: Path) -> None:
    for source in paths:
        relative = source.relative_to(project_root)
        payload = _read_source_file(source, project_root)
        destination = source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())


def _source_identity(project_root: Path, paths: list[Path]) -> tuple[str, dict[str, str]]:
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    for path in paths:
        relative = path.relative_to(project_root).as_posix()
        payload = path.read_bytes()
        _scan_source_payload(relative, payload)
        file_digest = _sha256(payload)
        files[relative] = file_digest
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    if not files:
        raise SubmissionValidationError("source bundle is empty")
    return digest.hexdigest(), files


def _verify_submission(
    target: Path, expected_submission_id: str, *, require_directory_name: bool = True
) -> dict[str, Any]:
    try:
        manifest = json.loads(
            (target / "submission.json").read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        expected_manifest_fields = {
            "schema_version",
            "spec",
            "source_sha256",
            "execution_envelope",
            "submission_id",
            "dataset_snapshot_id",
            "runner_image",
            "source_files",
        }
        if set(manifest) != expected_manifest_fields:
            raise ValueError("unexpected or missing manifest fields")
        if manifest.get("submission_id") != expected_submission_id:
            raise ValueError("submission identity mismatch")
        if require_directory_name and target.name != expected_submission_id:
            raise ValueError("submission directory identity mismatch")
        source_root = target / "source"
        expected_files = manifest["source_files"]
        if not isinstance(expected_files, dict) or not expected_files:
            raise ValueError("source file manifest is empty")
        actual_paths: list[Path] = []
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"source bundle contains symlink: {path.relative_to(source_root)}")
            if path.is_file():
                actual_paths.append(path)
        source_sha256, source_files = _source_identity(source_root, actual_paths)
        if source_files != expected_files:
            raise ValueError("source file checksums do not match")
        if source_sha256 != manifest["source_sha256"]:
            raise ValueError("aggregate source checksum does not match")
        if manifest["dataset_snapshot_id"] != manifest["spec"]["dataset_snapshot_id"]:
            raise ValueError("dataset identity mismatch")
        if manifest["runner_image"] != manifest["spec"]["runner_image"]:
            raise ValueError("runner image identity mismatch")
        if not is_immutable_runner_image(manifest["runner_image"]):
            raise ValueError("runner image is not digest pinned")
        if manifest["execution_envelope"] != EXECUTION_ENVELOPE:
            raise ValueError("execution envelope does not match the fixed policy")
        spec_fields = set(manifest["spec"])
        if not REQUIRED_FIELDS.issubset(spec_fields) or spec_fields - REQUIRED_FIELDS - OPTIONAL_FIELDS:
            raise ValueError("submission specification fields are invalid")
        _validate_json_value(manifest["spec"], "spec")
        if not set(ROOT_SOURCE_FILES).issubset(expected_files):
            raise ValueError("required replay files are missing from the bundle")
        identity = {
            "schema_version": manifest["schema_version"],
            "spec": manifest["spec"],
            "source_sha256": manifest["source_sha256"],
            "execution_envelope": manifest["execution_envelope"],
        }
        if _sha256(_canonical_json(identity)) != expected_submission_id:
            raise ValueError("submission ID does not match its identity inputs")
        entrypoint = source_root / Path(*PurePosixPath(manifest["spec"]["entrypoint"]).parts)
        if not entrypoint.is_file() or entrypoint.is_symlink():
            raise ValueError("entrypoint is not present in the source bundle")
        platform_root = target.parent.parent
        _bound_snapshot(platform_root, manifest["dataset_snapshot_id"])
        return manifest
    except (KeyError, OSError, SubmissionValidationError, TypeError, ValueError) as exc:
        raise RuntimeError(f"corrupt submission {target}: {exc}") from exc


def _chmod_tree(directory: Path) -> None:
    directory.chmod(0o755)
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise SubmissionValidationError(f"temporary bundle contains symlink: {path}")
        path.chmod(0o755 if path.is_dir() else 0o644)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_submission(
    spec: dict[str, Any], project_root: Path | str, root: Path | str
) -> dict[str, str]:
    """Atomically freeze an experiment source bundle and its execution contract."""

    project_root = Path(project_root).resolve()
    root = Path(root).resolve()
    _validate_project(project_root)
    normalized_spec = _validate_spec(spec, project_root, root)
    source_paths = _source_paths(project_root)
    entrypoint_path = project_root / Path(*PurePosixPath(normalized_spec["entrypoint"]).parts)
    if entrypoint_path not in source_paths:
        raise SubmissionValidationError("entrypoint is not part of the allowlisted source bundle")

    submissions_root = root / "submissions"
    submissions_root.mkdir(parents=True, exist_ok=True)
    submissions_root.chmod(0o755)
    incoming = Path(tempfile.mkdtemp(prefix=".incoming.", dir=submissions_root))
    try:
        source_root = incoming / "source"
        _copy_source_bundle(project_root, source_paths, source_root)
        copied_paths = sorted(path for path in source_root.rglob("*") if path.is_file())
        source_sha256, source_files = _source_identity(source_root, copied_paths)
        identity = {
            "schema_version": 1,
            "spec": normalized_spec,
            "source_sha256": source_sha256,
            "execution_envelope": EXECUTION_ENVELOPE,
        }
        submission_id = _sha256(_canonical_json(identity))
        manifest = identity | {
            "submission_id": submission_id,
            "dataset_snapshot_id": normalized_spec["dataset_snapshot_id"],
            "runner_image": normalized_spec["runner_image"],
            "source_files": source_files,
        }
        manifest_path = incoming / "submission.json"
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    manifest,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        _chmod_tree(incoming)
        _verify_submission(incoming, submission_id, require_directory_name=False)

        target = submissions_root / submission_id
        if target.exists():
            _verify_submission(target, submission_id)
            status = "NO_CHANGE"
        else:
            try:
                os.rename(incoming, target)
            except FileExistsError:
                _verify_submission(target, submission_id)
                status = "NO_CHANGE"
            else:
                _fsync_directory(submissions_root)
                status = "CREATED"
                incoming = target
        _verify_submission(target, submission_id)
        return {"status": status, "submission_id": submission_id, "path": str(target)}
    finally:
        if incoming.exists() and incoming.name.startswith(".incoming."):
            shutil.rmtree(incoming)


def submission_status(root: Path | str, submission_id: str) -> dict[str, str]:
    """Return a complete immutable submission by ID."""

    if not SNAPSHOT_ID.fullmatch(submission_id):
        raise SubmissionValidationError("submission_id must be a SHA-256 value")
    target = Path(root).resolve() / "submissions" / submission_id
    if not target.is_dir() or not (target / "submission.json").is_file() or not (target / "source").is_dir():
        raise FileNotFoundError(f"submission does not exist or is incomplete: {submission_id}")
    _verify_submission(target, submission_id)
    return {"submission_id": submission_id, "path": str(target)}
