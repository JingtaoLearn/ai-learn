from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .strategy_operators import (
    OPERATOR_REGISTRY,
    OperatorSpec,
    ParameterSpec,
    validate_registry,
)


class StrategyConfigError(ValueError):
    """Raised when a strategy run configuration is not exact and safe."""


@dataclass(frozen=True)
class ValidatedStrategyConfig:
    canonical: dict[str, Any]
    canonical_bytes: bytes
    config_sha256: str
    template_parameters: Mapping[str, Any]
    operators: Mapping[str, OperatorSpec]


TEMPLATE_PARAMETERS = {
    "instrument_display_name": ParameterSpec("string"),
    "evaluation_start": ParameterSpec("date"),
    "evaluation_end": ParameterSpec("date", nullable=True),
    "initial_capital_cny": ParameterSpec("number", minimum=0),
    "initial_state": ParameterSpec("string", choices=("flat",)),
    "terminal_handling": ParameterSpec(
        "string", choices=("mark_to_market", "force_liquidate")
    ),
    "cost_assumption_label": ParameterSpec("string"),
}
REQUIRED_SLOTS = {
    "fit",
    "smoothing",
    "statistic",
    "decision",
    "sizing",
    "cost",
    "report",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")

validate_registry(OPERATOR_REGISTRY, set(TEMPLATE_PARAMETERS))


def _exact_fields(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StrategyConfigError(f"{path} must be a JSON object")
    fields = set(value)
    missing = sorted(expected - fields)
    unknown = sorted(fields - expected)
    if missing:
        raise StrategyConfigError(f"{path} has missing fields: {missing}")
    if unknown:
        raise StrategyConfigError(f"{path} has unknown fields: {unknown}")
    return value


def _validate_parameter(value: Any, spec: ParameterSpec, path: str) -> Any:
    if value is None:
        if spec.nullable:
            return None
        raise StrategyConfigError(f"{path} cannot be null")
    if spec.kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise StrategyConfigError(f"{path} must be a non-empty string")
        normalized: Any = value
    elif spec.kind == "date":
        if not isinstance(value, str):
            raise StrategyConfigError(f"{path} must be an ISO date string")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise StrategyConfigError(f"{path} must be an ISO date string") from exc
        if parsed.isoformat() != value:
            raise StrategyConfigError(f"{path} must use canonical YYYY-MM-DD format")
        normalized = value
    elif spec.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise StrategyConfigError(f"{path} must be an integer")
        normalized = value
    elif spec.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StrategyConfigError(f"{path} must be a number")
        if not math.isfinite(value):
            raise StrategyConfigError(f"{path} must be finite")
        normalized = float(value)
    else:
        raise RuntimeError(f"unsupported parameter kind: {spec.kind}")
    if spec.choices and normalized not in spec.choices:
        raise StrategyConfigError(f"{path} must be one of {list(spec.choices)}")
    if spec.minimum is not None and normalized < spec.minimum:
        raise StrategyConfigError(f"{path} must be at least {spec.minimum}")
    if spec.maximum is not None and normalized > spec.maximum:
        raise StrategyConfigError(f"{path} must be at most {spec.maximum}")
    return normalized


def _validate_parameters(
    value: Any, specs: Mapping[str, ParameterSpec], path: str
) -> dict[str, Any]:
    parameters = _exact_fields(value, set(specs), path)
    unknown = set(parameters) - set(specs)
    if unknown:
        raise StrategyConfigError(
            f"{path} has unrecognized parameters: {sorted(unknown)}"
        )
    return {
        name: _validate_parameter(parameters[name], spec, f"{path}.{name}")
        for name, spec in specs.items()
    }


def _validate_operator(
    slot: str, value: Any
) -> tuple[dict[str, Any], OperatorSpec]:
    operator = _exact_fields(value, {"name", "version", "parameters"}, f"operators.{slot}")
    name = operator["name"]
    version = operator["version"]
    if not isinstance(name, str) or not isinstance(version, str):
        raise StrategyConfigError(
            f"operators.{slot} name and version must be strings"
        )
    key = (slot, name, version)
    spec = OPERATOR_REGISTRY.get(key)
    if spec is None:
        other_slot = any(
            candidate.name == name and candidate.version == version
            for candidate in OPERATOR_REGISTRY.values()
        )
        if other_slot:
            raise StrategyConfigError(
                f"operator {name}@{version} is incompatible with slot {slot}"
            )
        raise StrategyConfigError(
            f"unknown operator for slot {slot}: {name}@{version}"
        )
    parameters_value = operator["parameters"]
    if not isinstance(parameters_value, dict):
        raise StrategyConfigError(f"operators.{slot}.parameters must be a JSON object")
    unknown = set(parameters_value) - set(spec.parameters)
    missing = set(spec.parameters) - set(parameters_value)
    if unknown:
        raise StrategyConfigError(
            f"operators.{slot}.parameters has unrecognized parameters: {sorted(unknown)}"
        )
    if missing:
        raise StrategyConfigError(
            f"operators.{slot}.parameters has missing parameters: {sorted(missing)}"
        )
    parameters = {
        parameter_name: _validate_parameter(
            parameters_value[parameter_name],
            parameter_spec,
            f"operators.{slot}.parameters.{parameter_name}",
        )
        for parameter_name, parameter_spec in spec.parameters.items()
    }
    return {
        "name": name,
        "version": version,
        "parameters": parameters,
    }, spec


def _validate_path(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise StrategyConfigError(f"{path} must be a non-empty filesystem path")
    return value


def validate_strategy_config(value: Any) -> ValidatedStrategyConfig:
    config = _exact_fields(
        value,
        {"schema_version", "dataset", "output_root", "template", "operators"},
        "config",
    )
    if isinstance(config["schema_version"], bool) or config["schema_version"] != 1:
        raise StrategyConfigError("schema_version must be integer 1")
    dataset = _exact_fields(config["dataset"], {"root", "snapshot_id"}, "dataset")
    dataset_root = _validate_path(dataset["root"], "dataset.root")
    snapshot_id = dataset["snapshot_id"]
    if not isinstance(snapshot_id, str) or not SHA256.fullmatch(snapshot_id):
        raise StrategyConfigError("dataset.snapshot_id must be a lowercase SHA-256 value")

    template = _exact_fields(
        config["template"], {"name", "version", "parameters"}, "template"
    )
    if (
        template["name"] != "single_stock_daily_causal"
        or template["version"] != "1"
    ):
        raise StrategyConfigError(
            f"unknown template: {template['name']}@{template['version']}"
        )
    template_parameters = _validate_parameters(
        template["parameters"], TEMPLATE_PARAMETERS, "template.parameters"
    )
    start = date.fromisoformat(template_parameters["evaluation_start"])
    end_value = template_parameters["evaluation_end"]
    if end_value is not None and date.fromisoformat(end_value) < start:
        raise StrategyConfigError(
            "template.parameters.evaluation_end cannot precede evaluation_start"
        )
    if template_parameters["initial_capital_cny"] <= 0:
        raise StrategyConfigError(
            "template.parameters.initial_capital_cny must be strictly positive"
        )

    operators_value = _exact_fields(config["operators"], REQUIRED_SLOTS, "operators")
    normalized_operators: dict[str, Any] = {}
    resolved_operators: dict[str, OperatorSpec] = {}
    for slot in sorted(REQUIRED_SLOTS):
        normalized, resolved = _validate_operator(slot, operators_value[slot])
        normalized_operators[slot] = normalized
        resolved_operators[slot] = resolved

    canonical = {
        "schema_version": 1,
        "dataset": {"root": dataset_root, "snapshot_id": snapshot_id},
        "output_root": _validate_path(config["output_root"], "output_root"),
        "template": {
            "name": "single_stock_daily_causal",
            "version": "1",
            "parameters": template_parameters,
        },
        "operators": normalized_operators,
    }
    canonical_bytes = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return ValidatedStrategyConfig(
        canonical=canonical,
        canonical_bytes=canonical_bytes,
        config_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        template_parameters=template_parameters,
        operators=resolved_operators,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise StrategyConfigError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def load_strategy_config(path: Path | str) -> ValidatedStrategyConfig:
    try:
        payload = Path(path).read_text(encoding="utf-8")
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                StrategyConfigError(f"non-finite JSON number: {constant}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise StrategyConfigError(f"invalid JSON configuration: {exc}") from exc
    return validate_strategy_config(value)
