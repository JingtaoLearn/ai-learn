from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


PROJECT = Path(__file__).parents[1]
PRODUCTION = PROJECT / "production"
SHARE_HOSTING = PROJECT.parent / "share-hosting"


def test_stable_reports_route_to_zhlearn_without_mutable_report_mount() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text())
    api = compose["services"]["production-api"]
    edge = compose["services"]["report-edge"]
    assert all(volume.get("target") != "/srv/reports" for volume in edge["volumes"])
    assert all("publication" not in volume.get("target", "") for volume in api["volumes"])

    production_nginx = (PRODUCTION / "nginx.conf").read_text()
    assert 'location ~ "^/api/v1/production/stable-reports/' in production_nginx
    assert "proxy_pass http://production_api;" in production_nginx

    share_nginx = (SHARE_HOSTING / "nginx.conf").read_text()
    for report_uuid in (
        "f642b386-74c0-4e9f-92e6-563e7c6a5d69",
        "8991e9a8-1caa-41f5-b76b-6368259db5b4",
    ):
        assert f"location = /{report_uuid}.html" in share_nginx
        assert (
            f"https://quant.ai.jingtao.fun/api/v1/production/stable-reports/{report_uuid}.html"
            in share_nginx
        )


@pytest.mark.parametrize(
    "header_variable",
    ["$http_cookie", "$http_authorization", "$http_x_csrf_token"],
)
def test_effective_nginx_ingress_rejects_forbidden_headers(header_variable: str) -> None:
    nginx = (PRODUCTION / "nginx.conf").read_text()
    selector_match = re.search(
        r'map "([^"]+)" \$forbidden_request_header \{\s*default 1;\s*"\|\|" 0;\s*\}',
        nginx,
    )
    assert selector_match is not None
    assert selector_match.group(1).split("|") == [
        "$http_cookie",
        "$http_authorization",
        "$http_x_csrf_token",
    ]
    assert header_variable in selector_match.group(1)

    api_location = nginx.split("location /api/v1/production/ {", 1)[1].split(
        "location / {", 1
    )[0]
    rejection = "if ($forbidden_request_header) { return 403;"
    assert rejection in api_location
    assert api_location.index(rejection) < api_location.index("proxy_pass http://production_api;")
    assert "proxy_set_header Cookie" not in api_location
    assert "proxy_set_header Authorization" not in api_location
    assert "proxy_set_header X-CSRF-Token" not in api_location
    assert "proxy_set_header X-QuantResearch-Verified-Client $ssl_client_s_dn;" in api_location