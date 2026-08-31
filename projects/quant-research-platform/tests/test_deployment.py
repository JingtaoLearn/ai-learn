import os
import sqlite3
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = (
    Path(os.environ["AI_LEARN_REPOSITORY_ROOT"])
    if "AI_LEARN_REPOSITORY_ROOT" in os.environ
    else ROOT.parents[1]
)


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
    release = "/home/feng/quant-platform/releases/REPLACE_WITH_RELEASE_ID"
    runtime = (
        "/home/feng/quant-platform/runtime/"
        "venv-ui-REPLACE_WITH_RELEASE_ID/bin/python"
    )

    assert "User=root" not in service
    assert f"WorkingDirectory={release}" in service
    assert f"ExecStart={runtime} -m quant_platform.web" in service
    assert f"QUANT_PROJECT_ROOT={release}" in environment
    assert "/home/feng/quant-platform/current" not in service
    assert "/home/feng/quant-platform/current" not in environment
    assert f"{release}/.venv/bin/python" not in service
    assert "127.0.0.1:8090" not in service
    assert "PrivateTmp=true" in service
    assert "QUANT_FORWARDED_ALLOW_IPS=127.0.0.1" in environment
    assert (
        "QUANT_STATE_ROOT=/home/feng/quant-platform/state/platform"
        in environment
    )
    assert (
        "ExecStartPre=/usr/bin/mkdir -p /home/feng/quant-platform/state/platform"
        in service
    )
    assert "/home/feng/quant-platform/state/ui" not in service
    assert "/home/feng/quant-platform/state/ui" not in environment
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
    repository = REPOSITORY_ROOT
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
    assert (
        "EnvironmentFile=/home/jingtao/.config/quant-research-tunnel.env"
        in unit
    )
    assert (
        "ExecStart=/home/jingtao/ai-learn/vm/host-services/"
        "quant-research-tunnel/run-tunnel.sh"
    ) in unit
    assert "/home/ailearn" not in unit
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

    tunnel_readme = (
        repository
        / "vm/host-services/quant-research-tunnel/README.md"
    ).read_text()
    assert "/home/jingtao/.config/quant-research-tunnel.env" in tunnel_readme
    assert "/home/ailearn" not in tunnel_readme


def test_deployment_ignores_real_auth_env_and_documents_ms_login_binding():
    repository = REPOSITORY_ROOT
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


def test_documentation_uses_the_authoritative_shared_platform_root():
    readme = (ROOT / "README.md").read_text()
    plan = (
        ROOT
        / "docs/plans/2026-08-27-operator-registry-ui.md"
    ).read_text()
    release = "/home/feng/quant-platform/releases/REPLACE_WITH_RELEASE_ID"
    runtime = (
        "/home/feng/quant-platform/runtime/"
        "venv-ui-REPLACE_WITH_RELEASE_ID/bin/python"
    )

    assert "--root state/platform" in readme
    assert "--root state/ui" not in readme
    assert "/home/feng/quant-platform/state/platform" in plan
    assert "/home/feng/quant-platform/state/ui" not in plan
    assert release in readme
    assert release in plan
    assert runtime in readme
    assert runtime in plan
    assert "substitute the exact immutable release ID" in readme
    assert "substitute the exact immutable release ID" in plan
    assert "Do not use the `current` symlink" in readme
    assert (
        "`/home/feng/quant-platform/current` is never accepted as the project root"
        in plan
    )


def test_release_documentation_explains_project_script_convention_exception():
    deployment = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

    assert "project-level exception" in deployment
    assert "vm/scripts/lib/common.sh" in deployment
    assert "Issue #175" in deployment
    assert "subsequent invocation fails" in deployment


DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-release.sh"
PRODUCTION_HOST = "quant.ai.jingtao.fun"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _catalog_state(path: Path) -> tuple[list[int], list[str]]:
    with sqlite3.connect(path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        values = [
            row[0]
            for row in connection.execute(
                "SELECT value FROM deployment_probe ORDER BY value"
            )
        ]
    return versions, values


def _catalog_user_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def _deployment_fixture(tmp_path: Path) -> dict[str, object]:
    release_id = "release-175"
    platform_root = tmp_path / "quant-platform"
    release_root = platform_root / "releases"
    release_dir = release_root / release_id
    release_dir.mkdir(parents=True)
    runtime_root = platform_root / "runtime"
    runtime_dir = runtime_root / f"venv-ui-{release_id}"
    runtime_python = runtime_dir / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    _write_executable(runtime_python, "#!/usr/bin/env bash\nexit 0\n")

    live_root = tmp_path / "live"
    live_root.mkdir()
    unit_path = live_root / "quant-research-ui.service"
    env_path = live_root / "quant-research-ui.env"
    unit_template = tmp_path / "quant-research-ui.service.template"
    unit_template.write_text(
        (ROOT / "deploy" / "quant-research-ui.service")
        .read_text(encoding="utf-8")
        .replace("/home/feng/quant-platform/releases", str(release_root))
        .replace("/home/feng/quant-platform/runtime", str(runtime_root))
        .replace(
            "/home/feng/.config/quant-research-ui.env",
            str(env_path),
        ),
        encoding="utf-8",
    )
    old_release = release_root / "previous-release"
    old_runtime = runtime_root / "venv-ui-previous-release"
    old_unit = textwrap.dedent(
        f"""\
        [Service]
        WorkingDirectory={old_release}
        EnvironmentFile={env_path}
        ExecStart={old_runtime}/bin/python -m quant_platform.web
        """
    )
    secret = "fixture-secret-must-not-be-printed"
    old_env = textwrap.dedent(
        f"""\
        QUANT_ENVIRONMENT=production
        QUANT_PROJECT_ROOT={old_release}
        AUTH_SHARED_SECRET={secret}
        QUANT_SESSION_SECRET=another-{secret}
        """
    )
    unit_path.write_text(old_unit, encoding="utf-8")
    env_path.write_text(old_env, encoding="utf-8")
    env_path.chmod(0o600)

    state_root = platform_root / "state" / "platform"
    state_root.mkdir(parents=True)
    catalog_path = state_root / "catalog.sqlite3"
    with sqlite3.connect(catalog_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(version, f"2026-08-{version:02d}T00:00:00Z") for version in range(1, 9)],
        )
        connection.execute("CREATE TABLE deployment_probe(value TEXT NOT NULL)")
        connection.execute("INSERT INTO deployment_probe(value) VALUES ('before-deploy')")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    curl_log = tmp_path / "curl.log"
    rm_log = tmp_path / "rm.log"
    _write_executable(
        fake_bin / "systemctl",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail

        printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
        if [[ "$*" == "--user stop quant-research-ui.service" ]] \
            && [[ "${FAKE_FAIL_ROLLBACK_STOP:-0}" == "1" ]]; then
          exit 1
        fi
        if [[ "$*" == "--user restart quant-research-ui.service" ]]; then
          if grep -Fqx "WorkingDirectory=$FAKE_NEW_WORKING_DIRECTORY" \
              "$QUANT_DEPLOY_UNIT_PATH" \
              && [[ "${FAKE_MIGRATE_SCHEMA:-1}" == "1" ]]; then
            python3 - "$QUANT_DEPLOY_STATE_ROOT/catalog.sqlite3" <<'PY'
        import sqlite3
        import sys

        with sqlite3.connect(sys.argv[1]) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (9, '2026-08-31T00:00:00Z')"
            )
        PY
          fi
          exit 0
        fi
        if [[ "$*" == *"--property=WorkingDirectory --value" ]]; then
          if [[ -n "${FAKE_WORKING_DIRECTORY:-}" ]]; then
            printf '%s\n' "$FAKE_WORKING_DIRECTORY"
          else
            sed -n 's/^WorkingDirectory=//p' "$QUANT_DEPLOY_UNIT_PATH"
          fi
          exit 0
        fi
        if [[ "$*" == *"--property=ExecStart --value" ]]; then
          command="$(
            sed -n 's/^ExecStart=//p' "$QUANT_DEPLOY_UNIT_PATH"
          )"
          command="${FAKE_EXEC_START_COMMAND:-$command}"
          executable="${command%% *}"
          printf '{ path=%s ; argv[]=%s ; ignore_errors=no ; }\n' \
            "$executable" "$command"
          exit 0
        fi
        if [[ "$*" == *"--property=NRestarts --value" ]]; then
          printf '%s\n' "${FAKE_NRESTARTS:-0}"
          exit 0
        fi
        """,
    )
    _write_executable(
        fake_bin / "rm",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail

        for argument in "$@"; do
          if [[ "${FAKE_FAIL_STAGED_CLEANUP:-0}" == "1" ]] \
              && [[ "$argument" == *.new.* ]]; then
            printf '%s\n' "$*" >> "$FAKE_RM_LOG"
            exit 1
          fi
        done
        exec /usr/bin/rm "$@"
        """,
    )
    _write_executable(
        fake_bin / "curl",
        r"""
        #!/usr/bin/env python3
        import os
        import stat
        import sys
        from pathlib import Path

        arguments = sys.argv[1:]
        with Path(os.environ["FAKE_CURL_LOG"]).open("a", encoding="utf-8") as log:
            log.write(" ".join(arguments) + "\n")
        if os.environ.get("FAKE_CURL_MODE") == "all-fail":
            raise SystemExit(7)

        url = arguments[-1]
        output_path = arguments[arguments.index("--output") + 1]
        status = 200
        body = ""
        if url == "http://127.0.0.1:8090/health":
            body = os.environ.get("FAKE_LOCAL_HEALTH_BODY", '{"status":"ok"}')
        elif url.endswith("/health"):
            body = os.environ.get("FAKE_PUBLIC_HEALTH_BODY", '{"status":"ok"}')
        elif url.endswith("/api/operators"):
            status = int(os.environ.get("FAKE_API_STATUS", "401"))
        else:
            status = int(os.environ.get("FAKE_ROOT_STATUS", "303"))

        if output_path != "/dev/null":
            with Path(os.environ["FAKE_PROBE_MODE_LOG"]).open(
                "a", encoding="utf-8"
            ) as mode_log:
                mode = stat.S_IMODE(Path(output_path).stat().st_mode)
                mode_log.write(f"{mode:04o}\n")
            Path(output_path).write_text(body, encoding="utf-8")
        if "--write-out" in arguments:
            sys.stdout.write(str(status))
        if "--fail" in arguments and status >= 400:
            raise SystemExit(22)
        """,
    )

    rollback_dir = platform_root / "rollback"
    probe_path = tmp_path / "health-response.json"
    probe_mode_log = tmp_path / "probe-mode.log"
    environment = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "QUANT_DEPLOY_RELEASE_ROOT": str(release_root),
        "QUANT_DEPLOY_RUNTIME_ROOT": str(runtime_root),
        "QUANT_DEPLOY_UNIT_TEMPLATE": str(unit_template),
        "QUANT_DEPLOY_UNIT_PATH": str(unit_path),
        "QUANT_DEPLOY_ENV_PATH": str(env_path),
        "QUANT_DEPLOY_STATE_ROOT": str(state_root),
        "QUANT_DEPLOY_ROLLBACK_DIR": str(rollback_dir),
        "QUANT_DEPLOY_PROBE_FILE": str(probe_path),
        "QUANT_DEPLOY_HEALTH_ATTEMPTS": "3",
        "QUANT_DEPLOY_HEALTH_INTERVAL_SECONDS": "0",
        "FAKE_NEW_WORKING_DIRECTORY": str(release_dir),
        "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
        "FAKE_CURL_LOG": str(curl_log),
        "FAKE_RM_LOG": str(rm_log),
        "FAKE_PROBE_MODE_LOG": str(probe_mode_log),
    }
    return {
        "release_id": release_id,
        "release_dir": release_dir,
        "runtime_root": runtime_root,
        "runtime_dir": runtime_dir,
        "runtime_python": runtime_python,
        "unit_template": unit_template,
        "unit_path": unit_path,
        "env_path": env_path,
        "old_unit": old_unit,
        "old_env": old_env,
        "secret": secret,
        "catalog_path": catalog_path,
        "rollback_dir": rollback_dir,
        "probe_path": probe_path,
        "probe_mode_log": probe_mode_log,
        "systemctl_log": systemctl_log,
        "curl_log": curl_log,
        "rm_log": rm_log,
        "environment": environment,
    }


