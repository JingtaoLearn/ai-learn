from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from importlib.metadata import version
from numbers import Number
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd
import requests

from .catalog import Catalog
from .datasets import (
    DatasetValidationError,
    SAFE_INSTRUMENT,
    _InstrumentLock,
    _normalize_frame,
    _validate_metadata,
    _verify_snapshot,
    _verified_action_evidence,
    snapshot_status,
)
from .schemas import SchemaValidationError, canonical_json_bytes
from .updates import reconcile_daily_history, snapshot_update_lineage
from .yahoo import yahoo_chart_url


DATASET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,63}$")
PRODUCTION_DATASET_NAMES = {
    "601328.SS": "Bank of Communications (601328.SS)",
}
CATALOG_CREATED_AT = "2026-08-27T00:00:00Z"
MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024
PRODUCTION_XSHG_YAHOO_METADATA = {
    "dataGranularity": "1d",
    "currency": "CNY",
    "exchangeName": "SHH",
    "instrumentType": "EQUITY",
    "exchangeTimezoneName": "Asia/Shanghai",
    "gmtoffset": 28800,
}
PROVIDER_STREAM_CHUNK_BYTES = 64 * 1024


class DatasetResolutionError(ValueError):
    """Raised when a catalog range cannot be bound to complete immutable data."""


@dataclass(frozen=True)
class DatasetCatalogItem:
    dataset_id: str
    name: str
    instrument: str
    provider: str
    market: str
    currency: str
    adjustment: str
    calendar: str
    default_start: str
    created_at: str = CATALOG_CREATED_AT

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "instrument": self.instrument,
            "provider": self.provider,
            "market": self.market,
            "currency": self.currency,
            "adjustment": self.adjustment,
        }


@dataclass(frozen=True)
class FetchedDailyBars:
    bars: pd.DataFrame
    source_identity: dict[str, Any]


class DailyBarsSource(Protocol):
    provider: str

    def latest_available_close(self, instrument: str) -> str: ...

    def fetch(self, instrument: str, start: str, end: str) -> FetchedDailyBars: ...


class SessionSource(Protocol):
    source_identity: dict[str, str]

    def sessions(self, start: str, end: str) -> list[str]: ...


def _date(value: Any, label: str) -> str:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise DatasetResolutionError(f"{label} must be a valid date") from exc
    if pd.isna(parsed) or parsed.tz is not None or parsed != parsed.normalize():
        raise DatasetResolutionError(f"{label} must be a timezone-naive daily date")
    return str(parsed.date())


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DatasetResolutionError(f"Yahoo response contains duplicate field: {key}")
        value[key] = item
    return value


SSE_CURRENT_ENDPOINT = "https://yunhq.sse.com.cn:32042/v1/sh1/list/exchange/ashare"
SSE_CURRENT_FIELDS = (
    "code",
    "name",
    "open",
    "high",
    "low",
    "last",
    "prev_close",
    "chg_rate",
    "volume",
    "amount",
    "tradephase",
    "change",
    "amp_rate",
    "cpxxsubtype",
    "cpxxprodusta",
)
SSE_CURRENT_PARAMS = {
    "select": ",".join(SSE_CURRENT_FIELDS),
    "begin": 0,
    "end": 5000,
}
SSE_CURRENT_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.sse.com.cn/market/price/report/",
    "User-Agent": "quant-research-platform/0.1",
}
SSE_PRICE_REPORT_SCRIPT_URL = (
    "https://www.sse.com.cn/xhtml/home/2021public/querySearch/"
    "search_price_2021.js?v=ssesite_V3.8.0_20260828"
)


def _sse_strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DatasetResolutionError(f"SSE response contains duplicate field: {key}")
        value[key] = item
    return value


