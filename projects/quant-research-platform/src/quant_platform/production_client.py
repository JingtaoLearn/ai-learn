from __future__ import annotations

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

    def validate(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
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
            if not path.is_absolute():
                raise ProductionClientError(f"{label} path must be absolute")
            metadata = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ProductionClientError(f"{label} path is unsafe")
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
        if not 1 <= transport_attempts <= 5 or not 1 <= poll_attempts <= 120:
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

    @staticmethod
    def _verify_result(
        manifest: Mapping[str, Any], status: Mapping[str, Any], request: ProductionRequest
    ) -> dict[str, Any]:
        if manifest.get("result_id") != status.get("result_id"):
            raise ProductionClientError("result identity differs from run status")
        core = {key: value for key, value in manifest.items() if key != "result_id"}
        import hashlib

        if hashlib.sha256(canonical_json_bytes(core)).hexdigest() != manifest.get("result_id"):
            raise ProductionClientError("result manifest identity does not verify")
        if (
            manifest.get("request_id") != request.request_id
            or manifest.get("production_run_id") != status.get("production_run_id")
            or manifest.get("production_release_id") != status.get("production_release_id")
            or manifest.get("experiment_id") != status.get("experiment_id")
            or manifest.get("attempt_id") != status.get("attempt_id")
            or manifest.get("automatic_ordering") is not False
        ):
            raise ProductionClientError("result manifest bindings do not verify")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise ProductionClientError("result manifest file inventory is absent")
        for name, item in files.items():
            if (
                not isinstance(name, str)
                or "/" in name
                or not isinstance(item, dict)
                or set(item) != {"sha256", "size"}
                or not isinstance(item["sha256"], str)
                or len(item["sha256"]) != 64
                or type(item["size"]) is not int
                or item["size"] < 0
            ):
                raise ProductionClientError("result manifest file inventory is invalid")
        return dict(manifest)

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
