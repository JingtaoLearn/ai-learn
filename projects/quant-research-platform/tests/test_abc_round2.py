import numpy as np
import pandas as pd
import pytest

from gold_research.abc_round2 import (
    _dmi_adx_14,
    _dmi_state,
    abc_round2_diagnostics,
    abc_round2_signals,
    core50_old20_10_signal,
    d55_20_close_signal,
    dmi_adx_14_25_20_signal,
    ma_hys_1_200_b1_signal,
    mom_12m_monthly_signal,
    turtle_n_entry_weight,
)


def ohlc_frame(close, *, high=None, low=None, start="2020-01-02"):
    close = np.asarray(close, dtype=float)
    if high is None:
        high = close + 0.5
    if low is None:
        low = close - 0.5
    return pd.DataFrame(
        {
            "High": np.asarray(high, dtype=float),
            "Low": np.asarray(low, dtype=float),
            "Close": close,
        },
        index=pd.bdate_range(start, periods=len(close)),
    )


def test_d55_20_uses_prior_channels_strict_comparisons_and_next_open_timing():
    close = np.full(61, 9.0)
    high = np.full(61, 10.0)
    low = np.full(61, 5.0)
    close[55] = 10.0
    close[56] = 10.1
    high[56] = 10.2
    close[57] = 6.0
    close[58] = 5.0
    close[59] = 4.9
    low[59] = 4.8
    signal = d55_20_close_signal(ohlc_frame(close, high=high, low=low))

    assert signal.iloc[:57].eq(0.0).all()
    assert signal.iloc[57:60].eq(1.0).all()
    assert signal.iloc[60] == 0.0


def test_d55_20_is_prefix_stable_and_ignores_current_high_in_entry_channel():
    close = np.linspace(10.0, 30.0, 90)
    frame = ohlc_frame(close, high=close + 0.1, low=close - 0.1)
    frame.iloc[55, frame.columns.get_loc("High")] = 1_000.0

    full = d55_20_close_signal(frame)
    prefix = d55_20_close_signal(frame.iloc[:70])

    pd.testing.assert_series_equal(full.iloc[:70], prefix)
    assert full.iloc[56] == 1.0


def test_ma_hysteresis_observes_warmup_strict_bands_and_next_open_timing():
    close = np.full(205, 100.0)
    entry_equal = 1.01 * close[:199].sum() / (200.0 - 1.01)
    close[199] = entry_equal
    close[200] = 103.0
    exit_equal = 0.99 * close[3:202].sum() / (200.0 - 0.99)
    close[202] = exit_equal
    close[203] = 90.0
    series = pd.Series(close, index=pd.bdate_range("2020-01-02", periods=len(close)))

    signal = ma_hys_1_200_b1_signal(series)

    assert close[199] == pytest.approx(1.01 * series.iloc[:200].mean())
    assert signal.iloc[:201].eq(0.0).all()
    assert signal.iloc[201:204].eq(1.0).all()
    assert close[202] == pytest.approx(0.99 * series.iloc[3:203].mean())
    assert signal.iloc[204] == 0.0


def test_ma_hysteresis_is_prefix_stable():
    close = pd.Series(
        np.linspace(10.0, 20.0, 260),
        index=pd.bdate_range("2020-01-02", periods=260),
    )
    full = ma_hys_1_200_b1_signal(close)
    pd.testing.assert_series_equal(full.iloc[:230], ma_hys_1_200_b1_signal(close.iloc[:230]))


def monthly_fixture() -> pd.Series:
    dates = []
    values = []
    month_ends = {
        pd.Period("2020-01", freq="M"): 110.0,
        pd.Period("2020-02", freq="M"): 100.0,
        pd.Period("2020-03", freq="M"): 90.0,
    }
    for month in pd.period_range("2019-01", "2020-04", freq="M"):
        first = month.start_time + pd.offsets.BMonthBegin(0)
        last = pd.offsets.BMonthEnd().rollback(month.end_time).normalize()
        dates.extend([first, last])
        values.extend(
            [
                500.0 if month == pd.Period("2020-03", freq="M") else 100.0,
                month_ends.get(month, 100.0),
            ]
        )
    return pd.Series(values, index=pd.DatetimeIndex(dates), dtype=float)


def test_monthly_momentum_uses_twelve_month_ends_and_changes_only_next_month():
    close = monthly_fixture()
    signal = mom_12m_monthly_signal(close)

    february_first = close.index[close.index.to_period("M") == pd.Period("2020-02", freq="M")][0]
    april_first = close.index[close.index.to_period("M") == pd.Period("2020-04", freq="M")][0]
    assert signal.loc[: pd.Timestamp("2020-01-31")].eq(0.0).all()
    assert signal.loc[february_first : april_first - pd.Timedelta(days=1)].eq(1.0).all()
    assert signal.loc[april_first] == 0.0


