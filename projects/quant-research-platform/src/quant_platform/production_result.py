from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from .production_contract import SHA256, canonical_json_bytes
from .production_jobs import REPORT_EVIDENCE_FILE_NAMES, FormalComputation, JobComputation


class ProductionResultError(RuntimeError):
    """Raised when a production result cannot be sealed or verified."""


DAILY_RESULT_FILES = frozenset(
    {
        "provider-response.bin",
        "normalized-snapshot.json",
        "action.json",
        "report.html",
        "notification.txt",
        *REPORT_EVIDENCE_FILE_NAMES,
    }
)
FORMAL_RESULT_FILES = frozenset(
    {
        "calibration.json",
        "03-CALIBRATION_CLAIMED.json",
        "04-CALIBRATION_SEALED.json",
    }
)
RESULT_FILES_BY_SCHEMA = {
    "quantresearch-production-result/v1": DAILY_RESULT_FILES,
    "quantresearch-production-formal-result/v1": FORMAL_RESULT_FILES,
}
CLIENT_RESULT_FILES = (
    REPORT_EVIDENCE_FILE_NAMES
    | frozenset({"normalized-snapshot.json", "notification.txt", "report.html"})
    | FORMAL_RESULT_FILES
)
MAX_RESULT_MEMBER_BYTES = 16 * 1024 * 1024
STABLE_REPORT_JOB_IDS = {
    "f642b386-74c0-4e9f-92e6-563e7c6a5d69": "1cd5557264db",
    "8991e9a8-1caa-41f5-b76b-6368259db5b4": "297c11cad0dc",
}


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


