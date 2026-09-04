from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest

from gold_research.prospective_evidence import (
    EVENT_FIELD_SCHEMAS,
    EVENT_TYPES,
    AuthoritativeSessionFacts,
    CalendarStatus,
    ContractViolation,
    canonical_json_bytes,
    classify_session,
    replay_ledger as _replay_ledger,
    run_fixture,
    seal_ledger as _seal_ledger,
)

CALENDAR_SOURCE_SHA256 = "7846f46179038abfed539682acfb88640fcaf653e4c0ef225d24b0e519244cf5"
TRADING_HOURS_SHA256 = "990132d23319344ba0291639a9733de39532d871ab808f996cac135184a985d0"
HOLIDAY_2025_SHA256 = "670c7fc46db7af05ae0d328550bb6d574795565f8e56ba3ca91b7ae6f3e3f437"
ROW_SHA256 = "1" * 64
BUILD_SHA256 = "2" * 64
ALTERNATE_ROW_SHA256 = "3" * 64
RESPONSE_HEADERS = {"content-type": "text/tab-separated-values; charset=gbk"}
RESPONSE_HEADERS_SHA256 = "5717015fd01e1a468ace57eacba66f827ac594ecd8a8a4a7ca59e49350c3f954"
AVAILABLE = {
    CALENDAR_SOURCE_SHA256,
    TRADING_HOURS_SHA256,
    HOLIDAY_2025_SHA256,
    ROW_SHA256,
    BUILD_SHA256,
    ALTERNATE_ROW_SHA256,
    RESPONSE_HEADERS_SHA256,
}
ARTIFACT_BYTE_LENGTHS = {identity: 128 for identity in AVAILABLE}
INITIALIZATION_SEAL = {
    "sealed_at": "2025-10-12T01:00:00Z",
    "pre_s1_replay_sha256": ROW_SHA256,
    "initial_target_sha256": BUILD_SHA256,
    "intended_s1_position_sha256": ROW_SHA256,
    "quantity_inputs_sha256": BUILD_SHA256,
}
GENESIS = {
    "contract": "synthetic-offline-fixture",
    "sealed_at": "2025-10-12T00:00:00Z",
    "warmup_row_sha256s": {},
    "initialization_seal": INITIALIZATION_SEAL,
}


def facts(**changes) -> AuthoritativeSessionFacts:
    values = {
        "evidence_sha256": (CALENDAR_SOURCE_SHA256, TRADING_HOURS_SHA256),
        "sources_complete": True,
        "full_closure": False,
        "exceptional_opening": False,
        "exceptional_open_time": None,
        "no_prior_night": False,
        "prior_night_cancelled": False,
        "remaining_open_time": None,
        "conflicting": False,
        "ambiguous": False,
    }
    values.update(changes)
    return AuthoritativeSessionFacts(**values)


def fact_mapping(**changes) -> dict:
    values = {
        "evidence_sha256": [CALENDAR_SOURCE_SHA256, TRADING_HOURS_SHA256],
        "sources_complete": True,
        "full_closure": False,
        "exceptional_opening": False,
        "exceptional_open_time": None,
        "no_prior_night": False,
        "prior_night_cancelled": False,
        "remaining_open_time": None,
        "conflicting": False,
        "ambiguous": False,
    }
    values.update(changes)
    return values


def _test_calendar(items, genesis=GENESIS):
    materialized = [json.loads(item) if isinstance(item, bytes) else item for item in items]
    declarations = {
        item["candidate_date"]: item.get("initial_status")
        for item in materialized
        if item.get("event_type") == "DATE_DECLARED" and "candidate_date" in item
    }
    first = date.fromisoformat(genesis["sealed_at"][:10]) + timedelta(days=1)
    named_dates = [date.fromisoformat(value) for value in declarations]
    through = max(named_dates, default=first) + timedelta(days=10)
    calendar = {}
    candidate = first
    while candidate <= through:
        status = declarations.get(candidate.isoformat())
        if status == "OFFICIAL_FULL_CLOSURE":
            session_facts = facts(full_closure=True)
        elif status == "EXPECTED_PENDING" and candidate.weekday() >= 5:
            session_facts = facts(exceptional_opening=True, exceptional_open_time="09:00:00")
        else:
            session_facts = facts(
                no_prior_night=(candidate == first and candidate.weekday() == 0)
            )
        calendar[candidate.isoformat()] = session_facts
        candidate += timedelta(days=1)
    return calendar


def seal_ledger(events, genesis, available, calendar=None):
    materialized = list(events)
    genesis = {
        **genesis,
        "warmup_row_sha256s": genesis.get("warmup_row_sha256s", {}),
        "initialization_seal": genesis.get(
            "initialization_seal",
            {**INITIALIZATION_SEAL, "sealed_at": genesis["sealed_at"]},
        ),
    }
    authoritative_calendar = _test_calendar(materialized, genesis)
    if calendar is not None:
        authoritative_calendar.update(calendar)
    expanded = []
    accepted_count = 0
    accepted_rows = dict(genesis.get("warmup_row_sha256s", {}))
    declared_candidates = set()
    for event in materialized:
        if event.get("event_type") == "FETCH_ATTEMPT" and isinstance(
            event.get("history_row_sha256s"),
            dict,
        ):
            event = dict(event)
            history_rows = dict(event["history_row_sha256s"])
            for accepted_date, accepted_hash in accepted_rows.items():
                if accepted_date != event.get("candidate_date"):
                    history_rows.setdefault(accepted_date, accepted_hash)
            event["history_row_sha256s"] = history_rows
        if event.get("event_type") == "DATE_DECLARED":
            declared_candidates.add(event.get("candidate_date"))
        if event.get("event_type") == "CANONICAL_ROW_ACCEPTED":
            assigned = accepted_count + 1
            candidate = date.fromisoformat(event["candidate_date"])
            following = candidate + timedelta(days=1)
            while True:
                decision = classify_session(following, authoritative_calendar[following.isoformat()])
                if decision.status is CalendarStatus.EXPECTED_SESSION:
                    break
                following += timedelta(days=1)
            assert decision.open_boundary is not None
            if assigned <= 251:
                deadline = decision.open_boundary
            else:
                deadline = datetime.combine(
                    following,
                    time(15, 45),
                    tzinfo=decision.open_boundary.tzinfo,
                )
            occurred = datetime.fromisoformat(event["event_at"].replace("Z", "+00:00"))
            on_time = occurred < deadline if assigned <= 251 else occurred <= deadline
            if assigned <= 251 and occurred.date() != candidate:
                on_time = False
            if candidate.isoformat() not in declared_candidates:
                on_time = False
            already_has_attempt = (
                bool(expanded)
                and expanded[-1].get("event_type") == "FETCH_ATTEMPT"
                and expanded[-1].get("attempt_outcome") == "VALID_TARGET_ROW"
                and expanded[-1].get("candidate_date") == candidate.isoformat()
            )
            if on_time and not already_has_attempt:
                attempt = exact_fetch_attempt(candidate, event["event_at"])
                attempt["history_row_sha256s"] = {
                    **accepted_rows,
                    candidate.isoformat(): ROW_SHA256,
                }
                expanded.append(attempt)
            if on_time:
                accepted_count += 1
                accepted_rows[candidate.isoformat()] = event["canonical_row_sha256"]
        expanded.append(event)
    return _seal_ledger(
        expanded,
        genesis,
        available,
        authoritative_calendar,
        {identity: length for identity, length in ARTIFACT_BYTE_LENGTHS.items() if identity in available},
    )


def replay_ledger(records, genesis, available, calendar=None):
    materialized = list(records)
    genesis = {
        **genesis,
        "warmup_row_sha256s": genesis.get("warmup_row_sha256s", {}),
        "initialization_seal": genesis.get(
            "initialization_seal",
            {**INITIALIZATION_SEAL, "sealed_at": genesis["sealed_at"]},
        ),
    }
    authoritative_calendar = _test_calendar(materialized, genesis)
    if calendar is not None:
        authoritative_calendar.update(calendar)
    return _replay_ledger(
        materialized,
        genesis,
        available,
        authoritative_calendar,
        {identity: length for identity, length in ARTIFACT_BYTE_LENGTHS.items() if identity in available},
    )


def declared(day: date, status: str = "EXPECTED_PENDING") -> dict:
    return {
        "event_type": "DATE_DECLARED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T09:00:00+08:00",
        "initial_status": status,
        "evidence_sha256": [CALENDAR_SOURCE_SHA256],
    }


def accepted(day: date, *, event_hour: int = 16, deadline_day: date | None = None) -> dict:
    event_at = f"{day.isoformat()}T{event_hour:02d}:00:00+08:00"
    return {
        "event_type": "CANONICAL_ROW_ACCEPTED",
        "candidate_date": day.isoformat(),
        "event_at": event_at,
        "target_sealed_at": event_at,
        "canonical_row_sha256": ROW_SHA256,
        "parser_sha256": BUILD_SHA256,
        "build_sha256": BUILD_SHA256,
        "evidence_sha256": [ROW_SHA256, BUILD_SHA256, RESPONSE_HEADERS_SHA256],
    }


def source_revision(
    *,
    event_at: str,
    revision_scope: str,
    touches_evaluation_data: object,
    candidate_date: date | None = None,
) -> dict:
    event = {
        "event_type": "SOURCE_REVISION_OBSERVED",
        "event_at": event_at,
        "revision_scope": revision_scope,
        "touches_evaluation_data": touches_evaluation_data,
        "evidence_sha256": [ROW_SHA256],
    }
    if candidate_date is not None:
        event["candidate_date"] = candidate_date.isoformat()
    return event


def correction_event(
    *,
    day: date,
    superseded_sequence: int,
    superseded_record_sha256: str,
    correction_scope: str,
) -> dict:
    clerical = correction_scope == "CLERICAL_METADATA"
    return {
        "event_type": "CORRECTION",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T17:00:00+08:00",
        "superseded_sequence": superseded_sequence,
        "superseded_record_sha256": superseded_record_sha256,
        "old_value_sha256": BUILD_SHA256,
        "new_observation_sha256": ALTERNATE_ROW_SHA256,
        "source_sha256s": [ALTERNATE_ROW_SHA256],
        "correction_scope": correction_scope,
        "decision_surface_before_sha256": ROW_SHA256,
        "decision_surface_after_sha256": ROW_SHA256 if clerical else ALTERNATE_ROW_SHA256,
        "issuer": "offline-review-fixture",
        "reason": "append-only synthetic correction",
        "evidence_sha256": [ROW_SHA256, BUILD_SHA256, ALTERNATE_ROW_SHA256],
    }


def operator_access(day: date) -> dict:
    return {
        "event_type": "OPERATOR_ACCESS",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T16:30:00+08:00",
        "operator_identity": "offline-review-operator",
        "access_identity_sha256": BUILD_SHA256,
        "files_viewed": [
            {"path": "captures/synthetic-response.tsv", "artifact_sha256": ROW_SHA256}
        ],
        "linked_diagnostic_sequence": 1,
        "diagnostic_record_sha256": ROW_SHA256,
        "diagnostic_purpose": "SILENT_MISS",
        "reason": "synthetic clerical metadata surface",
        "evidence_sha256": [ROW_SHA256, BUILD_SHA256],
    }


def d252_prefix(genesis: dict) -> tuple[list[dict], date]:
    candidate = date.fromisoformat(genesis["sealed_at"][:10]) + timedelta(days=1)
    events = []
    ordinal = 0
    while ordinal < 252:
        expected = candidate.weekday() < 5
        events.append(declared(candidate, "EXPECTED_PENDING" if expected else "NON_SESSION"))
        if expected:
            ordinal += 1
            if ordinal < 252:
                events.append(accepted(candidate))
            else:
                return events, candidate
        candidate += timedelta(days=1)
    raise AssertionError("unreachable")


def test_historical_calendar_cases_bind_to_accepted_source_hashes():
    ordinary = classify_session(date(2025, 10, 14), facts())
    closure = classify_session(
        date(2025, 10, 1),
        facts(full_closure=True, evidence_sha256=(CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256)),
    )
    reopening = classify_session(
        date(2025, 10, 9),
        facts(no_prior_night=True, evidence_sha256=(CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256)),
    )
    weekend = classify_session(
        date(2025, 10, 11),
        facts(evidence_sha256=(CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256)),
    )

    assert ordinary.status is CalendarStatus.EXPECTED_SESSION
    assert ordinary.open_boundary.isoformat() == "2025-10-13T20:00:00+08:00"
    assert closure.status is CalendarStatus.OFFICIAL_FULL_CLOSURE
    assert closure.open_boundary is None
    assert reopening.status is CalendarStatus.EXPECTED_SESSION
    assert reopening.open_boundary.isoformat() == "2025-10-09T09:00:00+08:00"
    assert weekend.status is CalendarStatus.NON_SESSION
    assert weekend.open_boundary is None
    assert CALENDAR_SOURCE_SHA256 in ordinary.evidence_sha256


def test_monday_prior_night_and_explicit_cancellation_boundaries():
    monday = classify_session(date(2025, 10, 13), facts())
    cancelled = classify_session(
        date(2025, 10, 14),
        facts(prior_night_cancelled=True, remaining_open_time="10:30:00"),
    )

    assert monday.open_boundary.isoformat() == "2025-10-10T20:00:00+08:00"
    assert cancelled.open_boundary.isoformat() == "2025-10-14T10:30:00+08:00"


def test_exceptional_non_weekday_opening_requires_explicit_interval():
    opened = classify_session(
        date(2025, 10, 11),
        facts(exceptional_opening=True, exceptional_open_time="09:30:00"),
    )
    blocked = classify_session(date(2025, 10, 11), facts(exceptional_opening=True))

    assert opened.status is CalendarStatus.EXPECTED_SESSION
    assert opened.open_boundary.isoformat() == "2025-10-11T09:30:00+08:00"
    assert blocked.status is CalendarStatus.AMBIGUOUS_BLOCKED
    assert blocked.open_boundary is None


@pytest.mark.parametrize(
    "session_facts",
    [
        facts(conflicting=True),
        facts(sources_complete=False),
        facts(full_closure=True, exceptional_opening=True),
        facts(full_closure=True, no_prior_night=True),
        facts(exceptional_open_time="09:30:00"),
        facts(remaining_open_time="10:30:00"),
        facts(ambiguous=True),
    ],
)
def test_calendar_fails_closed_on_incomplete_or_conflicting_facts(session_facts):
    decision = classify_session(date(2025, 10, 14), session_facts)
    assert decision.status is CalendarStatus.AMBIGUOUS_BLOCKED
    assert decision.open_boundary is None


def test_canonical_json_is_exact_and_rejects_noncanonical_values():
    assert canonical_json_bytes({"中文": "金", "b": 1, "a": 2}) == (
        b'{"a":2,"b":1,"\xe4\xb8\xad\xe6\x96\x87":"\xe9\x87\x91"}'
    )
    for value in (math.nan, math.inf, 1.5):
        with pytest.raises(ContractViolation):
            canonical_json_bytes({"value": value})


