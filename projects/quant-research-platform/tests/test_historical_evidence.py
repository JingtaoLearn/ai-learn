from __future__ import annotations

import copy
import itertools

import pytest

from quant_platform.historical_evidence import (
    ContractError,
    DIMENSION_VALUES,
    build_output,
    strict_json_loads,
    validate_or_fail_closed,
    validate_output,
)

H = "a" * 64
BASE_KEYS = ("integrity", "accounting", "policy", "holdout_exposure", "matched_control")


def _sources(entity_type: str, report_source: str = "STUDY") -> dict[str, object]:
    if entity_type == "STUDY":
        return {
            "study_id_sha256": H,
            "frozen_plan_sha256": H,
            "event_projection_sha256": None,
            "evidence_projection_sha256": None,
            "binding_projection_sha256": None,
            "holdout_projection_sha256": None,
            "catalog_schema_version": 64,
        }
    if entity_type == "EXPERIMENT":
        return {
            "experiment_id_sha256": H,
            "identity_sha256": H,
            "canonical_attempt_id_sha256": None,
            "catalog_schema_version": 64,
        }
    if entity_type == "ATTEMPT":
        return {
            "attempt_id_sha256": H,
            "experiment_id_sha256": H,
            "resolved_sha256": H,
            "result_digest_sha256": None,
            "catalog_schema_version": 64,
        }
    if entity_type == "METRIC_DOCUMENT":
        return {
            "binding_id_sha256": H,
            "study_id_sha256": H,
            "experiment_id_sha256": H,
            "attempt_id_sha256": H,
            "metric_document_sha256": None,
            "catalog_schema_version": 64,
        }
    return {
        "source_entity_type": report_source,
        "source_identity_sha256": H,
        "report_sha256": None,
        "catalog_schema_version": 64,
    }


def test_contract_exhaustively_matches_the_sealed_totality_counts():
    contexts = [
        ("STUDY", _sources("STUDY")),
        ("EXPERIMENT", _sources("EXPERIMENT")),
        ("ATTEMPT", _sources("ATTEMPT")),
        ("METRIC_DOCUMENT", _sources("METRIC_DOCUMENT")),
        ("REPORT", _sources("REPORT", "STUDY")),
        ("REPORT", _sources("REPORT", "ATTEMPT")),
    ]
    admitted = 0
    rejected = 0
    rejected_deployment_mutations = 0
    for entity_type, sources in contexts:
        for values in itertools.product(*(DIMENSION_VALUES[key] for key in BASE_KEYS)):
            dimensions = dict(zip(BASE_KEYS, values, strict=True))
            try:
                output = build_output(
                    entity_type=entity_type,
                    source_identities=sources,
                    **dimensions,
                )
            except ContractError:
                rejected += 1
                continue
            admitted += 1
            assert validate_output(output) == output
            alternatives = set(DIMENSION_VALUES["deployment_qualification"]) - {
                output["dimensions"]["deployment_qualification"]
            }
            for alternative in alternatives:
                mutated = copy.deepcopy(output)
                mutated["dimensions"]["deployment_qualification"] = alternative
                with pytest.raises(ContractError):
                    validate_output(mutated)
                rejected_deployment_mutations += 1
    assert admitted == 4_400
    assert rejected == 10_600
    assert rejected_deployment_mutations == 8_800


def test_positive_contract_fixtures_preserve_qualified_nonrankable_and_bocom_states():
    qualified = build_output(
        entity_type="STUDY",
        source_identities=_sources("STUDY"),
        integrity="VERIFIED_IMMUTABLE",
        accounting="AFTER_TAX_TOTAL_RETURN_VERIFIED",
        policy="CURRENT_BOUND",
        holdout_exposure="PRISTINE",
        matched_control="SUFFICIENT",
    )
    assert qualified["primary_state"] == "QUALIFIED_CURRENT_EVIDENCE"
    assert qualified["reason_codes"] == []
    assert qualified["effects"] == {
        "validated": True,
        "ranking_eligible": True,
        "promotion_evidence_eligible": True,
        "promotion_ready": False,
        "deployment_evidence_qualified": True,
        "deployment_authorized": False,
        "production_signal_authorized": False,
    }

    attempt = build_output(
        entity_type="ATTEMPT",
        source_identities=_sources("ATTEMPT"),
        integrity="VERIFIED_IMMUTABLE",
        accounting="AFTER_TAX_TOTAL_RETURN_VERIFIED",
        policy="CURRENT_BOUND",
        holdout_exposure="NOT_APPLICABLE",
        matched_control="NOT_APPLICABLE",
    )
    assert attempt["primary_state"] == "VALIDATED_CURRENT_NON_RANKABLE_EVIDENCE"
    assert attempt["reason_codes"] == ["ENTITY_NOT_RANKABLE", "NOT_DEPLOYMENT_QUALIFIED"]
    assert attempt["effects"]["validated"] is True
    assert sum(attempt["effects"].values()) == 1

    bocom = build_output(
        entity_type="METRIC_DOCUMENT",
        source_identities=_sources("METRIC_DOCUMENT"),
        integrity="VERIFIED_IMMUTABLE",
        accounting="KNOWN_EVENT_CORRECTED_PARTIAL",
        policy="LEGACY_POLICY",
        holdout_exposure="UNKNOWN",
        matched_control="MISSING",
    )
    assert bocom["primary_state"] == "KNOWN_EVENT_PARTIAL"
    assert bocom["reason_codes"][:2] == [
        "KNOWN_EVENT_PARTIAL",
        "INCOMPLETE_INTERVAL_COVERAGE",
    ]
    assert not any(bocom["effects"].values())


def test_unknown_versions_fields_duplicate_keys_and_identity_mismatches_fail_closed():
    canonical_sources = _sources("STUDY")
    valid = build_output(
        entity_type="STUDY",
        source_identities=canonical_sources,
        integrity="VERIFIED_IMMUTABLE",
        accounting="AFTER_TAX_TOTAL_RETURN_VERIFIED",
        policy="CURRENT_BOUND",
        holdout_exposure="PRISTINE",
        matched_control="SUFFICIENT",
    )
    unknown_version = valid | {"schema_version": 999}
    fallback = validate_or_fail_closed(
        unknown_version,
        expected_entity_type="STUDY",
        canonical_source_identities=canonical_sources,
    )
    assert fallback["primary_state"] == "UNKNOWN_ENTITY_VERSION"
    assert not any(fallback["effects"].values())

    for malformed in (
        valid | {"unexpected": True},
        valid | {"source_identities": _sources("STUDY") | {"study_id_sha256": "b" * 64}},
        valid | {"dimensions": valid["dimensions"] | {"unexpected": "x"}},
    ):
        fallback = validate_or_fail_closed(
            malformed,
            expected_entity_type="STUDY",
            canonical_source_identities=canonical_sources,
        )
        assert fallback["primary_state"] == "CONTRADICTORY_AUTHORITY"
        assert fallback["source_identities"] == canonical_sources
        assert not any(fallback["effects"].values())

    with pytest.raises(ContractError, match="duplicate JSON key"):
        strict_json_loads('{"schema_version":2,"schema_version":2}')
