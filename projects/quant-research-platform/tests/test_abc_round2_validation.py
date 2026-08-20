import numpy as np
import pandas as pd
import pytest

from gold_research.abc_round2_validation import (
    constant_exposure_signal,
    circular_shift_timing_test,
    candidate_gate_decision,
    non_overlapping_four_year_blocks,
    risk_utility_pass,
    subperiod_stability,
)


def test_constant_exposure_signal_matches_scored_mean_and_preserves_index():
    index = pd.bdate_range("2024-01-02", periods=4)
    candidate = pd.Series([0.0, 1.0, 0.5, 0.5], index=index, name="candidate")

    result = constant_exposure_signal(candidate)

    pd.testing.assert_index_equal(result.index, index)
    assert result.name == "constant_exposure_matched"
    assert result.tolist() == [0.5, 0.5, 0.5, 0.5]


@pytest.mark.parametrize(
    "values",
    ([0.0, np.nan], [0.0, np.inf], [-0.01, 0.5], [0.5, 1.01]),
)
def test_constant_exposure_signal_rejects_invalid_values(values):
    signal = pd.Series(values, index=pd.bdate_range("2024-01-02", periods=len(values)))

    with pytest.raises(ValueError, match="finite.*between zero and one"):
        constant_exposure_signal(signal)


def test_four_year_blocks_use_exact_calendar_boundaries_and_drop_trailing_block():
    index = pd.bdate_range("2011-08-15", "2026-08-20")

    blocks = non_overlapping_four_year_blocks(index)

    assert blocks == [
        (pd.Timestamp("2012-01-01"), pd.Timestamp("2015-12-31")),
        (pd.Timestamp("2016-01-01"), pd.Timestamp("2019-12-31")),
        (pd.Timestamp("2020-01-01"), pd.Timestamp("2023-12-31")),
    ]


def test_four_year_blocks_include_a_final_block_ending_with_the_calendar_year():
    index = pd.bdate_range("2012-01-02", "2023-12-29")

    blocks = non_overlapping_four_year_blocks(index)

    assert blocks[-1] == (pd.Timestamp("2020-01-01"), pd.Timestamp("2023-12-31"))
    assert len(blocks) == 3


def test_four_year_blocks_exclude_a_final_block_before_the_last_business_day():
    index = pd.bdate_range("2012-01-02", "2023-12-28")

    blocks = non_overlapping_four_year_blocks(index)

    assert blocks[-1] == (pd.Timestamp("2016-01-01"), pd.Timestamp("2019-12-31"))
    assert len(blocks) == 2


@pytest.mark.parametrize(
    "index",
    [
        pd.Index([1, 2]),
        pd.DatetimeIndex(["2024-01-02", "2024-01-02"]),
        pd.DatetimeIndex(["2024-01-03", "2024-01-02"]),
    ],
)
def test_four_year_blocks_require_a_unique_sorted_datetime_index(index):
    with pytest.raises(ValueError, match="DatetimeIndex.*unique.*sorted"):
        non_overlapping_four_year_blocks(index)


def _result_from_returns(returns: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"net_return": returns}, index=returns.index)


def test_subperiod_stability_reports_compounded_relative_log_rows_and_passes():
    index = pd.bdate_range("2011-07-01", "2023-12-29")
    constant_returns = pd.Series(0.0001, index=index)
    candidate_returns = constant_returns.copy()
    candidate_returns.loc[candidate_returns.index.year <= 2018] = 0.0002
    candidate_returns.loc[candidate_returns.index.year >= 2019] = 0.0

    summary = subperiod_stability(
        _result_from_returns(candidate_returns),
        _result_from_returns(constant_returns),
    )

    assert summary["status"] == "PASS"
    assert summary["complete_blocks"] == 3
    assert summary["positive_relative_log_blocks"] == 2
    assert summary["positive_fraction"] == pytest.approx(2 / 3)
    assert [row["pass"] for row in summary["blocks"]] == [True, True, False]
    first = summary["blocks"][0]
    candidate_factor = (1.0 + candidate_returns.loc["2012":"2015"]).prod()
    constant_factor = (1.0 + constant_returns.loc["2012":"2015"]).prod()
    assert first["candidate_compounded_return"] == pytest.approx(candidate_factor - 1.0)
    assert first["constant_compounded_return"] == pytest.approx(constant_factor - 1.0)
    assert first["relative_log_return"] == pytest.approx(
        np.log(candidate_factor) - np.log(constant_factor)
    )


def test_subperiod_stability_is_insufficient_with_fewer_than_three_complete_blocks():
    index = pd.bdate_range("2017-02-01", "2026-08-20")
    candidate = _result_from_returns(pd.Series(0.001, index=index))
    constant = _result_from_returns(pd.Series(0.0, index=index))

    summary = subperiod_stability(candidate, constant)

    assert summary["complete_blocks"] == 2
    assert summary["status"] == "INSUFFICIENT"


