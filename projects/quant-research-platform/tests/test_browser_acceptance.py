import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.settings import Settings
from quant_platform.web import create_app

from test_experiment_service import _task
from test_operator_submission import IMAGE, _passing_validator
from test_web_api import snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]

def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_real_browser_desktop_mobile_with_and_without_javascript(tmp_path: Path):
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    node = shutil.which("node")
    if chromium is None or node is None:
        pytest.skip("Chromium and Node are required for browser acceptance")
    port = _available_port()
    base_url = f"http://127.0.0.1:{port}"
    allowlist = tmp_path / "allowed.txt"
    allowlist.write_text("researcher@example.com\n", encoding="utf-8")
    settings = Settings(
        environment="test",
        auth_mode="sso",
        state_root=tmp_path / "state",
        public_url=base_url,
        allowed_hosts=("127.0.0.1",),
        auth_shared_secret="a" * 48,
        session_secret="b" * 48,
        allowed_emails_file=allowlist,
        sso_login_url="https://ms-login.ai.jingtao.fun/auth/login",
        sso_audience=f"{base_url}/auth/callback",
        sso_callback_url=f"{base_url}/auth/callback",
        password_scrypt_hash=None,
        secure_cookies=False,
    ).validated()
    app = create_app(settings)
    original_study_submit = app.state.studies.submit
    study_submit_calls = 0

    def submit_stale_once(spec, *, expected_preview_digest, action_id):
        nonlocal study_submit_calls
        study_submit_calls += 1
        if study_submit_calls % 2:
            return {
                "status": "PREVIEW_STALE",
                "expected_preview_digest": expected_preview_digest,
                "current_preview_digest": expected_preview_digest,
            }
        return original_study_submit(
            spec,
            expected_preview_digest=expected_preview_digest,
            action_id=action_id,
        )

    app.state.studies.submit = submit_stale_once
    app.state.operators.runner_image = IMAGE
    app.state.operators.validator = _passing_validator
    report_experiment = app.state.experiments.submit(
        _task(snapshot(app)), action_id="browser-report"
    )
    report_attempt = app.state.experiments.claim_next_attempt()
    report_result = ResolvedAttemptExecutor(
        app.state.catalog,
        output_root=app.state.catalog.state_root / "experiment-runs",
        project_root=PROJECT_ROOT,
    )(report_attempt)
    app.state.experiments.finish_success(
        report_attempt["attempt_id"],
        result_path=report_result["result_path"],
        result_digest=report_result["result_digest"],
    )
    issued = app.state.auth.issue_session(
        {"email": "researcher@example.com", "display_name": "Researcher"}
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        try:
            subprocess.run(
                [
                    node,
                    str(Path(__file__).with_name("browser_acceptance.mjs")),
                    base_url,
                    issued.cookie,
                    chromium,
                    report_experiment["experiment_id"],
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.CalledProcessError as exc:
            raise AssertionError(exc.stderr) from exc
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                (exc.stderr or b"").decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "browser acceptance timed out without diagnostics")
            ) from exc
    finally:
        server.should_exit = True
        thread.join(timeout=10)
