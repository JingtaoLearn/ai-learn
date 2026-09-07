from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 2
ENTITY_TYPES = ("STUDY", "EXPERIMENT", "ATTEMPT", "METRIC_DOCUMENT", "REPORT")
DIMENSION_KEYS = (
    "integrity",
    "accounting",
    "policy",
    "holdout_exposure",
    "matched_control",
    "deployment_qualification",
)
DIMENSION_VALUES = {
    "integrity": (
        "VERIFIED_IMMUTABLE",
        "TAMPERED_OR_WRITABLE",
        "UNKNOWN_ENTITY_VERSION",
        "CONTRADICTORY_AUTHORITY",
        "MISSING_AUTHORITY",
    ),
    "accounting": (
        "PRICE_RETURN_ONLY",
        "KNOWN_EVENT_CORRECTED_PARTIAL",
        "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
        "AFTER_TAX_TOTAL_RETURN_VERIFIED",
        "UNKNOWN",
    ),
    "policy": (
        "CURRENT_BOUND",
        "LEGACY_POLICY",
        "MISSING_POLICY",
        "CONTRADICTORY_POLICY",
        "NOT_APPLICABLE_PRICE_ONLY",
    ),
    "holdout_exposure": ("PRISTINE", "EXPOSED", "UNKNOWN", "NOT_APPLICABLE"),
    "matched_control": ("SUFFICIENT", "INSUFFICIENT", "MISSING", "CONTRADICTORY", "NOT_APPLICABLE"),
    "deployment_qualification": (
        "DEPLOYMENT_EVIDENCE_QUALIFIED_NOT_AUTHORIZED",
        "NOT_DEPLOYMENT_QUALIFIED",
        "UNKNOWN_NOT_DEPLOYMENT_QUALIFIED",
    ),
}
PRIMARY_STATES = (
    "TAMPERED_OR_WRITABLE",
    "UNKNOWN_ENTITY_VERSION",
    "CONTRADICTORY_AUTHORITY",
    "MISSING_AUTHORITY",
    "UNRECONCILED_ACCOUNTING",
    "PRICE_RETURN_ONLY",
    "KNOWN_EVENT_PARTIAL",
    "LEGACY_OR_MISSING_POLICY",
    "HISTORICALLY_EXPOSED",
    "HISTORICAL_EXPOSURE_UNKNOWN",
    "INSUFFICIENT_MATCHED_CONTROL",
    "VALIDATED_CURRENT_NON_RANKABLE_EVIDENCE",
    "QUALIFIED_CURRENT_EVIDENCE",
    "FAIL_CLOSED_FALLBACK",
)
REASON_ORDER = (
    "EVIDENCE_TAMPERED_OR_WRITABLE",
    "UNKNOWN_ENTITY_VERSION",
    "CONTRADICTORY_AUTHORITY",
    "MISSING_AUTHORITY",
    "TOTAL_RETURN_UNVERIFIED",
    "PRICE_ONLY",
    "KNOWN_EVENT_PARTIAL",
    "INCOMPLETE_INTERVAL_COVERAGE",
    "POLICY_NOT_CURRENT_BOUND",
    "HISTORICALLY_EXPOSED",
    "HISTORICAL_EXPOSURE_UNKNOWN",
    "MATCHED_CONTROL_NOT_SUFFICIENT",
    "TRUSTED_QUALIFICATION_ABSENT",
    "ENTITY_NOT_RANKABLE",
    "NOT_DEPLOYMENT_QUALIFIED",
)
EFFECT_KEYS = (
    "validated",
    "ranking_eligible",
    "promotion_evidence_eligible",
    "promotion_ready",
    "deployment_evidence_qualified",
    "deployment_authorized",
    "production_signal_authorized",
)
TOP_KEYS = (
    "schema_version",
    "entity_type",
    "source_identities",
    "dimensions",
    "primary_state",
    "reason_codes",
    "effects",
    "warning",
)
SOURCE_SPECS: dict[str, dict[str, str]] = {
    "STUDY": {
        "study_id_sha256": "sha",
        "frozen_plan_sha256": "sha",
        "event_projection_sha256": "nullable_sha",
        "evidence_projection_sha256": "nullable_sha",
        "binding_projection_sha256": "nullable_sha",
        "holdout_projection_sha256": "nullable_sha",
        "catalog_schema_version": "nonnegative_int",
    },
    "EXPERIMENT": {
        "experiment_id_sha256": "sha",
        "identity_sha256": "sha",
        "canonical_attempt_id_sha256": "nullable_sha",
        "catalog_schema_version": "nonnegative_int",
    },
    "ATTEMPT": {
        "attempt_id_sha256": "sha",
        "experiment_id_sha256": "sha",
        "resolved_sha256": "sha",
        "result_digest_sha256": "nullable_sha",
        "catalog_schema_version": "nonnegative_int",
    },
    "METRIC_DOCUMENT": {
        "binding_id_sha256": "sha",
        "study_id_sha256": "sha",
        "experiment_id_sha256": "sha",
        "attempt_id_sha256": "sha",
        "metric_document_sha256": "nullable_sha",
        "catalog_schema_version": "nonnegative_int",
    },
    "REPORT": {
        "source_entity_type": "report_source",
        "source_identity_sha256": "sha",
        "report_sha256": "nullable_sha",
        "catalog_schema_version": "nonnegative_int",
    },
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    pass


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def identifier_sha256(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("source identifier must be a non-empty string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_a_share_document(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key in sorted(value):
            item = value[key]
            if key == "market" and item in {"XSHG", "XSHE"}:
                return True
            if (
                key in {"instrument", "instrument_id"}
                and isinstance(item, str)
                and re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)", item) is not None
            ):
                return True
            if is_a_share_document(item):
                return True
    elif isinstance(value, list):
        return any(is_a_share_document(item) for item in value)
    return False


def strict_json_loads(payload: str | bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ContractError("classification root must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: tuple[str, ...] | set[str], label: str) -> None:
    if set(value) != set(expected):
        raise ContractError(f"{label} fields are invalid")


def _validate_source_identities(entity_type: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("source_identities must be an object")
    spec = SOURCE_SPECS[entity_type]
    _exact_keys(value, set(spec), "source_identities")
    for key, kind in spec.items():
        item = value[key]
        if kind == "sha" and (not isinstance(item, str) or _SHA256.fullmatch(item) is None):
            raise ContractError(f"{key} must be a lowercase SHA-256")
        if kind == "nullable_sha" and item is not None and (
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
        ):
            raise ContractError(f"{key} must be null or a lowercase SHA-256")
        if kind == "nonnegative_int" and (type(item) is not int or item < 0):
            raise ContractError(f"{key} must be a nonnegative integer")
        if kind == "report_source" and item not in {"STUDY", "ATTEMPT"}:
            raise ContractError("source_entity_type must be STUDY or ATTEMPT")
    return copy.deepcopy(dict(value))


def is_rankable_context(entity_type: str, source_identities: Mapping[str, Any]) -> bool:
    if entity_type in {"STUDY", "EXPERIMENT", "METRIC_DOCUMENT"}:
        return True
    if entity_type == "REPORT":
        return source_identities["source_entity_type"] == "STUDY"
    return False


def _validate_applicability(
    entity_type: str,
    source_identities: Mapping[str, Any],
    dimensions: Mapping[str, str],
) -> None:
    rankable = is_rankable_context(entity_type, source_identities)
    holdout = dimensions["holdout_exposure"]
    control = dimensions["matched_control"]
    if rankable:
        if holdout == "NOT_APPLICABLE" or control == "NOT_APPLICABLE":
            raise ContractError("rankable context requires holdout and matched-control authorities")
    elif holdout != "NOT_APPLICABLE" or control != "NOT_APPLICABLE":
        raise ContractError("non-rankable context requires both dimensions to be NOT_APPLICABLE")
    if (
        dimensions["policy"] == "NOT_APPLICABLE_PRICE_ONLY"
        and dimensions["accounting"] != "PRICE_RETURN_ONLY"
    ):
        raise ContractError("NOT_APPLICABLE_PRICE_ONLY requires PRICE_RETURN_ONLY accounting")
    if (
        dimensions["integrity"] == "UNKNOWN_ENTITY_VERSION"
        and dimensions["accounting"] != "UNKNOWN"
    ):
        raise ContractError("unknown entity version requires UNKNOWN accounting")


def derive_deployment_qualification(
    entity_type: str,
    source_identities: Mapping[str, Any],
    dimensions: Mapping[str, str],
) -> str:
    if not is_rankable_context(entity_type, source_identities):
        return "NOT_DEPLOYMENT_QUALIFIED"
    if (
        dimensions["integrity"] == "VERIFIED_IMMUTABLE"
        and dimensions["accounting"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
        and dimensions["policy"] == "CURRENT_BOUND"
        and dimensions["holdout_exposure"] == "PRISTINE"
        and dimensions["matched_control"] == "SUFFICIENT"
    ):
        return "DEPLOYMENT_EVIDENCE_QUALIFIED_NOT_AUTHORIZED"
    if (
        dimensions["integrity"] in {"UNKNOWN_ENTITY_VERSION", "MISSING_AUTHORITY"}
        or dimensions["accounting"] == "UNKNOWN"
        or dimensions["holdout_exposure"] == "UNKNOWN"
    ):
        return "UNKNOWN_NOT_DEPLOYMENT_QUALIFIED"
    return "NOT_DEPLOYMENT_QUALIFIED"


def derive_primary_state(
    entity_type: str,
    source_identities: Mapping[str, Any],
    dimensions: Mapping[str, str],
) -> tuple[str, str]:
    if dimensions["integrity"] == "TAMPERED_OR_WRITABLE":
        return "R01_TAMPERED", "TAMPERED_OR_WRITABLE"
    if dimensions["integrity"] == "UNKNOWN_ENTITY_VERSION":
        return "R02_UNKNOWN_VERSION", "UNKNOWN_ENTITY_VERSION"
    if (
        dimensions["integrity"] == "CONTRADICTORY_AUTHORITY"
        or dimensions["policy"] == "CONTRADICTORY_POLICY"
        or dimensions["matched_control"] == "CONTRADICTORY"
    ):
        return "R03_CONTRADICTION", "CONTRADICTORY_AUTHORITY"
    if (
        dimensions["integrity"] == "MISSING_AUTHORITY"
        or dimensions["accounting"] == "UNKNOWN"
    ):
        return "R04_MISSING", "MISSING_AUTHORITY"
    if dimensions["accounting"] == "AFTER_TAX_TOTAL_RETURN_UNVERIFIED":
        return "R05_UNRECONCILED", "UNRECONCILED_ACCOUNTING"
    if dimensions["accounting"] == "PRICE_RETURN_ONLY":
        return "R06_PRICE_ONLY", "PRICE_RETURN_ONLY"
    if dimensions["accounting"] == "KNOWN_EVENT_CORRECTED_PARTIAL":
        return "R07_KNOWN_EVENT_PARTIAL", "KNOWN_EVENT_PARTIAL"
    if dimensions["policy"] in {
        "LEGACY_POLICY",
        "MISSING_POLICY",
        "NOT_APPLICABLE_PRICE_ONLY",
    }:
        return "R08_POLICY", "LEGACY_OR_MISSING_POLICY"
    if dimensions["holdout_exposure"] == "EXPOSED":
        return "R09_EXPOSED", "HISTORICALLY_EXPOSED"
    if dimensions["holdout_exposure"] == "UNKNOWN":
        return "R10_EXPOSURE_UNKNOWN", "HISTORICAL_EXPOSURE_UNKNOWN"
    if dimensions["matched_control"] in {"INSUFFICIENT", "MISSING"}:
        return "R11_CONTROL", "INSUFFICIENT_MATCHED_CONTROL"
    if (
        not is_rankable_context(entity_type, source_identities)
        and dimensions["integrity"] == "VERIFIED_IMMUTABLE"
        and dimensions["accounting"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
        and dimensions["policy"] == "CURRENT_BOUND"
        and dimensions["holdout_exposure"] == "NOT_APPLICABLE"
        and dimensions["matched_control"] == "NOT_APPLICABLE"
        and dimensions["deployment_qualification"] == "NOT_DEPLOYMENT_QUALIFIED"
    ):
        return "R12_VALID_NON_RANKABLE", "VALIDATED_CURRENT_NON_RANKABLE_EVIDENCE"
    if (
        is_rankable_context(entity_type, source_identities)
        and dimensions["integrity"] == "VERIFIED_IMMUTABLE"
        and dimensions["accounting"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
        and dimensions["policy"] == "CURRENT_BOUND"
        and dimensions["holdout_exposure"] == "PRISTINE"
        and dimensions["matched_control"] == "SUFFICIENT"
        and dimensions["deployment_qualification"]
        == "DEPLOYMENT_EVIDENCE_QUALIFIED_NOT_AUTHORIZED"
    ):
        return "R13_QUALIFIED", "QUALIFIED_CURRENT_EVIDENCE"
    return "R14_FAIL_CLOSED_FALLBACK", "FAIL_CLOSED_FALLBACK"


def derive_reason_codes(
    entity_type: str,
    source_identities: Mapping[str, Any],
    dimensions: Mapping[str, str],
) -> list[str]:
    present: set[str] = set()
    integrity = dimensions["integrity"]
    accounting = dimensions["accounting"]
    policy = dimensions["policy"]
    holdout = dimensions["holdout_exposure"]
    control = dimensions["matched_control"]
    deployment = dimensions["deployment_qualification"]
    if integrity == "TAMPERED_OR_WRITABLE":
        present.add("EVIDENCE_TAMPERED_OR_WRITABLE")
    elif integrity == "UNKNOWN_ENTITY_VERSION":
        present.add("UNKNOWN_ENTITY_VERSION")
    elif integrity == "CONTRADICTORY_AUTHORITY":
        present.add("CONTRADICTORY_AUTHORITY")
    elif integrity == "MISSING_AUTHORITY":
        present.add("MISSING_AUTHORITY")
    if accounting == "AFTER_TAX_TOTAL_RETURN_UNVERIFIED":
        present.add("TOTAL_RETURN_UNVERIFIED")
    elif accounting == "PRICE_RETURN_ONLY":
        present.update({"PRICE_ONLY", "TRUSTED_QUALIFICATION_ABSENT"})
    elif accounting == "KNOWN_EVENT_CORRECTED_PARTIAL":
        present.update({"KNOWN_EVENT_PARTIAL", "INCOMPLETE_INTERVAL_COVERAGE"})
    elif accounting == "UNKNOWN":
        present.add("MISSING_AUTHORITY")
    if policy == "CONTRADICTORY_POLICY":
        present.add("CONTRADICTORY_AUTHORITY")
    elif policy in {"LEGACY_POLICY", "MISSING_POLICY", "NOT_APPLICABLE_PRICE_ONLY"}:
        present.add("POLICY_NOT_CURRENT_BOUND")
    if holdout == "EXPOSED":
        present.add("HISTORICALLY_EXPOSED")
    elif holdout == "UNKNOWN":
        present.add("HISTORICAL_EXPOSURE_UNKNOWN")
    if control == "CONTRADICTORY":
        present.add("CONTRADICTORY_AUTHORITY")
    elif control in {"INSUFFICIENT", "MISSING"}:
        present.add("MATCHED_CONTROL_NOT_SUFFICIENT")
    if not is_rankable_context(entity_type, source_identities):
        present.add("ENTITY_NOT_RANKABLE")
    if deployment != "DEPLOYMENT_EVIDENCE_QUALIFIED_NOT_AUTHORIZED":
        present.add("NOT_DEPLOYMENT_QUALIFIED")
    return [reason for reason in REASON_ORDER if reason in present]


def derive_effects(primary_state: str) -> dict[str, bool]:
    effects = {key: False for key in EFFECT_KEYS}
    if primary_state == "QUALIFIED_CURRENT_EVIDENCE":
        effects.update(
            validated=True,
            ranking_eligible=True,
            promotion_evidence_eligible=True,
            deployment_evidence_qualified=True,
        )
    elif primary_state == "VALIDATED_CURRENT_NON_RANKABLE_EVIDENCE":
        effects["validated"] = True
    return effects


def derive_warning(primary_state: str) -> str:
    if primary_state == "QUALIFIED_CURRENT_EVIDENCE":
        return (
            "Evidence-qualified; promotion, deployment, production-signal, and trading "
            "authorization are not conveyed."
        )
    if primary_state == "VALIDATED_CURRENT_NON_RANKABLE_EVIDENCE":
        return (
            "Validated immutable evidence; this entity is not rankable or deployment-qualified "
            "and conveys no authorization."
        )
    return (
        "Historical evidence is fail-closed for validation, ranking, promotion, deployment, "
        "production signal, and trading."
    )


def build_output(
    *,
    entity_type: str,
    source_identities: Mapping[str, Any],
    integrity: str,
    accounting: str,
    policy: str,
    holdout_exposure: str,
    matched_control: str,
) -> dict[str, Any]:
    if entity_type not in ENTITY_TYPES:
        raise ContractError("unknown entity_type")
    sources = _validate_source_identities(entity_type, source_identities)
    dimensions = {
        "integrity": integrity,
        "accounting": accounting,
        "policy": policy,
        "holdout_exposure": holdout_exposure,
        "matched_control": matched_control,
    }
    for key, value in dimensions.items():
        if value not in DIMENSION_VALUES[key]:
            raise ContractError(f"unknown {key} value")
    _validate_applicability(entity_type, sources, dimensions)
    dimensions["deployment_qualification"] = derive_deployment_qualification(
        entity_type,
        sources,
        dimensions,
    )
    _, primary_state = derive_primary_state(entity_type, sources, dimensions)
    result = {
        "schema_version": SCHEMA_VERSION,
        "entity_type": entity_type,
        "source_identities": sources,
        "dimensions": dimensions,
        "primary_state": primary_state,
        "reason_codes": derive_reason_codes(entity_type, sources, dimensions),
        "effects": derive_effects(primary_state),
        "warning": derive_warning(primary_state),
    }
    return validate_output(result)


def validate_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("classification must be an object")
    _exact_keys(value, TOP_KEYS, "classification")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported classification schema_version")
    entity_type = value["entity_type"]
    if entity_type not in ENTITY_TYPES:
        raise ContractError("unknown entity_type")
    sources = _validate_source_identities(entity_type, value["source_identities"])
    dimensions = value["dimensions"]
    if not isinstance(dimensions, Mapping):
        raise ContractError("dimensions must be an object")
    _exact_keys(dimensions, DIMENSION_KEYS, "dimensions")
    checked_dimensions: dict[str, str] = {}
    for key in DIMENSION_KEYS:
        item = dimensions[key]
        if not isinstance(item, str) or item not in DIMENSION_VALUES[key]:
            raise ContractError(f"unknown {key} value")
        checked_dimensions[key] = item
    _validate_applicability(entity_type, sources, checked_dimensions)
    expected_deployment = derive_deployment_qualification(
        entity_type,
        sources,
        checked_dimensions,
    )
    if checked_dimensions["deployment_qualification"] != expected_deployment:
        raise ContractError("deployment_qualification is not derived from exact authorities")
    rule_id, expected_primary = derive_primary_state(entity_type, sources, checked_dimensions)
    if value["primary_state"] not in PRIMARY_STATES or value["primary_state"] != expected_primary:
        raise ContractError(f"primary_state violates precedence ({rule_id})")
    reasons = value["reason_codes"]
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise ContractError("reason_codes must be a string array")
    if len(reasons) != len(set(reasons)) or any(item not in REASON_ORDER for item in reasons):
        raise ContractError("reason_codes contain duplicates or unknown values")
    expected_reasons = derive_reason_codes(entity_type, sources, checked_dimensions)
    if reasons != expected_reasons:
        raise ContractError("reason_codes are incomplete or out of canonical order")
    effects = value["effects"]
    if not isinstance(effects, Mapping):
        raise ContractError("effects must be an object")
    _exact_keys(effects, EFFECT_KEYS, "effects")
    if any(type(effects[key]) is not bool for key in EFFECT_KEYS):
        raise ContractError("every effect must be boolean")
    if dict(effects) != derive_effects(expected_primary):
        raise ContractError("effects are not exactly derived from primary_state")
    warning = value["warning"]
    if not isinstance(warning, str) or not warning:
        raise ContractError("warning must be non-empty")
    if warning != derive_warning(expected_primary):
        raise ContractError("warning is not the canonical warning for primary_state")
    return copy.deepcopy(dict(value))


def validate_or_fail_closed(
    value: Any,
    *,
    expected_entity_type: str,
    canonical_source_identities: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        checked = validate_output(value)
        if checked["entity_type"] != expected_entity_type:
            raise ContractError("entity_type does not match adapter expectation")
        if checked["source_identities"] != dict(canonical_source_identities):
            raise ContractError("source identities do not match adapter authorities")
        return checked
    except (ContractError, KeyError, TypeError, ValueError):
        sources = _validate_source_identities(
            expected_entity_type,
            canonical_source_identities,
        )
        rankable = is_rankable_context(expected_entity_type, sources)
        unknown_version = (
            isinstance(value, Mapping) and value.get("schema_version") != SCHEMA_VERSION
        )
        return build_output(
            entity_type=expected_entity_type,
            source_identities=sources,
            integrity=(
                "UNKNOWN_ENTITY_VERSION" if unknown_version else "CONTRADICTORY_AUTHORITY"
            ),
            accounting=("UNKNOWN" if unknown_version else "AFTER_TAX_TOTAL_RETURN_UNVERIFIED"),
            policy="MISSING_POLICY",
            holdout_exposure="UNKNOWN" if rankable else "NOT_APPLICABLE",
            matched_control="MISSING" if rankable else "NOT_APPLICABLE",
        )