def test_subperiod_stability_fails_when_fewer_than_sixty_percent_are_positive():
    index = pd.bdate_range("2008-01-02", "2019-12-31")
    constant_returns = pd.Series(0.0, index=index)
    candidate_returns = pd.Series(0.001, index=index)
    candidate_returns.loc[candidate_returns.index.year >= 2012] = -0.001

    summary = subperiod_stability(
        _result_from_returns(candidate_returns),
        _result_from_returns(constant_returns),
    )

    assert summary["positive_relative_log_blocks"] == 1
    assert summary["positive_fraction"] == pytest.approx(1 / 3)
    assert summary["status"] == "FAIL"


def _genuinely_timed_path() -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(42)
    index = pd.bdate_range("2018-01-02", periods=900)
    signal = pd.Series(rng.integers(0, 2, len(index)).astype(float), index=index)
    forward_returns = np.where(signal.to_numpy()[:-1] > 0.0, 0.003, -0.003)
    prices = np.r_[100.0, 100.0 * np.cumprod(1.0 + forward_returns)]
    return pd.Series(prices, index=index, name="Open"), signal


def test_circular_shift_timing_test_detects_a_genuinely_timed_signal():
    prices, signal = _genuinely_timed_path()

    result = circular_shift_timing_test(
        prices,
        signal,
        buy_cost_bps=8,
        sell_cost_bps=13,
        samples=999,
        min_shift=60,
        seed=17,
        family_size=4,
    )

    assert result["actual_cagr"] > result["random_cagr_q95"]
    assert result["raw_p_value"] <= 0.01
    assert result["bonferroni_p_value"] <= 0.05
    assert result["random_cagr_q05"] <= result["random_cagr_median"]
    assert result["random_cagr_median"] <= result["random_cagr_q95"]


def test_circular_shift_timing_test_is_deterministic_for_a_seed():
    prices, signal = _genuinely_timed_path()
    kwargs = {
        "buy_cost_bps": 8,
        "sell_cost_bps": 13,
        "samples": 200,
        "min_shift": 60,
        "seed": 20260820,
        "family_size": 4,
    }

    first = circular_shift_timing_test(prices, signal, **kwargs)
    second = circular_shift_timing_test(prices, signal, **kwargs)

    assert first == second


def test_circular_shift_timing_test_applies_bonferroni_correction():
    prices, signal = _genuinely_timed_path()

    result = circular_shift_timing_test(
        prices,
        signal,
        buy_cost_bps=0,
        sell_cost_bps=0,
        samples=100,
        min_shift=60,
        seed=5,
        family_size=4,
    )

    assert result["bonferroni_p_value"] == pytest.approx(min(1.0, 4.0 * result["raw_p_value"]))


def test_circular_shift_timing_test_accepts_fractional_exposure():
    index = pd.bdate_range("2020-01-02", periods=160)
    prices = pd.Series(np.linspace(100.0, 120.0, len(index)), index=index)
    signal = pd.Series(np.tile([0.0, 0.25, 0.75, 1.0], 40), index=index)

    result = circular_shift_timing_test(
        prices,
        signal,
        buy_cost_bps=8,
        sell_cost_bps=13,
        samples=10,
        min_shift=60,
    )

    assert result["requested_samples"] == 10
    assert result["samples"] == 10
    assert result["evaluated_samples"] == 10
    assert result["distinct_shift_count"] == 10


def test_circular_shift_timing_test_evaluates_the_only_allowed_shift_once():
    index = pd.bdate_range("2024-01-02", periods=120)
    signal = pd.Series(np.r_[np.ones(60), np.zeros(60)], index=index)
    forward_returns = np.r_[np.full(60, 0.01), np.full(59, -0.01)]
    prices = pd.Series(
        np.r_[100.0, 100.0 * np.cumprod(1.0 + forward_returns)],
        index=index,
    )

    result = circular_shift_timing_test(
        prices,
        signal,
        buy_cost_bps=0,
        sell_cost_bps=0,
        samples=1_000,
        min_shift=60,
        seed=17,
        family_size=4,
    )

    assert result["requested_samples"] == 1_000
    assert result["samples"] == 1
    assert result["evaluated_samples"] == 1
    assert result["distinct_shift_count"] == 1
    assert result["random_cagr_q95"] < result["actual_cagr"]
    assert result["raw_p_value"] == 0.5
    assert result["bonferroni_p_value"] == 1.0


