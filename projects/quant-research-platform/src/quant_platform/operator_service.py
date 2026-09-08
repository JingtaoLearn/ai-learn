from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .attempt_report import (
    REPORT_BUNDLE_FILES,
    canonical_report_operator_bundle,
    verify_report_operator_bundle,
)
from .catalog import Catalog
from .isolation import build_operator_validation_command
from .postgres_persistence import (
    ArtifactInput,
    OperatorPersistenceConflict,
    PostgresOperatorPersistence,
)
from .runner import RunnerTerminationError, _terminate_container, reconcile_container
from .submissions import EXECUTION_ENVELOPE
from .schemas import (
    canonical_json_bytes,
    parse_semantic_version,
    validate_defaults,
    validate_parameter_schema,
    validate_parameters,
)


class OperatorSubmissionError(ValueError):
    """Raised when an operator candidate cannot be safely published."""


class OperatorConflictError(OperatorSubmissionError):
    """Raised when an immutable operator identity already has other content."""


class OperatorValidationRunError(OperatorSubmissionError):
    def __init__(self, message: str, *, outcome: str, control_path: str):
        super().__init__(message)
        self.outcome = outcome
        self.control_path = control_path


OPERATOR_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SUBMISSION_FIELDS = {
    "operator_id",
    "slot",
    "version",
    "source",
    "parameter_schema",
    "defaults",
    "title_zh",
    "summary_zh",
    "documentation",
    "tests",
}
FIXTURE_FIELDS = {"input", "parameters", "expected"}
SLOTS = {"fit", "smoothing", "statistic", "decision", "sizing", "cost", "report"}
MAX_SOURCE_BYTES = 64 * 1024
MAX_DOCUMENTATION_BYTES = 128 * 1024
MAX_FIXTURES = 50
Validator = Callable[[Path], dict[str, Any]]


def write_canonical_report_bundle(catalog: Catalog) -> tuple[str, dict[str, Any]]:
    """Materialize and verify the immutable API-v2 built-in report bundle."""

    bundle = canonical_report_operator_bundle()
    relative = Path("operators/canonical_attempt_report/1.0.0")
    target = catalog.state_root / relative
    if target.exists():
        identity = verify_report_operator_bundle(
            target,
            expected_content_digest=bundle["content_digest"],
        )
        return relative.as_posix(), identity
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".1.0.0-report-", dir=target.parent))
    try:
        for name, payload in bundle["content"].items():
            (staging / name).write_bytes(payload)
        (staging / "manifest.json").write_bytes(
            canonical_json_bytes(bundle["manifest"]) + b"\n"
        )
        (staging / "evidence.json").write_bytes(
            canonical_json_bytes(bundle["evidence"]) + b"\n"
        )
        if {path.name for path in staging.iterdir()} != REPORT_BUNDLE_FILES:
            raise OperatorSubmissionError("canonical report bundle topology is invalid")
        for path in staging.iterdir():
            metadata = os.stat(path, follow_symlinks=False)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise OperatorSubmissionError(
                    f"canonical report bundle contains unsafe file: {path.name}"
                )
            path.chmod(0o444)
        staging.chmod(0o555)
        os.rename(staging, target)
    finally:
        if staging.exists():
            staging.chmod(0o700)
            for path in staging.iterdir():
                path.chmod(0o600)
            shutil.rmtree(staging)
    identity = verify_report_operator_bundle(
        target,
        expected_content_digest=bundle["content_digest"],
    )
    return relative.as_posix(), identity


