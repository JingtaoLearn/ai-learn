from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any, Callable, Mapping

from .schemas import canonical_json_bytes
from .study_suggesters import (
    Exhausted,
    OptunaTPEParameterSuggester,
    Suggestion,
    optuna_tpe_frozen_identity,
)

TRAINING_JOB_TYPE = "optuna-tpe-synthetic-objective-v1"
TRAINING_KERNEL_NAME = "quant_platform.study_training_kernel.StudyTrainingKernel"
TRAINING_KERNEL_VERSION = "1.0.0"
OBJECTIVE_NAME = "synthetic-quadratic-maximum"
OBJECTIVE_VERSION = "1.0.0"
MAX_TRIAL_BUDGET = 256


class TrainingKernelValidationError(ValueError):
    """Raised when the frozen training contract cannot be executed exactly."""


def training_kernel_identity() -> dict[str, Any]:
    return {
        "name": TRAINING_KERNEL_NAME,
        "version": TRAINING_KERNEL_VERSION,
        "suggester": optuna_tpe_frozen_identity(),
    }


def freeze_training_spec(
    *,
    trial_budget: int,
    seed: int,
    parameter_low: float,
    parameter_high: float,
    objective_target: float,
) -> dict[str, Any]:
    if type(trial_budget) is not int or not 1 <= trial_budget <= MAX_TRIAL_BUDGET:
        raise TrainingKernelValidationError(
            f"trial_budget must be between 1 and {MAX_TRIAL_BUDGET}"
        )
    if type(seed) is not int or not 0 <= seed <= 9_007_199_254_740_991:
        raise TrainingKernelValidationError("seed must be a non-negative safe JSON integer")
    numeric = (parameter_low, parameter_high, objective_target)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
        raise TrainingKernelValidationError("training parameter bounds and target must be numbers")
    low, high, target = (float(value) for value in numeric)
    if not all(isfinite(value) for value in (low, high, target)):
        raise TrainingKernelValidationError("training parameter bounds and target must be finite")
    if low >= high:
        raise TrainingKernelValidationError("parameter_low must be less than parameter_high")
    if not low <= target <= high:
        raise TrainingKernelValidationError("objective_target must be inside the parameter range")
    return {
        "training_kernel": training_kernel_identity(),
        "objective": {
            "name": OBJECTIVE_NAME,
            "version": OBJECTIVE_VERSION,
            "target": target,
            "score": "negative_squared_error",
            "data_classification": "SYNTHETIC_NON_MARKET",
        },
        "search": {
            "parameter": "x",
            "low": low,
            "high": high,
            "seed": seed,
            "trial_budget": trial_budget,
        },
    }


def validate_training_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"training_kernel", "objective", "search"}:
        raise TrainingKernelValidationError("training_spec fields differ from the frozen contract")
    kernel = value.get("training_kernel")
    objective = value.get("objective")
    search = value.get("search")
    if not isinstance(kernel, Mapping) or canonical_json_bytes(kernel) != canonical_json_bytes(
        training_kernel_identity()
    ):
        raise TrainingKernelValidationError("training kernel identity is unsupported")
    if not isinstance(objective, Mapping) or set(objective) != {
        "name",
        "version",
        "target",
        "score",
        "data_classification",
    }:
        raise TrainingKernelValidationError("training objective identity is invalid")
    if (
        objective.get("name") != OBJECTIVE_NAME
        or objective.get("version") != OBJECTIVE_VERSION
        or objective.get("score") != "negative_squared_error"
        or objective.get("data_classification") != "SYNTHETIC_NON_MARKET"
    ):
        raise TrainingKernelValidationError("training objective identity is unsupported")
    if not isinstance(search, Mapping) or set(search) != {
        "parameter",
        "low",
        "high",
        "seed",
        "trial_budget",
    }:
        raise TrainingKernelValidationError("training search contract is invalid")
    if search.get("parameter") != "x":
        raise TrainingKernelValidationError("training search parameter is unsupported")
    return freeze_training_spec(
        trial_budget=search.get("trial_budget"),
        seed=search.get("seed"),
        parameter_low=search.get("low"),
        parameter_high=search.get("high"),
        objective_target=objective.get("target"),
    )


