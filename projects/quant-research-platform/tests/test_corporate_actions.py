from __future__ import annotations

import copy
import hashlib
from datetime import date
from pathlib import Path

import pytest

from quant_platform.corporate_actions import (
    CorporateActionEvidenceError,
    SettlementSchedule,
    accounting_cash_dividends,
    admit_corporate_action_evidence,
    dividend_tax_burden,
    identity_digest,
    load_strict_json,
    project_corporate_action_evidence,
    tax_policy_identity,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IDENTITY_FIXTURE = PROJECT_ROOT / "docs" / "fixtures" / "corporate-actions" / "identity-v1.json"
PDF_FIXTURE = Path(__file__).parent / "fixtures" / "corporate_actions" / "bocom_2025H1_dividend.pdf"


def _vectors() -> dict:
    return load_strict_json(IDENTITY_FIXTURE.read_bytes())


def bocom_evidence_inputs() -> dict:
    fixture = _vectors()
    vectors = {item["name"]: item for item in fixture["vectors"]}
    artifact_id = fixture["raw_artifact_vector"]["expected_artifact_id"]
    request = vectors["bocom_request"]
    retrieval = vectors["bocom_retrieval"]
    series = vectors["bocom_event_series"]
    revision = vectors["bocom_event_revision"]
    coverage = vectors["bocom_coverage"]
    return {
        "document": {
            "schema_version": 1,
            "collector_version": "accepted-audit-import@1",
            "source_contract_version": "bocom-xshg-dividend@1",
            "complete_enumeration_contract": False,
            "requests": [{"request_id": request["expected_sha256"], "payload": request["payload"]}],
            "retrievals": [
                {
                    "retrieval_id": retrieval["expected_sha256"],
                    "payload": retrieval["payload"],
                }
            ],
            "artifacts": [
                {
                    "artifact_id": artifact_id,
                    "body_sha256": fixture["raw_artifact_vector"]["body_sha256"],
                    "byte_length": fixture["raw_artifact_vector"]["body_bytes"],
                    "media_type": "application/pdf",
                    "path": f"corporate-action-{artifact_id}.bin",
                    "source_url": retrieval["payload"]["final_url"],
                }
            ],
            "revisions": [
                {
                    "event_revision_id": revision["expected_sha256"],
                    "event_series": series["payload"],
                    "payload": revision["payload"],
                    "available_at": "2026-08-31T15:06:07Z",
                    "use_role": "CAUSAL_FEATURE",
                    "source_url": retrieval["payload"]["final_url"],
                    "acceptance_state": "ACCEPTED",
                    "normalization_digest": revision["expected_sha256"],
                    "findings": [],
                }
            ],
            "coverage": {
                "coverage_id": coverage["expected_sha256"],
                "payload": coverage["payload"],
            },
            "findings": ["SSE_CO_PRIMARY_REJECTED_MEDIA_TYPE_HTML_CHALLENGE"],
            "total_return_claim": "KNOWN_EVENT_CORRECTED_PARTIAL",
        },
        "artifact_bytes": {artifact_id: PDF_FIXTURE.read_bytes()},
    }


def bocom_evidence():
    return admit_corporate_action_evidence(**bocom_evidence_inputs())


def _append_correction(
    inputs: dict,
    *,
    notice_id: str,
    corrects_notice_id: str,
    amount: str,
    available_at: str,
) -> dict:
    correction = copy.deepcopy(inputs["document"]["revisions"][0])
    correction["payload"]["contributing_notice_ids"] = [notice_id]
    correction["payload"]["correction_links"] = [corrects_notice_id]
    correction["payload"]["gross_cash_per_share"] = amount
    correction_id = identity_digest(
        "quant-platform/corporate-action-revision/v1", correction["payload"]
    )
    correction["event_revision_id"] = correction_id
    correction["normalization_digest"] = correction_id
    correction["available_at"] = available_at
    inputs["document"]["revisions"].append(correction)
    coverage = inputs["document"]["coverage"]
    coverage["payload"]["event_revision_ids"].append(correction_id)
    coverage["coverage_id"] = identity_digest(
        "quant-platform/corporate-action-coverage/v1", coverage["payload"]
    )
    return correction


def test_all_accepted_identity_vectors_reproduce_exactly():
    fixture = _vectors()
    raw = PDF_FIXTURE.read_bytes()

    assert len(raw) == fixture["raw_artifact_vector"]["body_bytes"]
    assert hashlib.sha256(raw).hexdigest() == fixture["raw_artifact_vector"]["body_sha256"]
    assert (
        identity_digest(fixture["domain_tags"]["artifact"], raw)
        == fixture["raw_artifact_vector"]["expected_artifact_id"]
    )
    for vector in fixture["vectors"]:
        assert identity_digest(vector["domain_tag"], vector["payload"]) == vector["expected_sha256"]


def test_bocom_evidence_admission_preserves_exact_source_and_event_identities():
    evidence = bocom_evidence()
    descriptor = evidence.document

    assert evidence.publishable is True
    assert descriptor["coverage"]["payload"]["coverage_state"] == "VERIFIED_EVENTS"
    assert descriptor["coverage"]["coverage_id"] == (
        "cfc3f55919fe62cb85a0afcefd16dd5d8d7cae29475deefab74c95275ebe3385"
    )
    assert descriptor["revisions"][0]["event_revision_id"] == (
        "2e02b9f67d5561bb5bf199233fb011d58fb98b22b02ea4f91ca0e0d22630ba3a"
    )
    assert descriptor["artifacts"][0]["body_sha256"] == (
        "c2da69cd9ababa957c029dfd4a11fcca08efb66b73d0bac381024676ffd1f7a6"
    )
    assert evidence.artifact_bytes[descriptor["artifacts"][0]["artifact_id"]] == (
        PDF_FIXTURE.read_bytes()
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("byte_length", 1, "byte length"),
        ("body_sha256", "0" * 64, "body digest"),
        ("media_type", "text/html", "media type"),
        ("source_url", "https://example.invalid/file.pdf", "source URL"),
    ],
)
def test_raw_source_mismatches_fail_closed(field: str, value, message: str):
    inputs = bocom_evidence_inputs()
    inputs["document"]["artifacts"][0][field] = value

    with pytest.raises(CorporateActionEvidenceError, match=message):
        admit_corporate_action_evidence(**inputs)