def verify_production_result(
    result_id: str,
    manifest: Mapping[str, Any],
    payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    """Verify one complete immutable production result independent of its storage backend."""
    if not isinstance(result_id, str) or SHA256.fullmatch(result_id) is None:
        raise ProductionResultError("result_id must be lowercase SHA-256")
    if not isinstance(manifest, Mapping) or not isinstance(payloads, Mapping):
        raise ProductionResultError("production result is malformed")
    stored_manifest = dict(manifest)
    schema = stored_manifest.get("schema")
    result_files = RESULT_FILES_BY_SCHEMA.get(schema) if isinstance(schema, str) else None
    if result_files is None or set(payloads) != result_files:
        raise ProductionResultError("immutable result member set is invalid")
    if stored_manifest.get("result_id") != result_id:
        raise ProductionResultError("stored result_id does not match the requested result")
    core = {key: value for key, value in stored_manifest.items() if key != "result_id"}
    if hashlib.sha256(canonical_json_bytes(core)).hexdigest() != result_id:
        raise ProductionResultError("result manifest identity is invalid")
    files = core.get("files")
    if not isinstance(files, Mapping) or set(files) != result_files:
        raise ProductionResultError("result file inventory is invalid")
    for name in result_files:
        payload = payloads[name]
        if not isinstance(payload, bytes):
            raise ProductionResultError(f"result member is not bytes: {name}")
        actual = {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        if files[name] != actual:
            raise ProductionResultError(f"result member identity mismatch: {name}")
    if core.get("automatic_ordering") is not False:
        raise ProductionResultError("result violates the no-order contract")
    if schema == "quantresearch-production-formal-result/v1":
        result_input = core.get("input")
        phase_claims = core.get("phase_claims")
        if (
            core.get("operation") != "calibrate-once"
            or not isinstance(result_input, Mapping)
            or result_input.get("kind") != "no-network-operation"
            or result_input.get("network_access") is not False
            or core.get("calibration_sha256") != files["calibration.json"]["sha256"]
            or phase_claims
            != {
                "CALIBRATION_CLAIMED": files["03-CALIBRATION_CLAIMED.json"]["sha256"],
                "CALIBRATION_SEALED": files["04-CALIBRATION_SEALED.json"]["sha256"],
            }
        ):
            raise ProductionResultError("formal result bindings are invalid")
    else:
        filename = core.get("report_filename")
        report_uuid = filename.removesuffix(".html") if isinstance(filename, str) else ""
        digest_bindings = {
            "provider_response_sha256": "provider-response.bin",
            "dataset_snapshot_id": "normalized-snapshot.json",
            "action_sha256": "action.json",
            "report_document_sha256": "report-document.json",
            "report_sha256": "report.html",
        }
        action_payload = payloads["action.json"]
        document_payload = payloads["report-document.json"]
        action = _strict_json(action_payload, "production action")
        document = _strict_json(document_payload, "production report document")
        from .production_schedule_client import (
            JOBS,
            REPORT_OPERATOR,
            ProductionClientError,
            _verify_report_document_sources,
        )

        try:
            evidence_bindings = _verify_report_document_sources(document, payloads)
        except (KeyError, TypeError, ValueError, ProductionClientError) as exc:
            raise ProductionResultError("daily semantic attestation is invalid") from exc
        provider_request = core.get("provider_request")
        frozen_job = JOBS.get(str(core.get("job_id")))
        if (
            filename != f"{report_uuid}.html"
            or STABLE_REPORT_JOB_IDS.get(report_uuid) != core.get("job_id")
            or frozen_job is None
            or frozen_job.report_filename != filename
            or frozen_job.model_id != core.get("model_id")
            or frozen_job.production_manifest_sha256
            != core.get("production_manifest_sha256")
            or any(core.get(field) != files[name]["sha256"] for field, name in digest_bindings.items())
            or canonical_json_bytes(action) != action_payload
            or canonical_json_bytes(document) != document_payload
            or action.get("job_id") != core.get("job_id")
            or action.get("report_uuid") != report_uuid
            or action.get("model_version") != core.get("model_id")
            or action.get("production_manifest_sha256")
            != core.get("production_manifest_sha256")
            or action.get("generated_at") != core.get("generated_at")
            or action.get("automatic_ordering") is not False
            or core.get("report_operator") != REPORT_OPERATOR
            or not isinstance(provider_request, Mapping)
            or provider_request.get("method") != "GET"
            or provider_request.get("url") != evidence_bindings["provider_url"]
            or core.get("attempt_id") != evidence_bindings["attempt_id"]
            or core.get("experiment_id") != evidence_bindings["experiment_id"]
            or core.get("dataset_snapshot_id") != evidence_bindings["dataset_snapshot_id"]
        ):
            raise ProductionResultError("daily result bindings are invalid")
    return stored_manifest


class ProductionResultStore:
    """Create-once result module with stable pointers to immutable reports."""

    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        self.results_root = self.root / "results"
        self.stable_database_path = self.root / "stable-reports.sqlite3"
        self._prepare_lock = threading.Lock()
        self._postgres = None
        if os.environ.get("QUANT_POSTGRES_PASSWORD_FILE"):
            from .full_persistence import FullPostgresPersistence

            self._postgres = FullPostgresPersistence.from_environment()

    def _prepare(self) -> None:
        with self._prepare_lock:
            self.results_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for path in (self.root, self.results_root):
                if path.is_symlink() or not path.is_dir():
                    raise ProductionResultError("result root is unsafe")
            connection = sqlite3.connect(self.stable_database_path, timeout=30)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stable_production_reports (
                        report_uuid TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        result_id TEXT NOT NULL,
                        report_sha256 TEXT NOT NULL,
                        report_size INTEGER NOT NULL,
                        generated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _stable_pointer(manifest: Mapping[str, Any]) -> dict[str, Any]:
        filename = manifest.get("report_filename")
        report_uuid = filename.removesuffix(".html") if isinstance(filename, str) else ""
        expected_job_id = STABLE_REPORT_JOB_IDS.get(report_uuid)
        files = manifest.get("files")
        report = files.get("report.html") if isinstance(files, Mapping) else None
        if (
            filename != f"{report_uuid}.html"
            or expected_job_id is None
            or manifest.get("job_id") != expected_job_id
            or not isinstance(report, Mapping)
            or manifest.get("report_sha256") != report.get("sha256")
            or not isinstance(report.get("size"), int)
            or not isinstance(manifest.get("generated_at"), str)
            or not manifest["generated_at"].endswith("Z")
        ):
            raise ProductionResultError("stable report binding is invalid")
        return {
            "report_uuid": report_uuid,
            "job_id": expected_job_id,
            "result_id": manifest.get("result_id"),
            "report_sha256": report["sha256"],
            "report_size": report["size"],
            "generated_at": manifest.get("generated_at"),
        }

    def _advance_stable_report(self, manifest: Mapping[str, Any]) -> None:
        pointer = self._stable_pointer(manifest)
        connection = sqlite3.connect(self.stable_database_path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO stable_production_reports(
                    report_uuid, job_id, result_id, report_sha256, report_size, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_uuid) DO UPDATE SET
                    job_id=excluded.job_id,
                    result_id=excluded.result_id,
                    report_sha256=excluded.report_sha256,
                    report_size=excluded.report_size,
                    generated_at=excluded.generated_at
                WHERE excluded.generated_at > stable_production_reports.generated_at
                   OR (excluded.generated_at = stable_production_reports.generated_at
                       AND excluded.result_id > stable_production_reports.result_id)
                """,
                tuple(pointer.values()),
            )
            stored = connection.execute(
                "SELECT * FROM stable_production_reports WHERE report_uuid = ?",
                (pointer["report_uuid"],),
            ).fetchone()
            selected = None if stored is None else dict(
                zip(pointer, stored, strict=True)
            )
            if selected is None or (
                selected["generated_at"], selected["result_id"]
            ) < (pointer["generated_at"], pointer["result_id"]):
                raise ProductionResultError("stable report pointer read-back differs")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def stable_report(self, report_uuid: str) -> dict[str, Any]:
        expected_job_id = STABLE_REPORT_JOB_IDS.get(report_uuid)
        if expected_job_id is None:
            raise ProductionResultError("stable report is unknown")
        if self._postgres is not None:
            try:
                value = self._postgres.stable_production_report(report_uuid)
            except (ValueError, RuntimeError) as exc:
                raise ProductionResultError(str(exc)) from exc
            if value.get("job_id") != expected_job_id:
                raise ProductionResultError("stable report job binding is invalid")
            return value
        self._prepare()
        connection = sqlite3.connect(self.stable_database_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM stable_production_reports WHERE report_uuid = ?",
                (report_uuid,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ProductionResultError("stable report is unknown")
        pointer = dict(row)
        manifest = self.verify(pointer["result_id"])
        expected = self._stable_pointer(manifest)
        if pointer != expected or pointer["job_id"] != expected_job_id:
            raise ProductionResultError("stable report pointer binding is invalid")
        payload = self.read_client_file(pointer["result_id"], "report.html")
        if {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        } != {
            "sha256": pointer["report_sha256"],
            "size": pointer["report_size"],
        }:
            raise ProductionResultError("stable report member identity mismatch")
        return {**pointer, "html": payload}

    def complete_stable_report(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        expected = self._stable_pointer(manifest)
        verified = self.verify(expected["result_id"])
        if verified != dict(manifest):
            raise ProductionResultError("stable production result read-back differs")
        if self._postgres is not None:
            current = self._postgres.advance_stable_production_report(verified)
        else:
            self._advance_stable_report(verified)
            current = self.stable_report(expected["report_uuid"])
        if current["generated_at"] < expected["generated_at"]:
            raise ProductionResultError("stable report pointer regressed")
        return current

    @staticmethod
    def _artifact_payloads(
        computation: JobComputation | FormalComputation,
    ) -> dict[str, bytes]:
        if isinstance(computation, FormalComputation):
            if set(computation.files) != FORMAL_RESULT_FILES:
                raise ProductionResultError("formal result member set is invalid")
            return dict(computation.files)
        return {
            "provider-response.bin": computation.raw_bytes,
            "normalized-snapshot.json": computation.normalized_bytes,
            "action.json": canonical_json_bytes(computation.action),
            **dict(computation.report_evidence),
            "report.html": computation.report_html,
            "notification.txt": computation.notification_bytes,
        }

    @staticmethod
    def _manifest_core(
        row: Mapping[str, Any],
        computation: JobComputation | FormalComputation,
        payloads: Mapping[str, bytes],
    ) -> dict[str, Any]:
        files = {
            name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
            for name, payload in sorted(payloads.items())
        }
        common = {
            "request_id": row["request_id"],
            "production_run_id": row["production_run_id"],
            "production_release_id": row["production_release_id"],
            "experiment_id": computation.experiment_id,
            "attempt_id": computation.attempt_id,
            "job_id": computation.job_id,
            "production_manifest_sha256": computation.production_manifest_sha256,
            "automatic_ordering": False,
            "files": files,
        }
        if isinstance(computation, FormalComputation):
            calibration = payloads["calibration.json"]
            claimed = payloads["03-CALIBRATION_CLAIMED.json"]
            sealed = payloads["04-CALIBRATION_SEALED.json"]
            return {
                "schema": "quantresearch-production-formal-result/v1",
                **common,
                "operation": computation.operation,
                "input": {
                    "kind": "no-network-operation",
                    "authority_sha256": computation.authority_sha256,
                    "network_access": False,
                },
                "calibration_sha256": hashlib.sha256(calibration).hexdigest(),
                "phase_claims": {
                    "CALIBRATION_CLAIMED": hashlib.sha256(claimed).hexdigest(),
                    "CALIBRATION_SEALED": hashlib.sha256(sealed).hexdigest(),
                },
            }
        return {
            "schema": "quantresearch-production-result/v1",
            **common,
            "model_id": computation.model_id,
            "provider_request": {"method": "GET", "url": computation.provider_url},
            "provider_response_sha256": hashlib.sha256(computation.raw_bytes).hexdigest(),
            "dataset_snapshot_id": hashlib.sha256(computation.normalized_bytes).hexdigest(),
            "action_sha256": hashlib.sha256(canonical_json_bytes(computation.action)).hexdigest(),
            "report_filename": f"{computation.report_uuid}.html",
            "report_operator": dict(computation.report_operator),
            "report_document_sha256": files["report-document.json"]["sha256"],
            "report_sha256": hashlib.sha256(computation.report_html).hexdigest(),
            "generated_at": computation.action["generated_at"],
        }

    def verify(self, result_id: str) -> dict[str, Any]:
        if self._postgres is not None:
            try:
                return self._postgres.production_result(result_id)["manifest"]
            except (ValueError, RuntimeError) as exc:
                raise ProductionResultError(str(exc)) from exc
        if not isinstance(result_id, str) or SHA256.fullmatch(result_id) is None:
            raise ProductionResultError("result_id must be lowercase SHA-256")
        target = self.results_root / result_id
        if target.is_symlink() or not target.is_dir() or stat.S_IMODE(target.stat().st_mode) & 0o222:
            raise ProductionResultError("unsafe immutable result directory is unavailable")
        members = {path.name for path in target.iterdir()}
        if "result-manifest.json" not in members:
            raise ProductionResultError("immutable result manifest is absent")
        manifest = _strict_json(
            read_immutable(target / "result-manifest.json"), "result manifest"
        )
        schema = manifest.get("schema")
        result_files = RESULT_FILES_BY_SCHEMA.get(schema) if isinstance(schema, str) else None
        if result_files is None or members != result_files | {"result-manifest.json"}:
            raise ProductionResultError("immutable result member set is invalid")
        payloads = {name: read_immutable(target / name) for name in result_files}
        return verify_production_result(result_id, manifest, payloads)

    def read_client_file(self, result_id: str, name: str) -> bytes:
        if name not in CLIENT_RESULT_FILES:
            raise ProductionResultError("result member is not available to the client")
        if self._postgres is not None:
            try:
                result = self._postgres.production_result(result_id)
            except (ValueError, RuntimeError) as exc:
                raise ProductionResultError(str(exc)) from exc
            manifest = result["manifest"]
            if name not in manifest["files"]:
                raise ProductionResultError("result member is not available for this result class")
            return result["members"][name]
        manifest = self.verify(result_id)
        if name not in manifest["files"]:
            raise ProductionResultError("result member is not available for this result class")
        payload = read_immutable(self.results_root / result_id / name)
        actual = {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        if manifest["files"].get(name) != actual:
            raise ProductionResultError("result member identity changed during read-back")
        return payload

    def publish(
        self, row: Mapping[str, Any], computation: JobComputation | FormalComputation
    ) -> dict[str, Any]:
        payloads = self._artifact_payloads(computation)
        if any(len(payload) > MAX_RESULT_MEMBER_BYTES for payload in payloads.values()):
            raise ProductionResultError("result member exceeds the size limit")
        core = self._manifest_core(row, computation, payloads)
        result_id = hashlib.sha256(canonical_json_bytes(core)).hexdigest()
        manifest = core | {"result_id": result_id}
        if self._postgres is not None:
            return self._postgres.publish_production_result(manifest, payloads)
        self._prepare()
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
        if self.verify(result_id) != verified:
            raise ProductionResultError("result changed during stable pointer advancement")
        return verified
