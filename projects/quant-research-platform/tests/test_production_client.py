from __future__ import annotations

import hashlib

import pytest

from quant_platform.production_client import ProductionClient, ProductionClientUnknown
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
        self.core = {
            "schema": "quantresearch-production-result/v1",
            "request_id": value.request_id,
            "production_run_id": self.run,
            "production_release_id": self.release,
            "experiment_id": self.experiment,
            "attempt_id": self.attempt,
            "automatic_ordering": False,
            "files": {"action.json": {"sha256": "d" * 64, "size": 10}},
        }
        self.result_id = hashlib.sha256(canonical_json_bytes(self.core)).hexdigest()

    def request(self, method, path, *, headers, body):
        if method == "POST":
            self.posts.append((path, dict(headers), body))
            status = "ACCEPTED"
        elif path.endswith(self.run):
            status = "SUCCEEDED"
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


def test_client_rejects_tampered_result_identity() -> None:
    value = request()
    transport = SuccessTransport(value)
    transport.result_id = "0" * 64
    client = ProductionClient(transport, sleep=lambda _: None)

    with pytest.raises(Exception, match="identity"):
        client.submit_and_wait(value)
