from __future__ import annotations

import os
import stat
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class SettingsError(ValueError):
    """Raised when application security settings are incomplete."""


@dataclass(frozen=True)
class Settings:
    environment: str
    auth_mode: str
    state_root: Path
    public_url: str
    allowed_hosts: tuple[str, ...]
    auth_shared_secret: str | None
    session_secret: str
    allowed_emails_file: Path | None
    sso_login_url: str
    sso_audience: str
    sso_callback_url: str
    password_scrypt_hash: str | None
    secure_cookies: bool
    runner_image: str | None = None
    project_root: Path | None = None

    def validated(self) -> Settings:
        if self.environment not in {"development", "test", "production"}:
            raise SettingsError("environment must be development, test, or production")
        if self.auth_mode not in {"sso", "password"}:
            raise SettingsError("auth_mode must be sso or password")
        if not isinstance(self.state_root, Path):
            raise SettingsError("state_root must be a filesystem path")
        public = urlsplit(self.public_url)
        if not public.scheme or not public.netloc or public.query or public.fragment:
            raise SettingsError("public_url must be an absolute origin URL")
        if self.environment == "production" and public.scheme != "https":
            raise SettingsError("production public_url must use HTTPS")
        if not self.allowed_hosts or any(
            not host or "/" in host for host in self.allowed_hosts
        ):
            raise SettingsError("allowed_hosts must contain exact host names")
        if not isinstance(self.session_secret, str) or len(self.session_secret) < 32:
            raise SettingsError("session_secret must contain at least 32 characters")
        if self.environment == "production" and not self.secure_cookies:
            raise SettingsError("production session cookies must be secure")
        if self.environment == "production" and (
            not isinstance(self.runner_image, str)
            or re.fullmatch(
                r"(?:sha256:|[A-Za-z0-9][A-Za-z0-9._/:=-]*@sha256:)[0-9a-f]{64}",
                self.runner_image,
            )
            is None
        ):
            raise SettingsError("production runner_image must be pinned by SHA-256")
        if self.auth_mode == "sso":
            if (
                not isinstance(self.auth_shared_secret, str)
                or len(self.auth_shared_secret) < 32
            ):
                raise SettingsError(
                    "SSO auth_shared_secret must contain at least 32 characters"
                )
            if self.allowed_emails_file is None:
                raise SettingsError("SSO allowed_emails_file is required")
            try:
                metadata = os.stat(self.allowed_emails_file, follow_symlinks=False)
            except OSError as exc:
                raise SettingsError("SSO allowed-email file is unavailable") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SettingsError("SSO allowed-email file must be a regular file")
            if urlsplit(self.sso_login_url).scheme != "https":
                raise SettingsError("SSO login URL must use HTTPS")
            if not self.sso_audience or any(
                character.isspace() for character in self.sso_audience
            ):
                raise SettingsError("SSO audience must be a non-empty identifier")
            expected_callback = self.public_url.rstrip("/") + "/auth/callback"
            if self.sso_callback_url != expected_callback:
                raise SettingsError(
                    "SSO callback URL must exactly match the public auth callback"
                )
            if self.sso_audience != self.sso_callback_url:
                raise SettingsError(
                    "SSO audience must exactly equal the public auth callback URL"
                )
        elif not self.password_scrypt_hash:
            raise SettingsError("password fallback requires a configured scrypt hash")
        return self

    @classmethod
    def from_environment(cls) -> Settings:
        allowed_file = os.environ.get("QUANT_ALLOWED_EMAILS_FILE")
        return cls(
            environment=os.environ.get("QUANT_ENVIRONMENT", "production"),
            auth_mode=os.environ.get("QUANT_AUTH_MODE", "sso"),
            state_root=Path(
                os.environ.get(
                    "QUANT_STATE_ROOT", "/home/feng/quant-platform/state/ui"
                )
            ),
            public_url=os.environ.get(
                "QUANT_PUBLIC_URL", "https://quant.ai.jingtao.fun"
            ),
            allowed_hosts=tuple(
                host.strip().lower()
                for host in os.environ.get(
                    "QUANT_ALLOWED_HOSTS", "quant.ai.jingtao.fun"
                ).split(",")
                if host.strip()
            ),
            auth_shared_secret=os.environ.get("AUTH_SHARED_SECRET"),
            session_secret=os.environ.get("QUANT_SESSION_SECRET", ""),
            allowed_emails_file=Path(allowed_file) if allowed_file else None,
            sso_login_url=os.environ.get(
                "QUANT_SSO_LOGIN_URL",
                "https://ms-login.ai.jingtao.fun/auth/login",
            ),
            sso_audience=os.environ.get(
                "QUANT_SSO_AUDIENCE",
                "https://quant.ai.jingtao.fun/auth/callback",
            ),
            sso_callback_url=os.environ.get(
                "QUANT_SSO_CALLBACK_URL",
                "https://quant.ai.jingtao.fun/auth/callback",
            ),
            password_scrypt_hash=os.environ.get("QUANT_PASSWORD_SCRYPT_HASH"),
            secure_cookies=os.environ.get("QUANT_SECURE_COOKIES", "true").lower()
            == "true",
            runner_image=os.environ.get("QUANT_RUNNER_IMAGE"),
            project_root=(
                Path(os.environ["QUANT_PROJECT_ROOT"])
                if os.environ.get("QUANT_PROJECT_ROOT")
                else None
            ),
        ).validated()
