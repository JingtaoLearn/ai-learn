import copy
import hashlib
import json

import pytest
from jsonschema import Draft202012Validator

from quant_platform.study_qualification import (
    MATCHED_EXPOSURE_SCHEMA_SHA256,
    POST_SELECTION_AUTHORITY,
    QUALIFICATION_SCHEMA,
    QUALIFICATION_SCHEMA_BYTES,
    QualificationError,
    admit_candidate,
    basic_arithmetic_fixture,
    bootstrap_seed,
    canonical_json_bytes,
    canonical_json_fixture_bytes,
    classify_episodes,
    component_stress_fixture,
    effective_sample_size,
    evaluate_gates,
    forward_issuance_summary_bytes,
    hashed_projection_fixture,
    holm_adjust,
    issue_intake,
    issue_post_selection,
    matched_returns,
    no_qualified_candidate,
    numerical_runtime,
    rank_qualified,
    retrospective_classification,
    seal_family,
    stationary_block_bootstrap,
    strict_json_loads,
    validate_schema,
)


def _sha(value: str) -> str:
    return value * 64


def test_reviewed_schema_is_embedded_byte_for_byte_and_structurally_valid():
    assert hashlib.sha256(QUALIFICATION_SCHEMA_BYTES).hexdigest() == (
        MATCHED_EXPOSURE_SCHEMA_SHA256
    )
    assert json.loads(QUALIFICATION_SCHEMA_BYTES) == QUALIFICATION_SCHEMA
    assert QUALIFICATION_SCHEMA["x-strict-authority-validator"]["required_invariants"] == [
        item
        for item in QUALIFICATION_SCHEMA["x-strict-authority-validator"]["required_invariants"]
        if item.startswith("QAV-")
    ]
    assert len(QUALIFICATION_SCHEMA["x-strict-authority-validator"]["required_invariants"]) == 12
    Draft202012Validator.check_schema(QUALIFICATION_SCHEMA)


def test_qualification_canonical_json_normative_fixture():
    payload = canonical_json_fixture_bytes()
    assert len(payload) == 376
    assert hashlib.sha256(payload).hexdigest() == (
        "b20b069d5aff1df1f4727aa7ab7b4bec725230a0e6f4afa3279ba5ceda554730"
    )
    assert b'["f64","8000000000000000"]' in payload
    assert b'["f64","0000000000000000"]' in payload


def test_forward_issuance_summary_is_ancestry_complete_and_exact():
    payload = forward_issuance_summary_bytes()
    assert len(payload) == 2406
    assert hashlib.sha256(payload).hexdigest() == (
        "50916e424511332d648f6b94ad075e8822c346c237a31f0609e3e6d773d2fde5"
    )


def test_all_twelve_hashed_projection_domains_and_outer_fixture_are_exact():
    fixture = hashed_projection_fixture()
    assert fixture["control_output"]["output_digest"] == (
        "1fe39a367336a9b64855d91b01a629417d61217e629228d73a687f1600073a0d"
    )
    assert fixture["family_record"]["family_digest"] == (
        "9819a89f2fc92ba2fcfa904865de54f22b8d27b2d50653fc89a5a8db3fb4a2c0"
    )
    assert fixture["numerical_runtime"]["manifest_sha256"] == (
        "cea0096efa08c3e6f7c35fa3943c2502603eb0799d9a9bb6c209561ca05ac00a"
    )


def test_schema_directed_numbers_ignore_equivalent_lexemes_and_reject_bool():
    schema = {
        "type": "object",
        "required": ["number"],
        "additionalProperties": False,
        "properties": {"number": {"type": "number"}},
    }
    values = [
        strict_json_loads(item) for item in ('{"number":1}', '{"number":1.0}', '{"number":1e0}')
    ]
    assert len({canonical_json_bytes(item, schema, root=schema) for item in values}) == 1
    with pytest.raises(QualificationError, match="wrong type"):
        canonical_json_bytes({"number": True}, schema, root=schema)


@pytest.mark.parametrize(
    "payload, message",
    [
        (b'{"x":1,"x":2}', "duplicate object key"),
        (b'{"x":NaN}', "non-finite"),
        (b"\xef\xbb\xbf{}", "BOM"),
    ],
)
def test_strict_json_rejects_ambiguous_or_invalid_input(payload, message):
    with pytest.raises(QualificationError, match=message):
        strict_json_loads(payload)


