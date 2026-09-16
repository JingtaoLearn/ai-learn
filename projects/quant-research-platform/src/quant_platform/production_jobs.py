from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .attempt_report import (
    DOMAIN_DOCUMENT,
    FIELD_SOURCE_AND_UNIT,
    REPORT_DOCUMENT_SCHEMA_ID,
    canonical_report_operator_bundle,
    render_report_document,
    validate_report_document,
)
from .production_contract import SHA256, canonical_json_bytes


class ProductionJobError(RuntimeError):
    """Raised when immutable job input cannot produce a verified action."""


REPORT_INITIAL_CAPITAL_CNY = 1_000_000.0
REPORT_EVIDENCE_FILE_NAMES = frozenset(
    {
        "attempt-audit.json",
        "bundle-descriptor.json",
        "config.json",
        "contract.json",
        "cost_breakdown.json",
        "daily_replay.csv",
        "events.csv",
        "metrics.json",
        "operator-manifest.json",
        "report-document.json",
        "run_manifest.json",
        "trades.csv",
    }
)


@dataclass(frozen=True)
class CanonicalJsonBytes:
    """Validated canonical JSON bytes for the already-encoded identity seam."""

    value: bytes

    def __post_init__(self) -> None:
        if type(self.value) is not bytes:
            raise TypeError("canonical JSON identity input must contain bytes")
        try:
            decoded = json.loads(self.value)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionJobError("canonical JSON identity input is invalid") from exc
        if canonical_json_bytes(decoded) != self.value:
            raise ProductionJobError("canonical JSON identity input is not canonical")


class ProviderClient(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], maximum_bytes: int) -> bytes: ...


@dataclass(frozen=True)
class TrendConfig:
    window_sessions: int
    ema_span: int
    buy_threshold_pct_per_day: float
    sell_threshold_pct_per_day: float
    anchor_date: date


@dataclass(frozen=True)
class ProductionReportSpec:
    display_name: str
    qualification: str
    execution_price_key: str
    mark_price_key: str
    execution_price_basis: str
    price_unit: str
    buy_cost_bps: float
    sell_cost_bps: float
    completed_roundtrip_cost_per_unit: float
    cost_description: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalProductionReport:
    document: dict[str, Any]
    html: bytes
    operator_identity: dict[str, Any]
    evidence_files: Mapping[str, bytes]


def _report_field(field_id: str, raw: Any, *, display: str | None = None) -> dict[str, Any]:
    artifact, pointer, unit = FIELD_SOURCE_AND_UNIT[field_id]
    return {
        "field_id": field_id,
        "source_ref": {"artifact": artifact, "pointer": pointer},
        "availability": "AVAILABLE",
        "reason": None,
        "raw": raw,
        "display": display,
        "unit": unit,
    }


def _unavailable_report_field(field_id: str, reason: str) -> dict[str, Any]:
    artifact, pointer, unit = FIELD_SOURCE_AND_UNIT[field_id]
    return {
        "field_id": field_id,
        "source_ref": {"artifact": artifact, "pointer": pointer},
        "availability": "NOT_EVALUATED",
        "reason": reason,
        "raw": None,
        "display": reason,
        "unit": unit,
    }


def _operator_identity() -> dict[str, Any]:
    bundle = canonical_report_operator_bundle()
    return {
        "operator_id": bundle["manifest"]["operator_id"],
        "version": bundle["manifest"]["semantic_version"],
        "api_version": bundle["manifest"]["api_version"],
        "source_sha256": bundle["source_sha256"],
        "content_digest": bundle["content_digest"],
    }


