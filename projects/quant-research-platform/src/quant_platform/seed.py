from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .schemas import canonical_json_bytes, validate_defaults, validate_parameter_schema


TEMPLATE_NAME = "single_stock_daily_causal"
TEMPLATE_VERSION = "1"
CREATED_AT = "2026-08-27T00:00:00Z"
SLOTS = ["fit", "smoothing", "statistic", "decision", "sizing", "cost", "report"]


def _object_schema(properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


TEMPLATE_SCHEMA = _object_schema(
    {
        "instrument_display_name": {"type": "string"},
        "evaluation_start": {"type": "string"},
        "evaluation_end": {"type": "string", "nullable": True},
        "initial_capital_cny": {"type": "number", "minimum": 0},
        "initial_state": {"type": "string", "enum": ["flat"]},
        "terminal_handling": {
            "type": "string",
            "enum": ["mark_to_market", "force_liquidate"],
        },
        "cost_assumption_label": {"type": "string"},
    }
)
TEMPLATE_DEFAULTS = {
    "instrument_display_name": "Bank of Communications (601328.SS)",
    "evaluation_start": "2025-01-02",
    "evaluation_end": None,
    "initial_capital_cny": 100000.0,
    "initial_state": "flat",
    "terminal_handling": "mark_to_market",
    "cost_assumption_label": (
        "Conservative research assumptions; not an account-specific fee schedule."
    ),
}


BUILTINS = (
    {
        "operator_id": "prior_log_ols",
        "slot": "fit",
        "title_zh": "前序对数线性拟合",
        "summary_zh": "仅使用执行日前历史窗口拟合对数价格趋势。",
        "documentation": "Fits log prices from the strictly prior session window.",
        "parameter_schema": _object_schema(
            {
                "window_sessions": {"type": "integer", "minimum": 2},
                "price_column": {
                    "type": "string",
                    "enum": ["AdjustedClose", "Close"],
                },
            }
        ),
        "defaults": {"window_sessions": 20, "price_column": "AdjustedClose"},
    },
    {
        "operator_id": "recursive_log_ema",
        "slot": "smoothing",
        "title_zh": "递归对数指数平滑",
        "summary_zh": "以递归指数均线平滑拟合曲线。",
        "documentation": "Applies a recursive EMA in log-price space.",
        "parameter_schema": _object_schema(
            {"span_sessions": {"type": "integer", "minimum": 1}}
        ),
        "defaults": {"span_sessions": 5},
    },
    {
        "operator_id": "adjacent_curve_pct_slope",
        "slot": "statistic",
        "title_zh": "相邻曲线百分比斜率",
        "summary_zh": "计算相邻平滑曲线的日百分比变化。",
        "documentation": "Computes adjacent smoothed-curve percentage change.",
        "parameter_schema": _object_schema({}),
        "defaults": {},
    },
    {
        "operator_id": "post_start_threshold_crossing_hysteresis",
        "slot": "decision",
        "title_zh": "阈值交叉滞回决策",
        "summary_zh": "仅在评估期内识别买卖阈值交叉。",
        "documentation": "Uses post-start threshold crossings with flat/long hysteresis.",
        "parameter_schema": _object_schema(
            {
                "buy_threshold_pct_per_day": {"type": "number", "minimum": 0},
                "sell_threshold_abs_pct_per_day": {"type": "number", "minimum": 0},
            }
        ),
        "defaults": {
            "buy_threshold_pct_per_day": 0.2,
            "sell_threshold_abs_pct_per_day": 0.2,
        },
    },
    {
        "operator_id": "all_in_all_out_a_share_lots",
        "slot": "sizing",
        "title_zh": "A股整手全进全出",
        "summary_zh": "按资金比例和整手约束计算可负担数量。",
        "documentation": "Sizes the largest affordable board-lot position.",
        "parameter_schema": _object_schema(
            {
                "lot_size": {"type": "integer", "minimum": 1},
                "target_fraction": {"type": "number", "minimum": 0, "maximum": 1},
            }
        ),
        "defaults": {"lot_size": 100, "target_fraction": 1.0},
    },
    {
        "operator_id": "cms_china_a_share",
        "slot": "cost",
        "title_zh": "中国A股研究成本",
        "summary_zh": "逐项计算佣金、过户费、印花税和滑点。",
        "documentation": "Applies explicit conservative China A-share research costs.",
        "parameter_schema": _object_schema(
            {
                "commission_rate": {"type": "number", "minimum": 0},
                "minimum_commission_cny": {"type": "number", "minimum": 0},
                "transfer_fee_rate": {"type": "number", "minimum": 0},
                "sell_stamp_tax_rate": {"type": "number", "minimum": 0},
                "buy_slippage_bps": {"type": "number", "minimum": 0},
                "sell_slippage_bps": {"type": "number", "minimum": 0},
            }
        ),
        "defaults": {
            "commission_rate": 0.0003,
            "minimum_commission_cny": 5.0,
            "transfer_fee_rate": 0.00001,
            "sell_stamp_tax_rate": 0.0005,
            "buy_slippage_bps": 5.0,
            "sell_slippage_bps": 5.0,
        },
    },
    {
        "operator_id": "concise_chinese_causal_trade",
        "slot": "report",
        "title_zh": "简明中文因果交易报告",
        "summary_zh": "生成自包含的因果回放与账户核对报告。",
        "documentation": "Renders the existing self-contained causal Chinese report.",
        "parameter_schema": _object_schema({}),
        "defaults": {},
    },
)
BUILTIN_OPERATOR_IDS = tuple(item["operator_id"] for item in BUILTINS)


def _write_builtin_bundle(
    catalog: Catalog, descriptor: dict[str, Any], content_digest: str
) -> str:
    relative = Path("operators") / descriptor["operator_id"] / "1.0.0"
    target = catalog.state_root / relative
    manifest = descriptor | {
        "version": "1.0.0",
        "content_digest": content_digest,
        "validation_evidence": {"kind": "trusted_builtin", "passed": True},
        "created_at": CREATED_AT,
    }
    if target.exists():
        current = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if current != manifest:
            raise RuntimeError(f"immutable built-in bundle conflict: {relative}")
        return relative.as_posix()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".seed-", dir=target.parent))
    try:
        manifest_path = temporary / "manifest.json"
        with manifest_path.open("xb") as stream:
            stream.write(
                json.dumps(
                    manifest,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        manifest_path.chmod(0o444)
        temporary.chmod(0o555)
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            temporary.chmod(0o755)
            shutil.rmtree(temporary)
    return relative.as_posix()


def seed_catalog(catalog: Catalog) -> None:
    validate_parameter_schema(TEMPLATE_SCHEMA)
    validate_defaults(TEMPLATE_SCHEMA, TEMPLATE_DEFAULTS)
    template_identity = {
        "name": TEMPLATE_NAME,
        "version": TEMPLATE_VERSION,
        "slots": SLOTS,
        "parameter_schema": TEMPLATE_SCHEMA,
        "defaults": TEMPLATE_DEFAULTS,
    }
    template_digest = hashlib.sha256(canonical_json_bytes(template_identity)).hexdigest()
    with catalog.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO templates(
                name, version, slots_json, parameter_schema_json, defaults_json,
                content_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                TEMPLATE_NAME,
                TEMPLATE_VERSION,
                json.dumps(SLOTS, separators=(",", ":")),
                canonical_json_bytes(TEMPLATE_SCHEMA).decode(),
                canonical_json_bytes(TEMPLATE_DEFAULTS).decode(),
                template_digest,
                CREATED_AT,
            ),
        )

    for descriptor in BUILTINS:
        validate_parameter_schema(descriptor["parameter_schema"])
        validate_defaults(descriptor["parameter_schema"], descriptor["defaults"])
        identity = descriptor | {"version": "1.0.0", "kind": "builtin"}
        digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
        bundle_path = _write_builtin_bundle(catalog, descriptor, digest)
        with catalog.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO operators(
                    operator_id, slot, title_zh, summary_zh, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    descriptor["operator_id"],
                    descriptor["slot"],
                    descriptor["title_zh"],
                    descriptor["summary_zh"],
                    CREATED_AT,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO operator_versions(
                    operator_id, version, content_digest, parameter_schema_json,
                    defaults_json, documentation, bundle_path,
                    validation_evidence_json, status, created_at
                ) VALUES (?, '1.0.0', ?, ?, ?, ?, ?, ?, 'PUBLISHED', ?)
                """,
                (
                    descriptor["operator_id"],
                    digest,
                    canonical_json_bytes(descriptor["parameter_schema"]).decode(),
                    canonical_json_bytes(descriptor["defaults"]).decode(),
                    descriptor["documentation"],
                    bundle_path,
                    '{"kind":"trusted_builtin","passed":true}',
                    CREATED_AT,
                ),
            )
            catalog._set_latest_if_newer(
                connection, descriptor["operator_id"], "1.0.0", digest, "PUBLISHED"
            )
