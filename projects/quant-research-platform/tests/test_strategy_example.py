from pathlib import Path

from quant_platform.strategy_config import load_strategy_config


EXAMPLE = (
    Path(__file__).parents[1]
    / "examples"
    / "bocom-causal-slope.json"
)


def test_bocom_example_is_valid_and_freezes_owned_strategy_parameters():
    config = load_strategy_config(EXAMPLE).canonical
    template = config["template"]["parameters"]
    operators = config["operators"]

    assert template["instrument_display_name"] == "Bank of Communications (601328.SS)"
    assert template["evaluation_start"] == "2025-01-02"
    assert template["evaluation_end"] is None
    assert template["initial_capital_cny"] == 100000.0
    assert template["initial_state"] == "flat"
    assert template["terminal_handling"] == "mark_to_market"
    assert "conservative research assumption" in template[
        "cost_assumption_label"
    ].lower()
    assert "not an account-specific rate" in template[
        "cost_assumption_label"
    ].lower()

    assert operators["fit"]["parameters"] == {
        "window_sessions": 20,
        "price_column": "AdjustedClose",
    }
    assert operators["smoothing"]["parameters"] == {"span_sessions": 5}
    assert operators["decision"]["parameters"] == {
        "buy_threshold_pct_per_day": 0.2,
        "sell_threshold_abs_pct_per_day": 0.2,
    }
    assert operators["sizing"]["parameters"] == {
        "lot_size": 100,
        "target_fraction": 1.0,
    }
    assert operators["report"]["parameters"] == {}
