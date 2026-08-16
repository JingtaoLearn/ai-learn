from __future__ import annotations

import math

import numpy as np
import pandas as pd


def backtest(open_price: pd.Series, signal: pd.Series, cost_bps: float = 5.0) -> pd.DataFrame:
    """Backtest positions entered at each session's open.

    ``signal[t]`` must be knowable before ``open[t]``. The position then earns the
    open[t]-to-open[t+1] return. This makes a close[t-1] signal executable at the
    next available daily open without a same-close fill assumption.
    """
    open_price = open_price.astype(float).dropna().rename("open")
    signal = signal.reindex(open_price.index).fillna(0.0).clip(0.0, 1.0).rename("signal")
    forward_return = open_price.shift(-1).div(open_price).sub(1.0).fillna(0.0)
    gross_return = signal * forward_return
    turnover = signal.diff().abs()
    if not turnover.empty:
        turnover.iloc[0] = abs(signal.iloc[0])
    turnover = turnover.fillna(0.0)
    cost = turnover * cost_bps / 10_000.0
    net_return = gross_return - cost
    return pd.DataFrame(
        {
            "open": open_price,
            "signal": signal,
            "asset_forward_return": forward_return,
            "gross_return": gross_return,
            "turnover": turnover,
            "cost": cost,
            "net_return": net_return,
            "equity_gross": (1.0 + gross_return).cumprod(),
            "equity_net": (1.0 + net_return).cumprod(),
        },
        index=open_price.index,
    )


def trade_ledger(result: pd.DataFrame) -> pd.DataFrame:
    held = result["signal"].astype(float) > 0.0
    previously_held = held.shift(1, fill_value=False)
    entries = list(np.flatnonzero((held & ~previously_held).to_numpy()))
    exits = list(np.flatnonzero((~held & previously_held).to_numpy()))
    records: list[dict] = []
    for entry in entries:
        exit_candidates = [candidate for candidate in exits if candidate > entry]
        is_open = not exit_candidates
        exit_idx = exit_candidates[0] if exit_candidates else None
        window = result.iloc[entry : (exit_idx + 1 if exit_idx is not None else len(result))]
        held_end = exit_idx if exit_idx is not None else max(entry, len(result) - 1)
        held = result.iloc[entry:held_end]
        records.append(
            {
                "entry_date": result.index[entry],
                "entry_price": float(result["open"].iloc[entry]),
                "exit_date": pd.NaT if is_open else result.index[exit_idx],
                "exit_price": np.nan if is_open else float(result["open"].iloc[exit_idx]),
                "bars": int((held["signal"] > 0).sum()),
                "net_return": float((1.0 + window["net_return"]).prod() - 1.0),
                "is_open": is_open,
            }
        )
    return pd.DataFrame(records)


def metrics(result: pd.DataFrame) -> dict[str, float | int]:
    returns = result["net_return"].astype(float)
    equity = result["equity_net"].astype(float)
    years = max(
        (equity.index[-1] - equity.index[0]).days / 365.2425,
        max(len(equity) - 1, 1) / 252.0,
    )
    cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 and equity.iloc[-1] > 0 else -1.0
    peak = equity.cummax().clip(lower=1.0)
    drawdown = equity / peak - 1.0
    std = float(returns.std(ddof=0))
    sharpe = float(returns.mean() / std * math.sqrt(252)) if std > 0 else 0.0
    downside_deviation = float(np.sqrt(np.mean(np.minimum(returns.to_numpy(), 0.0) ** 2)))
    sortino = float(returns.mean() / downside_deviation * math.sqrt(252)) if downside_deviation > 0 else 0.0
    trades = trade_ledger(result)
    if trades.empty:
        closed = trades
        open_count = 0
    else:
        closed = trades.loc[~trades["is_open"].astype(bool)]
        open_count = int(trades["is_open"].astype(bool).sum())
    total_return = float(equity.iloc[-1] - 1.0)
    max_drawdown = float(drawdown.min())
    exposure = float(result["signal"].mean())
    turnover = float(result["turnover"].sum()) if "turnover" in result else 0.0
    cost_paid = float(result["cost"].sum()) if "cost" in result else 0.0
    return {
        "total_return": total_return,
        "cumulative_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "annual_volatility": float(std * math.sqrt(252)),
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": float(cagr / abs(max_drawdown)) if max_drawdown < 0 else 0.0,
        "trade_count": int(len(closed)),
        "open_trade_count": open_count,
        "win_rate": float((closed["net_return"] > 0).mean()) if not closed.empty else 0.0,
        "exposure": exposure,
        "market_exposure": exposure,
        "turnover": turnover,
        "cost_paid_return_points": cost_paid,
    }


def chronological_split(index: pd.Index, train_fraction: float = 0.7) -> tuple[pd.Index, pd.Index]:
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    cut = int(len(index) * train_fraction)
    if cut < 1 or cut >= len(index):
        raise ValueError("not enough rows for chronological split")
    return index[:cut], index[cut:]
