import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from quant_platform.settings import Settings
from quant_platform.web import create_app


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_real_browser_desktop_mobile_with_and_without_javascript(tmp_path: Path):
    chromium = shutil.which("chromium-browser") or shutil.which("chromium")
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
        subprocess.run(
            [
                node,
                str(Path(__file__).with_name("browser_acceptance.mjs")),
                base_url,
                issued.cookie,
                chromium,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