def test_request_retrieval_parser_and_normalization_mismatches_fail_closed():
    mutations = [
        (lambda document: document["requests"][0].__setitem__("request_id", "0" * 64), "request"),
        (
            lambda document: document["retrievals"][0].__setitem__("retrieval_id", "0" * 64),
            "retrieval",
        ),
        (
            lambda document: document["revisions"][0]["payload"].__setitem__(
                "parser_version", "wrong@1"
            ),
            "revision",
        ),
        (
            lambda document: document["revisions"][0].__setitem__("normalization_digest", "0" * 64),
            "normalization",
        ),
    ]
    for mutate, message in mutations:
        inputs = bocom_evidence_inputs()
        mutate(inputs["document"])
        with pytest.raises(CorporateActionEvidenceError, match=message):
            admit_corporate_action_evidence(**inputs)


def test_noncanonical_decimal_and_invalid_required_dates_fail_closed():
    for field, value, message in (
        ("gross_cash_per_share", "00.1563", "decimal"),
        ("record_date", "2025-12-26", "date order"),
        ("pay_date", "UNKNOWN", "date"),
    ):
        inputs = bocom_evidence_inputs()
        revision = inputs["document"]["revisions"][0]
        revision["payload"][field] = value
        revision_id = identity_digest(
            "quant-platform/corporate-action-revision/v1", revision["payload"]
        )
        revision["event_revision_id"] = revision_id
        revision["normalization_digest"] = revision_id
        coverage = inputs["document"]["coverage"]
        coverage["payload"]["event_revision_ids"] = [revision_id]
        coverage["coverage_id"] = identity_digest(
            "quant-platform/corporate-action-coverage/v1", coverage["payload"]
        )
        with pytest.raises(CorporateActionEvidenceError, match=message):
            admit_corporate_action_evidence(**inputs)


