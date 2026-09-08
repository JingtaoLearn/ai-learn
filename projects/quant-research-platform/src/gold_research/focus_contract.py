"""Closed contracts and constants for the frozen Gold FOCuS candidate."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping

LABEL_PROXY = "SGE_AU9999_PROXY"
LABEL_SPREAD = "FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G"
DISCLAIMER_ZH = "市场代理评估，不代表招行实际可成交收益"
COLLECTION_CLASS = "HISTORICAL_SNAPSHOT_V1"
PROSPECTIVE_CLASS = "PROSPECTIVE_D10_V1"
INSTRUMENT = "Au99.99"
FIELD = "Close"
UNIT = "CNY/g"
TIMEZONE = "Asia/Shanghai"
CAPITAL = Decimal("100000.00")
HALF_SPREAD = Decimal("2.50")
INCREMENTS = 756
WARMUP_INCREMENTS = 63
FIRST_UPDATE = 64
FIRST_CONFIRMATION = 65
COOLDOWN_UPDATES = 5
BOOTSTRAP_BLOCK = 20
BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = 2_026_090_701
BOOTSTRAP_LOWER_RANK = 500
SYNTHETIC_SEED = 2_026_090_702
SYNTHETIC_PATHS = 140_000
NULL_CALIBRATION_PATHS = 25_000
NULL_VALIDATION_PATHS = 25_000
CALIBRATION_RANK = 24_375


class ContractViolation(ValueError):
    """A closed candidate contract was not satisfied."""


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class Position(StrEnum):
    CASH = "CASH"
    LONG = "LONG"


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TerminalClass(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INVALIDATED = "INVALIDATED"


class Phase(StrEnum):
    IMPLEMENTATION_VERIFIED = "IMPLEMENTATION_VERIFIED"
    IMPLEMENTATION_REVIEWED = "IMPLEMENTATION_REVIEWED"
    CALIBRATION_CLAIMED = "CALIBRATION_CLAIMED"
    CALIBRATION_SEALED = "CALIBRATION_SEALED"
    CALIBRATION_REVIEWED = "CALIBRATION_REVIEWED"
    CAPTURE_LOCKED = "CAPTURE_LOCKED"
    RAW_SEALED = "RAW_SEALED"
    RAW_REVIEWED = "RAW_REVIEWED"
    EVALUATION_CLAIMED = "EVALUATION_CLAIMED"
    EVALUATION_SEALED = "EVALUATION_SEALED"


@dataclass(frozen=True)
class RequestIdentity:
    method: str
    url: str
    ordered_query: tuple[tuple[str, str], ...]
    expected_date: str
    instrument: str = INSTRUMENT
    collection_class: str = COLLECTION_CLASS


@dataclass(frozen=True)
class ReceiptMetadata:
    retrieved_at_utc: str
    retrieved_at_asia_shanghai: str
    receipt_sequence: int
    http_status: int
    content_type: str
    request_sha256: str


@dataclass(frozen=True)
class ParsedVersion:
    trading_date: str
    instrument: str
    field: str
    value_decimal: str
    unit: str
    raw_sha256: str
    raw_size_bytes: int
    labels: tuple[str, str] = (LABEL_PROXY, LABEL_SPREAD)
    disclaimer_zh: str = DISCLAIMER_ZH


@dataclass(frozen=True)
class DetectorOutput:
    statistic: float
    changepoint: int
    direction: Direction
    observations: int


@dataclass(frozen=True)
class MachineState:
    position: Position = Position.CASH
    cooldown: int = 0
    confirmation: Direction | None = None
    pending: Action | None = None


@dataclass(frozen=True)
class Portfolio:
    cash: Decimal = CAPITAL
    grams: Decimal = Decimal("0")


@dataclass(frozen=True)
class CalibrationDocument:
    threshold: float
    master_seed: int = SYNTHETIC_SEED
    path_count: int = SYNTHETIC_PATHS
    thresholds_calibrated: int = 1
    recalibrations: int = 0


@dataclass(frozen=True)
class EvaluationDocument:
    terminal_class: TerminalClass
    lower_bounds: Mapping[str, float]
    candidate_terminal_wealth: str
    qbar: float
    labels: tuple[str, str] = (LABEL_PROXY, LABEL_SPREAD)
    disclaimer_zh: str = DISCLAIMER_ZH


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractViolation(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise ContractViolation(f"JSON decimal {value!r} must be a canonical string")


def _reject_constant(value: str) -> None:
    raise ContractViolation(f"non-finite JSON value {value!r} is forbidden")


def strict_json_loads(raw: bytes) -> Any:
    """Load UTF-8 JSON while rejecting duplicate keys, floats, and constants."""

    if not isinstance(raw, bytes):
        raise ContractViolation("JSON input must be bytes")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractViolation("input is not strict UTF-8 JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Encode deterministic JSON; decimal quantities must already be strings."""

    def validate(item: Any) -> None:
        if isinstance(item, float):
            raise ContractViolation("floating-point values are forbidden in manifests")
        if isinstance(item, Decimal):
            raise ContractViolation("Decimal values must be canonical strings in manifests")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ContractViolation("manifest keys must be strings")
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)

    validate(value)
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractViolation("value is not canonical JSON") from exc


def require_finite_number(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ContractViolation(f"{label} must be finite")
    return result


def require_positive_decimal(value: str | Decimal, label: str) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ContractViolation(f"{label} must be an exact decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContractViolation(f"{label} must be decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ContractViolation(f"{label} must be finite and positive")
    return parsed


def contract_document() -> dict[str, Any]:
    """Return the non-overridable machine constants for identity sealing."""

    calibration = asdict(CalibrationDocument(threshold=0.0))
    calibration["threshold"] = "CALIBRATED_ONCE_AFTER_IMPLEMENTATION_REVIEW"
    return {
        "schema": "quant-research/gold-focus-contract/v1",
        "collection_class": COLLECTION_CLASS,
        "prohibited_mixed_class": PROSPECTIVE_CLASS,
        "instrument": INSTRUMENT,
        "field": FIELD,
        "unit": UNIT,
        "timezone": TIMEZONE,
        "labels": [LABEL_PROXY, LABEL_SPREAD],
        "disclaimer_zh": DISCLAIMER_ZH,
        "capital": str(CAPITAL),
        "half_spread": str(HALF_SPREAD),
        "increments": INCREMENTS,
        "warmup_increments": WARMUP_INCREMENTS,
        "first_update": FIRST_UPDATE,
        "first_confirmation": FIRST_CONFIRMATION,
        "cooldown_updates": COOLDOWN_UPDATES,
        "bootstrap": {
            "block": BOOTSTRAP_BLOCK,
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "lower_rank": BOOTSTRAP_LOWER_RANK,
        },
        "calibration": calibration,
        "terminal_classes": [item.value for item in TerminalClass],
    }
