from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import yaml


PROJECT = Path(__file__).parents[1]
PRODUCTION = PROJECT / "production"
PACKAGE = PROJECT / "src" / "quant_platform"
WRITE_SET = {
    "production/Dockerfile.api",
    "production/Dockerfile.api.dockerignore",
    "production/Dockerfile.provider-proxy",
    "production/Dockerfile.provider-proxy.dockerignore",
    "production/compose.yaml",
    "production/nginx.conf",
    "production/provider-proxy-policy.json",
    "production/release-manifest.schema.json",
    "src/quant_platform/production_contract.py",
    "src/quant_platform/production_store.py",
    "src/quant_platform/production_service.py",
    "src/quant_platform/production_worker.py",
    "src/quant_platform/production_web.py",
    "src/quant_platform/production_client.py",
    "src/quant_platform/production_jobs.py",
    "src/quant_platform/production_bocom.py",
    "src/quant_platform/production_gold.py",
    "src/quant_platform/production_result.py",
    "tests/test_production_contract.py",
    "tests/test_production_store.py",
    "tests/test_production_api.py",
    "tests/test_production_client.py",
    "tests/test_production_jobs.py",
    "tests/test_production_compose_contract.py",
    "tests/fixtures/production/bocom-yahoo-chart.json",
    "tests/fixtures/production/gold-au9999.tsv",
    "tests/fixtures/production/bocom-model-manifest.json",
    "tests/fixtures/production/gold-model-manifest.json",
    "tests/fixtures/production/expected-bocom-result.json",
    "tests/fixtures/production/expected-gold-result.json",
}


def test_exact_thirty_path_source_contract_exists_and_is_regular() -> None:
    assert len(WRITE_SET) == 30
    for relative in WRITE_SET:
        path = PROJECT / relative
        metadata = os.stat(path, follow_symlinks=False)
        assert path.is_file() and not path.is_symlink() and metadata.st_nlink == 1
    assert PROJECT / ".dockerignore" not in {PROJECT / item for item in WRITE_SET}


def test_dockerfile_specific_default_deny_rules_and_context_members() -> None:
    assert (PRODUCTION / "Dockerfile.api.dockerignore").read_text().splitlines() == [
        "*",
        "!README.md",
        "!pyproject.toml",
        "!requirements.lock",
        "!src/",
        "!src/**",
        "!production/",
        "!production/Dockerfile.api",
        "!production/Dockerfile.api.dockerignore",
    ]
    assert (PRODUCTION / "Dockerfile.provider-proxy.dockerignore").read_text().splitlines() == [
        "*",
        "!production/",
        "!production/Dockerfile.provider-proxy",
        "!production/Dockerfile.provider-proxy.dockerignore",
        "!production/provider-proxy-policy.json",
    ]
    app_members = {"README.md", "pyproject.toml", "requirements.lock"} | {
        path.relative_to(PROJECT).as_posix() for path in (PROJECT / "src").rglob("*") if path.is_file()
    }
    assert all(not path.is_symlink() for path in (PROJECT / "src").rglob("*"))
    assert all(not name.startswith("tests/") and ".env" not in name for name in app_members)
    proxy_members = {
        "production/Dockerfile.provider-proxy",
        "production/Dockerfile.provider-proxy.dockerignore",
        "production/provider-proxy-policy.json",
    }
    assert not proxy_members & app_members
    assert "COPY ." not in (PRODUCTION / "Dockerfile.api").read_text()
    assert "COPY ." not in (PRODUCTION / "Dockerfile.provider-proxy").read_text()


def test_compose_is_exact_three_service_three_network_no_build_authority() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text())
    assert set(compose["services"]) == {"production-api", "report-edge", "provider-egress-proxy"}
    assert set(compose["networks"]) == {
        "api_internal",
        "provider_proxy_internal",
        "provider_egress",
    }
    assert compose["services"]["production-api"]["networks"] == [
        "api_internal",
        "provider_proxy_internal",
    ]
    assert compose["services"]["provider-egress-proxy"]["networks"] == [
        "provider_proxy_internal",
        "provider_egress",
    ]
    for service in compose["services"].values():
        assert "build" not in service
        assert service["pull_policy"] == "never"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["user"] != "0:0"
        assert "${" in service["image"] and "IMAGE" in service["image"]


def test_proxy_policy_is_exact_and_not_duplicated_in_proxy_dockerfile() -> None:
    policy = json.loads((PRODUCTION / "provider-proxy-policy.json").read_bytes())
    hosts = [item["host"] for item in policy["allowed_destinations"]]
    assert hosts == ["query1.finance.yahoo.com", "vip.stock.finance.sina.com.cn"]
    assert [item["port"] for item in policy["allowed_destinations"]] == [443, 443]
    assert policy["methods"] == ["CONNECT"]
    assert all(policy[key] is True for key in policy if key.startswith("deny_"))
    dockerfile = (PRODUCTION / "Dockerfile.provider-proxy").read_text()
    assert all(host not in dockerfile for host in hosts)
    assert dockerfile.count("class Handler") == 1
    assert "ipaddress.ip_address" in dockerfile and "is_global" in dockerfile
    assert "RUN python3 <<'PY'" in dockerfile


def test_client_static_import_surface_has_no_compute_renderer_or_fallback() -> None:
    source = (PACKAGE / "production_client.py").read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {"pandas", "numpy", "requests", "production_jobs", "production_gold", "production_bocom"}
    assert not imports & forbidden
    assert "HERMES_HOME" not in source
    assert "flearn" not in source.casefold()
    assert "fallback" not in source.casefold()


def test_nginx_has_fixed_routes_and_private_mtls_identity_rebuild() -> None:
    nginx = (PRODUCTION / "nginx.conf").read_text()
    assert nginx.count("8991e9a8-1caa-41f5-b76b-6368259db5b4.html") >= 2
    assert nginx.count("f642b386-74c0-4e9f-92e6-563e7c6a5d69.html") >= 2
    assert "ssl_protocols TLSv1.3" in nginx
    assert "ssl_verify_client on" in nginx
    assert 'proxy_set_header X-QuantResearch-Verified-Client ""' in nginx
    assert "proxy_set_header X-QuantResearch-Verified-Client $ssl_client_s_dn" in nginx
    assert "location /api" not in nginx.split("listen 8080", 1)[1].split("server {", 1)[0]
