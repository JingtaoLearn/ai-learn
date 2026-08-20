from __future__ import annotations

import numpy as np
import pandas as pd


def _validate_index(index: pd.Index) -> None:
    if not isinstance(index, pd.DatetimeIndex) or index.hasnans:
        raise ValueError("dates must be a valid DatetimeIndex")
    if index.has_duplicates:
        raise ValueError("dates must not contain duplicates")
    if not index.is_monotonic_increasing:
        raise ValueError("dates must be strictly increasing")


def _validated_frame(frame: pd.DataFrame) -> pd.DataFrame:
    required = ["High", "Low", "Close"]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"missing required OHLC columns: {', '.join(sorted(missing))}")
    _validate_index(frame.index)
    if frame.empty:
        raise ValueError("OHLC frame must not be empty")
    values = frame.loc[:, required]
    contains_boolean = values.map(lambda value: isinstance(value, (bool, np.bool_))).any().any()
    numeric = values.apply(pd.to_numeric, errors="coerce")
    if (
        contains_boolean
        or not np.isfinite(numeric.to_numpy(dtype=float)).all()
        or (numeric <= 0.0).any().any()
    ):
        raise ValueError("OHLC prices must be finite and strictly positive")
    numeric = numeric.astype(float)
    tolerance = numeric.abs().max(axis=1).clip(lower=1.0) * 1e-12
    if (numeric["High"] + tolerance < numeric["Low"]).any():
        raise ValueError("OHLC High must be greater than or equal to Low")
    if (
        (numeric["Close"] + tolerance < numeric["Low"])
        | (numeric["Close"] - tolerance > numeric["High"])
    ).any():
        raise ValueError("OHLC Close must be within the High-Low range")
    return numeric


def _validated_close(close: pd.Series) -> pd.Series:
    if not isinstance(close, pd.Series):
        raise ValueError("close must be a pandas Series")
    _validate_index(close.index)
    if close.empty:
        raise ValueError("close series must not be empty")
    contains_boolean = close.map(lambda value: isinstance(value, (bool, np.bool_))).any()
    numeric = pd.to_numeric(close, errors="coerce")
    if (
        contains_boolean
        or not np.isfinite(numeric.to_numpy(dtype=float)).all()
        or (numeric <= 0.0).any()
    ):
        raise ValueError("close prices must be finite and strictly positive")
    return numeric.astype(float)


def _next_open_exposure(state: pd.Series, name: str) -> pd.Series:
    return state.shift(1, fill_value=0.0).astype(float).rename(name)


def d55_20_close_signal(frame: pd.DataFrame) -> pd.Series:
    """Return next-open exposure for the frozen 55/20 close-confirmed Donchian rule."""
    prices = _validated_frame(frame)
    upper = prices["High"].shift(1).rolling(55, min_periods=55).max()
    lower = prices["Low"].shift(1).rolling(20, min_periods=20).min()
    state = pd.Series(0.0, index=prices.index)
    current = 0.0
    for position in range(len(prices)):
        close = prices["Close"].iloc[position]
        if current == 0.0 and pd.notna(upper.iloc[position]) and close > upper.iloc[position]:
            current = 1.0
        elif current == 1.0 and pd.notna(lower.iloc[position]) and close < lower.iloc[position]:
            current = 0.0
        state.iloc[position] = current
    return _next_open_exposure(state, "d55_20_close")


def ma_hys_1_200_b1_signal(close: pd.Series) -> pd.Series:
    """Return next-open exposure for the frozen 1/200 moving-average hysteresis rule."""
    prices = _validated_close(close)
    average = prices.rolling(200, min_periods=200).mean()
    state = pd.Series(0.0, index=prices.index)
    current = 0.0
    for position in range(len(prices)):
        if pd.isna(average.iloc[position]):
            state.iloc[position] = current
            continue
        price = prices.iloc[position]
        if current == 0.0 and price > 1.01 * average.iloc[position]:
            current = 1.0
        elif current == 1.0 and price < 0.99 * average.iloc[position]:
            current = 0.0
        state.iloc[position] = current
    return _next_open_exposure(state, "ma_hys_1_200_b1")