class SseCurrentDailySource:
    provider = "sse-current-ashare-report"

    def __init__(
        self,
        *,
        http_get: Callable[..., Any] = requests.get,
        clock: Callable[[], datetime] | None = None,
        timeout: int = 30,
    ):
        self.http_get = http_get
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout = timeout

    @staticmethod
    def _instrument_code(instrument: str) -> str:
        if not isinstance(instrument, str) or re.fullmatch(r"[0-9]{6}\.SS", instrument) is None:
            raise DatasetResolutionError("SSE instrument must be an ordinary six-digit .SS symbol")
        return instrument[:-3]

    def _payload(self, instrument: str) -> tuple[FetchedDailyBars, str]:
        code = self._instrument_code(instrument)
        canonical_url = requests.Request(
            "GET", SSE_CURRENT_ENDPOINT, params=SSE_CURRENT_PARAMS
        ).prepare().url
        if not isinstance(canonical_url, str):
            raise DatasetResolutionError("SSE canonical request URL is invalid")
        try:
            response = self.http_get(
                SSE_CURRENT_ENDPOINT,
                params=SSE_CURRENT_PARAMS,
                headers=SSE_CURRENT_HEADERS,
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
            if type(getattr(response, "status_code", None)) is not int or response.status_code != 200:
                raise DatasetResolutionError("SSE response must have exact HTTP 200 status")
            history = getattr(response, "history", None)
            if type(history) is not list or history:
                raise DatasetResolutionError("SSE response must not have redirect history")
            effective_url = getattr(response, "url", None)
            if not isinstance(effective_url, str) or effective_url != canonical_url:
                raise DatasetResolutionError("SSE response effective URL is not canonical")
            content_length = response.headers.get("content-length")
            if isinstance(content_length, str):
                digits = content_length.strip()
                if re.fullmatch(r"[0-9]+", digits):
                    significant = digits.lstrip("0") or "0"
                    limit = str(MAX_PROVIDER_RESPONSE_BYTES)
                    if len(significant) > len(limit) or (
                        len(significant) == len(limit) and significant > limit
                    ):
                        raise DatasetResolutionError("SSE response body exceeds the size limit")
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("application/json"):
                raise DatasetResolutionError("SSE response is not JSON")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=PROVIDER_STREAM_CHUNK_BYTES):
                if not chunk:
                    continue
                if not isinstance(chunk, bytes):
                    raise DatasetResolutionError("SSE response body is invalid")
                if len(raw) + len(chunk) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise DatasetResolutionError("SSE response body exceeds the size limit")
                raw.extend(chunk)
        except requests.RequestException as exc:
            raise DatasetResolutionError(f"SSE current report request failed for {instrument}: {exc}") from exc
        finally:
            if "response" in locals():
                response.close()

        raw_bytes = bytes(raw)
        if not raw_bytes:
            raise DatasetResolutionError("SSE response body is empty")
        try:
            payload = json.loads(
                raw_bytes,
                object_pairs_hook=_sse_strict_object,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    DatasetResolutionError(f"SSE response contains non-finite value: {item}")
                ),
            )
        except DatasetResolutionError:
            raise
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DatasetResolutionError("SSE response schema is invalid") from exc
        if not isinstance(payload, dict):
            raise DatasetResolutionError("SSE response schema is invalid")

        report_date = payload.get("date")
        if type(report_date) is not int or re.fullmatch(r"[0-9]{8}", str(report_date)) is None:
            raise DatasetResolutionError("SSE response date must be a real YYYYMMDD date")
        try:
            response_date = datetime.strptime(str(report_date), "%Y%m%d").date().isoformat()
        except ValueError as exc:
            raise DatasetResolutionError("SSE response date must be a real YYYYMMDD date") from exc

        report_time = payload.get("time")
        if type(report_time) is not int or not 0 <= report_time <= 235959:
            raise DatasetResolutionError("SSE response time must be a real HHMMSS time")
        compact_time = f"{report_time:06d}"
        try:
            response_time = datetime.strptime(compact_time, "%H%M%S").time().isoformat()
        except ValueError as exc:
            raise DatasetResolutionError("SSE response time must be a real HHMMSS time") from exc

        total = payload.get("total")
        if type(total) is not int or total < 0:
            raise DatasetResolutionError("SSE response total must be a non-negative integer")
        rows = payload.get("list")
        if not isinstance(rows, list):
            raise DatasetResolutionError("SSE response list must be a list")
        matches = [row for row in rows if isinstance(row, list) and row and row[0] == code]
        if not matches:
            raise DatasetResolutionError("SSE response is missing the requested code")
        if len(matches) != 1:
            raise DatasetResolutionError("SSE response must contain exactly one requested code row")
        row = matches[0]
        if len(row) != len(SSE_CURRENT_FIELDS):
            raise DatasetResolutionError("SSE requested code row must contain exactly 15 fields")

        name = row[1]
        if not isinstance(name, str) or not name.strip():
            raise DatasetResolutionError("SSE response name must be non-empty")
        ohlc = row[2:6]
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in ohlc):
            raise DatasetResolutionError("SSE response OHLC values must be finite and positive")
        open_value, high, low, last = ohlc
        if high < max(open_value, low, last) or low > min(open_value, high, last):
            raise DatasetResolutionError("SSE response OHLC values are inconsistent")
        volume = row[8]
        if type(volume) is not int or volume < 0:
            raise DatasetResolutionError("SSE response volume must be a non-negative integer")
        try:
            float_volume = float(volume)
        except OverflowError as exc:
            raise DatasetResolutionError(
                "SSE response volume must be losslessly representable as float64"
            ) from exc
        if not math.isfinite(float_volume) or int(float_volume) != volume:
            raise DatasetResolutionError(
                "SSE response volume must be losslessly representable as float64"
            )
        tradephase = row[10]
        if not isinstance(tradephase, str) or tradephase[:4] != "E110":
            raise DatasetResolutionError("SSE response trading phase is not E110")
        if row[13] != "ASH":
            raise DatasetResolutionError("SSE response subtype is not ASH")

        canonical_content = {
            "date": report_date,
            "time": report_time,
            "field_order": list(SSE_CURRENT_FIELDS),
            "row": row,
        }
        try:
            bars = _normalize_frame(
                pd.DataFrame(
                    [
                        {
                            "Date": pd.Timestamp(response_date),
                            "Open": open_value,
                            "High": high,
                            "Low": low,
                            "Close": last,
                            "Volume": volume,
                        }
                    ]
                )
            )
        except DatasetValidationError as exc:
            raise DatasetResolutionError(f"SSE response data is invalid: {exc}") from exc
        return (
            FetchedDailyBars(
                bars=bars,
                source_identity={
                    "provider": self.provider,
                    "instrument": instrument,
                    "request": {
                        "method": "GET",
                        "url": effective_url,
                        "params": dict(SSE_CURRENT_PARAMS),
                        "headers": dict(SSE_CURRENT_HEADERS),
                    },
                    "response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    "canonical_content_sha256": hashlib.sha256(
                        canonical_json_bytes(canonical_content)
                    ).hexdigest(),
                    "response_date": response_date,
                    "response_time": response_time,
                    "trading_phase": tradephase[:4],
                    "contract": "IS120 STEP 0.62",
                    "price_report_script_url": SSE_PRICE_REPORT_SCRIPT_URL,
                },
            ),
            response_date,
        )

    def fetch(self, instrument: str, start: str, end: str) -> FetchedDailyBars:
        start = _date(start, "SSE request range start")
        end = _date(end, "SSE request range end")
        if start != end:
            raise DatasetResolutionError("SSE current report range must be exactly one date")
        fetched, response_date = self._payload(instrument)
        if start != response_date:
            raise DatasetResolutionError("SSE response date does not match the requested range")
        return fetched

    def latest_available_close(self, instrument: str) -> str:
        now = self.clock()
        if now.tzinfo is None:
            raise DatasetResolutionError("SSE source clock must be timezone-aware")
        _, response_date = self._payload(instrument)
        return response_date


class XSHGCalendar:
    def __init__(self) -> None:
        self._calendar = exchange_calendars.get_calendar("XSHG")
        self.source_identity = {
            "calendar": "XSHG",
            "library": "exchange_calendars",
            "version": version("exchange-calendars"),
        }

    def sessions(self, start: str, end: str) -> list[str]:
        start = _date(start, "range start")
        end = _date(end, "range end")
        if start > end:
            raise DatasetResolutionError("range start must not be after range end")
        first = str(self._calendar.first_session.date())
        last = str(self._calendar.last_session.date())
        if start < first or end > last:
            year = start[:4] if start < first else end[:4]
            raise DatasetResolutionError(
                f"XSHG calendar has no authoritative session coverage for {year}"
            )
        return self._calendar.sessions_in_range(start, end).strftime("%Y-%m-%d").tolist()


