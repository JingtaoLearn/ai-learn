from __future__ import annotations

import ast
import json
import math
import sys
from pathlib import Path
from typing import Any

from .schemas import validate_parameters


ALLOWED_BUILTINS = {
    "abs": abs,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "range": range,
    "sum": sum,
}
FORBIDDEN_CALLS = {"compile", "eval", "exec", "globals", "locals", "open", "__import__"}


def _load_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON value: {value}")
        ),
    )


def _validate_source(source: str) -> Any:
    tree = ast.parse(source, filename="operator.py", mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("operator source cannot import modules")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                raise ValueError(f"operator source cannot call {node.func.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("operator source cannot access dunder attributes")
    return compile(tree, "operator.py", "exec", dont_inherit=True, optimize=2)


def _validate_values(values: Any) -> list[float]:
    if not isinstance(values, list) or not values:
        raise ValueError("fit values must be a non-empty array")
    normalized: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("fit values must contain only numbers")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("fit values must be finite and strictly positive")
        normalized.append(value)
    return normalized


def validate_candidate(candidate_dir: Path | str) -> dict[str, Any]:
    candidate = Path(candidate_dir)
    source_path = candidate / "operator.py"
    manifest = _load_json(candidate / "manifest.json")
    tests = _load_json(candidate / "tests.json")
    source = source_path.read_text(encoding="utf-8")
    code = _validate_source(source)
    namespace: dict[str, Any] = {"__builtins__": ALLOWED_BUILTINS}
    exec(code, namespace)
    if namespace.get("OPERATOR_API_VERSION") != 1:
        raise ValueError("operator API version must be integer 1")
    if namespace.get("SLOT") != "fit" or manifest.get("slot") != "fit":
        raise ValueError("only the custom fit operator contract is supported")
    apply = namespace.get("apply")
    if not callable(apply):
        raise ValueError("operator must export callable apply(values, parameters)")
    if not isinstance(tests, list) or not tests:
        raise ValueError("operator tests must be a non-empty array")
    for index, case in enumerate(tests):
        if not isinstance(case, dict) or set(case) != {
            "values",
            "parameters",
            "expected",
        }:
            raise ValueError(f"operator fixture {index} must have exact fields")
        values = _validate_values(case["values"])
        parameters = validate_parameters(
            manifest["parameter_schema"], case["parameters"]
        )
        expected = case["expected"]
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise ValueError(f"operator fixture {index} expected value must be numeric")
        expected = float(expected)
        if not math.isfinite(expected) or expected <= 0:
            raise ValueError(
                f"operator fixture {index} expected value must be finite and positive"
            )
        first = apply(list(values), dict(parameters))
        second = apply(list(values), dict(parameters))
        for result in (first, second):
            if isinstance(result, bool) or not isinstance(result, (int, float)):
                raise ValueError(f"operator fixture {index} returned a non-number")
            if not math.isfinite(result) or result <= 0:
                raise ValueError(
                    f"operator fixture {index} returned a non-finite or non-positive value"
                )
            if not math.isclose(float(result), expected, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"operator fixture {index} result did not match expected")
        if first != second:
            raise ValueError(f"operator fixture {index} is not deterministic")
    return {
        "api_version": 1,
        "compile": True,
        "contract": True,
        "fixtures": len(tests),
        "passed": True,
        "slot": "fit",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 2 or arguments[0] != "validate":
            raise ValueError("usage: operator_worker validate CANDIDATE_DIR")
        evidence = validate_candidate(arguments[1])
        output = Path("/evidence/evidence.json")
        output.write_text(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, ValueError, SyntaxError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
