"""Exact detector, state machine, accounting, comparators, and bootstrap semantics."""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from decimal import Decimal
from statistics import NormalDist, median
from typing import Any, Mapping, Sequence

import numpy as np

from gold_research.focus_contract import (
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_LOWER_RANK,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    CALIBRATION_RANK,
    CAPITAL,
    COOLDOWN_UPDATES,
    HALF_SPREAD,
    INCREMENTS,
    NULL_CALIBRATION_PATHS,
    NULL_VALIDATION_PATHS,
    SYNTHETIC_PATHS,
    SYNTHETIC_SEED,
    WARMUP_INCREMENTS,
    Action,
    CalibrationDocument,
    ContractViolation,
    DetectorOutput,
    Direction,
    EvaluationDocument,
    MachineState,
    Portfolio,
    Position,
    TerminalClass,
    require_finite_number,
    require_positive_decimal,
)

COMPARATOR_NAMES = ("CASH", "BUY_AND_HOLD", "EQUAL_EXPOSURE")
_CALIBRATION_BATCH_PATHS = 128
_SYNTHETIC_UPDATES = INCREMENTS - WARMUP_INCREMENTS


def _candidate_glr(prefix: np.ndarray, tau: np.ndarray, total: float, count: int) -> np.ndarray:
    left = prefix[tau - 1]
    right = total - left
    return 0.5 * (
        left * left / tau + right * right / (count - tau) - total * total / count
    )


class FocusDetector:
    """Exact unknown-pre-change Gaussian GLR detector with deterministic ties."""

    def __init__(self) -> None:
        self._values: list[float] = []
        self._prefix: list[float] = []

    @property
    def observations(self) -> int:
        return len(self._values)

    def reset(self) -> None:
        self._values.clear()
        self._prefix.clear()

    def update(self, value: float) -> DetectorOutput:
        value = require_finite_number(value, "detector observation")
        self._values.append(value)
        self._prefix.append(value + (self._prefix[-1] if self._prefix else 0.0))
        count = len(self._values)
        if count == 1:
            direction = Direction.UP if value > 0.0 else Direction.DOWN
            return DetectorOutput(0.0, 0, direction, count)

        prefix = np.asarray(self._prefix, dtype=np.float64)
        candidates = np.arange(1, count, dtype=np.int64)
        statistics = _candidate_glr(prefix, candidates, prefix[-1], count)
        if not np.all(np.isfinite(statistics)):
            raise ContractViolation("detector produced a non-finite statistic")
        best_zero_based = int(np.argmax(statistics))
        changepoint = int(candidates[best_zero_based])
        statistic = max(0.0, float(statistics[best_zero_based]))
        post_mean = (prefix[-1] - prefix[changepoint - 1]) / (count - changepoint)
        direction = Direction.UP if post_mean > 0.0 else Direction.DOWN
        return DetectorOutput(statistic, changepoint, direction, count)


def exhaustive_glr_oracle(values: Sequence[float]) -> DetectorOutput:
    """Independent all-changepoint implementation used only as a falsifying oracle."""

    clean = [require_finite_number(item, "oracle observation") for item in values]
    if not clean:
        raise ContractViolation("oracle needs at least one observation")
    if len(clean) == 1:
        direction = Direction.UP if clean[0] > 0.0 else Direction.DOWN
        return DetectorOutput(0.0, 0, direction, 1)

    total = math.fsum(clean)
    null_score = total * total / len(clean)
    best_statistic = -math.inf
    best_tau = 1
    running = 0.0
    for tau in range(1, len(clean)):
        running += clean[tau - 1]
        right = total - running
        statistic = 0.5 * (
            running * running / tau
            + right * right / (len(clean) - tau)
            - null_score
        )
        if statistic > best_statistic:
            best_statistic = statistic
            best_tau = tau
    post_mean = math.fsum(clean[best_tau:]) / (len(clean) - best_tau)
    direction = Direction.UP if post_mean > 0.0 else Direction.DOWN
    return DetectorOutput(max(0.0, best_statistic), best_tau, direction, len(clean))


