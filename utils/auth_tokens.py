"""Restart-safe bearer tokens shared by API blueprints."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time

from utils.config import Config

logger = logging.getLogger("AuthTokens")

AUTH_TOKEN_TTL = max(int(Config.AUTH_TOKEN_TTL_DAYS or 30), 1) * 86400
_auth_tokens: dict[str, dict] = {}
_auth_tokens_lock = threading.Lock()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ensure_auth_token_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token_hash VARCHAR(64) PRIMARY KEY,
            user_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_auth_tokens_expires_at ON auth_tokens (expires_at)")
    except Exception:
        pass


def _save_token(token: str, user_data: dict, expires_at: int) -> None:
    try:
        from utils.database import connect_db

        conn = connect_db()
        try:
            _ensure_auth_token_table(conn)
            conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(token),))
            conn.execute(
                """
                INSERT INTO auth_tokens
                (token_hash, user_json, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    _token_hash(token),
                    json.dumps(user_data, separators=(",", ":")),
                    expires_at,
                    int(time.time()),
                ),
            )
            conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (int(time.time()),))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not persist auth token; falling back to memory only: %s", exc)


def _load_token(token: str) -> dict | None:
    try:
        from utils.database import connect_db

        conn = connect_db()
        try:
            _ensure_auth_token_table(conn)
            row = conn.execute(
                "SELECT user_json, expires_at FROM auth_tokens WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
            if not row:
                return None
            expires_at = int(row["expires_at"])
            if time.time() > expires_at:
                conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(token),))
                conn.commit()
                return None
            user = json.loads(row["user_json"])
            with _auth_tokens_lock:
                _auth_tokens[token] = {"user": user, "expires_at": expires_at}
            return user
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Could not load persisted auth token: %s", exc)
        return None


def make_auth_token(user_data: dict) -> str:
    """Create a restart-safe opaque token for a Discord-authenticated user."""
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + AUTH_TOKEN_TTL
    with _auth_tokens_lock:
        _auth_tokens[token] = {"user": user_data, "expires_at": expires_at}
    _save_token(token, user_data, expires_at)
    return token


def get_auth_user(token: str) -> dict | None:
    """Return user dict if token is valid and not expired, else None."""
    if not token:
        return None
    with _auth_tokens_lock:
        entry = _auth_tokens.get(token)
    if entry:
        if time.time() <= int(entry["expires_at"]):
            return entry["user"]
        with _auth_tokens_lock:
            _auth_tokens.pop(token, None)
    return _load_token(token)


def revoke_auth_token(token: str) -> None:
    """Remove a token from memory and persistent storage."""
    if not token:
        return
    with _auth_tokens_lock:
        _auth_tokens.pop(token, None)
    try:
        from utils.database import connect_db

        conn = connect_db()
        try:
            _ensure_auth_token_table(conn)
            conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(token),))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Could not revoke persisted auth token: %s", exc)
