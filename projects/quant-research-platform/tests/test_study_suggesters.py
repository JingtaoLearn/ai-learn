from __future__ import annotations

import hashlib
import warnings
from copy import deepcopy
from math import prod
from typing import Any

from optuna.distributions import (
    CategoricalDistribution,
    FloatDistribution,
    IntDistribution,
)
from optuna.trial import TrialState
import pytest

import quant_platform.study_suggesters as study_suggesters
from quant_platform.schemas import canonical_json_bytes
from quant_platform.study_suggesters import (
    EvaluationRole,
    Exhausted,
    ExhaustionReason,
    GridParameterSuggester,
    HistoryEventType,
    MAX_CANDIDATE_CAPACITY,
    MAX_CANDIDATE_CANONICAL_BYTES,
    MAX_JSON_CONTAINER_SIZE,
    MAX_JSON_NESTING_DEPTH,
    MAX_JSON_STRING_BYTES,
    MAX_ORDERED_HISTORY_LENGTH,
    MAX_SEARCH_DIMENSIONS,
    MAX_SUGGESTIONS,
    MAX_UNIQUE_TRIAL_BUDGET,
    MAX_VALUES_PER_DIMENSION,
    OptunaTPEParameterSuggester,
    ParameterSuggester,
    SeededRandomParameterSuggester,
    Suggestion,
    SuggestionClassification,
    SuggestionDisposition,
    SuggesterHistoryLeakageError,
    SuggesterValidationError,
    optuna_tpe_frozen_identity,
)


def _frozen_plan() -> dict:
    return {
        "schema_version": 1,
        "round_identity": "study-001/search-round-001",
        "template": {
            "name": "synthetic_daily",
            "version": "1",
            "content_digest": "a" * 64,
            "parameters": {
                "evaluation_start": "2026-01-05",
                "evaluation_end": "2026-01-30",
                "initial_capital_cny": 100_000.0,
                "initial_state": "flat",
                "terminal_handling": "mark_to_market",
            },
        },
        "operators": {
            "decision": {
                "operator_id": "threshold",
                "slot": "decision",
                "resolved_version": "1.0.0",
                "content_digest": "d" * 64,
                "parameter_schema": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "mode": {"type": "string", "enum": ["cross", "level"]},
                        "threshold": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["enabled", "mode", "threshold"],
                    "additionalProperties": False,
                },
                "defaults": {
                    "enabled": True,
                    "mode": "cross",
                    "threshold": 0.2,
                },
                "parameters": {
                    "enabled": True,
                    "mode": "cross",
                    "threshold": 0.2,
                },
            },
            "fit": {
                "operator_id": "rolling_mean",
                "slot": "fit",
                "resolved_version": "1.0.0",
                "content_digest": "f" * 64,
                "parameter_schema": {
                    "type": "object",
                    "properties": {
                        "window": {
                            "type": "integer",
                            "minimum": 2,
                            "maximum": 5,
                        }
                    },
                    "required": ["window"],
                    "additionalProperties": False,
                },
                "defaults": {"window": 3},
                "parameters": {"window": 3},
            },
        },
        "search": {
            "suggester": "GRID",
            "suggester_version": "1.0.0",
            "seed": 17,
            "unique_trial_budget": 8,
            "max_suggestions": 12,
            "space": {
                "/operators/decision/threshold": {"values": [0.1, 0.2, 0.3]},
                "/operators/fit/window": {"values": [2, 3]},
            },
            "candidate_capacity": 6,
        },
    }


def _plan_with_integer_dimensions(sizes: list[int]) -> dict:
    plan = deepcopy(_frozen_plan())
    operator = plan["operators"]["decision"]
    properties = {
        f"p{index}": {
            "type": "integer",
            "minimum": 0,
            "maximum": size - 1,
        }
        for index, size in enumerate(sizes)
    }
    parameters = {name: 0 for name in properties}
    operator["parameter_schema"] = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    operator["defaults"] = deepcopy(parameters)
    operator["parameters"] = deepcopy(parameters)
    plan["operators"] = {"decision": operator}
    plan["search"]["space"] = {
        f"/operators/decision/p{index}": {"values": list(range(size))}
        for index, size in enumerate(sizes)
    }
    plan["search"]["candidate_capacity"] = prod(sizes)
    return plan


def _optuna_plan() -> dict:
    plan = deepcopy(_frozen_plan())
    decision = plan["operators"]["decision"]
    decision["parameter_schema"]["properties"]["mode"]["enum"] = [
        "cross",
        "level",
        "breakout",
    ]
    plan["search"].update(
        suggester="OPTUNA_TPE",
        adapter_identity=optuna_tpe_frozen_identity(),
        unique_trial_budget=8,
        max_suggestions=12,
        space={
            "/operators/decision/mode": {
                "type": "categorical",
                "choices": ["cross", "level", "breakout"],
            },
            "/operators/decision/threshold": {
                "type": "float",
                "low": 0.1,
                "high": 0.9,
                "step": None,
                "log": False,
            },
            "/operators/fit/window": {
                "type": "int",
                "low": 2,
                "high": 5,
                "step": 1,
                "log": False,
            },
        },
    )
    plan["search"].pop("candidate_capacity")
    return plan


def _inner_evaluation(
    suggestion: Suggestion,
    *,
    score: float | None = None,
    status: str = "COMPLETED",
    round_identity: str = "study-001/search-round-001",
) -> dict:
    evaluation: dict[str, Any] = {"status": status}
    if score is not None:
        evaluation["validation_score"] = score
    return {
        "event_type": "INNER_EVALUATION_RECORDED",
        "round_identity": round_identity,
        "role": "INNER_SCORE",
        "candidate_digest": suggestion.candidate_digest,
        "evaluation": evaluation,
    }


