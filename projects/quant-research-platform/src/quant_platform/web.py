from __future__ import annotations

import hashlib
import json
import os
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

from .auth import AuthError, AuthManager, SessionData
from .catalog import initialize_catalog
from .experiment_service import ExperimentService, TaskValidationError
from .operator_service import OperatorService, OperatorSubmissionError
from .settings import Settings


SESSION_COOKIE = "quant_session"
MAX_BODY_BYTES = 1_048_576
PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
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


async def _json_body(request: Request) -> Any:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("request body exceeds the size limit")
    try:
        return json.loads(
            body,
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body is not valid JSON") from exc


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
    try:
        return json.loads(
            value,
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"{path} contains non-finite value {constant}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON") from exc


def _session(request: Request) -> SessionData:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie is None:
        raise AuthError("Authentication required")
    return request.app.state.auth.verify_session(cookie)


def _csrf(request: Request, session: SessionData, token: str | None = None) -> None:
    request.app.state.auth.verify_csrf(
        session, token or request.headers.get("x-csrf-token", "")
    )


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
        context={"session": session, "csrf_token": session.csrf_token, **context},
        status_code=status_code,
    )


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
                        f"{manifest['date_start']} to {manifest['date_end']}"
                    ),
                }
            )
        except (KeyError, OSError, ValueError):
            continue
    return datasets


