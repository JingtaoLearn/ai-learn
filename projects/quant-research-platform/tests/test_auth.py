import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from quant_platform.auth import (
    AuthError,
    AuthManager,
    RateLimitError,
    encode_scrypt_password,
)
from quant_platform.catalog import initialize_catalog
from quant_platform.settings import Settings, SettingsError


NOW = 1_787_800_000
SHARED = "s" * 48
SESSION = "c" * 48
AUDIENCE = "https://quant.ai.jingtao.fun/auth/callback"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _token(claims: dict, *, secret: str = SHARED, header: dict | None = None) -> str:
    header = header or {"alg": "HS256", "typ": "JWT"}
    parts = [
        _b64(json.dumps(header, separators=(",", ":")).encode()),
        _b64(json.dumps(claims, separators=(",", ":")).encode()),
    ]
    signature = hmac.new(secret.encode(), ".".join(parts).encode(), hashlib.sha256).digest()
    return ".".join([*parts, _b64(signature)])


def _claims(**changes):
    return {
        "email": "Researcher@Example.com",
        "displayName": "Researcher",
        "aud": AUDIENCE,
        "iat": NOW - 5,
        "exp": NOW + 25,
    } | changes


def _settings(tmp_path: Path, **changes) -> Settings:
    allowlist = tmp_path / "allowed-emails.txt"
    allowlist.write_text("researcher@example.com\n", encoding="utf-8")
    values = {
        "environment": "production",
        "auth_mode": "sso",
        "state_root": tmp_path / "state",
        "public_url": "https://quant.ai.jingtao.fun",
        "allowed_hosts": ("quant.ai.jingtao.fun",),
        "auth_shared_secret": SHARED,
        "session_secret": SESSION,
        "allowed_emails_file": allowlist,
        "sso_login_url": "https://ms-login.ai.jingtao.fun/auth/login",
        "sso_audience": AUDIENCE,
        "sso_callback_url": "https://quant.ai.jingtao.fun/auth/callback",
        "password_scrypt_hash": None,
        "secure_cookies": True,
        "runner_image": "sha256:" + "a" * 64,
    }
    return Settings(**(values | changes)).validated()


def _manager(tmp_path: Path, **settings) -> AuthManager:
    configuration = _settings(tmp_path, **settings)
    return AuthManager(
        initialize_catalog(configuration.state_root),
        configuration,
        clock=lambda: NOW,
    )


def test_valid_sso_assertion_is_allowlisted_one_time_and_normalized(tmp_path: Path):
    manager = _manager(tmp_path)
    token = _token(_claims())

    user = manager.authenticate_sso(token, remote_address="192.0.2.1")

    assert user == {"email": "researcher@example.com", "display_name": "Researcher"}
    with pytest.raises(AuthError, match="already used"):
        manager.authenticate_sso(token, remote_address="192.0.2.1")


@pytest.mark.parametrize(
    ("token_factory", "message"),
    [
        (lambda: "not-a-token", "format"),
        (lambda: _token(_claims(), secret="x" * 48), "signature"),
        (lambda: _token(_claims(), header={"alg": "none", "typ": "JWT"}), "algorithm"),
        (lambda: _token(_claims(exp=NOW - 1)), "expired"),
        (lambda: _token(_claims(iat=NOW + 10)), "future"),
        (lambda: _token(_claims(iat=NOW - 5, exp=NOW + 120)), "lifetime"),
        (lambda: _token({key: value for key, value in _claims().items() if key != "aud"}), "audience"),
        (lambda: _token(_claims(aud="other-service")), "audience"),
        (lambda: _token(_claims(aud=[AUDIENCE])), "audience"),
        (lambda: _token({key: value for key, value in _claims().items() if key != "email"}), "email"),
        (lambda: _token(_claims(email="denied@example.com")), "allowed"),
    ],
)
def test_sso_assertions_fail_closed_without_echoing_token(
    tmp_path: Path, token_factory, message
):
    manager = _manager(tmp_path)
    token = token_factory()

    with pytest.raises(AuthError, match=message) as failure:
        manager.authenticate_sso(token, remote_address="192.0.2.1")

    assert token not in str(failure.value)


