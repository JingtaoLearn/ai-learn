from __future__ import annotations

from decimal import Decimal

import pytest

from gold_research.focus_contract import (
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    CAPITAL,
    COOLDOWN_UPDATES,
    DISCLAIMER_ZH,
    INCREMENTS,
    LABEL_PROXY,
    LABEL_SPREAD,
    SYNTHETIC_PATHS,
    SYNTHETIC_SEED,
    WARMUP_INCREMENTS,
    ContractViolation,
    canonical_json_bytes,
    contract_document,
    require_positive_decimal,
    strict_json_loads,
)


def test_frozen_contract_constants_and_required_labels() -> None:
    contract = contract_document()
    assert contract["collection_class"] == "HISTORICAL_SNAPSHOT_V1"
    assert contract["prohibited_mixed_class"] == "PROSPECTIVE_D10_V1"
    assert contract["labels"] == [LABEL_PROXY, LABEL_SPREAD]
    assert contract["disclaimer_zh"] == DISCLAIMER_ZH
    assert CAPITAL == Decimal("100000.00")
    assert contract["half_spread"] == "2.50"
    assert (INCREMENTS, WARMUP_INCREMENTS, COOLDOWN_UPDATES) == (756, 63, 5)
    assert contract["bootstrap"] == {
        "block": BOOTSTRAP_BLOCK,
        "replicates": BOOTSTRAP_REPLICATES,
        "seed": BOOTSTRAP_SEED,
        "lower_rank": 500,
    }
    assert contract["calibration"]["master_seed"] == SYNTHETIC_SEED
    assert contract["calibration"]["path_count"] == SYNTHETIC_PATHS
    assert contract["calibration"]["thresholds_calibrated"] == 1
    assert contract["calibration"]["recalibrations"] == 0


def test_canonical_json_rejects_float_decimal_and_duplicate_keys() -> None:
    with pytest.raises(ContractViolation, match="floating-point"):
        canonical_json_bytes({"value": 1.25})
    with pytest.raises(ContractViolation, match="canonical strings"):
        canonical_json_bytes({"value": Decimal("1.25")})
    with pytest.raises(ContractViolation, match="duplicate JSON key"):
        strict_json_loads(b'{"a":1,"a":2}')
    with pytest.raises(ContractViolation, match="canonical string"):
        strict_json_loads(b'{"a":1.2}')
    with pytest.raises(ContractViolation, match="non-finite"):
        strict_json_loads(b'{"a":NaN}')


def test_positive_decimal_is_exact_finite_and_strictly_positive() -> None:
    assert require_positive_decimal("500.00", "mark") == Decimal("500.00")
    for invalid in ("Infinity", "-Infinity", "NaN", "0", "-1", "bad"):
        with pytest.raises(ContractViolation):
            require_positive_decimal(invalid, "mark")