def test_basic_binary64_fixture_reproduces_exact_hashes_and_values():
    fixture_input, output = basic_arithmetic_fixture()
    encoded_input = canonical_json_bytes(fixture_input, "#/$defs/binary64_basic_input")
    encoded_output = canonical_json_bytes(output, "#/$defs/binary64_basic_output")
    assert (len(encoded_input), hashlib.sha256(encoded_input).hexdigest()) == (
        715,
        "34fda3676016c9d7d056d1c9c6ab7c0c1ed47a08f83f6d103f8bc2dc880f1d84",
    )
    assert (len(encoded_output), hashlib.sha256(encoded_output).hexdigest()) == (
        337,
        "b4fba1d01a321c5197d61b9adb4f35f3e5f3eda23fe40721e621377afc65c828",
    )
    assert output["cash_aggregate"].hex() == "0x1.39e15188b2c00p-8"
    assert output["candidate_aggregate"].hex() == "-0x1.799bda0000000p-30"
    assert output["matched_excess"].hex() == "-0x1.dbf2cf625e700p-9"


def test_component_stress_fixture_reproduces_all_schema_bound_hashes():
    fixture_input, output = component_stress_fixture()
    encoded_input = canonical_json_bytes(fixture_input, "#/$defs/component_stress_input")
    encoded_output = canonical_json_bytes(output, "#/$defs/component_stress_output")
    assert (len(encoded_input), hashlib.sha256(encoded_input).hexdigest()) == (
        3514,
        "7e73c296f0e38e9af244d097139515e769f1861b7c4ea68fa6df5dc18d7e8e88",
    )
    assert (len(encoded_output), hashlib.sha256(encoded_output).hexdigest()) == (
        7209,
        "597747cf86b79b459058272dd3a3bd90ab9e0b53282ba7fe85d3d26de6affd5e",
    )
    assert output["candidate"]["events"][0]["stressed_total_cost_bits"] == ("401f75c28f5c28f6")
    assert output["candidate_aggregate_return_bits"] == "3f62c91029c3c800"
    assert output["matched_aggregate_return_bits"] == "3f5794c5ca293800"
    assert output["active_excess_boundaries"] == {
        "next_down_bits": "3f4bfab512bcafff",
        "next_down_pass": True,
        "equal_bits": "3f4bfab512bcb000",
        "equal_pass": True,
        "next_up_bits": "3f4bfab512bcb001",
        "next_up_pass": False,
    }


def test_component_stress_rejects_aggregate_or_omitted_component():
    fixture_input, _ = component_stress_fixture()
    aggregate = copy.deepcopy(fixture_input)
    aggregate["accounts"][0]["events"][0]["aggregate_cost"] = 1.0
    with pytest.raises(QualificationError, match="unknown fields"):
        validate_schema(aggregate, "#/$defs/component_stress_input")
    omitted = copy.deepcopy(fixture_input)
    del omitted["accounts"][0]["events"][0]["base_components"]["settlement_cost"]
    with pytest.raises(QualificationError, match="missing required"):
        validate_schema(omitted, "#/$defs/component_stress_input")


def test_seed_derivation_and_stationary_block_bootstrap_toy():
    digest, state = bootstrap_seed(7, "a" * 64)
    assert digest == "3212b52e6302bb94775b5d816302183a2da368379c13e5816a6bd23eb02849a0"
    assert state == 0x3212B52E6302BB94
    result = stationary_block_bootstrap(
        [[0.02, -0.01, 0.03], [0.0, 0.015]],
        [[0.01, 0.0, 0.02], [-0.005, 0.01]],
        initial_state=7,
        resamples=9,
        mean_length=2.0,
    )
    assert result.observed.hex() == "0x1.ff6a685a15430p-9"
    assert result.traces == (
        ((1, 2, 0), (0, 0)),
        ((0, 1, 2), (1, 0)),
        ((2, 0, 1), (0, 1)),
        ((2, 0, 1), (0, 1)),
        ((2, 1, 2), (0, 1)),
        ((0, 0, 1), (1, 1)),
        ((0, 1, 2), (0, 0)),
        ((0, 1, 2), (1, 0)),
        ((2, 0, 1), (0, 0)),
    )
    assert result.exceedance_count == 0
    assert result.raw_p_value == 0.1


def test_bootstrap_inclusive_boundary_counts_equal_statistics():
    result = stationary_block_bootstrap(
        [[0.01, 0.0]],
        [[0.0, 0.01]],
        initial_state=7,
        resamples=9,
        mean_length=2.0,
    )
    assert result.observed.hex() == "0x0.0p+0"
    assert result.exceedance_count == 8
    assert result.raw_p_value == 0.9


