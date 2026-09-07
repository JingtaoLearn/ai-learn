from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .production_jobs import (
    JobComputation,
    ProductionJobError,
    ProviderClient,
    TrendConfig,
    close_for_slope,
    decision_points,
    evaluate,
    identity,
    identity_canonical_bytes,
    next_weekday,
    normalized_rows,
    parse_manifest,
    render_private_report,
)


class BocomProductionJob:
    job_id = "297c11cad0dc"
    model_id = "bocom-20d-ema5-hysteresis-crossing-v1.2"
    production_manifest_sha256 = "6f9f10ed235c6229582ca2843c8b983a887dbdc0ac289170ca834e580bcae969"
    report_uuid = "8991e9a8-1caa-41f5-b76b-6368259db5b4"
    symbol = "601328.SS"
    config = TrendConfig(20, 5, 0.20, -0.20, date(2025, 1, 2))

    def __init__(self, manifest_path: Path | str):
        self.manifest_bytes = Path(manifest_path).read_bytes()
        self.manifest = parse_manifest(
            self.manifest_bytes,
            expected_sha256=self.production_manifest_sha256,
            model_id=self.model_id,
            parameters={
                "window_sessions": 20,
                "ema_span": 5,
                "buy_threshold_pct_per_day": 0.2,
                "sell_threshold_pct_per_day": -0.2,
                "anchor_date": "2025-01-02",
            },
        )

    def acquire(self, client: ProviderClient, scheduled_for: datetime) -> tuple[str, bytes]:
        period1 = int(datetime(2009, 12, 1, tzinfo=UTC).timestamp())
        period2 = int((scheduled_for.astimezone(UTC) + timedelta(days=3)).timestamp())
        query = urlencode(
            {
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "div,splits",
                "includeAdjustedClose": "true",
            }
        )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{self.symbol}?{query}"
        return url, client.get(
            url,
            headers={
                "User-Agent": "quantresearch-production/1",
                "Accept": "application/json",
            },
            maximum_bytes=16 * 1024 * 1024,
        )

    def _rows(self, raw: bytes) -> list[dict[str, Any]]:
        try:
            payload = json.loads(raw)
            result = payload["chart"]["result"]
            if payload["chart"].get("error") is not None or len(result) != 1:
                raise ProductionJobError("Yahoo chart generation is not singular")
            result = result[0]
            if result["meta"].get("symbol") != self.symbol:
                raise ProductionJobError("Yahoo symbol does not match BOCOM")
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"]
            adjusted = result["indicators"]["adjclose"]
            if len(quote) != 1 or len(adjusted) != 1:
                raise ProductionJobError("Yahoo indicator generation is not singular")
            quote, adjusted = quote[0], adjusted[0]["adjclose"]
        except ProductionJobError:
            raise
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProductionJobError("Yahoo chart schema is invalid") from exc
        lengths = {len(timestamps), len(adjusted)} | {
            len(quote.get(field, [])) for field in ("open", "high", "low", "close", "volume")
        }
        if len(lengths) != 1:
            raise ProductionJobError("Yahoo arrays are not aligned")
        rows: dict[date, dict[str, Any]] = {}
        for index, timestamp in enumerate(timestamps):
            values = {field: quote[field][index] for field in ("open", "high", "low", "close", "volume")}
            adjusted_close = adjusted[index]
            if values["open"] is None or values["close"] is None or adjusted_close is None:
                continue
            close = float(values["close"])
            factor = float(adjusted_close) / close
            session = datetime.fromtimestamp(int(timestamp), ZoneInfo("Asia/Shanghai")).date()
            numeric = [float(values[field]) for field in ("open", "close")] + [float(adjusted_close)]
            if any(not math.isfinite(value) or value <= 0 for value in numeric):
                raise ProductionJobError("Yahoo BOCOM prices are invalid")
            if session in rows:
                raise ProductionJobError("Yahoo BOCOM date is duplicated")
            rows[session] = {
                "date": session,
                "open": float(values["open"]),
                "high": float(values["high"]) if values["high"] is not None else None,
                "low": float(values["low"]) if values["low"] is not None else None,
                "close": close,
                "volume": int(values["volume"]) if values["volume"] is not None else None,
                "signal_close": float(adjusted_close),
                "adjusted_open": float(values["open"]) * factor,
            }
        ordered = [rows[key] for key in sorted(rows)]
        if len(ordered) < self.config.window_sessions + 2:
            raise ProductionJobError("Yahoo BOCOM history is insufficient")
        return ordered

    def compute(
        self, raw: bytes, provider_url: str, scheduled_for: datetime
    ) -> JobComputation:
        rows = self._rows(raw)
        points = decision_points(rows, self.config)
        action = evaluate(points, self.config)
        latest, previous = rows[-1], rows[-2]
        action.update(
            {
                "generated_at": scheduled_for.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "job_id": self.job_id,
                "model_version": self.model_id,
                "production_manifest_sha256": self.production_manifest_sha256,
                "symbol": self.symbol,
                "name": "交通银行",
                "latest_market_date": latest["date"].isoformat(),
                "latest_close": latest["close"],
                "previous_close": previous["close"],
                "daily_change_pct": (latest["close"] / previous["close"] - 1) * 100,
                "next_session_date_estimate": next_weekday(latest["date"]).isoformat(),
                "rules": {
                    "window": 20,
                    "ema_span": 5,
                    "buy_crossing_pct": 0.2,
                    "sell_crossing_pct": -0.2,
                    "initial_position": 0,
                    "anchor_date": "2025-01-02",
                },
                "costs": {"buy_cost_bps": 8, "sell_cost_bps": 13},
                "automatic_ordering": False,
                "report_uuid": self.report_uuid,
                "next_completed_close_scenarios": {
                    "buy_threshold_equivalent_raw_close": close_for_slope(rows, self.config, 0.2),
                    "sell_threshold_equivalent_raw_close": close_for_slope(rows, self.config, -0.2),
                    "basis": "raw close assuming no corporate action",
                },
            }
        )
        normalized = normalized_rows(rows)
        experiment_id = identity(
            b"quantresearch-production-experiment/v1\0",
            {
                "job_id": self.job_id,
                "model": self.production_manifest_sha256,
                "snapshot": identity_canonical_bytes(
                    b"quantresearch-production-dataset/v1\0", normalized
                ),
            },
        )
        attempt_id = identity(
            b"quantresearch-production-attempt/v1\0",
            {"experiment_id": experiment_id, "scheduled_for": action["generated_at"]},
        )
        report = render_private_report(
            display_name="交通银行",
            report_uuid=self.report_uuid,
            qualification="KNOWN_EVENT_CORRECTED_PARTIAL",
            action=action,
            model_id=self.model_id,
            costs="buy_cost_bps=8; sell_cost_bps=13",
        )
        notification = (
            f"交通银行 {action['action']} · {action['latest_market_date']} · report {self.report_uuid}"
        ).encode("utf-8")
        return JobComputation(
            self.job_id,
            self.model_id,
            self.production_manifest_sha256,
            self.report_uuid,
            provider_url,
            "bocom-yahoo-chart.json",
            raw,
            normalized.value,
            action,
            report,
            notification,
            experiment_id,
            attempt_id,
        )
