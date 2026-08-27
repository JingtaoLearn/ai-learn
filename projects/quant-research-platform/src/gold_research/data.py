from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from quant_platform.yahoo import yahoo_chart_url


class DataDownloadError(RuntimeError):
    def __init__(self, symbol: str, detail: str):
        super().__init__(f"Yahoo chart download failed for {symbol}: {detail}")


def fetch_yahoo(symbol: str, start: str, end: str, timeout: int = 30) -> tuple[pd.DataFrame, str]:
    url = yahoo_chart_url(symbol, start, end)
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "gold-quant-research/0.1"})
        response.raise_for_status()
        payload = response.json()
        result = payload["chart"]["result"][0]
        error = payload["chart"].get("error")
        if error:
            raise ValueError(str(error))
        quote_data = result["indicators"]["quote"][0]
        frame = pd.DataFrame(quote_data)
        frame["Date"] = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(None)
        adj = result["indicators"].get("adjclose", [{}])[0].get("adjclose")
        if adj is not None:
            frame["Adj Close"] = adj
        frame = frame.rename(columns={k: k.title() for k in ["open", "high", "low", "close", "volume"]})
        frame = frame.dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)
        if frame.empty:
            raise ValueError("response contained no daily close rows")
        return frame, url
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        raise DataDownloadError(symbol, str(exc)) from exc


def save_dataset(
    frame: pd.DataFrame, symbol: str, url: str, period_start: str, output_dir: Path, period_end: str | None = None
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    clean = frame.copy()
    if "Date" not in clean:
        clean = clean.reset_index(names="Date")
    clean["Date"] = pd.to_datetime(clean["Date"])
    safe = symbol.replace("=", "_")
    csv_path = output_dir / f"{safe}.csv"
    parquet_path = output_dir / f"{safe}.parquet"
    clean.to_csv(csv_path, index=False, float_format="%.17g")
    clean.to_parquet(parquet_path, index=False)
    csv_digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    parquet_digest = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    item = {
        "symbol": symbol,
        "url": url,
        "period_start": period_start,
        "period_end": period_end or str(clean["Date"].max().date()),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sha256": csv_digest,
        "csv_sha256": csv_digest,
        "parquet_sha256": parquet_digest,
        "rows": int(len(clean)),
        "data_start": str(clean["Date"].min().date()),
        "data_end": str(clean["Date"].max().date()),
        "csv": str(csv_path),
        "parquet": str(parquet_path),
    }
    manifest_path = output_dir / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest[symbol] = item
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return item


def download_universe(symbols: list[str], start: str, end: str, output_dir: Path) -> dict[str, pd.DataFrame]:
    data = {}
    for symbol in symbols:
        frame, url = fetch_yahoo(symbol, start, end)
        save_dataset(frame, symbol, url, start, output_dir, end)
        data[symbol] = frame.set_index("Date").sort_index()
    return data
