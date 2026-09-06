import copy
from pathlib import Path

import pytest

import quant_platform.total_return_claims as claims
from quant_platform.total_return_claims import (
    SOURCE_ISSUER_CLAIMS,
    TotalReturnQualificationError,
    canonical_json_bytes,
    is_trusted_qualification,
    qualification_record,
    qualify_total_return,
    read_time_classification,
)
from test_study_evaluation import _trusted_attempt_and_factory, _trusted_document


def _qualification(tmp_path: Path, **options):
    factory, attempt, _ = _trusted_attempt_and_factory(tmp_path, **options)
    return qualify_total_return(
        state_root=factory.state_root,
        result_path=attempt["result_path"],
        instrument=attempt["resolved"]["dataset"]["instrument"],
        expected_dataset_snapshot_id=attempt["resolved"]["dataset"]["snapshot_id"],
        expected_result_digest=attempt["result_digest"],
        historical_exposure=attempt["requested"]["historical_exposure"],
    )


def test_synthetic_complete_interval_issues_pristine_verified_capability(tmp_path: Path):
    qualification = _qualification(tmp_path)
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


def test_issuer_is_not_importable_or_callable_without_verified_evidence(tmp_path: Path):
    assert not hasattr(claims, "_issue")
    forged = claims.TrustedTotalReturnQualification(
        {
            "claim_state": "AFTER_TAX_TOTAL_RETURN_VERIFIED",
            "ranking": {"eligible_for_ranking": True, "eligible_for_promotion": True},
        }
    )
    assert not is_trusted_qualification(forged)
    with pytest.raises(TotalReturnQualificationError, match="not pristine"):
        qualification_record(forged)

    with pytest.raises(TypeError, match="unexpected keyword"):
        qualify_total_return(
            checks={field: True for field in claims.CHECK_FIELDS},
            bindings={field: "a" * 64 for field in claims.BINDING_FIELDS},
        )

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TotalReturnQualificationError):
        qualify_total_return(
            state_root=empty,
            result_path=empty,
            instrument="601328.SS",
            expected_dataset_snapshot_id="a" * 64,
            expected_result_digest="b" * 64,
            historical_exposure="PRISTINE",
        )


def test_plain_copy_and_mutated_capability_are_not_trusted(tmp_path: Path):
    qualification = _qualification(tmp_path)
    assert not is_trusted_qualification(dict(qualification))
    qualification["ranking"]["eligible_for_promotion"] = False
    assert not is_trusted_qualification(qualification)
    with pytest.raises(TotalReturnQualificationError, match="not pristine"):
        qualification_record(qualification)


def test_exposed_and_unknown_verified_accounting_can_never_be_eligible(tmp_path: Path):
    for exposure, reason in (
        ("EXPOSED", "HISTORICALLY_EXPOSED"),
        ("UNKNOWN", "HISTORICAL_EXPOSURE_UNKNOWN"),
    ):
        document = _trusted_document(tmp_path / exposure.lower(), historical_exposure=exposure)
        record = document["total_return_qualification"]
        assert record["claim_state"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
        assert record["ranking"]["eligible_for_ranking"] is False
        assert record["ranking"]["eligible_for_promotion"] is False
        assert reason in record["ranking"]["reason_codes"]


def test_same_verified_evidence_can_only_continue_verified_lineage(tmp_path: Path):
    prior = _qualification(tmp_path)
    factory, attempt, _ = _trusted_attempt_and_factory(tmp_path / "successor")
    successor = qualify_total_return(
        state_root=factory.state_root,
        result_path=attempt["result_path"],
        instrument="601328.SS",
        expected_dataset_snapshot_id=attempt["resolved"]["dataset"]["snapshot_id"],
        expected_result_digest=attempt["result_digest"],
        historical_exposure="PRISTINE",
        prior_qualification=prior,
    )
    transition = qualification_record(successor)["transition"]
    assert transition["from_state"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
    assert transition["to_state"] == "AFTER_TAX_TOTAL_RETURN_VERIFIED"


def test_full_record_mutations_reject_before_capability_issuance(tmp_path: Path):
    record = qualification_record(_qualification(tmp_path))
    mutations = [
        lambda value: value["ranking"].__setitem__("historical_exposure", "EXPOSED"),
        lambda value: value["ranking"].__setitem__("historical_exposure", "UNKNOWN"),
        lambda value: value["transition"].__setitem__("to_state", "PRICE_RETURN_ONLY"),
        lambda value: value.__setitem__("source_issuer", "CORPORATE_ACTION_COLLECTOR"),
        lambda value: value["bindings"].__setitem__("settlement_policy_id", None),
        lambda value: value.__setitem__("issuer", "STUDY_EVALUATOR"),
        lambda value: value.__setitem__("schema_version", True),
        lambda value: value["checks"].__setitem__("issuer_authorized", False),
        lambda value: value["ranking"].__setitem__("eligible_for_ranking", False),
        lambda value: value["coverage_state"].__setitem__ if False else value.__setitem__(
            "coverage_state", "UNKNOWN_MISSING"
        ),
    ]
    for mutate in mutations:
        candidate = copy.deepcopy(record)
        mutate(candidate)
        candidate["qualification_id"] = claims.qualification_id(
            {key: value for key, value in candidate.items() if key != "qualification_id"}
        )
        with pytest.raises(TotalReturnQualificationError):
            claims._validate_record(candidate, None)


def test_read_time_classifier_preserves_real_bocom_partial_and_historical_bytes():
    source = {"source_total_return_claim": "KNOWN_EVENT_CORRECTED_PARTIAL"}
    before = copy.deepcopy(source)
    classification = read_time_classification(
        source_issuer="STRATEGY_RUNNER",
        source_total_return_claim=source["source_total_return_claim"],
        coverage_state="VERIFIED_EVENTS",
    )
    assert source == before
    assert classification["claim_state"] == "KNOWN_EVENT_CORRECTED_PARTIAL"
    assert classification["ranking"]["eligible_for_ranking"] is False
    assert "AFTER_TAX_TOTAL_RETURN_VERIFIED" not in canonical_json_bytes(classification).decode()


def test_strict_identity_json_rejects_boolean_float_and_duplicate_aliases():
    with pytest.raises(TotalReturnQualificationError, match="floating-point"):
        canonical_json_bytes({"count": 1.0})
    with pytest.raises(TotalReturnQualificationError, match="duplicate"):
        claims.load_strict_json(b'{"schema_version":1,"schema_version":1}')


def test_all_sixteen_source_issuer_claim_combinations_are_closed():
    claims_under_test = [
        "FORBIDDEN",
        "PRICE_RETURN_ONLY",
        "KNOWN_EVENT_CORRECTED_PARTIAL",
        "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
    ]
    for source_issuer, admitted in SOURCE_ISSUER_CLAIMS.items():
        for source_claim in claims_under_test:
            if source_claim in admitted:
                coverage = (
                    "VERIFIED_EVENTS"
                    if source_claim == "KNOWN_EVENT_CORRECTED_PARTIAL"
                    else "UNKNOWN_MISSING"
                )
                result = read_time_classification(
                    source_issuer=source_issuer,
                    source_total_return_claim=source_claim,
                    coverage_state=coverage,
                )
                assert result["ranking"]["eligible_for_ranking"] is False
            else:
                with pytest.raises(TotalReturnQualificationError, match="forbidden"):
                    read_time_classification(
                        source_issuer=source_issuer,
                        source_total_return_claim=source_claim,
                        coverage_state="UNKNOWN_MISSING",
                    )
