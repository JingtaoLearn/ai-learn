from urllib.parse import quote

import pandas as pd


def yahoo_chart_url(symbol: str, start: str, end: str) -> str:
    period1 = int(pd.Timestamp(start, tz="UTC").timestamp())
    period2 = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp())
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        f"?period1={period1}&period2={period2}&interval=1d&events=history"
    )
