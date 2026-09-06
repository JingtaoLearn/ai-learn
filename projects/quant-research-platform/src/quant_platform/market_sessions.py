from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

import requests


POLICY_VERSION = "XSHG_OFFICIAL_2026_ZERO_SUSPENSION_UPDATE_V1"
EXPECTED_SESSIONS_SOURCE_KIND = "xshg_official_eligible_sessions_v1"
EVIDENCE_DOMAIN = b"quant-platform/xshg-market-session-evidence/v1"
ARTIFACT_DOMAIN = b"quant-platform/xshg-market-session-artifact/v1"
REQUEST_DOMAIN = b"quant-platform/source-request/v1"
RETRIEVAL_DOMAIN = b"quant-platform/source-retrieval/v1"
EFFECTIVE_START = date(2026, 7, 6)
EFFECTIVE_END = date(2026, 12, 31)
IN_SCOPE_CLOSURES = frozenset(
    {
        date(2026, 9, 25),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    }
)
LIMITATIONS = [
    "POSITIVE_SUSPENSION_ROWS_UNSUPPORTED_V1",
    "VALID_ONLY_FOR_EXACT_INSTRUMENT_AND_INTERVAL",
]
CLASSIFICATION = "ELIGIBLE_ZERO_OFFICIAL_SUSPENSION_ROWS"
SYNTHETIC_CLASSIFICATION = "SYNTHETIC_REPLAY_ONLY"
SYNTHETIC_LIMITATIONS = ["NOT_ADMISSIBLE_FOR_PUBLICATION", *LIMITATIONS]
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CALLBACK = "jsonpCallback202"
SUSPENSION_URL = "https://query.sse.com.cn/commonSoaQuery.do"
USER_AGENT = "quant-research-platform/0.1"


class MarketSessionEvidenceError(ValueError):
    """Raised when official XSHG session evidence cannot be admitted."""


@dataclass(frozen=True)
class MarketSessionEvidence:
    document: dict[str, Any]
    artifact_bytes: dict[str, bytes]
    digest: str
    publishable: bool

    def json_bytes(self) -> bytes:
        return _canonical_json(self.document) + b"\n"


@dataclass(frozen=True)
class FetchedMarketSessions:
    market_sessions: tuple[str, ...]
    eligible_sessions: tuple[str, ...]
    evidence: MarketSessionEvidence


class MarketSessionEvidenceSource(Protocol):
    def fetch(self, instrument: str, start: str, end: str) -> FetchedMarketSessions: ...


@dataclass(frozen=True)
class _Authority:
    name: str
    kind: str
    document_id: str
    effective_from: str | None
    effective_through: str | None
    clauses: tuple[str, ...]
    url: str
    media_type: str
    body_sha256: str
    headers: Mapping[str, str]