def test_short_ledger_replays_states_ordinals_deadlines_and_hashes():
    first = date(2025, 10, 13)
    second = first + timedelta(days=1)
    events = [
        declared(first),
        accepted(first, deadline_day=second),
        declared(second, "OFFICIAL_FULL_CLOSURE"),
    ]
    records = seal_ledger(events, GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert replay.states == {
        "2025-10-13": "EXPECTED_ACCEPTED",
        "2025-10-14": "OFFICIAL_FULL_CLOSURE",
    }
    assert replay.ordinals == {"2025-10-13": 1}
    assert replay.deadlines == {"2025-10-13": "2025-10-14T20:00:00+08:00"}
    assert replay.max_ordinal == 1
    assert replay.final_chain_sha256 == json.loads(records[-1])["chain_sha256"]
    first_record = json.loads(records[0])
    logical = {
        key: value
        for key, value in first_record.items()
        if key not in {"record_sha256", "chain_sha256"}
    }
    expected_record_hash = hashlib.sha256(canonical_json_bytes(logical)).hexdigest()
    expected_genesis_hash = hashlib.sha256(canonical_json_bytes(GENESIS)).hexdigest()
    assert first_record["record_sha256"] == expected_record_hash
    assert first_record["chain_sha256"] == hashlib.sha256(
        bytes.fromhex(expected_genesis_hash) + bytes.fromhex(expected_record_hash)
    ).hexdigest()


def test_late_d1_acceptance_becomes_deadline_breach_without_ordinal():
    day = date(2025, 10, 13)
    late = accepted(day, event_hour=21, deadline_day=day)
    records = seal_ledger([declared(day), late], GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)
    final = json.loads(records[-1])

    assert final["event_type"] == "DEADLINE_EXPIRED"
    assert replay.states[day.isoformat()] == "PROTOCOL_BREACH"
    assert replay.max_ordinal == 0
    assert replay.terminal_class == "INVALIDATED"


def test_target_sealed_exactly_at_d1_deadline_is_rejected():
    day = date(2025, 10, 13)
    event = accepted(day, event_hour=20, deadline_day=day)
    event["target_sealed_at"] = f"{day.isoformat()}T20:00:00+08:00"
    records = seal_ledger([declared(day), event], GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)
    assert replay.max_ordinal == 0
    assert json.loads(records[-1])["event_type"] == "DEADLINE_EXPIRED"


def test_d252_uses_recovery_boundary_and_never_assigns_ordinal_253():
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2024-12-31T00:00:00Z"}
    events, d252 = d252_prefix(genesis)
    recovery_day = d252 + timedelta(days=1)
    while recovery_day.weekday() >= 5:
        events.append(declared(recovery_day, "NON_SESSION"))
        recovery_day += timedelta(days=1)
    events.append(declared(recovery_day))
    event = accepted(d252)
    event.pop("target_sealed_at")
    event["event_at"] = f"{recovery_day.isoformat()}T15:45:00+08:00"
    events.append(event)

    records = seal_ledger(events, genesis, AVAILABLE)
    replay = replay_ledger(records, genesis, AVAILABLE)
    assert replay.max_ordinal == 252
    assert replay.ordinals[d252.isoformat()] == 252

    override = dict(event)
    override["recovery_deadline"] = (
        f"{(recovery_day + timedelta(days=10)).isoformat()}T15:45:00+08:00"
    )
    with pytest.raises(ContractViolation, match="generated field"):
        seal_ledger([*events[:-1], override], genesis, AVAILABLE)

    with pytest.raises(ContractViolation, match="ordinal 252"):
        seal_ledger([*events, accepted(recovery_day)], genesis, AVAILABLE)


def test_duplicate_observation_does_not_increment_and_duplicate_acceptance_is_forbidden():
    day = date(2025, 10, 13)
    duplicate_capture = exact_fetch_attempt(day, f"{day.isoformat()}T17:00:00+08:00")
    events = [declared(day), accepted(day), duplicate_capture]
    replay = replay_ledger(seal_ledger(events, GENESIS, AVAILABLE), GENESIS, AVAILABLE)
    assert replay.max_ordinal == 1

    with pytest.raises(ContractViolation, match="terminal"):
        seal_ledger([*events, accepted(day, event_hour=18)], GENESIS, AVAILABLE)


def test_forbidden_transition_and_candidate_date_gap_fail_closed():
    first = date(2025, 10, 13)
    skipped = declared(first + timedelta(days=2))
    skipped["event_at"] = f"{first.isoformat()}T09:00:00+08:00"
    with pytest.raises(ContractViolation, match="contiguous"):
        seal_ledger([declared(first), skipped], GENESIS, AVAILABLE)

    with pytest.raises(ContractViolation, match="terminal"):
        seal_ledger(
            [
                declared(first, "OFFICIAL_FULL_CLOSURE"),
                {
                    "event_type": "DEADLINE_EXPIRED",
                    "candidate_date": first.isoformat(),
                    "event_at": f"{first.isoformat()}T20:00:00+08:00",
                    "evidence_sha256": [CALENDAR_SOURCE_SHA256],
                },
            ],
            GENESIS,
            AVAILABLE,
        )


def test_candidate_coverage_begins_immediately_after_genesis_shanghai_date():
    with pytest.raises(ContractViolation, match="immediately after"):
        seal_ledger([declared(date(2025, 10, 14))], GENESIS, AVAILABLE)


def test_failed_post_close_fetch_automatically_appends_linked_silent_miss():
    day = date(2025, 10, 13)
    attempt_event = exact_fetch_attempt(
        day,
        f"{day.isoformat()}T15:45:00+08:00",
        "TARGET_DATE_ABSENT",
    )
    records = seal_ledger([declared(day), attempt_event], GENESIS, AVAILABLE)
    attempt = json.loads(records[1])
    silent_miss = json.loads(records[2])

    assert attempt["event_type"] == "FETCH_ATTEMPT"
    assert silent_miss["event_type"] == "SILENT_MISS"
    assert silent_miss["linked_attempt_sequence"] == attempt["sequence"]
    replay = replay_ledger(records, GENESIS, AVAILABLE)
    assert replay.states[day.isoformat()] == "EXPECTED_PENDING"


def test_sequence_gap_hash_tampering_noncanonical_bytes_and_missing_identity_fail_closed():
    day = date(2025, 10, 13)
    records = list(seal_ledger([declared(day), accepted(day)], GENESIS, AVAILABLE))

    with pytest.raises(ContractViolation, match="sequence"):
        replay_ledger(records[1:], GENESIS, AVAILABLE)

    tampered = bytearray(records[0])
    tampered[tampered.index(b"DATE_DECLARED")] = ord("X")
    with pytest.raises(ContractViolation):
        replay_ledger([bytes(tampered), records[1]], GENESIS, AVAILABLE)

    noncanonical = json.dumps(json.loads(records[0]), indent=2).encode()
    with pytest.raises(ContractViolation, match="canonical"):
        replay_ledger([noncanonical], GENESIS, AVAILABLE)

    with pytest.raises(ContractViolation, match="referenced identity"):
        replay_ledger(records, GENESIS, AVAILABLE - {ROW_SHA256})


def test_source_revision_invalidates_globally_without_mutating_accepted_date():
    day = date(2025, 10, 13)
    revision = accepted_revision_attempt(day, f"{day.isoformat()}T18:00:00+08:00")
    replay = replay_ledger(
        seal_ledger([declared(day), accepted(day), revision], GENESIS, AVAILABLE),
        GENESIS,
        AVAILABLE,
    )
    assert replay.states[day.isoformat()] == "EXPECTED_ACCEPTED"
    assert replay.terminal_class == "INVALIDATED"


def test_offline_fixture_acceptance_path_emits_only_after_success(tmp_path):
    day = date(2025, 10, 13)
    fixture = {
        "genesis": GENESIS,
        "available_artifact_sha256": sorted(AVAILABLE),
        "artifact_byte_lengths": ARTIFACT_BYTE_LENGTHS,
        "calendar": [
            {
                "session_date": day.isoformat(),
                "facts": fact_mapping(no_prior_night=True),
            },
            _calendar_item(day + timedelta(days=1)),
        ],
        "events": [
            declared(day),
            exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00"),
            accepted(day),
        ],
    }
    result = run_fixture(fixture)
    assert result["replay"]["max_ordinal"] == 1
    assert result["calendar"][0]["status"] == "EXPECTED_SESSION"

    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture))
    completed = subprocess.run(
        [sys.executable, "-m", "gold_research.prospective_evidence", str(fixture_path)],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["replay"]["max_ordinal"] == 1

    fixture["events"][-1]["target_sealed_at"] = "not-a-timestamp"
    fixture_path.write_text(json.dumps(fixture))
    failed = subprocess.run(
        [sys.executable, "-m", "gold_research.prospective_evidence", str(fixture_path)],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert "contract violation" in failed.stderr.lower()


def _calendar_item(day: date, **fact_changes) -> dict:
    if day == date(2025, 10, 13) and not fact_changes:
        fact_changes["no_prior_night"] = True
    return {"session_date": day.isoformat(), "facts": fact_mapping(**fact_changes)}


def _rechain_single_record(record: dict, genesis: dict = GENESIS) -> bytes:
    genesis = {
        **genesis,
        "warmup_row_sha256s": genesis.get("warmup_row_sha256s", {}),
        "initialization_seal": genesis.get(
            "initialization_seal",
            {**INITIALIZATION_SEAL, "sealed_at": genesis["sealed_at"]},
        ),
    }
    previous = hashlib.sha256(canonical_json_bytes(genesis)).hexdigest()
    record["previous_chain_sha256"] = previous
    logical = {
        key: value
        for key, value in record.items()
        if key not in {"record_sha256", "chain_sha256"}
    }
    record_hash = hashlib.sha256(canonical_json_bytes(logical)).hexdigest()
    record["record_sha256"] = record_hash
    record["chain_sha256"] = hashlib.sha256(
        bytes.fromhex(previous) + bytes.fromhex(record_hash)
    ).hexdigest()
    return canonical_json_bytes(record)


def _rechain_records(records: list[dict], genesis: dict = GENESIS) -> tuple[bytes, ...]:
    genesis = {
        **genesis,
        "warmup_row_sha256s": genesis.get("warmup_row_sha256s", {}),
        "initialization_seal": genesis.get(
            "initialization_seal",
            {**INITIALIZATION_SEAL, "sealed_at": genesis["sealed_at"]},
        ),
    }
    previous = hashlib.sha256(canonical_json_bytes(genesis)).hexdigest()
    rechained = []
    for record in records:
        record["previous_chain_sha256"] = previous
        logical = {
            key: value
            for key, value in record.items()
            if key not in {"record_sha256", "chain_sha256"}
        }
        record_hash = hashlib.sha256(canonical_json_bytes(logical)).hexdigest()
        record["record_sha256"] = record_hash
        record["chain_sha256"] = hashlib.sha256(
            bytes.fromhex(previous) + bytes.fromhex(record_hash)
        ).hexdigest()
        rechained.append(canonical_json_bytes(record))
        previous = record["chain_sha256"]
    return tuple(rechained)


def test_authoritative_next_open_rejects_caller_supplied_later_d1_deadline():
    day = date(2025, 10, 13)
    next_day = day + timedelta(days=1)
    malicious = accepted(day, deadline_day=next_day + timedelta(days=1))
    malicious["execution_seal_deadline"] = (
        f"{(next_day + timedelta(days=1)).isoformat()}T20:00:00+08:00"
    )
    fixture = {
        "genesis": GENESIS,
        "available_artifact_sha256": sorted(AVAILABLE),
        "artifact_byte_lengths": ARTIFACT_BYTE_LENGTHS,
        "calendar": [_calendar_item(day), _calendar_item(next_day)],
        "events": [declared(day), malicious],
    }

    with pytest.raises(ContractViolation, match="generated field"):
        run_fixture(fixture)


def test_d252_recovery_uses_first_authoritative_expected_session_not_caller_date():
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2024-12-31T00:00:00Z"}
    events, d252 = d252_prefix(genesis)
    first_expected_after = d252 + timedelta(days=1)
    while first_expected_after.weekday() >= 5:
        first_expected_after += timedelta(days=1)
    malicious_deadline_date = d252 + timedelta(days=10)
    candidate = d252 + timedelta(days=1)
    while candidate <= first_expected_after:
        events.append(
            declared(candidate, "EXPECTED_PENDING" if candidate.weekday() < 5 else "NON_SESSION")
        )
        candidate += timedelta(days=1)
    final = accepted(d252)
    final.pop("target_sealed_at")
    final["event_at"] = f"{malicious_deadline_date.isoformat()}T15:45:00+08:00"
    events.append(final)

    records = seal_ledger(events, genesis, AVAILABLE)
    result = replay_ledger(records, genesis, AVAILABLE)

    assert result.max_ordinal == 251
    assert result.states[d252.isoformat()] == "PROTOCOL_BREACH"
    assert result.deadlines[d252.isoformat()] == (
        f"{first_expected_after.isoformat()}T15:45:00+08:00"
    )


def test_event_chronology_and_completed_close_availability_fail_closed():
    day = date(2025, 10, 13)
    retrograde = accepted(day)
    retrograde["event_at"] = "2025-10-12T16:00:00+08:00"
    retrograde["target_sealed_at"] = retrograde["event_at"]
    with pytest.raises(ContractViolation, match="chronolog"):
        seal_ledger([declared(day), retrograde], GENESIS, AVAILABLE)

    pre_close = accepted(day)
    pre_close["event_at"] = f"{day.isoformat()}T15:44:59+08:00"
    pre_close["target_sealed_at"] = pre_close["event_at"]
    with pytest.raises(ContractViolation, match="15:45"):
        seal_ledger([declared(day), pre_close], GENESIS, AVAILABLE)


def test_replay_rejects_unsupported_events_and_contradictory_timestamp_pair():
    day = date(2025, 10, 13)
    original = json.loads(seal_ledger([declared(day)], GENESIS, AVAILABLE)[0])

    unsupported = dict(original)
    unsupported["event_type"] = "UNSUPPORTED_EVENT"
    with pytest.raises(ContractViolation, match="unsupported"):
        replay_ledger([_rechain_single_record(unsupported)], GENESIS, AVAILABLE)

    contradictory = dict(original)
    contradictory["event_at_utc"] = "2030-01-01T00:00:00Z"
    with pytest.raises(ContractViolation, match="timestamp"):
        replay_ledger([_rechain_single_record(contradictory)], GENESIS, AVAILABLE)

    missing_required_shape = dict(original)
    missing_required_shape.pop("reason")
    with pytest.raises(ContractViolation, match="normalized shape"):
        replay_ledger([_rechain_single_record(missing_required_shape)], GENESIS, AVAILABLE)

    changed_calendar = _test_calendar([original])
    changed_calendar[day.isoformat()] = facts()
    with pytest.raises(ContractViolation, match="reproduce|initialization.*S1 open"):
        replay_ledger([canonical_json_bytes(original)], GENESIS, AVAILABLE, changed_calendar)


def test_replay_reconstructs_generated_deadline_expiration_semantics():
    day = date(2025, 10, 13)
    late = accepted(day, event_hour=21, deadline_day=day)
    records = seal_ledger([declared(day), late], GENESIS, AVAILABLE)

    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert replay.deadlines == {day.isoformat(): f"{day.isoformat()}T20:00:00+08:00"}


def test_pre_open_reclassification_governs_preceding_date_deadline_and_replay():
    friday = date(2025, 10, 10)
    saturday = friday + timedelta(days=1)
    sunday = saturday + timedelta(days=1)
    monday = sunday + timedelta(days=1)
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2025-10-09T00:00:00Z"}
    saturday_declaration = declared(saturday, "NON_SESSION")
    saturday_declaration["event_at"] = f"{friday.isoformat()}T17:00:00+08:00"
    reclassification = {
        "event_type": "CALENDAR_RECLASSIFIED",
        "candidate_date": saturday.isoformat(),
        "event_at": f"{saturday.isoformat()}T08:00:00+08:00",
        "reclassified_status": "EXPECTED_PENDING",
        "authoritative_facts": fact_mapping(
            evidence_sha256=[CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256],
            exceptional_opening=True,
            exceptional_open_time="09:00:00",
        ),
        "evidence_sha256": [CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256],
        "governed_open_boundary": f"{saturday.isoformat()}T09:00:00+08:00",
    }
    late_friday = accepted(friday)
    late_friday["event_at"] = f"{saturday.isoformat()}T10:00:00+08:00"
    late_friday["target_sealed_at"] = late_friday["event_at"]
    calendar = {
        friday.isoformat(): facts(),
        saturday.isoformat(): facts(),
        sunday.isoformat(): facts(),
        monday.isoformat(): facts(no_prior_night=True),
    }

    records = seal_ledger(
        [declared(friday), saturday_declaration, reclassification, late_friday],
        genesis,
        AVAILABLE,
        calendar,
    )
    replay = replay_ledger(records, genesis, AVAILABLE, calendar)

    assert json.loads(records[-1])["event_type"] == "DEADLINE_EXPIRED"
    assert replay.states[friday.isoformat()] == "PROTOCOL_BREACH"
    assert replay.max_ordinal == 0
    assert replay.deadlines[friday.isoformat()] == (
        f"{saturday.isoformat()}T09:00:00+08:00"
    )


def test_reclassification_requires_structured_authoritative_facts():
    day = date(2025, 10, 11)
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2025-10-10T00:00:00Z"}
    declaration = declared(day, "NON_SESSION")
    declaration["event_at"] = "2025-10-10T17:00:00+08:00"
    reclassification = {
        "event_type": "CALENDAR_RECLASSIFIED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T08:00:00+08:00",
        "reclassified_status": "EXPECTED_PENDING",
        "governed_open_boundary": f"{day.isoformat()}T09:00:00+08:00",
        "evidence_sha256": [CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256],
    }

    with pytest.raises(ContractViolation, match="authoritative[_ ]facts"):
        seal_ledger(
            [declaration, reclassification],
            genesis,
            AVAILABLE,
            {day.isoformat(): facts()},
        )


@pytest.mark.parametrize(
    ("event_evidence", "reclassified_status", "boundary_hour", "message"),
    [
        (
            [CALENDAR_SOURCE_SHA256],
            "EXPECTED_PENDING",
            9,
            "include every authoritative fact identity",
        ),
        (
            [CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256],
            "OFFICIAL_FULL_CLOSURE",
            9,
            "contradicts authoritative facts",
        ),
        (
            [CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256],
            "EXPECTED_PENDING",
            10,
            "boundary contradicts authoritative facts",
        ),
    ],
)
def test_reclassification_rejects_unbound_evidence_status_or_boundary(
    event_evidence,
    reclassified_status,
    boundary_hour,
    message,
):
    day = date(2025, 10, 11)
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2025-10-10T00:00:00Z"}
    declaration = declared(day, "NON_SESSION")
    declaration["event_at"] = "2025-10-10T17:00:00+08:00"
    reclassification = {
        "event_type": "CALENDAR_RECLASSIFIED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T08:00:00+08:00",
        "reclassified_status": reclassified_status,
        "authoritative_facts": fact_mapping(
            evidence_sha256=[CALENDAR_SOURCE_SHA256, HOLIDAY_2025_SHA256],
            exceptional_opening=True,
            exceptional_open_time="09:00:00",
        ),
        "evidence_sha256": event_evidence,
        "governed_open_boundary": (
            f"{day.isoformat()}T{boundary_hour:02d}:00:00+08:00"
        ),
    }

    with pytest.raises(ContractViolation, match=message):
        seal_ledger(
            [declaration, reclassification],
            genesis,
            AVAILABLE,
            {day.isoformat(): facts()},
        )


@pytest.mark.parametrize(
    ("event", "missing_field"),
    [
        (declared(date(2025, 10, 13)), "prior_state"),
        (
            {
                "event_type": "EXTERNAL_PRODUCTION_CHANGE",
                "event_at": "2025-10-13T09:00:00+08:00",
                "evidence_sha256": [BUILD_SHA256],
            },
            "next_state",
        ),
    ],
)
def test_replay_rejects_missing_null_valued_generated_fields(event, missing_field):
    supplied = [event]
    if event["event_type"] != "DATE_DECLARED":
        supplied.insert(0, declared(date(2025, 10, 13)))
    records = [json.loads(record) for record in seal_ledger(supplied, GENESIS, AVAILABLE)]
    original = records[-1]
    assert original[missing_field] is None
    original.pop(missing_field)

    with pytest.raises(ContractViolation, match="generated field"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


@pytest.mark.parametrize("breach_type", ["DEADLINE_EXPIRED", "PROTOCOL_BREACH_RECORDED"])
@pytest.mark.parametrize("later_event_type", ["DATE_DECLARED", "CANONICAL_ROW_ACCEPTED"])
def test_irreversible_breach_ends_candidate_coverage(breach_type, later_event_type):
    day = date(2025, 10, 13)
    breach = {
        "event_type": breach_type,
        "candidate_date": day.isoformat(),
        "event_at": (
            f"{day.isoformat()}T20:00:00+08:00"
            if breach_type == "DEADLINE_EXPIRED"
            else f"{day.isoformat()}T19:59:00+08:00"
        ),
        "evidence_sha256": [CALENDAR_SOURCE_SHA256],
    }
    next_day = day + timedelta(days=1)
    later = declared(next_day) if later_event_type == "DATE_DECLARED" else accepted(next_day)
    later["event_at"] = f"{next_day.isoformat()}T16:00:00+08:00"
    if "target_sealed_at" in later:
        later["target_sealed_at"] = later["event_at"]

    with pytest.raises(ContractViolation, match="invalidation.*coverage"):
        seal_ledger([declared(day), breach, later], GENESIS, AVAILABLE)


def test_post_breach_provenance_event_is_append_only_but_cannot_assign_an_ordinal():
    day = date(2025, 10, 13)
    breach = {
        "event_type": "PROTOCOL_BREACH_RECORDED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T10:00:00+08:00",
        "evidence_sha256": [CALENDAR_SOURCE_SHA256],
    }
    access = operator_access(day)
    access["event_at"] = f"{day.isoformat()}T10:01:00+08:00"
    sealed_breach = json.loads(seal_ledger([declared(day), breach], GENESIS, AVAILABLE)[-1])
    access.update(
        {
            "linked_diagnostic_sequence": sealed_breach["sequence"],
            "diagnostic_record_sha256": sealed_breach["record_sha256"],
            "diagnostic_purpose": "PROTOCOL_BREACH_RECORDED",
        }
    )

    records = seal_ledger([declared(day), breach, access], GENESIS, AVAILABLE)
    result = replay_ledger(records, GENESIS, AVAILABLE)

    assert result.states == {day.isoformat(): "PROTOCOL_BREACH"}
    assert result.max_ordinal == 0
    assert result.terminal_class == "INVALIDATED"


def test_replay_rejects_boolean_sequence_scalar():
    day = date(2025, 10, 13)
    records = [json.loads(raw) for raw in seal_ledger([declared(day), accepted(day)], GENESIS, AVAILABLE)]

    boolean_sequence = dict(records[0])
    boolean_sequence["sequence"] = True
    with pytest.raises(ContractViolation, match="sequence.*integer"):
        replay_ledger([_rechain_single_record(boolean_sequence)], GENESIS, AVAILABLE)


def test_replay_rejects_boolean_expected_ordinal_scalar():
    day = date(2025, 10, 13)
    records = [json.loads(raw) for raw in seal_ledger([declared(day), accepted(day)], GENESIS, AVAILABLE)]
    records[-1]["expected_ordinal"] = True
    with pytest.raises(ContractViolation, match="expected_ordinal.*integer"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


@pytest.mark.parametrize("breach_type", ["DEADLINE_EXPIRED", "PROTOCOL_BREACH_RECORDED"])
@pytest.mark.parametrize("later_event_type", ["DATE_DECLARED", "CANONICAL_ROW_ACCEPTED"])
def test_replay_rejects_rechained_candidate_progress_after_irreversible_breach(
    breach_type,
    later_event_type,
):
    day = date(2025, 10, 13)
    next_day = day + timedelta(days=1)
    breach = {
        "event_type": breach_type,
        "candidate_date": day.isoformat(),
        "event_at": (
            f"{day.isoformat()}T20:00:00+08:00"
            if breach_type == "DEADLINE_EXPIRED"
            else f"{day.isoformat()}T19:59:00+08:00"
        ),
        "evidence_sha256": [CALENDAR_SOURCE_SHA256],
    }
    prefix = [
        json.loads(raw)
        for raw in seal_ledger(
            [declared(day), breach],
            GENESIS,
            AVAILABLE,
        )
    ]
    if later_event_type == "DATE_DECLARED":
        later_day = next_day + timedelta(days=1)
        next_declaration = declared(next_day)
        next_declaration["event_at"] = f"{day.isoformat()}T09:00:00+08:00"
        later_declaration = declared(later_day)
        later_declaration["event_at"] = f"{day.isoformat()}T09:00:00+08:00"
        clean = seal_ledger(
            [declared(day), next_declaration, later_declaration],
            GENESIS,
            AVAILABLE,
        )
        forged = json.loads(clean[-1])
    else:
        forged = {
            "event_type": "CANONICAL_ROW_ACCEPTED",
            "candidate_date": next_day.isoformat(),
            "event_at_utc": f"{next_day.isoformat()}T08:00:00Z",
            "event_at_asia_shanghai": f"{next_day.isoformat()}T16:00:00+08:00",
            "target_sealed_at": f"{next_day.isoformat()}T16:00:00+08:00",
            "canonical_row_sha256": ROW_SHA256,
            "canonical_raw_sha256": ROW_SHA256,
            "capture_at": f"{next_day.isoformat()}T08:00:00Z",
            "parser_sha256": BUILD_SHA256,
            "build_sha256": BUILD_SHA256,
            "linked_attempt_sequence": 1,
            "evidence_sha256": [ROW_SHA256, BUILD_SHA256],
            "reason": "contract event: CANONICAL_ROW_ACCEPTED",
            "prior_state": "EXPECTED_PENDING",
            "next_state": "EXPECTED_ACCEPTED",
            "expected_ordinal": 1,
            "execution_seal_deadline": f"{next_day.isoformat()}T20:00:00+08:00",
            "sequence": 4,
            "previous_chain_sha256": "0" * 64,
            "record_sha256": "0" * 64,
            "chain_sha256": "0" * 64,
        }
    forged["sequence"] = len(prefix) + 1

    with pytest.raises(ContractViolation, match="invalidation.*coverage"):
        replay_ledger(_rechain_records([*prefix, forged]), GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("sources_complete", 1),
        ("full_closure", "false"),
        ("ambiguous", None),
    ],
)
def test_direct_calendar_interface_rejects_non_boolean_authoritative_facts(
    field_name,
    invalid_value,
):
    with pytest.raises(ContractViolation, match=f"{field_name!s}.*boolean"):
        classify_session(date(2025, 10, 13), facts(**{field_name: invalid_value}))


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("sources_complete", 1),
        ("full_closure", "false"),
        ("ambiguous", None),
    ],
)
def test_offline_fixture_rejects_non_boolean_authoritative_facts(
    field_name,
    invalid_value,
):
    day = date(2025, 10, 13)
    fixture = {
        "genesis": GENESIS,
        "available_artifact_sha256": sorted(AVAILABLE),
        "artifact_byte_lengths": ARTIFACT_BYTE_LENGTHS,
        "calendar": [
            _calendar_item(day, **{field_name: invalid_value}),
            _calendar_item(day + timedelta(days=1)),
        ],
        "events": [declared(day)],
    }

    with pytest.raises(ContractViolation, match=f"{field_name!s}.*boolean"):
        run_fixture(fixture)


def test_initial_calendar_construction_rejects_non_boolean_direct_facts():
    day = date(2025, 10, 13)
    calendar = _test_calendar([declared(day)])
    calendar[day.isoformat()] = facts(sources_complete=1)

    with pytest.raises(ContractViolation, match="sources_complete.*boolean"):
        seal_ledger([declared(day)], GENESIS, AVAILABLE, calendar)


def _source_revision_events(
    through: date,
    *,
    declaration_time: str | None = None,
) -> list[dict]:
    first = date(2025, 10, 13)
    events = []
    candidate = first
    while candidate <= through:
        event = declared(
            candidate,
            "EXPECTED_PENDING" if candidate.weekday() < 5 else "NON_SESSION",
        )
        if declaration_time is not None:
            event["event_at"] = declaration_time
        events.append(event)
        if candidate.weekday() < 5 and declaration_time is None:
            events.append(accepted(candidate))
        candidate += timedelta(days=1)
    if declaration_time is not None:
        events.append(accepted(first))
    return events


def test_global_revision_seals_and_replays_exact_breach_civil_date_coverage():
    first = date(2025, 10, 13)
    breach_day = date(2025, 10, 20)
    revision = accepted_revision_attempt(first, f"{breach_day.isoformat()}T16:00:00+08:00")
    records = seal_ledger(
        [*_source_revision_events(breach_day), revision],
        GENESIS,
        AVAILABLE,
    )
    sealed_revision = json.loads(records[-1])
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert sealed_revision["breach_civil_date"] == breach_day.isoformat()
    assert replay.breach_civil_date == breach_day.isoformat()
    assert sorted(replay.states) == [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((breach_day - first).days + 1)
    ]

    sealed_revision["breach_civil_date"] = (breach_day - timedelta(days=1)).isoformat()
    forged = [json.loads(record) for record in records[:-1]] + [sealed_revision]
    with pytest.raises(ContractViolation, match="reproduce|breach civil"):
        replay_ledger(_rechain_records(forged), GENESIS, AVAILABLE)


def test_global_revision_rejects_missing_or_excess_breach_date_coverage():
    first = date(2025, 10, 13)
    breach_day = date(2025, 10, 20)
    revision = accepted_revision_attempt(first, f"{breach_day.isoformat()}T16:00:00+08:00")

    with pytest.raises(ContractViolation, match="coverage.*(breach|event) civil date"):
        seal_ledger([declared(first), accepted(first), revision], GENESIS, AVAILABLE)

    day_after_breach = breach_day + timedelta(days=1)
    early_declarations = _source_revision_events(
        day_after_breach,
        declaration_time=f"{first.isoformat()}T09:00:00+08:00",
    )
    with pytest.raises(ContractViolation, match="coverage.*breach civil date"):
        seal_ledger([*early_declarations, revision], GENESIS, AVAILABLE)


@pytest.mark.parametrize("ingress", ["direct", "calendar", "fixture", "reclassification"])
@pytest.mark.parametrize("invalid_shape", ["missing", "contradictory"])
def test_every_authoritative_fact_ingress_requires_complete_coherent_shape(
    ingress,
    invalid_shape,
):
    day = date(2025, 10, 13)
    invalid_facts = fact_mapping()
    if invalid_shape == "missing":
        invalid_facts.pop("full_closure")
        expected_message = "complete canonical shape"
    else:
        invalid_facts["no_prior_night"] = True
        invalid_facts["prior_night_cancelled"] = True
        expected_message = "mutually exclusive"

    if ingress == "direct":
        with pytest.raises(ContractViolation, match=expected_message):
            classify_session(day, invalid_facts)
        return

    if ingress == "calendar":
        operation = partial(
            seal_ledger,
            [declared(day)],
            GENESIS,
            AVAILABLE,
            {day.isoformat(): invalid_facts},
        )
    elif ingress == "fixture":
        operation = partial(
            run_fixture,
            {
                "genesis": GENESIS,
                "available_artifact_sha256": sorted(AVAILABLE),
                "calendar": [
                    {"session_date": day.isoformat(), "facts": invalid_facts},
                ],
                "events": [declared(day)],
            },
        )
    else:
        saturday = date(2025, 10, 11)
        genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2025-10-10T00:00:00Z"}
        declaration = declared(saturday, "NON_SESSION")
        declaration["event_at"] = "2025-10-10T17:00:00+08:00"
        reclassification = {
            "event_type": "CALENDAR_RECLASSIFIED",
            "candidate_date": saturday.isoformat(),
            "event_at": f"{saturday.isoformat()}T08:00:00+08:00",
            "reclassified_status": "EXPECTED_PENDING",
            "authoritative_facts": invalid_facts,
            "evidence_sha256": [CALENDAR_SOURCE_SHA256, TRADING_HOURS_SHA256],
            "governed_open_boundary": f"{saturday.isoformat()}T09:00:00+08:00",
        }
        operation = partial(
            seal_ledger,
            [declaration, reclassification],
            genesis,
            AVAILABLE,
            {saturday.isoformat(): facts()},
        )

    with pytest.raises(ContractViolation, match=expected_message):
        operation()


def test_d252_success_seals_exact_terminal_coverage_and_rejects_post_boundary_dates():
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2024-12-31T00:00:00Z"}
    events, d252 = d252_prefix(genesis)
    recovery_day = d252 + timedelta(days=1)
    while True:
        recovery_status = "EXPECTED_PENDING" if recovery_day.weekday() < 5 else "NON_SESSION"
        events.append(declared(recovery_day, recovery_status))
        if recovery_status == "EXPECTED_PENDING":
            break
        recovery_day += timedelta(days=1)
    final_acceptance = accepted(d252)
    final_acceptance.pop("target_sealed_at")
    final_acceptance["event_at"] = f"{recovery_day.isoformat()}T15:45:00+08:00"
    complete_events = [*events, final_acceptance]
    records = seal_ledger(complete_events, genesis, AVAILABLE)
    final_record = json.loads(records[-1])

    assert final_record["terminal_coverage_date"] == recovery_day.isoformat()
    replay = replay_ledger(records, genesis, AVAILABLE)
    assert replay.max_ordinal == 252
    assert replay.terminal_coverage_date == recovery_day.isoformat()

    finalized = {
        "event_type": "FINALIZED",
        "event_at": f"{recovery_day.isoformat()}T15:46:00+08:00",
        "evidence_sha256": [BUILD_SHA256],
    }
    finalized_replay = replay_ledger(
        seal_ledger([*complete_events, finalized], genesis, AVAILABLE),
        genesis,
        AVAILABLE,
    )
    assert finalized_replay.terminal_coverage_date == recovery_day.isoformat()

    extra_day = recovery_day + timedelta(days=1)
    extra_status = "EXPECTED_PENDING" if extra_day.weekday() < 5 else "NON_SESSION"
    with pytest.raises(ContractViolation, match="terminal coverage"):
        seal_ledger(
            [*complete_events, declared(extra_day, extra_status), finalized],
            genesis,
            AVAILABLE,
        )

    decision = classify_session(extra_day, facts())
    forged_extra = {
        "event_type": "DATE_DECLARED",
        "candidate_date": extra_day.isoformat(),
        "event_at_utc": f"{extra_day.isoformat()}T01:00:00Z",
        "event_at_asia_shanghai": f"{extra_day.isoformat()}T09:00:00+08:00",
        "initial_status": extra_status,
        "reason": "contract event: DATE_DECLARED",
        "evidence_sha256": [CALENDAR_SOURCE_SHA256],
        "prior_state": None,
        "next_state": extra_status,
        "calendar_status": decision.status.value,
        "calendar_evidence_sha256": list(decision.evidence_sha256),
        "open_boundary_asia_shanghai": (
            decision.open_boundary.isoformat() if decision.open_boundary else None
        ),
        "open_boundary_utc": (
            decision.open_boundary.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
            if decision.open_boundary
            else None
        ),
        "sequence": len(records) + 1,
        "previous_chain_sha256": "0" * 64,
        "record_sha256": "0" * 64,
        "chain_sha256": "0" * 64,
    }
    forged_records = [json.loads(record) for record in records]
    with pytest.raises(ContractViolation, match="terminal coverage"):
        replay_ledger(
            _rechain_records([*forged_records, forged_extra], genesis),
            genesis,
            AVAILABLE,
        )


@pytest.mark.parametrize(
    ("day", "fact_changes"),
    [
        (
            date(2025, 10, 14),
            {"exceptional_opening": True, "exceptional_open_time": "09:30:00"},
        ),
        (date(2025, 10, 11), {"no_prior_night": True}),
        (date(2025, 10, 11), {"prior_night_cancelled": True}),
        (
            date(2025, 10, 11),
            {
                "exceptional_opening": True,
                "exceptional_open_time": "09:30:00",
                "no_prior_night": True,
            },
        ),
    ],
)
def test_complete_but_date_incoherent_calendar_facts_are_ambiguous(day, fact_changes):
    decision = classify_session(day, facts(**fact_changes))

    assert decision.status is CalendarStatus.AMBIGUOUS_BLOCKED
    assert decision.open_boundary is None


@pytest.mark.parametrize("ingress", ["calendar", "fixture", "reclassification"])
def test_every_calendar_ingress_rejects_complete_but_date_incoherent_facts(ingress):
    if ingress == "reclassification":
        day = date(2025, 10, 11)
        genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2025-10-10T00:00:00Z"}
        declaration = declared(day, "NON_SESSION")
        declaration["event_at"] = "2025-10-10T17:00:00+08:00"
        invalid_facts = fact_mapping(
            exceptional_opening=True,
            exceptional_open_time="09:30:00",
            no_prior_night=True,
        )
        event = {
            "event_type": "CALENDAR_RECLASSIFIED",
            "candidate_date": day.isoformat(),
            "event_at": f"{day.isoformat()}T08:00:00+08:00",
            "reclassified_status": "EXPECTED_PENDING",
            "authoritative_facts": invalid_facts,
            "evidence_sha256": [CALENDAR_SOURCE_SHA256, TRADING_HOURS_SHA256],
            "governed_open_boundary": f"{day.isoformat()}T09:30:00+08:00",
        }
        operation = partial(
            seal_ledger,
            [declaration, event],
            genesis,
            AVAILABLE,
            {day.isoformat(): facts()},
        )
    else:
        day = date(2025, 10, 14)
        invalid_facts = fact_mapping(
            exceptional_opening=True,
            exceptional_open_time="09:30:00",
        )
        if ingress == "calendar":
            operation = partial(
                seal_ledger,
                [declared(day)],
                GENESIS,
                AVAILABLE,
                {day.isoformat(): invalid_facts},
            )
        else:
            operation = partial(
                run_fixture,
                {
                    "genesis": GENESIS,
                    "available_artifact_sha256": sorted(AVAILABLE),
                    "calendar": [{"session_date": day.isoformat(), "facts": invalid_facts}],
                    "events": [declared(day)],
                },
            )

    with pytest.raises(ContractViolation, match="ambiguous"):
        operation()


@pytest.mark.parametrize("invalid_value", [None, "false"])
def test_source_revision_scope_flag_rejects_non_boolean_values(invalid_value):
    day = date(2025, 10, 13)
    revision = source_revision(
        event_at=f"{day.isoformat()}T18:00:00+08:00",
        revision_scope="INITIALIZATION_DATA",
        touches_evaluation_data=invalid_value,
    )

    with pytest.raises(ContractViolation, match="touches_evaluation_data.*boolean"):
        seal_ledger([declared(day), accepted(day), revision], GENESIS, AVAILABLE)


def test_source_revision_requires_explicit_scope_flag_and_scope_identity():
    day = date(2025, 10, 13)
    revision = source_revision(
        event_at=f"{day.isoformat()}T18:00:00+08:00",
        revision_scope="INITIALIZATION_DATA",
        touches_evaluation_data=True,
    )
    missing_flag = dict(revision)
    missing_flag.pop("touches_evaluation_data")
    missing_scope = dict(revision)
    missing_scope.pop("revision_scope")

    with pytest.raises(
        ContractViolation,
        match="touches_evaluation_data.*required|requires field 'touches_evaluation_data'",
    ):
        seal_ledger([declared(day), accepted(day), missing_flag], GENESIS, AVAILABLE)
    with pytest.raises(
        ContractViolation,
        match="revision_scope.*required|requires field 'revision_scope'",
    ):
        seal_ledger([declared(day), accepted(day), missing_scope], GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("revision_scope", "touches_evaluation_data", "candidate_required", "invalidated"),
    [
        ("INITIALIZATION_DATA", True, False, True),
        ("NON_EVALUATION_EVIDENCE", False, False, False),
    ],
)
def test_source_revision_scope_is_explicit_coherent_and_replayable(
    revision_scope,
    touches_evaluation_data,
    candidate_required,
    invalidated,
):
    day = date(2025, 10, 13)
    revision = source_revision(
        candidate_date=day if candidate_required else None,
        event_at=f"{day.isoformat()}T18:00:00+08:00",
        revision_scope=revision_scope,
        touches_evaluation_data=touches_evaluation_data,
    )

    records = seal_ledger([declared(day), accepted(day), revision], GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert replay.terminal_class == ("INVALIDATED" if invalidated else None)


def test_source_revision_rejects_scope_flag_contradiction_and_missing_evaluation_date():
    day = date(2025, 10, 13)
    contradictory = source_revision(
        event_at=f"{day.isoformat()}T18:00:00+08:00",
        revision_scope="NON_EVALUATION_EVIDENCE",
        touches_evaluation_data=True,
    )
    missing_candidate = source_revision(
        event_at=f"{day.isoformat()}T18:00:00+08:00",
        revision_scope="ACCEPTED_EVALUATION_DATA",
        touches_evaluation_data=True,
    )

    with pytest.raises(ContractViolation, match="contradicts revision_scope"):
        seal_ledger([declared(day), accepted(day), contradictory], GENESIS, AVAILABLE)
    with pytest.raises(ContractViolation, match="linked FETCH_ATTEMPT"):
        seal_ledger([declared(day), accepted(day), missing_candidate], GENESIS, AVAILABLE)


@pytest.mark.parametrize("invalid_value", [None, "false"])
def test_replay_rejects_rechained_non_boolean_revision_scope_flag(invalid_value):
    day = date(2025, 10, 13)
    revision = accepted_revision_attempt(day, f"{day.isoformat()}T18:00:00+08:00")
    records = [
        json.loads(raw)
        for raw in seal_ledger([declared(day), accepted(day), revision], GENESIS, AVAILABLE)
    ]
    records[-1]["touches_evaluation_data"] = invalid_value

    with pytest.raises(ContractViolation, match="touches_evaluation_data.*boolean"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


def test_replay_rejects_rechained_revision_missing_explicit_scope_flag():
    day = date(2025, 10, 13)
    revision = accepted_revision_attempt(day, f"{day.isoformat()}T18:00:00+08:00")
    records = [
        json.loads(raw)
        for raw in seal_ledger([declared(day), accepted(day), revision], GENESIS, AVAILABLE)
    ]
    records[-1].pop("touches_evaluation_data")

    with pytest.raises(
        ContractViolation,
        match="touches_evaluation_data.*required|requires field 'touches_evaluation_data'",
    ):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("event_at", "expected_silent_misses"),
    [
        ("2025-10-13T15:44:59+08:00", 0),
        ("2025-10-13T15:45:00+08:00", 1),
        ("2025-10-13T23:00:00+08:00", 0),
        ("2025-10-14T10:00:00+08:00", 0),
    ],
)
def test_failed_attempt_silent_miss_uses_full_post_close_instant(
    event_at,
    expected_silent_misses,
):
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, event_at, "TARGET_DATE_ABSENT")

    records = seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)

    assert sum(json.loads(record)["event_type"] == "SILENT_MISS" for record in records) == (
        expected_silent_misses
    )
    replay_ledger(records, GENESIS, AVAILABLE)


