from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import quant_platform.production_client as production_client_module

from quant_platform.production_client import (
    MAX_API_RESPONSE_BYTES,
    MAX_RESULT_FILE_BYTES,
    REPORT_EVIDENCE_FILE_NAMES,
    ClientTLS,
    ProductionClient,
    ProductionClientError,
    ProductionClientUnknown,
    StdlibMTLSTransport,
)
from quant_platform.production_contract import ProductionRequest, canonical_json_bytes, production_run_id


MANIFEST = "6f9f10ed235c6229582ca2843c8b983a887dbdc0ac289170ca834e580bcae969"


def request() -> ProductionRequest:
    return ProductionRequest.build(
        job_id="297c11cad0dc",
        scheduled_for="2026-03-09T00:40:00Z",
        production_manifest_sha256=MANIFEST,
    )


class SuccessTransport:
    def __init__(self, value):
        self.value = value
        self.posts = []
        self.release = "a" * 64
        self.run = production_run_id(value.request_id, self.release)
        self.experiment = "b" * 64
        self.attempt = "c" * 64
        self.payloads = {
            "provider-response.bin": b"provider",
            "normalized-snapshot.json": b"[]",
            "action.json": b"{}",
            "report.html": b"<html></html>",
            "notification.txt": b"verified notification",
            **{name: b"{}" for name in REPORT_EVIDENCE_FILE_NAMES},
        }
        self.core = {
            "schema": "quantresearch-production-result/v1",
            "request_id": value.request_id,
            "production_run_id": self.run,
            "production_release_id": self.release,
            "experiment_id": self.experiment,
            "attempt_id": self.attempt,
            "job_id": value.job_id,
            "production_manifest_sha256": value.production_manifest_sha256,
            "automatic_ordering": False,
            "files": {
                name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
                for name, payload in self.payloads.items()
            },
        }
        self.result_id = hashlib.sha256(canonical_json_bytes(self.core)).hexdigest()

    def request(self, method, path, *, headers, body):
        if method == "POST":
            self.posts.append((path, dict(headers), body))
            status = "ACCEPTED"
        elif path.endswith(self.run):
            status = "SUCCEEDED"
        elif "/files/" in path:
            return 200, {}, self.payloads[path.rsplit("/", 1)[1]]
        else:
            return 200, {}, canonical_json_bytes(self.core | {"result_id": self.result_id})
        value = {
            "request_id": self.value.request_id,
            "production_run_id": self.run,
            "production_release_id": self.release,
            "status": status,
            "poll_uri": f"/api/v1/production/runs/{self.run}",
        }
        if status == "SUCCEEDED":
            value.update(
                {
                    "experiment_id": self.experiment,
                    "attempt_id": self.attempt,
                    "result_id": self.result_id,
                    "result_uri": f"/api/v1/production/results/{self.result_id}",
                }
            )
        return (202 if method == "POST" else 200), {}, canonical_json_bytes(value)


def test_client_polls_and_verifies_bound_immutable_result() -> None:
    value = request()
    transport = SuccessTransport(value)
    client = ProductionClient(transport, sleep=lambda _: None)

    result = client.submit_and_wait(value)

    assert result["result_id"] == transport.result_id
    assert client.fetch_verified_file(result, "notification.txt") == b"verified notification"
    assert transport.posts == [
        (
            "/api/v1/production/runs",
            {"Content-Type": "application/json", "Idempotency-Key": value.request_id},
            value.canonical_body,
        )
    ]


class FailedTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, *, headers, body):
        self.calls.append((method, path, dict(headers), body))
        raise TimeoutError("unavailable")


def test_unavailable_api_is_unknown_with_identical_body_key_and_no_fallback() -> None:
    value = request()
    transport = FailedTransport()
    client = ProductionClient(transport, transport_attempts=3, sleep=lambda _: None)

    with pytest.raises(ProductionClientUnknown) as raised:
        client.submit_and_wait(value)

    assert raised.value.request_id == value.request_id
    assert len(transport.calls) == 3
    assert {call[2]["Idempotency-Key"] for call in transport.calls} == {value.request_id}
    assert {call[3] for call in transport.calls} == {value.canonical_body}
    assert all("flearn" not in call[1] for call in transport.calls)


