from __future__ import annotations

import calendar
import hashlib
import html
import json
import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


SCHEMA_ID = "quant-platform/gold-evaluation-report/v1"
EVIDENCE_CLASS = "VALIDATION_EVIDENCE"
ARTIFACT_NAME = "gold-evaluation.json"
NATIVE_ARTIFACT_NAMES = frozenset(
    {"RESULT.json", "OBSERVATIONS.jsonl", "TRADE-LEDGER.json", "COMPARATORS.json"}
)
PANORAMA_START = "2023-09-01"
PROXY_LABEL = "SGE_AU9999_PROXY"
SPREAD_LABEL = "FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G"
DISCLAIMER = "市场代理评估，不代表招行实际可成交收益"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
POSITIONS = {"PENDING", "CASH", "LONG"}
VERDICTS = {
    "PENDING",
    "PASS",
    "NO_EDGE",
    "NOT_SUPPORTED",
    "INVALIDATED",
    "SUPPORTED_FOR_FURTHER_RESEARCH",
}
EVIDENCE_STATUSES = {"PENDING", "DESCRIPTIVE_ONLY", "UNTOUCHED_EVALUATED"}
EFFECTIVENESS_BASES = {"PENDING", "RETROSPECTIVE_DESCRIPTIVE", "UNTOUCHED_EVALUATION"}
METRIC_UNITS = {
    "initial_wealth": "CNY",
    "final_wealth": "CNY",
    "strategy_return": "PERCENT",
    "buy_hold_return": "PERCENT",
    "excess_return": "PERCENT",
    "max_drawdown": "CNY",
    "transaction_count": "COUNT",
    "full_spread": "CNY_PER_G",
}
TOP_LEVEL_KEYS = {
    "schema_id",
    "labels",
    "evidence",
    "current_state",
    "period",
    "fit",
    "metrics",
    "series",
    "trades",
    "limitations",
}
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class GoldEvaluationReportError(ValueError):
    """Raised when immutable Gold evaluation evidence cannot support the report."""


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GoldEvaluationReportError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _exact_keys(value: Any, expected: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise GoldEvaluationReportError(f"{path} fields are invalid")
    return value


def _iso_date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise GoldEvaluationReportError(f"{path} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GoldEvaluationReportError(f"{path} must be an ISO date") from exc


def _finite(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GoldEvaluationReportError(f"{path} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise GoldEvaluationReportError(f"{path} must be finite")
    return number


def load_gold_evaluation_artifact(
    payload: bytes,
    *,
    evidence_id: str,
) -> tuple[dict[str, Any], str]:
    """Load strict JSON and bind it to an immutable PostgreSQL evidence identity."""

    if SHA256.fullmatch(evidence_id) is None:
        raise GoldEvaluationReportError("evidence_id must be lowercase SHA-256")
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise GoldEvaluationReportError("Gold evaluation artifact is too large")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise GoldEvaluationReportError("Gold evaluation artifact contains a UTF-8 BOM")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, GoldEvaluationReportError):
            raise
        raise GoldEvaluationReportError("Gold evaluation artifact is not strict JSON") from exc
    if not isinstance(value, dict):
        raise GoldEvaluationReportError("Gold evaluation artifact root must be an object")
    validate_gold_evaluation(value)
    return value, hashlib.sha256(payload).hexdigest()


def _strict_json_bytes(payload: bytes, label: str) -> Any:
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise GoldEvaluationReportError(f"{label} is too large")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, GoldEvaluationReportError):
            raise
        raise GoldEvaluationReportError(f"{label} is not strict JSON") from exc


def _decimal(value: Any, path: str) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise GoldEvaluationReportError(f"{path} must be an exact decimal string or integer")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise GoldEvaluationReportError(f"{path} must be an exact decimal") from exc
    if not number.is_finite():
        raise GoldEvaluationReportError(f"{path} must be finite")
    return number


def load_native_gold_evaluation_package(
    members: Mapping[str, bytes],
    *,
    evidence_id: str,
) -> tuple[dict[str, Any], str]:
    """Adapt the immutable rLS package without copying or recomputing research."""

    if SHA256.fullmatch(evidence_id) is None:
        raise GoldEvaluationReportError("evidence_id must be lowercase SHA-256")
    if not NATIVE_ARTIFACT_NAMES.issubset(members):
        raise GoldEvaluationReportError("native Gold evaluation package is incomplete")
    result = _strict_json_bytes(members["RESULT.json"], "RESULT.json")
    ledger = _strict_json_bytes(members["TRADE-LEDGER.json"], "TRADE-LEDGER.json")
    comparators = _strict_json_bytes(members["COMPARATORS.json"], "COMPARATORS.json")
    observations = [
        _strict_json_bytes(line + b"\n", f"OBSERVATIONS.jsonl line {index}")
        for index, line in enumerate(members["OBSERVATIONS.jsonl"].splitlines(), 1)
        if line
    ]
    required_labels = [PROXY_LABEL, SPREAD_LABEL, DISCLAIMER]
    if (
        not isinstance(result, Mapping)
        or result.get("schema") != "quantresearch-gold-rls-retrospective-result/v1"
        or result.get("classification") != "USER_SELECTED_HISTORICALLY_EXPOSED_RETROSPECTIVE"
        or result.get("untouched_confirmation") is not False
        or result.get("production_or_trading_authority") != "NOT_AUTHORIZED"
        or result.get("labels") != required_labels
    ):
        raise GoldEvaluationReportError("RESULT.json does not preserve retrospective research meaning")
    if (
        not isinstance(ledger, Mapping)
        or ledger.get("schema") != "quantresearch-gold-trade-ledger/v1"
        or ledger.get("labels") != required_labels
        or not isinstance(ledger.get("trades"), list)
    ):
        raise GoldEvaluationReportError("TRADE-LEDGER.json is invalid")
    if (
        not isinstance(comparators, Mapping)
        or comparators.get("schema") != "quantresearch-gold-comparators/v1"
        or not isinstance(comparators.get("BUY_AND_HOLD"), Mapping)
    ):
        raise GoldEvaluationReportError("COMPARATORS.json is invalid")
    if not observations or not all(isinstance(item, Mapping) for item in observations):
        raise GoldEvaluationReportError("OBSERVATIONS.jsonl has no observations")

    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        raise GoldEvaluationReportError("RESULT.json metrics are unavailable")
    initial = _decimal(ledger.get("initial_cash_cny"), "TRADE-LEDGER.initial_cash_cny")
    final = _decimal(ledger.get("final_cash_cny"), "TRADE-LEDGER.final_cash_cny")
    strategy_final = _decimal(metrics.get("strategy_final_wealth_cny"), "RESULT.metrics.strategy_final_wealth_cny")
    buy_hold_final = _decimal(metrics.get("buy_and_hold_final_wealth_cny"), "RESULT.metrics.buy_and_hold_final_wealth_cny")
    excess = _decimal(metrics.get("strategy_excess_vs_buy_and_hold_cny"), "RESULT.metrics.strategy_excess_vs_buy_and_hold_cny")
    drawdown = _decimal(metrics.get("strategy_max_drawdown_cny"), "RESULT.metrics.strategy_max_drawdown_cny")
    if final != strategy_final or strategy_final - buy_hold_final != excess or initial <= 0:
        raise GoldEvaluationReportError("native Gold wealth metrics do not join exactly")
    if ledger.get("initial_gold_grams") != 0 or ledger.get("final_gold_grams") != 0:
        raise GoldEvaluationReportError("native Gold evaluation must start and end in cash")
    comparator_final = _decimal(
        comparators["BUY_AND_HOLD"].get("final_wealth_cny"),
        "COMPARATORS.BUY_AND_HOLD.final_wealth_cny",
    )
    if comparator_final != buy_hold_final:
        raise GoldEvaluationReportError("native Gold comparator identity does not join")

    series = []
    for index, item in enumerate(observations):
        required = {
            "trading_date",
            "close_decimal",
            "fitted_level_after_current_close",
            "fitted_slope_after_current_close",
            "gold_grams",
        }
        if not required.issubset(item):
            raise GoldEvaluationReportError(f"OBSERVATIONS.jsonl line {index + 1} is incomplete")
        series.append(
            {
                "date": item["trading_date"],
                "official_close": float(_decimal(item["close_decimal"], "observation.close_decimal")),
                "fitted_level": _finite(item["fitted_level_after_current_close"], "observation.fitted_level"),
                "fitted_trend": _finite(item["fitted_slope_after_current_close"], "observation.fitted_slope"),
                "fit_information_cutoff": item["trading_date"],
                "position": "LONG" if item["gold_grams"] > 0 else "CASH",
            }
        )

    trades = []
    position = "CASH"
    for index, item in enumerate(ledger["trades"]):
        if not isinstance(item, Mapping):
            raise GoldEvaluationReportError("TRADE-LEDGER trades must be objects")
        kind = item.get("kind")
        if kind == "SIGNAL_BUY":
            side, after = "BUY", "LONG"
        elif kind in {"SIGNAL_SELL", "FORCED_TERMINAL_SELL"}:
            side, after = "SELL", "CASH"
        else:
            raise GoldEvaluationReportError("TRADE-LEDGER contains an unsupported action")
        trades.append(
            {
                "trade_id": f"{index + 1}:{item.get('date')}:{kind}",
                "signal_date": item.get("signal_source_date"),
                "execution_date": item.get("date"),
                "kind": kind,
                "side": side,
                "position_before": position,
                "position_after": after,
                "execution_price": float(_decimal(item.get("price_cny_per_g"), "trade.price_cny_per_g")),
                "spread_cny_per_g": 5,
            }
        )
        position = after

    strategy_return = (strategy_final / initial - 1) * 100
    buy_hold_return = (buy_hold_final / initial - 1) * 100
    view = {
        "schema_id": SCHEMA_ID,
        "labels": {
            "market_proxy": PROXY_LABEL,
            "cost_assumption": SPREAD_LABEL,
            "disclaimer": DISCLAIMER,
        },
        "evidence": {
            "status": "DESCRIPTIVE_ONLY",
            "verdict": result.get("verdict"),
            "verdict_line": str(result.get("implication", "Retrospective market-proxy evidence only.")),
            "effectiveness_basis": "RETROSPECTIVE_DESCRIPTIVE",
            "evaluation_run_id": evidence_id,
            "generated_at": None,
        },
        "current_state": {"position": "CASH", "label": "现金（评估期末强制平仓）"},
        "period": {
            "start": metrics.get("first_evaluation_trading_date"),
            "end": metrics.get("last_evaluation_trading_date"),
            "recent_start": max(
                PANORAMA_START, _month_prior(observations[-1]["trading_date"])
            ),
        },
        "fit": {
            "method": "FROZEN_RLS_HUBER_KALMAN",
            "causal": True,
            "description": "回顾性描述拟合；每个拟合状态仅使用该日及此前已发布的官方收盘价，不是未触碰有效性证据。",
        },
        "metrics": {
            "initial_wealth": {"availability": "AVAILABLE", "value": float(initial), "unit": "CNY"},
            "final_wealth": {"availability": "AVAILABLE", "value": float(final), "unit": "CNY"},
            "strategy_return": {"availability": "AVAILABLE", "value": float(strategy_return), "unit": "PERCENT"},
            "buy_hold_return": {"availability": "AVAILABLE", "value": float(buy_hold_return), "unit": "PERCENT"},
            "excess_return": {"availability": "AVAILABLE", "value": float(strategy_return - buy_hold_return), "unit": "PERCENT"},
            "max_drawdown": {"availability": "AVAILABLE", "value": float(drawdown), "unit": "CNY"},
            "transaction_count": {"availability": "AVAILABLE", "value": len(trades), "unit": "COUNT"},
            "full_spread": {"availability": "AVAILABLE", "value": 5, "unit": "CNY_PER_G"},
        },
        "series": series,
        "trades": trades,
        "limitations": [
            "User-selected historically exposed retrospective; not untouched confirmation.",
            "The fixed 5 CNY/g spread is a modeled market-proxy assumption.",
            "The immutable result explicitly grants no production, signal, order, trading, or real-fund authority.",
        ],
    }
    validate_gold_evaluation(view)
    package_digest = hashlib.sha256()
    for name in sorted(NATIVE_ARTIFACT_NAMES):
        package_digest.update(name.encode("utf-8") + b"\0" + members[name] + b"\0")
    return view, package_digest.hexdigest()


def validate_gold_evaluation(value: Mapping[str, Any]) -> None:
    """Validate report meaning before any supplied value reaches HTML."""

    root = _exact_keys(value, TOP_LEVEL_KEYS, "$")
    if root["schema_id"] != SCHEMA_ID:
        raise GoldEvaluationReportError("$.schema_id is unsupported")

    labels = _exact_keys(root["labels"], {"market_proxy", "cost_assumption", "disclaimer"}, "$.labels")
    if labels != {
        "market_proxy": PROXY_LABEL,
        "cost_assumption": SPREAD_LABEL,
        "disclaimer": DISCLAIMER,
    }:
        raise GoldEvaluationReportError("$.labels must preserve the required research labels")

    evidence = _exact_keys(
        root["evidence"],
        {"status", "verdict", "verdict_line", "effectiveness_basis", "evaluation_run_id", "generated_at"},
        "$.evidence",
    )
    status = evidence["status"]
    verdict = evidence["verdict"]
    basis = evidence["effectiveness_basis"]
    if status not in EVIDENCE_STATUSES or verdict not in VERDICTS or basis not in EFFECTIVENESS_BASES:
        raise GoldEvaluationReportError("$.evidence contains an unsupported status, verdict, or basis")
    if not all(isinstance(evidence[name], str) and evidence[name].strip() for name in ("verdict_line", "evaluation_run_id")):
        raise GoldEvaluationReportError("$.evidence text identities must be non-empty")
    if evidence["generated_at"] is not None and (
        not isinstance(evidence["generated_at"], str) or not evidence["generated_at"].strip()
    ):
        raise GoldEvaluationReportError("$.evidence.generated_at must be a timestamp or null")
    if status == "PENDING" and (verdict != "PENDING" or basis != "PENDING"):
        raise GoldEvaluationReportError("pending evidence cannot carry a research conclusion")
    if status == "DESCRIPTIVE_ONLY" and basis != "RETROSPECTIVE_DESCRIPTIVE":
        raise GoldEvaluationReportError("descriptive evidence must be labelled retrospective")
    if verdict == "PASS" and (status != "UNTOUCHED_EVALUATED" or basis != "UNTOUCHED_EVALUATION"):
        raise GoldEvaluationReportError("PASS requires untouched effectiveness evidence")

    current = _exact_keys(root["current_state"], {"position", "label"}, "$.current_state")
    if current["position"] not in POSITIONS or not isinstance(current["label"], str) or not current["label"].strip():
        raise GoldEvaluationReportError("$.current_state is invalid")

    period = _exact_keys(root["period"], {"start", "end", "recent_start"}, "$.period")
    if period["start"] != PANORAMA_START:
        raise GoldEvaluationReportError(f"$.period.start must be {PANORAMA_START}")

    fit = _exact_keys(root["fit"], {"method", "causal", "description"}, "$.fit")
    if fit["causal"] is not True:
        raise GoldEvaluationReportError("$.fit.causal must be true")
    if not all(isinstance(fit[name], str) and fit[name].strip() for name in ("method", "description")):
        raise GoldEvaluationReportError("$.fit method and description must be non-empty")

    metrics = _exact_keys(root["metrics"], set(METRIC_UNITS), "$.metrics")
    for name, expected_unit in METRIC_UNITS.items():
        metric = _exact_keys(metrics[name], {"availability", "value", "unit"}, f"$.metrics.{name}")
        if metric["unit"] != expected_unit or metric["availability"] not in {"AVAILABLE", "PENDING", "UNAVAILABLE"}:
            raise GoldEvaluationReportError(f"$.metrics.{name} has invalid availability or unit")
        if metric["availability"] == "AVAILABLE":
            number = _finite(metric["value"], f"$.metrics.{name}.value")
            if name == "transaction_count" and (not number.is_integer() or number < 0):
                raise GoldEvaluationReportError("$.metrics.transaction_count must be a non-negative integer")
        elif metric["value"] is not None:
            raise GoldEvaluationReportError(f"$.metrics.{name} unavailable value must be null")
    spread = metrics["full_spread"]
    if spread != {"availability": "AVAILABLE", "value": 5, "unit": "CNY_PER_G"}:
        raise GoldEvaluationReportError("$.metrics.full_spread must be the fixed 5 CNY/g assumption")

    limitations = root["limitations"]
    if not isinstance(limitations, list) or not limitations or not all(isinstance(item, str) and item.strip() for item in limitations):
        raise GoldEvaluationReportError("$.limitations must contain concise non-empty statements")

    points = root["series"]
    trades = root["trades"]
    if not isinstance(points, list) or not isinstance(trades, list):
        raise GoldEvaluationReportError("$.series and $.trades must be arrays")
    if status == "PENDING":
        if period["end"] is not None or period["recent_start"] is not None or points or trades:
            raise GoldEvaluationReportError("pending evidence must not carry dates, prices, fit values, or trades")
        if current["position"] != "PENDING":
            raise GoldEvaluationReportError("pending evidence must expose a pending position")
        return

    start = _iso_date(period["start"], "$.period.start")
    end = _iso_date(period["end"], "$.period.end")
    recent_start = _iso_date(period["recent_start"], "$.period.recent_start")
    if not start <= recent_start <= end:
        raise GoldEvaluationReportError("$.period.recent_start must lie inside the panorama")
    if not points:
        raise GoldEvaluationReportError("evaluated evidence requires chart points")

    parsed_points: list[tuple[date, Mapping[str, Any]]] = []
    seen_dates: set[date] = set()
    for index, item in enumerate(points):
        point = _exact_keys(
            item,
            {"date", "official_close", "fitted_level", "fitted_trend", "fit_information_cutoff", "position"},
            f"$.series[{index}]",
        )
        day = _iso_date(point["date"], f"$.series[{index}].date")
        cutoff = _iso_date(point["fit_information_cutoff"], f"$.series[{index}].fit_information_cutoff")
        if day in seen_dates or not start <= day <= end or cutoff > day:
            raise GoldEvaluationReportError("$.series dates/cutoffs violate the declared causal period")
        if point["position"] not in {"CASH", "LONG"}:
            raise GoldEvaluationReportError(f"$.series[{index}].position is invalid")
        _finite(point["official_close"], f"$.series[{index}].official_close")
        _finite(point["fitted_level"], f"$.series[{index}].fitted_level")
        _finite(point["fitted_trend"], f"$.series[{index}].fitted_trend")
        seen_dates.add(day)
        parsed_points.append((day, point))
    if [item[0] for item in parsed_points] != sorted(seen_dates):
        raise GoldEvaluationReportError("$.series dates must be unique and increasing")
    if parsed_points[0][0] != start or parsed_points[-1][0] != end:
        raise GoldEvaluationReportError("$.series must span the exact declared panorama")
    if not any(day >= recent_start for day, _ in parsed_points):
        raise GoldEvaluationReportError("recent window has no chart point")
    point_by_day = {day: point for day, point in parsed_points}
    position = "CASH"
    trade_by_day: dict[date, Mapping[str, Any]] = {}
    trade_ids: set[str] = set()
    for index, item in enumerate(trades):
        trade = _exact_keys(
            item,
            {"trade_id", "signal_date", "execution_date", "kind", "side", "position_before", "position_after", "execution_price", "spread_cny_per_g"},
            f"$.trades[{index}]",
        )
        if not isinstance(trade["trade_id"], str) or not trade["trade_id"] or trade["trade_id"] in trade_ids:
            raise GoldEvaluationReportError("$.trades trade_id must be non-empty and unique")
        execution_day = _iso_date(trade["execution_date"], f"$.trades[{index}].execution_date")
        kind = trade["kind"]
        if kind not in {"SIGNAL_BUY", "SIGNAL_SELL", "FORCED_TERMINAL_SELL"}:
            raise GoldEvaluationReportError("$.trades kind is invalid")
        if kind == "FORCED_TERMINAL_SELL":
            if trade["signal_date"] is not None or execution_day != end:
                raise GoldEvaluationReportError("forced terminal sell must be unsignaled and terminal")
        else:
            signal_day = _iso_date(trade["signal_date"], f"$.trades[{index}].signal_date")
            if signal_day >= execution_day:
                raise GoldEvaluationReportError("signal-driven trades must execute on a later session")
        if execution_day not in point_by_day or execution_day in trade_by_day:
            raise GoldEvaluationReportError("$.trades must use a later unique in-period execution session")
        _finite(trade["execution_price"], f"$.trades[{index}].execution_price")
        if _finite(trade["spread_cny_per_g"], f"$.trades[{index}].spread_cny_per_g") != 5:
            raise GoldEvaluationReportError("every trade must preserve the fixed 5 CNY/g spread assumption")
        expected = ("BUY", "CASH", "LONG") if position == "CASH" else ("SELL", "LONG", "CASH")
        if (trade["side"], trade["position_before"], trade["position_after"]) != expected:
            raise GoldEvaluationReportError("$.trades do not form a complete alternating cash/long ledger")
        position = expected[2]
        trade_by_day[execution_day] = trade
        trade_ids.add(trade["trade_id"])

    derived_position = "CASH"
    for day, point in parsed_points:
        if day in trade_by_day:
            derived_position = str(trade_by_day[day]["position_after"])
        if point["position"] != derived_position:
            raise GoldEvaluationReportError("$.series positions do not equal the trade ledger")
    if current["position"] != derived_position:
        raise GoldEvaluationReportError("$.current_state.position does not equal the final ledger state")
    transaction_count = metrics["transaction_count"]
    if transaction_count["availability"] != "AVAILABLE" or int(transaction_count["value"]) != len(trades):
        raise GoldEvaluationReportError("$.metrics.transaction_count must equal the trade ledger")


def _metric_text(metric: Mapping[str, Any]) -> str:
    availability = metric["availability"]
    if availability != "AVAILABLE":
        return "待评估" if availability == "PENDING" else "不可用"
    value = metric["value"]
    unit = metric["unit"]
    if unit == "CNY":
        return f"¥{float(value):,.2f}"
    if unit == "PERCENT":
        return f"{float(value):+.2f}%"
    if unit == "COUNT":
        return str(int(value))
    if unit == "CNY_PER_G":
        return f"{float(value):g} CNY/g"
    return str(value)


def _month_prior(value: str) -> str:
    current = _iso_date(value, "evaluation end date")
    year = current.year if current.month > 1 else current.year - 1
    month = current.month - 1 if current.month > 1 else 12
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def _chart(points: Sequence[Mapping[str, Any]], trades: Sequence[Mapping[str, Any]], title: str, test_id: str) -> str:
    if not points:
        return (
            f'<section class="chart pending" data-testid="{test_id}" role="status">'
            f'<div><h2>{html.escape(title)}</h2><p>待评估：权威不可变评估产物尚未提供。</p></div></section>'
        )
    width, height = 960.0, 340.0
    left, right, top, bottom = 52.0, 20.0, 22.0, 42.0
    values = [float(point[key]) for point in points for key in ("official_close", "fitted_level")]
    low, high = min(values), max(values)
    padding = max((high - low) * 0.08, 1.0)
    low -= padding
    high += padding

    def x(index: int) -> float:
        return left if len(points) == 1 else left + index * (width - left - right) / (len(points) - 1)

    def y(value: float) -> float:
        return top + (high - value) * (height - top - bottom) / (high - low)

    price_path = " ".join(f"{x(index):.2f},{y(float(point['official_close'])):.2f}" for index, point in enumerate(points))
    fit_path = " ".join(f"{x(index):.2f},{y(float(point['fitted_level'])):.2f}" for index, point in enumerate(points))
    dots = []
    for index, point in enumerate(points):
        tooltip = (
            f"{point['date']} · Au99.99 {float(point['official_close']):.2f} CNY/g · "
            f"causal fit {float(point['fitted_level']):.2f} · trend {float(point['fitted_trend']):+.6f}"
        )
        dots.append(
            f'<circle class="hover-dot" cx="{x(index):.2f}" cy="{y(float(point["official_close"])):.2f}" r="7" tabindex="0"><title>{html.escape(tooltip)}</title></circle>'
        )
    index_by_date = {str(point["date"]): index for index, point in enumerate(points)}
    markers = []
    for trade in trades:
        index = index_by_date.get(str(trade["execution_date"]))
        if index is None:
            continue
        px = x(index)
        py = y(float(trade["execution_price"]))
        side = str(trade["side"])
        shape = (
            f"{px:.2f},{py - 10:.2f} {px - 8:.2f},{py + 7:.2f} {px + 8:.2f},{py + 7:.2f}"
            if side == "BUY"
            else f"{px:.2f},{py + 10:.2f} {px - 8:.2f},{py - 7:.2f} {px + 8:.2f},{py - 7:.2f}"
        )
        tooltip = f"{trade['execution_date']} · {side} · {float(trade['execution_price']):.2f} CNY/g"
        markers.append(f'<polygon class="marker {side.lower()}" points="{shape}" tabindex="0"><title>{html.escape(tooltip)}</title></polygon>')
    first, last = html.escape(str(points[0]["date"])), html.escape(str(points[-1]["date"]))
    return (
        f'<section class="chart" data-testid="{test_id}"><div class="chart-heading"><div><h2>{html.escape(title)}</h2>'
        '<p>官方 Au99.99 收盘价与逐日因果拟合；悬停或聚焦数据点查看数值。</p></div>'
        '<div class="legend"><span class="price-key">收盘价</span><span class="fit-key">因果拟合</span>'
        '<span class="buy-key">BUY</span><span class="sell-key">SELL</span></div></div>'
        f'<svg viewBox="0 0 {width:g} {height:g}" role="img" aria-label="{html.escape(title)}">'
        f'<line class="axis" x1="{left:g}" y1="{height-bottom:g}" x2="{width-right:g}" y2="{height-bottom:g}" />'
        f'<line class="axis" x1="{left:g}" y1="{top:g}" x2="{left:g}" y2="{height-bottom:g}" />'
        f'<text x="{left:g}" y="{height-13:g}">{first}</text><text text-anchor="end" x="{width-right:g}" y="{height-13:g}">{last}</text>'
        f'<text x="6" y="{top+6:g}">{high:.1f}</text><text x="6" y="{height-bottom:g}">{low:.1f}</text>'
        f'<polyline class="price-line" points="{price_path}"/><polyline class="fit-line" points="{fit_path}"/>'
        f'{"".join(dots)}{"".join(markers)}</svg></section>'
    )


def render_gold_evaluation_report(
    value: Mapping[str, Any],
    *,
    evidence_id: str,
    artifact_sha256: str,
) -> bytes:
    """Render a validated immutable evaluation as self-contained offline HTML."""

    validate_gold_evaluation(value)
    if SHA256.fullmatch(evidence_id) is None or SHA256.fullmatch(artifact_sha256) is None:
        raise GoldEvaluationReportError("report provenance identities must be lowercase SHA-256")
    evidence = value["evidence"]
    period = value["period"]
    metrics = value["metrics"]
    points = value["series"]
    trades = value["trades"]
    recent = [] if period["recent_start"] is None else [point for point in points if point["date"] >= period["recent_start"]]
    metric_labels = {
        "initial_wealth": "初始资产",
        "final_wealth": "期末资产",
        "strategy_return": "策略收益",
        "buy_hold_return": "买入持有",
        "excess_return": "超额收益",
        "max_drawdown": "最大回撤",
        "transaction_count": "交易次数",
        "full_spread": "完整价差",
    }
    metric_html = "".join(
        f'<div><span>{label}</span><strong>{html.escape(_metric_text(metrics[name]))}</strong></div>'
        for name, label in metric_labels.items()
    )
    trade_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item['execution_date']))}</td>"
        f"<td><strong class=\"{str(item['side']).lower()}\">{html.escape(str(item['side']))}</strong></td>"
        f"<td>{float(item['execution_price']):.2f} CNY/g</td>"
        f"<td>{html.escape(str(item['position_before']))} → {html.escape(str(item['position_after']))}</td>"
        "</tr>"
        for item in trades
    ) or '<tr><td colspan="4">待评估：交易台账尚未提供。</td></tr>'
    limitation_html = "".join(f"<li>{html.escape(item)}</li>" for item in value["limitations"])
    period_text = "待评估" if period["end"] is None else f"{period['start']} — {period['end']}"
    status_class = "pending" if evidence["status"] == "PENDING" else "ready"
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gold evaluation report</title>
<style>
:root{{--ink:#17221c;--muted:#637068;--paper:#f6f3eb;--card:#fffdf7;--gold:#a96f08;--fit:#2369a8;--buy:#147a45;--sell:#b43838;--line:#d9d3c5}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1120px;margin:auto;padding:18px}}header,.chart,.metrics,.trades,.limits{{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:0 4px 18px #453a2210}}header{{padding:20px;margin-bottom:14px}}.eyebrow,.labels{{font-size:12px;letter-spacing:.04em;color:var(--muted)}}h1{{font-size:clamp(1.25rem,4vw,2rem);margin:.25rem 0}}.verdict{{font-size:1.05rem;margin:.5rem 0}}.state{{display:inline-block;padding:.25rem .55rem;border-radius:999px;background:#e8eee9}}.status.pending{{color:#875c00}}.labels{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}}.labels span{{border:1px solid var(--line);border-radius:999px;padding:3px 7px}}.chart{{padding:14px;margin:14px 0;min-height:300px}}.chart.pending{{display:grid;place-items:center;text-align:center}}.chart-heading{{display:flex;justify-content:space-between;gap:12px;align-items:start}}h2{{font-size:1rem;margin:0 0 4px}}p{{margin:.25rem 0}}.legend{{display:flex;flex-wrap:wrap;gap:10px;font-size:12px}}.legend span:before{{content:"";display:inline-block;width:14px;height:3px;margin-right:4px;vertical-align:middle;background:var(--ink)}}.legend .price-key:before{{background:var(--gold)}}.legend .fit-key:before{{background:var(--fit)}}.legend .buy-key:before{{background:var(--buy)}}.legend .sell-key:before{{background:var(--sell)}}svg{{display:block;width:100%;height:auto;min-height:240px}}svg text{{font-size:12px;fill:var(--muted)}}.axis{{stroke:#c9c2b3;stroke-width:1}}.price-line,.fit-line{{fill:none;stroke-width:2.2;stroke-linejoin:round;stroke-linecap:round}}.price-line{{stroke:var(--gold)}}.fit-line{{stroke:var(--fit)}}.hover-dot{{fill:transparent;stroke:transparent;cursor:crosshair}}.hover-dot:hover,.hover-dot:focus{{fill:var(--gold);stroke:#fff;stroke-width:2;outline:none}}.marker{{stroke:#fff;stroke-width:1.5}}.marker.buy{{fill:var(--buy)}}.marker.sell{{fill:var(--sell)}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:14px 0}}.metrics div{{padding:12px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}.metrics span{{display:block;color:var(--muted);font-size:12px}}.metrics strong{{display:block;margin-top:3px}}.trades,.limits{{padding:14px;margin:14px 0}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{padding:8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}.buy{{color:var(--buy)}}.sell{{color:var(--sell)}}.limits ul{{margin:.4rem 0;padding-left:1.2rem}}footer{{color:var(--muted);font-size:11px;word-break:break-all;padding:8px 2px 24px}}@media(max-width:640px){{main{{padding:8px}}header{{padding:14px}}.chart{{min-height:260px;padding:10px}}.chart-heading{{display:block}}.legend{{margin-top:8px}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}svg{{min-height:220px}}}}
</style>
</head>
<body><main>
<header data-testid="report-summary"><div class="eyebrow">GOLD RESEARCH · {html.escape(str(evidence['status']))}</div><h1>黄金两周期评估</h1><p class="verdict status {status_class}"><strong>{html.escape(str(evidence['verdict']))}</strong> · {html.escape(str(evidence['verdict_line']))}</p><p>当前状态：<span class="state">{html.escape(str(value['current_state']['label']))}</span> · 评估区间：{html.escape(period_text)}</p><div class="labels"><span>{PROXY_LABEL}</span><span>{SPREAD_LABEL}</span><span>{DISCLAIMER}</span></div></header>
{_chart(recent, trades, "最近一个月", "recent-chart")}
<section class="metrics" aria-label="核心指标">{metric_html}</section>
{_chart(points, trades, "三年全景（自 2023-09-01）", "panorama-chart")}
<section class="trades"><h2>交易序列</h2><div class="table-wrap"><table><thead><tr><th>执行日</th><th>动作</th><th>执行价</th><th>仓位</th></tr></thead><tbody>{trade_rows}</tbody></table></div></section>
<section class="limits"><h2>限制</h2><p><strong>拟合性质：</strong>{html.escape(str(value['fit']['description']))}</p><p><strong>有效性证据：</strong>{html.escape(str(evidence['effectiveness_basis']))}</p><ul>{limitation_html}</ul></section>
<footer>Evaluation run: {html.escape(str(evidence['evaluation_run_id']))}<br>PostgreSQL evidence id: {evidence_id}<br>gold-evaluation.json SHA-256: {artifact_sha256}</footer>
</main></body></html>\n"""
    return document.encode("utf-8")
