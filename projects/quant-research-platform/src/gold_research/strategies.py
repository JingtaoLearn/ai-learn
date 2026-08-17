from __future__ import annotations

import math

import numpy as np
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


def trend_temperature_score(close: pd.Series, lookback: int = 63) -> pd.Series:
    """Return volatility-normalized log momentum over a frozen lookback."""
    if lookback <= 1:
        raise ValueError("lookback must be greater than one")
    close = close.astype(float)
    log_return = np.log(close.div(close.shift(1)))
    momentum = np.log(close.div(close.shift(lookback)))
    realized = log_return.rolling(lookback).std(ddof=0) * math.sqrt(lookback)
    return (
        momentum.div(realized.replace(0.0, np.nan))
        .replace([np.inf, -np.inf], np.nan)
        .rename("temperature_score")
    )


def trend_temperature(
    close: pd.Series | None = None,
    *,
    score: pd.Series | None = None,
    lookback: int = 63,
    cold_threshold: float = -0.5,
    warm_threshold: float = 0.5,
    hot_threshold: float = 1.0,
) -> pd.Series:
    """Classify a transparent trend score into cold, flat, warm, and hot states."""
    if not cold_threshold < warm_threshold < hot_threshold:
        raise ValueError("temperature thresholds must be strictly increasing")
    if score is None:
        if close is None:
            raise ValueError("close or score is required")
        score = trend_temperature_score(close, lookback)
    assert score is not None
    values = pd.Series(pd.NA, index=score.index, dtype="string", name="state")
    valid = score.notna()
    values.loc[valid & (score < cold_threshold)] = "cold"
    values.loc[valid & (score >= cold_threshold) & (score < warm_threshold)] = "flat"
    values.loc[valid & (score >= warm_threshold) & (score < hot_threshold)] = "warm"
    values.loc[valid & (score >= hot_threshold)] = "hot"
    return values


def trend_temperature_signal(
    close: pd.Series,
    lookback: int = 63,
    entry_threshold: float = 1.0,
    exit_threshold: float = 0.5,
) -> pd.Series:
    """Enter when trend is hot and exit after it cools to flat, with next-open delay."""
    if entry_threshold <= exit_threshold:
        raise ValueError("entry_threshold must exceed exit_threshold")
    score = trend_temperature_score(close, lookback)
    held = 0.0
    raw: list[float] = []
    for value in score:
        if pd.notna(value):
            if not held and value >= entry_threshold:
                held = 1.0
            elif held and value < exit_threshold:
                held = 0.0
        raw.append(held)
    return pd.Series(raw, index=close.index, name="signal").shift(1).fillna(0.0)


def risk_managed_trend_temperature_signal(
    close: pd.Series,
    lookback: int = 63,
    entry_threshold: float = 1.0,
    exit_threshold: float = 0.5,
    volatility_window: int = 60,
    target_volatility: float = 0.10,
) -> pd.Series:
    """Apply a capped volatility target to the delayed temperature state."""
    if volatility_window <= 1:
        raise ValueError("volatility_window must be greater than one")
    if target_volatility <= 0:
        raise ValueError("target_volatility must be positive")
    regime = trend_temperature_signal(close, lookback, entry_threshold, exit_threshold)
    log_return = np.log(close.astype(float).div(close.astype(float).shift(1)))
    realized = log_return.rolling(volatility_window).std(ddof=0) * math.sqrt(252)
    scale = target_volatility / realized.shift(1).replace(0.0, np.nan)
    return (regime * scale.clip(lower=0.0, upper=1.0).fillna(0.0)).rename("signal")


def strategy_signals(close: pd.Series) -> dict[str, pd.Series]:
    return {
        "buy_and_hold": buy_hold_signal(close),
        "sma_50_200": sma_signal(close, 50, 200),
        "donchian_55_20": donchian_signal(close, 55, 20),
    }
