from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode

import bleach
import markdown
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .auth import AuthError, AuthManager, SessionData
from .catalog import initialize_catalog
from .dataset_service import DatasetResolutionError, DatasetService
from .datasets import _verify_snapshot
from .experiment_service import ExperimentService, TaskValidationError
from .operator_service import OperatorService, OperatorSubmissionError
from .parameter_study import ParameterStudy, StudyNotFoundError, StudyValidationError
from .resolved_runner import effective_execution_identity
from .schemas import SchemaValidationError, canonical_json_bytes, validate_parameters
from .seed import BUILTINS
from .settings import Settings
from .strategy_runner import (
    ARTIFACT_NAMES,
    HASHED_ARTIFACT_NAMES,
)


SESSION_COOKIE = "quant_session"
THEME_COOKIE = "quant_theme"
THEMES = frozenset({"light", "dark", "system"})
MAX_BODY_BYTES = 1_048_576
MAX_JSON_DEPTH = 64
MAX_JSON_CONTAINERS = 10_000
MAX_JSON_NODES = 20_000
PACKAGE_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
STUDY_ID = re.compile(r"^[0-9a-f]{64}$")
STUDY_OUTCOMES = {
    "ACTION_CONFLICT": "Action conflict: this action ID was already used differently.",
    "ADVANCED": "Study advanced.",
    "CANCELLED": "Study cancelled.",
    "EFFECT_AUTHORIZED": "The next Study effect is authorized.",
    "EFFECT_COMMITTED": "Study effect committed.",
    "EFFECT_PENDING": "A Study effect is pending.",
    "EXECUTION_IDENTITY_DRIFT": (
        "Execution identity drift detected. New Study effects remain blocked."
    ),
    "INVALID_TRANSITION": "The requested Study transition is not valid.",
    "LEASE_BUSY": "Another coordinator currently holds the Study lease.",
    "NO_CHANGE": "The Study was already in the requested state.",
    "PAUSED": "Study paused.",
    "RESUMED": "Study resumed.",
}


def _canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


TEMPLATES = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
TEMPLATES.env.filters["canonical_json"] = _canonical_json_text
MARKDOWN_TAGS = {
    "a",
    "blockquote",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "ul",
}
DEFAULT_OPERATOR_IDS = {
    descriptor["slot"]: descriptor["operator_id"] for descriptor in BUILTINS
}


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _bounded_json_loads(value: str | bytes, path: str) -> Any:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"{path} contains non-finite value {constant}")
            ),
        )
    except RecursionError as exc:
        raise ValueError(f"{path} exceeds the JSON nesting limit") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON") from exc
    containers = 0
    nodes = 1
    pending = [(parsed, 0)]
    while pending:
        item, depth = pending.pop()
        if not isinstance(item, (dict, list)):
            continue
        containers += 1
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{path} exceeds the JSON nesting limit")
        if containers > MAX_JSON_CONTAINERS:
            raise ValueError(f"{path} exceeds the JSON container limit")
        children = item.values() if isinstance(item, dict) else item
        children = list(children)
        nodes += len(children)
        if nodes > MAX_JSON_NODES:
            raise ValueError(f"{path} exceeds the JSON value limit")
        pending.extend((child, depth + 1) for child in children)
    return parsed


def _study_outcome_token(
    secret: str,
    session: SessionData,
    study_id: str,
    outcome: str,
) -> str:
    payload = f"{session.csrf_token}\0{study_id}\0{outcome}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"{outcome}.{signature}"


def _study_outcome(
    secret: str,
    session: SessionData,
    study_id: str,
    token: str,
) -> str | None:
    outcome, separator, signature = token.partition(".")
    if not separator or outcome not in STUDY_OUTCOMES:
        return None
    expected = _study_outcome_token(secret, session, study_id, outcome)
    return outcome if hmac.compare_digest(token, expected) else None


async def _json_body(request: Request) -> Any:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("request body exceeds the size limit")
    return _bounded_json_loads(body, "request body")


