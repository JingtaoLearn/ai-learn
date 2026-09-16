from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .production_jobs import (
    JobComputation,
    ProductionJobError,
    ProductionReportSpec,
    ProviderClient,
    TrendConfig,
    build_canonical_production_report,
    close_for_slope,
    decision_points,
    evaluate,
    identity,
    identity_canonical_bytes,
    next_weekday,
    normalized_rows,
    parse_manifest,
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
            source_events = result.get("events", {})
            if not isinstance(source_events, dict):
                raise ProductionJobError("Yahoo corporate-action events are invalid")
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
                "corporate_actions": [],
            }
        for event_type, source_name in (("SPLIT", "splits"), ("DIVIDEND", "dividends")):
            source_group = source_events.get(source_name, {})
            if not isinstance(source_group, dict):
                raise ProductionJobError("Yahoo corporate-action event group is invalid")
            for source_event_id, source_event in source_group.items():
                if not isinstance(source_event_id, str) or not isinstance(source_event, dict):
                    raise ProductionJobError("Yahoo corporate-action event is invalid")
                try:
                    source_timestamp = int(source_event["date"])
                    session = datetime.fromtimestamp(
                        source_timestamp, ZoneInfo("Asia/Shanghai")
                    ).date()
                    if event_type == "SPLIT":
                        numerator = float(source_event["numerator"])
                        denominator = float(source_event["denominator"])
                        ratio = numerator / denominator
                        if not all(
                            math.isfinite(value) and value > 0
                            for value in (numerator, denominator, ratio)
                        ):
                            raise ProductionJobError("Yahoo split terms are invalid")
                        action = {
                            "type": event_type,
                            "effective_date": session.isoformat(),
                            "ratio": ratio,
                            "numerator": numerator,
                            "denominator": denominator,
                            "source_event_id": source_event_id,
                            "source_event_timestamp": source_timestamp,
                        }
                    else:
                        amount = float(source_event["amount"])
                        if not math.isfinite(amount) or amount < 0:
                            raise ProductionJobError("Yahoo dividend amount is invalid")
                        action = {
                            "type": event_type,
                            "effective_date": session.isoformat(),
                            "amount_per_share_cny": amount,
                            "source_amount_per_share_cny": amount,
                            "source_event_id": source_event_id,
                            "source_event_timestamp": source_timestamp,
                            "event_date_basis": "Yahoo ex-date evidence",
                            "payment_date": None,
                            "recognition_basis": (
                                "gross pre-tax ex-date receivable / total-return accrual"
                            ),
                        }
                except ProductionJobError:
                    raise
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise ProductionJobError("Yahoo corporate-action event schema is invalid") from exc
                if session not in rows:
                    raise ProductionJobError(
                        "Yahoo corporate-action event has no observed market session"
                    )
                rows[session]["corporate_actions"].append(action)
        for row in rows.values():
            row["corporate_actions"].sort(
                key=lambda item: (
                    0 if item["type"] == "SPLIT" else 1,
                    item["source_event_id"],
                )
            )
        splits = [
            action
            for row in rows.values()
            for action in row["corporate_actions"]
            if action["type"] == "SPLIT"
        ]
        for session, row in rows.items():
            factor = math.prod(
                float(action["ratio"])
                for action in splits
                if session.isoformat() < action["effective_date"]
            )
            for field in ("open", "high", "low", "close"):
                if row[field] is not None:
                    row[field] = float(row[field]) * factor
            for action in row["corporate_actions"]:
                if action["type"] == "DIVIDEND":
                    action["amount_per_share_cny"] = (
                        float(action["source_amount_per_share_cny"]) * factor
                    )
                    action["source_dividend_split_adjustment_factor"] = factor
            row["source_quote_split_unadjustment_factor"] = factor
        ordered = [rows[key] for key in sorted(rows)]
        if len(ordered) < self.config.window_sessions + 2:
            raise ProductionJobError("Yahoo BOCOM history is insufficient")
        return ordered

    @staticmethod
    def render_notification(action: Mapping[str, Any]) -> bytes:
        positions = {0: "空仓（0%）", 1: "持有（100%）"}
        action_labels = {"WAIT": "等待", "BUY": "买入", "HOLD": "继续持有", "SELL": "卖出"}
        current_state = int(action["state_before_next"])
        target_state = int(action["target_state"])
        if current_state not in positions or target_state not in positions:
            raise ProductionJobError("BOCOM notification position is invalid")
        action_name = str(action["action"])
        if action_name not in action_labels:
            raise ProductionJobError("BOCOM notification action is invalid")
        scenarios = action["next_completed_close_scenarios"]
        if target_state == 0:
            boundary_name = "买入"
            boundary = float(scenarios["buy_threshold_equivalent_raw_close"])
            threshold = BocomProductionJob.config.buy_threshold_pct_per_day
        else:
            boundary_name = "卖出"
            boundary = float(scenarios["sell_threshold_equivalent_raw_close"])
            threshold = BocomProductionJob.config.sell_threshold_pct_per_day
        lines = [
            f"交通银行生产信号｜市场日期：{action['latest_market_date']}",
            (
                f"完成收盘：{float(action['latest_close']):.3f} 元/股；"
                f"日涨跌：{float(action['daily_change_pct']):+.2f}%"
            ),
            (
                f"仓位：当前{positions[current_state]} → 目标{positions[target_state]}；"
                f"动作：{action_name}（{action_labels[action_name]}）"
            ),
            (
                f"斜率：上一 {float(action['previous_slope_pct']):+.4f}%/日 → "
                f"当前 {float(action['next_slope_pct']):+.4f}%/日"
            ),
            (
                f"下一完整收盘{boundary_name}边界：{boundary:.3f} 元/股"
                f"（斜率 {threshold:+.3f}%/日；假设无公司行动的原始收盘价）"
            ),
            (
                f"如本次动作为 BUY/SELL，执行时点：{action['next_session_date_estimate']} "
                "下一交易日开盘；"
                "仅人工决策，不会自动下单（automatic_ordering=false）"
            ),
            "成本假设：买入 8 bps；卖出 13 bps",
            f"报告：{action['report_uuid']}",
        ]
        return ("\n".join(lines) + "\n").encode("utf-8")

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
        report = build_canonical_production_report(
            rows=rows,
            points=points,
            config=self.config,
            action=action,
            experiment_id=experiment_id,
            attempt_id=attempt_id,
            provider_url=provider_url,
            normalized_bytes=normalized.value,
            spec=ProductionReportSpec(
                display_name="交通银行",
                qualification="KNOWN_EVENT_CORRECTED_PARTIAL",
                execution_price_key="open",
                mark_price_key="close",
                execution_price_basis=(
                    "next-session as-traded open reconstructed from observed Yahoo "
                    "split-adjusted quote and bound split events"
                ),
                price_unit="CNY_PER_SHARE",
                buy_cost_bps=8.0,
                sell_cost_bps=13.0,
                completed_roundtrip_cost_per_unit=0.0,
                cost_description="buy 8 bps; sell 13 bps",
                limitations=(
                    "Yahoo quote OHLC is split-adjusted; execution and marks are reconstructed to as-traded price units from bound split events before quantity changes are applied.",
                    "Signals use Yahoo adjusted close; P&L uses the reconstructed open/close path, split terms, and gross dividend events bound to this immutable source snapshot.",
                    "Yahoo dividend evidence is split-adjusted and dated by ex-date; amounts are restored by all strictly later split factors and recognized as gross pre-tax ex-date receivables / total-return accruals, not observed cash payments.",
                    "Yahoo does not supply payment date, withholding, tax, cash-in-lieu, or tax-lot authority, so net means net of modeled transaction costs before dividend tax.",
                    "Yahoo adjusted prices and corporate-action history may revise; later revisions create new immutable evidence and never rewrite this result.",
                    "KNOWN_EVENT_CORRECTED_PARTIAL; this report does not promote the model.",
                ),
                corporate_action_source="Yahoo chart events=div,splits",
                corporate_action_revision_policy=(
                    "Yahoo adjusted prices and corporate-action history may revise; this immutable "
                    "snapshot binds the exact event payload used for accounting"
                ),
            ),
        )
        notification = self.render_notification(action)
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
            report.operator_identity,
            report.evidence_files,
            report.html,
            notification,
            experiment_id,
            attempt_id,
        )