def _content_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _frozen_plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    objective = spec["objective"]
    search = spec["search"]
    baseline = (float(search["low"]) + float(search["high"])) / 2
    identity = {
        "training_kernel": spec["training_kernel"],
        "objective": objective,
        "search": search,
    }
    return {
        "schema_version": 1,
        "round_identity": f"training-kernel/{_content_digest(identity)}",
        "template": {
            "name": "synthetic_training_objective",
            "version": str(objective["version"]),
            "content_digest": _content_digest(objective),
            "parameters": {},
        },
        "operators": {
            "training": {
                "operator_id": str(objective["name"]),
                "slot": "training",
                "resolved_version": str(objective["version"]),
                "content_digest": _content_digest(objective),
                "parameter_schema": {
                    "type": "object",
                    "properties": {
                        "x": {
                            "type": "number",
                            "minimum": float(search["low"]),
                            "maximum": float(search["high"]),
                        }
                    },
                    "required": ["x"],
                    "additionalProperties": False,
                },
                "defaults": {"x": baseline},
                "parameters": {"x": baseline},
            }
        },
        "search": {
            "suggester": "OPTUNA_TPE",
            "suggester_version": "1.0.0",
            "adapter_identity": deepcopy(spec["training_kernel"]["suggester"]),
            "seed": int(search["seed"]),
            "unique_trial_budget": int(search["trial_budget"]),
            "max_suggestions": min(1_024, int(search["trial_budget"]) * 4),
            "space": {
                "/operators/training/x": {
                    "kind": "float",
                    "low": float(search["low"]),
                    "high": float(search["high"]),
                    "step": None,
                    "log": False,
                }
            },
        },
    }


@dataclass(frozen=True)
class TrainingKernelResult:
    progress: dict[str, Any]
    evidence: dict[str, Any]


class StudyTrainingKernel:
    """Run one frozen search in memory and expose only bounded aggregate evidence."""

    def run(
        self,
        frozen_spec: Mapping[str, Any],
        *,
        checkpoint_count: int,
        on_checkpoint: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> TrainingKernelResult:
        spec = validate_training_spec(frozen_spec)
        if type(checkpoint_count) is not int or not 1 <= checkpoint_count <= 20:
            raise TrainingKernelValidationError("checkpoint_count must be between 1 and 20")
        plan = _frozen_plan(spec)
        budget = int(spec["search"]["trial_budget"])
        thresholds = sorted(
            {max(1, budget * index // checkpoint_count) for index in range(1, checkpoint_count + 1)}
        )
        suggester = OptunaTPEParameterSuggester()
        history: list[dict[str, Any]] = []
        completed = 0
        suggestions = 0
        checkpoint_index = 0
        best_score = float("-inf")
        best_parameter = 0.0
        best_candidate_digest: str | None = None
        target = float(spec["objective"]["target"])

        while True:
            outcome = suggester.next_suggestion(plan, history)
            if isinstance(outcome, Exhausted):
                break
            if not isinstance(outcome, Suggestion):
                raise RuntimeError("training suggester returned an unsupported outcome")
            history.append(outcome.as_history_event())
            suggestions += 1
            if not outcome.creates_trial:
                continue
            parameter = float(outcome.candidate["operators"]["training"]["parameters"]["x"])
            score = -((parameter - target) ** 2)
            history.append(
                {
                    "event_type": "INNER_EVALUATION_RECORDED",
                    "round_identity": plan["round_identity"],
                    "role": "INNER_SCORE",
                    "candidate_digest": outcome.candidate_digest,
                    "evaluation": {"status": "COMPLETED", "validation_score": score},
                }
            )
            completed += 1
            if score > best_score:
                best_score = score
                best_parameter = parameter
                best_candidate_digest = outcome.candidate_digest
            if checkpoint_index < len(thresholds) and completed >= thresholds[checkpoint_index]:
                progress = {
                    "completed_trials": completed,
                    "total_trials": budget,
                    "suggestion_count": suggestions,
                    "checkpoint_sequence": checkpoint_index + 1,
                }
                checkpoint = {
                    "sequence": checkpoint_index + 1,
                    "completed_trials": completed,
                    "best_parameter": best_parameter,
                    "best_score": best_score,
                }
                on_checkpoint(progress, checkpoint)
                checkpoint_index += 1

        if completed != budget or best_candidate_digest is None:
            raise RuntimeError(
                f"training search ended after {completed} unique trials; expected {budget}"
            )
        progress = {
            "completed_trials": completed,
            "total_trials": budget,
            "suggestion_count": suggestions,
            "checkpoint_sequence": checkpoint_index,
        }
        return TrainingKernelResult(
            progress=progress,
            evidence={
                "conclusion": "SYNTHETIC_OBJECTIVE_SEARCH_COMPLETED",
                "data_classification": "SYNTHETIC_NON_MARKET",
                "training_kernel": deepcopy(spec["training_kernel"]),
                "objective": deepcopy(spec["objective"]),
                "completed_trials": completed,
                "suggestion_count": suggestions,
                "best_candidate_digest": best_candidate_digest,
                "best_parameter": best_parameter,
                "best_score": best_score,
                "checkpoint_count": checkpoint_index,
            },
        )