_AUTHORITIES = (
    _Authority(
        "trading_rule",
        "XSHG_TRADING_RULE",
        "上证发〔2026〕41号",
        "2026-07-06",
        "2026-12-31",
        ("2.4.1", "4.2.5", "4.2.6"),
        "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/10816492/files/704204728fe74fff89de4f16efda4791.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "fc922c433438b2636cb631eab25cca405209712acbb6aaded768c45456ff8888",
        {"Accept": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "User-Agent": USER_AGENT},
    ),
    _Authority(
        "closure_notice",
        "XSHG_2026_CLOSURE_NOTICE",
        "上证公告〔2025〕45号",
        "2026-01-01",
        "2026-12-31",
        ("2026 holiday closure schedule",),
        "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
        "text/html",
        "31a7485aac90814727a0bb09a30763cb81b753863e51a6c986ad2ffc9c76a697",
        {"Accept": "text/html", "User-Agent": USER_AGENT},
    ),
    _Authority(
        "suspension_page",
        "XSHG_SUSPENSION_PAGE",
        "SSE suspension/resumption page",
        None,
        None,
        (),
        "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/",
        "text/html",
        "901418905223d0ea05a432784e0e285eb597185136b84397864229e57a046b6f",
        {"Accept": "text/html", "User-Agent": USER_AGENT},
    ),
    _Authority(
        "suspension_script",
        "XSHG_SUSPENSION_PAGE_SCRIPT",
        "search_tradeTip_2021.js ssesite_V3.8.0_20260828",
        None,
        None,
        ("GW_PL_JYTS_TFPXX query contract",),
        "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_tradeTip_2021.js?v=ssesite_V3.8.0_20260828",
        "application/javascript",
        "32fc98ea41e599f457609247ba68defb82e6d855aa2551da6923dbf7e44153a7",
        {"Accept": "application/javascript", "User-Agent": USER_AGENT},
    ),
)
_AUTHORITY_NAMES = tuple(authority.name for authority in _AUTHORITIES)
_REQUEST_NAMES = (*_AUTHORITY_NAMES, "suspension_query_page_1")
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "policy_version",
    "scope",
    "authorities",
    "requests",
    "retrievals",
    "suspension_query",
    "market_sessions",
    "eligible_sessions",
    "suspended_sessions",
    "classification",
    "limitations",
    "artifacts",
}
_SCOPE_FIELDS = {"market", "instrument", "start", "end", "timezone"}
_AUTHORITY_FIELDS = {
    "kind",
    "document_id",
    "effective_from",
    "effective_through",
    "clauses",
    "url",
    "artifact_id",
}
_REQUEST_FIELDS = {"request_id", "payload"}
_REQUEST_PAYLOAD_FIELDS = {"schema_version", "method", "url", "query", "headers"}
_RETRIEVAL_FIELDS = {"retrieval_id", "payload"}
_RETRIEVAL_PAYLOAD_FIELDS = {
    "schema_version",
    "transport",
    "request_id",
    "attempt",
    "final_url",
    "final_status",
    "redirects",
    "media_type",
    "artifact_id",
}
_ARTIFACT_FIELDS = {
    "artifact_id",
    "body_sha256",
    "byte_length",
    "media_type",
    "source_url",
    "path",
}
_SUSPENSION_FIELDS = {"sql_id", "callback", "page_size", "page_count", "total", "rows"}
_ZERO_ROOT = {
    "actionErrors": [],
    "actionMessages": [],
    "fieldErrors": {},
    "isPagination": "true",
    "jsonCallBack": CALLBACK,
    "locale": "en_CN",
    "pageNo": None,
    "pageSize": None,
    "queryDate": "",
    "result": [],
    "securityCode": "",
    "sqlId": "GW_PL_JYTS_TFPXX",
    "texts": None,
    "type": "",
    "validateCode": "",
}
_ZERO_PAGE = {
    "beginPage": 0,
    "cacheSize": 1,
    "data": [],
    "endDate": None,
    "endPage": 1,
    "objectResult": None,
    "pageCount": 0,
    "pageNo": 1,
    "pageSize": 100,
    "pageSizeWithOutLimit": 100,
    "searchDate": None,
    "sort": None,
    "startDate": None,
    "total": 0,
}
_POSITIVE_ROW_FIELDS = {
    "productCode",
    "productName",
    "controlType",
    "startStopDate",
    "endStopDate",
    "stopTime",
    "stopReason",
    "endStopReason",
    "type",
}


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: non-canonical JSON") from exc


def _digest(domain: bytes, value: Any) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical_json(value)).hexdigest()


def _artifact_id(body: bytes) -> str:
    return hashlib.sha256(ARTIFACT_DOMAIN + b"\0" + body).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MarketSessionEvidenceError(
                f"SUSPENSION_RESPONSE_INVALID: duplicate JSON field {key}"
            )
        result[key] = value
    return result