def test_not_testable_and_holm_tie_rules_are_exact():
    with pytest.raises(QualificationError, match="ZERO_CENTERED_VARIANCE"):
        stationary_block_bootstrap(
            [[0.0], [0.0]],
            [[0.0], [0.0]],
            initial_state=7,
            resamples=9,
            mean_length=2.0,
        )
    assert holm_adjust({"a" * 64: 0.25, "b" * 64: 0.25, "c" * 64: None}) == {
        "a" * 64: 0.75,
        "b" * 64: 0.75,
        "c" * 64: None,
    }


def test_zero_and_overlapping_natural_episode_conventions():
    assert effective_sample_size([]) == 0.0
    assert effective_sample_size([1, 1]) == 2.0
    assert effective_sample_size([2, 1]) == 1.8
    with pytest.raises(QualificationError, match="positive integers"):
        effective_sample_size([0])


def _policy() -> dict:
    return {
        "policy_id": "robust_walk_forward",
        "version": "2.0.0",
        "source_digest": "9" * 64,
        "frozen_before_first_result": True,
        "minimum_economic_edge": 0.0,
        "minimum_matched_excess": 0.0,
        "minimum_fold_matched_excess": 0.0,
        "minimum_passing_fold_fraction": 1.0,
        "maximum_allowed_fold_shortfall": 0.0,
        "minimum_stressed_economic_edge": 0.0,
        "minimum_stressed_matched_excess": 0.0,
        "minimum_natural_exits": 2,
        "minimum_effective_sample_size": 1.0,
        "family_wise_alpha": 0.05,
        "bootstrap_seed": 7,
        "bootstrap_resamples": 1000,
        "stationary_block_mean_length": 2.0,
    }


def _gate_inputs(candidate_returns=(0.03, 0.0), matched_returns=(0.005, 0.005)):
    fold = "e" * 64
    sessions = [
        {
            "session_id": digest,
            "valuation_date": day,
            "fold_id": fold,
            "candidate_costed_return": candidate,
            "cash_return": 0.0,
            "buy_and_hold_total_return": 0.005,
            "matched_control_return": matched,
            "opening_gross_exposure": 0.5,
        }
        for digest, day, candidate, matched in zip(
            ("0" * 64, "f" * 64),
            ("2020-01-01", "2020-01-02"),
            candidate_returns,
            matched_returns,
            strict=True,
        )
    ]
    folds = [
        {
            "fold_id": fold,
            "scoring_start": "2020-01-01",
            "scoring_end": "2020-01-02",
        }
    ]
    episodes = {
        "natural_exit_count": 2,
        "forced_exit_count": 0,
        "open_episode_count": 0,
        "dependence_cluster_sizes": [1, 1],
        "effective_sample_size": 2.0,
    }
    continuity = {"status": "VERIFIED", "state_reset_count": 0}
    stress = [{"economic_edge": 0.01, "active_excess": 0.005}]
    multiplicity = {
        "test_status": "ESTABLISHED",
        "adjusted_p_value": 0.04,
    }
    return sessions, folds, episodes, continuity, stress, multiplicity


def test_profitable_candidate_below_matched_exposure_is_rejected_before_ranking():
    inputs = _gate_inputs(candidate_returns=(0.01, 0.01), matched_returns=(0.02, 0.02))
    result = evaluate_gates(
        policy=_policy(),
        sessions=inputs[0],
        folds=inputs[1],
        episodes=inputs[2],
        continuous_state=inputs[3],
        stress_results=inputs[4],
        multiplicity_entry=inputs[5],
    )
    assert result["aggregate"]["candidate_costed_return"] > 0.0
    assert result["gates"]["matched_exposure_excess"] == {
        "status": "FAIL",
        "reason_code": "MATCHED_EXPOSURE_EXCESS_FAILED",
        "evidence_sha256": result["gates"]["matched_exposure_excess"]["evidence_sha256"],
    }
    assert result["state"] == "REJECTED"
    assert result["validation_score"] is None
    assert result["ranking_position"] is None


def test_forced_liquidation_is_not_a_natural_exit_or_closed_timing_episode():
    counts = classify_episodes(
        [
            {"side": "BUY", "reason": "SIGNAL"},
            {"side": "SELL", "reason": "SIGNAL"},
            {"side": "BUY", "reason": "SIGNAL"},
            {"side": "SELL", "reason": "TERMINAL_FORCED_LIQUIDATION"},
        ]
    )
    assert counts == {
        "natural_exit_count": 1,
        "forced_exit_count": 1,
        "open_episode_count": 1,
    }


