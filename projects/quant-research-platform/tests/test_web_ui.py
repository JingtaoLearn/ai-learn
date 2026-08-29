import json
import re
from pathlib import Path

from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.web import _task_from_form

from test_experiment_service import _task
from test_web_api import authenticate, make_app, snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _opening_control(html: str, name: str) -> str:
    match = re.search(
        rf'<(?:input|select)\b[^>]*name="{re.escape(name)}"[^>]*>',
        html,
    )
    assert match is not None, name
    return match.group(0)


def _experiment_form(app, snapshot_id: str, csrf_token: str) -> dict[str, str]:
    template = app.state.catalog.template_detail("single_stock_daily_causal", "1")
    form = {
        "csrf_token": csrf_token,
        "action_id": "preview-action",
        "dataset_id": "SYNTH.SS",
        "start_date": "2026-01-05",
        "end_date": "2026-01-12",
    }
    for name, value in template["defaults"].items():
        schema = template["parameter_schema"]["properties"][name]
        form[f"template_{name}"] = (
            json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            if "enum" in schema
            else ""
            if value is None
            else str(value)
        )
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
            schema = detail["parameter_schema"]["properties"][name]
            form[
                f"operator_{slot}_param__{operator['operator_id']}__"
                f"{detail['version']}__{name}"
            ] = (
                json.dumps(value, separators=(",", ":"), ensure_ascii=False)
                if "enum" in schema
                else str(value)
            )
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


