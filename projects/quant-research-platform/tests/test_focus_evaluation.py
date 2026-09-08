from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest

from gold_research.focus_contract import (
    Action,
    CalibrationDocument,
    ContractViolation,
    DetectorOutput,
    Direction,
    MachineState,
    Portfolio,
    Position,
    SYNTHETIC_PATHS,
    SYNTHETIC_SEED,
    TerminalClass,
)
from gold_research.focus_evaluation import (
    COMPARATOR_NAMES,
    FocusDetector,
    _detector_path_outputs,
    apply_detector_output,
    calibration_plan,
    circular_bootstrap_lower_bounds,
    evaluate_once,
    exhaustive_glr_oracle,
    modeled_sides,
    portfolio_wealth,
    robust_scale,
    settle_pending,
)


@pytest.mark.parametrize(
    "values",
    [
        [0.0],
        [1.0, 1.0, 1.0, 1.0],
        [1.0, -1.0, 1.0, -1.0, 1.0],
        [-0.25] * 4 + [1.0] * 5,
        [0.75] * 3 + [-0.5] * 6,
        [0.0, 0.0, 0.0, 0.0],
    ],
)
def test_focus_detector_matches_independent_exhaustive_oracle(values: list[float]) -> None:
    detector = FocusDetector()
    for index, value in enumerate(values, start=1):
        actual = detector.update(value)
        expected = exhaustive_glr_oracle(values[:index])
        assert actual.changepoint == expected.changepoint
        assert actual.direction is expected.direction
        assert actual.statistic == pytest.approx(expected.statistic, abs=1e-12)
        assert math.isfinite(actual.statistic) and actual.statistic >= 0.0
    detector.reset()
    assert detector.update(-1.0) == DetectorOutput(0.0, 0, Direction.DOWN, 1)


def test_scale_uses_exactly_63_increments_and_fails_closed() -> None:
    values = [float(index % 7 - 3) for index in range(63)]
    assert robust_scale(values) > 0.0
    with pytest.raises(ContractViolation, match="exactly 63"):
        robust_scale(values[:-1])
    with pytest.raises(ContractViolation, match="positive"):
        robust_scale([0.0] * 63)


def test_two_confirmations_next_mark_settlement_and_five_ineligible_updates() -> None:
    state = MachineState()
    up = DetectorOutput(10.0, 1, Direction.UP, 2)
    state, action = apply_detector_output(state, up, threshold=10.0)
    assert action is None and state.confirmation is Direction.UP
    state, action = apply_detector_output(state, up, threshold=10.0)
    assert action is Action.BUY and state.pending is Action.BUY

    state, portfolio, settled = settle_pending(state, Portfolio(), Decimal("500.00"))
    assert settled and state.position is Position.LONG and state.cooldown == 5
    assert portfolio.cash == 0
    assert portfolio.grams == Decimal("100000.00") / Decimal("502.50")

    for expected in (4, 3, 2, 1, 0):
        state, action = apply_detector_output(state, up, threshold=10.0)
        assert action is None and state.confirmation is None and state.cooldown == expected
    state, action = apply_detector_output(state, up, threshold=10.0)
    assert action is None and state.confirmation is Direction.UP
    state, action = apply_detector_output(state, up, threshold=10.0)
    assert action is None
    assert state.position is Position.LONG
    assert state.pending is None
    assert state.cooldown == 0


def test_below_threshold_clears_confirmation_and_pending_overlap_fails() -> None:
    state = MachineState(confirmation=Direction.UP)
    below = DetectorOutput(9.99, 1, Direction.UP, 2)
    state, action = apply_detector_output(state, below, threshold=10.0)
    assert action is None and state.confirmation is None
    with pytest.raises(ContractViolation, match="pending"):
        apply_detector_output(MachineState(pending=Action.BUY), below, threshold=10.0)


def test_exact_decimal_costs_capital_and_sell_side_failure() -> None:
    assert modeled_sides("500.00") == (Decimal("502.50"), Decimal("497.50"))
    assert modeled_sides("500.00")[0] - modeled_sides("500.00")[1] == Decimal("5.00")
    for mark in ("2.50", "2.49"):
        with pytest.raises(ContractViolation):
            modeled_sides(mark)
    assert portfolio_wealth(Portfolio(), "500.00") == Decimal("100000.00")


def test_circular_bootstrap_reuses_one_matrix_and_appends_terminal_once() -> None:
    ordinary = {
        "CASH": [1.0, 2.0, 3.0],
        "BUY_AND_HOLD": [2.0, 3.0, 4.0],
        "EQUAL_EXPOSURE": [3.0, 4.0, 5.0],
    }
    terminal = {"CASH": 4.0, "BUY_AND_HOLD": 5.0, "EQUAL_EXPOSURE": 6.0}
    actual = circular_bootstrap_lower_bounds(
        ordinary, terminal, replicates=1, block=2, seed=7, lower_rank=1
    )
    starts = np.random.default_rng(7).integers(0, 3, size=(1, 2))
    indices = ((starts[:, :, None] + np.arange(2)) % 3).reshape(1, -1)[:, :3][0]
    for name in COMPARATOR_NAMES:
        expected = (sum(ordinary[name][index] for index in indices) + terminal[name]) / 4
        assert actual[name] == expected
    shifted = [actual[name] - actual["CASH"] for name in COMPARATOR_NAMES]
    assert shifted == [0.0, 1.0, 2.0]


def test_calibration_plan_is_exact_and_does_not_execute_paths() -> None:
    plan = calibration_plan()
    assert plan["master_seed"] == SYNTHETIC_SEED
    assert plan["total_paths"] == SYNTHETIC_PATHS
    assert plan["null_calibration_paths"] == 25_000
    assert plan["null_validation_paths"] == 25_000
    assert plan["calibration_rank"] == 24_375
    assert len(plan["stream_spawn_keys"]) == 11
    assert len(plan["change_cells"]) == 9
    assert plan["thresholds"] == 1
    assert plan["recalibrations"] == 0


def test_batched_calibration_detector_matches_the_reviewed_scalar_detector() -> None:
    innovations = np.random.default_rng(42).standard_normal((2, 756))
    statistics, changepoints, directions = _detector_path_outputs(innovations)
    for path in range(2):
        detector = FocusDetector()
        for offset, value in enumerate(innovations[path, 63:]):
            expected = detector.update(float(value))
            assert statistics[path, offset] == expected.statistic
            assert changepoints[path, offset] == expected.changepoint
            assert directions[path, offset] == (1 if expected.direction is Direction.UP else -1)


def test_full_synthetic_fixture_preserves_three_comparators_and_terminal() -> None:
    marks = [
        Decimal("500") + Decimal(index) / Decimal("100") + Decimal(index % 5) / Decimal("1000")
        for index in range(758)
    ]
    result = evaluate_once(marks, CalibrationDocument(threshold=1_000_000.0))
    assert result.terminal_class in {TerminalClass.PASS, TerminalClass.FAIL}
    assert tuple(result.lower_bounds) == COMPARATOR_NAMES
    assert result.qbar == 0.0
    assert result.candidate_terminal_wealth == "100000.00"
