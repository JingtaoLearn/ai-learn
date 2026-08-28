from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from quant_platform.datasets import publish_snapshot, snapshot_status
from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.settings import Settings
from quant_platform.web import MAX_BODY_BYTES, _run_workers_once, create_app
from quant_platform.worker import SerialAttemptWorker, SerialStudyWorker

from test_auth import AUDIENCE, NOW, SESSION, SHARED, _claims, _token
from test_experiment_service import FIXTURE, _task
from test_parameter_study import (
    EXECUTION_IDENTITY,
    _minimal_orchestration_spec,
    _study_service,
)


def make_app(tmp_path: Path):
    allowlist = tmp_path / "allowed.txt"
    allowlist.write_text("researcher@example.com\n", encoding="utf-8")
    settings = Settings(
        environment="test",
        auth_mode="sso",
        state_root=tmp_path / "state",
        public_url="https://quant.ai.jingtao.fun",
        allowed_hosts=("quant.ai.jingtao.fun",),
        auth_shared_secret=SHARED,
        session_secret=SESSION,
        allowed_emails_file=allowlist,
        sso_login_url="https://ms-login.ai.jingtao.fun/auth/login",
        sso_audience=AUDIENCE,
        sso_callback_url="https://quant.ai.jingtao.fun/auth/callback",
        password_scrypt_hash=None,
        secure_cookies=True,
    ).validated()
    app = create_app(settings, clock=lambda: NOW)
    client = TestClient(
        app,
        base_url="https://quant.ai.jingtao.fun",
        headers={"host": "quant.ai.jingtao.fun"},
    )
    return app, client


def test_production_worker_tick_advances_study_before_attempt():
    calls = []

    class Worker:
        def __init__(self, name: str, progressed: bool):
            self.name = name
            self.progressed = progressed

        def run_once(self) -> bool:
            calls.append(self.name)
            return self.progressed

    assert _run_workers_once(Worker("study", True), Worker("attempt", False)) is True
    assert calls == ["study", "attempt"]

    calls.clear()
    assert _run_workers_once(Worker("study", False), Worker("attempt", False)) is False
    assert calls == ["study", "attempt"]


def test_production_worker_tick_isolates_each_worker_failure(caplog):
    calls = []

    class FailingStudyWorker:
        def run_once(self) -> bool:
            calls.append("study")
            raise RuntimeError("synthetic Study failure")

    class AttemptWorker:
        def run_once(self) -> bool:
            calls.append("attempt")
            return True

    assert _run_workers_once(FailingStudyWorker(), AttemptWorker()) is True
    assert calls == ["study", "attempt"]
    assert "Study worker tick failed" in caplog.text
    assert "synthetic Study failure" in caplog.text


def test_production_worker_loop_completes_a_real_parameter_study(tmp_path: Path):
    studies, experiments = _study_service(tmp_path)
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="production-worker-loop-study",
    )
    executor = ResolvedAttemptExecutor(
        studies.catalog,
        output_root=studies.catalog.state_root / "experiment-runs",
        project_root=Path(__file__).parents[1],
        attempt_controller=experiments,
        identity_provider=lambda project_root, runner_image: EXECUTION_IDENTITY,
    )
    study_worker = SerialStudyWorker(studies)
    attempt_worker = SerialAttemptWorker(experiments, executor=executor)

    for _ in range(128):
        _run_workers_once(study_worker, attempt_worker)
        detail = studies.detail(submitted["study_id"])
        if detail["phase"] == "COMPLETED":
            break
    else:
        raise AssertionError("production worker loop did not complete the Study")

    assert detail["selection_outcome"] == "CHAMPION_SELECTED"
    assert detail["holdout"]["access"] == "ACCESSED"
    assert detail["holdout"]["outcome"] == "PASSED"
    assert all(binding["state"] == "VERIFIED" for binding in detail["bindings"])


def authenticate(app, client):
    issued = app.state.auth.issue_session(
        {"email": "researcher@example.com", "display_name": "Researcher"}
    )
    client.cookies.set("quant_session", issued.cookie)
    return issued


def snapshot(app):
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    return publish_snapshot(
        frame,
        app.state.catalog.state_root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )["snapshot_id"]


def test_public_health_and_security_headers(tmp_path: Path):
    _, client = make_app(tmp_path)

    response = client.get("/health")

    assert response.json() == {"status": "ok"}
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_api_auth_and_errors_are_always_json(tmp_path: Path):
    _, client = make_app(tmp_path)

    unauthenticated = client.get("/api/operators")
    bad_host = client.get("/api/operators", headers={"host": "evil.example"})

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTH_REQUIRED"
    assert bad_host.status_code == 400
    assert bad_host.json()["error"]["code"] == "BAD_HOST"