def _append_optuna_result(
    plan: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    score: float,
) -> Suggestion:
    suggestion = OptunaTPEParameterSuggester().next_suggestion(plan, history)
    assert isinstance(suggestion, Suggestion)
    history.append(suggestion.as_history_event())
    if suggestion.creates_trial:
        history.append(_inner_evaluation(suggestion, score=score))
    return suggestion


def test_optuna_tpe_baseline_is_first_even_outside_search_distributions():
    plan = _optuna_plan()
    plan["search"]["space"]["/operators/decision/mode"]["choices"] = [
        "level",
        "breakout",
    ]
    plan["search"]["space"]["/operators/decision/threshold"]["low"] = 0.3
    plan["search"]["space"]["/operators/fit/window"]["low"] = 4
    suggester = OptunaTPEParameterSuggester()

    baseline = suggester.next_suggestion(plan, [])

    assert isinstance(suggester, ParameterSuggester)
    assert isinstance(baseline, Suggestion)
    assert not hasattr(suggester, "__dict__")
    assert baseline.proposal_sequence == 0
    assert baseline.candidate["operators"]["decision"]["parameters"] == {
        "enabled": True,
        "mode": "cross",
        "threshold": 0.2,
    }
    assert baseline.candidate["operators"]["fit"]["parameters"] == {"window": 3}
    assert baseline.classification is SuggestionClassification.BASELINE_ONLY

    history = [baseline.as_history_event(), _inner_evaluation(baseline, score=0.125)]
    first_in_range = suggester.next_suggestion(plan, history)
    assert isinstance(first_in_range, Suggestion)
    assert first_in_range.classification is SuggestionClassification.IN_RANGE


def test_optuna_tpe_uses_explicit_typed_distributions_and_frozen_sampler_settings(
    monkeypatch,
):
    plan = _optuna_plan()
    observed_distributions = []
    original_ask = study_suggesters.optuna.study.Study.ask

    def recording_ask(study, fixed_distributions=None):
        if fixed_distributions is not None:
            observed_distributions.append(fixed_distributions)
        return original_ask(study, fixed_distributions=fixed_distributions)

    monkeypatch.setattr(study_suggesters.optuna.study.Study, "ask", recording_ask)

    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always", FutureWarning)
        baseline = OptunaTPEParameterSuggester().next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    assert not [warning for warning in emitted if issubclass(warning.category, FutureWarning)]

    distributions = observed_distributions[-1]
    assert isinstance(
        distributions["/operators/decision/mode"],
        CategoricalDistribution,
    )
    assert isinstance(
        distributions["/operators/decision/threshold"],
        FloatDistribution,
    )
    assert isinstance(distributions["/operators/fit/window"], IntDistribution)
    assert distributions["/operators/decision/mode"].choices == (
        "cross",
        "level",
        "breakout",
    )
    assert distributions["/operators/decision/threshold"].step is None
    assert distributions["/operators/fit/window"].step == 1

    identity = optuna_tpe_frozen_identity()
    assert identity["sampler_settings"] == {
        "consider_prior": None,
        "prior_weight": None,
        "consider_magic_clip": None,
        "consider_endpoints": None,
        "n_startup_trials": 5,
        "n_ei_candidates": 24,
        "gamma": None,
        "weights": None,
        "multivariate": False,
        "group": False,
        "warn_independent_sampling": None,
        "constant_liar": False,
        "constraints_func": None,
        "categorical_distance_func": None,
    }


def test_optuna_tpe_uses_deterministic_study_name_without_info_noise(
    capsys,
    monkeypatch,
):
    plan = _optuna_plan()
    observed_names = []
    original_create_study = study_suggesters.optuna.create_study

    def recording_create_study(*args, **kwargs):
        observed_names.append(kwargs.get("study_name"))
        return original_create_study(*args, **kwargs)

    monkeypatch.setattr(study_suggesters.optuna, "create_study", recording_create_study)

    OptunaTPEParameterSuggester().next_suggestion(plan, [])
    OptunaTPEParameterSuggester().next_suggestion(plan, [])

    expected_name = (
        "quant-platform-optuna-tpe-"
        + hashlib.sha256(plan["round_identity"].encode("utf-8")).hexdigest()
    )
    assert observed_names == [expected_name, expected_name]
    assert "[I " not in capsys.readouterr().err


def test_optuna_tpe_replays_successes_and_restarts_at_exact_next_candidate():
    plan = _optuna_plan()
    suggester = OptunaTPEParameterSuggester()
    baseline = suggester.next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)

    with pytest.raises(SuggesterValidationError, match="terminal inner evaluation"):
        suggester.next_suggestion(plan, [baseline.as_history_event()])

    history: list[dict[str, Any]] = []
    completed = [
        _append_optuna_result(plan, history, score=score)
        for score in (0.125, -0.25, 0.375, 0.5, -0.625, 0.75)
    ]
    uninterrupted = suggester.next_suggestion(plan, history)
    restarted = OptunaTPEParameterSuggester().next_suggestion(plan, deepcopy(history))

    assert all(suggestion.disposition is SuggestionDisposition.UNIQUE for suggestion in completed)
    assert isinstance(uninterrupted, Suggestion)
    assert isinstance(restarted, Suggestion)
    assert restarted.as_history_event() == uninterrupted.as_history_event()
    assert restarted.proposal_sequence == 6
    assert (
        restarted.candidate_digest
        == "e8974216bd5cc148dc41a11904054dafdd99c6664ada242fd9a24bb005260826"
    )
    parameters = restarted.candidate["operators"]
    assert parameters["decision"]["parameters"]["mode"] == "breakout"
    assert parameters["decision"]["parameters"]["threshold"] == 0.8932884654599286
    assert parameters["fit"]["parameters"]["window"] == 5


