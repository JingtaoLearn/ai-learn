from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest
import yaml


PROJECT = Path(__file__).parents[1]
PRODUCTION = PROJECT / "production"


def _bind_source_for_container_path(service: dict, container_path: PurePosixPath) -> PurePosixPath:
    candidates: list[tuple[int, PurePosixPath]] = []
    for volume in service["volumes"]:
        if volume["type"] != "bind":
            continue
        target = PurePosixPath(volume["target"])
        if container_path == target or target in container_path.parents:
            source = PurePosixPath(volume["source"]) / container_path.relative_to(target)
            candidates.append((len(target.parts), source))
    assert candidates, f"no bind backs {container_path}"
    return max(candidates)[1]


def test_writer_publication_and_report_edge_resolve_to_one_host_tree() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text())
    api = compose["services"]["production-api"]
    edge = compose["services"]["report-edge"]
    state_root = PurePosixPath(api["environment"]["QR_PRODUCTION_STATE_ROOT"])
    writer_current = _bind_source_for_container_path(api, state_root / "publication" / "current")
    edge_current = _bind_source_for_container_path(edge, PurePosixPath("/srv/reports"))

    assert writer_current == edge_current == PurePosixPath(
        "/var/lib/quantresearch-production/state/publication/current"
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