def _parse_date(value: Any, label: str) -> date:
    if type(value) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise MarketSessionEvidenceError(f"UNSUPPORTED_DATE_RANGE: invalid {label}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MarketSessionEvidenceError(f"UNSUPPORTED_DATE_RANGE: invalid {label}") from exc
    if parsed.isoformat() != value:
        raise MarketSessionEvidenceError(f"UNSUPPORTED_DATE_RANGE: non-canonical {label}")
    return parsed


def _validate_scope(instrument: str, start: str, end: str) -> tuple[date, date]:
    if type(instrument) is not str or re.fullmatch(r"[0-9]{6}\.SS", instrument) is None:
        raise MarketSessionEvidenceError(
            "UNSUPPORTED_DATE_RANGE: instrument must be an ordinary six-digit .SS symbol"
        )
    first = _parse_date(start, "start")
    last = _parse_date(end, "end")
    if first > last or first < EFFECTIVE_START or last > EFFECTIVE_END:
        raise MarketSessionEvidenceError(
            "UNSUPPORTED_DATE_RANGE: range must be inside 2026-07-06..2026-12-31"
        )
    return first, last


def _sessions(first: date, last: date) -> tuple[str, ...]:
    values: list[str] = []
    current = first
    while current <= last:
        if current.weekday() < 5 and current not in IN_SCOPE_CLOSURES:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(values)


def _prepared_url(url: str, query: Mapping[str, str]) -> str:
    prepared = requests.Request("GET", url, params=sorted(query.items())).prepare().url
    if prepared is None:
        raise MarketSessionEvidenceError("OFFICIAL_AUTHORITY_UNAVAILABLE: request URL is invalid")
    return prepared


def _request_payload(url: str, query: Mapping[str, str], headers: Mapping[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "method": "GET",
        "url": url,
        "query": dict(query),
        "headers": dict(headers),
    }


def _read_response(response: Any, maximum_response_bytes: int) -> bytes:
    payload = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not isinstance(chunk, bytes):
                raise MarketSessionEvidenceError(
                    "OFFICIAL_AUTHORITY_UNAVAILABLE: response chunk is not bytes"
                )
            payload.extend(chunk)
            if len(payload) > maximum_response_bytes:
                raise MarketSessionEvidenceError(
                    "OFFICIAL_AUTHORITY_UNAVAILABLE: response exceeds size limit"
                )
    finally:
        response.close()
    return bytes(payload)


def _parse_suspension(body: bytes, product_code: str) -> dict[str, Any]:
    prefix = f"{CALLBACK}(".encode()
    if not body.startswith(prefix) or not body.endswith(b")"):
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: JSONP wrapper mismatch")
    inner = body[len(prefix) : -1]
    if not inner or body.startswith(b"\xef\xbb\xbf") or body.endswith(b");"):
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: JSONP framing mismatch")
    try:
        decoded = inner.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                MarketSessionEvidenceError(
                    f"SUSPENSION_RESPONSE_INVALID: non-finite JSON value {item}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: invalid JSONP body") from exc
    if type(value) is not dict or set(value) != {*_ZERO_ROOT, "pageHelp"}:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: root fields mismatch")
    page = value.get("pageHelp")
    if type(page) is not dict or set(page) != set(_ZERO_PAGE):
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: page fields mismatch")
    rows = value.get("result")
    page_rows = page.get("data")
    if not isinstance(rows, list) or rows != page_rows:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: row arrays mismatch")
    integer_fields = {
        "beginPage": 0 if not rows else None,
        "cacheSize": 1,
        "endPage": 1 if not rows else None,
        "pageCount": 0 if not rows else None,
        "pageNo": 1,
        "pageSize": 100,
        "pageSizeWithOutLimit": 100,
        "total": 0 if not rows else None,
    }
    if any(
        type(page.get(field)) is not int
        or (expected is not None and page[field] != expected)
        for field, expected in integer_fields.items()
    ):
        raise MarketSessionEvidenceError(
            "SUSPENSION_RESPONSE_INVALID: page integer fields mismatch"
        )
    if rows:
        total: int = page["total"]
        page_count: int = page["pageCount"]
        valid = (
            total > 0
            and page_count == math.ceil(total / 100)
            and len(rows) == min(total, 100)
            and 1 <= page["beginPage"] <= page_count
            and 1 <= page["endPage"] <= page_count
            and all(page.get(field) is None for field in ("endDate", "objectResult", "searchDate", "sort", "startDate"))
            and all(
                type(row) is dict
                and set(row) == _POSITIVE_ROW_FIELDS
                and all(type(item) is str for item in row.values())
                and row["productCode"] == product_code
                for row in rows
            )
        )
        scalar = dict(value)
        scalar.pop("pageHelp")
        scalar["result"] = []
        if not valid or scalar != _ZERO_ROOT:
            raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: positive row shape mismatch")
        raise MarketSessionEvidenceError(
            "UNSUPPORTED_POSITIVE_SUSPENSION_ROWS: positive rows are not publishable"
        )
    scalar = dict(value)
    scalar.pop("pageHelp")
    if scalar != _ZERO_ROOT or page != _ZERO_PAGE:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: zero-row envelope mismatch")
    return {
        "sql_id": "GW_PL_JYTS_TFPXX",
        "callback": CALLBACK,
        "page_size": 100,
        "page_count": 0,
        "total": 0,
        "rows": [],
    }


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise MarketSessionEvidenceError(f"SUSPENSION_RESPONSE_INVALID: {label} fields mismatch")
    return value


def _validate_ordered_dates(values: Any, label: str) -> tuple[str, ...]:
    if type(values) is not list:
        raise MarketSessionEvidenceError(f"SUSPENSION_RESPONSE_INVALID: {label} must be an array")
    parsed = tuple(_parse_date(value, label) for value in values)
    if list(parsed) != sorted(set(parsed)):
        raise MarketSessionEvidenceError(f"SUSPENSION_RESPONSE_INVALID: {label} is not ordered unique")
    return tuple(value.isoformat() for value in parsed)


def admit_market_session_evidence(
    document: Any, artifact_bytes: Mapping[str, bytes]
) -> MarketSessionEvidence:
    """Strictly verify one canonical market-session evidence document and its artifacts."""

    document = _exact_fields(document, _TOP_LEVEL_FIELDS, "evidence")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: evidence version mismatch")
    if document["policy_version"] != POLICY_VERSION:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: evidence version mismatch")
    scope = _exact_fields(document["scope"], _SCOPE_FIELDS, "scope")
    first, last = _validate_scope(scope.get("instrument"), scope.get("start"), scope.get("end"))
    if scope.get("market") != "XSHG" or scope.get("timezone") != "Asia/Shanghai":
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: scope mismatch")
    expected_sessions = _sessions(first, last)
    market_sessions = _validate_ordered_dates(document["market_sessions"], "market_sessions")
    eligible_sessions = _validate_ordered_dates(document["eligible_sessions"], "eligible_sessions")
    suspended_sessions = _validate_ordered_dates(document["suspended_sessions"], "suspended_sessions")
    if market_sessions != expected_sessions or eligible_sessions != market_sessions or suspended_sessions:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: eligible sessions mismatch")

    authorities = document["authorities"]
    requests_map = document["requests"]
    retrievals = document["retrievals"]
    if type(authorities) is not dict or set(authorities) != set(_AUTHORITY_NAMES):
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: authorities mismatch")
    if type(requests_map) is not dict or set(requests_map) != set(_REQUEST_NAMES):
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: requests mismatch")
    if type(retrievals) is not dict or set(retrievals) != set(_REQUEST_NAMES):
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: retrievals mismatch")

    artifacts = document["artifacts"]
    if type(artifacts) is not dict or type(artifact_bytes) is not dict:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: artifacts must be objects")
    if set(artifacts) != set(artifact_bytes):
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: artifact set mismatch")
    for artifact_id, record in artifacts.items():
        _exact_fields(record, _ARTIFACT_FIELDS, "artifact")
        body = artifact_bytes[artifact_id]
        if (
            type(artifact_id) is not str
            or re.fullmatch(r"[0-9a-f]{64}", artifact_id) is None
            or type(body) is not bytes
            or record["artifact_id"] != artifact_id
            or record["path"] != f"market-session-{artifact_id}.bin"
            or record["body_sha256"] != hashlib.sha256(body).hexdigest()
            or type(record["byte_length"]) is not int
            or record["byte_length"] != len(body)
            or _artifact_id(body) != artifact_id
        ):
            raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: artifact identity mismatch")

    for name in _REQUEST_NAMES:
        request = _exact_fields(requests_map[name], _REQUEST_FIELDS, "request")
        payload = _exact_fields(request["payload"], _REQUEST_PAYLOAD_FIELDS, "request payload")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or request["request_id"] != _digest(REQUEST_DOMAIN, payload)
        ):
            raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: request identity mismatch")
        retrieval = _exact_fields(retrievals[name], _RETRIEVAL_FIELDS, "retrieval")
        retrieval_payload = _exact_fields(
            retrieval["payload"], _RETRIEVAL_PAYLOAD_FIELDS, "retrieval payload"
        )
        if (
            type(retrieval_payload["schema_version"]) is not int
            or retrieval_payload["schema_version"] != 1
            or type(retrieval_payload["attempt"]) is not int
            or type(retrieval_payload["final_status"]) is not int
            or retrieval_payload["request_id"] != request["request_id"]
            or retrieval_payload["artifact_id"] not in artifacts
            or retrieval["retrieval_id"] != _digest(RETRIEVAL_DOMAIN, retrieval_payload)
        ):
            raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: retrieval identity mismatch")

    for authority in _AUTHORITIES:
        record = _exact_fields(authorities[authority.name], _AUTHORITY_FIELDS, "authority")
        if record != {
            "kind": authority.kind,
            "document_id": authority.document_id,
            "effective_from": authority.effective_from,
            "effective_through": authority.effective_through,
            "clauses": list(authority.clauses),
            "url": authority.url,
            "artifact_id": retrievals[authority.name]["payload"]["artifact_id"],
        }:
            raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: authority mismatch")
        artifact = artifacts[record["artifact_id"]]
        request_payload = requests_map[authority.name]["payload"]
        retrieval_payload = retrievals[authority.name]["payload"]
        if request_payload != _request_payload(authority.url, {}, authority.headers):
            raise MarketSessionEvidenceError(
                "SUSPENSION_RESPONSE_INVALID: authority request mismatch"
            )
        if (
            retrieval_payload["final_url"] != authority.url
            or retrieval_payload["final_status"] != 200
            or retrieval_payload["redirects"] != []
            or retrieval_payload["media_type"] != authority.media_type
            or type(retrieval_payload["attempt"]) is not int
            or not 1 <= retrieval_payload["attempt"] <= 3
            or artifact["media_type"] != authority.media_type
            or artifact["source_url"] != authority.url
        ):
            raise MarketSessionEvidenceError(
                "SUSPENSION_RESPONSE_INVALID: authority retrieval mismatch"
            )
        if artifact["body_sha256"] != authority.body_sha256:
            raise MarketSessionEvidenceError("OFFICIAL_AUTHORITY_HASH_MISMATCH: authority bytes changed")

    product_code = scope["instrument"].removesuffix(".SS")
    query = {
        "isPagination": "true",
        "sqlId": "GW_PL_JYTS_TFPXX",
        "pageHelp.pageSize": "100",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "productCode": product_code,
        "keyWords": "",
        "startStopDate": first.strftime("%Y%m%d"),
        "endStopDate": last.strftime("%Y%m%d"),
        "jsonCallBack": CALLBACK,
    }
    query_headers = {
        "Accept": "application/javascript, application/json;q=0.9",
        "Referer": "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/",
        "User-Agent": USER_AGENT,
    }
    query_request = requests_map["suspension_query_page_1"]["payload"]
    query_retrieval = retrievals["suspension_query_page_1"]["payload"]
    query_artifact = artifacts[query_retrieval["artifact_id"]]
    if query_request != _request_payload(SUSPENSION_URL, query, query_headers):
        raise MarketSessionEvidenceError(
            "SUSPENSION_RESPONSE_INVALID: suspension request mismatch"
        )
    if (
        query_retrieval["final_url"] != _prepared_url(SUSPENSION_URL, query)
        or query_retrieval["final_status"] != 200
        or query_retrieval["redirects"] != []
        or query_retrieval["media_type"] != "application/json;charset=UTF-8"
        or type(query_retrieval["attempt"]) is not int
        or not 1 <= query_retrieval["attempt"] <= 3
        or query_artifact["media_type"] != "application/json;charset=UTF-8"
        or query_artifact["source_url"] != SUSPENSION_URL
    ):
        raise MarketSessionEvidenceError(
            "SUSPENSION_RESPONSE_INVALID: suspension retrieval mismatch"
        )
    _parse_suspension(artifact_bytes[query_retrieval["artifact_id"]], product_code)

    suspension = _exact_fields(document["suspension_query"], _SUSPENSION_FIELDS, "suspension query")
    if any(
        type(suspension[field]) is not int
        for field in ("page_size", "page_count", "total")
    ) or suspension != {
        "sql_id": "GW_PL_JYTS_TFPXX",
        "callback": CALLBACK,
        "page_size": 100,
        "page_count": 0,
        "total": 0,
        "rows": [],
    }:
        raise MarketSessionEvidenceError("SUSPENSION_PAGINATION_INCOMPLETE: query is incomplete")

    transports = {retrievals[name]["payload"]["transport"] for name in _REQUEST_NAMES}
    ordinary = (
        document["classification"] == CLASSIFICATION
        and document["limitations"] == LIMITATIONS
        and transports == {"LIVE_HTTP"}
    )
    synthetic = (
        document["classification"] == SYNTHETIC_CLASSIFICATION
        and document["limitations"] == SYNTHETIC_LIMITATIONS
        and transports == {"SYNTHETIC_TEST_FIXTURE"}
    )
    if not ordinary and not synthetic:
        raise MarketSessionEvidenceError("SUSPENSION_RESPONSE_INVALID: publication classification mismatch")
    detached = json.loads(_canonical_json(document), object_pairs_hook=_strict_object)
    detached_artifacts = {key: bytes(value) for key, value in artifact_bytes.items()}
    return MarketSessionEvidence(
        document=detached,
        artifact_bytes=detached_artifacts,
        digest=_digest(EVIDENCE_DOMAIN, detached),
        publishable=ordinary,
    )


class SseOfficialXshg2026Source:
    """Retrieve and admit the exact official XSHG 2026 eligible-session evidence."""

    def __init__(
        self,
        *,
        http_get: Callable[..., Any] = requests.get,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 20.0,
        maximum_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.http_get = http_get
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.timeout = timeout
        self.maximum_response_bytes = maximum_response_bytes
        if type(timeout) not in {int, float} or timeout <= 0:
            raise ValueError("timeout must be positive")
        if type(maximum_response_bytes) is not int or maximum_response_bytes < 1:
            raise ValueError("maximum_response_bytes must be positive")

    def _retrieve(
        self,
        *,
        name: str,
        url: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        media_type: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bytes]:
        payload = _request_payload(url, query, headers)
        request = {"request_id": _digest(REQUEST_DOMAIN, payload), "payload": payload}
        expected_url = _prepared_url(url, query)
        for attempt in range(1, 4):
            try:
                response = self.http_get(
                    url,
                    params=dict(sorted(query.items())),
                    headers=dict(headers),
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == 3:
                    raise MarketSessionEvidenceError(
                        f"OFFICIAL_AUTHORITY_UNAVAILABLE: {name} transport failed"
                    ) from exc
                self.sleep(float(attempt))
                continue
            status = getattr(response, "status_code", None)
            if status in {429, 502, 503, 504}:
                response.close()
                if attempt == 3:
                    raise MarketSessionEvidenceError(
                        f"OFFICIAL_AUTHORITY_UNAVAILABLE: {name} exhausted retries"
                    )
                self.sleep(float(attempt))
                continue
            history = getattr(response, "history", [])
            final_url = getattr(response, "url", None)
            content_type = getattr(response, "headers", {}).get("Content-Type")
            if content_type is None:
                content_type = getattr(response, "headers", {}).get("content-type")
            if status != 200 or history or final_url != expected_url or content_type != media_type:
                response.close()
                raise MarketSessionEvidenceError(
                    f"OFFICIAL_AUTHORITY_UNAVAILABLE: {name} response identity mismatch"
                )
            body = _read_response(response, self.maximum_response_bytes)
            artifact_id = _artifact_id(body)
            retrieval_payload = {
                "schema_version": 1,
                "transport": "LIVE_HTTP",
                "request_id": request["request_id"],
                "attempt": attempt,
                "final_url": final_url,
                "final_status": status,
                "redirects": [],
                "media_type": media_type,
                "artifact_id": artifact_id,
            }
            retrieval = {
                "retrieval_id": _digest(RETRIEVAL_DOMAIN, retrieval_payload),
                "payload": retrieval_payload,
            }
            return request, retrieval, body
        raise AssertionError("unreachable retrieval attempt")

    def fetch(self, instrument: str, start: str, end: str) -> FetchedMarketSessions:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise MarketSessionEvidenceError("UNSUPPORTED_DATE_RANGE: clock must be timezone-aware")
        first, last = _validate_scope(instrument, start, end)
        product_code = instrument.removesuffix(".SS")
        query = {
            "isPagination": "true",
            "sqlId": "GW_PL_JYTS_TFPXX",
            "pageHelp.pageSize": "100",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "productCode": product_code,
            "keyWords": "",
            "startStopDate": first.strftime("%Y%m%d"),
            "endStopDate": last.strftime("%Y%m%d"),
            "jsonCallBack": CALLBACK,
        }
        suspension_headers = {
            "Accept": "application/javascript, application/json;q=0.9",
            "Referer": "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/",
            "User-Agent": USER_AGENT,
        }
        requests_map: dict[str, Any] = {}
        retrievals: dict[str, Any] = {}
        artifacts: dict[str, Any] = {}
        artifact_bytes: dict[str, bytes] = {}
        authorities: dict[str, Any] = {}

        retrieval_plan = [
            (authority.name, authority.url, {}, authority.headers, authority.media_type)
            for authority in _AUTHORITIES
        ] + [
            (
                "suspension_query_page_1",
                SUSPENSION_URL,
                query,
                suspension_headers,
                "application/json;charset=UTF-8",
            )
        ]
        for name, url, request_query, headers, media_type in retrieval_plan:
            request, retrieval, body = self._retrieve(
                name=name,
                url=url,
                query=request_query,
                headers=headers,
                media_type=media_type,
            )
            requests_map[name] = request
            retrievals[name] = retrieval
            artifact_id = retrieval["payload"]["artifact_id"]
            artifacts[artifact_id] = {
                "artifact_id": artifact_id,
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "byte_length": len(body),
                "media_type": media_type,
                "source_url": url,
                "path": f"market-session-{artifact_id}.bin",
            }
            artifact_bytes[artifact_id] = body

        for authority in _AUTHORITIES:
            artifact_id = retrievals[authority.name]["payload"]["artifact_id"]
            if artifacts[artifact_id]["body_sha256"] != authority.body_sha256:
                raise MarketSessionEvidenceError(
                    f"OFFICIAL_AUTHORITY_HASH_MISMATCH: {authority.name} bytes changed"
                )
            authorities[authority.name] = {
                "kind": authority.kind,
                "document_id": authority.document_id,
                "effective_from": authority.effective_from,
                "effective_through": authority.effective_through,
                "clauses": list(authority.clauses),
                "url": authority.url,
                "artifact_id": artifact_id,
            }

        query_body = artifact_bytes[
            retrievals["suspension_query_page_1"]["payload"]["artifact_id"]
        ]
        suspension_query = _parse_suspension(query_body, product_code)
        sessions = _sessions(first, last)
        if not sessions:
            raise MarketSessionEvidenceError("NO_ELIGIBLE_SESSIONS: range has no market sessions")
        document = {
            "schema_version": 1,
            "policy_version": POLICY_VERSION,
            "scope": {
                "market": "XSHG",
                "instrument": instrument,
                "start": first.isoformat(),
                "end": last.isoformat(),
                "timezone": "Asia/Shanghai",
            },
            "authorities": authorities,
            "requests": requests_map,
            "retrievals": retrievals,
            "suspension_query": suspension_query,
            "market_sessions": list(sessions),
            "eligible_sessions": list(sessions),
            "suspended_sessions": [],
            "classification": CLASSIFICATION,
            "limitations": list(LIMITATIONS),
            "artifacts": artifacts,
        }
        evidence = admit_market_session_evidence(document, artifact_bytes)
        if not evidence.publishable:
            raise MarketSessionEvidenceError(
                "SUSPENSION_RESPONSE_INVALID: live source produced non-publishable evidence"
            )
        return FetchedMarketSessions(
            market_sessions=sessions,
            eligible_sessions=sessions,
            evidence=evidence,
        )
