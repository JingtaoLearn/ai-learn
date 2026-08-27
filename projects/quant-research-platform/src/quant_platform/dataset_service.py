from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd
import requests

from .catalog import Catalog
from .datasets import (
    DatasetValidationError,
    SAFE_INSTRUMENT,
    _normalize_frame,
    _validate_metadata,
    _verify_snapshot,
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
            )
            response.raise_for_status()
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

        for value in quote["volume"]:
            if value is None:
                continue
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                or not float(value).is_integer()
            ):
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
            "lineage": lineage,
            "update_id": update["update_id"] if update else None,
            "update_path": update["update_path"] if update else None,
        }