def robust_scale(increments: Sequence[float]) -> float:
    if len(increments) != WARMUP_INCREMENTS:
        raise ContractViolation("scale requires exactly 63 increments")
    clean = [require_finite_number(item, "scale increment") for item in increments]
    center = median(clean)
    scale = 1.4826 * median(abs(item - center) for item in clean)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ContractViolation("robust scale must be finite and positive")
    return scale


def modeled_sides(mark: str | Decimal) -> tuple[Decimal, Decimal]:
    reference = require_positive_decimal(mark, "reference mark")
    buy = reference + HALF_SPREAD
    sell = reference - HALF_SPREAD
    if not buy.is_finite() or not sell.is_finite() or sell <= 0:
        raise ContractViolation("modeled buy and sell sides must be finite and positive")
    return buy, sell


def portfolio_wealth(portfolio: Portfolio, mark: str | Decimal) -> Decimal:
    reference = require_positive_decimal(mark, "valuation mark")
    wealth = portfolio.cash + portfolio.grams * reference
    if not wealth.is_finite() or wealth <= 0:
        raise ContractViolation("portfolio wealth must be finite and positive")
    return wealth


def settle_pending(
    state: MachineState, portfolio: Portfolio, mark: str | Decimal
) -> tuple[MachineState, Portfolio, bool]:
    """Settle at the immediate next mark before valuation and detector update."""

    if state.pending is None:
        return state, portfolio, False
    buy, sell = modeled_sides(mark)
    if state.pending is Action.BUY:
        if state.position is not Position.CASH or portfolio.grams != 0 or portfolio.cash <= 0:
            raise ContractViolation("buy settlement requires an all-cash portfolio")
        grams = portfolio.cash / buy
        if not grams.is_finite() or grams <= 0:
            raise ContractViolation("modeled grams must be finite and positive")
        portfolio = Portfolio(cash=Decimal("0"), grams=grams)
        position = Position.LONG
    else:
        if state.position is not Position.LONG or portfolio.cash != 0 or portfolio.grams <= 0:
            raise ContractViolation("sell settlement requires an all-long portfolio")
        cash = portfolio.grams * sell
        if not cash.is_finite() or cash <= 0:
            raise ContractViolation("modeled sale proceeds must be finite and positive")
        portfolio = Portfolio(cash=cash, grams=Decimal("0"))
        position = Position.CASH
    return MachineState(position=position, cooldown=COOLDOWN_UPDATES), portfolio, True


def apply_detector_output(
    state: MachineState, output: DetectorOutput, threshold: float
) -> tuple[MachineState, Action | None]:
    threshold = require_finite_number(threshold, "threshold")
    if threshold < 0.0:
        raise ContractViolation("threshold must be nonnegative")
    statistic = require_finite_number(output.statistic, "detector statistic")
    if statistic < 0.0 or output.observations < 1:
        raise ContractViolation("detector output is invalid")

    if state.pending is not None:
        raise ContractViolation("a second detector update cannot overlap a pending transition")
    if state.cooldown > 0:
        return replace(state, cooldown=state.cooldown - 1, confirmation=None), None
    if statistic < threshold:
        return replace(state, confirmation=None), None
    if state.confirmation is not output.direction:
        return replace(state, confirmation=output.direction), None

    action: Action | None = None
    if output.direction is Direction.UP and state.position is Position.CASH:
        action = Action.BUY
    elif output.direction is Direction.DOWN and state.position is Position.LONG:
        action = Action.SELL
    return replace(state, confirmation=None, pending=action), action


def _log_ratio(numerator: Decimal, denominator: Decimal) -> float:
    if numerator <= 0 or denominator <= 0:
        raise ContractViolation("log-return wealth must be positive")
    result = math.log(float(numerator / denominator))
    return require_finite_number(result, "log return")


