from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from math import prod
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeVar, runtime_checkable

import optuna
from optuna.distributions import (
    BaseDistribution,
    CategoricalDistribution,
    FloatDistribution,
    IntDistribution,
)
from optuna.trial import Trial, TrialState

from .schemas import (
    SchemaValidationError,
    canonical_json_bytes,
    validate_parameter_schema,
    validate_parameters,
)

# Operational limits for a serial, single-node daily research platform.
MAX_SEARCH_DIMENSIONS = 12
MAX_VALUES_PER_DIMENSION = 128
MAX_CANDIDATE_CAPACITY = 16_384
MAX_UNIQUE_TRIAL_BUDGET = 256
MAX_SUGGESTIONS = 1_024
MAX_ORDERED_HISTORY_LENGTH = 2_048
MAX_SEED = 9_007_199_254_740_991
MAX_JSON_NESTING_DEPTH = 32
MAX_JSON_STRING_BYTES = 65_536
MAX_JSON_CONTAINER_SIZE = 16_384
MAX_JSON_TOTAL_VALUES = 65_536
MAX_CANDIDATE_CANONICAL_BYTES = 1_048_576
MAX_EVENT_CANONICAL_BYTES = 1_048_576

OPTUNA_TPE_ADAPTER_VERSION = "1.0.0"
OPTUNA_TPE_LIBRARY_VERSION = "4.9.0"

_OPTUNA_TPE_SAMPLER_SETTINGS: dict[str, Any] = {
    "consider_prior": None,
    "prior_weight": None,
    "consider_magic_clip": None,
    "consider_endpoints": None,
    "n_startup_trials": 5,
    "n_ei_candidates": 24,
    "gamma": None,
    "weights": None,
    "multivariate": False,
    "group": False,
    "warn_independent_sampling": None,
    "constant_liar": False,
    "constraints_func": None,
    "categorical_distance_func": None,
}


def optuna_tpe_frozen_identity() -> dict[str, Any]:
    """Return the exact adapter, library, sampler, and objective identity."""
    return {
        "adapter": "quant_platform.study_suggesters.OptunaTPEParameterSuggester",
        "adapter_version": OPTUNA_TPE_ADAPTER_VERSION,
        "library": "optuna",
        "library_version": OPTUNA_TPE_LIBRARY_VERSION,
        "sampler": "TPESampler",
        "sampler_settings": deepcopy(_OPTUNA_TPE_SAMPLER_SETTINGS),
        "direction": "MAXIMIZE",
        "objective": "validation_score",
    }


class SuggesterValidationError(ValueError):
    """Raised when a frozen plan or suggester history violates the contract."""


class SuggesterHistoryLeakageError(SuggesterValidationError):
    """Raised when non-inner evaluation evidence reaches a suggester."""


class SuggestionClassification(StrEnum):
    IN_RANGE = "IN_RANGE"
    BASELINE_ONLY = "BASELINE_ONLY"


class SuggestionDisposition(StrEnum):
    UNIQUE = "UNIQUE"
    DUPLICATE = "DUPLICATE"


class ExhaustionReason(StrEnum):
    UNIQUE_TRIAL_BUDGET = "UNIQUE_TRIAL_BUDGET"
    RAW_SUGGESTION_BUDGET = "RAW_SUGGESTION_BUDGET"
    SEARCH_SPACE_EXHAUSTED = "SEARCH_SPACE_EXHAUSTED"


class HistoryEventType(StrEnum):
    SUGGESTION_RECORDED = "SUGGESTION_RECORDED"
    DUPLICATE_SUGGESTION = "DUPLICATE_SUGGESTION"
    INNER_EVALUATION_RECORDED = "INNER_EVALUATION_RECORDED"
    OUTER_EVALUATION_RECORDED = "OUTER_EVALUATION_RECORDED"
    HOLDOUT_EVALUATION_RECORDED = "HOLDOUT_EVALUATION_RECORDED"


class EvaluationRole(StrEnum):
    INNER_SCORE = "INNER_SCORE"
    OUTER_AUDIT = "OUTER_AUDIT"
    OUTER_OOS = "OUTER_OOS"
    TERMINAL_HOLDOUT = "TERMINAL_HOLDOUT"
    HOLDOUT = "HOLDOUT"


_EnumType = TypeVar("_EnumType", bound=StrEnum)