def test_circular_shift_timing_test_rejects_invalid_actual_net_returns():
    index = pd.bdate_range("2024-01-02", periods=120)
    prices = pd.Series(100.0, index=index)
    signal = pd.Series(np.r_[np.ones(60), np.zeros(60)], index=index)

    with pytest.raises(ValueError, match="net returns.*greater than negative one"):
        circular_shift_timing_test(
            prices,
            signal,
            buy_cost_bps=10_000,
            sell_cost_bps=0,
            samples=1,
            min_shift=60,
        )


def test_circular_shift_timing_test_rejects_invalid_randomized_net_returns():
    index = pd.bdate_range("2024-01-02", periods=120)
    prices = pd.Series(100.0, index=index)
    signal = pd.Series(np.r_[np.zeros(60), np.ones(60)], index=index)

    with pytest.raises(ValueError, match="randomized net returns.*greater than negative one"):
        circular_shift_timing_test(
            prices,
            signal,
            buy_cost_bps=0,
            sell_cost_bps=10_000,
            samples=1,
            min_shift=60,
        )


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (lambda prices, signal: (prices.iloc[:-1], signal), "indexes must match"),
        (
            lambda prices, signal: (prices.mask(prices.index == prices.index[0], 0.0), signal),
            "prices",
        ),
        (
            lambda prices, signal: (prices, signal.mask(signal.index == signal.index[0], np.nan)),
            "signal",
        ),
        (lambda prices, signal: (prices, signal + 0.01), "signal"),
    ],
)
def test_circular_shift_timing_test_rejects_invalid_paths(mutator, error):
    index = pd.bdate_range("2024-01-02", periods=160)
    prices = pd.Series(np.linspace(100.0, 110.0, len(index)), index=index)
    signal = pd.Series(np.tile([0.0, 1.0], 80), index=index)
    invalid_prices, invalid_signal = mutator(prices, signal)

    with pytest.raises(ValueError, match=error):
        circular_shift_timing_test(
            invalid_prices,
            invalid_signal,
            buy_cost_bps=8,
            sell_cost_bps=13,
            samples=10,
            min_shift=60,
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"buy_cost_bps": -1}, "cost"),
        ({"sell_cost_bps": np.inf}, "cost"),
        ({"samples": 0}, "samples"),
        ({"min_shift": 0}, "min_shift"),
        ({"family_size": 0}, "family_size"),
    ],
)
def test_circular_shift_timing_test_rejects_invalid_parameters(overrides, error):
    index = pd.bdate_range("2024-01-02", periods=160)
    prices = pd.Series(np.linspace(100.0, 110.0, len(index)), index=index)
    signal = pd.Series(np.tile([0.0, 1.0], 80), index=index)
    kwargs = {
        "buy_cost_bps": 8,
        "sell_cost_bps": 13,
        "samples": 10,
        "min_shift": 60,
        "family_size": 4,
    }
    kwargs.update(overrides)

    with pytest.raises(ValueError, match=error):
        circular_shift_timing_test(prices, signal, **kwargs)


def test_circular_shift_timing_test_requires_enough_rows_for_allowed_shifts():
    index = pd.bdate_range("2024-01-02", periods=119)
    prices = pd.Series(np.linspace(100.0, 110.0, len(index)), index=index)
    signal = pd.Series(np.tile([0.0, 1.0], 60)[: len(index)], index=index)

    with pytest.raises(ValueError, match="enough rows"):
        circular_shift_timing_test(
            prices,
            signal,
            buy_cost_bps=8,
            sell_cost_bps=13,
            samples=10,
            min_shift=60,
        )


def _metric_set(
    *, cagr: float = 0.08, sharpe: float = 1.1, max_drawdown: float = -0.30, calmar: float = 0.3
) -> dict[str, float]:
    return {
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
    }


def test_risk_utility_pass_implements_either_declared_branch_exactly():
    buy_hold = _metric_set(sharpe=1.0, max_drawdown=-0.40, calmar=0.30)
    drawdown_branch = _metric_set(sharpe=1.01, max_drawdown=-0.34, calmar=0.20)
    calmar_branch = _metric_set(sharpe=0.8, max_drawdown=-0.50, calmar=0.40)
    weak = _metric_set(sharpe=1.0, max_drawdown=-0.34, calmar=0.399)

    assert risk_utility_pass(drawdown_branch, buy_hold)
    assert risk_utility_pass(calmar_branch, buy_hold)
    assert not risk_utility_pass(weak, buy_hold)


@pytest.mark.parametrize(
    ("side", "invalid_drawdown"),
    [
        ("candidate", 0.01),
        ("candidate", -1.01),
        ("buy_hold", 0.01),
        ("buy_hold", -1.01),
    ],
)
def test_risk_utility_pass_rejects_drawdowns_outside_financial_bounds(side, invalid_drawdown):
    candidate = _metric_set(sharpe=1.1, max_drawdown=-0.30, calmar=0.40)
    buy_hold = _metric_set(sharpe=1.0, max_drawdown=-0.40, calmar=0.20)
    metrics = candidate if side == "candidate" else buy_hold
    metrics["max_drawdown"] = invalid_drawdown

    with pytest.raises(ValueError, match="maximum drawdowns.*between negative one and zero"):
        risk_utility_pass(candidate, buy_hold)


