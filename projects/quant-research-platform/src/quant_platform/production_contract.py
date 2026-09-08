from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo


class ProductionContractError(ValueError):
    """Raised when a production identity or document is not canonical."""


SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SCHEDULED_REQUEST_FIELDS = {
    "schema_version",
    "job_id",
    "scheduled_for",
    "production_manifest_sha256",
    "request_id",
}
VALIDATION_REQUEST_FIELDS = {
    "schema_version",
    "job_id",
    "validation_for",
    "validation_id",
    "production_manifest_sha256",
    "request_id",
}
OPERATION_REQUEST_FIELDS = {
    "schema_version",
    "job_id",
    "operation",
    "production_manifest_sha256",
    "request_id",
}
RELEASE_FIELDS = {
    "schema",
    "production_api_image_digest",
    "source_commit",
    "source_tree_sha256",
    "dependency_lock_sha256",
    "effective_compose_config_sha256",
    "provider_contract_sha256",
    "api_contract_sha256",
}
DAILY_JOB_IDS = frozenset({"297c11cad0dc", "1cd5557264db"})
FOCUS_CALIBRATION_JOB_ID = "9d9adff99888"
SUPPORTED_JOB_IDS = DAILY_JOB_IDS | {FOCUS_CALIBRATION_JOB_ID}
FOCUS_CALIBRATION_OPERATION = "calibrate-once"
RUN_ID_DOMAIN = b"quantresearch-production-run-id/v1\n"


