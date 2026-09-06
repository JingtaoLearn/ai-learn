import copy

import pytest

from quant_platform.total_return_claims import (
    ACCOUNT_NAMES,
    BINDING_FIELDS,
    CHECK_FIELDS,
    SOURCE_ISSUER_CLAIMS,
    TotalReturnQualificationError,
    canonical_json_bytes,
    is_trusted_qualification,
    qualification_record,
    qualify_total_return,
    read_time_classification,
)


SHA = "a" * 64


def _bindings() -> dict:
    value: dict = {field: SHA for field in BINDING_FIELDS}
    value.update(
        {
            "raw_artifact_sha256s": ["1" * 64],
            "event_revision_ids": ["2" * 64],
            "causal_feature_cutoff": "2026-01-01",
            "accounting_outcome_checked_as_of": "2026-01-02T00:00:00Z",
            "accounting_outcome_use_role": "ACCOUNTING_OUTCOME",
            "accounts": {
                name: {
                    "events_sha256": str(index) * 64,
                    "trades_sha256": str(index + 3) * 64,
                    "final_state_sha256": str(index + 6) * 64,
                }
                for index, name in enumerate(sorted(ACCOUNT_NAMES), start=1)
            },
        }
    )
    return value


def _checks(value: bool = True) -> dict[str, bool]:
    return {field: value for field in CHECK_FIELDS}


def _verified(**overrides):
    arguments = {
        "source_issuer": "STRATEGY_RUNNER",
        "source_total_return_claim": "KNOWN_EVENT_CORRECTED_PARTIAL",
        "coverage_state": "VERIFIED_COMPLETE_INTERVAL",
        "coverage_basis": "COMPLETE_ENUMERATION_CONTRACT",
        "coverage_id": "b" * 64,
        "complete_contract_id": "c" * 64,
        "equivalent_contract_approval_id": None,
        "bindings": _bindings(),
        "checks": _checks(),
        "historical_exposure": "PRISTINE",
    }
    arguments.update(overrides)
    return qualify_total_return(**arguments)