def test_optuna_tpe_failed_tell_has_no_score_and_continues_deterministically(
    monkeypatch,
):
    plan = _optuna_plan()
    baseline = OptunaTPEParameterSuggester().next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    observed_tells = []
    original_tell = study_suggesters.optuna.study.Study.tell

    def recording_tell(study, trial, values=None, state=None, skip_if_finished=False):
        observed_tells.append((trial.number, values, state))
        return original_tell(
            study,
            trial,
            values,
            state=state,
            skip_if_finished=skip_if_finished,
        )

    monkeypatch.setattr(study_suggesters.optuna.study.Study, "tell", recording_tell)
    history = [
        baseline.as_history_event(),
        _inner_evaluation(baseline, status="FAILED"),
    ]

    continued = OptunaTPEParameterSuggester().next_suggestion(plan, history)
    restarted = OptunaTPEParameterSuggester().next_suggestion(plan, history)

    assert isinstance(continued, Suggestion)
    assert isinstance(restarted, Suggestion)
    assert continued.as_history_event() == restarted.as_history_event()
    assert observed_tells
    assert all(values is None and state is TrialState.FAIL for _, values, state in observed_tells)

    fake_score = _inner_evaluation(baseline, status="FAILED", score=0.0)
    with pytest.raises(SuggesterValidationError, match="fabricated score"):
        OptunaTPEParameterSuggester().next_suggestion(
            plan,
            [baseline.as_history_event(), fake_score],
        )


def test_optuna_tpe_duplicate_is_audited_without_another_platform_evaluation():
    plan = _optuna_plan()
    plan["search"]["space"] = {
        "/operators/decision/mode": {
            "type": "categorical",
            "choices": ["cross", "level", "breakout"],
        }
    }
    plan["search"]["unique_trial_budget"] = 3
    history: list[dict[str, Any]] = []

    baseline = _append_optuna_result(plan, history, score=0.0)
    first = _append_optuna_result(plan, history, score=1.0)
    duplicate = OptunaTPEParameterSuggester().next_suggestion(plan, history)

    assert baseline.candidate["operators"]["decision"]["parameters"]["mode"] == "cross"
    assert first.candidate["operators"]["decision"]["parameters"]["mode"] == "level"
    assert isinstance(duplicate, Suggestion)
    assert duplicate.proposal_sequence == 2
    assert duplicate.disposition is SuggestionDisposition.DUPLICATE
    assert duplicate.duplicate_of_sequence == 1
    assert duplicate.creates_trial is False
    assert duplicate.as_history_event()["event_type"] is HistoryEventType.DUPLICATE_SUGGESTION

    continued = OptunaTPEParameterSuggester().next_suggestion(
        plan,
        [*history, duplicate.as_history_event()],
    )
    assert isinstance(continued, Suggestion)
    assert continued.proposal_sequence == 3


@pytest.mark.parametrize(
    ("identity_path", "drifted_value"),
    [
        (("adapter_version",), "1.0.1"),
        (("library_version",), "4.9.1"),
        (("sampler_settings", "n_startup_trials"), 10),
        (("direction",), "MINIMIZE"),
        (("objective",), "outer_score"),
    ],
)
def test_optuna_tpe_rejects_adapter_identity_drift(identity_path, drifted_value):
    plan = _optuna_plan()
    target = plan["search"]["adapter_identity"]
    for key in identity_path[:-1]:
        target = target[key]
    target[identity_path[-1]] = drifted_value

    with pytest.raises(SuggesterValidationError, match="adapter_identity"):
        OptunaTPEParameterSuggester().next_suggestion(plan, [])


@pytest.mark.parametrize(
    "forbidden",
    [
        {
            "event_type": "OUTER_EVALUATION_RECORDED",
            "role": "OUTER_OOS",
            "candidate_digest": "unused",
            "evaluation": {"validation_score": 99.0},
        },
        {
            "event_type": "HOLDOUT_EVALUATION_RECORDED",
            "role": "TERMINAL_HOLDOUT",
            "candidate_digest": "unused",
            "evaluation": {"passed": True},
        },
        {
            "event_type": "INNER_EVALUATION_RECORDED",
            "round_identity": "study-001/search-round-001",
            "role": "OUTER_AUDIT",
            "candidate_digest": "replace",
            "evaluation": {"status": "COMPLETED", "validation_score": 99.0},
        },
    ],
)
def test_optuna_tpe_rejects_outer_and_holdout_history(forbidden):
    plan = _optuna_plan()
    baseline = OptunaTPEParameterSuggester().next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    forbidden = deepcopy(forbidden)
    if forbidden["candidate_digest"] == "replace":
        forbidden["candidate_digest"] = baseline.candidate_digest

    with pytest.raises(SuggesterHistoryLeakageError, match=forbidden["role"]):
        OptunaTPEParameterSuggester().next_suggestion(
            plan,
            [baseline.as_history_event(), forbidden],
        )


def test_optuna_tpe_rejects_cross_round_inner_history():
    plan = _optuna_plan()
    baseline = OptunaTPEParameterSuggester().next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)

    with pytest.raises(SuggesterHistoryLeakageError, match="same search round"):
        OptunaTPEParameterSuggester().next_suggestion(
            plan,
            [
                baseline.as_history_event(),
                _inner_evaluation(
                    baseline,
                    score=0.125,
                    round_identity="study-001/search-round-previous",
                ),
            ],
        )


