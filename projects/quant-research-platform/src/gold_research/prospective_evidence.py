from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
HEX_DIGITS = frozenset("0123456789abcdef")
TERMINAL_DATE_STATES = {
    "EXPECTED_ACCEPTED",
    "OFFICIAL_FULL_CLOSURE",
    "PROTOCOL_BREACH",
}
SESSION_STATUSES = {
    "NON_SESSION",
    "EXPECTED_PENDING",
    "EXPECTED_ACCEPTED",
    "OFFICIAL_FULL_CLOSURE",
    "PROTOCOL_BREACH",
}
EVENT_TYPES = {
    "DATE_DECLARED",
    "CALENDAR_RECLASSIFIED",
    "FETCH_ATTEMPT",
    "SILENT_MISS",
    "CANONICAL_ROW_ACCEPTED",
    "IDENTICAL_DUPLICATE_OBSERVED",
    "SOURCE_REVISION_OBSERVED",
    "DEADLINE_EXPIRED",
    "PROTOCOL_BREACH_RECORDED",
    "OPERATOR_ACCESS",
    "CORRECTION",
    "EXTERNAL_PRODUCTION_CHANGE",
    "FINALIZED",
}
ATTEMPT_OUTCOMES = {
    "VALID_TARGET_ROW",
    "TRANSPORT_ERROR",
    "HTTP_ERROR",
    "DECODE_ERROR",
    "SCHEMA_ERROR",
    "TARGET_DATE_ABSENT",
    "DUPLICATE_TARGET_DATE",
    "INVALID_OHLC",
}
ACCEPTED_REVISION_OUTCOMES = {
    "VALID_TARGET_ROW",
    "HTTP_ERROR",
    "DECODE_ERROR",
    "SCHEMA_ERROR",
    "TARGET_DATE_ABSENT",
    "INVALID_OHLC",
}
HISTORY_MAP_OUTCOMES = {
    "VALID_TARGET_ROW",
    "TARGET_DATE_ABSENT",
    "DUPLICATE_TARGET_DATE",
    "INVALID_OHLC",
}
SOURCE_REVISION_SCOPES = {
    "INITIALIZATION_DATA",
    "ACCEPTED_EVALUATION_DATA",
    "NON_EVALUATION_EVIDENCE",
}
CORRECTION_SCOPES = {
    "CLERICAL_METADATA",
    "CALENDAR_DECISION",
    "INITIALIZATION_DATA",
    "EVALUATION_MARKET_DATA",
    "MODEL_OR_METHOD",
}
POST_INVALIDATION_PROVENANCE_EVENTS = {
    "FETCH_ATTEMPT",
    "SILENT_MISS",
    "IDENTICAL_DUPLICATE_OBSERVED",
    "SOURCE_REVISION_OBSERVED",
    "OPERATOR_ACCESS",
    "CORRECTION",
    "EXTERNAL_PRODUCTION_CHANGE",
}
GENERATED_FIELDS = {
    "sequence",
    "event_at_utc",
    "event_at_asia_shanghai",
    "prior_state",
    "next_state",
    "expected_ordinal",
    "previous_chain_sha256",
    "record_sha256",
    "chain_sha256",
    "execution_seal_deadline",
    "recovery_deadline",
    "calendar_status",
    "calendar_evidence_sha256",
    "open_boundary_asia_shanghai",
    "open_boundary_utc",
    "breach_civil_date",
    "terminal_coverage_date",
    "canonical_raw_sha256",
    "capture_at",
}
DERIVED_RECORD_FIELDS = {
    "prior_state",
    "next_state",
    "expected_ordinal",
    "execution_seal_deadline",
    "recovery_deadline",
    "calendar_status",
    "calendar_evidence_sha256",
    "open_boundary_asia_shanghai",
    "open_boundary_utc",
    "breach_civil_date",
    "terminal_coverage_date",
    "linked_attempt_sequence",
    "canonical_raw_sha256",
    "capture_at",
}
AUTHORITATIVE_FACT_FIELDS = {
    "evidence_sha256",
    "sources_complete",
    "full_closure",
    "exceptional_opening",
    "exceptional_open_time",
    "no_prior_night",
    "prior_night_cancelled",
    "remaining_open_time",
    "conflicting",
    "ambiguous",
}
INITIALIZATION_SEAL_FIELDS = {
    "sealed_at",
    "pre_s1_replay_sha256",
    "initial_target_sha256",
    "intended_s1_position_sha256",
    "quantity_inputs_sha256",
}
FETCH_ATTEMPT_FIELDS = {
    "event_type",
    "candidate_date",
    "event_at",
    "attempt_outcome",
    "request_url",
    "request_at",
    "request_at_asia_shanghai",
    "response_at",
    "response_at_asia_shanghai",
    "collector_sha256",
    "parser_sha256",
    "build_sha256",
    "runtime_sha256",
    "http_status",
    "response_headers",
    "response_headers_sha256",
    "response_byte_length",
    "raw_byte_sha256",
    "parsed_row_sha256",
    "history_row_sha256s",
    "error_details",
    "evidence_sha256",
    "reason",
}
CANONICAL_ACCEPTANCE_FIELDS = {
    "event_type",
    "candidate_date",
    "event_at",
    "target_sealed_at",
    "canonical_row_sha256",
    "parser_sha256",
    "build_sha256",
    "evidence_sha256",
    "reason",
}
ACCEPTED_ATTEMPT_LINK_FIELDS = {
    "event_type",
    "candidate_date",
    "event_at",
    "linked_attempt_sequence",
    "baseline_raw_sha256",
    "canonical_row_sha256",
    "observed_raw_sha256",
    "observed_row_sha256",
    "observed_outcome",
    "evidence_sha256",
    "reason",
}
ACCEPTED_REVISION_LINK_FIELDS = ACCEPTED_ATTEMPT_LINK_FIELDS | {
    "revision_scope",
    "touches_evaluation_data",
}
CORRECTION_FIELDS = {
    "event_type",
    "candidate_date",
    "event_at",
    "superseded_sequence",
    "superseded_record_sha256",
    "old_value_sha256",
    "new_observation_sha256",
    "source_sha256s",
    "correction_scope",
    "decision_surface_before_sha256",
    "decision_surface_after_sha256",
    "issuer",
    "reason",
    "evidence_sha256",
}
BASE_EVENT_FIELDS = {"event_type", "event_at", "evidence_sha256", "reason"}
DATE_DECLARED_FIELDS = BASE_EVENT_FIELDS | {"candidate_date", "initial_status"}
CALENDAR_RECLASSIFIED_FIELDS = BASE_EVENT_FIELDS | {
    "candidate_date",
    "reclassified_status",
    "authoritative_facts",
    "governed_open_boundary",
}
SILENT_MISS_FIELDS = BASE_EVENT_FIELDS | {"candidate_date", "linked_attempt_sequence"}
DEADLINE_EXPIRED_FIELDS = BASE_EVENT_FIELDS | {"candidate_date"}
PROTOCOL_BREACH_FIELDS = BASE_EVENT_FIELDS | {"candidate_date", "breach_reason"}
LINKED_PROTOCOL_BREACH_FIELDS = PROTOCOL_BREACH_FIELDS | {"linked_attempt_sequence"}
LATE_CALENDAR_BREACH_FIELDS = CALENDAR_RECLASSIFIED_FIELDS | {"breach_reason"}
OPERATOR_ACCESS_FIELDS = BASE_EVENT_FIELDS | {
    "candidate_date",
    "operator_identity",
    "access_identity_sha256",
    "files_viewed",
    "linked_diagnostic_sequence",
    "diagnostic_record_sha256",
    "diagnostic_purpose",
}
EXTERNAL_PRODUCTION_CHANGE_FIELDS = BASE_EVENT_FIELDS
FINALIZED_FIELDS = BASE_EVENT_FIELDS
UNLINKED_SOURCE_REVISION_FIELDS = BASE_EVENT_FIELDS | {
    "revision_scope",
    "touches_evaluation_data",
}
FULL_HISTORY_REVISION_FIELDS = BASE_EVENT_FIELDS | {
    "candidate_date",
    "linked_attempt_sequence",
    "comparison_findings",
    "revision_scope",
    "touches_evaluation_data",
}
LATE_ACCEPTANCE_DEADLINE_FIELDS = (
    CANONICAL_ACCEPTANCE_FIELDS | {"rejected_event_type", "breach_reason"}
)

# Each event type has a finite exact input shape. Tuples represent contract-defined
# variants produced at the same event seam (for example, a linked breach).
EVENT_FIELD_SCHEMAS: dict[str, tuple[frozenset[str], ...]] = {
    "DATE_DECLARED": (frozenset(DATE_DECLARED_FIELDS),),
    "CALENDAR_RECLASSIFIED": (frozenset(CALENDAR_RECLASSIFIED_FIELDS),),
    "FETCH_ATTEMPT": (frozenset(FETCH_ATTEMPT_FIELDS),),
    "SILENT_MISS": (frozenset(SILENT_MISS_FIELDS),),
    "CANONICAL_ROW_ACCEPTED": (
        frozenset(CANONICAL_ACCEPTANCE_FIELDS),
        frozenset(CANONICAL_ACCEPTANCE_FIELDS - {"target_sealed_at"}),
    ),
    "IDENTICAL_DUPLICATE_OBSERVED": (frozenset(ACCEPTED_ATTEMPT_LINK_FIELDS),),
    "SOURCE_REVISION_OBSERVED": (
        frozenset(ACCEPTED_REVISION_LINK_FIELDS),
        frozenset(FULL_HISTORY_REVISION_FIELDS),
        frozenset(UNLINKED_SOURCE_REVISION_FIELDS),
    ),
    "DEADLINE_EXPIRED": (
        frozenset(DEADLINE_EXPIRED_FIELDS),
        frozenset(LATE_ACCEPTANCE_DEADLINE_FIELDS),
        frozenset(LATE_ACCEPTANCE_DEADLINE_FIELDS - {"target_sealed_at"}),
    ),
    "PROTOCOL_BREACH_RECORDED": (
        frozenset(PROTOCOL_BREACH_FIELDS),
        frozenset(PROTOCOL_BREACH_FIELDS - {"breach_reason"}),
        frozenset(LINKED_PROTOCOL_BREACH_FIELDS),
        frozenset(LATE_CALENDAR_BREACH_FIELDS),
    ),
    "OPERATOR_ACCESS": (frozenset(OPERATOR_ACCESS_FIELDS),),
    "CORRECTION": (frozenset(CORRECTION_FIELDS),),
    "EXTERNAL_PRODUCTION_CHANGE": (
        frozenset(EXTERNAL_PRODUCTION_CHANGE_FIELDS),
        frozenset(EXTERNAL_PRODUCTION_CHANGE_FIELDS | {"candidate_date"}),
    ),
    "FINALIZED": (frozenset(FINALIZED_FIELDS),),
}


class ContractViolation(ValueError):
    """Raised when prospective evidence cannot satisfy the frozen contract."""


class CalendarStatus(str, Enum):
    NON_SESSION = "NON_SESSION"
    EXPECTED_SESSION = "EXPECTED_SESSION"
    OFFICIAL_FULL_CLOSURE = "OFFICIAL_FULL_CLOSURE"
    AMBIGUOUS_BLOCKED = "AMBIGUOUS_BLOCKED"


@dataclass(frozen=True)
class AuthoritativeSessionFacts:
    """Already captured calendar facts; this module never retrieves them."""

    evidence_sha256: tuple[str, ...]
    sources_complete: bool
    full_closure: bool
    exceptional_opening: bool
    exceptional_open_time: str | None
    no_prior_night: bool
    prior_night_cancelled: bool
    remaining_open_time: str | None
    conflicting: bool
    ambiguous: bool


@dataclass(frozen=True)
class SessionDecision:
    status: CalendarStatus
    open_boundary: datetime | None
    evidence_sha256: tuple[str, ...]


@dataclass(frozen=True)
class LedgerReplay:
    states: dict[str, str]
    ordinals: dict[str, int]
    deadlines: dict[str, str]
    max_ordinal: int
    terminal_class: str | None
    breach_civil_date: str | None
    terminal_coverage_date: str | None
    final_chain_sha256: str


@dataclass
class _LedgerState:
    states: dict[str, str] = field(default_factory=dict)
    ordinals: dict[str, int] = field(default_factory=dict)
    deadlines: dict[str, str] = field(default_factory=dict)
    declared_dates: list[date] = field(default_factory=list)
    expected_first_date: date | None = None
    max_ordinal: int = 0
    invalidated: bool = False
    finalized: bool = False
    calendar: dict[str, SessionDecision] = field(default_factory=dict)
    canonical_rows: dict[str, str] = field(default_factory=dict)
    canonical_raws: dict[str, str] = field(default_factory=dict)
    last_event_at: datetime | None = None
    breach_civil_date: str | None = None
    terminal_coverage_date: str | None = None
    genesis_sealed_at: datetime | None = None
    valid_attempts: dict[int, dict[str, Any]] = field(default_factory=dict)
    duplicate_attempts: dict[int, dict[str, Any]] = field(default_factory=dict)
    accepted_attempts: dict[int, dict[str, Any]] = field(default_factory=dict)
    pending_valid_attempt_sequence: int | None = None
    pending_accepted_attempt_sequence: int | None = None
    pending_accepted_attempt_event_type: str | None = None
    warmup_rows: dict[str, str] = field(default_factory=dict)
    artifact_byte_lengths: dict[str, int] = field(default_factory=dict)
    pending_history_revision_sequence: int | None = None
    pending_history_revision_findings: list[dict[str, Any]] | None = None
    sealed_records: dict[int, dict[str, Any]] = field(default_factory=dict)