def mom_12m_monthly_signal(close: pd.Series) -> pd.Series:
    """Return exposure updated at each first session from the prior month-end close."""
    prices = _validated_close(close)
    exposure = pd.Series(0.0, index=prices.index, name="mom_12m_monthly")
    month_ends: dict[pd.Period, float] = {}
    current = 0.0
    periods = prices.index.to_period("M")
    for position in range(1, len(prices)):
        if periods[position] != periods[position - 1]:
            completed_month = periods[position - 1]
            comparison_month = completed_month - 12
            month_ends[completed_month] = float(prices.iloc[position - 1])
            if len(month_ends) >= 13:
                scored_months = pd.period_range(comparison_month, completed_month, freq="M")
                if not all(month in month_ends for month in scored_months):
                    raise ValueError("monthly momentum requires every calendar month")
                if month_ends[completed_month] > month_ends[comparison_month]:
                    current = 1.0
                elif month_ends[completed_month] < month_ends[comparison_month]:
                    current = 0.0
        exposure.iloc[position] = current
    return exposure


def _dmi_adx_14(frame: pd.DataFrame) -> pd.DataFrame:
    prices = _validated_frame(frame)
    period = 14
    size = len(prices)
    high = prices["High"].to_numpy()
    low = prices["Low"].to_numpy()
    close = prices["Close"].to_numpy()
    tr = np.full(size, np.nan)
    plus_dm = np.full(size, np.nan)
    minus_dm = np.full(size, np.nan)
    for position in range(1, size):
        tr[position] = max(
            high[position] - low[position],
            abs(high[position] - close[position - 1]),
            abs(low[position] - close[position - 1]),
        )
        up = high[position] - high[position - 1]
        down = low[position - 1] - low[position]
        plus_dm[position] = up if up > down and up > 0.0 else 0.0
        minus_dm[position] = down if down > up and down > 0.0 else 0.0

    smooth_tr = np.full(size, np.nan)
    smooth_plus = np.full(size, np.nan)
    smooth_minus = np.full(size, np.nan)
    if size > period:
        seed_tr = tr[1:period].sum()
        seed_plus = plus_dm[1:period].sum()
        seed_minus = minus_dm[1:period].sum()
        smooth_tr[period] = seed_tr - seed_tr / period + tr[period]
        smooth_plus[period] = seed_plus - seed_plus / period + plus_dm[period]
        smooth_minus[period] = seed_minus - seed_minus / period + minus_dm[period]
        for position in range(period + 1, size):
            smooth_tr[position] = (
                smooth_tr[position - 1] - smooth_tr[position - 1] / period + tr[position]
            )
            smooth_plus[position] = (
                smooth_plus[position - 1] - smooth_plus[position - 1] / period + plus_dm[position]
            )
            smooth_minus[position] = (
                smooth_minus[position - 1]
                - smooth_minus[position - 1] / period
                + minus_dm[position]
            )
        if (smooth_tr[period:] <= 0.0).any():
            raise ValueError("DMI true-range denominator must be positive")

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * smooth_plus / smooth_tr
        minus_di = 100.0 * smooth_minus / smooth_tr
        direction_total = plus_di + minus_di
        dx = 100.0 * np.abs(plus_di - minus_di) / direction_total
    if size > period and (
        (direction_total[period:] <= 0.0).any() or not np.isfinite(dx[period:]).all()
    ):
        raise ValueError("DMI directional denominator must be positive")

    adx = np.full(size, np.nan)
    first_adx = 2 * period - 1
    if size > first_adx:
        adx[first_adx] = dx[period : first_adx + 1].mean()
        for position in range(first_adx + 1, size):
            adx[position] = ((period - 1) * adx[position - 1] + dx[position]) / period
    return pd.DataFrame({"+DI": plus_di, "-DI": minus_di, "DX": dx, "ADX": adx}, index=prices.index)


def _dmi_state(plus_di: pd.Series, minus_di: pd.Series, adx: pd.Series) -> pd.Series:
    state = pd.Series(0.0, index=adx.index)
    current = 0.0
    for position in range(len(adx)):
        if pd.isna(adx.iloc[position]):
            state.iloc[position] = current
            continue
        if (
            current == 0.0
            and plus_di.iloc[position] > minus_di.iloc[position]
            and adx.iloc[position] > 25.0
        ):
            current = 1.0
        elif current == 1.0 and (
            plus_di.iloc[position] <= minus_di.iloc[position] or adx.iloc[position] < 20.0
        ):
            current = 0.0
        state.iloc[position] = current
    return state


def dmi_adx_14_25_20_signal(frame: pd.DataFrame) -> pd.Series:
    """Return next-open exposure for the frozen Wilder DMI/ADX rule."""
    indicators = _dmi_adx_14(frame)
    state = _dmi_state(indicators["+DI"], indicators["-DI"], indicators["ADX"])
    return _next_open_exposure(state, "dmi_adx_14_25_20")


