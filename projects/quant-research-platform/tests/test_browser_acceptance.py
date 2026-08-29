import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from quant_platform.datasets import publish_snapshot
from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.settings import Settings
from quant_platform.web import create_app

from test_experiment_service import _task
from test_operator_submission import IMAGE, _passing_validator
from test_parameter_study import _bars, _persist_production_completed_study
from test_web_api import snapshot
from test_web_ui import _experiment_form


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
    app.state.operators.runner_image = IMAGE
    app.state.operators.validator = _passing_validator
    submit_study = app.state.studies.submit
    stale_previews: set[str] = set()

    def submit_with_one_stale_preview(
        spec, *, expected_preview_digest: str, action_id: str
    ):
        key = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        if key not in stale_previews:
            stale_previews.add(key)
            return {"status": "PREVIEW_STALE"}
        return submit_study(
            spec,
            expected_preview_digest=expected_preview_digest,
            action_id=action_id,
        )

    snapshot_id = snapshot(app)
    publish_snapshot(
        _bars(),
        app.state.catalog.state_root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )
    completed_study_id = _persist_production_completed_study(
        app.state.studies,
        app.state.experiments,
    )
    app.state.studies.submit = submit_with_one_stale_preview
    report_experiment = app.state.experiments.submit(
        _task(snapshot_id), action_id="browser-report"
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
        screenshot_root = Path(
            os.environ.get(
                "PROOFLINE_SCREENSHOT_DIR",
                "/tmp/proofline-browser-artifacts",
            )
        )
        screenshot_root.mkdir(parents=True, exist_ok=True)
        child_environment = os.environ.copy()
        child_environment.pop("NODE_OPTIONS", None)
        try:
            subprocess.run(
                [
                    node,
                    str(Path(__file__).with_name("browser_acceptance.mjs")),
                    base_url,
                    issued.cookie,
                    chromium,
                    report_experiment["experiment_id"],
                    json.dumps(
                        _experiment_form(app, snapshot_id, issued.csrf_token)
                    ),
                    completed_study_id,
                    str(screenshot_root),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
                env=child_environment,
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