def circular_bootstrap_lower_bounds(
    differences: Mapping[str, Sequence[float]],
    terminal_differences: Mapping[str, float],
    *,
    replicates: int = BOOTSTRAP_REPLICATES,
    block: int = BOOTSTRAP_BLOCK,
    seed: int = BOOTSTRAP_SEED,
    lower_rank: int = BOOTSTRAP_LOWER_RANK,
) -> dict[str, float]:
    """Use one circular index matrix for all comparators and append terminal once."""

    if tuple(differences) != COMPARATOR_NAMES or tuple(terminal_differences) != COMPARATOR_NAMES:
        raise ContractViolation("all three comparator identities must be present in frozen order")
    lengths = {len(values) for values in differences.values()}
    if len(lengths) != 1:
        raise ContractViolation("paired comparator paths must have equal length")
    observations = lengths.pop()
    if observations < 1 or replicates < 1 or block < 1:
        raise ContractViolation("bootstrap dimensions must be positive")
    if not 1 <= lower_rank <= replicates:
        raise ContractViolation("bootstrap lower rank is outside the replicate count")

    blocks_needed = math.ceil(observations / block)
    generator = np.random.default_rng(seed)
    starts = generator.integers(0, observations, size=(replicates, blocks_needed))
    offsets = np.arange(block, dtype=np.int64)
    indices = ((starts[:, :, None] + offsets) % observations).reshape(replicates, -1)
    indices = indices[:, :observations]

    result: dict[str, float] = {}
    for name in COMPARATOR_NAMES:
        ordinary = np.asarray(differences[name], dtype=np.float64)
        terminal = require_finite_number(terminal_differences[name], f"{name} terminal")
        if not np.all(np.isfinite(ordinary)):
            raise ContractViolation(f"{name} differences must be finite")
        means = (ordinary[indices].sum(axis=1) + terminal) / (observations + 1)
        result[name] = float(np.sort(means)[lower_rank - 1])
    return result


def _terminal_candidate(
    state: MachineState, portfolio: Portfolio, mark: Decimal
) -> Portfolio:
    buy, sell = modeled_sides(mark)
    if state.position is Position.LONG:
        return Portfolio(cash=portfolio.grams * sell, grams=Decimal("0"))
    if state.pending is Action.BUY:
        grams = portfolio.cash / buy
        return Portfolio(cash=grams * sell, grams=Decimal("0"))
    return portfolio


def _comparator_returns(
    marks: Sequence[Decimal], fraction: Decimal
) -> tuple[list[float], float, Decimal]:
    if not Decimal("0") <= fraction <= Decimal("1"):
        raise ContractViolation("equal-exposure fraction must be in [0, 1]")
    entry_mark = marks[64]
    buy, _ = modeled_sides(entry_mark)
    invested_cash = CAPITAL * fraction
    portfolio = Portfolio(cash=CAPITAL - invested_cash, grams=invested_cash / buy)
    previous = CAPITAL
    ordinary: list[float] = []
    for index in range(64, 757):
        wealth = portfolio_wealth(portfolio, marks[index])
        ordinary.append(_log_ratio(wealth, previous))
        previous = wealth
    _, terminal_sell = modeled_sides(marks[757])
    terminal = portfolio.cash + portfolio.grams * terminal_sell
    return ordinary, _log_ratio(terminal, previous), terminal


