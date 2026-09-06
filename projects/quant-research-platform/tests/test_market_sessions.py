from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import requests

import quant_platform.market_sessions as market_sessions
from quant_platform.market_sessions import (
    EXPECTED_SESSIONS_SOURCE_KIND,
    MarketSessionEvidenceError,
    SseOfficialXshg2026Source,
    admit_market_session_evidence,
)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        media_type: str,
        status: int = 200,
        history: list | None = None,
    ):
        self.body = body
        self.url = url
        self.status_code = status
        self.history = history or []
        self.headers = {"Content-Type": media_type}
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self):
        self.closed = True


def _zero_body() -> bytes:
    value = dict(market_sessions._ZERO_ROOT)
    value["pageHelp"] = dict(market_sessions._ZERO_PAGE)
    return b"jsonpCallback202(" + json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode() + b")"


def _zero_body_with_page_value(field: str, value) -> bytes:
    document = dict(market_sessions._ZERO_ROOT)
    page = dict(market_sessions._ZERO_PAGE)
    page[field] = value
    document["pageHelp"] = page
    return b"jsonpCallback202(" + json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode() + b")"


def _positive_body(*, malformed: bool = False) -> bytes:
    row = {
        "productCode": "601328",
        "productName": "Bank of Communications",
        "controlType": "S",
        "startStopDate": "20260831",
        "endStopDate": "20260831",
        "stopTime": "09:30",
        "stopReason": "test",
        "endStopReason": "test",
        "type": "stock",
    }
    if malformed:
        row["PRODUCT_CODE"] = row.pop("productCode")
    value = dict(market_sessions._ZERO_ROOT)
    value["result"] = [row]
    page = dict(market_sessions._ZERO_PAGE)
    page |= {
        "beginPage": 1,
        "data": [row],
        "pageCount": 1,
        "total": 1,
    }
    value["pageHelp"] = page
    return b"jsonpCallback202(" + json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode() + b")"


def live_evidence(
    monkeypatch,
    *,
    instrument: str = "601328.SS",
    start: str = "2026-08-31",
    end: str = "2026-08-31",
    query_body: bytes | None = None,
):
    bodies = {
        "trading_rule": b"test trading rule",
        "closure_notice": b"test closure notice",
        "suspension_page": b"test suspension page",
        "suspension_script": b"test suspension script",
        "suspension_query_page_1": query_body or _zero_body(),
    }
    authorities = tuple(
        replace(authority, body_sha256=hashlib.sha256(bodies[authority.name]).hexdigest())
        for authority in market_sessions._AUTHORITIES
    )
    monkeypatch.setattr(market_sessions, "_AUTHORITIES", authorities)
    monkeypatch.setattr(
        market_sessions, "_AUTHORITY_NAMES", tuple(authority.name for authority in authorities)
    )
    monkeypatch.setattr(
        market_sessions,
        "_REQUEST_NAMES",
        (*tuple(authority.name for authority in authorities), "suspension_query_page_1"),
    )
    calls: list[str] = []

    def get(url, **kwargs):
        name = next(
            (
                authority.name
                for authority in authorities
                if authority.url == url
            ),
            "suspension_query_page_1",
        )
        calls.append(name)
        prepared = requests.Request(
            "GET", url, params=sorted(kwargs["params"].items())
        ).prepare().url
        media_type = next(
            (authority.media_type for authority in authorities if authority.name == name),
            "application/json;charset=UTF-8",
        )
        return FakeResponse(bodies[name], url=prepared, media_type=media_type)

    source = SseOfficialXshg2026Source(
        http_get=get,
        clock=lambda: datetime(2026, 8, 31, 16, tzinfo=UTC),
        sleep=lambda _: None,
    )
    return source.fetch(instrument, start, end), calls, bodies