def canonical_json_bytes(value: Any) -> bytes:
    """Canonicalize the contract's integer/string/object JSON subset."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProductionContractError("document is not finite canonical JSON") from exc


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _strict_json_object(payload: bytes, *, maximum: int) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload or len(payload) > maximum:
        raise ProductionContractError("JSON body size is invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ProductionContractError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ProductionContractError(f"non-finite JSON value: {item}")
            ),
        )
    except ProductionContractError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionContractError("body is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProductionContractError("body must be a JSON object")
    return value


def canonical_scheduled_fire(value: Any) -> str:
    canonical = canonical_utc_second(value, field="scheduled_for")
    parsed = datetime.fromisoformat(canonical[:-1] + "+00:00")
    local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    if (local.hour, local.minute, local.second) != (8, 40, 0) or local.weekday() >= 5:
        raise ProductionContractError("scheduled_for is not a weekday 08:40 Asia/Shanghai fire")
    return canonical


def canonical_utc_second(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProductionContractError(f"{field} must be canonical UTC RFC3339")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProductionContractError(f"{field} must be canonical UTC RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed) or parsed.microsecond:
        raise ProductionContractError(f"{field} must be whole-second UTC")
    canonical = parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if canonical != value:
        raise ProductionContractError(f"{field} is not canonical")
    return canonical


@dataclass(frozen=True)
class ProductionRequest:
    schema_version: int
    job_id: str
    production_manifest_sha256: str
    request_id: str
    scheduled_for: str | None = None
    validation_for: str | None = None
    validation_id: str | None = None
    operation: str | None = None

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        maximum: int = 16_384,
        validate_identity: bool = True,
    ) -> ProductionRequest:
        return cls.from_mapping(
            _strict_json_object(payload, maximum=maximum),
            validate_identity=validate_identity,
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, validate_identity: bool = True
    ) -> ProductionRequest:
        if not isinstance(value, Mapping):
            raise ProductionContractError("request must be an object")
        fields = set(value)
        if fields == SCHEDULED_REQUEST_FIELDS:
            schema_version = 1
            scheduled_for = canonical_scheduled_fire(value["scheduled_for"])
            validation_for = None
            validation_id = None
            operation = None
        elif fields == VALIDATION_REQUEST_FIELDS:
            schema_version = 2
            scheduled_for = None
            validation_for = canonical_utc_second(value["validation_for"], field="validation_for")
            validation_id = value["validation_id"]
            operation = None
            if not isinstance(validation_id, str) or SHA256.fullmatch(validation_id) is None:
                raise ProductionContractError("validation_id must be lowercase SHA-256")
        elif fields == OPERATION_REQUEST_FIELDS:
            schema_version = 3
            scheduled_for = None
            validation_for = None
            validation_id = None
            operation = value["operation"]
            if operation != FOCUS_CALIBRATION_OPERATION:
                raise ProductionContractError("formal operation is not supported")
        else:
            raise ProductionContractError("request fields do not match a supported invocation")
        if type(value["schema_version"]) is not int or value["schema_version"] != schema_version:
            raise ProductionContractError(f"schema_version must be integer {schema_version}")
        job_id = value["job_id"]
        if not isinstance(job_id, str) or job_id not in SUPPORTED_JOB_IDS:
            raise ProductionContractError(f"job_id is not supported by schema v{schema_version}")
        if (schema_version == 3) != (job_id == FOCUS_CALIBRATION_JOB_ID):
            raise ProductionContractError("job_id does not match the request operation class")
        manifest = value["production_manifest_sha256"]
        request_id = value["request_id"]
        if not isinstance(manifest, str) or SHA256.fullmatch(manifest) is None:
            raise ProductionContractError("production_manifest_sha256 must be lowercase SHA-256")
        if not isinstance(request_id, str) or SHA256.fullmatch(request_id) is None:
            raise ProductionContractError("request_id must be lowercase SHA-256")
        request = cls(
            schema_version,
            job_id,
            manifest,
            request_id,
            scheduled_for=scheduled_for,
            validation_for=validation_for,
            validation_id=validation_id,
            operation=operation,
        )
        if validate_identity and request.request_id != request.expected_request_id:
            raise ProductionContractError("request_id does not match the canonical request subject")
        return request

    @classmethod
    def build(
        cls,
        *,
        job_id: str,
        scheduled_for: str,
        production_manifest_sha256: str,
    ) -> ProductionRequest:
        subject = {
            "schema_version": 1,
            "job_id": job_id,
            "scheduled_for": canonical_scheduled_fire(scheduled_for),
            "production_manifest_sha256": production_manifest_sha256,
        }
        request_id = sha256_hex(canonical_json_bytes(subject))
        return cls.from_mapping(subject | {"request_id": request_id})

    @classmethod
    def build_validation(
        cls,
        *,
        job_id: str,
        validation_for: str,
        validation_id: str,
        production_manifest_sha256: str,
    ) -> ProductionRequest:
        subject = {
            "schema_version": 2,
            "job_id": job_id,
            "validation_for": canonical_utc_second(validation_for, field="validation_for"),
            "validation_id": validation_id,
            "production_manifest_sha256": production_manifest_sha256,
        }
        request_id = sha256_hex(canonical_json_bytes(subject))
        return cls.from_mapping(subject | {"request_id": request_id})

    @classmethod
    def build_operation(
        cls,
        *,
        job_id: str,
        operation: str,
        production_manifest_sha256: str,
    ) -> ProductionRequest:
        subject = {
            "schema_version": 3,
            "job_id": job_id,
            "operation": operation,
            "production_manifest_sha256": production_manifest_sha256,
        }
        request_id = sha256_hex(canonical_json_bytes(subject))
        return cls.from_mapping(subject | {"request_id": request_id})

    @property
    def is_validation(self) -> bool:
        return self.validation_id is not None

    @property
    def is_operation(self) -> bool:
        return self.operation is not None

    @property
    def effective_for(self) -> str:
        if self.is_operation:
            return self.operation or ""
        value = self.validation_for if self.is_validation else self.scheduled_for
        if value is None:
            raise ProductionContractError("request invocation time is absent")
        return value

    @property
    def subject(self) -> dict[str, Any]:
        if self.is_operation:
            return {
                "schema_version": self.schema_version,
                "job_id": self.job_id,
                "operation": self.operation,
                "production_manifest_sha256": self.production_manifest_sha256,
            }
        if self.is_validation:
            return {
                "schema_version": self.schema_version,
                "job_id": self.job_id,
                "validation_for": self.validation_for,
                "validation_id": self.validation_id,
                "production_manifest_sha256": self.production_manifest_sha256,
            }
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "scheduled_for": self.scheduled_for,
            "production_manifest_sha256": self.production_manifest_sha256,
        }

    @property
    def body(self) -> dict[str, Any]:
        return self.subject | {"request_id": self.request_id}

    @property
    def canonical_body(self) -> bytes:
        return canonical_json_bytes(self.body)

    @property
    def expected_request_id(self) -> str:
        return sha256_hex(canonical_json_bytes(self.subject))

    @property
    def request_digest(self) -> str:
        return sha256_hex(self.canonical_body)


@dataclass(frozen=True)
class ProductionRelease:
    manifest: dict[str, str]
    production_release_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProductionRelease:
        if not isinstance(value, Mapping) or set(value) != RELEASE_FIELDS:
            raise ProductionContractError("release manifest fields are invalid")
        manifest = dict(value)
        if manifest["schema"] != "quantresearch-production-release/v1":
            raise ProductionContractError("release manifest schema is invalid")
        for field in RELEASE_FIELDS - {"schema", "source_commit"}:
            item = manifest[field]
            if not isinstance(item, str) or SHA256.fullmatch(item) is None:
                raise ProductionContractError(f"release {field} must be lowercase SHA-256")
        source_commit = manifest["source_commit"]
        if not isinstance(source_commit, str) or GIT_COMMIT.fullmatch(source_commit) is None:
            raise ProductionContractError("release source_commit must be a lowercase Git object ID")
        release_id = sha256_hex(canonical_json_bytes(manifest))
        return cls(manifest=manifest, production_release_id=release_id)


def production_run_preimage(request_id: str, production_release_id: str) -> bytes:
    if SHA256.fullmatch(request_id) is None or SHA256.fullmatch(production_release_id) is None:
        raise ProductionContractError("run identity inputs must be lowercase SHA-256")
    payload = RUN_ID_DOMAIN + request_id.encode("ascii") + b"\n" + production_release_id.encode("ascii") + b"\n"
    if len(payload) != 165:
        raise AssertionError("production run preimage length invariant failed")
    return payload


def production_run_id(request_id: str, production_release_id: str) -> str:
    return sha256_hex(production_run_preimage(request_id, production_release_id))
