from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from .production_contract import ProductionRequest, canonical_json_bytes, production_run_id


DAILY_RESULT_FILE_NAMES = frozenset(
    {
        "provider-response.bin",
        "normalized-snapshot.json",
        "action.json",
        "report.html",
        "notification.txt",
    }
)
FORMAL_RESULT_FILE_NAMES = frozenset(
    {
        "calibration.json",
        "03-CALIBRATION_CLAIMED.json",
        "04-CALIBRATION_SEALED.json",
    }
)
RESULT_FILE_NAMES_BY_SCHEMA = {
    "quantresearch-production-result/v1": DAILY_RESULT_FILE_NAMES,
    "quantresearch-production-formal-result/v1": FORMAL_RESULT_FILE_NAMES,
}
RESULT_FILE_NAMES = DAILY_RESULT_FILE_NAMES | FORMAL_RESULT_FILE_NAMES


class ProductionClientError(RuntimeError):
    pass


class ProductionClientUnknown(ProductionClientError):
    """Transport ambiguity; callers must retain and reuse the same request identity."""

    def __init__(self, request_id: str, message: str):
        super().__init__(message)
        self.request_id = request_id


class ClientTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, Mapping[str, str], bytes]: ...


@dataclass(frozen=True)
class ClientTLS:
    base_url: str
    client_certificate: Path
    client_private_key: Path
    server_ca: Path
    timeout_seconds: float = 15.0

    @staticmethod
    def _validate_path(label: str, path: Path, *, private: bool) -> None:
        if not path.is_absolute():
            raise ProductionClientError(f"{label} path must be absolute")
        current = Path(path.anchor)
        metadata: os.stat_result | None = None
        try:
            for component in path.parts[1:]:
                current /= component
                metadata = os.lstat(current)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ProductionClientError(f"{label} path must not contain symlinks")
        except OSError as exc:
            raise ProductionClientError(f"{label} path is unavailable") from exc
        if (
            metadata is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size == 0
        ):
            raise ProductionClientError(f"{label} path is unsafe")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o7000 or mode & 0o022 or (private and mode & 0o077):
            raise ProductionClientError(f"{label} permissions are unsafe")

    def validate(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ProductionClientError("production API must be loopback HTTPS")
        for label, path in (
            ("client certificate", self.client_certificate),
            ("client private key", self.client_private_key),
            ("server CA", self.server_ca),
        ):
            self._validate_path(label, path, private=label == "client private key")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ProductionClientError("client timeout is outside the bounded range")


class StdlibMTLSTransport:
    def __init__(self, configuration: ClientTLS):
        configuration.validate()
        self.configuration = configuration
        parsed = urlsplit(configuration.base_url)
        self.host = parsed.hostname or ""
        self.port = parsed.port or 443
        context = ssl.create_default_context(cafile=str(configuration.server_ca))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(
            certfile=str(configuration.client_certificate),
            keyfile=str(configuration.client_private_key),
        )
        self.context = context

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        connection = http.client.HTTPSConnection(
            self.host,
            self.port,
            timeout=self.configuration.timeout_seconds,
            context=self.context,
        )
        try:
            connection.request(method, path, body=body, headers=dict(headers))
            response = connection.getresponse()
            content_length = response.getheader("content-length")
            if content_length is not None and int(content_length) > 1_048_576:
                raise ProductionClientError("production API response exceeds size limit")
            payload = response.read(1_048_577)
            if len(payload) > 1_048_576:
                raise ProductionClientError("production API response exceeds size limit")
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()


def _json_response(payload: bytes) -> dict[str, Any]:
    if len(payload) > 1_048_576:
        raise ProductionClientError("production API response exceeds size limit")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionClientError("production API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProductionClientError("production API response must be an object")
    return value


class ProductionClient:
    """Thin mTLS orchestration client with one immutable request across all retries."""

    def __init__(
        self,
        transport: ClientTransport,
        *,
        transport_attempts: int = 3,
        poll_attempts: int = 30,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not 1 <= transport_attempts <= 5 or not 1 <= poll_attempts <= 360:
            raise ProductionClientError("client retry or poll bound is invalid")
        self.transport = transport
        self.transport_attempts = transport_attempts
        self.poll_attempts = poll_attempts
        self.sleep = sleep

    def _request(
        self, method: str, path: str, *, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, dict[str, Any]]:
        status, _response_headers, payload = self.transport.request(
            method, path, headers=headers, body=body
        )
        return status, _json_response(payload)

    @staticmethod
    def _verify_status(value: Mapping[str, Any], request: ProductionRequest) -> None:
        required = {
            "request_id",
            "production_run_id",
            "production_release_id",
            "status",
            "poll_uri",
        }
        if not required <= set(value) or value["request_id"] != request.request_id:
            raise ProductionClientError("production status identity is incomplete")
        expected_run = production_run_id(request.request_id, value["production_release_id"])
        if value["production_run_id"] != expected_run:
            raise ProductionClientError("production run identity does not verify")
        if value["poll_uri"] != f"/api/v1/production/runs/{expected_run}":
            raise ProductionClientError("production poll URI does not verify")
        expected_validation = (
            {"validation_id": request.validation_id, "validation_for": request.validation_for}
            if request.is_validation
            else {}
        )
        actual_validation = {
            field: value[field]
            for field in ("validation_id", "validation_for")
            if field in value
        }
        if actual_validation != expected_validation:
            raise ProductionClientError("validation invocation identity does not verify")
        expected_operation = request.operation if request.is_operation else None
        if value.get("operation") != expected_operation:
            raise ProductionClientError("formal operation identity does not verify")
        if value["status"] == "SUCCEEDED" and value.get("result_uri") != (
            f"/api/v1/production/results/{value.get('result_id')}"
        ):
            raise ProductionClientError("production result URI does not verify")

    @staticmethod
    def _verify_result(
        manifest: Mapping[str, Any], status: Mapping[str, Any], request: ProductionRequest
    ) -> dict[str, Any]:
        if manifest.get("result_id") != status.get("result_id"):
            raise ProductionClientError("result identity differs from run status")
        core = {key: value for key, value in manifest.items() if key != "result_id"}
        if hashlib.sha256(canonical_json_bytes(core)).hexdigest() != manifest.get("result_id"):
            raise ProductionClientError("result manifest identity does not verify")
        if (
            manifest.get("request_id") != request.request_id
            or manifest.get("production_run_id") != status.get("production_run_id")
            or manifest.get("production_release_id") != status.get("production_release_id")
            or manifest.get("job_id") != request.job_id
            or manifest.get("production_manifest_sha256")
            != request.production_manifest_sha256
            or manifest.get("experiment_id") != status.get("experiment_id")
            or manifest.get("attempt_id") != status.get("attempt_id")
            or manifest.get("automatic_ordering") is not False
        ):
            raise ProductionClientError("result manifest bindings do not verify")
        files = manifest.get("files")
        schema = manifest.get("schema")
        expected_files = (
            RESULT_FILE_NAMES_BY_SCHEMA.get(schema) if isinstance(schema, str) else None
        )
        if expected_files is None or not isinstance(files, dict) or set(files) != expected_files:
            raise ProductionClientError("result manifest file inventory is absent")
        for name, item in files.items():
            if (
                not isinstance(name, str)
                or "/" in name
                or not isinstance(item, dict)
                or set(item) != {"sha256", "size"}
                or not isinstance(item["sha256"], str)
                or len(item["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in item["sha256"])
                or type(item["size"]) is not int
                or item["size"] < 0
            ):
                raise ProductionClientError("result manifest file inventory is invalid")
        return dict(manifest)

    def fetch_verified_file(self, manifest: Mapping[str, Any], name: str) -> bytes:
        files = manifest.get("files")
        result_id = manifest.get("result_id")
        if (
            not isinstance(files, Mapping)
            or name not in RESULT_FILE_NAMES
            or not isinstance(result_id, str)
            or len(result_id) != 64
            or any(character not in "0123456789abcdef" for character in result_id)
        ):
            raise ProductionClientError("result file request is invalid")
        expected = files[name]
        if not isinstance(expected, Mapping) or set(expected) != {"sha256", "size"}:
            raise ProductionClientError("result file identity is invalid")
        try:
            status, _headers, payload = self.transport.request(
                "GET",
                f"/api/v1/production/results/{result_id}/files/{name}",
                headers={"Accept": "application/octet-stream"},
                body=None,
            )
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise ProductionClientUnknown(
                str(manifest.get("request_id", "")), "production result file outcome is UNKNOWN"
            ) from exc
        actual = {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        if status != 200 or actual != dict(expected):
            raise ProductionClientError("result file identity does not verify")
        return payload

    def verify_focus_calibration(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        result_input = manifest.get("input")
        if (
            manifest.get("schema") != "quantresearch-production-formal-result/v1"
            or manifest.get("operation") != "calibrate-once"
            or manifest.get("automatic_ordering") is not False
            or not isinstance(result_input, Mapping)
            or result_input.get("kind") != "no-network-operation"
            or result_input.get("network_access") is not False
        ):
            raise ProductionClientError("FOCuS calibration result class does not verify")
        calibration_raw = self.fetch_verified_file(manifest, "calibration.json")
        claimed_raw = self.fetch_verified_file(manifest, "03-CALIBRATION_CLAIMED.json")
        sealed_raw = self.fetch_verified_file(manifest, "04-CALIBRATION_SEALED.json")
        calibration = _json_response(calibration_raw)
        claimed = _json_response(claimed_raw)
        sealed = _json_response(sealed_raw)
        calibration_sha256 = hashlib.sha256(calibration_raw).hexdigest()
        phase_claims = manifest.get("phase_claims")
        totals = calibration.get("totals")
        if (
            manifest.get("calibration_sha256") != calibration_sha256
            or not isinstance(phase_claims, Mapping)
            or phase_claims.get("CALIBRATION_CLAIMED")
            != hashlib.sha256(claimed_raw).hexdigest()
            or phase_claims.get("CALIBRATION_SEALED")
            != hashlib.sha256(sealed_raw).hexdigest()
            or claimed
            != {
                "schema": "quant-research/focus-phase-claim/v1",
                "ordinal": 3,
                "phase": "CALIBRATION_CLAIMED",
                "evidence_sha256": manifest.get("request_id"),
            }
            or sealed
            != {
                "schema": "quant-research/focus-phase-claim/v1",
                "ordinal": 4,
                "phase": "CALIBRATION_SEALED",
                "evidence_sha256": calibration_sha256,
            }
            or calibration.get("schema") != "focus-synthetic-calibration/v1"
            or not isinstance(totals, Mapping)
            or totals.get("generated_paths") != 140_000
            or totals.get("provider_requests") != 0
            or totals.get("sge_requests") != 0
            or totals.get("evaluation_executions") != 0
            or totals.get("flearn_fallbacks") != 0
            or totals.get("market_outcome_bytes") != 0
        ):
            raise ProductionClientError("FOCuS calibration members do not verify")
        return calibration

    def submit_and_wait(self, request: ProductionRequest) -> dict[str, Any]:
        body = request.canonical_body
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": request.request_id,
        }
        accepted: dict[str, Any] | None = None
        for attempt in range(self.transport_attempts):
            try:
                status, value = self._request(
                    "POST", "/api/v1/production/runs", headers=headers, body=body
                )
                if status not in {200, 202}:
                    raise ProductionClientError(f"production admission returned HTTP {status}")
                self._verify_status(value, request)
                accepted = value
                break
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                if attempt + 1 == self.transport_attempts:
                    raise ProductionClientUnknown(
                        request.request_id, "production admission outcome is UNKNOWN"
                    ) from exc
                self.sleep(2**attempt)
        if accepted is None:
            raise ProductionClientUnknown(request.request_id, "production admission outcome is UNKNOWN")
        status_value = accepted
        for poll in range(self.poll_attempts):
            self._verify_status(status_value, request)
            if status_value["status"] == "SUCCEEDED":
                result_status, manifest = self._request(
                    "GET",
                    status_value["result_uri"],
                    headers={"Accept": "application/json"},
                    body=None,
                )
                if result_status != 200:
                    raise ProductionClientError("immutable result retrieval failed")
                return self._verify_result(manifest, status_value, request)
            if status_value["status"] == "FAILED":
                raise ProductionClientError(
                    f"production run failed: {status_value.get('failure_reason', 'unknown')}"
                )
            if poll + 1 < self.poll_attempts:
                self.sleep(min(5, 1 + poll))
                poll_status, status_value = self._request(
                    "GET",
                    status_value["poll_uri"],
                    headers={"Accept": "application/json"},
                    body=None,
                )
                if poll_status != 200:
                    raise ProductionClientError("production status polling failed")
        raise ProductionClientUnknown(request.request_id, "production status remains UNKNOWN")