def test_signed_session_detects_tampering_expiry_and_has_csrf(tmp_path: Path):
    manager = _manager(tmp_path)
    issued = manager.issue_session(
        {"email": "researcher@example.com", "display_name": "Researcher"}
    )

    session = manager.verify_session(issued.cookie)

    assert session.email == "researcher@example.com"
    assert session.csrf_token == issued.csrf_token
    manager.verify_csrf(session, issued.csrf_token)
    with pytest.raises(AuthError, match="CSRF"):
        manager.verify_csrf(session, "wrong")
    with pytest.raises(AuthError, match="signature"):
        manager.verify_session(issued.cookie[:-1] + ("A" if issued.cookie[-1] != "A" else "B"))

    manager.clock = lambda: NOW + 4000
    with pytest.raises(AuthError, match="expired"):
        manager.verify_session(issued.cookie)


def test_origin_and_host_checks_are_exact(tmp_path: Path):
    manager = _manager(tmp_path)

    manager.verify_request_origin(
        host="quant.ai.jingtao.fun",
        origin="https://quant.ai.jingtao.fun",
        mutation=True,
    )
    with pytest.raises(AuthError, match="host"):
        manager.verify_request_origin(
            host="evil.example", origin="https://quant.ai.jingtao.fun", mutation=False
        )
    with pytest.raises(AuthError, match="origin"):
        manager.verify_request_origin(
            host="quant.ai.jingtao.fun",
            origin="https://evil.example",
            mutation=True,
        )


def test_login_rate_limit_is_bounded_per_address(tmp_path: Path):
    manager = _manager(tmp_path)
    for index in range(5):
        with pytest.raises(AuthError):
            manager.authenticate_sso("bad", remote_address="192.0.2.9")
    with pytest.raises(RateLimitError, match="rate"):
        manager.authenticate_sso("bad", remote_address="192.0.2.9")
    with pytest.raises(AuthError, match="format"):
        manager.authenticate_sso("bad", remote_address="192.0.2.10")


def test_password_fallback_uses_scrypt_and_is_not_the_production_default(tmp_path: Path):
    encoded = encode_scrypt_password("correct horse", salt=b"fixed-test-salt")
    manager = _manager(
        tmp_path,
        environment="development",
        auth_mode="password",
        auth_shared_secret=None,
        password_scrypt_hash=encoded,
        secure_cookies=False,
    )

    assert manager.authenticate_password(
        "correct horse", remote_address="127.0.0.1"
    ) == {"email": "password-user", "display_name": "Researcher"}
    with pytest.raises(AuthError, match="credentials"):
        manager.authenticate_password("wrong", remote_address="127.0.0.1")


@pytest.mark.parametrize(
    "changes",
    [
        {"auth_shared_secret": None},
        {"session_secret": ""},
        {"allowed_emails_file": None},
        {"secure_cookies": False},
        {"auth_mode": "password", "password_scrypt_hash": None},
    ],
)
def test_incomplete_production_auth_configuration_prevents_startup(
    tmp_path: Path, changes
):
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("researcher@example.com\n", encoding="utf-8")
    values = {
        "environment": "production",
        "auth_mode": "sso",
        "state_root": tmp_path / "state",
        "public_url": "https://quant.ai.jingtao.fun",
        "allowed_hosts": ("quant.ai.jingtao.fun",),
        "auth_shared_secret": SHARED,
        "session_secret": SESSION,
        "allowed_emails_file": allowlist,
        "sso_login_url": "https://ms-login.ai.jingtao.fun/auth/login",
        "sso_audience": AUDIENCE,
        "sso_callback_url": "https://quant.ai.jingtao.fun/auth/callback",
        "password_scrypt_hash": None,
        "secure_cookies": True,
        "runner_image": "sha256:" + "a" * 64,
    }
    with pytest.raises(SettingsError):
        Settings(**(values | changes)).validated()


def test_callback_and_audience_configuration_are_exact(tmp_path: Path):
    with pytest.raises(SettingsError, match="callback"):
        _settings(
            tmp_path,
            sso_callback_url="https://quant.ai.jingtao.fun/wrong",
        )
    with pytest.raises(SettingsError, match="audience"):
        _settings(tmp_path, sso_audience="")
    with pytest.raises(SettingsError, match="audience"):
        _settings(tmp_path, sso_audience="quant-research-ui")