def test_next_day_failed_attempt_replays_deadline_breach_instead_of_pending_miss():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(
        day,
        "2025-10-14T10:00:00+08:00",
        "TARGET_DATE_ABSENT",
    )
    records = seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)
    assert [json.loads(record)["event_type"] for record in records[-2:]] == [
        "DEADLINE_EXPIRED",
        "FETCH_ATTEMPT",
    ]
    assert replay_ledger(records, GENESIS, AVAILABLE).terminal_class == "INVALIDATED"


def failed_attempt(day: date, event_at: str, outcome: str = "TARGET_DATE_ABSENT") -> dict:
    return exact_fetch_attempt(day, event_at, outcome)


def exact_fetch_attempt(
    day: date,
    event_at: str,
    outcome: str = "VALID_TARGET_ROW",
) -> dict:
    response_exists = outcome != "TRANSPORT_ERROR"
    valid_row = outcome == "VALID_TARGET_ROW"
    parsed_row_exists = valid_row or outcome == "INVALID_OHLC"
    request_instant = datetime.fromisoformat(event_at)
    request_at_asia_shanghai = request_instant.astimezone(
        timezone(timedelta(hours=8))
    ).isoformat(timespec="seconds")
    request_date = request_at_asia_shanghai[:10]
    return {
        "event_type": "FETCH_ATTEMPT",
        "candidate_date": day.isoformat(),
        "event_at": event_at,
        "attempt_outcome": outcome,
        "request_url": (
            "https://vip.stock.finance.sina.com.cn/q/view/"
            "download_gold_history.php?breed=AU9999&start=2021-01-01"
            f"&end={request_date}"
        ),
        "request_at": event_at,
        "request_at_asia_shanghai": request_at_asia_shanghai,
        "response_at": event_at if response_exists else None,
        "response_at_asia_shanghai": request_at_asia_shanghai if response_exists else None,
        "collector_sha256": BUILD_SHA256,
        "parser_sha256": BUILD_SHA256,
        "build_sha256": BUILD_SHA256,
        "runtime_sha256": BUILD_SHA256,
        "http_status": 500 if outcome == "HTTP_ERROR" else (200 if response_exists else None),
        "response_headers": RESPONSE_HEADERS if response_exists else None,
        "response_headers_sha256": RESPONSE_HEADERS_SHA256 if response_exists else None,
        "response_byte_length": 128 if response_exists else None,
        "raw_byte_sha256": ROW_SHA256 if response_exists else None,
        "parsed_row_sha256": ROW_SHA256 if parsed_row_exists else None,
        "history_row_sha256s": (
            {day.isoformat(): ROW_SHA256}
            if valid_row
            else ({} if outcome in {"TARGET_DATE_ABSENT", "DUPLICATE_TARGET_DATE", "INVALID_OHLC"} else None)
        ),
        "error_details": None if valid_row else f"synthetic {outcome.lower()}",
        "evidence_sha256": [ROW_SHA256, BUILD_SHA256, RESPONSE_HEADERS_SHA256],
        "reason": "synthetic offline frozen-source capture",
    }


