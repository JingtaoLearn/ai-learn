from __future__ import annotations

import copy
import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


REQUEST_DOMAIN = "quant-platform/source-request/v1"
RETRIEVAL_DOMAIN = "quant-platform/source-retrieval/v1"
ARTIFACT_DOMAIN = "quant-platform/source-artifact/v1"
SERIES_DOMAIN = "quant-platform/corporate-action-series/v1"
REVISION_DOMAIN = "quant-platform/corporate-action-revision/v1"
COVERAGE_DOMAIN = "quant-platform/corporate-action-coverage/v1"
EVIDENCE_DOMAIN = "quant-platform/corporate-action-evidence/v1"
TAX_POLICY_DOMAIN = "quant-platform/tax-policy/v1"
SETTLEMENT_POLICY_DOMAIN = "quant-platform/settlement-policy/v1"
ROUNDING_POLICY_DOMAIN = "quant-platform/rounding-policy/v1"
COVERAGE_STATES = {
    "VERIFIED_EVENTS",
    "VERIFIED_NO_ACTION",
    "VERIFIED_COMPLETE_INTERVAL",
    "UNKNOWN_MISSING",
}
USE_ROLES = {"CAUSAL_FEATURE", "ACCOUNTING_OUTCOME"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_BOCOM_URL = (
    "https",
    "www.bankcomm.com",
    "/BankCommSite/file/fileDownload.html",
)
_BOCOM_REQUEST_URL = "https://www.bankcomm.com/BankCommSite/file/fileDownload.html"
_BOCOM_ARTIFACT_URL = f"{_BOCOM_REQUEST_URL}?fileId=94697c067ebe4427a4165910712df44d"
_XSHG_TIMEZONE = "Asia/Shanghai"
_XSHG_SESSION_CLOSE = time(15, 0)
FEN_PER_CNY = Decimal("100")
ROUNDING_ASSUMPTION = "ROUND_HALF_UP_RESEARCH_ASSUMPTION"
TAX_POLICY_PAYLOAD = {
    "account_scope": "PER_SECURITIES_ACCOUNT_WITH_SEPARATE_CUSTODIAN_PARTITIONS",
    "acquisition_scope": "SUPPORTED_PUBLIC_OR_TRANSFER_MARKET_A_SHARE",
    "assumptions": {"currency_rounding": ROUNDING_ASSUMPTION},
    "brackets": [
        {
            "actual_burden": "0.20",
            "holding_period": "UP_TO_AND_INCLUDING_ONE_NATURAL_MONTH",
        },
        {
            "actual_burden": "0.10",
            "holding_period": "OVER_ONE_NATURAL_MONTH_THROUGH_ONE_NATURAL_YEAR",
        },
        {"actual_burden": "0.00", "holding_period": "OVER_ONE_NATURAL_YEAR"},
    ],
    "checked_at": "2026-08-31T15:28:12Z",
    "collection": "AFTER_TRANSFER_SETTLEMENT_NEXT_TRADING_DAY_PROCESS",
    "effective_record_date_start": "2015-09-09",
    "fifo": "PER_SECURITIES_ACCOUNT_END_OF_DAY_NET_CHANGE",
    "holding_period_endpoint": "DAY_BEFORE_TRANSFER_SETTLEMENT",
    "policy_family": "CN-INDIVIDUAL-A-2015-101",
    "policy_version": 1,
    "schema_version": 1,
    "source_artifact_ids": [
        "8d66656959a6d24afe8eee860700d319b8d52eaa18dea4f72115d421cd4235a3",
        "cfde8e517c8d5989eef3ffed8fb203e5e5aadfc793f61b6ec345c22f72a85363",
        "4f2bc55a4899497d2a7e1bec59f9fd71b7437fc29e8a343be9d4f09bbc129e62",
    ],
    "taxpayer_scope": "ORDINARY_MAINLAND_INDIVIDUAL",
    "unknowns": ["OFFICIAL_CURRENCY_ROUNDING", "TRADE_DATE_TO_SETTLEMENT_MAPPING"],
}
TAX_POLICY_ID = "ea2910dace5c605a6ddd39b8346f7f12003689641c7db4b56a56ca3c015d3223"
ROUNDING_POLICY_PAYLOAD = {
    "schema_version": 1,
    "policy": ROUNDING_ASSUMPTION,
    "currency": "CNY",
    "minor_unit": "FEN",
}
SYNTHETIC_COMPLETE_CONTRACT = {
    "schema_version": 1,
    "source_contract_version": "synthetic-complete-enumeration@1",
    "scope": "DETERMINISTIC_NON_OBSERVATIONAL_TEST_FIXTURE_ONLY",
    "coverage_states": ["VERIFIED_NO_ACTION", "VERIFIED_COMPLETE_INTERVAL"],
}


class CorporateActionEvidenceError(ValueError):
    """Raised when corporate-action evidence cannot be admitted exactly."""


def cny_to_fen(value: Decimal | str | int) -> int:
    """Post an exact CNY value at the versioned research minor-unit assumption."""

    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CorporateActionEvidenceError("money value is not an exact decimal") from exc
    if not decimal.is_finite():
        raise CorporateActionEvidenceError("money value must be finite")
    return int((decimal * FEN_PER_CNY).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fen_to_cny(value: int) -> float:
    if type(value) is not int:
        raise CorporateActionEvidenceError("fen value must be an integer")
    return float(Decimal(value) / FEN_PER_CNY)


def _calendar_anniversary(value: date, *, months: int = 0, years: int = 0) -> date:
    year = value.year + years + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def dividend_tax_burden(acquisition_date: date, transfer_settlement_date: date) -> Decimal:
    """Return the frozen individual A-share burden through settlement's prior day."""

    if transfer_settlement_date <= acquisition_date:
        raise CorporateActionEvidenceError("transfer settlement must follow acquisition")
    holding_period_endpoint = transfer_settlement_date - timedelta(days=1)
    if holding_period_endpoint <= _calendar_anniversary(acquisition_date, months=1):
        return Decimal("0.20")
    if holding_period_endpoint <= _calendar_anniversary(acquisition_date, years=1):
        return Decimal("0.10")
    return Decimal("0.00")


@dataclass(frozen=True)
class CashDividend:
    event_revision_id: str
    record_date: date
    ex_date: date
    pay_date: date
    gross_cash_per_share: Decimal


@dataclass(frozen=True)
class SettlementSchedule:
    """Explicit research mapping for facts the admitted official policy leaves unknown."""

    trade_to_settlement: Mapping[str, str]
    settlement_to_collection: Mapping[str, str]
    assumption: str = "EXPLICIT_TRANSFER_SETTLEMENT_AND_COLLECTION_DATES_V1"

    def __post_init__(self) -> None:
        trade_map = dict(self.trade_to_settlement)
        collection_map = dict(self.settlement_to_collection)
        if not trade_map:
            raise CorporateActionEvidenceError("settlement schedule must contain trade mappings")
        for trade, settlement in trade_map.items():
            trade_date = _date(trade, "trade settlement mapping trade")
            settlement_date = _date(settlement, "trade settlement mapping settlement")
            if settlement_date < trade_date:
                raise CorporateActionEvidenceError("transfer settlement precedes trade")
        for settlement, collection in collection_map.items():
            settlement_date = _date(settlement, "tax collection mapping settlement")
            collection_date = _date(collection, "tax collection mapping collection")
            if collection_date <= settlement_date:
                raise CorporateActionEvidenceError(
                    "tax collection must be after transfer settlement"
                )
        if not isinstance(self.assumption, str) or not self.assumption:
            raise CorporateActionEvidenceError("settlement assumption must be explicit")
        object.__setattr__(self, "trade_to_settlement", MappingProxyType(trade_map))
        object.__setattr__(self, "settlement_to_collection", MappingProxyType(collection_map))

    @property
    def document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "assumption": self.assumption,
            "trade_to_settlement": dict(self.trade_to_settlement),
            "settlement_to_collection": dict(self.settlement_to_collection),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.document)).hexdigest()

    @property
    def policy_id(self) -> str:
        return identity_digest(SETTLEMENT_POLICY_DOMAIN, self.document)

    def settlement_date(self, trade_date: date) -> date:
        value = self.trade_to_settlement.get(trade_date.isoformat())
        if value is None:
            raise CorporateActionEvidenceError(
                f"transfer settlement mapping is unknown for {trade_date.isoformat()}"
            )
        return _date(value, "transfer settlement")

    def collection_date(self, settlement_date: date) -> date:
        value = self.settlement_to_collection.get(settlement_date.isoformat())
        if value is None:
            raise CorporateActionEvidenceError(
                f"next-trading-day collection mapping is unknown for {settlement_date.isoformat()}"
            )
        return _date(value, "tax collection")