def test_grid_proposes_canonical_defaults_as_sequence_zero():
    suggester = GridParameterSuggester()
    suggestion = suggester.next_suggestion(_frozen_plan(), [])

    assert isinstance(suggestion, Suggestion)
    assert not hasattr(suggester, "__dict__")
    assert suggestion.as_history_event() == {
        "event_type": "SUGGESTION_RECORDED",
        "proposal_sequence": 0,
        "candidate_digest": ("cc0dcf0d54c383ce86739e5266b394eddb567fc561b08f63ec7824e2942bc93e"),
        "candidate": {
            "schema_version": 1,
            "template": {
                "name": "synthetic_daily",
                "version": "1",
                "content_digest": "a" * 64,
                "parameters": {
                    "initial_capital_cny": 100_000.0,
                    "initial_state": "flat",
                    "terminal_handling": "mark_to_market",
                },
            },
            "operators": {
                "decision": {
                    "operator_id": "threshold",
                    "version": "1.0.0",
                    "content_digest": "d" * 64,
                    "parameters": {
                        "enabled": True,
                        "mode": "cross",
                        "threshold": 0.2,
                    },
                },
                "fit": {
                    "operator_id": "rolling_mean",
                    "version": "1.0.0",
                    "content_digest": "f" * 64,
                    "parameters": {"window": 3},
                },
            },
        },
        "classification": "IN_RANGE",
        "disposition": "UNIQUE",
        "duplicate_of_sequence": None,
    }
    assert suggestion.creates_trial is True
    assert suggestion.champion_eligible is True


def test_grid_uses_indexed_values_in_canonical_path_order():
    suggester = GridParameterSuggester()
    baseline = suggester.next_suggestion(_frozen_plan(), [])

    suggestion = suggester.next_suggestion(
        _frozen_plan(),
        [baseline.as_history_event()],
    )

    assert isinstance(suggestion, Suggestion)
    assert suggestion.proposal_sequence == 1
    assert suggestion.candidate["operators"]["decision"]["parameters"] == {
        "enabled": True,
        "mode": "cross",
        "threshold": 0.1,
    }
    assert suggestion.candidate["operators"]["fit"]["parameters"] == {"window": 2}
    assert (
        suggestion.candidate_digest
        == "f35b8c7f7cd753abeb050147b3abb9272e70c91e7c509bd4f3caa31e38d5bdde"
    )
    assert suggestion.classification == "IN_RANGE"
    assert suggestion.disposition == "UNIQUE"


def test_out_of_range_defaults_are_baseline_only_and_consume_unique_budget():
    plan = deepcopy(_frozen_plan())
    plan["operators"]["decision"]["defaults"]["threshold"] = 0.4
    plan["operators"]["decision"]["parameters"]["threshold"] = 0.4
    plan["search"]["unique_trial_budget"] = 1
    suggester = GridParameterSuggester()

    baseline = suggester.next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    assert baseline.proposal_sequence == 0
    assert baseline.classification == "BASELINE_ONLY"
    assert baseline.champion_eligible is False
    assert baseline.creates_trial is True

    exhausted = suggester.next_suggestion(plan, [baseline.as_history_event()])
    assert exhausted == Exhausted(
        reason="UNIQUE_TRIAL_BUDGET",
        raw_suggestion_count=1,
        unique_trial_count=1,
    )


def test_duplicate_grid_proposal_is_audited_without_creating_a_trial():
    plan = deepcopy(_frozen_plan())
    plan["search"]["unique_trial_budget"] = 5
    plan["search"]["max_suggestions"] = 5
    suggester = GridParameterSuggester()
    history = []
    for _ in range(4):
        suggestion = suggester.next_suggestion(plan, history)
        assert isinstance(suggestion, Suggestion)
        history.append(suggestion.as_history_event())

    duplicate = suggester.next_suggestion(plan, history)

    assert isinstance(duplicate, Suggestion)
    assert duplicate.proposal_sequence == 4
    assert (
        duplicate.candidate_digest
        == "cc0dcf0d54c383ce86739e5266b394eddb567fc561b08f63ec7824e2942bc93e"
    )
    assert duplicate.disposition == "DUPLICATE"
    assert duplicate.duplicate_of_sequence == 0
    assert duplicate.creates_trial is False
    assert duplicate.champion_eligible is False
    assert duplicate.as_history_event()["event_type"] == "DUPLICATE_SUGGESTION"

    exhausted = suggester.next_suggestion(
        plan,
        [*history, duplicate.as_history_event()],
    )
    assert exhausted == Exhausted(
        reason="RAW_SUGGESTION_BUDGET",
        raw_suggestion_count=5,
        unique_trial_count=4,
    )


def test_grid_restart_uses_only_frozen_plan_and_ordered_platform_history():
    first_process = GridParameterSuggester()
    baseline = first_process.next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    first_grid = first_process.next_suggestion(
        _frozen_plan(),
        [baseline.as_history_event()],
    )
    assert isinstance(first_grid, Suggestion)
    persisted_history = [
        baseline.as_history_event(),
        {
            "event_type": "INNER_EVALUATION_RECORDED",
            "role": "INNER_SCORE",
            "candidate_digest": baseline.candidate_digest,
            "evaluation": {
                "eligible": True,
                "validation_score": 0.125,
            },
        },
        first_grid.as_history_event(),
    ]

    after_restart = GridParameterSuggester().next_suggestion(
        _frozen_plan(),
        persisted_history,
    )

    assert isinstance(after_restart, Suggestion)
    assert after_restart.proposal_sequence == 2
    assert after_restart.candidate["operators"]["decision"]["parameters"]["threshold"] == 0.1
    assert after_restart.candidate["operators"]["fit"]["parameters"]["window"] == 3
    assert (
        after_restart.candidate_digest
        == "56cb9a84ed30d7883c054786f9c35bc8c87ebe6a54359d5438ddbf1c73498277"
    )