def accepted_revision_attempt(day: date, event_at: str) -> dict:
    """Return a captured response that omits an already accepted target row."""

    attempt = exact_fetch_attempt(day, event_at, "TARGET_DATE_ABSENT")
    attempt["raw_byte_sha256"] = ALTERNATE_ROW_SHA256
    attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)
    return attempt


@pytest.mark.parametrize("event_time", ["20:00:00", "20:01:00"])
@pytest.mark.parametrize("outcome", ["TARGET_DATE_ABSENT", "TRANSPORT_ERROR"])
def test_d1_failed_attempt_at_or_after_deadline_seals_and_replays_breach(event_time, outcome):
    day = date(2025, 10, 13)
    attempt = failed_attempt(day, f"{day.isoformat()}T{event_time}+08:00", outcome)

    records = seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)
    sealed = [json.loads(record) for record in records]

    assert [record["event_type"] for record in sealed] == [
        "DATE_DECLARED",
        "DEADLINE_EXPIRED",
        "FETCH_ATTEMPT",
    ]
    assert sealed[-1]["prior_state"] == "PROTOCOL_BREACH"
    assert replay.states[day.isoformat()] == "PROTOCOL_BREACH"
    assert replay.max_ordinal == 0
    assert replay.terminal_class == "INVALIDATED"


