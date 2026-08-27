from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .catalog import Catalog
from .settings import Settings


class AuthError(ValueError):
    """Raised when authentication or request integrity fails."""


class RateLimitError(AuthError):
    """Raised when an authentication source exceeds its bounded allowance."""


@dataclass(frozen=True)
class SessionData:
    email: str
    display_name: str
    issued_at: int
    expires_at: int
    csrf_token: str


@dataclass(frozen=True)
class IssuedSession:
    cookie: str
    csrf_token: str


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _b64decode(value: str, label: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise AuthError(f"invalid {label} encoding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise AuthError(f"invalid {label} encoding") from exc


def _strict_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                AuthError(f"{label} contains non-finite value {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise AuthError(f"{label} must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthError(f"duplicate authentication field: {key}")
        result[key] = value
    return result


def encode_scrypt_password(password: str, *, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("password must be non-empty")
    salt = secrets.token_bytes(16) if salt is None else salt
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32
    )
    return f"scrypt$16384$8$1${_b64encode(salt)}${_b64encode(derived)}"


class AuthManager:
    def __init__(
        self,
        catalog: Catalog,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.catalog = catalog
        self.settings = settings.validated()
        self.clock = clock
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self.allowed_emails = self._load_allowed_emails()

    def _load_allowed_emails(self) -> frozenset[str]:
        if self.settings.auth_mode != "sso":
            return frozenset()
        path = self.settings.allowed_emails_file
        if path is None:
            raise AuthError("allowed-email file is required")
        values = {
            line.strip().lower()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if not values:
            raise AuthError("allowed-email file cannot be empty")
        if any("@" not in value or any(character.isspace() for character in value) for value in values):
            raise AuthError("allowed-email file contains an invalid address")
        return frozenset(values)

    def _rate_limit(self, remote_address: str) -> None:
        now = float(self.clock())
        attempts = self._attempts[remote_address]
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise RateLimitError("authentication rate limit exceeded")
        attempts.append(now)

    def authenticate_sso(
        self, token: str, *, remote_address: str
    ) -> dict[str, str]:
        self._rate_limit(remote_address)
        if not isinstance(token, str) or len(token) > 8192:
            raise AuthError("invalid token format")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("invalid token format")
        header = _strict_json(_b64decode(parts[0], "token header"), "token header")
        if header.get("alg") != "HS256" or set(header) != {"alg", "typ"}:
            raise AuthError("token algorithm must be exactly HS256")
        if header["typ"] != "JWT":
            raise AuthError("token type must be JWT")
        secret = self.settings.auth_shared_secret
        if secret is None:
            raise AuthError("SSO is not configured")
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        signature = _b64decode(parts[2], "token signature")
        if not hmac.compare_digest(signature, expected):
            raise AuthError("invalid token signature")
        claims = _strict_json(_b64decode(parts[1], "token payload"), "token payload")
        required = {"email", "displayName", "aud", "iat", "exp"}
        if not required <= set(claims):
            missing = sorted(required - set(claims))
            raise AuthError(
                f"token is missing required audience, email, or time claims: {missing}"
            )
        email = claims["email"]
        display_name = claims["displayName"]
        audience = claims["aud"]
        issued = claims["iat"]
        expires = claims["exp"]
        if not isinstance(email, str) or "@" not in email:
            raise AuthError("token email is invalid")
        if not isinstance(display_name, str) or not display_name.strip():
            raise AuthError("token display name is invalid")
        if (
            not isinstance(audience, str)
            or not hmac.compare_digest(audience, self.settings.sso_audience)
        ):
            raise AuthError("token audience is invalid")
        if (
            isinstance(issued, bool)
            or isinstance(expires, bool)
            or not isinstance(issued, (int, float))
            or not isinstance(expires, (int, float))
            or not math.isfinite(issued)
            or not math.isfinite(expires)
        ):
            raise AuthError("token iat and exp must be finite timestamps")
        now = float(self.clock())
        if issued > now + 5:
            raise AuthError("token iat is in the future")
        if expires <= now:
            raise AuthError("token has expired")
        if expires - issued > 60 or expires <= issued:
            raise AuthError("token lifetime exceeds the allowed maximum")
        email = email.strip().lower()
        if email not in self.allowed_emails:
            raise AuthError("email is not allowed")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.catalog.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM replay_tokens WHERE expires_at <= ?", (int(now),)
            )
            try:
                connection.execute(
                    "INSERT INTO replay_tokens(token_hash, expires_at) VALUES (?, ?)",
                    (token_hash, int(expires)),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthError("authentication token was already used") from exc
        return {"email": email, "display_name": display_name.strip()}

    def authenticate_password(
        self, password: str, *, remote_address: str
    ) -> dict[str, str]:
        self._rate_limit(remote_address)
        encoded = self.settings.password_scrypt_hash
        if self.settings.auth_mode != "password" or encoded is None:
            raise AuthError("password authentication is not enabled")
        try:
            algorithm, n, r, p, salt_text, expected_text = encoded.split("$")
            if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
                raise ValueError("algorithm")
            salt = _b64decode(salt_text, "password salt")
            expected = _b64decode(expected_text, "password hash")
            if not 8 <= len(salt) <= 64 or len(expected) != 32:
                raise ValueError("encoded length")
            derived = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=16384,
                r=8,
                p=1,
                dklen=len(expected),
            )
        except (AuthError, TypeError, ValueError) as exc:
            raise AuthError("password credentials are invalid") from exc
        if not hmac.compare_digest(derived, expected):
            raise AuthError("password credentials are invalid")
        return {"email": "password-user", "display_name": "Researcher"}

    def issue_session(self, user: dict[str, str]) -> IssuedSession:
        now = int(self.clock())
        csrf = secrets.token_urlsafe(32)
        payload = {
            "email": user["email"],
            "display_name": user["display_name"],
            "iat": now,
            "exp": now + 3600,
            "csrf": csrf,
        }
        encoded = _b64encode(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(
                self.settings.session_secret.encode("utf-8"),
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return IssuedSession(cookie=f"{encoded}.{signature}", csrf_token=csrf)

    def verify_session(self, cookie: str) -> SessionData:
        if not isinstance(cookie, str) or len(cookie) > 4096:
            raise AuthError("invalid session format")
        parts = cookie.split(".")
        if len(parts) != 2:
            raise AuthError("invalid session format")
        expected = hmac.new(
            self.settings.session_secret.encode("utf-8"),
            parts[0].encode("ascii"),
            hashlib.sha256,
        ).digest()
        signature = _b64decode(parts[1], "session signature")
        if not hmac.compare_digest(signature, expected):
            raise AuthError("invalid session signature")
        payload = _strict_json(_b64decode(parts[0], "session"), "session")
        if set(payload) != {"email", "display_name", "iat", "exp", "csrf"}:
            raise AuthError("session has invalid fields")
        if type(payload["iat"]) is not int or type(payload["exp"]) is not int:
            raise AuthError("session timestamps are invalid")
        if payload["exp"] <= self.clock():
            raise AuthError("session has expired")
        for field in ("email", "display_name", "csrf"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise AuthError(f"session {field} is invalid")
        return SessionData(
            email=payload["email"],
            display_name=payload["display_name"],
            issued_at=payload["iat"],
            expires_at=payload["exp"],
            csrf_token=payload["csrf"],
        )

    def verify_csrf(self, session: SessionData, token: str) -> None:
        if not isinstance(token, str) or not hmac.compare_digest(
            session.csrf_token, token
        ):
            raise AuthError("invalid CSRF token")

    def verify_request_origin(
        self, *, host: str, origin: str | None, mutation: bool
    ) -> None:
        normalized_host = host.lower().split(":", 1)[0]
        if normalized_host not in self.settings.allowed_hosts:
            raise AuthError("request host is not allowed")
        if mutation:
            if not origin:
                raise AuthError("mutation origin is required")
            parsed = urlsplit(origin)
            public = urlsplit(self.settings.public_url)
            if (
                parsed.scheme,
                parsed.netloc.lower(),
                parsed.path,
                parsed.query,
                parsed.fragment,
            ) != (public.scheme, public.netloc.lower(), "", "", ""):
                raise AuthError("mutation origin is not allowed")
