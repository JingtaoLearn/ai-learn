#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYMBOL_FILES = {"GC=F": "GC_F.csv", "GLD": "GLD.csv"}


def parse_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="") as handle:
        for item in csv.DictReader(handle):
            try:
                day = datetime.fromisoformat(item["Date"][:10]).date().isoformat()
                open_price = float(item["Open"])
                close_price = float(item["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(open_price) and math.isfinite(close_price) and open_price > 0 and close_price > 0:
                rows.append({"date": day, "open": open_price, "close": close_price})
    rows.sort(key=lambda row: row["date"])
    deduped = {row["date"]: row for row in rows}
    return [deduped[key] for key in sorted(deduped)]


def fetch_yahoo(symbol: str, start: str = "2005-01-01") -> list[dict]:
    start_timestamp = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    end_timestamp = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol, safe="")
        + f"?period1={start_timestamp}&period2={end_timestamp}&interval=1d&events=history"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "gold-strategy-lab/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    result = payload["chart"]["result"][0]
    quotes = result["indicators"]["quote"][0]
    rows = []
    for index, timestamp in enumerate(result["timestamp"]):
        open_price = quotes["open"][index]
        close_price = quotes["close"][index]
        if open_price is None or close_price is None:
            continue
        open_value = float(open_price)
        close_value = float(close_price)
        if not (math.isfinite(open_value) and math.isfinite(close_value) and open_value > 0 and close_value > 0):
            continue
        day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        rows.append({"date": day, "open": open_value, "close": close_value})
    deduped = {row["date"]: row for row in rows}
    return [deduped[key] for key in sorted(deduped)]


def load_sources(data_dir: Path | None, refresh: bool) -> dict:
    symbols = {}
    for symbol, filename in SYMBOL_FILES.items():
        if refresh:
            rows = fetch_yahoo(symbol)
        else:
            if data_dir is None:
                raise ValueError("data_dir is required without refresh")
            rows = parse_csv(data_dir / filename)
        if len(rows) < 300:
            raise RuntimeError(f"{symbol}: only {len(rows)} usable rows")
        canonical = json.dumps(rows, separators=(",", ":"), sort_keys=True).encode()
        symbols[symbol] = {
            "start": rows[0]["date"],
            "end": rows[-1]["date"],
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "rows": rows,
        }
    return {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "execution": "prior information, next-open position, open-to-open return",
        "symbols": symbols,
    }


def build(project_root: Path, output: Path, data: dict) -> None:
    lab = project_root / "web" / "strategy-lab"
    template = (lab / "index.template.html").read_text()
    replacements = {
        "/*__STYLE__*/": (lab / "style.css").read_text(),
        "/*__CORE__*/": (lab / "core.js").read_text(),
        "/*__APP__*/": (lab / "app.js").read_text(),
        "/*__DATA__*/": json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False).replace("<", "\\u003c"),
    }
    for marker, value in replacements.items():
        if marker not in template:
            raise RuntimeError(f"missing template marker: {marker}")
        template = template.replace(marker, value)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(template)
    temporary.chmod(0o644)
    temporary.replace(output)
    output.chmod(0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the self-contained gold strategy lab")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.refresh and args.data_dir is None:
        parser.error("--data-dir is required unless --refresh is used")
    project_root = Path(__file__).resolve().parents[1]
    build(project_root, args.output, load_sources(args.data_dir, args.refresh))
    print(json.dumps({"output": str(args.output), "bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