def test_zero_natural_exits_fail_both_episode_gates_without_division():
    sessions, folds, _, continuity, stress, multiplicity = _gate_inputs()
    episodes = {
        "natural_exit_count": 0,
        "forced_exit_count": 1,
        "open_episode_count": 1,
        "dependence_cluster_sizes": [],
        "effective_sample_size": 0.0,
    }
    result = evaluate_gates(
        policy=_policy(),
        sessions=sessions,
        folds=folds,
        episodes=episodes,
        continuous_state=continuity,
        stress_results=stress,
        multiplicity_entry=multiplicity,
    )
    assert result["gates"]["natural_episode_evidence"]["status"] == "FAIL"
    assert result["gates"]["effective_sample_size"]["status"] == "FAIL"
    assert result["state"] == "REJECTED"


def test_one_session_is_not_testable_and_rejected_without_bootstrap():
    sessions, folds, episodes, continuity, stress, _ = _gate_inputs()
    sessions = sessions[:1]
    folds[0]["scoring_end"] = "2020-01-01"
    result = evaluate_gates(
        policy=_policy(),
        sessions=sessions,
        folds=folds,
        episodes=episodes,
        continuous_state=continuity,
        stress_results=stress,
        multiplicity_entry={
            "test_status": "NOT_TESTABLE",
            "adjusted_p_value": None,
            "not_testable_reason": "TOO_FEW_SESSIONS",
        },
    )
    assert result["gates"]["significance"]["reason_code"] == ("SIGNIFICANCE_NOT_TESTABLE")
    assert result["state"] == "REJECTED"


def test_matched_control_uses_fsum_mean_and_nonfused_affine_terms():
    average, returns = matched_returns(
        [1.0, 2**-53, 2**-53],
        [0.01, -0.02, 0.015],
        [0.001, 0.001, 0.001],
    )
    assert average.hex() == "0x1.5555555555557p-2"
    assert [item.hex() for item in returns] == [
        "0x1.0624dd2f1a9fdp-8",
        "-0x1.89374bc6a7efcp-8",
        "0x1.735ee402bb0d0p-8",
    ]


def test_no_qualified_candidate_has_no_scalar_order_or_holdout_effect():
    records = [
        {
            "candidate_digest": "b" * 64,
            "qualification_id": "2" * 64,
            "state": "REJECTED",
        },
        {
            "candidate_digest": "a" * 64,
            "qualification_id": "1" * 64,
            "state": "ADMISSION_REJECTED",
        },
    ]
    terminal = no_qualified_candidate(records)
    assert terminal == {
        "qualification_outcome": "NO_QUALIFIED_CANDIDATE",
        "decision": "REJECTED_NO_EDGE",
        "selection_outcome": "NO_QUALIFIED_CANDIDATE",
        "champion": None,
        "champion_evidence": None,
        "holdout": {"access": "NOT_GRANTED", "outcome": "NOT_RUN"},
        "ranking": [
            {
                "candidate_digest": "a" * 64,
                "qualification_id": "1" * 64,
                "state": "ADMISSION_REJECTED",
                "ranking_status": "NOT_RANKED",
            },
            {
                "candidate_digest": "b" * 64,
                "qualification_id": "2" * 64,
                "state": "REJECTED",
                "ranking_status": "NOT_RANKED",
            },
        ],
    }


def test_ranker_rejects_plain_or_rejected_candidate_before_score_comparison():
    forged = {
        "candidate_digest": "a" * 64,
        "validation_score": 1_000_000.0,
        "qualification": {"state": "QUALIFIED", "candidate_digest": "a" * 64},
        "tie_break": {
            "lower_maximum_drawdown": 0.0,
            "lower_annual_turnover": 0.0,
            "strategy_configuration_digest": "a" * 64,
        },
    }
    with pytest.raises(QualificationError, match="pristine QUALIFIED"):
        rank_qualified([forged])


def test_exposed_and_historical_v1_evidence_are_read_only_classifications():
    assert retrospective_classification("EXPOSED", version="2.0.0") == (
        "RETROSPECTIVE_DIAGNOSIS_ONLY"
    )
    assert retrospective_classification("PRISTINE", version="1.0.0") == (
        "LEGACY_NO_MATCHED_EXPOSURE_QUALIFICATION"
    )


