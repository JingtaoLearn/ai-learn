import html
import re
from pathlib import Path

from test_web_api import authenticate, make_app, snapshot
from test_web_ui import _experiment_form


STUDY_ID = "a" * 64


def _study_detail() -> dict:
    return {
        "study_id": STUDY_ID,
        "preview_digest": STUDY_ID,
        "created_at": "2026-08-28T08:00:00Z",
        "updated_at": "2026-08-28T08:10:00Z",
        "phase": "VALIDATING_SELECTION_PROCESS",
        "control_status": "ACTIVE",
        "selection_outcome": "NOT_DETERMINED",
        "holdout": {
            "access": "SEALED",
            "outcome": "NOT_RUN",
            "freshness": "LEGACY_UNKNOWN",
        },
        "coordination": {
            "lease": {
                "owner": "study-worker",
                "expires_at": "2026-08-28T08:11:00Z",
                "fencing_token": 2,
            }
        },
        "frozen_plan": {
            "dataset": {
                "dataset_id": "SYNTH.SS",
                "name": "<Synthetic & safe>",
                "requested_start": "2026-01-01",
                "requested_end": "2026-06-30",
                "snapshot_id": "b" * 64,
                "lineage": {"kind": "snapshot"},
            },
            "search": {
                "suggester": "GRID",
                "unique_trial_budget": 4,
                "max_suggestions": 8,
                "candidate_capacity": 4,
                "space": {
                    "/operators/fit/window_sessions": {"values": [20, 40]},
                },
            },
            "validation": {
                "rules": {"outer_folds": 2, "inner_folds": 2},
                "outer_rounds": [
                    {
                        "outer_audit": {
                            "scoring_start": "2026-03-01",
                            "scoring_end": "2026-04-30",
                        }
                    }
                ],
            },
            "holdout": {
                "fold_window": {
                    "scoring_start": "2026-05-01",
                    "scoring_end": "2026-06-30",
                }
            },
            "lineage": {
                "parent_study_ids": ["c" * 64],
                "prior_unique_candidate_count": 3,
                "is_complete": True,
            },
            "execution": {"identity": {"source_sha256": "d" * 64}},
        },
        "lineage": {
            "parent_study_ids": ["c" * 64],
            "prior_unique_candidate_count": 3,
            "is_complete": True,
        },
        "identities": {
            "execution": {"source_sha256": "d" * 64},
            "dataset": {"snapshot_id": "b" * 64},
        },
        "execution_identity_drift": {
            "detected": True,
            "reason": "SOURCE_CHANGED",
        },
        "trials": [
            {
                "rank": 1,
                "trial_id": "trial-1",
                "parameter_identity": "e" * 64,
                "parameters": {"window_sessions": 20},
                "validation_score": 1.25,
                "independent_metrics": {"max_drawdown": -0.08},
                "eligible": False,
                "constraint_reasons": ["minimum_trades"],
                "experiment_bindings": [
                    {
                        "role": "OUTER_AUDIT",
                        "experiment_id": "f" * 64,
                        "attempt_id": "1" * 64,
                        "attempt_status": "RUNNING",
                    }
                ],
            }
        ],
        "events": [
            {
                "sequence": 1,
                "event_type": "STUDY_SUBMITTED",
                "occurred_at": "2026-08-28T08:00:00Z",
                "payload": {},
            }
        ],
    }


def test_study_json_routes_use_the_public_service(tmp_path: Path, monkeypatch):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    calls = []

    monkeypatch.setattr(
        app.state.studies,
        "list",
        lambda: calls.append(("list",)) or [_study_detail()],
    )
    monkeypatch.setattr(
        app.state.studies,
        "preview",
        lambda spec: calls.append(("preview", spec))
        or {"preview_digest": STUDY_ID, "frozen_plan": {"normalized_request": spec}},
    )
    monkeypatch.setattr(
        app.state.studies,
        "submit",
        lambda spec, *, expected_preview_digest, action_id: calls.append(
            ("submit", spec, expected_preview_digest, action_id)
        )
        or {"status": "SUBMITTED", "study_id": STUDY_ID},
    )
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
    }
    spec = {"schema_version": 1}

    listed = client.get("/api/studies")
    previewed = client.post("/api/studies/preview", json={"study": spec}, headers=headers)
    submitted = client.post(
        "/api/studies",
        json={
            "study": spec,
            "expected_preview_digest": STUDY_ID,
            "action_id": "web-submit",
        },
        headers=headers,
    )

    assert listed.json()["studies"][0]["study_id"] == STUDY_ID
    assert previewed.json()["preview"]["preview_digest"] == STUDY_ID
    assert submitted.status_code == 201
    assert submitted.json()["status"] == "SUBMITTED"
    assert calls == [
        ("list",),
        ("preview", spec),
        ("submit", spec, STUDY_ID, "web-submit"),
    ]