def test_monthly_momentum_equality_retains_state_and_is_prefix_stable():
    close = monthly_fixture()
    full = mom_12m_monthly_signal(close)
    march_rows = close.index.to_period("M") == pd.Period("2020-03", freq="M")

    assert full.loc[close.index[march_rows][0]] == 1.0
    prefix = close.iloc[:-1]
    pd.testing.assert_series_equal(full.iloc[:-1], mom_12m_monthly_signal(prefix))


def test_monthly_momentum_fails_closed_when_a_calendar_month_is_missing():
    dates = []
    values = []
    for month in pd.period_range("2018-12", "2020-02", freq="M"):
        if month == pd.Period("2019-07", freq="M"):
            continue
        first = month.start_time + pd.offsets.BMonthBegin(0)
        last = pd.offsets.BMonthEnd().rollback(month.end_time).normalize()
        dates.extend([first, last])
        month_end = 90.0 if month == pd.Period("2018-12", freq="M") else 100.0
        if month == pd.Period("2020-01", freq="M"):
            month_end = 110.0
        values.extend([100.0, month_end])
    close = pd.Series(values, index=pd.DatetimeIndex(dates), dtype=float)

    signal = mom_12m_monthly_signal(close)

    assert signal.eq(0.0).all()


def dmi_fixture(periods: int = 70) -> pd.DataFrame:
    moves = np.resize(np.array([1.2, 0.8, -0.3, 1.5, -0.6, 0.4]), periods)
    close = 100.0 + np.cumsum(moves)
    width = np.resize(np.array([0.7, 1.1, 0.9, 1.3]), periods)
    return ohlc_frame(close, high=close + width, low=close - width)


def test_dmi_adx_matches_talib_0_6_7_points_and_exact_wilder_warmup():
    frame = dmi_fixture()
    actual = _dmi_adx_14(frame)

    assert actual["ADX"].iloc[:27].isna().all()
    assert actual["ADX"].iloc[27:].notna().all()
    assert actual.iloc[14]["+DI"] == pytest.approx(35.88541666666655, rel=1e-12, abs=1e-12)
    assert actual.iloc[14]["-DI"] == pytest.approx(2.057291666666842, rel=1e-12, abs=1e-12)
    assert actual.iloc[14]["DX"] == pytest.approx(89.15579958819401, rel=1e-12, abs=1e-12)
    assert np.isnan(actual.iloc[14]["ADX"])
    assert actual.iloc[27]["+DI"] == pytest.approx(38.55678552886503, rel=1e-12, abs=1e-12)
    assert actual.iloc[27]["-DI"] == pytest.approx(1.9898397363845517, rel=1e-12, abs=1e-12)
    assert actual.iloc[27]["DX"] == pytest.approx(90.18493044307714, rel=1e-12, abs=1e-12)
    assert actual.iloc[27]["ADX"] == pytest.approx(90.63333449919035, rel=1e-12, abs=1e-12)
    assert actual.iloc[28]["ADX"] == pytest.approx(90.6013056380394, rel=1e-12, abs=1e-12)
    assert actual.iloc[40]["ADX"] == pytest.approx(90.21533860650113, rel=1e-12, abs=1e-12)
    assert actual.iloc[69]["ADX"] == pytest.approx(90.4833624970207, rel=1e-12, abs=1e-12)


def test_dmi_adx_signal_enters_only_after_completed_first_adx_close():
    close = np.arange(100.0, 140.0)
    frame = ohlc_frame(close, high=close + 0.5, low=close - 0.5)
    signal = dmi_adx_14_25_20_signal(frame)

    assert signal.iloc[:28].eq(0.0).all()
    assert signal.iloc[28:].eq(1.0).all()


def test_dmi_state_retains_adx_threshold_equality_but_exits_on_direction_equality():
    index = pd.bdate_range("2024-01-02", periods=4)
    plus_di = pd.Series([30.0, 30.0, 30.0, 10.0], index=index)
    minus_di = pd.Series([10.0, 10.0, 10.0, 10.0], index=index)
    adx = pd.Series([25.0, 26.0, 20.0, 20.0], index=index)

    state = _dmi_state(plus_di, minus_di, adx)

    assert state.tolist() == [0.0, 1.0, 1.0, 0.0]


@pytest.mark.parametrize("bad", [0.0, np.nan, np.inf])
def test_dmi_adx_rejects_invalid_ohlc(bad):
    frame = dmi_fixture()
    frame.iloc[3, frame.columns.get_loc("High")] = bad
    with pytest.raises(ValueError, match="finite and strictly positive"):
        dmi_adx_14_25_20_signal(frame)


def test_signal_validation_rejects_empty_ohlc_and_close_inputs():
    empty_index = pd.DatetimeIndex([])
    empty_frame = pd.DataFrame(columns=["High", "Low", "Close"], index=empty_index)
    empty_close = pd.Series(index=empty_index, dtype=float)

    with pytest.raises(ValueError, match="must not be empty"):
        d55_20_close_signal(empty_frame)
    with pytest.raises(ValueError, match="must not be empty"):
        ma_hys_1_200_b1_signal(empty_close)