def _domain_value(value: Any, enum_type: type[_EnumType], path: str) -> _EnumType:
    if not isinstance(value, str):
        raise SuggesterValidationError(f"{path} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise SuggesterValidationError(f"{path} must be one of: {allowed}") from exc


@dataclass(frozen=True, init=False)
class Suggestion:
    proposal_sequence: int
    candidate_digest: str
    _candidate_bytes: bytes = field(repr=False)
    classification: SuggestionClassification
    disposition: SuggestionDisposition
    duplicate_of_sequence: int | None

    def __init__(
        self,
        proposal_sequence: int,
        candidate_digest: str,
        candidate: Mapping[str, Any],
        classification: SuggestionClassification | str,
        disposition: SuggestionDisposition | str,
        duplicate_of_sequence: int | None,
    ) -> None:
        sequence = _bounded_nonnegative_integer(
            proposal_sequence,
            "suggestion.proposal_sequence",
            MAX_SUGGESTIONS - 1,
        )
        if (
            not isinstance(candidate_digest, str)
            or len(candidate_digest) != 64
            or any(character not in "0123456789abcdef" for character in candidate_digest)
        ):
            raise SuggesterValidationError(
                "suggestion.candidate_digest must be a lowercase SHA-256 digest"
            )
        candidate_bytes = _canonical_json_bytes(
            candidate,
            "suggestion.candidate",
            MAX_CANDIDATE_CANONICAL_BYTES,
        )
        if _candidate_digest(candidate_bytes) != candidate_digest:
            raise SuggesterValidationError(
                "suggestion candidate digest does not match its canonical JSON"
            )
        normalized_classification = _domain_value(
            classification,
            SuggestionClassification,
            "suggestion.classification",
        )
        normalized_disposition = _domain_value(
            disposition,
            SuggestionDisposition,
            "suggestion.disposition",
        )
        if normalized_disposition is SuggestionDisposition.DUPLICATE:
            if (
                isinstance(duplicate_of_sequence, bool)
                or not isinstance(duplicate_of_sequence, int)
                or duplicate_of_sequence < 0
                or duplicate_of_sequence >= sequence
            ):
                raise SuggesterValidationError(
                    "suggestion.duplicate_of_sequence must identify an earlier proposal "
                    "for duplicate disposition"
                )
        elif duplicate_of_sequence is not None:
            raise SuggesterValidationError(
                "suggestion.duplicate_of_sequence is only valid for duplicate disposition"
            )
        object.__setattr__(self, "proposal_sequence", sequence)
        object.__setattr__(self, "candidate_digest", candidate_digest)
        object.__setattr__(self, "_candidate_bytes", candidate_bytes)
        object.__setattr__(self, "classification", normalized_classification)
        object.__setattr__(self, "disposition", normalized_disposition)
        object.__setattr__(self, "duplicate_of_sequence", duplicate_of_sequence)

    @property
    def candidate(self) -> dict[str, Any]:
        """Return a defensive copy decoded from immutable canonical bytes."""
        candidate = json.loads(self._candidate_bytes)
        if not isinstance(candidate, dict):
            raise SuggesterValidationError("suggestion candidate must be an object")
        return candidate

    @property
    def creates_trial(self) -> bool:
        return self.disposition is SuggestionDisposition.UNIQUE

    @property
    def champion_eligible(self) -> bool:
        return self.classification is SuggestionClassification.IN_RANGE and self.creates_trial

    def as_history_event(self) -> dict[str, Any]:
        return {
            "event_type": (
                HistoryEventType.DUPLICATE_SUGGESTION
                if self.disposition is SuggestionDisposition.DUPLICATE
                else HistoryEventType.SUGGESTION_RECORDED
            ),
            "proposal_sequence": self.proposal_sequence,
            "candidate_digest": self.candidate_digest,
            "candidate": self.candidate,
            "classification": self.classification,
            "disposition": self.disposition,
            "duplicate_of_sequence": self.duplicate_of_sequence,
        }


@dataclass(frozen=True, init=False)
class Exhausted:
    reason: ExhaustionReason
    raw_suggestion_count: int
    unique_trial_count: int

    def __init__(
        self,
        reason: ExhaustionReason | str,
        raw_suggestion_count: int,
        unique_trial_count: int,
    ) -> None:
        normalized_reason = _domain_value(reason, ExhaustionReason, "exhausted.reason")
        raw_count = _bounded_nonnegative_integer(
            raw_suggestion_count,
            "exhausted.raw_suggestion_count",
            MAX_SUGGESTIONS,
        )
        unique_count = _bounded_nonnegative_integer(
            unique_trial_count,
            "exhausted.unique_trial_count",
            MAX_UNIQUE_TRIAL_BUDGET,
        )
        if unique_count > raw_count:
            raise SuggesterValidationError(
                "exhausted.unique_trial_count cannot exceed raw_suggestion_count"
            )
        object.__setattr__(self, "reason", normalized_reason)
        object.__setattr__(self, "raw_suggestion_count", raw_count)
        object.__setattr__(self, "unique_trial_count", unique_count)


@runtime_checkable
class ParameterSuggester(Protocol):
    def next_suggestion(
        self,
        frozen_plan: Mapping[str, Any],
        ordered_history: Sequence[Suggestion | Mapping[str, Any]],
    ) -> Suggestion | Exhausted:
        """Return the next auditable proposal outcome without storing state."""


@dataclass(frozen=True)
class _SearchDimension:
    path: str
    slot: str
    parameter: str
    values: tuple[Any, ...]
    decimal_start: Decimal | None = None
    decimal_step: Decimal | None = None
    optuna_distribution: BaseDistribution | None = field(default=None, repr=False)
    cardinality: int | None = None

    def value_at(self, index: int) -> Any:
        if self.decimal_start is None or self.decimal_step is None:
            return self.values[index]
        value = float(self.decimal_start + self.decimal_step * index)
        if canonical_json_bytes(value) != canonical_json_bytes(self.values[index]):
            raise SuggesterValidationError(
                f"indexed decimal generation drifted at {self.path}[{index}]"
            )
        return value

    def contains(self, value: Any) -> bool:
        if self.optuna_distribution is None:
            return canonical_json_bytes(value) in {
                canonical_json_bytes(candidate) for candidate in self.values
            }
        distribution = self.optuna_distribution
        if isinstance(distribution, CategoricalDistribution):
            return canonical_json_bytes(value) in {
                canonical_json_bytes(choice) for choice in distribution.choices
            }
        if isinstance(distribution, IntDistribution):
            return (
                type(value) is int
                and distribution.low <= value <= distribution.high
                and (value - distribution.low) % distribution.step == 0
            )
        if isinstance(distribution, FloatDistribution):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            numeric = float(value)
            if not isfinite(numeric) or not distribution.low <= numeric <= distribution.high:
                return False
            if distribution.step is None:
                return True
            offset = Decimal(str(numeric)) - Decimal(str(distribution.low))
            return offset % Decimal(str(distribution.step)) == 0
        raise SuggesterValidationError(f"unsupported Optuna distribution at {self.path}")


@dataclass(frozen=True)
class _CompiledPlan:
    baseline_candidate: dict[str, Any]
    dimensions: tuple[_SearchDimension, ...]
    seed: int
    round_identity: str
    unique_trial_budget: int
    max_suggestions: int

    @property
    def candidate_capacity(self) -> int | None:
        cardinalities = tuple(dimension.cardinality for dimension in self.dimensions)
        if any(cardinality is None for cardinality in cardinalities):
            return None
        return prod(int(cardinality) for cardinality in cardinalities)


@dataclass(frozen=True)
class _ObservedCandidate:
    canonical_bytes: bytes
    first_sequence: int


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SuggesterValidationError(f"{path} must be an object")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SuggesterValidationError(f"{path} must be a non-empty trimmed string")
    return value


def _bounded_positive_integer(value: Any, path: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise SuggesterValidationError(f"{path} must be an integer from 1 to {maximum}")
    return value


def _bounded_nonnegative_integer(value: Any, path: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise SuggesterValidationError(f"{path} must be an integer from 0 to {maximum}")
    return value


def _require_json_value(
    value: Any,
    path: str,
    maximum_canonical_bytes: int | None = None,
) -> None:
    pending: list[tuple[Any, str, int]] = [(value, path, 0)]
    value_count = 0
    canonical_size = 0

    def account_canonical_bytes(size: int) -> None:
        nonlocal canonical_size
        canonical_size += size
        if maximum_canonical_bytes is not None and canonical_size > maximum_canonical_bytes:
            raise SuggesterValidationError(
                f"{path} exceeds maximum canonical size {maximum_canonical_bytes} bytes"
            )

    def utf8_size(text: str, text_path: str) -> int:
        try:
            return len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise SuggesterValidationError(f"{text_path} is not canonical JSON") from exc

    def canonical_size_of(current: Any, current_path: str) -> int:
        try:
            return len(canonical_json_bytes(current))
        except (RecursionError, UnicodeEncodeError, SchemaValidationError) as exc:
            raise SuggesterValidationError(f"{current_path} is not canonical JSON") from exc

    while pending:
        current, current_path, depth = pending.pop()
        value_count += 1
        if value_count > MAX_JSON_TOTAL_VALUES:
            raise SuggesterValidationError(
                f"{path} exceeds maximum total JSON value count {MAX_JSON_TOTAL_VALUES}"
            )
        if depth > MAX_JSON_NESTING_DEPTH:
            raise SuggesterValidationError(
                f"{current_path} exceeds maximum JSON nesting depth {MAX_JSON_NESTING_DEPTH}"
            )
        if current is None or isinstance(current, bool):
            account_canonical_bytes(canonical_size_of(current, current_path))
            continue
        if isinstance(current, str):
            if utf8_size(current, current_path) > MAX_JSON_STRING_BYTES:
                raise SuggesterValidationError(
                    f"{current_path} exceeds maximum JSON string size {MAX_JSON_STRING_BYTES} bytes"
                )
            account_canonical_bytes(canonical_size_of(current, current_path))
            continue
        if isinstance(current, int):
            account_canonical_bytes(canonical_size_of(current, current_path))
            continue
        if isinstance(current, float):
            if not isfinite(current):
                raise SuggesterValidationError(f"{current_path} must be a finite JSON number")
            account_canonical_bytes(canonical_size_of(current, current_path))
            continue
        if isinstance(current, list):
            if len(current) > MAX_JSON_CONTAINER_SIZE:
                raise SuggesterValidationError(
                    f"{current_path} exceeds maximum JSON container size {MAX_JSON_CONTAINER_SIZE}"
                )
            account_canonical_bytes(2 + max(0, len(current) - 1))
            pending.extend(
                (item, f"{current_path}[{index}]", depth + 1) for index, item in enumerate(current)
            )
            continue
        if isinstance(current, dict):
            if len(current) > MAX_JSON_CONTAINER_SIZE:
                raise SuggesterValidationError(
                    f"{current_path} exceeds maximum JSON container size {MAX_JSON_CONTAINER_SIZE}"
                )
            for key, item in current.items():
                if not isinstance(key, str):
                    raise SuggesterValidationError(
                        f"{current_path} contains a non-string object key"
                    )
                key_path = f"{current_path} object key"
                if utf8_size(key, key_path) > MAX_JSON_STRING_BYTES:
                    raise SuggesterValidationError(
                        f"{current_path} contains an object key exceeding maximum "
                        f"JSON string size {MAX_JSON_STRING_BYTES} bytes"
                    )
                account_canonical_bytes(canonical_size_of(key, key_path) + 1)
                pending.append((item, f"{current_path}.{key}", depth + 1))
            account_canonical_bytes(2 + max(0, len(current) - 1))
            continue
        raise SuggesterValidationError(
            f"{current_path} contains non-JSON type {type(current).__name__}"
        )


def _canonical_json_bytes(value: Any, path: str, maximum_bytes: int) -> bytes:
    _require_json_value(value, path, maximum_bytes)
    try:
        encoded = canonical_json_bytes(value)
    except (RecursionError, UnicodeEncodeError, SchemaValidationError) as exc:
        raise SuggesterValidationError(f"{path} is not canonical JSON") from exc
    if len(encoded) > maximum_bytes:
        raise SuggesterValidationError(
            f"{path} exceeds maximum canonical size {maximum_bytes} bytes"
        )
    return encoded


def _candidate_digest(candidate_bytes: bytes) -> str:
    return hashlib.sha256(candidate_bytes).hexdigest()


def _require_string_keys(value: Mapping[Any, Any], path: str) -> None:
    if any(not isinstance(key, str) or not key for key in value):
        raise SuggesterValidationError(f"{path} keys must be non-empty strings")


def _schema_accepts_number(property_schema: Mapping[str, Any]) -> bool:
    declared_type = property_schema.get("type")
    return declared_type == "number" or (
        isinstance(declared_type, list) and "number" in declared_type
    )


def _normalize_scalar(
    property_schema: Mapping[str, Any],
    value: Any,
    path: str,
) -> Any:
    scalar_schema = {
        "type": "object",
        "properties": {"value": dict(property_schema)},
        "required": ["value"],
        "additionalProperties": False,
    }
    try:
        return validate_parameters(scalar_schema, {"value": value})["value"]
    except SchemaValidationError as exc:
        raise SuggesterValidationError(f"{path} is invalid: {exc}") from exc


def _compile_values_dimension(
    *,
    path: str,
    slot: str,
    parameter: str,
    definition: Mapping[str, Any],
    property_schema: Mapping[str, Any],
) -> _SearchDimension:
    if set(definition) != {"values"}:
        raise SuggesterValidationError(
            f"frozen_plan.search.space.{path} must contain only values"
        )
    raw_values = definition["values"]
    if not isinstance(raw_values, list) or not raw_values:
        raise SuggesterValidationError(
            f"frozen_plan.search.space.{path}.values must be a non-empty array"
        )
    if len(raw_values) > MAX_VALUES_PER_DIMENSION:
        raise SuggesterValidationError(
            f"frozen_plan.search.space.{path}.values must contain at most "
            f"{MAX_VALUES_PER_DIMENSION} values"
        )
    values = tuple(
        _normalize_scalar(
            property_schema,
            value,
            f"frozen_plan.search.space.{path}.values[{index}]",
        )
        for index, value in enumerate(raw_values)
    )
    identities = [canonical_json_bytes(value) for value in values]
    if len(set(identities)) != len(identities):
        raise SuggesterValidationError(f"frozen_plan.search.space.{path}.values must be unique")
    decimal_start: Decimal | None = None
    decimal_step: Decimal | None = None
    if (
        _schema_accepts_number(property_schema)
        and len(values) > 1
        and all(value is not None for value in values)
    ):
        decimal_values = tuple(Decimal(str(value)) for value in values)
        candidate_step = decimal_values[1] - decimal_values[0]
        if candidate_step and all(
            value == decimal_values[0] + candidate_step * index
            for index, value in enumerate(decimal_values)
        ):
            decimal_start = decimal_values[0]
            decimal_step = candidate_step
    return _SearchDimension(
        path=path,
        slot=slot,
        parameter=parameter,
        values=values,
        decimal_start=decimal_start,
        decimal_step=decimal_step,
        cardinality=len(values),
    )


def _typed_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise SuggesterValidationError(f"{path} must be a finite number")
    return float(value)


def _compile_optuna_dimension(
    *,
    path: str,
    slot: str,
    parameter: str,
    definition: Mapping[str, Any],
    property_schema: Mapping[str, Any],
) -> _SearchDimension:
    definition_path = f"frozen_plan.search.space.{path}"
    distribution_type = definition.get("kind")
    if distribution_type == "categorical":
        if set(definition) != {"kind", "choices"}:
            raise SuggesterValidationError(
                f"{definition_path} categorical distribution must contain only kind and choices"
            )
        choices = definition.get("choices")
        if not isinstance(choices, list) or not choices:
            raise SuggesterValidationError(f"{definition_path}.choices must be a non-empty array")
        if len(choices) > MAX_VALUES_PER_DIMENSION:
            raise SuggesterValidationError(
                f"{definition_path}.choices must contain at most {MAX_VALUES_PER_DIMENSION} values"
            )
        normalized = tuple(
            _normalize_scalar(property_schema, choice, f"{definition_path}.choices[{index}]")
            for index, choice in enumerate(choices)
        )
        if any(isinstance(choice, (dict, list)) for choice in normalized):
            raise SuggesterValidationError(f"{definition_path}.choices must contain JSON scalars")
        identities = [canonical_json_bytes(choice) for choice in normalized]
        if len(set(identities)) != len(identities):
            raise SuggesterValidationError(f"{definition_path}.choices must be unique")
        return _SearchDimension(
            path=path,
            slot=slot,
            parameter=parameter,
            values=normalized,
            optuna_distribution=CategoricalDistribution(normalized),
            cardinality=len(normalized),
        )

    required_fields = {"kind", "low", "high", "log"}
    allowed_fields = {*required_fields, "step"}
    if distribution_type not in {"int", "float"}:
        raise SuggesterValidationError(
            f"{definition_path}.kind must be categorical, int, or float"
        )
    if not required_fields.issubset(definition) or not set(definition).issubset(
        allowed_fields
    ):
        raise SuggesterValidationError(
            f"{definition_path} {distribution_type} distribution must contain "
            "kind, low, high, and log, with optional step"
        )
    log = definition.get("log")
    if not isinstance(log, bool):
        raise SuggesterValidationError(f"{definition_path}.log must be a boolean")
    raw_step = definition.get("step")
    if log and raw_step is not None:
        raise SuggesterValidationError(f"{definition_path}.step and log are mutually exclusive")

    if "enum" in property_schema:
        raise SuggesterValidationError(
            f"{definition_path} enum parameters require a categorical distribution"
        )
    if distribution_type == "int":
        declared_type = property_schema.get("type")
        if declared_type != "integer":
            raise SuggesterValidationError(
                f"{definition_path} int distribution requires an integer parameter"
            )
        low = _normalize_scalar(property_schema, definition.get("low"), f"{definition_path}.low")
        high = _normalize_scalar(property_schema, definition.get("high"), f"{definition_path}.high")
        if type(low) is not int or type(high) is not int:
            raise SuggesterValidationError(f"{definition_path} bounds must be integers")
        if low > high:
            raise SuggesterValidationError(f"{definition_path}.low must not exceed high")
        if raw_step is None:
            step = 1
        elif type(raw_step) is not int or raw_step <= 0:
            raise SuggesterValidationError(f"{definition_path}.step must be a positive integer or null")
        else:
            step = raw_step
        if (high - low) % step:
            raise SuggesterValidationError(
                f"{definition_path} range must be exactly divisible by step"
            )
        try:
            distribution: BaseDistribution = IntDistribution(
                low=low,
                high=high,
                step=step,
                log=log,
            )
        except ValueError as exc:
            raise SuggesterValidationError(f"{definition_path} is invalid: {exc}") from exc
        cardinality: int | None = (high - low) // step + 1
    else:
        if not _schema_accepts_number(property_schema):
            raise SuggesterValidationError(
                f"{definition_path} float distribution requires a number parameter"
            )
        low = _typed_number(definition.get("low"), f"{definition_path}.low")
        high = _typed_number(definition.get("high"), f"{definition_path}.high")
        _normalize_scalar(property_schema, low, f"{definition_path}.low")
        _normalize_scalar(property_schema, high, f"{definition_path}.high")
        if low > high:
            raise SuggesterValidationError(f"{definition_path}.low must not exceed high")
        if raw_step is None:
            step = None
            cardinality = None
        else:
            step = _typed_number(raw_step, f"{definition_path}.step")
            if step <= 0:
                raise SuggesterValidationError(f"{definition_path}.step must be positive or null")
            span = Decimal(str(high)) - Decimal(str(low))
            decimal_step = Decimal(str(step))
            if span % decimal_step:
                raise SuggesterValidationError(
                    f"{definition_path} range must be exactly divisible by step"
                )
            cardinality = int(span / decimal_step) + 1
        try:
            distribution = FloatDistribution(low=low, high=high, step=step, log=log)
        except ValueError as exc:
            raise SuggesterValidationError(f"{definition_path} is invalid: {exc}") from exc
    return _SearchDimension(
        path=path,
        slot=slot,
        parameter=parameter,
        values=(),
        optuna_distribution=distribution,
        cardinality=cardinality,
    )


def normalize_optuna_search_definition(
    definition: Mapping[str, Any],
    property_schema: Mapping[str, Any],
    path: str,
) -> tuple[dict[str, Any], int | None]:
    """Validate and canonicalize one public Optuna search distribution."""
    dimension = _compile_optuna_dimension(
        path=path,
        slot="validation",
        parameter="value",
        definition=definition,
        property_schema=property_schema,
    )
    distribution = dimension.optuna_distribution
    if distribution is None:
        raise SuggesterValidationError(
            f"frozen_plan.search.space.{path} has no Optuna distribution"
        )
    if isinstance(distribution, CategoricalDistribution):
        normalized = {
            "kind": "categorical",
            "choices": list(distribution.choices),
        }
    elif isinstance(distribution, IntDistribution):
        normalized = {
            "kind": "int",
            "low": distribution.low,
            "high": distribution.high,
            "log": distribution.log,
        }
        if not distribution.log:
            normalized["step"] = distribution.step
    elif isinstance(distribution, FloatDistribution):
        normalized = {
            "kind": "float",
            "low": distribution.low,
            "high": distribution.high,
            "log": distribution.log,
        }
        if distribution.step is not None:
            normalized["step"] = distribution.step
    else:
        raise SuggesterValidationError(
            f"frozen_plan.search.space.{path} has no supported Optuna distribution"
        )
    return normalized, dimension.cardinality


def _compile_plan(
    frozen_plan: Mapping[str, Any],
    expected_suggester: str,
) -> _CompiledPlan:
    plan = _mapping(frozen_plan, "frozen_plan")
    _require_json_value(plan, "frozen_plan")
    search = _mapping(plan.get("search"), "frozen_plan.search")
    _require_json_value(search, "frozen_plan.search")
    if search.get("suggester") != expected_suggester:
        raise SuggesterValidationError(f"frozen_plan.search.suggester must be {expected_suggester}")
    if search.get("suggester_version") != "1.0.0":
        raise SuggesterValidationError("frozen_plan.search.suggester_version must be 1.0.0")
    if expected_suggester == "OPTUNA_TPE":
        if optuna.__version__ != OPTUNA_TPE_LIBRARY_VERSION:
            raise SuggesterValidationError(
                "installed Optuna version does not match the frozen adapter library version"
            )
        if canonical_json_bytes(search.get("adapter_identity")) != canonical_json_bytes(
            optuna_tpe_frozen_identity()
        ):
            raise SuggesterValidationError(
                "frozen_plan.search.adapter_identity does not match the frozen Optuna TPE identity"
            )

    template = _mapping(plan.get("template"), "frozen_plan.template")
    template_parameters = _mapping(
        template.get("parameters"),
        "frozen_plan.template.parameters",
    )
    _require_json_value(template_parameters, "frozen_plan.template.parameters")
    round_identity = _string(
        plan.get("round_identity"),
        "frozen_plan.round_identity",
    )
    candidate = {
        "schema_version": 1,
        "template": {
            "name": _string(template.get("name"), "frozen_plan.template.name"),
            "version": _string(
                template.get("version"),
                "frozen_plan.template.version",
            ),
            "content_digest": _string(
                template.get("content_digest"),
                "frozen_plan.template.content_digest",
            ),
            "parameters": {
                key: deepcopy(value)
                for key, value in template_parameters.items()
                if key not in {"evaluation_start", "evaluation_end"}
            },
        },
        "operators": {},
    }

    operators = _mapping(plan.get("operators"), "frozen_plan.operators")
    _require_string_keys(operators, "frozen_plan.operators")
    normalized_defaults: dict[str, dict[str, Any]] = {}
    schemas: dict[str, dict[str, Any]] = {}
    for slot, operator_value in sorted(operators.items()):
        operator = _mapping(operator_value, f"frozen_plan.operators.{slot}")
        if operator.get("slot") != slot:
            raise SuggesterValidationError(f"frozen_plan.operators.{slot}.slot must equal {slot}")
        _require_json_value(
            operator.get("defaults"),
            f"frozen_plan.operators.{slot}.defaults",
        )
        _require_json_value(
            operator.get("parameters"),
            f"frozen_plan.operators.{slot}.parameters",
        )
        try:
            schema = validate_parameter_schema(operator.get("parameter_schema"))
            defaults = validate_parameters(schema, operator.get("defaults"))
            parameters = validate_parameters(schema, operator.get("parameters"))
        except SchemaValidationError as exc:
            raise SuggesterValidationError(
                f"frozen_plan.operators.{slot} is invalid: {exc}"
            ) from exc
        schemas[slot] = schema
        normalized_defaults[slot] = defaults
        candidate["operators"][slot] = {
            "operator_id": _string(
                operator.get("operator_id"),
                f"frozen_plan.operators.{slot}.operator_id",
            ),
            "version": _string(
                operator.get("resolved_version"),
                f"frozen_plan.operators.{slot}.resolved_version",
            ),
            "content_digest": _string(
                operator.get("content_digest"),
                f"frozen_plan.operators.{slot}.content_digest",
            ),
            "parameters": parameters,
        }

    space = _mapping(search.get("space"), "frozen_plan.search.space")
    _require_string_keys(space, "frozen_plan.search.space")
    if not space:
        raise SuggesterValidationError("frozen_plan.search.space must not be empty")
    if len(space) > MAX_SEARCH_DIMENSIONS:
        raise SuggesterValidationError(
            f"frozen_plan.search.space must contain at most {MAX_SEARCH_DIMENSIONS} dimensions"
        )
    dimensions: list[_SearchDimension] = []
    for path, definition_value in sorted(space.items()):
        parts = path.split("/")
        if len(parts) != 4 or parts[:2] != ["", "operators"]:
            raise SuggesterValidationError(
                f"search path is not a frozen operator parameter: {path}"
            )
        slot, parameter = parts[2:]
        if slot in {"cost", "report"}:
            raise SuggesterValidationError(f"search path cannot target {slot}: {path}")
        if slot not in schemas or parameter not in schemas[slot]["properties"]:
            raise SuggesterValidationError(
                f"search path is not a frozen operator parameter: {path}"
            )
        definition = _mapping(
            definition_value,
            f"frozen_plan.search.space.{path}",
        )
        property_schema = schemas[slot]["properties"][parameter]
        candidate["operators"][slot]["parameters"][parameter] = normalized_defaults[slot][parameter]
        compiler = (
            _compile_optuna_dimension
            if expected_suggester == "OPTUNA_TPE"
            else _compile_values_dimension
        )
        dimensions.append(
            compiler(
                path=path,
                slot=slot,
                parameter=parameter,
                definition=definition,
                property_schema=property_schema,
            )
        )

    _canonical_json_bytes(
        candidate,
        "frozen candidate",
        MAX_CANDIDATE_CANONICAL_BYTES,
    )
    if expected_suggester != "OPTUNA_TPE":
        candidate_capacity = prod(len(dimension.values) for dimension in dimensions)
        if candidate_capacity > MAX_CANDIDATE_CAPACITY:
            raise SuggesterValidationError(
                f"frozen_plan.search candidate capacity exceeds {MAX_CANDIDATE_CAPACITY}"
            )
        declared_capacity = _bounded_positive_integer(
            search.get("candidate_capacity"),
            "frozen_plan.search.candidate_capacity",
            MAX_CANDIDATE_CAPACITY,
        )
        if declared_capacity != candidate_capacity:
            raise SuggesterValidationError(
                "frozen_plan.search.candidate_capacity does not match its finite domains"
            )
    unique_trial_budget = _bounded_positive_integer(
        search.get("unique_trial_budget"),
        "frozen_plan.search.unique_trial_budget",
        MAX_UNIQUE_TRIAL_BUDGET,
    )
    max_suggestions = _bounded_positive_integer(
        search.get("max_suggestions"),
        "frozen_plan.search.max_suggestions",
        MAX_SUGGESTIONS,
    )
    if max_suggestions < unique_trial_budget:
        raise SuggesterValidationError(
            "frozen_plan.search.max_suggestions must be greater than or equal to "
            "unique_trial_budget"
        )
    return _CompiledPlan(
        baseline_candidate=candidate,
        dimensions=tuple(dimensions),
        seed=_bounded_nonnegative_integer(
            search.get("seed"),
            "frozen_plan.search.seed",
            MAX_SEED,
        ),
        round_identity=round_identity,
        unique_trial_budget=unique_trial_budget,
        max_suggestions=max_suggestions,
    )


def _suggestion(
    proposal_sequence: int,
    candidate: dict[str, Any],
    dimensions: tuple[_SearchDimension, ...],
    first_candidate_by_digest: Mapping[str, _ObservedCandidate],
) -> Suggestion:
    candidate_bytes = _canonical_json_bytes(
        candidate,
        "suggestion.candidate",
        MAX_CANDIDATE_CANONICAL_BYTES,
    )
    in_range = all(
        dimension.contains(
            candidate["operators"][dimension.slot]["parameters"][dimension.parameter]
        )
        for dimension in dimensions
    )
    candidate_digest = _candidate_digest(candidate_bytes)
    observed = first_candidate_by_digest.get(candidate_digest)
    if observed is not None and observed.canonical_bytes != candidate_bytes:
        raise SuggesterValidationError(
            "candidate digest collision: identical digest has different canonical bytes"
        )
    duplicate_of_sequence = observed.first_sequence if observed is not None else None
    return Suggestion(
        proposal_sequence=proposal_sequence,
        candidate_digest=candidate_digest,
        candidate=candidate,
        classification=(
            SuggestionClassification.IN_RANGE
            if in_range
            else SuggestionClassification.BASELINE_ONLY
        ),
        disposition=(
            SuggestionDisposition.DUPLICATE
            if duplicate_of_sequence is not None
            else SuggestionDisposition.UNIQUE
        ),
        duplicate_of_sequence=duplicate_of_sequence,
    )


def _grid_candidate(compiled: _CompiledPlan, grid_index: int) -> dict[str, Any]:
    if grid_index < 0 or grid_index >= compiled.candidate_capacity:
        raise IndexError(grid_index)
    candidate = deepcopy(compiled.baseline_candidate)
    remainder = grid_index
    indexes = [0] * len(compiled.dimensions)
    for dimension_index in range(len(compiled.dimensions) - 1, -1, -1):
        domain_size = len(compiled.dimensions[dimension_index].values)
        indexes[dimension_index] = remainder % domain_size
        remainder //= domain_size
    for dimension, value_index in zip(compiled.dimensions, indexes, strict=True):
        candidate["operators"][dimension.slot]["parameters"][dimension.parameter] = (
            dimension.value_at(value_index)
        )
    return candidate


def _seeded_index(
    *,
    seed: int,
    round_identity: str,
    proposal_sequence: int,
    path: str,
    domain_size: int,
) -> int:
    draw = 0
    ceiling = 1 << 256
    limit = ceiling - ceiling % domain_size
    while True:
        payload = {
            "adapter": "SEEDED_RANDOM",
            "draw": draw,
            "path": path,
            "proposal_sequence": proposal_sequence,
            "round_identity": round_identity,
            "seed": seed,
            "version": "1.0.0",
        }
        value = int.from_bytes(
            hashlib.sha256(canonical_json_bytes(payload)).digest(),
            "big",
        )
        if value < limit:
            return value % domain_size
        draw += 1


def _random_candidate(
    compiled: _CompiledPlan,
    proposal_sequence: int,
) -> dict[str, Any]:
    candidate = deepcopy(compiled.baseline_candidate)
    for dimension in compiled.dimensions:
        value_index = _seeded_index(
            seed=compiled.seed,
            round_identity=compiled.round_identity,
            proposal_sequence=proposal_sequence,
            path=dimension.path,
            domain_size=len(dimension.values),
        )
        candidate["operators"][dimension.slot]["parameters"][dimension.parameter] = (
            dimension.value_at(value_index)
        )
    return candidate


def _history_event(item: Suggestion | Mapping[str, Any], index: int) -> Mapping[str, Any]:
    if isinstance(item, Suggestion):
        event = item.as_history_event()
        _canonical_json_bytes(
            event,
            f"ordered_history[{index}]",
            MAX_EVENT_CANONICAL_BYTES,
        )
        return event
    event = _mapping(item, f"ordered_history[{index}]")
    _canonical_json_bytes(
        event,
        f"ordered_history[{index}]",
        MAX_EVENT_CANONICAL_BYTES,
    )
    expected_fields = {
        "event_type",
        "proposal_sequence",
        "candidate_digest",
        "candidate",
        "classification",
        "disposition",
        "duplicate_of_sequence",
    }
    if set(event) != expected_fields:
        raise SuggesterValidationError(f"ordered_history[{index}] is not a suggestion record")
    event_type = _domain_value(
        event.get("event_type"),
        HistoryEventType,
        f"ordered_history[{index}].event_type",
    )
    if event_type not in {
        HistoryEventType.SUGGESTION_RECORDED,
        HistoryEventType.DUPLICATE_SUGGESTION,
    }:
        raise SuggesterValidationError(f"ordered_history[{index}] is not a suggestion record")
    suggestion = Suggestion(
        proposal_sequence=event.get("proposal_sequence"),
        candidate_digest=event.get("candidate_digest"),
        candidate=_mapping(event.get("candidate"), f"ordered_history[{index}].candidate"),
        classification=event.get("classification"),
        disposition=event.get("disposition"),
        duplicate_of_sequence=event.get("duplicate_of_sequence"),
    )
    if (event_type is HistoryEventType.DUPLICATE_SUGGESTION) != (
        suggestion.disposition is SuggestionDisposition.DUPLICATE
    ):
        raise SuggesterValidationError(
            f"ordered_history[{index}].event_type does not match its disposition"
        )
    return suggestion.as_history_event()


def _proposal_events(
    ordered_history: Sequence[Suggestion | Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if len(ordered_history) > MAX_ORDERED_HISTORY_LENGTH:
        raise SuggesterValidationError(
            f"ordered_history must contain at most {MAX_ORDERED_HISTORY_LENGTH} events"
        )
    proposals: list[Mapping[str, Any]] = []
    recorded_candidate_digests: set[str] = set()
    for index, item in enumerate(ordered_history):
        if isinstance(item, Suggestion):
            event = item.as_history_event()
            _canonical_json_bytes(
                event,
                f"ordered_history[{index}]",
                MAX_EVENT_CANONICAL_BYTES,
            )
            proposals.append(event)
            recorded_candidate_digests.add(item.candidate_digest)
            continue
        event = _mapping(item, f"ordered_history[{index}]")
        _canonical_json_bytes(
            event,
            f"ordered_history[{index}]",
            MAX_EVENT_CANONICAL_BYTES,
        )
        event_type = _domain_value(
            event.get("event_type"),
            HistoryEventType,
            f"ordered_history[{index}].event_type",
        )
        if event_type in {
            HistoryEventType.SUGGESTION_RECORDED,
            HistoryEventType.DUPLICATE_SUGGESTION,
        }:
            proposal = _history_event(event, index)
            proposals.append(proposal)
            recorded_candidate_digests.add(proposal["candidate_digest"])
            continue
        role = _domain_value(
            event.get("role"),
            EvaluationRole,
            f"ordered_history[{index}].role",
        )
        if role is not EvaluationRole.INNER_SCORE:
            raise SuggesterHistoryLeakageError(
                f"ordered_history[{index}] contains forbidden {role.value} evidence"
            )
        if event_type is HistoryEventType.INNER_EVALUATION_RECORDED:
            if set(event) != {
                "event_type",
                "role",
                "candidate_digest",
                "evaluation",
            }:
                raise SuggesterValidationError(
                    f"ordered_history[{index}] has an invalid inner-evaluation shape"
                )
            candidate_digest = event.get("candidate_digest")
            if not isinstance(candidate_digest, str):
                raise SuggesterValidationError(
                    f"ordered_history[{index}].candidate_digest must be a string"
                )
            if candidate_digest not in recorded_candidate_digests:
                raise SuggesterValidationError(
                    f"ordered_history[{index}] evaluates an unknown candidate"
                )
            continue
        raise SuggesterValidationError(f"ordered_history[{index}] is not valid suggester history")
    return tuple(proposals)


def _replay_history(
    compiled: _CompiledPlan,
    proposal_events: Sequence[Mapping[str, Any]],
    candidate_for_sequence: Callable[[int], dict[str, Any]],
) -> tuple[dict[str, _ObservedCandidate], int, set[str]]:
    first_candidate_by_digest: dict[str, _ObservedCandidate] = {}
    in_range_candidate_digests: set[str] = set()
    unique_trial_count = 0
    for sequence, event in enumerate(proposal_events):
        if sequence >= compiled.max_suggestions:
            raise SuggesterValidationError(
                "ordered_history continues after the raw-suggestion budget"
            )
        if unique_trial_count >= compiled.unique_trial_budget:
            raise SuggesterValidationError(
                "ordered_history continues after the unique-Trial budget"
            )
        candidate = (
            compiled.baseline_candidate if sequence == 0 else candidate_for_sequence(sequence)
        )
        expected = _suggestion(
            sequence,
            candidate,
            compiled.dimensions,
            first_candidate_by_digest,
        )
        if canonical_json_bytes(event) != canonical_json_bytes(expected.as_history_event()):
            raise SuggesterValidationError(
                f"ordered_history[{sequence}] does not match deterministic replay"
            )
        if expected.creates_trial:
            first_candidate_by_digest[expected.candidate_digest] = _ObservedCandidate(
                canonical_bytes=expected._candidate_bytes,
                first_sequence=sequence,
            )
            unique_trial_count += 1
        if expected.classification is SuggestionClassification.IN_RANGE:
            in_range_candidate_digests.add(expected.candidate_digest)
    return first_candidate_by_digest, unique_trial_count, in_range_candidate_digests


_CandidateForSequence = Callable[[_CompiledPlan, int], dict[str, Any]]


def _next_suggestion(
    frozen_plan: Mapping[str, Any],
    ordered_history: Sequence[Suggestion | Mapping[str, Any]],
    expected_suggester: str,
    candidate_for_sequence: _CandidateForSequence,
) -> Suggestion | Exhausted:
    compiled = _compile_plan(frozen_plan, expected_suggester)
    proposal_events = _proposal_events(ordered_history)
    (
        first_candidate_by_digest,
        unique_trial_count,
        in_range_candidate_digests,
    ) = _replay_history(
        compiled,
        proposal_events,
        lambda sequence: candidate_for_sequence(compiled, sequence),
    )
    proposal_sequence = len(proposal_events)
    if unique_trial_count >= compiled.unique_trial_budget:
        return Exhausted(
            reason=ExhaustionReason.UNIQUE_TRIAL_BUDGET,
            raw_suggestion_count=proposal_sequence,
            unique_trial_count=unique_trial_count,
        )
    if proposal_sequence >= compiled.max_suggestions:
        return Exhausted(
            reason=ExhaustionReason.RAW_SUGGESTION_BUDGET,
            raw_suggestion_count=proposal_sequence,
            unique_trial_count=unique_trial_count,
        )
    if len(in_range_candidate_digests) == compiled.candidate_capacity:
        return Exhausted(
            reason=ExhaustionReason.SEARCH_SPACE_EXHAUSTED,
            raw_suggestion_count=proposal_sequence,
            unique_trial_count=unique_trial_count,
        )
    candidate = (
        compiled.baseline_candidate
        if proposal_sequence == 0
        else candidate_for_sequence(compiled, proposal_sequence)
    )
    return _suggestion(
        proposal_sequence,
        candidate,
        compiled.dimensions,
        first_candidate_by_digest,
    )


def _grid_candidate_for_sequence(
    compiled: _CompiledPlan,
    proposal_sequence: int,
) -> dict[str, Any]:
    return _grid_candidate(compiled, proposal_sequence - 1)


def _random_candidate_for_sequence(
    compiled: _CompiledPlan,
    proposal_sequence: int,
) -> dict[str, Any]:
    return _random_candidate(compiled, proposal_sequence)


class GridParameterSuggester:
    """Stateless deterministic adapter for a frozen finite Cartesian grid."""

    __slots__ = ()

    def next_suggestion(
        self,
        frozen_plan: Mapping[str, Any],
        ordered_history: Sequence[Suggestion | Mapping[str, Any]],
    ) -> Suggestion | Exhausted:
        return _next_suggestion(
            frozen_plan,
            ordered_history,
            "GRID",
            _grid_candidate_for_sequence,
        )


class SeededRandomParameterSuggester:
    """Stateless deterministic adapter for seeded finite-domain sampling."""

    __slots__ = ()

    def next_suggestion(
        self,
        frozen_plan: Mapping[str, Any],
        ordered_history: Sequence[Suggestion | Mapping[str, Any]],
    ) -> Suggestion | Exhausted:
        return _next_suggestion(
            frozen_plan,
            ordered_history,
            "SEEDED_RANDOM",
            _random_candidate_for_sequence,
        )


def _new_optuna_tpe_study(compiled: _CompiledPlan) -> optuna.study.Study:
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    sampler = optuna.samplers.TPESampler(
        seed=compiled.seed,
        **_OPTUNA_TPE_SAMPLER_SETTINGS,
    )
    study_name = (
        "quant-platform-optuna-tpe-"
        + hashlib.sha256(compiled.round_identity.encode("utf-8")).hexdigest()
    )
    return optuna.create_study(
        direction="maximize",
        sampler=sampler,
        study_name=study_name,
    )


def _optuna_candidate(compiled: _CompiledPlan, trial: Trial) -> dict[str, Any]:
    candidate = deepcopy(compiled.baseline_candidate)
    for dimension in compiled.dimensions:
        candidate["operators"][dimension.slot]["parameters"][dimension.parameter] = trial.params[
            dimension.path
        ]
    return candidate


def _ask_optuna_trial(compiled: _CompiledPlan, study: optuna.study.Study) -> Trial:
    distributions = {
        dimension.path: dimension.optuna_distribution
        for dimension in compiled.dimensions
        if dimension.optuna_distribution is not None
    }
    if len(distributions) != len(compiled.dimensions):
        raise SuggesterValidationError("Optuna TPE requires typed distributions")
    return study.ask(fixed_distributions=distributions)


def _ask_optuna_proposal(
    compiled: _CompiledPlan,
    study: optuna.study.Study,
    proposal_sequence: int,
) -> tuple[Trial, dict[str, Any]]:
    if proposal_sequence != 0:
        trial = _ask_optuna_trial(compiled, study)
        return trial, _optuna_candidate(compiled, trial)

    candidate = deepcopy(compiled.baseline_candidate)
    baseline_parameters = {
        dimension.path: candidate["operators"][dimension.slot]["parameters"][
            dimension.parameter
        ]
        for dimension in compiled.dimensions
    }
    if all(
        dimension.contains(baseline_parameters[dimension.path])
        for dimension in compiled.dimensions
    ):
        study.enqueue_trial(baseline_parameters)
        trial = _ask_optuna_trial(compiled, study)
    else:
        trial = study.ask()
    return trial, candidate


@dataclass(frozen=True)
class _OptunaOutcome:
    state: TrialState
    score: float | None


def _optuna_inner_outcome(
    event: Mapping[str, Any],
    index: int,
    compiled: _CompiledPlan,
    known_candidate_digests: set[str],
) -> tuple[str, _OptunaOutcome]:
    if set(event) != {
        "event_type",
        "round_identity",
        "role",
        "candidate_digest",
        "evaluation",
    }:
        raise SuggesterValidationError(
            f"ordered_history[{index}] has an invalid same-round inner-evaluation shape"
        )
    role = _domain_value(event.get("role"), EvaluationRole, f"ordered_history[{index}].role")
    if role is not EvaluationRole.INNER_SCORE:
        raise SuggesterHistoryLeakageError(
            f"ordered_history[{index}] contains forbidden {role.value} evidence"
        )
    round_identity = event.get("round_identity")
    if round_identity != compiled.round_identity:
        raise SuggesterHistoryLeakageError(
            f"ordered_history[{index}] is not from the same search round"
        )
    candidate_digest = event.get("candidate_digest")
    if not isinstance(candidate_digest, str) or candidate_digest not in known_candidate_digests:
        raise SuggesterValidationError(
            f"ordered_history[{index}] evaluates an unknown candidate"
        )
    evaluation = _mapping(event.get("evaluation"), f"ordered_history[{index}].evaluation")
    status = evaluation.get("status")
    if status == "FAILED":
        if "validation_score" in evaluation:
            raise SuggesterValidationError(
                f"ordered_history[{index}] failed candidate must not have a fabricated score"
            )
        return candidate_digest, _OptunaOutcome(TrialState.FAIL, None)
    if status != "COMPLETED":
        raise SuggesterValidationError(
            f"ordered_history[{index}].evaluation.status must be COMPLETED or FAILED"
        )
    score = evaluation.get("validation_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score):
        raise SuggesterValidationError(
            f"ordered_history[{index}].evaluation.validation_score must be a finite number"
        )
    return candidate_digest, _OptunaOutcome(TrialState.COMPLETE, float(score))


def _tell_optuna_outcome(
    study: optuna.study.Study,
    trial: Trial,
    outcome: _OptunaOutcome,
) -> None:
    if outcome.state is TrialState.COMPLETE:
        if outcome.score is None:
            raise SuggesterValidationError("completed Optuna outcome requires a score")
        study.tell(trial, outcome.score, state=TrialState.COMPLETE)
    else:
        study.tell(trial, state=TrialState.FAIL)


def _next_optuna_tpe_suggestion(
    compiled: _CompiledPlan,
    ordered_history: Sequence[Suggestion | Mapping[str, Any]],
) -> Suggestion | Exhausted:
    if len(ordered_history) > MAX_ORDERED_HISTORY_LENGTH:
        raise SuggesterValidationError(
            f"ordered_history must contain at most {MAX_ORDERED_HISTORY_LENGTH} events"
        )
    study = _new_optuna_tpe_study(compiled)
    first_candidate_by_digest: dict[str, _ObservedCandidate] = {}
    known_candidate_digests: set[str] = set()
    in_range_candidate_digests: set[str] = set()
    outcomes_by_digest: dict[str, _OptunaOutcome] = {}
    pending_trial: Trial | None = None
    pending_digest: str | None = None
    proposal_count = 0
    unique_trial_count = 0

    for index, item in enumerate(ordered_history):
        if isinstance(item, Suggestion):
            event_type = HistoryEventType(item.as_history_event()["event_type"])
        else:
            event = _mapping(item, f"ordered_history[{index}]")
            _canonical_json_bytes(
                event,
                f"ordered_history[{index}]",
                MAX_EVENT_CANONICAL_BYTES,
            )
            event_type = _domain_value(
                event.get("event_type"),
                HistoryEventType,
                f"ordered_history[{index}].event_type",
            )

        if event_type in {
            HistoryEventType.OUTER_EVALUATION_RECORDED,
            HistoryEventType.HOLDOUT_EVALUATION_RECORDED,
        }:
            role = event.get("role") if not isinstance(item, Suggestion) else event_type.value
            raise SuggesterHistoryLeakageError(
                f"ordered_history[{index}] contains forbidden {role} evidence"
            )

        if event_type is HistoryEventType.INNER_EVALUATION_RECORDED:
            if isinstance(item, Suggestion):
                raise SuggesterValidationError(
                    f"ordered_history[{index}] is not a valid inner evaluation"
                )
            candidate_digest, outcome = _optuna_inner_outcome(
                event,
                index,
                compiled,
                known_candidate_digests,
            )
            if candidate_digest in outcomes_by_digest:
                raise SuggesterValidationError(
                    f"ordered_history[{index}] repeats a terminal inner evaluation"
                )
            if pending_trial is None or pending_digest != candidate_digest:
                raise SuggesterValidationError(
                    f"ordered_history[{index}] is not ordered after its proposal"
                )
            _tell_optuna_outcome(study, pending_trial, outcome)
            outcomes_by_digest[candidate_digest] = outcome
            pending_trial = None
            pending_digest = None
            continue

        if event_type not in {
            HistoryEventType.SUGGESTION_RECORDED,
            HistoryEventType.DUPLICATE_SUGGESTION,
        }:
            raise SuggesterValidationError(
                f"ordered_history[{index}] is not valid Optuna suggester history"
            )
        if pending_trial is not None:
            raise SuggesterValidationError(
                f"ordered_history[{index}] continues before a terminal inner evaluation"
            )
        if proposal_count >= compiled.max_suggestions:
            raise SuggesterValidationError(
                "ordered_history continues after the raw-suggestion budget"
            )
        if unique_trial_count >= compiled.unique_trial_budget:
            raise SuggesterValidationError(
                "ordered_history continues after the unique-Trial budget"
            )
        trial, candidate = _ask_optuna_proposal(compiled, study, proposal_count)
        expected = _suggestion(
            proposal_count,
            candidate,
            compiled.dimensions,
            first_candidate_by_digest,
        )
        proposal_event = _history_event(item, index)
        if canonical_json_bytes(proposal_event) != canonical_json_bytes(
            expected.as_history_event()
        ):
            raise SuggesterValidationError(
                f"ordered_history[{index}] does not match exact Optuna proposal replay"
            )
        proposal_count += 1
        known_candidate_digests.add(expected.candidate_digest)
        if expected.creates_trial:
            first_candidate_by_digest[expected.candidate_digest] = _ObservedCandidate(
                canonical_bytes=expected._candidate_bytes,
                first_sequence=expected.proposal_sequence,
            )
            unique_trial_count += 1
        if expected.classification is SuggestionClassification.IN_RANGE:
            in_range_candidate_digests.add(expected.candidate_digest)
        known_outcome = outcomes_by_digest.get(expected.candidate_digest)
        if known_outcome is not None:
            _tell_optuna_outcome(study, trial, known_outcome)
        else:
            pending_trial = trial
            pending_digest = expected.candidate_digest

    if pending_trial is not None:
        raise SuggesterValidationError(
            "the latest Optuna proposal requires a terminal inner evaluation before the next ask"
        )
    if unique_trial_count >= compiled.unique_trial_budget:
        return Exhausted(
            ExhaustionReason.UNIQUE_TRIAL_BUDGET,
            proposal_count,
            unique_trial_count,
        )
    if proposal_count >= compiled.max_suggestions:
        return Exhausted(
            ExhaustionReason.RAW_SUGGESTION_BUDGET,
            proposal_count,
            unique_trial_count,
        )
    candidate_capacity = compiled.candidate_capacity
    if (
        candidate_capacity is not None
        and len(in_range_candidate_digests) == candidate_capacity
    ):
        return Exhausted(
            ExhaustionReason.SEARCH_SPACE_EXHAUSTED,
            proposal_count,
            unique_trial_count,
        )
    _, candidate = _ask_optuna_proposal(compiled, study, proposal_count)
    return _suggestion(
        proposal_count,
        candidate,
        compiled.dimensions,
        first_candidate_by_digest,
    )


class OptunaTPEParameterSuggester:
    """Stateless replay adapter for a version-frozen Optuna TPE ask/tell loop."""

    __slots__ = ()

    def next_suggestion(
        self,
        frozen_plan: Mapping[str, Any],
        ordered_history: Sequence[Suggestion | Mapping[str, Any]],
    ) -> Suggestion | Exhausted:
        compiled = _compile_plan(frozen_plan, "OPTUNA_TPE")
        return _next_optuna_tpe_suggestion(compiled, ordered_history)
