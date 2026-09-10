from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import urllib.request
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

SANITIZED_PROXY_SCHEMA = "quantresearch-yahoo-chart-sanitized-source/v1"
MSFT_PROXY_STATIC_IDENTITY = {
    "symbol": "MSFT",
    "currency": "USD",
    "dataGranularity": "1d",
    "exchangeTimezoneName": "America/New_York",
}


class YahooProxySanitizationError(ValueError):
    """The provider response cannot produce the closed historical source artifact."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise YahooProxySanitizationError("Yahoo response contains a duplicate key")
        value[key] = item
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sanitize_msft_yahoo_proxy_response(payload_bytes: bytes, *, start: str, end: str) -> bytes:
    """Admit only frozen identity metadata and bounded historical chart arrays."""

    if not payload_bytes:
        raise YahooProxySanitizationError("Yahoo response is empty")
    try:
        payload = json.loads(
            payload_bytes,
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                YahooProxySanitizationError(f"Yahoo response contains non-finite value: {item}")
            ),
        )
        chart = payload["chart"]
        if chart.get("error") is not None:
            raise YahooProxySanitizationError("Yahoo response reports an error")
        results = chart["result"]
        if not isinstance(results, list) or len(results) != 1:
            raise YahooProxySanitizationError("Yahoo response must contain one chart result")
        result = results[0]
        metadata = result["meta"]
        if (
            not isinstance(metadata, dict)
            or set(metadata) != set(MSFT_PROXY_STATIC_IDENTITY)
            or any(
                metadata.get(key) != expected
                for key, expected in MSFT_PROXY_STATIC_IDENTITY.items()
            )
        ):
            raise YahooProxySanitizationError("Yahoo response identity does not match XNYS/MSFT")
        timestamps = result["timestamp"]
        quotes = result["indicators"]["quote"]
        adjusted = result["indicators"]["adjclose"]
        if not isinstance(quotes, list) or len(quotes) != 1:
            raise YahooProxySanitizationError("Yahoo quote generation is invalid")
        if not isinstance(adjusted, list) or len(adjusted) != 1:
            raise YahooProxySanitizationError("Yahoo adjusted-close generation is invalid")
        quote = quotes[0]
        arrays = {
            "timestamp": timestamps,
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "close": quote["close"],
            "adjclose": adjusted[0]["adjclose"],
        }
        if not all(isinstance(values, list) for values in arrays.values()):
            raise YahooProxySanitizationError("Yahoo chart arrays are invalid")
        if len({len(values) for values in arrays.values()}) != 1 or not timestamps:
            raise YahooProxySanitizationError("Yahoo chart arrays are not aligned")
    except YahooProxySanitizationError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YahooProxySanitizationError("Yahoo response schema is invalid") from exc

    admitted: list[int] = []
    prior_date: str | None = None
    for index, value in enumerate(timestamps):
        if type(value) is not int:
            raise YahooProxySanitizationError("Yahoo timestamp is invalid")
        try:
            session_date = str(
                datetime.fromtimestamp(value, UTC)
                .astimezone(ZoneInfo("America/New_York"))
                .date()
            )
        except (OverflowError, OSError, ValueError) as exc:
            raise YahooProxySanitizationError("Yahoo timestamp is invalid") from exc
        if prior_date is not None and session_date <= prior_date:
            raise YahooProxySanitizationError("Yahoo sessions must be unique and increasing")
        prior_date = session_date
        if start <= session_date <= end:
            admitted.append(index)
    if not admitted:
        raise YahooProxySanitizationError("Yahoo response has no admitted historical sessions")

    selected = {
        name: [values[index] for index in admitted]
        for name, values in arrays.items()
    }
    for name, values in selected.items():
        if name == "timestamp":
            continue
        if any(
            type(value) not in (int, float) or not math.isfinite(float(value)) or float(value) <= 0
            for value in values
        ):
            raise YahooProxySanitizationError("Yahoo admitted price arrays are incomplete")

    artifact = {
        "schema": SANITIZED_PROXY_SCHEMA,
        "identity": dict(MSFT_PROXY_STATIC_IDENTITY),
        "request_window": {"start": start, "end": end},
        "raw_response_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "chart": selected,
    }
    return _canonical_bytes(artifact)


def _fetch_and_sanitize(args: argparse.Namespace) -> bytes:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": args.proxy_url})
    )
    request = urllib.request.Request(
        args.url,
        headers={"User-Agent": "quant-research-platform/0.1"},
        method="GET",
    )
    with opener.open(request, timeout=25) as response:
        payload = response.read(args.maximum_bytes + 1)
        if len(payload) > args.maximum_bytes:
            raise YahooProxySanitizationError("Yahoo response exceeds size limit")
        if response.status != 200 or response.geturl() != args.url:
            raise YahooProxySanitizationError("Yahoo response status or URL is invalid")
    return sanitize_msft_yahoo_proxy_response(payload, start=args.start, end=args.end)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--maximum-bytes", required=True, type=int)
    parser.add_argument("--proxy-url", required=True)
    try:
        artifact = _fetch_and_sanitize(parser.parse_args())
    except Exception:
        # Raw provider values are deliberately never emitted on the process boundary.
        return 1
    sys.stdout.buffer.write(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
