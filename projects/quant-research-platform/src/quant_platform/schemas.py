from __future__ import annotations

import json
import math
import re
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when the supported JSON-schema subset is violated."""


SEMANTIC_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PROPERTY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SCALAR_TYPES = {"string", "integer", "number", "boolean"}
SCHEMA_FIELDS = {"type", "properties", "required", "additionalProperties"}
PROPERTY_FIELDS = {"type", "enum", "minimum", "maximum", "nullable"}
MAX_WEB_SAFE_INTEGER = 9_007_199_254_740_991


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError("JSON values must be serializable and finite") from exc


def parse_semantic_version(version: str) -> tuple[int, int, int]:
    if not isinstance(version, str):
        raise SchemaValidationError("operator version must be a semantic version string")
    match = SEMANTIC_VERSION.fullmatch(version)
    if match is None:
        raise SchemaValidationError(
            "operator version must be a canonical semantic version release"
        )
    return tuple(int(part) for part in match.groups())


def _exact_fields(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{path} must be an object")
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise SchemaValidationError(f"{path} has missing fields: {missing}")
    if unknown:
        raise SchemaValidationError(f"{path} has unknown fields: {unknown}")
    return value


def _validate_property_schema(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{path} must be an object")
    unknown = sorted(set(value) - PROPERTY_FIELDS)
    if unknown:
        raise SchemaValidationError(f"{path} has unknown fields: {unknown}")
    if "type" not in value:
        raise SchemaValidationError(f"{path} has missing fields: ['type']")
    kind = value["type"]
    if kind not in SCALAR_TYPES:
        raise SchemaValidationError(f"{path}.type is unsupported")
    nullable = value.get("nullable", False)
    if type(nullable) is not bool:
        raise SchemaValidationError(f"{path}.nullable must be boolean")
    if "enum" in value:
        choices = value["enum"]
        if not isinstance(choices, list) or not choices:
            raise SchemaValidationError(f"{path}.enum must be a non-empty array")
        for index, choice in enumerate(choices):
            _validate_scalar(choice, value, f"{path}.enum[{index}]", enforce_constraints=False)
        if len({canonical_json_bytes(choice) for choice in choices}) != len(choices):
            raise SchemaValidationError(f"{path}.enum values must be unique")
    for bound in ("minimum", "maximum"):
        if bound in value:
            if kind not in {"integer", "number"}:
                raise SchemaValidationError(f"{path}.{bound} requires a numeric type")
            bound_value = value[bound]
            if isinstance(bound_value, bool) or not isinstance(bound_value, (int, float)):
                raise SchemaValidationError(f"{path}.{bound} must be numeric")
            if not math.isfinite(bound_value):
                raise SchemaValidationError(f"{path}.{bound} must be finite")
            if isinstance(bound_value, float) and bound_value == 0 and math.copysign(
                1.0, bound_value
            ) < 0:
                raise SchemaValidationError(f"{path}.{bound} cannot be negative zero")
            if kind == "integer" and abs(bound_value) > MAX_WEB_SAFE_INTEGER:
                raise SchemaValidationError(
                    f"{path}.{bound} must be within the web-safe integer range"
                )
    if (
        "minimum" in value
        and "maximum" in value
        and value["minimum"] > value["maximum"]
    ):
        raise SchemaValidationError(f"{path} minimum cannot exceed maximum")
    return value


def validate_parameter_schema(value: Any) -> dict[str, Any]:
    schema = _exact_fields(value, SCHEMA_FIELDS, "parameter_schema")
    if schema["type"] != "object":
        raise SchemaValidationError("parameter_schema.type must be object")
    if schema["additionalProperties"] is not False:
        raise SchemaValidationError("parameter_schema.additionalProperties must be false")
    properties = schema["properties"]
    required = schema["required"]
    if not isinstance(properties, dict):
        raise SchemaValidationError("parameter_schema.properties must be an object")
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise SchemaValidationError("parameter_schema.required must be a string array")
    if len(set(required)) != len(required):
        raise SchemaValidationError("parameter_schema.required names must be unique")
    if set(required) != set(properties):
        raise SchemaValidationError("parameter_schema.required must name every property exactly")
    for name, property_schema in properties.items():
        if not isinstance(name, str) or PROPERTY_NAME.fullmatch(name) is None:
            raise SchemaValidationError(f"invalid parameter property name: {name!r}")
        _validate_property_schema(property_schema, f"parameter_schema.properties.{name}")
    canonical_json_bytes(schema)
    return schema


def _validate_scalar(
    value: Any,
    schema: dict[str, Any],
    path: str,
    *,
    enforce_constraints: bool = True,
) -> Any:
    if value is None:
        if schema.get("nullable") is True:
            return None
        raise SchemaValidationError(f"{path} cannot be null")
    kind = schema["type"]
    if kind == "string":
        if not isinstance(value, str):
            raise SchemaValidationError(f"{path} must be a string")
    elif kind == "boolean":
        if type(value) is not bool:
            raise SchemaValidationError(f"{path} must be a boolean")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaValidationError(f"{path} must be an integer")
        if abs(value) > MAX_WEB_SAFE_INTEGER:
            raise SchemaValidationError(
                f"{path} must be within the web-safe integer range"
            )
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaValidationError(f"{path} must be a number")
        if not math.isfinite(value):
            raise SchemaValidationError(f"{path} must be finite")
        if isinstance(value, float) and value == 0 and math.copysign(1.0, value) < 0:
            raise SchemaValidationError(f"{path} cannot be negative zero")
        value = float(value)
    else:
        raise SchemaValidationError(f"{path} has unsupported type")
    if enforce_constraints:
        if "enum" in schema and value not in schema["enum"]:
            raise SchemaValidationError(f"{path} must be one of {schema['enum']}")
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(f"{path} must be at most {schema['maximum']}")
    return value


def validate_parameters(schema: Any, value: Any) -> dict[str, Any]:
    schema = validate_parameter_schema(schema)
    if not isinstance(value, dict):
        raise SchemaValidationError("parameters must be an object")
    expected = set(schema["properties"])
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise SchemaValidationError(f"parameters has missing fields: {missing}")
    if unknown:
        raise SchemaValidationError(f"parameters has unknown fields: {unknown}")
    normalized = {
        name: _validate_scalar(
            value[name], property_schema, f"parameters.{name}"
        )
        for name, property_schema in schema["properties"].items()
    }
    canonical_json_bytes(normalized)
    return normalized


def validate_defaults(schema: Any, defaults: Any) -> dict[str, Any]:
    try:
        return validate_parameters(schema, defaults)
    except SchemaValidationError as exc:
        raise SchemaValidationError(f"invalid defaults: {exc}") from exc