def test_post_selection_authority_is_role_isolated_and_ranking_ineligible():
    common = {
        "record_kind": "OUTER_AUDIT_EVALUATION",
        "phase": "POST_SELECTION_OUTER_AUDIT",
        "development_qualification_id": _sha("1"),
        "selection_run_id": _sha("2"),
        "selected_candidate_digest": _sha("3"),
        "selected_ranking_digest": _sha("4"),
        "champion_digest": None,
        "champion_freeze_digest": None,
        "holdout_grant_digest": None,
        "grant_consumption_digest": None,
        "folds": [
            {
                "fold_id": _sha("5"),
                "role": "OUTER_AUDIT",
                "scoring_start": "2026-01-01",
                "scoring_end": "2026-01-02",
            }
        ],
        "session_ids": [_sha("6")],
        "valuation_dates": ["2026-01-02"],
        "outcome": "PASSED",
        "ranking_eligible": False,
    }
    record = issue_post_selection(common)
    assert record["authority"] == POST_SELECTION_AUTHORITY
    assert record["ranking_eligible"] is False
    changed = copy.deepcopy(common)
    changed["folds"][0]["role"] = "TERMINAL_HOLDOUT"
    with pytest.raises(QualificationError, match="phase or role"):
        issue_post_selection(changed)


def test_live_runtime_binds_complete_nested_objects_and_conformance():
    runtime = numerical_runtime()
    validate_schema(runtime, "#/$defs/numerical_runtime")
    assert runtime["canonical_json_conformance"]["passed"] is True
    assert runtime["binary64_arithmetic_conformance"]["fixtures"][0]["passed"] is True
    assert runtime["loaded_native_objects"]["entries"]
    assert runtime["manifest_sha256"] != "0" * 64


def test_raw_intake_gets_content_identity_and_truthful_missing_candidate_rejection():
    raw = b'{"candidate_digest":null,"study_id":"' + b"a" * 64 + b'"}'
    record = issue_intake(raw)
    assert record["state"] == "INTAKE_REJECTED"
    assert record["candidate_digest"] is None
    assert record["admission_rejection"]["missing_fields"] == ["candidate_digest"]
    assert record["admission_rejection"]["raw_intake_sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["intake_id"] != record["admission_rejection"]["raw_intake_sha256"]


def test_each_identified_candidate_gets_a_distinct_unadmitted_predecessor():
    common = (
        b'{"historical_exposure":"PRISTINE","study_id":"' + b"a" * 64 + b'","candidate_digest":"'
    )
    first = issue_intake(common + b"b" * 64 + b'"}')
    second = issue_intake(common + b"c" * 64 + b'"}')
    assert first["state"] == second["state"] == "UNADMITTED"
    assert first["qualification_id"] != second["qualification_id"]
    assert first["intake_id"] != second["intake_id"]


def test_one_session_family_closes_not_testable_without_bootstrap():
    raw = json.dumps(
        {
            "study_id": "a" * 64,
            "candidate_digest": "b" * 64,
            "study_plan_digest": "c" * 64,
            "selection_run_id": "d" * 64,
            "historical_exposure": "PRISTINE",
        },
        separators=(",", ":"),
    ).encode()
    initial = issue_intake(raw)
    admitted = admit_candidate(
        initial,
        raw_intake_sha256=hashlib.sha256(raw).hexdigest(),
        study_plan_digest="c" * 64,
        selection_run_id="d" * 64,
        trusted_claim={
            "issuer": "quant-platform/total-return-qualification@1",
            "claim_state": "AFTER_TAX_TOTAL_RETURN_VERIFIED",
            "qualification_id": "e" * 64,
            "ranking": {
                "eligible_for_ranking": True,
                "historical_exposure": "PRISTINE",
            },
        },
    )
    session = {
        "session_id": "f" * 64,
        "valuation_date": "2026-01-05",
        "fold_id": "1" * 64,
        "candidate_costed_return": 0.01,
        "cash_return": 0.0,
        "buy_and_hold_total_return": 0.01,
        "matched_control_return": 0.005,
        "opening_gross_exposure": 0.5,
    }
    family = seal_family(
        prefamily_records=[admitted],
        scored_session_sets={"b" * 64: [session]},
        search={
            "suggester_id": "synthetic-grid",
            "suggester_version": "1",
            "source_digest": "2" * 64,
            "seed": 7,
            "candidate_budget": 1,
            "evaluation_budget": 1,
            "stop_reason": "SEARCH_EXHAUSTED",
            "search_exhausted": True,
        },
        policy=_policy(),
        stress_scenario_set_digest="3" * 64,
        timing_placebo_plan_digest=None,
    )
    assert (
        family["candidate_population_entries"][0]["prefamily_qualification_id"]
        == (admitted["qualification_id"])
    )
    assert family["multiplicity_entries"] == [
        {
            "candidate_digest": "b" * 64,
            "raw_p_value": None,
            "adjusted_p_value": None,
            "test_status": "NOT_TESTABLE",
            "not_testable_reason": "TOO_FEW_SESSIONS",
            "artifact_sha256": family["multiplicity_entries"][0]["artifact_sha256"],
        }
    ]