def test_suggester_history_rejects_outer_oos_and_holdout_evidence():
    baseline = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    forbidden_events = (
        {
            "event_type": "OUTER_EVALUATION_RECORDED",
            "role": "OUTER_AUDIT",
            "candidate_digest": baseline.candidate_digest,
            "evaluation": {"validation_score": 99.0},
        },
        {
            "event_type": "HOLDOUT_EVALUATION_RECORDED",
            "role": "TERMINAL_HOLDOUT",
            "candidate_digest": baseline.candidate_digest,
            "evaluation": {"passed": True},
        },
    )

    for forbidden in forbidden_events:
        with pytest.raises(
            SuggesterHistoryLeakageError,
            match=forbidden["role"],
        ):
            GridParameterSuggester().next_suggestion(
                _frozen_plan(),
                [baseline.as_history_event(), forbidden],
            )


def test_frozen_search_domains_require_operator_paths_exact_types_and_finite_capacity():
    invalid_path = deepcopy(_frozen_plan())
    definition = invalid_path["search"]["space"].pop("/operators/fit/window")
    invalid_path["search"]["space"]["/template/window"] = definition

    wrong_integer_type = deepcopy(_frozen_plan())
    wrong_integer_type["search"]["space"]["/operators/fit/window"]["values"] = [
        2.0,
        3,
    ]

    non_finite_number = deepcopy(_frozen_plan())
    non_finite_number["search"]["space"]["/operators/decision/threshold"]["values"] = [
        0.1,
        float("inf"),
    ]

    invalid_enum = deepcopy(_frozen_plan())
    invalid_enum["search"]["space"] = {"/operators/decision/mode": {"values": ["cross", "unknown"]}}
    invalid_enum["search"]["candidate_capacity"] = 2

    wrong_boolean_type = deepcopy(_frozen_plan())
    wrong_boolean_type["search"]["space"] = {"/operators/decision/enabled": {"values": [1, False]}}
    wrong_boolean_type["search"]["candidate_capacity"] = 2

    duplicate_after_normalization = deepcopy(_frozen_plan())
    duplicate_after_normalization["search"]["space"]["/operators/decision/threshold"]["values"] = [
        0.1,
        0.10,
    ]

    empty_domain = deepcopy(_frozen_plan())
    empty_domain["search"]["space"]["/operators/fit/window"]["values"] = []

    incorrect_capacity = deepcopy(_frozen_plan())
    incorrect_capacity["search"]["candidate_capacity"] = 7

    cases = (
        (invalid_path, "frozen operator parameter"),
        (wrong_integer_type, "must be an integer"),
        (non_finite_number, "finite"),
        (invalid_enum, "must be one of"),
        (wrong_boolean_type, "must be a boolean"),
        (duplicate_after_normalization, "must be unique"),
        (empty_domain, "non-empty array"),
        (incorrect_capacity, "candidate_capacity"),
    )
    for plan, message in cases:
        with pytest.raises(SuggesterValidationError, match=message):
            GridParameterSuggester().next_suggestion(plan, [])


def test_seeded_random_restarts_with_the_exact_same_proposal_and_duplicate_outcome():
    plan = deepcopy(_frozen_plan())
    plan["search"]["suggester"] = "SEEDED_RANDOM"
    plan["search"]["unique_trial_budget"] = 3
    plan["search"]["max_suggestions"] = 3
    first_process = SeededRandomParameterSuggester()
    assert isinstance(first_process, ParameterSuggester)

    baseline = first_process.next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    assert not hasattr(first_process, "__dict__")
    assert baseline.proposal_sequence == 0
    assert (
        baseline.candidate_digest
        == "cc0dcf0d54c383ce86739e5266b394eddb567fc561b08f63ec7824e2942bc93e"
    )
    first_random = first_process.next_suggestion(
        plan,
        [baseline.as_history_event()],
    )
    assert isinstance(first_random, Suggestion)
    assert first_random.proposal_sequence == 1
    assert first_random.candidate["operators"]["decision"]["parameters"]["threshold"] == 0.2
    assert first_random.candidate["operators"]["fit"]["parameters"]["window"] == 3
    assert (
        first_random.candidate_digest
        == "cc0dcf0d54c383ce86739e5266b394eddb567fc561b08f63ec7824e2942bc93e"
    )
    assert first_random.disposition == "DUPLICATE"
    assert first_random.duplicate_of_sequence == 0

    persisted_history = [
        baseline.as_history_event(),
        first_random.as_history_event(),
        {
            "event_type": "INNER_EVALUATION_RECORDED",
            "role": "INNER_SCORE",
            "candidate_digest": first_random.candidate_digest,
            "evaluation": {"eligible": True, "validation_score": -0.25},
        },
    ]
    after_restart = SeededRandomParameterSuggester().next_suggestion(
        plan,
        persisted_history,
    )

    assert isinstance(after_restart, Suggestion)
    assert after_restart.proposal_sequence == 2
    assert (
        after_restart.candidate_digest
        == "4c68db3f6a89ebc1a641a1f35bbab757e43fba0534093521ef68e9e2a9b638af"
    )
    assert after_restart.disposition == "UNIQUE"
    assert after_restart.duplicate_of_sequence is None
    assert after_restart.as_history_event()["event_type"] == "SUGGESTION_RECORDED"
    assert after_restart.creates_trial is True

    exhausted = SeededRandomParameterSuggester().next_suggestion(
        plan,
        [*persisted_history, after_restart.as_history_event()],
    )
    assert exhausted == Exhausted(
        reason="RAW_SUGGESTION_BUDGET",
        raw_suggestion_count=3,
        unique_trial_count=2,
    )