def test_replay_rejects_rechained_post_deadline_attempt_without_expiry_record():
    day = date(2025, 10, 13)
    attempt = failed_attempt(day, f"{day.isoformat()}T15:44:59+08:00")
    records = [
        json.loads(record)
        for record in seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)
    ]
    records[-1]["event_at_asia_shanghai"] = f"{day.isoformat()}T20:00:00+08:00"
    records[-1]["event_at_utc"] = f"{day.isoformat()}T12:00:00Z"

    with pytest.raises(ContractViolation, match="preceding DEADLINE_EXPIRED"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


def _pending_d252_events(genesis: dict) -> tuple[list[dict], date, date]:
    events, d252 = d252_prefix(genesis)
    recovery_day = d252 + timedelta(days=1)
    while True:
        status = "EXPECTED_PENDING" if recovery_day.weekday() < 5 else "NON_SESSION"
        events.append(declared(recovery_day, status))
        if status == "EXPECTED_PENDING":
            return events, d252, recovery_day
        recovery_day += timedelta(days=1)


@pytest.mark.parametrize("event_time", ["15:45:00", "15:46:00"])
@pytest.mark.parametrize("outcome", ["TARGET_DATE_ABSENT", "TRANSPORT_ERROR"])
def test_d252_failed_attempt_at_or_after_recovery_deadline_seals_breach(event_time, outcome):
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2024-12-31T00:00:00Z"}
    events, d252, recovery_day = _pending_d252_events(genesis)
    attempt = failed_attempt(d252, f"{recovery_day.isoformat()}T{event_time}+08:00", outcome)

    records = seal_ledger([*events, attempt], genesis, AVAILABLE)
    replay = replay_ledger(records, genesis, AVAILABLE)
    sealed = [json.loads(record) for record in records]

    assert [record["event_type"] for record in sealed[-2:]] == [
        "DEADLINE_EXPIRED",
        "FETCH_ATTEMPT",
    ]
    assert replay.states[d252.isoformat()] == "PROTOCOL_BREACH"
    assert replay.max_ordinal == 251
    assert replay.terminal_class == "INVALIDATED"


def test_within_response_duplicate_atomically_seals_and_replays_breach():
    day = date(2025, 10, 13)
    duplicate_attempt = failed_attempt(
        day,
        f"{day.isoformat()}T16:00:00+08:00",
        "DUPLICATE_TARGET_DATE",
    )

    records = seal_ledger([declared(day), duplicate_attempt], GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)
    event_types = [json.loads(record)["event_type"] for record in records]

    assert event_types == [
        "DATE_DECLARED",
        "FETCH_ATTEMPT",
        "SILENT_MISS",
        "PROTOCOL_BREACH_RECORDED",
    ]
    assert replay.states[day.isoformat()] == "PROTOCOL_BREACH"
    assert replay.terminal_class == "INVALIDATED"

    with pytest.raises(ContractViolation, match="duplicate.*breach"):
        replay_ledger(records[:-1], GENESIS, AVAILABLE)


def test_identical_duplicate_requires_accepted_canonical_hash_baseline():
    day = date(2025, 10, 13)
    duplicate = {
        "event_type": "IDENTICAL_DUPLICATE_OBSERVED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T17:00:00+08:00",
        "canonical_row_sha256": ROW_SHA256,
        "observed_row_sha256": ROW_SHA256,
        "evidence_sha256": [ROW_SHA256],
    }

    with pytest.raises(ContractViolation, match="linked FETCH_ATTEMPT"):
        seal_ledger([declared(day), duplicate], GENESIS, AVAILABLE)


def test_identical_duplicate_rejects_unequal_or_unverified_hashes():
    day = date(2025, 10, 13)
    duplicate = {
        "event_type": "IDENTICAL_DUPLICATE_OBSERVED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T17:00:00+08:00",
        "canonical_row_sha256": ROW_SHA256,
        "observed_row_sha256": BUILD_SHA256,
        "evidence_sha256": [ROW_SHA256, BUILD_SHA256],
    }

    with pytest.raises(ContractViolation, match="linked FETCH_ATTEMPT"):
        seal_ledger([declared(day), accepted(day), duplicate], GENESIS, AVAILABLE)

    duplicate["observed_row_sha256"] = ROW_SHA256
    duplicate["canonical_row_sha256"] = BUILD_SHA256
    with pytest.raises(ContractViolation, match="linked FETCH_ATTEMPT"):
        seal_ledger([declared(day), accepted(day), duplicate], GENESIS, AVAILABLE)


@pytest.mark.parametrize("revision_scope", ["INITIALIZATION_DATA", "ACCEPTED_EVALUATION_DATA"])
def test_post_d252_revision_invalidates_without_extending_terminal_coverage(revision_scope):
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2024-12-31T00:00:00Z"}
    events, d252, recovery_day = _pending_d252_events(genesis)
    final_acceptance = accepted(d252)
    final_acceptance.pop("target_sealed_at")
    final_acceptance["event_at"] = f"{recovery_day.isoformat()}T15:45:00+08:00"
    revision_day = recovery_day + timedelta(days=1)
    if revision_scope == "ACCEPTED_EVALUATION_DATA":
        revision = accepted_revision_attempt(
            d252,
            f"{revision_day.isoformat()}T09:00:00+08:00",
        )
    else:
        revision = source_revision(
            event_at=f"{revision_day.isoformat()}T09:00:00+08:00",
            revision_scope=revision_scope,
            touches_evaluation_data=True,
        )

    records = seal_ledger([*events, final_acceptance, revision], genesis, AVAILABLE)
    replay = replay_ledger(records, genesis, AVAILABLE)
    sealed_revision = json.loads(records[-1])

    assert replay.max_ordinal == 252
    assert replay.terminal_coverage_date == recovery_day.isoformat()
    assert sorted(replay.states)[-1] == recovery_day.isoformat()
    assert replay.breach_civil_date == revision_day.isoformat()
    assert replay.terminal_class == "INVALIDATED"
    assert sealed_revision["breach_civil_date"] == revision_day.isoformat()


@pytest.mark.parametrize("trigger", ["valid_fetch", "external_provenance"])
def test_deadline_expiry_is_a_state_invariant_for_every_later_event(trigger):
    day = date(2025, 10, 13)
    if trigger == "valid_fetch":
        event = exact_fetch_attempt(day, f"{day.isoformat()}T20:00:00+08:00")
    else:
        event = {
            "event_type": "EXTERNAL_PRODUCTION_CHANGE",
            "reason": "synthetic unrelated provenance",
            "evidence_sha256": [ROW_SHA256],
        }
        event["event_at"] = "2025-10-14T09:00:00+08:00"

    records = seal_ledger([declared(day), event], GENESIS, AVAILABLE)
    sealed = [json.loads(record) for record in records]
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert [record["event_type"] for record in sealed[:2]] == [
        "DATE_DECLARED",
        "DEADLINE_EXPIRED",
    ]
    assert replay.states[day.isoformat()] == "PROTOCOL_BREACH"
    assert replay.terminal_class == "INVALIDATED"


def test_canonical_acceptance_requires_and_seals_matching_valid_attempt_provenance():
    day = date(2025, 10, 13)
    calendar = _test_calendar([declared(day), accepted(day)])
    with pytest.raises(ContractViolation, match="VALID_TARGET_ROW"):
        _seal_ledger([declared(day), accepted(day)], GENESIS, AVAILABLE, calendar)

    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    records = _seal_ledger(
        [declared(day), attempt, accepted(day)],
        GENESIS,
        AVAILABLE,
        calendar,
        ARTIFACT_BYTE_LENGTHS,
    )
    sealed_acceptance = json.loads(records[-1])

    assert sealed_acceptance["linked_attempt_sequence"] == 2
    assert sealed_acceptance["canonical_raw_sha256"] == ROW_SHA256
    assert sealed_acceptance["canonical_row_sha256"] == ROW_SHA256
    assert sealed_acceptance["capture_at"] == "2025-10-13T08:00:00Z"
    assert sealed_acceptance["parser_sha256"] == BUILD_SHA256
    assert sealed_acceptance["build_sha256"] == BUILD_SHA256
    replay_ledger(records, GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    "missing_field",
    [
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
    ],
)
def test_fetch_attempt_exact_schema_rejects_missing_provenance(missing_field):
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    attempt.pop(missing_field)

    with pytest.raises(ContractViolation, match=missing_field):
        seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)


def test_replay_rejects_rechained_canonical_attempt_link_mismatch():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    records = [
        json.loads(record)
        for record in _seal_ledger(
            [declared(day), attempt, accepted(day)],
            GENESIS,
            AVAILABLE,
            _test_calendar([declared(day), attempt, accepted(day)]),
            ARTIFACT_BYTE_LENGTHS,
        )
    ]
    records[-1]["linked_attempt_sequence"] = 1

    with pytest.raises(ContractViolation, match="attempt"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("event_at", "accepted_by_genesis_boundary"),
    [
        ("2025-10-12T07:59:59+08:00", False),
        ("2025-10-12T08:00:00+08:00", True),
        ("2025-10-12T00:00:01Z", True),
    ],
)
def test_event_timestamp_cannot_predate_genesis_seal(event_at, accepted_by_genesis_boundary):
    day = date(2025, 10, 13)
    declaration = declared(day)
    declaration["event_at"] = event_at

    if accepted_by_genesis_boundary:
        records = seal_ledger([declaration], GENESIS, AVAILABLE)
        replay_ledger(records, GENESIS, AVAILABLE)
    else:
        with pytest.raises(ContractViolation, match="genesis"):
            seal_ledger([declaration], GENESIS, AVAILABLE)


def test_replay_rejects_correctly_rechained_pre_genesis_event():
    day = date(2025, 10, 13)
    record = json.loads(seal_ledger([declared(day)], GENESIS, AVAILABLE)[0])
    record["event_at_asia_shanghai"] = "2025-10-12T07:59:59+08:00"
    record["event_at_utc"] = "2025-10-11T23:59:59Z"

    with pytest.raises(ContractViolation, match="genesis"):
        replay_ledger([_rechain_single_record(record)], GENESIS, AVAILABLE)


@pytest.mark.parametrize("field_name", ["parser_sha256", "build_sha256"])
def test_canonical_acceptance_rejects_attempt_identity_mismatch(field_name):
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    acceptance = accepted(day)
    acceptance[field_name] = CALENDAR_SOURCE_SHA256
    acceptance["evidence_sha256"].append(CALENDAR_SOURCE_SHA256)

    with pytest.raises(ContractViolation, match=f"{field_name}.*attempt"):
        _seal_ledger(
            [declared(day), attempt, acceptance],
            GENESIS,
            AVAILABLE,
            _test_calendar([declared(day), attempt, acceptance]),
            ARTIFACT_BYTE_LENGTHS,
        )


def test_valid_target_attempt_requires_immediate_canonical_acceptance():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    access = operator_access(day)
    access["event_at"] = f"{day.isoformat()}T16:01:00+08:00"

    with pytest.raises(ContractViolation, match="VALID_TARGET_ROW.*followed"):
        _seal_ledger(
            [declared(day), attempt, access],
            GENESIS,
            AVAILABLE,
            _test_calendar([declared(day), attempt, access]),
            ARTIFACT_BYTE_LENGTHS,
        )


def test_d252_valid_attempt_before_recovery_deadline_accepts_at_exact_boundary():
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2024-12-31T00:00:00Z"}
    events, d252, recovery_day = _pending_d252_events(genesis)
    attempt = exact_fetch_attempt(
        d252,
        f"{recovery_day.isoformat()}T15:44:00+08:00",
    )
    acceptance = accepted(d252)
    acceptance.pop("target_sealed_at")
    acceptance["event_at"] = f"{recovery_day.isoformat()}T15:45:00+08:00"

    records = seal_ledger([*events, attempt, acceptance], genesis, AVAILABLE)
    replay = replay_ledger(records, genesis, AVAILABLE)

    assert replay.max_ordinal == 252
    assert replay.terminal_class is None
    assert json.loads(records[-1])["linked_attempt_sequence"] == len(records) - 1


def test_valid_target_response_before_completed_close_fails_seal_and_replay():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    attempt["request_at"] = f"{day.isoformat()}T13:59:00+08:00"
    attempt["request_at_asia_shanghai"] = f"{day.isoformat()}T13:59:00+08:00"
    attempt["response_at"] = f"{day.isoformat()}T14:00:00+08:00"
    attempt["response_at_asia_shanghai"] = f"{day.isoformat()}T14:00:00+08:00"
    acceptance = accepted(day)
    calendar = _test_calendar([declared(day), attempt, acceptance])

    with pytest.raises(ContractViolation, match="response.*15:45"):
        _seal_ledger(
            [declared(day), attempt, acceptance],
            GENESIS,
            AVAILABLE,
            calendar,
            ARTIFACT_BYTE_LENGTHS,
        )

    valid_attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    records = [
        json.loads(record)
        for record in _seal_ledger(
            [declared(day), valid_attempt, acceptance],
            GENESIS,
            AVAILABLE,
            calendar,
            ARTIFACT_BYTE_LENGTHS,
        )
    ]
    records[1]["request_at"] = "2025-10-13T05:59:00Z"
    records[1]["request_at_asia_shanghai"] = "2025-10-13T13:59:00+08:00"
    records[1]["response_at"] = "2025-10-13T06:00:00Z"
    records[1]["response_at_asia_shanghai"] = "2025-10-13T14:00:00+08:00"

    with pytest.raises(ContractViolation, match="response.*15:45"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE, calendar)