def _passing_gate_inputs() -> dict[str, object]:
    return {
        "target_base_metrics": _metric_set(),
        "target_stress_metrics": _metric_set(cagr=0.04, sharpe=0.5),
        "target_buy_hold": _metric_set(cagr=0.10, sharpe=1.0, max_drawdown=-0.40, calmar=0.20),
        "constant_benchmark": _metric_set(cagr=0.07),
        "timing_result": {"bonferroni_p_value": 0.04},
        "stability_summary": {"status": "PASS"},
        "peer_sharpe_wins_count": 4,
        "execution_complete": True,
    }


def test_candidate_gate_decision_emits_all_eight_gates_in_frozen_order():
    decision = candidate_gate_decision(**_passing_gate_inputs())

    assert [gate["id"] for gate in decision["gates"]] == [
        "positive_base_cagr_and_sharpe",
        "positive_stress_cagr_and_sharpe",
        "target_cagr_at_least_70pct_buy_hold",
        "risk_utility_improvement",
        "beats_constant_exposure_and_familywise_random_timing_p_le_0_05",
        "stable_across_predeclared_subperiods",
        "improves_risk_adjusted_objective_on_at_least_4_of_6_peers",
        "executable_data_and_cost_model_complete",
    ]
    assert [gate["status"] for gate in decision["gates"]] == ["PASS"] * 8
    assert decision["overall"] == "HISTORICALLY_SUPPORTED_FOR_PAPER_FORWARD"


def test_all_weak_candidate_family_is_rejected_without_a_forced_winner():
    weak_inputs = _passing_gate_inputs()
    weak_inputs.update(
        {
            "target_base_metrics": _metric_set(cagr=-0.01, sharpe=-0.2, calmar=-0.1),
            "target_stress_metrics": _metric_set(cagr=-0.02, sharpe=-0.3, calmar=-0.2),
            "constant_benchmark": _metric_set(cagr=0.02),
            "timing_result": {"bonferroni_p_value": 0.8},
            "stability_summary": {"status": "FAIL"},
            "peer_sharpe_wins_count": 0,
        }
    )

    family = [candidate_gate_decision(**weak_inputs) for _ in range(4)]

    assert [candidate["overall"] for candidate in family] == ["REJECTED_NO_EDGE"] * 4
    assert all(any(gate["status"] == "FAIL" for gate in candidate["gates"]) for candidate in family)


def test_candidate_gate_decision_prefers_fail_over_insufficient():
    inputs = _passing_gate_inputs()
    inputs["target_base_metrics"] = _metric_set(cagr=-0.01)
    inputs["stability_summary"] = {"status": "INSUFFICIENT"}
    inputs["execution_complete"] = None

    decision = candidate_gate_decision(**inputs)

    assert decision["overall"] == "REJECTED_NO_EDGE"


def test_candidate_gate_decision_is_insufficient_when_no_gate_fails():
    inputs = _passing_gate_inputs()
    inputs["stability_summary"] = {"status": "INSUFFICIENT"}
    inputs["execution_complete"] = None

    decision = candidate_gate_decision(**inputs)

    assert decision["gates"][5]["status"] == "INSUFFICIENT"
    assert decision["gates"][7]["status"] == "INSUFFICIENT"
    assert decision["overall"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("side", ["target_base_metrics", "target_buy_hold"])
def test_candidate_gate_decision_is_insufficient_for_invalid_drawdown(side):
    inputs = _passing_gate_inputs()
    metrics = inputs[side]
    assert isinstance(metrics, dict)
    metrics["max_drawdown"] = 0.01
    target_base = inputs["target_base_metrics"]
    assert isinstance(target_base, dict)
    target_base["calmar"] = 0.40

    decision = candidate_gate_decision(**inputs)

    assert decision["gates"][3]["status"] == "INSUFFICIENT"
    assert decision["overall"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("invalid_p_value", [-0.01, 1.01])
def test_candidate_gate_decision_is_insufficient_for_out_of_range_timing_p_value(
    invalid_p_value,
):
    inputs = _passing_gate_inputs()
    inputs["timing_result"] = {"bonferroni_p_value": invalid_p_value}

    decision = candidate_gate_decision(**inputs)

    assert decision["gates"][4]["status"] == "INSUFFICIENT"
    assert decision["overall"] == "INSUFFICIENT_EVIDENCE"
