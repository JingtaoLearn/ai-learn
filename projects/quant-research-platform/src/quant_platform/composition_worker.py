from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .operator_worker import load_published_operator
from .strategy_runner import run_strategy_config


def execute_composition(
    composition_path: Path | str,
    config_path: Path | str,
    *,
    project_root: Path | str,
) -> dict[str, str]:
    composition = json.loads(Path(composition_path).read_text(encoding="utf-8"))
    if not isinstance(composition, dict) or set(composition) != {
        "schema_version",
        "composition_digest",
        "operators",
    }:
        raise ValueError("composition manifest fields are invalid")
    if composition["schema_version"] != 1:
        raise ValueError("composition schema version must be 1")
    implementations: dict[str, Any] = {}
    parameters: dict[str, dict[str, Any]] = {}
    for slot, descriptor in composition["operators"].items():
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "bundle_path",
            "parameters",
        }:
            raise ValueError(f"composition operator descriptor is invalid: {slot}")
        loaded_slot, implementation = load_published_operator(
            descriptor["bundle_path"]
        )
        if loaded_slot != slot:
            raise ValueError(f"composition operator slot mismatch: {slot}")
        implementations[slot] = implementation
        parameters[slot] = descriptor["parameters"]
    return run_strategy_config(
        config_path,
        project_root=project_root,
        implementations=implementations,
        implementation_parameters=parameters,
        composition_digest=composition["composition_digest"],
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 2:
            raise ValueError("usage: composition_worker COMPOSITION CONFIG")
        result = execute_composition(
            arguments[0], arguments[1], project_root=Path("/tmp/project")
        )
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