def test_seeded_random_terminates_when_every_finite_candidate_is_already_a_trial():
    plan = deepcopy(_frozen_plan())
    plan["search"]["suggester"] = "SEEDED_RANDOM"
    plan["search"]["space"] = {
        "/operators/decision/threshold": {"values": [0.2]},
        "/operators/fit/window": {"values": [3]},
    }
    plan["search"]["candidate_capacity"] = 1
    plan["search"]["unique_trial_budget"] = 4
    plan["search"]["max_suggestions"] = 9
    suggester = SeededRandomParameterSuggester()

    baseline = suggester.next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    assert baseline.classification == "IN_RANGE"

    exhausted = suggester.next_suggestion(plan, [baseline.as_history_event()])
    assert exhausted == Exhausted(
        reason="SEARCH_SPACE_EXHAUSTED",
        raw_suggestion_count=1,
        unique_trial_count=1,
    )


def test_grid_decimal_indexes_are_exact_through_finite_space_exhaustion():
    suggester = GridParameterSuggester()
    history = []
    proposals = []
    while True:
        outcome = suggester.next_suggestion(_frozen_plan(), history)
        if isinstance(outcome, Exhausted):
            break
        proposals.append(
            (
                outcome.proposal_sequence,
                outcome.candidate["operators"]["decision"]["parameters"]["threshold"],
                outcome.candidate["operators"]["fit"]["parameters"]["window"],
                outcome.disposition,
            )
        )
        history.append(outcome.as_history_event())

    assert proposals == [
        (0, 0.2, 3, "UNIQUE"),
        (1, 0.1, 2, "UNIQUE"),
        (2, 0.1, 3, "UNIQUE"),
        (3, 0.2, 2, "UNIQUE"),
        (4, 0.2, 3, "DUPLICATE"),
        (5, 0.3, 2, "UNIQUE"),
        (6, 0.3, 3, "UNIQUE"),
    ]
    assert outcome == Exhausted(
        reason="SEARCH_SPACE_EXHAUSTED",
        raw_suggestion_count=7,
        unique_trial_count=6,
    )


def test_nullable_numeric_domains_preserve_null_and_number_canonical_types():
    plan = deepcopy(_frozen_plan())
    threshold_schema = plan["operators"]["decision"]["parameter_schema"]["properties"]["threshold"]
    threshold_schema["nullable"] = True
    plan["operators"]["decision"]["defaults"]["threshold"] = None
    plan["operators"]["decision"]["parameters"]["threshold"] = None
    plan["search"]["space"]["/operators/decision/threshold"]["values"] = [None, 0.1]
    plan["search"]["candidate_capacity"] = 4
    suggester = GridParameterSuggester()

    baseline = suggester.next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    first_grid = suggester.next_suggestion(plan, [baseline.as_history_event()])
    assert isinstance(first_grid, Suggestion)
    persisted_history = [baseline.as_history_event(), first_grid.as_history_event()]

    after_restart = GridParameterSuggester().next_suggestion(plan, persisted_history)
    assert isinstance(after_restart, Suggestion)
    assert after_restart.candidate["operators"]["decision"]["parameters"]["threshold"] is None
    assert (
        canonical_json_bytes(
            after_restart.candidate["operators"]["decision"]["parameters"]["threshold"]
        )
        == b"null"
    )
    assert (
        GridParameterSuggester().next_suggestion(plan, persisted_history).as_history_event()
        == after_restart.as_history_event()
    )
    numeric = GridParameterSuggester().next_suggestion(
        plan,
        [*persisted_history, after_restart.as_history_event()],
    )
    assert isinstance(numeric, Suggestion)
    numeric_threshold = numeric.candidate["operators"]["decision"]["parameters"]["threshold"]
    assert type(numeric_threshold) is float
    assert canonical_json_bytes(numeric_threshold) == b"0.1"


def test_canonical_json_inputs_reject_non_json_types_and_non_string_keys():
    string_key_plan = deepcopy(_frozen_plan())
    string_key_plan["template"]["parameters"]["metadata"] = {"1": "x"}
    accepted = GridParameterSuggester().next_suggestion(string_key_plan, [])
    assert isinstance(accepted, Suggestion)

    invalid_plans = []
    non_string_template_key = deepcopy(_frozen_plan())
    non_string_template_key["template"]["parameters"]["metadata"] = {1: "x"}
    invalid_plans.append(non_string_template_key)

    non_string_operator_key = deepcopy(_frozen_plan())
    non_string_operator_key["operators"]["decision"]["parameters"][1] = "x"
    invalid_plans.append(non_string_operator_key)

    non_json_operator_value = deepcopy(_frozen_plan())
    non_json_operator_value["operators"]["decision"]["parameters"]["threshold"] = (0.2,)
    invalid_plans.append(non_json_operator_value)

    non_string_search_key = deepcopy(_frozen_plan())
    non_string_search_key["search"]["space"]["/operators/decision/threshold"][1] = "x"
    invalid_plans.append(non_string_search_key)

    non_json_search_value = deepcopy(_frozen_plan())
    non_json_search_value["search"]["space"]["/operators/decision/threshold"]["values"] = [{0.1}]
    invalid_plans.append(non_json_search_value)

    for invalid_plan in invalid_plans:
        with pytest.raises(SuggesterValidationError, match="non-(JSON|string)"):
            GridParameterSuggester().next_suggestion(invalid_plan, [])

    baseline = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    valid_evaluation = {
        "event_type": "INNER_EVALUATION_RECORDED",
        "role": "INNER_SCORE",
        "candidate_digest": baseline.candidate_digest,
        "evaluation": {"1": "x"},
    }
    assert isinstance(
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), valid_evaluation],
        ),
        Suggestion,
    )
    invalid_evaluation = deepcopy(valid_evaluation)
    invalid_evaluation["evaluation"] = {1: "x"}
    with pytest.raises(SuggesterValidationError, match="non-string object key"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), invalid_evaluation],
        )


