import copy
import json
from pathlib import Path

import pytest

from quant_platform.strategy_config import (
    StrategyConfigError,
    load_strategy_config,
    validate_strategy_config,
)
from quant_platform.strategy_operators import (
    OPERATOR_REGISTRY,
    OperatorSpec,
    ParameterSpec,
    validate_registry,
)


def valid_config(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "dataset": {
            "root": str(tmp_path / "state"),
            "snapshot_id": "a" * 64,
        },
        "output_root": str(tmp_path / "runs"),
        "template": {
            "name": "single_stock_daily_causal",
            "version": "1",
            "parameters": {
                "instrument_display_name": "Synthetic Bank",
                "evaluation_start": "2025-01-02",
                "evaluation_end": None,
                "initial_capital_cny": 100000.0,
                "initial_state": "flat",
                "terminal_handling": "mark_to_market",
                "cost_assumption_label": "Conservative research assumption",
            },
        },
        "operators": {
            "fit": {
                "name": "prior_log_ols",
                "version": "1",
                "parameters": {
                    "window_sessions": 20,
                    "price_column": "AdjustedClose",
                },
            },
            "smoothing": {
                "name": "recursive_log_ema",
                "version": "1",
                "parameters": {"span_sessions": 5},
            },
            "statistic": {
                "name": "adjacent_curve_pct_slope",
                "version": "1",
                "parameters": {},
            },
            "decision": {
                "name": "post_start_threshold_crossing_hysteresis",
                "version": "1",
                "parameters": {
                    "buy_threshold_pct_per_day": 0.2,
                    "sell_threshold_abs_pct_per_day": 0.2,
                },
            },
            "sizing": {
                "name": "all_in_all_out_a_share_lots",
                "version": "1",
                "parameters": {"lot_size": 100, "target_fraction": 1.0},
            },
            "cost": {
                "name": "cms_china_a_share",
                "version": "1",
                "parameters": {
                    "commission_rate": 0.0003,
                    "minimum_commission_cny": 5.0,
                    "transfer_fee_rate": 0.00001,
                    "sell_stamp_tax_rate": 0.0005,
                    "buy_slippage_bps": 2.0,
                    "sell_slippage_bps": 2.0,
                },
            },
            "report": {
                "name": "concise_chinese_causal_trade",
                "version": "1",
                "parameters": {},
            },
        },
    }


def test_valid_config_is_canonical_and_hash_stable(tmp_path: Path):
    config = valid_config(tmp_path)
    first = validate_strategy_config(config)
    reordered = json.loads(json.dumps(config))
    reordered["operators"] = dict(reversed(reordered["operators"].items()))
    second = validate_strategy_config(reordered)

    assert first.canonical == second.canonical
    assert first.canonical_bytes == second.canonical_bytes
    assert first.config_sha256 == second.config_sha256
    assert json.loads(first.canonical_bytes)["operators"]["fit"]["parameters"] == {
        "price_column": "AdjustedClose",
        "window_sessions": 20,
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(extra=True), "unknown fields"),
        (lambda value: value.pop("dataset"), "missing fields"),
        (lambda value: value["dataset"].update(instrument="601328.SS"), "unknown fields"),
        (lambda value: value["template"].pop("version"), "missing fields"),
        (
            lambda value: value["template"]["parameters"].update(initial_capital_cny=True),
            "initial_capital_cny",
        ),
        (
            lambda value: value["operators"]["fit"]["parameters"].update(window_sessions=20.0),
            "window_sessions",
        ),
        (
            lambda value: value["operators"]["fit"]["parameters"].update(hidden_default=1),
            "unrecognized parameters",
        ),
        (
            lambda value: value["operators"]["report"]["parameters"].update(title="hidden"),
            "unrecognized parameters",
        ),
        (
            lambda value: value["operators"]["fit"]["parameters"].update(price_column="ClosePrice"),
            "price_column",
        ),
        (
            lambda value: value["template"]["parameters"].update(initial_state="long"),
            "initial_state",
        ),
        (
            lambda value: value["template"]["parameters"].update(
                evaluation_end="2024-12-31"
            ),
            "evaluation_end",
        ),
    ],
)
def test_config_fails_closed_on_schema_and_ownership_errors(
    tmp_path: Path, mutate, message: str
):
    config = valid_config(tmp_path)
    mutate(config)

    with pytest.raises(StrategyConfigError, match=message):
        validate_strategy_config(config)


@pytest.mark.parametrize(
    ("slot", "name", "version", "message"),
    [
        ("fit", "not_registered", "1", "unknown operator"),
        ("fit", "prior_log_ols", "2", "unknown operator"),
        ("fit", "recursive_log_ema", "1", "incompatible"),
    ],
)
def test_config_rejects_unknown_version_and_incompatible_slot(
    tmp_path: Path, slot: str, name: str, version: str, message: str
):
    config = valid_config(tmp_path)
    config["operators"][slot]["name"] = name
    config["operators"][slot]["version"] = version

    with pytest.raises(StrategyConfigError, match=message):
        validate_strategy_config(config)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_loader_rejects_non_finite_constants(tmp_path: Path, constant: str):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(valid_config(tmp_path)).replace("100000.0", constant),
        encoding="utf-8",
    )

    with pytest.raises(StrategyConfigError, match="non-finite"):
        load_strategy_config(path)


def test_json_loader_rejects_duplicate_keys_and_non_object_root(tmp_path: Path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")

    with pytest.raises(StrategyConfigError, match="duplicate"):
        load_strategy_config(duplicate)
    with pytest.raises(StrategyConfigError, match="JSON object"):
        load_strategy_config(array)


def test_registry_declarations_reject_parameter_name_collisions():
    registry = {
        ("fit", "one", "1"): OperatorSpec(
            slot="fit",
            name="one",
            version="1",
            parameters={"shared": ParameterSpec("integer")},
        ),
        ("smoothing", "two", "1"): OperatorSpec(
            slot="smoothing",
            name="two",
            version="1",
            parameters={"shared": ParameterSpec("number")},
        ),
    }

    with pytest.raises(ValueError, match="parameter name collision.*shared"):
        validate_registry(registry, set())


def test_validation_does_not_mutate_callers_config(tmp_path: Path):
    config = valid_config(tmp_path)
    before = copy.deepcopy(config)
    validate_strategy_config(config)
    assert config == before


def test_builtin_operator_registry_is_immutable():
    with pytest.raises(TypeError):
        OPERATOR_REGISTRY[("fit", "unsafe", "1")] = OperatorSpec(
            slot="fit",
            name="unsafe",
            version="1",
            parameters={},
        )
