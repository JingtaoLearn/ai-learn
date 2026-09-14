from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from quant_platform.lightweight_study import LightweightStudyService
from quant_platform.msft_trend_study import PROXY_RESULT_SCHEMA
from quant_platform.postgres_persistence import PostgresOperatorPersistence
from quant_platform.settings import Settings
from quant_platform.study_remote import (
    StudyPostgresStore,
    StudyValidationError,
    freeze_synthetic_request,
)
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


def test_lightweight_list_is_one_bounded_newest_first_postgres_projection() -> None:
    executed: list[tuple[str, tuple[object, ...]]] = []
    updated_at = datetime(2026, 9, 10, 6, 0, 1, tzinfo=UTC)

    class Result:
        def fetchall(self):
            return [
                {
                    "study_id": STUDY_ID,
                    "kind": "SYNTHETIC_TRAINING",
                    "status": "SUCCEEDED",
                    "progress": {"completed_trials": 32, "total_trials": 32},
                    "updated_at": updated_at,
                    "failure": None,
                    "report_state": "NOT_APPLICABLE",
                }
            ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, parameters):
            executed.append((query, parameters))
            return Result()

    class Config:
        def validated(self):
            return self

        def connect(self):
            return Connection()

    page = StudyPostgresStore(Config()).list_summaries()

    assert page == {"studies": [
        {
            "study_id": STUDY_ID,
            "kind": "SYNTHETIC_TRAINING",
            "status": "SUCCEEDED",
            "progress": {"completed_trials": 32, "total_trials": 32},
            "updated_at": "2026-09-10T06:00:01Z",
            "failure": None,
            "report_state": "NOT_APPLICABLE",
        }
    ], "next_cursor": None}
    assert len(executed) == 1
    query, parameters = executed[0]
    assert "SELECT *" not in query.upper()
    assert "ORDER BY updated_at DESC, study_id DESC" in query
    assert "LIMIT %s" in query
    assert "deterministic-synthetic-search-v1" in query
    assert "optuna-tpe-synthetic-objective-v1" in query
    assert "NON_CONFIRMATORY_PROXY_EXECUTION_FAILED" in query
    assert parameters == (51,)