def test_operational_bounds_accept_limits_and_reject_the_next_value():
    assert (
        MAX_SEARCH_DIMENSIONS,
        MAX_VALUES_PER_DIMENSION,
        MAX_CANDIDATE_CAPACITY,
        MAX_UNIQUE_TRIAL_BUDGET,
        MAX_SUGGESTIONS,
        MAX_ORDERED_HISTORY_LENGTH,
    ) == (12, 128, 16_384, 256, 1_024, 2_048)

    boundary_plans = [
        _plan_with_integer_dimensions([1] * MAX_SEARCH_DIMENSIONS),
        _plan_with_integer_dimensions([MAX_VALUES_PER_DIMENSION]),
        _plan_with_integer_dimensions([MAX_VALUES_PER_DIMENSION, MAX_VALUES_PER_DIMENSION]),
    ]
    budget_boundary = deepcopy(_frozen_plan())
    budget_boundary["search"]["unique_trial_budget"] = MAX_UNIQUE_TRIAL_BUDGET
    budget_boundary["search"]["max_suggestions"] = MAX_SUGGESTIONS
    boundary_plans.append(budget_boundary)
    for plan in boundary_plans:
        assert isinstance(GridParameterSuggester().next_suggestion(plan, []), Suggestion)

    over_limit_plans = [
        _plan_with_integer_dimensions([1] * (MAX_SEARCH_DIMENSIONS + 1)),
        _plan_with_integer_dimensions([MAX_VALUES_PER_DIMENSION + 1]),
        _plan_with_integer_dimensions([MAX_VALUES_PER_DIMENSION, MAX_VALUES_PER_DIMENSION, 2]),
    ]
    declared_capacity_too_large = deepcopy(_frozen_plan())
    declared_capacity_too_large["search"]["candidate_capacity"] = MAX_CANDIDATE_CAPACITY + 1
    over_limit_plans.append(declared_capacity_too_large)
    unique_budget_too_large = deepcopy(_frozen_plan())
    unique_budget_too_large["search"]["unique_trial_budget"] = MAX_UNIQUE_TRIAL_BUDGET + 1
    over_limit_plans.append(unique_budget_too_large)
    suggestion_budget_too_large = deepcopy(_frozen_plan())
    suggestion_budget_too_large["search"]["max_suggestions"] = MAX_SUGGESTIONS + 1
    over_limit_plans.append(suggestion_budget_too_large)
    for plan in over_limit_plans:
        with pytest.raises(SuggesterValidationError):
            GridParameterSuggester().next_suggestion(plan, [])

    inverted_budgets = deepcopy(_frozen_plan())
    inverted_budgets["search"]["max_suggestions"] = (
        inverted_budgets["search"]["unique_trial_budget"] - 1
    )
    with pytest.raises(
        SuggesterValidationError,
        match="greater than or equal to unique_trial_budget",
    ):
        GridParameterSuggester().next_suggestion(inverted_budgets, [])

    baseline = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    evaluation = {
        "event_type": "INNER_EVALUATION_RECORDED",
        "role": "INNER_SCORE",
        "candidate_digest": baseline.candidate_digest,
        "evaluation": {},
    }
    boundary_history = [
        baseline.as_history_event(),
        *([evaluation] * (MAX_ORDERED_HISTORY_LENGTH - 1)),
    ]
    assert isinstance(
        GridParameterSuggester().next_suggestion(_frozen_plan(), boundary_history),
        Suggestion,
    )
    with pytest.raises(SuggesterValidationError, match="ordered_history"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [*boundary_history, evaluation],
        )


def test_seeded_random_is_round_scoped_and_restart_equivalent():
    plan = deepcopy(_frozen_plan())
    plan["search"]["suggester"] = "SEEDED_RANDOM"
    suggester = SeededRandomParameterSuggester()
    baseline = suggester.next_suggestion(plan, [])
    assert isinstance(baseline, Suggestion)
    first = suggester.next_suggestion(plan, [baseline.as_history_event()])
    assert isinstance(first, Suggestion)
    history = [baseline.as_history_event(), first.as_history_event()]

    uninterrupted = suggester.next_suggestion(plan, history)
    restarted = SeededRandomParameterSuggester().next_suggestion(plan, history)
    assert isinstance(uninterrupted, Suggestion)
    assert isinstance(restarted, Suggestion)
    assert restarted.as_history_event() == uninterrupted.as_history_event()

    distinct_round = deepcopy(plan)
    distinct_round["round_identity"] = "study-001/search-round-002"
    distinct_baseline = SeededRandomParameterSuggester().next_suggestion(
        distinct_round,
        [],
    )
    assert isinstance(distinct_baseline, Suggestion)
    distinct_first = SeededRandomParameterSuggester().next_suggestion(
        distinct_round,
        [distinct_baseline.as_history_event()],
    )
    assert isinstance(distinct_first, Suggestion)
    assert distinct_first.candidate_digest != first.candidate_digest

    missing_identity = deepcopy(plan)
    del missing_identity["round_identity"]
    with pytest.raises(SuggesterValidationError, match="round_identity"):
        SeededRandomParameterSuggester().next_suggestion(missing_identity, [])
    mutable_identity = deepcopy(plan)
    mutable_identity["round_identity"] = {
        "study_id": "study-001",
        "search_round_id": "search-round-001",
    }
    with pytest.raises(SuggesterValidationError, match="round_identity"):
        SeededRandomParameterSuggester().next_suggestion(mutable_identity, [])


def test_suggestion_candidate_is_defensive_and_digest_bound():
    suggestion = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(suggestion, Suggestion)
    candidate = suggestion.candidate
    candidate["operators"]["decision"]["parameters"]["threshold"] = 0.9
    event = suggestion.as_history_event()
    event["candidate"]["operators"]["fit"]["parameters"]["window"] = 5

    pristine = suggestion.candidate
    assert pristine["operators"]["decision"]["parameters"]["threshold"] == 0.2
    assert pristine["operators"]["fit"]["parameters"]["window"] == 3
    assert hashlib.sha256(canonical_json_bytes(pristine)).hexdigest() == suggestion.candidate_digest