def core50_old20_10_signal(frame: pd.DataFrame) -> pd.Series:
    """Return the diagnostic 50% core plus old 20/10 overlay next-open weight."""
    prices = _validated_frame(frame)
    upper = prices["High"].shift(1).rolling(20, min_periods=20).max()
    lower = prices["Low"].shift(1).rolling(10, min_periods=10).min()
    state = pd.Series(0.0, index=prices.index)
    current = 0.0
    for position in range(len(prices)):
        close = prices["Close"].iloc[position]
        if current == 0.0 and pd.notna(upper.iloc[position]) and close > upper.iloc[position]:
            current = 1.0
        elif current == 1.0 and pd.notna(lower.iloc[position]) and close < lower.iloc[position]:
            current = 0.0
        state.iloc[position] = current
    exposure = _next_open_exposure(state, "core50_old20_10")
    return 0.5 + 0.5 * exposure


def _turtle_n_20(prices: pd.DataFrame) -> pd.Series:
    size = len(prices)
    high = prices["High"].to_numpy()
    low = prices["Low"].to_numpy()
    close = prices["Close"].to_numpy()
    tr = np.empty(size)
    if size:
        tr[0] = high[0] - low[0]
    for position in range(1, size):
        tr[position] = max(
            high[position] - low[position],
            abs(high[position] - close[position - 1]),
            abs(low[position] - close[position - 1]),
        )
    result = np.full(size, np.nan)
    if size >= 20:
        result[19] = tr[:20].mean()
        for position in range(20, size):
            result[position] = (19.0 * result[position - 1] + tr[position]) / 20.0
        if (result[19:] <= 0.0).any() or not np.isfinite(result[19:]).all():
            raise ValueError("Turtle N must be finite and strictly positive")
    return pd.Series(result, index=prices.index, name="turtle_n_20")


def turtle_n_entry_weight(frame: pd.DataFrame, binary_signal: pd.Series) -> pd.Series:
    """Freeze signal-close risk sizing for each next-open holding segment.

    The frozen candidate formula is ``min(1, 0.01 * C_t / N_t)`` at the signal close.
    An opening gap can therefore make exact realized next-open risk differ from this weight.
    """
    prices = _validated_frame(frame)
    if not isinstance(binary_signal, pd.Series) or not binary_signal.index.equals(prices.index):
        raise ValueError("binary signal index must exactly match the OHLC frame")
    signal = pd.to_numeric(binary_signal, errors="coerce")
    if not np.isfinite(signal.to_numpy(dtype=float)).all() or not signal.isin([0.0, 1.0]).all():
        raise ValueError("binary signal must contain only finite 0 or 1 values")
    n_value = _turtle_n_20(prices)
    weight = pd.Series(0.0, index=prices.index, name="turtle_n_size_d55_20_close")
    frozen = 0.0
    previous = 0.0
    for position in range(len(prices)):
        current = float(signal.iloc[position])
        if current == 1.0 and previous == 0.0:
            metric_position = position - 1
            if metric_position < 0 or pd.isna(n_value.iloc[metric_position]):
                raise ValueError("Turtle N is unavailable at the entry signal close")
            frozen = min(
                1.0,
                0.01 * prices["Close"].iloc[metric_position] / n_value.iloc[metric_position],
            )
        elif current == 0.0:
            frozen = 0.0
        weight.iloc[position] = frozen
        previous = current
    if not weight.between(0.0, 1.0).all():
        raise ValueError("Turtle entry weight must remain between zero and one")
    return weight


def abc_round2_signals(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """Return exactly the four eligible frozen Round-2 candidates in declared order."""
    return {
        "d55_20_close": d55_20_close_signal(frame),
        "ma_hys_1_200_b1": ma_hys_1_200_b1_signal(frame["Close"]),
        "mom_12m_monthly": mom_12m_monthly_signal(frame["Close"]),
        "dmi_adx_14_25_20": dmi_adx_14_25_20_signal(frame),
    }


def abc_round2_diagnostics(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """Return frozen causal diagnostics, separate from eligible candidates."""
    breakout = d55_20_close_signal(frame)
    return {
        "core50_old20_10": core50_old20_10_signal(frame),
        "turtle_n_size_d55_20_close": turtle_n_entry_weight(frame, breakout),
    }