def test_study_pages_expose_research_evidence_and_escape_values(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    detail = _study_detail()
    monkeypatch.setattr(app.state.studies, "list", lambda: [detail])
    monkeypatch.setattr(app.state.studies, "detail", lambda study_id: detail)

    listing = client.get("/studies")
    study = client.get(f"/studies/{STUDY_ID}")
    report = client.get(f"/studies/{STUDY_ID}/report")

    assert listing.status_code == 200
    assert 'data-page="studies"' in listing.text
    assert study.status_code == 200
    assert 'data-page="study-detail"' in study.text
    assert "Trial ranking" in study.text
    assert all(
        label in study.text
        for label in (
            "Validation score",
            "Independent metrics",
            "Constraint reasons",
            "Parameter identity",
            "Experiment bindings",
            "Outer OOS evidence",
            "Terminal holdout evidence",
            "Study Lineage",
            "Current in-flight Attempt",
            "Execution identity drift",
        )
    )
    assert "&lt;Synthetic &amp; safe&gt;" in study.text
    assert "<Synthetic & safe>" not in study.text
    assert report.status_code == 200
    assert 'data-page="study-report"' in report.text
    assert "Outer OOS evidence" in report.text
    assert "Terminal holdout evidence" in report.text


def test_study_controls_work_as_plain_html_forms(tmp_path: Path, monkeypatch):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    calls = []
    monkeypatch.setattr(
        app.state.studies,
        "control",
        lambda study_id, operation, *, action_id: calls.append(
            (study_id, operation, action_id)
        )
        or {"status": "PAUSED", "study_id": study_id},
    )
    monkeypatch.setattr(
        app.state.studies,
        "advance",
        lambda study_id: calls.append((study_id, "ADVANCE"))
        or {"status": "ADVANCED", "study_id": study_id},
    )
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "content-type": "application/x-www-form-urlencoded",
    }

    paused = client.post(
        f"/studies/{STUDY_ID}/control",
        content=f"csrf_token={issued.csrf_token}&operation=PAUSE&action_id=pause-web",
        headers=headers,
        follow_redirects=False,
    )
    advanced = client.post(
        f"/studies/{STUDY_ID}/advance",
        content=f"csrf_token={issued.csrf_token}",
        headers=headers,
        follow_redirects=False,
    )

    assert paused.status_code == advanced.status_code == 303
    assert paused.headers["location"] == advanced.headers["location"] == (
        f"/studies/{STUDY_ID}"
    )
    assert calls == [(STUDY_ID, "PAUSE", "pause-web"), (STUDY_ID, "ADVANCE")]


def test_study_wizard_and_submit_work_without_javascript(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot_id = snapshot(app)

    wizard = client.get("/studies/new")

    assert wizard.status_code == 200
    assert 'data-page="study-new"' in wizard.text
    assert 'data-testid="study-form"' in wizard.text
    assert "<noscript" in wizard.text
    assert all(
        f'name="{name}"' in wizard.text
        for name in (
            "dataset_id",
            "start_date",
            "end_date",
            "unique_trial_budget",
            "max_suggestions",
            "outer_folds",
            "inner_folds",
            "scoring_sessions",
            "holdout_sessions",
            "parent_study_ids",
            "lineage_complete",
        )
    )
    form = _experiment_form(app, snapshot_id, issued.csrf_token)
    form.update(
        {
            "search__fit__prior_log_ols__1.0.0__window_sessions": "[2]",
            "suggester": "GRID",
            "seed": "17",
            "unique_trial_budget": "1",
            "max_suggestions": "1",
            "outer_folds": "1",
            "inner_folds": "1",
            "scoring_sessions": "1",
            "minimum_training_sessions": "2",
            "purge_sessions": "0",
            "holdout_sessions": "1",
            "evaluation_version": "latest",
            "parent_study_ids": "",
            "prior_unique_candidate_count": "0",
            "lineage_complete": "true",
        }
    )
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "content-type": "application/x-www-form-urlencoded",
    }
    preview = client.post("/studies/preview", data=form, headers=headers)

    assert preview.status_code == 200
    assert 'data-page="study-preview"' in preview.text
    assert "Split preview" in preview.text
    assert "Experiment bindings" in preview.text
    values = {
        name: html.unescape(
            re.search(
                rf'name="{name}"[^>]*value="([^"]*)"',
                preview.text,
            ).group(1)
        )
        for name in ("action_id", "expected_preview_digest")
    }
    values["csrf_token"] = issued.csrf_token
    values["study_json"] = html.unescape(
        re.search(
            r'<textarea name="study_json" hidden>(.*?)</textarea>',
            preview.text,
        ).group(1)
    )
    submitted = client.post(
        "/studies",
        data=values,
        headers=headers,
        follow_redirects=False,
    )

    assert submitted.status_code == 303
    assert submitted.headers["location"].startswith("/studies/")