def test_rehashed_wrong_parser_and_source_contract_still_fail_closed():
    inputs = bocom_evidence_inputs()
    revision = inputs["document"]["revisions"][0]
    revision["payload"]["parser_version"] = "unreviewed-parser@1"
    revision_id = identity_digest(
        "quant-platform/corporate-action-revision/v1", revision["payload"]
    )
    revision["event_revision_id"] = revision_id
    revision["normalization_digest"] = revision_id
    coverage = inputs["document"]["coverage"]
    coverage["payload"]["event_revision_ids"] = [revision_id]
    coverage["coverage_id"] = identity_digest(
        "quant-platform/corporate-action-coverage/v1", coverage["payload"]
    )
    with pytest.raises(CorporateActionEvidenceError, match="parser identity"):
        admit_corporate_action_evidence(**inputs)

    inputs = bocom_evidence_inputs()
    inputs["document"]["source_contract_version"] = "unreviewed@1"
    with pytest.raises(CorporateActionEvidenceError, match="source_contract_version"):
        admit_corporate_action_evidence(**inputs)


def test_rehashed_wrong_official_url_still_fails_closed():
    inputs = bocom_evidence_inputs()
    wrong_url = "https://www.bankcomm.com/BankCommSite/file/fileDownload.html?fileId=wrong"
    inputs["document"]["artifacts"][0]["source_url"] = wrong_url
    retrieval = inputs["document"]["retrievals"][0]
    retrieval["payload"]["final_url"] = wrong_url
    retrieval["retrieval_id"] = identity_digest(
        "quant-platform/source-retrieval/v1", retrieval["payload"]
    )
    inputs["document"]["revisions"][0]["source_url"] = wrong_url

    with pytest.raises(CorporateActionEvidenceError, match="accepted source"):
        admit_corporate_action_evidence(**inputs)


def test_empty_source_is_unknown_and_unsupported_no_action_proof_fails():
    inputs = bocom_evidence_inputs()
    document = inputs["document"]
    document["revisions"] = []
    coverage_payload = document["coverage"]["payload"]
    coverage_payload["event_revision_ids"] = []
    coverage_payload["coverage_state"] = "UNKNOWN_MISSING"
    coverage_payload["limitations"] = ["EMPTY_QUERY_IS_NOT_COMPLETE_ABSENCE_EVIDENCE"]
    document["total_return_claim"] = "FORBIDDEN"
    document["coverage"]["coverage_id"] = identity_digest(
        "quant-platform/corporate-action-coverage/v1", coverage_payload
    )
    admitted = admit_corporate_action_evidence(**inputs)
    assert admitted.document["coverage"]["payload"]["coverage_state"] == "UNKNOWN_MISSING"

    document["coverage"]["payload"]["coverage_state"] = "VERIFIED_NO_ACTION"
    document["coverage"]["coverage_id"] = identity_digest(
        "quant-platform/corporate-action-coverage/v1", document["coverage"]["payload"]
    )
    with pytest.raises(CorporateActionEvidenceError, match="complete-enumeration"):
        admit_corporate_action_evidence(**inputs)

    inputs = bocom_evidence_inputs()
    inputs["document"]["complete_enumeration_contract"] = True
    with pytest.raises(CorporateActionEvidenceError, match="no admitted complete-enumeration"):
        admit_corporate_action_evidence(**inputs)


def test_causal_projection_excludes_post_cutoff_revision_and_changes_identity():
    evidence = bocom_evidence()

    before = project_corporate_action_evidence(evidence, "2026-08-30")
    after_close = project_corporate_action_evidence(evidence, "2026-08-31")
    after = project_corporate_action_evidence(evidence, "2026-09-01")

    assert before.document["revisions"] == []
    assert before.document["coverage"]["payload"]["coverage_state"] == "UNKNOWN_MISSING"
    assert before.document["total_return_claim"] == "FORBIDDEN"
    assert (
        "CAUSAL_CUTOFF_EXCLUDES_ACTION_EVIDENCE"
        in before.document["coverage"]["payload"]["limitations"]
    )
    assert after_close.document["revisions"] == []
    assert after_close.document["projection"]["decision_cutoff"] == {
        "market": "XSHG",
        "signal_time": "SESSION_CLOSE",
        "timezone": "Asia/Shanghai",
        "local_time": "15:00:00",
        "timestamp_utc": "2026-08-31T07:00:00Z",
    }
    assert after_close.document["projection"]["excluded_revisions"] == [
        {
            "event_revision_id": evidence.document["revisions"][0]["event_revision_id"],
            "reason": "AVAILABLE_AFTER_DECISION_CUTOFF",
        }
    ]
    assert (
        after.document["revisions"][0]["event_revision_id"]
        == evidence.document["revisions"][0]["event_revision_id"]
    )
    assert before.digest != after.digest
    assert after.document["revisions"][0]["use_role"] == "CAUSAL_FEATURE"
    assert after.document["total_return_claim"] == "KNOWN_EVENT_CORRECTED_PARTIAL"


