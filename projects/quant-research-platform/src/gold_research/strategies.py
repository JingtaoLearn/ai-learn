from __future__ import annotations

import math

import pandas as pd


def buy_hold_signal(close: pd.Series) -> pd.Series:
    return pd.Series(1.0, index=close.index, name="signal")


def sma_signal(close: pd.Series, fast: int = 50, slow: int = 200) -> pd.Series:
    raw = (close.rolling(fast).mean() > close.rolling(slow).mean()).astype(float)
    return raw.shift(1).fillna(0.0)


def donchian_signal(close: pd.Series, entry: int = 55, exit_: int = 20) -> pd.Series:
    upper = close.shift(1).rolling(entry).max()
    lower = close.shift(1).rolling(exit_).min()
    state = 0.0
    raw = []
    for price, high, low in zip(close, upper, lower, strict=True):
        if pd.notna(high) and price > high:
            state = 1.0
        elif pd.notna(low) and price < low:
            state = 0.0
        raw.append(state)
    return pd.Series(raw, index=close.index, name="signal").shift(1).fillna(0.0)


def trend_filter_signal(close: pd.Series, window: int = 200) -> pd.Series:
    """Hold gold when the latest close is above its long moving average."""
    raw = (close > close.rolling(window).mean()).astype(float)
    return raw.shift(1).fillna(0.0).rename("signal")


def absolute_momentum_signal(close: pd.Series, lookback: int = 252) -> pd.Series:
    """Hold gold when its trailing return over ``lookback`` sessions is positive."""
    raw = (close > close.shift(lookback)).astype(float)
    return raw.shift(1).fillna(0.0).rename("signal")


def multi_horizon_momentum_signal(
    close: pd.Series,
    lookbacks: tuple[int, ...] = (63, 126, 252),
    votes_required: int = 2,
) -> pd.Series:
    """Hold gold when a majority of pre-registered momentum horizons are positive."""
    if not lookbacks or not 1 <= votes_required <= len(lookbacks):
        raise ValueError("votes_required must be between one and the number of lookbacks")
    votes = sum((close > close.shift(lookback)).astype(int) for lookback in lookbacks)
    raw = (votes >= votes_required).astype(float)
    return raw.shift(1).fillna(0.0).rename("signal")


def risk_managed_trend_signal(
    close: pd.Series,
    trend_window: int = 200,
    volatility_window: int = 60,
    target_volatility: float = 0.10,
) -> pd.Series:
    """Scale a long-only trend position down when trailing volatility is high."""
    if target_volatility <= 0:
        raise ValueError("target_volatility must be positive")
    trend = (close > close.rolling(trend_window).mean()).astype(float)
    realized = close.pct_change().rolling(volatility_window).std(ddof=0) * math.sqrt(252)
    scale = target_volatility / realized.replace(0.0, pd.NA)
    raw = trend * scale.astype(float).clip(lower=0.0, upper=1.0).fillna(0.0)
    return raw.shift(1).fillna(0.0).rename("signal")


def strategy_signals(close: pd.Series) -> dict[str, pd.Series]:
    return {
        "buy_and_hold": buy_hold_signal(close),
        "sma_50_200": sma_signal(close, 50, 200),
        "donchian_55_20": donchian_signal(close, 55, 20),
    }