class YahooChartSource:
    provider = "yahoo-chart-api"

    def __init__(
        self,
        *,
        http_get: Callable[..., Any] = requests.get,
        clock: Callable[[], datetime] | None = None,
        timeout: int = 30,
    ):
        self.http_get = http_get
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout = timeout

    def _payload(
        self,
        instrument: str,
        start: str,
        end: str,
        *,
        allow_trailing_incomplete: bool,
    ) -> FetchedDailyBars:
        if not SAFE_INSTRUMENT.fullmatch(instrument):
            raise DatasetResolutionError("Yahoo instrument has invalid syntax")
        start = _date(start, "Yahoo request start")
        end = _date(end, "Yahoo request end")
        url = yahoo_chart_url(instrument, start, end)
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "query1.finance.yahoo.com"
            or parsed_url.path
            != f"/v8/finance/chart/{instrument.replace('=', '%3D')}"
        ):
            raise DatasetResolutionError("Yahoo request URL is not the canonical chart endpoint")
        try:
            response = self.http_get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": "quant-research-platform/0.1"},
                stream=True,
                allow_redirects=False,
            )
            if type(getattr(response, "status_code", None)) is not int or response.status_code != 200:
                raise DatasetResolutionError("Yahoo response must have exact HTTP 200 status")
            history = getattr(response, "history", None)
            if type(history) is not list or history:
                raise DatasetResolutionError("Yahoo response must not have redirect history")
            effective_url = getattr(response, "url", None)
            if not isinstance(effective_url, str) or effective_url != url:
                raise DatasetResolutionError("Yahoo response effective URL is not canonical")
            content_length = response.headers.get("content-length")
            if isinstance(content_length, str):
                digits = content_length.strip()
                if re.fullmatch(r"[0-9]+", digits):
                    significant = digits.lstrip("0") or "0"
                    limit = str(MAX_PROVIDER_RESPONSE_BYTES)
                    if len(significant) > len(limit) or (
                        len(significant) == len(limit) and significant > limit
                    ):
                        raise DatasetResolutionError(
                            "Yahoo response body exceeds the size limit"
                        )
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("application/json"):
                raise DatasetResolutionError("Yahoo response is not JSON")
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=PROVIDER_STREAM_CHUNK_BYTES):
                if not chunk:
                    continue
                if not isinstance(chunk, bytes):
                    raise DatasetResolutionError("Yahoo response body is invalid")
                if len(payload) + len(chunk) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise DatasetResolutionError(
                        "Yahoo response body exceeds the size limit"
                    )
                payload.extend(chunk)
        except requests.RequestException as exc:
            raise DatasetResolutionError(
                f"Yahoo chart request failed for {instrument}: {exc}"
            ) from exc
        finally:
            if "response" in locals():
                response.close()
        payload_bytes = bytes(payload)
        if not payload_bytes:
            raise DatasetResolutionError("Yahoo response body is empty")
        try:
            payload = json.loads(
                payload_bytes,
                object_pairs_hook=_strict_object,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    DatasetResolutionError(
                        f"Yahoo response contains non-finite value: {item}"
                    )
                ),
            )
            chart = payload["chart"]
            if chart.get("error") is not None:
                raise DatasetResolutionError(
                    f"Yahoo response reports an error: {chart['error']}"
                )
            results = chart["result"]
            if not isinstance(results, list) or len(results) != 1:
                raise DatasetResolutionError(
                    "Yahoo response must contain exactly one chart result"
                )
            result = results[0]
            metadata = result["meta"]
            if metadata.get("symbol") != instrument:
                raise DatasetResolutionError("Yahoo response symbol does not match request")
            if instrument.endswith(".SS"):
                for field, expected in PRODUCTION_XSHG_YAHOO_METADATA.items():
                    if metadata.get(field) != expected:
                        raise DatasetResolutionError(
                            f"Yahoo response {field} does not match XSHG production data"
                        )
            elif metadata.get("dataGranularity") != "1d":
                raise DatasetResolutionError(
                    "Yahoo response dataGranularity does not match daily data"
                )
            timestamps = result["timestamp"]
            quotes = result["indicators"]["quote"]
            if not isinstance(quotes, list) or len(quotes) != 1:
                raise DatasetResolutionError(
                    "Yahoo response must contain exactly one quote generation"
                )
            quote = quotes[0]
            adjusted_groups = result["indicators"].get("adjclose", [])
        except DatasetResolutionError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DatasetResolutionError("Yahoo response schema is invalid") from exc

        if not isinstance(timestamps, list) or not timestamps:
            raise DatasetResolutionError("Yahoo response contains no daily timestamps")
        required = ("open", "high", "low", "close", "volume")
        if any(not isinstance(quote.get(field), list) for field in required):
            raise DatasetResolutionError("Yahoo OHLCV arrays are missing")
        lengths = {len(timestamps), *(len(quote[field]) for field in required)}
        adjusted: list[Any] | None = None
        if adjusted_groups:
            if not isinstance(adjusted_groups, list) or len(adjusted_groups) != 1:
                raise DatasetResolutionError(
                    "Yahoo response must contain at most one adjusted-close generation"
                )
            adjusted = adjusted_groups[0].get("adjclose")
            if not isinstance(adjusted, list):
                raise DatasetResolutionError("Yahoo adjusted-close array is invalid")
            lengths.add(len(adjusted))
        if len(lengths) != 1:
            raise DatasetResolutionError("Yahoo response arrays are not aligned")

        if any(type(value) is not int for value in timestamps):
            raise DatasetResolutionError(
                "Yahoo response timestamps must be exact integer epoch seconds"
            )
        price_arrays = [quote[field] for field in ("open", "high", "low", "close")]
        if adjusted is not None:
            price_arrays.append(adjusted)
        for values in price_arrays:
            for value in values:
                if value is None:
                    continue
                if type(value) not in (int, float):
                    raise DatasetResolutionError(
                        "Yahoo response OHLC price values must be finite positive numbers"
                    )
                try:
                    float_value = float(value)
                except OverflowError as exc:
                    raise DatasetResolutionError(
                        "Yahoo response OHLC price values must be finite positive numbers"
                    ) from exc
                if not math.isfinite(float_value) or float_value <= 0:
                    raise DatasetResolutionError(
                        "Yahoo response OHLC price values must be finite positive numbers"
                    )
        for value in quote["volume"]:
            if value is None:
                continue
            if type(value) is int:
                invalid_volume = value < 0 or value > 2**53
            elif type(value) is float:
                invalid_volume = (
                    not math.isfinite(value)
                    or value < 0
                    or not value.is_integer()
                    or value > 2**53
                    or int(value) != value
                )
            else:
                invalid_volume = True
            if invalid_volume:
                raise DatasetResolutionError(
                    "Yahoo response volume values must be non-negative finite counts"
                )

        try:
            exchange_timezone = metadata.get("exchangeTimezoneName", "UTC")
            dates = (
                pd.to_datetime(timestamps, unit="s", utc=True)
                .tz_convert(ZoneInfo(exchange_timezone))
                .normalize()
                .tz_localize(None)
            )
        except (TypeError, ValueError) as exc:
            raise DatasetResolutionError("Yahoo response timestamps are invalid") from exc
        if dates.duplicated().any():
            raise DatasetResolutionError("Yahoo response contains duplicate daily timestamps")
        if not dates.is_monotonic_increasing:
            raise DatasetResolutionError("Yahoo response timestamps are not sorted")

        rows: list[dict[str, Any]] = []
        incomplete: list[str] = []
        for index, session in enumerate(dates):
            values = [quote[field][index] for field in required]
            if adjusted is not None:
                values.append(adjusted[index])
            if any(value is None for value in values):
                incomplete.append(str(session.date()))
                continue
            row = {
                "Date": session,
                "Open": quote["open"][index],
                "High": quote["high"][index],
                "Low": quote["low"][index],
                "Close": quote["close"][index],
                "Volume": quote["volume"][index],
            }
            if adjusted is not None:
                row["AdjustedClose"] = adjusted[index]
            rows.append(row)
        if incomplete:
            last_complete = str(rows[-1]["Date"].date()) if rows else None
            invalid = [
                session
                for session in incomplete
                if not allow_trailing_incomplete
                or last_complete is None
                or session <= last_complete
            ]
            if invalid:
                raise DatasetResolutionError(
                    "Yahoo response contains incomplete OHLCV rows: "
                    + ", ".join(invalid)
                )
        if not rows:
            raise DatasetResolutionError("Yahoo response contains no complete daily close rows")
        try:
            frame = _normalize_frame(pd.DataFrame(rows))
        except DatasetValidationError as exc:
            raise DatasetResolutionError(f"Yahoo response data is invalid: {exc}") from exc
        if frame["Date"].min() < pd.Timestamp(start) or frame["Date"].max() > pd.Timestamp(end):
            raise DatasetResolutionError("Yahoo response contains dates outside the request")
        return FetchedDailyBars(
            bars=frame,
            source_identity={
                "provider": self.provider,
                "instrument": instrument,
                "request_url": url,
                "response_sha256": hashlib.sha256(payload_bytes).hexdigest(),
                "canonical_content_sha256": hashlib.sha256(
                    canonical_json_bytes(payload)
                ).hexdigest(),
            },
        )

    def fetch(self, instrument: str, start: str, end: str) -> FetchedDailyBars:
        return self._payload(
            instrument,
            start,
            end,
            allow_trailing_incomplete=False,
        )

    def latest_available_close(self, instrument: str) -> str:
        now = self.clock()
        if now.tzinfo is None:
            raise DatasetResolutionError("Yahoo source clock must be timezone-aware")
        local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        local_date = local_now.date()
        start = local_date - timedelta(days=45)
        result = self._payload(
            instrument,
            str(start),
            str(local_date),
            allow_trailing_incomplete=True,
        )
        complete = result.bars
        if (
            complete["Date"].max().date() == local_date
            and local_now.time() < time(15, 0)
        ):
            complete = complete[complete["Date"].dt.date < local_date]
        if complete.empty:
            raise DatasetResolutionError(
                "Yahoo response contains no completed daily close"
            )
        return str(complete["Date"].max().date())