def test_causal_projection_rejects_future_leakage_even_when_rehashed():
    projected = project_corporate_action_evidence(bocom_evidence(), "2026-09-01")
    document = copy.deepcopy(projected.document)
    document["revisions"][0]["available_at"] = "2026-09-01T08:00:00Z"

    with pytest.raises(CorporateActionEvidenceError, match="availability|future evidence"):
        admit_corporate_action_evidence(document, projected.artifact_bytes)


def test_explicit_correction_preserves_both_immutable_revisions():
    inputs = bocom_evidence_inputs()
    original = inputs["document"]["revisions"][0]
    correction = _append_correction(
        inputs,
        notice_id="临2025-080",
        corrects_notice_id="临2025-079",
        amount="0.1663",
        available_at="2026-08-31T15:07:00Z",
    )

    admitted = admit_corporate_action_evidence(**inputs)
    assert admitted.publishable is True
    assert [item["event_revision_id"] for item in admitted.document["revisions"]] == [
        original["event_revision_id"],
        correction["event_revision_id"],
    ]


def test_correction_chain_rejects_cross_series_and_reverse_availability():
    inputs = bocom_evidence_inputs()
    cross_series = _append_correction(
        inputs,
        notice_id="UNRELATED-REVISION",
        corrects_notice_id="临2025-079",
        amount="0.1663",
        available_at="2026-08-31T15:07:00Z",
    )
    cross_series["event_series"]["root_notice_id"] = "UNRELATED-ROOT"
    cross_series["payload"]["logical_event_id"] = identity_digest(
        "quant-platform/corporate-action-series/v1", cross_series["event_series"]
    )
    cross_series_id = identity_digest(
        "quant-platform/corporate-action-revision/v1", cross_series["payload"]
    )
    old_id = cross_series["event_revision_id"]
    cross_series["event_revision_id"] = cross_series_id
    cross_series["normalization_digest"] = cross_series_id
    coverage = inputs["document"]["coverage"]
    coverage["payload"]["event_revision_ids"][-1] = cross_series_id
    coverage["coverage_id"] = identity_digest(
        "quant-platform/corporate-action-coverage/v1", coverage["payload"]
    )
    assert old_id != cross_series_id
    with pytest.raises(CorporateActionEvidenceError, match="crosses an Event Series"):
        admit_corporate_action_evidence(**inputs)

    inputs = bocom_evidence_inputs()
    inputs["document"]["revisions"][0]["available_at"] = "2026-08-31T15:07:00Z"
    _append_correction(
        inputs,
        notice_id="临2025-080",
        corrects_notice_id="临2025-079",
        amount="0.1663",
        available_at="2026-08-31T15:06:30Z",
    )
    with pytest.raises(CorporateActionEvidenceError, match="availability precedes"):
        admit_corporate_action_evidence(**inputs)


def test_transitive_three_revision_correction_chain_is_not_a_same_rank_conflict():
    inputs = bocom_evidence_inputs()
    second = _append_correction(
        inputs,
        notice_id="临2025-080",
        corrects_notice_id="临2025-079",
        amount="0.1663",
        available_at="2026-08-31T15:07:00Z",
    )
    third = _append_correction(
        inputs,
        notice_id="临2025-081",
        corrects_notice_id="临2025-080",
        amount="0.1763",
        available_at="2026-08-31T15:08:00Z",
    )

    admitted = admit_corporate_action_evidence(**inputs)

    assert admitted.publishable is True
    assert admitted.quarantined_revision_ids == ()
    assert "SAME_RANK_OFFICIAL_CONFLICT" not in admitted.document["findings"]
    assert [item["event_revision_id"] for item in admitted.document["revisions"]][-2:] == [
        second["event_revision_id"],
        third["event_revision_id"],
    ]


