from __future__ import annotations

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


def strategy_signals(close: pd.Series) -> dict[str, pd.Series]:
    return {
        "buy_and_hold": buy_hold_signal(close),
        "sma_50_200": sma_signal(close, 50, 200),
        "donchian_55_20": donchian_signal(close, 55, 20),
    }