def test_synthetic_complete_interval_issues_pristine_verified_capability():
    qualification = _verified()
    record = qualification_record(qualification)

    assert is_trusted_qualification(qualification)
    assert record["issuer"] == "quant-platform/total-return-qualification@1"
    assert record["claim_state"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
    assert record["ranking"] == {
        "eligible_for_ranking": True,
        "eligible_for_promotion": True,
        "historical_exposure": "PRISTINE",
        "reason_codes": [],
    }
    assert len(record["qualification_id"]) == 64


def test_exposed_and_unknown_verified_accounting_can_never_be_eligible():
    for exposure, reason in (
        ("EXPOSED", "HISTORICALLY_EXPOSED"),
        ("UNKNOWN", "HISTORICAL_EXPOSURE_UNKNOWN"),
    ):
        record = qualification_record(_verified(historical_exposure=exposure))
        assert record["claim_state"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
        assert record["ranking"]["eligible_for_ranking"] is False
        assert record["ranking"]["eligible_for_promotion"] is False
        assert reason in record["ranking"]["reason_codes"]


def test_other_study_gate_withholds_promotion_and_supplies_stable_reason():
    record = qualification_record(_verified(other_study_gates_passed=False))
    assert record["ranking"]["eligible_for_ranking"] is True
    assert record["ranking"]["eligible_for_promotion"] is False
    assert record["ranking"]["reason_codes"] == ["OTHER_STUDY_GATE_FAILED"]


def test_real_bocom_shape_remains_partial_and_ineligible():
    checks = _checks()
    checks["coverage_complete"] = False
    record = qualification_record(
        qualify_total_return(
            source_issuer="STRATEGY_RUNNER",
            source_total_return_claim="KNOWN_EVENT_CORRECTED_PARTIAL",
            coverage_state="VERIFIED_EVENTS",
            coverage_basis="NONE",
            coverage_id="d" * 64,
            complete_contract_id=None,
            equivalent_contract_approval_id=None,
            bindings=_bindings(),
            checks=checks,
            historical_exposure="PRISTINE",
        )
    )
    assert record["claim_state"] == "KNOWN_EVENT_CORRECTED_PARTIAL"
    assert record["ranking"]["reason_codes"] == ["KNOWN_EVENT_PARTIAL"]
    assert record["ranking"]["eligible_for_ranking"] is False
    assert "AFTER_TAX_TOTAL_RETURN_VERIFIED" not in canonical_json_bytes(record).decode()


def test_plain_copy_and_mutated_capability_are_not_trusted():
    qualification = _verified()
    assert not is_trusted_qualification(dict(qualification))
    qualification["ranking"]["eligible_for_promotion"] = False
    assert not is_trusted_qualification(qualification)
    with pytest.raises(TotalReturnQualificationError, match="not pristine"):
        qualification_record(qualification)


@pytest.mark.parametrize("bad", [True, 1.0])
def test_identity_integer_aliases_reject_boolean_and_float(bad):
    bindings = _bindings()
    bindings["schema_version_for_test"] = bad
    with pytest.raises(
        TotalReturnQualificationError, match="binding fields|floating-point"
    ):
        _verified(bindings=bindings)
    if isinstance(bad, float):
        with pytest.raises(TotalReturnQualificationError, match="floating-point"):
            canonical_json_bytes({"count": bad})


def test_all_sixteen_source_issuer_claim_combinations_are_closed():
    claims = [
        "FORBIDDEN",
        "PRICE_RETURN_ONLY",
        "KNOWN_EVENT_CORRECTED_PARTIAL",
        "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
    ]
    for source_issuer, admitted in SOURCE_ISSUER_CLAIMS.items():
        for source_claim in claims:
            arguments = {
                "source_issuer": source_issuer,
                "source_total_return_claim": source_claim,
                "coverage_state": "UNKNOWN_MISSING",
                "coverage_basis": "NONE",
                "coverage_id": None,
                "complete_contract_id": None,
                "equivalent_contract_approval_id": None,
                "bindings": _bindings(),
                "checks": _checks(False),
                "historical_exposure": "UNKNOWN",
            }
            if source_claim in admitted:
                assert is_trusted_qualification(qualify_total_return(**arguments))
            else:
                with pytest.raises(TotalReturnQualificationError, match="forbidden"):
                    qualify_total_return(**arguments)


def test_linked_transition_requires_pristine_exact_prior_and_new_evidence():
    prior_checks = _checks()
    prior_checks["coverage_complete"] = False
    prior = qualify_total_return(
        source_issuer="STRATEGY_RUNNER",
        source_total_return_claim="KNOWN_EVENT_CORRECTED_PARTIAL",
        coverage_state="VERIFIED_EVENTS",
        coverage_basis="NONE",
        coverage_id="d" * 64,
        complete_contract_id=None,
        equivalent_contract_approval_id=None,
        bindings=_bindings(),
        checks=prior_checks,
        historical_exposure="PRISTINE",
    )
    successor = _verified(prior_qualification=prior)
    assert qualification_record(successor)["transition"]["from_state"] == (
        "KNOWN_EVENT_CORRECTED_PARTIAL"
    )
    with pytest.raises(TotalReturnQualificationError, match="same evidence"):
        _verified(prior_qualification=prior, same_corporate_action_evidence=True)


def test_read_time_classifier_never_issues_or_upgrades_historical_bytes():
    source = {"source_total_return_claim": "FORBIDDEN"}
    before = copy.deepcopy(source)
    classification = read_time_classification(
        source_issuer="HISTORICAL_RECORD",
        source_total_return_claim=source["source_total_return_claim"],
        coverage_state="UNKNOWN_MISSING",
    )
    assert source == before
    assert classification["claim_state"] == "PRICE_RETURN_ONLY"
    assert classification["trusted_qualification_absent"] is True
    assert classification["ranking"]["eligible_for_ranking"] is False


def test_missing_settlement_policy_identity_prevents_verified_issuance():
    bindings = _bindings()
    bindings["settlement_policy_id"] = None
    with pytest.raises(TotalReturnQualificationError, match="settlement_policy_id"):
        _verified(bindings=bindings)