def _csv_bytes(rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _execution_cost(price: float, side: str, spec: ProductionReportSpec) -> float:
    if side == "BUY":
        return price * spec.buy_cost_bps / 10_000.0
    return (
        price * spec.sell_cost_bps / 10_000.0
        + spec.completed_roundtrip_cost_per_unit
    )


def _historical_events(
    rows: Sequence[Mapping[str, Any]],
    points: Sequence[Mapping[str, Any]],
    config: TrendConfig,
    spec: ProductionReportSpec,
) -> list[dict[str, Any]]:
    by_date = {row["date"]: (index, row) for index, row in enumerate(rows)}
    state = 0
    prior_slope: float | None = None
    events: list[dict[str, Any]] = []
    for point in points:
        action_date = point["decision_date"]
        if point["is_next_session"] or not isinstance(action_date, date):
            continue
        if action_date < config.anchor_date:
            continue
        if point["slope_pct"] is None:
            continue
        slope = float(point["slope_pct"])
        side = None
        reason = None
        if prior_slope is not None:
            if state == 0 and prior_slope < config.buy_threshold_pct_per_day <= slope:
                side = "BUY"
                reason = "signal crossed upward through the frozen buy line"
            elif state == 1 and prior_slope > config.sell_threshold_pct_per_day >= slope:
                side = "SELL"
                reason = "signal crossed downward through the frozen sell line"
        if side is not None:
            index, row = by_date[action_date]
            if index == 0:
                raise ProductionJobError("historical action has no preceding signal date")
            before = state
            state = 1 if side == "BUY" else 0
            price = float(row[spec.execution_price_key])
            events.append(
                {
                    "Date": action_date.isoformat(),
                    "side": side,
                    "signal_date": rows[index - 1]["date"].isoformat(),
                    "action_date": action_date.isoformat(),
                    "price": price,
                    "price_basis": spec.execution_price_basis,
                    "position_before": before,
                    "position_after": state,
                    "previous_slope_pct": prior_slope,
                    "signal_slope_pct": slope,
                    "reason": reason,
                    "cost_per_unit": _execution_cost(price, side, spec),
                }
            )
        prior_slope = slope
    return events


def _account_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cash = REPORT_INITIAL_CAPITAL_CNY
    holdings = 0
    result = []
    for event in events:
        price = float(event["price"])
        unit_cost = float(event["cost_per_unit"])
        cash_before = cash
        holdings_before = holdings
        if event["side"] == "BUY":
            if holdings != 0:
                raise ProductionJobError("accounting ledger contains a nested BUY")
            quantity = math.floor(cash / (price + unit_cost))
            if quantity < 1:
                raise ProductionJobError("reporting account cannot fund one normalization unit")
            total_cost = quantity * unit_cost
            cash -= quantity * price + total_cost
            holdings = quantity
        else:
            if holdings < 1:
                raise ProductionJobError("accounting ledger contains a SELL without holdings")
            quantity = holdings
            total_cost = quantity * unit_cost
            cash += quantity * price - total_cost
            holdings = 0
        result.append(
            {
                **event,
                "quantity": quantity,
                "notional_cny": quantity * price,
                "total_cost_cny": total_cost,
                "cash_before_cny": cash_before,
                "cash_after_cny": cash,
                "holdings_before_units": holdings_before,
                "holdings_after_units": holdings,
            }
        )
    return result


def _trade_and_holding_ledgers(
    events: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    spec: ProductionReportSpec,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    entry: Mapping[str, Any] | None = None
    for event in events:
        if event["side"] == "BUY":
            if entry is not None:
                raise ProductionJobError("historical event ledger contains nested entries")
            entry = event
            continue
        if entry is None:
            raise ProductionJobError("historical event ledger exits before entry")
        quantity = int(entry["quantity"])
        if event["quantity"] != quantity:
            raise ProductionJobError("trade quantity changes within one holding span")
        gross = (float(event["price"]) - float(entry["price"])) * quantity
        costs = float(entry["total_cost_cny"]) + float(event["total_cost_cny"])
        net = gross - costs
        trades.append(
            {
                "entry_date": entry["action_date"],
                "entry_price": entry["price"],
                "quantity": quantity,
                "entry_cost_cny": entry["total_cost_cny"],
                "exit_date": event["action_date"],
                "exit_price": event["price"],
                "exit_cost_cny": event["total_cost_cny"],
                "mark_date": event["action_date"],
                "mark_price": event["price"],
                "status": "CLOSED",
                "gross_pnl_cny": gross,
                "net_pnl_cny": net,
                "return": net / (float(entry["price"]) * quantity),
            }
        )
        holdings.append(
            {
                "entry_date": entry["action_date"],
                "quantity": quantity,
                "last_held_market_date": event["signal_date"],
                "exit_action_date": event["action_date"],
                "status": "CLOSED",
            }
        )
        entry = None
    if entry is not None:
        latest = rows[-1]
        mark = float(latest[spec.mark_price_key])
        quantity = int(entry["quantity"])
        gross = (mark - float(entry["price"])) * quantity
        net = gross - float(entry["total_cost_cny"])
        trades.append(
            {
                "entry_date": entry["action_date"],
                "entry_price": entry["price"],
                "quantity": quantity,
                "entry_cost_cny": entry["total_cost_cny"],
                "exit_date": None,
                "exit_price": None,
                "exit_cost_cny": 0.0,
                "mark_date": latest["date"].isoformat(),
                "mark_price": mark,
                "status": "OPEN",
                "gross_pnl_cny": gross,
                "net_pnl_cny": net,
                "return": net / (float(entry["price"]) * quantity),
            }
        )
        holdings.append(
            {
                "entry_date": entry["action_date"],
                "quantity": quantity,
                "last_held_market_date": latest["date"].isoformat(),
                "exit_action_date": None,
                "status": "OPEN",
            }
        )
    return trades, holdings


def _equity_path(
    rows: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    config: TrendConfig,
    spec: ProductionReportSpec,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events_by_date = {event["action_date"]: event for event in events}
    cash = REPORT_INITIAL_CAPITAL_CNY
    holdings = 0
    cumulative_cost = 0.0
    equity = REPORT_INITIAL_CAPITAL_CNY
    peak = REPORT_INITIAL_CAPITAL_CNY
    maximum_drawdown = 0.0
    held_sessions = 0
    comparator_entry: float | None = None
    comparator_cash = REPORT_INITIAL_CAPITAL_CNY
    comparator_holdings = 0
    comparator_equity = REPORT_INITIAL_CAPITAL_CNY
    path: list[dict[str, Any]] = [
        {
            "date": row["date"].isoformat(),
            "price": float(row[spec.execution_price_key]),
            "close": float(row[spec.mark_price_key]),
            "phase": "WARMUP",
            "position": None,
            "equity_gross": None,
            "equity": REPORT_INITIAL_CAPITAL_CNY,
            "buy_and_hold_reference": None,
        }
        for row in rows
        if row["date"] < config.anchor_date
    ]
    period_rows = [row for row in rows if row["date"] >= config.anchor_date]
    if not period_rows:
        raise ProductionJobError("report performance period is empty")
    for row in period_rows:
        action_date = row["date"].isoformat()
        execution = float(row[spec.execution_price_key])
        mark = float(row[spec.mark_price_key])
        event = events_by_date.get(action_date)
        if event is not None:
            cash = float(event["cash_after_cny"])
            holdings = int(event["holdings_after_units"])
            cumulative_cost += float(event["total_cost_cny"])
        if holdings > 0:
            held_sessions += 1
        if comparator_entry is None:
            comparator_entry = execution
            unit_cost = _execution_cost(execution, "BUY", spec)
            comparator_holdings = math.floor(
                REPORT_INITIAL_CAPITAL_CNY / (execution + unit_cost)
            )
            comparator_cash -= comparator_holdings * (execution + unit_cost)
        equity = cash + holdings * mark
        comparator_equity = comparator_cash + comparator_holdings * mark
        peak = max(peak, equity)
        maximum_drawdown = min(maximum_drawdown, equity / peak - 1.0)
        path.append(
            {
                "date": action_date,
                "price": execution,
                "close": mark,
                "phase": "PERFORMANCE",
                "position": 1 if holdings else 0,
                "holdings": holdings,
                "equity_gross": equity + cumulative_cost,
                "equity": equity,
                "buy_and_hold_reference": comparator_equity,
            }
        )
    return path, {
        "initial_capital_cny": REPORT_INITIAL_CAPITAL_CNY,
        "final_equity_cny": equity,
        "net_profit_cny": equity - REPORT_INITIAL_CAPITAL_CNY,
        "cumulative_return": equity / REPORT_INITIAL_CAPITAL_CNY - 1.0,
        "max_drawdown": maximum_drawdown,
        "exposure": held_sessions / len(period_rows),
        "buy_and_hold_return": comparator_equity / REPORT_INITIAL_CAPITAL_CNY - 1.0,
        "period_start": period_rows[0]["date"].isoformat(),
        "period_end": period_rows[-1]["date"].isoformat(),
    }


def _path_display(rows: Sequence[Mapping[str, Any]]) -> str:
    def shown(value: Any) -> str:
        return "" if value is None else format(float(value), ".12g")

    lines = ["date,phase,open,close,position,equity_gross,equity_net,buy_and_hold_reference"]
    lines.extend(
        ",".join(
            (
                str(row["date"]),
                str(row["phase"]),
                shown(row["price"]),
                shown(row["close"]),
                "" if row["position"] is None else str(row["position"]),
                shown(row["equity_gross"]),
                shown(row["equity"]),
                shown(row["buy_and_hold_reference"]),
            )
        )
        for row in rows
    )
    return "\n".join(lines)


def build_canonical_production_report(
    *,
    rows: Sequence[Mapping[str, Any]],
    points: Sequence[Mapping[str, Any]],
    config: TrendConfig,
    action: Mapping[str, Any],
    experiment_id: str,
    attempt_id: str,
    provider_url: str,
    normalized_bytes: bytes,
    spec: ProductionReportSpec,
) -> CanonicalProductionReport:
    """Adapt one frozen daily computation to the existing canonical Report operator."""

    expected_action = evaluate(points, config)
    for key in ("action", "reason", "state_before_next", "target_state"):
        if action.get(key) != expected_action[key]:
            raise ProductionJobError("report adapter action differs from frozen evaluation")
    signal_events = _historical_events(rows, points, config, spec)
    if (signal_events[-1]["position_after"] if signal_events else 0) != action["state_before_next"]:
        raise ProductionJobError("historical report position differs from current action")
    events = _account_events(signal_events)
    trades, holdings = _trade_and_holding_ledgers(events, rows, spec)
    price_equity, metrics = _equity_path(rows, events, config, spec)
    operator = _operator_identity()
    provider = urlsplit(provider_url)
    event_cost = sum(float(event["total_cost_cny"]) for event in events)
    closed = sum(trade["status"] == "CLOSED" for trade in trades)
    opened = sum(trade["status"] == "OPEN" for trade in trades)
    configuration = {
        "display_name": spec.display_name,
        "qualification": spec.qualification,
        "current_action": dict(action),
        "strategy_parameters": {
            "window_sessions": config.window_sessions,
            "ema_span": config.ema_span,
            "buy_threshold_pct_per_day": config.buy_threshold_pct_per_day,
            "sell_threshold_pct_per_day": config.sell_threshold_pct_per_day,
            "anchor_date": config.anchor_date.isoformat(),
        },
        "execution_price_basis": spec.execution_price_basis,
        "price_unit": spec.price_unit,
        "cost_description": spec.cost_description,
        "reporting_account": {
            "initial_capital_cny": REPORT_INITIAL_CAPITAL_CNY,
            "quantity_rule": "integer units purchased with available cash at each BUY",
            "lot_size": 1,
            "actual_invested_capital": False,
        },
        "event_ledger": events,
        "trade_ledger": trades,
        "holding_spans": holdings,
        "performance_summary": {
            **metrics,
            "transitions": len(events),
            "turnover": len(events),
            "turnover_definition": (
                "sum absolute long/cash position changes; each full transition equals 1.0"
            ),
            "total_cost_cny": event_cost,
            "turnover_notional_cny": sum(float(event["notional_cny"]) for event in events),
            "closed_trades": closed,
            "open_trades": opened,
        },
    }
    provenance = {
        "job_id": action["job_id"],
        "report_uuid": action["report_uuid"],
        "model_id": action["model_version"],
        "production_manifest_sha256": action["production_manifest_sha256"],
        "provider_source": provider.netloc or provider.scheme,
        "provider_request_sha256": hashlib.sha256(provider_url.encode("utf-8")).hexdigest(),
        "market_window": [rows[0]["date"].isoformat(), rows[-1]["date"].isoformat()],
        "performance_window": [metrics["period_start"], metrics["period_end"]],
        "execution_authority": "zhlearn production API",
        "trigger_role": "ailearn trigger/read-back/delivery only",
        "local_compute": False,
        "feng_fallback": False,
    }
    core_result_digest = hashlib.sha256(
        b"quantresearch-production-report-evidence/v1\0"
        + canonical_json_bytes(
            {
                "normalized_snapshot_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
                "action": dict(action),
                "price_equity": price_equity,
                "events": events,
                "trades": trades,
                "holdings": holdings,
                "metrics": metrics,
                "provenance": provenance,
            }
        )
    ).hexdigest()
    bundle_id = identity(
        b"quant-platform/attempt-result-bundle/v1\0",
        {
            "attempt_id": attempt_id,
            "experiment_id": experiment_id,
            "core_result_digest": core_result_digest,
        },
    )
    limitations = [
        *spec.limitations,
        "Buy-and-hold is a period-dependent reference, not a decisive price judgment.",
        "This evidence does not promote the strategy and grants no order, broker, or fund authority.",
        "Open positions are marked to the latest completed close; no terminal exit or exit cost is fabricated.",
    ]
    canonical_price_equity = [
        {
            "date": row["date"],
            "price": row["price"],
            "close": row["close"],
            "equity": 1.0 if row["equity"] is None else row["equity"],
        }
        for row in price_equity
    ]
    canonical_events = []
    for event in events:
        cost = float(event["total_cost_cny"])
        canonical_events.append(
            {
                "Date": event["action_date"],
                "side": event["side"],
                "price": event["price"],
                "quantity": event["quantity"],
                "notional_cny": event["notional_cny"],
                "commission_cny": cost if spec.buy_cost_bps or spec.sell_cost_bps else 0.0,
                "transfer_fee_cny": 0.0,
                "stamp_tax_cny": 0.0,
                "slippage_cny": cost if spec.completed_roundtrip_cost_per_unit else 0.0,
                "total_cost_cny": cost,
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
        )
    canonical_trades = [
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
        for trade in trades
    ]
    canonical_holdings = [
        {
            "date": row["date"],
            "holdings": int(row.get("holdings", 0)),
            "position_after": 0 if row["position"] is None else row["position"],
        }
        for row in price_equity
    ]
    final_equity = metrics["final_equity_cny"]
    sections = [
        {"section_id": "identity_and_purpose", "fields": [
            _report_field("attempt_id", attempt_id),
            _report_field("experiment_id", experiment_id),
            _report_field("run_id", f"{action['job_id']}:{action['generated_at']}"),
            _report_field("dataset_snapshot_id", hashlib.sha256(normalized_bytes).hexdigest()),
            _report_field(
                "purpose",
                "PRESENTATION_ONLY",
                display=(
                    f"Historical daily decision evidence; current action {action['action']} for "
                    f"{action.get('next_trade_date_estimate', action.get('next_session_date_estimate'))}; "
                    "automatic_ordering=false"
                ),
            ),
        ]},
        {"section_id": "evidence_status", "fields": [
            _report_field("bundle_integrity", "VERIFIED", display="Verified canonical production adapter"),
            _unavailable_report_field("total_return_status", "No total-return authority attachment was supplied"),
            _unavailable_report_field("matched_exposure_status", "No matched-exposure qualification was performed"),
            _unavailable_report_field("ranking_status", "No strategy ranking was performed"),
            _unavailable_report_field("promotion_ready", "No total-return authority attachment was supplied"),
        ]},
        {"section_id": "account_summary", "fields": [
            _report_field("period_start", metrics["period_start"]),
            _report_field("period_end", metrics["period_end"]),
            _report_field("initial_capital_cny", REPORT_INITIAL_CAPITAL_CNY, display="1000000 CNY reporting normalization; not actual invested capital"),
            _report_field("final_equity_cny", final_equity),
            _report_field("net_profit_cny", metrics["net_profit_cny"]),
            _report_field("current_position", "LONG" if action["state_before_next"] else "FLAT"),
            _report_field("closed_trades", closed),
            _report_field("open_trades", opened),
        ]},
        {"section_id": "configuration", "fields": [
            _report_field("template_parameters", configuration, display=canonical_json_bytes(configuration).decode()),
            _report_field("operators", {"report": operator}, display=canonical_json_bytes({"report": operator}).decode()),
            _report_field("runtime", provenance, display=canonical_json_bytes(provenance).decode()),
        ]},
        {"section_id": "price_equity_path", "fields": [
            _report_field("price_equity_rows", canonical_price_equity, display=_path_display(price_equity)),
        ]},
        {"section_id": "events_trades_holdings", "fields": [
            _report_field("events", canonical_events, display=canonical_json_bytes(events).decode()),
            _report_field("trades", canonical_trades, display=canonical_json_bytes(trades).decode()),
            _report_field("holdings", canonical_holdings, display=canonical_json_bytes(holdings).decode()),
        ]},
        {"section_id": "costs_and_accounting", "fields": [
            _report_field("commission_cny", event_cost if spec.buy_cost_bps or spec.sell_cost_bps else 0.0),
            _report_field("transfer_fee_cny", 0.0),
            _report_field("stamp_tax_cny", 0.0),
            _report_field("slippage_cny", event_cost if spec.completed_roundtrip_cost_per_unit else 0.0),
            _report_field("total_cost_cny", event_cost, display=f"{event_cost:.12g} CNY; {spec.cost_description}"),
            _unavailable_report_field("gross_dividends_cny", "Dividend cash flows are not separately evaluated"),
            _unavailable_report_field("dividend_tax_cny", "Dividend tax is not separately evaluated"),
            _unavailable_report_field("outstanding_tax_cny", "Outstanding tax is not separately evaluated"),
        ]},
        {"section_id": "total_return_claim", "fields": [
            _report_field("net_return", metrics["cumulative_return"]),
            _report_field("max_drawdown", metrics["max_drawdown"]),
            _unavailable_report_field(
                "total_return_attachment",
                (
                    f"No promotion authority attachment; normalized exposure={metrics['exposure']:.12g}, "
                    f"transitions={len(events)}, turnover={len(events)}, total_cost_cny={event_cost:.12g}"
                ),
            ),
        ]},
        {"section_id": "matched_exposure_qualification", "fields": [
            _unavailable_report_field(
                "matched_exposure_attachment",
                f"Period-dependent buy-and-hold reference return={metrics['buy_and_hold_return']:.12g}",
            ),
            _unavailable_report_field("study_terminal_attachment", "This daily production Attempt is not a Study"),
        ]},
        {"section_id": "limitations", "fields": [
            _report_field("integrity_not_qualification", "INTEGRITY_IS_NOT_QUALIFICATION", display="; ".join(limitations)),
            _report_field("qualification_not_deployment", "QUALIFICATION_IS_NOT_DEPLOYMENT_OR_TRADING_AUTHORITY", display=limitations[-2]),
            _report_field("no_recomputation", "PRESENTATION_ONLY_NO_RECOMPUTATION", display=limitations[-1]),
        ]},
        {"section_id": "provenance", "fields": [
            _report_field("bundle_id", bundle_id),
            _report_field("core_result_digest", core_result_digest),
            _report_field("operator_id", operator["operator_id"]),
            _report_field("operator_version", operator["version"]),
            _report_field("operator_source_sha256", operator["source_sha256"]),
            _report_field("operator_content_digest", operator["content_digest"]),
        ]},
    ]
    core = {
        "schema_id": REPORT_DOCUMENT_SCHEMA_ID,
        "schema_version": 1,
        "sections": sections,
    }
    document = core | {
        "document_id": hashlib.sha256(DOMAIN_DOCUMENT + canonical_json_bytes(core)).hexdigest()
    }
    checked = validate_report_document(document)
    daily_rows = [
        {
            "Date": row["date"],
            "price": row["price"],
            "close": row["close"],
            "equity": row["equity"],
            "holdings": int(row.get("holdings", 0)),
            "position_after": 0 if row["position"] is None else row["position"],
        }
        for row in price_equity
    ]
    cost_breakdown = {
        "commission_cny": event_cost if spec.buy_cost_bps or spec.sell_cost_bps else 0.0,
        "transfer_fee_cny": 0.0,
        "stamp_tax_cny": 0.0,
        "slippage_cny": event_cost if spec.completed_roundtrip_cost_per_unit else 0.0,
        "total_cost_cny": event_cost,
    }
    report_html = render_report_document(checked)
    evidence_files = {
        "attempt-audit.json": canonical_json_bytes(
            {
                "attempt_id": attempt_id,
                "experiment_id": experiment_id,
                "run_id": f"{action['job_id']}:{action['generated_at']}",
                "dataset": {"snapshot_id": hashlib.sha256(normalized_bytes).hexdigest()},
                "result_digest": core_result_digest,
                "operators": {"report": operator},
            }
        ),
        "bundle-descriptor.json": canonical_json_bytes(
            {
                "bundle_id": bundle_id,
                "attempt_id": attempt_id,
                "experiment_id": experiment_id,
                "core_result_digest": core_result_digest,
                "verification": {"status": "VERIFIED"},
            }
        ),
        "config.json": canonical_json_bytes({"template": {"parameters": configuration}}),
        "contract.json": canonical_json_bytes(
            {
                "limitations": [
                    "INTEGRITY_IS_NOT_QUALIFICATION",
                    "QUALIFICATION_IS_NOT_DEPLOYMENT_OR_TRADING_AUTHORITY",
                    "PRESENTATION_ONLY_NO_RECOMPUTATION",
                ],
                "purpose": "PRESENTATION_ONLY",
                "details": limitations,
            }
        ),
        "cost_breakdown.json": canonical_json_bytes(cost_breakdown),
        "daily_replay.csv": _csv_bytes(
            daily_rows,
            ("Date", "price", "close", "equity", "holdings", "position_after"),
        ),
        "events.csv": _csv_bytes(
            canonical_events,
            (
                "Date", "side", "price", "quantity", "notional_cny", "commission_cny",
                "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny",
                "cash_before_cny", "cash_after_cny", "holdings_before", "holdings_after",
                "reason",
            ),
        ),
        "metrics.json": canonical_json_bytes(
            {
                **metrics,
                "net_return": metrics["cumulative_return"],
                "current_position": "LONG" if action["state_before_next"] else "FLAT",
                "closed_trades": closed,
                "open_trades": opened,
            }
        ),
        "operator-manifest.json": canonical_json_bytes(
            {
                "api_version": operator["api_version"],
                "operator_id": operator["operator_id"],
                "semantic_version": operator["version"],
                "source": {"sha256": operator["source_sha256"]},
                "content_digest": operator["content_digest"],
            }
        ),
        "report-document.json": canonical_json_bytes(checked),
        "run_manifest.json": canonical_json_bytes({"runtime": provenance}),
        "trades.csv": _csv_bytes(
            canonical_trades,
            (
                "entry_date", "entry_price", "quantity", "entry_cost_cny", "exit_date",
                "exit_price", "exit_cost_cny", "status", "gross_pnl_cny", "net_pnl_cny",
                "return",
            ),
        ),
    }
    if frozenset(evidence_files) != REPORT_EVIDENCE_FILE_NAMES:
        raise ProductionJobError("canonical report evidence member set is incomplete")
    return CanonicalProductionReport(checked, report_html, operator, evidence_files)


@dataclass(frozen=True)
class JobComputation:
    job_id: str
    model_id: str
    production_manifest_sha256: str
    report_uuid: str
    provider_url: str
    raw_name: str
    raw_bytes: bytes
    normalized_bytes: bytes
    action: dict[str, Any]
    report_operator: Mapping[str, Any]
    report_evidence: Mapping[str, bytes]
    report_html: bytes
    notification_bytes: bytes
    experiment_id: str
    attempt_id: str


@dataclass(frozen=True)
class ProductionInput:
    kind: str
    identity: Mapping[str, Any]
    payload: bytes

    def __post_init__(self) -> None:
        if self.kind not in {"provider-get", "no-network-operation"}:
            raise ProductionJobError("production input kind is not supported")
        if type(self.payload) is not bytes:
            raise ProductionJobError("production input payload must be bytes")


@dataclass(frozen=True)
class FormalComputation:
    job_id: str
    production_manifest_sha256: str
    operation: str
    authority_sha256: str
    files: Mapping[str, bytes]
    experiment_id: str
    attempt_id: str


ProductionComputation = JobComputation | FormalComputation


_INPUT_MEMBERS = frozenset({"identity.json", "raw.bin"})
_DAILY_COMPUTATION_MEMBERS = frozenset(
    {
        "identity.json",
        "raw.bin",
        "normalized.json",
        "action.json",
        "report.html",
        "notification.txt",
        *REPORT_EVIDENCE_FILE_NAMES,
    }
)
_FORMAL_RESULT_MEMBERS = frozenset(
    {
        "calibration.json",
        "03-CALIBRATION_CLAIMED.json",
        "04-CALIBRATION_SEALED.json",
    }
)
_FORMAL_COMPUTATION_MEMBERS = frozenset({"identity.json", *_FORMAL_RESULT_MEMBERS})
_STAGED_PACKAGE_IDENTITY_DOMAIN = b"quantresearch-production-staged-package/v1\0"


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _safe_member_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def staged_package_identity(payloads: Mapping[str, bytes]) -> str:
    """Return the content identity that must be retained outside a staged package."""

    if not payloads or any(
        not _safe_member_name(name) or type(payload) is not bytes
        for name, payload in payloads.items()
    ):
        raise ProductionJobError("staged package identity input is invalid")
    inventory = {
        name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in payloads.items()
    }
    return hashlib.sha256(
        _STAGED_PACKAGE_IDENTITY_DOMAIN + canonical_json_bytes(inventory)
    ).hexdigest()


def _open_staged_member(
    directory_fd: int, name: str, label: str, expected_fingerprint: tuple[int, ...]
) -> tuple[int, tuple[int, ...]]:
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        member_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ProductionJobError(f"staged {label} member is unsafe") from exc
    try:
        before = os.fstat(member_fd)
        path_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        fingerprint = _stat_fingerprint(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o222
            or fingerprint != _stat_fingerprint(path_before)
        ):
            raise ProductionJobError(f"staged {label} member is unsafe")
        if fingerprint != expected_fingerprint:
            raise ProductionJobError(f"staged {label} member changed during read")
        return member_fd, fingerprint
    except OSError as exc:
        os.close(member_fd)
        raise ProductionJobError(f"staged {label} member is unsafe") from exc
    except BaseException:
        os.close(member_fd)
        raise


def _read_staged_members(
    target: Path,
    allowed_shapes: frozenset[frozenset[str]],
    label: str,
    expected_package_identity: str | None,
) -> dict[str, bytes]:
    if expected_package_identity is not None and (
        not isinstance(expected_package_identity, str)
        or SHA256.fullmatch(expected_package_identity) is None
    ):
        raise ProductionJobError(f"staged {label} package identity is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(target, flags)
    except OSError as exc:
        raise ProductionJobError(f"staged {label} directory is unsafe") from exc
    members: dict[str, tuple[int, tuple[int, ...]]] = {}
    try:
        before = os.fstat(directory_fd)
        names = os.listdir(directory_fd)
        shape = frozenset(names)
        if (
            not stat.S_ISDIR(before.st_mode)
            or len(names) != len(shape)
            or any(not _safe_member_name(name) for name in names)
            or shape not in allowed_shapes
        ):
            raise ProductionJobError(f"staged {label} member set is invalid")
        ordered_names = ["identity.json", *sorted(shape - {"identity.json"})]
        expected_stats = {
            name: os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            for name in ordered_names
        }
        # The directory is sealed after every member; its ctime is the package-wide baseline.
        if any(value.st_ctime_ns > before.st_ctime_ns for value in expected_stats.values()):
            raise ProductionJobError(f"staged {label} member changed during read")
        expected_fingerprints = {
            name: _stat_fingerprint(value) for name, value in expected_stats.items()
        }
        for name in ordered_names:
            members[name] = _open_staged_member(
                directory_fd, name, label, expected_fingerprints[name]
            )
        for name, (member_fd, fingerprint) in members.items():
            if (
                _stat_fingerprint(os.fstat(member_fd)) != fingerprint
                or _stat_fingerprint(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                )
                != fingerprint
            ):
                raise ProductionJobError(f"staged {label} member changed during read")
        payloads = {name: _read_fd(member_fd) for name, (member_fd, _) in members.items()}
        for name, (member_fd, _) in members.items():
            os.lseek(member_fd, 0, os.SEEK_SET)
            if _read_fd(member_fd) != payloads[name]:
                raise ProductionJobError(f"staged {label} member changed during read")
        for name, (member_fd, fingerprint) in members.items():
            if (
                _stat_fingerprint(os.fstat(member_fd)) != fingerprint
                or _stat_fingerprint(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                )
                != fingerprint
            ):
                raise ProductionJobError(f"staged {label} member changed during read")
        after = os.fstat(directory_fd)
        target_after = os.stat(target, follow_symlinks=False)
        if (
            frozenset(os.listdir(directory_fd)) != shape
            or _stat_fingerprint(before) != _stat_fingerprint(after)
            or _stat_fingerprint(before) != _stat_fingerprint(target_after)
        ):
            raise ProductionJobError(f"staged {label} directory changed during read")
        if (
            expected_package_identity is not None
            and staged_package_identity(payloads) != expected_package_identity
        ):
            raise ProductionJobError(f"staged {label} package identity mismatch")
        return payloads
    except OSError as exc:
        raise ProductionJobError(f"staged {label} directory is unsafe") from exc
    finally:
        for member_fd, _ in members.values():
            os.close(member_fd)
        os.close(directory_fd)


def inspect_staged_package(target: Path, *, label: str) -> str:
    """Read one sealed package safely and derive its identity for the external authority."""

    if label == "production input":
        allowed_shapes = frozenset({_INPUT_MEMBERS})
    elif label == "production computation":
        allowed_shapes = frozenset({_DAILY_COMPUTATION_MEMBERS, _FORMAL_COMPUTATION_MEMBERS})
    else:
        raise ProductionJobError("staged package label is invalid")
    return staged_package_identity(_read_staged_members(target, allowed_shapes, label, None))


def _staged_identity(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionJobError(f"staged {label} identity is invalid") from exc
    if not isinstance(value, dict):
        raise ProductionJobError(f"staged {label} identity is invalid")
    return value


class ProductionJob(Protocol):
    job_id: str
    production_manifest_sha256: str

    def acquire(self, client: ProviderClient, scheduled_for: datetime) -> tuple[str, bytes]: ...

    def compute(
        self,
        raw: bytes,
        provider_url: str,
        scheduled_for: datetime,
    ) -> JobComputation: ...


class NoNetworkProductionJob(Protocol):
    job_id: str
    production_manifest_sha256: str

    def acquire_no_network(self, request_id: str) -> ProductionInput: ...

    def compute_no_network(
        self, value: ProductionInput, request_id: str
    ) -> FormalComputation: ...


def parse_manifest(
    payload: bytes,
    *,
    expected_sha256: str,
    model_id: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ProductionJobError("production manifest byte identity mismatch")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionJobError("production manifest is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("stage") != "PRODUCTION_FROZEN"
        or value.get("model_id") != model_id
        or value.get("parameters") != dict(parameters)
        or value.get("execution", {}).get("automatic_ordering") is not False
    ):
        raise ProductionJobError("production manifest semantics mismatch")
    return value


def fit_next_log(values: Sequence[float], window: int) -> float:
    if len(values) != window or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ProductionJobError("trend window must contain finite positive values")
    logs = [math.log(value) for value in values]
    x_mean = (window - 1) / 2.0
    y_mean = sum(logs) / window
    denominator = sum((index - x_mean) ** 2 for index in range(window))
    slope = sum((index - x_mean) * (logs[index] - y_mean) for index in range(window))
    return y_mean + slope / denominator * (window - x_mean)


def decision_points(rows: Sequence[Mapping[str, Any]], config: TrendConfig) -> list[dict[str, Any]]:
    values = [float(row["signal_close"]) for row in rows]
    alpha = 2.0 / (config.ema_span + 1.0)
    smoothed: float | None = None
    previous: float | None = None
    points: list[dict[str, Any]] = []
    for position in range(config.window_sessions, len(rows) + 1):
        raw = fit_next_log(values[position - config.window_sessions : position], config.window_sessions)
        smoothed = raw if smoothed is None else alpha * raw + (1 - alpha) * smoothed
        slope = None if previous is None else 100.0 * math.expm1(smoothed - previous)
        points.append(
            {
                "decision_date": rows[position]["date"] if position < len(rows) else None,
                "is_next_session": position == len(rows),
                "raw_curve": math.exp(raw),
                "smooth_curve": math.exp(smoothed),
                "slope_pct": slope,
            }
        )
        previous = smoothed
    return points


def evaluate(points: Sequence[Mapping[str, Any]], config: TrendConfig) -> dict[str, Any]:
    eligible = [
        point
        for point in points
        if point["is_next_session"]
        or (
            isinstance(point["decision_date"], date)
            and point["decision_date"] >= config.anchor_date
        )
    ]
    state = 0
    prior_slope: float | None = None
    state_before = 0
    action = "WAIT"
    reason = "no upward crossing; remain flat"
    for point in eligible:
        value = point["slope_pct"]
        if value is None:
            continue
        slope = float(value)
        if point["is_next_session"]:
            state_before = state
            if prior_slope is not None:
                if state == 0 and prior_slope < config.buy_threshold_pct_per_day <= slope:
                    state, action, reason = 1, "BUY", "signal crossed upward through the frozen buy line"
                elif state == 1 and prior_slope > config.sell_threshold_pct_per_day >= slope:
                    state, action, reason = 0, "SELL", "signal crossed downward through the frozen sell line"
                elif state == 1:
                    action, reason = "HOLD", "signal did not cross the frozen sell line"
            break
        if prior_slope is not None:
            if state == 0 and prior_slope < config.buy_threshold_pct_per_day <= slope:
                state = 1
            elif state == 1 and prior_slope > config.sell_threshold_pct_per_day >= slope:
                state = 0
        prior_slope = slope
    if not eligible or eligible[-1]["is_next_session"] is not True or prior_slope is None:
        raise ProductionJobError("history cannot produce the next-session decision")
    point = eligible[-1]
    return {
        "action": action,
        "reason": reason,
        "state_before_next": state_before,
        "target_state": state,
        "previous_slope_pct": prior_slope,
        "next_slope_pct": float(point["slope_pct"]),
        "next_raw_curve": float(point["raw_curve"]),
        "next_smooth_curve": float(point["smooth_curve"]),
    }


def projected_slope(
    rows: Sequence[Mapping[str, Any]], config: TrendConfig, candidate_raw_close: float
) -> float:
    if not math.isfinite(candidate_raw_close) or candidate_raw_close <= 0:
        raise ProductionJobError("candidate close must be finite and positive")
    points = decision_points(rows, config)
    current = math.log(float(points[-1]["smooth_curve"]))
    factor = float(rows[-1]["signal_close"]) / float(rows[-1]["close"])
    values = [float(row["signal_close"]) for row in rows[-(config.window_sessions - 1) :]]
    values.append(candidate_raw_close * factor)
    next_raw = fit_next_log(values, config.window_sessions)
    alpha = 2.0 / (config.ema_span + 1.0)
    next_smooth = alpha * next_raw + (1 - alpha) * current
    return 100.0 * math.expm1(next_smooth - current)


def close_for_slope(
    rows: Sequence[Mapping[str, Any]], config: TrendConfig, target: float
) -> float:
    if not math.isfinite(target) or target <= -100:
        raise ProductionJobError("target slope is invalid")
    low = math.log(float(rows[-1]["close"])) - 50
    high = math.log(float(rows[-1]["close"])) + 50
    for _ in range(180):
        middle = (low + high) / 2
        if projected_slope(rows, config, math.exp(middle)) < target:
            low = middle
        else:
            high = middle
    return math.exp((low + high) / 2)


def normalized_rows(rows: Sequence[Mapping[str, Any]]) -> CanonicalJsonBytes:
    serializable = [
        {key: (value.isoformat() if isinstance(value, date) else value) for key, value in row.items()}
        for row in rows
    ]
    return CanonicalJsonBytes(canonical_json_bytes(serializable))


def identity(domain: bytes, value: Any) -> str:
    return hashlib.sha256(domain + canonical_json_bytes(value)).hexdigest()


def identity_canonical_bytes(domain: bytes, value: CanonicalJsonBytes) -> str:
    if type(value) is not CanonicalJsonBytes:
        raise TypeError("already-canonical identity requires CanonicalJsonBytes")
    return hashlib.sha256(domain + value.value).hexdigest()


def next_weekday(value: date) -> date:
    result = value + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


class ProductionJobs:
    """Production computation interface, including its durable staging representation."""

    def __init__(self, jobs: Sequence[ProductionJob | NoNetworkProductionJob]):
        self._jobs: dict[str, Any] = {job.job_id: job for job in jobs}
        if len(self._jobs) != len(jobs):
            raise ProductionJobError("production job IDs must be unique")

    def acquire(
        self, job_id: str, client: ProviderClient, scheduled_for: datetime
    ) -> tuple[str, bytes]:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        return job.acquire(client, scheduled_for)

    def stage_input(
        self,
        job_id: str,
        client: ProviderClient,
        scheduled_for: datetime | None,
        *,
        request_id: str,
    ) -> ProductionInput:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        acquire_no_network = getattr(job, "acquire_no_network", None)
        if acquire_no_network is not None:
            return acquire_no_network(request_id)
        if scheduled_for is None:
            raise ProductionJobError("provider job requires a scheduled invocation time")
        url, raw = job.acquire(client, scheduled_for)
        return ProductionInput(
            "provider-get", {"method": "GET", "provider_url": url}, raw
        )

    @staticmethod
    def input_payloads(value: ProductionInput) -> dict[str, bytes]:
        return {
            "identity.json": canonical_json_bytes(
                {"kind": value.kind, **dict(value.identity)}
            ),
            "raw.bin": value.payload,
        }

    @classmethod
    def read_input(cls, target: Path, *, expected_package_identity: str) -> ProductionInput:
        payloads = _read_staged_members(
            target,
            frozenset({_INPUT_MEMBERS}),
            "production input",
            expected_package_identity,
        )
        identity_bytes = payloads["identity.json"]
        identity = _staged_identity(identity_bytes, "production input")
        legacy_identity = "kind" not in identity or (
            identity.get("kind", "provider-get") == "provider-get" and "method" not in identity
        )
        kind = identity.pop("kind", "provider-get")
        if kind == "provider-get" and "method" not in identity:
            identity["method"] = "GET"
        value = ProductionInput(kind, identity, payloads["raw.bin"])
        expected = cls.input_payloads(value)
        if not legacy_identity and payloads != expected:
            raise ProductionJobError("staged production input read-back differs")
        return value

    def compute(
        self,
        job_id: str,
        raw: bytes,
        provider_url: str,
        scheduled_for: datetime,
    ) -> JobComputation:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        return job.compute(raw, provider_url, scheduled_for)

    def compute_input(
        self,
        job_id: str,
        value: ProductionInput,
        scheduled_for: datetime | None,
        *,
        request_id: str,
    ) -> JobComputation | FormalComputation:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        if value.kind == "no-network-operation":
            compute_no_network = getattr(job, "compute_no_network", None)
            if compute_no_network is None:
                raise ProductionJobError("job does not support a no-network operation")
            return compute_no_network(value, request_id)
        if scheduled_for is None or value.identity.get("method") != "GET":
            raise ProductionJobError("provider input identity is invalid")
        provider_url = value.identity.get("provider_url")
        if not isinstance(provider_url, str):
            raise ProductionJobError("provider input URL is invalid")
        return self.compute(job_id, value.payload, provider_url, scheduled_for)

    @staticmethod
    def computation_payloads(value: ProductionComputation) -> dict[str, bytes]:
        if isinstance(value, FormalComputation):
            identity = {
                "kind": "formal",
                "job_id": value.job_id,
                "production_manifest_sha256": value.production_manifest_sha256,
                "operation": value.operation,
                "authority_sha256": value.authority_sha256,
                "experiment_id": value.experiment_id,
                "attempt_id": value.attempt_id,
                "files": sorted(value.files),
            }
            return {"identity.json": canonical_json_bytes(identity), **dict(value.files)}
        identity = {
            "kind": "daily",
            "job_id": value.job_id,
            "model_id": value.model_id,
            "production_manifest_sha256": value.production_manifest_sha256,
            "report_uuid": value.report_uuid,
            "provider_url": value.provider_url,
            "raw_name": value.raw_name,
            "report_operator": dict(value.report_operator),
            "report_evidence": sorted(value.report_evidence),
            "experiment_id": value.experiment_id,
            "attempt_id": value.attempt_id,
        }
        return {
            "identity.json": canonical_json_bytes(identity),
            "raw.bin": value.raw_bytes,
            "normalized.json": value.normalized_bytes,
            "action.json": canonical_json_bytes(value.action),
            **dict(value.report_evidence),
            "report.html": value.report_html,
            "notification.txt": value.notification_bytes,
        }

    @classmethod
    def read_computation(
        cls, target: Path, *, expected_package_identity: str
    ) -> ProductionComputation:
        payloads = _read_staged_members(
            target,
            frozenset({_DAILY_COMPUTATION_MEMBERS, _FORMAL_COMPUTATION_MEMBERS}),
            "production computation",
            expected_package_identity,
        )
        identity = _staged_identity(payloads["identity.json"], "production computation")
        if identity.get("kind") == "formal":
            if (
                frozenset(payloads) != _FORMAL_COMPUTATION_MEMBERS
                or identity.get("files") != sorted(_FORMAL_RESULT_MEMBERS)
            ):
                raise ProductionJobError(
                    "staged production computation member set is invalid"
                )
            value: ProductionComputation = FormalComputation(
                job_id=identity["job_id"],
                production_manifest_sha256=identity["production_manifest_sha256"],
                operation=identity["operation"],
                authority_sha256=identity["authority_sha256"],
                files={name: payloads[name] for name in identity["files"]},
                experiment_id=identity["experiment_id"],
                attempt_id=identity["attempt_id"],
            )
        else:
            if (
                identity.get("kind") != "daily"
                or frozenset(payloads) != _DAILY_COMPUTATION_MEMBERS
            ):
                raise ProductionJobError(
                    "staged production computation member set is invalid"
                )
            value = JobComputation(
                identity["job_id"],
                identity["model_id"],
                identity["production_manifest_sha256"],
                identity["report_uuid"],
                identity["provider_url"],
                identity["raw_name"],
                payloads["raw.bin"],
                payloads["normalized.json"],
                json.loads(payloads["action.json"]),
                identity["report_operator"],
                {name: payloads[name] for name in identity["report_evidence"]},
                payloads["report.html"],
                payloads["notification.txt"],
                identity["experiment_id"],
                identity["attempt_id"],
            )
        expected = cls.computation_payloads(value)
        if payloads != expected:
            raise ProductionJobError("staged production computation read-back differs")
        return value