def test_lightweight_list_cursor_reaches_rows_older_than_first_page() -> None:
    executed: list[tuple[str, tuple[object, ...]]] = []
    rows = [
        {
            "study_id": f"{index:064x}",
            "kind": "SYNTHETIC_TRAINING",
            "status": "SUCCEEDED",
            "progress": {"completed_trials": 1, "total_trials": 1},
            "updated_at": datetime(2026, 9, 10, 6, 0, 59 - index, tzinfo=UTC),
            "failure": None,
            "report_state": "NOT_APPLICABLE",
        }
        for index in range(51)
    ]

    class Result:
        def __init__(self, values):
            self.values = values

        def fetchall(self):
            return self.values

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, parameters):
            executed.append((query, parameters))
            return Result(rows if len(executed) == 1 else [rows[-1]])

    class Config:
        def validated(self):
            return self

        def connect(self):
            return Connection()

    store = StudyPostgresStore(Config())
    first = store.list_summaries()
    second = store.list_summaries(cursor=first["next_cursor"])

    assert len(first["studies"]) == 50
    assert first["studies"][-1]["study_id"] == f"{49:064x}"
    assert first["next_cursor"] is not None
    assert second == {
        "studies": [
            {
                **rows[-1],
                "updated_at": rows[-1]["updated_at"].isoformat().replace("+00:00", "Z"),
            }
        ],
        "next_cursor": None,
    }
    query, parameters = executed[1]
    assert "WHERE (updated_at, study_id) < (%s, %s)" in query
    assert parameters[1:] == (f"{49:064x}", 51)


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

    def list_summaries(self, cursor: str | None = None) -> dict:
        self.calls.append(("list_summaries", cursor))
        return {
            "studies": [
                {
                    "study_id": STUDY_ID,
                    "kind": "SYNTHETIC_TRAINING",
                    "status": "SUCCEEDED",
                    "progress": {"completed_trials": 32, "total_trials": 32},
                    "updated_at": "2026-09-10T06:00:01Z",
                    "failure": None,
                    "report_state": "NOT_APPLICABLE",
                },
                {
                    "study_id": "b" * 64,
                    "kind": "SYNTHETIC_ITERATIONS",
                    "status": "RUNNING",
                    "progress": {"completed_iterations": 8, "total_iterations": 10},
                    "updated_at": "2026-09-10T05:00:01Z",
                    "failure": None,
                    "report_state": "NOT_APPLICABLE",
                },
                {
                    "study_id": "c" * 64,
                    "kind": "MSFT_YAHOO_ADJUSTED_OHLC_PROXY",
                    "status": "RUNNING",
                    "progress": {"completed_candidates": 5, "total_candidates": 15},
                    "updated_at": "2026-09-10T04:00:01Z",
                    "failure": None,
                    "report_state": "PENDING",
                },
            ],
            "next_cursor": "older-page" if cursor is None else None,
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


def test_signed_in_user_rediscovers_lightweight_study_in_ui_and_api(tmp_path: Path) -> None:
    client, _, service = _application(tmp_path)

    page = client.get("/studies")
    api = client.get("/api/studies?cursor=current-page")

    assert page.status_code == api.status_code == 200
    assert 'data-testid="lightweight-study-list"' in page.text
    assert f'href="/studies/lightweight/{STUDY_ID}"' in page.text
    assert "SYNTHETIC_TRAINING" in page.text
    assert "32 / 32" in page.text
    assert "SYNTHETIC_ITERATIONS" in page.text
    assert "8 / 10" in page.text
    assert "MSFT_YAHOO_ADJUSTED_OHLC_PROXY" in page.text
    assert "5 / 15" in page.text
    assert 'href="/studies?cursor=older-page"' in page.text
    assert api.json()["lightweight_studies"][0]["study_id"] == STUDY_ID
    assert api.json()["lightweight_studies_next_cursor"] is None
    assert service.calls == [
        ("list_summaries", None),
        ("list_summaries", "current-page"),
    ]


def test_signed_in_api_rejects_an_invalid_lightweight_study_cursor(
    tmp_path: Path, monkeypatch
) -> None:
    client, _, service = _application(tmp_path)

    def reject_cursor(*, cursor: str | None = None):
        raise StudyValidationError("Lightweight Study list cursor is invalid")

    monkeypatch.setattr(service, "list_summaries", reject_cursor)

    response = client.get("/api/studies?cursor=invalid")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_signed_in_discovery_reports_unavailable_authority_instead_of_empty_list(
    tmp_path: Path,
) -> None:
    client, _, _ = _application(tmp_path)
    client.app.state.lightweight_studies = None

    page = client.get("/studies")
    api = client.get("/api/studies")

    assert page.status_code == 200
    assert 'data-testid="lightweight-study-unavailable"' in page.text
    assert "No Lightweight Studies have been submitted" not in page.text
    assert api.status_code == 503
    assert api.json()["error"]["code"] == "LIGHTWEIGHT_STUDY_LIST_UNAVAILABLE"


def test_rediscovered_legacy_synthetic_study_has_a_renderable_detail(tmp_path: Path) -> None:
    request = freeze_synthetic_request(
        iterations=10,
        seed=7,
        checkpoint_count=2,
        source_commit="c" * 40,
        source_tree="d" * 40,
        worker_image="sha256:" + "e" * 64,
    )
    view = LightweightStudyService._view(
        {
            "study_id": request["job_id"],
            "status": "SUCCEEDED",
            "created_at": "2026-09-10T06:00:00Z",
            "updated_at": "2026-09-10T06:00:01Z",
            "worker_endpoint": "https://flearn.example.test",
            "worker_image": request["worker_image"],
            "source_commit": request["source_commit"],
            "source_tree": request["source_tree"],
            "frozen_request": request,
            "latest_progress": {
                "completed_iterations": 10,
                "total_iterations": 10,
                "checkpoint_sequence": 2,
            },
            "final_result": {
                "conclusion": "SYNTHETIC_MINIMUM_FOUND",
                "iterations": 10,
                "best_parameter": 0.618,
                "best_score": 0.00001,
                "checkpoint_count": 2,
            },
            "failure": None,
        }
    )
    client, _, service = _application(tmp_path)
    service.view = view

    detail = client.get(f"/studies/lightweight/{request['job_id']}")

    assert view["kind"] == "SYNTHETIC_ITERATIONS"
    assert view["objective"] == {"data_classification": "SYNTHETIC_NON_MARKET"}
    assert detail.status_code == 200
    assert "Completed work units</span><strong>10" in detail.text
    assert "Bounded budget</span><strong>10" in detail.text
    assert "SYNTHETIC_MINIMUM_FOUND" in detail.text
    assert "Iterations" in detail.text


def test_rediscovered_msft_proxy_study_keeps_market_detail_and_report_link(tmp_path: Path) -> None:
    client, _, service = _application(tmp_path)
    service.view = {
        **service.view,
        "kind": "MSFT_YAHOO_ADJUSTED_OHLC_PROXY",
        "market": True,
        "classification": "YAHOO_ADJUSTED_OHLC_PROXY_UNQUALIFIED_NON_CONFIRMATORY",
        "objective": {
            "data_classification": "YAHOO_ADJUSTED_OHLC_PROXY_UNQUALIFIED_NON_CONFIRMATORY"
        },
        "snapshot_id": "b" * 64,
        "progress": {"completed_candidates": 15, "total_candidates": 15},
        "report_available": True,
        "result": {
            "schema": PROXY_RESULT_SCHEMA,
            "conclusion": "NO_QUALIFIED_CANDIDATE",
            "verdict": "REJECTED_VALIDATION",
            "candidate_count": 15,
        },
    }

    detail = client.get(f"/studies/lightweight/{STUDY_ID}")

    assert detail.status_code == 200
    assert "XNYS MSFT RESEARCH" in detail.text
    assert "Completed work units</span><strong>15" in detail.text
    assert "Bounded budget</span><strong>15" in detail.text
    assert "Candidate count</dt><dd>15" in detail.text
    assert f'href="/studies/{STUDY_ID}/report"' in detail.text
    assert "synthetic non-market objective" not in detail.text


def test_proxy_execution_failure_does_not_advertise_an_unrenderable_report(
    tmp_path: Path,
) -> None:
    client, _, service = _application(tmp_path)
    service.view = {
        **service.view,
        "kind": "MSFT_YAHOO_ADJUSTED_OHLC_PROXY",
        "market": True,
        "classification": "YAHOO_ADJUSTED_OHLC_PROXY_UNQUALIFIED_NON_CONFIRMATORY",
        "objective": {
            "data_classification": "YAHOO_ADJUSTED_OHLC_PROXY_UNQUALIFIED_NON_CONFIRMATORY"
        },
        "snapshot_id": "b" * 64,
        "progress": {"completed_candidates": 0, "total_candidates": 15},
        "report_available": False,
        "result": {
            "schema": PROXY_RESULT_SCHEMA,
            "conclusion": "YAHOO_ADJUSTED_OHLC_PROXY_UNQUALIFIED_NON_CONFIRMATORY",
            "verdict": "NON_CONFIRMATORY_PROXY_EXECUTION_FAILED",
            "candidate_count": 0,
            "reason": "insufficient proxy history",
        },
    }

    detail = client.get(f"/studies/lightweight/{STUDY_ID}")

    assert detail.status_code == 200
    assert "Candidate count</dt><dd>0" in detail.text
    assert "insufficient proxy history" in detail.text
    assert "Immutable report unavailable for this terminal result" in detail.text
    assert f'href="/studies/{STUDY_ID}/report"' not in detail.text