def test_every_protected_route_authenticates_before_any_meaningful_work(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("protected work ran before authentication")

    for target, names in (
        (
            app.state.experiments,
            (
                "resolve_task",
                "preview_task",
                "submit",
                "rerun",
                "list_experiments",
                "experiment_detail",
                "list_attempts",
                "attempt_detail",
                "create_replacement_attempt",
            ),
        ),
        (
            app.state.operators,
            ("list", "detail", "list_versions", "submit"),
        ),
        (
            app.state.datasets,
            ("list_available", "resolve"),
        ),
        (
            app.state.studies,
            ("list", "preview", "submit", "advance", "control", "detail"),
        ),
        (
            app.state.catalog,
            ("connect", "template_detail", "operator_detail"),
        ),
    ):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    for name in (
        "_datasets",
        "_verified_run_payloads",
        "_report_payload",
        "_form_body",
        "_json_body",
        "_task_from_form",
    ):
        monkeypatch.setattr(f"quant_platform.web.{name}", forbidden)

    html_gets = (
        "/",
        "/operators",
        "/operators/submit",
        "/operators/prior_log_ols/1.0.0",
        "/templates/single_stock_daily_causal/1",
        "/experiments/new",
        "/studies",
        "/studies/new",
        "/studies/not-found",
        "/studies/not-found/report",
        "/history?status=FAILED&search=x&drift=drifted",
        "/experiments/not-found",
        "/reports/not-found",
        "/reports/not-found/content",
    )
    html_posts = (
        "/logout",
        "/operators/submit",
        "/experiments/preview",
        "/experiments/new",
        "/studies/preview",
        "/studies",
        "/studies/not-found/advance",
        "/studies/not-found/control",
        "/experiments/not-found/rerun",
    )
    api_gets = (
        "/api/operators",
        "/api/datasets",
        "/api/operators/prior_log_ols",
        "/api/templates/single_stock_daily_causal/1",
        "/api/experiments",
        "/api/studies",
        "/api/experiments/not-found",
        "/api/experiments/not-found/attempts",
        "/api/attempts/not-found",
        "/api/studies/not-found",
    )
    api_posts = (
        "/api/operators",
        "/api/tasks/resolve",
        "/api/experiments/preview",
        "/api/experiments",
        "/api/studies/preview",
        "/api/studies",
        "/api/experiments/not-found/rerun",
        "/api/attempts/not-found/recover",
        "/api/studies/not-found/advance",
        "/api/studies/not-found/control",
    )

    for path in html_gets:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"
    for path in html_posts:
        response = client.post(
            path,
            content=b"not=a-valid-body",
            headers={
                "origin": "https://quant.ai.jingtao.fun",
                "content-type": "application/octet-stream",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"
    for path in api_gets:
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    for path in api_posts:
        response = client.post(
            path,
            content=b"{broken",
            headers={
                "origin": "https://quant.ai.jingtao.fun",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert calls == []


def test_study_api_requires_csrf_and_preserves_domain_statuses(
    tmp_path: Path,
    monkeypatch,
):
    app, client = make_app(tmp_path)
    study_id = "a" * 64
    assert app.state.studies.catalog is app.state.catalog
    assert app.state.studies.datasets is app.state.datasets
    assert app.state.studies.experiments is app.state.experiments

    issued = authenticate(app, client)
    mutation_headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
    }
    rejected = client.post(
        f"/api/studies/{study_id}/advance",
        json={},
        headers={
            "origin": "https://quant.ai.jingtao.fun",
            "x-csrf-token": "wrong",
        },
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "FORBIDDEN"

    monkeypatch.setattr(
        app.state.studies,
        "detail",
        lambda value: {"study_id": value, "phase": "FROZEN"},
    )
    detail = client.get(f"/api/studies/{study_id}")
    assert detail.json()["study"]["study_id"] == study_id

    for status in ("LEASE_BUSY", "EXECUTION_IDENTITY_DRIFT"):
        monkeypatch.setattr(
            app.state.studies,
            "advance",
            lambda value, status=status: {
                "status": status,
                "study_id": value,
            },
        )
        response = client.post(
            f"/api/studies/{study_id}/advance",
            json={},
            headers=mutation_headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == status

    for status in ("ACTION_CONFLICT", "INVALID_TRANSITION"):
        monkeypatch.setattr(
            app.state.studies,
            "control",
            lambda value, operation, *, action_id, status=status: {
                "status": status,
                "study_id": value,
                "operation": operation,
                "action_id": action_id,
            },
        )
        response = client.post(
            f"/api/studies/{study_id}/control",
            json={"operation": "RESUME", "action_id": "control-action"},
            headers=mutation_headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == status


def test_sso_callback_sets_secure_cookie_and_rejects_replay(tmp_path: Path):
    _, client = make_app(tmp_path)
    token = _token(_claims())

    response = client.post(
        "/auth/callback",
        content="token=" + token,
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    replay = client.post(
        "/auth/callback",
        content="token=" + token,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert all(value in cookie for value in ("HttpOnly", "Secure", "SameSite=lax"))
    assert replay.status_code == 401
    assert token not in replay.text


def test_json_catalog_submit_duplicate_rerun_and_history_flow(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    task = _task(snapshot(app))
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
    }

    assert len(client.get("/api/operators").json()["operators"]) == 7
    resolved = client.post(
        "/api/tasks/resolve", json={"task": task}, headers=headers
    )
    created = client.post(
        "/api/experiments",
        json={"task": task, "action_id": "create"},
        headers=headers,
    )
    duplicate = client.post(
        "/api/experiments",
        json={"task": task, "action_id": "duplicate"},
        headers=headers,
    )
    experiment_id = created.json()["experiment_id"]
    rerun = client.post(
        f"/api/experiments/{experiment_id}/rerun",
        json={"action_id": "rerun"},
        headers=headers,
    )

    assert resolved.json()["resolved"]["operators"]["fit"]["resolved_version"] == "1.0.0"
    assert created.status_code == 201
    assert duplicate.json()["status"] == "DUPLICATE"
    assert rerun.status_code == 201
    assert len(client.get("/api/experiments").json()["experiments"]) == 1
    assert (
        len(client.get(f"/api/experiments/{experiment_id}/attempts").json()["attempts"])
        == 2
    )


def test_api_cannot_preclaim_a_study_internal_experiment_action(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    task = _task(snapshot(app))

    response = client.post(
        "/api/experiments",
        json={
            "task": task,
            "action_id": f"study-internal:effect:{'a' * 64}",
        },
        headers={
            "origin": "https://quant.ai.jingtao.fun",
            "x-csrf-token": issued.csrf_token,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "DOMAIN_ERROR",
        "message": "action_id uses a reserved internal namespace",
    }
    assert app.state.experiments.list_experiments() == []


def test_api_accepts_catalog_dataset_and_date_range_and_freezes_snapshot(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot(app)
    task = _task("0" * 64)
    task["dataset"] = {
        "dataset_id": "SYNTH.SS",
        "start": "2026-01-05",
        "end": "2026-01-12",
    }
    task["template"]["parameters"]["evaluation_start"] = "2026-01-05"
    task["template"]["parameters"]["evaluation_end"] = "2026-01-12"
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
    }

    response = client.post(
        "/api/experiments",
        json={"task": task, "action_id": "catalog-create"},
        headers=headers,
    )
    detail = client.get(
        f"/api/experiments/{response.json()['experiment_id']}"
    ).json()["experiment"]
    datasets = client.get("/api/datasets").json()["datasets"]

    assert response.status_code == 201
    assert detail["dataset"]["dataset_id"] == "SYNTH.SS"
    assert detail["dataset"]["name"] == "SYNTH.SS"
    assert detail["dataset"]["requested_start"] == "2026-01-05"
    assert detail["dataset"]["requested_end"] == "2026-01-12"
    assert detail["dataset"]["effective_start"] == "2026-01-05"
    assert detail["dataset"]["effective_end"] == "2026-01-12"
    assert detail["dataset"]["lineage"] == {"kind": "legacy_snapshot"}
    assert len(detail["dataset"]["snapshot_id"]) == 64
    assert datasets == [
        {
            "dataset_id": "SYNTH.SS",
            "name": "SYNTH.SS",
            "instrument": "SYNTH.SS",
            "default_start": "2026-01-01",
            "latest_available_close": "2026-01-12",
            "latest_snapshot_id": detail["dataset"]["snapshot_id"],
        }
    ]


def test_api_weekend_bounds_canonicalize_to_sessions_and_suppress_duplicates(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot(app)
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
    }

    def task(start: str) -> dict:
        value = _task("0" * 64)
        value["dataset"] = {
            "dataset_id": "SYNTH.SS",
            "start": start,
            "end": "2026-01-12",
        }
        value["template"]["parameters"]["evaluation_start"] = start
        value["template"]["parameters"]["evaluation_end"] = "2026-01-12"
        return value

    weekend_task = task("2026-01-04")
    session_task = task("2026-01-05")
    weekend_preview = client.post(
        "/api/experiments/preview",
        json={"task": weekend_task},
        headers=headers,
    )
    session_preview = client.post(
        "/api/experiments/preview",
        json={"task": session_task},
        headers=headers,
    )

    assert weekend_preview.status_code == 200
    assert session_preview.status_code == 200
    assert (
        weekend_preview.json()["preview"]["experiment_id"]
        == session_preview.json()["preview"]["experiment_id"]
    )
    resolved = weekend_preview.json()["preview"]["resolved"]
    assert resolved["dataset"]["requested_start"] == "2026-01-04"
    assert resolved["dataset"]["effective_start"] == "2026-01-05"
    assert resolved["template"]["parameters"]["evaluation_start"] == "2026-01-05"
    assert resolved["requested"]["dataset"]["start"] == "2026-01-04"

    created = client.post(
        "/api/experiments",
        json={"task": weekend_task, "action_id": "weekend-create"},
        headers=headers,
    )
    duplicate = client.post(
        "/api/experiments",
        json={"task": session_task, "action_id": "session-create"},
        headers=headers,
    )
    experiment_id = created.json()["experiment_id"]
    detail = client.get(f"/api/experiments/{experiment_id}").json()["experiment"]

    assert created.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "DUPLICATE"
    assert duplicate.json()["experiment_id"] == experiment_id
    assert detail["dataset"]["requested_start"] == "2026-01-04"
    assert detail["dataset"]["effective_start"] == "2026-01-05"
    assert detail["template"]["parameters"]["evaluation_start"] == "2026-01-05"
    assert (
        len(client.get(f"/api/experiments/{experiment_id}/attempts").json()["attempts"])
        == 1
    )


def test_api_repairs_incomplete_catalog_range_after_security_checks(tmp_path: Path):
    from quant_platform.dataset_service import FetchedDailyBars

    def bars(dates):
        closes = [6.1 + index / 10 for index in range(len(dates))]
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(dates),
                "Open": [value - 0.02 for value in closes],
                "High": [value + 0.04 for value in closes],
                "Low": [value - 0.05 for value in closes],
                "Close": closes,
                "Volume": [1000.0 + index for index in range(len(dates))],
            }
        )

    source_identity = {
        "provider": "synthetic",
        "instrument": "REPAIR.SS",
        "request": "fixed-test-generation",
    }

    class Source:
        provider = "synthetic"

        def __init__(self):
            self.fetch_calls = []

        def latest_available_close(self, instrument):
            return "2026-08-20"

        def fetch(self, instrument, start, end):
            self.fetch_calls.append((instrument, start, end))
            return FetchedDailyBars(
                bars=bars(["2026-08-18", "2026-08-19", "2026-08-20"]),
                source_identity=source_identity
                | {"instrument": instrument},
            )

    class Calendar:
        source_identity = {
            "calendar": "XSHG",
            "library": "test",
            "version": "2026",
        }

        @staticmethod
        def sessions(start, end):
            return ["2026-08-18", "2026-08-19", "2026-08-20"]

    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    publish_snapshot(
        bars(["2026-08-18"]),
        app.state.catalog.state_root,
        {
            "instrument": "REPAIR.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "unadjusted",
        },
    )
    source = Source()
    app.state.datasets.sources["synthetic"] = source
    app.state.datasets.calendars["XSHG"] = Calendar()
    task = _task("0" * 64)
    task["dataset"] = {
        "dataset_id": "REPAIR.SS",
        "start": "2026-08-18",
        "end": "2026-08-20",
    }
    task["template"]["parameters"]["evaluation_start"] = "2026-08-18"
    task["template"]["parameters"]["evaluation_end"] = "2026-08-20"

    response = client.post(
        "/api/experiments",
        json={"task": task, "action_id": "repair-create"},
        headers={
            "origin": "https://quant.ai.jingtao.fun",
            "x-csrf-token": issued.csrf_token,
        },
    )

    assert response.status_code == 201
    assert source.fetch_calls == [
        ("REPAIR.SS", "2026-08-18", "2026-08-20")
    ]
    detail = app.state.experiments.experiment_detail(
        response.json()["experiment_id"]
    )
    assert detail["dataset"]["requested_end"] == "2026-08-20"
    assert detail["dataset"]["lineage"]["kind"] == "verified_update"
    assert detail["dataset"]["lineage"]["source"] == source_identity
    assert detail["dataset"]["lineage"]["expected_sessions_source"] == (
        app.state.datasets.calendars["XSHG"].source_identity
    )
    assert snapshot_status(
        app.state.catalog.state_root, "REPAIR.SS"
    )["snapshot_id"] == detail["dataset"]["snapshot_id"]


def test_oversized_authenticated_body_cannot_reach_dataset_updater(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("dataset updater ran before body limit")

    monkeypatch.setattr(app.state.datasets, "resolve", forbidden)

    response = client.post(
        "/api/experiments",
        content=b"{" + b"x" * MAX_BODY_BYTES + b"}",
        headers={
            "content-type": "application/json",
            "origin": "https://quant.ai.jingtao.fun",
            "x-csrf-token": issued.csrf_token,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"
    assert calls == []


def test_mutations_require_exact_origin_and_csrf(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    task = _task(snapshot(app))

    missing = client.post("/api/tasks/resolve", json={"task": task})
    wrong = client.post(
        "/api/tasks/resolve",
        json={"task": task},
        headers={
            "origin": "https://evil.example",
            "x-csrf-token": "wrong",
        },
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing.headers["content-type"].startswith("application/json")


def test_rerun_endpoint_rejects_task_selectors_and_source(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    task = _task(snapshot(app))
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
    }
    created = client.post(
        "/api/experiments",
        json={"task": task, "action_id": "create"},
        headers=headers,
    ).json()

    response = client.post(
        f"/api/experiments/{created['experiment_id']}/rerun",
        json={"action_id": "bad", "task": task, "source": "print('no')"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_catalog_coexists_with_authoritative_platform_datasets(tmp_path: Path):
    root = tmp_path / "state" / "platform"
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    snapshot_id = publish_snapshot(
        frame,
        root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )["snapshot_id"]
    snapshot_dir = root / "datasets" / "SYNTH.SS" / snapshot_id
    before = {
        path.name: path.read_bytes() for path in snapshot_dir.iterdir()
    }
    allowlist = tmp_path / "allowed.txt"
    allowlist.write_text("researcher@example.com\n", encoding="utf-8")
    settings = Settings(
        environment="test",
        auth_mode="sso",
        state_root=root,
        public_url="https://quant.ai.jingtao.fun",
        allowed_hosts=("quant.ai.jingtao.fun",),
        auth_shared_secret=SHARED,
        session_secret=SESSION,
        allowed_emails_file=allowlist,
        sso_login_url="https://ms-login.ai.jingtao.fun/auth/login",
        sso_audience=AUDIENCE,
        sso_callback_url=AUDIENCE,
        password_scrypt_hash=None,
        secure_cookies=True,
    ).validated()

    app = create_app(settings, clock=lambda: NOW)
    resolved = app.state.experiments.resolve_task(_task(snapshot_id))

    assert app.state.catalog.database_path == root / "catalog.sqlite3"
    assert resolved["dataset"]["snapshot_id"] == snapshot_id
    assert {
        path.name: path.read_bytes() for path in snapshot_dir.iterdir()
    } == before
    assert not snapshot_dir.is_symlink()


def test_password_fallback_has_reachable_csrf_protected_login(tmp_path: Path):
    from quant_platform.auth import encode_scrypt_password

    app, client = make_app(tmp_path)
    settings = app.state.settings
    password_settings = Settings(
        **{
            **settings.__dict__,
            "environment": "test",
            "auth_mode": "password",
            "auth_shared_secret": None,
            "password_scrypt_hash": encode_scrypt_password(
                "correct horse", salt=b"fixed-test-salt"
            ),
            "secure_cookies": False,
        }
    ).validated()
    password_app = create_app(password_settings)
    password_client = TestClient(
        password_app,
        base_url="https://quant.ai.jingtao.fun",
        headers={"host": "quant.ai.jingtao.fun"},
    )

    login = password_client.get("/login")
    assert 'action="/auth/password"' in login.text
    csrf = login.text.split('name="login_csrf" value="', 1)[1].split('"', 1)[0]
    response = password_client.post(
        "/auth/password",
        data={"password": "correct horse", "login_csrf": csrf},
        headers={"origin": "https://quant.ai.jingtao.fun"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