async def _form_body(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("request body exceeds the size limit")
    if request.headers.get("content-type", "").split(";", 1)[0] != (
        "application/x-www-form-urlencoded"
    ):
        raise ValueError("form body must be application/x-www-form-urlencoded")
    values = parse_qs(
        body.decode("utf-8"), keep_blank_values=True, strict_parsing=True
    )
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("form fields must occur exactly once")
    return {key: items[0] for key, items in values.items()}


def _safe_markdown(value: str) -> str:
    rendered = markdown.markdown(value, extensions=[])
    return bleach.clean(
        rendered,
        tags=MARKDOWN_TAGS,
        attributes={"a": ["href", "title"]},
        protocols={"https"},
        strip=True,
    )


def _json_text(value: str, path: str) -> Any:
    return _bounded_json_loads(value, path)


def _study_id(value: str) -> str:
    if STUDY_ID.fullmatch(value) is None:
        raise StudyNotFoundError(f"unknown Parameter Study: {value}")
    return value


def _session(request: Request) -> SessionData:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie is None:
        raise AuthError("Authentication required")
    session = request.app.state.auth.verify_session(cookie)
    if session.email == "__login__":
        raise AuthError("Authentication required")
    return session


def _csrf(request: Request, session: SessionData, token: str | None = None) -> None:
    request.app.state.auth.verify_csrf(
        session, token or request.headers.get("x-csrf-token", "")
    )


def _theme(request: Request) -> str:
    requested = request.query_params.get("theme")
    if requested in THEMES:
        return requested
    stored = request.cookies.get(THEME_COOKIE)
    return stored if stored in THEMES else "system"


def _render(
    request: Request,
    name: str,
    *,
    session: SessionData,
    status_code: int = 200,
    **context: Any,
) -> Response:
    return TEMPLATES.TemplateResponse(
        request=request,
        name=name,
        context={
            "session": session,
            "csrf_token": session.csrf_token,
            "theme": _theme(request),
            **context,
        },
        status_code=status_code,
    )


def _form_error_context(
    values: dict[str, str],
    error: Exception,
    *,
    fallback_field: str,
) -> dict[str, Any]:
    message = str(error)
    field = next(
        (
            name
            for name in values
            if message.startswith(name)
            or f".{name}" in message
            or f" {name} " in f" {message} "
        ),
        fallback_field,
    )
    errors = [{"field": field, "message": message}]
    return {
        "errors": errors,
        "error_messages": {field: message},
        "invalid_fields": {field},
    }


def _datasets(state_root: Path) -> list[dict[str, str]]:
    datasets: list[dict[str, str]] = []
    root = state_root / "datasets"
    if not root.is_dir() or root.is_symlink():
        return datasets
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        snapshot = manifest_path.parent
        if snapshot.is_symlink() or manifest_path.is_symlink():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            datasets.append(
                {
                    "instrument": manifest["metadata"]["instrument"],
                    "snapshot_id": manifest["snapshot_id"],
                    "label": (
                        f"{manifest['metadata']['instrument']} · "
                        f"{manifest['data_start']} to {manifest['data_end']}"
                    ),
                }
            )
        except (KeyError, OSError, ValueError):
            continue
    return datasets


def _operator_groups(operators: OperatorService) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for operator in operators.list():
        detail = operators.detail(
            operator["operator_id"], operator["latest_version"]
        )
        grouped.setdefault(operator["slot"], []).append(
            detail
            | {
                "latest_version": operator["latest_version"],
                "versions": operators.list_versions(operator["operator_id"]),
            }
        )
    for slot, items in grouped.items():
        items.sort(
            key=lambda item: (
                item["operator_id"] != DEFAULT_OPERATOR_IDS[slot],
                item["operator_id"],
            )
        )
    return grouped


def _study_operator_groups(
    creation: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for operator in creation["operators"]:
        grouped.setdefault(operator["slot"], []).append(operator)
    for slot, items in grouped.items():
        items.sort(
            key=lambda item: (
                item["operator_id"] != DEFAULT_OPERATOR_IDS[slot],
                item["operator_id"],
            )
        )
    return grouped


def _form_parameter_value(raw: str, schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        value = _json_text(raw, "enum parameter")
        if canonical_json_bytes(value).decode("utf-8") != raw:
            raise ValueError("enum parameter must use canonical JSON")
        return value
    if raw == "" and schema.get("nullable"):
        return None
    if schema["type"] == "number":
        return float(raw)
    if schema["type"] == "integer":
        return int(raw)
    if schema["type"] == "boolean":
        if raw.lower() not in {"true", "false"}:
            raise ValueError("boolean parameter must be true or false")
        return raw.lower() == "true"
    return raw


def _form_parameter_default(value: Any, schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return canonical_json_bytes(value).decode("utf-8")
    if value is None:
        return ""
    return str(value)


def _form_parameter(
    form: dict[str, str],
    field_name: str,
    schema: dict[str, Any],
    default: str = "",
) -> Any:
    try:
        return _form_parameter_value(form.get(field_name, default), schema)
    except ValueError as exc:
        raise TaskValidationError(f"{field_name}: {exc}") from exc


def _task_from_form(
    form: dict[str, str],
    *,
    catalog: Any,
) -> dict[str, Any]:
    dataset_id = form.get("dataset_id", "")
    start_date = form.get("start_date", "")
    end_date = form.get("end_date", "")
    if not dataset_id or not start_date or not end_date:
        raise TaskValidationError("dataset selection is required")
    template = catalog.template_detail("single_stock_daily_causal", "1")
    template_parameters = {
        name: (
            start_date
            if name == "evaluation_start"
            else end_date
            if name == "evaluation_end"
            else _form_parameter(form, f"template_{name}", schema)
        )
        for name, schema in template["parameter_schema"]["properties"].items()
    }
    task_operators: dict[str, Any] = {}
    for slot in template["slots"]:
        selector = form.get(f"operator_{slot}_selector", "")
        if "@" not in selector:
            raise TaskValidationError(f"{slot} operator selection is required")
        operator_id, requested_version = selector.rsplit("@", 1)
        selected = catalog.operator_detail(
            operator_id,
            None if requested_version == "latest" else requested_version,
        )
        parameters = {
            name: _form_parameter(
                form,
                (
                    f"operator_{slot}_param__{operator_id}__"
                    f"{selected['version']}__{name}"
                ),
                schema,
                _form_parameter_default(selected["defaults"][name], schema),
            )
            for name, schema in selected["parameter_schema"]["properties"].items()
        }
        task_operators[slot] = {
            "operator_id": operator_id,
            "version": requested_version,
            "parameters": parameters,
        }
    return {
        "schema_version": 1,
        "dataset": {
            "dataset_id": dataset_id,
            "start": start_date,
            "end": end_date,
        },
        "template": {
            "name": template["name"],
            "version": template["version"],
            "parameters": template_parameters,
        },
        "operators": task_operators,
    }


def _study_operator_version(
    creation: dict[str, Any],
    slot: str,
    operator_id: str,
    requested_version: str,
) -> dict[str, Any]:
    for operator in creation["operators"]:
        if operator["slot"] != slot or operator["operator_id"] != operator_id:
            continue
        resolved_version = (
            operator["latest_version"]
            if requested_version == "latest"
            else requested_version
        )
        for version in operator["versions"]:
            if version["version"] == resolved_version:
                return version
    raise TaskValidationError(
        f"unknown published Study operator: {operator_id}@{requested_version}"
    )


def _study_task_from_form(
    form: dict[str, str], creation: dict[str, Any]
) -> dict[str, Any]:
    dataset_id = form.get("dataset_id", "")
    start_date = form.get("start_date", "")
    end_date = form.get("end_date", "")
    if not dataset_id or not start_date or not end_date:
        raise TaskValidationError("dataset selection is required")
    if dataset_id not in {item["dataset_id"] for item in creation["datasets"]}:
        raise TaskValidationError("dataset selection is not published")
    template = creation["template"]
    template_parameters = {
        name: (
            start_date
            if name == "evaluation_start"
            else end_date
            if name == "evaluation_end"
            else _form_parameter(
                form,
                f"template_{name}",
                schema,
                _form_parameter_default(template["defaults"][name], schema),
            )
        )
        for name, schema in template["parameter_schema"]["properties"].items()
    }
    task_operators: dict[str, Any] = {}
    for slot in template["slots"]:
        selector = form.get(f"operator_{slot}_selector", "")
        if "@" not in selector:
            raise TaskValidationError(f"{slot} operator selection is required")
        operator_id, requested_version = selector.rsplit("@", 1)
        selected = _study_operator_version(
            creation, slot, operator_id, requested_version
        )
        parameters = {
            name: _form_parameter(
                form,
                (
                    f"operator_{slot}_param__{operator_id}__"
                    f"{selected['version']}__{name}"
                ),
                schema,
                _form_parameter_default(selected["defaults"][name], schema),
            )
            for name, schema in selected["parameter_schema"]["properties"].items()
        }
        task_operators[slot] = {
            "operator_id": operator_id,
            "version": requested_version,
            "parameters": parameters,
        }
    return {
        "schema_version": 1,
        "dataset": {
            "dataset_id": dataset_id,
            "start": start_date,
            "end": end_date,
        },
        "template": {
            "name": template["name"],
            "version": template["version"],
            "parameters": template_parameters,
        },
        "operators": task_operators,
    }


def _study_from_form(
    form: dict[str, str],
    *,
    creation: dict[str, Any],
) -> dict[str, Any]:
    task = _study_task_from_form(form, creation)
    suggester = form.get("suggester", "GRID")
    if suggester not in {"GRID", "SEEDED_RANDOM", "OPTUNA_TPE"}:
        raise StudyValidationError("suggester: unsupported Study suggester")

    search_space: dict[str, dict[str, Any]] = {}
    eligible: dict[str, tuple[str, str, dict[str, Any], dict[str, Any]]] = {}
    for slot, selector in task["operators"].items():
        selected = _study_operator_version(
            creation,
            slot,
            selector["operator_id"],
            selector["version"],
        )
        for name, schema in selected["parameter_schema"]["properties"].items():
            if slot in {"cost", "report"}:
                continue
            identity = (
                f"{slot}__{selector['operator_id']}__{selected['version']}__{name}"
            )
            eligible[identity] = (
                slot,
                name,
                schema,
                selected["parameter_schema"],
            )

    selected_domains: dict[str, str] = {}
    for field_name, raw_kind in form.items():
        if not field_name.startswith("study__"):
            continue
        identity = field_name.removeprefix("study__")
        if identity not in eligible:
            raise StudyValidationError(
                f"{field_name}: parameter is not eligible for the selected operator version"
            )
        schema = eligible[identity][2]
        expected_kind = (
            "categorical"
            if "enum" in schema or schema["type"] in {"string", "boolean"}
            else "int"
            if schema["type"] == "integer"
            else "float"
        )
        if raw_kind != expected_kind:
            raise StudyValidationError(
                f"{field_name}: {schema['type']} parameter must use {expected_kind} domain"
            )
        selected_domains[identity] = expected_kind

    for field_name, raw in form.items():
        if not (
            field_name.startswith("domain__") or field_name.startswith("search__")
        ):
            continue
        parts = field_name.split("__")
        identity = "__".join(parts[1:5])
        if raw.strip() and identity not in selected_domains:
            raise StudyValidationError(
                f"{field_name}: domain fields are not allowed for an unchecked parameter"
            )

    def domain_value(
        *,
        field_name: str,
        raw: str,
        schema: dict[str, Any],
        parameter_schema: dict[str, Any],
        parameter_name: str,
        cast: type[int] | type[float] | None = None,
    ) -> Any:
        try:
            value = _json_text(raw, field_name) if cast is None else cast(raw)
            parameters = dict(task["operators"][identity.split("__", 1)[0]]["parameters"])
            parameters[parameter_name] = value
            return validate_parameters(parameter_schema, parameters)[parameter_name]
        except (SchemaValidationError, ValueError) as exc:
            raise StudyValidationError(f"{field_name}: {exc}") from exc

    for identity, kind in selected_domains.items():
        slot, name, schema, parameter_schema = eligible[identity]
        path = f"/operators/{slot}/{name}"
        prefix = f"domain__{identity}__"
        if suggester in {"GRID", "SEEDED_RANDOM"}:
            field_name = f"search__{identity}"
            raw = form.get(field_name, "").strip()
            if not raw and kind == "categorical":
                field_name = f"{prefix}choices"
                raw = form.get(field_name, "").strip()
            if not raw:
                raise StudyValidationError(
                    f"{field_name}: finite search values are required for {suggester}"
                )
            try:
                values = _json_text(raw, f"finite search values for {slot}.{name}")
            except (SchemaValidationError, ValueError) as exc:
                raise StudyValidationError(f"{field_name}: {exc}") from exc
            if not isinstance(values, list) or not values:
                raise StudyValidationError(
                    f"{field_name}: finite search values must be a non-empty JSON array"
                )
            normalized = [
                domain_value(
                    field_name=field_name,
                    raw=_canonical_json_text(value),
                    schema=schema,
                    parameter_schema=parameter_schema,
                    parameter_name=name,
                )
                for value in values
            ]
            identities = [_canonical_json_text(value) for value in normalized]
            if len(set(identities)) != len(identities):
                raise StudyValidationError(
                    f"{field_name}: finite search values must be unique"
                )
            search_space[path] = {"values": normalized}
            continue

        if kind == "categorical":
            field_name = f"{prefix}choices"
            raw = form.get(field_name, "").strip()
            try:
                choices = _json_text(raw, f"categorical choices for {slot}.{name}")
            except (SchemaValidationError, ValueError) as exc:
                raise StudyValidationError(f"{field_name}: {exc}") from exc
            if not isinstance(choices, list) or not choices:
                raise StudyValidationError(
                    f"{field_name}: choices must be a non-empty JSON array"
                )
            normalized = [
                domain_value(
                    field_name=field_name,
                    raw=_canonical_json_text(value),
                    schema=schema,
                    parameter_schema=parameter_schema,
                    parameter_name=name,
                )
                for value in choices
            ]
            identities = [_canonical_json_text(value) for value in normalized]
            if len(set(identities)) != len(identities):
                raise StudyValidationError(f"{field_name}: choices must be unique")
            search_space[path] = {"kind": "categorical", "choices": normalized}
            continue

        low_field = f"{prefix}low"
        high_field = f"{prefix}high"
        step_field = f"{prefix}step"
        log_field = f"{prefix}log"
        cast = int if kind == "int" else float
        low = domain_value(
            field_name=low_field,
            raw=form.get(low_field, ""),
            schema=schema,
            parameter_schema=parameter_schema,
            parameter_name=name,
            cast=cast,
        )
        high = domain_value(
            field_name=high_field,
            raw=form.get(high_field, ""),
            schema=schema,
            parameter_schema=parameter_schema,
            parameter_name=name,
            cast=cast,
        )
        if high < low:
            raise StudyValidationError(
                f"{high_field}: high must be greater than or equal to low"
            )
        raw_log = form.get(log_field, "")
        if raw_log not in {"", "true"}:
            raise StudyValidationError(f"{log_field}: log must be true when selected")
        log = raw_log == "true"
        raw_step = form.get(step_field, "").strip()
        if raw_step and log:
            raise StudyValidationError(
                f"{step_field}: step and log cannot be used together"
            )
        definition: dict[str, Any] = {
            "kind": kind,
            "low": low,
            "high": high,
        }
        if kind == "int" and not log:
            raw_step = raw_step or "1"
        if raw_step:
            try:
                step = cast(raw_step)
            except ValueError as exc:
                raise StudyValidationError(
                    f"{step_field}: step must be a {schema['type']}"
                ) from exc
            if step <= 0:
                raise StudyValidationError(f"{step_field}: step must be greater than zero")
            definition["step"] = step
        definition["log"] = log
        search_space[path] = definition
    if not search_space:
        raise StudyValidationError(
            "study-form: at least one eligible parameter must be selected"
        )

    def integer(name: str, default: str) -> int:
        raw = form.get(name, default)
        try:
            return int(raw)
        except ValueError as exc:
            raise StudyValidationError(f"{name} must be an integer") from exc

    task["search"] = {
        "suggester": suggester,
        "suggester_version": "1.0.0",
        "seed": integer("seed", "17"),
        "unique_trial_budget": integer("unique_trial_budget", "1"),
        "max_suggestions": integer("max_suggestions", "1"),
        "space": search_space,
    }
    task["validation"] = {
        "outer_folds": integer("outer_folds", "1"),
        "inner_folds": integer("inner_folds", "1"),
        "scoring_sessions": integer("scoring_sessions", "1"),
        "minimum_training_sessions": integer("minimum_training_sessions", "2"),
        "purge_sessions": integer("purge_sessions", "0"),
        "outer_account_policy": "FORCE_FLAT_WITH_COST",
    }
    task["evaluation"] = {
        "policy_id": "robust_walk_forward",
        "version": form.get("evaluation_version", "latest"),
        "parameters": {},
    }
    task["holdout"] = {
        "sessions": integer("holdout_sessions", "1"),
        "pass_rule": "POLICY_CONSTRAINTS",
    }
    parents = [
        value.strip()
        for value in form.get("parent_study_ids", "").splitlines()
        if value.strip()
    ]
    task["lineage"] = {
        "parent_study_ids": parents,
        "prior_unique_candidate_count": integer(
            "prior_unique_candidate_count", "0"
        ),
        "is_complete": form.get("lineage_complete", "") == "true",
    }
    return task


def _verified_run_payloads(
    settings: Settings, attempt: dict[str, Any]
) -> dict[str, bytes]:
    if attempt["status"] != "SUCCEEDED" or not attempt["result_path"]:
        raise FileNotFoundError("attempt has no successful report")
    allowed_root = settings.state_root.absolute() / "experiment-runs"
    run_dir = Path(attempt["result_path"]).absolute()
    if run_dir.parent != allowed_root or run_dir.is_symlink() or not run_dir.is_dir():
        raise FileNotFoundError("report run path is outside the result store")
    if stat.S_IMODE(run_dir.stat().st_mode) & 0o222:
        raise FileNotFoundError("report run directory is not sealed")
    if {path.name for path in run_dir.iterdir()} != ARTIFACT_NAMES:
        raise FileNotFoundError("report run artifact set is invalid")
    payloads: dict[str, bytes] = {}
    for path in run_dir.iterdir():
        metadata = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o222
        ):
            raise FileNotFoundError("report artifact is not immutable")
        payloads[path.name] = path.read_bytes()
    manifest = json.loads(
        payloads["run_manifest.json"],
        object_pairs_hook=_strict_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite run manifest value: {value}")
        ),
    )
    if (
        manifest.get("run_id") != run_dir.name
        or set(manifest.get("files", {})) != HASHED_ARTIFACT_NAMES
    ):
        raise FileNotFoundError("report run manifest identity is invalid")
    for name, expected in manifest["files"].items():
        payload = payloads[name]
        if expected != {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }:
            raise FileNotFoundError(f"report run artifact checksum mismatch: {name}")
    config = json.loads(
        payloads["config.json"],
        object_pairs_hook=_strict_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite run configuration value: {value}")
        ),
    )
    if (
        hashlib.sha256(canonical_json_bytes(config)).hexdigest()
        != manifest["config_sha256"]
    ):
        raise FileNotFoundError("report run configuration binding is invalid")
    resolved_dataset = attempt["resolved"]["dataset"]
    if manifest["dataset_snapshot_id"] != resolved_dataset["snapshot_id"]:
        raise FileNotFoundError("report run dataset binding is invalid")
    snapshot_dir = (
        settings.state_root
        / "datasets"
        / resolved_dataset["instrument"]
        / resolved_dataset["snapshot_id"]
    )
    snapshot_manifest = _verify_snapshot(
        snapshot_dir, resolved_dataset["snapshot_id"]
    )
    if (
        snapshot_manifest["canonical_sha256"]
        != resolved_dataset["canonical_sha256"]
    ):
        raise FileNotFoundError("report dataset canonical binding is invalid")
    return payloads


def _report_payload(settings: Settings, attempt: dict[str, Any]) -> bytes:
    return _verified_run_payloads(settings, attempt)["report.html"]


def create_app(
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    settings = settings.validated()
    catalog = initialize_catalog(settings.state_root)
    datasets = DatasetService(catalog)
    experiments = ExperimentService(
        catalog,
        execution_identity=effective_execution_identity(
            settings.project_root, settings.runner_image
        ),
        datasets=datasets,
    )
    experiments.recover_abandoned_attempts()
    studies = ParameterStudy.from_experiments(
        catalog,
        experiments=experiments,
        release_locator=str(settings.project_root or settings.state_root),
    )
    auth = AuthManager(catalog, settings, **({"clock": clock} if clock else {}))
    operators = OperatorService(catalog, runner_image=settings.runner_image)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.datasets = datasets
    app.state.experiments = experiments
    app.state.studies = studies
    app.state.operators = operators
    app.state.auth = auth
    app.mount(
        "/static",
        StaticFiles(directory=PACKAGE_ROOT / "static"),
        name="static",
    )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        is_api = request.url.path.startswith("/api/")
        try:
            auth.verify_request_origin(
                host=request.headers.get("host", ""),
                origin=(
                    None
                    if request.url.path == "/auth/callback"
                    else request.headers.get("origin")
                ),
                mutation=(
                    request.method not in {"GET", "HEAD", "OPTIONS"}
                    and request.url.path != "/auth/callback"
                ),
            )
        except AuthError as exc:
            if is_api:
                is_host = "host" in str(exc).lower()
                return _json_error(
                    400 if is_host else 403,
                    "BAD_HOST" if is_host else "REQUEST_FORBIDDEN",
                    str(exc),
                )
            return HTMLResponse("Invalid request origin.", status_code=403)
        response = await call_next(request)
        requested_theme = request.query_params.get("theme")
        if requested_theme in THEMES:
            response.set_cookie(
                THEME_COOKIE,
                requested_theme,
                max_age=31_536_000,
                secure=settings.secure_cookies,
                httponly=False,
                samesite="lax",
                path="/",
            )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'self'",
        )
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.exception_handler(TaskValidationError)
    @app.exception_handler(StudyValidationError)
    @app.exception_handler(OperatorSubmissionError)
    async def domain_error(request: Request, exc: Exception):
        if request.url.path.startswith("/api/"):
            return _json_error(400, "DOMAIN_ERROR", str(exc))
        try:
            session = _session(request)
        except AuthError:
            return RedirectResponse("/login", status_code=303)
        return _render(
            request,
            "error.html",
            session=session,
            status_code=400,
            message=str(exc),
        )

    @app.exception_handler(StudyNotFoundError)
    async def study_not_found(request: Request, exc: StudyNotFoundError):
        if request.url.path.startswith("/api/"):
            return _json_error(404, "NOT_FOUND", str(exc))
        try:
            session = _session(request)
        except AuthError:
            return RedirectResponse("/login", status_code=303)
        return _render(
            request,
            "error.html",
            session=session,
            status_code=404,
            message=str(exc),
        )

    @app.exception_handler(AuthError)
    async def authentication_error(request: Request, exc: AuthError):
        if request.url.path.startswith("/api/"):
            code = "AUTH_REQUIRED" if str(exc) == "Authentication required" else "FORBIDDEN"
            return _json_error(401 if code == "AUTH_REQUIRED" else 403, code, str(exc))
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(ValueError)
    async def invalid_request(request: Request, exc: ValueError):
        if request.url.path.startswith("/api/"):
            return _json_error(400, "INVALID_REQUEST", str(exc))
        try:
            session = _session(request)
        except AuthError:
            return RedirectResponse("/login", status_code=303)
        return _render(
            request,
            "error.html",
            session=session,
            status_code=400,
            message=str(exc),
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/login")
    async def login(request: Request):
        login_url = settings.sso_login_url + "?" + urlencode(
            {"redirect": settings.sso_callback_url, "audience": settings.sso_audience}
        )
        context = {
            "login_url": login_url,
            "auth_mode": settings.auth_mode,
            "login_csrf": None,
            "theme": _theme(request),
        }
        response = TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context=context,
        )
        if settings.auth_mode == "password":
            issued = auth.issue_session(
                {"email": "__login__", "display_name": "Login"}
            )
            context["login_csrf"] = issued.csrf_token
            response = TEMPLATES.TemplateResponse(
                request=request,
                name="login.html",
                context=context,
            )
            response.set_cookie(
                SESSION_COOKIE,
                issued.cookie,
                max_age=600,
                secure=settings.secure_cookies,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return response

    @app.get("/auth/login")
    async def auth_login():
        return RedirectResponse(
            settings.sso_login_url
            + "?"
            + urlencode(
                {
                    "redirect": settings.sso_callback_url,
                    "audience": settings.sso_audience,
                }
            ),
            status_code=303,
        )

    @app.post("/auth/callback")
    async def auth_callback(request: Request):
        try:
            form = await _form_body(request)
            if set(form) != {"token"}:
                raise AuthError("authentication callback fields are invalid")
            user = auth.authenticate_sso(
                form["token"],
                remote_address=request.client.host if request.client else "unknown",
            )
            issued = auth.issue_session(user)
        except (AuthError, ValueError) as exc:
            return _json_error(401, "AUTH_FAILED", str(exc))
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            issued.cookie,
            max_age=3600,
            secure=settings.secure_cookies,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/auth/password")
    async def auth_password(request: Request):
        if settings.auth_mode != "password":
            return _json_error(404, "NOT_FOUND", "Password login is not enabled")
        form = await _form_body(request)
        if set(form) != {"password", "login_csrf"}:
            return _json_error(400, "INVALID_REQUEST", "Password login fields are invalid")
        cookie = request.cookies.get(SESSION_COOKIE, "")
        try:
            login_session = auth.verify_session(cookie)
            if login_session.email != "__login__":
                raise AuthError("password login session is invalid")
            auth.verify_csrf(login_session, form["login_csrf"])
            user = await run_in_threadpool(
                auth.authenticate_password,
                form["password"],
                remote_address=request.client.host if request.client else "unknown",
            )
        except AuthError as exc:
            return _json_error(401, "AUTH_FAILED", str(exc))
        issued = auth.issue_session(user)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            issued.cookie,
            max_age=3600,
            secure=settings.secure_cookies,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/operators")
    async def api_operators(request: Request):
        _session(request)
        return {"operators": operators.list()}

    @app.get("/api/datasets")
    async def api_datasets(request: Request):
        _session(request)
        return {
            "datasets": await run_in_threadpool(datasets.list_available)
        }

    @app.get("/api/datasets/{dataset_id}/snapshots/{snapshot_id}")
    async def api_dataset_detail(request: Request, dataset_id: str, snapshot_id: str):
        _session(request)
        try:
            detail = await run_in_threadpool(
                datasets.snapshot_detail, dataset_id, snapshot_id
            )
        except DatasetResolutionError as exc:
            return _json_error(404, "NOT_FOUND", str(exc))
        return {"dataset": detail}

    @app.get("/api/operators/{operator_id}")
    async def api_operator(request: Request, operator_id: str, version: str | None = None):
        _session(request)
        try:
            return {
                "operator": operators.detail(operator_id, version),
                "versions": operators.list_versions(operator_id),
            }
        except ValueError as exc:
            return _json_error(404, "NOT_FOUND", str(exc))

    @app.post("/api/operators")
    async def api_operator_submit(request: Request):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        result = await run_in_threadpool(operators.submit, body)
        return JSONResponse(
            result, status_code=201 if result["status"] == "CREATED" else 200
        )

    @app.get("/api/templates/{name}/{version}")
    async def api_template(request: Request, name: str, version: str):
        _session(request)
        try:
            return {"template": catalog.template_detail(name, version)}
        except ValueError as exc:
            return _json_error(404, "NOT_FOUND", str(exc))

    @app.post("/api/tasks/resolve")
    async def api_resolve(request: Request):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if not isinstance(body, dict) or set(body) != {"task"}:
            return _json_error(400, "INVALID_REQUEST", "Expected exactly task")
        return {
            "resolved": await run_in_threadpool(
                experiments.resolve_task, body["task"]
            )
        }

    @app.post("/api/experiments/preview")
    async def api_preview(request: Request):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if not isinstance(body, dict) or set(body) != {"task"}:
            return _json_error(400, "INVALID_REQUEST", "Expected exactly task")
        return {
            "preview": await run_in_threadpool(
                experiments.preview_task, body["task"]
            )
        }

    @app.post("/api/experiments")
    async def api_submit(request: Request):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if not isinstance(body, dict) or set(body) != {"task", "action_id"}:
            return _json_error(
                400, "INVALID_REQUEST", "Expected exactly task and action_id"
            )
        result = await run_in_threadpool(
            experiments.submit, body["task"], action_id=body["action_id"]
        )
        return JSONResponse(result, status_code=201 if result["status"] == "CREATED" else 200)

    @app.post("/api/experiments/{experiment_id}/rerun")
    async def api_rerun(request: Request, experiment_id: str):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if not isinstance(body, dict) or set(body) != {"action_id"}:
            return _json_error(400, "INVALID_REQUEST", "Expected exactly action_id")
        result = await run_in_threadpool(
            experiments.rerun, experiment_id, action_id=body["action_id"]
        )
        return JSONResponse(result, status_code=201 if result["status"] == "CREATED" else 200)

    @app.get("/api/experiments")
    async def api_experiments(request: Request):
        _session(request)
        return {"experiments": experiments.list_experiments()}

    @app.get("/api/experiments/{experiment_id}")
    async def api_experiment(request: Request, experiment_id: str):
        _session(request)
        return {"experiment": experiments.experiment_detail(experiment_id)}

    @app.get("/api/experiments/{experiment_id}/attempts")
    async def api_attempts(request: Request, experiment_id: str):
        _session(request)
        return {"attempts": experiments.list_attempts(experiment_id)}

    @app.get("/api/attempts/{attempt_id}")
    async def api_attempt(request: Request, attempt_id: str):
        _session(request)
        try:
            return {"attempt": experiments.attempt_detail(attempt_id)}
        except TaskValidationError as exc:
            return _json_error(404, "NOT_FOUND", str(exc))

    @app.post("/api/attempts/{attempt_id}/recover")
    async def api_attempt_recover(request: Request, attempt_id: str):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if not isinstance(body, dict) or set(body) != {"action_id"}:
            return _json_error(400, "INVALID_REQUEST", "Expected exactly action_id")
        result = await run_in_threadpool(
            experiments.create_replacement_attempt,
            attempt_id,
            action_id=body["action_id"],
        )
        return JSONResponse(
            result, status_code=201 if result["status"] == "CREATED" else 200
        )

    @app.get("/api/studies")
    async def api_studies(request: Request):
        _session(request)
        return {"studies": await run_in_threadpool(studies.list)}

    @app.post("/api/studies/preview")
    async def api_study_preview(request: Request):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or set(body) != {"study"}:
            return _json_error(400, "INVALID_REQUEST", "Expected exactly study")
        return {
            "preview": await run_in_threadpool(studies.preview, body["study"])
        }

    @app.post("/api/studies")
    async def api_study_submit(request: Request):
        session = _session(request)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or set(body) != {
            "study",
            "expected_preview_digest",
            "action_id",
        }:
            return _json_error(
                400,
                "INVALID_REQUEST",
                "Expected exactly study, expected_preview_digest, and action_id",
            )
        result = await run_in_threadpool(
            studies.submit,
            body["study"],
            expected_preview_digest=body["expected_preview_digest"],
            action_id=body["action_id"],
        )
        return JSONResponse(
            result,
            status_code=201 if result["status"] == "SUBMITTED" else 200,
        )

    @app.get("/api/studies/{study_id}")
    async def api_study(request: Request, study_id: str):
        _session(request)
        study_id = _study_id(study_id)
        return {"study": await run_in_threadpool(studies.detail, study_id)}

    @app.post("/api/studies/{study_id}/advance")
    async def api_study_advance(request: Request, study_id: str):
        session = _session(request)
        study_id = _study_id(study_id)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or body:
            return _json_error(400, "INVALID_REQUEST", "Expected an empty object")
        result = await run_in_threadpool(studies.advance, study_id)
        return JSONResponse(result)

    @app.post("/api/studies/{study_id}/control")
    async def api_study_control(request: Request, study_id: str):
        session = _session(request)
        study_id = _study_id(study_id)
        _csrf(request, session)
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or set(body) != {"operation", "action_id"}:
            return _json_error(
                400,
                "INVALID_REQUEST",
                "Expected exactly operation and action_id",
            )
        result = await run_in_threadpool(
            studies.control,
            study_id,
            body["operation"],
            action_id=body["action_id"],
        )
        return JSONResponse(result)

    @app.get("/")
    async def dashboard(request: Request):
        try:
            session = _session(request)
        except AuthError:
            return RedirectResponse("/login", status_code=303)
        history = experiments.list_experiments()
        connection = catalog.connect()
        try:
            attempts = connection.execute(
                "SELECT * FROM attempts ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
            failures = connection.execute(
                """
                SELECT COUNT(*) FROM attempts
                WHERE status IN (
                    'FAILED', 'INTERRUPTED', 'TERMINATION_UNCONFIRMED'
                )
                """
            ).fetchone()[0]
        finally:
            connection.close()
        return _render(
            request,
            "dashboard.html",
            session=session,
            experiment_count=len(history),
            attempt_count=sum(item["attempt_count"] for item in history),
            operator_count=len(operators.list()),
            failure_count=failures,
            attempts=[dict(row) for row in attempts],
        )

    @app.get("/datasets/{dataset_id}/snapshots/{snapshot_id}")
    async def dataset_detail(request: Request, dataset_id: str, snapshot_id: str):
        session = _session(request)
        try:
            detail = await run_in_threadpool(
                datasets.snapshot_detail, dataset_id, snapshot_id
            )
        except DatasetResolutionError as exc:
            return HTMLResponse(str(exc), status_code=404)
        return _render(
            request,
            "dataset_detail.html",
            session=session,
            dataset=detail,
        )

    @app.get("/operators")
    async def operator_list(request: Request):
        session = _session(request)
        grouped = _operator_groups(operators)
        return _render(
            request, "operators.html", session=session, grouped=grouped
        )

    @app.get("/operators/submit")
    async def operator_submit_form(request: Request):
        session = _session(request)
        return _render(
            request,
            "operator_submit.html",
            session=session,
            form_values={},
            errors=[],
            invalid_fields=set(),
        )

    @app.post("/operators/submit")
    async def operator_submit_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        try:
            submission = {
                "operator_id": form.get("operator_id", ""),
                "slot": form.get("slot", ""),
                "version": form.get("version", ""),
                "source": form.get("source", ""),
                "parameter_schema": _json_text(
                    form.get("parameter_schema", ""), "parameter_schema"
                ),
                "defaults": _json_text(form.get("defaults", ""), "defaults"),
                "title_zh": form.get("title_zh", ""),
                "summary_zh": form.get("summary_zh", ""),
                "documentation": form.get("documentation", ""),
                "tests": _json_text(form.get("tests", ""), "tests"),
            }
            result = await run_in_threadpool(operators.submit, submission)
        except (OperatorSubmissionError, ValueError) as exc:
            return _render(
                request,
                "operator_submit.html",
                session=session,
                status_code=400,
                form_values=form,
                **_form_error_context(
                    form, exc, fallback_field="operator-form"
                ),
            )
        return RedirectResponse(
            f"/operators/{result['operator_id']}/{result['version']}",
            status_code=303,
        )

    @app.get("/operators/{operator_id}/{version}")
    async def operator_detail(request: Request, operator_id: str, version: str):
        session = _session(request)
        detail = operators.detail(operator_id, version)
        versions = operators.list_versions(operator_id)
        latest = operators.detail(operator_id)
        return _render(
            request,
            "operator_detail.html",
            session=session,
            operator=detail,
            documentation=_safe_markdown(detail["documentation"]),
            versions=versions,
            latest=latest,
            is_latest=detail["version"] == latest["version"],
        )

    @app.get("/templates/{name}/{version}")
    async def template_detail(request: Request, name: str, version: str):
        session = _session(request)
        template = catalog.template_detail(name, version)
        grouped = _operator_groups(operators)
        return _render(
            request,
            "template_detail.html",
            session=session,
            template=template,
            slot_defaults={
                slot: grouped[slot][0] for slot in template["slots"]
            },
        )

    async def experiment_form_context(
        form_values: dict[str, str] | None = None,
        validation_error: Exception | None = None,
    ) -> dict[str, Any]:
        grouped = _operator_groups(operators)
        dataset_options = await run_in_threadpool(datasets.list_available)
        values = form_values or {}
        context = {
            "datasets": dataset_options,
            "grouped": grouped,
            "template": catalog.template_detail("single_stock_daily_causal", "1"),
            "action_id": values.get("action_id") or secrets.token_hex(16),
            "form_values": values,
            "errors": [],
            "error_messages": {},
            "invalid_fields": set(),
        }
        if validation_error is not None:
            context.update(
                _form_error_context(
                    values,
                    validation_error,
                    fallback_field="experiment-form",
                )
            )
        return context

    @app.get("/experiments/new")
    async def experiment_new(request: Request):
        session = _session(request)
        return _render(
            request,
            "experiment_new.html",
            session=session,
            **await experiment_form_context(),
        )

    @app.post("/experiments/preview")
    async def experiment_preview_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        try:
            task = _task_from_form(form, catalog=catalog)
            preview = await run_in_threadpool(experiments.preview_task, task)
        except (TaskValidationError, ValueError) as exc:
            return _render(
                request,
                "experiment_new.html",
                session=session,
                status_code=400,
                **await experiment_form_context(form, validation_error=exc),
            )
        return _render(
            request,
            "experiment_preview.html",
            session=session,
            preview=preview,
            form_values=form,
            theme_action="/experiments/preview",
            theme_form_values=form,
        )

    @app.post("/experiments/new")
    async def experiment_create_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        intent = form.pop("intent", None)
        if intent is not None:
            if intent != "edit":
                raise ValueError("experiment form intent is invalid")
            return _render(
                request,
                "experiment_new.html",
                session=session,
                **await experiment_form_context(form),
            )
        try:
            result = await run_in_threadpool(
                experiments.submit,
                _task_from_form(form, catalog=catalog),
                action_id=form.get("action_id") or secrets.token_hex(16),
            )
        except (TaskValidationError, ValueError) as exc:
            return _render(
                request,
                "experiment_new.html",
                session=session,
                status_code=400,
                **await experiment_form_context(form, validation_error=exc),
            )
        return RedirectResponse(
            f"/experiments/{result['experiment_id']}", status_code=303
        )

    @app.get("/history")
    async def history(request: Request):
        session = _session(request)
        status_filter = request.query_params.get("status", "all")
        drift_filter = request.query_params.get("drift", "all")
        search = request.query_params.get("search", "").strip()
        allowed_statuses = {
            "all",
            "PENDING",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "INTERRUPTED",
            "TERMINATION_UNCONFIRMED",
        }
        if status_filter not in allowed_statuses:
            raise ValueError("history status filter is invalid")
        if drift_filter not in {"all", "current", "drifted"}:
            raise ValueError("history drift filter is invalid")
        history_rows = experiments.list_experiments()
        if status_filter != "all":
            history_rows = [
                item
                for item in history_rows
                if item["current_status"] == status_filter
            ]
        if drift_filter != "all":
            expected_drift = drift_filter == "drifted"
            history_rows = [
                item for item in history_rows if item["has_drift"] is expected_drift
            ]
        if search:
            needle = search.casefold()
            history_rows = [
                item
                for item in history_rows
                if needle
                in " ".join(
                    (
                        item["experiment_id"],
                        item["dataset"]["instrument"],
                        item["dataset"]["snapshot_id"],
                        item["template"]["name"],
                        *(
                            operator["operator_id"]
                            for operator in item["operators"].values()
                        ),
                    )
                ).casefold()
            ]
        return _render(
            request,
            "history.html",
            session=session,
            experiments=history_rows,
            filters={
                "status": status_filter,
                "drift": drift_filter,
                "search": search,
            },
        )

    @app.get("/studies")
    async def study_list(request: Request):
        session = _session(request)
        return _render(
            request,
            "studies.html",
            session=session,
            studies=await run_in_threadpool(studies.list),
        )

    async def study_form_context(
        form_values: dict[str, str] | None = None,
        *,
        validation_error: Exception | None = None,
        creation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if creation is None:
            creation = await run_in_threadpool(studies.creation_options)
        grouped = _study_operator_groups(creation)
        dataset_options = creation["datasets"]
        template = creation["template"]
        default_search = None
        for slot in template["slots"]:
            if slot in {"cost", "report"} or not grouped.get(slot):
                continue
            operator = grouped[slot][0]
            properties = operator["parameter_schema"]["properties"]
            if properties:
                default_search = (
                    slot,
                    operator["operator_id"],
                    operator["version"],
                    next(iter(properties)),
                )
                break
        values = form_values or {}
        errors: list[dict[str, str]] = []
        if validation_error is not None:
            message = str(validation_error)
            field = next(
                (
                    name
                    for name in values
                    if message.startswith(name)
                    or f".{name}" in message
                ),
                "study-form",
            )
            errors.append({"field": field, "message": message})
        return {
            "datasets": dataset_options,
            "grouped": grouped,
            "template": template,
            "default_search": default_search,
            "action_id": values.get("action_id") or secrets.token_hex(16),
            "form_values": values,
            "errors": errors,
            "error_messages": {error["field"]: error["message"] for error in errors},
            "invalid_fields": {error["field"] for error in errors},
        }

    @app.get("/studies/new")
    async def study_new(request: Request):
        session = _session(request)
        return _render(
            request,
            "study_new.html",
            session=session,
            **await study_form_context(),
        )

    @app.post("/studies/preview")
    async def study_preview_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        creation = await run_in_threadpool(studies.creation_options)
        try:
            spec = _study_from_form(form, creation=creation)
            preview = await run_in_threadpool(studies.preview, spec)
        except (StudyValidationError, TaskValidationError, ValueError) as exc:
            return _render(
                request,
                "study_new.html",
                session=session,
                status_code=400,
                **await study_form_context(
                    form, validation_error=exc, creation=creation
                ),
            )
        return _render(
            request,
            "study_preview.html",
            session=session,
            preview=preview,
            study_json=_canonical_json_text(spec),
            wizard_json=_canonical_json_text(
                {key: value for key, value in form.items() if key != "csrf_token"}
            ),
            action_id=form.get("action_id") or secrets.token_hex(16),
            stale=False,
            theme_action="/studies/preview",
            theme_form_values=form,
        )

    @app.post("/studies/edit")
    async def study_edit_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        if set(form) != {"csrf_token", "wizard_json"}:
            raise StudyValidationError("Study edit form fields are invalid")
        _csrf(request, session, form["csrf_token"])
        values = _json_text(form["wizard_json"], "wizard_json")
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise StudyValidationError("wizard_json must contain form text values")
        return _render(
            request,
            "study_new.html",
            session=session,
            **await study_form_context(values),
        )

    @app.post("/studies")
    async def study_submit_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        if set(form) != {
            "csrf_token",
            "action_id",
            "expected_preview_digest",
            "study_json",
            "wizard_json",
        }:
            raise StudyValidationError("Study submission form fields are invalid")
        spec = _json_text(form["study_json"], "study_json")
        result = await run_in_threadpool(
            studies.submit,
            spec,
            expected_preview_digest=form["expected_preview_digest"],
            action_id=form["action_id"],
        )
        if result["status"] == "PREVIEW_STALE":
            preview = await run_in_threadpool(studies.preview, spec)
            return _render(
                request,
                "study_preview.html",
                session=session,
                preview=preview,
                study_json=_canonical_json_text(spec),
                wizard_json=form["wizard_json"],
                action_id=secrets.token_hex(16),
                stale=True,
                status_code=409,
                theme_action="/studies/preview",
                theme_form_values=_json_text(form["wizard_json"], "wizard_json")
                | {"csrf_token": form["csrf_token"]},
            )
        if "study_id" not in result:
            raise StudyValidationError(
                f"Study submission was not accepted: {result['status']}"
            )
        return RedirectResponse(
            f"/studies/{result['study_id']}", status_code=303
        )

    @app.post("/studies/{study_id}/advance")
    async def study_advance_action(request: Request, study_id: str):
        session = _session(request)
        study_id = _study_id(study_id)
        form = await _form_body(request)
        if set(form) != {"csrf_token"}:
            raise StudyValidationError("Study advance form fields are invalid")
        _csrf(request, session, form["csrf_token"])
        result = await run_in_threadpool(studies.advance, study_id)
        outcome = result.get("status", "")
        query = (
            urlencode(
                {
                    "status": _study_outcome_token(
                        settings.session_secret, session, study_id, outcome
                    )
                }
            )
            if outcome in STUDY_OUTCOMES
            else ""
        )
        location = f"/studies/{study_id}" + (f"?{query}" if query else "")
        return RedirectResponse(location, status_code=303)

    @app.post("/studies/{study_id}/control")
    async def study_control_action(request: Request, study_id: str):
        session = _session(request)
        study_id = _study_id(study_id)
        form = await _form_body(request)
        if set(form) != {"csrf_token", "operation", "action_id"}:
            raise StudyValidationError("Study control form fields are invalid")
        _csrf(request, session, form["csrf_token"])
        result = await run_in_threadpool(
            studies.control,
            study_id,
            form["operation"],
            action_id=form["action_id"],
        )
        outcome = result.get("status", "")
        query = (
            urlencode(
                {
                    "status": _study_outcome_token(
                        settings.session_secret, session, study_id, outcome
                    )
                }
            )
            if outcome in STUDY_OUTCOMES
            else ""
        )
        location = f"/studies/{study_id}" + (f"?{query}" if query else "")
        return RedirectResponse(location, status_code=303)

    @app.get("/studies/{study_id}/report")
    async def study_report(request: Request, study_id: str):
        session = _session(request)
        study_id = _study_id(study_id)
        detail = await run_in_threadpool(studies.detail, study_id)
        return _render(
            request,
            "study_report.html",
            session=session,
            study=detail,
        )

    @app.get("/studies/{study_id}")
    async def study_detail(request: Request, study_id: str):
        session = _session(request)
        study_id = _study_id(study_id)
        detail = await run_in_threadpool(studies.detail, study_id)
        outcome = _study_outcome(
            settings.session_secret,
            session,
            study_id,
            request.query_params.get("status", ""),
        )
        return _render(
            request,
            "study_detail.html",
            session=session,
            study=detail,
            control_action_id=secrets.token_hex(16),
            outcome_message=STUDY_OUTCOMES.get(outcome),
        )

    @app.get("/experiments/{experiment_id}")
    async def experiment_detail(request: Request, experiment_id: str):
        session = _session(request)
        detail = experiments.experiment_detail(experiment_id)
        canonical_attempt = next(
            (
                attempt
                for attempt in detail["attempts"]
                if attempt["attempt_id"] == detail["canonical_attempt_id"]
            ),
            None,
        )
        canonical_metrics = None
        if canonical_attempt is not None:
            try:
                payloads = await run_in_threadpool(
                    _verified_run_payloads, settings, canonical_attempt
                )
                canonical_metrics = json.loads(payloads["metrics.json"])
            except (KeyError, OSError, RuntimeError, ValueError):
                canonical_metrics = None
        return _render(
            request,
            "experiment_detail.html",
            session=session,
            experiment=detail,
            report_attempt=next(
                (
                    attempt
                    for attempt in detail["attempts"]
                    if attempt["status"] == "SUCCEEDED"
                ),
                None,
            ),
            canonical_attempt=canonical_attempt,
            canonical_metrics=canonical_metrics,
            rerun_action_id=secrets.token_hex(16),
        )

    @app.post("/experiments/{experiment_id}/rerun")
    async def experiment_rerun_action(request: Request, experiment_id: str):
        session = _session(request)
        form = await _form_body(request)
        if set(form) != {"csrf_token", "action_id"}:
            raise TaskValidationError("rerun form fields are invalid")
        _csrf(request, session, form.get("csrf_token"))
        result = await run_in_threadpool(
            experiments.rerun, experiment_id, action_id=form["action_id"]
        )
        return RedirectResponse(
            f"/experiments/{result['experiment_id']}", status_code=303
        )

    @app.get("/reports/{attempt_id}")
    async def report(request: Request, attempt_id: str):
        session = _session(request)
        attempt = experiments.attempt_detail(attempt_id)
        experiment = experiments.experiment_detail(attempt["experiment_id"])
        is_canonical = experiment["canonical_attempt_id"] == attempt_id
        report_label = (
            "Verified canonical report"
            if is_canonical
            else "Verified divergent rerun report"
            if attempt.get("comparison") == "DIVERGENT"
            else "Verified equivalent rerun report"
        )
        return _render(
            request,
            "report_wrapper.html",
            session=session,
            attempt_id=attempt_id,
            attempt=attempt,
            report_label=report_label,
        )

    @app.get("/reports/{attempt_id}/content")
    async def report_content(request: Request, attempt_id: str):
        _session(request)
        if (
            request.headers.get("sec-fetch-dest") != "iframe"
            or request.headers.get("sec-fetch-site") not in {"same-origin", "same-site"}
        ):
            return HTMLResponse("Report content requires a same-site sandbox frame.", status_code=403)
        try:
            payload = _report_payload(
                settings, experiments.attempt_detail(attempt_id)
            )
        except (FileNotFoundError, KeyError, OSError, RuntimeError, ValueError):
            return HTMLResponse("Report not found.", status_code=404)
        return Response(
            payload,
            media_type="text/html",
            headers={
                "Content-Security-Policy": (
                    "sandbox allow-scripts; default-src 'none'; "
                    "connect-src 'none'; img-src data:; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "form-action 'none'; base-uri 'none'; frame-ancestors 'self'; "
                    "navigate-to 'none'"
                ),
                "Content-Disposition": "inline",
            },
        )

    return app


def _run_workers_once(study_worker: Any, attempt_worker: Any) -> bool:
    progressed = False
    for name, worker in (("Study", study_worker), ("Attempt", attempt_worker)):
        try:
            progressed = worker.run_once() or progressed
        except Exception:
            LOGGER.exception("%s worker tick failed", name)
    return progressed


def main() -> None:
    import uvicorn

    from .resolved_runner import ResolvedAttemptExecutor
    from .worker import SerialAttemptWorker, SerialStudyWorker

    settings = Settings.from_environment()
    application = create_app(settings)
    executor = ResolvedAttemptExecutor(
        application.state.catalog,
        output_root=settings.state_root / "experiment-runs",
        project_root=settings.project_root,
        runner_image=settings.runner_image,
        attempt_controller=application.state.experiments,
    )
    study_worker = SerialStudyWorker(application.state.studies)
    attempt_worker = SerialAttemptWorker(
        application.state.experiments,
        executor=executor,
    )

    def run_worker() -> None:
        while True:
            if not _run_workers_once(study_worker, attempt_worker):
                time.sleep(1)

    threading.Thread(
        target=run_worker,
        name="quant-platform-worker",
        daemon=True,
    ).start()
    uvicorn.run(
        application,
        host="127.0.0.1",
        port=8090,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("QUANT_FORWARDED_ALLOW_IPS", ""),
    )


if __name__ == "__main__":
    main()