def test_duplicate_and_same_rank_conflicts_are_quarantined_and_not_publishable():
    inputs = bocom_evidence_inputs()
    duplicate = copy.deepcopy(inputs["document"]["revisions"][0])
    inputs["document"]["revisions"].append(duplicate)
    with pytest.raises(CorporateActionEvidenceError, match="duplicate revision"):
        admit_corporate_action_evidence(**inputs)

    inputs = bocom_evidence_inputs()
    conflict = copy.deepcopy(inputs["document"]["revisions"][0])
    conflict["payload"]["gross_cash_per_share"] = "0.1663"
    conflict_id = identity_digest(
        "quant-platform/corporate-action-revision/v1", conflict["payload"]
    )
    conflict["event_revision_id"] = conflict_id
    conflict["normalization_digest"] = conflict_id
    inputs["document"]["revisions"].append(conflict)
    inputs["document"]["coverage"]["payload"]["event_revision_ids"].append(conflict_id)
    inputs["document"]["coverage"]["coverage_id"] = identity_digest(
        "quant-platform/corporate-action-coverage/v1",
        inputs["document"]["coverage"]["payload"],
    )

    admitted = admit_corporate_action_evidence(**inputs)
    assert admitted.publishable is False
    assert set(admitted.quarantined_revision_ids) == {
        inputs["document"]["revisions"][0]["event_revision_id"],
        conflict_id,
    }
    assert "SAME_RANK_OFFICIAL_CONFLICT" in admitted.document["findings"]


def test_strict_json_rejects_duplicate_keys_and_floats():
    with pytest.raises(CorporateActionEvidenceError, match="duplicate key"):
        load_strict_json(b'{"a":1,"a":2}')
    with pytest.raises(CorporateActionEvidenceError, match="floating-point"):
        load_strict_json(b'{"a":1.5}')


def test_tax_policy_identity_and_natural_period_boundaries_are_exact():
    policy = tax_policy_identity()

    assert policy["tax_policy_id"] == (
        "ea2910dace5c605a6ddd39b8346f7f12003689641c7db4b56a56ca3c015d3223"
    )
    assert policy["payload"]["assumptions"]["currency_rounding"] == (
        "ROUND_HALF_UP_RESEARCH_ASSUMPTION"
    )
    assert (
        str(dividend_tax_burden(date.fromisoformat("2025-11-06"), date.fromisoformat("2025-12-06")))
        == "0.20"
    )
    assert (
        str(dividend_tax_burden(date.fromisoformat("2025-11-06"), date.fromisoformat("2025-12-07")))
        == "0.10"
    )
    assert (
        str(dividend_tax_burden(date.fromisoformat("2025-01-22"), date.fromisoformat("2026-01-22")))
        == "0.10"
    )
    assert (
        str(dividend_tax_burden(date.fromisoformat("2025-01-22"), date.fromisoformat("2026-01-23")))
        == "0.00"
    )


def test_settlement_schedule_requires_explicit_transfer_and_collection_dates():
    schedule = SettlementSchedule(
        {"2026-01-05": "2026-01-06"},
        {"2026-01-06": "2026-01-07"},
    )

    assert schedule.settlement_date(date.fromisoformat("2026-01-05")) == date.fromisoformat(
        "2026-01-06"
    )
    assert schedule.collection_date(date.fromisoformat("2026-01-06")) == date.fromisoformat(
        "2026-01-07"
    )
    with pytest.raises(CorporateActionEvidenceError, match="unknown"):
        schedule.settlement_date(date.fromisoformat("2026-01-08"))
    with pytest.raises(CorporateActionEvidenceError, match="after transfer settlement"):
        SettlementSchedule(
            {"2026-01-05": "2026-01-06"},
            {"2026-01-06": "2026-01-06"},
        )


def test_accounting_projection_selects_only_explicit_terminal_correction():
    inputs = bocom_evidence_inputs()
    correction = _append_correction(
        inputs,
        notice_id="临2025-080",
        corrects_notice_id="临2025-079",
        amount="0.1663",
        available_at="2026-08-31T15:07:00Z",
    )

    actions = accounting_cash_dividends(admit_corporate_action_evidence(**inputs))

    assert [action.event_revision_id for action in actions] == [correction["event_revision_id"]]
    assert str(actions[0].gross_cash_per_share) == "0.1663"