def test_official_source_publishes_exact_live_evidence_and_identity(monkeypatch):
    fetched, calls, _ = live_evidence(monkeypatch)

    assert calls == [
        "trading_rule",
        "closure_notice",
        "suspension_page",
        "suspension_script",
        "suspension_query_page_1",
    ]
    assert fetched.market_sessions == ("2026-08-31",)
    assert fetched.eligible_sessions == fetched.market_sessions
    assert fetched.evidence.publishable is True
    assert len(fetched.evidence.digest) == 64
    assert EXPECTED_SESSIONS_SOURCE_KIND == "xshg_official_eligible_sessions_v1"
    assert admit_market_session_evidence(
        fetched.evidence.document, fetched.evidence.artifact_bytes
    ).digest == fetched.evidence.digest


def test_mid_autumn_closure_enumerates_only_24_and_28(monkeypatch):
    fetched, _, _ = live_evidence(
        monkeypatch, start="2026-09-24", end="2026-09-28"
    )
    assert fetched.market_sessions == ("2026-09-24", "2026-09-28")


def test_national_day_closure_enumerates_only_30_and_08(monkeypatch):
    fetched, _, _ = live_evidence(
        monkeypatch, start="2026-09-30", end="2026-10-08"
    )
    assert fetched.market_sessions == ("2026-09-30", "2026-10-08")


def test_exact_authority_hashes_are_pinned_and_one_byte_drift_rejects(monkeypatch):
    _, _, bodies = live_evidence(monkeypatch)
    expected = market_sessions._AUTHORITIES[0].body_sha256
    bodies["trading_rule"] += b"x"

    with pytest.raises(MarketSessionEvidenceError, match="HASH_MISMATCH"):
        live_evidence_with_bodies(monkeypatch, bodies, expected)


def live_evidence_with_bodies(monkeypatch, bodies, expected_rule_hash):
    authorities = list(market_sessions._AUTHORITIES)
    authorities[0] = replace(authorities[0], body_sha256=expected_rule_hash)
    monkeypatch.setattr(market_sessions, "_AUTHORITIES", tuple(authorities))

    def get(url, **kwargs):
        name = next(
            (authority.name for authority in authorities if authority.url == url),
            "suspension_query_page_1",
        )
        prepared = requests.Request(
            "GET", url, params=sorted(kwargs["params"].items())
        ).prepare().url
        media_type = next(
            (authority.media_type for authority in authorities if authority.name == name),
            "application/json;charset=UTF-8",
        )
        return FakeResponse(bodies[name], url=prepared, media_type=media_type)

    return SseOfficialXshg2026Source(
        http_get=get, clock=lambda: datetime.now(UTC), sleep=lambda _: None
    ).fetch("601328.SS", "2026-08-31", "2026-08-31")


@pytest.mark.parametrize(
    ("instrument", "start", "end", "message"),
    [
        ("601328.SZ", "2026-08-31", "2026-08-31", "six-digit"),
        ("601328.SS", "2026-07-05", "2026-08-31", "inside"),
        ("601328.SS", "2026-08-31", "2027-01-01", "inside"),
        ("601328.SS", "2026-09-01", "2026-08-31", "inside"),
    ],
)
def test_unsupported_scope_fails_before_http(monkeypatch, instrument, start, end, message):
    called = False

    def get(*args, **kwargs):
        nonlocal called
        called = True

    source = SseOfficialXshg2026Source(http_get=get)
    with pytest.raises(MarketSessionEvidenceError, match=message):
        source.fetch(instrument, start, end)
    assert called is False


def test_naive_clock_fails_before_http():
    source = SseOfficialXshg2026Source(
        http_get=lambda *args, **kwargs: pytest.fail("HTTP must not run"),
        clock=lambda: datetime(2026, 8, 31),
    )
    with pytest.raises(MarketSessionEvidenceError, match="timezone-aware"):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")


