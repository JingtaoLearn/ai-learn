from __future__ import annotations

import csv
import io
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping

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


class GoldProductionJob:
    job_id = "1cd5557264db"
    model_id = "gold-au9999-ols55-ema1-b0175-s0275-v1"
    production_manifest_sha256 = "8153c89ba46ce4a52b39b914a706aa3a321caacf01366e3f2e4e7087a2e3e745"
    report_uuid = "f642b386-74c0-4e9f-92e6-563e7c6a5d69"
    config = TrendConfig(55, 1, 0.175, -0.275, date(2021, 5, 10))

    def __init__(self, manifest_path: Path | str):
        self.manifest_bytes = Path(manifest_path).read_bytes()
        self.manifest = parse_manifest(
            self.manifest_bytes,
            expected_sha256=self.production_manifest_sha256,
            model_id=self.model_id,
            parameters={
                "window_sessions": 55,
                "ema_span": 1,
                "buy_threshold_pct_per_day": 0.175,
                "sell_threshold_pct_per_day": -0.275,
                "anchor_date": "2021-05-10",
            },
        )

    def acquire(self, client: ProviderClient, scheduled_for: datetime) -> tuple[str, bytes]:
        end = scheduled_for.date().isoformat()
        url = (
            "https://vip.stock.finance.sina.com.cn/q/view/download_gold_history.php"
            f"?breed=AU9999&start=2021-01-01&end={end}"
        )
        return url, client.get(
            url,
            headers={
                "User-Agent": "quantresearch-production/1",
                "Accept": "text/plain,*/*",
            },
            maximum_bytes=16 * 1024 * 1024,
        )

    @staticmethod
    def _decoded(raw: bytes) -> str:
        for encoding in ("gb18030", "utf-8"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ProductionJobError("Gold history encoding is invalid")

    def _rows(self, raw: bytes) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(self._decoded(raw)), delimiter="\t")
        chinese = {"contract": "合约", "date": "日期", "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价"}
        english = {key: key for key in chinese}
        fields = set(reader.fieldnames or [])
        names = chinese if set(chinese.values()) <= fields else english
        if not set(names.values()) <= fields:
            raise ProductionJobError("Gold history columns are invalid")
        by_date: dict[date, dict[str, Any]] = {}
        for source in reader:
            if str(source[names["contract"]]).strip().casefold() not in {"au99.99", "au9999"}:
                continue
            session = date.fromisoformat(str(source[names["date"]]).strip())
            values = {
                key: float(str(source[names[key]]).replace(",", ""))
                for key in ("open", "high", "low", "close")
            }
            if all(value == 0 for value in values.values()):
                continue
            if any(not math.isfinite(value) or value <= 0 for value in values.values()):
                raise ProductionJobError("Gold history contains invalid prices")
            if not (
                values["low"] <= values["open"] <= values["high"]
                and values["low"] <= values["close"] <= values["high"]
            ):
                raise ProductionJobError("Gold history OHLC bounds are invalid")
            if session in by_date:
                raise ProductionJobError("Gold history date is duplicated")
            by_date[session] = {"date": session, **values, "signal_close": values["close"]}
        rows = [by_date[key] for key in sorted(by_date)]
        if len(rows) < self.config.window_sessions + 2:
            raise ProductionJobError("Gold history is insufficient")
        return rows

    @staticmethod
    def render_notification(action: Mapping[str, Any]) -> bytes:
        positions = {0: "空仓（0%）", 1: "持有（100%）"}
        action_labels = {"WAIT": "等待", "BUY": "买入", "HOLD": "继续持有", "SELL": "卖出"}
        current_state = int(action["state_before_next"])
        target_state = int(action["target_state"])
        if current_state not in positions or target_state not in positions:
            raise ProductionJobError("Gold notification position is invalid")
        action_name = str(action["action"])
        if action_name not in action_labels:
            raise ProductionJobError("Gold notification action is invalid")
        scenarios = action["next_completed_close_scenarios"]
        if target_state == 0:
            boundary_name = "买入"
            boundary = float(scenarios["buy_threshold_equivalent_cny_per_g"])
            threshold = GoldProductionJob.config.buy_threshold_pct_per_day
        else:
            boundary_name = "卖出"
            boundary = float(scenarios["sell_threshold_equivalent_cny_per_g"])
            threshold = GoldProductionJob.config.sell_threshold_pct_per_day
        lines = [
            f"黄金生产信号｜市场日期：{action['latest_market_date']}",
            (
                f"完成收盘：{float(action['latest_close_cny_per_g']):.2f} 元/克；"
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
                f"下一完整收盘{boundary_name}边界：{boundary:.2f} 元/克"
                f"（斜率 {threshold:+.3f}%/日）"
            ),
            (
                f"如本次动作为 BUY/SELL，执行时点：{action['next_trade_date_estimate']} "
                "下一交易日开盘；"
                "仅人工决策，不会自动下单（automatic_ordering=false）"
            ),
            (
                "口径：SGE_AU9999_PROXY；FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G；"
                "市场代理评估，不代表招行实际可成交收益"
            ),
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
                "production_research_run_id": self.manifest["research_run_id"],
                "latest_market_date": latest["date"].isoformat(),
                "latest_close_cny_per_g": latest["close"],
                "previous_close_cny_per_g": previous["close"],
                "daily_change_pct": (latest["close"] / previous["close"] - 1) * 100,
                "next_trade_date_estimate": next_weekday(latest["date"]).isoformat(),
                "parameters": {
                    "window_sessions": 55,
                    "ema_span": 1,
                    "buy_threshold_pct_per_day": 0.175,
                    "sell_threshold_pct_per_day": -0.275,
                    "anchor_date": "2021-05-10",
                    "roundtrip_spread_cny_per_g": 5.0,
                },
                "automatic_ordering": False,
                "report_uuid": self.report_uuid,
                "next_completed_close_scenarios": {
                    "buy_threshold_equivalent_cny_per_g": close_for_slope(rows, self.config, 0.175),
                    "sell_threshold_equivalent_cny_per_g": close_for_slope(rows, self.config, -0.275),
                    "basis": "completed Au99.99 close",
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
            display_name="黄金（Au99.99）",
            report_uuid=self.report_uuid,
            qualification="OVERFIT_RISK_SUBSTANTIATED",
            action=action,
            model_id=self.model_id,
            costs="roundtrip_spread_cny_per_g=5.0",
        )
        notification = self.render_notification(action)
        return JobComputation(
            self.job_id,
            self.model_id,
            self.production_manifest_sha256,
            self.report_uuid,
            provider_url,
            "gold-au9999.tsv",
            raw,
            normalized.value,
            action,
            report,
            notification,
            experiment_id,
            attempt_id,
        )