def _require_text(value: Any, path: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise OperatorSubmissionError(f"{path} must be non-empty text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized.encode("utf-8")) > maximum:
        raise OperatorSubmissionError(f"{path} exceeds its size limit")
    return normalized


def _normalize_submission(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperatorSubmissionError("operator submission must be an object")
    missing = sorted(SUBMISSION_FIELDS - set(value))
    unknown = sorted(set(value) - SUBMISSION_FIELDS)
    if missing:
        raise OperatorSubmissionError(f"operator submission has missing fields: {missing}")
    if unknown:
        raise OperatorSubmissionError(f"operator submission has unknown fields: {unknown}")
    operator_id = value["operator_id"]
    if not isinstance(operator_id, str) or OPERATOR_ID.fullmatch(operator_id) is None:
        raise OperatorSubmissionError("operator_id has invalid syntax")
    if value["slot"] not in SLOTS:
        raise OperatorSubmissionError(f"unsupported custom operator slot: {value['slot']}")
    parse_semantic_version(value["version"])
    source = _require_text(value["source"], "source", MAX_SOURCE_BYTES)
    documentation = _require_text(
        value["documentation"], "documentation", MAX_DOCUMENTATION_BYTES
    )
    title = _require_text(value["title_zh"], "title_zh", 256)
    summary = _require_text(value["summary_zh"], "summary_zh", 1024)
    schema = validate_parameter_schema(value["parameter_schema"])
    defaults = validate_defaults(schema, value["defaults"])
    tests = value["tests"]
    if not isinstance(tests, list) or not tests or len(tests) > MAX_FIXTURES:
        raise OperatorSubmissionError(
            f"tests must contain between 1 and {MAX_FIXTURES} fixtures"
        )
    normalized_tests: list[dict[str, Any]] = []
    for index, case in enumerate(tests):
        if not isinstance(case, dict) or set(case) != FIXTURE_FIELDS:
            raise OperatorSubmissionError(f"tests[{index}] must have exact fixture fields")
        normalized_tests.append(
            {
                "input": case["input"],
                "parameters": validate_parameters(schema, case["parameters"]),
                "expected": case["expected"],
            }
        )
    normalized = {
        "operator_id": operator_id,
        "slot": value["slot"],
        "version": value["version"],
        "source": source,
        "parameter_schema": schema,
        "defaults": defaults,
        "title_zh": title,
        "summary_zh": summary,
        "documentation": documentation,
        "tests": normalized_tests,
    }
    canonical_json_bytes(normalized)
    return normalized


class OperatorService:
    def __init__(
        self,
        persistence: PostgresOperatorPersistence,
        *,
        validator: Validator | None = None,
        runner_image: str | None = None,
    ):
        self.persistence = persistence
        self.runner_image = runner_image
        self.validator = validator or self._docker_validator

    def _docker_validator(self, candidate: Path) -> dict[str, Any]:
        if self.runner_image is None:
            raise OperatorSubmissionError("a pinned operator validator image is required")
        digest = json.loads(
            (candidate / "manifest.json").read_text(encoding="utf-8")
        )["content_digest"]
        evidence_root = candidate.parent / ".validation-evidence" / digest
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        control_dir = Path(tempfile.mkdtemp(prefix=".run-", dir=evidence_root))
        cidfile = control_dir / "container.cid"
        stdout_path = control_dir / "stdout.log"
        stderr_path = control_dir / "stderr.log"
        command = build_operator_validation_command(
            candidate, cidfile, self.runner_image
        )
        container_name = command[command.index("--name") + 1]
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        exit_status: int | None = None
        outcome = "LAUNCH_FAILED"
        termination_confirmed = True
        process = None
        control_finalized = False
        try:
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        shell=False,
                        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                        start_new_session=True,
                        close_fds=True,
                    )
                except OSError:
                    outcome = "LAUNCH_FAILED"
                else:
                    try:
                        exit_status = process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        try:
                            exit_status = _terminate_container(
                                cidfile, container_name, process
                            )
                            termination_confirmed = True
                            outcome = "TIMED_OUT"
                        except RunnerTerminationError:
                            termination_confirmed = False
                            outcome = "TERMINATION_UNCONFIRMED"
                    else:
                        termination_confirmed = (
                            reconcile_container(cidfile)
                            if cidfile.exists()
                            else exit_status != 0
                        )
                        outcome = (
                            "SUCCEEDED"
                            if exit_status == 0 and termination_confirmed
                            else (
                                "FAILED"
                                if termination_confirmed
                                else "TERMINATION_UNCONFIRMED"
                            )
                        )
                for stream in (stdout, stderr):
                    stream.flush()
                    os.fsync(stream.fileno())
            stdout_payload = stdout_path.read_bytes()
            stderr_payload = stderr_path.read_bytes()
            if len(stdout_payload) > 1_048_576 or len(stderr_payload) > 1_048_576:
                outcome = "OUTPUT_REJECTED"
            result = None
            if outcome == "SUCCEEDED":
                lines = stdout_payload.splitlines()
                if len(lines) != 1:
                    outcome = "OUTPUT_REJECTED"
                else:
                    try:
                        message = json.loads(lines[0])
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        outcome = "OUTPUT_REJECTED"
                    else:
                        if (
                            not isinstance(message, dict)
                            or set(message) != {"ok", "result"}
                            or message["ok"] is not True
                        ):
                            outcome = "OUTPUT_REJECTED"
                        else:
                            result = message["result"]
            finished_at = datetime.now(UTC)
            container_id = None
            if cidfile.exists() and not cidfile.is_symlink():
                candidate_id = cidfile.read_text(encoding="ascii").strip()
                if re.fullmatch(r"[0-9a-f]{64}", candidate_id):
                    container_id = candidate_id
            final_parent = (
                candidate.parent
                / (
                    ".validation-evidence"
                    if termination_confirmed
                    else ".validation-quarantine"
                )
                / digest
            )
            final_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            final = final_parent / control_dir.name.removeprefix(".")
            control_relative = f"transient/{digest}/{final.name}"
            evidence = {
                "schema_version": 1,
                "passed": outcome == "SUCCEEDED",
                "slot": result.get("slot") if isinstance(result, dict) else None,
                "candidate_digest": (
                    result.get("candidate_digest") if isinstance(result, dict) else digest
                ),
                "fixture_digest": (
                    result.get("fixture_digest")
                    if isinstance(result, dict)
                    else hashlib.sha256(
                        canonical_json_bytes(
                            json.loads(
                                (candidate / "tests.json").read_text(encoding="utf-8")
                            )
                        )
                    ).hexdigest()
                ),
                "validator_image": self.runner_image,
                "execution_envelope": EXECUTION_ENVELOPE,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": time.monotonic() - started_monotonic,
                "exit_status": exit_status,
                "outcome": outcome,
                "container_id": container_id,
                "termination_confirmed": termination_confirmed,
                "stdout_sha256": hashlib.sha256(stdout_payload).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr_payload).hexdigest(),
                "control_path": control_relative,
                "observations": (
                    result.get("observations") if isinstance(result, dict) else {}
                ),
            }
            (control_dir / "evidence.json").write_bytes(
                canonical_json_bytes(evidence) + b"\n"
            )
            os.rename(control_dir, final)
            control_dir = final
            self._seal_validation_control(control_dir)
            control_finalized = True
            if outcome != "SUCCEEDED":
                raise OperatorValidationRunError(
                    f"operator validation ended as {outcome}",
                    outcome=outcome,
                    control_path=control_relative,
                )
            return evidence
        except BaseException:
            if control_dir.exists() and not control_finalized:
                shutil.rmtree(control_dir, ignore_errors=True)
            raise

    def _seal_validation_control(self, control_dir: Path) -> None:
        allowed = {"container.cid", "stdout.log", "stderr.log", "evidence.json"}
        for path in control_dir.iterdir():
            metadata = os.stat(path, follow_symlinks=False)
            if (
                path.name not in allowed
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o7000
            ):
                raise OperatorSubmissionError(
                    f"validation control entry is unsafe: {path.name}"
                )
            path.chmod(0o444)
        control_dir.chmod(0o555)

    def submit(self, value: Any) -> dict[str, str]:
        submission = _normalize_submission(value)
        digest = hashlib.sha256(canonical_json_bytes(submission)).hexdigest()
        operator_id = submission["operator_id"]
        version = submission["version"]
        try:
            current = self.persistence.operator_detail(operator_id, version)
        except ValueError:
            current = None
        if current is not None:
            if current["content_digest"] != digest:
                raise OperatorConflictError(
                    f"{operator_id}@{version} already exists with different content"
                )
            self._verify_persisted_submission(current, submission=submission, digest=digest)
            return {
                "status": "NO_CHANGE",
                "operator_id": operator_id,
                "version": version,
                "content_digest": digest,
            }

        with tempfile.TemporaryDirectory(prefix=f"operator-{operator_id}-{version}-") as root:
            staging = Path(root) / "candidate"
            staging.mkdir(mode=0o700)
            worker_manifest = {
                key: submission[key]
                for key in (
                    "operator_id",
                    "slot",
                    "version",
                    "parameter_schema",
                    "defaults",
                    "title_zh",
                    "summary_zh",
                    "documentation",
                )
            }
            (staging / "operator.py").write_text(submission["source"], encoding="utf-8")
            (staging / "tests.json").write_bytes(
                canonical_json_bytes(submission["tests"]) + b"\n"
            )
            (staging / "documentation.md").write_text(
                submission["documentation"], encoding="utf-8"
            )
            (staging / "manifest.json").write_bytes(
                canonical_json_bytes(worker_manifest | {"content_digest": digest}) + b"\n"
            )
            evidence = self.validator(staging)
            self._verify_evidence(
                evidence,
                digest=digest,
                slot=submission["slot"],
                tests=submission["tests"],
            )
            (staging / "evidence.json").write_bytes(
                canonical_json_bytes(evidence) + b"\n"
            )
            artifacts = [
                ArtifactInput(
                    logical_name=path.name,
                    media_type={
                        ".json": "application/json",
                        ".md": "text/markdown; charset=utf-8",
                        ".py": "text/x-python; charset=utf-8",
                    }[path.suffix],
                    payload=path.read_bytes(),
                )
                for path in sorted(staging.iterdir())
            ]
            try:
                return self.persistence.publish_operator(
                    operator_id=operator_id,
                    slot=submission["slot"],
                    version=version,
                    title_zh=submission["title_zh"],
                    summary_zh=submission["summary_zh"],
                    content_digest=digest,
                    parameter_schema=submission["parameter_schema"],
                    defaults=submission["defaults"],
                    documentation=submission["documentation"],
                    validation_evidence=evidence,
                    artifacts=artifacts,
                    created_at=datetime.now(UTC),
                )
            except OperatorPersistenceConflict as exc:
                raise OperatorConflictError(str(exc)) from exc

    def _verify_persisted_submission(
        self,
        current: dict[str, Any],
        *,
        submission: dict[str, Any],
        digest: str,
    ) -> None:
        expected = {
            "operator.py": submission["source"].encode("utf-8"),
            "tests.json": canonical_json_bytes(submission["tests"]) + b"\n",
            "documentation.md": submission["documentation"].encode("utf-8"),
        }
        manifest = {
            key: submission[key]
            for key in (
                "operator_id",
                "slot",
                "version",
                "parameter_schema",
                "defaults",
                "title_zh",
                "summary_zh",
                "documentation",
            )
        } | {"content_digest": digest}
        expected["manifest.json"] = canonical_json_bytes(manifest) + b"\n"
        expected["evidence.json"] = canonical_json_bytes(current["validation_evidence"]) + b"\n"
        if set(expected) != {item["logical_name"] for item in current["artifacts"]}:
            raise OperatorSubmissionError("immutable Operator artifact set is incomplete")
        for logical_name, payload in expected.items():
            stored, _, stored_digest = self.persistence.read_artifact(
                submission["operator_id"], submission["version"], logical_name
            )
            if stored != payload or hashlib.sha256(payload).hexdigest() != stored_digest:
                raise OperatorSubmissionError("immutable Operator artifact digest binding mismatch")

    def _verify_evidence(
        self,
        evidence: Any,
        *,
        digest: str,
        slot: str,
        tests: list[dict[str, Any]],
    ) -> None:
        expected_fields = {
            "schema_version",
            "passed",
            "slot",
            "candidate_digest",
            "fixture_digest",
            "validator_image",
            "execution_envelope",
            "started_at",
            "finished_at",
            "duration_seconds",
            "exit_status",
            "outcome",
            "container_id",
            "termination_confirmed",
            "stdout_sha256",
            "stderr_sha256",
            "control_path",
            "observations",
        }
        if not isinstance(evidence, dict) or set(evidence) != expected_fields:
            raise OperatorSubmissionError("operator validation evidence has invalid fields")
        expected_fixture_digest = hashlib.sha256(canonical_json_bytes(tests)).hexdigest()
        if (
            evidence["schema_version"] != 1
            or evidence["passed"] is not True
            or evidence["slot"] != slot
            or evidence["candidate_digest"] != digest
            or evidence["fixture_digest"] != expected_fixture_digest
            or evidence["validator_image"] != self.runner_image
            or evidence["execution_envelope"] != EXECUTION_ENVELOPE
            or not isinstance(evidence["started_at"], str)
            or not isinstance(evidence["finished_at"], str)
            or isinstance(evidence["duration_seconds"], bool)
            or not isinstance(evidence["duration_seconds"], (int, float))
            or evidence["duration_seconds"] < 0
            or evidence["exit_status"] != 0
            or evidence["outcome"] != "SUCCEEDED"
            or not isinstance(evidence["container_id"], str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["container_id"]) is None
            or evidence["termination_confirmed"] is not True
            or not isinstance(evidence["stdout_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["stdout_sha256"]) is None
            or not isinstance(evidence["stderr_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["stderr_sha256"]) is None
            or not isinstance(evidence["control_path"], str)
            or not evidence["control_path"]
        ):
            raise OperatorSubmissionError("operator validation evidence binding mismatch")
        observations = evidence["observations"]
        if (
            not isinstance(observations, dict)
            or observations.get("compile") is not True
            or observations.get("contract") is not True
            or observations.get("fixtures") != len(tests)
        ):
            raise OperatorSubmissionError("operator validation evidence did not pass")

    def list(self) -> list[dict[str, Any]]:
        return self.persistence.list_operators()

    def detail(
        self, operator_id: str, version: str | None = None
    ) -> dict[str, Any]:
        return self.persistence.operator_detail(operator_id, version)

    def list_versions(self, operator_id: str) -> list[dict[str, Any]]:
        return self.persistence.list_operator_versions(operator_id)

    def artifact(
        self, operator_id: str, version: str, logical_name: str
    ) -> tuple[bytes, str, str]:
        return self.persistence.read_artifact(operator_id, version, logical_name)