def test_proofline_shell_maps_sections_and_keeps_mobile_utilities_native(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    snapshot(app)

    expected_sections = {
        "/": "Overview",
        "/operators": "Operators",
        "/operators/submit": "Operators",
        "/templates/single_stock_daily_causal/1": "Template",
        "/experiments/new": "New experiment",
        "/history": "History",
        "/studies": "Studies",
        "/studies/new": "Studies",
    }
    for route, section in expected_sections.items():
        html = client.get(route).text
        assert 'class="masthead"' in html
        assert 'class="task-rail"' in html
        assert 'class="utility-menu"' in html
        assert '<summary' in html
        assert f'>{section}</a>' in html.split('aria-current="page"', 1)[1]
        mobile = html.split('aria-label="Mobile primary navigation"', 1)[1].split(
            "</nav>", 1
        )[0]
        assert mobile.count("<a ") == 5
        for label in ("Overview", "Operators", "New experiment", "Studies", "History"):
            assert f">{label}</a>" in mobile
        assert 'method="post" action="/logout"' in html
        assert 'name="csrf_token"' in html
        assert app.state.auth.verify_session(
            client.cookies.get("quant_session")
        ).display_name in html

    themed = client.get("/?theme=dark")
    assert themed.status_code == 200
    assert '<html lang="en" data-theme="dark">' in themed.text
    assert "quant_theme=dark" in themed.headers["set-cookie"]
    assert 'href="/?theme=light"' in themed.text
    assert 'href="/?theme=system"' in themed.text

    login = client.get("/login")
    assert "Proofline" in login.text
    assert 'data-testid="login-panel"' in login.text


def test_new_experiment_primary_action_works_without_javascript(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    snapshot(app)

    response = client.get("/experiments/new")

    assert 'data-testid="experiment-form"' in response.text
    assert re.search(r'<select\b[^>]*name="dataset_id"', response.text)
    assert 'value="SYNTH.SS"' in response.text
    assert re.search(
        r'<input\b[^>]*name="start_date"[^>]*type="date"|'
        r'<input\b[^>]*type="date"[^>]*name="start_date"',
        response.text,
    )
    assert 'name="end_date"' in response.text
    assert 'value="2026-01-12"' in response.text
    assert 'max="2026-01-12"' in response.text
    assert snapshot(app) not in response.text
    assert 'data-slot="fit"' in response.text
    assert 'value="prior_log_ols@latest"' in response.text
    assert 'value="prior_log_ols@1.0.0"' in response.text
    assert 'data-testid="generated-params-fit-prior_log_ols-1.0.0"' in response.text
    assert "operator_fit_param__prior_log_ols__1.0.0__window_sessions" in response.text
    assert 'data-testid="preview-experiment"' in response.text
    assert "<noscript" in response.text


def test_schema_generated_controls_are_typed_and_enums_are_selects(tmp_path: Path):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    snapshot(app)
    app.state.catalog.insert_operator_version_for_test(
        operator_id="typed_fit",
        slot="fit",
        version="1.0.0",
        content_digest="8" * 64,
        parameter_schema={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "mode": {"type": "integer", "enum": [1, 2]},
            },
            "required": ["enabled", "mode"],
            "additionalProperties": False,
        },
        defaults={"enabled": True, "mode": 2},
    )

    response = client.get("/experiments/new")

    for name in (
        "template_initial_state",
        "template_terminal_handling",
        "operator_fit_param__prior_log_ols__1.0.0__price_column",
        "operator_fit_param__typed_fit__1.0.0__enabled",
        "operator_fit_param__typed_fit__1.0.0__mode",
    ):
        assert re.search(rf'<select\b[^>]*name="{re.escape(name)}"', response.text)
    assert re.search(
        r'<input\b[^>]*name="template_initial_capital_cny"[^>]*type="number"|'
        r'<input\b[^>]*type="number"[^>]*name="template_initial_capital_cny"',
        response.text,
    )
    assert 'step="any"' in response.text
    assert re.search(
        r'<input\b[^>]*name="operator_fit_param__prior_log_ols__1\.0\.0__window_sessions"'
        r'[^>]*type="number"|'
        r'<input\b[^>]*type="number"[^>]*name="operator_fit_param__prior_log_ols__1\.0\.0__window_sessions"',
        response.text,
    )
    assert 'value="false"' in response.text
    assert 'value="2" selected' in response.text


def test_nullable_controls_omit_required_and_submit_blank_as_json_null_without_js(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot_id = snapshot(app)
    nullable_schema = {
        "type": "object",
        "properties": {
            "choice": {
                "type": "string",
                "enum": ["strict", "relaxed"],
                "nullable": True,
            },
            "enabled": {"type": "boolean", "nullable": True},
            "threshold": {"type": "number", "nullable": True},
            "note": {"type": "string", "nullable": True},
        },
        "required": ["choice", "enabled", "note", "threshold"],
        "additionalProperties": False,
    }
    defaults = {name: None for name in nullable_schema["properties"]}
    app.state.catalog.insert_operator_version_for_test(
        operator_id="nullable_fit",
        slot="fit",
        version="1.0.0",
        content_digest="7" * 64,
        parameter_schema=nullable_schema,
        defaults=defaults,
    )

    response = client.get("/experiments/new")
    nullable_names = [
        f"operator_fit_param__nullable_fit__1.0.0__{name}"
        for name in nullable_schema["properties"]
    ]

    for name in nullable_names:
        assert " required" not in _opening_control(response.text, name)
    for name in (
        "template_initial_state",
        "operator_fit_param__prior_log_ols__1.0.0__window_sessions",
    ):
        assert " required" in _opening_control(response.text, name)

    form = _experiment_form(app, snapshot_id, issued.csrf_token)
    form["operator_fit_selector"] = "nullable_fit@latest"
    for name, schema in nullable_schema["properties"].items():
        field_name = f"operator_fit_param__nullable_fit__1.0.0__{name}"
        form[field_name] = "null" if "enum" in schema else ""
    task = _task_from_form(form, catalog=app.state.catalog)

    assert task["operators"]["fit"]["parameters"] == defaults
    preview = client.post(
        "/experiments/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )
    assert preview.status_code == 200
    assert "nullable_fit" in preview.text


def test_enum_controls_round_trip_canonical_json_types_without_collisions_or_xss(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    snapshot_id = snapshot(app)
    hostile = '</option><script>alert("enum")</script>'
    parameter_schema = {
        "type": "object",
        "properties": {
            "choice": {
                "type": "string",
                "enum": [None, "", "x", hostile],
                "nullable": True,
            },
            "enabled": {"type": "boolean", "enum": [True, False]},
            "count": {"type": "integer", "enum": [1, 2]},
            "ratio": {"type": "number", "enum": [1.5, 2.5]},
        },
        "required": ["choice", "count", "enabled", "ratio"],
        "additionalProperties": False,
    }
    app.state.catalog.insert_operator_version_for_test(
        operator_id="enum_fit",
        slot="fit",
        version="1.0.0",
        content_digest="6" * 64,
        parameter_schema=parameter_schema,
        defaults={"choice": None, "enabled": True, "count": 1, "ratio": 1.5},
    )

    response = client.get("/experiments/new")
    choice_name = "operator_fit_param__enum_fit__1.0.0__choice"
    choice_markup = response.text.split(f'name="{choice_name}"', 1)[1].split(
        "</select>", 1
    )[0]

    assert choice_markup.count('value="null"') == 1
    assert 'value="&#34;&#34;"' in choice_markup
    assert 'value="&#34;x&#34;"' in choice_markup
    assert ">None<" not in choice_markup
    assert "<script>" not in response.text
    assert "&lt;/option&gt;&lt;script&gt;" in response.text

    form = _experiment_form(app, snapshot_id, "csrf")
    form["operator_fit_selector"] = "enum_fit@latest"
    prefix = "operator_fit_param__enum_fit__1.0.0__"
    form.update(
        {
            f"{prefix}choice": '""',
            f"{prefix}enabled": "false",
            f"{prefix}count": "2",
            f"{prefix}ratio": "2.5",
        }
    )
    task = _task_from_form(form, catalog=app.state.catalog)
    assert task["operators"]["fit"]["parameters"] == {
        "choice": "",
        "enabled": False,
        "count": 2,
        "ratio": 2.5,
    }

    form[f"{prefix}choice"] = "null"
    null_task = _task_from_form(form, catalog=app.state.catalog)
    assert null_task["operators"]["fit"]["parameters"]["choice"] is None


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


def test_overview_and_catalog_surfaces_expose_mobile_records_and_grouped_evidence(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    authenticate(app, client)
    app.state.experiments.submit(_task(snapshot(app)), action_id="catalog-layout")

    dashboard = client.get("/").text
    submission = client.get("/operators/submit").text
    operator = client.get("/operators/prior_log_ols/1.0.0").text
    template = client.get("/templates/single_stock_daily_causal/1").text
    digest = app.state.catalog.operator_detail("prior_log_ols", "1.0.0")[
        "content_digest"
    ]

    assert "<caption>Recent experiment attempts</caption>" in dashboard
    assert 'class="record-table"' in dashboard
    for label in ("Attempt", "Experiment", "Status", "Created"):
        assert f'data-label="{label}"' in dashboard

    for heading in (
        "Operator identity",
        "Implementation source",
        "Contract and deterministic tests",
        "Documentation",
    ):
        assert heading in submission
    assert submission.count('class="form-section') == 4

    assert "<caption>Declared operator parameters</caption>" in operator
    assert 'class="record-table"' in operator
    assert f">{digest[:12]}…" in operator
    assert "Full immutable operator identity" in operator
    assert digest in operator.split("Full immutable operator identity", 1)[1]

    assert "<caption>Initial operator resolution by slot</caption>" in template
    assert "<caption>Template-owned parameters</caption>" in template
    assert template.count('class="record-table"') == 2
    for label in ("Slot", "Default operator", "Version", "Parameters"):
        assert f'data-label="{label}"' in template


def test_preview_renders_complete_resolved_audit_and_duplicate_link(tmp_path: Path):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot_id = snapshot(app)
    form = _experiment_form(app, snapshot_id, issued.csrf_token)
    task = _task_from_form(form, catalog=app.state.catalog)
    assert task["dataset"] == {
        "dataset_id": "SYNTH.SS",
        "start": "2026-01-05",
        "end": "2026-01-12",
    }
    assert task["template"]["parameters"]["initial_capital_cny"] == 100000.0
    assert task["template"]["parameters"]["terminal_handling"] == "mark_to_market"
    assert task["operators"]["fit"]["parameters"]["window_sessions"] == 20
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


def test_experiment_workflow_groups_inputs_and_preserves_preview_edits(
    tmp_path: Path,
):
    app, client = make_app(tmp_path)
    issued = authenticate(app, client)
    snapshot_id = snapshot(app)
    form = _experiment_form(app, snapshot_id, issued.csrf_token)
    form["template_initial_capital_cny"] = "123456.5"
    task = _task_from_form(form, catalog=app.state.catalog)
    created = app.state.experiments.submit(task, action_id="preview-preservation")

    new_page = client.get("/experiments/new").text
    for heading in (
        "Dataset and evaluation range",
        "Template parameters",
        "Published operators",
        "Review and submit",
    ):
        assert heading in new_page

    preview = client.post(
        "/experiments/preview",
        data=form,
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )
    assert preview.status_code == 200
    assert preview.text.index("Open existing experiment") < preview.text.index(
        "Edit selections"
    )
    assert 'method="post" action="/experiments/new"' in preview.text
    assert 'name="intent" value="edit"' in preview.text
    assert 'name="template_initial_capital_cny" value="123456.5"' in preview.text

    edited = client.post(
        "/experiments/new",
        data=form | {"intent": "edit"},
        headers={"origin": "https://quant.ai.jingtao.fun"},
    )
    assert edited.status_code == 200
    assert 'data-page="experiment-new"' in edited.text
    capital = _opening_control(edited.text, "template_initial_capital_cny")
    assert 'value="123456.5"' in capital
    assert created["experiment_id"] in preview.text


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
    assert f'href="/experiments/{created["experiment_id"]}"' in wrapper.text
    assert attempt["attempt_id"] in wrapper.text
    assert "Verified canonical report" in wrapper.text
    assert 'data-fullscreen-report' in wrapper.text
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
    assert 'data-testid="experiment-context"' in detail.text
    assert detail.text.index('data-testid="experiment-context"') < detail.text.index(
        'data-testid="experiment-dataset"'
    )
    assert "Full experiment identity and resolution audit" in detail.text
    assert 'data-label="Attempt"' in detail.text
    assert 'data-label="Status"' in detail.text
    assert "<caption>Canonical experiment history</caption>" in history.text
    assert 'class="record-table"' in history.text
    assert 'data-copy-value="' + created["experiment_id"] + '"' in history.text
    assert created["experiment_id"][:12] + "…" in history.text


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


def test_static_assets_match_proofline_tokens_and_accessibility_contract(
    tmp_path: Path,
):
    _, client = make_app(tmp_path)

    css = client.get("/static/app.css").text
    javascript = client.get("/static/app.js").text
    theme_init = client.get("/static/theme-init.js").text

    allowed_colors = {
        "#00677a",
        "#005364",
        "#00424f",
        "#f4f6f7",
        "#10191f",
        "#ffffff",
        "#e9eef1",
        "#dde5e9",
        "#11181c",
        "#46545d",
        "#596871",
        "#ccd6db",
        "#71808a",
        "#7127a8",
        "#146c43",
        "#e4f7ed",
        "#7a4d00",
        "#fff2cc",
        "#a3212b",
        "#fce7e8",
        "#115ea3",
        "#e5f1ff",
        "#0b1114",
        "#081014",
        "#111a1f",
        "#18242a",
        "#213038",
        "#f1f5f6",
        "#b7c2c7",
        "#9eadb4",
        "#34434b",
        "#657681",
        "#67d5ea",
        "#8de1f0",
        "#42b8d0",
        "#062027",
        "#7cdbee",
        "#d49cff",
        "#74d9a7",
        "#113427",
        "#ffd080",
        "#3b2b10",
        "#ff9da3",
        "#401b20",
        "#9cc8ff",
        "#142e4a",
    }
    for token in ("#00677a", "#f4f6f7", "#10191f", "#67d5ea", "#0b1114"):
        assert token in css
    assert {
        color.lower() for color in re.findall(r"#[0-9a-fA-F]{6}", css)
    } <= allowed_colors
    assert "--space-1: 4px" in css
    assert "--radius-panel: 6px" in css
    assert "height: 52px" in css
    assert "grid-template-columns: 240px minmax(0, 1fr)" in css
    assert "min-height: 44px" in css
    assert "min-height: 56px" in css
    assert "env(safe-area-inset-bottom)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css
    assert ".masthead :focus-visible,\n.mobile-nav :focus-visible" in css
    assert "outline-color: #ffffff" in css
    assert "overflow-x: auto" in css
    assert "linear-gradient" not in css
    assert "radial-gradient" not in css
    assert "backdrop-filter" not in css
    assert "box-shadow:" not in css
    assert "url(" not in css
    assert "innerHTML" not in javascript
    assert "quant:preview-settled" in javascript
    assert "new AbortController()" in javascript
    assert "request timed out after 10 seconds" in javascript
    assert "quant-theme" in theme_init
    assert "localStorage.getItem" in theme_init
    assert 'document.documentElement.dataset.theme = theme' in theme_init
    assert 'document.documentElement.classList.add("js")' in theme_init
    assert "matchMedia" in javascript
    assert "localStorage.setItem" in javascript
    assert "field.dataset.parameterEnum" in javascript
    assert "JSON.parse(field.value)" in javascript


def test_theme_bootstrap_precedes_css_and_selector_is_global(tmp_path: Path):
    app, client = make_app(tmp_path)
    login = client.get("/login")
    authenticate(app, client)

    for response in (login, client.get("/"), client.get("/experiments/new")):
        assert response.text.index('/static/theme-init.js') < response.text.index(
            '/static/app.css'
        )
        assert 'data-theme-selector' in response.text
        assert '<option value="light">Light</option>' in response.text
        assert '<option value="dark">Dark</option>' in response.text
        assert '<option value="system">System</option>' in response.text
        assert '<script src="/static/app.js" defer></script>' in response.text


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