def test_transient_capacity_retries_bounded_identical_admission() -> None:
    value = request()

    class CapacityTransport:
        def __init__(self, request_value):
            self.success = SuccessTransport(request_value)
            self.posts = self.success.posts
            self.result_id = self.success.result_id
            self.capacity_responses = 0

        def request(self, method, path, *, headers, body):
            if method == "POST" and self.capacity_responses == 0:
                self.posts.append((path, dict(headers), body))
                self.capacity_responses += 1
                return 429, {}, b'{"ok":false,"error":{"code":"CAPACITY_UNAVAILABLE"}}'
            return self.success.request(method, path, headers=headers, body=body)

    delays = []
    transport = CapacityTransport(value)
    client = ProductionClient(transport, transport_attempts=3, sleep=delays.append)

    result = client.submit_and_wait(value)

    assert result["result_id"] == transport.result_id
    assert delays == [1, 1]
    assert len(transport.posts) == 2
    assert {call[1]["Idempotency-Key"] for call in transport.posts} == {value.request_id}
    assert {call[2] for call in transport.posts} == {value.canonical_body}


def test_client_rejects_tampered_result_identity() -> None:
    value = request()
    transport = SuccessTransport(value)
    transport.result_id = "0" * 64
    client = ProductionClient(transport, sleep=lambda _: None)

    with pytest.raises(Exception, match="identity"):
        client.submit_and_wait(value)


def test_client_rejects_tampered_result_file() -> None:
    value = request()
    transport = SuccessTransport(value)
    client = ProductionClient(transport, sleep=lambda _: None)
    result = client.submit_and_wait(value)
    transport.payloads["notification.txt"] = b"tampered"

    with pytest.raises(ProductionClientError, match="file identity"):
        client.fetch_verified_file(result, "notification.txt")


def test_stdlib_transport_uses_a_bounded_result_artifact_limit_without_loosening_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * (MAX_API_RESPONSE_BYTES + 1)

    class Response:
        status = 200

        @staticmethod
        def getheader(name):
            return str(len(payload)) if name.casefold() == "content-length" else None

        @staticmethod
        def getheaders():
            return []

        @staticmethod
        def read(limit):
            return payload[:limit]

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        @staticmethod
        def getresponse():
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(production_client_module.http.client, "HTTPSConnection", Connection)
    transport = StdlibMTLSTransport.__new__(StdlibMTLSTransport)
    transport.host = "127.0.0.1"
    transport.port = 8443
    transport.context = None
    transport.configuration = SimpleNamespace(timeout_seconds=15.0)
    result_id = "a" * 64

    status, _headers, actual = transport.request(
        "GET",
        f"/api/v1/production/results/{result_id}/files/report-document.json",
        headers={"Accept": "application/octet-stream"},
        body=None,
    )

    assert status == 200
    assert actual == payload
    assert len(actual) < MAX_RESULT_FILE_BYTES
    manifest = {
        "request_id": "request-id",
        "result_id": result_id,
        "files": {
            "report-document.json": {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        },
    }
    assert ProductionClient(transport).fetch_verified_file(
        manifest, "report-document.json"
    ) == payload
    with pytest.raises(ProductionClientError, match="size limit"):
        transport.request(
            "GET",
            f"/api/v1/production/results/{result_id}",
            headers={"Accept": "application/json"},
            body=None,
        )


def test_client_rejects_result_artifacts_above_the_enforced_transport_limit() -> None:
    value = request()
    transport = SuccessTransport(value)
    transport.core["files"]["report-document.json"]["size"] = MAX_RESULT_FILE_BYTES + 1
    transport.result_id = hashlib.sha256(canonical_json_bytes(transport.core)).hexdigest()
    client = ProductionClient(transport, sleep=lambda _: None)

    with pytest.raises(ProductionClientError, match="file inventory"):
        client.submit_and_wait(value)


def test_client_tls_rejects_non_loopback_and_unsafe_key_paths(tmp_path: Path) -> None:
    certificate = tmp_path / "client.crt"
    private_key = tmp_path / "client.key"
    server_ca = tmp_path / "server-ca.crt"
    for path in (certificate, private_key, server_ca):
        path.write_text("test", encoding="utf-8")
        path.chmod(0o600 if path == private_key else 0o644)

    ClientTLS("https://127.0.0.1:8443", certificate, private_key, server_ca).validate()
    with pytest.raises(ProductionClientError, match="loopback HTTPS"):
        ClientTLS("https://zhlearn:8443", certificate, private_key, server_ca).validate()

    private_key.chmod(0o644)
    with pytest.raises(ProductionClientError, match="permissions"):
        ClientTLS("https://127.0.0.1:8443", certificate, private_key, server_ca).validate()

    private_key.chmod(0o600)
    real_certificate = tmp_path / "real.crt"
    real_certificate.write_text("test", encoding="utf-8")
    linked_certificate = tmp_path / "linked.crt"
    linked_certificate.symlink_to(real_certificate)
    with pytest.raises(ProductionClientError, match="symlinks"):
        ClientTLS("https://localhost:8443", linked_certificate, private_key, server_ca).validate()