def evaluate_once(
    marks: Sequence[str | Decimal], calibration: CalibrationDocument
) -> EvaluationDocument:
    """Evaluate the frozen 757-mark path plus one common terminal observation once."""

    if len(marks) != INCREMENTS + 2:
        raise ContractViolation("evaluation requires exactly 757 marks and one terminal mark")
    if (
        calibration.master_seed != SYNTHETIC_SEED
        or calibration.path_count != SYNTHETIC_PATHS
        or calibration.thresholds_calibrated != 1
        or calibration.recalibrations != 0
    ):
        raise ContractViolation("calibration identity is not the frozen one-threshold document")
    threshold = require_finite_number(calibration.threshold, "calibrated threshold")
    if threshold < 0.0:
        raise ContractViolation("calibrated threshold must be nonnegative")
    prices = [require_positive_decimal(item, "selected mark") for item in marks]
    increments = [_log_ratio(prices[index], prices[index - 1]) for index in range(1, 757)]
    scale = robust_scale(increments[:WARMUP_INCREMENTS])

    detector = FocusDetector()
    state = MachineState()
    portfolio = Portfolio()
    previous_wealth = CAPITAL
    candidate_returns: list[float] = []
    exposures: list[Decimal] = []

    for index in range(64, 757):
        state, portfolio, settled = settle_pending(state, portfolio, prices[index])
        if settled:
            detector.reset()
        wealth = portfolio_wealth(portfolio, prices[index])
        candidate_returns.append(_log_ratio(wealth, previous_wealth))
        exposure = portfolio.grams * prices[index] / wealth
        if not exposure.is_finite() or not Decimal("0") <= exposure <= Decimal("1"):
            raise ContractViolation("candidate exposure must be finite and in [0, 1]")
        exposures.append(exposure)
        output = detector.update(increments[index - 1] / scale)
        state, _ = apply_detector_output(state, output, threshold)
        previous_wealth = wealth

    terminal_portfolio = _terminal_candidate(state, portfolio, prices[757])
    candidate_terminal = terminal_portfolio.cash
    terminal_return = _log_ratio(candidate_terminal, previous_wealth)
    qbar = sum(exposures, Decimal("0")) / Decimal(len(exposures))

    cash_returns = [0.0] * len(candidate_returns)
    buy_returns, buy_terminal, _ = _comparator_returns(prices, Decimal("1"))
    equal_returns, equal_terminal, _ = _comparator_returns(prices, qbar)
    comparator_returns = {
        "CASH": cash_returns,
        "BUY_AND_HOLD": buy_returns,
        "EQUAL_EXPOSURE": equal_returns,
    }
    comparator_terminals = {
        "CASH": 0.0,
        "BUY_AND_HOLD": buy_terminal,
        "EQUAL_EXPOSURE": equal_terminal,
    }
    differences = {
        name: [candidate - comparator for candidate, comparator in zip(
            candidate_returns, comparator_returns[name], strict=True
        )]
        for name in COMPARATOR_NAMES
    }
    terminal_differences = {
        name: terminal_return - comparator_terminals[name] for name in COMPARATOR_NAMES
    }
    lower_bounds = circular_bootstrap_lower_bounds(differences, terminal_differences)
    terminal_class = (
        TerminalClass.PASS
        if all(lower_bounds[name] > 0.0 for name in COMPARATOR_NAMES)
        else TerminalClass.FAIL
    )
    return EvaluationDocument(
        terminal_class=terminal_class,
        lower_bounds=lower_bounds,
        candidate_terminal_wealth=str(candidate_terminal),
        qbar=float(qbar),
    )


def calibration_plan() -> dict[str, object]:
    """Return the sealed, non-configurable synthetic calibration plan."""

    streams = np.random.SeedSequence(SYNTHETIC_SEED).spawn(11)
    return {
        "master_seed": SYNTHETIC_SEED,
        "stream_spawn_keys": [list(stream.spawn_key) for stream in streams],
        "null_calibration_paths": NULL_CALIBRATION_PATHS,
        "calibration_rank": CALIBRATION_RANK,
        "null_validation_paths": NULL_VALIDATION_PATHS,
        "change_cells": [
            {"delta": delta, "boundary": boundary, "paths": 10_000}
            for delta in ("0.25", "0.50", "1.00")
            for boundary in (189, 378, 567)
        ],
        "total_paths": SYNTHETIC_PATHS,
        "thresholds": 1,
        "recalibrations": 0,
    }


def _array_bytes(value: np.ndarray, dtype: str) -> bytes:
    return np.asarray(value, dtype=dtype, order="C").tobytes(order="C")


