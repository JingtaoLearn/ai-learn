from __future__ import annotations

import copy
import hashlib
import json
import re
import weakref
from collections.abc import Mapping
from typing import Any


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

    return TrustedTotalReturnQualification, issue, pristine


TrustedTotalReturnQualification, _issue, is_trusted_qualification = _capability()


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


def qualify_total_return(
    *,
    source_issuer: str,
    source_total_return_claim: str,
    coverage_state: str,
    coverage_basis: str,
    coverage_id: str | None,
    complete_contract_id: str | None,
    equivalent_contract_approval_id: str | None,
    bindings: Mapping[str, Any],
    checks: Mapping[str, bool],
    historical_exposure: str,
    other_study_gates_passed: bool = True,
    prior_qualification: Any = None,
    same_corporate_action_evidence: bool = False,
) -> Any:
    """Issue one immutable qualification after enforcing the complete record contract."""

    if source_issuer not in SOURCE_ISSUER_CLAIMS or source_total_return_claim not in SOURCE_ISSUER_CLAIMS[
        source_issuer
    ]:
        raise TotalReturnQualificationError("source issuer/claim combination is forbidden")
    if not isinstance(checks, Mapping) or set(checks) != CHECK_FIELDS:
        raise TotalReturnQualificationError("qualification checks are incomplete")
    exact_checks = dict(checks)
    if any(type(value) is not bool for value in exact_checks.values()):
        raise TotalReturnQualificationError("qualification checks must be booleans")
    if type(other_study_gates_passed) is not bool:
        raise TotalReturnQualificationError("other Study gate state must be boolean")
    complete = (
        coverage_state in COMPLETE_COVERAGE_STATES
        and coverage_basis != "NONE"
        and coverage_id is not None
        and complete_contract_id is not None
    )
    if complete and all(exact_checks.values()):
        claim_state = "AFTER_TAX_TOTAL_RETURN_VERIFIED"
    elif source_total_return_claim == "KNOWN_EVENT_CORRECTED_PARTIAL" and coverage_state == "VERIFIED_EVENTS":
        claim_state = "KNOWN_EVENT_CORRECTED_PARTIAL"
    elif source_total_return_claim in {"FORBIDDEN", "PRICE_RETURN_ONLY"} and not any(
        exact_checks.values()
    ):
        claim_state = "PRICE_RETURN_ONLY"
    else:
        claim_state = "AFTER_TAX_TOTAL_RETURN_UNVERIFIED"
    reasons = _failure_reasons(exact_checks)
    if claim_state == "PRICE_RETURN_ONLY":
        reasons = ["PRICE_ONLY"]
    elif claim_state == "KNOWN_EVENT_CORRECTED_PARTIAL":
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
    if prior_qualification is not None:
        if not is_trusted_qualification(prior_qualification):
            raise TotalReturnQualificationError("prior qualification is not pristine trusted evidence")
        prior_record = qualification_record(prior_qualification)
        prior_bytes = canonical_json_bytes(prior_record)
        prior_id = prior_record["qualification_id"]
        from_state = prior_record["claim_state"]
    record: dict[str, Any] = {
        "schema_version": 1,
        "qualification_id": "",
        "issuer": ISSUER,
        "source_issuer": source_issuer,
        "source_total_return_claim": source_total_return_claim,
        "claim_state": claim_state,
        "coverage_state": coverage_state,
        "coverage_basis": coverage_basis,
        "coverage_id": coverage_id,
        "complete_contract_id": complete_contract_id,
        "equivalent_contract_approval_id": equivalent_contract_approval_id,
        "bindings": copy.deepcopy(dict(bindings)),
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
            "same_corporate_action_evidence": same_corporate_action_evidence,
        },
    }
    record["qualification_id"] = qualification_id(
        {key: value for key, value in record.items() if key != "qualification_id"}
    )
    _validate_record(record, prior_bytes)
    return _issue(record)


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