def _reject_float(value: str) -> None:
    raise CorporateActionEvidenceError(f"floating-point JSON value is forbidden: {value}")


def load_strict_json(payload: bytes) -> Any:
    """Load identity JSON while rejecting duplicate keys and floating-point values."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CorporateActionEvidenceError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorporateActionEvidenceError(f"invalid JSON evidence: {exc}") from exc


def _validate_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if isinstance(value, float):
        raise CorporateActionEvidenceError(f"floating-point value is forbidden at {path}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CorporateActionEvidenceError(f"non-string object key at {path}")
        for key, item in value.items():
            _validate_json_value(item, f"{path}.{key}")
        return
    raise CorporateActionEvidenceError(f"unsupported identity value at {path}")


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def identity_digest(domain_tag: str, payload: Any) -> str:
    if not isinstance(domain_tag, str) or not domain_tag:
        raise CorporateActionEvidenceError("identity domain tag must be a non-empty string")
    body = payload if isinstance(payload, bytes) else canonical_json_bytes(payload)
    return hashlib.sha256(domain_tag.encode("utf-8") + b"\0" + body).hexdigest()


def tax_policy_identity() -> dict[str, Any]:
    """Return the checksum-bound accepted tax policy or fail closed."""

    if identity_digest(TAX_POLICY_DOMAIN, TAX_POLICY_PAYLOAD) != TAX_POLICY_ID:
        raise CorporateActionEvidenceError("frozen tax policy identity mismatch")
    return {
        "tax_policy_id": TAX_POLICY_ID,
        "sha256": hashlib.sha256(canonical_json_bytes(TAX_POLICY_PAYLOAD)).hexdigest(),
        "payload": copy.deepcopy(TAX_POLICY_PAYLOAD),
    }


def rounding_policy_identity() -> dict[str, Any]:
    """Return the exact versioned integer-fen rounding policy identity."""

    return {
        "rounding_policy_id": identity_digest(ROUNDING_POLICY_DOMAIN, ROUNDING_POLICY_PAYLOAD),
        "sha256": hashlib.sha256(canonical_json_bytes(ROUNDING_POLICY_PAYLOAD)).hexdigest(),
        "payload": copy.deepcopy(ROUNDING_POLICY_PAYLOAD),
    }


def _require_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorporateActionEvidenceError(f"{label} fields are invalid")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CorporateActionEvidenceError(f"{label} must be a lower-case SHA-256 value")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CorporateActionEvidenceError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CorporateActionEvidenceError(f"{label} is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise CorporateActionEvidenceError(f"{label} must be UTC")
    return parsed


def _date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise CorporateActionEvidenceError(f"{label} date is required")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CorporateActionEvidenceError(f"{label} date is invalid") from exc
    if parsed.isoformat() != value:
        raise CorporateActionEvidenceError(f"{label} date is not canonical")
    return parsed


def _causal_decision_cutoff(available_through: str) -> tuple[date, datetime, dict[str, str]]:
    cutoff_date = _date(available_through, "projection cutoff")
    local_cutoff = datetime.combine(
        cutoff_date,
        _XSHG_SESSION_CLOSE,
        tzinfo=ZoneInfo(_XSHG_TIMEZONE),
    )
    cutoff_utc = local_cutoff.astimezone(timezone.utc)
    return (
        cutoff_date,
        cutoff_utc,
        {
            "market": "XSHG",
            "signal_time": "SESSION_CLOSE",
            "timezone": _XSHG_TIMEZONE,
            "local_time": "15:00:00",
            "timestamp_utc": cutoff_utc.isoformat().replace("+00:00", "Z"),
        },
    )


def _validate_bocom_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CorporateActionEvidenceError(f"{label} source URL is invalid")
    parsed = urlsplit(value)
    if (
        (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.path,
        )
        != _BOCOM_URL
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise CorporateActionEvidenceError(f"{label} source URL is outside the BOCOM contract")
    if parsed.port not in (None, 443):
        raise CorporateActionEvidenceError(f"{label} source URL has an invalid port")
    return value


@dataclass(frozen=True)
class CorporateActionEvidence:
    document: dict[str, Any]
    artifact_bytes: Mapping[str, bytes]
    digest: str
    publishable: bool
    quarantined_revision_ids: tuple[str, ...]

    def json_bytes(self) -> bytes:
        return canonical_json_bytes(self.document) + b"\n"


def _validate_request(item: Any) -> str:
    item = _require_fields(item, {"request_id", "payload"}, "request")
    payload = _require_fields(
        item["payload"],
        {"schema_version", "method", "url", "query", "headers"},
        "request payload",
    )
    if payload["schema_version"] != 1 or payload["method"] != "GET":
        raise CorporateActionEvidenceError("request method or schema is invalid")
    if _validate_bocom_url(payload["url"], "request") != _BOCOM_REQUEST_URL:
        raise CorporateActionEvidenceError("request URL is outside the accepted source contract")
    if not isinstance(payload["query"], dict) or payload["query"] != {
        "fileId": "94697c067ebe4427a4165910712df44d"
    }:
        raise CorporateActionEvidenceError("request query is outside the accepted source contract")
    if payload["headers"] != {"accept": "application/pdf"}:
        raise CorporateActionEvidenceError(
            "request headers are outside the accepted source contract"
        )
    request_id = _require_sha256(item["request_id"], "request ID")
    if identity_digest(REQUEST_DOMAIN, payload) != request_id:
        raise CorporateActionEvidenceError("request identity mismatch")
    return request_id


def _validate_artifact(item: Any, artifact_bytes: Mapping[str, bytes]) -> str:
    item = _require_fields(
        item,
        {"artifact_id", "body_sha256", "byte_length", "media_type", "path", "source_url"},
        "artifact",
    )
    artifact_id = _require_sha256(item["artifact_id"], "artifact ID")
    body = artifact_bytes.get(artifact_id)
    if not isinstance(body, bytes):
        raise CorporateActionEvidenceError("exact artifact bytes are missing")
    if type(item["byte_length"]) is not int or item["byte_length"] != len(body):
        raise CorporateActionEvidenceError("artifact byte length mismatch")
    if item["body_sha256"] != hashlib.sha256(body).hexdigest():
        raise CorporateActionEvidenceError("artifact body digest mismatch")
    if identity_digest(ARTIFACT_DOMAIN, body) != artifact_id:
        raise CorporateActionEvidenceError("artifact identity mismatch")
    if item["media_type"] != "application/pdf" or not body.startswith(b"%PDF-"):
        raise CorporateActionEvidenceError("artifact media type mismatch")
    expected_path = f"corporate-action-{artifact_id}.bin"
    if item["path"] != expected_path or Path(expected_path).name != expected_path:
        raise CorporateActionEvidenceError("artifact path is invalid")
    if _validate_bocom_url(item["source_url"], "artifact") != _BOCOM_ARTIFACT_URL:
        raise CorporateActionEvidenceError("artifact source URL does not match the accepted source")
    return artifact_id


def _validate_retrieval(
    item: Any, request_ids: set[str], artifacts: dict[str, dict[str, Any]]
) -> str:
    item = _require_fields(item, {"retrieval_id", "payload"}, "retrieval")
    payload = _require_fields(
        item["payload"],
        {
            "schema_version",
            "request_id",
            "attempt",
            "started_at",
            "completed_at",
            "redirects",
            "final_url",
            "final_status",
            "media_type",
            "artifact_id",
        },
        "retrieval payload",
    )
    if payload["schema_version"] != 1 or payload["request_id"] not in request_ids:
        raise CorporateActionEvidenceError("retrieval request identity mismatch")
    if payload["attempt"] != 1 or payload["redirects"] != [] or payload["final_status"] != 200:
        raise CorporateActionEvidenceError("retrieval response facts are invalid")
    started = _timestamp(payload["started_at"], "retrieval start")
    completed = _timestamp(payload["completed_at"], "retrieval completion")
    if completed < started:
        raise CorporateActionEvidenceError("retrieval timestamps are invalid")
    artifact = artifacts.get(payload["artifact_id"])
    if artifact is None:
        raise CorporateActionEvidenceError("retrieval artifact identity is dangling")
    if payload["media_type"] != artifact["media_type"]:
        raise CorporateActionEvidenceError("retrieval media type mismatch")
    if payload["final_url"] != artifact["source_url"]:
        raise CorporateActionEvidenceError("retrieval source URL mismatch")
    if _validate_bocom_url(payload["final_url"], "retrieval") != _BOCOM_ARTIFACT_URL:
        raise CorporateActionEvidenceError(
            "retrieval source URL does not match the accepted source"
        )
    retrieval_id = _require_sha256(item["retrieval_id"], "retrieval ID")
    if identity_digest(RETRIEVAL_DOMAIN, payload) != retrieval_id:
        raise CorporateActionEvidenceError("retrieval identity mismatch")
    return retrieval_id


def _validate_revision(
    item: Any,
    artifact_ids: set[str],
) -> tuple[str, tuple[Any, ...]]:
    item = _require_fields(
        item,
        {
            "event_revision_id",
            "event_series",
            "payload",
            "available_at",
            "use_role",
            "source_url",
            "acceptance_state",
            "normalization_digest",
            "findings",
        },
        "event revision",
    )
    series = _require_fields(
        item["event_series"],
        {"schema_version", "instrument", "market", "event_class", "root_notice_id"},
        "event series",
    )
    if (
        series
        != {
            "schema_version": 1,
            "instrument": "601328.SS",
            "market": "XSHG",
            "event_class": "CASH_DIVIDEND",
            "root_notice_id": series["root_notice_id"],
        }
        or not isinstance(series["root_notice_id"], str)
        or not series["root_notice_id"]
    ):
        raise CorporateActionEvidenceError("event series is outside BOCOM/XSHG v1 scope")
    payload = _require_fields(
        item["payload"],
        {
            "schema_version",
            "logical_event_id",
            "instrument",
            "market",
            "event_class",
            "contributing_notice_ids",
            "record_date",
            "ex_date",
            "pay_date",
            "gross_cash_per_share",
            "normalized_currency",
            "source_artifact_ids",
            "parser_version",
            "correction_links",
        },
        "event revision payload",
    )
    logical_event_id = identity_digest(SERIES_DOMAIN, series)
    if payload["logical_event_id"] != logical_event_id:
        raise CorporateActionEvidenceError("event series identity mismatch")
    if (
        payload["schema_version"] != 1
        or payload["instrument"] != series["instrument"]
        or payload["market"] != series["market"]
        or payload["event_class"] != series["event_class"]
        or payload["normalized_currency"] != "CNY"
    ):
        raise CorporateActionEvidenceError("event revision source scope mismatch")
    notices = payload["contributing_notice_ids"]
    links = payload["correction_links"]
    if (
        not isinstance(notices, list)
        or not notices
        or not all(isinstance(value, str) and value for value in notices)
        or len(set(notices)) != len(notices)
        or not isinstance(links, list)
        or not all(isinstance(value, str) and value for value in links)
        or len(set(links)) != len(links)
    ):
        raise CorporateActionEvidenceError("event revision notice or correction links are invalid")
    sources = payload["source_artifact_ids"]
    if (
        not isinstance(sources, list)
        or not sources
        or len(set(sources)) != len(sources)
        or not set(sources).issubset(artifact_ids)
    ):
        raise CorporateActionEvidenceError("event revision source artifact identity mismatch")
    record_date = _date(payload["record_date"], "record")
    ex_date = _date(payload["ex_date"], "ex")
    pay_date = _date(payload["pay_date"], "pay")
    if not record_date < ex_date <= pay_date:
        raise CorporateActionEvidenceError("event date order is invalid")
    amount = payload["gross_cash_per_share"]
    if not isinstance(amount, str) or _DECIMAL.fullmatch(amount) is None:
        raise CorporateActionEvidenceError("gross cash decimal is not canonical")
    if not isinstance(payload["parser_version"], str) or not payload["parser_version"]:
        raise CorporateActionEvidenceError("parser identity is invalid")
    revision_id = _require_sha256(item["event_revision_id"], "event revision ID")
    if identity_digest(REVISION_DOMAIN, payload) != revision_id:
        raise CorporateActionEvidenceError("event revision identity mismatch")
    if item["normalization_digest"] != revision_id:
        raise CorporateActionEvidenceError("normalization digest mismatch")
    _timestamp(item["available_at"], "event availability")
    if item["use_role"] not in USE_ROLES:
        raise CorporateActionEvidenceError("event use role is invalid")
    if _validate_bocom_url(item["source_url"], "event") != _BOCOM_ARTIFACT_URL:
        raise CorporateActionEvidenceError("event source URL does not match the accepted source")
    if item["acceptance_state"] != "ACCEPTED":
        raise CorporateActionEvidenceError("event acceptance state is invalid")
    if not isinstance(item["findings"], list) or not all(
        isinstance(value, str) for value in item["findings"]
    ):
        raise CorporateActionEvidenceError("event findings are invalid")
    terms = (
        payload["record_date"],
        payload["ex_date"],
        payload["pay_date"],
        payload["gross_cash_per_share"],
        payload["normalized_currency"],
    )
    return revision_id, terms


def _correction_and_conflict_findings(
    revisions: list[dict[str, Any]], terms_by_id: dict[str, tuple[Any, ...]]
) -> tuple[list[str], set[str]]:
    notice_to_revisions: dict[str, set[str]] = {}
    edges: dict[str, set[str]] = {}
    revisions_by_id = {revision["event_revision_id"]: revision for revision in revisions}
    for revision in revisions:
        revision_id = revision["event_revision_id"]
        notices = revision["payload"]["contributing_notice_ids"]
        for notice in notices:
            notice_to_revisions.setdefault(notice, set()).add(revision_id)
        edges[revision_id] = set()
    for revision in revisions:
        revision_id = revision["event_revision_id"]
        for notice in revision["payload"]["correction_links"]:
            targets = notice_to_revisions.get(notice)
            if not targets:
                raise CorporateActionEvidenceError("dangling correction link")
            if len(targets) != 1:
                raise CorporateActionEvidenceError("ambiguous correction link")
            target = next(iter(targets))
            if target == revision_id:
                raise CorporateActionEvidenceError("cyclic correction link")
            target_revision = revisions_by_id[target]
            if target_revision["event_series"] != revision["event_series"]:
                raise CorporateActionEvidenceError("correction link crosses an Event Series")
            if _timestamp(revision["available_at"], "correction availability") < _timestamp(
                target_revision["available_at"], "corrected availability"
            ):
                raise CorporateActionEvidenceError(
                    "correction availability precedes corrected evidence"
                )
            edges[revision_id].add(target)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(revision_id: str) -> None:
        if revision_id in visiting:
            raise CorporateActionEvidenceError("cyclic correction chain")
        if revision_id in visited:
            return
        visiting.add(revision_id)
        for parent in edges[revision_id]:
            visit(parent)
        visiting.remove(revision_id)
        visited.add(revision_id)

    for revision_id in edges:
        visit(revision_id)

    ancestors: dict[str, set[str]] = {}

    def reachable_parents(revision_id: str) -> set[str]:
        if revision_id not in ancestors:
            ancestors[revision_id] = set(edges[revision_id])
            for parent in edges[revision_id]:
                ancestors[revision_id].update(reachable_parents(parent))
        return ancestors[revision_id]

    for revision in revisions:
        revision_id = revision["event_revision_id"]
        root_notice_id = revision["event_series"]["root_notice_id"]
        reachable_notices = {
            notice
            for ancestor_id in reachable_parents(revision_id)
            for notice in revisions_by_id[ancestor_id]["payload"]["contributing_notice_ids"]
        }
        if (
            root_notice_id not in revision["payload"]["contributing_notice_ids"]
            and root_notice_id not in reachable_notices
        ):
            raise CorporateActionEvidenceError(
                "correction chain does not resolve to its Event Series root"
            )

    quarantined: set[str] = set()
    by_series: dict[str, list[str]] = {}
    for revision in revisions:
        logical = revision["payload"]["logical_event_id"]
        by_series.setdefault(logical, []).append(revision["event_revision_id"])
    for revision_ids in by_series.values():
        for index, left in enumerate(revision_ids):
            for right in revision_ids[index + 1 :]:
                explicitly_related = left in reachable_parents(right) or right in reachable_parents(
                    left
                )
                if not explicitly_related and terms_by_id[left] != terms_by_id[right]:
                    quarantined.update((left, right))
    findings = ["SAME_RANK_OFFICIAL_CONFLICT"] if quarantined else []
    return findings, quarantined


def admit_corporate_action_evidence(
    document: Mapping[str, Any], artifact_bytes: Mapping[str, bytes]
) -> CorporateActionEvidence:
    """Validate and close one immutable BOCOM/XSHG evidence package."""

    value = copy.deepcopy(dict(document))
    allowed_fields = {
        "schema_version",
        "collector_version",
        "source_contract_version",
        "complete_enumeration_contract",
        "complete_contract_id",
        "requests",
        "retrievals",
        "artifacts",
        "revisions",
        "coverage",
        "findings",
        "total_return_claim",
        "projection",
    }
    compatible_fields = allowed_fields - {"projection", "complete_contract_id"}
    if set(value) not in (
        compatible_fields,
        compatible_fields | {"projection"},
        allowed_fields - {"projection"},
        allowed_fields,
    ):
        raise CorporateActionEvidenceError("corporate-action evidence fields are invalid")
    if value["schema_version"] != 1:
        raise CorporateActionEvidenceError("unsupported corporate-action evidence schema")
    source_contract = value["source_contract_version"]
    if source_contract == "bocom-xshg-dividend@1":
        if value["collector_version"] != "accepted-audit-import@1":
            raise CorporateActionEvidenceError(
                "collector_version is outside the accepted source contract"
            )
    elif source_contract == "synthetic-complete-enumeration@1":
        if value["collector_version"] != "synthetic-complete-fixture@1":
            raise CorporateActionEvidenceError("synthetic complete collector identity is invalid")
    else:
        raise CorporateActionEvidenceError(
            "source_contract_version is outside the accepted source contract"
        )
    if type(value["complete_enumeration_contract"]) is not bool:
        raise CorporateActionEvidenceError("complete-enumeration contract flag is invalid")
    complete_contract_id = value.get("complete_contract_id")
    if source_contract == "bocom-xshg-dividend@1" and value["complete_enumeration_contract"]:
        raise CorporateActionEvidenceError(
            "BOCOM/XSHG v1 has no admitted complete-enumeration contract"
        )
    expected_complete_contract_id = identity_digest(
        "quant-platform/complete-enumeration-contract/v1", SYNTHETIC_COMPLETE_CONTRACT
    )
    if source_contract == "synthetic-complete-enumeration@1" and (
        value["complete_enumeration_contract"] is not True
        or complete_contract_id != expected_complete_contract_id
    ):
        raise CorporateActionEvidenceError("synthetic complete-enumeration contract is invalid")
    if source_contract == "bocom-xshg-dividend@1" and complete_contract_id is not None:
        raise CorporateActionEvidenceError("BOCOM/XSHG v1 cannot name a complete contract")
    if not isinstance(value["findings"], list) or not all(
        isinstance(item, str) for item in value["findings"]
    ):
        raise CorporateActionEvidenceError("evidence findings are invalid")
    if value["total_return_claim"] not in {"KNOWN_EVENT_CORRECTED_PARTIAL", "FORBIDDEN"}:
        raise CorporateActionEvidenceError("evidence total-return claim is invalid")
    for field in ("requests", "retrievals", "artifacts", "revisions"):
        if not isinstance(value[field], list):
            raise CorporateActionEvidenceError(f"{field} must be an array")

    request_ids = [_validate_request(item) for item in value["requests"]]
    if len(request_ids) != len(set(request_ids)):
        raise CorporateActionEvidenceError("duplicate request identity")
    artifacts_by_id = {item.get("artifact_id"): item for item in value["artifacts"]}
    if len(artifacts_by_id) != len(value["artifacts"]):
        raise CorporateActionEvidenceError("duplicate artifact identity")
    artifact_ids = [_validate_artifact(item, artifact_bytes) for item in value["artifacts"]]
    if set(artifact_bytes) != set(artifact_ids):
        raise CorporateActionEvidenceError("artifact byte set does not match descriptor")
    retrieval_ids = [
        _validate_retrieval(item, set(request_ids), artifacts_by_id) for item in value["retrievals"]
    ]
    if len(retrieval_ids) != len(set(retrieval_ids)):
        raise CorporateActionEvidenceError("duplicate retrieval identity")
    if {item["payload"]["request_id"] for item in value["retrievals"]} != set(request_ids):
        raise CorporateActionEvidenceError("request set does not match retrieval evidence")
    if {item["payload"]["artifact_id"] for item in value["retrievals"]} != set(artifact_ids):
        raise CorporateActionEvidenceError("artifact set does not match retrieval evidence")

    revision_ids: list[str] = []
    terms_by_id: dict[str, tuple[Any, ...]] = {}
    for revision in value["revisions"]:
        revision_id, terms = _validate_revision(revision, set(artifact_ids))
        if revision_id in terms_by_id:
            raise CorporateActionEvidenceError("duplicate revision identity")
        revision_ids.append(revision_id)
        terms_by_id[revision_id] = terms
        if revision["payload"]["parser_version"] != "bocom-dividend-pdf@1":
            raise CorporateActionEvidenceError("parser identity is outside the accepted contract")
        available_at = _timestamp(revision["available_at"], "event availability")
        source_retrievals = [
            retrieval
            for retrieval in value["retrievals"]
            if retrieval["payload"]["artifact_id"] in revision["payload"]["source_artifact_ids"]
        ]
        if not source_retrievals or available_at < max(
            _timestamp(item["payload"]["completed_at"], "retrieval completion")
            for item in source_retrievals
        ):
            raise CorporateActionEvidenceError(
                "event availability precedes its source retrieval evidence"
            )
    conflict_findings, quarantined = _correction_and_conflict_findings(
        value["revisions"], terms_by_id
    )
    for finding in conflict_findings:
        if finding not in value["findings"]:
            value["findings"].append(finding)

    coverage = _require_fields(value["coverage"], {"coverage_id", "payload"}, "coverage")
    coverage_payload = _require_fields(
        coverage["payload"],
        {
            "schema_version",
            "instrument",
            "market",
            "interval_start",
            "interval_end",
            "checked_as_of",
            "event_revision_ids",
            "query_retrieval_ids",
            "coverage_state",
            "limitations",
        },
        "coverage payload",
    )
    if (
        coverage_payload["schema_version"] != 1
        or coverage_payload["instrument"] != "601328.SS"
        or coverage_payload["market"] != "XSHG"
    ):
        raise CorporateActionEvidenceError("coverage source scope mismatch")
    if _date(coverage_payload["interval_start"], "coverage start") > _date(
        coverage_payload["interval_end"], "coverage end"
    ):
        raise CorporateActionEvidenceError("coverage interval is invalid")
    _timestamp(coverage_payload["checked_as_of"], "coverage checked_as_of")
    if coverage_payload["event_revision_ids"] != revision_ids:
        raise CorporateActionEvidenceError("coverage revision identities do not match")
    query_retrieval_ids = coverage_payload["query_retrieval_ids"]
    if (
        not isinstance(query_retrieval_ids, list)
        or not all(isinstance(item, str) for item in query_retrieval_ids)
        or len(set(query_retrieval_ids)) != len(query_retrieval_ids)
        or not set(query_retrieval_ids).issubset(set(retrieval_ids))
    ):
        raise CorporateActionEvidenceError("coverage query retrieval identity is dangling")
    state = coverage_payload["coverage_state"]
    if state not in COVERAGE_STATES:
        raise CorporateActionEvidenceError("coverage state is invalid")
    if not isinstance(coverage_payload["limitations"], list) or not all(
        isinstance(item, str) for item in coverage_payload["limitations"]
    ):
        raise CorporateActionEvidenceError("coverage limitations are invalid")
    if state == "VERIFIED_EVENTS" and not revision_ids:
        raise CorporateActionEvidenceError("VERIFIED_EVENTS requires an accepted revision")
    if state in {"VERIFIED_NO_ACTION", "VERIFIED_COMPLETE_INTERVAL"} and not value[
        "complete_enumeration_contract"
    ]:
        raise CorporateActionEvidenceError(
            "complete coverage requires an admitted complete-enumeration contract"
        )
    if state == "VERIFIED_NO_ACTION" and revision_ids:
        raise CorporateActionEvidenceError("VERIFIED_NO_ACTION cannot contain events")
    if state == "VERIFIED_COMPLETE_INTERVAL" and not revision_ids:
        raise CorporateActionEvidenceError("VERIFIED_COMPLETE_INTERVAL requires terminal events")
    expected_claim = "KNOWN_EVENT_CORRECTED_PARTIAL" if state == "VERIFIED_EVENTS" else "FORBIDDEN"
    if value["total_return_claim"] != expected_claim:
        raise CorporateActionEvidenceError(
            "evidence total-return claim does not match its bounded coverage state"
        )
    if state == "VERIFIED_EVENTS" and (
        value["complete_enumeration_contract"]
        or "NO_COMPLETE_AUTHORITATIVE_ENUMERATION" not in coverage_payload["limitations"]
    ):
        raise CorporateActionEvidenceError(
            "VERIFIED_EVENTS must retain the incomplete-enumeration limitation"
        )
    interval_start = _date(coverage_payload["interval_start"], "coverage start")
    interval_end = _date(coverage_payload["interval_end"], "coverage end")
    if any(
        not interval_start <= _date(item["payload"]["record_date"], "record") <= interval_end
        for item in value["revisions"]
    ):
        raise CorporateActionEvidenceError("event record date is outside the coverage interval")
    checked_as_of = _timestamp(coverage_payload["checked_as_of"], "coverage checked_as_of")
    if any(
        _timestamp(item["available_at"], "event availability") > checked_as_of
        for item in value["revisions"]
    ):
        raise CorporateActionEvidenceError("coverage precedes included event availability")
    coverage_id = _require_sha256(coverage["coverage_id"], "coverage ID")
    if identity_digest(COVERAGE_DOMAIN, coverage_payload) != coverage_id:
        raise CorporateActionEvidenceError("coverage identity mismatch")

    if "projection" in value:
        projection = _require_fields(
            value["projection"],
            {
                "parent_evidence_sha256",
                "available_through",
                "decision_cutoff",
                "excluded_revisions",
            },
            "evidence projection",
        )
        _require_sha256(projection["parent_evidence_sha256"], "parent evidence digest")
        _, projection_cutoff, expected_cutoff = _causal_decision_cutoff(
            projection["available_through"]
        )
        if projection["decision_cutoff"] != expected_cutoff:
            raise CorporateActionEvidenceError("causal projection decision cutoff is invalid")
        excluded_revisions = projection["excluded_revisions"]
        if not isinstance(excluded_revisions, list) or any(
            not isinstance(item, dict)
            or set(item) != {"event_revision_id", "reason"}
            or not isinstance(item["event_revision_id"], str)
            or _SHA256.fullmatch(item["event_revision_id"]) is None
            or not isinstance(item["reason"], str)
            or item["reason"]
            not in {"ACCOUNTING_OUTCOME_NOT_CAUSAL", "AVAILABLE_AFTER_DECISION_CUTOFF"}
            for item in excluded_revisions
        ):
            raise CorporateActionEvidenceError("causal projection exclusions are invalid")
        if len({item["event_revision_id"] for item in excluded_revisions}) != len(
            excluded_revisions
        ):
            raise CorporateActionEvidenceError("causal projection exclusions are duplicated")
        if {item["event_revision_id"] for item in excluded_revisions}.intersection(revision_ids):
            raise CorporateActionEvidenceError(
                "causal projection revision is both included and excluded"
            )
        if any(item["use_role"] != "CAUSAL_FEATURE" for item in value["revisions"]):
            raise CorporateActionEvidenceError(
                "causal projection contains retrospective accounting evidence"
            )
        if any(
            _timestamp(item["available_at"], "event availability") > projection_cutoff
            for item in value["revisions"]
        ):
            raise CorporateActionEvidenceError("causal projection contains future evidence")

    digest = identity_digest(EVIDENCE_DOMAIN, value)
    return CorporateActionEvidence(
        document=value,
        artifact_bytes=MappingProxyType(dict(artifact_bytes)),
        digest=digest,
        publishable=not quarantined,
        quarantined_revision_ids=tuple(sorted(quarantined)),
    )


def project_corporate_action_evidence(
    evidence: CorporateActionEvidence, available_through: str
) -> CorporateActionEvidence:
    """Project causal evidence available by the XSHG session-close decision cutoff."""

    _, cutoff, decision_cutoff = _causal_decision_cutoff(available_through)
    document = copy.deepcopy(evidence.document)
    document.pop("projection", None)
    selected = [
        revision
        for revision in document["revisions"]
        if revision["use_role"] == "CAUSAL_FEATURE"
        and _timestamp(revision["available_at"], "event availability") <= cutoff
    ]
    excluded_revisions = [
        {
            "event_revision_id": revision["event_revision_id"],
            "reason": (
                "ACCOUNTING_OUTCOME_NOT_CAUSAL"
                if revision["use_role"] == "ACCOUNTING_OUTCOME"
                else "AVAILABLE_AFTER_DECISION_CUTOFF"
            ),
        }
        for revision in document["revisions"]
        if revision not in selected
    ]
    selected_ids = [revision["event_revision_id"] for revision in selected]
    selected_artifact_ids = {
        artifact_id
        for revision in selected
        for artifact_id in revision["payload"]["source_artifact_ids"]
    }
    selected_artifacts = [
        artifact
        for artifact in document["artifacts"]
        if artifact["artifact_id"] in selected_artifact_ids
    ]
    selected_retrievals = [
        retrieval
        for retrieval in document["retrievals"]
        if retrieval["payload"]["artifact_id"] in selected_artifact_ids
    ]
    selected_request_ids = {retrieval["payload"]["request_id"] for retrieval in selected_retrievals}
    document["requests"] = [
        request for request in document["requests"] if request["request_id"] in selected_request_ids
    ]
    document["retrievals"] = selected_retrievals
    document["artifacts"] = selected_artifacts
    document["revisions"] = selected
    coverage_payload = document["coverage"]["payload"]
    original_revision_ids = coverage_payload["event_revision_ids"]
    coverage_payload["event_revision_ids"] = selected_ids
    coverage_payload["query_retrieval_ids"] = [
        retrieval_id
        for retrieval_id in coverage_payload["query_retrieval_ids"]
        if retrieval_id in {item["retrieval_id"] for item in selected_retrievals}
    ]
    checked = _timestamp(coverage_payload["checked_as_of"], "coverage checked_as_of")
    if checked > cutoff:
        coverage_payload["checked_as_of"] = decision_cutoff["timestamp_utc"]
    if selected_ids != original_revision_ids:
        coverage_payload["coverage_state"] = "UNKNOWN_MISSING"
        document["total_return_claim"] = "FORBIDDEN"
        limitations = list(coverage_payload["limitations"])
        if "CAUSAL_CUTOFF_EXCLUDES_ACTION_EVIDENCE" not in limitations:
            limitations.append("CAUSAL_CUTOFF_EXCLUDES_ACTION_EVIDENCE")
        exclusion_limitations = {
            "ACCOUNTING_OUTCOME_NOT_CAUSAL": "RETROSPECTIVE_ACCOUNTING_OUTCOME_EXCLUDED",
            "AVAILABLE_AFTER_DECISION_CUTOFF": "EVIDENCE_AVAILABLE_AFTER_DECISION_CUTOFF",
        }
        for exclusion in excluded_revisions:
            limitation = exclusion_limitations[exclusion["reason"]]
            if limitation not in limitations:
                limitations.append(limitation)
        coverage_payload["limitations"] = limitations
    document["coverage"]["coverage_id"] = identity_digest(COVERAGE_DOMAIN, coverage_payload)
    document["projection"] = {
        "parent_evidence_sha256": evidence.digest,
        "available_through": available_through,
        "decision_cutoff": decision_cutoff,
        "excluded_revisions": excluded_revisions,
    }
    selected_bytes = {
        artifact_id: evidence.artifact_bytes[artifact_id] for artifact_id in selected_artifact_ids
    }
    return admit_corporate_action_evidence(document, selected_bytes)


def accounting_cash_dividends(evidence: CorporateActionEvidence) -> tuple[CashDividend, ...]:
    """Project one accepted terminal revision per Event Series for account settlement."""

    document = evidence.document
    coverage = document["coverage"]["payload"]
    if not evidence.publishable:
        raise CorporateActionEvidenceError("quarantined action evidence cannot be accounted")
    if (
        coverage["coverage_state"]
        not in {"VERIFIED_EVENTS", "VERIFIED_NO_ACTION", "VERIFIED_COMPLETE_INTERVAL"}
        or document["total_return_claim"]
        not in {"KNOWN_EVENT_CORRECTED_PARTIAL", "FORBIDDEN"}
    ):
        raise CorporateActionEvidenceError(
            "accounting requires admitted event coverage and a collector source fact"
        )
    revisions = document["revisions"]
    notices_to_revision = {
        notice: revision["event_revision_id"]
        for revision in revisions
        for notice in revision["payload"]["contributing_notice_ids"]
    }
    corrected_ids = {
        notices_to_revision[notice]
        for revision in revisions
        for notice in revision["payload"]["correction_links"]
    }
    terminal = [
        revision for revision in revisions if revision["event_revision_id"] not in corrected_ids
    ]
    by_series: dict[str, dict[str, Any]] = {}
    for revision in terminal:
        logical_id = revision["payload"]["logical_event_id"]
        if logical_id in by_series:
            raise CorporateActionEvidenceError("Event Series has multiple terminal revisions")
        by_series[logical_id] = revision
    actions = tuple(
        CashDividend(
            event_revision_id=revision["event_revision_id"],
            record_date=_date(revision["payload"]["record_date"], "record"),
            ex_date=_date(revision["payload"]["ex_date"], "ex"),
            pay_date=_date(revision["payload"]["pay_date"], "pay"),
            gross_cash_per_share=Decimal(revision["payload"]["gross_cash_per_share"]),
        )
        for revision in by_series.values()
    )
    return tuple(sorted(actions, key=lambda item: (item.record_date, item.event_revision_id)))
