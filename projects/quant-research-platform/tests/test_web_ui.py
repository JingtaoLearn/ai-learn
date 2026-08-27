from pathlib import Path

from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.web import _task_from_form

from test_experiment_service import _task
from test_web_api import authenticate, make_app, snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _experiment_form(app, snapshot_id: str, csrf_token: str) -> dict[str, str]:
    template = app.state.catalog.template_detail("single_stock_daily_causal", "1")
    form = {
        "csrf_token": csrf_token,
        "action_id": "preview-action",
        "dataset": f"SYNTH.SS|{snapshot_id}",
    }
    for name, value in template["defaults"].items():
        form[f"template_{name}"] = "" if value is None else str(value)
    for slot in template["slots"]:
        operator = next(
            item
            for item in app.state.operators.list()
            if item["slot"] == slot
        )
        detail = app.state.operators.detail(
            operator["operator_id"], operator["latest_version"]
        )
        form[f"operator_{slot}_selector"] = f"{operator['operator_id']}@latest"
        for name, value in detail["defaults"].items():
            form[
                f"operator_{slot}_param__{operator['operator_id']}__"
                f"{detail['version']}__{name}"
            ] = str(value)
    return form


def test_primary_pages_have_semantic_browser_selectors(tmp_path: Path):
    app, client = make_app(tmp_path)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    authenticate(app, client)
    snapshot(app)

    expected = {
        "/": "dashboard",
        "/operators": "operators",
        "/operators/submit": "operator-submit",
        "/templates/single_stock_daily_causal/1": "template-detail",
        "/experiments/new": "experiment-new",
        "/history": "history",
    }
    for route, page in expected.items():
        response = client.get(route)
        assert response.status_code == 200, route
        assert f'data-page="{page}"' in response.text
        assert all(tag in response.text for tag in ("<nav", "<main", "data-testid="))


