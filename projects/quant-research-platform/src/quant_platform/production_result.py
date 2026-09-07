from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .production_contract import SHA256, canonical_json_bytes
from .production_jobs import JobComputation


class ProductionResultError(RuntimeError):
    """Raised when a production result cannot be sealed or verified."""


RESULT_FILES = frozenset(
    {
        "result-manifest.json",
        "provider-response.bin",
        "normalized-snapshot.json",
        "action.json",
        "report.html",
        "notification.txt",
    }
)
MAX_RESULT_MEMBER_BYTES = 16 * 1024 * 1024


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def read_immutable(path: Path, *, maximum: int = MAX_RESULT_MEMBER_BYTES) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_mode & 0o222
        or before.st_size > maximum
    ):
        raise ProductionResultError(f"immutable result member is unsafe: {path.name}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise ProductionResultError(f"result member changed while opening: {path.name}")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 64 * 1024):
            total += len(chunk)
            if total > maximum:
                raise ProductionResultError(f"result member exceeds size limit: {path.name}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_identity(after) != _file_identity(opened) or total != after.st_size:
        raise ProductionResultError(f"result member changed while reading: {path.name}")
    current = os.stat(path, follow_symlinks=False)
    if _file_identity(current) != _file_identity(after):
        raise ProductionResultError(f"result member path changed while reading: {path.name}")
    return b"".join(chunks)


def _strict_json(payload: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ProductionResultError(f"duplicate {label} field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionResultError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProductionResultError(f"{label} must be an object")
    return value


class ProductionResultStore:
    """Create-once result module plus atomic private fixture report publication."""

    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        self.results_root = self.root / "results"
        self.publication_root = self.root / "publication"

    def _prepare(self) -> None:
        self.results_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.publication_root / "staging").mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.publication_root / "current").mkdir(parents=True, exist_ok=True, mode=0o755)
        (self.publication_root / "backups").mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.root, self.results_root, self.publication_root):
            if path.is_symlink() or not path.is_dir():
                raise ProductionResultError("result root is unsafe")

    @staticmethod
    def _artifact_payloads(computation: JobComputation) -> dict[str, bytes]:
        return {
            "provider-response.bin": computation.raw_bytes,
            "normalized-snapshot.json": computation.normalized_bytes,
            "action.json": canonical_json_bytes(computation.action),
            "report.html": computation.report_html,
            "notification.txt": computation.notification_bytes,
        }

    @staticmethod
    def _manifest_core(
        row: Mapping[str, Any], computation: JobComputation, payloads: Mapping[str, bytes]
    ) -> dict[str, Any]:
        files = {
            name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
            for name, payload in sorted(payloads.items())
        }
        return {
            "schema": "quantresearch-production-result/v1",
            "request_id": row["request_id"],
            "production_run_id": row["production_run_id"],
            "production_release_id": row["production_release_id"],
            "experiment_id": computation.experiment_id,
            "attempt_id": computation.attempt_id,
            "job_id": computation.job_id,
            "production_manifest_sha256": computation.production_manifest_sha256,
            "model_id": computation.model_id,
            "provider_request": {"method": "GET", "url": computation.provider_url},
            "provider_response_sha256": hashlib.sha256(computation.raw_bytes).hexdigest(),
            "dataset_snapshot_id": hashlib.sha256(computation.normalized_bytes).hexdigest(),
            "action_sha256": hashlib.sha256(canonical_json_bytes(computation.action)).hexdigest(),
            "report_filename": f"{computation.report_uuid}.html",
            "report_sha256": hashlib.sha256(computation.report_html).hexdigest(),
            "generated_at": computation.action["generated_at"],
            "automatic_ordering": False,
            "files": files,
        }

    def verify(self, result_id: str) -> dict[str, Any]:
        if not isinstance(result_id, str) or SHA256.fullmatch(result_id) is None:
            raise ProductionResultError("result_id must be lowercase SHA-256")
        target = self.results_root / result_id
        if target.is_symlink() or not target.is_dir() or stat.S_IMODE(target.stat().st_mode) & 0o222:
            raise ProductionResultError("immutable result directory is unavailable")
        members = {path.name for path in target.iterdir()}
        if members != RESULT_FILES:
            raise ProductionResultError("immutable result member set is invalid")
        payloads = {name: read_immutable(target / name) for name in RESULT_FILES}
        manifest = _strict_json(payloads["result-manifest.json"], "result manifest")
        if manifest.get("result_id") != result_id:
            raise ProductionResultError("stored result_id does not match its path")
        core = {key: value for key, value in manifest.items() if key != "result_id"}
        if hashlib.sha256(canonical_json_bytes(core)).hexdigest() != result_id:
            raise ProductionResultError("result manifest identity is invalid")
        files = core.get("files")
        if not isinstance(files, dict) or set(files) != RESULT_FILES - {"result-manifest.json"}:
            raise ProductionResultError("result file inventory is invalid")
        for name, expected in files.items():
            actual = {"sha256": hashlib.sha256(payloads[name]).hexdigest(), "size": len(payloads[name])}
            if expected != actual:
                raise ProductionResultError(f"result member identity mismatch: {name}")
        if core.get("automatic_ordering") is not False:
            raise ProductionResultError("result violates the no-order contract")
        return manifest

    def _publish_report(self, computation: JobComputation) -> None:
        current = self.publication_root / "current" / f"{computation.report_uuid}.html"
        staging = self.publication_root / "staging" / f".{computation.report_uuid}.{os.getpid()}.tmp"
        previous = current.read_bytes() if current.exists() else None
        if current.exists() and (current.is_symlink() or not current.is_file()):
            raise ProductionResultError("current report path is unsafe")
        try:
            with staging.open("xb") as stream:
                stream.write(computation.report_html)
                stream.flush()
                os.fsync(stream.fileno())
            staging.chmod(0o444)
            if read_immutable(staging) != computation.report_html:
                raise ProductionResultError("staged report read-back differs")
            if previous is not None:
                digest = hashlib.sha256(previous).hexdigest()
                backup = self.publication_root / "backups" / f"{computation.report_uuid}.{digest}.html"
                if backup.exists():
                    if read_immutable(backup) != previous:
                        raise ProductionResultError("prior report backup conflicts")
                else:
                    with backup.open("xb") as stream:
                        stream.write(previous)
                        stream.flush()
                        os.fsync(stream.fileno())
                    backup.chmod(0o444)
            os.replace(staging, current)
            _fsync_directory(current.parent)
            if read_immutable(current) != computation.report_html:
                raise ProductionResultError("promoted report read-back differs")
        except BaseException as exc:
            staging.unlink(missing_ok=True)
            if previous is None:
                current.unlink(missing_ok=True)
            elif not current.exists() or current.read_bytes() != previous:
                temporary = current.with_name(f".{current.name}.rollback.{os.getpid()}")
                with temporary.open("xb") as stream:
                    stream.write(previous)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o444)
                os.replace(temporary, current)
                _fsync_directory(current.parent)
                if read_immutable(current) != previous:
                    raise ProductionResultError("prior report rollback failed") from exc
            raise

    def publish(self, row: Mapping[str, Any], computation: JobComputation) -> dict[str, Any]:
        self._prepare()
        payloads = self._artifact_payloads(computation)
        if any(len(payload) > MAX_RESULT_MEMBER_BYTES for payload in payloads.values()):
            raise ProductionResultError("result member exceeds the size limit")
        core = self._manifest_core(row, computation, payloads)
        result_id = hashlib.sha256(canonical_json_bytes(core)).hexdigest()
        manifest = core | {"result_id": result_id}
        target = self.results_root / result_id
        if not target.exists():
            staging = Path(tempfile.mkdtemp(prefix=f".{result_id}.", dir=self.results_root))
            try:
                for name, payload in payloads.items():
                    with (staging / name).open("xb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    (staging / name).chmod(0o444)
                with (staging / "result-manifest.json").open("xb") as stream:
                    stream.write(canonical_json_bytes(manifest))
                    stream.flush()
                    os.fsync(stream.fileno())
                (staging / "result-manifest.json").chmod(0o444)
                _fsync_directory(staging)
                staging.chmod(0o555)
                os.rename(staging, target)
                _fsync_directory(self.results_root)
            finally:
                if staging.exists():
                    staging.chmod(0o700)
                    shutil.rmtree(staging)
        verified = self.verify(result_id)
        self._publish_report(computation)
        if self.verify(result_id) != verified:
            raise ProductionResultError("result changed during report publication")
        return verified
