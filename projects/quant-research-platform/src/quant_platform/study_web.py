from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from .auth import SessionData
from .experiment_service import TaskValidationError
from .lightweight_study import LightweightStudyNotFound, LightweightStudyService
from .parameter_study import ParameterStudy, StudyNotFoundError, StudyValidationError
from .settings import Settings
from .study_remote import StudyRemoteError

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
STUDY_ROUTE_INVENTORY = (
    ("GET", "/api/studies"),
    ("POST", "/api/lightweight-studies"),
    ("GET", "/api/lightweight-studies/{study_id}"),
    ("POST", "/api/studies/preview"),
    ("POST", "/api/studies"),
    ("GET", "/api/studies/{study_id}"),
    ("POST", "/api/studies/{study_id}/advance"),
    ("POST", "/api/studies/{study_id}/control"),
    ("GET", "/studies"),
    ("GET", "/studies/new"),
    ("GET", "/studies/new/lightweight"),
    ("GET", "/studies/new/legacy"),
    ("POST", "/studies/lightweight"),
    ("POST", "/studies/lightweight/msft"),
    ("GET", "/studies/lightweight/{study_id}"),
    ("POST", "/studies/preview"),
    ("POST", "/studies/edit"),
    ("POST", "/studies"),
    ("POST", "/studies/{study_id}/advance"),
    ("POST", "/studies/{study_id}/control"),
    ("GET", "/studies/{study_id}/report"),
    ("GET", "/studies/{study_id}"),
)


class CsrfVerifier(Protocol):
    def __call__(
        self,
        request: Request,
        session: SessionData,
        token: str | None = None,
    ) -> None: ...


def _study_id(value: str) -> str:
    if STUDY_ID.fullmatch(value) is None:
        raise StudyNotFoundError(f"unknown Parameter Study: {value}")
    return value


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


