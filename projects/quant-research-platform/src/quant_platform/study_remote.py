from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .msft_trend_study import (
    MARKET_JOB_TYPE,
    VERDICT_PRECEDENCE,
    MsftStudyValidationError,
    run_study,
    validate_snapshot,
)
from .study_training_kernel import (
    TRAINING_JOB_TYPE,
    StudyTrainingKernel,
    TrainingKernelValidationError,
    freeze_training_spec,
    validate_training_spec,
)

PROTOCOL = "quantresearch-study-worker/v1"
DISPATCH_PROTOCOL = "quantresearch-study-dispatch/v2"
JOB_TYPE = "deterministic-synthetic-search-v1"
SCHEMA_IDENTITY = "quantresearch-lightweight-study-postgresql-v4"
MAX_BODY_BYTES = 4 * 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
MAX_ITERATIONS = 100_000_000
MAX_CHECKPOINTS = 20
MAX_DISPATCH_GENERATION = 9_223_372_036_854_775_807
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED"})


class StudyRemoteError(RuntimeError):
    """Base error for the lightweight remote Study seam."""


class StudyValidationError(StudyRemoteError):
    pass


class StudyAuthenticationError(StudyRemoteError):
    pass


class StudyIdempotencyConflict(StudyRemoteError):
    pass


class StudyDispatchFenced(StudyRemoteError):
    pass