def test_target_seal_before_canonical_response_fails_seal_and_replay():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    acceptance = accepted(day)
    acceptance["event_at"] = f"{day.isoformat()}T16:01:00+08:00"
    acceptance["target_sealed_at"] = f"{day.isoformat()}T15:59:00+08:00"
    calendar = _test_calendar([declared(day), attempt, acceptance])

    with pytest.raises(ContractViolation, match="target seal.*captured response"):
        _seal_ledger(
            [declared(day), attempt, acceptance],
            GENESIS,
            AVAILABLE,
            calendar,
            ARTIFACT_BYTE_LENGTHS,
        )

    acceptance["target_sealed_at"] = f"{day.isoformat()}T16:00:00+08:00"
    records = [
        json.loads(record)
        for record in _seal_ledger(
            [declared(day), attempt, acceptance],
            GENESIS,
            AVAILABLE,
            calendar,
            ARTIFACT_BYTE_LENGTHS,
        )
    ]
    records[-1]["target_sealed_at"] = f"{day.isoformat()}T15:59:00+08:00"

    with pytest.raises(ContractViolation, match="target seal.*captured response"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE, calendar)


def test_post_acceptance_within_response_duplicate_irreversibly_invalidates():
    day = date(2025, 10, 13)
    duplicate_attempt = failed_attempt(
        day,
        f"{day.isoformat()}T17:00:00+08:00",
        "DUPLICATE_TARGET_DATE",
    )

    records = seal_ledger(
        [declared(day), accepted(day), duplicate_attempt],
        GENESIS,
        AVAILABLE,
    )
    sealed = [json.loads(record) for record in records]
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert [record["event_type"] for record in sealed] == [
        "DATE_DECLARED",
        "FETCH_ATTEMPT",
        "CANONICAL_ROW_ACCEPTED",
        "FETCH_ATTEMPT",
        "PROTOCOL_BREACH_RECORDED",
    ]
    assert sealed[-1]["linked_attempt_sequence"] == sealed[-2]["sequence"]
    assert sealed[-1]["prior_state"] == "EXPECTED_ACCEPTED"
    assert sealed[-1]["next_state"] is None
    assert replay.states[day.isoformat()] == "EXPECTED_ACCEPTED"
    assert replay.ordinals[day.isoformat()] == 1
    assert replay.max_ordinal == 1
    assert replay.breach_civil_date == day.isoformat()
    assert replay.terminal_class == "INVALIDATED"

    with pytest.raises(ContractViolation, match="duplicate target attempt.*linked breach"):
        replay_ledger(records[:-1], GENESIS, AVAILABLE)

    sealed[-1]["linked_attempt_sequence"] = 1
    with pytest.raises(ContractViolation, match="duplicate target attempt.*linked.*breach"):
        replay_ledger(_rechain_records(sealed), GENESIS, AVAILABLE)


@pytest.mark.parametrize("declaration_time", ["20:00:00", "20:01:00"])
def test_late_declaration_atomically_expires_new_pending_date(declaration_time):
    day = date(2025, 10, 13)
    declaration = declared(day)
    declaration["event_at"] = f"{day.isoformat()}T{declaration_time}+08:00"

    records = seal_ledger([declaration], GENESIS, AVAILABLE)
    sealed = [json.loads(record) for record in records]
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert [record["event_type"] for record in sealed] == [
        "DATE_DECLARED",
        "DEADLINE_EXPIRED",
    ]
    assert sealed[-1]["candidate_date"] == day.isoformat()
    assert sealed[-1]["event_at_asia_shanghai"] == declaration["event_at"]
    assert replay.states[day.isoformat()] == "PROTOCOL_BREACH"
    assert replay.max_ordinal == 0
    assert replay.terminal_class == "INVALIDATED"


def test_replay_rejects_rechained_late_declaration_pending_prefix():
    day = date(2025, 10, 13)
    record = json.loads(seal_ledger([declared(day)], GENESIS, AVAILABLE)[0])
    record["event_at_asia_shanghai"] = f"{day.isoformat()}T20:00:00+08:00"
    record["event_at_utc"] = f"{day.isoformat()}T12:00:00Z"

    with pytest.raises(ContractViolation, match="deadline.*expiry"):
        replay_ledger([_rechain_single_record(record)], GENESIS, AVAILABLE)


def test_post_acceptance_equal_full_history_capture_generates_linked_duplicate():
    day = date(2025, 10, 13)
    later_attempt = exact_fetch_attempt(day, f"{day.isoformat()}T17:00:00+08:00")

    records = seal_ledger([declared(day), accepted(day), later_attempt], GENESIS, AVAILABLE)
    sealed = [json.loads(record) for record in records]
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert [record["event_type"] for record in sealed[-2:]] == [
        "FETCH_ATTEMPT",
        "IDENTICAL_DUPLICATE_OBSERVED",
    ]
    duplicate = sealed[-1]
    assert duplicate["linked_attempt_sequence"] == sealed[-2]["sequence"]
    assert duplicate["baseline_raw_sha256"] == ROW_SHA256
    assert duplicate["canonical_row_sha256"] == ROW_SHA256
    assert duplicate["observed_raw_sha256"] == ROW_SHA256
    assert duplicate["observed_row_sha256"] == ROW_SHA256
    assert replay.states[day.isoformat()] == "EXPECTED_ACCEPTED"
    assert replay.max_ordinal == 1
    assert replay.terminal_class is None


@pytest.mark.parametrize("outcome", ["VALID_TARGET_ROW", "TARGET_DATE_ABSENT"])
def test_post_acceptance_changed_or_missing_row_generates_linked_revision(outcome):
    day = date(2025, 10, 13)
    later_attempt = exact_fetch_attempt(day, f"{day.isoformat()}T17:00:00+08:00", outcome)
    later_attempt["raw_byte_sha256"] = ALTERNATE_ROW_SHA256
    later_attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)
    if outcome == "VALID_TARGET_ROW":
        later_attempt["parsed_row_sha256"] = ALTERNATE_ROW_SHA256
        later_attempt["history_row_sha256s"][day.isoformat()] = ALTERNATE_ROW_SHA256

    records = seal_ledger([declared(day), accepted(day), later_attempt], GENESIS, AVAILABLE)
    sealed = [json.loads(record) for record in records]
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert [record["event_type"] for record in sealed[-2:]] == [
        "FETCH_ATTEMPT",
        "SOURCE_REVISION_OBSERVED",
    ]
    revision = sealed[-1]
    assert revision["linked_attempt_sequence"] == sealed[-2]["sequence"]
    assert revision["baseline_raw_sha256"] == ROW_SHA256
    assert revision["canonical_row_sha256"] == ROW_SHA256
    assert revision["observed_raw_sha256"] == ALTERNATE_ROW_SHA256
    assert revision["observed_row_sha256"] == (
        ALTERNATE_ROW_SHA256 if outcome == "VALID_TARGET_ROW" else None
    )
    assert revision["observed_outcome"] == outcome
    assert replay.states[day.isoformat()] == "EXPECTED_ACCEPTED"
    assert replay.max_ordinal == 1
    assert replay.terminal_class == "INVALIDATED"


@pytest.mark.parametrize(
    "tamper",
    ["omit_link", "wrong_sequence", "wrong_identity", "extra_link_field"],
)
def test_replay_rejects_missing_or_tampered_accepted_capture_link(tamper):
    day = date(2025, 10, 13)
    later_attempt = exact_fetch_attempt(day, f"{day.isoformat()}T17:00:00+08:00")
    records = [
        json.loads(record)
        for record in seal_ledger([declared(day), accepted(day), later_attempt], GENESIS, AVAILABLE)
    ]
    if tamper == "omit_link":
        forged = records[:-1]
    elif tamper == "wrong_sequence":
        records[-1]["linked_attempt_sequence"] = records[-2]["sequence"] - 1
        forged = records
    elif tamper == "wrong_identity":
        records[-1]["observed_raw_sha256"] = ALTERNATE_ROW_SHA256
        records[-1]["evidence_sha256"].append(ALTERNATE_ROW_SHA256)
        forged = records
    else:
        records[-1]["unsupported_link_claim"] = "not emitted by the sealer"
        forged = records

    with pytest.raises(
        ContractViolation,
        match=(
            "accepted-date.*linked|linked.*attempt|accepted-date.*unsupported|"
            "IDENTICAL_DUPLICATE_OBSERVED contains unsupported"
        ),
    ):
        replay_ledger(_rechain_records(forged), GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("request_at", "accepted_by_genesis_boundary"),
    [
        ("2025-10-12T07:59:59+08:00", False),
        ("2025-10-12T08:00:00+08:00", False),
        ("2025-10-12T00:00:01Z", False),
    ],
)
def test_fetch_attempt_provenance_cannot_predate_genesis(request_at, accepted_by_genesis_boundary):
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T15:44:59+08:00", "TARGET_DATE_ABSENT")
    attempt["request_at"] = request_at
    request_instant = datetime.fromisoformat(request_at.replace("Z", "+00:00"))
    request_local = request_instant.astimezone(timezone(timedelta(hours=8)))
    attempt["request_at_asia_shanghai"] = request_local.isoformat(timespec="seconds")
    attempt["request_url"] = attempt["request_url"].rsplit("=", 1)[0] + "=" + request_local.date().isoformat()

    if accepted_by_genesis_boundary:
        records = seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)
        replay_ledger(records, GENESIS, AVAILABLE)
    else:
        with pytest.raises(
            ContractViolation,
            match="provenance.*genesis|candidate date.*request end",
        ):
            seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)


def test_replay_rejects_rechained_pre_genesis_attempt_provenance():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T15:44:59+08:00", "TARGET_DATE_ABSENT")
    records = [
        json.loads(record)
        for record in seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)
    ]
    records[-1]["request_at"] = "2025-10-11T23:59:59Z"
    records[-1]["request_at_asia_shanghai"] = "2025-10-12T07:59:59+08:00"
    records[-1]["request_url"] = records[-1]["request_url"].rsplit("=", 1)[0] + "=2025-10-12"

    with pytest.raises(
        ContractViolation,
        match="provenance.*genesis|candidate date.*request end",
    ):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


def test_d252_rejects_fractional_post_recovery_boundary_in_seal_and_replay():
    genesis = {"contract": "synthetic-offline-fixture", "sealed_at": "2024-12-31T00:00:00Z"}
    events, d252, recovery_day = _pending_d252_events(genesis)
    attempt = exact_fetch_attempt(d252, f"{recovery_day.isoformat()}T15:44:59+08:00")
    acceptance = accepted(d252)
    acceptance.pop("target_sealed_at")
    acceptance["event_at"] = f"{recovery_day.isoformat()}T15:45:00.900000+08:00"

    with pytest.raises(ContractViolation, match="whole-second"):
        seal_ledger([*events, attempt, acceptance], genesis, AVAILABLE)

    acceptance["event_at"] = f"{recovery_day.isoformat()}T15:45:00+08:00"
    records = [
        json.loads(record)
        for record in seal_ledger([*events, attempt, acceptance], genesis, AVAILABLE)
    ]
    records[-1]["event_at_asia_shanghai"] = (
        f"{recovery_day.isoformat()}T15:45:00.900000+08:00"
    )
    records[-1]["event_at_utc"] = f"{recovery_day.isoformat()}T07:45:00.900000Z"

    with pytest.raises(ContractViolation, match="whole-second"):
        replay_ledger(_rechain_records(records, genesis), genesis, AVAILABLE)


@pytest.mark.parametrize("outcome", ["DECODE_ERROR", "SCHEMA_ERROR", "HTTP_ERROR"])
def test_post_acceptance_uncomparable_response_generates_linked_revision(outcome):
    day = date(2025, 10, 13)
    later_attempt = exact_fetch_attempt(day, f"{day.isoformat()}T17:00:00+08:00", outcome)

    records = seal_ledger([declared(day), accepted(day), later_attempt], GENESIS, AVAILABLE)
    sealed = [json.loads(record) for record in records]
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert [record["event_type"] for record in sealed[-2:]] == [
        "FETCH_ATTEMPT",
        "SOURCE_REVISION_OBSERVED",
    ]
    assert sealed[-1]["linked_attempt_sequence"] == sealed[-2]["sequence"]
    assert sealed[-1]["observed_outcome"] == outcome
    assert sealed[-1]["observed_raw_sha256"] == (
        ROW_SHA256
    )
    assert sealed[-1]["observed_row_sha256"] is None
    assert replay.states[day.isoformat()] == "EXPECTED_ACCEPTED"
    assert replay.max_ordinal == 1
    assert replay.terminal_class == "INVALIDATED"

    with pytest.raises(ContractViolation, match="accepted-date.*linked"):
        replay_ledger(records[:-1], GENESIS, AVAILABLE)


def test_post_acceptance_no_response_transport_failure_remains_provenance_only():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T17:00:00+08:00", "TRANSPORT_ERROR")

    records = seal_ledger([declared(day), accepted(day), attempt], GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)

    assert json.loads(records[-1])["event_type"] == "FETCH_ATTEMPT"
    assert replay.states[day.isoformat()] == "EXPECTED_ACCEPTED"
    assert replay.max_ordinal == 1
    assert replay.terminal_class is None


def test_bare_correction_is_rejected_by_exact_schema():
    day = date(2025, 10, 13)
    bare = {
        "event_type": "CORRECTION",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T17:00:00+08:00",
        "evidence_sha256": [ROW_SHA256],
    }

    with pytest.raises(ContractViolation, match="CORRECTION requires field"):
        seal_ledger([declared(day), accepted(day), bare], GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("scope", "invalidated"),
    [
        ("CLERICAL_METADATA", False),
        ("EVALUATION_MARKET_DATA", True),
    ],
)
def test_correction_scope_derives_decision_relevance_and_replays(scope, invalidated):
    day = date(2025, 10, 13)
    prefix_events = [declared(day), accepted(day)]
    if scope == "CLERICAL_METADATA":
        diagnostic = exact_fetch_attempt(
            day,
            f"{day.isoformat()}T17:00:00+08:00",
            "TRANSPORT_ERROR",
        )
        sealed_diagnostic = json.loads(
            seal_ledger([*prefix_events, diagnostic], GENESIS, AVAILABLE)[-1]
        )
        access = operator_access(day)
        access["event_at"] = f"{day.isoformat()}T17:01:00+08:00"
        access.update(
            {
                "linked_diagnostic_sequence": sealed_diagnostic["sequence"],
                "diagnostic_record_sha256": sealed_diagnostic["record_sha256"],
                "diagnostic_purpose": "TRANSPORT_ERROR",
            }
        )
        prefix_events.extend([diagnostic, access])
    prefix = seal_ledger(prefix_events, GENESIS, AVAILABLE)
    superseded = json.loads(prefix[-1])
    correction = correction_event(
        day=day,
        superseded_sequence=superseded["sequence"],
        superseded_record_sha256=superseded["record_sha256"],
        correction_scope=scope,
    )
    if scope == "CLERICAL_METADATA":
        correction["event_at"] = f"{day.isoformat()}T17:02:00+08:00"

    records = seal_ledger([*prefix_events, correction], GENESIS, AVAILABLE)
    replay = replay_ledger(records, GENESIS, AVAILABLE)
    sealed = json.loads(records[-1])

    assert replay.states[day.isoformat()] == "EXPECTED_ACCEPTED"
    assert replay.max_ordinal == 1
    assert replay.terminal_class == ("INVALIDATED" if invalidated else None)
    assert ("breach_civil_date" in sealed) is invalidated