def _detector_path_outputs(innovations: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Evaluate the exact exhaustive statistic for a bounded batch of synthetic paths."""

    if innovations.ndim != 2 or innovations.shape[1] != INCREMENTS:
        raise ContractViolation("synthetic path batch must have exactly 756 innovations")
    observed = innovations[:, WARMUP_INCREMENTS:]
    if not np.all(np.isfinite(observed)):
        raise ContractViolation("synthetic innovations must be finite")
    prefix = np.cumsum(observed, axis=1, dtype=np.float64)
    paths = innovations.shape[0]
    statistics = np.empty((paths, _SYNTHETIC_UPDATES), dtype=np.float64)
    changepoints = np.empty((paths, _SYNTHETIC_UPDATES), dtype=np.int16)
    directions = np.empty((paths, _SYNTHETIC_UPDATES), dtype=np.int8)
    statistics[:, 0] = 0.0
    changepoints[:, 0] = 0
    directions[:, 0] = np.where(observed[:, 0] > 0.0, 1, -1)
    rows = np.arange(paths)
    for count in range(2, _SYNTHETIC_UPDATES + 1):
        candidates = np.arange(1, count, dtype=np.float64)
        total = prefix[:, count - 1, None]
        left = prefix[:, : count - 1]
        candidate_statistics = 0.5 * (
            left * left / candidates
            + (total - left) ** 2 / (count - candidates)
            - total * total / count
        )
        best = np.argmax(candidate_statistics, axis=1)
        statistics[:, count - 1] = np.maximum(0.0, candidate_statistics[rows, best])
        changepoints[:, count - 1] = best + 1
        post_sum = prefix[:, count - 1] - prefix[rows, best]
        directions[:, count - 1] = np.where(post_sum > 0.0, 1, -1)
    if not np.all(np.isfinite(statistics)) or np.any(statistics < 0.0):
        raise ContractViolation("synthetic detector output is invalid")
    return statistics, changepoints, directions


def _digest_outputs(
    digest: Any, statistics: np.ndarray, changepoints: np.ndarray, directions: np.ndarray
) -> None:
    digest.update(_array_bytes(statistics, "<f8"))
    digest.update(_array_bytes(changepoints, "<i2"))
    digest.update(_array_bytes(directions, "i1"))


def _stream_batches(
    stream: np.random.SeedSequence,
    paths: int,
    *,
    delta: float = 0.0,
    boundary: int | None = None,
):
    generator = np.random.default_rng(stream)
    innovations_digest = hashlib.sha256()
    outputs_digest = hashlib.sha256()
    generated = 0
    while generated < paths:
        size = min(_CALIBRATION_BATCH_PATHS, paths - generated)
        innovations = generator.standard_normal((size, INCREMENTS), dtype=np.float64)
        innovations_digest.update(_array_bytes(innovations, "<f8"))
        observations = innovations.copy()
        if boundary is not None:
            observations[:, boundary:] += delta
        statistics, changepoints, directions = _detector_path_outputs(observations)
        _digest_outputs(outputs_digest, statistics, changepoints, directions)
        generated += size
        yield statistics, directions, generated, innovations_digest, outputs_digest


def _stream_identity(
    stream: np.random.SeedSequence,
    paths: int,
    innovations_digest: Any,
    outputs_digest: Any,
) -> dict[str, Any]:
    return {
        "spawn_key": list(stream.spawn_key),
        "path_count": paths,
        "innovations_sha256": innovations_digest.hexdigest(),
        "detector_outputs_sha256": outputs_digest.hexdigest(),
        "encoding": (
            "fixed-128-path chunks; innovations=<f8 C-order 756/path; "
            "outputs=<f8 statistic then <i2 changepoint then i1 direction, C-order"
        ),
    }


def _calibration_stream(stream: np.random.SeedSequence) -> tuple[dict[str, Any], np.ndarray]:
    maxima: list[np.ndarray] = []
    innovations_digest = outputs_digest = None
    for statistics, directions, _, innovations_digest, outputs_digest in _stream_batches(
        stream, NULL_CALIBRATION_PATHS
    ):
        adjacent = np.where(
            directions[:, 1:] == directions[:, :-1],
            np.minimum(statistics[:, 1:], statistics[:, :-1]),
            -np.inf,
        )
        maxima.append(np.max(adjacent, axis=1))
    assert innovations_digest is not None and outputs_digest is not None
    values = np.concatenate(maxima)
    if len(values) != NULL_CALIBRATION_PATHS or not np.all(np.isfinite(values)):
        raise ContractViolation("null calibration maxima are invalid")
    return (
        _stream_identity(
            stream, NULL_CALIBRATION_PATHS, innovations_digest, outputs_digest
        ),
        values,
    )


def _null_validation_stream(
    stream: np.random.SeedSequence, threshold: float
) -> tuple[dict[str, Any], int]:
    false_transitions = 0
    innovations_digest = outputs_digest = None
    for statistics, directions, _, innovations_digest, outputs_digest in _stream_batches(
        stream, NULL_VALIDATION_PATHS
    ):
        qualifies = statistics >= threshold
        false_transitions += int(
            np.count_nonzero(
                np.any(
                    qualifies[:, 1:]
                    & qualifies[:, :-1]
                    & (directions[:, 1:] == 1)
                    & (directions[:, :-1] == 1),
                    axis=1,
                )
            )
        )
    assert innovations_digest is not None and outputs_digest is not None
    return (
        _stream_identity(stream, NULL_VALIDATION_PATHS, innovations_digest, outputs_digest),
        false_transitions,
    )


def _first_confirmations(
    statistics: np.ndarray, directions: np.ndarray, threshold: float
) -> tuple[np.ndarray, np.ndarray]:
    paths = statistics.shape[0]
    registers = np.zeros(paths, dtype=np.int8)
    confirmation_time = np.zeros(paths, dtype=np.int16)
    confirmation_direction = np.zeros(paths, dtype=np.int8)
    unresolved = np.ones(paths, dtype=bool)
    for offset in range(_SYNTHETIC_UPDATES):
        qualifies = statistics[:, offset] >= threshold
        current = directions[:, offset]
        confirms = unresolved & qualifies & (registers == current)
        confirmation_time[confirms] = WARMUP_INCREMENTS + 1 + offset
        confirmation_direction[confirms] = current[confirms]
        unresolved[confirms] = False
        clear = ~qualifies | confirms
        registers[clear] = 0
        arm = unresolved & qualifies & (registers != current)
        registers[arm] = current[arm]
    return confirmation_time, confirmation_direction


def _detectability_stream(
    stream: np.random.SeedSequence, threshold: float, delta: float, boundary: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    counts = {
        "correct": 0,
        "pre_change": 0,
        "boundary_straddling": 0,
        "wrong_direction": 0,
        "late": 0,
        "missing": 0,
    }
    delays = np.zeros(62, dtype=np.int64)
    innovations_digest = outputs_digest = None
    for statistics, directions, _, innovations_digest, outputs_digest in _stream_batches(
        stream, 10_000, delta=delta, boundary=boundary
    ):
        times, alarm_directions = _first_confirmations(statistics, directions, threshold)
        missing = times == 0
        pre_change = (times != 0) & (times <= boundary)
        straddling = times == boundary + 1
        wrong = (times > boundary + 1) & (alarm_directions == -1)
        delay = times.astype(np.int32) - boundary
        correct = (alarm_directions == 1) & (delay >= 2) & (delay <= 63)
        late = (alarm_directions == 1) & (delay > 63)
        counts["missing"] += int(np.count_nonzero(missing))
        counts["pre_change"] += int(np.count_nonzero(pre_change))
        counts["boundary_straddling"] += int(np.count_nonzero(straddling))
        counts["wrong_direction"] += int(np.count_nonzero(wrong))
        counts["correct"] += int(np.count_nonzero(correct))
        counts["late"] += int(np.count_nonzero(late))
        if np.any(correct):
            delays += np.bincount(delay[correct] - 2, minlength=62)
    assert innovations_digest is not None and outputs_digest is not None
    if sum(counts.values()) != 10_000:
        raise ContractViolation("detectability classifications are not exhaustive")
    rate = counts["correct"] / 10_000
    record = {
        "delta": format(delta, ".2f"),
        "boundary": boundary,
        "paths": 10_000,
        "correct_count": counts["correct"],
        "correct_rate_hex": rate.hex(),
        "correct_rate_decimal": repr(rate),
        "failure_counts": {key: value for key, value in counts.items() if key != "correct"},
        "delay_2_to_63_counts": delays.tolist(),
        "delay_histogram_sha256": hashlib.sha256(_array_bytes(delays, "<i8")).hexdigest(),
        "pass": rate >= 0.80 if delta == 0.50 else None,
    }
    return _stream_identity(stream, 10_000, innovations_digest, outputs_digest), record


def _wilson_upper(successes: int, trials: int) -> float:
    z = NormalDist().inv_cdf(0.95)
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    center = proportion + z * z / (2.0 * trials)
    radius = z * math.sqrt(proportion * (1.0 - proportion) / trials + z * z / (4.0 * trials**2))
    return (center + radius) / denominator


def execute_synthetic_calibration() -> dict[str, Any]:
    """Execute the frozen one-threshold, 140,000-path calibration exactly once."""

    streams = np.random.SeedSequence(SYNTHETIC_SEED).spawn(11)
    stream_documents: list[dict[str, Any]] = []
    calibration_stream, maxima = _calibration_stream(streams[0])
    stream_documents.append({"name": "calibration", **calibration_stream})
    ordered = np.sort(maxima)
    threshold = float(ordered[CALIBRATION_RANK - 1])
    calibration = CalibrationDocument(threshold=threshold)

    null_stream, false_transitions = _null_validation_stream(streams[1], threshold)
    stream_documents.append({"name": "null_validation", **null_stream})
    wilson_upper = _wilson_upper(false_transitions, NULL_VALIDATION_PATHS)

    cells: list[dict[str, Any]] = []
    stream_index = 2
    for delta in (0.25, 0.50, 1.00):
        for boundary in (189, 378, 567):
            stream_document, cell = _detectability_stream(
                streams[stream_index], threshold, delta, boundary
            )
            stream_documents.append(
                {"name": f"delta_{delta:.2f}_boundary_{boundary}", **stream_document}
            )
            cells.append(cell)
            stream_index += 1

    null_pass = wilson_upper <= 0.05
    detectability_pass = all(cell["pass"] is True for cell in cells if cell["delta"] == "0.50")
    terminal = "PASS" if null_pass and detectability_pass else "DETECTABILITY_FAILED"
    return {
        "schema": "focus-synthetic-calibration/v1",
        "status": terminal,
        "labels": ["SGE_AU9999_PROXY", "FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G"],
        "protocol": {
            "horizon": INCREMENTS,
            "warmup": WARMUP_INCREMENTS,
            "master_seed": SYNTHETIC_SEED,
            "detector_variants": 1,
            "calibrated_thresholds": 1,
            "recalibrations": 0,
            "extra_seeds_effects_horizons_nulls": 0,
            "batch_paths": _CALIBRATION_BATCH_PATHS,
        },
        "streams": stream_documents,
        "calibration": {
            "paths": NULL_CALIBRATION_PATHS,
            "q_rule": "min adjacent statistics only when adjacent directions are equal",
            "ordered_rank": CALIBRATION_RANK,
            "threshold_float_hex": threshold.hex(),
            "threshold_decimal": repr(threshold),
            "ordered_statistics_sha256": hashlib.sha256(_array_bytes(ordered, "<f8")).hexdigest(),
            "document": {
                "threshold_float_hex": calibration.threshold.hex(),
                "master_seed": calibration.master_seed,
                "path_count": calibration.path_count,
                "thresholds_calibrated": calibration.thresholds_calibrated,
                "recalibrations": calibration.recalibrations,
            },
        },
        "null_validation": {
            "paths": NULL_VALIDATION_PATHS,
            "paths_with_false_state_transition": false_transitions,
            "wilson_one_sided_95_upper_float_hex": wilson_upper.hex(),
            "wilson_one_sided_95_upper_decimal": repr(wilson_upper),
            "pass": null_pass,
        },
        "detectability_cells": cells,
        "totals": {
            "generated_paths": SYNTHETIC_PATHS,
            "null_calibration_paths": NULL_CALIBRATION_PATHS,
            "null_validation_paths": NULL_VALIDATION_PATHS,
            "detectability_paths": 90_000,
            "provider_requests": 0,
            "sge_requests": 0,
            "evaluation_executions": 0,
            "flearn_fallbacks": 0,
            "market_outcome_bytes": 0,
        },
    }