@pytest.mark.parametrize(
    ("high", "low", "close"),
    [
        (9.0, 10.0, 9.5),
        (10.0, 9.0, 10.1),
        (10.0, 9.0, 8.9),
    ],
)
def test_signal_validation_rejects_impossible_ohlc_bars(high, low, close):
    frame = ohlc_frame([close], high=[high], low=[low])

    with pytest.raises(ValueError, match="High.*Low|Close.*range"):
        d55_20_close_signal(frame)


def test_dmi_adx_rejects_duplicate_and_non_monotonic_dates():
    duplicate = dmi_fixture()
    duplicate.index = duplicate.index.where(
        duplicate.index != duplicate.index[3], duplicate.index[2]
    )
    with pytest.raises(ValueError, match="duplicates"):
        dmi_adx_14_25_20_signal(duplicate)

    reversed_frame = dmi_fixture().iloc[::-1]
    with pytest.raises(ValueError, match="strictly increasing"):
        dmi_adx_14_25_20_signal(reversed_frame)


def test_dmi_adx_signal_is_prefix_stable():
    frame = dmi_fixture()
    full = dmi_adx_14_25_20_signal(frame)
    pd.testing.assert_series_equal(full.iloc[:55], dmi_adx_14_25_20_signal(frame.iloc[:55]))


def test_core50_old20_10_maps_off_on_to_half_and_full_with_next_open_delay():
    close = np.full(26, 9.0)
    high = np.full(26, 10.0)
    low = np.full(26, 5.0)
    close[20] = 10.0
    close[21] = 10.1
    high[21] = 10.2
    close[22] = 6.0
    close[23] = 5.0
    close[24] = 4.9
    low[24] = 4.8

    weight = core50_old20_10_signal(ohlc_frame(close, high=high, low=low))

    assert weight.iloc[:22].eq(0.5).all()
    assert weight.iloc[22:25].eq(1.0).all()
    assert weight.iloc[25] == 0.5


def reference_turtle_n(frame: pd.DataFrame) -> pd.Series:
    high = frame["High"].to_numpy(dtype=float)
    low = frame["Low"].to_numpy(dtype=float)
    close = frame["Close"].to_numpy(dtype=float)
    tr = np.empty(len(frame))
    tr[0] = high[0] - low[0]
    for position in range(1, len(frame)):
        tr[position] = max(
            high[position] - low[position],
            abs(high[position] - close[position - 1]),
            abs(low[position] - close[position - 1]),
        )
    result = np.full(len(frame), np.nan)
    result[19] = tr[:20].mean()
    for position in range(20, len(frame)):
        result[position] = (19.0 * result[position - 1] + tr[position]) / 20.0
    return pd.Series(result, index=frame.index)


def test_turtle_n_weight_uses_prior_signal_close_and_freezes_each_holding_segment():
    close = np.full(35, 100.0)
    width = np.linspace(0.5, 5.0, len(close))
    frame = ohlc_frame(close, high=close + width, low=close - width)
    binary = pd.Series(0.0, index=frame.index)
    binary.iloc[21:27] = 1.0
    binary.iloc[28:33] = 1.0
    n_value = reference_turtle_n(frame)

    weight = turtle_n_entry_weight(frame, binary)

    first = min(1.0, 0.01 * close[20] / n_value.iloc[20])
    second = min(1.0, 0.01 * close[27] / n_value.iloc[27])
    assert weight.iloc[21:27].eq(first).all()
    assert weight.iloc[27] == 0.0
    assert weight.iloc[28:33].eq(second).all()
    assert first != second
    assert weight.between(0.0, 1.0).all()


def test_turtle_n_weight_rejects_nonbinary_or_misaligned_signal():
    frame = dmi_fixture(40)
    nonbinary = pd.Series(0.0, index=frame.index)
    nonbinary.iloc[25] = 1.2
    with pytest.raises(ValueError, match="binary"):
        turtle_n_entry_weight(frame, nonbinary)

    with pytest.raises(ValueError, match="index"):
        turtle_n_entry_weight(frame, nonbinary.reset_index(drop=True))


def test_round2_registry_contains_exactly_four_eligible_candidates_in_frozen_order():
    frame = dmi_fixture(320)

    candidates = abc_round2_signals(frame)
    diagnostics = abc_round2_diagnostics(frame)

    assert list(candidates) == [
        "d55_20_close",
        "ma_hys_1_200_b1",
        "mom_12m_monthly",
        "dmi_adx_14_25_20",
    ]
    assert len(candidates) == 4
    assert list(diagnostics) == ["core50_old20_10", "turtle_n_size_d55_20_close"]
    assert set(candidates).isdisjoint(diagnostics)
    assert all(signal.index.equals(frame.index) for signal in candidates.values())