def _validate_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in HEX_DIGITS for character in value)
    ):
        raise ContractViolation(f"{label} must be a lowercase SHA-256 identity")
    return value


def _reject_float(value: str) -> None:
    raise ContractViolation(f"JSON decimal {value!r} must be preserved as a canonical string")


def _reject_constant(value: str) -> None:
    raise ContractViolation(f"non-finite JSON value {value!r} is forbidden")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractViolation(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the contract's canonical logical JSON representation."""

    def reject_floats(item: Any) -> None:
        if isinstance(item, float):
            raise ContractViolation("decimal values must be preserved as canonical strings")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ContractViolation("JSON object keys must be strings")
                reject_floats(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                reject_floats(child)

    reject_floats(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractViolation("value is not finite canonical JSON") from exc


def _parse_canonical_record(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes):
        raise ContractViolation("ledger records must be immutable UTF-8 bytes")
    logical = raw[:-1] if raw.endswith(b"\n") else raw
    if not logical or logical.endswith((b"\n", b"\r", b" ", b"\t")):
        raise ContractViolation("ledger record has noncanonical trailing bytes")
    try:
        decoded = logical.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractViolation("ledger record is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ContractViolation("ledger record must be a JSON object")
    if canonical_json_bytes(value) != logical:
        raise ContractViolation("ledger record bytes are not canonical JSON")
    return value


def _parse_date(value: Any, label: str = "candidate_date") -> date:
    if not isinstance(value, str):
        raise ContractViolation(f"{label} must be an ISO civil date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ContractViolation(f"{label} must be an ISO civil date") from exc
    if parsed.isoformat() != value:
        raise ContractViolation(f"{label} must be canonical ISO format")
    return parsed


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ContractViolation(f"{label} must be an RFC 3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractViolation(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractViolation(f"{label} must include an explicit UTC offset")
    if parsed.microsecond:
        raise ContractViolation(f"{label} must identify a whole-second instant")
    return parsed


def _canonical_local_timestamp(value: datetime) -> str:
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _canonical_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_local_time(value: str | None, label: str) -> time:
    if not isinstance(value, str):
        raise ContractViolation(f"{label} requires an explicit contribution-capable interval")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ContractViolation(f"{label} must be an ISO local time") from exc
    if parsed.tzinfo is not None or parsed.microsecond:
        raise ContractViolation(f"{label} must be a whole-second local time without offset")
    return parsed


def classify_session(
    session_date: date,
    facts: AuthoritativeSessionFacts | Mapping[str, Any],
) -> SessionDecision:
    """Classify one date and derive its earliest contribution-capable boundary."""

    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise ContractViolation("session_date must be a civil date")
    facts = _validated_authoritative_facts(facts, label="classify_session")
    evidence = facts.evidence_sha256
    is_weekday = session_date.weekday() < 5
    session_only_facts = (
        facts.exceptional_opening
        or facts.exceptional_open_time is not None
        or facts.no_prior_night
        or facts.prior_night_cancelled
        or facts.remaining_open_time is not None
    )
    ambiguous = (
        not evidence
        or not facts.sources_complete
        or facts.conflicting
        or facts.ambiguous
        or (facts.full_closure and session_only_facts)
        or (is_weekday and facts.exceptional_opening)
        or (
            not is_weekday
            and (facts.no_prior_night or facts.prior_night_cancelled or facts.remaining_open_time)
        )
        or (
            facts.exceptional_opening
            and (facts.no_prior_night or facts.prior_night_cancelled or facts.remaining_open_time)
        )
        or (facts.exceptional_open_time is not None and not facts.exceptional_opening)
        or (facts.remaining_open_time is not None and not facts.prior_night_cancelled)
    )
    if ambiguous:
        return SessionDecision(CalendarStatus.AMBIGUOUS_BLOCKED, None, evidence)
    if facts.full_closure:
        return SessionDecision(CalendarStatus.OFFICIAL_FULL_CLOSURE, None, evidence)

    if not is_weekday and not facts.exceptional_opening:
        return SessionDecision(CalendarStatus.NON_SESSION, None, evidence)

    try:
        if not is_weekday:
            opening = _parse_local_time(
                facts.exceptional_open_time,
                "exceptional non-weekday opening",
            )
            boundary_date = session_date
        elif facts.no_prior_night:
            opening = time(9, 0)
            boundary_date = session_date
        elif facts.prior_night_cancelled:
            opening = (
                _parse_local_time(facts.remaining_open_time, "remaining opening interval")
                if facts.remaining_open_time is not None
                else time(9, 0)
            )
            boundary_date = session_date
        else:
            opening = time(20, 0)
            boundary_date = (
                session_date - timedelta(days=3)
                if session_date.weekday() == 0
                else session_date - timedelta(days=1)
            )
    except ContractViolation:
        return SessionDecision(CalendarStatus.AMBIGUOUS_BLOCKED, None, evidence)

    boundary = datetime.combine(boundary_date, opening, tzinfo=SHANGHAI)
    return SessionDecision(CalendarStatus.EXPECTED_SESSION, boundary, evidence)


def _normalize_evidence(event: Mapping[str, Any], available: set[str]) -> list[str]:
    evidence = event.get("evidence_sha256")
    if not isinstance(evidence, list) or not evidence:
        raise ContractViolation("every ledger event must reference immutable evidence identities")
    normalized = [_validate_sha256(value, "event evidence") for value in evidence]
    if len(set(normalized)) != len(normalized):
        raise ContractViolation("event evidence identities must be unique")
    missing = [value for value in normalized if value not in available]
    if missing:
        raise ContractViolation(f"missing referenced identity: {missing[0]}")
    return normalized


def _validated_authoritative_facts(
    value: Any,
    *,
    label: str,
) -> AuthoritativeSessionFacts:
    if type(value) is AuthoritativeSessionFacts:
        raw_values: Mapping[str, Any] = asdict(value)
    elif isinstance(value, Mapping):
        raw_values = value
    else:
        raise ContractViolation(f"{label} requires structured authoritative facts")
    unsupported = set(raw_values) - AUTHORITATIVE_FACT_FIELDS
    if unsupported:
        raise ContractViolation(f"{label} contains unsupported fact {sorted(unsupported)[0]!r}")
    missing = AUTHORITATIVE_FACT_FIELDS - set(raw_values)
    if missing:
        raise ContractViolation(
            f"{label} requires the complete canonical shape; missing {sorted(missing)[0]!r}"
        )
    evidence = raw_values["evidence_sha256"]
    if not isinstance(evidence, (list, tuple)) or not evidence:
        raise ContractViolation(f"{label} authoritative facts require evidence identities")
    normalized = dict(raw_values)
    normalized["evidence_sha256"] = tuple(
        _validate_sha256(identity, f"{label} calendar evidence") for identity in evidence
    )
    if len(set(normalized["evidence_sha256"])) != len(normalized["evidence_sha256"]):
        raise ContractViolation(f"{label} authoritative fact identities must be unique")
    for field_name in {
        "sources_complete",
        "full_closure",
        "exceptional_opening",
        "no_prior_night",
        "prior_night_cancelled",
        "conflicting",
        "ambiguous",
    }:
        if not isinstance(normalized[field_name], bool):
            raise ContractViolation(f"{label} fact {field_name!r} must be boolean")
    for field_name in {"exceptional_open_time", "remaining_open_time"}:
        if not isinstance(normalized[field_name], (str, type(None))):
            raise ContractViolation(f"{label} fact {field_name!r} must be a local time or null")
        if normalized.get(field_name) is not None:
            parsed = _parse_local_time(normalized[field_name], f"{label} fact {field_name!r}")
            if normalized[field_name] != parsed.isoformat():
                raise ContractViolation(
                    f"{label} fact {field_name!r} must be a canonical whole-second local time"
                )
    if normalized["no_prior_night"] and normalized["prior_night_cancelled"]:
        raise ContractViolation(
            f"{label} facts 'no_prior_night' and 'prior_night_cancelled' are mutually exclusive"
        )
    try:
        return AuthoritativeSessionFacts(**normalized)
    except TypeError as exc:
        raise ContractViolation(f"{label} contains invalid authoritative facts") from exc


def _validate_exact_event_fields(
    event: Mapping[str, Any],
    allowed: set[str],
    *,
    label: str,
    optional: set[str] | frozenset[str] = frozenset({"reason"}),
) -> None:
    missing = allowed - optional - set(event)
    if missing:
        raise ContractViolation(f"{label} requires field {sorted(missing)[0]!r}")
    unsupported = set(event) - allowed
    if unsupported:
        raise ContractViolation(f"{label} contains unsupported field {sorted(unsupported)[0]!r}")


def _validate_event_schema(event: Mapping[str, Any], event_type: str) -> None:
    """Keep the exhaustive ledger language closed at one validation seam."""

    if event_type == "IDENTICAL_DUPLICATE_OBSERVED" and "linked_attempt_sequence" not in event:
        raise ContractViolation(
            "IDENTICAL_DUPLICATE_OBSERVED requires a linked FETCH_ATTEMPT"
        )
    if (
        event_type == "SOURCE_REVISION_OBSERVED"
        and event.get("revision_scope") == "ACCEPTED_EVALUATION_DATA"
        and "linked_attempt_sequence" not in event
    ):
        raise ContractViolation(
            "ACCEPTED_EVALUATION_DATA revision requires a linked FETCH_ATTEMPT"
        )
    supplied = set(event)
    variants = EVENT_FIELD_SCHEMAS[event_type]
    for allowed in variants:
        if allowed - {"reason"} <= supplied <= allowed:
            return
    allowed_union = set().union(*variants)
    unsupported = supplied - allowed_union
    if unsupported:
        raise ContractViolation(
            f"{event_type} contains unsupported field {sorted(unsupported)[0]!r}"
        )
    closest = min(variants, key=lambda allowed: len((allowed - {"reason"}) - supplied))
    missing = (set(closest) - {"reason"}) - supplied
    raise ContractViolation(f"{event_type} requires field {sorted(missing)[0]!r}")


def _normalize_fetch_attempt(
    event: Mapping[str, Any],
    normalized: dict[str, Any],
) -> None:
    _validate_exact_event_fields(event, FETCH_ATTEMPT_FIELDS, label="FETCH_ATTEMPT")
    outcome = event["attempt_outcome"]
    if type(outcome) is not str or outcome not in ATTEMPT_OUTCOMES:
        raise ContractViolation("FETCH_ATTEMPT has an invalid attempt_outcome")
    request_at = _parse_timestamp(event["request_at"], "FETCH_ATTEMPT request_at")
    request_local = _parse_timestamp(
        event["request_at_asia_shanghai"],
        "FETCH_ATTEMPT request_at_asia_shanghai",
    )
    if (
        event["request_at_asia_shanghai"] != _canonical_local_timestamp(request_local)
        or request_local.astimezone(timezone.utc) != request_at.astimezone(timezone.utc)
    ):
        raise ContractViolation("FETCH_ATTEMPT request timestamp pair is not canonical or coherent")
    request_end_date = request_at.astimezone(SHANGHAI).date()
    expected_url = (
        "https://vip.stock.finance.sina.com.cn/q/view/download_gold_history.php"
        "?breed=AU9999&start=2021-01-01"
        f"&end={request_end_date.isoformat()}"
    )
    if type(event["request_url"]) is not str or event["request_url"] != expected_url:
        raise ContractViolation(
            "FETCH_ATTEMPT request_url must match the frozen Sina endpoint and request date"
        )
    if _parse_date(event["candidate_date"]) > request_end_date:
        raise ContractViolation("FETCH_ATTEMPT candidate date cannot follow its request end date")
    response_value = event["response_at"]
    response_at = (
        None
        if response_value is None
        else _parse_timestamp(response_value, "FETCH_ATTEMPT response_at")
    )
    response_local_value = event["response_at_asia_shanghai"]
    response_local = (
        None
        if response_local_value is None
        else _parse_timestamp(
            response_local_value,
            "FETCH_ATTEMPT response_at_asia_shanghai",
        )
    )
    if (response_at is None) is not (response_local is None) or (
        response_at is not None
        and response_local is not None
        and (
            response_local_value != _canonical_local_timestamp(response_local)
            or response_local.astimezone(timezone.utc) != response_at.astimezone(timezone.utc)
        )
    ):
        raise ContractViolation("FETCH_ATTEMPT response timestamp pair is not canonical or coherent")
    occurred = _event_time(normalized)
    if response_at is not None and response_at < request_at:
        raise ContractViolation("FETCH_ATTEMPT response_at cannot precede request_at")
    if request_at > occurred or (response_at is not None and response_at > occurred):
        raise ContractViolation("FETCH_ATTEMPT provenance timestamps cannot follow event_at")
    normalized["request_at"] = _canonical_utc_timestamp(request_at)
    normalized["request_at_asia_shanghai"] = _canonical_local_timestamp(request_at)
    normalized["response_at"] = (
        _canonical_utc_timestamp(response_at) if response_at is not None else None
    )
    normalized["response_at_asia_shanghai"] = (
        _canonical_local_timestamp(response_at) if response_at is not None else None
    )

    evidence = set(normalized["evidence_sha256"])
    for field_name in (
        "collector_sha256",
        "parser_sha256",
        "build_sha256",
        "runtime_sha256",
    ):
        identity = _validate_sha256(event[field_name], f"FETCH_ATTEMPT {field_name}")
        if identity not in evidence:
            raise ContractViolation(f"FETCH_ATTEMPT {field_name} must name referenced evidence")
    for field_name in ("raw_byte_sha256", "parsed_row_sha256"):
        identity = event[field_name]
        if identity is not None:
            identity = _validate_sha256(identity, f"FETCH_ATTEMPT {field_name}")
            if identity not in evidence:
                raise ContractViolation(f"FETCH_ATTEMPT {field_name} must name referenced evidence")

    history_rows = event["history_row_sha256s"]
    if outcome in HISTORY_MAP_OUTCOMES:
        if not isinstance(history_rows, Mapping):
            raise ContractViolation(f"{outcome} requires a deterministic history row map")
        normalized_history: dict[str, str] = {}
        for raw_date, raw_identity in history_rows.items():
            history_date = _parse_date(raw_date, "history row date")
            if history_date > request_end_date:
                raise ContractViolation("FETCH_ATTEMPT history row date cannot follow request end date")
            candidate_date = history_date.isoformat()
            identity = _validate_sha256(raw_identity, "history parsed-row identity")
            if identity not in evidence:
                raise ContractViolation("history parsed-row identity must name event evidence")
            normalized_history[candidate_date] = identity
        normalized["history_row_sha256s"] = dict(sorted(normalized_history.items()))
        candidate = event["candidate_date"]
        if outcome == "VALID_TARGET_ROW" and normalized_history.get(candidate) != event[
            "parsed_row_sha256"
        ]:
            raise ContractViolation("VALID_TARGET_ROW must appear exactly in its history row map")
        if outcome != "VALID_TARGET_ROW" and candidate in normalized_history:
            raise ContractViolation(f"{outcome} cannot assert a valid target in its history row map")
    elif history_rows is not None:
        raise ContractViolation(f"{outcome} cannot assert a parsed history row map")

    http_status = event["http_status"]
    if http_status is not None and (
        type(http_status) is not int or not 100 <= http_status <= 599
    ):
        raise ContractViolation("FETCH_ATTEMPT http_status must be null or an HTTP integer")
    error_details = event["error_details"]
    if error_details is not None and (type(error_details) is not str or not error_details.strip()):
        raise ContractViolation("FETCH_ATTEMPT error_details must be null or a nonempty string")

    response_headers = event["response_headers"]
    response_headers_sha256 = event["response_headers_sha256"]
    response_byte_length = event["response_byte_length"]
    if response_at is None:
        if any(
            value is not None
            for value in (response_headers, response_headers_sha256, response_byte_length)
        ):
            raise ContractViolation("response metadata requires an actual response")
    else:
        if not isinstance(response_headers, Mapping) or not response_headers:
            raise ContractViolation("FETCH_ATTEMPT response_headers must be a nonempty object")
        normalized_headers: dict[str, str] = {}
        for key, value in response_headers.items():
            if (
                type(key) is not str
                or not key.strip()
                or key != key.strip().lower()
                or type(value) is not str
                or not value.strip()
            ):
                raise ContractViolation(
                    "FETCH_ATTEMPT response_headers require lowercase names and nonempty values"
                )
            normalized_headers[key] = value
        header_identity = _validate_sha256(
            response_headers_sha256,
            "FETCH_ATTEMPT response_headers_sha256",
        )
        if header_identity not in evidence:
            raise ContractViolation("FETCH_ATTEMPT response headers hash must name event evidence")
        if header_identity != hashlib.sha256(canonical_json_bytes(normalized_headers)).hexdigest():
            raise ContractViolation("FETCH_ATTEMPT response headers hash does not match metadata")
        _validate_exact_integer(
            response_byte_length,
            "FETCH_ATTEMPT response_byte_length",
            minimum=1,
        )
        normalized["response_headers"] = normalized_headers

    if outcome == "VALID_TARGET_ROW":
        if (
            response_at is None
            or http_status != 200
            or event["raw_byte_sha256"] is None
            or event["parsed_row_sha256"] is None
            or error_details is not None
        ):
            raise ContractViolation("VALID_TARGET_ROW requires complete successful provenance")
    elif outcome == "TRANSPORT_ERROR":
        if any(
            event[field_name] is not None
            for field_name in (
                "response_at",
                "http_status",
                "response_headers",
                "response_headers_sha256",
                "response_byte_length",
                "raw_byte_sha256",
                "parsed_row_sha256",
            )
        ) or error_details is None:
            raise ContractViolation("TRANSPORT_ERROR requires transport-only failure provenance")
    else:
        if response_at is None or http_status is None or error_details is None:
            raise ContractViolation(f"{outcome} requires response and error provenance")
        if outcome == "HTTP_ERROR":
            if (
                200 <= http_status < 300
                or event["raw_byte_sha256"] is None
                or event["parsed_row_sha256"] is not None
            ):
                raise ContractViolation("HTTP_ERROR requires non-success status and no parsed row")
        elif http_status != 200 or event["raw_byte_sha256"] is None:
            raise ContractViolation(f"{outcome} requires HTTP 200 raw-byte provenance")
        if outcome in {
            "DECODE_ERROR",
            "SCHEMA_ERROR",
            "TARGET_DATE_ABSENT",
            "DUPLICATE_TARGET_DATE",
        } and event["parsed_row_sha256"] is not None:
            raise ContractViolation(f"{outcome} cannot assert one canonical parsed target row")
        if outcome == "INVALID_OHLC" and event["parsed_row_sha256"] is None:
            raise ContractViolation("INVALID_OHLC requires its parsed-row identity")


def _normalize_correction(event: Mapping[str, Any], normalized: dict[str, Any]) -> None:
    """Validate the exact additive-correction evidence interface."""

    _validate_exact_event_fields(event, CORRECTION_FIELDS, label="CORRECTION", optional=set())
    _validate_exact_integer(
        event["superseded_sequence"],
        "CORRECTION superseded_sequence",
        minimum=1,
    )
    _validate_sha256(event["superseded_record_sha256"], "CORRECTION superseded record")
    identity_fields = {
        "old_value_sha256",
        "new_observation_sha256",
        "decision_surface_before_sha256",
        "decision_surface_after_sha256",
    }
    identities = {
        field_name: _validate_sha256(event[field_name], f"CORRECTION {field_name}")
        for field_name in identity_fields
    }
    source_sha256s = event["source_sha256s"]
    if not isinstance(source_sha256s, list) or not source_sha256s:
        raise ContractViolation("CORRECTION source_sha256s must be a nonempty array")
    normalized_sources = [
        _validate_sha256(identity, "CORRECTION source identity")
        for identity in source_sha256s
    ]
    if len(set(normalized_sources)) != len(normalized_sources):
        raise ContractViolation("CORRECTION source identities must be unique")
    evidence = set(normalized["evidence_sha256"])
    if not {*identities.values(), *normalized_sources}.issubset(evidence):
        raise ContractViolation("CORRECTION source and value identities must name event evidence")
    scope = event["correction_scope"]
    if type(scope) is not str or scope not in CORRECTION_SCOPES:
        raise ContractViolation("CORRECTION correction_scope is unsupported")
    issuer = event["issuer"]
    if type(issuer) is not str or not issuer.strip():
        raise ContractViolation("CORRECTION issuer must be a nonempty identity")
    reason = event["reason"]
    if type(reason) is not str or not reason.strip():
        raise ContractViolation("CORRECTION reason must be nonempty")
    if (
        scope == "CLERICAL_METADATA"
        and identities["decision_surface_before_sha256"]
        != identities["decision_surface_after_sha256"]
    ):
        raise ContractViolation("clerical CORRECTION must prove an unchanged decision surface")
    normalized["source_sha256s"] = normalized_sources


def _normalize_operator_access(event: Mapping[str, Any], normalized: dict[str, Any]) -> None:
    """Seal the exact operator identity and content-addressed files viewed."""

    operator = event["operator_identity"]
    if type(operator) is not str or not operator.strip():
        raise ContractViolation("OPERATOR_ACCESS operator_identity must be nonempty")
    access_identity = _validate_sha256(
        event["access_identity_sha256"],
        "OPERATOR_ACCESS access_identity_sha256",
    )
    evidence = set(normalized["evidence_sha256"])
    if access_identity not in evidence:
        raise ContractViolation("OPERATOR_ACCESS access identity must name event evidence")
    _validate_exact_integer(
        event["linked_diagnostic_sequence"],
        "OPERATOR_ACCESS linked_diagnostic_sequence",
        minimum=1,
    )
    _validate_sha256(
        event["diagnostic_record_sha256"],
        "OPERATOR_ACCESS diagnostic_record_sha256",
    )
    purpose = event["diagnostic_purpose"]
    if type(purpose) is not str or not purpose:
        raise ContractViolation("OPERATOR_ACCESS diagnostic_purpose must be nonempty")
    files = event["files_viewed"]
    if not isinstance(files, list) or not files:
        raise ContractViolation("OPERATOR_ACCESS files_viewed must be a nonempty array")
    normalized_files: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {"path", "artifact_sha256"}:
            raise ContractViolation("OPERATOR_ACCESS files_viewed entries require exact path/hash shape")
        path = item["path"]
        if type(path) is not str or not path.strip() or path in seen_paths:
            raise ContractViolation("OPERATOR_ACCESS file paths must be unique nonempty strings")
        identity = _validate_sha256(
            item["artifact_sha256"],
            "OPERATOR_ACCESS viewed-file identity",
        )
        if identity not in evidence:
            raise ContractViolation("OPERATOR_ACCESS viewed-file identity must name event evidence")
        seen_paths.add(path)
        normalized_files.append({"path": path, "artifact_sha256": identity})
    normalized["files_viewed"] = normalized_files


def _normalize_event(event: Mapping[str, Any], available: set[str]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ContractViolation("ledger event must be an object")
    reserved = GENERATED_FIELDS.intersection(event)
    if reserved:
        raise ContractViolation(f"ledger event supplied generated field {sorted(reserved)[0]!r}")
    event_type = event.get("event_type")
    if type(event_type) is not str or event_type not in EVENT_TYPES:
        raise ContractViolation(f"unsupported ledger event type {event_type!r}")
    _validate_event_schema(event, event_type)
    supplied_reason = event.get("reason")
    if "reason" in event and (
        type(supplied_reason) is not str or not supplied_reason.strip()
    ):
        raise ContractViolation(f"{event_type} reason must be nonempty")
    if "breach_reason" in event and (
        type(event["breach_reason"]) is not str or not event["breach_reason"].strip()
    ):
        raise ContractViolation(f"{event_type} breach_reason must be nonempty")
    occurred = _parse_timestamp(event.get("event_at"), "event_at")
    normalized = dict(event)
    normalized.pop("event_at", None)
    normalized["event_type"] = event_type
    normalized.setdefault("reason", f"contract event: {event_type}")
    normalized["event_at_utc"] = _canonical_utc_timestamp(occurred)
    normalized["event_at_asia_shanghai"] = _canonical_local_timestamp(occurred)
    normalized["evidence_sha256"] = _normalize_evidence(event, available)
    if event_type == "FETCH_ATTEMPT":
        _normalize_fetch_attempt(event, normalized)
    if event_type == "CORRECTION":
        _normalize_correction(event, normalized)
    if event_type == "OPERATOR_ACCESS":
        _normalize_operator_access(event, normalized)
    if event_type == "CANONICAL_ROW_ACCEPTED":
        _validate_exact_event_fields(
            event,
            CANONICAL_ACCEPTANCE_FIELDS,
            label="CANONICAL_ROW_ACCEPTED",
            optional={"reason", "target_sealed_at"},
        )
        canonical_row = _validate_sha256(
            event.get("canonical_row_sha256"),
            "canonical row",
        )
        if canonical_row not in normalized["evidence_sha256"]:
            raise ContractViolation("canonical row hash must name referenced immutable evidence")
        for field_name in ("parser_sha256", "build_sha256"):
            identity = _validate_sha256(event.get(field_name), f"canonical {field_name}")
            if identity not in normalized["evidence_sha256"]:
                raise ContractViolation(f"canonical {field_name} must name referenced evidence")
    if event_type == "IDENTICAL_DUPLICATE_OBSERVED":
        canonical_row = _validate_sha256(
            event.get("canonical_row_sha256"),
            "duplicate canonical row",
        )
        observed_row = _validate_sha256(
            event.get("observed_row_sha256"),
            "duplicate observed row",
        )
        if not {canonical_row, observed_row}.issubset(normalized["evidence_sha256"]):
            raise ContractViolation("duplicate row hashes must name referenced immutable evidence")
        if "linked_attempt_sequence" in event:
            _validate_exact_event_fields(
                event,
                ACCEPTED_ATTEMPT_LINK_FIELDS,
                label="linked accepted-date duplicate",
            )
            for field_name in ("baseline_raw_sha256", "observed_raw_sha256"):
                identity = _validate_sha256(event.get(field_name), f"duplicate {field_name}")
                if identity not in normalized["evidence_sha256"]:
                    raise ContractViolation(
                        f"duplicate {field_name} must name referenced immutable evidence"
                    )
            if event.get("observed_outcome") != "VALID_TARGET_ROW":
                raise ContractViolation("linked identical duplicate requires VALID_TARGET_ROW")
    if event_type == "SOURCE_REVISION_OBSERVED":
        if "touches_evaluation_data" not in event:
            raise ContractViolation(
                "SOURCE_REVISION_OBSERVED touches_evaluation_data is required"
            )
        touches_evaluation_data = event["touches_evaluation_data"]
        if type(touches_evaluation_data) is not bool:
            raise ContractViolation("touches_evaluation_data must be boolean")
        if "revision_scope" not in event:
            raise ContractViolation("SOURCE_REVISION_OBSERVED revision_scope is required")
        revision_scope = event["revision_scope"]
        if type(revision_scope) is not str or revision_scope not in SOURCE_REVISION_SCOPES:
            raise ContractViolation("SOURCE_REVISION_OBSERVED revision_scope is unsupported")
        expected_invalidation_scope = revision_scope != "NON_EVALUATION_EVIDENCE"
        if touches_evaluation_data is not expected_invalidation_scope:
            raise ContractViolation("touches_evaluation_data contradicts revision_scope")
        if revision_scope == "ACCEPTED_EVALUATION_DATA" and "candidate_date" not in event:
            raise ContractViolation(
                "ACCEPTED_EVALUATION_DATA revision requires candidate_date provenance"
            )
        if "comparison_findings" in event:
            findings = event["comparison_findings"]
            if not isinstance(findings, list) or not findings:
                raise ContractViolation("full-history revision requires comparison findings")
            normalized_findings: list[dict[str, Any]] = []
            for finding in findings:
                if not isinstance(finding, Mapping) or set(finding) != {
                    "candidate_date",
                    "baseline_row_sha256",
                    "observed_row_sha256",
                    "change_type",
                }:
                    raise ContractViolation("comparison finding has an invalid exact shape")
                change_type = finding["change_type"]
                if change_type not in {"CHANGED", "DELETED", "INSERTED", "UNCOMPARABLE"}:
                    raise ContractViolation("comparison finding has an invalid change_type")
                normalized_finding = {
                    "candidate_date": _parse_date(
                        finding["candidate_date"], "comparison candidate date"
                    ).isoformat(),
                    "baseline_row_sha256": finding["baseline_row_sha256"],
                    "observed_row_sha256": finding["observed_row_sha256"],
                    "change_type": change_type,
                }
                for field_name in ("baseline_row_sha256", "observed_row_sha256"):
                    identity = normalized_finding[field_name]
                    if identity is not None:
                        identity = _validate_sha256(identity, f"comparison {field_name}")
                        if identity not in normalized["evidence_sha256"]:
                            raise ContractViolation(
                                f"comparison {field_name} must name event evidence"
                            )
                normalized_findings.append(normalized_finding)
            if normalized_findings != sorted(
                normalized_findings,
                key=lambda item: (item["candidate_date"], item["change_type"]),
            ):
                raise ContractViolation("comparison findings must be in deterministic date order")
            normalized["comparison_findings"] = normalized_findings
        elif "linked_attempt_sequence" in event:
            _validate_exact_event_fields(
                event,
                ACCEPTED_REVISION_LINK_FIELDS,
                label="linked accepted-date revision",
            )
            for field_name in ("baseline_raw_sha256", "canonical_row_sha256"):
                identity = _validate_sha256(event.get(field_name), f"revision {field_name}")
                if identity not in normalized["evidence_sha256"]:
                    raise ContractViolation(
                        f"revision {field_name} must name referenced immutable evidence"
                    )
            observed_raw = event.get("observed_raw_sha256")
            if observed_raw is not None:
                observed_raw = _validate_sha256(observed_raw, "revision observed_raw_sha256")
                if observed_raw not in normalized["evidence_sha256"]:
                    raise ContractViolation(
                        "revision observed_raw_sha256 must name referenced immutable evidence"
                    )
            observed_row = event.get("observed_row_sha256")
            if observed_row is not None:
                observed_row = _validate_sha256(observed_row, "revision observed_row_sha256")
                if observed_row not in normalized["evidence_sha256"]:
                    raise ContractViolation(
                        "revision observed_row_sha256 must name referenced immutable evidence"
                    )
            if event.get("observed_outcome") not in ACCEPTED_REVISION_OUTCOMES:
                raise ContractViolation("linked source revision has an invalid observed_outcome")
    if event_type == "CALENDAR_RECLASSIFIED":
        facts = _validated_authoritative_facts(
            event.get("authoritative_facts"),
            label="CALENDAR_RECLASSIFIED",
        )
        if not set(facts.evidence_sha256).issubset(normalized["evidence_sha256"]):
            raise ContractViolation(
                "CALENDAR_RECLASSIFIED event evidence must include every authoritative fact identity"
            )
        normalized["authoritative_facts"] = {
            **asdict(facts),
            "evidence_sha256": list(facts.evidence_sha256),
        }
        _, normalized["governed_open_boundary"] = _deadline(
            event.get("governed_open_boundary"),
            "governed_open_boundary",
        )
    if "candidate_date" in event:
        normalized["candidate_date"] = _parse_date(event["candidate_date"]).isoformat()
    canonical_json_bytes(normalized)
    return normalized


def _event_time(event: Mapping[str, Any]) -> datetime:
    return _parse_timestamp(event["event_at_asia_shanghai"], "event_at_asia_shanghai")


def _deadline(value: Any, label: str) -> tuple[datetime, str]:
    parsed = _parse_timestamp(value, label)
    local = _canonical_local_timestamp(parsed)
    return parsed, local


def _calendar_decisions(
    authoritative_calendar: Mapping[str, AuthoritativeSessionFacts],
    available: set[str],
) -> dict[str, SessionDecision]:
    if not isinstance(authoritative_calendar, Mapping) or not authoritative_calendar:
        raise ContractViolation("authoritative calendar facts are required")
    decisions: dict[str, SessionDecision] = {}
    for raw_date, facts in authoritative_calendar.items():
        candidate = _parse_date(raw_date, "authoritative calendar date")
        facts = _validated_authoritative_facts(
            facts,
            label=f"authoritative calendar date {candidate.isoformat()}",
        )
        decision = classify_session(candidate, facts)
        if decision.status is CalendarStatus.AMBIGUOUS_BLOCKED:
            raise ContractViolation(f"calendar date {raw_date} is ambiguous and blocked")
        missing = [identity for identity in decision.evidence_sha256 if identity not in available]
        if missing:
            raise ContractViolation(f"missing referenced identity: {missing[0]}")
        decisions[candidate.isoformat()] = decision
    return decisions


def _next_expected_decision(
    candidate: date,
    state: _LedgerState,
) -> tuple[date, SessionDecision]:
    following = candidate + timedelta(days=1)
    while True:
        decision = state.calendar.get(following.isoformat())
        if decision is None:
            raise ContractViolation(
                "authoritative calendar must be contiguous through the next expected session"
            )
        if decision.status is CalendarStatus.EXPECTED_SESSION:
            assert decision.open_boundary is not None
            return following, decision
        following += timedelta(days=1)


def _acceptance_deadline(
    candidate: date,
    assigned_ordinal: int,
    state: _LedgerState,
) -> tuple[datetime, str, str]:
    next_date, next_decision = _next_expected_decision(candidate, state)
    if assigned_ordinal <= 251:
        assert next_decision.open_boundary is not None
        deadline = next_decision.open_boundary
        field_name = "execution_seal_deadline"
    else:
        deadline = datetime.combine(next_date, time(15, 45), tzinfo=SHANGHAI)
        field_name = "recovery_deadline"
    return deadline, _canonical_local_timestamp(deadline), field_name


def _validate_record_event(record: Mapping[str, Any], available: set[str]) -> dict[str, Any]:
    base = _base_event_from_record(record)
    local_value = base.get("event_at_asia_shanghai")
    utc_value = base.get("event_at_utc")
    local = _parse_timestamp(local_value, "event_at_asia_shanghai")
    utc = _parse_timestamp(utc_value, "event_at_utc")
    if (
        local_value != _canonical_local_timestamp(local)
        or utc_value != _canonical_utc_timestamp(utc)
        or local.astimezone(timezone.utc) != utc.astimezone(timezone.utc)
    ):
        raise ContractViolation("sealed event timestamp pair is not canonical or coherent")
    supplied = dict(base)
    supplied.pop("event_at_asia_shanghai", None)
    supplied.pop("event_at_utc", None)
    supplied["event_at"] = local_value
    normalized = _normalize_event(supplied, available)
    if normalized != base:
        raise ContractViolation("sealed event does not have the required normalized shape")
    return base


def _date_state(event: Mapping[str, Any], state: _LedgerState) -> tuple[str, str]:
    candidate = event.get("candidate_date")
    if not isinstance(candidate, str) or candidate not in state.states:
        raise ContractViolation("event candidate_date must name a declared date")
    return candidate, state.states[candidate]


def _validate_exact_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ContractViolation(f"{label} must be an integer, not a boolean or other scalar")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" through {maximum}" if maximum is not None else " or greater"
        raise ContractViolation(f"{label} must be {minimum}{upper}")
    return value


def _validate_generated_record_types(record: Mapping[str, Any]) -> None:
    """Reject JSON scalars that compare equal but are outside the sealed language."""

    _validate_exact_integer(record.get("sequence"), "sequence", minimum=1)
    for field_name in ("event_at_utc", "event_at_asia_shanghai"):
        if type(record.get(field_name)) is not str:
            raise ContractViolation(f"{field_name} must be a timestamp string")
    for field_name in ("prior_state", "next_state"):
        if field_name in record:
            value = record[field_name]
            if value is not None and (type(value) is not str or value not in SESSION_STATUSES):
                raise ContractViolation(f"{field_name} must be null or an exact session-status string")
    if "expected_ordinal" in record:
        _validate_exact_integer(
            record["expected_ordinal"],
            "expected_ordinal",
            minimum=1,
            maximum=252,
        )
    for field_name in ("previous_chain_sha256", "record_sha256", "chain_sha256"):
        _validate_sha256(record.get(field_name), field_name)
    for field_name in ("execution_seal_deadline", "recovery_deadline"):
        if field_name in record and type(record[field_name]) is not str:
            raise ContractViolation(f"{field_name} must be a timestamp string")
    if "calendar_status" in record:
        calendar_status = record["calendar_status"]
        if type(calendar_status) is not str or calendar_status not in {
            status.value for status in CalendarStatus
        }:
            raise ContractViolation("calendar_status must be an exact calendar-status string")
    if "calendar_evidence_sha256" in record:
        evidence = record["calendar_evidence_sha256"]
        if type(evidence) is not list:
            raise ContractViolation("calendar_evidence_sha256 must be a JSON array")
        for identity in evidence:
            _validate_sha256(identity, "calendar evidence")
    for field_name in ("open_boundary_asia_shanghai", "open_boundary_utc"):
        if field_name in record:
            value = record[field_name]
            if value is not None and type(value) is not str:
                raise ContractViolation(f"{field_name} must be null or a timestamp string")
    if record.get("event_type") == "SILENT_MISS" or (
        record.get("event_type") == "PROTOCOL_BREACH_RECORDED"
        and "linked_attempt_sequence" in record
    ) or record.get("event_type") == "CANONICAL_ROW_ACCEPTED" or (
        record.get("event_type") in {"IDENTICAL_DUPLICATE_OBSERVED", "SOURCE_REVISION_OBSERVED"}
        and "linked_attempt_sequence" in record
    ):
        _validate_exact_integer(
            record.get("linked_attempt_sequence"),
            "linked_attempt_sequence",
            minimum=1,
        )
    if record.get("event_type") == "CANONICAL_ROW_ACCEPTED":
        _validate_sha256(record.get("canonical_raw_sha256"), "canonical_raw_sha256")
        capture = _parse_timestamp(record.get("capture_at"), "capture_at")
        if record["capture_at"] != _canonical_utc_timestamp(capture):
            raise ContractViolation("capture_at must be a canonical UTC timestamp")
    if record.get("event_type") in {
        "IDENTICAL_DUPLICATE_OBSERVED",
        "SOURCE_REVISION_OBSERVED",
    } and "linked_attempt_sequence" in record and "comparison_findings" not in record:
        for field_name in ("baseline_raw_sha256", "canonical_row_sha256"):
            _validate_sha256(record.get(field_name), field_name)
        observed_raw = record.get("observed_raw_sha256")
        if observed_raw is not None:
            _validate_sha256(observed_raw, "observed_raw_sha256")
        observed_row = record.get("observed_row_sha256")
        if observed_row is not None:
            _validate_sha256(observed_row, "observed_row_sha256")
    if "breach_civil_date" in record:
        _parse_date(record["breach_civil_date"], "breach_civil_date")
    if "terminal_coverage_date" in record:
        _parse_date(record["terminal_coverage_date"], "terminal_coverage_date")


def _invalidate_at_boundary(state: _LedgerState, boundary: datetime) -> str:
    breach_civil_date = boundary.astimezone(SHANGHAI).date()
    if not state.declared_dates or state.declared_dates[-1] != breach_civil_date:
        raise ContractViolation(
            "candidate-date coverage must end exactly on the applicable breach civil date"
        )
    state.invalidated = True
    state.breach_civil_date = breach_civil_date.isoformat()
    return state.breach_civil_date


def _invalidate_global_revision(state: _LedgerState, boundary: datetime) -> str:
    """Invalidate without extending candidate coverage after the D252 terminal seam."""

    if state.max_ordinal == 252 and state.terminal_coverage_date is not None:
        state.invalidated = True
        state.breach_civil_date = boundary.astimezone(SHANGHAI).date().isoformat()
        return state.breach_civil_date
    return _invalidate_at_boundary(state, boundary)


def _overdue_pending_deadline(
    event: Mapping[str, Any],
    state: _LedgerState,
) -> tuple[str, datetime, str, str] | None:
    if state.invalidated:
        return None
    occurred = _event_time(event)
    for candidate_date in state.declared_dates:
        candidate = candidate_date.isoformat()
        if state.states[candidate] != "EXPECTED_PENDING":
            continue
        assigned = state.max_ordinal + 1
        deadline, canonical_deadline, deadline_field = _acceptance_deadline(
            candidate_date,
            assigned,
            state,
        )
        if occurred < deadline:
            return None
        if (
            event.get("event_type") == "CALENDAR_RECLASSIFIED"
            and event.get("reclassified_status") == "EXPECTED_PENDING"
            and _parse_date(event.get("candidate_date")) > candidate_date
            and occurred
            < _parse_timestamp(
                event.get("governed_open_boundary"),
                "governed_open_boundary",
            )
        ):
            return None
        if assigned == 252 and occurred == deadline:
            if (
                event.get("event_type") == "FETCH_ATTEMPT"
                and event.get("attempt_outcome") == "VALID_TARGET_ROW"
                and event.get("candidate_date") == candidate
            ):
                return None
            pending_sequence = state.pending_valid_attempt_sequence
            pending_attempt = state.valid_attempts.get(pending_sequence or -1)
            if (
                event.get("event_type") == "CANONICAL_ROW_ACCEPTED"
                and event.get("candidate_date") == candidate
                and pending_attempt is not None
            ):
                return None
        return candidate, deadline, canonical_deadline, deadline_field
    return None


def _failed_attempt_requires_silent_miss(event: Mapping[str, Any]) -> bool:
    if (
        event.get("event_type") != "FETCH_ATTEMPT"
        or event.get("prior_state") != "EXPECTED_PENDING"
        or event.get("attempt_outcome") == "VALID_TARGET_ROW"
    ):
        return False
    candidate = _parse_date(event.get("candidate_date"))
    close_available = datetime.combine(candidate, time(15, 45), tzinfo=SHANGHAI)
    return _event_time(event) >= close_available


def _consume_accepted_attempt_link(
    event: Mapping[str, Any],
    state: _LedgerState,
) -> dict[str, Any]:
    attempt_sequence = _validate_exact_integer(
        event.get("linked_attempt_sequence"),
        "linked_attempt_sequence",
        minimum=1,
    )
    if attempt_sequence != state.pending_accepted_attempt_sequence:
        raise ContractViolation("linked accepted-date attempt sequence does not reproduce")
    attempt = state.accepted_attempts.get(attempt_sequence)
    if attempt is None:
        raise ContractViolation("linked accepted-date attempt is missing")
    candidate = attempt["candidate_date"]
    if event.get("candidate_date") != candidate or _event_time(event) != _event_time(attempt):
        raise ContractViolation("linked accepted-date attempt candidate or instant does not reproduce")
    expected_evidence = {
        *attempt["evidence_sha256"],
        state.canonical_raws[candidate],
        state.canonical_rows[candidate],
    }
    if set(event["evidence_sha256"]) != expected_evidence:
        raise ContractViolation("linked accepted-date attempt evidence does not reproduce")
    expected_fields = {
        "baseline_raw_sha256": state.canonical_raws[candidate],
        "canonical_row_sha256": state.canonical_rows[candidate],
        "observed_raw_sha256": attempt["raw_byte_sha256"],
        "observed_row_sha256": attempt["parsed_row_sha256"],
        "observed_outcome": attempt["attempt_outcome"],
    }
    for field_name, expected in expected_fields.items():
        if event.get(field_name) != expected:
            raise ContractViolation(
                f"linked accepted-date attempt field {field_name} does not reproduce"
            )
    state.pending_accepted_attempt_sequence = None
    state.pending_accepted_attempt_event_type = None
    return attempt


def _history_comparison_findings(
    event: Mapping[str, Any],
    state: _LedgerState,
) -> list[dict[str, Any]]:
    """Compare one full-history capture with every frozen decision-bearing row."""

    if event.get("event_type") != "FETCH_ATTEMPT" or event.get("response_at") is None:
        return []
    expected_rows = {**state.warmup_rows, **state.canonical_rows}
    observed_rows = event.get("history_row_sha256s")
    findings: list[dict[str, Any]] = []
    if observed_rows is None:
        for candidate, baseline in expected_rows.items():
            if candidate == event.get("candidate_date") and candidate in state.canonical_rows:
                continue
            findings.append(
                {
                    "candidate_date": candidate,
                    "baseline_row_sha256": baseline,
                    "observed_row_sha256": None,
                    "change_type": "UNCOMPARABLE",
                }
            )
        return findings

    for candidate, baseline in expected_rows.items():
        if (
            candidate == event.get("candidate_date")
            and (
                event.get("attempt_outcome") == "DUPLICATE_TARGET_DATE"
                or candidate in state.canonical_rows
            )
        ):
            continue
        observed = observed_rows.get(candidate)
        if observed is None:
            change_type = "DELETED"
        elif observed != baseline:
            change_type = "CHANGED"
        else:
            continue
        findings.append(
            {
                "candidate_date": candidate,
                "baseline_row_sha256": baseline,
                "observed_row_sha256": observed,
                "change_type": change_type,
            }
        )
    assert state.expected_first_date is not None
    for candidate, observed in observed_rows.items():
        if _parse_date(candidate) < state.expected_first_date and candidate not in expected_rows:
            findings.append(
                {
                    "candidate_date": candidate,
                    "baseline_row_sha256": None,
                    "observed_row_sha256": observed,
                    "change_type": "INSERTED",
                }
            )
    return sorted(findings, key=lambda item: (item["candidate_date"], item["change_type"]))


def _diagnostic_purpose(record: Mapping[str, Any]) -> str | None:
    """Return the sole diagnostic purpose authorized by an existing alert record."""

    event_type = record.get("event_type")
    if event_type == "FETCH_ATTEMPT" and record.get("attempt_outcome") != "VALID_TARGET_ROW":
        outcome = record.get("attempt_outcome")
        return outcome if isinstance(outcome, str) else None
    if event_type in {
        "SILENT_MISS",
        "SOURCE_REVISION_OBSERVED",
        "DEADLINE_EXPIRED",
        "PROTOCOL_BREACH_RECORDED",
    }:
        return str(event_type)
    return None


def _require_candidate_coverage_through_event(
    event: Mapping[str, Any],
    state: _LedgerState,
) -> None:
    """Keep the active candidate ledger gapless through each observed civil date."""

    if state.invalidated or state.terminal_coverage_date is not None:
        return
    event_date = _event_time(event).astimezone(SHANGHAI).date()
    if not state.declared_dates or state.declared_dates[-1] < event_date:
        raise ContractViolation(
            "candidate-date coverage must extend through the event civil date before processing"
        )


def _consume_history_revision_link(
    event: Mapping[str, Any],
    state: _LedgerState,
) -> None:
    attempt_sequence = _validate_exact_integer(
        event.get("linked_attempt_sequence"),
        "linked_attempt_sequence",
        minimum=1,
    )
    if attempt_sequence != state.pending_history_revision_sequence:
        raise ContractViolation("linked full-history attempt sequence does not reproduce")
    attempt = state.sealed_records.get(attempt_sequence)
    if attempt is None:
        raise ContractViolation("linked full-history attempt is missing")
    if (
        event.get("candidate_date") != attempt.get("candidate_date")
        or _event_time(event) != _event_time(attempt)
        or event.get("comparison_findings") != state.pending_history_revision_findings
    ):
        raise ContractViolation("linked full-history comparison does not reproduce")
    expected_evidence = set(attempt["evidence_sha256"])
    for finding in state.pending_history_revision_findings or []:
        expected_evidence.update(
            identity
            for identity in (
                finding["baseline_row_sha256"],
                finding["observed_row_sha256"],
            )
            if identity is not None
        )
    if set(event["evidence_sha256"]) != expected_evidence:
        raise ContractViolation("linked full-history comparison evidence does not reproduce")
    state.pending_history_revision_sequence = None
    state.pending_history_revision_findings = None
    if state.pending_valid_attempt_sequence == attempt_sequence:
        state.pending_valid_attempt_sequence = None


def _process_event(
    event: dict[str, Any],
    state: _LedgerState,
    *,
    convert_late_acceptance: bool,
    sequence: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event_type = event["event_type"]
    prior_state: str | None = None
    next_state: str | None = None
    expected_ordinal: int | None = None
    deadline_field: str | None = None
    canonical_deadline: str | None = None
    breach_civil_date: str | None = None
    terminal_coverage_date: str | None = None
    calendar_derived: dict[str, Any] = {}

    if state.finalized:
        raise ContractViolation("no ledger event may follow FINALIZED")
    if state.pending_history_revision_sequence is not None and event_type != "SOURCE_REVISION_OBSERVED":
        raise ContractViolation(
            "full-history FETCH_ATTEMPT requires its linked SOURCE_REVISION_OBSERVED event"
        )
    if state.pending_accepted_attempt_sequence is not None and (
        event_type != state.pending_accepted_attempt_event_type
    ):
        raise ContractViolation(
            "accepted-date FETCH_ATTEMPT requires its linked duplicate or revision event"
        )
    linked_duplicate_breach = (
        event_type == "PROTOCOL_BREACH_RECORDED"
        and "linked_attempt_sequence" in event
    )
    if (
        state.invalidated
        and event_type not in POST_INVALIDATION_PROVENANCE_EVENTS
        and not linked_duplicate_breach
    ):
        raise ContractViolation(
            "irreversible invalidation ends candidate-date coverage; only provenance events may follow"
        )
    occurred = _event_time(event)
    if state.genesis_sealed_at is None or occurred < state.genesis_sealed_at:
        raise ContractViolation("ledger event timestamp cannot predate the genesis seal")
    if event_type == "FETCH_ATTEMPT":
        request_at = _parse_timestamp(event["request_at"], "FETCH_ATTEMPT request_at")
        response_at = (
            None
            if event["response_at"] is None
            else _parse_timestamp(event["response_at"], "FETCH_ATTEMPT response_at")
        )
        if request_at < state.genesis_sealed_at or (
            response_at is not None and response_at < state.genesis_sealed_at
        ):
            raise ContractViolation("FETCH_ATTEMPT provenance cannot predate the genesis seal")
    if state.last_event_at is not None and occurred < state.last_event_at:
        raise ContractViolation(
            "ledger event chronology must be nondecreasing; sequence breaks timestamp ties"
        )
    if event_type != "DATE_DECLARED":
        _require_candidate_coverage_through_event(event, state)
    overdue = _overdue_pending_deadline(event, state)
    if overdue is not None:
        overdue_candidate, _, _, _ = overdue
        if not (
            event_type == "DEADLINE_EXPIRED"
            and event.get("candidate_date") == overdue_candidate
        ):
            raise ContractViolation(
                "pending date at or after its deadline requires a preceding DEADLINE_EXPIRED"
            )
    if state.pending_valid_attempt_sequence is not None and event_type not in {
        "CANONICAL_ROW_ACCEPTED",
        "DEADLINE_EXPIRED",
        "SOURCE_REVISION_OBSERVED",
    }:
        raise ContractViolation(
            "VALID_TARGET_ROW attempt must be followed by its canonical acceptance or deadline expiry"
        )

    if event_type == "DATE_DECLARED":
        candidate = _parse_date(event.get("candidate_date"))
        if state.terminal_coverage_date is not None:
            raise ContractViolation("successful terminal coverage forbids later candidate dates")
        if candidate.isoformat() in state.states:
            raise ContractViolation("candidate date may be declared exactly once")
        if not state.declared_dates and candidate != state.expected_first_date:
            raise ContractViolation(
                "candidate-date coverage must begin immediately after the genesis Shanghai date"
            )
        if state.declared_dates and candidate != state.declared_dates[-1] + timedelta(days=1):
            raise ContractViolation("candidate-date coverage must be contiguous without gaps")
        initial = event.get("initial_status")
        if initial not in {"NON_SESSION", "EXPECTED_PENDING", "OFFICIAL_FULL_CLOSURE"}:
            raise ContractViolation("DATE_DECLARED has an invalid initial status")
        decision = state.calendar.get(candidate.isoformat())
        if decision is None:
            raise ContractViolation("declared date requires authoritative calendar facts")
        authoritative_initial = {
            CalendarStatus.NON_SESSION: "NON_SESSION",
            CalendarStatus.EXPECTED_SESSION: "EXPECTED_PENDING",
            CalendarStatus.OFFICIAL_FULL_CLOSURE: "OFFICIAL_FULL_CLOSURE",
        }[decision.status]
        if initial != authoritative_initial:
            raise ContractViolation("DATE_DECLARED status contradicts authoritative calendar facts")
        calendar_derived = {
            "calendar_status": decision.status.value,
            "calendar_evidence_sha256": list(decision.evidence_sha256),
            "open_boundary_asia_shanghai": (
                _canonical_local_timestamp(decision.open_boundary)
                if decision.open_boundary is not None
                else None
            ),
            "open_boundary_utc": (
                _canonical_utc_timestamp(decision.open_boundary)
                if decision.open_boundary is not None
                else None
            ),
        }
        state.declared_dates.append(candidate)
        state.states[candidate.isoformat()] = initial
        next_state = initial
        _require_candidate_coverage_through_event(event, state)

    elif event_type == "CALENDAR_RECLASSIFIED":
        candidate, prior_state = _date_state(event, state)
        if prior_state in TERMINAL_DATE_STATES:
            raise ContractViolation(f"terminal date state {prior_state} cannot transition")
        prior_decision = state.calendar.get(candidate)
        if prior_decision is None:
            raise ContractViolation("calendar reclassification requires an authoritative prior decision")
        facts = _validated_authoritative_facts(
            event.get("authoritative_facts"),
            label="CALENDAR_RECLASSIFIED",
        )
        decision = classify_session(_parse_date(candidate), facts)
        if decision.status is CalendarStatus.AMBIGUOUS_BLOCKED:
            raise ContractViolation("calendar reclassification authoritative facts are ambiguous")
        requested = event.get("reclassified_status")
        authoritative_requested = {
            CalendarStatus.NON_SESSION: "NON_SESSION",
            CalendarStatus.EXPECTED_SESSION: "EXPECTED_PENDING",
            CalendarStatus.OFFICIAL_FULL_CLOSURE: "OFFICIAL_FULL_CLOSURE",
        }[decision.status]
        if requested != authoritative_requested:
            raise ContractViolation("calendar reclassification contradicts authoritative facts")
        permitted = {
            ("NON_SESSION", "EXPECTED_PENDING"),
            ("EXPECTED_PENDING", "OFFICIAL_FULL_CLOSURE"),
        }
        if (prior_state, requested) not in permitted:
            raise ContractViolation("forbidden calendar reclassification transition")
        assert isinstance(requested, str)
        if prior_state == "NON_SESSION":
            if prior_decision.status is not CalendarStatus.NON_SESSION:
                raise ContractViolation("calendar reclassification prior state is not authoritative")
            boundary = decision.open_boundary
        else:
            if prior_decision.status is not CalendarStatus.EXPECTED_SESSION:
                raise ContractViolation("calendar reclassification prior state is not authoritative")
            boundary = prior_decision.open_boundary
        if boundary is None:
            raise ContractViolation("calendar reclassification has no governed opening boundary")
        supplied_boundary, supplied_canonical = _deadline(
            event.get("governed_open_boundary"),
            "governed_open_boundary",
        )
        if (
            supplied_canonical != _canonical_local_timestamp(boundary)
            or supplied_boundary.astimezone(timezone.utc) != boundary.astimezone(timezone.utc)
        ):
            raise ContractViolation(
                "calendar reclassification governed boundary contradicts authoritative facts"
            )
        if _event_time(event) >= boundary:
            event["event_type"] = "PROTOCOL_BREACH_RECORDED"
            event["breach_reason"] = "late contradictory calendar discovery"
            requested = "PROTOCOL_BREACH"
            breach_civil_date = _invalidate_at_boundary(state, occurred)
        else:
            state.calendar[candidate] = decision
        state.states[candidate] = requested
        next_state = requested

    elif event_type == "CANONICAL_ROW_ACCEPTED":
        candidate, prior_state = _date_state(event, state)
        if prior_state in TERMINAL_DATE_STATES:
            raise ContractViolation(f"terminal date state {prior_state} cannot transition")
        if prior_state != "EXPECTED_PENDING":
            raise ContractViolation("canonical acceptance requires EXPECTED_PENDING")
        attempt_sequence = state.pending_valid_attempt_sequence
        attempt = state.valid_attempts.get(attempt_sequence or -1)
        if attempt is None:
            raise ContractViolation(
                "canonical acceptance requires a preceding VALID_TARGET_ROW attempt"
            )
        if attempt["candidate_date"] != candidate:
            raise ContractViolation("canonical acceptance candidate does not match its attempt")
        if event["canonical_row_sha256"] != attempt["parsed_row_sha256"]:
            raise ContractViolation("canonical row identity does not match its attempt")
        for field_name in ("parser_sha256", "build_sha256"):
            if event[field_name] != attempt[field_name]:
                raise ContractViolation(
                    f"canonical {field_name} does not match its VALID_TARGET_ROW attempt"
                )
        attempt_evidence = {
            attempt["raw_byte_sha256"],
            attempt["parsed_row_sha256"],
            attempt["response_headers_sha256"],
            attempt["collector_sha256"],
            attempt["parser_sha256"],
            attempt["build_sha256"],
            attempt["runtime_sha256"],
        }
        if not attempt_evidence.issubset(event["evidence_sha256"]):
            raise ContractViolation("canonical acceptance evidence does not bind its attempt")
        captured_at = _parse_timestamp(attempt["response_at"], "VALID_TARGET_ROW response_at")
        if occurred < captured_at:
            raise ContractViolation("canonical acceptance cannot precede its captured response")
        state.pending_valid_attempt_sequence = None
        candidate_date = _parse_date(candidate)
        for earlier in state.declared_dates:
            if earlier >= candidate_date:
                break
            if state.states[earlier.isoformat()] == "EXPECTED_PENDING":
                raise ContractViolation("earlier candidate dates must reach a final state first")
        if state.max_ordinal >= 252:
            raise ContractViolation("ordinal 252 is the sole final accepted ordinal")
        assigned = state.max_ordinal + 1
        close_available = datetime.combine(candidate_date, time(15, 45), tzinfo=SHANGHAI)
        if occurred < close_available:
            raise ContractViolation("canonical acceptance cannot precede D 15:45 close availability")
        deadline, canonical_deadline, deadline_field = _acceptance_deadline(
            candidate_date,
            assigned,
            state,
        )
        state.deadlines[candidate] = canonical_deadline
        if assigned <= 251:
            target_sealed = _parse_timestamp(event.get("target_sealed_at"), "target_sealed_at")
            if target_sealed < captured_at:
                raise ContractViolation("target seal cannot precede its captured response")
            if target_sealed < close_available or target_sealed > occurred:
                raise ContractViolation(
                    "target seal must be at or after D 15:45 and no later than its event"
                )
            late = occurred >= deadline or target_sealed >= deadline
        else:
            if "target_sealed_at" in event:
                raise ContractViolation("D252 cannot seal a next-session target")
            recovery_date = deadline.astimezone(SHANGHAI).date().isoformat()
            if recovery_date not in state.states:
                raise ContractViolation(
                    "D252 candidate coverage must include its recovery-deadline civil date"
                )
            late = occurred > deadline
        if late:
            if not convert_late_acceptance:
                raise ContractViolation("late acceptance was not converted to its breach path")
            event = dict(event)
            event["event_type"] = "DEADLINE_EXPIRED"
            event["rejected_event_type"] = "CANONICAL_ROW_ACCEPTED"
            event["breach_reason"] = "canonical evidence was not sealed before its deadline"
            breach_civil_date = _invalidate_at_boundary(state, deadline)
            state.states[candidate] = "PROTOCOL_BREACH"
            next_state = "PROTOCOL_BREACH"
        else:
            state.states[candidate] = "EXPECTED_ACCEPTED"
            state.max_ordinal = assigned
            state.ordinals[candidate] = assigned
            state.canonical_rows[candidate] = event["canonical_row_sha256"]
            state.canonical_raws[candidate] = attempt["raw_byte_sha256"]
            expected_ordinal = assigned
            next_state = "EXPECTED_ACCEPTED"
            calendar_derived = {
                "linked_attempt_sequence": attempt_sequence,
                "canonical_raw_sha256": attempt["raw_byte_sha256"],
                "capture_at": attempt["response_at"],
            }
            if assigned == 252:
                terminal_date = max(candidate_date, deadline.astimezone(SHANGHAI).date())
                if not state.declared_dates or state.declared_dates[-1] != terminal_date:
                    raise ContractViolation(
                        "candidate-date coverage must end exactly on successful terminal coverage"
                    )
                terminal_coverage_date = terminal_date.isoformat()
                state.terminal_coverage_date = terminal_coverage_date

    elif event_type == "DEADLINE_EXPIRED":
        candidate, prior_state = _date_state(event, state)
        if prior_state in TERMINAL_DATE_STATES:
            raise ContractViolation(f"terminal date state {prior_state} cannot transition")
        if prior_state != "EXPECTED_PENDING":
            raise ContractViolation("DEADLINE_EXPIRED requires EXPECTED_PENDING")
        assigned = state.max_ordinal + 1
        deadline, canonical_deadline, deadline_field = _acceptance_deadline(
            _parse_date(candidate),
            assigned,
            state,
        )
        if assigned <= 251 and occurred < deadline:
            raise ContractViolation("execution deadline cannot expire before its opening boundary")
        if assigned == 252 and occurred < deadline:
            raise ContractViolation("D252 recovery deadline cannot expire before 15:45")
        state.deadlines[candidate] = canonical_deadline
        breach_civil_date = _invalidate_at_boundary(state, deadline)
        state.states[candidate] = "PROTOCOL_BREACH"
        state.pending_valid_attempt_sequence = None
        next_state = "PROTOCOL_BREACH"

    elif event_type == "PROTOCOL_BREACH_RECORDED":
        candidate, prior_state = _date_state(event, state)
        linked_attempt_sequence = event.get("linked_attempt_sequence")
        if linked_attempt_sequence is not None:
            duplicate_attempt = state.duplicate_attempts.get(linked_attempt_sequence)
            if (
                duplicate_attempt is None
                or duplicate_attempt.get("candidate_date") != candidate
                or duplicate_attempt.get("attempt_outcome") != "DUPLICATE_TARGET_DATE"
                or duplicate_attempt.get("evidence_sha256") != event.get("evidence_sha256")
                or _event_time(duplicate_attempt) != occurred
            ):
                raise ContractViolation(
                    "linked duplicate breach must reproduce its FETCH_ATTEMPT evidence"
                )
        if prior_state == "EXPECTED_ACCEPTED" and linked_attempt_sequence is not None:
            breach_civil_date = _invalidate_global_revision(state, occurred)
        elif prior_state in TERMINAL_DATE_STATES:
            raise ContractViolation(f"terminal date state {prior_state} cannot transition")
        elif prior_state not in {"NON_SESSION", "EXPECTED_PENDING"}:
            raise ContractViolation("protocol breach cannot transition this date state")
        else:
            breach_civil_date = _invalidate_at_boundary(state, occurred)
            state.states[candidate] = "PROTOCOL_BREACH"
            state.pending_valid_attempt_sequence = None
            next_state = "PROTOCOL_BREACH"

    elif event_type == "FETCH_ATTEMPT":
        candidate, prior_state = _date_state(event, state)
        if event.get("attempt_outcome") not in ATTEMPT_OUTCOMES:
            raise ContractViolation("FETCH_ATTEMPT has an invalid attempt_outcome")
        raw_identity = event.get("raw_byte_sha256")
        if raw_identity is not None:
            captured_length = state.artifact_byte_lengths.get(raw_identity)
            if captured_length is None:
                raise ContractViolation("captured response byte length identity is missing")
            if captured_length != event["response_byte_length"]:
                raise ContractViolation("declared response byte length does not match captured bytes")
        history_findings = _history_comparison_findings(event, state)
        if history_findings:
            state.pending_history_revision_sequence = sequence
            state.pending_history_revision_findings = history_findings
        if (
            event["attempt_outcome"] == "DUPLICATE_TARGET_DATE"
            and prior_state in {"EXPECTED_PENDING", "EXPECTED_ACCEPTED"}
        ):
            state.duplicate_attempts[sequence] = event
        if event["attempt_outcome"] == "VALID_TARGET_ROW" and prior_state == "EXPECTED_PENDING":
            candidate_date = _parse_date(event["candidate_date"])
            close_available = datetime.combine(candidate_date, time(15, 45), tzinfo=SHANGHAI)
            captured_at = _parse_timestamp(event["response_at"], "VALID_TARGET_ROW response_at")
            if captured_at < close_available:
                raise ContractViolation(
                    "VALID_TARGET_ROW response cannot precede D 15:45 close availability"
                )
            assigned = state.max_ordinal + 1
            deadline, _, _ = _acceptance_deadline(candidate_date, assigned, state)
            if (assigned <= 251 and captured_at >= deadline) or (
                assigned == 252 and captured_at > deadline
            ):
                raise ContractViolation("VALID_TARGET_ROW response is outside its capture deadline")
            state.valid_attempts[sequence] = event
            state.pending_valid_attempt_sequence = sequence
        if (
            not history_findings
            and
            prior_state == "EXPECTED_ACCEPTED"
            and event["attempt_outcome"] in ACCEPTED_REVISION_OUTCOMES
        ):
            state.accepted_attempts[sequence] = event
            state.pending_accepted_attempt_sequence = sequence
            if (
                event["attempt_outcome"] == "VALID_TARGET_ROW"
                and event["parsed_row_sha256"] == state.canonical_rows[candidate]
            ):
                state.pending_accepted_attempt_event_type = "IDENTICAL_DUPLICATE_OBSERVED"
            else:
                state.pending_accepted_attempt_event_type = "SOURCE_REVISION_OBSERVED"

    elif event_type == "SOURCE_REVISION_OBSERVED":
        revision_scope = event["revision_scope"]
        if "comparison_findings" in event:
            _, prior_state = _date_state(event, state)
            _consume_history_revision_link(event, state)
        elif "candidate_date" in event:
            _, prior_state = _date_state(event, state)
        if "linked_attempt_sequence" in event and "comparison_findings" not in event:
            attempt = _consume_accepted_attempt_link(event, state)
            if attempt["attempt_outcome"] not in ACCEPTED_REVISION_OUTCOMES:
                raise ContractViolation("linked accepted-date attempt is not a source revision")
        if (
            revision_scope == "ACCEPTED_EVALUATION_DATA"
            and "comparison_findings" not in event
            and prior_state != "EXPECTED_ACCEPTED"
        ):
            raise ContractViolation(
                "ACCEPTED_EVALUATION_DATA revision must name an accepted candidate_date"
            )
        if event["touches_evaluation_data"] and not state.invalidated:
            breach_civil_date = _invalidate_global_revision(state, occurred)

    elif event_type == "IDENTICAL_DUPLICATE_OBSERVED":
        candidate, prior_state = _date_state(event, state)
        if prior_state != "EXPECTED_ACCEPTED" or candidate not in state.canonical_rows:
            raise ContractViolation(
                "identical duplicate observation requires an accepted canonical row"
            )
        if event["canonical_row_sha256"] != state.canonical_rows[candidate]:
            raise ContractViolation("duplicate canonical row hash does not match the accepted baseline")
        if event["observed_row_sha256"] != event["canonical_row_sha256"]:
            raise ContractViolation("duplicate observed and canonical row hashes must be identical")
        if "linked_attempt_sequence" in event:
            attempt = _consume_accepted_attempt_link(event, state)
            if (
                attempt["attempt_outcome"] != "VALID_TARGET_ROW"
                or attempt["parsed_row_sha256"] != state.canonical_rows[candidate]
            ):
                raise ContractViolation("linked accepted-date attempt is not an identical duplicate")

    elif event_type == "CORRECTION":
        candidate, prior_state = _date_state(event, state)
        superseded_sequence = _validate_exact_integer(
            event["superseded_sequence"],
            "CORRECTION superseded_sequence",
            minimum=1,
        )
        superseded = state.sealed_records.get(superseded_sequence)
        if superseded is None or superseded_sequence >= sequence:
            raise ContractViolation("CORRECTION superseded record sequence does not exist")
        if event["superseded_record_sha256"] != superseded["record_sha256"]:
            raise ContractViolation("CORRECTION superseded record hash does not reproduce")
        if superseded.get("candidate_date") != candidate:
            raise ContractViolation("CORRECTION candidate does not match its superseded record")
        superseded_identities = {
            superseded["record_sha256"],
            *superseded.get("evidence_sha256", []),
        }
        if event["old_value_sha256"] not in superseded_identities:
            raise ContractViolation("CORRECTION old value is not bound to the superseded record")
        if event["decision_surface_before_sha256"] not in superseded_identities:
            raise ContractViolation(
                "CORRECTION prior decision surface is not bound to the superseded record"
            )
        if (
            event["correction_scope"] == "CLERICAL_METADATA"
            and superseded["event_type"] != "OPERATOR_ACCESS"
        ):
            raise ContractViolation(
                "clerical CORRECTION cannot supersede a decision-bearing ledger record"
            )
        if (
            event["correction_scope"] == "EVALUATION_MARKET_DATA"
            and prior_state != "EXPECTED_ACCEPTED"
        ):
            raise ContractViolation(
                "evaluation-market-data CORRECTION must name an accepted candidate"
            )
        if event["correction_scope"] != "CLERICAL_METADATA" and not state.invalidated:
            breach_civil_date = _invalidate_global_revision(state, occurred)

    elif event_type == "OPERATOR_ACCESS":
        candidate, prior_state = _date_state(event, state)
        diagnostic_sequence = _validate_exact_integer(
            event["linked_diagnostic_sequence"],
            "OPERATOR_ACCESS linked_diagnostic_sequence",
            minimum=1,
        )
        diagnostic = state.sealed_records.get(diagnostic_sequence)
        if diagnostic is None or diagnostic_sequence >= sequence:
            raise ContractViolation("OPERATOR_ACCESS must link an existing eligible diagnostic")
        if diagnostic.get("record_sha256") != event["diagnostic_record_sha256"]:
            raise ContractViolation("OPERATOR_ACCESS diagnostic record does not reproduce")
        expected_purpose = _diagnostic_purpose(diagnostic)
        if expected_purpose is None:
            raise ContractViolation("OPERATOR_ACCESS link is not an eligible diagnostic")
        if event["diagnostic_purpose"] != expected_purpose:
            raise ContractViolation("OPERATOR_ACCESS diagnostic purpose does not reproduce")
        if diagnostic.get("candidate_date") != candidate:
            raise ContractViolation("OPERATOR_ACCESS diagnostic candidate does not reproduce")

    elif event_type == "SILENT_MISS":
        _, prior_state = _date_state(event, state)

    elif event_type == "EXTERNAL_PRODUCTION_CHANGE":
        if "candidate_date" in event:
            _, prior_state = _date_state(event, state)

    elif event_type == "FINALIZED":
        if state.max_ordinal != 252:
            raise ContractViolation("FINALIZED requires exactly 252 accepted ordinals")
        if (
            state.terminal_coverage_date is None
            or not state.declared_dates
            or state.declared_dates[-1].isoformat() != state.terminal_coverage_date
        ):
            raise ContractViolation("FINALIZED requires exact successful terminal coverage")
        state.finalized = True

    if prior_state is None and event_type != "DATE_DECLARED" and "candidate_date" in event:
        candidate = event["candidate_date"]
        prior_state = state.states.get(candidate)

    derived: dict[str, Any] = {
        "prior_state": prior_state,
        "next_state": next_state,
    }
    if event_type == "DATE_DECLARED":
        derived.update(calendar_derived)
    if event_type == "CANONICAL_ROW_ACCEPTED":
        derived.update(calendar_derived)
    if deadline_field is not None and canonical_deadline is not None:
        derived[deadline_field] = canonical_deadline
    if expected_ordinal is not None:
        derived["expected_ordinal"] = expected_ordinal
    if breach_civil_date is not None:
        derived["breach_civil_date"] = breach_civil_date
    if terminal_coverage_date is not None:
        derived["terminal_coverage_date"] = terminal_coverage_date
    state.last_event_at = occurred
    return event, derived


def _genesis_hash(genesis_document: Mapping[str, Any]) -> str:
    if not isinstance(genesis_document, Mapping):
        raise ContractViolation("genesis document must be a canonical JSON object")
    return hashlib.sha256(canonical_json_bytes(genesis_document)).hexdigest()


def _state_for_genesis(
    genesis_document: Mapping[str, Any],
    authoritative_calendar: Mapping[str, AuthoritativeSessionFacts],
    available: set[str],
    artifact_byte_lengths: Mapping[str, int],
) -> _LedgerState:
    if not isinstance(genesis_document, Mapping):
        raise ContractViolation("genesis document must be a canonical JSON object")
    sealed_at = _parse_timestamp(genesis_document.get("sealed_at"), "genesis sealed_at")
    if "warmup_row_sha256s" not in genesis_document:
        raise ContractViolation("genesis requires frozen warmup_row_sha256s")
    raw_warmup = genesis_document["warmup_row_sha256s"]
    if not isinstance(raw_warmup, Mapping):
        raise ContractViolation("genesis warmup_row_sha256s must be an exact date/hash map")
    warmup_rows: dict[str, str] = {}
    first_candidate = sealed_at.astimezone(SHANGHAI).date() + timedelta(days=1)
    for raw_date, raw_identity in raw_warmup.items():
        candidate = _parse_date(raw_date, "warm-up row date")
        if candidate >= first_candidate:
            raise ContractViolation("warm-up rows must predate the first candidate date")
        identity = _validate_sha256(raw_identity, "warm-up row identity")
        if identity not in available:
            raise ContractViolation("warm-up row identity must name available evidence")
        warmup_rows[candidate.isoformat()] = identity
    calendar = _calendar_decisions(authoritative_calendar, available)
    initialization = genesis_document.get("initialization_seal")
    if not isinstance(initialization, Mapping) or set(initialization) != INITIALIZATION_SEAL_FIELDS:
        raise ContractViolation("genesis requires an exact evidence-bound initialization seal")
    initialization_sealed_at = _parse_timestamp(
        initialization["sealed_at"],
        "initialization seal sealed_at",
    )
    if initialization_sealed_at < sealed_at:
        raise ContractViolation("initialization seal cannot predate the genesis seal")
    for field_name in INITIALIZATION_SEAL_FIELDS - {"sealed_at"}:
        identity = _validate_sha256(
            initialization[field_name],
            f"initialization seal {field_name}",
        )
        if identity not in available:
            raise ContractViolation(
                f"initialization seal {field_name} must name a referenced identity"
            )
    s1_date = first_candidate
    while True:
        decision = calendar.get(s1_date.isoformat())
        if decision is None:
            raise ContractViolation("authoritative calendar must be contiguous through S1")
        if decision.status is CalendarStatus.EXPECTED_SESSION:
            assert decision.open_boundary is not None
            if initialization_sealed_at >= decision.open_boundary:
                raise ContractViolation(
                    "initialization seal must be strictly before the authoritative S1 open"
                )
            break
        s1_date += timedelta(days=1)
    if not isinstance(artifact_byte_lengths, Mapping):
        raise ContractViolation("artifact byte lengths are required at the package seam")
    normalized_lengths: dict[str, int] = {}
    for raw_identity, raw_length in artifact_byte_lengths.items():
        identity = _validate_sha256(raw_identity, "artifact byte-length identity")
        if identity not in available:
            raise ContractViolation("artifact byte-length identity must name available evidence")
        normalized_lengths[identity] = _validate_exact_integer(
            raw_length,
            "artifact byte length",
            minimum=0,
        )
    return _LedgerState(
        expected_first_date=first_candidate,
        calendar=calendar,
        genesis_sealed_at=sealed_at,
        warmup_rows=dict(sorted(warmup_rows.items())),
        artifact_byte_lengths=normalized_lengths,
    )


def _record_hash(record: Mapping[str, Any]) -> str:
    logical = {
        key: value
        for key, value in record.items()
        if key not in {"record_sha256", "chain_sha256"}
    }
    return hashlib.sha256(canonical_json_bytes(logical)).hexdigest()


def _chain_hash(previous_chain: str, record_sha256: str) -> str:
    _validate_sha256(previous_chain, "previous chain")
    _validate_sha256(record_sha256, "record")
    return hashlib.sha256(bytes.fromhex(previous_chain) + bytes.fromhex(record_sha256)).hexdigest()


def seal_ledger(
    events: Iterable[Mapping[str, Any]],
    genesis_document: Mapping[str, Any],
    available_artifact_sha256: Iterable[str],
    authoritative_calendar: Mapping[str, AuthoritativeSessionFacts],
    artifact_byte_lengths: Mapping[str, int] | None = None,
) -> tuple[bytes, ...]:
    """Validate events, derive transitions, and seal canonical checksum-chain records."""

    available = {
        _validate_sha256(value, "available artifact") for value in available_artifact_sha256
    }
    state = _state_for_genesis(
        genesis_document,
        authoritative_calendar,
        available,
        artifact_byte_lengths or {},
    )
    previous_chain = _genesis_hash(genesis_document)
    records: list[bytes] = []

    def append_event(supplied: Mapping[str, Any], *, generated: bool = False) -> dict[str, Any]:
        nonlocal previous_chain
        if supplied.get("event_type") == "SILENT_MISS" and not generated:
            raise ContractViolation("SILENT_MISS is generated from its linked FETCH_ATTEMPT")
        if (
            supplied.get("event_type") == "PROTOCOL_BREACH_RECORDED"
            and "linked_attempt_sequence" in supplied
            and not generated
        ):
            raise ContractViolation("linked duplicate breach is generated from its FETCH_ATTEMPT")
        if (
            supplied.get("event_type") == "SOURCE_REVISION_OBSERVED"
            and "comparison_findings" in supplied
            and not generated
        ):
            raise ContractViolation("full-history revision is generated from its FETCH_ATTEMPT")
        event = _normalize_event(supplied, available)
        sequence = len(records) + 1
        event, derived = _process_event(
            event,
            state,
            convert_late_acceptance=True,
            sequence=sequence,
        )
        record = {
            **event,
            **derived,
            "sequence": sequence,
            "previous_chain_sha256": previous_chain,
        }
        record_sha256 = _record_hash(record)
        chain_sha256 = _chain_hash(previous_chain, record_sha256)
        record["record_sha256"] = record_sha256
        record["chain_sha256"] = chain_sha256
        records.append(canonical_json_bytes(record))
        state.sealed_records[sequence] = dict(record)
        previous_chain = chain_sha256
        return record

    for supplied in events:
        normalized = _normalize_event(supplied, available)
        overdue = _overdue_pending_deadline(normalized, state)
        if (
            overdue is not None
            and normalized["event_type"] == "PROTOCOL_BREACH_RECORDED"
            and normalized.get("candidate_date") == overdue[0]
        ):
            raise ContractViolation(
                "only the derived DEADLINE_EXPIRED event may close a reached deadline"
            )
        supplied_is_breach = (
            overdue is not None
            and normalized["event_type"] == "DEADLINE_EXPIRED"
            and normalized.get("candidate_date") == overdue[0]
        )
        if overdue is not None and not supplied_is_breach:
            overdue_candidate, _, overdue_at, _ = overdue
            append_event(
                {
                    "event_type": "DEADLINE_EXPIRED",
                    "candidate_date": overdue_candidate,
                    "event_at": overdue_at,
                    "reason": "event time proved the canonical acceptance deadline was reached",
                    "evidence_sha256": normalized["evidence_sha256"],
                },
                generated=True,
            )
            if normalized["event_type"] == "CANONICAL_ROW_ACCEPTED":
                continue
        record = append_event(supplied)
        if state.pending_history_revision_sequence == record["sequence"]:
            findings = state.pending_history_revision_findings or []
            linked_evidence = list(record["evidence_sha256"])
            for finding in findings:
                for identity in (
                    finding["baseline_row_sha256"],
                    finding["observed_row_sha256"],
                ):
                    if identity is not None and identity not in linked_evidence:
                        linked_evidence.append(identity)
            assert state.expected_first_date is not None
            revision_scope = (
                "INITIALIZATION_DATA"
                if any(
                    _parse_date(finding["candidate_date"]) < state.expected_first_date
                    for finding in findings
                )
                else "ACCEPTED_EVALUATION_DATA"
            )
            append_event(
                {
                    "event_type": "SOURCE_REVISION_OBSERVED",
                    "candidate_date": record["candidate_date"],
                    "event_at": record["event_at_asia_shanghai"],
                    "linked_attempt_sequence": record["sequence"],
                    "comparison_findings": findings,
                    "revision_scope": revision_scope,
                    "touches_evaluation_data": True,
                    "reason": "full-history comparison found a decision-bearing source revision",
                    "evidence_sha256": linked_evidence,
                },
                generated=True,
            )
        newly_overdue = _overdue_pending_deadline(record, state)
        if newly_overdue is not None:
            overdue_candidate, _, _, _ = newly_overdue
            append_event(
                {
                    "event_type": "DEADLINE_EXPIRED",
                    "candidate_date": overdue_candidate,
                    "event_at": record["event_at_asia_shanghai"],
                    "reason": "new pending state was created at or after its acceptance deadline",
                    "evidence_sha256": record["evidence_sha256"],
                },
                generated=True,
            )
        if _failed_attempt_requires_silent_miss(record):
            append_event(
                {
                    "event_type": "SILENT_MISS",
                    "candidate_date": record["candidate_date"],
                    "event_at": record["event_at_asia_shanghai"],
                    "linked_attempt_sequence": record["sequence"],
                    "reason": "post-15:45 attempt did not contain a valid target row",
                    "evidence_sha256": record["evidence_sha256"],
                },
                generated=True,
            )
        if (
            record["event_type"] == "FETCH_ATTEMPT"
            and record.get("attempt_outcome") == "DUPLICATE_TARGET_DATE"
            and record.get("prior_state") in {"EXPECTED_PENDING", "EXPECTED_ACCEPTED"}
        ):
            append_event(
                {
                    "event_type": "PROTOCOL_BREACH_RECORDED",
                    "candidate_date": record["candidate_date"],
                    "event_at": record["event_at_asia_shanghai"],
                    "linked_attempt_sequence": record["sequence"],
                    "breach_reason": "within-response duplicate target date",
                    "reason": "duplicate target row is an irreparable date-level breach",
                    "evidence_sha256": record["evidence_sha256"],
                },
                generated=True,
            )
        if state.pending_accepted_attempt_sequence == record["sequence"]:
            candidate = record["candidate_date"]
            linked_evidence = list(
                dict.fromkeys(
                    [
                        *record["evidence_sha256"],
                        state.canonical_raws[candidate],
                        state.canonical_rows[candidate],
                    ]
                )
            )
            linked_event = {
                "event_type": state.pending_accepted_attempt_event_type,
                "candidate_date": candidate,
                "event_at": record["event_at_asia_shanghai"],
                "linked_attempt_sequence": record["sequence"],
                "baseline_raw_sha256": state.canonical_raws[candidate],
                "canonical_row_sha256": state.canonical_rows[candidate],
                "observed_raw_sha256": record["raw_byte_sha256"],
                "observed_row_sha256": record["parsed_row_sha256"],
                "observed_outcome": record["attempt_outcome"],
                "evidence_sha256": linked_evidence,
            }
            if state.pending_accepted_attempt_event_type == "IDENTICAL_DUPLICATE_OBSERVED":
                linked_event["reason"] = (
                    "later full-history capture reproduced the accepted canonical row"
                )
            else:
                linked_event.update(
                    {
                        "revision_scope": "ACCEPTED_EVALUATION_DATA",
                        "touches_evaluation_data": True,
                        "reason": (
                            "later full-history capture changed or omitted an accepted row"
                        ),
                    }
                )
            append_event(linked_event, generated=True)
    if state.pending_valid_attempt_sequence is not None:
        raise ContractViolation(
            "ledger ends before VALID_TARGET_ROW is linked to canonical acceptance"
        )
    if state.pending_accepted_attempt_sequence is not None:
        raise ContractViolation(
            "ledger ends before accepted-date FETCH_ATTEMPT is linked to duplicate or revision"
        )
    if state.pending_history_revision_sequence is not None:
        raise ContractViolation(
            "ledger ends before full-history FETCH_ATTEMPT is linked to source revision"
        )
    if not records:
        raise ContractViolation("ledger must contain at least one event")
    return tuple(records)


def _base_event_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    replay_generated = {
        "sequence",
        "prior_state",
        "next_state",
        "expected_ordinal",
        "previous_chain_sha256",
        "record_sha256",
        "chain_sha256",
        "execution_seal_deadline",
        "recovery_deadline",
        "calendar_status",
        "calendar_evidence_sha256",
        "open_boundary_asia_shanghai",
        "open_boundary_utc",
        "breach_civil_date",
        "terminal_coverage_date",
        "canonical_raw_sha256",
        "capture_at",
    }
    if record.get("event_type") == "CANONICAL_ROW_ACCEPTED":
        replay_generated.add("linked_attempt_sequence")
    return {key: value for key, value in record.items() if key not in replay_generated}


def replay_ledger(
    records: Sequence[bytes],
    genesis_document: Mapping[str, Any],
    available_artifact_sha256: Iterable[str],
    authoritative_calendar: Mapping[str, AuthoritativeSessionFacts],
    artifact_byte_lengths: Mapping[str, int] | None = None,
) -> LedgerReplay:
    """Reconstruct and verify all ledger state exclusively from sealed bytes."""

    available = {
        _validate_sha256(value, "available artifact") for value in available_artifact_sha256
    }
    if not records:
        raise ContractViolation("ledger replay requires at least one record")
    state = _state_for_genesis(
        genesis_document,
        authoritative_calendar,
        available,
        artifact_byte_lengths or {},
    )
    previous_chain = _genesis_hash(genesis_document)
    expected_silent_miss: int | None = None
    expected_duplicate_breach: int | None = None
    for expected_sequence, raw in enumerate(records, start=1):
        record = _parse_canonical_record(raw)
        _validate_generated_record_types(record)
        if record.get("sequence") != expected_sequence:
            raise ContractViolation("ledger sequence must be contiguous from one")
        if record.get("previous_chain_sha256") != previous_chain:
            raise ContractViolation("ledger previous chain hash does not match")
        supplied_record_hash = _validate_sha256(record.get("record_sha256"), "record")
        if supplied_record_hash != _record_hash(record):
            raise ContractViolation("ledger record hash does not match canonical bytes")
        supplied_chain_hash = _validate_sha256(record.get("chain_sha256"), "chain")
        if supplied_chain_hash != _chain_hash(previous_chain, supplied_record_hash):
            raise ContractViolation("ledger chain hash does not match")
        _normalize_evidence(record, available)

        if expected_silent_miss is not None:
            if (
                record.get("event_type") != "SILENT_MISS"
                or record.get("linked_attempt_sequence") != expected_silent_miss
            ):
                raise ContractViolation("post-15:45 failed attempt requires a linked SILENT_MISS")
            expected_silent_miss = None
        elif expected_duplicate_breach is not None:
            if (
                record.get("event_type") != "PROTOCOL_BREACH_RECORDED"
                or record.get("linked_attempt_sequence") != expected_duplicate_breach
            ):
                raise ContractViolation(
                    "duplicate target attempt requires its linked protocol breach"
                )
            expected_duplicate_breach = None
        elif record.get("event_type") == "SILENT_MISS":
            raise ContractViolation("SILENT_MISS does not name a pending failed attempt")

        base = _validate_record_event(record, available)
        reconstructed_event, derived = _process_event(
            base,
            state,
            convert_late_acceptance=False,
            sequence=expected_sequence,
        )
        if reconstructed_event != base:
            raise ContractViolation("replay changed sealed event semantics")
        present_derived_fields = DERIVED_RECORD_FIELDS.intersection(record)
        if record.get("event_type") != "CANONICAL_ROW_ACCEPTED":
            present_derived_fields.discard("linked_attempt_sequence")
        required_derived_fields = set(derived)
        if present_derived_fields != required_derived_fields:
            missing = sorted(required_derived_fields - present_derived_fields)
            unexpected = sorted(present_derived_fields - required_derived_fields)
            detail = missing[0] if missing else unexpected[0]
            raise ContractViolation(f"sealed record has invalid generated field presence: {detail}")
        sealed_derived = {field_name: record[field_name] for field_name in present_derived_fields}
        if derived != sealed_derived:
            if record.get("event_type") == "CANONICAL_ROW_ACCEPTED" and record.get(
                "linked_attempt_sequence"
            ) != derived.get("linked_attempt_sequence"):
                raise ContractViolation("canonical acceptance attempt link does not reproduce")
            raise ContractViolation("replay did not reproduce state transition or ordinal")
        state.sealed_records[expected_sequence] = dict(record)
        if _failed_attempt_requires_silent_miss(record) and (
            state.pending_history_revision_sequence != record["sequence"]
        ):
            expected_silent_miss = expected_sequence
        if (
            record["event_type"] == "FETCH_ATTEMPT"
            and record.get("attempt_outcome") == "DUPLICATE_TARGET_DATE"
            and record.get("prior_state") in {"EXPECTED_PENDING", "EXPECTED_ACCEPTED"}
            and state.pending_history_revision_sequence != record["sequence"]
        ):
            expected_duplicate_breach = expected_sequence
        if record["event_type"] == "SOURCE_REVISION_OBSERVED" and "comparison_findings" in record:
            linked_attempt = state.sealed_records.get(record["linked_attempt_sequence"])
            if linked_attempt is None:
                raise ContractViolation("linked full-history attempt is missing")
            if _failed_attempt_requires_silent_miss(linked_attempt):
                expected_silent_miss = linked_attempt["sequence"]
            if (
                linked_attempt.get("attempt_outcome") == "DUPLICATE_TARGET_DATE"
                and linked_attempt.get("prior_state")
                in {"EXPECTED_PENDING", "EXPECTED_ACCEPTED"}
            ):
                expected_duplicate_breach = linked_attempt["sequence"]
        previous_chain = supplied_chain_hash

    if expected_silent_miss is not None:
        raise ContractViolation("ledger ends before the required linked SILENT_MISS")
    if expected_duplicate_breach is not None:
        raise ContractViolation("ledger ends before the duplicate target attempt's linked breach")
    if state.pending_valid_attempt_sequence is not None:
        raise ContractViolation(
            "ledger ends before VALID_TARGET_ROW is linked to canonical acceptance"
        )
    if state.pending_accepted_attempt_sequence is not None:
        raise ContractViolation(
            "ledger ends before accepted-date FETCH_ATTEMPT is linked to duplicate or revision"
        )
    if state.pending_history_revision_sequence is not None:
        raise ContractViolation(
            "ledger ends before full-history FETCH_ATTEMPT is linked to source revision"
        )
    last_record = _parse_canonical_record(records[-1])
    if _overdue_pending_deadline(last_record, state) is not None:
        raise ContractViolation("ledger ends with a pending deadline reached without expiry")

    terminal_class = "INVALIDATED" if state.invalidated else None
    return LedgerReplay(
        states=dict(state.states),
        ordinals=dict(state.ordinals),
        deadlines=dict(state.deadlines),
        max_ordinal=state.max_ordinal,
        terminal_class=terminal_class,
        breach_civil_date=state.breach_civil_date,
        terminal_coverage_date=state.terminal_coverage_date,
        final_chain_sha256=previous_chain,
    )


def run_fixture(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Run the complete offline calendar -> seal -> replay acceptance seam."""

    if not isinstance(fixture, Mapping):
        raise ContractViolation("fixture must be a JSON object")
    calendar_input = fixture.get("calendar", [])
    if not isinstance(calendar_input, list):
        raise ContractViolation("fixture calendar must be a list")
    calendar_results = []
    calendar_states: dict[str, str] = {}
    authoritative_calendar: dict[str, AuthoritativeSessionFacts] = {}
    for item in calendar_input:
        if not isinstance(item, Mapping) or not isinstance(item.get("facts"), Mapping):
            raise ContractViolation("calendar fixture entries require session_date and facts")
        facts = _validated_authoritative_facts(item["facts"], label="calendar fixture")
        decision = classify_session(_parse_date(item.get("session_date"), "session_date"), facts)
        if decision.status is CalendarStatus.AMBIGUOUS_BLOCKED:
            raise ContractViolation(f"calendar date {item['session_date']} is ambiguous and blocked")
        ledger_status = {
            CalendarStatus.EXPECTED_SESSION: "EXPECTED_PENDING",
            CalendarStatus.NON_SESSION: "NON_SESSION",
            CalendarStatus.OFFICIAL_FULL_CLOSURE: "OFFICIAL_FULL_CLOSURE",
        }[decision.status]
        if item["session_date"] in calendar_states:
            raise ContractViolation("calendar fixture dates must be unique")
        calendar_states[item["session_date"]] = ledger_status
        authoritative_calendar[item["session_date"]] = facts
        calendar_results.append(
            {
                "session_date": item["session_date"],
                "status": decision.status.value,
                "open_boundary_asia_shanghai": (
                    decision.open_boundary.isoformat() if decision.open_boundary else None
                ),
                "open_boundary_utc": (
                    _canonical_utc_timestamp(decision.open_boundary)
                    if decision.open_boundary
                    else None
                ),
                "evidence_sha256": list(decision.evidence_sha256),
            }
        )

    events = fixture.get("events")
    available = fixture.get("available_artifact_sha256")
    artifact_byte_lengths = fixture.get("artifact_byte_lengths")
    genesis = fixture.get("genesis")
    if (
        not isinstance(events, list)
        or not isinstance(available, list)
        or not isinstance(artifact_byte_lengths, Mapping)
    ):
        raise ContractViolation(
            "fixture requires events, available identities, and artifact byte lengths"
        )
    if not isinstance(genesis, Mapping):
        raise ContractViolation("fixture requires a genesis document")
    for event in events:
        if isinstance(event, Mapping) and event.get("event_type") == "DATE_DECLARED":
            candidate = event.get("candidate_date")
            if candidate not in calendar_states:
                raise ContractViolation("every declared date requires an authoritative calendar decision")
            if event.get("initial_status") != calendar_states[candidate]:
                raise ContractViolation("DATE_DECLARED status contradicts its calendar decision")
    records = seal_ledger(
        events,
        genesis,
        available,
        authoritative_calendar,
        artifact_byte_lengths,
    )
    replay = replay_ledger(
        records,
        genesis,
        available,
        authoritative_calendar,
        artifact_byte_lengths,
    )
    return {
        "calendar": calendar_results,
        "records": [_parse_canonical_record(record) for record in records],
        "replay": asdict(replay),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an offline prospective-evidence fixture")
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args(argv)
    try:
        fixture = json.loads(
            args.fixture.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
        result = run_fixture(fixture)
        output = canonical_json_bytes(result).decode("utf-8")
    except (ContractViolation, OSError, json.JSONDecodeError) as exc:
        print(f"contract violation: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
