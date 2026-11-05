"""Password hashing and secret redaction helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Any

_ITERATIONS = 240_000
_SECRET_KEYS = re.compile(r"(private.?key|secret|passphrase|password|api.?key|authorization|cookie)", re.I)
_PRIVATE_KEY_RE = re.compile(r"0x[0-9a-fA-F]{64}")


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively mask secret-looking values for logs and API payloads."""
    if _depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SECRET_KEYS.search(str(key)):
                out[key] = "***REDACTED***"
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return _PRIVATE_KEY_RE.sub("0x" + "*" * 64, value)
    return value