def register_study_routes(
    app: FastAPI,
    *,
    settings: Settings,
    studies: ParameterStudy,
    get_lightweight_studies: Callable[[], LightweightStudyService | None],
    session_for: Callable[[Request], SessionData],
    verify_csrf: CsrfVerifier,
    load_json_body: Callable[[Request], Awaitable[Any]],
    load_form_body: Callable[[Request], Awaitable[dict[str, str]]],
    render: Callable[..., Response],
    json_error: Callable[[int, str, str], JSONResponse],
    json_text: Callable[[str, str], Any],
    canonical_json_text: Callable[[Any], str],
    study_from_form: Callable[..., dict[str, Any]],
    study_operator_groups: Callable[[dict[str, Any]], dict[str, list[dict[str, Any]]]],
) -> None:
    """Register the complete Study API and HTML route family."""

    def lightweight_service() -> LightweightStudyService:
        service = get_lightweight_studies()
        if service is None:
            raise StudyRemoteError("Lightweight Study training is not enabled")
        return service

    def lightweight_summaries(cursor: str | None = None) -> dict[str, Any] | None:
        service = get_lightweight_studies()
        return None if service is None else service.list_summaries(cursor=cursor)

    @app.get("/api/studies")
    async def api_studies(request: Request):
        session_for(request)
        legacy = await run_in_threadpool(studies.list)
        lightweight_page = await run_in_threadpool(
            lightweight_summaries, request.query_params.get("cursor")
        )
        if lightweight_page is None:
            return json_error(
                503,
                "LIGHTWEIGHT_STUDY_LIST_UNAVAILABLE",
                "Lightweight Study authority is unavailable",
            )
        return {
            "studies": legacy,
            "lightweight_studies": lightweight_page["studies"],
            "lightweight_studies_next_cursor": lightweight_page["next_cursor"],
        }

    @app.post("/api/lightweight-studies")
    async def api_lightweight_study_submit(request: Request):
        session = session_for(request)
        verify_csrf(request, session)
        try:
            body = await load_json_body(request)
        except ValueError as exc:
            return json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or set(body) != {"action_id", "trial_budget"}:
            return json_error(
                400, "INVALID_REQUEST", "Expected exactly action_id and trial_budget"
            )
        try:
            study = await run_in_threadpool(
                lightweight_service().submit,
                action_id=body["action_id"],
                trial_budget=body["trial_budget"],
            )
        except StudyRemoteError as exc:
            return json_error(400, "LIGHTWEIGHT_STUDY_REJECTED", str(exc))
        return JSONResponse({"study": study}, status_code=201)

    @app.get("/api/lightweight-studies/{study_id}")
    async def api_lightweight_study_detail(request: Request, study_id: str):
        session_for(request)
        try:
            study = await run_in_threadpool(lightweight_service().detail, study_id)
        except LightweightStudyNotFound as exc:
            return json_error(404, "NOT_FOUND", str(exc))
        except StudyRemoteError as exc:
            return json_error(400, "LIGHTWEIGHT_STUDY_REJECTED", str(exc))
        return {"study": study}

    @app.post("/api/studies/preview")
    async def api_study_preview(request: Request):
        session = session_for(request)
        verify_csrf(request, session)
        try:
            body = await load_json_body(request)
        except ValueError as exc:
            return json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or set(body) != {"study"}:
            return json_error(400, "INVALID_REQUEST", "Expected exactly study")
        return {"preview": await run_in_threadpool(studies.preview, body["study"])}

    @app.post("/api/studies")
    async def api_study_submit(request: Request):
        session = session_for(request)
        verify_csrf(request, session)
        try:
            body = await load_json_body(request)
        except ValueError as exc:
            return json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or set(body) != {
            "study",
            "expected_preview_digest",
            "action_id",
        }:
            return json_error(
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
        session_for(request)
        study_id = _study_id(study_id)
        return {"study": await run_in_threadpool(studies.detail, study_id)}

    @app.post("/api/studies/{study_id}/advance")
    async def api_study_advance(request: Request, study_id: str):
        session = session_for(request)
        study_id = _study_id(study_id)
        verify_csrf(request, session)
        try:
            body = await load_json_body(request)
        except ValueError as exc:
            return json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or body:
            return json_error(400, "INVALID_REQUEST", "Expected an empty object")
        result = await run_in_threadpool(studies.advance, study_id)
        return JSONResponse(result)

    @app.post("/api/studies/{study_id}/control")
    async def api_study_control(request: Request, study_id: str):
        session = session_for(request)
        study_id = _study_id(study_id)
        verify_csrf(request, session)
        try:
            body = await load_json_body(request)
        except ValueError as exc:
            return json_error(400, "INVALID_JSON", str(exc))
        if type(body) is not dict or set(body) != {"operation", "action_id"}:
            return json_error(
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

    @app.get("/studies")
    async def study_list(request: Request):
        session = session_for(request)
        legacy = await run_in_threadpool(studies.list_summaries)
        lightweight_page = await run_in_threadpool(
            lightweight_summaries, request.query_params.get("cursor")
        )
        lightweight = None if lightweight_page is None else lightweight_page["studies"]
        next_cursor = None if lightweight_page is None else lightweight_page["next_cursor"]
        return render(
            request,
            "studies.html",
            session=session,
            studies=legacy,
            lightweight_studies=lightweight,
            lightweight_next_href=(
                None if next_cursor is None else f"/studies?{urlencode({'cursor': next_cursor})}"
            ),
        )

    async def study_form_context(
        form_values: dict[str, str] | None = None,
        *,
        validation_error: Exception | None = None,
        creation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if creation is None:
            creation = await run_in_threadpool(studies.creation_options)
        grouped = study_operator_groups(creation)
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
                    if message.startswith(name) or f".{name}" in message
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
        session = session_for(request)
        return render(
            request,
            "study_mode.html",
            session=session,
            lightweight_available=get_lightweight_studies() is not None,
        )

    @app.get("/studies/new/lightweight")
    async def lightweight_study_new(request: Request):
        session = session_for(request)
        lightweight_service()
        return render(
            request,
            "lightweight_study_new.html",
            session=session,
            action_id=secrets.token_hex(16),
            trial_budget=32,
        )

    @app.get("/studies/new/legacy")
    async def legacy_study_new(request: Request):
        session = session_for(request)
        return render(
            request,
            "study_new.html",
            session=session,
            **await study_form_context(),
        )

    @app.post("/studies/lightweight")
    async def lightweight_study_submit(request: Request):
        session = session_for(request)
        form = await load_form_body(request)
        if set(form) != {"csrf_token", "action_id", "trial_budget"}:
            raise StudyRemoteError("Lightweight Study submission fields are invalid")
        verify_csrf(request, session, form["csrf_token"])
        try:
            trial_budget = int(form["trial_budget"])
        except ValueError as exc:
            raise StudyRemoteError("trial_budget must be an integer") from exc
        study = await run_in_threadpool(
            lightweight_service().submit,
            action_id=form["action_id"],
            trial_budget=trial_budget,
        )
        return RedirectResponse(
            f"/studies/lightweight/{study['study_id']}", status_code=303
        )

    @app.post("/studies/lightweight/msft")
    async def msft_study_submit(request: Request):
        session = session_for(request)
        form = await load_form_body(request)
        if set(form) != {"csrf_token", "action_id", "snapshot_id"}:
            raise StudyRemoteError("MSFT Study submission fields are invalid")
        verify_csrf(request, session, form["csrf_token"])
        study = await run_in_threadpool(
            lightweight_service().submit_msft,
            action_id=form["action_id"],
            snapshot_id=form["snapshot_id"],
        )
        return RedirectResponse(
            f"/studies/lightweight/{study['study_id']}", status_code=303
        )

    @app.get("/studies/lightweight/{study_id}")
    async def lightweight_study_detail(request: Request, study_id: str):
        session = session_for(request)
        study_id = _study_id(study_id)
        study = await run_in_threadpool(lightweight_service().detail, study_id)
        return render(
            request,
            "lightweight_study_detail.html",
            session=session,
            study=study,
        )

    @app.post("/studies/preview")
    async def study_preview_action(request: Request):
        session = session_for(request)
        form = await load_form_body(request)
        verify_csrf(request, session, form.get("csrf_token"))
        creation = await run_in_threadpool(studies.creation_options)
        try:
            spec = study_from_form(form, creation=creation)
            preview = await run_in_threadpool(studies.preview, spec)
        except (StudyValidationError, TaskValidationError, ValueError) as exc:
            return render(
                request,
                "study_new.html",
                session=session,
                status_code=400,
                **await study_form_context(
                    form, validation_error=exc, creation=creation
                ),
            )
        return render(
            request,
            "study_preview.html",
            session=session,
            preview=preview,
            study_json=canonical_json_text(spec),
            wizard_json=canonical_json_text(
                {key: value for key, value in form.items() if key != "csrf_token"}
            ),
            action_id=form.get("action_id") or secrets.token_hex(16),
            stale=False,
            theme_action="/studies/preview",
            theme_form_values=form,
        )

    @app.post("/studies/edit")
    async def study_edit_action(request: Request):
        session = session_for(request)
        form = await load_form_body(request)
        if set(form) != {"csrf_token", "wizard_json"}:
            raise StudyValidationError("Study edit form fields are invalid")
        verify_csrf(request, session, form["csrf_token"])
        values = json_text(form["wizard_json"], "wizard_json")
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise StudyValidationError("wizard_json must contain form text values")
        return render(
            request,
            "study_new.html",
            session=session,
            **await study_form_context(values),
        )

    @app.post("/studies")
    async def study_submit_action(request: Request):
        session = session_for(request)
        form = await load_form_body(request)
        verify_csrf(request, session, form.get("csrf_token"))
        if set(form) != {
            "csrf_token",
            "action_id",
            "expected_preview_digest",
            "study_json",
            "wizard_json",
        }:
            raise StudyValidationError("Study submission form fields are invalid")
        spec = json_text(form["study_json"], "study_json")
        result = await run_in_threadpool(
            studies.submit,
            spec,
            expected_preview_digest=form["expected_preview_digest"],
            action_id=form["action_id"],
        )
        if result["status"] == "PREVIEW_STALE":
            preview = await run_in_threadpool(studies.preview, spec)
            return render(
                request,
                "study_preview.html",
                session=session,
                preview=preview,
                study_json=canonical_json_text(spec),
                wizard_json=form["wizard_json"],
                action_id=secrets.token_hex(16),
                stale=True,
                status_code=409,
                theme_action="/studies/preview",
                theme_form_values=json_text(form["wizard_json"], "wizard_json")
                | {"csrf_token": form["csrf_token"]},
            )
        if "study_id" not in result:
            raise StudyValidationError(
                f"Study submission was not accepted: {result['status']}"
            )
        return RedirectResponse(f"/studies/{result['study_id']}", status_code=303)

    @app.post("/studies/{study_id}/advance")
    async def study_advance_action(request: Request, study_id: str):
        session = session_for(request)
        study_id = _study_id(study_id)
        form = await load_form_body(request)
        if set(form) != {"csrf_token"}:
            raise StudyValidationError("Study advance form fields are invalid")
        verify_csrf(request, session, form["csrf_token"])
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
        session = session_for(request)
        study_id = _study_id(study_id)
        form = await load_form_body(request)
        if set(form) != {"csrf_token", "operation", "action_id"}:
            raise StudyValidationError("Study control form fields are invalid")
        verify_csrf(request, session, form["csrf_token"])
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
        session = session_for(request)
        study_id = _study_id(study_id)
        if get_lightweight_studies() is not None:
            try:
                report = await run_in_threadpool(lightweight_service().report, study_id)
            except LightweightStudyNotFound:
                pass
            else:
                return Response(
                    report["html"],
                    media_type="text/html",
                    headers={
                        "Content-Security-Policy": (
                            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                            "form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
                        ),
                        "X-QuantResearch-Report-Artifact": report["report_artifact_id"],
                    },
                )
        detail = await run_in_threadpool(studies.page_detail, study_id)
        return render(
            request,
            "study_report.html",
            session=session,
            study=detail,
        )

    @app.get("/studies/{study_id}")
    async def study_detail(request: Request, study_id: str):
        session = session_for(request)
        study_id = _study_id(study_id)
        detail = await run_in_threadpool(studies.page_detail, study_id)
        outcome = _study_outcome(
            settings.session_secret,
            session,
            study_id,
            request.query_params.get("status", ""),
        )
        return render(
            request,
            "study_detail.html",
            session=session,
            study=detail,
            control_action_id=secrets.token_hex(16),
            outcome_message=STUDY_OUTCOMES.get(outcome),
        )
