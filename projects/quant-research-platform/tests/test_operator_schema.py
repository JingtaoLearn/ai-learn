import math

import pytest

from quant_platform.schemas import (
    SchemaValidationError,
    canonical_json_bytes,
    parse_semantic_version,
    validate_defaults,
    validate_parameters,
    validate_parameter_schema,
)


def test_parameter_schema_and_defaults_are_exact_and_typed():
    schema = {
        "type": "object",
        "properties": {
            "window": {"type": "integer", "minimum": 2},
            "column": {"type": "string", "enum": ["Close", "AdjustedClose"]},
            "enabled": {"type": "boolean"},
        },
        "required": ["column", "enabled", "window"],
        "additionalProperties": False,
    }
    defaults = {"window": 20, "column": "AdjustedClose", "enabled": True}

    assert validate_parameter_schema(schema) == schema
    assert validate_defaults(schema, defaults) == defaults
    assert validate_parameters(schema, {"window": 5, "column": "Close", "enabled": False}) == {
        "window": 5,
        "column": "Close",
        "enabled": False,
    }


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "array"}, "fields"),
        (
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": True,
            },
            "additionalProperties",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "number", "pattern": "x"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "unknown",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "number"}},
                "required": [],
                "additionalProperties": False,
            },
            "required",
        ),
    ],
)
def test_parameter_schema_rejects_unsupported_or_inexact_shapes(schema, message):
    with pytest.raises(SchemaValidationError, match=message):
        validate_parameter_schema(schema)


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"window": True},
        {"window": 1},
        {"window": 3, "extra": 1},
        {"window": math.inf},
    ],
)
def test_parameters_fail_closed(parameters):
    schema = {
        "type": "object",
        "properties": {"window": {"type": "integer", "minimum": 2}},
        "required": ["window"],
        "additionalProperties": False,
    }
    with pytest.raises(SchemaValidationError):
        validate_parameters(schema, parameters)


@pytest.mark.parametrize("version", ["1", "1.0", "01.0.0", "1.0.0-alpha", "v1.0.0"])
def test_semantic_versions_require_canonical_release_triplets(version):
    with pytest.raises(SchemaValidationError, match="semantic version"):
        parse_semantic_version(version)


def test_semantic_version_ordering_is_numeric():
    assert parse_semantic_version("2.0.0") > parse_semantic_version("1.99.99")
    assert parse_semantic_version("1.10.0") > parse_semantic_version("1.9.9")


def test_canonical_json_rejects_non_finite_values():
    assert canonical_json_bytes({"b": 1, "a": "x"}) == b'{"a":"x","b":1}'
    with pytest.raises(SchemaValidationError, match="finite"):
        canonical_json_bytes({"value": math.nan})