class StudyTransportError(StudyRemoteError):
    def __init__(self, message: str, *, acceptance_ambiguous: bool = True):
        super().__init__(message)
        self.acceptance_ambiguous = acceptance_ambiguous


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StudyValidationError("JSON values must be serializable and finite") from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _jsonb(value: Any) -> Any:
    # The Feng worker is deliberately runnable without the PostgreSQL client.
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _strict_object(payload: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StudyValidationError(f"{label} contains duplicate field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                StudyValidationError(f"{label} contains non-finite value: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudyValidationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise StudyValidationError(f"{label} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise StudyValidationError(
            f"{label} fields differ; missing={missing}, unknown={unknown}"
        )


def _job_identity(frozen_inputs: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        b"quantresearch-lightweight-study/v1\0" + canonical_json_bytes(frozen_inputs)
    ).hexdigest()


@dataclass(frozen=True)
class _WorkerJobResult:
    progress: dict[str, Any]
    evidence: dict[str, Any]


class _SyntheticWorkerJob:
    job_type = JOB_TYPE

    def freeze(
        self,
        *,
        iterations: int,
        seed: int,
        checkpoint_count: int,
        source_commit: str,
        source_tree: str,
        worker_image: str,
    ) -> dict[str, Any]:
        if type(iterations) is not int or not 1 <= iterations <= MAX_ITERATIONS:
            raise StudyValidationError(f"iterations must be between 1 and {MAX_ITERATIONS}")
        if type(seed) is not int or not 0 <= seed <= 4_294_967_295:
            raise StudyValidationError("seed must be an unsigned 32-bit integer")
        if type(checkpoint_count) is not int or not 1 <= checkpoint_count <= MAX_CHECKPOINTS:
            raise StudyValidationError(
                f"checkpoint_count must be between 1 and {MAX_CHECKPOINTS}"
            )
        _validate_source_identity(source_commit, source_tree, worker_image)
        frozen = {
            "schema_version": 1,
            "protocol": PROTOCOL,
            "job_type": self.job_type,
            "iterations": iterations,
            "seed": seed,
            "checkpoint_count": checkpoint_count,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "worker_image": worker_image,
        }
        return {"job_id": _job_identity(frozen), **frozen}

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(
            value,
            {
                "job_id",
                "schema_version",
                "protocol",
                "job_type",
                "iterations",
                "seed",
                "checkpoint_count",
                "source_commit",
                "source_tree",
                "worker_image",
            },
            "Study request",
        )
        rebuilt = self.freeze(
            iterations=value["iterations"],
            seed=value["seed"],
            checkpoint_count=value["checkpoint_count"],
            source_commit=value["source_commit"],
            source_tree=value["source_tree"],
            worker_image=value["worker_image"],
        )
        _validate_request_identity(value, rebuilt)
        return rebuilt

    def initial_progress(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "completed_iterations": 0,
            "total_iterations": request["iterations"],
        }

    def run(
        self,
        request: Mapping[str, Any],
        on_checkpoint: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> _WorkerJobResult:
        total = int(request["iterations"])
        checkpoint_count = int(request["checkpoint_count"])
        checkpoints = sorted(
            {
                max(1, total * index // checkpoint_count)
                for index in range(1, checkpoint_count + 1)
            }
        )
        checkpoint_index = 0
        generator = int(request["seed"])
        best_score = float("inf")
        best_parameter = 0.0
        for iteration in range(1, total + 1):
            generator = (1_664_525 * generator + 1_013_904_223) & 0xFFFFFFFF
            parameter = generator / 4_294_967_296
            score = (parameter - 0.61803398875) ** 2 + (iteration % 997) * 1e-12
            if score < best_score:
                best_score = score
                best_parameter = parameter
            if checkpoint_index < len(checkpoints) and iteration == checkpoints[checkpoint_index]:
                on_checkpoint(
                    {
                        "completed_iterations": iteration,
                        "total_iterations": total,
                        "checkpoint_sequence": checkpoint_index + 1,
                    },
                    {
                        "sequence": checkpoint_index + 1,
                        "completed_iterations": iteration,
                        "best_parameter": best_parameter,
                        "best_score": best_score,
                    },
                )
                checkpoint_index += 1
        return _WorkerJobResult(
            progress={
                "completed_iterations": total,
                "total_iterations": total,
                "checkpoint_sequence": checkpoint_index,
            },
            evidence={
                "conclusion": "SYNTHETIC_MINIMUM_FOUND",
                "iterations": total,
                "best_parameter": best_parameter,
                "best_score": best_score,
                "checkpoint_count": checkpoint_index,
            },
        )


class _TrainingWorkerJob:
    job_type = TRAINING_JOB_TYPE

    def freeze(
        self,
        *,
        trial_budget: int,
        seed: int,
        checkpoint_count: int,
        parameter_low: float,
        parameter_high: float,
        objective_target: float,
        source_commit: str,
        source_tree: str,
        worker_image: str,
    ) -> dict[str, Any]:
        if type(checkpoint_count) is not int or not 1 <= checkpoint_count <= MAX_CHECKPOINTS:
            raise StudyValidationError(
                f"checkpoint_count must be between 1 and {MAX_CHECKPOINTS}"
            )
        _validate_source_identity(source_commit, source_tree, worker_image)
        try:
            training_spec = freeze_training_spec(
                trial_budget=trial_budget,
                seed=seed,
                parameter_low=parameter_low,
                parameter_high=parameter_high,
                objective_target=objective_target,
            )
        except TrainingKernelValidationError as exc:
            raise StudyValidationError(str(exc)) from exc
        frozen = {
            "schema_version": 1,
            "protocol": PROTOCOL,
            "job_type": self.job_type,
            "training_spec": training_spec,
            "checkpoint_count": checkpoint_count,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "worker_image": worker_image,
        }
        return {"job_id": _job_identity(frozen), **frozen}

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(
            value,
            {
                "job_id",
                "schema_version",
                "protocol",
                "job_type",
                "training_spec",
                "checkpoint_count",
                "source_commit",
                "source_tree",
                "worker_image",
            },
            "Study request",
        )
        if not isinstance(value.get("training_spec"), Mapping):
            raise StudyValidationError("training_spec must be an object")
        try:
            spec = validate_training_spec(value["training_spec"])
            search = spec["search"]
            objective = spec["objective"]
            rebuilt = self.freeze(
                trial_budget=search["trial_budget"],
                seed=search["seed"],
                checkpoint_count=value["checkpoint_count"],
                parameter_low=search["low"],
                parameter_high=search["high"],
                objective_target=objective["target"],
                source_commit=value["source_commit"],
                source_tree=value["source_tree"],
                worker_image=value["worker_image"],
            )
        except TrainingKernelValidationError as exc:
            raise StudyValidationError(str(exc)) from exc
        _validate_request_identity(value, rebuilt)
        return rebuilt

    def initial_progress(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "completed_trials": 0,
            "total_trials": request["training_spec"]["search"]["trial_budget"],
            "suggestion_count": 0,
        }

    def run(
        self,
        request: Mapping[str, Any],
        on_checkpoint: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> _WorkerJobResult:
        completed = StudyTrainingKernel().run(
            request["training_spec"],
            checkpoint_count=int(request["checkpoint_count"]),
            on_checkpoint=on_checkpoint,
        )
        return _WorkerJobResult(progress=completed.progress, evidence=completed.evidence)


class _MarketWorkerJob:
    job_type = MARKET_JOB_TYPE

    def freeze(
        self,
        *,
        snapshot: Mapping[str, Any],
        checkpoint_count: int,
        source_commit: str,
        source_tree: str,
        worker_image: str,
    ) -> dict[str, Any]:
        if checkpoint_count != 3:
            raise StudyValidationError("MSFT market Study uses exactly three bounded checkpoints")
        _validate_source_identity(source_commit, source_tree, worker_image)
        try:
            frozen_snapshot = validate_snapshot(snapshot)
        except MsftStudyValidationError as exc:
            raise StudyValidationError(str(exc)) from exc
        frozen = {
            "schema_version": 1,
            "protocol": PROTOCOL,
            "job_type": self.job_type,
            "snapshot": frozen_snapshot,
            "checkpoint_count": checkpoint_count,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "worker_image": worker_image,
        }
        return {"job_id": _job_identity(frozen), **frozen}

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(
            value,
            {
                "job_id",
                "schema_version",
                "protocol",
                "job_type",
                "snapshot",
                "checkpoint_count",
                "source_commit",
                "source_tree",
                "worker_image",
            },
            "Study request",
        )
        if not isinstance(value.get("snapshot"), Mapping):
            raise StudyValidationError("MSFT Snapshot must be an object")
        rebuilt = self.freeze(
            snapshot=value["snapshot"],
            checkpoint_count=value["checkpoint_count"],
            source_commit=value["source_commit"],
            source_tree=value["source_tree"],
            worker_image=value["worker_image"],
        )
        _validate_request_identity(value, rebuilt)
        return rebuilt

    def initial_progress(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return {"completed_candidates": 0, "total_candidates": 15}

    def run(
        self,
        request: Mapping[str, Any],
        on_checkpoint: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> _WorkerJobResult:
        try:
            evidence = run_study(request["snapshot"], checkpoint=on_checkpoint)
        except MsftStudyValidationError as exc:
            evidence = {
                "schema": "quantresearch-msft-study-result/v1",
                "kernel": "quant_platform.msft_trend_study@1.0.0",
                "snapshot_id": request["snapshot"]["snapshot_id"],
                "trial_count": 0,
                "family_winners": [],
                "selection": None,
                "final": None,
                "verdict": "INCONCLUSIVE_DATA_OR_EXECUTION",
                "conclusion": "INCONCLUSIVE_DATA_OR_EXECUTION",
                "verdict_precedence": list(VERDICT_PRECEDENCE),
                "reason": str(exc),
                "per_trial_attempt_rows": 0,
                "terminal_exit_fabricated": False,
                "final_evaluation_counts": {
                    "primary_candidate": 0,
                    "neighbors": 0,
                    "alternatives": 0,
                    "reselection": 0,
                },
            }
        return _WorkerJobResult(
            progress={
                "completed_candidates": evidence["trial_count"],
                "total_candidates": 15,
                "checkpoint_sequence": 3 if evidence["trial_count"] == 15 else 0,
            },
            evidence=evidence,
        )


def _validate_source_identity(source_commit: str, source_tree: str, worker_image: str) -> None:
    if HEX_40.fullmatch(source_commit) is None or HEX_40.fullmatch(source_tree) is None:
        raise StudyValidationError("source commit and tree must be lowercase Git SHA-1 identities")
    if IMAGE_DIGEST.fullmatch(worker_image) is None:
        raise StudyValidationError("worker_image must be a sha256 image identity")


def _validate_request_identity(
    value: Mapping[str, Any], rebuilt: Mapping[str, Any]
) -> None:
    if value.get("schema_version") != 1 or value.get("protocol") != PROTOCOL:
        raise StudyValidationError("Study request protocol identity is unsupported")
    if value.get("job_id") != rebuilt["job_id"]:
        raise StudyValidationError("Study request identity does not match its frozen inputs")


_SYNTHETIC_WORKER_JOB = _SyntheticWorkerJob()
_TRAINING_WORKER_JOB = _TrainingWorkerJob()
_MARKET_WORKER_JOB = _MarketWorkerJob()
_WORKER_JOBS = {
    _SYNTHETIC_WORKER_JOB.job_type: _SYNTHETIC_WORKER_JOB,
    _TRAINING_WORKER_JOB.job_type: _TRAINING_WORKER_JOB,
    _MARKET_WORKER_JOB.job_type: _MARKET_WORKER_JOB,
}


def _worker_job(
    value: Mapping[str, Any],
) -> _SyntheticWorkerJob | _TrainingWorkerJob | _MarketWorkerJob:
    job_type = value.get("job_type")
    if not isinstance(job_type, str):
        raise StudyValidationError("Study request identity does not match its frozen inputs")
    try:
        return _WORKER_JOBS[job_type]
    except KeyError as exc:
        raise StudyValidationError("Study request identity does not match its frozen inputs") from exc


def freeze_synthetic_request(
    *,
    iterations: int,
    seed: int,
    checkpoint_count: int,
    source_commit: str,
    source_tree: str,
    worker_image: str,
) -> dict[str, Any]:
    return _SYNTHETIC_WORKER_JOB.freeze(
        iterations=iterations,
        seed=seed,
        checkpoint_count=checkpoint_count,
        source_commit=source_commit,
        source_tree=source_tree,
        worker_image=worker_image,
    )


def freeze_training_request(
    *,
    trial_budget: int,
    seed: int,
    checkpoint_count: int,
    parameter_low: float,
    parameter_high: float,
    objective_target: float,
    source_commit: str,
    source_tree: str,
    worker_image: str,
) -> dict[str, Any]:
    return _TRAINING_WORKER_JOB.freeze(
        trial_budget=trial_budget,
        seed=seed,
        checkpoint_count=checkpoint_count,
        parameter_low=parameter_low,
        parameter_high=parameter_high,
        objective_target=objective_target,
        source_commit=source_commit,
        source_tree=source_tree,
        worker_image=worker_image,
    )


def freeze_market_request(
    *,
    snapshot: Mapping[str, Any],
    source_commit: str,
    source_tree: str,
    worker_image: str,
) -> dict[str, Any]:
    return _MARKET_WORKER_JOB.freeze(
        snapshot=snapshot,
        checkpoint_count=3,
        source_commit=source_commit,
        source_tree=source_tree,
        worker_image=worker_image,
    )


def validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    return _worker_job(value).validate(value)


def _validate_dispatch_generation(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_DISPATCH_GENERATION:
        raise StudyValidationError(f"{label} must be a positive signed 64-bit integer")
    return value


def _dispatch_envelope(request: Mapping[str, Any], dispatch_generation: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "protocol": DISPATCH_PROTOCOL,
        "dispatch_generation": _validate_dispatch_generation(
            dispatch_generation, "dispatch_generation"
        ),
        "request": validate_request(request),
    }


def _validate_dispatch_envelope(value: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    _exact_fields(
        value,
        {"schema_version", "protocol", "dispatch_generation", "request"},
        "Study dispatch envelope",
    )
    if value.get("schema_version") != 1 or value.get("protocol") != DISPATCH_PROTOCOL:
        raise StudyValidationError("Study dispatch protocol identity is unsupported")
    if not isinstance(value.get("request"), dict):
        raise StudyValidationError("Study dispatch request must be an object")
    return validate_request(value["request"]), _validate_dispatch_generation(
        value.get("dispatch_generation"), "dispatch_generation"
    )


def _fence_envelope(request: Mapping[str, Any], through_generation: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "protocol": DISPATCH_PROTOCOL,
        "fence_through_generation": _validate_dispatch_generation(
            through_generation, "fence_through_generation"
        ),
        "request": validate_request(request),
    }


def _validate_fence_envelope(value: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    _exact_fields(
        value,
        {"schema_version", "protocol", "fence_through_generation", "request"},
        "Study dispatch fence",
    )
    if value.get("schema_version") != 1 or value.get("protocol") != DISPATCH_PROTOCOL:
        raise StudyValidationError("Study dispatch fence protocol identity is unsupported")
    if not isinstance(value.get("request"), dict):
        raise StudyValidationError("Study dispatch fence request must be an object")
    return validate_request(value["request"]), _validate_dispatch_generation(
        value.get("fence_through_generation"), "fence_through_generation"
    )


def _signature_message(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    return (
        f"{PROTOCOL}\n{method}\n{path}\n{timestamp}\n"
        f"{hashlib.sha256(body).hexdigest()}\n"
    ).encode("ascii")


def public_key_id(public_key: Path) -> str:
    payload = public_key.read_bytes()
    if not payload or len(payload) > 16_384:
        raise StudyAuthenticationError("worker public key is empty or too large")
    return hashlib.sha256(payload).hexdigest()


def _openssl_sign(private_key: Path, message: bytes) -> bytes:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix="study-message-", dir="/tmp")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(message)
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(private_key),
                "-rawin",
                "-in",
                temporary_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout:
            raise StudyAuthenticationError("request signing failed")
        return completed.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise StudyAuthenticationError("request signing is unavailable") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _openssl_verify(public_key: Path, message: bytes, signature: bytes) -> bool:
    message_name: str | None = None
    signature_name: str | None = None
    try:
        descriptor, signature_name = tempfile.mkstemp(prefix="study-signature-", dir="/tmp")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(signature)
        descriptor, message_name = tempfile.mkstemp(prefix="study-message-", dir="/tmp")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(message)
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key),
                "-rawin",
                "-sigfile",
                signature_name,
                "-in",
                message_name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        for temporary_name in (message_name, signature_name):
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass


def signed_headers(private_key: Path, method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = _openssl_sign(private_key, _signature_message(method, path, timestamp, body))
    public_key = private_key.with_suffix(".pub.pem")
    return {
        "x-study-key-id": public_key_id(public_key),
        "x-study-timestamp": timestamp,
        "x-study-signature": base64.urlsafe_b64encode(signature).decode("ascii"),
    }


def verify_headers(
    public_key: Path,
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    now: float | None = None,
) -> None:
    if not hmac.compare_digest(headers.get("x-study-key-id", ""), public_key_id(public_key)):
        raise StudyAuthenticationError("request key identity is not admitted")
    timestamp = headers.get("x-study-timestamp", "")
    try:
        issued = int(timestamp)
    except ValueError as exc:
        raise StudyAuthenticationError("request timestamp is invalid") from exc
    if abs(int(now if now is not None else time.time()) - issued) > 60:
        raise StudyAuthenticationError("request timestamp is outside the allowed window")
    encoded = headers.get("x-study-signature", "")
    try:
        signature = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise StudyAuthenticationError("request signature encoding is invalid") from exc
    if not _openssl_verify(
        public_key, _signature_message(method, path, timestamp, body), signature
    ):
        raise StudyAuthenticationError("request signature is invalid")


class WorkerJobStore:
    """Feng-local transient state: one bounded JSON document per remote Study job."""

    def __init__(self, root: Path | str, worker_image: str):
        if IMAGE_DIGEST.fullmatch(worker_image) is None:
            raise StudyValidationError("worker runtime image identity is invalid")
        self.root = Path(root).absolute()
        self.worker_image = worker_image
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._inflight_generations: dict[str, set[int]] = {}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, job_id: str) -> Path:
        if HEX_64.fullmatch(job_id) is None:
            raise StudyValidationError("job identity is invalid")
        return self.root / f"{job_id}.json"

    def _read(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return _strict_object(path.read_bytes(), "worker job state")

    def _write(self, state: Mapping[str, Any]) -> None:
        payload = canonical_json_bytes(state)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise StudyRemoteError("worker job state exceeds its bounded size")
        target = self._path(str(state["job_id"]))
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _fence_path(self, job_id: str) -> Path:
        self._path(job_id)
        return self.root / ".dispatch-fences" / f"{job_id}.json"

    def _read_fence(self, job_id: str) -> dict[str, Any] | None:
        path = self._fence_path(job_id)
        if not path.exists():
            return None
        return _strict_object(path.read_bytes(), "worker dispatch fence")

    def _write_fence(self, fence: Mapping[str, Any]) -> None:
        target = self._fence_path(str(fence["job_id"]))
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json_bytes(fence))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _before_acceptance(self, _request: Mapping[str, Any], _dispatch_generation: int) -> None:
        """Test seam after worker admission starts and before its task document is written."""

    def submit(
        self, request: Mapping[str, Any], dispatch_generation: int
    ) -> tuple[dict[str, Any], bool]:
        frozen = validate_request(request)
        generation = _validate_dispatch_generation(dispatch_generation, "dispatch_generation")
        if frozen["worker_image"] != self.worker_image:
            raise StudyValidationError("requested worker image does not match running worker")
        request_digest = hashlib.sha256(canonical_json_bytes(frozen)).hexdigest()
        with self._lock:
            existing = self._read(frozen["job_id"])
            if existing is not None:
                if existing.get("request_digest") != request_digest:
                    raise StudyIdempotencyConflict("job identity already has different frozen inputs")
                return existing, True
            fence = self._read_fence(frozen["job_id"])
            if fence is not None:
                if fence.get("request_digest") != request_digest:
                    raise StudyIdempotencyConflict("dispatch fence has different frozen inputs")
                if generation <= fence.get("fenced_through_generation", 0):
                    raise StudyDispatchFenced("Study dispatch generation is authoritatively fenced")
            self._inflight_generations.setdefault(frozen["job_id"], set()).add(generation)
        try:
            self._before_acceptance(frozen, generation)
            with self._lock:
                existing = self._read(frozen["job_id"])
                if existing is not None:
                    if existing.get("request_digest") != request_digest:
                        raise StudyIdempotencyConflict(
                            "job identity already has different frozen inputs"
                        )
                    return existing, True
                fence = self._read_fence(frozen["job_id"])
                if fence is not None and generation <= fence.get(
                    "fenced_through_generation", 0
                ):
                    raise StudyDispatchFenced(
                        "Study dispatch generation is authoritatively fenced"
                    )
                state = {
                    "schema_version": 1,
                    "protocol": PROTOCOL,
                    "job_id": frozen["job_id"],
                    "request_digest": request_digest,
                    "frozen_request": frozen,
                    "source_commit": frozen["source_commit"],
                    "source_tree": frozen["source_tree"],
                    "worker_image": self.worker_image,
                    "status": "ACCEPTED",
                    "progress": self._initial_progress(frozen),
                    "checkpoints": [],
                    "result": None,
                    "failure": None,
                    "updated_at": _utc_now(),
                }
                self._write(state)
                self._start(frozen)
                return state, False
        finally:
            with self._lock:
                inflight = self._inflight_generations.get(frozen["job_id"])
                if inflight is not None:
                    inflight.discard(generation)
                    if not inflight:
                        self._inflight_generations.pop(frozen["job_id"], None)

    def fence(self, request: Mapping[str, Any], through_generation: int) -> dict[str, Any]:
        frozen = validate_request(request)
        generation = _validate_dispatch_generation(
            through_generation, "fence_through_generation"
        )
        if frozen["worker_image"] != self.worker_image:
            raise StudyValidationError("requested worker image does not match running worker")
        request_digest = hashlib.sha256(canonical_json_bytes(frozen)).hexdigest()
        with self._lock:
            existing = self._read(frozen["job_id"])
            if existing is not None:
                if existing.get("request_digest") != request_digest:
                    raise StudyIdempotencyConflict("job identity already has different frozen inputs")
                return {
                    "schema_version": 1,
                    "protocol": DISPATCH_PROTOCOL,
                    "outcome": "EXISTS",
                    "job_id": frozen["job_id"],
                    "request_digest": request_digest,
                    "fenced_through_generation": 0,
                    "negative_receipt_id": None,
                    "job": existing,
                }
            fence = self._read_fence(frozen["job_id"])
            if fence is not None and fence.get("request_digest") != request_digest:
                raise StudyIdempotencyConflict("dispatch fence has different frozen inputs")
            active = sorted(
                item
                for item in self._inflight_generations.get(frozen["job_id"], set())
                if item <= generation
            )
            if active:
                return {
                    "schema_version": 1,
                    "protocol": DISPATCH_PROTOCOL,
                    "outcome": "IN_FLIGHT",
                    "job_id": frozen["job_id"],
                    "request_digest": request_digest,
                    "fenced_through_generation": (
                        0 if fence is None else fence["fenced_through_generation"]
                    ),
                    "negative_receipt_id": None,
                    "job": None,
                }
            fenced_through = max(
                generation, 0 if fence is None else fence["fenced_through_generation"]
            )
            receipt_identity = {
                "schema_version": 1,
                "protocol": DISPATCH_PROTOCOL,
                "job_id": frozen["job_id"],
                "request_digest": request_digest,
                "fenced_through_generation": fenced_through,
            }
            receipt_id = hashlib.sha256(canonical_json_bytes(receipt_identity)).hexdigest()
            persisted = {
                **receipt_identity,
                "negative_receipt_id": receipt_id,
                "updated_at": _utc_now(),
            }
            self._write_fence(persisted)
            return {
                **persisted,
                "outcome": "ABSENT_FENCED",
                "job": None,
            }

    def status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(job_id)

    @staticmethod
    def _initial_progress(request: Mapping[str, Any]) -> dict[str, Any]:
        return _worker_job(request).initial_progress(request)

    def _start(self, request: Mapping[str, Any]) -> None:
        job_id = str(request["job_id"])
        thread = threading.Thread(
            target=self._execute,
            args=(request,),
            name=f"study-{job_id[:12]}",
            daemon=True,
        )
        self._threads[job_id] = thread
        thread.start()

    def resume_incomplete(self) -> None:
        """Restart accepted work deterministically from frozen inputs after worker restart."""
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                state = _strict_object(path.read_bytes(), "worker job state")
                if state.get("status") in TERMINAL_STATES:
                    continue
                request = state.get("frozen_request")
                if not isinstance(request, Mapping):
                    state["status"] = "FAILED"
                    state["failure"] = {
                        "code": "WORKER_RESTART_UNRECOVERABLE",
                        "message": "accepted work predates frozen-request restart support",
                    }
                    state["updated_at"] = _utc_now()
                    self._write(state)
                    continue
                try:
                    frozen = validate_request(request)
                except StudyValidationError as exc:
                    state["status"] = "FAILED"
                    state["failure"] = {
                        "code": "WORKER_RESTART_INPUT_INVALID",
                        "message": str(exc),
                    }
                    state["updated_at"] = _utc_now()
                    self._write(state)
                    continue
                if frozen["worker_image"] != self.worker_image:
                    state["status"] = "FAILED"
                    state["failure"] = {
                        "code": "WORKER_RESTART_IMAGE_DRIFT",
                        "message": "persisted request targets a different worker image",
                    }
                    state["updated_at"] = _utc_now()
                    self._write(state)
                    continue
                expected = hashlib.sha256(canonical_json_bytes(frozen)).hexdigest()
                if expected != state.get("request_digest"):
                    state["status"] = "FAILED"
                    state["failure"] = {
                        "code": "WORKER_RESTART_INPUT_CONFLICT",
                        "message": "persisted frozen request does not match its accepted digest",
                    }
                    state["updated_at"] = _utc_now()
                    self._write(state)
                    continue
                state["status"] = "ACCEPTED"
                state["progress"] = self._initial_progress(frozen)
                state["checkpoints"] = []
                state["result"] = None
                state["failure"] = None
                state["updated_at"] = _utc_now()
                self._write(state)
                self._start(frozen)

    def _execute(self, request: Mapping[str, Any]) -> None:
        job_id = str(request["job_id"])
        try:
            with self._lock:
                state = self._read(job_id)
                if state is None:
                    raise StudyRemoteError("accepted worker state disappeared")
                state["status"] = "RUNNING"
                state["updated_at"] = _utc_now()
                self._write(state)
            def checkpoint(progress: dict[str, Any], aggregate: dict[str, Any]) -> None:
                with self._lock:
                    checkpoint_state = self._read(job_id)
                    if checkpoint_state is None:
                        raise StudyRemoteError("worker state disappeared during execution")
                    checkpoint_state["progress"] = progress
                    checkpoint_state["checkpoints"].append(aggregate)
                    checkpoint_state["updated_at"] = _utc_now()
                    self._write(checkpoint_state)

            completed = _worker_job(request).run(request, checkpoint)
            with self._lock:
                state = self._read(job_id)
                if state is None:
                    raise StudyRemoteError("worker state disappeared before completion")
                state["status"] = "SUCCEEDED"
                state["progress"] = completed.progress
                state["result"] = completed.evidence
                state["updated_at"] = _utc_now()
                self._write(state)
        except Exception as exc:
            with self._lock:
                state = self._read(job_id)
                if state is not None and state.get("status") not in TERMINAL_STATES:
                    state["status"] = "FAILED"
                    state["failure"] = {
                        "code": "WORKER_EXECUTION_FAILED",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                    state["updated_at"] = _utc_now()
                    self._write(state)


class _StudyWorkerHandler(BaseHTTPRequestHandler):
    server: "StudyWorkerServer"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _response(self, status: int, value: Mapping[str, Any]) -> None:
        payload = canonical_json_bytes(value)
        if len(payload) > MAX_RESPONSE_BYTES:
            status = 500
            payload = canonical_json_bytes(
                {"ok": False, "error": {"code": "RESPONSE_TOO_LARGE"}}
            )
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, code: str, message: str) -> None:
        self._response(status, {"ok": False, "error": {"code": code, "message": message}})

    def _authenticate(self, body: bytes) -> bool:
        try:
            verify_headers(
                self.server.public_key,
                self.command,
                self.path,
                body,
                {key.lower(): value for key, value in self.headers.items()},
            )
        except StudyAuthenticationError as exc:
            self._error(403, "AUTHENTICATION_REJECTED", str(exc))
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health/live":
            self._response(
                200,
                {
                    "status": "live",
                    "protocol": PROTOCOL,
                    "worker_image": self.server.store.worker_image,
                },
            )
            return
        prefix = "/v1/studies/"
        if not self.path.startswith(prefix) or "/" in self.path[len(prefix) :]:
            self._error(404, "NOT_FOUND", "unknown worker route")
            return
        if not self._authenticate(b""):
            return
        try:
            state = self.server.store.status(self.path[len(prefix) :])
        except StudyValidationError as exc:
            self._error(422, "INVALID_JOB_ID", str(exc))
            return
        if state is None:
            self._error(404, "NOT_FOUND", "unknown Study job")
            return
        self._response(200, {"ok": True, "job": state})

    def do_POST(self) -> None:
        prefix = "/v1/studies/"
        is_submit = self.path == "/v1/studies"
        is_fence = self.path.startswith(prefix) and self.path.endswith("/fence")
        if not is_submit and not is_fence:
            self._error(404, "NOT_FOUND", "unknown worker route")
            return
        if self.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            self._error(415, "CONTENT_TYPE_REJECTED", "Content-Type must be application/json")
            return
        try:
            length = int(self.headers.get("content-length", ""))
        except ValueError:
            self._error(422, "BODY_SIZE_REJECTED", "request body size is invalid")
            return
        if not 1 <= length <= MAX_BODY_BYTES:
            self._error(422, "BODY_SIZE_REJECTED", "request body size is invalid")
            return
        body = self.rfile.read(length)
        if not self._authenticate(body):
            return
        try:
            value = _strict_object(body, "Study dispatch request")
            if is_submit:
                request, generation = _validate_dispatch_envelope(value)
                state, replay = self.server.store.submit(request, generation)
            else:
                job_id = self.path[len(prefix) : -len("/fence")]
                if "/" in job_id or HEX_64.fullmatch(job_id) is None:
                    raise StudyValidationError("job identity is invalid")
                request, generation = _validate_fence_envelope(value)
                if request["job_id"] != job_id:
                    raise StudyValidationError("fence path and request identity differ")
                receipt = self.server.store.fence(request, generation)
        except StudyIdempotencyConflict as exc:
            self._error(409, "IDEMPOTENCY_CONFLICT", str(exc))
            return
        except StudyDispatchFenced as exc:
            self._error(409, "DISPATCH_FENCED", str(exc))
            return
        except StudyValidationError as exc:
            self._error(422, "INVALID_REQUEST", str(exc))
            return
        if is_fence:
            self._response(200, {"ok": True, "fence": receipt})
            return
        self._response(
            200 if replay else 202,
            {"ok": True, "idempotent_replay": replay, "job": state},
        )


class StudyWorkerServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: WorkerJobStore, public_key: Path):
        self.store = store
        self.public_key = public_key
        public_key_id(public_key)
        super().__init__(address, _StudyWorkerHandler)
        self.store.resume_incomplete()


@dataclass(frozen=True)
class RemoteResponse:
    status_code: int
    value: dict[str, Any]
    elapsed_ms: float


class SignedStudyClient:
    def __init__(
        self,
        endpoint: str,
        private_key: Path | str,
        *,
        timeout_seconds: float = 5,
        tls_ca_file: Path | str | None = None,
    ):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise StudyValidationError("worker endpoint must be an HTTP(S) origin without a path")
        if parsed.scheme == "http" and tls_ca_file is not None:
            raise StudyValidationError("a TLS CA file requires an HTTPS worker endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.private_key = Path(private_key)
        self.timeout_seconds = timeout_seconds
        try:
            self.ssl_context = (
                ssl.create_default_context(cafile=str(tls_ca_file))
                if parsed.scheme == "https"
                else None
            )
        except (OSError, ssl.SSLError) as exc:
            raise StudyAuthenticationError("worker TLS trust is unavailable") from exc

    def _request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        on_post_start: Callable[[], bool] | None = None,
    ) -> RemoteResponse:
        headers = signed_headers(self.private_key, method, path, body)
        if body:
            headers["content-type"] = "application/json"
        request = urllib.request.Request(
            f"{self.endpoint}{path}", data=body if method == "POST" else None, headers=headers, method=method
        )
        if on_post_start is not None and not on_post_start():
            raise StudyTransportError(
                "Study dispatch claim expired before POST", acceptance_ambiguous=False
            )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=self.ssl_context
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
        except (OSError, urllib.error.URLError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise StudyTransportError(
                f"Feng worker is unavailable: {type(exc).__name__}",
                acceptance_ambiguous=(
                    method == "POST" and not isinstance(reason, ConnectionRefusedError)
                ),
            ) from exc
        elapsed_ms = (time.monotonic() - started) * 1000
        if len(payload) > MAX_RESPONSE_BYTES:
            raise StudyTransportError("Feng worker response exceeds its bounded size")
        try:
            value = _strict_object(payload, "worker response")
        except StudyValidationError as exc:
            raise StudyTransportError("Feng worker returned invalid JSON") from exc
        if status >= 400:
            code = value.get("error", {}).get("code", "REMOTE_ERROR")
            raise StudyTransportError(
                f"Feng worker rejected request: {status} {code}",
                acceptance_ambiguous=(method == "POST" and status >= 500),
            )
        return RemoteResponse(status, value, elapsed_ms)

    def submit(
        self,
        request: Mapping[str, Any],
        *,
        dispatch_generation: int = 1,
        on_post_start: Callable[[], bool] | None = None,
    ) -> RemoteResponse:
        return self._request(
            "POST",
            "/v1/studies",
            canonical_json_bytes(_dispatch_envelope(request, dispatch_generation)),
            on_post_start=on_post_start,
        )

    def fence(self, request: Mapping[str, Any], through_generation: int) -> RemoteResponse:
        frozen = validate_request(request)
        path = f"/v1/studies/{frozen['job_id']}/fence"
        return self._request(
            "POST", path, canonical_json_bytes(_fence_envelope(frozen, through_generation))
        )

    def read(self, job_id: str) -> RemoteResponse:
        if HEX_64.fullmatch(job_id) is None:
            raise StudyValidationError("job identity is invalid")
        return self._request("GET", f"/v1/studies/{job_id}")


STUDY_SCHEMA_SQL = f"""
CREATE SCHEMA IF NOT EXISTS qr_study;
CREATE TABLE IF NOT EXISTS qr_study.schema_identity (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    identity text NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO qr_study.schema_identity(singleton, identity)
VALUES (true, '{SCHEMA_IDENTITY}')
ON CONFLICT (singleton) DO UPDATE SET identity=EXCLUDED.identity
WHERE qr_study.schema_identity.identity IN (
    'quantresearch-lightweight-study-postgresql-v1',
    'quantresearch-lightweight-study-postgresql-v2',
    'quantresearch-lightweight-study-postgresql-v3'
);
CREATE TABLE IF NOT EXISTS qr_study.jobs (
    study_id text PRIMARY KEY CHECK (study_id ~ '^[0-9a-f]{{64}}$'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{{64}}$'),
    frozen_request jsonb NOT NULL CHECK (jsonb_typeof(frozen_request) = 'object'),
    worker_endpoint text NOT NULL,
    worker_image text NOT NULL CHECK (worker_image ~ '^sha256:[0-9a-f]{{64}}$'),
    source_commit text NOT NULL CHECK (source_commit ~ '^[0-9a-f]{{40}}$'),
    source_tree text NOT NULL CHECK (source_tree ~ '^[0-9a-f]{{40}}$'),
    status text NOT NULL CHECK (status IN ('ACCEPTED','DISPATCHED','RUNNING','SUCCEEDED','FAILED')),
    acceptance_ambiguous boolean NOT NULL DEFAULT false,
    ambiguous_through_generation bigint NOT NULL DEFAULT 0 CHECK (ambiguous_through_generation >= 0),
    dispatch_generation bigint NOT NULL DEFAULT 0 CHECK (dispatch_generation >= 0),
    dispatch_claim_id text CHECK (dispatch_claim_id ~ '^[0-9a-f]{{32}}$'),
    dispatch_claim_expires_at timestamptz,
    dispatch_post_started boolean NOT NULL DEFAULT false,
    latest_progress jsonb,
    final_result jsonb,
    failure jsonb,
    negative_receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((dispatch_claim_id IS NULL) = (dispatch_claim_expires_at IS NULL)),
    CHECK (NOT dispatch_post_started OR dispatch_claim_id IS NOT NULL),
    CHECK ((status = 'SUCCEEDED') = (final_result IS NOT NULL)),
    CHECK ((status = 'FAILED') = (failure IS NOT NULL))
);
ALTER TABLE qr_study.jobs
    ADD COLUMN IF NOT EXISTS acceptance_ambiguous boolean NOT NULL DEFAULT false;
ALTER TABLE qr_study.jobs
    ADD COLUMN IF NOT EXISTS ambiguous_through_generation bigint NOT NULL DEFAULT 0;
ALTER TABLE qr_study.jobs
    ADD COLUMN IF NOT EXISTS dispatch_generation bigint NOT NULL DEFAULT 0;
ALTER TABLE qr_study.jobs ADD COLUMN IF NOT EXISTS dispatch_claim_id text;
ALTER TABLE qr_study.jobs ADD COLUMN IF NOT EXISTS dispatch_claim_expires_at timestamptz;
ALTER TABLE qr_study.jobs
    ADD COLUMN IF NOT EXISTS dispatch_post_started boolean NOT NULL DEFAULT false;
ALTER TABLE qr_study.jobs ADD COLUMN IF NOT EXISTS negative_receipt jsonb;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='valid_study_dispatch_generations'
          AND conrelid='qr_study.jobs'::regclass
    ) THEN
        ALTER TABLE qr_study.jobs ADD CONSTRAINT valid_study_dispatch_generations CHECK (
            ambiguous_through_generation >= 0 AND dispatch_generation >= 0 AND
            ambiguous_through_generation <= dispatch_generation
        );
    END IF;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='valid_study_dispatch_claim'
          AND conrelid='qr_study.jobs'::regclass
    ) THEN
        ALTER TABLE qr_study.jobs ADD CONSTRAINT valid_study_dispatch_claim CHECK (
            (dispatch_claim_id IS NULL AND dispatch_claim_expires_at IS NULL) OR
            (dispatch_claim_id ~ '^[0-9a-f]{{32}}$' AND dispatch_claim_expires_at IS NOT NULL)
        );
    END IF;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='valid_study_dispatch_post_started'
          AND conrelid='qr_study.jobs'::regclass
    ) THEN
        ALTER TABLE qr_study.jobs ADD CONSTRAINT valid_study_dispatch_post_started CHECK (
            NOT dispatch_post_started OR dispatch_claim_id IS NOT NULL
        );
    END IF;
END $$;
CREATE OR REPLACE FUNCTION qr_study.reject_terminal_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'terminal Study rows are immutable'; END $$;
DROP TRIGGER IF EXISTS immutable_terminal_study_update ON qr_study.jobs;
CREATE TRIGGER immutable_terminal_study_update BEFORE UPDATE ON qr_study.jobs
FOR EACH ROW WHEN (OLD.status IN ('SUCCEEDED','FAILED'))
EXECUTE FUNCTION qr_study.reject_terminal_mutation();
DROP TRIGGER IF EXISTS immutable_terminal_study_delete ON qr_study.jobs;
CREATE TRIGGER immutable_terminal_study_delete BEFORE DELETE ON qr_study.jobs
FOR EACH ROW WHEN (OLD.status IN ('SUCCEEDED','FAILED'))
EXECUTE FUNCTION qr_study.reject_terminal_mutation();
"""


@dataclass(frozen=True)
class StudyPostgresConfig:
    host: str
    port: int
    dbname: str
    user: str
    password_file: Path
    connect_timeout: int = 5

    @classmethod
    def from_environment(cls) -> "StudyPostgresConfig":
        prefix = "QR_STUDY_POSTGRES_"
        password_file = os.environ.get(f"{prefix}PASSWORD_FILE")
        if not password_file:
            raise StudyRemoteError(f"{prefix}PASSWORD_FILE is required")
        try:
            port = int(os.environ.get(f"{prefix}PORT", "5432"))
            timeout = int(os.environ.get(f"{prefix}CONNECT_TIMEOUT", "5"))
        except ValueError as exc:
            raise StudyRemoteError("PostgreSQL port/timeout must be integers") from exc
        return cls(
            host=os.environ.get(f"{prefix}HOST", "postgres"),
            port=port,
            dbname=os.environ.get(f"{prefix}DATABASE", "quantresearch"),
            user=os.environ.get(f"{prefix}USER", "qr_runtime"),
            password_file=Path(password_file),
            connect_timeout=timeout,
        ).validated()

    def validated(self) -> "StudyPostgresConfig":
        if not self.host or not self.dbname or not self.user:
            raise StudyRemoteError("PostgreSQL connection identity is incomplete")
        if not 1 <= self.port <= 65535 or not 1 <= self.connect_timeout <= 60:
            raise StudyRemoteError("PostgreSQL port/timeout is outside its allowed range")
        try:
            metadata = os.stat(self.password_file, follow_symlinks=False)
        except OSError as exc:
            raise StudyRemoteError("PostgreSQL password file is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StudyRemoteError("PostgreSQL password file must be a regular file")
        return self

    def password(self) -> str:
        try:
            value = self.password_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise StudyRemoteError("PostgreSQL password file is unreadable") from exc
        if not value or "\0" in value or "\n" in value or "\r" in value:
            raise StudyRemoteError("PostgreSQL password file has invalid content")
        return value

    def connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(
                host=self.host,
                port=self.port,
                dbname=self.dbname,
                user=self.user,
                password=self.password(),
                connect_timeout=self.connect_timeout,
                row_factory=dict_row,
            )
        except (ImportError, OSError) as exc:
            raise StudyRemoteError("PostgreSQL client is unavailable") from exc


class StudyPostgresStore:
    """One authoritative PostgreSQL row per lightweight Study, independent of Attempts."""

    def __init__(self, config: Any):
        self.config = config.validated()

    @classmethod
    def from_environment(cls) -> "StudyPostgresStore":
        return cls(StudyPostgresConfig.from_environment())

    def initialize(self) -> None:
        with self.config.connect() as connection:
            connection.execute(STUDY_SCHEMA_SQL)
            row = connection.execute(
                "SELECT identity FROM qr_study.schema_identity WHERE singleton"
            ).fetchone()
            if row is None or row["identity"] != SCHEMA_IDENTITY:
                raise StudyRemoteError("lightweight Study schema identity conflicts")

    def verify_initialized(self) -> None:
        """Verify an already-provisioned authority without requiring migration privileges."""
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT identity FROM qr_study.schema_identity WHERE singleton"
            ).fetchone()
            if row is None or row["identity"] != SCHEMA_IDENTITY:
                raise StudyRemoteError("lightweight Study schema identity conflicts")

    def admit(self, request: Mapping[str, Any], endpoint: str) -> tuple[dict[str, Any], bool]:
        frozen = validate_request(request)
        digest = hashlib.sha256(canonical_json_bytes(frozen)).hexdigest()
        with self.config.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO qr_study.jobs(
                    study_id, request_digest, frozen_request, worker_endpoint,
                    worker_image, source_commit, source_tree, status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'ACCEPTED')
                ON CONFLICT (study_id) DO NOTHING RETURNING *
                """,
                (
                    frozen["job_id"],
                    digest,
                    _jsonb(frozen),
                    endpoint,
                    frozen["worker_image"],
                    frozen["source_commit"],
                    frozen["source_tree"],
                ),
            ).fetchone()
            if row is not None:
                return self._normalized(row), True
            row = connection.execute(
                "SELECT * FROM qr_study.jobs WHERE study_id=%s FOR UPDATE",
                (frozen["job_id"],),
            ).fetchone()
            if row is None:
                raise StudyRemoteError("authoritative Study row disappeared")
            if row["request_digest"] != digest or row["frozen_request"] != frozen:
                raise StudyIdempotencyConflict("Study identity already has different frozen inputs")
            return self._normalized(row), False

    @staticmethod
    def _normalized(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: (value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value)
            for key, value in row.items()
        }

    def get(self, study_id: str) -> dict[str, Any] | None:
        if HEX_64.fullmatch(study_id) is None:
            raise StudyValidationError("Study identity is invalid")
        with self.config.connect() as connection:
            row = connection.execute(
                "SELECT * FROM qr_study.jobs WHERE study_id=%s", (study_id,)
            ).fetchone()
            return None if row is None else self._normalized(row)

    def mark_dispatched(
        self, study_id: str, remote: Mapping[str, Any], dispatch_claim_id: str
    ) -> dict[str, Any]:
        self._validate_dispatch_claim(dispatch_claim_id)
        return self._record_remote(
            study_id, remote, dispatch=True, dispatch_claim_id=dispatch_claim_id
        )

    def observe(self, study_id: str, remote: Mapping[str, Any]) -> dict[str, Any]:
        return self._record_remote(study_id, remote, dispatch=False)

    @staticmethod
    def _validate_dispatch_claim(dispatch_claim_id: str) -> None:
        if HEX_32.fullmatch(dispatch_claim_id) is None:
            raise StudyValidationError("dispatch claim identity is invalid")

    def begin_dispatch(
        self, study_id: str, dispatch_claim_id: str, *, lease_seconds: int
    ) -> tuple[dict[str, Any], bool]:
        self._validate_dispatch_claim(dispatch_claim_id)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise StudyValidationError("dispatch claim lease must be between 1 and 300 seconds")
        with self.config.connect() as connection:
            current = connection.execute(
                "SELECT * FROM qr_study.jobs WHERE study_id=%s FOR UPDATE", (study_id,)
            ).fetchone()
            if current is None:
                raise StudyRemoteError("authoritative Study row disappeared")
            if current["status"] in TERMINAL_STATES:
                return self._normalized(current), False
            row = connection.execute(
                """
                UPDATE qr_study.jobs SET
                    acceptance_ambiguous=(acceptance_ambiguous OR dispatch_post_started),
                    ambiguous_through_generation=CASE
                        WHEN dispatch_post_started THEN
                            greatest(ambiguous_through_generation, dispatch_generation)
                        ELSE ambiguous_through_generation END,
                    dispatch_generation=dispatch_generation + 1,
                    dispatch_claim_id=%s,
                    dispatch_claim_expires_at=clock_timestamp() + (%s * interval '1 second'),
                    dispatch_post_started=false, updated_at=clock_timestamp()
                WHERE study_id=%s AND (
                    dispatch_claim_id IS NULL OR dispatch_claim_id=%s OR
                    dispatch_claim_expires_at <= clock_timestamp()
                ) RETURNING *
                """,
                (dispatch_claim_id, lease_seconds, study_id, dispatch_claim_id),
            ).fetchone()
            if row is None:
                return self._normalized(current), False
            return self._normalized(row), True

    def mark_post_started(self, study_id: str, dispatch_claim_id: str) -> bool:
        self._validate_dispatch_claim(dispatch_claim_id)
        with self.config.connect() as connection:
            row = connection.execute(
                """
                UPDATE qr_study.jobs SET dispatch_post_started=true,
                    updated_at=clock_timestamp()
                WHERE study_id=%s AND status='ACCEPTED'
                  AND dispatch_claim_id=%s
                  AND dispatch_claim_expires_at > clock_timestamp()
                RETURNING study_id
                """,
                (study_id, dispatch_claim_id),
            ).fetchone()
            return row is not None

    def mark_acceptance_ambiguous(
        self, study_id: str, dispatch_claim_id: str
    ) -> dict[str, Any]:
        self._validate_dispatch_claim(dispatch_claim_id)
        with self.config.connect() as connection:
            current = connection.execute(
                "SELECT * FROM qr_study.jobs WHERE study_id=%s FOR UPDATE", (study_id,)
            ).fetchone()
            if current is None:
                raise StudyRemoteError("authoritative Study row disappeared")
            if current["status"] in TERMINAL_STATES:
                return self._normalized(current)
            row = connection.execute(
                """
                UPDATE qr_study.jobs SET
                    acceptance_ambiguous=(
                        acceptance_ambiguous OR
                        (status='ACCEPTED' AND dispatch_claim_id=%s AND dispatch_post_started)
                    ),
                    ambiguous_through_generation=CASE
                        WHEN status='ACCEPTED' AND dispatch_claim_id=%s AND dispatch_post_started
                        THEN greatest(ambiguous_through_generation, dispatch_generation)
                        ELSE ambiguous_through_generation END,
                    dispatch_claim_id=CASE WHEN dispatch_claim_id=%s THEN NULL
                                           ELSE dispatch_claim_id END,
                    dispatch_claim_expires_at=CASE WHEN dispatch_claim_id=%s THEN NULL
                                                   ELSE dispatch_claim_expires_at END,
                    dispatch_post_started=CASE WHEN dispatch_claim_id=%s THEN false
                                               ELSE dispatch_post_started END,
                    updated_at=clock_timestamp() WHERE study_id=%s RETURNING *
                """,
                (
                    dispatch_claim_id,
                    dispatch_claim_id,
                    dispatch_claim_id,
                    dispatch_claim_id,
                    dispatch_claim_id,
                    study_id,
                ),
            ).fetchone()
            return self._normalized(row)

    def _record_remote(
        self,
        study_id: str,
        remote: Mapping[str, Any],
        *,
        dispatch: bool,
        dispatch_claim_id: str | None = None,
    ) -> dict[str, Any]:
        status = remote.get("status")
        if status not in {"ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"}:
            raise StudyTransportError("Feng worker returned an unsupported state")
        with self.config.connect() as connection:
            current = connection.execute(
                "SELECT * FROM qr_study.jobs WHERE study_id=%s FOR UPDATE", (study_id,)
            ).fetchone()
            if current is None:
                raise StudyRemoteError("authoritative Study row disappeared")
            if current["status"] in TERMINAL_STATES:
                expected = current["final_result"] if status == "SUCCEEDED" else current["failure"]
                actual = remote.get("result") if status == "SUCCEEDED" else remote.get("failure")
                if current["status"] != status or expected != actual:
                    raise StudyIdempotencyConflict("terminal Study result conflicts with Feng read-back")
                return self._normalized(current)
            target = "DISPATCHED" if dispatch and status == "ACCEPTED" else status
            progress = remote.get("progress")
            result = remote.get("result") if target == "SUCCEEDED" else None
            failure = remote.get("failure") if target == "FAILED" else None
            row = connection.execute(
                """
                UPDATE qr_study.jobs SET status=%s, latest_progress=%s,
                    final_result=%s, failure=%s, acceptance_ambiguous=false,
                    ambiguous_through_generation=0, negative_receipt=NULL,
                    dispatch_claim_id=NULL, dispatch_claim_expires_at=NULL,
                    dispatch_post_started=false, updated_at=clock_timestamp()
                WHERE study_id=%s RETURNING *
                """,
                (
                    target,
                    _jsonb(progress) if progress is not None else None,
                    _jsonb(result) if result is not None else None,
                    _jsonb(failure) if failure is not None else None,
                    study_id,
                ),
            ).fetchone()
            return self._normalized(row)

    def resolve_fenced_absence(
        self,
        study_id: str,
        dispatch_claim_id: str,
        receipt: Mapping[str, Any],
        message: str,
    ) -> dict[str, Any]:
        self._validate_dispatch_claim(dispatch_claim_id)
        _exact_fields(
            receipt,
            {
                "schema_version",
                "protocol",
                "outcome",
                "job_id",
                "request_digest",
                "fenced_through_generation",
                "negative_receipt_id",
                "updated_at",
                "job",
            },
            "Feng negative receipt",
        )
        if (
            receipt.get("schema_version") != 1
            or receipt.get("protocol") != DISPATCH_PROTOCOL
            or receipt.get("outcome") != "ABSENT_FENCED"
            or receipt.get("job_id") != study_id
            or receipt.get("job") is not None
            or HEX_64.fullmatch(str(receipt.get("request_digest", ""))) is None
            or HEX_64.fullmatch(str(receipt.get("negative_receipt_id", ""))) is None
        ):
            raise StudyTransportError("Feng negative receipt identity is invalid")
        through_generation = _validate_dispatch_generation(
            receipt.get("fenced_through_generation"), "fenced_through_generation"
        )
        receipt_identity = {
            "schema_version": 1,
            "protocol": DISPATCH_PROTOCOL,
            "job_id": study_id,
            "request_digest": receipt["request_digest"],
            "fenced_through_generation": through_generation,
        }
        expected_receipt_id = hashlib.sha256(
            canonical_json_bytes(receipt_identity)
        ).hexdigest()
        if not hmac.compare_digest(str(receipt["negative_receipt_id"]), expected_receipt_id):
            raise StudyTransportError("Feng negative receipt digest is invalid")
        failure = {
            "code": "FENG_UNAVAILABLE",
            "message": message,
            "local_compute_attempted": False,
            "negative_receipt_id": receipt["negative_receipt_id"],
        }
        with self.config.connect() as connection:
            current = connection.execute(
                "SELECT * FROM qr_study.jobs WHERE study_id=%s FOR UPDATE", (study_id,)
            ).fetchone()
            if current is None:
                raise StudyRemoteError("authoritative Study row disappeared")
            if current["status"] in TERMINAL_STATES:
                return self._normalized(current)
            if current["dispatch_claim_id"] != dispatch_claim_id:
                return self._normalized(current)
            if (
                current["request_digest"] != receipt["request_digest"]
                or not current["acceptance_ambiguous"]
                or through_generation < current["ambiguous_through_generation"]
                or through_generation < current["dispatch_generation"]
            ):
                raise StudyTransportError("Feng negative receipt does not fence possible acceptance")
            row = connection.execute(
                """
                UPDATE qr_study.jobs SET status='FAILED', failure=%s,
                    acceptance_ambiguous=false, ambiguous_through_generation=0,
                    negative_receipt=%s, dispatch_claim_id=NULL,
                    dispatch_claim_expires_at=NULL, dispatch_post_started=false,
                    updated_at=clock_timestamp()
                WHERE study_id=%s AND dispatch_claim_id=%s RETURNING *
                """,
                (
                    _jsonb(failure),
                    _jsonb(dict(receipt)),
                    study_id,
                    dispatch_claim_id,
                ),
            ).fetchone()
            return self._normalized(row)

    def fail_unavailable(
        self, study_id: str, dispatch_claim_id: str, message: str
    ) -> dict[str, Any]:
        self._validate_dispatch_claim(dispatch_claim_id)
        failure = {
            "code": "FENG_UNAVAILABLE",
            "message": message,
            "local_compute_attempted": False,
        }
        with self.config.connect() as connection:
            current = connection.execute(
                "SELECT * FROM qr_study.jobs WHERE study_id=%s FOR UPDATE", (study_id,)
            ).fetchone()
            if current is None:
                raise StudyRemoteError("authoritative Study row disappeared")
            if current["status"] in TERMINAL_STATES:
                return self._normalized(current)
            if current["dispatch_claim_id"] != dispatch_claim_id:
                return self._normalized(current)
            if current["acceptance_ambiguous"] or current["status"] != "ACCEPTED":
                row = connection.execute(
                    """
                    UPDATE qr_study.jobs SET dispatch_claim_id=NULL,
                        dispatch_claim_expires_at=NULL, dispatch_post_started=false,
                        updated_at=clock_timestamp()
                    WHERE study_id=%s AND dispatch_claim_id=%s RETURNING *
                    """,
                    (study_id, dispatch_claim_id),
                ).fetchone()
                return self._normalized(row)
            row = connection.execute(
                """
                UPDATE qr_study.jobs SET status='FAILED', failure=%s,
                    dispatch_claim_id=NULL, dispatch_claim_expires_at=NULL,
                    dispatch_post_started=false, updated_at=clock_timestamp()
                WHERE study_id=%s AND dispatch_claim_id=%s RETURNING *
                """,
                (_jsonb(failure), study_id, dispatch_claim_id),
            ).fetchone()
            return self._normalized(row)

    def storage_shape(self, study_id: str) -> dict[str, Any]:
        with self.config.connect() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS study_rows,
                       coalesce(sum(octet_length(frozen_request::text)),0) AS frozen_bytes,
                       coalesce(sum(octet_length(coalesce(latest_progress::text,''))),0) AS progress_bytes,
                       coalesce(sum(octet_length(coalesce(final_result::text,''))),0) AS result_bytes,
                       coalesce(sum(octet_length(coalesce(failure::text,''))),0) AS failure_bytes
                FROM qr_study.jobs WHERE study_id=%s
                """,
                (study_id,),
            ).fetchone()
            objects = connection.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='qr_study' ORDER BY table_name
                """
            ).fetchall()
        return {
            **dict(row),
            "tables": [item["table_name"] for item in objects],
            "attempt_rows": 0,
            "experiment_rows": 0,
            "per_iteration_event_rows": 0,
        }


class StudyDispatcher:
    DISPATCH_LEASE_SECONDS: int = 30

    def __init__(self, store: StudyPostgresStore, client: SignedStudyClient):
        self.store = store
        self.client = client

    def submit(self, request: Mapping[str, Any]) -> dict[str, Any]:
        frozen = validate_request(request)
        self.store.admit(frozen, self.client.endpoint)
        dispatch_claim_id = secrets.token_hex(16)
        authoritative, acquired = self.store.begin_dispatch(
            frozen["job_id"], dispatch_claim_id, lease_seconds=self.DISPATCH_LEASE_SECONDS
        )
        if not acquired:
            return {
                "authoritative": authoritative,
                "remote": None,
                "idempotent_replay": False,
                "dispatch_elapsed_ms": None,
                "local_compute_attempted": False,
            }
        dispatch_generation = int(authoritative["dispatch_generation"])
        if authoritative["acceptance_ambiguous"]:
            return self._reconcile_prior_ambiguity(
                frozen, dispatch_claim_id, dispatch_generation
            )
        try:
            response = self.client.submit(
                frozen,
                dispatch_generation=dispatch_generation,
                on_post_start=lambda: self.store.mark_post_started(
                    frozen["job_id"], dispatch_claim_id
                ),
            )
            return self._record_submit_response(frozen["job_id"], dispatch_claim_id, response)
        except StudyTransportError as exc:
            if exc.acceptance_ambiguous:
                return self._reconcile_ambiguous_submit(
                    frozen, dispatch_claim_id, dispatch_generation, exc
                )
            return {
                "authoritative": self.store.fail_unavailable(
                    frozen["job_id"], dispatch_claim_id, str(exc)
                ),
                "remote": None,
                "idempotent_replay": False,
                "dispatch_elapsed_ms": None,
                "local_compute_attempted": False,
            }

    def _reconcile_prior_ambiguity(
        self,
        request: Mapping[str, Any],
        dispatch_claim_id: str,
        dispatch_generation: int,
    ) -> dict[str, Any]:
        study_id = str(request["job_id"])
        try:
            response = self.client.fence(request, dispatch_generation)
            receipt = response.value.get("fence")
            if not isinstance(receipt, dict) or receipt.get("job_id") != study_id:
                raise StudyTransportError("Feng worker returned an invalid fence receipt")
            outcome = receipt.get("outcome")
            if outcome == "EXISTS":
                remote = receipt.get("job")
                if not isinstance(remote, dict) or remote.get("job_id") != study_id:
                    raise StudyTransportError("Feng fence returned the wrong job identity")
                authoritative = self.store.observe(study_id, remote)
            elif outcome == "ABSENT_FENCED":
                remote = None
                authoritative = self.store.resolve_fenced_absence(
                    study_id,
                    dispatch_claim_id,
                    receipt,
                    "authenticated Feng fence proved no accepted Study task",
                )
            elif outcome == "IN_FLIGHT":
                remote = None
                authoritative = self.store.mark_acceptance_ambiguous(
                    study_id, dispatch_claim_id
                )
            else:
                raise StudyTransportError("Feng worker returned an unsupported fence outcome")
            return {
                "authoritative": authoritative,
                "remote": remote,
                "negative_receipt": receipt if outcome == "ABSENT_FENCED" else None,
                "idempotent_replay": False,
                "dispatch_elapsed_ms": None,
                "local_compute_attempted": False,
            }
        except StudyTransportError:
            authoritative = self.store.mark_acceptance_ambiguous(study_id, dispatch_claim_id)
            return {
                "authoritative": authoritative,
                "remote": None,
                "negative_receipt": None,
                "idempotent_replay": False,
                "dispatch_elapsed_ms": None,
                "local_compute_attempted": False,
            }

    def _record_submit_response(
        self, study_id: str, dispatch_claim_id: str, response: RemoteResponse
    ) -> dict[str, Any]:
        remote = response.value.get("job")
        if not isinstance(remote, dict) or remote.get("job_id") != study_id:
            raise StudyTransportError("Feng worker returned the wrong job identity")
        return {
            "authoritative": self.store.mark_dispatched(study_id, remote, dispatch_claim_id),
            "remote": remote,
            "idempotent_replay": response.value.get("idempotent_replay") is True,
            "dispatch_elapsed_ms": response.elapsed_ms,
            "local_compute_attempted": False,
        }

    def _reconcile_ambiguous_submit(
        self,
        request: Mapping[str, Any],
        dispatch_claim_id: str,
        dispatch_generation: int,
        initial_error: StudyTransportError,
    ) -> dict[str, Any]:
        study_id = str(request["job_id"])
        self.store.mark_acceptance_ambiguous(study_id, dispatch_claim_id)
        try:
            response = self.client.read(study_id)
            remote = response.value.get("job")
            if not isinstance(remote, dict) or remote.get("job_id") != study_id:
                raise StudyTransportError("Feng worker returned the wrong job identity")
            return {
                "authoritative": self.store.mark_dispatched(
                    study_id, remote, dispatch_claim_id
                ),
                "remote": remote,
                "idempotent_replay": False,
                "dispatch_elapsed_ms": None,
                "local_compute_attempted": False,
            }
        except StudyTransportError:
            pass

        try:
            return self._record_submit_response(
                study_id,
                dispatch_claim_id,
                self.client.submit(request, dispatch_generation=dispatch_generation),
            )
        except StudyTransportError:
            authoritative = self.store.get(study_id)
            if authoritative is None:
                raise StudyRemoteError("authoritative Study row disappeared") from initial_error
            return {
                "authoritative": authoritative,
                "remote": None,
                "idempotent_replay": False,
                "dispatch_elapsed_ms": None,
                "local_compute_attempted": False,
            }

    def read(self, study_id: str) -> dict[str, Any]:
        response = self.client.read(study_id)
        remote = response.value.get("job")
        if not isinstance(remote, dict) or remote.get("job_id") != study_id:
            raise StudyTransportError("Feng worker returned the wrong job identity")
        authoritative = self.store.observe(study_id, remote)
        return {
            "authoritative": authoritative,
            "remote": remote,
            "read_elapsed_ms": response.elapsed_ms,
            "local_compute_attempted": False,
        }


def _probe(url: str, host: str | None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read(4096)
            status = response.status
        error = None
    except Exception as exc:
        status = None
        error = type(exc).__name__
    return {
        "status": status,
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "error": error,
    }


def _worker_command(args: argparse.Namespace) -> int:
    server = StudyWorkerServer(
        (args.host, args.port),
        WorkerJobStore(args.state_root, args.worker_image),
        Path(args.public_key),
    )
    server.serve_forever()
    return 0


def _synthetic_request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return freeze_synthetic_request(
        iterations=args.iterations,
        seed=args.seed,
        checkpoint_count=args.checkpoint_count,
        source_commit=args.source_commit,
        source_tree=args.source_tree,
        worker_image=args.worker_image,
    )


def _training_request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return freeze_training_request(
        trial_budget=args.trial_budget,
        seed=args.seed,
        checkpoint_count=args.checkpoint_count,
        parameter_low=args.parameter_low,
        parameter_high=args.parameter_high,
        objective_target=args.objective_target,
        source_commit=args.source_commit,
        source_tree=args.source_tree,
        worker_image=args.worker_image,
    )


def _execute_command(args: argparse.Namespace) -> int:
    request = args.request_factory(args)
    store = StudyPostgresStore.from_environment()
    store.verify_initialized()
    dispatcher = StudyDispatcher(
        store,
        SignedStudyClient(
            args.endpoint,
            Path(args.private_key),
            tls_ca_file=Path(args.tls_ca_file) if args.tls_ca_file else None,
        ),
    )
    submission = dispatcher.submit(request)
    probes: list[dict[str, Any]] = []
    reads: list[float] = []
    authoritative = submission["authoritative"]
    while authoritative["status"] not in TERMINAL_STATES:
        if args.health_url:
            probes.append(_probe(args.health_url, args.health_host))
        time.sleep(args.poll_interval)
        observed = dispatcher.read(request["job_id"])
        reads.append(observed["read_elapsed_ms"])
        authoritative = observed["authoritative"]
    output = {
        "schema_version": 1,
        "request": request,
        "submission": submission,
        "authoritative": authoritative,
        "storage_shape": store.storage_shape(request["job_id"]),
        "health_probes": probes,
        "remote_read_elapsed_ms": reads,
    }
    print(canonical_json_bytes(output).decode("utf-8"))
    return 0 if authoritative["status"] == "SUCCEEDED" else 2


def _inspect_command(args: argparse.Namespace) -> int:
    store = StudyPostgresStore.from_environment()
    value = store.get(args.study_id)
    if value is None:
        raise StudyRemoteError("Study does not exist")
    print(
        canonical_json_bytes(
            {"authoritative": value, "storage_shape": store.storage_shape(args.study_id)}
        ).decode("utf-8")
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lightweight authenticated Study/Feng prototype")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--host", default="0.0.0.0")
    worker.add_argument("--port", type=int, default=8091)
    worker.add_argument("--state-root", required=True)
    worker.add_argument("--public-key", required=True)
    worker.add_argument("--worker-image", required=True)
    worker.set_defaults(handler=_worker_command)

    execute = commands.add_parser("execute")
    execute.add_argument("--endpoint", required=True)
    execute.add_argument("--private-key", required=True)
    execute.add_argument("--tls-ca-file")
    execute.add_argument("--iterations", required=True, type=int)
    execute.add_argument("--seed", required=True, type=int)
    execute.add_argument("--checkpoint-count", type=int, default=8)
    execute.add_argument("--source-commit", required=True)
    execute.add_argument("--source-tree", required=True)
    execute.add_argument("--worker-image", required=True)
    execute.add_argument("--poll-interval", type=float, default=0.2)
    execute.add_argument("--health-url")
    execute.add_argument("--health-host")
    execute.set_defaults(
        handler=_execute_command,
        request_factory=_synthetic_request_from_args,
    )

    execute_training = commands.add_parser("execute-training")
    execute_training.add_argument("--endpoint", required=True)
    execute_training.add_argument("--private-key", required=True)
    execute_training.add_argument("--tls-ca-file")
    execute_training.add_argument("--trial-budget", required=True, type=int)
    execute_training.add_argument("--seed", required=True, type=int)
    execute_training.add_argument("--checkpoint-count", type=int, default=8)
    execute_training.add_argument("--parameter-low", type=float, default=0.0)
    execute_training.add_argument("--parameter-high", type=float, default=1.0)
    execute_training.add_argument("--objective-target", type=float, default=0.61803398875)
    execute_training.add_argument("--source-commit", required=True)
    execute_training.add_argument("--source-tree", required=True)
    execute_training.add_argument("--worker-image", required=True)
    execute_training.add_argument("--poll-interval", type=float, default=0.2)
    execute_training.add_argument("--health-url")
    execute_training.add_argument("--health-host")
    execute_training.set_defaults(
        handler=_execute_command,
        request_factory=_training_request_from_args,
    )

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--study-id", required=True)
    inspect.set_defaults(handler=_inspect_command)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return int(args.handler(args))
    except StudyRemoteError as exc:
        print(canonical_json_bytes({"ok": False, "error": str(exc)}).decode("utf-8"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