def test_retryable_transport_uses_one_and_two_second_waits(monkeypatch):
    fetched, _, bodies = live_evidence(monkeypatch)
    waits: list[float] = []
    attempts = 0

    def get(url, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise requests.Timeout("transient")
        prepared = requests.Request(
            "GET", url, params=sorted(kwargs["params"].items())
        ).prepare().url
        authority = market_sessions._AUTHORITIES[0]
        return FakeResponse(
            bodies[authority.name], url=prepared, media_type=authority.media_type
        )

    source = SseOfficialXshg2026Source(http_get=get, sleep=waits.append)
    request, _, body = source._retrieve(
        name="trading_rule",
        url=market_sessions._AUTHORITIES[0].url,
        query={},
        headers=market_sessions._AUTHORITIES[0].headers,
        media_type=market_sessions._AUTHORITIES[0].media_type,
    )
    assert request["payload"]["method"] == "GET"
    assert body == bodies["trading_rule"]
    assert waits == [1.0, 2.0]
    assert fetched.evidence.publishable


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_positive_body(), "UNSUPPORTED_POSITIVE"),
        (_positive_body(malformed=True), "SUSPENSION_RESPONSE_INVALID"),
        (_zero_body() + b";", "SUSPENSION_RESPONSE_INVALID"),
        (b"jsonpCallback202({\"x\":NaN})", "SUSPENSION_RESPONSE_INVALID"),
    ],
)
def test_positive_and_malformed_suspension_responses_fail_closed(monkeypatch, body, message):
    with pytest.raises(MarketSessionEvidenceError, match=message):
        live_evidence(monkeypatch, query_body=body)


@pytest.mark.parametrize("field", sorted(market_sessions._ZERO_PAGE.keys() & {
    "beginPage",
    "cacheSize",
    "endPage",
    "pageCount",
    "pageNo",
    "pageSize",
    "pageSizeWithOutLimit",
    "total",
}))
@pytest.mark.parametrize("kind", ["boolean", "float"])
def test_zero_row_envelope_requires_exact_integer_types(monkeypatch, field, kind):
    expected = market_sessions._ZERO_PAGE[field]
    replacement = bool(expected) if kind == "boolean" else float(expected)

    with pytest.raises(MarketSessionEvidenceError, match="integer fields"):
        live_evidence(
            monkeypatch,
            query_body=_zero_body_with_page_value(field, replacement),
        )


@pytest.mark.parametrize(
    "location",
    [
        "evidence_schema",
        "request_schema",
        "retrieval_schema",
        "retrieval_attempt",
        "retrieval_status",
        "artifact_byte_length",
        "suspension_page_size",
        "suspension_page_count",
        "suspension_total",
    ],
)
@pytest.mark.parametrize("kind", ["boolean", "float"])
def test_evidence_schema_requires_exact_integer_types(monkeypatch, location, kind):
    fetched, _, _ = live_evidence(monkeypatch)
    document = json.loads(json.dumps(fetched.evidence.document))

    def wrong_type(value: int):
        return bool(value) if kind == "boolean" else float(value)

    if location == "evidence_schema":
        document["schema_version"] = wrong_type(1)
    elif location == "request_schema":
        name = "trading_rule"
        request = document["requests"][name]
        request["payload"]["schema_version"] = wrong_type(1)
        request["request_id"] = market_sessions._digest(
            market_sessions.REQUEST_DOMAIN, request["payload"]
        )
        retrieval = document["retrievals"][name]
        retrieval["payload"]["request_id"] = request["request_id"]
        retrieval["retrieval_id"] = market_sessions._digest(
            market_sessions.RETRIEVAL_DOMAIN, retrieval["payload"]
        )
    elif location.startswith("retrieval_"):
        retrieval = document["retrievals"]["trading_rule"]
        field = {
            "retrieval_schema": "schema_version",
            "retrieval_attempt": "attempt",
            "retrieval_status": "final_status",
        }[location]
        retrieval["payload"][field] = wrong_type(retrieval["payload"][field])
        retrieval["retrieval_id"] = market_sessions._digest(
            market_sessions.RETRIEVAL_DOMAIN, retrieval["payload"]
        )
    elif location == "artifact_byte_length":
        artifact = next(iter(document["artifacts"].values()))
        artifact["byte_length"] = wrong_type(artifact["byte_length"])
    else:
        field = location.removeprefix("suspension_")
        document["suspension_query"][field] = wrong_type(
            document["suspension_query"][field]
        )

    with pytest.raises(MarketSessionEvidenceError):
        admit_market_session_evidence(document, fetched.evidence.artifact_bytes)


