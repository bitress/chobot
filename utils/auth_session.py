"""Website sign-in in an HttpOnly cookie instead of a token the page's JavaScript can read.

The session token itself is unchanged (opaque, hashed in auth_tokens, revocable; see
utils/auth_tokens.py). What changes is where the browser keeps it:

* ``cp_session`` cookie: HttpOnly (scripts, including ad scripts, can't read it), Secure,
  SameSite=Lax, host-only on the API host. chopaeng.com -> console.chopaeng.com is same-site,
  so the site's credentialed fetches carry it.
* Sign-in: the Discord callback redirects with a one-time ``code`` (2 min, single use), never
  the token. The site trades it at POST /api/auth/session, which sets the cookie. Going through
  the site's own API base makes it work behind the Vite dev proxy too.
* ``Authorization: Bearer`` keeps working for scripts, bots and the dashboard secret, and lets
  a browser still holding an old localStorage token move it into the cookie once.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Optional

from flask import g, request

SESSION_COOKIE = "cp_session"
# What the site sends as "Bearer" once the real token lives in the cookie: its code paths still
# attach a header, and this placeholder (no secret) means "use my session cookie".
COOKIE_SESSION_PLACEHOLDER = "cookie-session"
LOGIN_CODE_TTL = 120

_login_codes: dict[str, tuple[float, str]] = {}
_login_codes_lock = threading.Lock()


def bearer_token() -> str:
    """An explicit Bearer token; the cookie-session placeholder doesn't count."""
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    return "" if token == COOKIE_SESSION_PLACEHOLDER else token


def request_auth_token() -> str:
    """The caller's session token: an explicit Bearer header wins, else the session cookie.

    Reading the cookie marks the request so its response is never cached at the edge
    (see private_if_cookie_auth): it may now be personalized.
    """
    token = bearer_token()
    if token:
        return token
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if token:
        g.auth_from_cookie = True
    return token


def has_auth_credentials() -> bool:
    """Whether the request carries any sign-in (header, API key or session cookie)."""
    return bool(
        request.headers.get("Authorization")
        or request.headers.get("X-API-Key")
        or request.cookies.get(SESSION_COOKIE)
    )


def set_session_cookie(resp, token: str, max_age: int) -> None:
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=max(int(max_age), 0), path="/",
        secure=True, httponly=True, samesite="Lax",
    )


def clear_session_cookie(resp) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="Lax")


def make_login_code(token: str) -> str:
    """A one-time code standing in for ``token`` in the post-login redirect URL."""
    code = secrets.token_urlsafe(24)
    now = time.time()
    with _login_codes_lock:
        for k in [k for k, (exp, _) in _login_codes.items() if exp < now]:
            _login_codes.pop(k, None)
        _login_codes[code] = (now + LOGIN_CODE_TTL, token)
    return code


def consume_login_code(code: str) -> Optional[str]:
    with _login_codes_lock:
        entry = _login_codes.pop((code or "").strip(), None)
    if not entry or entry[0] < time.time():
        return None
    return entry[1]


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def cookie_csrf_problem(allowed_origins: list[str]) -> Optional[str]:
    """Writes that ride on the session cookie must come from the website itself.

    SameSite=Lax already keeps other sites' forms out; this also covers other chopaeng.com
    subdomains (same-site) and old browsers. Bearer / API-key callers aren't affected.
    """
    if request.method not in _UNSAFE_METHODS or not request.cookies.get(SESSION_COOKIE):
        return None
    if bearer_token() or request.headers.get("X-API-Key"):
        return None
    origin = request.headers.get("Origin") or ""
    if not origin:
        referer = request.headers.get("Referer") or ""
        parts = referer.split("/")
        origin = "/".join(parts[:3]) if len(parts) >= 3 else ""
    if origin and origin.rstrip("/") in {o.rstrip("/") for o in allowed_origins}:
        return None
    return "Cross-site request refused"


def private_if_cookie_auth(resp):
    """A response built for a cookie-signed-in viewer must not land in a shared edge cache."""
    if getattr(g, "auth_from_cookie", False):
        cc = (resp.headers.get("Cache-Control") or "").lower()
        if not cc or "public" in cc or "s-maxage" in cc or "max-age" in cc:
            resp.headers["Cache-Control"] = "private, no-store"
        resp.vary.add("Cookie")
    return resp
