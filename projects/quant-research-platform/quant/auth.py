"""ms-login SSO (HS256 JWT, single use) -> signed session cookie; bearer token for automation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import jwt

COOKIE = "quant_session"
SESSION_TTL = 30 * 86400


class Auth:
    def __init__(self) -> None:
        self.mode = os.environ.get("QUANT_AUTH", "sso")  # sso | none (local dev)
        self.sso_secret = os.environ.get("AUTH_SHARED_SECRET", "")
        self.session_secret = os.environ.get("QUANT_SESSION_SECRET", "").encode()
        self.api_token = os.environ.get("QUANT_API_TOKEN", "")
        self.public_url = os.environ.get("QUANT_PUBLIC_URL", "https://quant.ai.jingtao.fun")
        self.login_url = os.environ.get("QUANT_SSO_LOGIN_URL", "https://ms-login.ai.jingtao.fun/auth/login")
        self.audience = f"{self.public_url}/auth/callback"
        self.allowed = {e.strip().lower() for e in os.environ.get("QUANT_ALLOWED_EMAILS", "").split(",") if e.strip()}
        self._used: dict[str, float] = {}
        if self.mode == "sso" and not (self.sso_secret and self.session_secret and self.allowed):
            raise RuntimeError("SSO needs AUTH_SHARED_SECRET, QUANT_SESSION_SECRET, QUANT_ALLOWED_EMAILS")

    def login_redirect(self) -> str:
        from urllib.parse import urlencode
        return f"{self.login_url}?{urlencode({'redirect': self.audience, 'audience': self.audience})}"

    def accept_sso(self, token: str) -> str:
        claims = jwt.decode(token, self.sso_secret, algorithms=["HS256"], audience=self.audience,
                            options={"require": ["email", "aud", "iat", "exp"]})
        if claims["exp"] - claims["iat"] > 60:
            raise jwt.InvalidTokenError("token lifetime too long")
        now = time.time()
        self._used = {k: v for k, v in self._used.items() if v > now}
        digest = hashlib.sha256(token.encode()).hexdigest()
        if digest in self._used:
            raise jwt.InvalidTokenError("token replayed")
        self._used[digest] = claims["exp"]
        email = str(claims["email"]).strip().lower()
        if email not in self.allowed:
            raise PermissionError(email)
        return self._sign({"email": email, "exp": int(now) + SESSION_TTL})

    def _sign(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        sig = hmac.new(self.session_secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def session_user(self, cookie: str | None) -> str | None:
        if self.mode == "none":
            return "local"
        if not cookie or "." not in cookie:
            return None
        body, sig = cookie.rsplit(".", 1)
        good = hmac.new(self.session_secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return payload["email"] if payload.get("exp", 0) > time.time() else None

    def token_ok(self, header: str | None) -> bool:
        if self.mode == "none":
            return True
        return bool(self.api_token) and hmac.compare_digest(header or "", f"Bearer {self.api_token}")
