from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.parse import urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .attempt_report import (
    AttemptReportError,
    render_report_document,
    validate_report_document,
)
from .production_client import (
    ClientTLS,
    ProductionClient,
    ProductionClientError,
    REPORT_EVIDENCE_FILE_NAMES,
    StdlibMTLSTransport,
)
from .production_contract import (
    ProductionContractError,
    ProductionRequest,
    canonical_json_bytes,
    canonical_scheduled_fire,
)


TIMEZONE = ZoneInfo("Asia/Shanghai")
DELIVERY = "feishu:oc_33bdb4845220ee3788fe50c50cf333ed"
DEFAULT_BASE_URL = "https://127.0.0.1:8443"
REPORT_BASE_URL = "https://share.ai.jingtao.fun"
REPORT_ROOT = Path("/data_static/share-hosting")
REPORT_OPERATOR = {
    "api_version": 2,
    "content_digest": "275a68f011fe9b45fadc8e1960966f5e7a94809df975507c15f92025b696932f",
    "operator_id": "canonical_attempt_report",
    "source_sha256": "11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8",
    "version": "1.0.0",
}


@dataclass(frozen=True)
class ScheduledJob:
    job_id: str
    name: str
    script: str
    model_id: str
    production_manifest_sha256: str
    report_filename: str
    schedule: str
    fire_hour: int
    fire_minute: int
    audit_prompt: str


def _audit_prompt(
    script: str,
    job_id: str,
    model_id: str,
    production_manifest_sha256: str,
    schedule: str,
) -> str:
    return (
        f"PRODUCTION AUDIT COPY — execute only {script}. "
        f"job_id={job_id}; model_id={model_id}; schedule={schedule} Asia/Shanghai; "
        "transport=loopback HTTPS mTLS via host-managed tunnel; "
        "behavior=trigger, verify immutable result/files, publish and read back the stable report, "
        "verify canonical_attempt_report@1.0.0 identity and bound ReportDocument evidence, "
        "render from the verified source "
        "notification, then emit notification only; "
        "local_compute=false; flearn_fallback=false; automatic_ordering=false; "
        f"production_manifest_sha256={production_manifest_sha256}."
    )


_GOLD_MANIFEST = "8153c89ba46ce4a52b39b914a706aa3a321caacf01366e3f2e4e7087a2e3e745"
_BOCOM_MANIFEST = "6f9f10ed235c6229582ca2843c8b983a887dbdc0ac289170ca834e580bcae969"
_GOLD_SCHEDULE = "40 8 * * 1-5"
_BOCOM_SCHEDULE = "45 8 * * 1-5"

JOBS = {
    "1cd5557264db": ScheduledJob(
        job_id="1cd5557264db",
        name="gold-production-daily-action",
        script="gold_production_api_action.py",
        model_id="gold-au9999-ols55-ema1-b0175-s0275-v1",
        production_manifest_sha256=_GOLD_MANIFEST,
        report_filename="f642b386-74c0-4e9f-92e6-563e7c6a5d69.html",
        schedule=_GOLD_SCHEDULE,
        fire_hour=8,
        fire_minute=40,
        audit_prompt=_audit_prompt(
            "gold_production_api_action.py",
            "1cd5557264db",
            "gold-au9999-ols55-ema1-b0175-s0275-v1",
            _GOLD_MANIFEST,
            _GOLD_SCHEDULE,
        ),
    ),
    "297c11cad0dc": ScheduledJob(
        job_id="297c11cad0dc",
        name="bocom-production-daily-action",
        script="bocom_production_api_action.py",
        model_id="bocom-20d-ema5-hysteresis-crossing-v1.2",
        production_manifest_sha256=_BOCOM_MANIFEST,
        report_filename="8991e9a8-1caa-41f5-b76b-6368259db5b4.html",
        schedule=_BOCOM_SCHEDULE,
        fire_hour=8,
        fire_minute=45,
        audit_prompt=_audit_prompt(
            "bocom_production_api_action.py",
            "297c11cad0dc",
            "bocom-20d-ema5-hysteresis-crossing-v1.2",
            _BOCOM_MANIFEST,
            _BOCOM_SCHEDULE,
        ),
    ),
}


