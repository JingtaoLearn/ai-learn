from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_context_is_allowlisted_and_build_uses_hash_locked_dependencies():
    dockerignore = (ROOT / ".dockerignore").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerignore.splitlines()[0] == "*"
    assert "!.env" not in dockerignore
    assert "COPY ." not in dockerfile
    assert "@sha256:" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "requirements.lock" in dockerfile


def test_jupyter_is_loopback_only_authenticated_and_non_root():
    compose = (ROOT / "compose.yaml").read_text()
    assert '127.0.0.1:8888:8888' in compose
    assert "JUPYTER_TOKEN" in compose
    assert "ServerApp.token=" not in compose
    assert 'user: "1000:1000"' in compose


def test_ui_user_service_is_loopback_only_non_root_and_fail_closed():
    service = (ROOT / "deploy" / "quant-research-ui.service").read_text()
    environment = (ROOT / "deploy" / "quant-research-ui.env.example").read_text()

    assert "User=root" not in service
    assert "WorkingDirectory=/home/feng/quant-platform/current" in service
    assert "python -m quant_platform.web" in service
    assert "127.0.0.1:8090" not in service
    assert "QUANT_FORWARDED_ALLOW_IPS=127.0.0.1" in environment
    assert "QUANT_STATE_ROOT=/home/feng/quant-platform/state/ui" in environment
    assert "QUANT_AUTH_MODE=sso" in environment
    assert (
        "QUANT_SSO_AUDIENCE=https://quant.ai.jingtao.fun/auth/callback"
        in environment
    )
    assert (
        "QUANT_SSO_CALLBACK_URL=https://quant.ai.jingtao.fun/auth/callback"
        in environment
    )
    assert "AUTH_SHARED_SECRET=<" in environment
    assert "QUANT_SESSION_SECRET=<" in environment


def test_tunnel_and_proxy_share_one_resolved_gateway_without_public_port():
    repository = ROOT.parents[1]
    tunnel = (
        repository
        / "vm/host-services/quant-research-tunnel/run-tunnel.sh"
    ).read_text()
    unit = (
        repository
        / "vm/host-services/quant-research-tunnel/quant-research-tunnel.service"
    ).read_text()
    compose = (
        repository
        / "vm/docker-services/quant-research-ui-proxy/docker-compose.yml"
    ).read_text()
    nginx = (
        repository
        / "vm/docker-services/quant-research-ui-proxy/nginx.conf"
    ).read_text()
    probe = (
        repository
        / "vm/docker-services/quant-research-ui-proxy/check-health.sh"
    ).read_text()

    assert "docker network inspect nginx-proxy" in tunnel
    assert "NGINX_PROXY_GATEWAY" in tunnel
    assert '${NGINX_PROXY_GATEWAY}:18090:127.0.0.1:8090' in tunnel
    assert "StrictHostKeyChecking=yes" in tunnel
    assert "ExitOnForwardFailure=yes" in tunnel
    assert "EnvironmentFile=" in unit
    assert "ports:" not in compose
    assert "${NGINX_PROXY_GATEWAY:?" in compose
    assert "host-gateway" not in compose
    assert "extra_hosts:" not in compose
    assert "name: nginx-proxy" in compose
    assert "proxy_pass http://${NGINX_PROXY_GATEWAY}:18090" in nginx
    assert "set_real_ip_from ${NGINX_PROXY_GATEWAY}" in nginx
    assert "real_ip_header X-Real-IP" in nginx
    assert "proxy_set_header X-Forwarded-Proto https" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr" in nginx
    assert "client_max_body_size 2m" in nginx
    assert "--header=Host: quant.ai.jingtao.fun" in compose
    assert "http://127.0.0.1/health" in compose
    assert 'return 200 "ok' not in nginx
    assert "docker compose" in probe
    assert "exec -T quant-research-ui-proxy" in probe
    assert "http://127.0.0.1/health" in probe
    assert '\'{"status":"ok"}\'' in probe


def test_deployment_ignores_real_auth_env_and_documents_ms_login_binding():
    repository = ROOT.parents[1]
    ignored = (ROOT / ".gitignore").read_text()
    ms_login = (repository / "projects/ms-login/README.md").read_text()
    ms_login_app = (repository / "projects/ms-login/app.js").read_text()

    assert "deploy/quant-research-ui.env" in ignored
    assert "quant.ai.jingtao.fun/auth/callback" in ms_login
    assert (
        '"https://quant.ai.jingtao.fun/auth/callback":"https://quant.ai.jingtao.fun/auth/callback"'
        in ms_login
    )
    assert "audience" in ms_login_app
    deploy_script = (repository / "projects/ms-login/deploy-azure.sh").read_text()
    assert 'DOWNSTREAM_CLIENTS="$DOWNSTREAM_CLIENTS"' in deploy_script