def test_clerical_scope_cannot_bypass_a_decision_bearing_superseded_record():
    day = date(2025, 10, 13)
    prefix = seal_ledger([declared(day), accepted(day)], GENESIS, AVAILABLE)
    superseded = json.loads(prefix[-1])
    correction = correction_event(
        day=day,
        superseded_sequence=superseded["sequence"],
        superseded_record_sha256=superseded["record_sha256"],
        correction_scope="CLERICAL_METADATA",
    )

    with pytest.raises(ContractViolation, match="clerical.*decision-bearing"):
        seal_ledger([declared(day), accepted(day), correction], GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_issuer", "CORRECTION requires field"),
        ("extra_field", "CORRECTION contains unsupported field"),
        ("wrong_superseded_hash", "superseded record"),
        ("clerical_decision_change", "clerical.*decision surface"),
        ("missing_source_identity", "source.*evidence"),
    ],
)
def test_correction_rejects_missing_extra_tampered_or_unproved_claims(mutation, message):
    day = date(2025, 10, 13)
    prefix = seal_ledger([declared(day), accepted(day)], GENESIS, AVAILABLE)
    superseded = json.loads(prefix[-1])
    correction = correction_event(
        day=day,
        superseded_sequence=superseded["sequence"],
        superseded_record_sha256=superseded["record_sha256"],
        correction_scope="CLERICAL_METADATA",
    )
    if mutation == "missing_issuer":
        correction.pop("issuer")
    elif mutation == "extra_field":
        correction["caller_claimed_noninvalidating"] = True
    elif mutation == "wrong_superseded_hash":
        correction["superseded_record_sha256"] = ALTERNATE_ROW_SHA256
    elif mutation == "clerical_decision_change":
        correction["decision_surface_after_sha256"] = ALTERNATE_ROW_SHA256
    else:
        correction["source_sha256s"] = [HOLIDAY_2025_SHA256]

    with pytest.raises(ContractViolation, match=message):
        seal_ledger([declared(day), accepted(day), correction], GENESIS, AVAILABLE)


def test_replay_rejects_rechained_correction_with_tampered_scope_or_record_link():
    day = date(2025, 10, 13)
    prefix_events = [declared(day), accepted(day)]
    diagnostic = exact_fetch_attempt(
        day,
        f"{day.isoformat()}T17:00:00+08:00",
        "TRANSPORT_ERROR",
    )
    sealed_diagnostic = json.loads(
        seal_ledger([*prefix_events, diagnostic], GENESIS, AVAILABLE)[-1]
    )
    access = operator_access(day)
    access["event_at"] = f"{day.isoformat()}T17:01:00+08:00"
    access.update(
        {
            "linked_diagnostic_sequence": sealed_diagnostic["sequence"],
            "diagnostic_record_sha256": sealed_diagnostic["record_sha256"],
            "diagnostic_purpose": "TRANSPORT_ERROR",
        }
    )
    prefix_events.extend([diagnostic, access])
    prefix = seal_ledger(prefix_events, GENESIS, AVAILABLE)
    superseded = json.loads(prefix[-1])
    correction = correction_event(
        day=day,
        superseded_sequence=superseded["sequence"],
        superseded_record_sha256=superseded["record_sha256"],
        correction_scope="CLERICAL_METADATA",
    )
    correction["event_at"] = f"{day.isoformat()}T17:02:00+08:00"
    records = [
        json.loads(record)
        for record in seal_ledger([*prefix_events, correction], GENESIS, AVAILABLE)
    ]

    records[-1]["superseded_record_sha256"] = ALTERNATE_ROW_SHA256
    with pytest.raises(ContractViolation, match="superseded record"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)

    records = [
        json.loads(record)
        for record in seal_ledger([*prefix_events, correction], GENESIS, AVAILABLE)
    ]
    records[-1]["correction_scope"] = "EVALUATION_MARKET_DATA"
    records[-1]["decision_surface_after_sha256"] = ALTERNATE_ROW_SHA256
    with pytest.raises(ContractViolation, match="generated field presence|state transition"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    "request_url",
    [
        "https://attacker.invalid/q/view/download_gold_history.php?breed=AU9999&start=2021-01-01&end=2025-10-13",
        "https://vip.stock.finance.sina.com.cn/q/view/other.php?breed=AU9999&start=2021-01-01&end=2025-10-13",
        "https://vip.stock.finance.sina.com.cn/q/view/download_gold_history.php?breed=AU9999&start=2021-01-02&end=2025-10-13",
        "https://vip.stock.finance.sina.com.cn/q/view/download_gold_history.php?breed=AU9999&start=2021-01-01&end=2030-01-01",
        "https://vip.stock.finance.sina.com.cn/q/view/download_gold_history.php?start=2021-01-01&breed=AU9999&end=2025-10-13",
    ],
)
def test_fetch_attempt_rejects_noncanonical_or_future_addressed_source(request_url):
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    attempt["request_url"] = request_url

    with pytest.raises(ContractViolation, match="frozen Sina endpoint"):
        seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_headers", "response_headers"),
        ("wrong_headers_hash", "response headers hash"),
        ("missing_byte_length", "response_byte_length"),
        ("boolean_byte_length", "response_byte_length.*integer"),
        ("incoherent_local_request", "request timestamp pair"),
        ("incoherent_local_response", "response timestamp pair"),
    ],
)
def test_fetch_attempt_rejects_missing_or_tampered_response_metadata(mutation, message):
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    if mutation == "missing_headers":
        attempt.pop("response_headers")
    elif mutation == "wrong_headers_hash":
        attempt["response_headers_sha256"] = ALTERNATE_ROW_SHA256
        attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)
    elif mutation == "missing_byte_length":
        attempt.pop("response_byte_length")
    elif mutation == "boolean_byte_length":
        attempt["response_byte_length"] = True
    elif mutation == "incoherent_local_request":
        attempt["request_at_asia_shanghai"] = "2025-10-13T16:00:01+08:00"
    else:
        attempt["response_at_asia_shanghai"] = "2025-10-13T16:00:01+08:00"

    with pytest.raises(ContractViolation, match=message):
        seal_ledger([declared(day), attempt], GENESIS, AVAILABLE)


def test_canonical_acceptance_binds_response_metadata_identity():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    acceptance = accepted(day)
    acceptance["evidence_sha256"].remove(RESPONSE_HEADERS_SHA256)

    with pytest.raises(ContractViolation, match="acceptance evidence.*attempt"):
        _seal_ledger(
            [declared(day), attempt, acceptance],
            GENESIS,
            AVAILABLE,
            _test_calendar([declared(day), attempt, acceptance]),
            ARTIFACT_BYTE_LENGTHS,
        )


def test_event_schema_registry_is_exhaustive_and_date_declaration_is_closed():
    assert set(EVENT_FIELD_SCHEMAS) == EVENT_TYPES
    day = date(2025, 10, 13)
    declaration = declared(day)
    declaration["reason"] = "declare from authoritative calendar"
    declaration["caller_claimed_override"] = {"initial_status": "EXPECTED_ACCEPTED"}

    with pytest.raises(ContractViolation, match="DATE_DECLARED contains unsupported field"):
        seal_ledger([declaration], GENESIS, AVAILABLE)


@pytest.mark.parametrize("invalid_reason", [None, False, "", "   "])
def test_every_event_reason_must_be_a_nonempty_string(invalid_reason):
    day = date(2025, 10, 13)
    declaration = declared(day)
    declaration["reason"] = invalid_reason

    with pytest.raises(ContractViolation, match="reason must be nonempty"):
        seal_ledger([declaration], GENESIS, AVAILABLE)


def test_observation_claims_cannot_bypass_fetch_attempt_provenance():
    day = date(2025, 10, 13)
    unlinked_duplicate = {
        "event_type": "IDENTICAL_DUPLICATE_OBSERVED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T17:00:00+08:00",
        "canonical_row_sha256": ROW_SHA256,
        "observed_row_sha256": ROW_SHA256,
        "evidence_sha256": [ROW_SHA256],
        "reason": "caller-asserted duplicate without a capture",
    }
    unlinked_revision = source_revision(
        candidate_date=day,
        event_at=f"{day.isoformat()}T17:00:00+08:00",
        revision_scope="ACCEPTED_EVALUATION_DATA",
        touches_evaluation_data=True,
    )
    unlinked_revision["reason"] = "caller-asserted accepted-data revision without a capture"

    with pytest.raises(ContractViolation, match="linked FETCH_ATTEMPT"):
        seal_ledger([declared(day), accepted(day), unlinked_duplicate], GENESIS, AVAILABLE)
    with pytest.raises(ContractViolation, match="linked FETCH_ATTEMPT"):
        seal_ledger([declared(day), accepted(day), unlinked_revision], GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("source", "frozen Sina endpoint"),
        ("missing_headers", "response_headers"),
        ("headers_hash", "response headers hash"),
        ("byte_length", "response_byte_length.*integer"),
    ],
)
def test_replay_rejects_rechained_source_or_response_metadata_tampering(mutation, message):
    day = date(2025, 10, 13)
    records = [
        json.loads(record)
        for record in seal_ledger([declared(day), accepted(day)], GENESIS, AVAILABLE)
    ]
    attempt = records[1]
    if mutation == "source":
        attempt["request_url"] = (
            "https://attacker.invalid/q/view/download_gold_history.php"
            "?breed=AU9999&start=2021-01-01&end=2025-10-13"
        )
    elif mutation == "missing_headers":
        attempt.pop("response_headers")
    elif mutation == "headers_hash":
        attempt["response_headers_sha256"] = ALTERNATE_ROW_SHA256
        attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)
    else:
        attempt["response_byte_length"] = False

    with pytest.raises(ContractViolation, match=message):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


@pytest.mark.parametrize("mutation", ["unknown_field", "invalid_reason"])
def test_replay_rejects_rechained_open_ended_or_untyped_date_claim(mutation):
    day = date(2025, 10, 13)
    record = json.loads(seal_ledger([declared(day)], GENESIS, AVAILABLE)[0])
    if mutation == "unknown_field":
        record["caller_claimed_override"] = {"initial_status": "EXPECTED_ACCEPTED"}
        message = "DATE_DECLARED contains unsupported field"
    else:
        record["reason"] = False
        message = "reason must be nonempty"

    with pytest.raises(ContractViolation, match=message):
        replay_ledger([_rechain_single_record(record)], GENESIS, AVAILABLE)


@pytest.mark.parametrize("observation", ["duplicate", "revision"])
def test_replay_rejects_rechained_observation_with_capture_link_removed(observation):
    day = date(2025, 10, 13)
    later_attempt = (
        exact_fetch_attempt(day, f"{day.isoformat()}T17:00:00+08:00")
        if observation == "duplicate"
        else accepted_revision_attempt(day, f"{day.isoformat()}T17:00:00+08:00")
    )
    records = [
        json.loads(record)
        for record in seal_ledger([declared(day), accepted(day), later_attempt], GENESIS, AVAILABLE)
    ]
    forged = records[-1]
    for field_name in {
        "linked_attempt_sequence",
        "baseline_raw_sha256",
        "observed_raw_sha256",
        "observed_outcome",
    }:
        forged.pop(field_name)
    if observation == "duplicate":
        forged.pop("observed_row_sha256")

    with pytest.raises(ContractViolation, match="linked FETCH_ATTEMPT"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


def test_full_history_capture_detects_revision_outside_named_candidate():
    first = date(2025, 10, 13)
    second = date(2025, 10, 14)
    attempt = exact_fetch_attempt(second, f"{second.isoformat()}T16:00:00+08:00")
    attempt["history_row_sha256s"] = {
        first.isoformat(): ALTERNATE_ROW_SHA256,
        second.isoformat(): ROW_SHA256,
    }
    attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)

    records = seal_ledger(
        [declared(first), accepted(first), declared(second), attempt],
        GENESIS,
        AVAILABLE,
    )
    sealed = [json.loads(record) for record in records]

    assert [record["event_type"] for record in sealed[-2:]] == [
        "FETCH_ATTEMPT",
        "SOURCE_REVISION_OBSERVED",
    ]
    assert sealed[-1]["linked_attempt_sequence"] == sealed[-2]["sequence"]
    assert sealed[-1]["comparison_findings"] == [
        {
            "baseline_row_sha256": ROW_SHA256,
            "candidate_date": first.isoformat(),
            "change_type": "CHANGED",
            "observed_row_sha256": ALTERNATE_ROW_SHA256,
        }
    ]
    assert replay_ledger(records, GENESIS, AVAILABLE).terminal_class == "INVALIDATED"


def test_zero_byte_valid_response_cannot_assign_an_ordinal():
    day = date(2025, 10, 13)
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    attempt["response_byte_length"] = 0

    with pytest.raises(ContractViolation, match="positive|byte length|response_byte_length"):
        _seal_ledger(
            [declared(day), attempt, accepted(day)],
            GENESIS,
            AVAILABLE,
            _test_calendar([declared(day), attempt, accepted(day)]),
            ARTIFACT_BYTE_LENGTHS,
        )


def test_operator_access_requires_exact_operator_and_files_viewed_audit():
    day = date(2025, 10, 13)
    bare = {
        "event_type": "OPERATOR_ACCESS",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T16:30:00+08:00",
        "linked_diagnostic_sequence": 1,
        "diagnostic_record_sha256": ROW_SHA256,
        "diagnostic_purpose": "SILENT_MISS",
        "reason": "diagnose synthetic source alert",
        "evidence_sha256": [ROW_SHA256],
    }

    with pytest.raises(ContractViolation, match="operator_identity|access_identity|files_viewed"):
        seal_ledger([declared(day), bare], GENESIS, AVAILABLE)


@pytest.mark.parametrize("breach_time", ["20:00:00", "20:01:00"])
def test_generic_breach_cannot_substitute_for_derived_deadline_expiry(breach_time):
    day = date(2025, 10, 13)
    generic = {
        "event_type": "PROTOCOL_BREACH_RECORDED",
        "candidate_date": day.isoformat(),
        "event_at": f"{day.isoformat()}T{breach_time}+08:00",
        "breach_reason": "caller supplied generic deadline claim",
        "reason": "must not replace DEADLINE_EXPIRED",
        "evidence_sha256": [ROW_SHA256],
    }

    with pytest.raises(ContractViolation, match="DEADLINE_EXPIRED"):
        seal_ledger([declared(day), generic], GENESIS, AVAILABLE)


@pytest.mark.parametrize(
    ("warmup_observation", "change_type"),
    [
        (ALTERNATE_ROW_SHA256, "CHANGED"),
        (None, "DELETED"),
    ],
)
def test_full_history_capture_compares_frozen_warmup_rows(warmup_observation, change_type):
    day = date(2025, 10, 13)
    warmup_date = "2025-10-10"
    genesis = {**GENESIS, "warmup_row_sha256s": {warmup_date: ROW_SHA256}}
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    if warmup_observation is not None:
        attempt["history_row_sha256s"][warmup_date] = warmup_observation
        attempt["evidence_sha256"].append(warmup_observation)

    records = _seal_ledger(
        [declared(day), attempt],
        genesis,
        AVAILABLE,
        _test_calendar([declared(day), attempt], genesis),
        ARTIFACT_BYTE_LENGTHS,
    )
    revision = json.loads(records[-1])

    assert revision["event_type"] == "SOURCE_REVISION_OBSERVED"
    assert revision["revision_scope"] == "INITIALIZATION_DATA"
    assert revision["comparison_findings"] == [
        {
            "baseline_row_sha256": ROW_SHA256,
            "candidate_date": warmup_date,
            "change_type": change_type,
            "observed_row_sha256": warmup_observation,
        }
    ]
    assert replay_ledger(records, genesis, AVAILABLE).terminal_class == "INVALIDATED"


