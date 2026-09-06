from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import stat
import weakref
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .corporate_actions import (
    CorporateActionEvidenceError,
    SettlementSchedule,
    accounting_cash_dividends,
    admit_corporate_action_evidence,
    dividend_tax_burden,
    rounding_policy_identity,
    tax_policy_identity,
)


ISSUER = "quant-platform/total-return-qualification@1"
IDENTITY_DOMAIN = "quant-platform/total-return-qualification/v1"
CLAIM_STATES = {
    "PRICE_RETURN_ONLY",
    "KNOWN_EVENT_CORRECTED_PARTIAL",
    "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
    "AFTER_TAX_TOTAL_RETURN_VERIFIED",
}
COVERAGE_STATES = {
    "UNKNOWN_MISSING",
    "VERIFIED_EVENTS",
    "VERIFIED_NO_ACTION",
    "VERIFIED_COMPLETE_INTERVAL",
}
COMPLETE_COVERAGE_STATES = {"VERIFIED_NO_ACTION", "VERIFIED_COMPLETE_INTERVAL"}
SOURCE_ISSUER_CLAIMS = {
    "CORPORATE_ACTION_COLLECTOR": {"FORBIDDEN", "KNOWN_EVENT_CORRECTED_PARTIAL"},
    "STRATEGY_REPLAY": {"PRICE_RETURN_ONLY", "KNOWN_EVENT_CORRECTED_PARTIAL"},
    "STRATEGY_RUNNER": {"PRICE_RETURN_ONLY", "KNOWN_EVENT_CORRECTED_PARTIAL"},
    "HISTORICAL_RECORD": {
        "FORBIDDEN",
        "PRICE_RETURN_ONLY",
        "KNOWN_EVENT_CORRECTED_PARTIAL",
        "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
    },
}
LEGAL_TRANSITIONS = {
    "PRICE_RETURN_ONLY": CLAIM_STATES,
    "KNOWN_EVENT_CORRECTED_PARTIAL": {
        "KNOWN_EVENT_CORRECTED_PARTIAL",
        "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
        "AFTER_TAX_TOTAL_RETURN_VERIFIED",
    },
    "AFTER_TAX_TOTAL_RETURN_UNVERIFIED": {
        "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
        "AFTER_TAX_TOTAL_RETURN_VERIFIED",
    },
    "AFTER_TAX_TOTAL_RETURN_VERIFIED": {"AFTER_TAX_TOTAL_RETURN_VERIFIED"},
}
SAME_EVIDENCE_FORBIDDEN = {
    ("PRICE_RETURN_ONLY", "AFTER_TAX_TOTAL_RETURN_VERIFIED"),
    ("KNOWN_EVENT_CORRECTED_PARTIAL", "AFTER_TAX_TOTAL_RETURN_VERIFIED"),
    ("AFTER_TAX_TOTAL_RETURN_UNVERIFIED", "AFTER_TAX_TOTAL_RETURN_VERIFIED"),
}
CHECK_FIELDS = {
    "immutable_bundle",
    "dataset_binding",
    "coverage_complete",
    "event_set_complete",
    "policy_applicable",
    "strategy_control_parity",
    "ledgers_complete",
    "exact_fen_quantity",
    "settled",
    "causal_separation",
    "controls_and_metrics",
    "issuer_authorized",
}
BINDING_FIELDS = {
    "corporate_action_evidence_sha256",
    "raw_artifact_sha256s",
    "event_revision_ids",
    "tax_policy_id",
    "tax_policy_sha256",
    "settlement_policy_id",
    "settlement_policy_sha256",
    "rounding_policy_id",
    "rounding_policy_sha256",
    "parent_snapshot_id",
    "execution_view_snapshot_id",
    "parent_corporate_action_evidence_sha256",
    "view_corporate_action_evidence_sha256",
    "scoring_mask_sha256",
    "causal_feature_cutoff",
    "accounting_outcome_checked_as_of",
    "accounting_outcome_use_role",
    "result_digest",
    "run_manifest_sha256",
    "account_events_sha256",
    "account_trades_sha256",
    "accounts",
    "control_parity_sha256",
}
SHA_BINDINGS = BINDING_FIELDS - {
    "raw_artifact_sha256s",
    "event_revision_ids",
    "causal_feature_cutoff",
    "accounting_outcome_checked_as_of",
    "accounting_outcome_use_role",
    "accounts",
}
ACCOUNT_FIELDS = {"events_sha256", "trades_sha256", "final_state_sha256"}
ACCOUNT_NAMES = {"strategy", "zero_cost", "buy_and_hold"}
REASON_CODES = {
    "PRICE_ONLY",
    "KNOWN_EVENT_PARTIAL",
    "UNKNOWN_OR_MISSING_COVERAGE",
    "CONFLICTED_OR_QUARANTINED_EVENT",
    "UNSUPPORTED_SCOPE_OR_ACTION",
    "COMPLETE_CONTRACT_MISSING",
    "POLICY_INAPPLICABLE_OR_MISSING",
    "SETTLEMENT_INCOMPLETE",
    "TAX_UNSETTLED",
    "RECONCILIATION_FAILED",
    "CONTROL_LEDGER_MISSING",
    "CONTROL_PARITY_FAILED",
    "DIGEST_MISMATCH",
    "CAUSAL_LEAKAGE",
    "STALE_SCHEMA",
    "HISTORICALLY_EXPOSED",
    "HISTORICAL_EXPOSURE_UNKNOWN",
    "OTHER_STUDY_GATE_FAILED",
}
FAILURE_REASONS = {
    "immutable_bundle": "DIGEST_MISMATCH",
    "dataset_binding": "DIGEST_MISMATCH",
    "coverage_complete": "UNKNOWN_OR_MISSING_COVERAGE",
    "event_set_complete": "CONFLICTED_OR_QUARANTINED_EVENT",
    "policy_applicable": "POLICY_INAPPLICABLE_OR_MISSING",
    "strategy_control_parity": "CONTROL_PARITY_FAILED",
    "ledgers_complete": "CONTROL_LEDGER_MISSING",
    "exact_fen_quantity": "RECONCILIATION_FAILED",
    "settled": "SETTLEMENT_INCOMPLETE",
    "causal_separation": "CAUSAL_LEAKAGE",
    "controls_and_metrics": "RECONCILIATION_FAILED",
    "issuer_authorized": "DIGEST_MISMATCH",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_FIELDS = {
    "schema_version",
    "qualification_id",
    "issuer",
    "source_issuer",
    "source_total_return_claim",
    "claim_state",
    "coverage_state",
    "coverage_basis",
    "coverage_id",
    "complete_contract_id",
    "equivalent_contract_approval_id",
    "bindings",
    "checks",
    "ranking",
    "transition",
}


class TotalReturnQualificationError(ValueError):
    """Raised before untrusted total-return evidence can become a capability."""


def _validate_json(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, str) or type(value) is bool:
        return
    if type(value) is int:
        return
    if isinstance(value, float):
        raise TotalReturnQualificationError(f"floating-point value is forbidden at {path}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TotalReturnQualificationError(f"non-string object key at {path}")
        for key, item in value.items():
            _validate_json(item, f"{path}.{key}")
        return
    raise TotalReturnQualificationError(f"unsupported JSON value at {path}")


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load_strict_json(payload: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise TotalReturnQualificationError(f"duplicate key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_float=lambda value: (_ for _ in ()).throw(
                TotalReturnQualificationError(f"floating-point value is forbidden: {value}")
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                TotalReturnQualificationError(f"non-finite value is forbidden: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TotalReturnQualificationError("qualification is not strict JSON") from exc
    if not isinstance(value, dict):
        raise TotalReturnQualificationError("qualification must be an object")
    return value


def _sha256(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TotalReturnQualificationError(f"{label} must be a lower-case SHA-256 value")
    return value


def qualification_id(record_without_id: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        IDENTITY_DOMAIN.encode("utf-8") + b"\0" + canonical_json_bytes(record_without_id)
    ).hexdigest()


def _capability():
    issued: dict[int, tuple[weakref.ReferenceType, bytes]] = {}

    class TrustedTotalReturnQualification(dict[str, Any]):
        __slots__ = ("__weakref__",)

    def issue(record: Mapping[str, Any]) -> Any:
        capability = TrustedTotalReturnQualification(copy.deepcopy(dict(record)))
        identifier = id(capability)

        def discard(reference: weakref.ReferenceType) -> None:
            current = issued.get(identifier)
            if current is not None and current[0] is reference:
                issued.pop(identifier, None)

        reference = weakref.ref(capability, discard)
        issued[identifier] = (reference, canonical_json_bytes(capability))
        return capability

    def pristine(value: Any) -> bool:
        entry = issued.get(id(value))
        if entry is None or entry[0]() is not value:
            return False
        try:
            return canonical_json_bytes(value) == entry[1]
        except (TypeError, ValueError):
            return False

    def qualify(**arguments: Any) -> Any:
        record, prior_bytes = _qualification_record_from_evidence(**arguments)
        _validate_record(record, prior_bytes)
        return issue(record)

    return TrustedTotalReturnQualification, qualify, pristine


TrustedTotalReturnQualification, qualify_total_return, is_trusted_qualification = _capability()


def _validate_bindings(bindings: Any, *, verified: bool) -> dict[str, Any]:
    if not isinstance(bindings, dict) or set(bindings) != BINDING_FIELDS:
        raise TotalReturnQualificationError("qualification binding fields are invalid")
    for field in SHA_BINDINGS:
        _sha256(bindings[field], field, nullable=not verified)
    for field in ("raw_artifact_sha256s", "event_revision_ids"):
        values = bindings[field]
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise TotalReturnQualificationError(f"{field} must be a unique array")
        for value in values:
            _sha256(value, field)
    accounts = bindings["accounts"]
    if not isinstance(accounts, dict) or set(accounts) != ACCOUNT_NAMES:
        raise TotalReturnQualificationError("exactly three account bindings are required")
    for account, value in accounts.items():
        if not isinstance(value, dict) or set(value) != ACCOUNT_FIELDS:
            raise TotalReturnQualificationError(f"{account} binding fields are invalid")
        for field, digest in value.items():
            _sha256(digest, f"{account}.{field}")
    if verified:
        for field in (
            "causal_feature_cutoff",
            "accounting_outcome_checked_as_of",
        ):
            if not isinstance(bindings[field], str) or not bindings[field]:
                raise TotalReturnQualificationError(f"verified {field} is required")
        if bindings["accounting_outcome_use_role"] != "ACCOUNTING_OUTCOME":
            raise TotalReturnQualificationError("verified outcome evidence role is invalid")
    elif bindings["accounting_outcome_use_role"] not in {None, "ACCOUNTING_OUTCOME"}:
        raise TotalReturnQualificationError("outcome evidence role is invalid")
    return bindings


def _validate_record(record: dict[str, Any], prior_record_bytes: bytes | None) -> None:
    if set(record) != _RECORD_FIELDS:
        raise TotalReturnQualificationError("qualification fields are invalid")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise TotalReturnQualificationError("unsupported qualification schema version")
    if record["issuer"] != ISSUER:
        raise TotalReturnQualificationError("qualification issuer is not authorized")
    source_claims = SOURCE_ISSUER_CLAIMS.get(record["source_issuer"])
    if source_claims is None or record["source_total_return_claim"] not in source_claims:
        raise TotalReturnQualificationError("source issuer/claim combination is forbidden")
    claim_state = record["claim_state"]
    coverage_state = record["coverage_state"]
    if claim_state not in CLAIM_STATES or coverage_state not in COVERAGE_STATES:
        raise TotalReturnQualificationError("claim or coverage state is invalid")
    if record["coverage_basis"] not in {
        "NONE",
        "COMPLETE_ENUMERATION_CONTRACT",
        "APPROVED_EQUIVALENT_CONTRACT",
    }:
        raise TotalReturnQualificationError("coverage basis is invalid")
    _sha256(record["coverage_id"], "coverage_id", nullable=claim_state != "AFTER_TAX_TOTAL_RETURN_VERIFIED")
    _sha256(
        record["complete_contract_id"],
        "complete_contract_id",
        nullable=claim_state != "AFTER_TAX_TOTAL_RETURN_VERIFIED",
    )
    approval = record["equivalent_contract_approval_id"]
    if record["coverage_basis"] == "APPROVED_EQUIVALENT_CONTRACT":
        _sha256(approval, "equivalent_contract_approval_id")
    elif approval is not None:
        raise TotalReturnQualificationError("equivalent approval is not applicable")
    checks = record["checks"]
    if not isinstance(checks, dict) or set(checks) != CHECK_FIELDS or any(
        type(value) is not bool for value in checks.values()
    ):
        raise TotalReturnQualificationError("qualification checks are invalid")
    verified = claim_state == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
    _validate_bindings(record["bindings"], verified=verified)
    if verified and (
        coverage_state not in COMPLETE_COVERAGE_STATES
        or record["coverage_basis"] == "NONE"
        or not all(checks.values())
    ):
        raise TotalReturnQualificationError("verified claim predicates are incomplete")
    if claim_state == "KNOWN_EVENT_CORRECTED_PARTIAL" and coverage_state != "VERIFIED_EVENTS":
        raise TotalReturnQualificationError("partial claim requires VERIFIED_EVENTS coverage")
    ranking = record["ranking"]
    if not isinstance(ranking, dict) or set(ranking) != {
        "eligible_for_ranking",
        "eligible_for_promotion",
        "historical_exposure",
        "reason_codes",
    }:
        raise TotalReturnQualificationError("ranking fields are invalid")
    if type(ranking["eligible_for_ranking"]) is not bool or type(
        ranking["eligible_for_promotion"]
    ) is not bool:
        raise TotalReturnQualificationError("eligibility values must be booleans")
    if ranking["historical_exposure"] not in {"PRISTINE", "EXPOSED", "UNKNOWN"}:
        raise TotalReturnQualificationError("historical exposure is invalid")
    reasons = ranking["reason_codes"]
    if (
        not isinstance(reasons, list)
        or len(reasons) != len(set(reasons))
        or any(reason not in REASON_CODES for reason in reasons)
    ):
        raise TotalReturnQualificationError("reason codes are invalid")
    if ranking["eligible_for_promotion"] and not ranking["eligible_for_ranking"]:
        raise TotalReturnQualificationError("promotion requires ranking eligibility")
    allowed_ranking_reasons = (
        ["OTHER_STUDY_GATE_FAILED"]
        if not ranking["eligible_for_promotion"]
        else []
    )
    if ranking["eligible_for_ranking"] and (
        not verified
        or ranking["historical_exposure"] != "PRISTINE"
        or reasons != allowed_ranking_reasons
    ):
        raise TotalReturnQualificationError("ranking eligibility contradicts qualification")
    if ranking["historical_exposure"] == "EXPOSED" and (
        ranking["eligible_for_ranking"]
        or ranking["eligible_for_promotion"]
        or "HISTORICALLY_EXPOSED" not in reasons
    ):
        raise TotalReturnQualificationError("historically exposed evidence is ineligible")
    if ranking["historical_exposure"] == "UNKNOWN" and (
        ranking["eligible_for_ranking"]
        or ranking["eligible_for_promotion"]
        or "HISTORICAL_EXPOSURE_UNKNOWN" not in reasons
    ):
        raise TotalReturnQualificationError("unknown exposure evidence is ineligible")
    required_reason = {
        "PRICE_RETURN_ONLY": "PRICE_ONLY",
        "KNOWN_EVENT_CORRECTED_PARTIAL": "KNOWN_EVENT_PARTIAL",
    }.get(claim_state)
    if required_reason is not None and required_reason not in reasons:
        raise TotalReturnQualificationError("claim state reason is missing")
    if claim_state == "AFTER_TAX_TOTAL_RETURN_UNVERIFIED" and not reasons:
        raise TotalReturnQualificationError("unverified claim requires a reason")
    transition = record["transition"]
    if not isinstance(transition, dict) or set(transition) != {
        "prior_qualification_id",
        "from_state",
        "to_state",
        "same_corporate_action_evidence",
    }:
        raise TotalReturnQualificationError("transition fields are invalid")
    if transition["to_state"] != claim_state or type(
        transition["same_corporate_action_evidence"]
    ) is not bool:
        raise TotalReturnQualificationError("transition target is invalid")
    prior_id = transition["prior_qualification_id"]
    from_state = transition["from_state"]
    if prior_id is None:
        if from_state is not None or transition["same_corporate_action_evidence"]:
            raise TotalReturnQualificationError("unlinked transition has prior state")
        if prior_record_bytes is not None:
            raise TotalReturnQualificationError("unexpected prior qualification bytes")
    else:
        _sha256(prior_id, "prior_qualification_id")
        if from_state not in CLAIM_STATES or claim_state not in LEGAL_TRANSITIONS[from_state]:
            raise TotalReturnQualificationError("qualification transition is illegal")
        if transition["same_corporate_action_evidence"] and (
            from_state,
            claim_state,
        ) in SAME_EVIDENCE_FORBIDDEN:
            raise TotalReturnQualificationError("same evidence cannot upgrade to verified")
        if prior_record_bytes is None:
            raise TotalReturnQualificationError("exact prior qualification bytes are unavailable")
        prior = load_strict_json(prior_record_bytes)
        prior_without_id = {
            key: value for key, value in prior.items() if key != "qualification_id"
        }
        if (
            prior.get("qualification_id") != prior_id
            or qualification_id(prior_without_id) != prior_id
        ):
            raise TotalReturnQualificationError("exact prior qualification identity does not match")
        if prior.get("claim_state") != from_state:
            raise TotalReturnQualificationError("prior qualification state does not match")
    expected_id = qualification_id(
        {key: value for key, value in record.items() if key != "qualification_id"}
    )
    if record["qualification_id"] != expected_id:
        raise TotalReturnQualificationError("qualification identity mismatch")


def _failure_reasons(checks: Mapping[str, bool]) -> list[str]:
    return sorted({FAILURE_REASONS[field] for field, passed in checks.items() if not passed})


_RESULT_DIGEST_FILES = (
    "daily_replay.csv",
    "events.csv",
    "trades.csv",
    "metrics.json",
    "cost_breakdown.json",
)
_SETTLEMENT_FILES = frozenset(
    {
        *_RESULT_DIGEST_FILES,
        "account_events.csv",
        "account_trades.csv",
        "config.json",
        "report.html",
        "run_manifest.json",
    }
)
_ACCOUNT_EVENT_COLUMNS = (
    "account",
    "Date",
    "sequence",
    "event_type",
    "trade_id",
    "lot_id",
    "event_revision_id",
    "quantity",
    "trade_quantity_delta",
    "settled_quantity_delta",
    "cash_delta_fen",
    "cost_fen",
    "note",
    "cash_fen",
    "trade_holdings",
    "settled_holdings",
    "receivable_fen",
    "unpaid_dividend_tax_base_fen",
    "deferred_tax_base_fen",
    "outstanding_tax_fen",
    "market_price_fen",
    "market_value_fen",
    "equity_fen",
)
_ACCOUNT_TRADE_COLUMNS = (
    "account",
    "lot_id",
    "entry_trade_id",
    "exit_trade_id",
    "entry_trade_date",
    "entry_settlement_date",
    "exit_trade_date",
    "exit_settlement_date",
    "quantity",
    "entry_notional_fen",
    "entry_cost_fen",
    "exit_notional_fen",
    "exit_cost_fen",
    "dividend_fen",
    "tax_burden",
    "status",
    "tax_fen",
    "net_pnl_fen",
)
_EVENT_INTEGER_FIELDS = frozenset(
    {
        "sequence",
        "quantity",
        "trade_quantity_delta",
        "settled_quantity_delta",
        "cash_delta_fen",
        "cost_fen",
        "cash_fen",
        "trade_holdings",
        "settled_holdings",
        "receivable_fen",
        "unpaid_dividend_tax_base_fen",
        "deferred_tax_base_fen",
        "outstanding_tax_fen",
        "market_price_fen",
        "market_value_fen",
        "equity_fen",
    }
)
_TRADE_INTEGER_FIELDS = frozenset(
    {
        "quantity",
        "entry_notional_fen",
        "entry_cost_fen",
        "exit_notional_fen",
        "exit_cost_fen",
        "dividend_fen",
        "tax_fen",
        "net_pnl_fen",
    }
)
_FINAL_STATE_FIELDS = frozenset(
    {
        "cash_fen",
        "trade_holdings",
        "settled_holdings",
        "receivable_fen",
        "unpaid_dividend_tax_base_fen",
        "deferred_tax_base_fen",
        "outstanding_tax_fen",
        "market_price_fen",
        "market_value_fen",
        "equity_fen",
    }
)
_ACCOUNT_METRIC_FIELDS = frozenset(
    {
        "initial_capital_fen",
        "final_state",
        "gross_dividend_fen",
        "net_dividend_fen",
        "deferred_tax_fen",
        "collected_tax_fen",
        "outstanding_tax_fen",
        "trading_cost_fen",
        "price_profit_fen",
        "after_tax_profit_fen",
    }
)
_INTEGER_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)")
_MAX_LEDGER_ROWS = 200_000
_MAX_EVIDENCE_BYTES = 128 * 1_048_576
_OUTCOME_PACKAGE_FIELDS = {
    "schema_version",
    "use_role",
    "checked_as_of",
    "attached_after_result_digest",
    "execution_view_snapshot_id",
    "result_artifact_set_sha256",
    "corporate_action_evidence_sha256",
    "files",
}


def _evidence_json(payload: bytes, label: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TotalReturnQualificationError(f"duplicate key in {label}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                TotalReturnQualificationError(f"non-finite value in {label}: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TotalReturnQualificationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise TotalReturnQualificationError(f"{label} must be an object")

    def finite(item: Any) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise TotalReturnQualificationError(f"non-finite value in {label}")
        if isinstance(item, list):
            for child in item:
                finite(child)
        elif isinstance(item, dict):
            for child in item.values():
                finite(child)

    finite(value)
    return value


def _immutable_payloads(state_root: Path | str, result_path: Path | str) -> dict[str, bytes]:
    state = Path(os.path.abspath(os.fspath(state_root)))
    root = Path(os.path.abspath(os.fspath(result_path)))
    try:
        root.relative_to(state)
    except ValueError as exc:
        raise TotalReturnQualificationError("result bundle is outside the trusted state root") from exc
    for path in (state, root):
        metadata = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise TotalReturnQualificationError("result bundle topology is unsafe")
    cursor = root
    while True:
        metadata = os.stat(cursor, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise TotalReturnQualificationError("result bundle path contains a symlink")
        if cursor == state:
            break
        cursor = cursor.parent
    root_metadata = os.stat(root, follow_symlinks=False)
    if stat.S_IMODE(root_metadata.st_mode) & 0o222:
        raise TotalReturnQualificationError("result bundle is mutable")
    names = {entry.name for entry in os.scandir(root)}
    if names != _SETTLEMENT_FILES:
        raise TotalReturnQualificationError("settlement result artifact set is invalid")
    payloads: dict[str, bytes] = {}
    total = 0
    for name in sorted(names):
        path = root / name
        before = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o222
        ):
            raise TotalReturnQualificationError(f"result artifact is not immutable: {name}")
        payload = path.read_bytes()
        after = os.stat(path, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or len(payload) != after.st_size
        ):
            raise TotalReturnQualificationError(f"result artifact changed while reading: {name}")
        payloads[name] = payload
        total += len(payload)
    if total > _MAX_EVIDENCE_BYTES:
        raise TotalReturnQualificationError("result bundle exceeds the evidence byte bound")
    return payloads


def _result_digest(payloads: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in _RESULT_DIGEST_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payloads[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _artifact_set_digest(payloads: Mapping[str, bytes]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
                for name, payload in sorted(payloads.items())
            }
        )
    ).hexdigest()


def _accounting_outcome_evidence(
    *,
    state_root: Path | str,
    result_path: Path | str,
    result_payloads: Mapping[str, bytes],
    expected_result_digest: str,
    execution_view_snapshot_id: str,
) -> tuple[Any, datetime]:
    state = Path(os.path.abspath(os.fspath(state_root)))
    result = Path(os.path.abspath(os.fspath(result_path)))
    package = state / "accounting-outcomes" / expected_result_digest
    try:
        package.relative_to(state)
        metadata = os.stat(package, follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise TotalReturnQualificationError(
            "separate accounting outcome package is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o222
    ):
        raise TotalReturnQualificationError("accounting outcome package is not immutable")
    try:
        result_sealed_ns = max(
            os.stat(result / name, follow_symlinks=False).st_ctime_ns for name in result_payloads
        )
    except OSError as exc:
        raise TotalReturnQualificationError("sealed result attachment time is unavailable") from exc
    if metadata.st_ctime_ns <= result_sealed_ns:
        raise TotalReturnQualificationError(
            "accounting outcome package was not attached after the sealed result"
        )
    manifest_path = package / "manifest.json"
    try:
        manifest_metadata = os.stat(manifest_path, follow_symlinks=False)
        manifest_payload = manifest_path.read_bytes()
    except OSError as exc:
        raise TotalReturnQualificationError("accounting outcome manifest is unavailable") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or stat.S_ISLNK(manifest_metadata.st_mode)
        or manifest_metadata.st_nlink != 1
        or stat.S_IMODE(manifest_metadata.st_mode) & 0o222
    ):
        raise TotalReturnQualificationError("accounting outcome manifest is not immutable")
    manifest = _evidence_json(manifest_payload, "accounting outcome manifest")
    if set(manifest) != _OUTCOME_PACKAGE_FIELDS or manifest.get("schema_version") != 1:
        raise TotalReturnQualificationError("accounting outcome manifest fields are invalid")
    if (
        manifest.get("use_role") != "ACCOUNTING_OUTCOME"
        or manifest.get("attached_after_result_digest") != expected_result_digest
        or manifest.get("execution_view_snapshot_id") != execution_view_snapshot_id
        or manifest.get("result_artifact_set_sha256") != _artifact_set_digest(result_payloads)
    ):
        raise TotalReturnQualificationError(
            "accounting outcome package is not bound after the sealed result"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or "corporate_actions.json" not in files:
        raise TotalReturnQualificationError("accounting outcome artifact map is invalid")
    if set(os.listdir(package)) != {"manifest.json", *files}:
        raise TotalReturnQualificationError("accounting outcome artifact set is invalid")
    package_payloads: dict[str, bytes] = {}
    for name, descriptor in files.items():
        if Path(name).name != name or name == "manifest.json":
            raise TotalReturnQualificationError("accounting outcome artifact name is invalid")
        path = package / name
        before = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o222
        ):
            raise TotalReturnQualificationError("accounting outcome artifact is not immutable")
        if before.st_ctime_ns <= result_sealed_ns:
            raise TotalReturnQualificationError(
                "accounting outcome artifact predates the sealed result"
            )
        payload = path.read_bytes()
        if descriptor != {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }:
            raise TotalReturnQualificationError("accounting outcome artifact digest mismatch")
        package_payloads[name] = payload
    document = _evidence_json(
        package_payloads.pop("corporate_actions.json"),
        "accounting outcome corporate actions",
    )
    artifacts = {
        descriptor["artifact_id"]: package_payloads[descriptor["path"]]
        for descriptor in document.get("artifacts", [])
        if isinstance(descriptor, dict) and descriptor.get("path") in package_payloads
    }
    try:
        admitted = admit_corporate_action_evidence(document, artifacts)
    except (KeyError, CorporateActionEvidenceError) as exc:
        raise TotalReturnQualificationError("accounting outcome evidence is invalid") from exc
    if (
        admitted.digest != manifest.get("corporate_action_evidence_sha256")
        or any(
            revision.get("use_role") != "ACCOUNTING_OUTCOME"
            for revision in admitted.document["revisions"]
        )
    ):
        raise TotalReturnQualificationError("accounting outcome evidence role or identity is invalid")
    encoded_digest = admitted.digest.encode("ascii")
    if any(encoded_digest in payload for payload in result_payloads.values()):
        raise TotalReturnQualificationError(
            "accounting outcome evidence was exposed to the sealed execution bundle"
        )
    checked_as_of = admitted.document["coverage"]["payload"].get("checked_as_of")
    if checked_as_of != manifest.get("checked_as_of"):
        raise TotalReturnQualificationError("accounting outcome checked-as-of differs")
    try:
        checked = datetime.fromisoformat(str(checked_as_of).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TotalReturnQualificationError("accounting outcome checked-as-of is invalid") from exc
    if checked.tzinfo != timezone.utc:
        raise TotalReturnQualificationError("accounting outcome checked-as-of is not UTC")
    return admitted, checked


def _csv_rows(
    payload: bytes,
    columns: tuple[str, ...],
    integer_fields: frozenset[str],
    label: str,
) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TotalReturnQualificationError(f"{label} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != columns:
        raise TotalReturnQualificationError(f"{label} columns are invalid")
    rows: list[dict[str, Any]] = []
    for source in reader:
        if None in source or any(value is None for value in source.values()):
            raise TotalReturnQualificationError(f"{label} row shape is invalid")
        row: dict[str, Any] = dict(source)
        for field in integer_fields:
            raw = row[field]
            if _INTEGER_TEXT.fullmatch(raw) is None:
                raise TotalReturnQualificationError(
                    f"{label} {field} is not an exact integer"
                )
            value = int(raw)
            if not -(2**63) <= value < 2**63:
                raise TotalReturnQualificationError(f"{label} {field} exceeds bounds")
            row[field] = value
        rows.append(row)
        if len(rows) > _MAX_LEDGER_ROWS:
            raise TotalReturnQualificationError(f"{label} exceeds the row bound")
    return rows


def _date_value(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise TotalReturnQualificationError(f"{label} is not a date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise TotalReturnQualificationError(f"{label} is not a date") from exc
    if parsed.isoformat() != value:
        raise TotalReturnQualificationError(f"{label} is not canonical")
    return parsed


def _ledger_bindings_and_close(
    payloads: Mapping[str, bytes],
    metrics: Mapping[str, Any],
    actions: tuple[Any, ...],
) -> tuple[dict[str, dict[str, str]], str, str]:
    events = _csv_rows(
        payloads["account_events.csv"],
        _ACCOUNT_EVENT_COLUMNS,
        _EVENT_INTEGER_FIELDS,
        "account event ledger",
    )
    trades = _csv_rows(
        payloads["account_trades.csv"],
        _ACCOUNT_TRADE_COLUMNS,
        _TRADE_INTEGER_FIELDS,
        "account trade ledger",
    )
    if not events or {row["account"] for row in events} != ACCOUNT_NAMES:
        raise TotalReturnQualificationError("account event ledger lacks the three accounts")
    if not trades or {row["account"] for row in trades} != ACCOUNT_NAMES:
        raise TotalReturnQualificationError("account trade ledger lacks the three accounts")
    facts = metrics.get("accounting_accounts")
    if not isinstance(facts, dict) or set(facts) != ACCOUNT_NAMES:
        raise TotalReturnQualificationError("three-account frozen metrics are missing")
    close = metrics.get("accounting_close_date")
    close_date = _date_value(close, "accounting close")
    bindings: dict[str, dict[str, str]] = {}
    event_rows_by_account: dict[str, list[dict[str, Any]]] = {}
    action_ids = {action.event_revision_id for action in actions}
    if len(action_ids) != len(actions):
        raise TotalReturnQualificationError("terminal corporate-action identity is duplicated")
    initial_capital: int | None = None
    for account in sorted(ACCOUNT_NAMES):
        account_events = [row for row in events if row["account"] == account]
        account_trades = [row for row in trades if row["account"] == account]
        event_rows_by_account[account] = account_events
        corporate_action_rows = [row for row in account_events if row["event_revision_id"]]
        if any(
            row["event_revision_id"] not in action_ids
            or row["event_type"] not in {"DIVIDEND_ENTITLEMENT", "DIVIDEND_PAYMENT"}
            for row in corporate_action_rows
        ):
            raise TotalReturnQualificationError(
                "account ledger contains a substituted corporate action"
            )
        sequences = [row["sequence"] for row in account_events]
        if sequences != list(range(1, len(account_events) + 1)):
            raise TotalReturnQualificationError("account event sequence is incomplete")
        event_dates = [_date_value(row["Date"], "account event date") for row in account_events]
        if event_dates != sorted(event_dates) or event_dates[-1] > close_date:
            raise TotalReturnQualificationError("account event close or ordering is invalid")
        account_facts = facts[account]
        if not isinstance(account_facts, dict) or set(account_facts) != _ACCOUNT_METRIC_FIELDS:
            raise TotalReturnQualificationError("account frozen metric fields are invalid")
        final_state = account_facts.get("final_state")
        if not isinstance(final_state, dict) or set(final_state) != _FINAL_STATE_FIELDS:
            raise TotalReturnQualificationError("account final state fields are invalid")
        exact_values = [
            value
            for field, value in account_facts.items()
            if field != "final_state"
        ] + list(final_state.values())
        if any(type(value) is not int or not -(2**63) <= value < 2**63 for value in exact_values):
            raise TotalReturnQualificationError("account frozen metrics are not bounded integers")
        if initial_capital is None:
            initial_capital = account_facts["initial_capital_fen"]
        elif account_facts["initial_capital_fen"] != initial_capital:
            raise TotalReturnQualificationError("control initial capital differs")
        latest = account_events[-1]
        if any(latest[field] != final_state[field] for field in _FINAL_STATE_FIELDS):
            raise TotalReturnQualificationError("account final state is not ledger-derived")
        if (
            account_facts["initial_capital_fen"]
            + sum(row["cash_delta_fen"] for row in account_events)
            != final_state["cash_fen"]
            or sum(row["trade_quantity_delta"] for row in account_events)
            != final_state["trade_holdings"]
            or sum(row["settled_quantity_delta"] for row in account_events)
            != final_state["settled_holdings"]
        ):
            raise TotalReturnQualificationError("account cash or quantity does not reconcile")
        nonnegative = _FINAL_STATE_FIELDS - {"equity_fen"}
        if any(row[field] < 0 for row in account_events for field in nonnegative):
            raise TotalReturnQualificationError("account ledger contains a negative state")
        if any(
            row["equity_fen"]
            != row["cash_fen"]
            + row["market_value_fen"]
            + row["receivable_fen"]
            - row["outstanding_tax_fen"]
            for row in account_events
        ):
            raise TotalReturnQualificationError("account equity identity does not reconcile")
        if any(
            final_state[field] != 0
            for field in (
                "trade_holdings",
                "settled_holdings",
                "receivable_fen",
                "unpaid_dividend_tax_base_fen",
                "deferred_tax_base_fen",
                "outstanding_tax_fen",
            )
        ):
            raise TotalReturnQualificationError("account settlement or tax remains open")
        event_trade_ids = {
            row["trade_id"] for row in account_events if row["trade_id"]
        }
        if any(
            row["trade_id"] and not row["trade_id"].startswith(f"{account}-trade-")
            for row in account_events
        ):
            raise TotalReturnQualificationError("account event trade identity crosses accounts")
        if any(
            not row["lot_id"].startswith(f"{account}-lot-")
            or not row["entry_trade_id"].startswith(f"{account}-trade-")
            or (row["exit_trade_id"] and not row["exit_trade_id"].startswith(f"{account}-trade-"))
            or row["entry_trade_id"] not in event_trade_ids
            or (row["exit_trade_id"] and row["exit_trade_id"] not in event_trade_ids)
            for row in account_trades
        ):
            raise TotalReturnQualificationError("account trade linkage or isolation failed")
        if (
            len({row["lot_id"] for row in account_trades}) != len(account_trades)
            or any(row["status"] != "CLOSED" for row in account_trades)
        ):
            raise TotalReturnQualificationError("account trade ledger is duplicated or open")
        event_cost = sum(
            row["cost_fen"]
            for row in account_events
            if row["event_type"] == "TRADE_COST"
        )
        trade_cost = sum(
            row["entry_cost_fen"] + row["exit_cost_fen"] for row in account_trades
        )
        trade_profit = sum(row["net_pnl_fen"] for row in account_trades)
        gross_dividend = sum(
            row["cash_delta_fen"]
            for row in account_events
            if row["event_type"] == "DIVIDEND_PAYMENT"
        )
        collected_tax = -sum(
            row["cash_delta_fen"]
            for row in account_events
            if row["event_type"] == "TAX_COLLECTION"
        )
        after_tax_profit = final_state["equity_fen"] - account_facts["initial_capital_fen"]
        if (
            event_cost != trade_cost
            or trade_profit != after_tax_profit
            or account_facts["gross_dividend_fen"] != gross_dividend
            or account_facts["collected_tax_fen"] != collected_tax
            or account_facts["net_dividend_fen"] != gross_dividend - collected_tax
            or account_facts["deferred_tax_fen"] != final_state["deferred_tax_base_fen"]
            or account_facts["outstanding_tax_fen"] != final_state["outstanding_tax_fen"]
            or account_facts["trading_cost_fen"] != event_cost
            or account_facts["after_tax_profit_fen"] != after_tax_profit
            or account_facts["price_profit_fen"]
            != after_tax_profit - gross_dividend + collected_tax
        ):
            raise TotalReturnQualificationError("account components do not reconcile")
        expected_gross_dividend = 0
        for action in actions:
            action_rows = [
                row
                for row in corporate_action_rows
                if row["event_revision_id"] == action.event_revision_id
            ]
            entitlement = [
                row for row in action_rows if row["event_type"] == "DIVIDEND_ENTITLEMENT"
            ]
            payment = [row for row in action_rows if row["event_type"] == "DIVIDEND_PAYMENT"]
            record_rows = [
                row
                for row in account_events
                if _date_value(row["Date"], "account event date") == action.record_date
            ]
            if not entitlement and not payment:
                if any(row["settled_holdings"] != 0 for row in record_rows):
                    raise TotalReturnQualificationError(
                        "corporate-action entitlement posting is missing"
                    )
                continue
            if len(entitlement) != 1 or len(payment) != 1:
                raise TotalReturnQualificationError(
                    "corporate-action posting is missing, extra, or duplicated"
                )
            entitled = entitlement[0]
            paid = payment[0]
            quantity = entitled["quantity"]
            gross_fen = int(
                (action.gross_cash_per_share * Decimal(quantity) * Decimal(100)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            if (
                quantity <= 0
                or entitled["Date"] != action.record_date.isoformat()
                or paid["Date"] != action.pay_date.isoformat()
                or entitled["settled_holdings"] != quantity
                or paid["quantity"] != quantity
                or entitled["cash_delta_fen"] != 0
                or paid["cash_delta_fen"] != gross_fen
            ):
                raise TotalReturnQualificationError(
                    "corporate-action terms do not reconcile to the account ledger"
                )
            expected_gross_dividend += gross_fen
        if expected_gross_dividend != gross_dividend:
            raise TotalReturnQualificationError(
                "account dividend postings do not match admitted terminal actions"
            )
        expected_tax = 0
        for row in account_trades:
            if row["exit_settlement_date"]:
                burden = dividend_tax_burden(
                    _date_value(row["entry_settlement_date"], "entry settlement"),
                    _date_value(row["exit_settlement_date"], "exit settlement"),
                )
                if row["tax_burden"] != format(burden, ".2f"):
                    raise TotalReturnQualificationError(
                        "account dividend tax burden is not policy-derived"
                    )
                expected_tax += int(
                    (Decimal(row["dividend_fen"]) * burden).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            elif row["tax_fen"] != 0:
                raise TotalReturnQualificationError("open account trade carries collected tax")
        if expected_tax != sum(row["tax_fen"] for row in account_trades):
            raise TotalReturnQualificationError(
                "account dividend tax postings do not match admitted action terms"
            )
        bindings[account] = {
            "events_sha256": hashlib.sha256(canonical_json_bytes(account_events)).hexdigest(),
            "trades_sha256": hashlib.sha256(canonical_json_bytes(account_trades)).hexdigest(),
            "final_state_sha256": hashlib.sha256(canonical_json_bytes(final_state)).hexdigest(),
        }
    parity_fields = ("Date", "event_type", "event_revision_id", "note")
    if [
        tuple(row[field] for field in parity_fields)
        for row in event_rows_by_account["strategy"]
    ] != [
        tuple(row[field] for field in parity_fields)
        for row in event_rows_by_account["zero_cost"]
    ] or facts["zero_cost"]["trading_cost_fen"] != 0:
        raise TotalReturnQualificationError("strategy and zero-cost control parity failed")
    return bindings, str(close), hashlib.sha256(
        canonical_json_bytes(
            {
                "accounts": bindings,
                "accounting_close_date": close,
                "initial_capital_fen": initial_capital,
            }
        )
    ).hexdigest()


def _verify_policy_and_schedule(
    accounting: Mapping[str, Any],
    payloads: Mapping[str, bytes],
    interval_start: date,
    interval_end: date,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tax = accounting.get("tax_policy")
    rounding = accounting.get("rounding_policy")
    settlement = accounting.get("settlement_schedule")
    if tax != tax_policy_identity() or rounding != rounding_policy_identity():
        raise TotalReturnQualificationError("tax or rounding policy identity is invalid")
    if not isinstance(settlement, dict) or set(settlement) != {
        "policy_id",
        "sha256",
        "document",
    }:
        raise TotalReturnQualificationError("settlement policy identity is invalid")
    document = settlement["document"]
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "assumption",
        "trade_to_settlement",
        "settlement_to_collection",
    }:
        raise TotalReturnQualificationError("settlement policy document is invalid")
    try:
        schedule = SettlementSchedule(
            document["trade_to_settlement"],
            document["settlement_to_collection"],
            document["assumption"],
        )
    except (KeyError, CorporateActionEvidenceError) as exc:
        raise TotalReturnQualificationError("settlement policy is inapplicable") from exc
    if (
        document.get("schema_version") != 1
        or schedule.document != document
        or schedule.policy_id != settlement["policy_id"]
        or schedule.digest != settlement["sha256"]
    ):
        raise TotalReturnQualificationError("settlement policy digest is invalid")
    tax_payload = tax["payload"]
    if (
        tax_payload.get("taxpayer_scope") != "ORDINARY_MAINLAND_INDIVIDUAL"
        or tax_payload.get("acquisition_scope")
        != "SUPPORTED_PUBLIC_OR_TRANSFER_MARKET_A_SHARE"
        or _date_value(tax_payload.get("effective_record_date_start"), "tax effective start")
        > interval_start
        or rounding["payload"]
        != {
            "schema_version": 1,
            "policy": "ROUND_HALF_UP_RESEARCH_ASSUMPTION",
            "currency": "CNY",
            "minor_unit": "FEN",
        }
    ):
        raise TotalReturnQualificationError("accounting policy is inapplicable")
    events = _csv_rows(
        payloads["account_events.csv"],
        _ACCOUNT_EVENT_COLUMNS,
        _EVENT_INTEGER_FIELDS,
        "account event ledger",
    )
    trade_dates = {
        row["Date"]
        for row in events
        if row["event_type"] in {"TRADE_BUY", "TRADE_SELL"}
    }
    if not trade_dates or not trade_dates.issubset(set(schedule.trade_to_settlement)):
        raise TotalReturnQualificationError("settlement schedule does not cover every trade")
    if any(not interval_start <= _date_value(value, "trade date") <= interval_end for value in trade_dates):
        raise TotalReturnQualificationError("trade is outside the accounting interval")
    liabilities = {
        row["Date"]
        for row in events
        if row["event_type"] == "TAX_LIABILITY" and row["cost_fen"] > 0
    }
    if not liabilities.issubset(set(schedule.settlement_to_collection)):
        raise TotalReturnQualificationError("tax collection schedule is incomplete")
    for row in events:
        if row["event_type"] not in {"TRADE_BUY", "TRADE_SELL"}:
            continue
        settlement_type = (
            "ACQUISITION_SETTLEMENT"
            if row["event_type"] == "TRADE_BUY"
            else "DISPOSAL_SETTLEMENT"
        )
        expected_date = schedule.trade_to_settlement[row["Date"]]
        if not any(
            candidate["account"] == row["account"]
            and candidate["trade_id"] == row["trade_id"]
            and candidate["event_type"] == settlement_type
            and candidate["Date"] == expected_date
            for candidate in events
        ):
            raise TotalReturnQualificationError("trade settlement posting is incomplete")
    for row in events:
        if row["event_type"] != "TAX_LIABILITY" or row["cost_fen"] <= 0:
            continue
        expected_date = schedule.settlement_to_collection[row["Date"]]
        if not any(
            candidate["account"] == row["account"]
            and candidate["trade_id"] == row["trade_id"]
            and candidate["event_type"] == "TAX_COLLECTION"
            and candidate["Date"] == expected_date
            and candidate["cash_delta_fen"] == -row["cost_fen"]
            for candidate in events
        ):
            raise TotalReturnQualificationError("tax collection posting is incomplete")
    return tax, settlement, rounding


def _verified_evidence_graph(
    *,
    state_root: Path | str,
    result_path: Path | str,
    instrument: str,
    expected_dataset_snapshot_id: str,
    expected_result_digest: str,
) -> tuple[dict[str, Any], dict[str, bool]]:
    if not isinstance(instrument, str) or re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)", instrument) is None:
        raise TotalReturnQualificationError("trusted A-share instrument is invalid")
    _sha256(expected_dataset_snapshot_id, "expected_dataset_snapshot_id")
    _sha256(expected_result_digest, "expected_result_digest")
    payloads = _immutable_payloads(state_root, result_path)
    if _result_digest(payloads) != expected_result_digest:
        raise TotalReturnQualificationError("result digest does not match immutable bytes")
    run_manifest = _evidence_json(payloads["run_manifest.json"], "run manifest")
    metrics = _evidence_json(payloads["metrics.json"], "metrics")
    files = run_manifest.get("files")
    if not isinstance(files, dict) or set(files) != _SETTLEMENT_FILES - {"run_manifest.json"}:
        raise TotalReturnQualificationError("run manifest artifact map is invalid")
    if any(
        descriptor
        != {"sha256": hashlib.sha256(payloads[name]).hexdigest(), "size": len(payloads[name])}
        for name, descriptor in files.items()
    ):
        raise TotalReturnQualificationError("run manifest artifact digest mismatch")
    root = Path(os.path.abspath(os.fspath(result_path)))
    identity = run_manifest.get("identity")
    if (
        not isinstance(identity, dict)
        or hashlib.sha256(canonical_json_bytes(identity)).hexdigest() != run_manifest.get("run_id")
        or run_manifest.get("run_id") != root.name
        or run_manifest.get("dataset_snapshot_id") != expected_dataset_snapshot_id
    ):
        raise TotalReturnQualificationError("run identity does not match immutable evidence")
    accounting = run_manifest.get("accounting")
    if not isinstance(accounting, dict) or identity.get("accounting") != accounting:
        raise TotalReturnQualificationError("run accounting identity is missing")
    from .datasets import _verified_action_evidence, _verify_snapshot

    dataset_path = (
        Path(os.path.abspath(os.fspath(state_root)))
        / "datasets"
        / instrument
        / expected_dataset_snapshot_id
    )
    try:
        dataset_result = _verify_snapshot(
            dataset_path,
            expected_dataset_snapshot_id,
            include_frame=True,
            verify_parent=True,
        )
        if not isinstance(dataset_result, tuple):
            raise TotalReturnQualificationError("dataset verifier omitted immutable rows")
        dataset_manifest, _ = dataset_result
        evidence = _verified_action_evidence(dataset_path, dataset_manifest)
        admitted = admit_corporate_action_evidence(
            evidence.document,
            evidence.artifact_bytes,
        )
    except (OSError, RuntimeError, TypeError, ValueError, CorporateActionEvidenceError) as exc:
        raise TotalReturnQualificationError("corporate-action evidence graph is invalid") from exc
    lineage = dataset_manifest.get("lineage")
    parent = lineage.get("parent") if isinstance(lineage, dict) else None
    view_spec = lineage.get("view_spec") if isinstance(lineage, dict) else None
    causal_coverage = admitted.document["coverage"]
    causal_coverage_payload = causal_coverage["payload"]
    period_start = _date_value(metrics.get("period_start"), "accounting period start")
    period_end = _date_value(metrics.get("period_end"), "accounting period end")
    if (
        dataset_manifest.get("metadata", {}).get("instrument") != instrument
        or run_manifest.get("dataset_canonical_sha256")
        != dataset_manifest.get("canonical_sha256")
        or run_manifest.get("dataset_snapshot_id") != dataset_manifest.get("snapshot_id")
        or accounting.get("corporate_action_evidence_sha256") != admitted.digest
        or dataset_manifest.get("corporate_action_evidence_sha256") != admitted.digest
        or causal_coverage_payload.get("instrument") != instrument
        or causal_coverage_payload.get("market") not in {"XSHG", "XSHE"}
    ):
        raise TotalReturnQualificationError("Dataset, coverage, and run bindings differ")
    if not isinstance(parent, dict) or not isinstance(view_spec, dict):
        raise TotalReturnQualificationError("execution Dataset is not a derived View")
    feature_cutoff = _date_value(view_spec.get("training_through"), "causal feature cutoff")
    if any(
        revision.get("use_role") != "CAUSAL_FEATURE"
        or datetime.fromisoformat(revision["available_at"].replace("Z", "+00:00")).date()
        > feature_cutoff
        for revision in admitted.document["revisions"]
    ):
        raise TotalReturnQualificationError("corporate-action evidence leaks after the feature cutoff")
    outcome, checked = _accounting_outcome_evidence(
        state_root=state_root,
        result_path=result_path,
        result_payloads=payloads,
        expected_result_digest=expected_result_digest,
        execution_view_snapshot_id=dataset_manifest["snapshot_id"],
    )
    coverage = outcome.document["coverage"]
    coverage_payload = coverage["payload"]
    coverage_start = _date_value(coverage_payload["interval_start"], "coverage start")
    coverage_end = _date_value(coverage_payload["interval_end"], "coverage end")
    if (
        coverage_payload.get("instrument") != instrument
        or coverage_payload.get("market") not in {"XSHG", "XSHE"}
        or not coverage_start <= period_start <= period_end <= coverage_end
        or checked.date() < period_end
    ):
        raise TotalReturnQualificationError("accounting outcome interval is invalid")
    checked_as_of = coverage_payload.get("checked_as_of")
    try:
        outcome_checked = datetime.fromisoformat(str(checked_as_of).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TotalReturnQualificationError("outcome checked-as-of is invalid") from exc
    if outcome_checked != checked:
        raise TotalReturnQualificationError("outcome evidence predates sealed decisions")
    tax, settlement, rounding = _verify_policy_and_schedule(
        accounting,
        payloads,
        coverage_start,
        coverage_end,
    )
    causal_actions = accounting_cash_dividends(admitted)
    actions = accounting_cash_dividends(outcome)
    if [
        (
            action.event_revision_id,
            action.record_date,
            action.ex_date,
            action.pay_date,
            action.gross_cash_per_share,
        )
        for action in causal_actions
    ] != [
        (
            action.event_revision_id,
            action.record_date,
            action.ex_date,
            action.pay_date,
            action.gross_cash_per_share,
        )
        for action in actions
    ]:
        raise TotalReturnQualificationError(
            "causal and accounting-outcome action terms differ"
        )
    account_bindings, accounting_close, parity_digest = _ledger_bindings_and_close(
        payloads, metrics, actions
    )
    terminal_ids = [action.event_revision_id for action in actions]
    if (
        accounting.get("coverage_state") != causal_coverage_payload.get("coverage_state")
        or accounting.get("coverage_id") != causal_coverage.get("coverage_id")
        or accounting.get("complete_contract_id") != admitted.document.get("complete_contract_id")
        or accounting.get("event_revision_ids")
        != causal_coverage_payload.get("event_revision_ids")
        or accounting.get("raw_artifact_sha256s")
        != sorted(admitted.artifact_bytes)
        or terminal_ids != coverage_payload.get("event_revision_ids", [])
        or accounting.get("claim") not in SOURCE_ISSUER_CLAIMS["STRATEGY_RUNNER"]
    ):
        raise TotalReturnQualificationError("coverage or terminal event identity differs")
    bindings = {
        "corporate_action_evidence_sha256": outcome.digest,
        "raw_artifact_sha256s": sorted(outcome.artifact_bytes),
        "event_revision_ids": list(coverage_payload["event_revision_ids"]),
        "tax_policy_id": tax["tax_policy_id"],
        "tax_policy_sha256": tax["sha256"],
        "settlement_policy_id": settlement["policy_id"],
        "settlement_policy_sha256": settlement["sha256"],
        "rounding_policy_id": rounding["rounding_policy_id"],
        "rounding_policy_sha256": rounding["sha256"],
        "parent_snapshot_id": parent["snapshot_id"],
        "execution_view_snapshot_id": dataset_manifest["snapshot_id"],
        "parent_corporate_action_evidence_sha256": parent[
            "corporate_action_evidence_sha256"
        ],
        "view_corporate_action_evidence_sha256": admitted.digest,
        "scoring_mask_sha256": dataset_manifest["scoring_mask_sha256"],
        "causal_feature_cutoff": feature_cutoff.isoformat(),
        "accounting_outcome_checked_as_of": outcome_checked.isoformat().replace("+00:00", "Z"),
        "accounting_outcome_use_role": "ACCOUNTING_OUTCOME",
        "result_digest": expected_result_digest,
        "run_manifest_sha256": hashlib.sha256(payloads["run_manifest.json"]).hexdigest(),
        "account_events_sha256": hashlib.sha256(payloads["account_events.csv"]).hexdigest(),
        "account_trades_sha256": hashlib.sha256(payloads["account_trades.csv"]).hexdigest(),
        "accounts": account_bindings,
        "control_parity_sha256": parity_digest,
    }
    complete = coverage_payload["coverage_state"] in COMPLETE_COVERAGE_STATES
    if complete and (
        not outcome.document.get("complete_enumeration_contract")
        or outcome.document.get("complete_contract_id") is None
    ):
        raise TotalReturnQualificationError("complete coverage contract is absent")
    checks = {field: True for field in CHECK_FIELDS}
    checks["coverage_complete"] = complete
    return {
        "source_issuer": "STRATEGY_RUNNER",
        "source_total_return_claim": accounting["claim"],
        "coverage_state": coverage_payload["coverage_state"],
        "coverage_basis": "COMPLETE_ENUMERATION_CONTRACT" if complete else "NONE",
        "coverage_id": coverage["coverage_id"],
        "complete_contract_id": outcome.document.get("complete_contract_id"),
        "equivalent_contract_approval_id": None,
        "bindings": bindings,
        "accounting_close": accounting_close,
    }, checks


def _qualification_record_from_evidence(
    *,
    state_root: Path | str,
    result_path: Path | str,
    instrument: str,
    expected_dataset_snapshot_id: str,
    expected_result_digest: str,
    historical_exposure: str,
    other_study_gates_passed: bool = True,
    prior_qualification: Any = None,
) -> tuple[dict[str, Any], bytes | None]:
    """Rebuild every qualification predicate from one immutable evidence graph."""

    graph, exact_checks = _verified_evidence_graph(
        state_root=state_root,
        result_path=result_path,
        instrument=instrument,
        expected_dataset_snapshot_id=expected_dataset_snapshot_id,
        expected_result_digest=expected_result_digest,
    )
    if historical_exposure not in {"PRISTINE", "EXPOSED", "UNKNOWN"}:
        raise TotalReturnQualificationError("historical exposure is invalid")
    if type(other_study_gates_passed) is not bool:
        raise TotalReturnQualificationError("other Study gate state must be boolean")
    complete = (
        graph["coverage_state"] in COMPLETE_COVERAGE_STATES
        and graph["coverage_basis"] != "NONE"
        and graph["coverage_id"] is not None
        and graph["complete_contract_id"] is not None
    )
    if complete and all(exact_checks.values()):
        claim_state = "AFTER_TAX_TOTAL_RETURN_VERIFIED"
    elif (
        graph["source_total_return_claim"] == "KNOWN_EVENT_CORRECTED_PARTIAL"
        and graph["coverage_state"] == "VERIFIED_EVENTS"
    ):
        claim_state = "KNOWN_EVENT_CORRECTED_PARTIAL"
    else:
        claim_state = "AFTER_TAX_TOTAL_RETURN_UNVERIFIED"
    reasons = _failure_reasons(exact_checks)
    if claim_state == "KNOWN_EVENT_CORRECTED_PARTIAL":
        reasons = ["KNOWN_EVENT_PARTIAL"]
    if historical_exposure == "EXPOSED":
        reasons = sorted(set(reasons) | {"HISTORICALLY_EXPOSED"})
    elif historical_exposure == "UNKNOWN":
        reasons = sorted(set(reasons) | {"HISTORICAL_EXPOSURE_UNKNOWN"})
    ranking_eligible = claim_state == "AFTER_TAX_TOTAL_RETURN_VERIFIED" and historical_exposure == "PRISTINE"
    promotion_eligible = ranking_eligible and other_study_gates_passed
    if ranking_eligible and not other_study_gates_passed:
        reasons = ["OTHER_STUDY_GATE_FAILED"]
    elif ranking_eligible:
        reasons = []
    prior_bytes = None
    prior_id = None
    from_state = None
    same_evidence = False
    if prior_qualification is not None:
        if not is_trusted_qualification(prior_qualification):
            raise TotalReturnQualificationError("prior qualification is not pristine trusted evidence")
        prior_record = qualification_record(prior_qualification)
        prior_bytes = canonical_json_bytes(prior_record)
        prior_id = prior_record["qualification_id"]
        from_state = prior_record["claim_state"]
        same_evidence = (
            prior_record["bindings"]["corporate_action_evidence_sha256"]
            == graph["bindings"]["corporate_action_evidence_sha256"]
        )
    record: dict[str, Any] = {
        "schema_version": 1,
        "qualification_id": "",
        "issuer": ISSUER,
        "source_issuer": graph["source_issuer"],
        "source_total_return_claim": graph["source_total_return_claim"],
        "claim_state": claim_state,
        "coverage_state": graph["coverage_state"],
        "coverage_basis": graph["coverage_basis"],
        "coverage_id": graph["coverage_id"],
        "complete_contract_id": graph["complete_contract_id"],
        "equivalent_contract_approval_id": graph["equivalent_contract_approval_id"],
        "bindings": graph["bindings"],
        "checks": exact_checks,
        "ranking": {
            "eligible_for_ranking": ranking_eligible,
            "eligible_for_promotion": promotion_eligible,
            "historical_exposure": historical_exposure,
            "reason_codes": reasons,
        },
        "transition": {
            "prior_qualification_id": prior_id,
            "from_state": from_state,
            "to_state": claim_state,
            "same_corporate_action_evidence": same_evidence,
        },
    }
    record["qualification_id"] = qualification_id(
        {key: value for key, value in record.items() if key != "qualification_id"}
    )
    return record, prior_bytes


def qualification_record(value: Any) -> dict[str, Any]:
    if not is_trusted_qualification(value):
        raise TotalReturnQualificationError("qualification is not pristine trusted evidence")
    return copy.deepcopy(dict(value))


def read_time_classification(
    *,
    source_issuer: str,
    source_total_return_claim: str,
    coverage_state: str,
    attempted_after_tax: bool = False,
    qualification: Any = None,
) -> dict[str, Any]:
    """Classify legacy evidence without issuing, mutating, or upgrading it."""

    if qualification is not None and is_trusted_qualification(qualification):
        return qualification_record(qualification)
    if source_issuer not in SOURCE_ISSUER_CLAIMS or source_total_return_claim not in SOURCE_ISSUER_CLAIMS[
        source_issuer
    ]:
        raise TotalReturnQualificationError("source issuer/claim combination is forbidden")
    if coverage_state not in COVERAGE_STATES or type(attempted_after_tax) is not bool:
        raise TotalReturnQualificationError("read-time classification input is invalid")
    if source_total_return_claim == "KNOWN_EVENT_CORRECTED_PARTIAL":
        state = "KNOWN_EVENT_CORRECTED_PARTIAL"
        reasons = ["KNOWN_EVENT_PARTIAL"]
    elif attempted_after_tax or source_total_return_claim == "AFTER_TAX_TOTAL_RETURN_UNVERIFIED":
        state = "AFTER_TAX_TOTAL_RETURN_UNVERIFIED"
        reasons = ["UNKNOWN_OR_MISSING_COVERAGE"]
    else:
        state = "PRICE_RETURN_ONLY"
        reasons = ["PRICE_ONLY"]
    return {
        "schema_version": 1,
        "qualification_id": None,
        "issuer": None,
        "source_issuer": source_issuer,
        "source_total_return_claim": source_total_return_claim,
        "claim_state": state,
        "coverage_state": coverage_state,
        "ranking": {
            "eligible_for_ranking": False,
            "eligible_for_promotion": False,
            "historical_exposure": "UNKNOWN",
            "reason_codes": reasons,
        },
        "trusted_qualification_absent": True,
    }
