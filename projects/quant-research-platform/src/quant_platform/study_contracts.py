from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pandas as pd


FOLD_WINDOW_FIELDS = {
    "allowed_start",
    "training_through",
    "available_through",
    "scoring_start",
    "scoring_end",
    "role",
    "information_interval",
    "account_policy",
}
FOLD_ROLES = {"INNER_SCORE", "OUTER_AUDIT", "TERMINAL_HOLDOUT"}
INFORMATION_INTERVAL = {
    "signal_time": "SESSION_CLOSE",
    "earliest_execution_time": "NEXT_SESSION_OPEN",
    "return_or_label_end_time": "EXECUTION_SESSION_CLOSE",
}


class FoldWindowValidationError(ValueError):
    """Raised when a FoldWindow violates the shared study contract."""


def _daily_session(value: Any, label: str) -> str:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise FoldWindowValidationError(f"{label} must be a valid date") from exc
    if pd.isna(parsed) or parsed.tz is not None or parsed != parsed.normalize():
        raise FoldWindowValidationError(
            f"{label} must be a timezone-naive daily session"
        )
    return str(parsed.date())


def normalize_fold_window(
    value: Mapping[str, Any], sessions: list[str]
) -> dict[str, Any]:
    """Return the canonical FoldWindow representation for ordered sessions."""

    if not isinstance(value, Mapping) or set(value) != FOLD_WINDOW_FIELDS:
        raise FoldWindowValidationError(
            f"fold_window fields must be exactly {sorted(FOLD_WINDOW_FIELDS)}"
        )
    normalized = {
        field: _daily_session(value[field], f"fold_window.{field}")
        for field in (
            "allowed_start",
            "training_through",
            "available_through",
            "scoring_start",
            "scoring_end",
        )
    }
    role = value["role"]
    if role not in FOLD_ROLES:
        raise FoldWindowValidationError(
            f"unsupported Fold Window role: {role!r}"
        )
    if value["account_policy"] != "FORCE_FLAT_WITH_COST":
        raise FoldWindowValidationError(
            "Fold Window account_policy must be FORCE_FLAT_WITH_COST"
        )
    if value["information_interval"] != INFORMATION_INTERVAL:
        raise FoldWindowValidationError(
            "Fold Window information_interval does not preserve causal execution timing"
        )
    normalized.update(
        {
            "role": role,
            "information_interval": deepcopy(INFORMATION_INTERVAL),
            "account_policy": "FORCE_FLAT_WITH_COST",
        }
    )
    missing = [
        normalized[field]
        for field in (
            "allowed_start",
            "training_through",
            "available_through",
            "scoring_start",
            "scoring_end",
        )
        if normalized[field] not in sessions
    ]
    if missing:
        raise FoldWindowValidationError(
            "Fold Window boundaries must be parent snapshot sessions: "
            + ", ".join(sorted(set(missing)))
        )
    if normalized["allowed_start"] != sessions[0]:
        raise FoldWindowValidationError(
            "Fold Window must retain the earliest parent history"
        )
    positions = {session: index for index, session in enumerate(sessions)}
    allowed = positions[normalized["allowed_start"]]
    training = positions[normalized["training_through"]]
    scoring_start = positions[normalized["scoring_start"]]
    scoring_end = positions[normalized["scoring_end"]]
    available = positions[normalized["available_through"]]
    if not allowed <= training < scoring_start <= scoring_end:
        raise FoldWindowValidationError(
            "Fold Window must expand from readable history through training and scoring"
        )
    if available != scoring_end:
        raise FoldWindowValidationError(
            "Fold Window available_through must equal scoring_end"
        )
    return normalized