class AuditedXshgDailySource:
    provider = "xshg-audited-daily-v1"
    policy_version = "sse-whole-t-yahoo-history-v1"
    history_provider = "yahoo-chart-api"
    current_provider = "sse-current-ashare-report"
    columns = ("Date", "Open", "High", "Low", "Close", "Volume")
    price_columns = ("Open", "High", "Low", "Close")
    price_tick = Decimal("0.01")
    price_tolerance = Decimal("0.000001")

    def __init__(
        self,
        *,
        history_source: DailyBarsSource,
        current_source: DailyBarsSource,
        clock: Callable[[], datetime] | None = None,
    ):
        self.history_source = history_source
        self.current_source = current_source
        self.clock = clock or (lambda: datetime.now(UTC))

    def _decision_date(self) -> str:
        now = self.clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise DatasetResolutionError("audited XSHG source clock must be timezone-aware")
        return str(now.astimezone(ZoneInfo("Asia/Shanghai")).date())

    @staticmethod
    def _validate_instrument(instrument: str) -> None:
        if not isinstance(instrument, str) or re.fullmatch(r"[0-9]{6}\.SS", instrument) is None:
            raise DatasetResolutionError(
                "audited XSHG instrument must be an ordinary six-digit .SS symbol"
            )

    @staticmethod
    def _require_component_provider(
        source: DailyBarsSource, expected_provider: str
    ) -> None:
        actual_provider = getattr(source, "provider", None)
        if type(actual_provider) is not str or actual_provider != expected_provider:
            raise DatasetResolutionError(
                f"configured component provider must be {expected_provider}"
            )

    @staticmethod
    def _component_identity(
        fetched: FetchedDailyBars,
        *,
        provider: str,
        instrument: str,
    ) -> dict[str, Any]:
        identity = fetched.source_identity
        if not isinstance(identity, dict):
            raise DatasetResolutionError("component source identity must be a finite dictionary")
        try:
            canonical_identity = canonical_json_bytes(identity)
            detached = json.loads(
                canonical_identity,
                object_pairs_hook=_strict_object,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    ValueError(f"non-finite identity value: {item}")
                ),
            )
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise DatasetResolutionError(
                "component source identity must be finite canonical JSON"
            ) from exc
        if type(detached) is not dict:
            raise DatasetResolutionError("component source identity must be a finite dictionary")
        if detached.get("provider") != provider:
            raise DatasetResolutionError(
                f"component source identity provider must be {provider}"
            )
        if detached.get("instrument") != instrument:
            raise DatasetResolutionError(
                "component source identity instrument does not match the request"
            )
        return detached

    @classmethod
    def _normalize_market_values(
        cls,
        frame: pd.DataFrame,
        *,
        source_label: str,
    ) -> pd.DataFrame:
        normalized_prices: dict[str, list[float]] = {}
        for column in cls.price_columns:
            prices: list[float] = []
            for value in frame[column]:
                if isinstance(value, bool) or not isinstance(value, (Number, Decimal)):
                    raise DatasetResolutionError(
                        f"{source_label} price values must be numeric, finite, and positive"
                    )
                try:
                    decimal_value = Decimal(str(value))
                except (InvalidOperation, ValueError) as exc:
                    raise DatasetResolutionError(
                        f"{source_label} price values must be numeric, finite, and positive"
                    ) from exc
                if not decimal_value.is_finite() or decimal_value <= 0:
                    raise DatasetResolutionError(
                        f"{source_label} price values must be finite and positive"
                    )
                try:
                    tick_value = decimal_value.quantize(
                        cls.price_tick, rounding=ROUND_HALF_UP
                    )
                except InvalidOperation as exc:
                    raise DatasetResolutionError(
                        f"{source_label} price cannot be normalized to the CNY 0.01 tick"
                    ) from exc
                if abs(decimal_value - tick_value) > cls.price_tolerance:
                    raise DatasetResolutionError(
                        f"{source_label} price is materially outside the CNY 0.01 tick"
                    )
                prices.append(float(tick_value))
            normalized_prices[column] = prices
        for column, prices in normalized_prices.items():
            frame[column] = prices

        normalized_volumes: list[float] = []
        for value in frame["Volume"]:
            if isinstance(value, bool) or not isinstance(value, (Number, Decimal)):
                raise DatasetResolutionError(
                    f"{source_label} Volume must be a numeric non-negative integer"
                )
            try:
                decimal_value = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise DatasetResolutionError(
                    f"{source_label} Volume must be a numeric non-negative integer"
                ) from exc
            if (
                not decimal_value.is_finite()
                or decimal_value < 0
                or decimal_value != decimal_value.to_integral_value()
            ):
                raise DatasetResolutionError(
                    f"{source_label} Volume must be a finite non-negative integer"
                )
            if decimal_value > 2**53:
                raise DatasetResolutionError(
                    f"{source_label} Volume must be losslessly representable as float64"
                )
            integer_value = int(decimal_value)
            try:
                float_value = float(integer_value)
            except OverflowError as exc:
                raise DatasetResolutionError(
                    f"{source_label} Volume must be losslessly representable as float64"
                ) from exc
            if not math.isfinite(float_value) or int(float_value) != integer_value:
                raise DatasetResolutionError(
                    f"{source_label} Volume must be losslessly representable as float64"
                )
            normalized_volumes.append(float_value)
        frame["Volume"] = normalized_volumes
        return frame

    @classmethod
    def _normalize_component_frame(
        cls,
        fetched: FetchedDailyBars,
        *,
        start: str,
        end: str,
        history: bool,
    ) -> pd.DataFrame:
        frame = fetched.bars
        if not isinstance(frame, pd.DataFrame):
            raise DatasetResolutionError("component source schema must be a data frame")
        missing = [column for column in cls.columns if column not in frame.columns]
        if missing:
            raise DatasetResolutionError(
                f"component source schema is missing required columns: {missing}"
            )
        if not history and tuple(frame.columns) != cls.columns:
            raise DatasetResolutionError(
                "current source schema must be exactly Date, Open, High, Low, Close, Volume"
            )
        projected = frame.loc[:, cls.columns].copy()
        try:
            raw_dates = [pd.Timestamp(value) for value in projected["Date"]]
            if raw_dates and not any(pd.isna(value) for value in raw_dates):
                raw_dates_are_sorted = pd.Index(raw_dates).is_monotonic_increasing
            else:
                raw_dates_are_sorted = True
        except (TypeError, ValueError, OverflowError):
            raw_dates_are_sorted = True
        if not raw_dates_are_sorted:
            raise DatasetResolutionError("component source dates must be sorted")
        projected = cls._normalize_market_values(
            projected,
            source_label="Yahoo history" if history else "SSE current",
        )

        try:
            normalized = _normalize_frame(projected)
        except DatasetValidationError as exc:
            raise DatasetResolutionError(f"component source data is invalid: {exc}") from exc
        if normalized["Date"].duplicated().any():
            raise DatasetResolutionError("component source contains duplicate dates")
        if not normalized["Date"].is_monotonic_increasing:
            raise DatasetResolutionError("component source dates must be sorted")
        if (
            normalized["Date"].min() < pd.Timestamp(start)
            or normalized["Date"].max() > pd.Timestamp(end)
        ):
            raise DatasetResolutionError("component source contains dates outside its request")
        return normalized

    def _validated_component(
        self,
        source: DailyBarsSource,
        *,
        expected_provider: str,
        instrument: str,
        start: str,
        end: str,
        history: bool,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        self._require_component_provider(source, expected_provider)
        fetched = source.fetch(instrument, start, end)
        if not isinstance(fetched, FetchedDailyBars):
            raise DatasetResolutionError("component source returned an invalid generation")
        identity = self._component_identity(
            fetched,
            provider=expected_provider,
            instrument=instrument,
        )
        frame = self._normalize_component_frame(
            fetched,
            start=start,
            end=end,
            history=history,
        )
        return frame, identity

    def fetch(self, instrument: str, start: str, end: str) -> FetchedDailyBars:
        self._validate_instrument(instrument)
        start = _date(start, "audited XSHG request start")
        end = _date(end, "audited XSHG request end")
        if start > end:
            raise DatasetResolutionError("audited XSHG request start is after its end")
        decision_date = self._decision_date()
        if end > decision_date:
            raise DatasetResolutionError("audited XSHG request cannot include a future date")

        components: list[dict[str, Any]]
        if end < decision_date:
            self._require_component_provider(
                self.history_source, self.history_provider
            )
            bars, history_identity = self._validated_component(
                self.history_source,
                expected_provider=self.history_provider,
                instrument=instrument,
                start=start,
                end=end,
                history=True,
            )
            components = [history_identity]
        else:
            self._require_component_provider(
                self.current_source, self.current_provider
            )
            if start < decision_date:
                self._require_component_provider(
                    self.history_source, self.history_provider
                )
            current_bars, current_identity = self._validated_component(
                self.current_source,
                expected_provider=self.current_provider,
                instrument=instrument,
                start=decision_date,
                end=decision_date,
                history=False,
            )
            if start == decision_date:
                bars = current_bars
                components = [current_identity]
            else:
                history_end = str(pd.Timestamp(decision_date).date() - timedelta(days=1))
                history_bars, history_identity = self._validated_component(
                    self.history_source,
                    expected_provider=self.history_provider,
                    instrument=instrument,
                    start=start,
                    end=history_end,
                    history=True,
                )
                try:
                    bars = _normalize_frame(
                        pd.concat([history_bars, current_bars], ignore_index=True)
                    )
                except DatasetValidationError as exc:
                    raise DatasetResolutionError(
                        f"audited XSHG source generations conflict: {exc}"
                    ) from exc
                components = [history_identity, current_identity]

        identity = {
            "provider": self.provider,
            "instrument": instrument,
            "policy_version": self.policy_version,
            "decision_date": decision_date,
            "requested_start": start,
            "requested_end": end,
            "components": components,
        }
        try:
            canonical_json_bytes(identity)
        except SchemaValidationError as exc:
            raise DatasetResolutionError(
                "audited XSHG source identity must be finite canonical JSON"
            ) from exc
        return FetchedDailyBars(bars=bars, source_identity=identity)

    def latest_available_close(self, instrument: str) -> str:
        self._validate_instrument(instrument)
        decision_date = self._decision_date()
        self._require_component_provider(self.current_source, self.current_provider)
        latest = _date(
            self.current_source.latest_available_close(instrument),
            "audited XSHG current latest close",
        )
        if latest > decision_date:
            raise DatasetResolutionError(
                "audited XSHG current latest close cannot be in the future"
            )
        return latest


class DatasetService:
    def __init__(
        self,
        catalog: Catalog,
        *,
        sources: dict[str, DailyBarsSource] | None = None,
        calendars: dict[str, SessionSource] | None = None,
    ):
        self.catalog = catalog
        self.sources = sources or {YahooChartSource.provider: YahooChartSource()}
        self.calendars = calendars or {"XSHG": XSHGCalendar()}

    def register(self, item: DatasetCatalogItem) -> dict[str, str]:
        if not DATASET_ID.fullmatch(item.dataset_id):
            raise DatasetResolutionError("dataset_id has invalid syntax")
        if not item.name.strip() or item.name != item.name.strip():
            raise DatasetResolutionError("dataset name must be non-empty and trimmed")
        try:
            metadata = _validate_metadata(item.metadata)
        except DatasetValidationError as exc:
            raise DatasetResolutionError(str(exc)) from exc
        if metadata != item.metadata:
            raise DatasetResolutionError("dataset metadata is not canonical")
        if item.calendar not in self.calendars:
            raise DatasetResolutionError(
                f"dataset calendar is unavailable: {item.calendar}"
            )
        _date(item.default_start, "dataset default start")
        try:
            return self.catalog.register_dataset(asdict(item))
        except ValueError as exc:
            raise DatasetResolutionError(str(exc)) from exc

    def _discover_existing(self) -> None:
        datasets_root = self.catalog.state_root / "datasets"
        if not datasets_root.exists():
            return
        if datasets_root.is_symlink() or not datasets_root.is_dir():
            raise DatasetResolutionError("dataset store is not a safe directory")
        for instrument_root in sorted(datasets_root.iterdir()):
            if instrument_root.name.startswith("."):
                continue
            if instrument_root.is_symlink() or not instrument_root.is_dir():
                raise DatasetResolutionError("dataset instrument store is unsafe")
            pointer = instrument_root / "latest.json"
            if not pointer.exists():
                continue
            status = snapshot_status(self.catalog.state_root, instrument_root.name)
            manifest = _verify_snapshot(Path(status["path"]), status["snapshot_id"])
            metadata = manifest["metadata"]
            try:
                current = self.catalog.dataset_detail(metadata["instrument"])
            except ValueError:
                self.register(
                    DatasetCatalogItem(
                        dataset_id=metadata["instrument"],
                        name=PRODUCTION_DATASET_NAMES.get(
                            metadata["instrument"], metadata["instrument"]
                        ),
                        instrument=metadata["instrument"],
                        provider=metadata["provider"],
                        market=metadata["market"],
                        currency=metadata["currency"],
                        adjustment=metadata["adjustment"],
                        calendar=metadata["market"],
                        default_start=manifest["data_start"],
                    )
                )
            else:
                expected = {
                    key: current[key]
                    for key in (
                        "instrument",
                        "provider",
                        "market",
                        "currency",
                        "adjustment",
                    )
                }
                if metadata != expected:
                    raise DatasetResolutionError(
                        f"latest snapshot metadata conflicts with dataset {current['dataset_id']}"
                    )

    def _item(self, dataset_id: str) -> DatasetCatalogItem:
        self._discover_existing()
        try:
            return DatasetCatalogItem(**self.catalog.dataset_detail(dataset_id))
        except ValueError as exc:
            raise DatasetResolutionError(str(exc)) from exc

    def _latest(
        self, item: DatasetCatalogItem
    ) -> tuple[dict[str, Any], pd.DataFrame] | None:
        pointer = (
            self.catalog.state_root
            / "datasets"
            / item.instrument
            / "latest.json"
        )
        if not pointer.exists():
            return None
        status = snapshot_status(self.catalog.state_root, item.instrument)
        verified = _verify_snapshot(
            Path(status["path"]), status["snapshot_id"], include_frame=True
        )
        if not isinstance(verified, tuple):
            raise DatasetResolutionError("snapshot verifier did not return market data")
        manifest, frame = verified
        if manifest["metadata"] != item.metadata:
            raise DatasetResolutionError("latest snapshot metadata conflicts with dataset catalog")
        return manifest, frame

    def _calendar(self, item: DatasetCatalogItem) -> SessionSource:
        try:
            return self.calendars[item.calendar]
        except KeyError as exc:
            raise DatasetResolutionError(
                f"dataset calendar is unavailable: {item.calendar}"
            ) from exc

    def list_available(self) -> list[dict[str, str | None]]:
        self._discover_existing()
        options: list[dict[str, str | None]] = []
        for row in self.catalog.list_datasets():
            item = DatasetCatalogItem(**row)
            latest = self._latest(item)
            source = self.sources.get(item.provider)
            if source is not None:
                latest_close = _date(
                    source.latest_available_close(item.instrument),
                    "latest available close",
                )
            elif latest is not None:
                latest_close = latest[0]["data_end"]
            else:
                raise DatasetResolutionError(
                    f"dataset source is unavailable: {item.provider}"
                )
            if self._calendar(item).sessions(latest_close, latest_close) != [latest_close]:
                raise DatasetResolutionError(
                    f"latest available close is not an {item.calendar} session"
                )
            options.append(
                {
                    "dataset_id": item.dataset_id,
                    "name": item.name,
                    "instrument": item.instrument,
                    "default_start": item.default_start,
                    "latest_available_close": latest_close,
                    "latest_snapshot_id": latest[0]["snapshot_id"] if latest else None,
                }
            )
        return options

    def snapshot_detail(self, dataset_id: str, snapshot_id: str) -> dict[str, Any]:
        """Return one recursively verified snapshot and its bounded action evidence."""

        if not isinstance(snapshot_id, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None:
            raise DatasetResolutionError("snapshot_id must be a lower-case SHA-256 value")
        item = self._item(dataset_id)
        target = self.catalog.state_root / "datasets" / item.instrument / snapshot_id
        if target.is_symlink() or not target.is_dir():
            raise DatasetResolutionError(
                f"unknown immutable dataset snapshot: {item.instrument}@{snapshot_id}"
            )
        try:
            manifest = _verify_snapshot(target, snapshot_id, verify_parent=True)
            if manifest["metadata"] != item.metadata:
                raise DatasetResolutionError(
                    "snapshot metadata conflicts with dataset catalog"
                )
            if manifest["schema_version"] in {4, 5}:
                evidence = _verified_action_evidence(target, manifest)
                document = evidence.document
                coverage = document["coverage"]
                corporate_actions = {
                    "coverage_state": coverage["payload"]["coverage_state"],
                    "coverage_id": coverage["coverage_id"],
                    "limitations": coverage["payload"]["limitations"],
                    "events": document["revisions"],
                    "artifacts": document["artifacts"],
                    "requests": document["requests"],
                    "retrievals": document["retrievals"],
                    "findings": document["findings"],
                    "total_return_claim": document["total_return_claim"],
                    "explanation": (
                        "Known events with incomplete interval coverage are not verified "
                        "total return."
                        if coverage["payload"]["coverage_state"] == "VERIFIED_EVENTS"
                        else "Unknown or partial corporate-action evidence is not verified "
                        "total return."
                    ),
                }
            else:
                corporate_actions = {
                    "coverage_state": "UNKNOWN_MISSING",
                    "coverage_id": None,
                    "limitations": ["LEGACY_SNAPSHOT_NO_ACTION_EVIDENCE"],
                    "events": [],
                    "artifacts": [],
                    "requests": [],
                    "retrievals": [],
                    "findings": [],
                    "total_return_claim": "FORBIDDEN",
                    "explanation": (
                        "Unknown or partial corporate-action evidence is not verified "
                        "total return."
                    ),
                }
        except DatasetResolutionError:
            raise
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise DatasetResolutionError(
                f"dataset snapshot detail failed verification: {exc}"
            ) from exc
        return {
            "dataset_id": item.dataset_id,
            "name": item.name,
            "instrument": item.instrument,
            "snapshot_id": manifest["snapshot_id"],
            "schema_version": manifest["schema_version"],
            "canonical_sha256": manifest["canonical_sha256"],
            "corporate_action_evidence_sha256": manifest.get(
                "corporate_action_evidence_sha256"
            ),
            "data_start": manifest["data_start"],
            "data_end": manifest["data_end"],
            "lineage": manifest.get("lineage"),
            "corporate_actions": corporate_actions,
        }

    def sessions(self, dataset_id: str, start: str, end: str) -> list[str]:
        start = _date(start, "dataset range start")
        end = _date(end, "dataset range end")
        if start > end:
            raise DatasetResolutionError(
                "dataset range start must not be after range end"
            )
        item = self._item(dataset_id)
        return [
            _date(session, "expected trading session")
            for session in self._calendar(item).sessions(start, end)
        ]

    @contextmanager
    def guard_latest_resolution(
        self,
        resolved: dict[str, Any],
    ) -> Iterator[bool]:
        if not isinstance(resolved, dict):
            raise DatasetResolutionError("resolved dataset must be an object")
        instrument = resolved.get("instrument")
        snapshot_id = resolved.get("snapshot_id")
        canonical_sha256 = resolved.get("canonical_sha256")
        if not isinstance(instrument, str) or SAFE_INSTRUMENT.fullmatch(instrument) is None:
            raise DatasetResolutionError("resolved dataset instrument has invalid syntax")
        if not isinstance(snapshot_id, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None:
            raise DatasetResolutionError("resolved dataset snapshot_id must be a SHA-256 value")
        if (
            not isinstance(canonical_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", canonical_sha256) is None
        ):
            raise DatasetResolutionError(
                "resolved dataset canonical_sha256 must be a SHA-256 value"
            )

        root = self.catalog.state_root.resolve()
        with _InstrumentLock(root, instrument):
            pointer = root / "datasets" / instrument / "latest.json"
            if not pointer.exists():
                yield False
                return
            try:
                status = snapshot_status(root, instrument)
                manifest = _verify_snapshot(
                    Path(status["path"]),
                    status["snapshot_id"],
                )
            except (DatasetValidationError, OSError, RuntimeError) as exc:
                raise DatasetResolutionError(
                    f"latest dataset resolution failed verification: {exc}"
                ) from exc
            yield (
                status["snapshot_id"] == snapshot_id
                and manifest["canonical_sha256"] == canonical_sha256
            )

    def resolve(self, dataset_id: str, start: str, end: str) -> dict[str, Any]:
        start = _date(start, "dataset range start")
        end = _date(end, "dataset range end")
        if start > end:
            raise DatasetResolutionError(
                "dataset range start must not be after range end"
            )
        item = self._item(dataset_id)
        calendar = self._calendar(item)
        expected_sessions = [
            _date(session, "expected trading session")
            for session in calendar.sessions(start, end)
        ]
        if not expected_sessions:
            raise DatasetResolutionError(
                f"selected range contains no {item.calendar} trading sessions"
            )
        if expected_sessions != sorted(set(expected_sessions)):
            raise DatasetResolutionError(
                f"{item.calendar} sessions must be unique and ordered"
            )
        if expected_sessions[0] < start or expected_sessions[-1] > end:
            raise DatasetResolutionError(
                f"{item.calendar} sessions fall outside the selected range"
            )
        effective_start = expected_sessions[0]
        effective_end = expected_sessions[-1]

        latest = self._latest(item)
        manifest: dict[str, Any] | None = latest[0] if latest else None
        frame = latest[1] if latest else None
        available_dates = set(
            frame["Date"].dt.strftime("%Y-%m-%d") if frame is not None else []
        )
        missing = [
            session for session in expected_sessions if session not in available_dates
        ]
        update: dict[str, Any] | None = None
        if missing:
            try:
                source = self.sources[item.provider]
            except KeyError as exc:
                raise DatasetResolutionError(
                    f"dataset source is unavailable: {item.provider}"
                ) from exc
            fetched = source.fetch(item.instrument, start, end)
            if not isinstance(fetched.source_identity, dict):
                raise DatasetResolutionError(
                    "provider source identity must be an object"
                )
            if fetched.source_identity.get("provider") != item.provider:
                raise DatasetResolutionError(
                    "provider source identity does not match the dataset provider"
                )
            if fetched.source_identity.get("instrument") != item.instrument:
                raise DatasetResolutionError(
                    "provider source identity does not match the dataset instrument"
                )
            try:
                canonical_json_bytes(fetched.source_identity)
                canonical_json_bytes(calendar.source_identity)
            except SchemaValidationError as exc:
                raise DatasetResolutionError(
                    "dataset provenance must contain finite JSON values"
                ) from exc
            try:
                fetched_frame = _normalize_frame(fetched.bars)
            except DatasetValidationError as exc:
                raise DatasetResolutionError(
                    f"provider response data is invalid: {exc}"
                ) from exc
            if frame is not None:
                previous_columns = list(frame.columns)
                missing_columns = [
                    column
                    for column in previous_columns
                    if column not in fetched_frame.columns
                ]
                if missing_columns:
                    raise DatasetResolutionError(
                        "provider response schema cannot extend the latest snapshot: "
                        + ", ".join(missing_columns)
                    )
                fetched_frame = fetched_frame.loc[:, previous_columns]
            fetched_dates = set(fetched_frame["Date"].dt.strftime("%Y-%m-%d"))
            absent = [
                session for session in expected_sessions if session not in fetched_dates
            ]
            if absent:
                raise DatasetResolutionError(
                    f"provider response is missing expected {item.calendar} sessions "
                    "(suspension or provider-gap evidence is required): "
                    + ", ".join(absent)
                )
            unexpected = sorted(fetched_dates - set(expected_sessions))
            if unexpected:
                raise DatasetResolutionError(
                    f"provider response contains non-{item.calendar} sessions: "
                    + ", ".join(unexpected)
                )
            update = reconcile_daily_history(
                fetched_frame,
                expected_sessions,
                self.catalog.state_root,
                item.metadata,
                start,
                end,
                source_identity=fetched.source_identity,
                expected_sessions_source=calendar.source_identity,
            )
            verified = _verify_snapshot(
                Path(str(update["path"])),
                str(update["snapshot_id"]),
                include_frame=True,
            )
            if not isinstance(verified, tuple):
                raise DatasetResolutionError(
                    "snapshot verifier did not return repaired market data"
                )
            manifest, frame = verified
            repaired_dates = set(frame["Date"].dt.strftime("%Y-%m-%d"))
            remaining = [
                session for session in expected_sessions if session not in repaired_dates
            ]
            if remaining:
                raise DatasetResolutionError(
                    "repaired snapshot remains incomplete: " + ", ".join(remaining)
                )
        assert manifest is not None
        try:
            lineage = snapshot_update_lineage(
                self.catalog.state_root,
                item.instrument,
                manifest["snapshot_id"],
            )
        except (DatasetValidationError, OSError, RuntimeError) as exc:
            raise DatasetResolutionError(
                f"dataset update lineage failed verification: {exc}"
            ) from exc
        return {
            "dataset_id": item.dataset_id,
            "name": item.name,
            "instrument": item.instrument,
            "provider": item.provider,
            "market": item.market,
            "currency": item.currency,
            "adjustment": item.adjustment,
            "requested_start": start,
            "requested_end": end,
            "effective_start": effective_start,
            "effective_end": effective_end,
            "snapshot_id": manifest["snapshot_id"],
            "canonical_sha256": manifest["canonical_sha256"],
            "snapshot_data_start": manifest["data_start"],
            "snapshot_data_end": manifest["data_end"],
            "lineage": lineage,
            "update_id": update["update_id"] if update else None,
            "update_path": update["update_path"] if update else None,
        }
