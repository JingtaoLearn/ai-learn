import html
import re
from copy import deepcopy
from pathlib import Path

import pytest

from quant_platform.parameter_study import (
    ParameterStudy,
    StudyValidationError,
)
from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.web import _json_text, _study_from_form

from test_parameter_study import (
    EXECUTION_IDENTITY,
    _minimal_orchestration_spec,
    _study_service,
)
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
        "drift": {
            "reason": "SOURCE_CHANGED",
        },
        "trials": [
            {
                "candidate_digest": "e" * 64,
                "configuration": {"window_sessions": 20},
                "first_search_round": "FINAL",
                "proposal_sequence": 1,
                "classification": "IN_RANGE",
                "created_at": "2026-08-28T08:01:00Z",
            }
        ],
        "rankings": [
            {
                "candidate_digest": "e" * 64,
                "champion_eligible": False,
                "eligible": False,
                "validation_score": 1.25,
                "studied_parameters": {"/operators/fit/window_sessions": 20},
                "independent_metrics": {
                    "maximum_drawdown": -0.08,
                    "annual_turnover": 12.5,
                },
                "policy_identity": {
                    "tie_break": [
                        "lower_maximum_drawdown",
                        "lower_annual_turnover",
                        "strategy_configuration_digest",
                    ]
                },
                "tie_break": {
                    "lower_maximum_drawdown": -0.08,
                    "lower_annual_turnover": 12.5,
                    "strategy_configuration_digest": "e" * 64,
                },
                "explanation": {
                    "formula": (
                        "median(fold_net_sharpe)"
                        "-stability_weight*MAD(fold_net_sharpe)"
                        "-turnover_weight*annual_turnover"
                    ),
                    "components": {
                        "median_fold_net_sharpe": 2.0,
                        "stability_weight": 0.5,
                        "mad_fold_net_sharpe": 0.5,
                        "turnover_weight": 0.05,
                        "annual_turnover": 12.5,
                    },
                    "constraint_failures": ["minimum_trades"],
                },
            }
        ],
        "unranked_trials": [],
        "decision_summary": {
            "claim": "TIE_BROKEN_BY_FROZEN_RULE",
            "champion_candidate_digest": "e" * 64,
            "champion_parameters": {"/operators/fit/window_sessions": 20},
            "validation_score": 1.25,
            "primary_ties": [
                {
                    "candidate_digest": "2" * 64,
                    "studied_parameters": {"/operators/fit/window_sessions": 10},
                }
            ],
            "outer_selections": [
                {
                    "search_round": "OUTER:1",
                    "candidate_digest": "3" * 64,
                    "studied_parameters": {"/operators/fit/window_sessions": 30},
                }
            ],
            "outer_stability": "DIVERGENT",
            "statistical_significance": "NOT_ESTABLISHED",
            "rationale": "The champion won only through the frozen tie-break.",
        },
        "bindings": [
            {
                "candidate_digest": "e" * 64,
                "role": "OUTER_AUDIT",
                "experiment_id": "f" * 64,
                "attempt_id": "1" * 64,
                "state": "SUBMITTED",
                "attempt": {
                    "status": "RUNNING",
                    "comparison": None,
                },
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


def test_study_html_creation_uses_only_the_public_parameter_study_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    form = _experiment_form(app, snapshot(app), issued.csrf_token)
    form.update(
        {
            "study__fit__prior_log_ols__1.0.0__window_sessions": "int",
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
    creation = app.state.studies.creation_options()
    spec = _study_from_form(form, creation=creation)
    preview = app.state.studies.preview(spec)
    monkeypatch.setattr(app.state.studies, "creation_options", lambda: creation)
    monkeypatch.setattr(app.state.studies, "preview", lambda value: preview)

    def forbidden(*args, **kwargs):
        raise AssertionError("Study Web bypassed the ParameterStudy public seam")

    monkeypatch.setattr(app.state.catalog, "template_detail", forbidden)
    monkeypatch.setattr(app.state.datasets, "list_available", forbidden)
    monkeypatch.setattr(app.state.operators, "list", forbidden)

    page = client.get("/studies/new")
    posted = client.post(
        "/studies/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert page.status_code == 200
    assert posted.status_code == 200


def test_study_pages_expose_research_evidence_and_escape_values(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    detail = _study_detail()
    incomplete_trial = deepcopy(detail["trials"][0])
    incomplete_trial["candidate_digest"] = "9" * 64
    detail["trials"].append(incomplete_trial)
    detail["unranked_trials"].append(
        {
            "candidate_digest": incomplete_trial["candidate_digest"],
            "proposal_sequence": incomplete_trial["proposal_sequence"],
            "studied_parameters": {"/operators/fit/window_sessions": 20},
            "missing_canonical_fold_evidence": [
                {
                    "search_round": "FINAL",
                    "role": "INNER_SCORE",
                    "fold_sequence": 1,
                    "status": "NO_BINDING",
                    "binding_id": None,
                    "attempt_id": None,
                }
            ],
        }
    )
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
    assert "Best observed parameters" in study.text
    assert "TIE_BROKEN_BY_FROZEN_RULE" in study.text
    assert "DIVERGENT" in study.text
    assert "NOT_ESTABLISHED" in study.text
    assert "/operators/fit/window_sessions" in study.text
    assert 'class="study-ranking-table"' in study.text
    assert "Unranked Trial 1" in study.text
    assert "FINAL INNER_SCORE fold 1" in study.text
    assert all(
        label in study.text
        for label in (
            "Validation score",
            "Independent metrics",
            "Constraint reasons",
            "Studied parameters",
            "Experiment bindings",
            "Planned outer OOS windows",
            "Terminal holdout plan and state",
            "Study Lineage",
            "Study coordinator lease",
            "Execution identity",
        )
    )
    assert "These chronological windows describe the frozen protocol" in study.text
    assert "Verified report" not in study.text
    assert "&lt;Synthetic &amp; safe&gt;" in study.text
    assert "<Synthetic & safe>" not in study.text
    assert report.status_code == 200
    assert 'data-page="study-report"' in report.text
    assert "Planned outer OOS windows" in report.text
    assert "Terminal holdout plan and state" in report.text
    ranking = study.text.split('data-testid="ranking-1"', 1)[1].split("</tr>", 1)[0]
    assert ranking.index("/operators/fit/window_sessions") < ranking.index("#1")
    assert ranking.index("#1") < ranking.index("e" * 64)
    assert "Score formula" in ranking
    assert (
        "median(fold_net_sharpe)-stability_weight*MAD(fold_net_sharpe)"
        "-turnover_weight*annual_turnover"
    ) in ranking
    assert "Score components" in ranking
    assert ranking.index("lower_maximum_drawdown") < ranking.index(
        "lower_annual_turnover"
    )
    assert ranking.index("lower_annual_turnover") < ranking.index(
        "strategy_configuration_digest"
    )
    assert "Ineligible" in ranking
    assert "minimum_trades" in ranking
    assert study.text.index('data-testid="decision-summary"') < study.text.index(
        'id="holdout-evidence-heading"'
    )
    assert study.text.index('id="holdout-evidence-heading"') < study.text.index(
        'id="outer-plan-heading"'
    )
    assert study.text.index('id="outer-plan-heading"') < study.text.index(
        'id="trial-ranking-heading"'
    )
    assert "Study coordinator lease details" in study.text
    assert "Study event history" in study.text
    for label in ("Sequence", "Event", "Occurred"):
        assert f'data-label="{label}"' in study.text


def test_completed_study_replaces_controls_and_identifies_unranked_trials(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    detail = _study_detail()
    detail.update(
        phase="COMPLETED",
        control_status="ACTIVE",
        selection_outcome="CHAMPION_SELECTED",
    )
    incomplete_trial = deepcopy(detail["trials"][0])
    incomplete_trial.update(
        candidate_digest="9" * 64,
        proposal_sequence=2,
    )
    detail["trials"].append(incomplete_trial)
    detail["unranked_trials"] = [
        {
            "candidate_digest": incomplete_trial["candidate_digest"],
            "proposal_sequence": 2,
            "studied_parameters": {"/operators/fit/window_sessions": 40},
            "missing_canonical_fold_evidence": [
                {
                    "search_round": "FINAL",
                    "role": "INNER_SCORE",
                    "fold_sequence": 1,
                    "status": "FAILED",
                    "binding_id": "8" * 64,
                    "attempt_id": "7" * 64,
                },
                {
                    "search_round": "FINAL",
                    "role": "INNER_SCORE",
                    "fold_sequence": 2,
                    "status": "NO_BINDING",
                    "binding_id": None,
                    "attempt_id": None,
                },
            ],
        }
    ]
    monkeypatch.setattr(app.state.studies, "detail", lambda study_id: detail)

    response = client.get(f"/studies/{STUDY_ID}")

    assert response.status_code == 200
    assert "This Study is complete. Its frozen evidence is read-only." in response.text
    assert f'action="/studies/{STUDY_ID}/advance"' not in response.text
    assert f'action="/studies/{STUDY_ID}/control"' not in response.text
    assert "Unranked Trial 2" in response.text
    assert incomplete_trial["candidate_digest"] in response.text
    assert "/operators/fit/window_sessions" in response.text
    assert "FINAL INNER_SCORE fold 1" in response.text
    assert "FAILED" in response.text
    assert "FINAL INNER_SCORE fold 2" in response.text
    assert "NO_BINDING" in response.text
    assert "Trial and binding records remain visible" not in response.text


def test_study_detail_and_report_render_optional_suggestion_journal(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    detail = _study_detail()
    detail["suggestion_journal"] = [
        {
            "search_round": "OUTER:1",
            "proposal_sequence": 1,
            "changed_parameters": {"/operators/fit/window_sessions": 40},
            "tell": {"state": "COMPLETE", "objective": 1.25},
        },
        {
            "search_round": "FINAL",
            "proposal_sequence": 2,
            "changed_parameters": {"/operators/fit/window_sessions": 60},
            "tell": {"state": "FAIL", "objective": None},
        },
    ]
    monkeypatch.setattr(app.state.studies, "detail", lambda study_id: detail)

    for path in (f"/studies/{STUDY_ID}", f"/studies/{STUDY_ID}/report"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'data-testid="suggestion-journal"' in response.text
        assert response.text.index("OUTER:1") < response.text.index("FINAL")
        assert "/operators/fit/window_sessions" in response.text
        assert "COMPLETE" in response.text
        assert "1.25" in response.text
        assert "FAIL" in response.text
        journal = response.text.split('data-testid="suggestion-journal"', 1)[1]
        complete = journal.index("COMPLETE")
        details = journal.index("Ask/tell event details")
        changed = journal.index("/operators/fit/window_sessions")
        assert complete < details < changed


def test_old_studies_without_suggestion_journal_render_safely(tmp_path: Path, monkeypatch):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    detail = _study_detail()
    monkeypatch.setattr(app.state.studies, "detail", lambda study_id: detail)

    missing = client.get(f"/studies/{STUDY_ID}")
    detail["suggestion_journal"] = []
    empty = client.get(f"/studies/{STUDY_ID}/report")

    assert missing.status_code == empty.status_code == 200
    assert "No adaptive suggestions have been journaled." in missing.text
    assert "No adaptive suggestions have been journaled." in empty.text


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
    assert paused.headers["location"].startswith(f"/studies/{STUDY_ID}?status=PAUSED.")
    assert advanced.headers["location"].startswith(
        f"/studies/{STUDY_ID}?status=ADVANCED."
    )
    assert calls == [(STUDY_ID, "PAUSE", "pause-web"), (STUDY_ID, "ADVANCE")]


def test_study_wizard_preserves_invalid_values_and_identifies_errors(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    form = _experiment_form(app, snapshot(app), issued.csrf_token)
    form.update(
        {
            "study__fit__prior_log_ols__1.0.0__window_sessions": "int",
            "search__fit__prior_log_ols__1.0.0__window_sessions": "[2]",
            "unique_trial_budget": "not-a-number",
        }
    )

    response = client.post(
        "/studies/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert response.status_code == 400
    assert 'role="alert"' in response.text
    assert 'href="#unique_trial_budget"' in response.text
    assert re.search(
        r'id="unique_trial_budget"[^>]*value="not-a-number"[^>]*aria-invalid="true"',
        response.text,
    )
    assert 'aria-describedby="unique_trial_budget-error"' in response.text
    assert response.text.count('<fieldset class="parameter-set"') == response.text.count(
        "Used when this exact published version is selected."
    )


def test_invalid_finite_range_identifies_the_search_field(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    field = "search__fit__prior_log_ols__1.0.0__window_sessions"
    form = _experiment_form(app, snapshot(app), issued.csrf_token)
    form["study__fit__prior_log_ols__1.0.0__window_sessions"] = "int"
    form[field] = "[2,"

    response = client.post(
        "/studies/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert response.status_code == 400
    assert f'href="#{field}"' in response.text
    control = re.search(rf'<input[^>]*id="{field}"[^>]*>', response.text).group(0)
    assert 'value="[2,"' in control
    assert 'aria-invalid="true"' in control
    assert f'aria-describedby="{field}-error"' in control
    assert "autofocus" in control
    assert f'id="{field}-error"' in response.text


def _typed_study_operator(app) -> None:
    app.state.catalog.insert_operator_version_for_test(
        operator_id="typed_study_fit",
        slot="fit",
        version="1.0.0",
        content_digest="8" * 64,
        parameter_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["slow", "fast"]},
                "window": {"type": "integer", "minimum": 2, "maximum": 20},
                "threshold": {"type": "number", "minimum": 0.1, "maximum": 2.0},
                "enabled": {"type": "boolean"},
            },
            "required": ["enabled", "mode", "threshold", "window"],
            "additionalProperties": False,
        },
        defaults={"mode": "slow", "window": 4, "threshold": 0.5, "enabled": True},
    )


def _typed_study_form(app, csrf_token: str) -> tuple[dict[str, str], dict]:
    form = _experiment_form(app, snapshot(app), csrf_token)
    form["operator_fit_selector"] = "typed_study_fit@latest"
    for name, value in {
        "mode": '"slow"',
        "window": "4",
        "threshold": "0.5",
        "enabled": "true",
    }.items():
        form[f"operator_fit_param__typed_study_fit__1.0.0__{name}"] = value
    return form, app.state.studies.creation_options()


def test_study_wizard_renders_explicit_schema_typed_parameter_selectors(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    snapshot(app)
    _typed_study_operator(app)

    response = client.get("/studies/new")

    assert response.status_code == 200
    for name, kind in (
        ("mode", "categorical"),
        ("window", "int"),
        ("threshold", "float"),
        ("enabled", "categorical"),
    ):
        selection = f"study__fit__typed_study_fit__1.0.0__{name}"
        assert re.search(
            rf'<input\b[^>]*type="checkbox"[^>]*name="{selection}"'
            rf'[^>]*value="{kind}"',
            response.text,
        )
        assert f'data-domain-editor="{selection}"' in response.text
        assert f"Fixed {name}" in response.text
    assert "OPTUNA_TPE" in response.text
    assert "Optuna TPE" in response.text
    assert "Adaptive unique Trial budget" in response.text
    assert "Adaptive raw suggestion budget" in response.text
    assert "study__cost__" not in response.text
    assert "study__report__" not in response.text


def test_typed_parameter_domains_emit_the_backend_contract(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    _typed_study_operator(app)
    form, creation = _typed_study_form(app, issued.csrf_token)
    form.update(
        {
            "suggester": "OPTUNA_TPE",
            "study__fit__typed_study_fit__1.0.0__mode": "categorical",
            "domain__fit__typed_study_fit__1.0.0__mode__choices": '["slow","fast"]',
            "study__fit__typed_study_fit__1.0.0__window": "int",
            "domain__fit__typed_study_fit__1.0.0__window__low": "2",
            "domain__fit__typed_study_fit__1.0.0__window__high": "10",
            "domain__fit__typed_study_fit__1.0.0__window__step": "2",
            "study__fit__typed_study_fit__1.0.0__threshold": "float",
            "domain__fit__typed_study_fit__1.0.0__threshold__low": "0.1",
            "domain__fit__typed_study_fit__1.0.0__threshold__high": "1.5",
            "domain__fit__typed_study_fit__1.0.0__threshold__log": "true",
        }
    )

    spec = _study_from_form(form, creation=creation)

    assert spec["search"]["suggester"] == "OPTUNA_TPE"
    assert spec["search"]["space"] == {
        "/operators/fit/mode": {
            "kind": "categorical",
            "choices": ["slow", "fast"],
        },
        "/operators/fit/window": {
            "kind": "int",
            "low": 2,
            "high": 10,
            "step": 2,
            "log": False,
        },
        "/operators/fit/threshold": {
            "kind": "float",
            "low": 0.1,
            "high": 1.5,
            "log": True,
        },
    }


def test_integer_log_domain_omits_step_in_the_backend_contract(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    _typed_study_operator(app)
    form, creation = _typed_study_form(app, issued.csrf_token)
    form.update(
        {
            "suggester": "OPTUNA_TPE",
            "study__fit__typed_study_fit__1.0.0__window": "int",
            "domain__fit__typed_study_fit__1.0.0__window__low": "2",
            "domain__fit__typed_study_fit__1.0.0__window__high": "10",
            "domain__fit__typed_study_fit__1.0.0__window__log": "true",
        }
    )

    spec = _study_from_form(form, creation=creation)

    assert spec["search"]["space"]["/operators/fit/window"] == {
        "kind": "int",
        "low": 2,
        "high": 10,
        "log": True,
    }


@pytest.mark.parametrize(
    ("updates", "field", "message"),
    [
        (
            {"domain__fit__typed_study_fit__1.0.0__window__low": "2"},
            "domain__fit__typed_study_fit__1.0.0__window__low",
            "unchecked parameter",
        ),
        (
            {"study__fit__typed_study_fit__1.0.0__window": "float"},
            "study__fit__typed_study_fit__1.0.0__window",
            "must use int domain",
        ),
        (
            {
                "study__fit__typed_study_fit__1.0.0__window": "int",
                "domain__fit__typed_study_fit__1.0.0__window__low": "2",
                "domain__fit__typed_study_fit__1.0.0__window__high": "10",
                "domain__fit__typed_study_fit__1.0.0__window__step": "2",
                "domain__fit__typed_study_fit__1.0.0__window__log": "true",
            },
            "domain__fit__typed_study_fit__1.0.0__window__step",
            "step and log",
        ),
        (
            {
                "study__fit__typed_study_fit__1.0.0__window": "int",
                "domain__fit__typed_study_fit__1.0.0__window__low": "12",
                "domain__fit__typed_study_fit__1.0.0__window__high": "10",
            },
            "domain__fit__typed_study_fit__1.0.0__window__high",
            "high must be greater than or equal to low",
        ),
        (
            {
                "study__fit__typed_study_fit__1.0.0__mode": "categorical",
                "domain__fit__typed_study_fit__1.0.0__mode__choices": '["slow","unknown"]',
            },
            "domain__fit__typed_study_fit__1.0.0__mode__choices",
            "must be one of",
        ),
    ],
)
def test_typed_parameter_domains_fail_closed(
    tmp_path: Path,
    updates: dict[str, str],
    field: str,
    message: str,
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    _typed_study_operator(app)
    form, creation = _typed_study_form(app, issued.csrf_token)
    form["suggester"] = "OPTUNA_TPE"
    form.update(updates)

    with pytest.raises(StudyValidationError, match=message) as error:
        _study_from_form(form, creation=creation)

    assert str(error.value).startswith(f"{field}:")


def test_typed_parameter_domain_errors_preserve_checked_values(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    _typed_study_operator(app)
    form, _ = _typed_study_form(app, issued.csrf_token)
    selection = "study__fit__typed_study_fit__1.0.0__threshold"
    low = "domain__fit__typed_study_fit__1.0.0__threshold__low"
    high = "domain__fit__typed_study_fit__1.0.0__threshold__high"
    form.update(
        {
            "suggester": "OPTUNA_TPE",
            selection: "float",
            low: "not-a-number",
            high: "1.5",
        }
    )

    response = client.post(
        "/studies/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert response.status_code == 400
    assert f'href="#{low}"' in response.text
    assert re.search(rf'name="{selection}"[^>]*checked', response.text)
    assert re.search(rf'id="{low}"[^>]*value="not-a-number"[^>]*aria-invalid="true"', response.text)
    assert re.search(rf'id="{high}"[^>]*value="1.5"', response.text)
    assert f'id="{low}-error"' in response.text


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
            "study__fit__prior_log_ols__1.0.0__window_sessions": "int",
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
    assert "Candidate capacity" in preview.text
    assert "Minimum Experiment bindings" in preview.text
    assert "Conditional maximum bindings" in preview.text
    assert "Full immutable Study preview identity" in preview.text
    identity = preview.text.split("Full immutable Study preview identity", 1)[1]
    assert re.search(r"[0-9a-f]{64}", identity)
    assert preview.text.index("Study estimates") < preview.text.index(
        "Full immutable Study preview identity"
    )
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
    values["wizard_json"] = html.unescape(
        re.search(
            r'<textarea name="wizard_json" hidden>(.*?)</textarea>',
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


def test_preview_edit_preserves_complete_wizard_values(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    form = _experiment_form(app, snapshot(app), issued.csrf_token)
    range_field = "search__fit__prior_log_ols__1.0.0__window_sessions"
    start_date = form["start_date"]
    end_date = form["end_date"]
    form.update(
        {
            "study__fit__prior_log_ols__1.0.0__window_sessions": "int",
            range_field: "[2,3]",
            "unique_trial_budget": "2",
            "max_suggestions": "3",
            "parent_study_ids": "",
            "prior_unique_candidate_count": "7",
            "lineage_complete": "false",
        }
    )
    headers = {"origin": "https://quant.ai.jingtao.fun"}

    preview = client.post("/studies/preview", data=form, headers=headers)
    wizard_json = html.unescape(
        re.search(
            r'<textarea name="wizard_json" hidden>(.*?)</textarea>',
            preview.text,
        ).group(1)
    )
    edited = client.post(
        "/studies/edit",
        data={"csrf_token": issued.csrf_token, "wizard_json": wizard_json},
        headers=headers,
    )

    assert edited.status_code == 200
    for name, value in (
        (range_field, "[2,3]"),
        ("start_date", start_date),
        ("end_date", end_date),
        ("unique_trial_budget", "2"),
        ("max_suggestions", "3"),
        ("prior_unique_candidate_count", "7"),
    ):
        assert re.search(
            rf'(?:name|id)="{re.escape(name)}"[^>]*value="{re.escape(value)}"',
            edited.text,
        )
    assert 'name="parent_study_ids"' in edited.text
    assert '<option value="false" selected>' in edited.text


def test_stale_study_submit_returns_a_fresh_reviewable_preview(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    fresh_digest = "b" * 64
    preview = {
        "preview_digest": fresh_digest,
        "execution_estimate": {
            "minimum_experiment_bindings": 2,
            "conditional_maximum_experiment_bindings": 4,
            "selection_dependent_bindings": 2,
            "rounds": [],
            "reuse_resolution": "CANONICAL_EXPERIMENT_IDENTITY_AT_DISPATCH",
        },
        "frozen_plan": {
            "search": {
                "unique_trial_budget": 1,
                "max_suggestions": 1,
                "candidate_capacity": 1,
            },
            "validation": {
                "rules": {"outer_account_policy": "FORCE_FLAT_WITH_COST"},
                "outer_rounds": [
                    {
                        "inner_folds": [{}],
                        "outer_audit": {
                            "scoring_start": "2026-01-03",
                            "scoring_end": "2026-01-03",
                        },
                    }
                ],
                "final_search_round": {"inner_folds": [{}]},
            },
            "holdout": {
                "fold_window": {
                    "scoring_start": "2026-01-04",
                    "scoring_end": "2026-01-04",
                }
            },
            "dataset": {
                "dataset_id": "SYNTH.SS",
                "name": "Synthetic",
                "snapshot_id": "c" * 64,
            },
            "template": {
                "name": "single_stock_daily_causal",
                "version": "1",
                "content_digest": "e" * 64,
            },
            "evaluation": {
                "policy_id": "robust_walk_forward",
                "resolved_version": "1.0.0",
                "content_digest": "f" * 64,
            },
            "execution": {"identity": {"source_sha256": "d" * 64}},
        },
    }
    monkeypatch.setattr(
        app.state.studies,
        "submit",
        lambda *args, **kwargs: {
            "status": "PREVIEW_STALE",
            "expected_preview_digest": STUDY_ID,
            "current_preview_digest": fresh_digest,
        },
    )
    monkeypatch.setattr(app.state.studies, "preview", lambda value: preview)

    response = client.post(
        "/studies",
        data={
            "csrf_token": issued.csrf_token,
            "action_id": "stale-submit",
            "expected_preview_digest": STUDY_ID,
            "study_json": '{"schema_version":1}',
            "wizard_json": "{}",
        },
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert response.status_code == 409
    assert 'data-testid="stale-preview"' in response.text
    assert fresh_digest in response.text
    assert "Nothing was created" in response.text


def test_real_parameter_study_evidence_renders_through_the_public_web_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    studies, experiments = _study_service(tmp_path / "domain")
    spec = _minimal_orchestration_spec()
    preview = studies.preview(spec)
    submitted = studies.submit(
        spec,
        expected_preview_digest=preview["preview_digest"],
        action_id="submit-real-web-integration",
    )
    runner = ResolvedAttemptExecutor(
        studies.catalog,
        output_root=studies.catalog.state_root / "study-runs",
        project_root=Path(__file__).parents[1],
        attempt_controller=experiments,
        identity_provider=lambda project_root, runner_image: EXECUTION_IDENTITY,
    )

    def execute(effect: dict, action_id: str) -> dict:
        attempt = experiments.claim_next_attempt()
        assert attempt is not None
        assert attempt["attempt_id"] == effect["attempt_id"]
        experiments.record_physical_launch(
            attempt["attempt_id"],
            container_name=f"study-{attempt['attempt_id'][:12]}",
        )
        result = runner(attempt)
        experiments.record_termination(
            attempt["attempt_id"],
            exit_status=0,
            outcome="SUCCEEDED",
        )
        experiments.finish_success(
            attempt["attempt_id"],
            result_path=result["result_path"],
            result_digest=result["result_digest"],
            logs=result["logs"],
        )
        return {
            "experiment_id": effect["experiment_id"],
            "attempt_id": effect["attempt_id"],
        }

    coordinator = ParameterStudy(
        studies.catalog,
        datasets=studies.datasets,
        experiments=experiments,
        release_locator="/srv/quant/releases/160",
        effect_executor=execute,
    )
    for _ in range(40):
        coordinator.advance(submitted["study_id"])
        detail = coordinator.detail(submitted["study_id"])
        if detail["phase"] == "HOLDOUT_READY":
            break
    else:
        pytest.fail("selection orchestration did not reach HOLDOUT_READY")

    assert detail["rankings"]
    assert detail["bindings"]
    web_root = tmp_path / "web"
    web_root.mkdir()
    app, client = make_app(web_root)
    authenticate(app, client)
    monkeypatch.setattr(app.state.studies, "detail", coordinator.detail)

    response = client.get(f"/studies/{submitted['study_id']}")

    assert response.status_code == 200
    assert str(detail["rankings"][0]["validation_score"]) in response.text
    assert detail["bindings"][0]["experiment_id"] in response.text


def test_deeply_nested_study_json_is_a_controlled_bad_request(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    nested = "[" * 1_100 + "0" + "]" * 1_100
    headers = {
        "origin": "https://quant.ai.jingtao.fun",
        "x-csrf-token": issued.csrf_token,
        "content-type": "application/json",
    }

    api = client.post(
        "/api/studies/preview",
        content='{"study":' + nested + "}",
        headers=headers,
    )
    html_response = client.post(
        "/studies",
        data={
            "csrf_token": issued.csrf_token,
            "action_id": "deep-study-json",
            "expected_preview_digest": STUDY_ID,
            "study_json": nested,
            "wizard_json": "{}",
        },
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert api.status_code == 400
    assert api.json()["error"]["code"] == "INVALID_JSON"
    assert "nesting limit" in api.json()["error"]["message"]
    assert html_response.status_code == 400
    assert "nesting limit" in html_response.text
    assert "RecursionError" not in api.text + html_response.text


def test_study_json_rejects_excessive_container_counts():
    containers = ",".join("{}" for _ in range(10_001))

    with pytest.raises(ValueError, match="container limit"):
        _json_text(f"[{containers}]", "study_json")


def test_study_json_rejects_excessive_scalar_counts():
    values = ",".join("0" for _ in range(20_000))

    with pytest.raises(ValueError, match="value limit"):
        _json_text(f"[{values}]", "study_json")


def test_study_not_found_and_mutation_outcomes_are_visible(
    tmp_path: Path, monkeypatch
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    headers = {"origin": "https://quant.ai.jingtao.fun"}

    missing_api = client.get("/api/studies/not-a-study")
    missing_html = client.get("/studies/not-a-study")

    assert missing_api.status_code == 404
    assert missing_api.json()["error"]["code"] == "NOT_FOUND"
    assert missing_html.status_code == 404

    monkeypatch.setattr(
        app.state.studies,
        "advance",
        lambda study_id: {
            "status": "EXECUTION_IDENTITY_DRIFT",
            "study_id": study_id,
        },
    )
    monkeypatch.setattr(app.state.studies, "detail", lambda study_id: _study_detail())
    response = client.post(
        f"/studies/{STUDY_ID}/advance",
        data={"csrf_token": issued.csrf_token},
        headers=headers,
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert 'role="status"' in response.text
    assert "Execution identity drift" in response.text
    forged = client.get(f"/studies/{STUDY_ID}?outcome=EFFECT_COMMITTED")
    assert "Study effect committed." not in forged.text


def test_invalid_wizard_preserves_values_and_links_accessible_errors(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot_id = snapshot(app)
    form = _experiment_form(app, snapshot_id, issued.csrf_token)
    form.update(
        {
            "study__fit__prior_log_ols__1.0.0__window_sessions": "int",
            "search__fit__prior_log_ols__1.0.0__window_sessions": "[2]",
            "suggester": "GRID",
            "seed": "17",
            "unique_trial_budget": "not-a-number",
            "max_suggestions": "2",
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

    response = client.post(
        "/studies/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert response.status_code == 400
    assert 'role="alert"' in response.text
    assert 'href="#unique_trial_budget"' in response.text
    control = re.search(
        r'<input\b[^>]*name="unique_trial_budget"[^>]*>',
        response.text,
    ).group(0)
    assert 'value="not-a-number"' in control
    assert 'aria-invalid="true"' in control
    assert 'aria-describedby="unique_trial_budget-error"' in control
    assert "prior_log_ols@1.0.0 parameters" in response.text


def test_study_pages_include_skip_navigation_and_non_scripted_system_theme(
    tmp_path: Path
):
    app, client = make_app(tmp_path)
    authenticate(app, client)

    page = client.get("/studies/new")
    css = client.get("/static/app.css").text

    assert 'class="skip-link" href="#main-content"' in page.text
    assert '<main id="main-content"' in page.text
    assert ':root:not([data-theme])' in css
    assert ".danger-button:hover" in css
    assert ".button.quiet:hover" in css
    assert "overflow-wrap: anywhere" in css
