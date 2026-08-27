from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from quant_platform.datasets import publish_snapshot
from quant_platform.settings import Settings
from quant_platform.web import create_app

from test_auth import NOW, SESSION, SHARED, _claims, _token
from test_experiment_service import FIXTURE, _task


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
        sso_audience="quant-research-ui",
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