def test_new_experiment_primary_action_works_without_javascript(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    snapshot(app)

    response = client.get("/experiments/new")

    assert 'data-testid="experiment-form"' in response.text
    assert 'name="dataset"' in response.text
    assert 'data-slot="fit"' in response.text
    assert 'value="prior_log_ols@latest"' in response.text
    assert 'value="prior_log_ols@1.0.0"' in response.text
    assert 'data-testid="generated-params-fit-prior_log_ols-1.0.0"' in response.text
    assert "operator_fit_param__prior_log_ols__1.0.0__window_sessions" in response.text
    assert 'data-testid="preview-experiment"' in response.text
    assert "<noscript" in response.text


def test_operator_listing_escapes_user_controlled_text(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    with app.state.catalog.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE operators SET title_zh = ? WHERE operator_id = ?",
            ('<img src=x onerror="alert(1)">', "prior_log_ols"),
        )

    response = client.get("/operators")

    assert "&lt;img" in response.text
    assert "<img src=x" not in response.text


def test_operator_detail_renders_schema_defaults_latest_and_linked_history(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    authenticate(app, client)

    response = client.get("/operators/prior_log_ols/1.0.0")

    assert 'data-testid="operator-parameter-schema"' in response.text
    assert 'data-testid="operator-defaults"' in response.text
    assert 'data-testid="operator-version-history"' in response.text
    assert 'data-testid="operator-validation-evidence"' in response.text
    assert "Latest version" in response.text
    assert "Selected version" in response.text
    assert "window_sessions" in response.text
    assert "AdjustedClose" in response.text
    assert "trusted_builtin" in response.text
    assert 'href="/operators/prior_log_ols/1.0.0"' in response.text


def test_template_and_dashboard_show_slot_defaults_and_linked_recent_attempts(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    created = app.state.experiments.submit(
        _task(snapshot(app)), action_id="dashboard-create"
    )

    template = client.get("/templates/single_stock_daily_causal/1")
    dashboard = client.get("/")

    assert 'data-testid="template-slot-defaults"' in template.text
    for operator_id in (
        "prior_log_ols",
        "recursive_log_ema",
        "adjacent_curve_pct_slope",
        "post_start_threshold_crossing_hysteresis",
        "all_in_all_out_a_share_lots",
        "cms_china_a_share",
        "concise_chinese_causal_trade",
    ):
        assert operator_id in template.text
    assert 'data-testid="recent-attempts"' in dashboard.text
    assert f'href="/experiments/{created["experiment_id"]}"' in dashboard.text


def test_preview_renders_complete_resolved_audit_and_duplicate_link(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot_id = snapshot(app)
    form = _experiment_form(app, snapshot_id, issued.csrf_token)
    task = _task_from_form(form, catalog=app.state.catalog)
    created = app.state.experiments.submit(task, action_id="existing")

    response = client.post(
        "/experiments/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )

    assert response.status_code == 200
    assert 'data-testid="preview-identity"' in response.text
    assert "single_stock_daily_causal@1" in response.text
    assert snapshot_id in response.text
    for slot in app.state.catalog.template_detail(
        "single_stock_daily_causal", "1"
    )["slots"]:
        assert f'data-preview-slot="{slot}"' in response.text
    for label in (
        "Requested selector",
        "Latest at submission",
        "Resolved version",
        "Resolved digest",
        "Parameters",
    ):
        assert label in response.text
    assert "Existing experiment" in response.text
    assert f'href="/experiments/{created["experiment_id"]}"' in response.text


def test_history_detail_and_report_use_verified_sandbox_route(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    created = app.state.experiments.submit(
        _task(snapshot(app)), action_id="create"
    )
    attempt = app.state.experiments.claim_next_attempt()
    result = ResolvedAttemptExecutor(
        app.state.catalog,
        output_root=app.state.catalog.state_root / "experiment-runs",
        project_root=PROJECT_ROOT,
    )(attempt)
    app.state.experiments.finish_success(
        attempt["attempt_id"],
        result_path=result["result_path"],
        result_digest=result["result_digest"],
    )

    history = client.get("/history")
    detail = client.get(f"/experiments/{created['experiment_id']}")
    wrapper = client.get(f"/reports/{attempt['attempt_id']}")
    report = client.get(
        f"/reports/{attempt['attempt_id']}/content",
        headers={"sec-fetch-dest": "iframe", "sec-fetch-site": "same-origin"},
    )

    assert created["experiment_id"] in history.text
    assert 'data-testid="attempt-timeline"' in detail.text
    assert '<iframe sandbox="allow-scripts"' in detail.text
    assert wrapper.status_code == 200
    assert 'data-page="report-wrapper"' in wrapper.text
    assert report.status_code == 200
    report_csp = report.headers["content-security-policy"]
    assert report_csp.startswith("sandbox allow-scripts")
    assert "default-src 'none'" in report_csp
    assert "connect-src 'none'" in report_csp
    assert "script-src 'unsafe-inline'" in report_csp
    assert "style-src 'unsafe-inline'" in report_csp
    assert "img-src data:" in report_csp
    assert "allow-same-origin" not in detail.text
    assert "connect-src 'none'" not in detail.headers["content-security-policy"]
    assert 'data-testid="experiment-dataset"' in detail.text
    assert 'data-testid="template-parameters"' in detail.text
    assert 'data-testid="operator-resolution"' in detail.text
    assert 'data-testid="canonical-metrics"' in detail.text
    assert result["result_digest"] in detail.text
    assert "SYNTH.SS" in detail.text
    assert "window_sessions" in detail.text
    assert "latest" in detail.text
    assert "1.0.0" in detail.text
    assert "final_equity_cny" in detail.text


def test_history_filters_status_search_and_drift_functionally(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    snapshot_id = snapshot(app)
    succeeded = app.state.experiments.submit(
        _task(snapshot_id), action_id="succeeded"
    )
    attempt = app.state.experiments.claim_next_attempt()
    app.state.experiments.finish_failure(attempt["attempt_id"], "failure")
    pending_task = _task(snapshot_id)
    pending_task["template"]["parameters"]["initial_capital_cny"] = 200000.0
    pending = app.state.experiments.submit(pending_task, action_id="pending")

    response = client.get(
        f"/history?status=FAILED&search={succeeded['experiment_id'][:12]}&drift=current"
    )

    assert 'data-testid="history-filters"' in response.text
    assert "<th>Status</th>" in response.text
    assert "<th>Attempts</th>" in response.text
    assert "<th>Current latest drift</th>" in response.text
    assert succeeded["experiment_id"] in response.text
    assert pending["experiment_id"] not in response.text
    assert "FAILED" in response.text

    empty = client.get("/history?status=SUCCEEDED&search=not-found")
    assert 'data-testid="history-empty"' in empty.text

    app.state.catalog.insert_operator_version_for_test(
        operator_id="prior_log_ols",
        slot="fit",
        version="1.1.0",
        content_digest="9" * 64,
        parameter_schema=app.state.catalog.operator_detail(
            "prior_log_ols", "1.0.0"
        )["parameter_schema"],
    )
    drifted = client.get(
        f"/history?status=all&search={succeeded['experiment_id'][:12]}&drift=drifted"
    )
    current = client.get(
        f"/history?status=all&search={succeeded['experiment_id'][:12]}&drift=current"
    )
    assert succeeded["experiment_id"] in drifted.text
    assert 'data-testid="history-empty"' in current.text


def test_report_route_rejects_database_bound_symlink_escape(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    app.state.experiments.submit(_task(snapshot(app)), action_id="create")
    attempt = app.state.experiments.claim_next_attempt()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.html").write_text("<h1>unsafe</h1>", encoding="utf-8")
    linked = app.state.catalog.state_root / "experiment-runs" / "linked"
    linked.parent.mkdir()
    linked.symlink_to(outside, target_is_directory=True)
    app.state.experiments.finish_success(
        attempt["attempt_id"],
        result_path=str(linked),
        result_digest="a" * 64,
    )

    response = client.get(
        f"/reports/{attempt['attempt_id']}/content",
        headers={"sec-fetch-dest": "iframe", "sec-fetch-site": "same-origin"},
    )

    assert response.status_code == 404
    assert "unsafe" not in response.text


def test_report_route_rejects_any_tampered_run_artifact(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    app.state.experiments.submit(_task(snapshot(app)), action_id="create")
    attempt = app.state.experiments.claim_next_attempt()
    result = ResolvedAttemptExecutor(
        app.state.catalog,
        output_root=app.state.catalog.state_root / "experiment-runs",
        project_root=PROJECT_ROOT,
    )(attempt)
    app.state.experiments.finish_success(
        attempt["attempt_id"],
        result_path=result["result_path"],
        result_digest=result["result_digest"],
    )
    metrics = Path(result["result_path"]) / "metrics.json"
    run_dir = metrics.parent
    run_dir.chmod(0o755)
    metrics.chmod(0o644)
    metrics.write_bytes(metrics.read_bytes() + b" ")
    metrics.chmod(0o444)
    run_dir.chmod(0o555)

    response = client.get(
        f"/reports/{attempt['attempt_id']}/content",
        headers={"sec-fetch-dest": "iframe", "sec-fetch-site": "same-origin"},
    )

    assert response.status_code == 404


def test_report_content_rejects_top_level_navigation(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    app.state.experiments.submit(_task(snapshot(app)), action_id="create")
    attempt = app.state.experiments.claim_next_attempt()
    result = ResolvedAttemptExecutor(
        app.state.catalog,
        output_root=app.state.catalog.state_root / "experiment-runs",
        project_root=PROJECT_ROOT,
    )(attempt)
    app.state.experiments.finish_success(
        attempt["attempt_id"],
        result_path=result["result_path"],
        result_digest=result["result_digest"],
    )

    response = client.get(f"/reports/{attempt['attempt_id']}/content")

    assert response.status_code == 403


def test_static_assets_match_linear_tokens_and_accessibility_contract(tmp_path: Path):
    _, client = make_app(tmp_path)

    css = client.get("/static/app.css").text
    javascript = client.get("/static/app.js").text

    for token in ("#08090a", "#0f1011", "#5e6ad2"):
        assert token in css
    assert "min-height: 44px" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "overflow-x: auto" in css
    assert "linear-gradient" not in css
    assert "backdrop-filter" not in css
    assert "innerHTML" not in javascript


def test_rendered_pages_have_no_inline_executable_content(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)

    response = client.get("/")

    assert "<script>" not in response.text
    assert "<style" not in response.text
    for handler in ("onclick=", "onchange=", "onsubmit=", "onerror="):
        assert handler not in response.text
    assert '<script src="/static/app.js" defer></script>' in response.text


def test_new_experiment_has_empty_and_live_resolution_states(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)

    empty = client.get("/experiments/new")
    assert 'data-testid="dataset-empty"' in empty.text

    snapshot(app)
    populated = client.get("/experiments/new")
    assert 'data-testid="resolved-summary"' in populated.text
    assert 'data-testid="live-duplicate-preview"' in populated.text
    assert "single_stock_daily_causal@1" in populated.text


def test_no_js_rerun_has_server_generated_id_and_rejects_extra_fields(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    created = app.state.experiments.submit(
        _task(snapshot(app)), action_id="create"
    )
    detail = client.get(f"/experiments/{created['experiment_id']}")
    marker = 'name="action_id" value="'
    action_id = detail.text.split(marker, 1)[1].split('"', 1)[0]
    assert action_id

    response = client.post(
        f"/experiments/{created['experiment_id']}/rerun",
        data={
            "csrf_token": issued.csrf_token,
            "action_id": action_id,
            "source": "forbidden",
        },
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )
    assert response.status_code == 400
    assert len(app.state.experiments.list_attempts(created["experiment_id"])) == 1