def test_suggestion_and_exhaustion_domain_types_enforce_cross_field_invariants():
    baseline = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    assert baseline.classification is SuggestionClassification.IN_RANGE
    assert baseline.disposition is SuggestionDisposition.UNIQUE
    assert baseline.as_history_event()["event_type"] is HistoryEventType.SUGGESTION_RECORDED
    assert (
        Exhausted("SEARCH_SPACE_EXHAUSTED", 1, 1).reason is ExhaustionReason.SEARCH_SPACE_EXHAUSTED
    )

    invalid_suggestions = (
        {
            "proposal_sequence": 0,
            "classification": "UNKNOWN",
            "disposition": "UNIQUE",
            "duplicate_of_sequence": None,
        },
        {
            "proposal_sequence": 1,
            "classification": "IN_RANGE",
            "disposition": "DUPLICATE",
            "duplicate_of_sequence": None,
        },
        {
            "proposal_sequence": 1,
            "classification": "IN_RANGE",
            "disposition": "UNIQUE",
            "duplicate_of_sequence": 0,
        },
    )
    for fields in invalid_suggestions:
        with pytest.raises(SuggesterValidationError):
            Suggestion(
                candidate_digest=baseline.candidate_digest,
                candidate=baseline.candidate,
                **fields,
            )

    with pytest.raises(SuggesterValidationError, match="reason"):
        Exhausted("UNKNOWN", 1, 1)
    with pytest.raises(SuggesterValidationError, match="cannot exceed"):
        Exhausted("SEARCH_SPACE_EXHAUSTED", 1, 2)

    mismatched_event = baseline.as_history_event()
    mismatched_event.update(
        proposal_sequence=1,
        disposition="DUPLICATE",
        duplicate_of_sequence=0,
    )
    with pytest.raises(SuggesterValidationError, match="event_type"):
        GridParameterSuggester().next_suggestion(_frozen_plan(), [mismatched_event])


def test_exact_dedupe_rejects_digest_collision_with_different_candidate_bytes(monkeypatch):
    monkeypatch.setattr(study_suggesters, "_candidate_digest", lambda candidate_bytes: "0" * 64)
    suggester = GridParameterSuggester()
    baseline = suggester.next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)

    with pytest.raises(SuggesterValidationError, match="digest collision"):
        suggester.next_suggestion(_frozen_plan(), [baseline.as_history_event()])


def test_history_json_validation_is_iterative_and_bounded():
    baseline = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    evaluation = {
        "event_type": "INNER_EVALUATION_RECORDED",
        "role": "INNER_SCORE",
        "candidate_digest": baseline.candidate_digest,
        "evaluation": {},
    }

    deeply_nested: object = True
    for _ in range(MAX_JSON_NESTING_DEPTH + 1):
        deeply_nested = {"nested": deeply_nested}
    deep_event = deepcopy(evaluation)
    deep_event["evaluation"] = deeply_nested
    with pytest.raises(SuggesterValidationError, match="nesting depth"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), deep_event],
        )

    oversized_string = deepcopy(evaluation)
    oversized_string["evaluation"] = "x" * (MAX_JSON_STRING_BYTES + 1)
    with pytest.raises(SuggesterValidationError, match="string size"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), oversized_string],
        )

    oversized_container = deepcopy(evaluation)
    oversized_container["evaluation"] = [None] * (MAX_JSON_CONTAINER_SIZE + 1)
    with pytest.raises(SuggesterValidationError, match="container size"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), oversized_container],
        )

    oversized_candidate = deepcopy(_frozen_plan())
    oversized_candidate["template"]["parameters"]["metadata"] = ["x" * MAX_JSON_STRING_BYTES] * (
        MAX_CANDIDATE_CANONICAL_BYTES // MAX_JSON_STRING_BYTES + 1
    )
    with pytest.raises(SuggesterValidationError, match="canonical size"):
        GridParameterSuggester().next_suggestion(oversized_candidate, [])


@pytest.mark.parametrize(
    "evaluation",
    [
        {"value": "\ud800"},
        {"\ud800": "value"},
    ],
    ids=["string-value", "object-key"],
)
def test_history_json_validation_rejects_unpaired_unicode_surrogates(evaluation):
    baseline = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    event = {
        "event_type": "INNER_EVALUATION_RECORDED",
        "role": "INNER_SCORE",
        "candidate_digest": baseline.candidate_digest,
        "evaluation": evaluation,
    }

    with pytest.raises(SuggesterValidationError, match="canonical JSON"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), event],
        )


def test_history_validates_operator_keys_event_types_and_roles_before_use():
    mixed_operator_keys = deepcopy(_frozen_plan())
    mixed_operator_keys["operators"][1] = mixed_operator_keys["operators"]["fit"]
    with pytest.raises(SuggesterValidationError, match="frozen_plan.operators.*non-string"):
        GridParameterSuggester().next_suggestion(mixed_operator_keys, [])

    baseline = GridParameterSuggester().next_suggestion(_frozen_plan(), [])
    assert isinstance(baseline, Suggestion)
    event = {
        "event_type": "INNER_EVALUATION_RECORDED",
        "role": ["INNER_SCORE"],
        "candidate_digest": baseline.candidate_digest,
        "evaluation": {},
    }
    with pytest.raises(SuggesterValidationError, match="role must be a string"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), event],
        )

    event["event_type"] = {"INNER_EVALUATION_RECORDED": True}
    event["role"] = EvaluationRole.INNER_SCORE
    with pytest.raises(SuggesterValidationError, match="event_type must be a string"):
        GridParameterSuggester().next_suggestion(
            _frozen_plan(),
            [baseline.as_history_event(), event],
        )
