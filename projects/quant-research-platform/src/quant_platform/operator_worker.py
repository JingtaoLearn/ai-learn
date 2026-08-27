from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .schemas import canonical_json_bytes, validate_parameters
from .submissions import EXECUTION_ENVELOPE, is_immutable_runner_image


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
SLOTS = {"fit", "smoothing", "statistic", "decision", "sizing", "cost", "report"}
COST_FIELDS = {
    "commission_cny",
    "transfer_fee_cny",
    "stamp_tax_cny",
    "slippage_cny",
    "total_cost_cny",
}


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


def _number(value: Any, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or (positive and normalized <= 0):
        qualifier = "finite and positive" if positive else "finite"
        raise ValueError(f"{path} must be {qualifier}")
    return normalized


def _number_list(
    value: Any, path: str, *, positive: bool = False, nullable: bool = False
) -> list[float | None]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{path} must be a non-empty array")
    result: list[float | None] = []
    for index, item in enumerate(value):
        if item is None and nullable:
            result.append(None)
        else:
            result.append(_number(item, f"{path}[{index}]", positive=positive))
    return result


def _exact(value: Any, fields: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{path} must have exact fields: {sorted(fields)}")
    return value


def _validate_input(slot: str, value: Any) -> dict[str, Any]:
    if slot in {"fit", "smoothing", "statistic"}:
        payload = _exact(value, {"values"}, f"{slot} input")
        _number_list(payload["values"], f"{slot}.values", positive=True)
    elif slot == "decision":
        payload = _exact(
            value, {"statistics", "initial_position"}, "decision input"
        )
        _number_list(payload["statistics"], "decision.statistics", nullable=True)
        if payload["initial_position"] not in {0, 1}:
            raise ValueError("decision.initial_position must be zero or one")
    elif slot == "sizing":
        payload = _exact(
            value, {"cash", "raw_price", "holdings", "side"}, "sizing input"
        )
        _number(payload["cash"], "sizing.cash")
        _number(payload["raw_price"], "sizing.raw_price", positive=True)
        if (
            isinstance(payload["holdings"], bool)
            or not isinstance(payload["holdings"], int)
            or payload["holdings"] < 0
        ):
            raise ValueError("sizing.holdings must be a non-negative integer")
        if payload["side"] not in {"BUY", "SELL"}:
            raise ValueError("sizing.side must be BUY or SELL")
    elif slot == "cost":
        payload = _exact(value, {"side", "raw_price", "quantity"}, "cost input")
        _number(payload["raw_price"], "cost.raw_price", positive=True)
        if payload["side"] not in {"BUY", "SELL"}:
            raise ValueError("cost.side must be BUY or SELL")
        if (
            isinstance(payload["quantity"], bool)
            or not isinstance(payload["quantity"], int)
            or payload["quantity"] < 0
        ):
            raise ValueError("cost.quantity must be a non-negative integer")
    elif slot == "report":
        payload = _exact(value, {"title", "metrics"}, "report input")
        if not isinstance(payload["title"], str) or not payload["title"]:
            raise ValueError("report.title must be non-empty")
        if not isinstance(payload["metrics"], dict):
            raise ValueError("report.metrics must be an object")
        canonical_json_bytes(payload["metrics"])
    else:
        raise ValueError(f"unsupported operator slot: {slot}")
    canonical_json_bytes(payload)
    return payload


def _validate_output(slot: str, value: Any, payload: dict[str, Any]) -> Any:
    if slot == "fit":
        return _number(value, "fit output", positive=True)
    if slot == "smoothing":
        output = _number_list(value, "smoothing output", positive=True)
        if len(output) != len(payload["values"]):
            raise ValueError("smoothing output length must match input")
        return output
    if slot == "statistic":
        output = _number_list(value, "statistic output", nullable=True)
        if len(output) != len(payload["values"]):
            raise ValueError("statistic output length must match input")
        return output
    if slot == "decision":
        if not isinstance(value, list) or len(value) != len(payload["statistics"]):
            raise ValueError("decision output length must match input")
        for index, decision in enumerate(value):
            decision = _exact(
                decision, {"action", "reason"}, f"decision output[{index}]"
            )
            if decision["action"] not in {"HOLD", "BUY", "SELL"}:
                raise ValueError("decision action is invalid")
            if not isinstance(decision["reason"], str) or not decision["reason"]:
                raise ValueError("decision reason must be non-empty")
        return value
    if slot == "sizing":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("sizing output must be a non-negative integer")
        return value
    if slot == "cost":
        output = _exact(value, COST_FIELDS, "cost output")
        for field in COST_FIELDS:
            if _number(output[field], f"cost output.{field}") < 0:
                raise ValueError("cost output values must be non-negative")
        component_total = sum(output[field] for field in COST_FIELDS - {"total_cost_cny"})
        if not math.isclose(
            component_total, output["total_cost_cny"], rel_tol=0, abs_tol=1e-9
        ):
            raise ValueError("cost output total does not reconcile")
        return output
    if slot == "report":
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1_000_000:
            raise ValueError("report output must be bounded non-empty HTML")
        lowered = value.lower()
        if "<script" in lowered or "http://" in lowered or "https://" in lowered:
            raise ValueError("report output cannot contain scripts or remote resources")
        return value
    raise ValueError(f"unsupported operator slot: {slot}")


def _runner_timestamp(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_candidate(
    candidate_dir: Path | str,
    *,
    validator_image: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    candidate = Path(candidate_dir)
    manifest = _load_json(candidate / "manifest.json")
    tests = _load_json(candidate / "tests.json")
    source = (candidate / "operator.py").read_text(encoding="utf-8")
    documentation = (candidate / "documentation.md").read_text(encoding="utf-8")
    if not is_immutable_runner_image(validator_image):
        raise ValueError("validator image must be pinned by SHA-256")
    required_manifest = {
        "operator_id",
        "slot",
        "version",
        "parameter_schema",
        "defaults",
        "title_zh",
        "summary_zh",
        "documentation",
        "content_digest",
    }
    _exact(manifest, required_manifest, "candidate manifest")
    if documentation != manifest["documentation"]:
        raise ValueError("candidate documentation binding mismatch")
    slot = manifest["slot"]
    if slot not in SLOTS:
        raise ValueError(f"unsupported operator slot: {slot}")
    reconstructed = {
        key: manifest[key] for key in required_manifest - {"content_digest"}
    } | {"source": source, "tests": tests}
    candidate_digest = hashlib.sha256(canonical_json_bytes(reconstructed)).hexdigest()
    if candidate_digest != manifest["content_digest"]:
        raise ValueError("candidate content digest mismatch")
    fixture_digest = hashlib.sha256(canonical_json_bytes(tests)).hexdigest()
    now = clock or (lambda: datetime.now(UTC))
    started_at = _runner_timestamp(now)
    code = _validate_source(source)
    namespace: dict[str, Any] = {"__builtins__": ALLOWED_BUILTINS}
    exec(code, namespace)
    if namespace.get("OPERATOR_API_VERSION") != 1:
        raise ValueError("operator API version must be integer 1")
    if namespace.get("SLOT") != slot:
        raise ValueError("operator SLOT does not match its manifest")
    apply = namespace.get("apply")
    if not callable(apply):
        raise ValueError("operator must export callable apply(payload, parameters)")
    if not isinstance(tests, list) or not tests:
        raise ValueError("operator tests must be a non-empty array")
    for index, case in enumerate(tests):
        case = _exact(case, {"input", "parameters", "expected"}, f"fixture {index}")
        payload = _validate_input(slot, case["input"])
        parameters = validate_parameters(
            manifest["parameter_schema"], case["parameters"]
        )
        expected = _validate_output(slot, case["expected"], payload)
        first = _validate_output(slot, apply(payload, dict(parameters)), payload)
        second = _validate_output(slot, apply(payload, dict(parameters)), payload)
        if canonical_json_bytes(first) != canonical_json_bytes(expected):
            raise ValueError(f"operator fixture {index} result did not match expected")
        if canonical_json_bytes(first) != canonical_json_bytes(second):
            raise ValueError(f"operator fixture {index} is not deterministic")
    return {
        "schema_version": 1,
        "passed": True,
        "slot": slot,
        "candidate_digest": candidate_digest,
        "fixture_digest": fixture_digest,
        "validator_image": validator_image,
        "execution_envelope": EXECUTION_ENVELOPE,
        "started_at": started_at,
        "finished_at": _runner_timestamp(now),
        "observations": {
            "api_version": 1,
            "compile": True,
            "contract": True,
            "fixtures": len(tests),
        },
    }


def load_published_operator(
    bundle_dir: Path | str,
) -> tuple[str, Callable[[dict[str, Any], dict[str, Any]], Any]]:
    bundle = Path(bundle_dir)
    manifest = _load_json(bundle / "manifest.json")
    tests = _load_json(bundle / "tests.json")
    evidence = _load_json(bundle / "evidence.json")
    source = (bundle / "operator.py").read_text(encoding="utf-8")
    documentation = (bundle / "documentation.md").read_text(encoding="utf-8")
    required_manifest = {
        "operator_id",
        "slot",
        "version",
        "parameter_schema",
        "defaults",
        "title_zh",
        "summary_zh",
        "documentation",
        "content_digest",
    }
    _exact(manifest, required_manifest, "published manifest")
    if documentation != manifest["documentation"]:
        raise ValueError("published documentation binding mismatch")
    reconstructed = {
        key: manifest[key] for key in required_manifest - {"content_digest"}
    } | {"source": source, "tests": tests}
    digest = hashlib.sha256(canonical_json_bytes(reconstructed)).hexdigest()
    fixture_digest = hashlib.sha256(canonical_json_bytes(tests)).hexdigest()
    if digest != manifest["content_digest"]:
        raise ValueError("published operator content digest mismatch")
    if (
        not isinstance(evidence, dict)
        or evidence.get("passed") is not True
        or evidence.get("candidate_digest") != digest
        or evidence.get("fixture_digest") != fixture_digest
        or evidence.get("slot") != manifest["slot"]
        or evidence.get("execution_envelope") != EXECUTION_ENVELOPE
    ):
        raise ValueError("published operator evidence binding mismatch")
    code = _validate_source(source)
    namespace: dict[str, Any] = {"__builtins__": ALLOWED_BUILTINS}
    exec(code, namespace)
    if (
        namespace.get("OPERATOR_API_VERSION") != 1
        or namespace.get("SLOT") != manifest["slot"]
        or not callable(namespace.get("apply"))
    ):
        raise ValueError("published operator runtime contract mismatch")
    apply = namespace["apply"]
    schema = manifest["parameter_schema"]
    slot = manifest["slot"]

    def invoke(payload: dict[str, Any], parameters: dict[str, Any]) -> Any:
        validated_payload = _validate_input(slot, payload)
        validated_parameters = validate_parameters(schema, parameters)
        return _validate_output(
            slot,
            apply(validated_payload, validated_parameters),
            validated_payload,
        )

    return slot, invoke


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 2 or arguments[0] != "validate":
            raise ValueError("usage: operator_worker validate CANDIDATE_DIR")
        validator_image = os.environ.get("QUANT_OPERATOR_VALIDATOR_IMAGE", "")
        evidence = validate_candidate(
            arguments[1], validator_image=validator_image
        )
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