def _run_deployment(fixture: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DEPLOY_SCRIPT), str(fixture["release_id"])],
        cwd=ROOT,
        env=fixture["environment"],
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_secrets_were_not_printed(
    fixture: dict[str, object],
    result: subprocess.CompletedProcess[str],
) -> None:
    output = result.stdout + result.stderr
    assert fixture["secret"] not in output
    assert "AUTH_SHARED_SECRET" not in output
    assert "QUANT_SESSION_SECRET" not in output


def _assert_rollback_restored(
    fixture: dict[str, object],
    result: subprocess.CompletedProcess[str],
) -> None:
    assert fixture["unit_path"].read_text(encoding="utf-8") == fixture["old_unit"]
    assert fixture["env_path"].read_text(encoding="utf-8") == fixture["old_env"]
    assert _catalog_state(fixture["catalog_path"]) == (
        list(range(1, 9)),
        ["before-deploy"],
    )
    systemctl_calls = fixture["systemctl_log"].read_text(encoding="utf-8")
    assert "--user stop quant-research-ui.service" in systemctl_calls
    assert systemctl_calls.count("--user restart quant-research-ui.service") == 2
    _assert_secrets_were_not_printed(fixture, result)


def test_release_deployment_uses_immutable_source_and_exact_separate_runtime(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    release_dir = fixture["release_dir"]

    result = _run_deployment(fixture)

    assert result.returncode == 0, result.stderr
    assert not (release_dir / ".venv").exists()
    service = fixture["unit_path"].read_text(encoding="utf-8")
    assert f"WorkingDirectory={release_dir}" in service
    assert f"ExecStart={fixture['runtime_python']} -m quant_platform.web" in service
    assert f"ExecStart={release_dir}/.venv/bin/python" not in service


def test_release_deployment_fails_closed_for_missing_symlinked_or_wrong_runtime(
    tmp_path: Path,
):
    for runtime_state in ("missing", "symlink", "wrong"):
        fixture = _deployment_fixture(tmp_path / runtime_state)
        runtime_root = fixture["runtime_root"]
        runtime_dir = fixture["runtime_dir"]
        if runtime_state == "symlink":
            runtime_target = runtime_root / "venv-ui-symlink-target"
            runtime_dir.rename(runtime_target)
            runtime_dir.symlink_to(runtime_target, target_is_directory=True)
        elif runtime_state == "wrong":
            runtime_dir.rename(runtime_root / "venv-ui-wrong-release")
        else:
            fixture["runtime_python"].unlink()

        result = _run_deployment(fixture)

        assert result.returncode != 0, runtime_state
        assert not fixture["rollback_dir"].exists()
        assert fixture["unit_path"].read_text(encoding="utf-8") == fixture["old_unit"]
        assert fixture["env_path"].read_text(encoding="utf-8") == fixture["old_env"]
        assert _catalog_state(fixture["catalog_path"]) == (
            list(range(1, 9)),
            ["before-deploy"],
        )
        assert not fixture["systemctl_log"].exists()


def test_stale_success_probe_with_only_current_curl_failures_rolls_back(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    fixture["probe_path"].write_text('{"status":"ok"}', encoding="utf-8")
    fixture["environment"]["FAKE_CURL_MODE"] = "all-fail"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    assert not fixture["probe_path"].exists()
    assert len(fixture["curl_log"].read_text(encoding="utf-8").splitlines()) == 3
    _assert_rollback_restored(fixture, result)


def test_legacy_probe_symlink_cannot_overwrite_its_target(tmp_path: Path):
    fixture = _deployment_fixture(tmp_path)
    protected = tmp_path / "protected.txt"
    protected.write_text("must remain intact", encoding="utf-8")
    fixture["probe_path"].symlink_to(protected)
    fixture["environment"]["FAKE_CURL_MODE"] = "all-fail"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    assert protected.read_text(encoding="utf-8") == "must remain intact"
    assert not fixture["probe_path"].exists()
    assert not list(fixture["rollback_dir"].glob("health-response.*"))
    _assert_rollback_restored(fixture, result)


def test_rollback_attempts_every_recovery_step_after_stop_failure(tmp_path: Path):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_CURL_MODE"] = "all-fail"
    fixture["environment"]["FAKE_FAIL_ROLLBACK_STOP"] = "1"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)
    assert "Rollback completed with errors; manual recovery is required." in result.stderr


def test_exit_cleanup_failure_cannot_skip_rollback(tmp_path: Path):
    fixture = _deployment_fixture(tmp_path)
    fixture["unit_template"].write_text("[Service]\n", encoding="utf-8")
    fixture["environment"]["FAKE_FAIL_STAGED_CLEANUP"] = "1"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    assert fixture["rm_log"].exists()
    systemctl_calls = fixture["systemctl_log"].read_text(encoding="utf-8")
    assert "--user stop quant-research-ui.service" in systemctl_calls
    assert systemctl_calls.count("--user restart quant-research-ui.service") == 1
    assert "Staged-file cleanup failed; continuing rollback." in result.stderr
    _assert_secrets_were_not_printed(fixture, result)


def test_local_health_probe_sends_the_production_host_header(tmp_path: Path):
    fixture = _deployment_fixture(tmp_path)

    result = _run_deployment(fixture)

    assert result.returncode == 0, result.stderr
    local_call = next(
        call
        for call in fixture["curl_log"].read_text(encoding="utf-8").splitlines()
        if "http://127.0.0.1:8090/health" in call
    )
    assert f"--header Host: {PRODUCTION_HOST}" in local_call


def test_release_deployment_requires_exact_schema_migration_and_rolls_back(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_MIGRATE_SCHEMA"] = "0"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)


def test_release_deployment_uses_schema_migrations_when_user_version_is_zero(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    assert _catalog_user_version(fixture["catalog_path"]) == 0

    result = _run_deployment(fixture)

    assert result.returncode == 0, result.stderr
    assert _catalog_state(fixture["catalog_path"])[0] == list(range(1, 10))
    assert _catalog_user_version(fixture["catalog_path"]) == 0


def test_release_deployment_requires_exact_systemd_working_directory(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_WORKING_DIRECTORY"] = "/wrong/release"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)


def test_release_deployment_requires_exact_systemd_exec_start(tmp_path: Path):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_EXEC_START_COMMAND"] = (
        "/wrong/runtime/venv-ui-release-175/bin/python -m quant_platform.web"
    )

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)


def test_release_deployment_requires_exact_public_health_response(tmp_path: Path):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_PUBLIC_HEALTH_BODY"] = '{"status":"not-ok"}'

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)


def test_release_deployment_requires_public_root_to_redirect_unauthenticated_users(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_ROOT_STATUS"] = "200"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)


def test_release_deployment_requires_public_api_to_reject_unauthenticated_users(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_API_STATUS"] = "200"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)


def test_release_deployment_requires_zero_systemd_restarts(tmp_path: Path):
    fixture = _deployment_fixture(tmp_path)
    fixture["environment"]["FAKE_NRESTARTS"] = "1"

    result = _run_deployment(fixture)

    assert result.returncode != 0
    _assert_rollback_restored(fixture, result)


def test_release_deployment_preserves_checked_rollback_backup_and_secret_hygiene(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)

    result = _run_deployment(fixture)

    assert result.returncode == 0, result.stderr
    assert (
        f"WorkingDirectory={fixture['release_dir']}"
        in fixture["unit_path"].read_text(encoding="utf-8")
    )
    deployed_env = fixture["env_path"].read_text(encoding="utf-8")
    assert f"QUANT_PROJECT_ROOT={fixture['release_dir']}" in deployed_env
    assert fixture["secret"] in deployed_env
    assert _catalog_state(fixture["catalog_path"])[0] == list(range(1, 10))

    rollback_dir = fixture["rollback_dir"]
    assert (rollback_dir / "quant-research-ui.service").read_text(
        encoding="utf-8"
    ) == fixture["old_unit"]
    assert (rollback_dir / "quant-research-ui.env").read_text(
        encoding="utf-8"
    ) == fixture["old_env"]
    assert _catalog_state(rollback_dir / "catalog.sqlite3") == (
        list(range(1, 9)),
        ["before-deploy"],
    )
    assert not list(rollback_dir.glob("health-response.*"))
    curl_calls = fixture["curl_log"].read_text(encoding="utf-8")
    assert "https://quant.ai.jingtao.fun/health" in curl_calls
    assert "https://quant.ai.jingtao.fun/api/operators" in curl_calls
    systemctl_calls = fixture["systemctl_log"].read_text(encoding="utf-8")
    assert "--property=WorkingDirectory --value" in systemctl_calls
    assert "--property=ExecStart --value" in systemctl_calls
    assert "--property=NRestarts --value" in systemctl_calls
    assert set(
        fixture["probe_mode_log"].read_text(encoding="utf-8").splitlines()
    ) == {"0600"}
    _assert_secrets_were_not_printed(fixture, result)


def test_release_deployment_refuses_to_overwrite_existing_rollback_backup(
    tmp_path: Path,
):
    fixture = _deployment_fixture(tmp_path)
    rollback_dir = fixture["rollback_dir"]
    rollback_dir.mkdir()
    marker = rollback_dir / "operator-owned-backup"
    marker.write_text("keep", encoding="utf-8")

    result = _run_deployment(fixture)

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "keep"
    assert fixture["unit_path"].read_text(encoding="utf-8") == fixture["old_unit"]
    assert fixture["env_path"].read_text(encoding="utf-8") == fixture["old_env"]
    assert _catalog_state(fixture["catalog_path"])[0] == list(range(1, 9))
    assert not fixture["systemctl_log"].exists()