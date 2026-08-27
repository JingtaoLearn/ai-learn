from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .catalog import Catalog
from .isolation import build_operator_validation_command
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
FIXTURE_FIELDS = {"values", "parameters", "expected"}
MAX_SOURCE_BYTES = 64 * 1024
MAX_DOCUMENTATION_BYTES = 128 * 1024
MAX_FIXTURES = 50
Validator = Callable[[Path], dict[str, Any]]


def _require_text(value: Any, path: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise OperatorSubmissionError(f"{path} must be non-empty text")
    if len(value.encode("utf-8")) > maximum:
        raise OperatorSubmissionError(f"{path} exceeds its size limit")
    return value


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
    if value["slot"] != "fit":
        raise OperatorSubmissionError("unsupported custom operator slot; only fit is supported")
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
        values = case["values"]
        if not isinstance(values, list) or not values:
            raise OperatorSubmissionError(f"tests[{index}].values must be non-empty")
        expected = case["expected"]
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise OperatorSubmissionError(f"tests[{index}].expected must be numeric")
        normalized_tests.append(
            {
                "values": values,
                "parameters": validate_parameters(schema, case["parameters"]),
                "expected": float(expected),
            }
        )
    normalized = {
        "operator_id": operator_id,
        "slot": "fit",
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
        catalog: Catalog,
        *,
        validator: Validator | None = None,
        runner_image: str | None = None,
    ):
        self.catalog = catalog
        self.runner_image = runner_image
        self.validator = validator or self._docker_validator

    @contextmanager
    def _publication_lock(self) -> Iterator[None]:
        lock_path = self.catalog.state_root / ".operator-publication.lock"
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _docker_validator(self, candidate: Path) -> dict[str, Any]:
        if self.runner_image is None:
            raise OperatorSubmissionError("a pinned operator validator image is required")
        evidence_dir = Path(
            tempfile.mkdtemp(prefix=".evidence-", dir=candidate.parent)
        )
        try:
            command = build_operator_validation_command(
                candidate, evidence_dir, self.runner_image
            )
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                message = completed.stdout.strip()[-1000:] or "isolated validation failed"
                raise OperatorSubmissionError(f"operator validation failed: {message}")
            evidence_path = evidence_dir / "evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict) or evidence.get("passed") is not True:
                raise OperatorSubmissionError("operator validation did not produce passing evidence")
            return evidence
        except subprocess.TimeoutExpired as exc:
            raise OperatorSubmissionError("operator validation timed out") from exc
        finally:
            shutil.rmtree(evidence_dir, ignore_errors=True)

    def submit(self, value: Any) -> dict[str, str]:
        submission = _normalize_submission(value)
        digest = hashlib.sha256(canonical_json_bytes(submission)).hexdigest()
        operator_id = submission["operator_id"]
        version = submission["version"]
        with self._publication_lock():
            try:
                current = self.catalog.operator_detail(operator_id, version)
            except ValueError:
                current = None
            if current is not None:
                if current["content_digest"] != digest:
                    raise OperatorConflictError(
                        f"{operator_id}@{version} already exists with different content"
                    )
                return {
                    "status": "NO_CHANGE",
                    "operator_id": operator_id,
                    "version": version,
                    "content_digest": digest,
                }

            parent = self.catalog.state_root / "operators" / operator_id
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / version
            if target.exists():
                raise OperatorConflictError(
                    f"unregistered immutable bundle already exists: {operator_id}@{version}"
                )
            staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=parent))
            try:
                worker_manifest = {
                    key: submission[key]
                    for key in (
                        "operator_id",
                        "slot",
                        "version",
                        "parameter_schema",
                        "defaults",
                    )
                }
                (staging / "operator.py").write_text(
                    submission["source"], encoding="utf-8"
                )
                (staging / "tests.json").write_bytes(
                    canonical_json_bytes(submission["tests"]) + b"\n"
                )
                (staging / "documentation.md").write_text(
                    submission["documentation"] + "\n", encoding="utf-8"
                )
                (staging / "manifest.json").write_bytes(
                    canonical_json_bytes(worker_manifest | {"content_digest": digest})
                    + b"\n"
                )
                evidence = self.validator(staging)
                if not isinstance(evidence, dict) or evidence.get("passed") is not True:
                    raise OperatorSubmissionError("operator validation evidence did not pass")
                (staging / "evidence.json").write_bytes(
                    canonical_json_bytes(evidence) + b"\n"
                )
                for path in staging.iterdir():
                    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                        raise OperatorSubmissionError(
                            f"operator bundle contains unsafe file: {path.name}"
                        )
                    path.chmod(0o444)
                staging.chmod(0o555)
                os.rename(staging, target)
                created_at = (
                    datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
                )
                self.catalog.publish_operator_record(
                    operator_id=operator_id,
                    slot=submission["slot"],
                    version=version,
                    title_zh=submission["title_zh"],
                    summary_zh=submission["summary_zh"],
                    content_digest=digest,
                    parameter_schema_json=canonical_json_bytes(
                        submission["parameter_schema"]
                    ).decode(),
                    defaults_json=canonical_json_bytes(submission["defaults"]).decode(),
                    documentation=submission["documentation"],
                    bundle_path=target.relative_to(self.catalog.state_root).as_posix(),
                    validation_evidence_json=canonical_json_bytes(evidence).decode(),
                    created_at=created_at,
                )
            except BaseException:
                if staging.exists():
                    staging.chmod(0o700)
                    shutil.rmtree(staging)
                if target.exists():
                    target.chmod(0o700)
                    for path in target.iterdir():
                        path.chmod(0o600)
                    shutil.rmtree(target)
                raise
        return {
            "status": "CREATED",
            "operator_id": operator_id,
            "version": version,
            "content_digest": digest,
        }

    def list(self) -> list[dict[str, Any]]:
        return self.catalog.list_operators()

    def detail(
        self, operator_id: str, version: str | None = None
    ) -> dict[str, Any]:
        return self.catalog.operator_detail(operator_id, version)

    def list_versions(self, operator_id: str) -> list[dict[str, Any]]:
        return self.catalog.list_operator_versions(operator_id)
