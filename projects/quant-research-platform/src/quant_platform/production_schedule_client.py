from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
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
    compact_holdings_display,
    compact_price_equity_display,
    compact_production_parameters_display,
)


TIMEZONE = ZoneInfo("Asia/Shanghai")
DELIVERY = "feishu:oc_33bdb4845220ee3788fe50c50cf333ed"
DEFAULT_BASE_URL = "https://127.0.0.1:8443"
REPORT_BASE_URL = "https://share.ai.jingtao.fun"
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
        "behavior=trigger, verify immutable result/files and read back the zhlearn-owned stable report, "
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
        parameter_display = report_field("template_parameters")
        if parameter_display.startswith("QR-PRODUCTION-PARAMETERS-DISPLAY-1\n"):
            parameter_header = parameter_display.splitlines()[1]
            if not parameter_header.startswith("parameters\t"):
                raise ValueError("compact parameter header is invalid")
            configuration = json.loads(parameter_header.removeprefix("parameters\t"))
        else:
            configuration = json.loads(parameter_display)
    except (IndexError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
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
    """Verify immutable bindings and zhlearn's semantic attestation without finance."""

    try:
        document = validate_report_document(document)
    except AttemptReportError as exc:
        raise ProductionClientError("report document contract is invalid") from exc
    fields = {
        field["field_id"]: field
        for section in document["sections"]
        for field in section["fields"]
    }
    optional = {
        "total_return_status", "matched_exposure_status", "ranking_status", "promotion_ready",
        "dividend_tax_cny", "outstanding_tax_cny", "total_return_attachment",
        "matched_exposure_attachment", "study_terminal_attachment",
    }
    if any(
        field["availability"] != "AVAILABLE"
        for field_id, field in fields.items()
        if field_id not in optional
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
    attestation = _verified_json_evidence(evidence, "semantic-attestation.json")
    daily = _csv_evidence(
        evidence,
        "daily_replay.csv",
        (
            "Date", "price", "close", "equity", "holdings", "position_after",
            "available_cash_cny", "dividend_receivable_cny",
        ),
    )
    event_rows = _csv_evidence(
        evidence,
        "events.csv",
        (
            "Date", "side", "price", "quantity", "notional_cny", "commission_cny",
            "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny",
            "cash_before_cny", "cash_after_cny", "holdings_before", "holdings_after", "reason",
        ),
    )
    trade_rows = _csv_evidence(
        evidence,
        "trades.csv",
        (
            "entry_date", "entry_price", "quantity", "entry_cost_cny", "exit_date",
            "exit_price", "exit_cost_cny", "status", "gross_pnl_cny", "net_pnl_cny", "return",
        ),
    )
    if (
        not isinstance(configuration, dict)
        or set(configuration) != {"template"}
        or not isinstance(configuration["template"], dict)
        or set(configuration["template"]) != {"parameters"}
    ):
        raise ProductionClientError("report configuration evidence contract is invalid")
    parameters = configuration["template"]["parameters"]
    expected_parameters = {
        "display_name", "qualification", "current_action", "strategy_parameters",
        "execution_price_basis", "price_unit", "cost_description", "signal_price_path",
        "reporting_account", "event_ledger", "trade_ledger", "holding_spans",
        "corporate_action_ledger", "performance_summary",
    }
    expected_metrics = {
        "initial_capital_cny", "final_equity_cny", "net_profit_cny", "cumulative_return",
        "max_drawdown", "exposure", "buy_and_hold_return", "gross_dividends_cny",
        "period_start", "period_end", "net_return", "current_position", "closed_trades",
        "open_trades", "available_cash_cny", "dividend_receivable_cny",
        "buy_and_hold_dividend_receivable_cny",
    }
    if (
        not isinstance(parameters, dict)
        or set(parameters) != expected_parameters
        or not isinstance(metrics, dict)
        or set(metrics) != expected_metrics
        or not isinstance(audit, dict)
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
        or not isinstance(contract, dict)
        or set(contract) != {"purpose", "limitations", "details"}
        or not isinstance(costs, dict)
        or set(costs) != {
            "commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny",
            "total_cost_cny",
        }
        or not isinstance(operator, dict)
        or set(operator) != {
            "api_version", "operator_id", "semantic_version", "source", "content_digest"
        }
        or not isinstance(operator["source"], dict)
        or set(operator["source"]) != {"sha256"}
        or not isinstance(runtime, dict)
        or set(runtime) != {"runtime"}
        or not isinstance(attestation, dict)
    ):
        raise ProductionClientError("report evidence member contract is invalid")

    action = parameters["current_action"]
    event_ledger = parameters["event_ledger"]
    trade_ledger = parameters["trade_ledger"]
    holding_spans = parameters["holding_spans"]
    corporate_actions = parameters["corporate_action_ledger"]
    provenance = runtime["runtime"]
    if not all(
        isinstance(value, expected)
        for value, expected in (
            (action, dict), (event_ledger, list), (trade_ledger, list),
            (holding_spans, list), (corporate_actions, list), (provenance, dict),
        )
    ):
        raise ProductionClientError("report configuration evidence is invalid")
    details = contract["details"]
    if (
        contract["purpose"] != "PRESENTATION_ONLY"
        or contract["limitations"] != [
            "INTEGRITY_IS_NOT_QUALIFICATION",
            "QUALIFICATION_IS_NOT_DEPLOYMENT_OR_TRADING_AUTHORITY",
            "PRESENTATION_ONLY_NO_RECOMPUTATION",
        ]
        or not isinstance(details, list)
        or len(details) < 4
        or not all(isinstance(item, str) and item for item in details)
    ):
        raise ProductionClientError("report contract limitations do not verify")
    expected_provenance = {
        "job_id", "report_uuid", "model_id", "production_manifest_sha256", "provider_source",
        "provider_request", "provider_request_sha256", "market_window", "performance_window",
        "execution_authority", "trigger_role", "local_compute", "feng_fallback",
        "corporate_action_source", "corporate_action_revision_policy",
    }
    if (
        set(provenance) != expected_provenance
        or provenance["job_id"] != action.get("job_id")
        or provenance["report_uuid"] != action.get("report_uuid")
        or provenance["model_id"] != action.get("model_version")
        or provenance["production_manifest_sha256"] != action.get("production_manifest_sha256")
        or provenance["execution_authority"] != "zhlearn production API"
        or provenance["trigger_role"] != "ailearn trigger/read-back/delivery only"
        or provenance["local_compute"] is not False
        or provenance["feng_fallback"] is not False
        or not isinstance(provenance["provider_request"], dict)
        or set(provenance["provider_request"]) != {"scheme", "netloc", "path", "query", "fragment"}
        or not all(isinstance(value, str) for value in provenance["provider_request"].values())
        or provenance["provider_source"]
        != (provenance["provider_request"]["netloc"] or provenance["provider_request"]["scheme"])
        or not all(
            isinstance(provenance[name], str) and provenance[name]
            for name in ("provider_source", "corporate_action_source", "corporate_action_revision_policy")
        )
    ):
        raise ProductionClientError("report provenance authority does not verify")
    provider_url = urlunsplit(tuple(provenance["provider_request"][name] for name in (
        "scheme", "netloc", "path", "query", "fragment"
    )))
    if hashlib.sha256(provider_url.encode()).hexdigest() != provenance["provider_request_sha256"]:
        raise ProductionClientError("report provider request identity does not verify")

    normalized = evidence.get("normalized-snapshot.json")
    if type(normalized) is not bytes:
        raise ProductionClientError("normalized snapshot evidence is absent")
    try:
        normalized_value = json.loads(normalized)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionClientError("normalized snapshot evidence is not JSON") from exc
    if (
        canonical_json_bytes(normalized_value) != normalized
        or not isinstance(normalized_value, list)
        or not all(isinstance(row, dict) for row in normalized_value)
    ):
        raise ProductionClientError("normalized snapshot evidence is invalid")
    snapshot_sha256 = hashlib.sha256(normalized).hexdigest()
    dataset_identity = hashlib.sha256(
        b"quantresearch-production-dataset/v1\0" + normalized
    ).hexdigest()
    experiment_id = hashlib.sha256(
        b"quantresearch-production-experiment/v1\0"
        + canonical_json_bytes({
            "job_id": action.get("job_id"),
            "model": action.get("production_manifest_sha256"),
            "snapshot": dataset_identity,
        })
    ).hexdigest()
    attempt_id = hashlib.sha256(
        b"quantresearch-production-attempt/v1\0"
        + canonical_json_bytes({
            "experiment_id": experiment_id,
            "scheduled_for": action.get("generated_at"),
        })
    ).hexdigest()
    if (
        audit["dataset"]["snapshot_id"] != snapshot_sha256
        or audit["experiment_id"] != experiment_id
        or audit["attempt_id"] != attempt_id
        or audit["run_id"] != f"{action.get('job_id')}:{action.get('generated_at')}"
        or descriptor["attempt_id"] != attempt_id
        or descriptor["experiment_id"] != experiment_id
        or descriptor["core_result_digest"] != audit["result_digest"]
    ):
        raise ProductionClientError("report run, attempt, experiment, or bundle binding differs")
    if operator != {
        "api_version": REPORT_OPERATOR["api_version"],
        "operator_id": REPORT_OPERATOR["operator_id"],
        "semantic_version": REPORT_OPERATOR["version"],
        "source": {"sha256": REPORT_OPERATOR["source_sha256"]},
        "content_digest": REPORT_OPERATOR["content_digest"],
    } or audit["operators"] != {"report": REPORT_OPERATOR}:
        raise ProductionClientError("report operator evidence differs from reviewed identity")

    price_equity = [
        {"date": row["Date"], "price": float(row["price"]), "close": float(row["close"]),
         "equity": float(row["equity"])}
        for row in daily
    ]
    holdings = [
        {"date": row["Date"], "holdings": int(row["holdings"]),
         "position_after": int(row["position_after"])}
        for row in daily
    ]
    events = [{
        key: row[key] if key in {"Date", "side", "reason"}
        else int(row[key]) if key in {"quantity", "holdings_before", "holdings_after"}
        else float(row[key])
        for key in row
    } for row in event_rows]
    trades = [{
        key: None if key in {"exit_date", "exit_price"} and row[key] == ""
        else row[key] if key in {"entry_date", "exit_date", "status"}
        else int(row[key]) if key == "quantity" else float(row[key])
        for key in row
    } for row in trade_rows]
    if not daily or len(normalized_value) != len(daily):
        raise ProductionClientError("report price/equity path inventory differs")
    dates = [row["date"] for row in price_equity]
    if dates != sorted(set(dates)):
        raise ProductionClientError("report price/equity dates are not unique and ordered")
    if action.get("job_id") == "1cd5557264db":
        mark_price_key = "signal_close"
        action_close_key = "latest_close_cny_per_g"
    elif action.get("job_id") == "297c11cad0dc":
        mark_price_key = "close"
        action_close_key = "latest_close"
    else:
        raise ProductionClientError("report names an unknown production job")
    if any(
        normalized_row.get("date") != daily_row["Date"]
        or float(normalized_row.get("open")) != float(daily_row["price"])
        or float(normalized_row.get(mark_price_key)) != float(daily_row["close"])
        for normalized_row, daily_row in zip(normalized_value, daily, strict=True)
    ):
        raise ProductionClientError("normalized snapshot and report price path differ")
    if parameters["signal_price_path"] != [
        {"date": row["date"], "signal_price": row["signal_close"]}
        for row in normalized_value
    ]:
        raise ProductionClientError("normalized snapshot and signal price path differ")
    source_actions = [
        source_action
        for row in normalized_value
        for source_action in row.get("corporate_actions", [])
    ]
    if len(source_actions) != len(corporate_actions) or any(
        any(accounted.get(key) != value for key, value in source.items())
        for source, accounted in zip(source_actions, corporate_actions, strict=True)
    ):
        raise ProductionClientError("normalized corporate actions and report ledger differ")
    if (
        action.get("latest_market_date") != normalized_value[-1].get("date")
        or float(action.get(action_close_key)) != float(normalized_value[-1].get("close"))
    ):
        raise ProductionClientError("normalized snapshot and current action differ")
    if (
        fields["price_equity_rows"]["raw"] != price_equity
        or fields["holdings"]["raw"] != holdings
        or fields["events"]["raw"] != events
        or fields["trades"]["raw"] != trades
        or provenance["market_window"] != [dates[0], dates[-1]]
        or provenance["performance_window"] != [metrics["period_start"], metrics["period_end"]]
    ):
        raise ProductionClientError("report tabular evidence or provenance window differs")
    if len(event_ledger) != len(events) or any(
        source["Date"] != detail.get("action_date")
        or source["side"] != detail.get("side")
        or source["price"] != detail.get("price")
        or source["quantity"] != detail.get("quantity")
        for source, detail in zip(events, event_ledger, strict=True)
    ):
        raise ProductionClientError("report event ledger references differ")
    trade_keys = (
        "entry_date", "entry_price", "quantity", "entry_cost_cny", "exit_date", "exit_price",
        "exit_cost_cny", "status", "gross_pnl_cny", "net_pnl_cny", "return",
    )
    if len(trade_ledger) != len(trades) or any(
        source != {key: detail[key] for key in trade_keys}
        for source, detail in zip(trades, trade_ledger, strict=True)
    ):
        raise ProductionClientError("report trade ledger references differ")
    statuses = [trade["status"] for trade in trades]
    if (
        any(status not in {"OPEN", "CLOSED"} for status in statuses)
        or metrics["open_trades"] != statuses.count("OPEN")
        or metrics["closed_trades"] != statuses.count("CLOSED")
        or metrics["current_position"] != ("LONG" if holdings[-1]["position_after"] else "FLAT")
        or action.get("state_before_next") != holdings[-1]["position_after"]
    ):
        raise ProductionClientError("report trade and position metric counts differ")

    core_metrics = {
        key: value for key, value in metrics.items()
        if key not in {"net_return", "current_position", "closed_trades", "open_trades"}
    }
    core_result_digest = hashlib.sha256(
        b"quantresearch-production-report-evidence/v1\0"
        + canonical_json_bytes({
            "normalized_snapshot_sha256": snapshot_sha256,
            "action": action,
            "price_equity": price_equity,
            "events": events,
            "trades": trades,
            "holdings": holdings,
            "metrics": core_metrics,
            "provenance": provenance,
        })
    ).hexdigest()
    bundle_id = hashlib.sha256(
        b"quant-platform/attempt-result-bundle/v1\0"
        + canonical_json_bytes({
            "attempt_id": attempt_id,
            "experiment_id": experiment_id,
            "core_result_digest": core_result_digest,
        })
    ).hexdigest()
    semantic_subject_digest = hashlib.sha256(
        b"quantresearch-production-semantic-attestation-subject/v1\0"
        + canonical_json_bytes({
            "core_result_digest": core_result_digest,
            "cost_breakdown": costs,
            "event_ledger": event_ledger,
            "trade_ledger": trade_ledger,
            "holding_spans": holding_spans,
            "corporate_action_ledger": corporate_actions,
            "performance_summary": parameters["performance_summary"],
            "daily_replay_sha256": hashlib.sha256(evidence["daily_replay.csv"]).hexdigest(),
        })
    ).hexdigest()
    expected_attestation = {
        "schema": "quantresearch-production-semantic-attestation/v1",
        "authority": "zhlearn production report adapter",
        "status": "VERIFIED",
        "subject_core_result_digest": core_result_digest,
        "subject_digest": semantic_subject_digest,
        "dataset_snapshot_sha256": snapshot_sha256,
        "evidence_counts": {
            "price_rows": len(price_equity), "events": len(events), "trades": len(trades),
            "holding_rows": len(holdings), "holding_spans": len(holding_spans),
            "corporate_actions": len(corporate_actions),
        },
        "checks": [
            "action_matches_frozen_evaluation", "next_open_event_timing",
            "transaction_cost_accounting", "corporate_action_quantity_and_receivable_accounting",
            "trade_and_open_mark_pnl", "equity_return_drawdown_exposure_and_comparator",
        ],
    }
    if (
        audit["result_digest"] != core_result_digest
        or descriptor["bundle_id"] != bundle_id
        or attestation != expected_attestation
    ):
        raise ProductionClientError("report semantic attestation or immutable identity differs")

    resolved = {
        "attempt_id": ("attempt-audit", "/attempt_id", attempt_id),
        "experiment_id": ("attempt-audit", "/experiment_id", experiment_id),
        "run_id": ("attempt-audit", "/run_id", audit["run_id"]),
        "dataset_snapshot_id": ("attempt-audit", "/dataset/snapshot_id", snapshot_sha256),
        "purpose": ("contract", "/purpose", contract["purpose"]),
        "bundle_integrity": ("bundle-descriptor", "/verification/status", "VERIFIED"),
        **{
            name: ("bundle/metrics.json", f"/{name}", metrics[name])
            for name in (
                "period_start", "period_end", "initial_capital_cny", "final_equity_cny",
                "net_profit_cny", "current_position", "closed_trades", "open_trades",
                "gross_dividends_cny", "net_return", "max_drawdown",
            )
        },
        "template_parameters": ("bundle/config.json", "/template/parameters", parameters),
        "operators": ("attempt-audit", "/operators", audit["operators"]),
        "runtime": ("bundle/run_manifest.json", "/runtime", provenance),
        "price_equity_rows": ("bundle/daily_replay.csv", "/rows/*/{Date,price,close,equity}", price_equity),
        "events": ("bundle/events.csv", "/rows", events),
        "trades": ("bundle/trades.csv", "/rows", trades),
        "holdings": ("bundle/daily_replay.csv", "/rows/*/{Date,holdings,position_after}", holdings),
        **{
            name: ("bundle/cost_breakdown.json", f"/{name}", costs[name])
            for name in ("commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny")
        },
        "integrity_not_qualification": ("contract", "/limitations/0", contract["limitations"][0]),
        "qualification_not_deployment": ("contract", "/limitations/1", contract["limitations"][1]),
        "no_recomputation": ("contract", "/limitations/2", contract["limitations"][2]),
        "bundle_id": ("bundle-descriptor", "/bundle_id", bundle_id),
        "core_result_digest": ("attempt-audit", "/result_digest", core_result_digest),
        "operator_id": ("operator-manifest", "/operator_id", operator["operator_id"]),
        "operator_version": ("operator-manifest", "/semantic_version", operator["semantic_version"]),
        "operator_source_sha256": ("operator-manifest", "/source/sha256", operator["source"]["sha256"]),
        "operator_content_digest": ("operator-manifest", "/content_digest", operator["content_digest"]),
    }
    expected_displays = {
        "purpose": (
            f"Historical daily decision evidence; current action {action.get('action')} for "
            f"{action.get('next_trade_date_estimate', action.get('next_session_date_estimate'))}; "
            "automatic_ordering=false"
        ),
        "bundle_integrity": "Verified canonical production adapter",
        "initial_capital_cny": "1000000 CNY reporting normalization; not actual invested capital",
        "template_parameters": compact_production_parameters_display(parameters),
        "operators": canonical_json_bytes(audit["operators"]).decode(),
        "runtime": canonical_json_bytes(provenance).decode(),
        "price_equity_rows": compact_price_equity_display(price_equity),
        "events": (
            f"{len(events)} source-bound rows; complete detailed ledger is included in "
            "template_parameters"
        ),
        "trades": (
            f"{len(trades)} source-bound rows; complete detailed ledger is included in "
            "template_parameters"
        ),
        "holdings": compact_holdings_display(holdings),
        "total_cost_cny": f"{float(costs['total_cost_cny']):.12g} CNY; {parameters['cost_description']}",
        "gross_dividends_cny": (
            f"{float(metrics['gross_dividends_cny']):.12g} CNY gross pre-tax ex-date "
            "receivable accrual; payment dates unavailable and no accrual is spendable cash"
        ),
        "integrity_not_qualification": "; ".join(details),
        "qualification_not_deployment": details[-2],
        "no_recomputation": details[-1],
    }
    for field_id, field in fields.items():
        if field["availability"] != "AVAILABLE":
            continue
        if field_id not in resolved:
            raise ProductionClientError(f"report source is unsupported: {field_id}")
        artifact, pointer, value = resolved[field_id]
        if (
            field["source_ref"] != {"artifact": artifact, "pointer": pointer}
            or field["raw"] != value
            or field["display"] != expected_displays.get(field_id)
        ):
            if field_id in {
                "integrity_not_qualification",
                "qualification_not_deployment",
                "no_recomputation",
            }:
                raise ProductionClientError("report contract limitation binding differs")
            raise ProductionClientError(f"report source binding differs: {field_id}")
    return {
        "attempt_id": attempt_id,
        "experiment_id": experiment_id,
        "dataset_snapshot_id": snapshot_sha256,
        "provider_url": provider_url,
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


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_readback(url: str, *, opener: Any | None = None) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ProductionClientError("report HTTPS read-back URL is invalid")
    request = Request(url, headers={"User-Agent": "quantresearch-production-client/1"})
    selected_opener = opener if opener is not None else build_opener(_RejectRedirects())
    try:
        response_context = selected_opener.open(request, timeout=20)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ProductionClientError("report HTTPS read-back rejected a redirect") from exc
        raise
    with response_context as response:
        if 300 <= response.status < 400:
            raise ProductionClientError("report HTTPS read-back rejected a redirect")
        if response.status != 200:
            raise ProductionClientError(f"report HTTPS read-back returned HTTP {response.status}")
        if response.geturl() != url:
            raise ProductionClientError("report HTTPS read-back effective URL differs")
        payload = response.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024:
        raise ProductionClientError("report HTTPS read-back exceeds size limit")
    return payload


def verify_stable_report(
    job: ScheduledJob,
    report: bytes,
    action: Mapping[str, Any],
    *,
    readback: Callable[[str], bytes] = _https_readback,
) -> str:
    if not report or len(report) > 16 * 1024 * 1024 or b"<html" not in report[:4096].lower():
        raise ProductionClientError("report payload is invalid")
    market_date = str(action.get("latest_market_date", ""))
    action_name = str(action.get("action", ""))
    if market_date.encode() not in report or action_name.encode() not in report:
        raise ProductionClientError("report market date or action differs from the verified action")
    url = f"{REPORT_BASE_URL}/{job.report_filename}"
    if readback(url) != report:
        raise ProductionClientError("stable report read-back does not verify")
    return url


def run_request(
    job: ScheduledJob,
    *,
    request: ProductionRequest,
    tls: ClientTLS,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
    stable_report_verifier: Callable[
        [ScheduledJob, bytes, Mapping[str, Any]], str
    ] = verify_stable_report,
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
    report_url = stable_report_verifier(job, report, action)
    return render_notification(job, action, report_url)


def run_job(
    job: ScheduledJob,
    *,
    scheduled_for: str,
    tls: ClientTLS,
    jobs_path: Path,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
    stable_report_verifier: Callable[
        [ScheduledJob, bytes, Mapping[str, Any]], str
    ] = verify_stable_report,
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
        stable_report_verifier=stable_report_verifier,
    )


def run_validation(
    job: ScheduledJob,
    *,
    validation_for: str,
    validation_id: str,
    tls: ClientTLS,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
    stable_report_verifier: Callable[
        [ScheduledJob, bytes, Mapping[str, Any]], str
    ] = verify_stable_report,
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
        stable_report_verifier=stable_report_verifier,
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
