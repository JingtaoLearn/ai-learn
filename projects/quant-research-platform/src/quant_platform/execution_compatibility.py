"""Outcome-blind compatibility checks for strategy execution inputs."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SUPPORTED_DATASET_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5})
ACTION_AWARE_DATASET_SCHEMA_VERSIONS = frozenset({4, 5})


class ExecutionInputCompatibilityError(ValueError):
    """Raised when metadata proves execution inputs are incompatible."""


def require_execution_input_compatibility(
    dataset_manifest: Mapping[str, Any],
    *,
    settlement_schedule_present: bool,
) -> None:
    """Reject deterministic schedule/action-evidence mismatches from metadata only."""
    schema_version = dataset_manifest.get("schema_version")
    if type(schema_version) is not int or schema_version not in SUPPORTED_DATASET_SCHEMA_VERSIONS:
        raise ExecutionInputCompatibilityError("unsupported snapshot schema")

    action_aware = schema_version in ACTION_AWARE_DATASET_SCHEMA_VERSIONS
    if action_aware and not settlement_schedule_present:
        raise ExecutionInputCompatibilityError(
            "action-aware dataset requires an explicit transfer-settlement mapping"
        )
    if settlement_schedule_present and not action_aware:
        raise ExecutionInputCompatibilityError(
            "settlement schedule cannot be applied without admitted corporate-action evidence"
        )
