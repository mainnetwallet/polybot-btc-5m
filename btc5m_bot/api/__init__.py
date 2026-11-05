"""Local-only dashboard API."""
from __future__ import annotations

from .auth import AuthManager, AuthError
from .app import create_app

__all__ = ["AuthManager", "AuthError", "create_app"]
