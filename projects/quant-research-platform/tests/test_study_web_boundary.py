from pathlib import Path

from quant_platform.study_web import STUDY_ROUTE_INVENTORY

from test_web_api import make_app


EXPECTED_STUDY_ROUTE_INVENTORY = (
    ("GET", "/api/studies"),
    ("POST", "/api/lightweight-studies"),
    ("GET", "/api/lightweight-studies/{study_id}"),
    ("POST", "/api/studies/preview"),
    ("POST", "/api/studies"),
    ("GET", "/api/studies/{study_id}"),
    ("POST", "/api/studies/{study_id}/advance"),
    ("POST", "/api/studies/{study_id}/control"),
    ("GET", "/studies"),
    ("GET", "/studies/new"),
    ("GET", "/studies/new/lightweight"),
    ("GET", "/studies/new/legacy"),
    ("POST", "/studies/lightweight"),
    ("POST", "/studies/lightweight/msft"),
    ("GET", "/studies/lightweight/{study_id}"),
    ("POST", "/studies/preview"),
    ("POST", "/studies/edit"),
    ("POST", "/studies"),
    ("POST", "/studies/{study_id}/advance"),
    ("POST", "/studies/{study_id}/control"),
    ("GET", "/studies/{study_id}/report"),
    ("GET", "/studies/{study_id}"),
)


def test_study_route_inventory_is_one_explicit_contract(tmp_path: Path) -> None:
    app, _client = make_app(tmp_path)
    registered = [
        (method, route.path)
        for route in app.routes
        if route.path.startswith(("/studies", "/api/studies", "/api/lightweight-studies"))
        for method in sorted(route.methods)
    ]

    assert STUDY_ROUTE_INVENTORY == EXPECTED_STUDY_ROUTE_INVENTORY
    assert registered == list(EXPECTED_STUDY_ROUTE_INVENTORY)
