from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from quant_platform.lightweight_study import LightweightStudyService
from quant_platform.postgres_persistence import PostgresOperatorPersistence
from quant_platform.settings import Settings
from quant_platform.web import create_app

from test_auth import AUDIENCE, NOW, SESSION, SHARED


STUDY_ID = "a" * 64


def test_lightweight_submission_identity_converges_without_local_compute() -> None:
    requests = []

    class Store:
        pass

    class Dispatcher:
        def submit(self, request: dict) -> dict:
            requests.append(request)
            return {
                "authoritative": {
                    "study_id": request["job_id"],
                    "status": "DISPATCHED",
                    "created_at": "2026-09-10T06:00:00Z",
                    "updated_at": "2026-09-10T06:00:00Z",
                    "worker_endpoint": "https://flearn.example.test",
                    "worker_image": request["worker_image"],
                    "source_commit": request["source_commit"],
                    "source_tree": request["source_tree"],
                    "frozen_request": request,
                    "latest_progress": None,
                    "final_result": None,
                    "failure": None,
                }
            }

    service = LightweightStudyService(
        store=Store(),  # type: ignore[arg-type]
        dispatcher=Dispatcher(),  # type: ignore[arg-type]
        source_commit="c" * 40,
        source_tree="d" * 40,
        worker_image="sha256:" + "b" * 64,
    )

    first = service.submit(action_id="1" * 32, trial_budget=32)
    duplicate = service.submit(action_id="1" * 32, trial_budget=32)

    assert first["study_id"] == duplicate["study_id"]
    assert requests[0] == requests[1]
    assert requests[0]["training_spec"]["search"]["seed"] == int("1" * 8, 16)
    assert requests[0]["training_spec"]["objective"]["data_classification"] == (
        "SYNTHETIC_NON_MARKET"
    )
    assert first["local_compute_attempted"] is False


class OperatorPersistence(PostgresOperatorPersistence):
    def __init__(self) -> None:
        pass

    def verify_schema(self) -> None:
        return None


class FakeLightweightStudies:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.view = {
            "study_id": STUDY_ID,
            "status": "SUCCEEDED",
            "created_at": "2026-09-10T06:00:00Z",
            "updated_at": "2026-09-10T06:00:01Z",
            "worker_endpoint": "https://flearn.example.test",
            "worker_image": "sha256:" + "b" * 64,
            "source_commit": "c" * 40,
            "source_tree": "d" * 40,
            "objective": {"data_classification": "SYNTHETIC_NON_MARKET"},
            "trial_budget": 32,
            "progress": {
                "completed_trials": 32,
                "total_trials": 32,
                "suggestion_count": 32,
                "checkpoint_sequence": 8,
            },
            "result": {
                "conclusion": "SYNTHETIC_OBJECTIVE_SEARCH_COMPLETED",
                "completed_trials": 32,
                "suggestion_count": 32,
                "best_parameter": 0.618,
                "best_score": -0.00001,
                "checkpoint_count": 8,
            },
            "failure": None,
            "sync_error": None,
            "local_compute_attempted": False,
        }

    def submit(self, *, action_id: str, trial_budget: int) -> dict:
        self.calls.append(("submit", action_id, trial_budget))
        return self.view

    def detail(self, study_id: str) -> dict:
        self.calls.append(("detail", study_id))
        return self.view


def _application(tmp_path: Path):
    allowlist = tmp_path / "allowed.txt"
    allowlist.write_text("researcher@example.com\n", encoding="utf-8")
    app = create_app(
        Settings(
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
        ),
        clock=lambda: NOW,
        operator_persistence=OperatorPersistence(),
    )
    client = TestClient(
        app,
        base_url="https://quant.ai.jingtao.fun",
        headers={"host": "quant.ai.jingtao.fun"},
    )
    issued = app.state.auth.issue_session(
        {"email": "researcher@example.com", "display_name": "Researcher"}
    )
    client.cookies.set("quant_session", issued.cookie)
    service = FakeLightweightStudies()
    app.state.lightweight_studies = service
    return client, issued, service


def test_signed_in_lightweight_study_journey_is_explicit_and_aggregate_only(
    tmp_path: Path,
) -> None:
    client, issued, service = _application(tmp_path)

    modes = client.get("/studies/new")
    lightweight = client.get("/studies/new/lightweight")
    action_match = re.search(
        r'name="action_id" value="([0-9a-f]{32})"', lightweight.text
    )
    assert action_match is not None
    action_id = action_match.group(1)
    submitted = client.post(
        "/studies/lightweight",
        data={
            "csrf_token": issued.csrf_token,
            "action_id": action_id,
            "trial_budget": "32",
        },
        headers={"origin": "https://quant.ai.jingtao.fun"},
        follow_redirects=False,
    )
    detail = client.get(submitted.headers["location"])
    api_headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
    }
    first = client.post(
        "/api/lightweight-studies",
        json={"action_id": action_id, "trial_budget": 32},
        headers=api_headers,
    )
    duplicate = client.post(
        "/api/lightweight-studies",
        json={"action_id": action_id, "trial_budget": 32},
        headers=api_headers,
    )

    assert modes.status_code == lightweight.status_code == 200
    assert "Choose lightweight training" in modes.text
    assert "Choose legacy mode" in modes.text
    assert "LEGACY · ATTEMPT-BACKED" in modes.text
    assert "Synthetic non-market prototype" in lightweight.text
    assert submitted.status_code == 303
    assert submitted.headers["location"] == f"/studies/lightweight/{STUDY_ID}"
    assert detail.status_code == 200
    assert "Lightweight training Study — not an Attempt sequence" in detail.text
    assert "SYNTHETIC_OBJECTIVE_SEARCH_COMPLETED" in detail.text
    assert "Local compute attempted</dt><dd>false" in detail.text
    assert "Experiment bindings" not in detail.text
    assert first.status_code == duplicate.status_code == 201
    assert first.json()["study"]["study_id"] == duplicate.json()["study"]["study_id"] == STUDY_ID
    assert service.calls == [
        ("submit", action_id, 32),
        ("detail", STUDY_ID),
        ("submit", action_id, 32),
        ("submit", action_id, 32),
    ]