def _report_payload(settings: Settings, attempt: dict[str, Any]) -> bytes:
    if attempt["status"] != "SUCCEEDED" or not attempt["result_path"]:
        raise FileNotFoundError("attempt has no successful report")
    allowed_root = settings.state_root.absolute() / "experiment-runs"
    run_dir = Path(attempt["result_path"]).absolute()
    if run_dir.parent != allowed_root or run_dir.is_symlink() or not run_dir.is_dir():
        raise FileNotFoundError("report run path is outside the result store")
    report = run_dir / "report.html"
    manifest_path = run_dir / "run_manifest.json"
    for path in (report, manifest_path):
        metadata = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o222
        ):
            raise FileNotFoundError("report artifact is not immutable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["files"]["report.html"]
    payload = report.read_bytes()
    if expected != {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }:
        raise FileNotFoundError("report artifact checksum mismatch")
    return payload


def create_app(
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    settings = settings.validated()
    catalog = initialize_catalog(settings.state_root)
    experiments = ExperimentService(
        catalog,
        execution_identity={"domain_schema": 1, "runner": "quant_platform"},
    )
    experiments.recover_abandoned_attempts()
    auth = AuthManager(catalog, settings, **({"clock": clock} if clock else {}))
    operators = OperatorService(catalog, runner_image=settings.runner_image)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.experiments = experiments
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
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={"login_url": login_url},
        )

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
        result = operators.submit(body)
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
        return {"resolved": experiments.resolve_task(body["task"])}

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
        result = experiments.submit(body["task"], action_id=body["action_id"])
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
        result = experiments.rerun(experiment_id, action_id=body["action_id"])
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
                "SELECT COUNT(*) FROM attempts WHERE status = 'FAILED'"
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

    @app.get("/operators")
    async def operator_list(request: Request):
        session = _session(request)
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
        return _render(
            request, "operators.html", session=session, grouped=grouped
        )

    @app.get("/operators/submit")
    async def operator_submit_form(request: Request):
        return _render(
            request, "operator_submit.html", session=_session(request)
        )

    @app.post("/operators/submit")
    async def operator_submit_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
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
        result = operators.submit(submission)
        return RedirectResponse(
            f"/operators/{result['operator_id']}/{result['version']}",
            status_code=303,
        )

    @app.get("/operators/{operator_id}/{version}")
    async def operator_detail(request: Request, operator_id: str, version: str):
        session = _session(request)
        detail = operators.detail(operator_id, version)
        return _render(
            request,
            "operator_detail.html",
            session=session,
            operator=detail,
            documentation=_safe_markdown(detail["documentation"]),
            versions=operators.list_versions(operator_id),
        )

    @app.get("/templates/{name}/{version}")
    async def template_detail(request: Request, name: str, version: str):
        return _render(
            request,
            "template_detail.html",
            session=_session(request),
            template=catalog.template_detail(name, version),
        )

    @app.get("/experiments/new")
    async def experiment_new(request: Request):
        session = _session(request)
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
        return _render(
            request,
            "experiment_new.html",
            session=session,
            datasets=_datasets(settings.state_root),
            grouped=grouped,
            template=catalog.template_detail("single_stock_daily_causal", "1"),
        )

    @app.post("/experiments/new")
    async def experiment_create_action(request: Request):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        dataset_value = form.get("dataset", "")
        if "|" not in dataset_value:
            raise TaskValidationError("dataset selection is required")
        instrument, snapshot_id = dataset_value.split("|", 1)
        template = catalog.template_detail("single_stock_daily_causal", "1")
        template_parameters: dict[str, Any] = {}
        for name, schema in template["parameter_schema"]["properties"].items():
            raw = form.get(f"template_{name}", "")
            if raw == "" and schema.get("nullable"):
                value: Any = None
            elif schema["type"] == "number":
                value = float(raw)
            elif schema["type"] == "integer":
                value = int(raw)
            elif schema["type"] == "boolean":
                value = raw.lower() == "true"
            else:
                value = raw
            template_parameters[name] = value
        task_operators: dict[str, Any] = {}
        for slot in template["slots"]:
            selector = form.get(f"operator_{slot}_selector", "")
            if "@" not in selector:
                raise TaskValidationError(f"{slot} operator selection is required")
            operator_id, version = selector.rsplit("@", 1)
            task_operators[slot] = {
                "operator_id": operator_id,
                "version": version,
                "parameters": _json_text(
                    form.get(f"operator_{slot}_parameters", "{}"),
                    f"{slot} parameters",
                ),
            }
        result = experiments.submit(
            {
                "schema_version": 1,
                "dataset": {
                    "instrument": instrument,
                    "snapshot_id": snapshot_id,
                },
                "template": {
                    "name": template["name"],
                    "version": template["version"],
                    "parameters": template_parameters,
                },
                "operators": task_operators,
            },
            action_id=form.get("action_id") or secrets.token_hex(16),
        )
        return RedirectResponse(
            f"/experiments/{result['experiment_id']}", status_code=303
        )

    @app.get("/history")
    async def history(request: Request):
        return _render(
            request,
            "history.html",
            session=_session(request),
            experiments=experiments.list_experiments(),
        )

    @app.get("/experiments/{experiment_id}")
    async def experiment_detail(request: Request, experiment_id: str):
        detail = experiments.experiment_detail(experiment_id)
        return _render(
            request,
            "experiment_detail.html",
            session=_session(request),
            experiment=detail,
            report_attempt=next(
                (
                    attempt
                    for attempt in detail["attempts"]
                    if attempt["status"] == "SUCCEEDED"
                ),
                None,
            ),
        )

    @app.post("/experiments/{experiment_id}/rerun")
    async def experiment_rerun_action(request: Request, experiment_id: str):
        session = _session(request)
        form = await _form_body(request)
        _csrf(request, session, form.get("csrf_token"))
        result = experiments.rerun(
            experiment_id,
            action_id=form.get("action_id") or secrets.token_hex(16),
        )
        return RedirectResponse(
            f"/experiments/{result['experiment_id']}", status_code=303
        )

    @app.get("/reports/{attempt_id}")
    async def report(request: Request, attempt_id: str):
        _session(request)
        try:
            payload = _report_payload(
                settings, experiments.attempt_detail(attempt_id)
            )
        except (FileNotFoundError, KeyError, OSError, ValueError):
            return HTMLResponse("Report not found.", status_code=404)
        return Response(
            payload,
            media_type="text/html",
            headers={
                "Content-Security-Policy": (
                    "sandbox; default-src 'none'; img-src data:; "
                    "style-src 'unsafe-inline'"
                ),
                "Content-Disposition": "inline",
            },
        )

    return app


def main() -> None:
    import uvicorn

    from .resolved_runner import ResolvedAttemptExecutor
    from .worker import SerialAttemptWorker

    settings = Settings.from_environment()
    application = create_app(settings)
    executor = ResolvedAttemptExecutor(
        application.state.catalog,
        output_root=settings.state_root / "experiment-runs",
        project_root=settings.project_root,
        runner_image=settings.runner_image,
    )
    worker = SerialAttemptWorker(application.state.experiments, executor=executor)

    def run_worker() -> None:
        while True:
            if not worker.run_once():
                time.sleep(1)

    threading.Thread(
        target=run_worker,
        name="quant-attempt-worker",
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
