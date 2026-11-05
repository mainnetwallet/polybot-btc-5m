"""Authentication, signed sessions, and CSRF for the loopback dashboard.

The dashboard is intentionally single-user: one operator password (PBKDF2) and
HMAC-signed session cookies. No token is ever written to disk in plaintext and
no secret is returned to the browser.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..security import verify_password

SESSION_COOKIE = "polybot_session"
CSRF_HEADER = "x-csrf-token"
MAX_FAILED_LOGINS = 5
LOGIN_WINDOW_SECONDS = 300.0
LOCKOUT_SECONDS = 300.0


class AuthError(Exception):
    """Raised when authentication cannot be configured or a token is invalid."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass
class Session:
    subject: str
    issued_at: int
    expires_at: int
    csrf: str
    nonce: str


class AuthManager:
    def __init__(self, settings: Settings) -> None:
        if settings.dashboard_password_hash is None:
            raise AuthError(
                "dashboard password hash is not configured; run "
                "`python -m btc5m_bot hash-password` and set POLYBOT_DASHBOARD_PASSWORD_HASH"
            )
        self._password_hash = settings.dashboard_password_hash.get_secret_value()
        secret = settings.session_secret
        if secret is None or not secret.get_secret_value():
            # Ephemeral is acceptable for a loopback UI but invalidates sessions
            # on restart. Never log or export the generated value.
            self._secret = secrets.token_bytes(32)
        else:
            self._secret = hashlib.sha256(secret.get_secret_value().encode("utf-8")).digest()
        self._username = settings.dashboard_username
        self._max_age = int(settings.session_max_age_seconds)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    @property
    def username(self) -> str:
        return self._username

    # ------------------------------------------------------------------ login
    def _client_key(self, ip: str) -> str:
        return ip or "unknown"

    def _blocked(self, key: str, now: float) -> bool:
        until = self._blocked_until.get(key)
        if until is None:
            return False
        if now >= until:
            self._blocked_until.pop(key, None)
            self._failures.pop(key, None)
            return False
        return True

    def login(self, username: str, password: str, *, ip: str = "") -> Session:
        key = self._client_key(ip)
        now = time.monotonic()
        with self._lock:
            if self._blocked(key, now):
                raise AuthError("too many failed attempts; try again later")
            attempts = [t for t in self._failures.get(key, []) if now - t <= LOGIN_WINDOW_SECONDS]
            self._failures[key] = attempts
            ok_user = hmac.compare_digest(username or "", self._username)
            ok_pass = verify_password(password or "", self._password_hash)
            if not (ok_user and ok_pass):
                attempts.append(now)
                if len(attempts) >= MAX_FAILED_LOGINS:
                    self._blocked_until[key] = now + LOCKOUT_SECONDS
                    self._failures[key] = []
                raise AuthError("invalid credentials")
            self._failures.pop(key, None)
        return self._issue(self._username)

    def _issue(self, subject: str) -> Session:
        now = int(time.time())
        session = Session(
            subject=subject,
            issued_at=now,
            expires_at=now + self._max_age,
            csrf=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(12),
        )
        return session

    # ----------------------------------------------------------------- tokens
    def encode(self, session: Session) -> str:
        payload = {
            "sub": session.subject,
            "iat": session.issued_at,
            "exp": session.expires_at,
            "csrf": session.csrf,
            "nonce": session.nonce,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = _b64encode(raw)
        signature = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64encode(signature)}"

    def decode(self, token: str | None) -> Session | None:
        if not token or "." not in token:
            return None
        body, signature = token.rsplit(".", 1)
        try:
            expected = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64decode(signature)):
                return None
            payload: dict[str, Any] = json.loads(_b64decode(body))
            session = Session(
                subject=str(payload["sub"]),
                issued_at=int(payload["iat"]),
                expires_at=int(payload["exp"]),
                csrf=str(payload["csrf"]),
                nonce=str(payload["nonce"]),
            )
        except Exception:
            return None
        if session.expires_at <= int(time.time()):
            return None
        return session

    def verify_csrf(self, session: Session, header_value: str | None) -> bool:
        if not header_value:
            return False
        return hmac.compare_digest(session.csrf, header_value)
