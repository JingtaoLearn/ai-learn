from pathlib import Path

from quant_platform.resolved_runner import ResolvedAttemptExecutor

from test_experiment_service import _task
from test_web_api import authenticate, make_app, snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    assert 'value="latest"' in response.text
    assert 'name="operator_fit_parameters"' in response.text
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
    report = client.get(f"/reports/{attempt['attempt_id']}")

    assert created["experiment_id"] in history.text
    assert 'data-testid="attempt-timeline"' in detail.text
    assert '<iframe sandbox=""' in detail.text
    assert report.status_code == 200
    assert report.headers["content-security-policy"].startswith("sandbox")
    assert "allow-scripts" not in detail.text
    assert "allow-same-origin" not in detail.text


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

    response = client.get(f"/reports/{attempt['attempt_id']}")

    assert response.status_code == 404
    assert "unsafe" not in response.text


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