def test_full_history_capture_detects_inserted_pre_start_row():
    day = date(2025, 10, 13)
    inserted_date = "2025-10-10"
    attempt = exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00")
    attempt["history_row_sha256s"][inserted_date] = ALTERNATE_ROW_SHA256
    attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)

    records = _seal_ledger(
        [declared(day), attempt],
        GENESIS,
        AVAILABLE,
        _test_calendar([declared(day), attempt]),
        ARTIFACT_BYTE_LENGTHS,
    )
    revision = json.loads(records[-1])

    assert revision["comparison_findings"] == [
        {
            "baseline_row_sha256": None,
            "candidate_date": inserted_date,
            "change_type": "INSERTED",
            "observed_row_sha256": ALTERNATE_ROW_SHA256,
        }
    ]
    assert replay_ledger(records, GENESIS, AVAILABLE).terminal_class == "INVALIDATED"


def test_full_history_capture_detects_missing_earlier_accepted_row_and_replay_tamper():
    first = date(2025, 10, 13)
    second = date(2025, 10, 14)
    first_attempt = exact_fetch_attempt(first, f"{first.isoformat()}T16:00:00+08:00")
    second_attempt = exact_fetch_attempt(second, f"{second.isoformat()}T16:00:00+08:00")
    calendar = _test_calendar([declared(first), declared(second)])
    records = list(
        _seal_ledger(
            [declared(first), first_attempt, accepted(first), declared(second), second_attempt],
            GENESIS,
            AVAILABLE,
            calendar,
            ARTIFACT_BYTE_LENGTHS,
        )
    )
    sealed = [json.loads(record) for record in records]

    assert sealed[-1]["comparison_findings"][0] == {
        "baseline_row_sha256": ROW_SHA256,
        "candidate_date": first.isoformat(),
        "change_type": "DELETED",
        "observed_row_sha256": None,
    }
    assert replay_ledger(records, GENESIS, AVAILABLE, calendar).terminal_class == "INVALIDATED"

    sealed[-2]["history_row_sha256s"][first.isoformat()] = ROW_SHA256
    with pytest.raises(ContractViolation, match="full-history|comparison"):
        replay_ledger(_rechain_records(sealed), GENESIS, AVAILABLE, calendar)


def test_uncomparable_later_capture_replays_revision_before_linked_silent_miss():
    first = date(2025, 10, 13)
    second = date(2025, 10, 14)
    failed = exact_fetch_attempt(second, f"{second.isoformat()}T16:00:00+08:00", "SCHEMA_ERROR")

    records = seal_ledger(
        [declared(first), accepted(first), declared(second), failed],
        GENESIS,
        AVAILABLE,
    )
    sealed = [json.loads(record) for record in records]

    assert [record["event_type"] for record in sealed[-3:]] == [
        "FETCH_ATTEMPT",
        "SOURCE_REVISION_OBSERVED",
        "SILENT_MISS",
    ]
    assert sealed[-2]["comparison_findings"][0]["candidate_date"] == first.isoformat()
    assert sealed[-1]["linked_attempt_sequence"] == sealed[-3]["sequence"]
    assert replay_ledger(records, GENESIS, AVAILABLE).terminal_class == "INVALIDATED"


def test_response_byte_length_is_bound_to_captured_artifact_in_seal_and_replay():
    day = date(2025, 10, 13)
    events = [
        declared(day),
        exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00"),
        accepted(day),
    ]
    calendar = _test_calendar(events)
    with pytest.raises(ContractViolation, match="byte length identity"):
        _seal_ledger(events, GENESIS, AVAILABLE, calendar)

    records = [json.loads(record) for record in seal_ledger(events, GENESIS, AVAILABLE)]
    records[1]["response_byte_length"] = 127
    with pytest.raises(ContractViolation, match="byte length.*captured bytes"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE, calendar)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_operator", "operator_identity"),
        ("empty_operator", "operator_identity"),
        ("missing_access", "access_identity_sha256"),
        ("unbound_access", "access identity.*evidence"),
        ("empty_files", "files_viewed"),
        ("extra_file_field", "exact path/hash"),
        ("unbound_file", "viewed-file identity.*evidence"),
    ],
)
def test_operator_access_rejects_incomplete_or_unbound_audit(mutation, message):
    day = date(2025, 10, 13)
    access = operator_access(day)
    if mutation == "missing_operator":
        access.pop("operator_identity")
    elif mutation == "empty_operator":
        access["operator_identity"] = ""
    elif mutation == "missing_access":
        access.pop("access_identity_sha256")
    elif mutation == "unbound_access":
        access["access_identity_sha256"] = ALTERNATE_ROW_SHA256
    elif mutation == "empty_files":
        access["files_viewed"] = []
    elif mutation == "extra_file_field":
        access["files_viewed"][0]["mode"] = "read"
    else:
        access["files_viewed"][0]["artifact_sha256"] = ALTERNATE_ROW_SHA256

    with pytest.raises(ContractViolation, match=message):
        seal_ledger([declared(day), access], GENESIS, AVAILABLE)


def test_operator_access_replays_exact_audit_and_rejects_rechained_tamper():
    day = date(2025, 10, 13)
    failed = exact_fetch_attempt(
        day,
        f"{day.isoformat()}T15:45:00+08:00",
        "TARGET_DATE_ABSENT",
    )
    diagnostic = json.loads(seal_ledger([declared(day), failed], GENESIS, AVAILABLE)[-1])
    access = operator_access(day)
    access.update(
        {
            "linked_diagnostic_sequence": diagnostic["sequence"],
            "diagnostic_record_sha256": diagnostic["record_sha256"],
            "diagnostic_purpose": "SILENT_MISS",
        }
    )
    records = [
        json.loads(record)
        for record in seal_ledger([declared(day), failed, access], GENESIS, AVAILABLE)
    ]
    replay_ledger(tuple(canonical_json_bytes(record) for record in records), GENESIS, AVAILABLE)

    records[-1]["files_viewed"][0]["artifact_sha256"] = ALTERNATE_ROW_SHA256
    with pytest.raises(ContractViolation, match="viewed-file identity.*evidence"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


def test_replay_rejects_rechained_generic_deadline_substitute():
    day = date(2025, 10, 13)
    records = [
        json.loads(record)
        for record in seal_ledger(
            [
                declared(day),
                {
                    "event_type": "DEADLINE_EXPIRED",
                    "candidate_date": day.isoformat(),
                    "event_at": f"{day.isoformat()}T20:00:00+08:00",
                    "reason": "strict deadline reached",
                    "evidence_sha256": [ROW_SHA256],
                },
            ],
            GENESIS,
            AVAILABLE,
        )
    ]
    records[-1]["event_type"] = "PROTOCOL_BREACH_RECORDED"
    records[-1]["breach_reason"] = "forged generic substitute"

    with pytest.raises(ContractViolation, match="preceding DEADLINE_EXPIRED"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


def test_rework_14_s1_initialization_seal_is_required_before_authoritative_open():
    day = date(2025, 10, 13)
    post_holiday_calendar = {
        day.isoformat(): facts(no_prior_night=True),
        (day + timedelta(days=1)).isoformat(): facts(),
    }
    missing = {key: value for key, value in GENESIS.items() if key != "initialization_seal"}

    with pytest.raises(ContractViolation, match="initialization seal"):
        _seal_ledger(
            [declared(day)],
            missing,
            AVAILABLE,
            post_holiday_calendar,
            ARTIFACT_BYTE_LENGTHS,
        )

    ordinary_monday_calendar = {
        day.isoformat(): facts(),
        (day + timedelta(days=1)).isoformat(): facts(),
    }
    with pytest.raises(ContractViolation, match="strictly before.*S1 open"):
        _seal_ledger(
            [declared(day)],
            GENESIS,
            AVAILABLE,
            ordinary_monday_calendar,
            ARTIFACT_BYTE_LENGTHS,
        )

    records = _seal_ledger(
        [declared(day)],
        GENESIS,
        AVAILABLE,
        post_holiday_calendar,
        ARTIFACT_BYTE_LENGTHS,
    )
    assert _replay_ledger(
        records,
        GENESIS,
        AVAILABLE,
        post_holiday_calendar,
        ARTIFACT_BYTE_LENGTHS,
    ).states == {day.isoformat(): "EXPECTED_PENDING"}


def test_rework_14_replay_rejects_post_holiday_initialization_at_s1_open():
    day = date(2025, 10, 13)
    calendar = {
        day.isoformat(): facts(no_prior_night=True),
        (day + timedelta(days=1)).isoformat(): facts(),
    }
    records = [
        json.loads(record)
        for record in _seal_ledger(
            [declared(day)],
            GENESIS,
            AVAILABLE,
            calendar,
            ARTIFACT_BYTE_LENGTHS,
        )
    ]
    invalid_genesis = {
        **GENESIS,
        "initialization_seal": {
            **INITIALIZATION_SEAL,
            "sealed_at": "2025-10-13T09:00:00+08:00",
        },
    }

    with pytest.raises(ContractViolation, match="strictly before.*S1 open"):
        _replay_ledger(
            _rechain_records(records, invalid_genesis),
            invalid_genesis,
            AVAILABLE,
            calendar,
            ARTIFACT_BYTE_LENGTHS,
        )


@pytest.mark.parametrize("future_shape", ["candidate_after_end", "history_after_end"])
def test_rework_14_request_end_bounds_candidate_and_full_history_in_seal_and_replay(future_shape):
    day = date(2025, 10, 13)
    events = [
        declared(day),
        exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00"),
        accepted(day),
    ]
    attempt = events[1]
    if future_shape == "candidate_after_end":
        attempt["request_at"] = "2025-10-12T16:00:00+08:00"
        attempt["request_at_asia_shanghai"] = "2025-10-12T16:00:00+08:00"
        attempt["request_url"] = attempt["request_url"].replace(
            "end=2025-10-13", "end=2025-10-12"
        )
        message = "candidate date.*request end"
    else:
        attempt["history_row_sha256s"]["2030-01-01"] = ALTERNATE_ROW_SHA256
        attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)
        message = "history row date.*request end"

    with pytest.raises(ContractViolation, match=message):
        seal_ledger(events, GENESIS, AVAILABLE)

    valid_records = [
        json.loads(record)
        for record in seal_ledger(
            [
                declared(day),
                exact_fetch_attempt(day, f"{day.isoformat()}T16:00:00+08:00"),
                accepted(day),
            ],
            GENESIS,
            AVAILABLE,
        )
    ]
    sealed_attempt = valid_records[1]
    if future_shape == "candidate_after_end":
        sealed_attempt["request_at"] = "2025-10-12T08:00:00Z"
        sealed_attempt["request_at_asia_shanghai"] = "2025-10-12T16:00:00+08:00"
        sealed_attempt["request_url"] = sealed_attempt["request_url"].replace(
            "end=2025-10-13", "end=2025-10-12"
        )
    else:
        sealed_attempt["history_row_sha256s"]["2030-01-01"] = ALTERNATE_ROW_SHA256
        sealed_attempt["evidence_sha256"].append(ALTERNATE_ROW_SHA256)
    with pytest.raises(ContractViolation, match=message):
        replay_ledger(_rechain_records(valid_records), GENESIS, AVAILABLE)


def test_rework_14_every_event_requires_candidate_coverage_through_its_civil_date():
    day = date(2025, 10, 13)
    no_candidate_event = {
        "event_type": "EXTERNAL_PRODUCTION_CHANGE",
        "event_at": f"{day.isoformat()}T09:00:00+08:00",
        "reason": "synthetic external provenance",
        "evidence_sha256": [ROW_SHA256],
    }
    with pytest.raises(ContractViolation, match="candidate-date coverage.*event civil date"):
        seal_ledger([no_candidate_event], GENESIS, AVAILABLE)

    late_event = {**no_candidate_event, "event_at": "2025-10-20T09:00:00+08:00"}
    with pytest.raises(ContractViolation, match="candidate-date coverage.*event civil date"):
        seal_ledger([declared(day), accepted(day), late_event], GENESIS, AVAILABLE)


def test_rework_14_replay_rejects_rechained_provenance_event_with_candidate_gap():
    day = date(2025, 10, 13)
    external = {
        "event_type": "EXTERNAL_PRODUCTION_CHANGE",
        "event_at": f"{day.isoformat()}T17:00:00+08:00",
        "reason": "synthetic external provenance",
        "evidence_sha256": [ROW_SHA256],
    }
    records = [
        json.loads(record)
        for record in seal_ledger([declared(day), accepted(day), external], GENESIS, AVAILABLE)
    ]
    records[-1]["event_at_asia_shanghai"] = "2025-10-20T09:00:00+08:00"
    records[-1]["event_at_utc"] = "2025-10-20T01:00:00Z"

    with pytest.raises(ContractViolation, match="candidate-date coverage.*event civil date"):
        replay_ledger(_rechain_records(records), GENESIS, AVAILABLE)


def test_rework_14_operator_access_requires_an_existing_eligible_diagnostic():
    day = date(2025, 10, 13)
    with pytest.raises(ContractViolation, match="diagnostic"):
        seal_ledger([declared(day), operator_access(day)], GENESIS, AVAILABLE)

    declaration = json.loads(seal_ledger([declared(day)], GENESIS, AVAILABLE)[0])
    mismatched = operator_access(day)
    mismatched.update(
        {
            "linked_diagnostic_sequence": declaration["sequence"],
            "diagnostic_record_sha256": declaration["record_sha256"],
            "diagnostic_purpose": "SILENT_MISS",
        }
    )
    with pytest.raises(ContractViolation, match="eligible diagnostic"):
        seal_ledger([declared(day), mismatched], GENESIS, AVAILABLE)


def test_rework_14_linked_diagnostic_access_seals_replays_and_rejects_mismatched_link():
    day = date(2025, 10, 13)
    failed = exact_fetch_attempt(
        day,
        f"{day.isoformat()}T15:45:00+08:00",
        "TARGET_DATE_ABSENT",
    )
    prefix = [json.loads(record) for record in seal_ledger([declared(day), failed], GENESIS, AVAILABLE)]
    diagnostic = prefix[-1]
    access = operator_access(day)
    access.update(
        {
            "linked_diagnostic_sequence": diagnostic["sequence"],
            "diagnostic_record_sha256": diagnostic["record_sha256"],
            "diagnostic_purpose": "SILENT_MISS",
        }
    )
    records = seal_ledger([declared(day), failed, access], GENESIS, AVAILABLE)
    replay_ledger(records, GENESIS, AVAILABLE)

    tampered = [json.loads(record) for record in records]
    tampered[-1]["diagnostic_record_sha256"] = ROW_SHA256
    with pytest.raises(ContractViolation, match="diagnostic.*does not reproduce"):
        replay_ledger(_rechain_records(tampered), GENESIS, AVAILABLE)