def test_evidence_tampering_and_undeclared_artifacts_reject(monkeypatch):
    fetched, _, _ = live_evidence(monkeypatch)
    document = json.loads(json.dumps(fetched.evidence.document))
    document["scope"]["instrument"] = "601288.SS"
    with pytest.raises(MarketSessionEvidenceError):
        admit_market_session_evidence(document, fetched.evidence.artifact_bytes)

    artifacts = dict(fetched.evidence.artifact_bytes)
    artifacts["0" * 64] = b"undeclared"
    with pytest.raises(MarketSessionEvidenceError, match="artifact set"):
        admit_market_session_evidence(fetched.evidence.document, artifacts)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"status": 500}, "response identity"),
        ({"history": [object()]}, "response identity"),
        ({"media_type": "text/plain"}, "response identity"),
    ],
)
def test_transport_identity_failures_are_terminal(monkeypatch, overrides, message):
    authority = market_sessions._AUTHORITIES[0]
    prepared = requests.Request("GET", authority.url, params=[]).prepare().url
    response = FakeResponse(
        b"test trading rule",
        url=prepared,
        media_type=overrides.get("media_type", authority.media_type),
        status=overrides.get("status", 200),
        history=overrides.get("history"),
    )
    source = SseOfficialXshg2026Source(http_get=lambda *args, **kwargs: response)

    with pytest.raises(MarketSessionEvidenceError, match=message):
        source._retrieve(
            name=authority.name,
            url=authority.url,
            query={},
            headers=authority.headers,
            media_type=authority.media_type,
        )
    assert response.closed


def test_oversized_response_and_exhausted_retry_fail_closed():
    authority = market_sessions._AUTHORITIES[0]
    prepared = requests.Request("GET", authority.url, params=[]).prepare().url
    oversized = FakeResponse(b"xx", url=prepared, media_type=authority.media_type)
    source = SseOfficialXshg2026Source(
        http_get=lambda *args, **kwargs: oversized, maximum_response_bytes=1
    )
    with pytest.raises(MarketSessionEvidenceError, match="size limit"):
        source._retrieve(
            name=authority.name,
            url=authority.url,
            query={},
            headers=authority.headers,
            media_type=authority.media_type,
        )
    waits: list[float] = []
    source = SseOfficialXshg2026Source(
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()),
        sleep=waits.append,
    )
    with pytest.raises(MarketSessionEvidenceError, match="transport failed"):
        source._retrieve(
            name=authority.name,
            url=authority.url,
            query={},
            headers=authority.headers,
            media_type=authority.media_type,
        )
    assert waits == [1.0, 2.0]


def test_synthetic_transport_replays_but_is_not_publishable(monkeypatch):
    fetched, _, _ = live_evidence(monkeypatch)
    document = json.loads(json.dumps(fetched.evidence.document))
    document["classification"] = market_sessions.SYNTHETIC_CLASSIFICATION
    document["limitations"] = market_sessions.SYNTHETIC_LIMITATIONS
    for retrieval in document["retrievals"].values():
        retrieval["payload"]["transport"] = "SYNTHETIC_TEST_FIXTURE"
        retrieval["retrieval_id"] = market_sessions._digest(
            market_sessions.RETRIEVAL_DOMAIN, retrieval["payload"]
        )

    admitted = admit_market_session_evidence(document, fetched.evidence.artifact_bytes)
    assert admitted.publishable is False