def scheduled_fire_for(job: ScheduledJob, now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ProductionContractError("current time must be timezone-aware")
    local = now.astimezone(TIMEZONE)
    if local.weekday() >= 5:
        raise ProductionContractError("scheduled client cannot derive a weekend fire")
    fire = local.replace(hour=job.fire_hour, minute=job.fire_minute, second=0, microsecond=0)
    if local < fire:
        raise ProductionContractError("scheduled client cannot run before its canonical fire")
    request_fire = local.replace(hour=8, minute=40, second=0, microsecond=0)
    return canonical_scheduled_fire(request_fire.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _read_jobs(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_absolute():
        raise ProductionClientError("Cron jobs path must be absolute")
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > 1_048_576
    ):
        raise ProductionClientError("Cron jobs file is unsafe")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionClientError("Cron jobs file is invalid") from exc
    jobs = value.get("jobs") if isinstance(value, dict) else None
    if not isinstance(jobs, list) or not all(isinstance(item, dict) for item in jobs):
        raise ProductionClientError("Cron jobs configuration is invalid")
    return jobs


def validate_schedule_record(job: ScheduledJob, jobs_path: Path) -> None:
    matches = [item for item in _read_jobs(jobs_path) if item.get("id") == job.job_id]
    if len(matches) != 1:
        raise ProductionClientError("exactly one scheduled job record is required")
    record = matches[0]
    expected = {
        "name": job.name,
        "script": job.script,
        "prompt": job.audit_prompt,
        "deliver": DELIVERY,
        "no_agent": True,
        "enabled": True,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ProductionClientError("scheduled job record differs from the reviewed client")
    schedule = record.get("schedule")
    if (
        not isinstance(schedule, dict)
        or schedule.get("kind") != "cron"
        or schedule.get("expr") != job.schedule
    ):
        raise ProductionClientError("scheduled job timing differs from the reviewed client")


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"


def _match(pattern: str, line: str) -> dict[str, str]:
    matched = re.fullmatch(pattern, line)
    if matched is None:
        raise ProductionClientError("verified source notification format is invalid")
    return matched.groupdict()


def _verified_action_from_notification(job: ScheduledJob, payload: bytes) -> dict[str, Any]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ProductionClientError("source notification is not UTF-8") from exc
    if len(lines) != 8:
        raise ProductionClientError("verified source notification line count is invalid")
    position_values = {"空仓（0%）": 0, "持有（100%）": 1}
    action_labels = {"WAIT": "等待", "BUY": "买入", "HOLD": "继续持有", "SELL": "卖出"}
    asset = "黄金" if job.job_id == "1cd5557264db" else "交通银行"
    identity = _match(rf"{asset}生产信号｜市场日期：(?P<date>\d{{4}}-\d{{2}}-\d{{2}})", lines[0])
    unit = "元/克" if job.job_id == "1cd5557264db" else "元/股"
    price = _match(
        rf"完成收盘：(?P<close>{_NUMBER}) {unit}；日涨跌：(?P<change>{_NUMBER})%", lines[1]
    )
    position = _match(
        r"仓位：当前(?P<current>空仓（0%）|持有（100%）) → "
        r"目标(?P<target>空仓（0%）|持有（100%）)；"
        r"动作：(?P<action>WAIT|BUY|HOLD|SELL)（(?P<label>等待|买入|继续持有|卖出)）",
        lines[2],
    )
    signal = _match(
        rf"斜率：上一 (?P<previous>{_NUMBER})%/日 → 当前 (?P<current>{_NUMBER})%/日",
        lines[3],
    )
    action_name = position["action"]
    current_state = position_values[position["current"]]
    target_state = position_values[position["target"]]
    if (
        action_labels[action_name] != position["label"]
        or (action_name, current_state, target_state)
        not in {("WAIT", 0, 0), ("BUY", 0, 1), ("HOLD", 1, 1), ("SELL", 1, 0)}
    ):
        raise ProductionClientError("source notification action and position are inconsistent")
    expected_boundary = "买入" if target_state == 0 else "卖出"
    report_uuid = job.report_filename.removesuffix(".html")

    common = {
        "job_id": job.job_id,
        "model_version": job.model_id,
        "production_manifest_sha256": job.production_manifest_sha256,
        "report_uuid": report_uuid,
        "automatic_ordering": False,
        "action": action_name,
        "state_before_next": current_state,
        "target_state": target_state,
        "latest_market_date": identity["date"],
        "daily_change_pct": float(price["change"]),
        "previous_slope_pct": float(signal["previous"]),
        "next_slope_pct": float(signal["current"]),
    }
    if job.job_id == "1cd5557264db":
        boundary = _match(
            rf"下一完整收盘(?P<name>买入|卖出)边界：(?P<value>{_NUMBER}) 元/克"
            rf"（斜率 (?P<threshold>{_NUMBER})%/日）",
            lines[4],
        )
        timing = _match(
            r"如本次动作为 BUY/SELL，执行时点：(?P<date>\d{4}-\d{2}-\d{2}) "
            r"下一交易日开盘；仅人工决策，不会自动下单（automatic_ordering=false）",
            lines[5],
        )
        if (
            boundary["name"] != expected_boundary
            or float(boundary["threshold"]) != (0.175 if target_state == 0 else -0.275)
            or lines[6]
            != "口径：SGE_AU9999_PROXY；FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G；市场代理评估，不代表招行实际可成交收益"
            or lines[7] != f"报告：{report_uuid}"
        ):
            raise ProductionClientError("Gold source notification differs from the frozen contract")
        return common | {
            "latest_close_cny_per_g": float(price["close"]),
            "next_trade_date_estimate": timing["date"],
            "parameters": {
                "buy_threshold_pct_per_day": 0.175,
                "sell_threshold_pct_per_day": -0.275,
                "roundtrip_spread_cny_per_g": 5.0,
            },
            "next_completed_close_scenarios": {
                f"{'buy' if target_state == 0 else 'sell'}_threshold_equivalent_cny_per_g": float(
                    boundary["value"]
                )
            },
        }
    if job.job_id != "297c11cad0dc":
        raise ProductionClientError("unknown scheduled job presentation")
    boundary = _match(
        rf"下一完整收盘(?P<name>买入|卖出)边界：(?P<value>{_NUMBER}) 元/股"
        rf"（斜率 (?P<threshold>{_NUMBER})%/日；假设无公司行动的原始收盘价）",
        lines[4],
    )
    timing = _match(
        r"如本次动作为 BUY/SELL，执行时点：(?P<date>\d{4}-\d{2}-\d{2}) "
        r"下一交易日开盘；仅人工决策，不会自动下单（automatic_ordering=false）",
        lines[5],
    )
    if (
        boundary["name"] != expected_boundary
        or float(boundary["threshold"]) != (0.2 if target_state == 0 else -0.2)
        or lines[6] != "成本假设：买入 8 bps；卖出 13 bps"
        or lines[7] != f"报告：{report_uuid}"
    ):
        raise ProductionClientError("BOCOM source notification differs from the frozen contract")
    return common | {
        "latest_close": float(price["close"]),
        "next_session_date_estimate": timing["date"],
        "rules": {"buy_crossing_pct": 0.2, "sell_crossing_pct": -0.2},
        "costs": {"buy_cost_bps": 8, "sell_cost_bps": 13},
        "next_completed_close_scenarios": {
            f"{'buy' if target_state == 0 else 'sell'}_threshold_equivalent_raw_close": float(
                boundary["value"]
            )
        },
    }


def _verified_action_from_report(job: ScheduledJob, payload: bytes) -> dict[str, Any]:
    try:
        document = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProductionClientError("report payload is not UTF-8") from exc
    def report_field(field_id: str) -> str:
        label = re.escape(field_id.replace("_", " "))
        matched = re.search(
            rf'<tr><th scope="row">{label}</th><td><span>AVAILABLE</span><br><code>'
            r"(?P<value>.*?)</code></td></tr>",
            document,
            re.DOTALL,
        )
        if matched is None:
            raise ProductionClientError(f"report field is absent: {field_id}")
        return html.unescape(matched.group("value"))

    try:
        configuration = json.loads(report_field("template_parameters"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProductionClientError("report canonical action is invalid") from exc
    action = configuration.get("current_action") if isinstance(configuration, dict) else None
    embedded_operator = {
        "api_version": 2,
        "content_digest": report_field("operator_content_digest"),
        "operator_id": report_field("operator_id"),
        "source_sha256": report_field("operator_source_sha256"),
        "version": report_field("operator_version"),
    }
    if (
        not isinstance(action, dict)
        or embedded_operator != REPORT_OPERATOR
        or action.get("job_id") != job.job_id
        or action.get("model_version") != job.model_id
        or action.get("production_manifest_sha256") != job.production_manifest_sha256
        or action.get("report_uuid") != job.report_filename.removesuffix(".html")
        or action.get("automatic_ordering") is not False
    ):
        raise ProductionClientError("report canonical action differs from the scheduled job contract")
    return action


def _verified_action_from_report_document(
    job: ScheduledJob, payload: bytes
) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionClientError("report document is not JSON") from exc
    if canonical_json_bytes(document) != payload or not isinstance(document, dict):
        raise ProductionClientError("report document bytes are not canonical")
    try:
        document = validate_report_document(document)
    except AttemptReportError as exc:
        raise ProductionClientError("report document contract is invalid") from exc
    fields = {}
    for section in document["sections"]:
        if not isinstance(section, dict) or not isinstance(section.get("fields"), list):
            raise ProductionClientError("report document sections are invalid")
        for field in section["fields"]:
            if not isinstance(field, dict) or not isinstance(field.get("field_id"), str):
                raise ProductionClientError("report document fields are invalid")
            field_id = field["field_id"]
            if field_id in fields:
                raise ProductionClientError("report document field identity is duplicated")
            fields[field_id] = field
    configuration = fields.get("template_parameters", {}).get("raw")
    action = configuration.get("current_action") if isinstance(configuration, dict) else None
    embedded_operator = {
        "api_version": 2,
        "content_digest": fields.get("operator_content_digest", {}).get("raw"),
        "operator_id": fields.get("operator_id", {}).get("raw"),
        "source_sha256": fields.get("operator_source_sha256", {}).get("raw"),
        "version": fields.get("operator_version", {}).get("raw"),
    }
    if (
        not isinstance(action, dict)
        or embedded_operator != REPORT_OPERATOR
        or action.get("job_id") != job.job_id
        or action.get("model_version") != job.model_id
        or action.get("production_manifest_sha256") != job.production_manifest_sha256
        or action.get("report_uuid") != job.report_filename.removesuffix(".html")
        or action.get("automatic_ordering") is not False
    ):
        raise ProductionClientError("report document action or operator does not verify")
    return action


def _verified_json_evidence(evidence: Mapping[str, bytes], name: str) -> Any:
    payload = evidence[name]
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionClientError(f"report evidence is not JSON: {name}") from exc
    if canonical_json_bytes(value) != payload:
        raise ProductionClientError(f"report evidence is not canonical: {name}")
    return value


def _csv_evidence(
    evidence: Mapping[str, bytes], name: str, expected_fields: Sequence[str]
) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(evidence[name].decode("utf-8"), newline=""))
        rows = list(reader)
    except UnicodeError as exc:
        raise ProductionClientError(f"report evidence is not UTF-8 CSV: {name}") from exc
    if reader.fieldnames != list(expected_fields) or any(None in row for row in rows):
        raise ProductionClientError(f"report evidence CSV is malformed: {name}")
    return rows


def _verify_report_document_sources(
    document: Mapping[str, Any], evidence: Mapping[str, bytes]
) -> dict[str, str]:
    try:
        document = validate_report_document(document)
    except AttemptReportError as exc:
        raise ProductionClientError("report document contract is invalid") from exc
    fields = {
        field["field_id"]: field
        for section in document["sections"]
        for field in section["fields"]
    }
    optional_authority_fields = {
        "total_return_status",
        "matched_exposure_status",
        "ranking_status",
        "promotion_ready",
        "gross_dividends_cny",
        "dividend_tax_cny",
        "outstanding_tax_cny",
        "total_return_attachment",
        "matched_exposure_attachment",
        "study_terminal_attachment",
    }
    if any(
        field["availability"] != "AVAILABLE"
        for field_id, field in fields.items()
        if field_id not in optional_authority_fields
    ):
        raise ProductionClientError("required production field availability is invalid")
    audit = _verified_json_evidence(evidence, "attempt-audit.json")
    descriptor = _verified_json_evidence(evidence, "bundle-descriptor.json")
    configuration = _verified_json_evidence(evidence, "config.json")
    contract = _verified_json_evidence(evidence, "contract.json")
    costs = _verified_json_evidence(evidence, "cost_breakdown.json")
    metrics = _verified_json_evidence(evidence, "metrics.json")
    operator = _verified_json_evidence(evidence, "operator-manifest.json")
    runtime = _verified_json_evidence(evidence, "run_manifest.json")
    daily = _csv_evidence(
        evidence,
        "daily_replay.csv",
        ("Date", "price", "close", "equity", "holdings", "position_after"),
    )
    event_rows = _csv_evidence(
        evidence,
        "events.csv",
        (
            "Date", "side", "price", "quantity", "notional_cny", "commission_cny",
            "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny",
            "cash_before_cny", "cash_after_cny", "holdings_before", "holdings_after",
            "reason",
        ),
    )
    trade_rows = _csv_evidence(
        evidence,
        "trades.csv",
        (
            "entry_date", "entry_price", "quantity", "entry_cost_cny", "exit_date",
            "exit_price", "exit_cost_cny", "status", "gross_pnl_cny", "net_pnl_cny",
            "return",
        ),
    )
    expected_metrics = {
        "initial_capital_cny", "final_equity_cny", "net_profit_cny", "cumulative_return",
        "max_drawdown", "exposure", "buy_and_hold_return", "period_start", "period_end",
        "net_return", "current_position", "closed_trades", "open_trades",
    }
    if (
        not isinstance(audit, dict)
        or set(audit) != {
            "attempt_id", "experiment_id", "run_id", "dataset", "result_digest", "operators"
        }
        or not isinstance(audit["dataset"], dict)
        or set(audit["dataset"]) != {"snapshot_id"}
        or not isinstance(descriptor, dict)
        or set(descriptor) != {
            "bundle_id", "attempt_id", "experiment_id", "core_result_digest", "verification"
        }
        or descriptor["verification"] != {"status": "VERIFIED"}
        or not isinstance(configuration, dict)
        or set(configuration) != {"template"}
        or not isinstance(configuration["template"], dict)
        or set(configuration["template"]) != {"parameters"}
        or not isinstance(configuration["template"]["parameters"], dict)
        or not isinstance(contract, dict)
        or set(contract) != {"purpose", "limitations", "details"}
        or not isinstance(costs, dict)
        or set(costs) != {
            "commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny",
            "total_cost_cny",
        }
        or not isinstance(metrics, dict)
        or set(metrics) != expected_metrics
        or not isinstance(operator, dict)
        or set(operator) != {
            "api_version", "operator_id", "semantic_version", "source", "content_digest"
        }
        or not isinstance(operator["source"], dict)
        or set(operator["source"]) != {"sha256"}
        or not isinstance(runtime, dict)
        or set(runtime) != {"runtime"}
        or not isinstance(runtime["runtime"], dict)
    ):
        raise ProductionClientError("report evidence member contract is invalid")
    parameters = configuration["template"]["parameters"]
    required_parameters = {
        "display_name", "qualification", "current_action", "strategy_parameters",
        "execution_price_basis", "price_unit", "cost_description", "signal_price_path",
        "reporting_account", "event_ledger", "trade_ledger", "holding_spans",
        "performance_summary",
    }
    if set(parameters) != required_parameters:
        raise ProductionClientError("report configuration inventory is invalid")
    details = contract["details"]
    if (
        contract["purpose"] != "PRESENTATION_ONLY"
        or contract["limitations"]
        != [
            "INTEGRITY_IS_NOT_QUALIFICATION",
            "QUALIFICATION_IS_NOT_DEPLOYMENT_OR_TRADING_AUTHORITY",
            "PRESENTATION_ONLY_NO_RECOMPUTATION",
        ]
        or not isinstance(details, list)
        or len(details) < 4
        or not all(isinstance(item, str) and item for item in details)
        or fields["integrity_not_qualification"]["display"] != "; ".join(details)
        or fields["qualification_not_deployment"]["display"] != details[-2]
        or fields["no_recomputation"]["display"] != details[-1]
    ):
        raise ProductionClientError("report contract limitations do not verify")
    action = parameters["current_action"]
    event_ledger = parameters["event_ledger"]
    trade_ledger = parameters["trade_ledger"]
    holding_spans = parameters["holding_spans"]
    summary = parameters["performance_summary"]
    provenance = runtime["runtime"]
    if not all(
        isinstance(value, expected)
        for value, expected in (
            (action, dict),
            (event_ledger, list),
            (trade_ledger, list),
            (holding_spans, list),
            (summary, dict),
        )
    ):
        raise ProductionClientError("report configuration evidence is invalid")
    if action.get("job_id") == "1cd5557264db":
        action_parameters = action.get("parameters")
        expected_strategy = {
            "window_sessions": 55,
            "ema_span": 1,
            "buy_threshold_pct_per_day": 0.175,
            "sell_threshold_pct_per_day": -0.275,
            "anchor_date": "2021-05-10",
        }
        expected_configuration = {
            "display_name": "黄金（Au99.99）",
            "qualification": "OVERFIT_RISK_SUBSTANTIATED",
            "execution_price_basis": "observed next-session Au99.99 raw open",
            "price_unit": "CNY_PER_GRAM",
            "cost_description": "fixed completed roundtrip spread 5 CNY per gram",
        }
        cost_rates = (0.0, 0.0, 5.0)
        mark_price_key = "signal_close"
        action_close_key = "latest_close_cny_per_g"
        action_strategy_matches = isinstance(action_parameters, dict) and all(
            action_parameters.get(key) == value for key, value in expected_strategy.items()
        )
    elif action.get("job_id") == "297c11cad0dc":
        rules = action.get("rules")
        expected_strategy = {
            "window_sessions": 20,
            "ema_span": 5,
            "buy_threshold_pct_per_day": 0.2,
            "sell_threshold_pct_per_day": -0.2,
            "anchor_date": "2025-01-02",
        }
        expected_configuration = {
            "display_name": "交通银行",
            "qualification": "KNOWN_EVENT_CORRECTED_PARTIAL",
            "execution_price_basis": "observed next-session raw open",
            "price_unit": "CNY_PER_SHARE",
            "cost_description": "buy 8 bps; sell 13 bps",
        }
        cost_rates = (8.0, 13.0, 0.0)
        mark_price_key = "close"
        action_close_key = "latest_close"
        action_strategy_matches = isinstance(rules, dict) and rules == {
            "window": 20,
            "ema_span": 5,
            "buy_crossing_pct": 0.2,
            "sell_crossing_pct": -0.2,
            "initial_position": 0,
            "anchor_date": "2025-01-02",
        }
    else:
        raise ProductionClientError("report configuration names an unknown production job")
    if (
        not action_strategy_matches
        or parameters["strategy_parameters"] != expected_strategy
        or any(parameters[key] != value for key, value in expected_configuration.items())
        or parameters["reporting_account"]
        != {
            "initial_capital_cny": 1_000_000.0,
            "quantity_rule": "integer units purchased with available cash at each BUY",
            "lot_size": 1,
            "actual_invested_capital": False,
        }
    ):
        raise ProductionClientError("report configuration differs from frozen production meaning")
    if (
        set(provenance)
        != {
            "job_id", "report_uuid", "model_id", "production_manifest_sha256",
            "provider_source", "provider_request", "provider_request_sha256", "market_window",
            "performance_window", "execution_authority", "trigger_role", "local_compute",
            "feng_fallback",
        }
        or provenance["job_id"] != action.get("job_id")
        or provenance["report_uuid"] != action.get("report_uuid")
        or provenance["model_id"] != action.get("model_version")
        or provenance["production_manifest_sha256"]
        != action.get("production_manifest_sha256")
        or provenance["execution_authority"] != "zhlearn production API"
        or provenance["trigger_role"] != "ailearn trigger/read-back/delivery only"
        or provenance["local_compute"] is not False
        or provenance["feng_fallback"] is not False
        or not isinstance(provenance["provider_source"], str)
        or not provenance["provider_source"]
        or not isinstance(provenance["provider_request"], dict)
        or set(provenance["provider_request"])
        != {"scheme", "netloc", "path", "query", "fragment"}
        or not all(isinstance(value, str) for value in provenance["provider_request"].values())
        or provenance["provider_source"]
        != (
            provenance["provider_request"]["netloc"]
            or provenance["provider_request"]["scheme"]
        )
        or hashlib.sha256(
            urlunsplit(
                (
                    str(provenance["provider_request"]["scheme"]),
                    str(provenance["provider_request"]["netloc"]),
                    str(provenance["provider_request"]["path"]),
                    str(provenance["provider_request"]["query"]),
                    str(provenance["provider_request"]["fragment"]),
                )
            ).encode("utf-8")
        ).hexdigest()
        != provenance["provider_request_sha256"]
        or re.fullmatch(r"[0-9a-f]{64}", provenance["provider_request_sha256"]) is None
    ):
        raise ProductionClientError("report provenance authority does not verify")
    next_action_date = action.get(
        "next_trade_date_estimate", action.get("next_session_date_estimate")
    )
    event_cost = sum(float(event["total_cost_cny"]) for event in event_ledger)
    expected_displays = {
        "purpose": (
            f"Historical daily decision evidence; current action {action.get('action')} for "
            f"{next_action_date}; automatic_ordering=false"
        ),
        "bundle_integrity": "Verified canonical production adapter",
        "initial_capital_cny": "1000000 CNY reporting normalization; not actual invested capital",
        "template_parameters": canonical_json_bytes(parameters).decode(),
        "operators": canonical_json_bytes(audit["operators"]).decode(),
        "runtime": canonical_json_bytes(provenance).decode(),
        "price_equity_rows": canonical_json_bytes(fields["price_equity_rows"]["raw"]).decode(),
        "events": canonical_json_bytes(event_ledger).decode(),
        "trades": canonical_json_bytes(trade_ledger).decode(),
        "holdings": canonical_json_bytes(holding_spans).decode(),
        "total_cost_cny": f"{event_cost:.12g} CNY; {parameters['cost_description']}",
        "integrity_not_qualification": "; ".join(details),
        "qualification_not_deployment": details[-2],
        "no_recomputation": details[-1],
    }
    if any(
        field["display"] != expected_displays.get(field_id)
        for field_id, field in fields.items()
        if field["availability"] == "AVAILABLE"
    ):
        raise ProductionClientError("report display differs from verified source evidence")
    normalized = evidence.get("normalized-snapshot.json")
    if type(normalized) is not bytes:
        raise ProductionClientError("normalized snapshot evidence is absent")
    try:
        normalized_value = json.loads(normalized)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionClientError("normalized snapshot evidence is not JSON") from exc
    if canonical_json_bytes(normalized_value) != normalized:
        raise ProductionClientError("normalized snapshot evidence is not canonical")
    if not isinstance(normalized_value, list) or not all(
        isinstance(row, dict) for row in normalized_value
    ):
        raise ProductionClientError("normalized snapshot row inventory is invalid")
    snapshot_sha256 = hashlib.sha256(normalized).hexdigest()
    dataset_identity = hashlib.sha256(
        b"quantresearch-production-dataset/v1\0" + normalized
    ).hexdigest()
    experiment_id = hashlib.sha256(
        b"quantresearch-production-experiment/v1\0"
        + canonical_json_bytes(
            {
                "job_id": action.get("job_id"),
                "model": action.get("production_manifest_sha256"),
                "snapshot": dataset_identity,
            }
        )
    ).hexdigest()
    attempt_id = hashlib.sha256(
        b"quantresearch-production-attempt/v1\0"
        + canonical_json_bytes(
            {"experiment_id": experiment_id, "scheduled_for": action.get("generated_at")}
        )
    ).hexdigest()
    if (
        audit["dataset"]["snapshot_id"] != snapshot_sha256
        or audit["experiment_id"] != experiment_id
        or audit["attempt_id"] != attempt_id
        or audit["run_id"] != f"{action.get('job_id')}:{action.get('generated_at')}"
    ):
        raise ProductionClientError("report run, attempt, or experiment identity does not verify")
    if operator != {
        "api_version": REPORT_OPERATOR["api_version"],
        "operator_id": REPORT_OPERATOR["operator_id"],
        "semantic_version": REPORT_OPERATOR["version"],
        "source": {"sha256": REPORT_OPERATOR["source_sha256"]},
        "content_digest": REPORT_OPERATOR["content_digest"],
    } or audit["operators"] != {"report": REPORT_OPERATOR}:
        raise ProductionClientError("report operator evidence differs from reviewed identity")
    if (
        descriptor["attempt_id"] != audit["attempt_id"]
        or descriptor["experiment_id"] != audit["experiment_id"]
        or descriptor["core_result_digest"] != audit["result_digest"]
    ):
        raise ProductionClientError("report bundle identity bindings differ")
    price_equity = [
        {
            "date": row["Date"],
            "price": float(row["price"]),
            "close": float(row["close"]),
            "equity": float(row["equity"]),
        }
        for row in daily
    ]
    holdings = [
        {
            "date": row["Date"],
            "holdings": int(row["holdings"]),
            "position_after": int(row["position_after"]),
        }
        for row in daily
    ]
    events = [
        {
            key: (
                row[key]
                if key in {"Date", "side", "reason"}
                else int(row[key])
                if key in {"quantity", "holdings_before", "holdings_after"}
                else float(row[key])
            )
            for key in row
        }
        for row in event_rows
    ]
    trades = [
        {
            key: (
                None
                if key in {"exit_date", "exit_price"} and row[key] == ""
                else row[key]
                if key in {"entry_date", "exit_date", "status"}
                else int(row[key])
                if key == "quantity"
                else float(row[key])
            )
            for key in row
        }
        for row in trade_rows
    ]
    if not daily:
        raise ProductionClientError("report price/equity path is empty")
    if len(normalized_value) != len(daily) or any(
        normalized_row.get("date") != daily_row["Date"]
        or not math.isclose(float(normalized_row.get("open", math.nan)), float(daily_row["price"]))
        or not math.isclose(
            float(normalized_row.get(mark_price_key, math.nan)), float(daily_row["close"])
        )
        for normalized_row, daily_row in zip(normalized_value, daily, strict=True)
    ):
        raise ProductionClientError("normalized snapshot and report price path differ")
    expected_signal_path = [
        {"date": row["date"], "signal_price": row["signal_close"]}
        for row in normalized_value
    ]
    if parameters["signal_price_path"] != expected_signal_path:
        raise ProductionClientError("normalized snapshot and signal price path differ")
    if (
        action.get("latest_market_date") != normalized_value[-1].get("date")
        or not math.isclose(
            float(action.get(action_close_key, math.nan)),
            float(normalized_value[-1].get("close", math.nan)),
        )
    ):
        raise ProductionClientError("normalized snapshot and current action differ")
    expected_events = [
        {
            "Date": event["action_date"],
            "side": event["side"],
            "price": event["price"],
            "quantity": event["quantity"],
            "notional_cny": event["notional_cny"],
            "commission_cny": (
                event["total_cost_cny"] if costs["commission_cny"] else 0.0
            ),
            "transfer_fee_cny": 0.0,
            "stamp_tax_cny": 0.0,
            "slippage_cny": (
                event["total_cost_cny"] if costs["slippage_cny"] else 0.0
            ),
            "total_cost_cny": event["total_cost_cny"],
            "cash_before_cny": event["cash_before_cny"],
            "cash_after_cny": event["cash_after_cny"],
            "holdings_before": event["holdings_before_units"],
            "holdings_after": event["holdings_after_units"],
            "reason": canonical_json_bytes(
                {
                    "trigger": event["reason"],
                    "signal_date": event["signal_date"],
                    "action_date": event["action_date"],
                    "price_basis": event["price_basis"],
                    "previous_slope_pct": event["previous_slope_pct"],
                    "signal_slope_pct": event["signal_slope_pct"],
                    "position_before": event["position_before"],
                    "position_after": event["position_after"],
                }
            ).decode(),
        }
        for event in event_ledger
    ]
    expected_trades = [
        {
            "entry_date": trade["entry_date"],
            "entry_price": trade["entry_price"],
            "quantity": trade["quantity"],
            "entry_cost_cny": trade["entry_cost_cny"],
            "exit_date": trade["exit_date"],
            "exit_price": trade["exit_price"],
            "exit_cost_cny": trade["exit_cost_cny"],
            "status": trade["status"],
            "gross_pnl_cny": trade["gross_pnl_cny"],
            "net_pnl_cny": trade["net_pnl_cny"],
            "return": trade["return"],
        }
        for trade in trade_ledger
    ]
    if events != expected_events or trades != expected_trades:
        raise ProductionClientError("report CSV and configuration ledgers differ")
    dates = [row["date"] for row in price_equity]
    if dates != sorted(set(dates)):
        raise ProductionClientError("report price/equity dates are not unique and ordered")
    date_indexes = {value: index for index, value in enumerate(dates)}
    expected_position = 0
    expected_holdings = 0
    buy_events: list[Mapping[str, Any]] = []
    sell_events: list[Mapping[str, Any]] = []
    for event in event_ledger:
        action_index = date_indexes.get(event["action_date"])
        expected_after = 1 if event["side"] == "BUY" else 0
        is_valid_crossing = (
            event["side"] == "BUY"
            and float(event["previous_slope_pct"])
            < float(expected_strategy["buy_threshold_pct_per_day"])
            <= float(event["signal_slope_pct"])
            and event["reason"] == "signal crossed upward through the frozen buy line"
        ) or (
            event["side"] == "SELL"
            and float(event["previous_slope_pct"])
            > float(expected_strategy["sell_threshold_pct_per_day"])
            >= float(event["signal_slope_pct"])
            and event["reason"] == "signal crossed downward through the frozen sell line"
        )
        if (
            event["side"] not in {"BUY", "SELL"}
            or not is_valid_crossing
            or not isinstance(action_index, int)
            or action_index < 1
            or event["action_date"] < expected_strategy["anchor_date"]
            or event["signal_date"] != dates[action_index - 1]
            or not math.isclose(
                float(event["price"]), float(price_equity[action_index]["price"])
            )
            or event["position_before"] != expected_position
            or event["position_after"] != expected_after
            or event["holdings_before_units"] != expected_holdings
            or holdings[action_index]["position_after"] != expected_after
            or holdings[action_index]["holdings"] != event["holdings_after_units"]
        ):
            raise ProductionClientError("report event timing or position transition is invalid")
        if event["side"] == "BUY":
            if expected_position != 0 or event["holdings_after_units"] <= 0:
                raise ProductionClientError("report BUY transition is invalid")
            buy_events.append(event)
        else:
            if expected_position != 1 or event["holdings_after_units"] != 0:
                raise ProductionClientError("report SELL transition is invalid")
            sell_events.append(event)
        expected_position = expected_after
        expected_holdings = event["holdings_after_units"]
    trade_statuses = [trade["status"] for trade in trade_ledger]
    if any(status not in {"OPEN", "CLOSED"} for status in trade_statuses):
        raise ProductionClientError("report trade status is invalid")
    open_trades = trade_statuses.count("OPEN")
    closed_trades = trade_statuses.count("CLOSED")
    final_position = holdings[-1]["position_after"]
    expected_period_start = next(
        (value for value in dates if value >= expected_strategy["anchor_date"]), None
    )
    if (
        metrics["open_trades"] != open_trades
        or metrics["closed_trades"] != closed_trades
        or metrics["initial_capital_cny"] != 1_000_000.0
        or len(holding_spans) != len(trade_ledger)
        or len(trade_ledger) != len(buy_events)
        or open_trades not in {0, 1}
        or final_position != open_trades
        or metrics["current_position"] != ("LONG" if final_position else "FLAT")
        or action.get("state_before_next") != final_position
        or action.get("latest_market_date") != metrics["period_end"]
        or metrics["period_end"] != dates[-1]
        or metrics["period_start"] not in dates
        or metrics["period_start"] != expected_period_start
    ):
        raise ProductionClientError("report trade, position, and metric counts differ")
    if any(
        (trade["status"] == "OPEN")
        != (trade["exit_date"] is None and trade["exit_price"] is None)
        for trade in trade_ledger
    ):
        raise ProductionClientError("report open trade exit semantics are invalid")
    for index, (trade, span, entry) in enumerate(
        zip(trade_ledger, holding_spans, buy_events, strict=True)
    ):
        exit_event = sell_events[index] if index < len(sell_events) else None
        if (
            trade["entry_date"] != entry["action_date"]
            or trade["entry_price"] != entry["price"]
            or trade["entry_cost_cny"] != entry["total_cost_cny"]
            or span["entry_date"] != entry["action_date"]
            or trade["quantity"] != entry["quantity"]
            or span["quantity"] != entry["quantity"]
            or trade["status"] != span["status"]
            or (
                trade["status"] == "CLOSED"
                and (
                    exit_event is None
                    or trade["exit_date"] != exit_event["action_date"]
                    or trade["exit_price"] != exit_event["price"]
                    or trade["exit_cost_cny"] != exit_event["total_cost_cny"]
                    or span["exit_action_date"] != exit_event["action_date"]
                    or span["last_held_market_date"] != exit_event["signal_date"]
                )
            )
            or (
                trade["status"] == "OPEN"
                and (
                    trade["exit_date"] is not None
                    or span["exit_action_date"] is not None
                    or span["last_held_market_date"] != dates[-1]
                )
            )
        ):
            raise ProductionClientError("report trade and holding span ledgers differ")
    buy_bps, sell_bps, fixed_sell_cost = cost_rates
    events_by_date = {event["action_date"]: event for event in event_ledger}
    if len(events_by_date) != len(event_ledger):
        raise ProductionClientError("report contains multiple transitions on one action date")
    cash = 1_000_000.0
    held_units = 0
    period_equities: list[float] = []
    period_positions: list[int] = []
    for row, holding in zip(price_equity, holdings, strict=True):
        event = events_by_date.get(row["date"])
        if event is not None:
            price = float(event["price"])
            unit_cost = price * (buy_bps if event["side"] == "BUY" else sell_bps) / 10_000
            if event["side"] == "SELL":
                unit_cost += fixed_sell_cost
            expected_quantity = (
                math.floor(cash / (price + unit_cost))
                if event["side"] == "BUY"
                else held_units
            )
            expected_cash = (
                cash - expected_quantity * (price + unit_cost)
                if event["side"] == "BUY"
                else cash + expected_quantity * (price - unit_cost)
            )
            if (
                event["quantity"] != expected_quantity
                or not math.isclose(float(event["cost_per_unit"]), unit_cost)
                or not math.isclose(float(event["notional_cny"]), expected_quantity * price)
                or not math.isclose(
                    float(event["total_cost_cny"]), expected_quantity * unit_cost
                )
                or not math.isclose(float(event["cash_before_cny"]), cash)
                or not math.isclose(float(event["cash_after_cny"]), expected_cash)
            ):
                raise ProductionClientError("report event cash or cost accounting is invalid")
            cash = expected_cash
            held_units = expected_quantity if event["side"] == "BUY" else 0
        if (
            holding["holdings"] != held_units
            or holding["position_after"] != (1 if held_units else 0)
            or not math.isclose(float(row["equity"]), cash + held_units * float(row["close"]))
        ):
            raise ProductionClientError("report daily holdings or equity accounting is invalid")
        if row["date"] >= metrics["period_start"]:
            period_equities.append(float(row["equity"]))
            period_positions.append(holding["position_after"])
    if not period_equities:
        raise ProductionClientError("report performance period has no rows")
    for trade in trade_ledger:
        quantity = int(trade["quantity"])
        entry_notional = float(trade["entry_price"]) * quantity
        mark_price = float(trade["mark_price"])
        gross = (mark_price - float(trade["entry_price"])) * quantity
        costs_paid = float(trade["entry_cost_cny"]) + (
            float(trade["exit_cost_cny"]) if trade["status"] == "CLOSED" else 0.0
        )
        if (
            entry_notional <= 0
            or not math.isclose(float(trade["gross_pnl_cny"]), gross)
            or not math.isclose(float(trade["net_pnl_cny"]), gross - costs_paid)
            or not math.isclose(float(trade["return"]), (gross - costs_paid) / entry_notional)
            or (
                trade["status"] == "OPEN"
                and (
                    trade["mark_date"] != dates[-1]
                    or not math.isclose(mark_price, float(price_equity[-1]["close"]))
                    or float(trade["exit_cost_cny"]) != 0.0
                )
            )
            or (
                trade["status"] == "CLOSED"
                and (
                    trade["mark_date"] != trade["exit_date"]
                    or not math.isclose(mark_price, float(trade["exit_price"]))
                )
            )
        ):
            raise ProductionClientError("report trade P&L accounting is invalid")
    peak = 1_000_000.0
    expected_drawdown = 0.0
    for equity in period_equities:
        peak = max(peak, equity)
        expected_drawdown = min(expected_drawdown, equity / peak - 1.0)
    period_start_index = dates.index(metrics["period_start"])
    comparator_execution = float(price_equity[period_start_index]["price"])
    comparator_unit_cost = comparator_execution * buy_bps / 10_000
    comparator_units = math.floor(
        1_000_000.0 / (comparator_execution + comparator_unit_cost)
    )
    comparator_cash = 1_000_000.0 - comparator_units * (
        comparator_execution + comparator_unit_cost
    )
    comparator_final = comparator_cash + comparator_units * float(price_equity[-1]["close"])
    if (
        not math.isclose(float(metrics["final_equity_cny"]), period_equities[-1])
        or not math.isclose(
            float(metrics["cumulative_return"]), period_equities[-1] / 1_000_000.0 - 1.0
        )
        or not math.isclose(float(metrics["max_drawdown"]), expected_drawdown)
        or not math.isclose(
            float(metrics["exposure"]), sum(period_positions) / len(period_positions)
        )
        or not math.isclose(
            float(metrics["buy_and_hold_return"]), comparator_final / 1_000_000.0 - 1.0
        )
    ):
        raise ProductionClientError("report return, risk, or comparator metrics do not reconcile")
    if provenance.get("market_window") != [dates[0], dates[-1]] or provenance.get(
        "performance_window"
    ) != [metrics["period_start"], metrics["period_end"]]:
        raise ProductionClientError("report price window and provenance differ")
    core_metrics = {
        key: value
        for key, value in metrics.items()
        if key not in {"net_return", "current_position", "closed_trades", "open_trades"}
    }
    if any(summary.get(key) != value for key, value in core_metrics.items()) or (
        summary.get("transitions") != len(event_ledger)
        or summary.get("turnover") != len(event_ledger)
        or not math.isclose(
            float(summary.get("turnover_notional_cny", math.nan)),
            sum(float(event["notional_cny"]) for event in event_ledger),
        )
        or summary.get("closed_trades") != closed_trades
        or summary.get("open_trades") != open_trades
        or not math.isclose(
            float(summary.get("total_cost_cny", math.nan)),
            sum(float(event["total_cost_cny"]) for event in event_ledger),
        )
    ):
        raise ProductionClientError("report performance summary and ledgers differ")
    if (
        not math.isclose(
            float(metrics["net_profit_cny"]),
            float(metrics["final_equity_cny"]) - float(metrics["initial_capital_cny"]),
        )
        or not math.isclose(float(metrics["net_return"]), float(metrics["cumulative_return"]))
        or not math.isclose(float(price_equity[-1]["equity"]), float(metrics["final_equity_cny"]))
        or not math.isclose(
            float(costs["total_cost_cny"]),
            sum(float(event["total_cost_cny"]) for event in events),
        )
        or not math.isclose(
            float(costs["total_cost_cny"]),
            sum(float(costs[name]) for name in (
                "commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny"
            )),
        )
        or any(
            not math.isclose(
                float(costs[name]), sum(float(event[name]) for event in events)
            )
            for name in (
                "commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny"
            )
        )
    ):
        raise ProductionClientError("report accounting evidence does not reconcile")
    core_result_digest = hashlib.sha256(
        b"quantresearch-production-report-evidence/v1\0"
        + canonical_json_bytes(
            {
                "normalized_snapshot_sha256": audit["dataset"]["snapshot_id"],
                "action": action,
                "price_equity": price_equity,
                "events": events,
                "trades": trades,
                "holdings": holdings,
                "metrics": core_metrics,
                "provenance": provenance,
            }
        )
    ).hexdigest()
    bundle_id = hashlib.sha256(
        b"quant-platform/attempt-result-bundle/v1\0"
        + canonical_json_bytes(
            {
                "attempt_id": audit["attempt_id"],
                "experiment_id": audit["experiment_id"],
                "core_result_digest": core_result_digest,
            }
        )
    ).hexdigest()
    if audit["result_digest"] != core_result_digest or descriptor["bundle_id"] != bundle_id:
        raise ProductionClientError("report core result or bundle identity does not verify")
    resolved = {
        "attempt_id": ("attempt-audit", "/attempt_id", audit["attempt_id"]),
        "experiment_id": ("attempt-audit", "/experiment_id", audit["experiment_id"]),
        "run_id": ("attempt-audit", "/run_id", audit["run_id"]),
        "dataset_snapshot_id": ("attempt-audit", "/dataset/snapshot_id", audit["dataset"]["snapshot_id"]),
        "purpose": ("contract", "/purpose", contract["purpose"]),
        "bundle_integrity": ("bundle-descriptor", "/verification/status", descriptor["verification"]["status"]),
        "period_start": ("bundle/metrics.json", "/period_start", metrics["period_start"]),
        "period_end": ("bundle/metrics.json", "/period_end", metrics["period_end"]),
        "initial_capital_cny": ("bundle/metrics.json", "/initial_capital_cny", metrics["initial_capital_cny"]),
        "final_equity_cny": ("bundle/metrics.json", "/final_equity_cny", metrics["final_equity_cny"]),
        "net_profit_cny": ("bundle/metrics.json", "/net_profit_cny", metrics["net_profit_cny"]),
        "current_position": ("bundle/metrics.json", "/current_position", metrics["current_position"]),
        "closed_trades": ("bundle/metrics.json", "/closed_trades", metrics["closed_trades"]),
        "open_trades": ("bundle/metrics.json", "/open_trades", metrics["open_trades"]),
        "template_parameters": ("bundle/config.json", "/template/parameters", configuration["template"]["parameters"]),
        "operators": ("attempt-audit", "/operators", audit["operators"]),
        "runtime": ("bundle/run_manifest.json", "/runtime", runtime["runtime"]),
        "price_equity_rows": ("bundle/daily_replay.csv", "/rows/*/{Date,price,close,equity}", price_equity),
        "events": ("bundle/events.csv", "/rows", events),
        "trades": ("bundle/trades.csv", "/rows", trades),
        "holdings": ("bundle/daily_replay.csv", "/rows/*/{Date,holdings,position_after}", holdings),
        **{
            name: ("bundle/cost_breakdown.json", f"/{name}", costs[name])
            for name in ("commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny")
        },
        "net_return": ("bundle/metrics.json", "/net_return", metrics["net_return"]),
        "max_drawdown": ("bundle/metrics.json", "/max_drawdown", metrics["max_drawdown"]),
        "integrity_not_qualification": ("contract", "/limitations/0", contract["limitations"][0]),
        "qualification_not_deployment": ("contract", "/limitations/1", contract["limitations"][1]),
        "no_recomputation": ("contract", "/limitations/2", contract["limitations"][2]),
        "bundle_id": ("bundle-descriptor", "/bundle_id", descriptor["bundle_id"]),
        "core_result_digest": ("attempt-audit", "/result_digest", audit["result_digest"]),
        "operator_id": ("operator-manifest", "/operator_id", operator["operator_id"]),
        "operator_version": ("operator-manifest", "/semantic_version", operator["semantic_version"]),
        "operator_source_sha256": ("operator-manifest", "/source/sha256", operator["source"]["sha256"]),
        "operator_content_digest": ("operator-manifest", "/content_digest", operator["content_digest"]),
    }
    for field_id, field in fields.items():
        if field.get("availability") != "AVAILABLE":
            continue
        if field_id not in resolved:
            raise ProductionClientError(f"report source is unsupported: {field_id}")
        artifact, pointer, value = resolved[field_id]
        if field.get("source_ref") != {"artifact": artifact, "pointer": pointer} or field.get(
            "raw"
        ) != value:
            raise ProductionClientError(f"report source binding differs: {field_id}")
    return {
        "attempt_id": attempt_id,
        "experiment_id": experiment_id,
        "dataset_snapshot_id": snapshot_sha256,
        "provider_url": urlunsplit(
            (
                str(provenance["provider_request"]["scheme"]),
                str(provenance["provider_request"]["netloc"]),
                str(provenance["provider_request"]["path"]),
                str(provenance["provider_request"]["query"]),
                str(provenance["provider_request"]["fragment"]),
            )
        ),
    }


def _number(action: Mapping[str, Any], key: str) -> float:
    value = action.get(key)
    if type(value) not in {int, float}:
        raise ProductionClientError(f"action field {key} is not numeric")
    return float(value)


def _mapping_number(value: Mapping[str, Any], key: str) -> float:
    number = value.get(key)
    if type(number) not in {int, float}:
        raise ProductionClientError(f"action field {key} is not numeric")
    return float(cast(int | float, number))


def _decision_basis(
    action_name: str, previous: float, current: float, buy: float, sell: float
) -> str:
    if action_name == "BUY":
        return f"决策依据：买卖指数由 {previous:+.4f}%/日向上穿越买入线 {buy:+.3f}%/日。"
    if action_name == "SELL":
        return f"决策依据：买卖指数由 {previous:+.4f}%/日向下穿越卖出线 {sell:+.3f}%/日。"
    if action_name == "HOLD":
        return f"决策依据：买卖指数未向下穿越卖出线 {sell:+.3f}%/日，继续持有。"
    return f"决策依据：买卖指数未向上穿越买入线 {buy:+.3f}%/日，继续等待。"


def render_notification(job: ScheduledJob, action: Mapping[str, Any], report_url: str) -> bytes:
    positions = {0: "空仓（0%）", 1: "持有（100%）"}
    action_labels = {"WAIT": "继续等待", "BUY": "买入", "HOLD": "继续持有", "SELL": "卖出"}
    action_name = str(action.get("action"))
    current_state = action.get("state_before_next")
    target_state = action.get("target_state")
    market_date = action.get("latest_market_date")
    scenarios = action.get("next_completed_close_scenarios")
    if (
        action_name not in action_labels
        or current_state not in positions
        or target_state not in positions
        or not isinstance(market_date, str)
        or not isinstance(scenarios, Mapping)
        or not report_url.startswith("https://")
    ):
        raise ProductionClientError("action payload cannot render a notification")
    previous = _number(action, "previous_slope_pct")
    current = _number(action, "next_slope_pct")
    is_buy_boundary = target_state == 0
    boundary_name = "买入" if is_buy_boundary else "卖出"
    boundary_key_prefix = "buy" if is_buy_boundary else "sell"

    if job.job_id == "1cd5557264db":
        parameters = action.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ProductionClientError("Gold action parameters are absent")
        buy = _mapping_number(parameters, "buy_threshold_pct_per_day")
        sell = _mapping_number(parameters, "sell_threshold_pct_per_day")
        spread = _mapping_number(parameters, "roundtrip_spread_cny_per_g")
        if (buy, sell, spread) != (0.175, -0.275, 5.0):
            raise ProductionClientError("Gold action meaning differs from the frozen contract")
        boundary_key = f"{boundary_key_prefix}_threshold_equivalent_cny_per_g"
        boundary = _mapping_number(scenarios, boundary_key)
        lines = [
            f"黄金｜已完成市场日 {market_date}｜{action_name}（{action_labels[action_name]}）",
            f"完成收盘：{_number(action, 'latest_close_cny_per_g'):.2f} 元/克｜日涨跌：{_number(action, 'daily_change_pct'):+.2f}%",
            f"策略仓位：{positions[current_state]} → {positions[target_state]}",
            f"买卖指数：上一 {previous:+.4f}%/日 → 当前 {current:+.4f}%/日｜冻结买入线 {buy:+.3f}%/日｜冻结卖出线 {sell:+.3f}%/日",
            _decision_basis(action_name, previous, current, buy, sell),
            f"下一完整收盘{boundary_name}边界：{boundary:.2f} 元/克；若触发 BUY/SELL，于 {action['next_trade_date_estimate']} 下一交易日开盘执行。",
            f"成本口径：FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G（完整往返价差 {spread:g} 元/克，不是单边）；仅供人工决策，不会自动下单。",
            "研究解释：SGE_AU9999_PROXY；市场代理评估，不代表招行实际可成交收益",
            report_url,
        ]
    elif job.job_id == "297c11cad0dc":
        rules = action.get("rules")
        costs = action.get("costs")
        if not isinstance(rules, Mapping) or not isinstance(costs, Mapping):
            raise ProductionClientError("BOCOM action rules or costs are absent")
        buy = _mapping_number(rules, "buy_crossing_pct")
        sell = _mapping_number(rules, "sell_crossing_pct")
        buy_cost = _mapping_number(costs, "buy_cost_bps")
        sell_cost = _mapping_number(costs, "sell_cost_bps")
        if (buy, sell, buy_cost, sell_cost) != (0.2, -0.2, 8.0, 13.0):
            raise ProductionClientError("BOCOM action meaning differs from the frozen contract")
        boundary_key = f"{boundary_key_prefix}_threshold_equivalent_raw_close"
        boundary = _mapping_number(scenarios, boundary_key)
        lines = [
            f"交通银行｜已完成市场日 {market_date}｜{action_name}（{action_labels[action_name]}）",
            f"完成收盘：{_number(action, 'latest_close'):.3f} 元/股｜日涨跌：{_number(action, 'daily_change_pct'):+.2f}%",
            f"策略仓位：{positions[current_state]} → {positions[target_state]}",
            f"买卖指数（复权收盘信号）：上一 {previous:+.4f}%/日 → 当前 {current:+.4f}%/日｜冻结买入线 {buy:+.3f}%/日｜冻结卖出线 {sell:+.3f}%/日",
            _decision_basis(action_name, previous, current, buy, sell),
            f"下一完整收盘{boundary_name}边界：{boundary:.3f} 元/股（原始收盘情景，假设无公司行动）；若触发 BUY/SELL，于 {action['next_session_date_estimate']} 下一交易日开盘执行。",
            f"成本口径：买入 {buy_cost:g} bps、卖出 {sell_cost:g} bps；仅供人工决策，不会自动下单。",
            report_url,
        ]
    else:
        raise ProductionClientError("unknown scheduled job presentation")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _https_readback(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "quantresearch-production-client/1"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ProductionClientError(f"report HTTPS read-back returned HTTP {response.status}")
        payload = response.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024:
        raise ProductionClientError("report HTTPS read-back exceeds size limit")
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_report(
    job: ScheduledJob,
    report: bytes,
    action: Mapping[str, Any],
    *,
    report_root: Path = REPORT_ROOT,
    readback: Callable[[str], bytes] = _https_readback,
) -> str:
    if not report or len(report) > 16 * 1024 * 1024 or b"<html" not in report[:4096].lower():
        raise ProductionClientError("report payload is invalid")
    market_date = str(action.get("latest_market_date", ""))
    action_name = str(action.get("action", ""))
    if market_date.encode() not in report or action_name.encode() not in report:
        raise ProductionClientError("report market date or action differs from the verified action")
    root_metadata = os.stat(report_root, follow_symlinks=False)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ProductionClientError("report publication root is unsafe")
    target = report_root / job.report_filename
    prior = target.read_bytes() if target.exists() else None
    url = f"{REPORT_BASE_URL}/{job.report_filename}"
    _atomic_write(target, report)
    try:
        if target.read_bytes() != report or readback(url) != report:
            raise ProductionClientError("published report read-back does not verify")
    except Exception:
        if prior is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, prior)
        raise
    return url


def run_request(
    job: ScheduledJob,
    *,
    request: ProductionRequest,
    tls: ClientTLS,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
    publisher: Callable[[ScheduledJob, bytes, Mapping[str, Any]], str] = publish_report,
) -> bytes:
    client = client_factory(transport_factory(tls))
    manifest = client.submit_and_wait(request)
    files = manifest.get("files")
    if (
        manifest.get("schema") != "quantresearch-production-result/v1"
        or manifest.get("job_id") != job.job_id
        or manifest.get("model_id") != job.model_id
        or manifest.get("production_manifest_sha256") != job.production_manifest_sha256
        or manifest.get("report_filename") != job.report_filename
        or manifest.get("report_operator") != REPORT_OPERATOR
        or not isinstance(files, Mapping)
        or manifest.get("report_document_sha256")
        != files.get("report-document.json", {}).get("sha256")
        or manifest.get("report_sha256") != files.get("report.html", {}).get("sha256")
        or not REPORT_EVIDENCE_FILE_NAMES.issubset(files)
        or manifest.get("automatic_ordering") is not False
    ):
        raise ProductionClientError("result does not match the scheduled job contract")
    report = client.fetch_verified_file(manifest, "report.html")
    action = _verified_action_from_report(job, report)
    evidence = {
        name: client.fetch_verified_file(manifest, name)
        for name in REPORT_EVIDENCE_FILE_NAMES
    }
    evidence["normalized-snapshot.json"] = client.fetch_verified_file(
        manifest, "normalized-snapshot.json"
    )
    document_action = _verified_action_from_report_document(
        job, evidence["report-document.json"]
    )
    try:
        verified_ids = _verify_report_document_sources(
            json.loads(evidence["report-document.json"]), evidence
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionClientError("report source evidence is invalid") from exc
    if (
        manifest.get("attempt_id") != verified_ids["attempt_id"]
        or manifest.get("experiment_id") != verified_ids["experiment_id"]
        or manifest.get("dataset_snapshot_id") != verified_ids["dataset_snapshot_id"]
        or manifest.get("provider_request")
        != {"method": "GET", "url": verified_ids["provider_url"]}
        or manifest.get("provider_response_sha256")
        != files.get("provider-response.bin", {}).get("sha256")
        or manifest.get("action_sha256") != files.get("action.json", {}).get("sha256")
        or manifest.get("generated_at") != document_action.get("generated_at")
        or document_action.get("generated_at") != request.scheduled_for
    ):
        raise ProductionClientError("result invocation or experiment identity does not verify")
    canonical_report = render_report_document(
        json.loads(evidence["report-document.json"])
    )
    if report != canonical_report:
        raise ProductionClientError("report HTML differs from canonical document rendering")
    if canonical_json_bytes(document_action) != canonical_json_bytes(action):
        raise ProductionClientError("report HTML and ReportDocument actions differ")
    if manifest.get("action_sha256") != hashlib.sha256(
        canonical_json_bytes(action)
    ).hexdigest():
        raise ProductionClientError("report action identity differs from the immutable result")
    source_action = _verified_action_from_notification(
        job, client.fetch_verified_file(manifest, "notification.txt")
    )
    comparison_url = f"{REPORT_BASE_URL}/{job.report_filename}"
    if render_notification(job, source_action, comparison_url) != render_notification(
        job, action, comparison_url
    ):
        raise ProductionClientError("report action and source notification do not agree")
    report_url = publisher(job, report, action)
    return render_notification(job, action, report_url)


def run_job(
    job: ScheduledJob,
    *,
    scheduled_for: str,
    tls: ClientTLS,
    jobs_path: Path,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
    publisher: Callable[[ScheduledJob, bytes, Mapping[str, Any]], str] = publish_report,
) -> bytes:
    validate_schedule_record(job, jobs_path)
    request = ProductionRequest.build(
        job_id=job.job_id,
        scheduled_for=scheduled_for,
        production_manifest_sha256=job.production_manifest_sha256,
    )
    return run_request(
        job,
        request=request,
        tls=tls,
        transport_factory=transport_factory,
        client_factory=client_factory,
        publisher=publisher,
    )


def run_validation(
    job: ScheduledJob,
    *,
    validation_for: str,
    validation_id: str,
    tls: ClientTLS,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
    publisher: Callable[[ScheduledJob, bytes, Mapping[str, Any]], str] = publish_report,
) -> bytes:
    request = ProductionRequest.build_validation(
        job_id=job.job_id,
        validation_for=validation_for,
        validation_id=validation_id,
        production_manifest_sha256=job.production_manifest_sha256,
    )
    return run_request(
        job,
        request=request,
        tls=tls,
        transport_factory=transport_factory,
        client_factory=client_factory,
        publisher=publisher,
    )


def _parser() -> argparse.ArgumentParser:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).absolute()
    secrets = home / "secrets" / "quantresearch-production"
    parser = argparse.ArgumentParser(description="Trigger one reviewed QuantResearch production job")
    parser.add_argument("--job-id", required=True, choices=sorted(JOBS))
    parser.add_argument("--scheduled-for")
    parser.add_argument("--validation-for")
    parser.add_argument("--validation-id")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--client-certificate", type=Path, default=secrets / "client.crt")
    parser.add_argument("--client-private-key", type=Path, default=secrets / "client.key")
    parser.add_argument("--server-ca", type=Path, default=secrets / "server-ca.crt")
    parser.add_argument("--jobs-file", type=Path, default=home / "cron" / "jobs.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.scheduled_for and (args.validation_for or args.validation_id):
            raise ProductionContractError("scheduled and validation invocations are distinct")
        if bool(args.validation_for) != bool(args.validation_id):
            raise ProductionContractError("validation-for and validation-id are required together")
        tls = ClientTLS(
            base_url=args.base_url,
            client_certificate=args.client_certificate,
            client_private_key=args.client_private_key,
            server_ca=args.server_ca,
        )
        if args.validation_id:
            notification = run_validation(
                JOBS[args.job_id],
                validation_for=args.validation_for,
                validation_id=args.validation_id,
                tls=tls,
            )
        else:
            scheduled_for = args.scheduled_for or scheduled_fire_for(
                JOBS[args.job_id], datetime.now(TIMEZONE)
            )
            scheduled_for = canonical_scheduled_fire(scheduled_for)
            notification = run_job(
                JOBS[args.job_id],
                scheduled_for=scheduled_for,
                tls=tls,
                jobs_path=args.jobs_file,
            )
    except (OSError, ProductionClientError, ProductionContractError, ValueError) as exc:
        print(f"production client failed closed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(notification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
