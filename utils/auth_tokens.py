"""Short-lived in-memory bearer tokens shared by API blueprints."""

from __future__ import annotations

import secrets
import threading
import time


AUTH_TOKEN_TTL = 86400  # 24 hours
_auth_tokens: dict[str, dict] = {}
_auth_tokens_lock = threading.Lock()


def make_auth_token(user_data: dict) -> str:
    """Create a short-lived opaque token for a Discord-authenticated user."""
    token = secrets.token_urlsafe(32)
    expires_at = time.monotonic() + AUTH_TOKEN_TTL
    with _auth_tokens_lock:
        _auth_tokens[token] = {"user": user_data, "expires_at": expires_at}
    return token


def get_auth_user(token: str) -> dict | None:
    """Return user dict if token is valid and not expired, else None."""
    if not token:
        return None
    with _auth_tokens_lock:
        entry = _auth_tokens.get(token)
    if not entry:
        return None
    if time.monotonic() > entry["expires_at"]:
        with _auth_tokens_lock:
            _auth_tokens.pop(token, None)
        return None
    return entry["user"]


def revoke_auth_token(token: str) -> None:
    """Remove a token from the in-memory store."""
    if not token:
        return
    with _auth_tokens_lock:
        _auth_tokens.pop(token, None)
