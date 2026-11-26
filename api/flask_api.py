"""
Flask API Module
Combines all API endpoints:
- Item/Villager Search
- Dodo Code/Island Status
- Patreon Posts
"""

import asyncio
import contextlib
import os
import re
import time
import json
import hashlib
import hmac
import secrets as _secrets
import logging
import queue
import gzip
import threading
import urllib.parse
import urllib.error
import urllib.request
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import requests
from flask import Flask, Response, jsonify, request, session, redirect, url_for, send_file
from flask_cors import CORS
from thefuzz import process, fuzz

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import ThreadedWSGIServer

from utils import island_residents
from utils.config import Config
from utils import island_access
from utils.database import connect_db
from utils.discord_http import request as discord_request
from utils.helpers import format_locations_text, parse_locations_json, normalize_text, clean_text
from utils.presence_hub import hub as presence_hub, format_sse, redact_radar
from utils.nickname_format import build_nickname, nickname_warning_for, is_valid_acnh_nickname
from utils.dodo_log_embed import build_dodo_log_embed, count_recent_reveals
from utils.auth_tokens import get_auth_user, make_auth_token, revoke_auth_token, update_auth_user
from utils.discord_membership import (
    DiscordMembershipUnavailable,
    DiscordNotGuildMember,
    is_beyond_stale_grace,
    refresh_user_payload,
    should_refresh,
)
from utils.nookipedia import NookipediaClient
from utils.ops_status import build_health_payload, get_ad_config, get_maintenance_settings, record_service_status, set_active_data_manager
from api.dashboard import (
    dashboard,
    init_dashboard_db,
    get_db,
    row_to_island_dict,
    _check_session as _check_dashboard_session,
    _parse_visitor_value,
    _parse_visitor_list,
)


logger = logging.getLogger("FlaskAPI")

CHOBOT_SQLITE_DB = "chobot.db"


def _client_ip() -> str:
    """Return the most useful client IP for audit logging and rate limits.

    Not the raw X-Forwarded-For: its first entry is whatever the client sent, so keying a rate
    limit on it let anyone dodge the limit (and grow the limiter) with a made-up header.
    """
    return _visitor_ip()


def _visitor_ip() -> str:
    """Real visitor IP behind Cloudflare (CF-Connecting-IP), falling back to the proxy-resolved address.

    Only trustworthy while the origin is reachable solely through Cloudflare (e.g. a Tunnel);
    otherwise a client could send its own CF-Connecting-IP.
    """
    return (request.headers.get("CF-Connecting-IP") or "").strip() or request.remote_addr or ""


def _record_website_login(event: dict) -> None:
    """Persist a successful website Discord OAuth login for dashboard audit history."""
    db = get_db()
    try:
        db.execute(
            """INSERT INTO website_login_events
                   (user_id, username, discord_name, global_name, account_name, nickname,
                    avatar, roles, role_count, is_admin, is_mod, ip_address, user_agent,
                    return_to, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.get("user_id") or "",
                event.get("username") or "",
                event.get("discord_name") or "",
                event.get("global_name") or "",
                event.get("account_name") or "",
                event.get("nickname") or "",
                event.get("avatar") or "",
                json.dumps(event.get("roles") or []),
                int(event.get("role_count") or 0),
                int(bool(event.get("is_admin"))),
                int(bool(event.get("is_mod"))),
                event.get("ip_address") or "",
                event.get("user_agent") or "",
                event.get("return_to") or "",
                event.get("created_at") or datetime.utcnow().isoformat(),
            ),
        )
        db.commit()
    except Exception as exc:
        logger.warning("Website login DB log failed: %s", exc)
        return
    finally:
        db.close()


def _persist_dodo_reveal_message(
    user_id: str,
    island_name: str,
    channel_id: str | None,
    message_url: str,
    username: str,
    nickname: str,
) -> int | None:
    """Store a Dodo reveal so the flight logger can match the traveler, even when the
    Discord webhook fails. message_url is "" until the webhook post is known. Returns the row ID."""
    island_clean = clean_text(island_name)
    if not island_clean:
        island_clean = clean_text(island_name.lower())
    values = (
        str(user_id),
        island_clean,
        str(channel_id) if channel_id else None,
        message_url,
        username or "",
        nickname or "",
        int(time.time()),
    )
    try:
        conn = connect_db()
        try:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO dodo_reveal_messages
                    (user_id, island_clean, channel_id, message_url, username, nickname, created_at, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'website')
                    """,
                    values,
                )
            except Exception as exc:
                # The source column is added by a startup migration. If it hasn't run, still record
                # the reveal: the flight logger depends on this row.
                logger.warning("dodo_reveal_messages insert with source failed, retrying without: %s", exc)
                with contextlib.suppress(Exception):
                    conn.rollback()
                cur = conn.execute(
                    """
                    INSERT INTO dodo_reveal_messages
                    (user_id, island_clean, channel_id, message_url, username, nickname, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            conn.commit()
            return getattr(cur, "lastrowid", None)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("dodo_reveal_messages insert failed: %s", exc)
        return None


def _update_dodo_reveal_url(row_id: int, message_url: str) -> None:
    try:
        conn = connect_db()
        try:
            conn.execute("UPDATE dodo_reveal_messages SET message_url = ? WHERE id = ?", (message_url, row_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("dodo_reveal_messages URL update failed: %s", exc)


def _log_dodo_reveal_attempt(user: dict | None, island: str, outcome: str, reason: str, **extra) -> None:
    """Log dodo reveal attempts with enough context for dashboard analytics/debugging."""
    logger.info(
        "dodo_reveal user_id=%s username=%s island=%s outcome=%s reason=%s extra=%s",
        user.get("user_id") if user else None,
        user.get("username") if user else None,
        island,
        outcome,
        reason,
        extra,
    )


def _record_api_audit_event(action: str, target: str | None = None, details: dict | None = None) -> None:
    """Best-effort audit log writer for public/API actions."""
    user = _current_auth_user()
    try:
        db = get_db()
        try:
            db.execute(
                """INSERT INTO dashboard_audit_events
                   (actor_user_id, actor_name, action, target, details, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    user.get("user_id") if user else None,
                    user.get("username") if user else None,
                    action,
                    target,
                    json.dumps(details or {}, sort_keys=True),
                    _client_ip(),
                    int(time.time()),
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("API audit insert failed: %s", exc)


def _resolve_search_alias(kind: str, query: str) -> tuple[str, str | None]:
    alias = clean_text(query)
    if not alias:
        return query, None
    try:
        db = get_db()
        try:
            row = db.execute(
                "SELECT target FROM search_aliases WHERE alias = ? AND kind = ?",
                (alias, kind),
            ).fetchone()
        finally:
            db.close()
    except Exception:
        row = None
    return (row["target"], alias) if row else (query, None)


def _log_command_search(
    command: str,
    query: str,
    *,
    found: bool,
    result_count: int = 0,
    source: str = "api",
) -> None:
    try:
        db = get_db()
        try:
            user = _current_auth_user()
            db.execute(
                """INSERT INTO command_search_events
                   (command, query, normalized_query, source, user_id, channel_id, found, result_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    command,
                    query,
                    clean_text(query),
                    source,
                    user.get("user_id") if user else request.args.get("user_id", ""),
                    request.args.get("channel_id", ""),
                    1 if found else 0,
                    int(result_count or 0),
                    int(time.time()),
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Command search log failed: %s", exc)


# Initialize Flask app
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY
app.permanent_session_lifetime = timedelta(days=max(int(Config.FLASK_SESSION_DAYS or 30), 1))
# Trust one level of X-Forwarded-For / X-Forwarded-Proto headers from the
# reverse proxy (nginx, Cloudflare Tunnel, etc.) so that url_for(_external=True)
# produces the correct https:// URL instead of http://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
CORS(app, resources={r"/*": {"origins": Config.FRONTEND_ORIGINS}}, supports_credentials=True)

# Register the mod-only web dashboard
app.register_blueprint(dashboard, url_prefix="/dashboard")
from api.sub_islands import sub_islands_bp
app.register_blueprint(sub_islands_bp)
from api.control_panel import control_panel_bp, init_control_panel_db
app.register_blueprint(control_panel_bp)
init_dashboard_db()
init_control_panel_db()
record_service_status("flask", mode="api", status="running")

# Suppress Flask/Werkzeug standard logs
logging.getLogger('werkzeug').setLevel(logging.ERROR)


# ── Request timing ───────────────────────────────────────────────────────────
# Every response carries Server-Timing: app;dur=<ms> (time inside Flask, including gzip), so
# slowness can be split from the outside: small app time + slow response = network/uplink;
# large app time = CPU or database. Requests over SLOW_REQUEST_MS are logged with the DB pool's
# state, which shows whether requests are queueing for a connection.
# Registered before the gzip hook on purpose: after_request hooks run in reverse order, so this
# one runs last and its duration includes compression.
SLOW_REQUEST_MS = int(os.getenv("SLOW_REQUEST_MS", "1000") or 1000)


@app.before_request
def _start_request_timer():
    request.environ["chobot.t0"] = time.perf_counter()


def _db_pool_status() -> str:
    try:
        from utils.database import get_backend, get_engine
        if get_backend() == "sqlite":
            return "sqlite"
        pool = get_engine().pool
        return f"in_use={pool.checkedout()} idle={pool.checkedin()} overflow={pool.overflow()} size={pool.size()}"
    except Exception as exc:  # diagnostics must never break a request
        return f"unknown ({exc.__class__.__name__})"


@app.after_request
def _record_request_timing(resp):
    t0 = request.environ.get("chobot.t0")
    if t0 is None:
        return resp
    ms = (time.perf_counter() - t0) * 1000
    resp.headers["Server-Timing"] = f"app;dur={ms:.0f}"
    if ms >= SLOW_REQUEST_MS:
        logger.warning(
            "[SLOW] %s %s -> %s in %.0f ms (threads=%d, db pool %s)",
            request.method, request.path, resp.status_code, ms, threading.active_count(), _db_pool_status(),
        )
    return resp


# The origin's uplink to Cloudflare is slow (~10 KB/s measured), and responses went out
# uncompressed: a 3 MB island map took 50-90 s, so the site's map spun "forever".
# Cloudflare asks the origin for gzip and re-encodes for browsers, so gzip here cuts the
# bytes on that link ~15x. Streams (presence SSE) and send_file passthroughs are left alone.
_GZIP_MIN_BYTES = 1024
_GZIP_MIMETYPES = (
    "application/json",
    "application/javascript",
    "text/html",
    "text/plain",
    "text/css",
    "text/javascript",
    "image/svg+xml",
)


@app.after_request
def _gzip_response(resp):
    try:
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return resp
        if resp.direct_passthrough or resp.is_streamed or resp.status_code in (204, 304) or resp.status_code < 200:
            return resp
        if resp.headers.get("Content-Encoding") or resp.mimetype not in _GZIP_MIMETYPES:
            return resp
        data = resp.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return resp
        resp.set_data(gzip.compress(data, compresslevel=5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        resp.vary.add("Accept-Encoding")
    except Exception as exc:  # never fail a request because compression did
        logger.debug("gzip response skipped: %s", exc)
    return resp


# Data manager will be set from main.py
data_manager = None
_fallback_item_cache = None
_fallback_item_cache_mtime = None
_fallback_item_cache_lock = threading.Lock()
_fallback_villager_cache = {}
_fallback_villager_cache_time = None
_fallback_villager_cache_lock = threading.Lock()
_FALLBACK_CACHE_FILE = "cache_dump.json"
_FALLBACK_VILLAGER_CACHE_TTL = 300

# Guard: prevents multiple concurrent cache-refresh operations
_refresh_lock = threading.Lock()


def _request_search_query(*names: str) -> str:
    for name in names:
        value = request.args.get(name, "")
        if value and value.strip():
            return normalize_text(value)
    return ""


def _load_fallback_item_cache() -> tuple[dict, datetime | None]:
    global _fallback_item_cache, _fallback_item_cache_mtime
    cache_path = os.path.join(os.getcwd(), _FALLBACK_CACHE_FILE)
    if not os.path.exists(cache_path):
        return {}, None

    try:
        mtime = os.path.getmtime(cache_path)
        with _fallback_item_cache_lock:
            if _fallback_item_cache is not None and _fallback_item_cache_mtime == mtime:
                return dict(_fallback_item_cache), datetime.fromtimestamp(mtime)

            with open(cache_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                return {}, None
            _fallback_item_cache = loaded
            _fallback_item_cache_mtime = mtime
            return dict(loaded), datetime.fromtimestamp(mtime)
    except Exception as exc:
        logger.warning("Failed to load fallback item cache: %s", exc)
        return {}, None


def _get_item_cache() -> tuple[dict, datetime | None, float | None, str]:
    if data_manager is not None:
        with data_manager.lock:
            return (
                dict(data_manager.cache),
                data_manager.last_update,
                float(data_manager.cache_refresh_hours or 0) * 3600,
                "data_manager",
            )

    cache, last_update = _load_fallback_item_cache()
    return cache, last_update, None, "disk_cache"


def _scan_villager_dirs(villager_dirs=None) -> dict:
    """Return empty dict; disk Villagers.txt scanning is disabled to prevent file lock contention with SysBot."""
    return {}


_SYSBOT_VILLAGERS_CACHE: dict[str, list[str]] = {}
_SYSBOT_VILLAGERS_CACHE_TIME: float = 0.0
_SYSBOT_VILLAGERS_CACHE_TTL: float = 60.0
_SYSBOT_VILLAGERS_LOCK = threading.Lock()


def _fetch_island_villagers_from_sysbot(island_name: str) -> list[str]:
    """Fetch villagers list from an island's SysBot HTTP API."""
    try:
        from utils.sysbot_api import get_island_sysbot_client
        client = get_island_sysbot_client(island_name, timeout=2)
        resp = client.get_villagers_sync()
        if isinstance(resp, dict):
            raw = resp.get("villagers") or resp.get("data") or []
            if isinstance(raw, list):
                res = []
                for item in raw:
                    if isinstance(item, str) and item.strip():
                        res.append(item.strip().title())
                    elif isinstance(item, dict) and item.get("name"):
                        res.append(str(item["name"]).strip().title())
                return sorted(list(dict.fromkeys(res)))
    except Exception as exc:
        logger.debug("SysBot villagers fetch failed for %s: %s", island_name, exc)
    return []


def _get_sysbot_villagers_manifest(force_refresh: bool = False) -> tuple[dict[str, list[str]], str]:
    """
    Query SysBot HTTP APIs for villagers currently residing on each island across all 48 ports.
    Falls back to disk Villagers.txt if SysBot is offline or returns empty.
    Results are cached in memory for 60 seconds.
    """
    global _SYSBOT_VILLAGERS_CACHE, _SYSBOT_VILLAGERS_CACHE_TIME
    now = time.time()
    with _SYSBOT_VILLAGERS_LOCK:
        if not force_refresh and _SYSBOT_VILLAGERS_CACHE and (now - _SYSBOT_VILLAGERS_CACHE_TIME < _SYSBOT_VILLAGERS_CACHE_TTL):
            return _SYSBOT_VILLAGERS_CACHE.copy(), "sysbot_api_cache"

    islands = getattr(Config, "ALL_ISLAND_PORT_ORDER", [])
    if not islands:
        islands = [
            "Hiraya", "Tagumpay", "Bahaghari", "Alapaap", "Bituin",
            "Lakan", "Galak", "Aruga", "Likha", "Dalisay",
            "Marahuyo", "Diwa", "Bonita", "Gabay", "Dakila",
            "Kalangitan", "Malaya", "Pangarap", "Giliw", "Adhika"
        ]

    manifest: dict[str, list[str]] = {}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=16) as executor:
        future_to_isl = {executor.submit(_fetch_island_villagers_from_sysbot, isl): isl for isl in islands}
        for fut in future_to_isl:
            isl = future_to_isl[fut]
            try:
                v_list = fut.result()
                if v_list:
                    manifest[isl] = v_list
            except Exception:
                pass

    final_manifest: dict[str, list[str]] = {}
    for isl in islands:
        isl_title = isl.strip().title()
        if isl in manifest and manifest[isl]:
            final_manifest[isl_title] = sorted(manifest[isl])
        else:
            final_manifest[isl_title] = []

    with _SYSBOT_VILLAGERS_LOCK:
        _SYSBOT_VILLAGERS_CACHE = final_manifest
        _SYSBOT_VILLAGERS_CACHE_TIME = now

    return final_manifest.copy(), "sysbot_api"


def _get_villager_map(villager_dirs=None, skip_sysbot_merge: bool = False) -> tuple[dict, str]:
    if data_manager is not None:
        return data_manager.get_villagers(), "data_manager"

    manifest, _ = _get_sysbot_villagers_manifest()
    merged = {}
    for isl_name, v_list in manifest.items():
        for v_name in v_list:
            key = normalize_text(v_name)
            if key in merged:
                current_locs = [x.strip() for x in merged[key].split(",")]
                if isl_name not in current_locs:
                    merged[key] += f", {isl_name}"
            else:
                merged[key] = isl_name

    return merged, "sysbot_api"

# ---------------------------------------------------------------------------
# Auth â€” short-lived opaque tokens for Discord OAuth (website subscribers)
# Works cross-domain: frontend stores the token in localStorage and sends it
# as "Authorization: Bearer <token>" on every authenticated request.
# ---------------------------------------------------------------------------
_DISCORD_UA = "DiscordBot (https://chopaeng.com, 1.0)"
_ADMINISTRATOR_PERM = 0x8   # Discord Administrator permission bit
_VIEW_CHANNEL_PERM = 1 << 10
_ROLE_NAME_CACHE: dict[str, tuple[dict[str, str], float]] = {}
_ROLE_NAME_CACHE_TTL = 3600
_CHANNEL_OVERWRITE_CACHE: dict[str, tuple[list[str] | None, str | None, float]] = {}
_GUILD_CHANNELS_CACHE: tuple[list[dict], float] | None = None
_CHANNEL_OVERWRITE_CACHE_TTL = 300
_GUILD_CHANNELS_CACHE_TTL = 300

DODO_REVEAL_ROLE_MAX_AGE_SECONDS = 120


def _roles_unverified(user: dict | None):
    """A 503 response when the user's Discord roles weren't confirmed in the last two minutes.

    Use after _current_auth_user(force_refresh=True) on actions gated by roles (Dodo reveals,
    drops): when Discord can't be reached the refresh falls back to the cached session, whose
    roles may still include one a mod removed (e.g. Island Access). None when roles are fresh.
    """
    if not user or not getattr(Config, "DISCORD_LOGIN_ENABLED", True) or _secrets_bot_user(user):
        return None
    if time.time() - int(user.get("discord_checked_at") or 0) <= DODO_REVEAL_ROLE_MAX_AGE_SECONDS:
        return None
    return jsonify({
        "success": False,
        "error": "We couldn't confirm your Discord roles right now. Please try again in a moment.",
        "code": "roles_unverified",
    }), 503


def _secrets_bot_user(user: dict) -> bool:
    """True for the internal bot/dashboard-secret caller built in _current_auth_user."""
    return bool(user.get("_internal_secret_auth"))


def _current_auth_user(*, force_refresh: bool = False) -> dict | None:
    """Extract Bearer token or API key from request and return user dict, or None."""
    auth = request.headers.get("Authorization", "")
    api_key_hdr = request.headers.get("X-API-Key", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
    elif api_key_hdr:
        token = api_key_hdr.strip()

    if token:
        dash_secret = getattr(Config, "DASHBOARD_SECRET", "") or ""
        sys_key = getattr(Config, "SYSBOT_API_KEY", "") or ""
        if (dash_secret and _secrets.compare_digest(token, dash_secret)) or (sys_key and _secrets.compare_digest(token, sys_key)):
            uid = request.headers.get("X-User-ID") or "bot_system"
            uname = request.headers.get("X-Username") or "ChorderBot"
            return {
                "user_id": uid,
                "id": uid,
                "username": uname,
                "nickname": uname,
                "roles": ["admin"],
                "is_admin": True,
                "_internal_secret_auth": True,
            }

    if not auth.startswith("Bearer "):
        return None

    token = auth[len("Bearer "):]
    user = get_auth_user(token)
    if not user:
        return None

    # Discord is the only sign-in. Sessions from the removed email / YouTube / Twitch /
    # Patreon logins end here; signing in with Discord (same verified email) keeps their data.
    has_discord_link = (
        user.get("provider") == "discord" or
        bool(user.get("discord_id")) or
        "discord" in (user.get("connected_providers") or [])
    )
    if not has_discord_link:
        revoke_auth_token(token)
        return None

    if not force_refresh and not should_refresh(user):
        return user

    # A page fires several authed requests at once; once the 5-minute check is due, each of them
    # would call Discord. Only the first refreshes; the rest carry on with the cached profile,
    # which that refresh is about to replace (or revoke).
    # An explicit force_refresh waits its turn instead, since that caller needs fresh roles.
    refresh_lock = _auth_refresh_lock_for(str(user.get("user_id") or token))
    if not refresh_lock.acquire(blocking=force_refresh, timeout=15 if force_refresh else -1):
        return user
    try:
        return _refresh_auth_user(token, user)
    finally:
        refresh_lock.release()


_AUTH_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_AUTH_REFRESH_LOCKS_GUARD = threading.Lock()


def _auth_refresh_lock_for(key: str) -> threading.Lock:
    with _AUTH_REFRESH_LOCKS_GUARD:
        lock = _AUTH_REFRESH_LOCKS.get(key)
        if lock is None:
            lock = _AUTH_REFRESH_LOCKS[key] = threading.Lock()
        return lock


def _refresh_auth_user(token: str, user: dict) -> dict | None:
    """Re-check the user's Discord membership and roles; None when the session must end."""
    if not getattr(Config, "DISCORD_LOGIN_ENABLED", False):
        try:
            from utils.unified_auth import get_unified_account_by_id
            unified = get_unified_account_by_id(user.get("user_id"))
            if unified:
                for k in ("is_admin", "is_mod"):
                    if user.get(k):
                        unified[k] = user[k]
                update_auth_user(token, unified)
                return unified
        except Exception as ex:
            logger.debug("Unified user refresh note: %s", ex)
        return user

    try:
        refreshed = refresh_user_payload(user)
        update_auth_user(token, refreshed)
        return refreshed
    except DiscordNotGuildMember:
        logger.info("Revoking auth token for user_id=%s: no longer in guild", user.get("user_id"))
        revoke_auth_token(token)
        return None
    except DiscordMembershipUnavailable as exc:
        logger.warning("Could not refresh Discord auth user %s: %s", user.get("user_id"), exc)
        if is_beyond_stale_grace(user):
            revoke_auth_token(token)
            return None
        return user


# ── Response hygiene ─────────────────────────────────────────────────────────

def _safe_error(exc: BaseException, fallback: str = "Something went wrong. Please try again.") -> str:
    """Message that is safe to return to a client.

    Validation errors (ValueError) are written for the caller and pass through. Everything
    else (DB, network, filesystem, Discord, SysBot...) can carry paths, SQL, hostnames or
    tokens, so it is logged server-side and replaced with a generic message.
    """
    if isinstance(exc, ValueError):
        return str(exc)
    logging.getLogger("flask_api").error("Request failed on %s: %s", request.path, exc, exc_info=exc)
    return fallback


def _is_staff_viewer() -> bool:
    user = _current_auth_user()
    return bool(user and (user.get("is_admin") or user.get("is_mod")))


def _public_presence_id(session_id: str) -> str:
    """Opaque roster id. The raw session id (usr_<discord id>) is a credential, so never expose it."""
    key = str(getattr(Config, "FLASK_SECRET_KEY", "") or "cp_secret").encode("utf-8")
    return "p_" + hmac.new(key, str(session_id).encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def _presence_session_for(session_id: str, auth_user: dict | None) -> str:
    """Return the client's session id, or "" if it claims to be a logged-in user it isn't."""
    session_id = (session_id or "").strip()
    if not session_id.startswith("usr_"):
        return session_id
    uid = ""
    if auth_user:
        uid = str(auth_user.get("user_id") or auth_user.get("discord_id") or auth_user.get("id") or "")
    return session_id if uid and session_id == f"usr_{uid}" else ""


@app.errorhandler(Exception)
def _handle_unexpected_error(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    logging.getLogger("flask_api").exception("Unhandled error on %s", request.path)
    return jsonify({"ok": False, "success": False, "error": "Internal server error"}), 500


# Keep legacy helper names stable while sharing the access engine with dashboard APIs.
_is_mod = island_access.is_mod
_has_island_access = island_access.has_island_access
_configured_subscription_role_ids = island_access.configured_subscription_role_ids
_discord_bot_auth_value = island_access.discord_bot_auth_value
_discord_api_json = island_access.discord_api_json
_discord_channel_overwrite_roles = island_access.discord_channel_overwrite_roles
_discord_guild_channels = island_access.discord_guild_channels
_find_discord_island_channel_id = island_access.find_discord_island_channel_id
_is_member_island = island_access.is_member_island
_effective_island_required_roles = island_access.effective_island_required_roles
_excluded_profile_role_ids = island_access.excluded_profile_role_ids
_get_guild_role_names = island_access.get_guild_role_names
_role_payload = island_access.role_payload


def _resolved_island_required_roles(
    island_name: str | None,
    cat: str | None,
    required_roles: list[str] | None,
    island_type: str | None = None,
    channel_id: str | None = None,
) -> tuple[list[str], str | None, str]:
    info = island_access.resolved_island_required_roles(
        island_name,
        cat,
        required_roles,
        island_type,
        channel_id,
    )
    return info.required_roles, info.channel_id, info.access_source


def _iso_to_unix(value: str | None) -> int | None:
    """Convert a Discord ISO timestamp to Unix seconds when possible."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def _user_has_sub_rules(user: dict | None) -> bool:
    """True if user has accepted sub rules (DB table or holds Discord role)."""
    if not user:
        return False
    if user.get("is_admin"):
        return True

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(user)
    sub_role_str = str(getattr(Config, "SUB_RULES_ROLE_ID", 1077997850165772398) or 1077997850165772398)
    island_access_role_str = str(Config.ISLAND_ACCESS_ROLE) if Config.ISLAND_ACCESS_ROLE else sub_role_str
    user_roles = {str(r) for r in user.get("roles", [])}
    has_role = (sub_role_str in user_roles) or (island_access_role_str in user_roles)

    in_db = False
    if cand_ids:
        try:
            conn = get_db()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS user_sub_rules_accepted (user_id TEXT PRIMARY KEY, accepted_at TEXT)")
                rule_ph = ",".join("?" for _ in cand_ids)
                row = conn.execute(
                    f"SELECT accepted_at FROM user_sub_rules_accepted WHERE user_id IN ({rule_ph}) LIMIT 1",
                    cand_ids,
                ).fetchone()
                in_db = bool(row)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Failed to query user_sub_rules_accepted: %s", exc)

    if has_role and not in_db and cand_ids:
        now_iso = datetime.utcnow().isoformat()
        try:
            with connect_db() as c_sync:
                for uid in set(cand_ids):
                    c_sync.execute(
                        "INSERT OR REPLACE INTO user_sub_rules_accepted (user_id, accepted_at) VALUES (?, ?)",
                        (uid, now_iso),
                    )
                c_sync.commit()
            in_db = True
        except Exception:
            pass

    # For Discord members the live role decides. Missing it while the DB says "accepted"
    # means a mod removed it in Discord: clear the stale acceptance so they must accept the
    # rules again (which grants the role back), instead of the website quietly re-adding it.
    # Website-only accounts have no role, so the DB decides for them.
    has_discord = bool(
        str(user.get("discord_id") or "").isdigit()
        or "discord" in (user.get("connected_providers") or [])
        or user.get("provider") == "discord"
    )
    roles_are_live = time.time() - int(user.get("discord_checked_at") or 0) < _LIVE_ROLES_SECONDS
    if in_db and not has_role and has_discord and roles_are_live and getattr(Config, "DISCORD_LOGIN_ENABLED", True):
        try:
            with connect_db() as c_sync:
                rule_ph = ",".join("?" for _ in cand_ids)
                c_sync.execute(f"DELETE FROM user_sub_rules_accepted WHERE user_id IN ({rule_ph})", cand_ids)
                c_sync.commit()
            logger.info("Sub rules role missing in Discord for %s: cleared website acceptance", user.get("user_id"))
        except Exception as exc:
            logger.warning("Could not clear stale sub rules acceptance for %s: %s", user.get("user_id"), exc)
        return False

    return in_db or has_role


# Roles fetched from Discord this recently count as live (a role missing from them was removed)
_LIVE_ROLES_SECONDS = 600


def _user_has_order_rules(user: dict | None) -> bool:
    """True if user has accepted order rules (DB table or holds Discord role)."""
    if not user:
        return False
    if user.get("is_admin"):
        return True

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(user)
    order_role_str = str(getattr(Config, "ORDER_RULES_ROLE_ID", 1176191811438510231) or 1176191811438510231)
    user_roles = {str(r) for r in user.get("roles", [])}
    has_role = order_role_str in user_roles

    in_db = False
    if cand_ids:
        try:
            conn = get_db()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS user_order_rules_accepted (user_id TEXT PRIMARY KEY, accepted_at TEXT)")
                rule_ph = ",".join("?" for _ in cand_ids)
                row = conn.execute(
                    f"SELECT accepted_at FROM user_order_rules_accepted WHERE user_id IN ({rule_ph}) LIMIT 1",
                    cand_ids,
                ).fetchone()
                in_db = bool(row)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Failed to query user_order_rules_accepted: %s", exc)

    if has_role and not in_db and cand_ids:
        now_iso = datetime.utcnow().isoformat()
        try:
            with connect_db() as c_sync:
                for uid in set(cand_ids):
                    c_sync.execute(
                        "INSERT OR REPLACE INTO user_order_rules_accepted (user_id, accepted_at) VALUES (?, ?)",
                        (uid, now_iso),
                    )
                c_sync.commit()
            in_db = True
        except Exception:
            pass

    if in_db and not has_role:
        from utils.discord_membership import add_guild_member_role
        discord_uids = [str(uid) for uid in cand_ids if str(uid).isdigit()]
        d_uid = str(user.get("discord_id") or user.get("user_id") or "").strip()
        if d_uid.isdigit() and d_uid not in discord_uids:
            discord_uids.append(d_uid)
        conn_accs = user.get("connected_accounts") or {}
        if isinstance(conn_accs, dict) and "discord" in conn_accs:
            disc_acc = conn_accs["discord"]
            if isinstance(disc_acc, dict) and disc_acc.get("id"):
                disc_id = str(disc_acc["id"]).strip()
                if disc_id.isdigit() and disc_id not in discord_uids:
                    discord_uids.append(disc_id)
        for du in discord_uids:
            if add_guild_member_role(du, order_role_str):
                user.setdefault("roles", []).append(order_role_str)
                has_role = True

    return in_db or has_role


def _user_id_param(user_id: str) -> int | str:
    """Use integer IDs for SQLite INTEGER comparisons, falling back to text."""
    return int(user_id) if str(user_id).isdigit() else str(user_id)


def _load_profile_subscriptions(user: dict) -> dict:
    """Return subscription/access info inferred from Discord roles and local DB."""
    user_role_ids = {str(r) for r in user.get("roles", [])}
    accessible_islands: list[dict] = []
    matched_role_ids: set[str] = set()
    configured_role_ids = set(_configured_subscription_role_ids())
    excluded_role_ids = _excluded_profile_role_ids()
    role_names = _get_guild_role_names()
    alert_subscriptions: list[dict] = []

    db = get_db()
    try:
        all_required_role_ids: set[str] = set()
        rows = db.execute(
            "SELECT id, name, display_name, is_visible, cat, type, required_roles, channel_id FROM islands ORDER BY name"
        ).fetchall()
        for row in rows:
            island = row_to_island_dict(dict(row))
            if island.get("is_visible") is False:
                continue
            raw_required_roles, resolved_channel_id, access_source = _resolved_island_required_roles(
                island.get("name"),
                island.get("cat"),
                island.get("required_roles", []),
                island.get("type"),
                island.get("channel_id"),
            )
            profile_required_roles = [
                str(r)
                for r in raw_required_roles
                if str(r) and str(r) not in excluded_role_ids
            ]
            is_member_island = _is_member_island(island.get("cat"), island.get("type"))

            # The raw channel overwrite roles decide access. The filtered profile roles
            # are only for display so general access/mod roles do not show as subscriptions.
            raw_matching_roles = sorted(user_role_ids & set(raw_required_roles))
            display_matching_roles = sorted(set(raw_matching_roles) - excluded_role_ids)
            all_required_role_ids.update(profile_required_roles)

            has_channel_access = bool(raw_matching_roles) or bool(user.get("is_mod")) or bool(user.get("is_admin"))
            looks_like_sub_island = is_member_island or bool(raw_required_roles)
            if has_channel_access and looks_like_sub_island:
                matched_role_ids.update(display_matching_roles)
                accessible_islands.append({
                    "id": island.get("id"),
                    "name": island.get("display_name") or island.get("name"),
                    "canonical_name": island.get("name"),
                    "type": island.get("type"),
                    "channel_id": resolved_channel_id,
                    "access_source": access_source,
                    "required_roles": [_role_payload(rid, role_names) for rid in profile_required_roles],
                    "matched_roles": [_role_payload(rid, role_names) for rid in display_matching_roles],
                })

        try:
            sub_rows = db.execute(
                "SELECT island_clean, kind, has_island_access "
                "FROM island_subscriptions WHERE user_id = ? ORDER BY island_clean, kind",
                (_user_id_param(user.get("user_id", "")),),
            ).fetchall()
            alert_subscriptions = [
                {
                    "island": row["island_clean"],
                    "kind": row["kind"],
                    "has_island_access": bool(row["has_island_access"]),
                }
                for row in sub_rows
            ]
        except Exception:
            # Older DBs may not have alert subscriptions yet.
            alert_subscriptions = []
    finally:
        db.close()

    subscription_role_ids = sorted((user_role_ids & all_required_role_ids) - excluded_role_ids)
    matched_subscription_role_ids = sorted(matched_role_ids - excluded_role_ids)
    subscription_roles = [_role_payload(rid, role_names) for rid in subscription_role_ids]
    configured_subscription_roles = [
        _role_payload(rid, role_names)
        for rid in sorted(configured_role_ids - excluded_role_ids)
    ]
    matched_subscription_roles = [
        _role_payload(rid, role_names)
        for rid in matched_subscription_role_ids
    ]

    return {
        "role_ids": subscription_role_ids,
        "role_names": [role["name"] for role in subscription_roles if role.get("name") and not str(role["name"]).isdigit()],
        "roles": subscription_roles,
        "configured_subscription_role_ids": sorted(configured_role_ids - excluded_role_ids),
        "configured_subscription_role_names": [role["name"] for role in configured_subscription_roles if role.get("name") and not str(role["name"]).isdigit()],
        "configured_subscription_roles": configured_subscription_roles,
        "matched_subscription_role_ids": matched_subscription_role_ids,
        "matched_subscription_role_names": [role["name"] for role in matched_subscription_roles if role.get("name") and not str(role["name"]).isdigit()],
        "matched_subscription_roles": matched_subscription_roles,
        "accessible_islands": accessible_islands,
        "accessible_member_islands": accessible_islands,
        "alert_subscriptions": alert_subscriptions,
        "island_alert_subscriptions": alert_subscriptions,
    }


def _load_profile_visit_stats(user_id: str) -> dict:
    """Return visit totals, top destinations, recent visits, and warning summary."""
    uid = _user_id_param(user_id)
    guild_id = Config.GUILD_ID
    empty = {
        "total": 0,
        "authorized": 0,
        "unauthorized": 0,
        "first_visit_at": None,
        "last_visit_at": None,
        "by_type": {},
        "most_visited_islands": [],
        "recent_visits": [],
        "warnings": {"total": 0, "last_warning_at": None},
    }

    db = get_db()
    try:
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN authorized = 1 THEN 1 ELSE 0 END) AS authorized, "
            "SUM(CASE WHEN authorized = 0 THEN 1 ELSE 0 END) AS unauthorized, "
            "MIN(timestamp) AS first_visit_at, MAX(timestamp) AS last_visit_at "
            "FROM island_visits WHERE user_id = ? AND guild_id = ?",
            (uid, guild_id),
        ).fetchone()
        if row:
            empty.update({
                "total": int(row["total"] or 0),
                "authorized": int(row["authorized"] or 0),
                "unauthorized": int(row["unauthorized"] or 0),
                "first_visit_at": row["first_visit_at"],
                "last_visit_at": row["last_visit_at"],
            })

        type_rows = db.execute(
            "SELECT island_type, COUNT(*) AS visit_count "
            "FROM island_visits WHERE user_id = ? AND guild_id = ? "
            "GROUP BY island_type ORDER BY visit_count DESC",
            (uid, guild_id),
        ).fetchall()
        empty["by_type"] = {
            (row["island_type"] or "unknown"): int(row["visit_count"] or 0)
            for row in type_rows
        }

        top_rows = db.execute(
            "SELECT destination, island_type, COUNT(*) AS visit_count, MAX(timestamp) AS last_visit_at "
            "FROM island_visits WHERE user_id = ? AND guild_id = ? "
            "GROUP BY destination, island_type "
            "ORDER BY visit_count DESC, last_visit_at DESC LIMIT 10",
            (uid, guild_id),
        ).fetchall()
        empty["most_visited_islands"] = [
            {
                "name": row["destination"],
                "type": row["island_type"],
                "visits": int(row["visit_count"] or 0),
                "last_visit_at": row["last_visit_at"],
            }
            for row in top_rows
        ]

        recent_rows = db.execute(
            "SELECT id, ign, origin_island, destination, authorized, timestamp, island_type "
            "FROM island_visits WHERE user_id = ? AND guild_id = ? "
            "ORDER BY timestamp DESC LIMIT 10",
            (uid, guild_id),
        ).fetchall()
        empty["recent_visits"] = [
            {
                "id": row["id"],
                "ign": row["ign"],
                "origin_island": row["origin_island"],
                "destination": row["destination"],
                "authorized": bool(row["authorized"]),
                "timestamp": row["timestamp"],
                "island_type": row["island_type"],
            }
            for row in recent_rows
        ]

        try:
            warn_row = db.execute(
                "SELECT COUNT(*) AS total, MAX(timestamp) AS last_warning_at "
                "FROM warnings WHERE user_id = ? AND guild_id = ?",
                (uid, guild_id),
            ).fetchone()
            if warn_row:
                empty["warnings"] = {
                    "total": int(warn_row["total"] or 0),
                    "last_warning_at": warn_row["last_warning_at"],
                }
        except Exception:
            pass
    except Exception:
        logger.exception("Failed to load profile visit stats for user_id=%s", user_id)
    finally:
        db.close()

    return empty


_DODO_WEBHOOK_DEBOUNCE = {}


def _prune_debounce(store: dict, ttl: float, now: float, max_size: int = 2000) -> None:
    """Drop expired entries once a debounce map grows, so it cannot grow without bound."""
    if len(store) < max_size:
        return
    for key in [k for k, ts in store.items() if now - ts >= ttl]:
        store.pop(key, None)
    if len(store) >= max_size:
        store.clear()
_DODO_WEBHOOK_DEBOUNCE_TTL = 300  # 5 minutes

def _fire_dodo_webhook(
    username: str,
    nickname: str,
    user_id: str,
    avatar_url: str,
    island_name: str,
    dodo_code: str,
    channel_id: str = None,
    char_ign: str = "",
    char_island: str = "",
    discord_id: str = "",
    reveal_row_id: int | None = None,
) -> None:
    """POST a Discord webhook message in the background, then attach its URL to the
    already-saved reveal row."""
    url = Config.DODO_LOG_WEBHOOK_URL
    if not url:
        return

    now = time.monotonic()
    cache_key = f"{user_id}:{island_name}"
    last_fired = _DODO_WEBHOOK_DEBOUNCE.get(cache_key)
    if last_fired and now - last_fired < _DODO_WEBHOOK_DEBOUNCE_TTL:
        return
    _prune_debounce(_DODO_WEBHOOK_DEBOUNCE, _DODO_WEBHOOK_DEBOUNCE_TTL, now)
    _DODO_WEBHOOK_DEBOUNCE[cache_key] = now

    display_name = (nickname or "").strip() or (username or "").strip() or "Unknown User"
    # Mention only real Discord IDs: website accounts (u_patreon_…, cp_usr_…) are not in the server
    mention_id = next((str(i).strip() for i in (discord_id, user_id) if str(i or "").strip().isdigit()), "")

    # The reveal row was saved under the Discord ID when there is one, else the website ID
    repeat_count = count_recent_reveals(mention_id or str(user_id or ""), island_name)
    embed = build_dodo_log_embed(
        source="website",
        island_name=island_name,
        display_name=display_name,
        user_id=mention_id,
        channel_id=channel_id,
        avatar_url=avatar_url,
        char_ign=char_ign,
        char_island=char_island,
        repeat_count=repeat_count,
        nickname_warning=bool(nickname_warning_for(nickname)),
    )

    payload = json.dumps({"embeds": [embed]}).encode()
    webhook_execute = url
    sep = "&" if "?" in webhook_execute else "?"
    webhook_execute = f"{webhook_execute}{sep}wait=true"
    try:
        resp = discord_request(
            webhook_execute,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _DISCORD_UA},
            method="POST",
            timeout=10,
        )
        body = resp.body
        if resp.status not in (200, 204):
            logger.warning("Dodo webhook unexpected HTTP status: %s", resp.status)
        else:
            logger.debug("Dodo webhook delivered for island=%s user=%s", island_name, username)
        message_url = None
        if resp.status == 200 and body and Config.GUILD_ID:
            try:
                data = json.loads(body)
                mid = data.get("id")
                cid = data.get("channel_id")
                if mid and cid:
                    message_url = f"https://discord.com/channels/{Config.GUILD_ID}/{cid}/{mid}"
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("Dodo webhook response not JSON: %s", exc)
        if message_url and reveal_row_id:
            _update_dodo_reveal_url(reveal_row_id, message_url)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            pass
        logger.warning("Dodo webhook failed HTTP %s: %s", exc.code, body)
    except Exception as exc:
        logger.warning("Dodo webhook failed: %s", exc)


_SUGGESTIONS_WEBHOOK_DEBOUNCE = {}
_SUGGESTIONS_WEBHOOK_DEBOUNCE_TTL = 15  # 15 seconds cooldown


def _fire_suggestions_webhook(
    title: str,
    description: str,
    discord_username: str = "",
    in_game_name: str = "",
    island_name: str = "",
    page_url: str = "",
    user_id: str = "",
    avatar_url: str = "",
) -> None:
    """POST a formatted suggestion embed to the configured Discord suggestions webhook."""
    url = getattr(Config, "SUGGESTIONS_WEBHOOK_URL", "") or os.getenv("SUGGESTIONS_WEBHOOK_URL", "")
    if not url:
        logger.warning("SUGGESTIONS_WEBHOOK_URL is not configured")
        return

    sender_identity = f"`{discord_username.strip()}`" if discord_username.strip() else ("Anonymous Resident" if not user_id else f"<@{user_id}>")
    in_game_parts = []
    if in_game_name.strip():
        in_game_parts.append(f"IGN: **{in_game_name.strip()}**")
    if island_name.strip():
        in_game_parts.append(f"Island: **{island_name.strip()}**")
    in_game_info = " • ".join(in_game_parts)

    fields = [
        {
            "name": "👤 Submitted By",
            "value": f"{sender_identity}\n{in_game_info}" if in_game_info else sender_identity,
            "inline": True,
        },
        {
            "name": "📝 Details / Feedback",
            "value": description.strip()[:1024],
            "inline": False,
        },
    ]

    if page_url.strip():
        clean_page_url = page_url.strip()
        fields.append({
            "name": "🌐 Submitted From",
            "value": f"[{clean_page_url}]({clean_page_url})",
            "inline": False,
        })

    embed = {
        "title": f"💡 Suggestion: {title.strip()[:250]}",
        "description": "A new resident suggestion has been submitted from **Chopaeng**!",
        "color": 0x198754,
        "fields": fields,
        "footer": {
            "text": "Chopaeng Resident Feedback System • Live Dispatcher",
            "icon_url": "https://www.chopaeng.com/logo.png",
        },
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    if avatar_url:
        embed["thumbnail"] = {"url": avatar_url}

    payload = json.dumps({
        "username": "Chopaeng Suggestion Box",
        "avatar_url": "https://www.chopaeng.com/logo.png",
        "embeds": [embed],
    }).encode("utf-8")

    try:
        resp = discord_request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _DISCORD_UA},
            method="POST",
            timeout=10,
        )
        if resp.status not in (200, 204):
            logger.warning("Suggestions webhook unexpected HTTP status: %s", resp.status)
        else:
            logger.info("Suggestions webhook delivered: %r by %s", title[:40], discord_username or user_id or "anonymous")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            pass
        logger.warning("Suggestions webhook failed HTTP %s: %s", exc.code, body)
    except Exception as exc:
        logger.warning("Suggestions webhook failed: %s", exc)


def set_data_manager(dm):
    """Set the data manager instance"""
    global data_manager
    data_manager = dm
    set_active_data_manager(dm)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_image_from_html(html_content):
    """Extract image URL from HTML content"""
    if not html_content:
        return None
    match = re.search(r'<img [^>]*src="([^"]+)"', html_content)
    return match.group(1) if match else None


def process_post_attributes(post_id, attrs, unlocked=False, required_tier=None):
    """Process Patreon post attributes.

    Posts are fetched with the creator's token, which can read patron-only posts. Those are served as
    teasers only (title, date, link to Patreon): no body, embed or images. ``unlocked=True`` serves
    the full post; only pass it after ``_viewer_can_read_post`` said the signed-in viewer may read it.
    """
    is_public = bool(attrs.get("is_public"))
    show_body = is_public or unlocked
    image_url = None

    if show_body and attrs.get("embed_data"):
        embed = attrs["embed_data"]
        if "image" in embed and "url" in embed["image"]:
            image_url = embed["image"]["url"]
        elif "thumbnail_url" in embed:
            image_url = embed["thumbnail_url"]

    if show_body and not image_url:
        image_url = extract_image_from_html(attrs.get("content"))

    out = {
        "id": post_id,
        "attributes": {
            "embed_data": attrs.get("embed_data") if show_body else None,
            "title": attrs["title"],
            "content": (attrs.get("content") or "") if show_body else "",
            "published_at": attrs["published_at"],
            "url": attrs["url"],
            "is_public": attrs["is_public"],
            "image": {"large_url": image_url}
        },
        "type": "post"
    }
    if not is_public:
        out["attributes"]["unlocked"] = bool(unlocked)
        out["attributes"]["required_tier"] = required_tier
    return out


def _patreon_tiers_by_id() -> dict[str, dict]:
    """Campaign tiers keyed by ID (the Patreon client caches them for 10 minutes). {} on failure."""
    try:
        from utils.patreon import patreon
        return {str(t["id"]): t for t in patreon.tiers(published_only=False)}
    except Exception as exc:
        logger.warning("Patreon tiers unavailable: %s", exc)
        return {}


def _post_required_tier(attrs, tiers_by_id) -> dict | None:
    """Cheapest tier that can read a patron-only post, for the "who can read this" label."""
    if attrs.get("is_public"):
        return None
    tiers = [tiers_by_id[str(t)] for t in attrs.get("tiers") or [] if str(t) in tiers_by_id]
    if not tiers:
        return None
    cheapest = min(tiers, key=lambda t: t.get("amount_cents") or 0)
    return {"title": cheapest.get("title"), "amount_cents": cheapest.get("amount_cents") or 0}


def _viewer_can_read_post(user, attrs, tiers_by_id) -> bool:
    """Whether the signed-in (Discord) viewer may read a patron-only Patreon post.

    - Staff can read everything.
    - A post open to the Free tier needs only a signed-in account: following on Patreon is free,
      and checking the ~9k-member list on every view is too slow.
    - Otherwise the viewer needs the Discord role (or tier name) of one of the post's tiers.
      Patreon gives each paid tier a Discord role, so a linked patron has it automatically.
    """
    if attrs.get("is_public"):
        return True
    if not user:
        return False
    roles = {str(r) for r in (user.get("roles") or [])}
    if user.get("is_admin") or user.get("is_mod") or _is_mod(list(roles)):
        return True
    try:
        from utils.discord_membership import _admin_role_ids
        if roles & _admin_role_ids():
            return True
    except Exception:
        pass

    tiers = [tiers_by_id.get(str(t)) for t in attrs.get("tiers") or []]
    tiers = [t for t in tiers if t]
    if not tiers:
        return False  # unknown tiers (lookup failed): stay locked
    if any((t.get("amount_cents") or 0) == 0 for t in tiers):
        return True

    lowered = {r.lower() for r in roles}
    for tier in tiers:
        if roles & {str(r) for r in (tier.get("discord_role_ids") or [])}:
            return True
        if (tier.get("title") or "").lower() in lowered:
            return True
    return False


_file_cache: dict = {}
_file_cache_lock = threading.Lock()
_FILE_CACHE_TTL = 3  # seconds


def get_file_content(folder_path, filename):
    """Fetch content from SysBot telemetry / API rather than reading disk files.
    Dodo.txt, Visitors.txt, and Villagers.txt are resolved from SysBot to avoid
    FileShare.None lock collisions.
    """
    if filename in ("Dodo.txt", "Visitors.txt", "Villagers.txt"):
        if filename == "Villagers.txt":
            return None
        isl_name = os.path.basename(str(folder_path or "").rstrip(r"\/"))
        try:
            from utils.sysbot_telemetry import get_cached_island_telemetry
            telem = get_cached_island_telemetry(isl_name, mask_sub_dodo=False)
            if telem and telem.get("is_online"):
                if filename == "Dodo.txt":
                    return telem.get("raw_dodo")
                elif filename == "Visitors.txt":
                    v_cnt = telem.get("visitors_count", 0)
                    v_list = telem.get("visitor_list") or []
                    return f"{v_cnt}\n" + "\n".join(v_list) if v_list else str(v_cnt)
        except Exception:
            pass
        return None

    path = os.path.join(folder_path, filename)

    now = time.monotonic()
    with _file_cache_lock:
        cached = _file_cache.get(path)
        if cached is not None:
            content, ts = cached
            if now - ts < _FILE_CACHE_TTL:
                return content

    if not os.path.exists(path):
        return None

    for attempt in range(3):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                content = f.read().strip()
            with _file_cache_lock:
                _file_cache[path] = (content, time.monotonic())
            return content
        except OSError:
            if attempt < 2:
                time.sleep(0.05)
        except Exception:
            break

    # Return stale cache rather than None if the file is still locked
    with _file_cache_lock:
        cached = _file_cache.get(path)
    if cached is not None:
        return cached[0]
    return None


def process_island(entry, island_type):
    """Process island data for Dodo API from SysBot telemetry"""
    name = entry.name.upper() if hasattr(entry, "name") else str(entry).upper()

    live_telem = None
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry
        live_telem = get_cached_island_telemetry(name, mask_sub_dodo=False)
    except Exception:
        pass

    raw_dodo = live_telem.get("raw_dodo") if live_telem and live_telem.get("is_online") else None
    raw_visitors = str(live_telem.get("visitors_count", 0)) if live_telem and live_telem.get("is_online") else "0"

    status = "ONLINE"
    display_dodo = raw_dodo
    display_visitors = "0/7"

    # Visitor Logic
    if raw_visitors:
        if raw_visitors.upper() == "FULL":
            display_visitors = "FULL"
        elif raw_visitors.isdigit():
            display_visitors = f"{raw_visitors}/7"
        else:
            display_visitors = raw_visitors

    # Dodo/Status Logic
    if island_type == "VIP":
        status = "SUB ONLY"
        display_dodo = "SUB ONLY"
    else:
        if raw_dodo is None:
            status = "OFFLINE"
            display_dodo = "....."
            display_visitors = "0/7"
        elif raw_dodo in ["00000", "-----", ""]:
            status = "REFRESHING"
            display_dodo = "WAIT..."
            display_visitors = "0/7"
        else:
            display_dodo = raw_dodo

    return {
        "name": name,
        "dodo": display_dodo,
        "status": status,
        "type": island_type,
        "visitors": display_visitors
    }


def _build_island_response(
    entry,
    island_type,
    db_island=None,
    discord_bot_online=None,
    viewer_roles=None,
    viewer_is_mod=False,
    entry_path=None,
):
    """Build the enriched island response merging SysBot API telemetry with DB metadata."""
    if isinstance(entry, str):
        name = entry.strip().upper()
        path = entry_path
    else:
        name = entry.name.upper()
        path = getattr(entry, "path", None) or entry_path

    db_island = db_island or {}
    viewer_roles = [str(role_id) for role_id in (viewer_roles or []) if str(role_id)]
    default_cat = "member" if island_type == "VIP" else "order" if island_type == "Order" else "public"
    island_cat = db_island.get("cat") or default_cat
    required_roles, resolved_channel_id, access_source = _resolved_island_required_roles(
        name,
        island_cat,
        db_island.get("required_roles", []),
        island_type,
        db_island.get("channel_id"),
    )
    # For member/VIP islands, fall back to configured subscription roles so
    # viewer_has_access is never True for non-subs when required_roles is empty.
    if _is_member_island(island_cat, island_type) and not required_roles:
        required_roles = _effective_island_required_roles(island_cat, required_roles, island_type)
    viewer_has_access = _has_island_access(
        viewer_roles,
        required_roles,
        viewer_is_mod,
        island_name=name,
    )

    # Check live SysBot telemetry cache first (mask_sub_dodo=False so raw dodo is checked; masked below)
    live_telemetry = None
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry
        live_telemetry = get_cached_island_telemetry(name, mask_sub_dodo=False)
    except Exception:
        live_telemetry = None

    if live_telemetry and live_telemetry.get("is_online"):
        is_valid = live_telemetry.get("is_dodo_valid")
        is_refreshing = live_telemetry.get("is_refreshing_dodo")
        status = "ONLINE" if is_valid else ("REFRESHING" if is_refreshing else "ONLINE")
        visitors = live_telemetry.get("visitors_count", 0)
        visitor_list = live_telemetry.get("visitor_list", [])
        raw_dodo = live_telemetry.get("raw_dodo")
        dodo_code = raw_dodo if is_valid else ("GETTIN'" if is_refreshing else None)
        sysbot_online = True
    else:
        status = "OFFLINE"
        dodo_code = None
        visitors = 0
        visitor_list = []
        raw_dodo = None
        sysbot_online = False

    # Access gating:
    #   Free islands  (public) → everyone sees the dodo code
    #   Sub islands   (member) → subs, mods, admins only (dodo revealed via POST /api/islands/<name>/dodo)
    #   Order islands (order)  → bot-only, never exposed directly in public islands list
    is_free_island   = island_type == "Free" or island_cat == "public"
    is_sub_island    = _is_member_island(island_cat, island_type)
    is_order_island  = island_type == "Order" or island_cat == "order"

    if is_sub_island:
        if not viewer_has_access:
            status = "SUB ONLY"
        dodo_code = None  # Sub island Dodo codes are strictly hidden; must be explicitly revealed via POST /api/islands/<name>/dodo
    elif is_order_island:
        dodo_code = None  # order dodo is bot-only, never exposed via API
    else:
        if status not in ("ONLINE", "REFRESHING"):
            dodo_code = None
        else:
            dodo_code = raw_dodo

    gametime_data = live_telemetry.get("gametime") if live_telemetry else None
    gt_obj = gametime_data if isinstance(gametime_data, dict) else {}
    game_time = gt_obj.get("formatted") or gt_obj.get("game_time")
    is_nooks_open = gt_obj.get("is_nooks_open")
    nooks_status = gt_obj.get("nooks_status")
    nooks_message = gt_obj.get("nooks_message")
    mode = gt_obj.get("mode") or (live_telemetry.get("mode") if live_telemetry else None)

    return {
        "id":                db_island.get("id", name.lower()),
        "name":              (db_island.get("display_name") or name.title()),
        "canonical_name":    name,
        "cat":               island_cat,
        "description":       db_island.get("description", ""),
        "dodo_code":         dodo_code,
        "visitors":          visitors,
        "visitor_list":      visitor_list,
        "items":             db_island.get("items", []),
        "map_url":           db_island.get("map_url"),
        "seasonal":          db_island.get("seasonal", ""),
        "status":            status,
        "theme":             db_island.get("theme", "teal"),
        "type":              db_island.get("type") or island_type,
        "updated_at":        db_island.get("updated_at") or datetime.now().isoformat(),
        "bot_online":        sysbot_online,
        "discord_bot_online": sysbot_online,  # Backwards compatibility alias for frontend
        "source":            "sysbot_api",
        "channel_id":        resolved_channel_id,
        "required_roles":    required_roles,
        "access_source":     access_source,
        "accessible":        viewer_has_access,
        "viewer_has_access": viewer_has_access,
        "console_stage":     live_telemetry.get("console_stage") if live_telemetry else ("Running" if sysbot_online else "Offline"),
        "game_time":         game_time,
        "is_nooks_open":     is_nooks_open,
        "nooks_status":      nooks_status,
        "nooks_message":     nooks_message,
        "mode":              mode,
        "gametime":          gametime_data,
    }

# ============================================================================
# ISLAND METADATA CRUD (separate from /api/islands Dodo-status endpoint)
# ============================================================================

ALLOWED_CATEGORIES = {"public", "member", "order"}
ALLOWED_THEMES = {"pink", "teal", "purple", "gold"}
ALLOWED_STATUSES = {"ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE"}

# ============================================================================
# AUTH ROUTES  (Discord OAuth for public website subscribers)
# ============================================================================

@app.route("/robots.txt")
def robots_txt():
    """This host is the website's API, not pages: keep every crawler off it.
    A crawler rendering the site once sent ~220k requests here in two hours and timed it out.

    Cloudflare's managed robots.txt is put in front of this and contains "User-agent: *" /
    "Allow: /", which wins over a "Disallow: /" for the same group. A group naming the crawler
    takes priority over "*", so the crawlers that render pages are listed by name.
    """
    crawlers = (
        "Googlebot", "bingbot", "AhrefsBot", "AhrefsSiteAudit", "SemrushBot", "MJ12bot", "DotBot",
        "YandexBot", "Baiduspider", "Applebot", "DuckDuckBot", "PetalBot", "DataForSeoBot", "*",
    )
    body = "".join(f"User-agent: {name}\n" for name in crawlers) + "Disallow: /\n"
    resp = Response(body, mimetype="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/auth/discord")
def auth_discord():
    """Initiate Discord OAuth flow for public website users & subscribers."""
    return_to = request.args.get("return_to", "")
    allowed_hosts = {"www.chopaeng.com", "chopaeng.com", "localhost"}
    try:
        parsed = urllib.parse.urlparse(return_to)
        if parsed.hostname not in allowed_hosts:
            return_to = "https://www.chopaeng.com/auth/callback"
    except Exception:
        return_to = "https://www.chopaeng.com/auth/callback"

    if not getattr(Config, "DISCORD_LOGIN_ENABLED", True):
        return redirect(f"{return_to}?error=discord_login_disabled")

    if not Config.DISCORD_CLIENT_ID:
        return jsonify({"error": "Discord OAuth not configured (DISCORD_CLIENT_ID missing)"}), 503

    from api.sub_islands import make_oauth_state

    # Capture linking token if existing user is connecting their Discord
    auth_token = request.args.get("auth_token", "").strip()
    active_user_id = None
    if auth_token:
        try:
            curr_user = get_auth_user(auth_token)
            if curr_user:
                active_user_id = curr_user.get("user_id")
        except Exception:
            pass
        session["sub_link_token"] = auth_token

    state = make_oauth_state(return_to, active_user_id=active_user_id, link_token=auth_token)
    session["sub_oauth_state"] = state
    session["sub_return_to"] = return_to

    callback_url = getattr(Config, "DISCORD_REDIRECT_URI", "") or url_for("auth_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id":     Config.DISCORD_CLIENT_ID,
        "redirect_uri":  callback_url,
        "response_type": "code",
        # email: carries over data of the old email / YouTube / Twitch / Patreon accounts
        "scope":         "identify email guilds.members.read",
        "state":         state,
    })
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@app.route("/api/auth/callback")
def auth_callback():
    """Handle Discord OAuth callback: authenticate user, sync guild roles, and provision unified account."""
    from api.sub_islands import verify_oauth_state

    sess_return = session.get("sub_return_to", "https://www.chopaeng.com/auth/callback")
    try:
        error = request.args.get("error")
        if error:
            session.pop("sub_return_to", None)
            session.pop("sub_oauth_state", None)
            session.pop("sub_link_token", None)
            return redirect(f"{sess_return}?error={urllib.parse.quote(error)}")

        state = request.args.get("state", "")
        saved_state = session.pop("sub_oauth_state", None)
        sess_return = session.pop("sub_return_to", sess_return)
        sess_link_token = session.pop("sub_link_token", None)

        is_valid, return_to, state_uid, state_link_token = verify_oauth_state(
            state, fallback_return=sess_return, saved_state=saved_state
        )
        if not is_valid:
            session.pop("sub_return_to", None)
            return redirect(f"{return_to}?error=invalid_state")

        code = request.args.get("code", "")
        if not code:
            return redirect(f"{return_to}?error=missing_code")

        link_token = sess_link_token or state_link_token

        callback_url = getattr(Config, "DISCORD_REDIRECT_URI", "") or url_for("auth_callback", _external=True)
        try:
            token_body = urllib.parse.urlencode({
                "client_id":     Config.DISCORD_CLIENT_ID,
                "client_secret": Config.DISCORD_CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  callback_url,
            }).encode()
            resp = discord_request(
                "https://discord.com/api/oauth2/token",
                data=token_body,
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _DISCORD_UA},
                method="POST",
                timeout=10,
            )
            token_resp = json.loads(resp.body)
        except Exception as exc:
            logger.warning(f"Discord OAuth token exchange error: {exc}")
            return redirect(f"{return_to}?error=token_exchange_failed")

        access_token = token_resp.get("access_token")
        if not access_token:
            return redirect(f"{return_to}?error=no_access_token")

        # 1. Fetch user identity (@me) - works universally for any Discord account without intents
        discord_user_id = discord_username = discord_avatar_url = ""
        discord_global_name = discord_account_name = ""
        user_data = {}
        try:
            resp = discord_request(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": _DISCORD_UA},
                timeout=10,
            )
            user_data = json.loads(resp.body)
            discord_user_id = str(user_data.get("id", ""))
            discord_global_name = str(user_data.get("global_name") or "")
            discord_account_name = str(user_data.get("username") or "")
            discord_username = discord_global_name or discord_account_name or f"User-{discord_user_id[-4:]}"
            avatar_hash = user_data.get("avatar") or ""
            if discord_user_id and avatar_hash and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar_hash):
                discord_avatar_url = (
                    f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png?size=64"
                )
        except Exception as exc:
            logger.warning(f"Failed to fetch Discord user profile: {exc}")
            return redirect(f"{return_to}?error=profile_fetch_failed")

        if not discord_user_id:
            return redirect(f"{return_to}?error=profile_fetch_failed")

        # 2. Optionally fetch guild member record (roles + permissions) - non-blocking!
        member_roles: list[str] = []
        member_nickname = ""
        member_joined_at = ""
        member_perms = 0
        if Config.GUILD_ID:
            try:
                resp = discord_request(
                    f"https://discord.com/api/users/@me/guilds/{Config.GUILD_ID}/member",
                    headers={"Authorization": f"Bearer {access_token}", "User-Agent": _DISCORD_UA},
                    timeout=10,
                )
                member_data = json.loads(resp.body)
                member_roles = [str(r) for r in member_data.get("roles", [])]
                member_nickname = (member_data.get("nick") or "").strip()
                member_joined_at = str(member_data.get("joined_at") or "")
                if member_nickname:
                    discord_username = member_nickname
                try:
                    member_perms = int(member_data.get("permissions", "0") or 0)
                except (ValueError, TypeError):
                    member_perms = 0
            except Exception as exc:
                # OAuth guild scope may be missing; membership is enforced by the bot-based check below
                logger.debug(f"Guild member fetch note for {discord_user_id}: {exc}")

        # /api/auth/me revokes tokens of non-members, so reject them here with a clear error
        # instead of letting the site report a generic session-verification failure.
        if Config.GUILD_ID and getattr(Config, "DISCORD_LOGIN_ENABLED", True):
            try:
                from utils.discord_membership import fetch_guild_member_snapshot
                fetch_guild_member_snapshot(discord_user_id)
            except DiscordNotGuildMember:
                from utils.discord_membership import is_guild_banned
                if is_guild_banned(discord_user_id):
                    logger.info("Rejecting Discord login for %s: banned from guild", discord_user_id)
                    return redirect(f"{return_to}?error=banned")
                logger.info("Rejecting Discord login for %s: not in guild", discord_user_id)
                return redirect(f"{return_to}?error=not_a_member")
            except Exception as exc:
                logger.debug("Guild membership precheck skipped for %s: %s", discord_user_id, exc)

        try:
            from utils.discord_membership import _admin_role_ids
            admin_roles = _admin_role_ids()
        except Exception:
            admin_roles = set()

        is_admin = bool(member_perms & _ADMINISTRATOR_PERM) or bool(set(member_roles) & admin_roles)
        is_mod_user = _is_mod(member_roles) or is_admin

        # 3. JIT Provision or Link with Unified Accounts
        active_user_id = None
        if link_token:
            try:
                tok_payload = get_auth_user(link_token)
                if tok_payload:
                    active_user_id = tok_payload.get("user_id")
            except Exception:
                pass
        if not active_user_id and state_uid:
            active_user_id = state_uid

        from utils.unified_auth import provision_or_link_unified_account
        unified_user = None
        try:
            unified_user = provision_or_link_unified_account(
                provider="discord",
                provider_user_id=discord_user_id,
                username=discord_username,
                avatar=discord_avatar_url,
                # Only a verified email may match an existing account (it links that account to this Discord)
                email=user_data.get("email") if user_data.get("verified") else None,
                roles=member_roles,
                sub_tier=None,
                raw_profile=user_data,
                active_user_id=active_user_id,
            )
        except Exception as exc:
            logger.warning(f"Unified auth JIT provisioning error: {exc}")

        user_payload = {
            "user_id":   discord_user_id,
            "username":  discord_username,
            "discord_name": discord_global_name or discord_account_name,
            "global_name": discord_global_name,
            "account_name": discord_account_name,
            "nickname":  member_nickname,
            "joined_at": member_joined_at,
            "joined_timestamp": _iso_to_unix(member_joined_at),
            "avatar":    discord_avatar_url,
            "roles":     member_roles,
            "is_admin":  is_admin,
            "is_mod":    is_mod_user,
            "discord_checked_at": int(time.time()),
        }
        if unified_user and isinstance(unified_user, dict):
            for k, v in unified_user.items():
                if v is not None:
                    user_payload[k] = v
            user_payload["user_id"] = unified_user.get("user_id") or discord_user_id
            # Preserve Discord admin/mod privilege if granted by Discord or DB
            user_payload["is_admin"] = bool(is_admin or unified_user.get("is_admin"))
            user_payload["is_mod"] = bool(is_mod_user or unified_user.get("is_mod") or user_payload["is_admin"])

        token = make_auth_token(user_payload)

        login_event = {
            "user_id": discord_user_id,
            "username": discord_username,
            "discord_name": discord_global_name or discord_account_name,
            "global_name": discord_global_name,
            "account_name": discord_account_name,
            "nickname": member_nickname,
            "avatar": discord_avatar_url,
            "roles": member_roles,
            "role_count": len(member_roles),
            "is_admin": is_admin,
            "is_mod": is_mod_user,
            "ip_address": _client_ip(),
            "user_agent": request.headers.get("User-Agent", ""),
            "return_to": return_to,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        _record_website_login(login_event)

        logger.info("Website Discord login: user=%s is_mod=%s roles=%d", discord_username, is_mod_user, len(member_roles))
        return redirect(f"{return_to}?token={urllib.parse.quote(token)}")
    except Exception as exc:
        logger.exception("Discord OAuth callback unhandled exception: %s", exc)
        return redirect(f"{return_to}?error=auth_internal_error")


@app.route("/api/auth/me")
def auth_me():
    """Return the current authenticated user's info.

    Every page load calls this first, so it follows the normal refresh policy (Discord is re-checked
    once the cached roles are 5 minutes old) instead of calling Discord on every visit. Pass
    ?refresh=1 to re-read roles right now, as the "Sync now" / "Check again" buttons do.
    """
    force = request.args.get("refresh", "").strip().lower() in ("1", "true", "yes")
    user = _current_auth_user(force_refresh=force)
    if not user:
        return jsonify({"logged_in": False}), 200

    from api.sub_islands import get_user_sub_tier
    from utils.unified_auth import get_unified_account_by_id

    try:
        unified = get_unified_account_by_id(user.get("user_id"))
        if unified:
            has_discord = "discord" in (unified.get("connected_providers") or []) or bool(unified.get("discord_id"))
            keys_to_copy = ["connected_providers", "connected_accounts", "roles", "email", "is_subscriber", "sub_tier"]
            if not has_discord:
                keys_to_copy.extend(["provider", "username"])
            if user.get("discord_checked_at"):
                # Roles and tier were just read live from Discord; the unified account only
                # holds the roles saved at login, so copying them would undo the refresh.
                keys_to_copy = [k for k in keys_to_copy if k not in ("roles", "is_subscriber", "sub_tier")]
            for k in keys_to_copy:
                if unified.get(k) is not None:
                    user[k] = unified[k]
            if unified.get("is_admin"):
                user["is_admin"] = True
            if unified.get("is_mod"):
                user["is_mod"] = True
            if not has_discord and unified.get("avatar") and not user.get("avatar"):
                user["avatar"] = unified["avatar"]
            if has_discord:
                user["provider"] = "discord"
                if unified.get("discord_id"):
                    user["discord_id"] = unified["discord_id"]
                disc_acc = (unified.get("connected_accounts") or {}).get("discord") or {}
                if disc_acc.get("username"):
                    user["discord_name"] = disc_acc["username"]
    except Exception as ex:
        logger.debug("Unified enrich note in auth_me: %s", ex)

    # Always re-derive is_admin and is_mod live from current roles
    # in case the user was promoted after their last login.
    current_roles = [str(r) for r in (user.get("roles") or [])]
    try:
        from utils.discord_membership import _admin_role_ids
        admin_roles = _admin_role_ids()
    except Exception:
        admin_roles = set()
    live_is_admin = bool(user.get("is_admin")) or bool(set(current_roles) & admin_roles)
    live_is_mod = _is_mod(current_roles) or live_is_admin or bool(user.get("is_mod"))
    user["is_admin"] = live_is_admin
    user["is_mod"] = live_is_mod

    connected = list((user.get("connected_accounts") or {}).keys())
    provider = user.get("provider", "unified")
    if provider and provider != "unified" and provider not in connected:
        connected.append(provider)

    sub_rules_accepted = _user_has_sub_rules(user)
    order_rules_accepted = _user_has_order_rules(user)

    moderation_data = {
        "has_active_restriction": False,
        "active_restriction": None,
        "unacknowledged_warnings": [],
        "warning_count": 0,
    }
    try:
        from api.control_panel import get_user_moderation_notices
        moderation_data = get_user_moderation_notices(user)
    except Exception as mod_err:
        logger.debug("Failed to fetch user moderation notices: %s", mod_err)

    return jsonify({
        "logged_in":  True,
        "user_id":    user["user_id"],
        "username":   user["username"],
        "discord_name": user.get("discord_name", user["username"]),
        "nickname":   user.get("nickname", ""),
        "joined_at":  user.get("joined_at", ""),
        "avatar":     user.get("avatar", ""),
        "roles":      user.get("roles", []),
        "is_admin":   user.get("is_admin", False),
        "is_mod":     user.get("is_mod", False),
        "sub_tier":   get_user_sub_tier(user),
        "connected_providers": connected,
        "connected_accounts": user.get("connected_accounts", {}),
        "email":      user.get("email", ""),
        "provider":   user.get("provider", "discord"),
        "is_subscriber": bool(user.get("is_subscriber", False)),
        "sub_rules_accepted": sub_rules_accepted,
        "order_rules_accepted": order_rules_accepted,
        "moderation": moderation_data,
    })


@app.route("/api/moderation/acknowledge-notice", methods=["POST"])
def api_acknowledge_moderation_notice():
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(user)
    if not cand_ids:
        return jsonify({"error": "User identifier not found"}), 400

    data = request.get_json(silent=True) or {}
    warning_id = data.get("warning_id")
    now = int(time.time())

    from api.control_panel import init_control_panel_db
    init_control_panel_db()  # warnings.acknowledged_at / web_user_id

    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in cand_ids)
        # Same ownership test as get_user_moderation_notices: website-only accounts are
        # warned through web_user_id, so matching user_id alone left their notices unread forever.
        owner = f"(user_id IN ({placeholders}) OR web_user_id IN ({placeholders}))"
        if warning_id:
            conn.execute(
                f"UPDATE warnings SET acknowledged_at = ? WHERE id = ? AND {owner}",
                [now, warning_id] + cand_ids + cand_ids,
            )
        else:
            conn.execute(
                f"UPDATE warnings SET acknowledged_at = ? WHERE acknowledged_at IS NULL AND {owner}",
                [now] + cand_ids + cand_ids,
            )
        conn.commit()
        return jsonify({"success": True, "acknowledged_at": now})
    except Exception as exc:
        logger.error("[Moderation] Error acknowledging notice: %s", exc)
        return jsonify({"error": "Failed to acknowledge notice"}), 500
    finally:
        conn.close()


@app.route("/api/moderation/my-notices", methods=["GET"])
def api_get_my_moderation_notices():
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    try:
        from api.control_panel import get_user_moderation_notices
        return jsonify(get_user_moderation_notices(user))
    except Exception as exc:
        logger.error("[Moderation] Error fetching notices: %s", exc)
        return jsonify({"error": "Failed to fetch notices"}), 500


@app.route("/api/profile")
def api_profile():
    """Return the authenticated user's Discord profile and ChoPaeng activity.

    ?fields=user skips the per-island access scan and the visit-history queries, for pages that
    only show who the user is (the /me dashboard).
    """
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    user_only = request.args.get("fields", "").strip().lower() == "user"
    subscriptions = {} if user_only else _load_profile_subscriptions(user)
    visits = {} if user_only else _load_profile_visit_stats(user.get("user_id", ""))

    conn = get_db()
    favorite_islands = []
    try:
        fav_rows = conn.execute(
            "SELECT island_id FROM user_favorite_islands WHERE user_id = ? ORDER BY created_at DESC",
            (user.get("user_id", ""),)
        ).fetchall()
        favorite_islands = [r["island_id"] for r in fav_rows if r["island_id"]]
    except Exception:
        favorite_islands = []
    finally:
        conn.close()

    return jsonify({
        "user": {
            "id": user.get("user_id", ""),
            "discord_name": user.get("discord_name") or user.get("username", ""),
            "global_name": user.get("global_name", ""),
            "account_name": user.get("account_name", ""),
            "display_name": user.get("nickname") or user.get("discord_name") or user.get("username", ""),
            "nickname": user.get("nickname", ""),
            "avatar": user.get("avatar", ""),
            "joined_at": user.get("joined_at", ""),
            "joined_timestamp": user.get("joined_timestamp"),
            "is_admin": bool(user.get("is_admin")),
            "is_mod": bool(user.get("is_mod")),
        },
        "subscriptions": subscriptions,
        "visits": visits,
        "favorite_islands": favorite_islands,
    })


@app.route("/api/subscriptions", methods=["GET", "POST", "DELETE"])
def api_subscriptions():
    """Manage authenticated user island/item/villager/slot notifications."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    user_id = str(user.get("user_id") or "")
    db = get_db()
    try:
        if request.method == "GET":
            rows = db.execute(
                "SELECT island_clean, kind, has_island_access FROM island_subscriptions WHERE user_id = ? ORDER BY kind, island_clean",
                (user_id,),
            ).fetchall()
            return jsonify({"ok": True, "items": [dict(row) for row in rows]})

        data = request.get_json(silent=True) or {}
        target = clean_text(data.get("target") or data.get("island") or data.get("item") or data.get("villager") or "")
        kind = (data.get("kind") or "island_online").strip().lower()
        allowed = {"island_online", "island_slot", "item", "villager", "sub"}
        if kind not in allowed or not target:
            return jsonify({"ok": False, "error": f"kind must be one of {sorted(allowed)} and target is required"}), 400
        if request.method == "POST":
            db.execute(
                "INSERT OR IGNORE INTO island_subscriptions (user_id, island_clean, kind, has_island_access) VALUES (?, ?, ?, ?)",
                (user_id, target, kind, 1 if bool(user.get("is_mod") or user.get("is_admin")) else 0),
            )
        else:
            db.execute(
                "DELETE FROM island_subscriptions WHERE user_id = ? AND island_clean = ? AND kind = ?",
                (user_id, target, kind),
            )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        db.close()

    _record_api_audit_event(
        "subscription_update",
        target,
        {"kind": kind, "method": request.method},
    )
    return jsonify({"ok": True, "target": target, "kind": kind, "method": request.method})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Invalidate the current auth token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        revoke_auth_token(token)
    return jsonify({"logged_out": True})


@app.route("/api/suggestions", methods=["POST"])
def api_suggestions():
    """Submit a resident suggestion/feedback via secure server-side Discord webhook."""
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    description = str(data.get("description") or "").strip()
    if not title or not description:
        return jsonify({"success": False, "error": "Both title and description are required."}), 400

    if len(title) > 250:
        title = title[:250]
    if len(description) > 2000:
        description = description[:2000]

    # Optional authenticated user
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or "") if auth_user else ""
    avatar_url = auth_user.get("avatar") or "" if auth_user else ""

    discord_username = str(
        data.get("discordUsername") or data.get("discord_username") or (auth_user.get("username") if auth_user else "") or ""
    ).strip()
    in_game_name = str(data.get("inGameName") or data.get("in_game_name") or "").strip()
    island_name = str(data.get("islandName") or data.get("island_name") or "").strip()
    page_url = str(data.get("pageUrl") or data.get("page_url") or "").strip()

    # Rate limiting: 15 seconds cooldown per IP / user
    ip = _client_ip()
    cache_key = f"sugg:{user_id or ip}"
    now = time.monotonic()
    last_fired = _SUGGESTIONS_WEBHOOK_DEBOUNCE.get(cache_key)
    if last_fired and (now - last_fired) < _SUGGESTIONS_WEBHOOK_DEBOUNCE_TTL:
        remaining = int(_SUGGESTIONS_WEBHOOK_DEBOUNCE_TTL - (now - last_fired)) + 1
        return jsonify({
            "success": False,
            "error": f"Please wait {remaining}s before submitting another suggestion.",
            "cooldownSeconds": remaining,
        }), 429
    _prune_debounce(_SUGGESTIONS_WEBHOOK_DEBOUNCE, _SUGGESTIONS_WEBHOOK_DEBOUNCE_TTL, now)
    _SUGGESTIONS_WEBHOOK_DEBOUNCE[cache_key] = now

    threading.Thread(
        target=_fire_suggestions_webhook,
        args=(title, description, discord_username, in_game_name, island_name, page_url, user_id, avatar_url),
        daemon=True,
    ).start()

    _record_api_audit_event(
        "suggestion_submitted",
        user_id or "anonymous",
        {"title": title[:50], "ip": ip},
    )

    return jsonify({"success": True, "message": "Suggestion submitted successfully."}), 200


# ============================================================================
# DODO REVEAL â€” authenticated, fires webhook
# ============================================================================

@app.route("/api/islands/<name>/dodo", methods=["POST"])
def reveal_dodo(name):
    """Return the dodo code for an island if the user has the required role.

    The client must send:   Authorization: Bearer <token>
    On success, fires a Discord webhook and returns the dodo code.
    """
    user = _current_auth_user(force_refresh=True)
    if not user:
        _log_dodo_reveal_attempt(None, name.upper(), "denied", "not_logged_in")
        return jsonify({"error": "Authentication required"}), 401

    # Fail closed: a forced refresh that couldn't reach Discord returns the cached session
    # (up to the stale grace window), whose roles may include a subscription that has since
    # ended. Only reveal against roles confirmed with Discord moments ago.
    if getattr(Config, "DISCORD_LOGIN_ENABLED", True) and not _secrets_bot_user(user):
        checked_at = int(user.get("discord_checked_at") or 0)
        if time.time() - checked_at > DODO_REVEAL_ROLE_MAX_AGE_SECONDS:
            _log_dodo_reveal_attempt(user, name.upper(), "denied", "roles_unverified", checked_at=checked_at)
            _record_api_audit_event("dodo_reveal_denied", name.upper(), {"reason": "roles_unverified"})
            return jsonify({
                "error": "We couldn't confirm your Discord roles right now. Please try again in a moment.",
                "code": "roles_unverified",
            }), 503

    maintenance = get_maintenance_settings()
    island_maintenance = (maintenance.get("islands") or {}).get(clean_text(name), {})
    if maintenance["maintenance_mode"] or maintenance["disable_dodo_reveals"] or island_maintenance.get("disable_dodo_reveals"):
        _log_dodo_reveal_attempt(user, name.upper(), "denied", "maintenance_mode")
        _record_api_audit_event("dodo_reveal_denied", name.upper(), {"reason": "maintenance_mode"})
        return jsonify({
            "error": island_maintenance.get("message") or maintenance["message"] or "Dodo reveals are temporarily unavailable.",
            "code": "maintenance_mode",
        }), 503

    target = name.upper()

    # Load island metadata (cat + required_roles)
    db = get_db()
    try:
        row = db.execute(
            "SELECT cat, type, required_roles, channel_id, is_visible FROM islands WHERE UPPER(name) = ?", (target,)
        ).fetchone()
    finally:
        db.close()

    island_cat = ""
    island_type = ""
    required_roles: list[str] = []
    channel_id = None
    if row:
        if row["is_visible"] is not None and not bool(row["is_visible"]):
            _log_dodo_reveal_attempt(user, target, "denied", "island_hidden")
            _record_api_audit_event("dodo_reveal_denied", target, {"reason": "island_hidden"})
            return jsonify({"error": "Island is not available"}), 404
        island_cat = (row["cat"] or "").strip().lower()
        island_type = row["type"] or ""
        channel_id = row["channel_id"]
        try:
            required_roles = json.loads(row["required_roles"] or "[]")
        except (ValueError, TypeError):
            required_roles = []
    elif target in {str(n).strip().upper() for n in Config.SUB_ISLANDS}:
        # No DB row: fall back to the configured sub-island list, not the SysBot folders.
        island_cat = "member"
        island_type = "VIP"

    # Safety: member islands must never become public because required_roles is empty.
    effective_required_roles, resolved_channel_id, _access_source = _resolved_island_required_roles(
        target,
        island_cat,
        required_roles,
        island_type,
        channel_id,
    )
    channel_id = resolved_channel_id or channel_id

    is_viewer_admin = bool(user.get("is_admin"))
    is_viewer_mod = bool(user.get("is_mod")) or is_viewer_admin

    # Check for active moderation suspension (Ban or Timeout from Control Panel)
    try:
        from api.control_panel import check_user_active_restriction
        active_rest = check_user_active_restriction(
            user.get("user_id"),
            user.get("discord_id") or user.get("id"),
        )
        if active_rest:
            _log_dodo_reveal_attempt(
                user,
                target,
                "denied",
                "moderation_restricted",
                action_type=active_rest["action_type"],
                restriction_reason=active_rest.get("reason"),
                channel_id=channel_id,
            )
            _record_api_audit_event(
                "dodo_reveal_denied",
                target,
                {
                    "reason": "moderation_restricted",
                    "action_type": active_rest.get("action_type"),
                    "restriction_reason": active_rest.get("reason"),
                    "channel_id": channel_id,
                },
            )
            return jsonify({
                "error": f"Your island access has been suspended ({active_rest['action_type'].capitalize()}). Reason: {active_rest['reason']}",
                "code": "access_suspended",
                "action_type": active_rest["action_type"],
                "reason": active_rest["reason"],
                "expires_at": active_rest["expires_at"],
            }), 403
    except Exception as rest_err:
        logger.warning("[DodoReveal] Moderation check error: %s", rest_err)

    # Enforce one-time Sub Rules acceptance for member islands
    if _is_member_island(island_cat, island_type, target) and not is_viewer_admin:
        if not _user_has_sub_rules(user):
            _log_dodo_reveal_attempt(user, target, "denied", "missing_sub_rules_acceptance", channel_id=channel_id)
            _record_api_audit_event("dodo_reveal_denied", target, {"reason": "missing_sub_rules_acceptance", "channel_id": channel_id})
            return jsonify({
                "error": "You must review and accept the Subscriber Island Rules (:c1_: to :c8_:) first before revealing Dodo codes.",
                "code": "missing_sub_rules_acceptance",
                "requires_sub_rules": True,
            }), 403

    # Check that user has saved at least one In-Game Character (IGN & Island Name)
    from utils.unified_auth import resolve_all_user_ids
    cand_user_ids = resolve_all_user_ids(user)

    saved_char = None
    if cand_user_ids:
        char_db = get_db()
        try:
            placeholders = ",".join("?" for _ in cand_user_ids)
            saved_char = char_db.execute(
                f"""
                SELECT ign, island_name FROM user_saved_characters
                WHERE user_id IN ({placeholders}) AND TRIM(ign) != ''
                ORDER BY is_default DESC, updated_at DESC LIMIT 1
                """,
                cand_user_ids,
            ).fetchone()
        finally:
            char_db.close()

    if not saved_char or not (saved_char["ign"] or "").strip():
        _log_dodo_reveal_attempt(user, target, "denied", "missing_saved_character", channel_id=channel_id)
        _record_api_audit_event("dodo_reveal_denied", target, {"reason": "missing_saved_character", "channel_id": channel_id})
        return jsonify({
            "error": "You must save your In-Game Character (IGN & Island Name) first before you can reveal Dodo codes. Please add your character in your <a href=\"/profile\">Profile Settings</a>.",
            "code": "missing_saved_character",
            "requires_character": True,
        }), 403

    if _is_member_island(island_cat, island_type, target) and not effective_required_roles and not is_viewer_mod:
        _log_dodo_reveal_attempt(user, target, "denied", "no_member_roles_configured", channel_id=channel_id)
        _record_api_audit_event("dodo_reveal_denied", target, {"reason": "no_member_roles_configured", "channel_id": channel_id})
        return jsonify({"error": "Subscriber roles are not configured for this island"}), 403

    # Check for general island access role
    island_access_role = str(Config.ISLAND_ACCESS_ROLE) if Config.ISLAND_ACCESS_ROLE else ""
    is_discord_auth = bool(user.get("provider") == "discord" or "discord" in user.get("connected_providers", []))
    user_roles_set = set(str(r) for r in user.get("roles", []))
    if island_access_role and not is_viewer_admin and getattr(Config, "DISCORD_LOGIN_ENABLED", True) and is_discord_auth:
        if island_access_role not in user_roles_set and not _user_has_sub_rules(user):
            _log_dodo_reveal_attempt(
                user,
                target,
                "denied",
                "missing_global_island_access_role",
                required_role=island_access_role,
                channel_id=channel_id,
            )
            _record_api_audit_event(
                "dodo_reveal_denied",
                target,
                {"reason": "missing_global_island_access_role", "channel_id": channel_id},
            )
            return jsonify({
                "error": "You need the Discord island access role to reveal this Dodo code. Please accept the rules in the sub-rules channel first: <a href=\"https://discord.com/channels/729590421478703135/783677194576330792\" target=\"_blank\">#sub-rules</a>.",
                "code": "missing_global_island_access_role",
                "requires_sub_rules": True,
            }), 403

    if not _has_island_access(user.get("roles", []), effective_required_roles, is_viewer_mod, island_name=target, user=user):
        _log_dodo_reveal_attempt(
            user,
            target,
            "denied",
            "missing_island_channel_role",
            required_roles=effective_required_roles,
            channel_id=channel_id,
        )
        _record_api_audit_event(
            "dodo_reveal_denied",
            target,
            {"reason": "missing_island_channel_role", "channel_id": channel_id},
        )
        return jsonify({
            "error": "You do not have the required membership tier or role for this island.",
            "code": "missing_island_channel_role",
        }), 403

    # Find the dodo code from SysBot HTTP API telemetry, direct SysBot query, or filesystem
    dodo_code = None
    try:
        from utils.sysbot_telemetry import _TELEMETRY_CACHE, _CACHE_LOCK
        with _CACHE_LOCK:
            t_data = _TELEMETRY_CACHE.get(target.lower())
            if t_data and t_data.get("raw_dodo"):
                cand = str(t_data["raw_dodo"]).strip()
                if cand not in ["00000", "-----", "", "GETTIN'", "None", "null"]:
                    dodo_code = cand
    except Exception:
        pass

    if not dodo_code:
        try:
            from utils.sysbot_api import get_island_sysbot_client
            client = get_island_sysbot_client(target, timeout=2)
            status_data = client.get_bot_status_sync()
            cand = str(status_data.get("dodo_code") or "").strip()
            if cand not in ["00000", "-----", "", "GETTIN'", "None", "null"]:
                dodo_code = cand
        except Exception:
            pass


    if not dodo_code:
        _log_dodo_reveal_attempt(user, target, "failed", "dodo_unavailable", channel_id=channel_id)
        _record_api_audit_event("dodo_reveal_failed", target, {"reason": "dodo_unavailable", "channel_id": channel_id})
        return jsonify({"error": "Dodo code not available right now"}), 404

    # Save the reveal before the webhook so flight matching never depends on Discord.
    # Discord members are stored by Discord ID (the logger resolves them as members);
    # website-only accounts by their website ID.
    discord_id = str(user.get("discord_id") or (user.get("connected_accounts") or {}).get("discord", {}).get("id") or "")
    reveal_row_id = _persist_dodo_reveal_message(
        user_id=discord_id if discord_id.isdigit() else str(user.get("user_id") or ""),
        island_name=target,
        channel_id=channel_id,
        message_url="",
        username=user.get("username") or "",
        nickname=user.get("nickname") or "",
    )

    # Fire webhook in background thread so the response isn't delayed
    threading.Thread(
        target=_fire_dodo_webhook,
        args=(
            user.get("username", ""),
            user.get("nickname", ""),
            user.get("user_id", ""),
            user.get("avatar", ""),
            target,
            dodo_code,
            channel_id,
            saved_char["ign"] if saved_char else "",
            saved_char["island_name"] if saved_char else "",
            discord_id,
            reveal_row_id,
        ),
        daemon=True,
    ).start()

    warning = nickname_warning_for(user.get("nickname"))
    response_payload = {"island": target, "dodo_code": dodo_code}
    if warning:
        response_payload["warning"] = warning

    _log_dodo_reveal_attempt(user, target, "allowed", "revealed", channel_id=channel_id)
    _record_api_audit_event("dodo_reveal_allowed", target, {"channel_id": channel_id})
    return jsonify(response_payload)


@app.route("/api/islands/<name>/queue", methods=["POST"])
def join_dodo_queue(name):
    """Join the Dodo reservation queue for an island."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    # Check for active moderation suspension
    try:
        from api.control_panel import check_user_active_restriction
        active_rest = check_user_active_restriction(
            user.get("user_id"),
            user.get("discord_id") or user.get("id"),
        )
        if active_rest:
            return jsonify({
                "ok": False,
                "error": f"Your account is currently suspended ({active_rest['action_type']}). Reason: {active_rest.get('reason') or 'Policy violation'}.",
                "code": "access_suspended",
            }), 403
    except Exception as rest_err:
        logger.warning("[DodoQueue] Moderation check error: %s", rest_err)

    target = name.upper().strip()
    island_clean = clean_text(target)
    maintenance = get_maintenance_settings()
    island_maintenance = (maintenance.get("islands") or {}).get(island_clean, {})
    if maintenance["maintenance_mode"] or island_maintenance.get("queue_paused"):
        return jsonify({
            "ok": False,
            "error": island_maintenance.get("message") or maintenance["message"] or "This island queue is temporarily paused.",
            "code": "maintenance_mode",
        }), 503
    note = (request.get_json(silent=True) or {}).get("note", "")
    now = int(time.time())
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id, status FROM dodo_queue WHERE island_clean = ? AND user_id = ? AND status IN ('waiting', 'called', 'investigating')",
            (island_clean, str(user.get("user_id") or "")),
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "id": existing["id"], "status": existing["status"], "already_queued": True})
        cur = db.execute(
            """INSERT INTO dodo_queue
               (island_clean, island_name, user_id, username, status, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'waiting', ?, ?, ?)""",
            (
                island_clean,
                target,
                str(user.get("user_id") or ""),
                user.get("username") or user.get("discord_name") or "",
                str(note or "")[:500],
                now,
                now,
            ),
        )
        db.commit()
        entry_id = cur.lastrowid
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        db.close()

    _record_api_audit_event("dodo_queue_join", target, {"entry_id": entry_id})
    return jsonify({"ok": True, "id": entry_id, "status": "waiting"})


@app.route("/api/dodo-queue/me", methods=["GET"])
def my_dodo_queue():
    """Return the authenticated user's active Dodo queue entries."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, island_name, status, note, created_at, updated_at FROM dodo_queue "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 25",
            (str(user.get("user_id") or ""),),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        db.close()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
@app.route('/api')
def home():
    """Minimal API root. Endpoint listings are intentionally not published."""
    return jsonify({"name": "ChoBot API", "status": "online"})

@app.route('/health')
@app.route('/api/health')
def health():
    """Health check endpoint for monitoring"""
    payload = build_health_payload(
        data_manager=data_manager,
        fallback_loader=_get_item_cache,
        include_private=False,
    )
    payload["islands"] = {
        "file_cache_ttl_seconds": _FILE_CACHE_TTL,
    }
    status_code = 200 if payload["status"] in {"ok", "degraded"} else 503
    return jsonify(payload), status_code


@app.route('/api/public/ad-config', methods=['GET'])
def public_ad_config():
    """Which ad network chopaeng.com should load: adsense, nitropay or none (settings.ad_provider)."""
    resp = jsonify(get_ad_config())
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp

# --- ITEM SEARCH ROUTES ---

@app.route('/find')
def find_item():
    """Text response for item search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "item", "name")

    if not query:
        return f"Hey {user}, type !find <item name> to search."

    cache, _last_update, _refresh_interval, _source = _get_item_cache()
    if not cache:
        return f"Hey {user}, the search service is not available right now. Please try again later."

    found_locs = cache.get(query)

    if found_locs:
        final_msg = format_locations_text(found_locs)
        return f"Hey {user}, I found {query.upper()} {final_msg}"

    matches = process.extract(query, list(cache.keys()), limit=5, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        suggestions_str = ", ".join(valid_suggestions)
        return f"Hey {user}, I couldn't find \"{query}\" - Did you mean: {suggestions_str}? If not, try !orderbot."

    return f"Hey {user}, I couldn't find \"{query}\" or anything similar. Please check spelling."


@app.route('/api/find')
def api_find_item():
    """JSON response for item search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "item", "name")

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !find <item name> to search."})

    original_query = query
    query, alias_used = _resolve_search_alias("item", query)
    cache, _last_update, _refresh_interval, source = _get_item_cache()
    if not cache:
        _log_command_search("find", original_query, found=False, source=source)
        return jsonify({"error": "Service unavailable - item cache is not loaded"}), 503

    found_locs = cache.get(query)

    if found_locs:
        free, sub, order = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        _log_command_search("find", original_query, found=True, result_count=len(free) + len(sub) + len(order), source=source)
        return jsonify({
            "found": True,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "results": {"free": free, "sub": sub, "order": order},
            "suggestions": [],
            "message": f"Hey {user}, I found {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(cache.keys()), limit=5, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        _log_command_search("find", original_query, found=False, source=source)
        return jsonify({
            "found": False,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    _log_command_search("find", original_query, found=False, source=source)
    return jsonify({
        "found": False,
        "query": original_query,
        "resolved_query": query,
        "alias_used": alias_used,
        "source": source,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find \"{query}\" or anything similar."
    })


_FIND_BATCH_MAX_NAMES = 300


def _batch_lookup_locations(names, lookup: dict) -> dict:
    """Map each requested name to its island lists; names with no stock are omitted."""
    found = {}
    if not isinstance(names, list) or not lookup:
        return found
    for raw in names[:_FIND_BATCH_MAX_NAMES]:
        if not isinstance(raw, str) or raw in found:
            continue
        locations = lookup.get(normalize_text(raw))
        if not locations:
            continue
        free, sub, order = parse_locations_json(locations)
        if free or sub or order:
            found[raw] = {"free": free, "sub": sub, "order": order}
    return found


@app.route('/api/find/batch', methods=['POST'])
def api_find_batch():
    """Exact-name stock lookup for many items/villagers at once (wishlist matching).

    Unlike /api/find this does no fuzzy matching and is not recorded as a search
    event, so a whole wishlist can be checked without skewing search analytics.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Expected a JSON object"}), 400

    item_names = data.get("items") or []
    villager_names = data.get("villagers") or []

    item_cache, _last_update, _refresh_interval, source = _get_item_cache() if item_names else ({}, None, None, "")
    villager_map = _get_villager_map()[0] if villager_names else {}

    if item_names and not item_cache:
        return jsonify({"ok": False, "error": "Service unavailable - item cache is not loaded"}), 503

    return jsonify({
        "ok": True,
        "source": source,
        "items": _batch_lookup_locations(item_names, item_cache),
        "villagers": _batch_lookup_locations(villager_names, villager_map),
    })

# --- VILLAGER SEARCH ROUTES ---

@app.route('/villager')
def find_villager():
    """Text response for villager search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "villager", "name")

    if not query:
        return f"Hey {user}, type !villager <n> to search."

    villager_map, _source = _get_villager_map()
    if not villager_map:
        return f"Hey {user}, the search service is not available right now. Please try again later."

    found_locs = villager_map.get(query)

    if found_locs:
        final_msg = format_locations_text(found_locs)
        return f"Hey {user}, I found villager {query.upper()} {final_msg}"

    matches = process.extract(query, list(villager_map.keys()), limit=3, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        suggestions_str = ", ".join(valid_suggestions)
        return f"Hey {user}, I couldn't find villager \"{query}\" - Did you mean: {suggestions_str}?"

    return f"Hey {user}, I couldn't find a villager named \"{query}\"."


@app.route('/api/villager')
def api_find_villager():
    """JSON response for villager search"""
    user = request.args.get('user', 'User')
    query = _request_search_query("q", "villager", "name")

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !villager <n> to search."})

    original_query = query
    query, alias_used = _resolve_search_alias("villager", query)
    villager_map, source = _get_villager_map()
    if not villager_map:
        _log_command_search("villager", original_query, found=False, source=source)
        return jsonify({"error": "Service unavailable - villager cache is not loaded"}), 503

    found_locs = villager_map.get(query)

    if found_locs:
        free, sub, order = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        _log_command_search("villager", original_query, found=True, result_count=len(free) + len(sub) + len(order), source=source)
        return jsonify({
            "found": True,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "results": {"free": free, "sub": sub, "order": order},
            "suggestions": [],
            "message": f"Hey {user}, I found villager {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(villager_map.keys()), limit=3, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        _log_command_search("villager", original_query, found=False, source=source)
        return jsonify({
            "found": False,
            "query": original_query,
            "resolved_query": query,
            "alias_used": alias_used,
            "source": source,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find villager \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    _log_command_search("villager", original_query, found=False, source=source)
    return jsonify({
        "found": False,
        "query": original_query,
        "resolved_query": query,
        "alias_used": alias_used,
        "source": source,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find a villager named \"{query}\"."
    })

@app.route('/api/villagers/list')
def api_list_villagers_by_island():
    """List all villagers grouped by island using SysBot API."""
    manifest, source = _get_sysbot_villagers_manifest()
    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "total_islands": len(manifest),
        "islands": manifest,
    })


@app.route('/api/islands/<name>/villagers', methods=['GET'])
def api_list_island_villagers(name):
    """List villagers residing on a specific island via SysBot API."""
    target = name.strip().title()
    manifest, source = _get_sysbot_villagers_manifest()
    villagers = manifest.get(target)
    if villagers is None:
        villagers = _fetch_island_villagers_from_sysbot(target)
    return jsonify({
        "island": target,
        "villagers": villagers or [],
        "count": len(villagers or []),
        "source": "sysbot_api",
        "timestamp": datetime.now().isoformat(),
    })


@app.route('/api/search/similar')
def api_search_similar():
    """Return similar item or villager names for typo-learning/search UI."""
    kind = (request.args.get("kind") or "item").strip().lower()
    query = _request_search_query("q", "query", "name")
    limit = min(max(request.args.get("limit", 8, type=int), 1), 25)
    if kind not in {"item", "villager"}:
        return jsonify({"error": "kind must be item or villager"}), 400
    if not query:
        return jsonify({"error": "query is required"}), 400

    if kind == "villager":
        data, source = _get_villager_map()
        choices = list(data.keys())
    else:
        cache, _last_update, _refresh_interval, source = _get_item_cache()
        display_map = cache.get("_display", {})
        choices = [key for key in cache if key != "_display"]

    matches = process.extract(query, choices, limit=limit, scorer=fuzz.WRatio)
    suggestions = []
    for key, score in matches:
        label = key.title() if kind == "villager" else display_map.get(key, key.title())
        suggestions.append({"key": key, "label": label, "score": score})
    return jsonify({"kind": kind, "query": query, "source": source, "suggestions": suggestions})

# --- DODO CODE / ISLAND STATUS ROUTES ---

@app.route('/api/islands', methods=['GET'])
def get_islands():
    """Get all island statuses and Dodo codes with full metadata via SysBot API."""
    viewer = _current_auth_user()
    viewer_roles = viewer.get("roles", []) if viewer else []
    viewer_is_admin = bool(viewer and viewer.get("is_admin"))
    viewer_is_mod = bool(viewer and (viewer.get("is_mod") or viewer_is_admin or _is_mod(viewer_roles)))

    # Ensure background telemetry worker is running across all 48 ports
    try:
        from utils.sysbot_telemetry import start_telemetry_worker, _is_sub_island
        start_telemetry_worker()
    except Exception as exc:
        logger.warning(f"Failed to start SysBot telemetry worker: {exc}")

    # Load island metadata from DB, keyed by uppercase name
    db_map = {}
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, display_name, is_visible, cat, description, items, map_url, seasonal, theme, type, updated_at, required_roles, channel_id "
            "FROM islands ORDER BY name"
        ).fetchall()
        for row in rows:
            isl = row_to_island_dict(dict(row))
            isl["required_roles"], resolved_channel_id, access_source = _resolved_island_required_roles(
                isl.get("name"),
                isl.get("cat"),
                isl.get("required_roles") or [],
                isl.get("type"),
                isl.get("channel_id"),
            )
            isl["channel_id"] = resolved_channel_id
            isl["access_source"] = access_source
            if isl.get("name"):
                db_map[isl["name"].upper()] = isl
    except Exception:
        logger.exception("Failed to load island metadata from DB for /api/islands")
    finally:
        db.close()

    # Map of all known islands: name_upper -> dict(name, display_name, type, cat, path, port_index)
    all_islands: dict[str, dict[str, Any]] = {}

    # 1. Configured 48 ports (canonical port order 8201..8248)
    port_order = getattr(Config, "ALL_ISLAND_PORT_ORDER", [])
    order_bot_name = (getattr(Config, "ORDER_BOT_ISLAND", "") or "Sinta").strip().upper()

    for idx, raw_name in enumerate(port_order):
        name = raw_name.strip().upper()
        if not name:
            continue
        try:
            from utils.sysbot_telemetry import _is_sub_island
            is_sub = _is_sub_island(name)
        except Exception:
            is_sub = False
        is_ord = (name == order_bot_name)
        isl_type = "Order" if is_ord else ("VIP" if is_sub else "Free")
        isl_cat = "order" if is_ord else ("member" if is_sub else "public")
        all_islands[name] = {
            "name": name,
            "display_name": raw_name.strip(),
            "type": isl_type,
            "cat": isl_cat,
            "path": None,
            "port_index": idx,
        }

    # Islands come only from the port list above and the DB below; the SysBot
    # folders (DIR_VIP / DIR_FREE / DIR_ORDER) are not scanned.


    # 3. Incorporate any additional DB islands
    for name, db_meta in db_map.items():
        if name not in all_islands:
            isl_cat = db_meta.get("cat") or "public"
            isl_type = db_meta.get("type") or ("VIP" if isl_cat == "member" else "Free")
            all_islands[name] = {
                "name": name,
                "display_name": db_meta.get("display_name") or name.title(),
                "type": isl_type,
                "cat": isl_cat,
                "path": None,
                "port_index": 999,
            }

    results = []
    for name, info in all_islands.items():
        db_meta = db_map.get(name, {})
        if db_meta.get("is_visible") is False:
            continue

        merged_meta = {
            "id": name.lower(),
            "name": name,
            "display_name": info["display_name"],
            "cat": info["cat"],
            "type": info["type"],
            **db_meta,
        }

        entry_obj = SimpleNamespace(name=info["display_name"], path=info["path"]) if info["path"] else SimpleNamespace(name=info["display_name"], path="")

        resp = _build_island_response(
            entry_obj,
            info["type"],
            merged_meta,
            discord_bot_online=None,
            viewer_roles=viewer_roles,
            viewer_is_mod=viewer_is_mod,
            entry_path=info["path"],
        )
        results.append(resp)

    # Sort stably: port order first (if in 48 configured ports), then alphabetically
    results.sort(key=lambda x: (all_islands.get(x['canonical_name'], {}).get('port_index', 999), x['name']))

    resp = jsonify({
        "meta": {
            "timestamp": datetime.now().isoformat(),
            "source": "sysbot_api",
            "total_islands": len(results),
            "cache_ttl_seconds": _FILE_CACHE_TTL,
        },
        "data": results,
    })
    resp.headers["Cache-Control"] = _islands_cache_control()
    return resp


# Seconds Cloudflare may serve a signed-out /api/islands response from its edge. The origin's
# uplink is slow and lossy (a 9 KB response can take 10+ s), so most visitors should never reach it.
# The zone's "Islands list (signed-out)" Cache Rule only matches requests without Authorization /
# X-API-Key and keys the cache on Origin (CORS echoes it back), so signed-in viewers, who can see
# sub-island access and mod fields, always get a fresh per-user response.
ISLANDS_EDGE_CACHE_SECONDS = 5
ISLANDS_EDGE_STALE_SECONDS = 30


def _islands_cache_control() -> str:
    if request.headers.get("Authorization") or request.headers.get("X-API-Key"):
        return "private, no-store"
    return (
        f"public, max-age=0, s-maxage={ISLANDS_EDGE_CACHE_SECONDS}, "
        f"stale-while-revalidate={ISLANDS_EDGE_STALE_SECONDS}"
    )


@app.route('/api/browser/islands', methods=['GET'])
def api_browser_islands():
    """Frontend-friendly public island cards without Dodo codes."""
    response = get_islands()
    payload = response.get_json() if hasattr(response, "get_json") else {}
    cards = []
    category = (request.args.get("cat") or "").strip().lower()
    seasonal = (request.args.get("seasonal") or "").strip().lower()
    for island in payload.get("data", []):
        if category and str(island.get("cat") or "").lower() != category:
            continue
        if seasonal and seasonal not in str(island.get("seasonal") or "").lower():
            continue
        cards.append({
            "id": island.get("id"),
            "name": island.get("display_name") or island.get("name"),
            "canonical_name": island.get("name"),
            "cat": island.get("cat"),
            "type": island.get("type"),
            "status": island.get("status"),
            "visitors": island.get("visitors"),
            "visitor_list": island.get("visitor_list", []),
            "map_url": island.get("map_url"),
            "items": island.get("items") or [],
            "seasonal": island.get("seasonal"),
            "theme": island.get("theme"),
            "required_role_count": len(island.get("required_roles") or []),
            "accessible": bool(island.get("accessible")),
            "bot_online": island.get("bot_online"),
            "discord_bot_online": island.get("bot_online"),
            "source": "sysbot_api",
            "console_stage": island.get("console_stage"),
            "updated_at": island.get("updated_at"),
            "game_time": island.get("game_time"),
            "is_nooks_open": island.get("is_nooks_open"),
            "nooks_status": island.get("nooks_status"),
            "nooks_message": island.get("nooks_message"),
            "mode": island.get("mode"),
            "gametime": island.get("gametime"),
        })
    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "source": "sysbot_api",
        "count": len(cards),
        "items": cards,
    })


@app.route('/api/islands/access', methods=['GET'])
def get_island_access():
    """Return the current user's per-island access state without Dodo/status payloads."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    subscriptions = _load_profile_subscriptions(user)
    accessible = subscriptions.get("accessible_member_islands", [])
    accessible_ids = {str(item.get("id") or "").lower() for item in accessible}
    accessible_names = {str(item.get("name") or "").upper() for item in accessible}
    role_names = _get_guild_role_names()

    rows = []
    db = get_db()
    try:
        db_rows = db.execute(
            "SELECT id, name, display_name, is_visible, cat, type, required_roles, channel_id FROM islands ORDER BY name"
        ).fetchall()
        for row in db_rows:
            island = row_to_island_dict(dict(row))
            if island.get("is_visible") is False:
                continue
            required_roles, resolved_channel_id, access_source = _resolved_island_required_roles(
                island.get("name"),
                island.get("cat"),
                island.get("required_roles", []),
                island.get("type"),
                island.get("channel_id"),
            )
            user_roles = island_access.normalize_roles(user.get("roles", []))
            if user.get("sub_tier"):
                user_roles.add(str(user.get("sub_tier")))
            matched = sorted(user_roles & set(required_roles))
            accessible_flag = (
                str(island.get("id") or "").lower() in accessible_ids
                or str(island.get("name") or "").upper() in accessible_names
                or _has_island_access(
                    user.get("roles", []),
                    required_roles,
                    bool(user.get("is_mod") or user.get("is_admin")),
                    island_name=island.get("name"),
                    user=user,
                )
            )
            rows.append({
                "id": island.get("id"),
                "name": island.get("display_name") or island.get("name"),
                "canonical_name": island.get("name"),
                "cat": island.get("cat"),
                "type": island.get("type"),
                "channel_id": resolved_channel_id,
                "access_source": access_source,
                "accessible": accessible_flag,
                "required_roles": [_role_payload(role_id, role_names) for role_id in required_roles],
                "matched_roles": [_role_payload(role_id, role_names) for role_id in matched],
            })
    finally:
        db.close()

    _record_api_audit_event(
        "access_check",
        "islands",
        {"accessible_count": sum(1 for item in rows if item["accessible"]), "island_count": len(rows)},
    )
    return jsonify({
        "user_id": user.get("user_id"),
        "is_mod": bool(user.get("is_mod")),
        "is_admin": bool(user.get("is_admin")),
        "accessible_count": sum(1 for item in rows if item["accessible"]),
        "items": rows,
    })


# --- PATREON ROUTES ---


@app.route('/api/islands/<name>/visitors', methods=['GET'])
def get_island_visitors(name):
    """Get the current visitor list for a single island by name via SysBot API (staff only: lists IGNs).

    Queries SysBot telemetry and live API first, with fallback to Visitors.txt.
    Returns 404 if the island is completely unknown.
    """
    if not _is_staff_viewer():
        return jsonify({"success": False, "error": "Admin or Moderator access required."}), 403
    target = name.strip().upper()
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry, _is_sub_island
    except Exception:
        _is_sub_island = lambda n: False
        get_cached_island_telemetry = lambda n: None

    # Check live SysBot telemetry cache first
    t_data = None
    try:
        t_data = get_cached_island_telemetry(target)
    except Exception:
        pass

    # If telemetry cache doesn't have it or is offline, query SysBot HTTP client directly
    if not t_data or not t_data.get("is_online"):
        try:
            from utils.sysbot_api import get_island_sysbot_client
            client = get_island_sysbot_client(target, timeout=2)
            status_data = client.get_bot_status_sync()
            if status_data.get("is_running") or status_data.get("is_connected"):
                from utils.sysbot_telemetry import parse_sysbot_visitors
                v_count, v_names = parse_sysbot_visitors(status_data)
                t_data = {
                    "visitors_count": v_count,
                    "visitor_list": v_names,
                    "is_online": True,
                }
        except Exception:
            pass

    order_name = (Config.ORDER_BOT_ISLAND or "Sinta").strip().upper()
    island_type = "Order" if target == order_name else ("VIP" if _is_sub_island(target) else "Free")

    if t_data and t_data.get("is_online"):
        v_list = t_data.get("visitor_list") or []
        v_count = t_data.get("visitors_count") if t_data.get("visitors_count") is not None else len(v_list)
        return jsonify({
            "island": target,
            "type": island_type,
            "visitor_count": v_count,
            "visitor_list": v_list,
            "bot_online": True,
            "discord_bot_online": True,
            "source": "sysbot_api",
            "timestamp": datetime.now().isoformat(),
        })

    # If SysBot is offline, return offline state without touching filesystem
    return jsonify({
        "island": target,
        "type": island_type,
        "visitor_count": 0,
        "visitor_list": [],
        "bot_online": False,
        "discord_bot_online": False,
        "source": "sysbot_api",
        "timestamp": datetime.now().isoformat(),
    })

    # Check if target is among known 48 islands even if offline and no folder exists
    all_known = [s.strip().upper() for s in getattr(Config, "ALL_ISLAND_PORT_ORDER", [])]
    if target in all_known:
        return jsonify({
            "island": target,
            "type": island_type,
            "visitor_count": 0,
            "visitor_list": [],
            "bot_online": False,
            "discord_bot_online": False,
            "source": "sysbot_api",
            "timestamp": datetime.now().isoformat(),
        })

    return jsonify({"error": f"Island '{name}' not found"}), 404


# --- FLIGHT & ACTIVITY LOGGING ROUTES (NO DISCORD) ---

@app.route('/api/islands/load-balancer', methods=['GET'])
@app.route('/api/islands/load-balancer/status', methods=['GET'])
def api_load_balancer_status():
    """Real-time cluster load balancing metrics and telemetry across all 48 islands."""
    from utils.sysbot_api import get_sysbot_load_balancer
    lb = get_sysbot_load_balancer()
    return jsonify(lb.get_stats())


@app.route('/api/flights/live', methods=['GET'])
def api_live_flights():
    """Real-time visitor overview across all 48 islands without Discord (staff only: lists IGNs)."""
    if not _is_staff_viewer():
        return jsonify({"success": False, "error": "Admin or Moderator access required."}), 403
    from utils.sysbot_telemetry import get_live_visitor_overview
    return jsonify(get_live_visitor_overview())


@app.route('/api/flights/history', methods=['GET'])
def api_flight_history():
    """Historical island visitor arrivals from SQLite island_visits (staff only: anyone's travel by IGN)."""
    if not _is_staff_viewer():
        return jsonify({"success": False, "error": "Admin or Moderator access required."}), 403
    from utils.sysbot_telemetry import get_flight_history
    island = request.args.get("island")
    ign = request.args.get("ign")
    limit = request.args.get("limit", 50, type=int) or 50
    offset = max(request.args.get("offset", 0, type=int) or 0, 0)
    history = get_flight_history(island_name=island, ign=ign, limit=limit, offset=offset)
    return jsonify({
        "success": True,
        "count": len(history),
        "history": history,
    })


@app.route('/api/islands/<name>/activity', methods=['GET'])
def api_island_activity(name):
    """Recent island activity events (arrivals, departures, dodo updates, crashes); staff only."""
    if not _is_staff_viewer():
        return jsonify({"success": False, "error": "Admin or Moderator access required."}), 403
    from utils.sysbot_telemetry import get_recent_island_events
    limit = request.args.get("limit", 50, type=int) or 50
    event_type = request.args.get("type")
    events = get_recent_island_events(island_name=name, event_type=event_type, limit=limit)
    return jsonify({
        "success": True,
        "island": name,
        "count": len(events),
        "events": events,
    })


@app.route('/api/islands/<name>/dodo/refresh', methods=['POST'])
def api_refresh_island_dodo(name):
    """Trigger SysBot to reboot game loop and fetch a fresh Dodo code."""
    user = _current_auth_user()
    if not user or not (user.get("is_admin") or user.get("is_mod")):
        return jsonify({"success": False, "error": "Admin or Moderator access required."}), 403

    from utils.sysbot_api import get_island_sysbot_client
    delay = int(request.json.get("delay_seconds", 0)) if request.is_json and request.json else 0
    client = get_island_sysbot_client(name, timeout=5)
    resp = client.refresh_dodo_sync(delay_seconds=delay)
    return jsonify(resp)


@app.route('/api/islands/<name>/unstick', methods=['POST'])
def api_unstick_island(name):
    """Mashes B 5 times on the Switch virtual controller to clear stuck airport dialogues."""
    user = _current_auth_user()
    if not user or not (user.get("is_admin") or user.get("is_mod")):
        return jsonify({"success": False, "error": "Admin or Moderator access required."}), 403

    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(name, timeout=5)
    resp = client.click_button_sync(button="UNSTICK", count=5, delay_ms=200)
    return jsonify(resp)


@app.route('/api/islands/<name>/clean', methods=['POST'])
def api_clean_island(name):
    """Triggers SysBot to pick up all dropped items on the ground."""
    user = _current_auth_user()
    if not user or not (user.get("is_admin") or user.get("is_mod")):
        return jsonify({"success": False, "error": "Admin or Moderator access required."}), 403

    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(name, timeout=5)
    resp = client.submit_clean_sync()
    return jsonify(resp)


@app.route('/api/islands/<name>/drop', methods=['POST'])
def api_island_drop(name):
    """
    Drop items on the ground on a specific island via SysBot HTTP API.
    Body: { "items": "Nook Miles Ticket 9" | [...], "type": "items"|"diy", "username": "..." }
    """
    # Roles are re-read from Discord on every drop, so a removed Island Access role counts at once
    user = _current_auth_user(force_refresh=True)
    if not user:
        return jsonify({"success": False, "error": "Authentication required. Please log in first."}), 401
    unverified = _roles_unverified(user)
    if unverified:
        return unverified

    # Check for active moderation suspension
    try:
        from api.control_panel import check_user_active_restriction
        active_rest = check_user_active_restriction(
            user.get("user_id"),
            user.get("discord_id") or user.get("id"),
        )
        if active_rest:
            return jsonify({
                "success": False,
                "error": f"Your account is currently suspended ({active_rest['action_type']}). Reason: {active_rest.get('reason') or 'Policy violation'}.",
                "code": "access_suspended",
            }), 403
    except Exception as rest_err:
        logger.warning("[IslandDrop] Moderation check error: %s", rest_err)

    clean_name = re.sub(r"^sub\s+island\s+", "", (name or "").strip(), flags=re.IGNORECASE).strip().title()

    # Access check: mod/admin bypass, or user has island access
    is_viewer_admin = bool(user.get("is_admin"))
    is_viewer_mod = bool(user.get("is_mod")) or is_viewer_admin
    if not is_viewer_mod and not _has_island_access(user.get("roles", []), [], is_viewer_mod, island_name=clean_name, user=user):
        return jsonify({"success": False, "error": f"You do not have the required membership tier to drop items on {clean_name}."}), 403

    if not is_viewer_admin and not _user_has_sub_rules(user):
        return jsonify({
            "success": False,
            "error": "You must review and accept the Subscriber Island Rules first before dropping items.",
            "code": "missing_sub_rules_acceptance",
            "requires_sub_rules": True,
        }), 403

    body = request.get_json(silent=True) or {}
    items = body.get("items") or body.get("item") or ""
    if not items:
        cmd = (body.get("command") or "").strip()
        if cmd.startswith("!drop "):
            items = cmd[6:].strip()
        elif cmd.startswith("!drop"):
            items = cmd[5:].strip()
        else:
            return jsonify({"success": False, "error": "Items list or drop command is required."}), 400

    username = body.get("username") or user.get("nickname") or user.get("username") or "WebUser"
    drop_type = body.get("type") or "items"
    count = body.get("count")

    # Presence check: verify user is physically on the island before dropping
    from utils.island_presence import verify_user_on_island
    is_present, matched_ign, current_visitors = verify_user_on_island(
        clean_name, user=user, request_data=body, allow_admin_bypass=True
    )
    if not is_present:
        return jsonify({
            "success": False,
            "error": f"You must be on {clean_name} before dropping items. We checked via the SysBot API and your character was not detected on the island. Please fly to {clean_name} first and step outside the airport gates.",
            "code": "not_on_island",
            "island": clean_name,
            "visitors": current_visitors,
        }), 403

    from utils.data_manager import resolve_drop_items
    hex_items, _ = resolve_drop_items(items)
    if hex_items:
        items = hex_items

    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(clean_name, timeout=5)
    resp = client.submit_drop_sync(items=items, drop_type=drop_type, username=username, count=count)

    _record_api_audit_event("island_drop_dispatched", clean_name.upper(), {
        "user_id": user.get("user_id"),
        "username": username,
        "items": str(items)[:100],
        "success": bool(resp.get("success")),
    })

    is_success = bool(resp.get("success"))
    err_text = resp.get("error") or resp.get("message") or f"Drop request failed on {clean_name}."
    succ_text = resp.get("message") or f"Drop request sent to {clean_name}!"
    status_code = 200 if is_success else (502 if any(w in str(err_text).lower() for w in ("unreachable", "offline", "timed out")) else 400)

    # Persist drop history to island_drops_log
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO island_drops_log (island_name, items, requested_by_user_id, status, response_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (clean_name, str(items)[:500], str(user.get("user_id") or "web"), "queued" if is_success else "failed", (succ_text if is_success else err_text)[:500]),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("Failed to write island_drops_log: %s", exc)

    if not is_success:
        _record_api_audit_event("island_drop_failed", clean_name.upper(), {
            "user_id": user.get("user_id"),
            "username": username,
            "items": str(items)[:200],
            "error": str(err_text)[:300],
        })

    return jsonify({
        "success": is_success,
        "alert": not is_success,
        "island": clean_name,
        "message": succ_text if is_success else f"Drop failed: {err_text}",
        "error": None if is_success else err_text,
        "alert_title": None if is_success else "Drop Request Rejected",
        "sysbot_response": resp,
    }), status_code



@app.route('/api/islands/<name>/villagers/inject', methods=['POST'])
@app.route('/api/islands/<name>/villager/inject', methods=['POST'])
def api_island_villager_inject(name):
    """
    Move villager(s) into houses on a specific island via SysBot. Plots are SysBot indexes 0-9.
    Body: {"villager", "name"?, "plot"?, "confirm_replace"?}
       or {"villagers": [{"villager", "name"?, "plot"}], "confirm_replace"?}
       or {"command": "!mvi ..."} (legacy, unchecked).
    409 with a `code` when the move-in clashes with current residents.
    """
    user = _current_auth_user()
    if not user:
        return jsonify({"success": False, "error": "Authentication required. Please log in first."}), 401

    # Check for active moderation suspension
    try:
        from api.control_panel import check_user_active_restriction
        active_rest = check_user_active_restriction(
            user.get("user_id"),
            user.get("discord_id") or user.get("id"),
        )
        if active_rest:
            return jsonify({
                "success": False,
                "error": f"Your account is currently suspended ({active_rest['action_type']}). Reason: {active_rest.get('reason') or 'Policy violation'}.",
                "code": "access_suspended",
            }), 403
    except Exception as rest_err:
        logger.warning("[VillagerInject] Moderation check error: %s", rest_err)

    clean_name = re.sub(r"^sub\s+island\s+", "", (name or "").strip(), flags=re.IGNORECASE).strip().title()

    is_viewer_admin = bool(user.get("is_admin"))
    is_viewer_mod = bool(user.get("is_mod")) or is_viewer_admin
    if not is_viewer_mod and not _has_island_access(user.get("roles", []), [], is_viewer_mod, island_name=clean_name, user=user):
        return jsonify({"success": False, "error": f"You do not have access to move villagers in on {clean_name}. It needs a higher membership tier."}), 403

    if not is_viewer_admin and not _user_has_sub_rules(user):
        return jsonify({
            "success": False,
            "error": "You must review and accept the Subscriber Island Rules first before injecting villagers.",
            "code": "missing_sub_rules_acceptance",
            "requires_sub_rules": True,
        }), 403

    body = request.get_json(silent=True) or {}
    command = (body.get("command") or "").strip()
    confirm_replace = bool(body.get("confirm_replace"))

    # Plots are SysBot house indexes 0-9 (the website shows them as House 1-10).
    # Parsed with `is not None`, never `or`: plot 0 is a real house, not "any free plot".
    raw_plot = body.get("plot") if body.get("plot") is not None else body.get("plot_number")
    plot = island_residents.parse_plot(raw_plot)
    if raw_plot not in (None, "") and plot is None:
        return jsonify({"success": False, "error": "Pick a house from 1 to 10.", "code": "bad_plot"}), 400

    # entries: (SysBot villager code, display name for checks/messages, plot)
    entries: list[tuple[str, str, int | None]] = []
    raw_list = body.get("villagers")
    if isinstance(raw_list, list):
        if not 1 <= len(raw_list) <= 10:
            return jsonify({"success": False, "error": "Move in between 1 and 10 villagers at a time."}), 400
        for item in raw_list:
            if not isinstance(item, dict):
                return jsonify({"success": False, "error": "Each villager needs a name and a house."}), 400
            code = str(item.get("villager") or item.get("villager_name") or "").strip()
            item_plot = island_residents.parse_plot(item.get("plot"))
            if not code or item_plot is None:
                return jsonify({"success": False, "error": "Each villager needs a name and a house from 1 to 10.", "code": "bad_plot"}), 400
            entries.append((code, str(item.get("name") or code).strip(), item_plot))
    else:
        single = body.get("villager") or body.get("villager_name")
        if isinstance(single, str) and single.strip():
            code = single.strip()
            entries.append((code, str(body.get("name") or code).strip(), plot))

    if not entries and not command:
        return jsonify({"success": False, "error": "Villager or injection command is required."}), 400

    from utils.data_manager import resolve_villager_code
    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(clean_name, timeout=5)

    replacements: list = []
    if entries:
        # Same guardrails as the Discord /inject command, checked against who lives there right now.
        # Names that are still raw "villager:<id>" codes can't be compared, so they skip the resident checks.
        residents = island_residents.get_live_residents(clean_name, fresh=True)
        checkable = [(n, p) for _c, n, p in entries if not n.lower().startswith("villager:")]
        blocking, replacements = island_residents.find_clashes(checkable, residents or [])
        if blocking:
            first = blocking[0]
            messages = {
                "already_resident": f"{first.villager} already lives on {clean_name}. An island can't have the same villager twice.",
                "duplicate_villager": f"{first.villager} is in the list twice.",
                "duplicate_plot": f"Two villagers are going to house {(first.plot or 0) + 1}.",
            }
            return jsonify({
                "success": False,
                "error": messages.get(first.code, "These move-ins clash."),
                "code": first.code,
                "clashes": [c.to_json() for c in blocking],
                "residents": [r.to_json() for r in residents or []],
            }), 409
        if replacements and not confirm_replace:
            return jsonify({
                "success": False,
                "error": "That would replace someone who lives there now. Confirm to go ahead.",
                "code": "plot_occupied",
                "replacements": [c.to_json() for c in replacements],
                "residents": [r.to_json() for r in residents or []],
            }), 409

        payload = [
            {"villager": resolve_villager_code(c), **({"plot": p} if p is not None else {})}
            for c, _n, p in entries
        ]
        resp = client.inject_villagers_sync(payload[0] if len(payload) == 1 else payload)
    else:
        resp = client.inject_villagers_sync(resolve_villager_code(command))

    if resp.get("success"):
        island_residents.forget(clean_name)

    names = ", ".join(n for _c, n, _p in entries) or command
    status_str = "queued" if resp.get("success") else "failed"
    try:
        from utils.sysbot_telemetry import ensure_telemetry_schema
        ensure_telemetry_schema()
        db = get_db()
        try:
            for _c, n, p in entries or [(command, command, None)]:
                db.execute("""
                    INSERT INTO villager_injections_log
                    (island_name, villager_name, plot_number, requested_by_user_id, status, response_message)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    clean_name,
                    n,
                    p if p is not None else -1,  # -1: SysBot picked the house
                    str(user.get("user_id") or ""),
                    status_str,
                    str(resp.get("message") or resp.get("error") or ""),
                ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug(f"Audit log error: {exc}")

    _record_api_audit_event("island_villager_inject", clean_name.upper(), {
        "user_id": user.get("user_id"),
        "villager": names,
        "plot": plot,
        "replaced": [c.occupant for c in replacements],
        "success": bool(resp.get("success")),
    })

    single_entry = entries[0] if len(entries) == 1 else None
    return jsonify({
        "success": bool(resp.get("success")),
        "island": clean_name,
        "villager": single_entry[1] if single_entry else names,
        "plot": single_entry[2] if single_entry else plot,
        "error": None if resp.get("success") else (resp.get("error") or resp.get("message") or "The island bot didn't accept the move-in."),
        "message": resp.get("message") or f"Villager injection sent to {clean_name}!",
        "sysbot_response": resp,
    })


@app.route('/api/islands/<name>/residents', methods=['GET'])
def api_island_residents(name):
    """
    Villagers living on an island right now, with SysBot house indexes (0-9) when it reports them.
    live=false means SysBot couldn't be reached and the names come from the shared villager list.
    """
    clean_name = re.sub(r"^sub\s+island\s+", "", (name or "").strip(), flags=re.IGNORECASE).strip().title()
    known = {n.lower(): n for n in (getattr(Config, "ALL_ISLAND_PORT_ORDER", None) or [])}
    if known and clean_name.lower() not in known:
        return jsonify({"success": False, "error": "Unknown island."}), 404
    clean_name = known.get(clean_name.lower(), clean_name)

    residents = island_residents.get_live_residents(clean_name)
    if residents is not None:
        return jsonify({
            "success": True,
            "island": clean_name,
            "live": True,
            "residents": [r.to_json() for r in residents],
        })

    manifest, _source = _get_sysbot_villagers_manifest()
    names = manifest.get(clean_name.title()) or manifest.get(clean_name) or []
    return jsonify({
        "success": True,
        "island": clean_name,
        "live": False,
        "residents": [{"name": n, "plot": None} for n in names],
    })



@app.route("/api/patreon/posts", methods=["GET"])
def get_patreon_posts():
    """Recent Patreon posts, newest first (the Patreon client caches them 15 min).

    Always teasers for patron-only posts, whoever asks: the list is the same for everyone.
    """
    from utils.patreon import PatreonError, patreon

    try:
        rows = patreon.posts(limit=20)
    except PatreonError as exc:
        logger.warning("Patreon posts failed: %s", exc)
        return jsonify({"error": "Patreon API Error"}), 502
    except Exception:
        logger.exception("Patreon posts failed")
        return jsonify({"error": "Server error"}), 500

    tiers_by_id = _patreon_tiers_by_id()
    data = [
        process_post_attributes(p["id"], p, required_tier=_post_required_tier(p, tiers_by_id))
        for p in rows
    ]
    return jsonify({"data": data})


@app.route("/api/patreon/posts/<post_id>", methods=["GET"])
def get_single_post(post_id):
    """One Patreon post. Patron-only posts come back in full only to a signed-in viewer allowed to
    read them (see ``_viewer_can_read_post``); everyone else gets the teaser."""
    from utils.patreon import PatreonError, patreon

    if not str(post_id).isdigit():
        return jsonify({"error": "Post not found"}), 404
    try:
        attrs = patreon.post(str(post_id))
    except PatreonError as exc:
        if exc.status == 404:
            return jsonify({"error": "Post not found"}), 404
        logger.warning("Patreon post %s failed: %s", post_id, exc)
        return jsonify({"error": "Patreon API Error"}), 502
    except Exception:
        logger.exception("Patreon post %s failed", post_id)
        return jsonify({"error": "Server error"}), 500

    if attrs.get("is_public"):
        return jsonify({"data": process_post_attributes(attrs["id"], attrs)})

    tiers_by_id = _patreon_tiers_by_id()
    unlocked = _viewer_can_read_post(_current_auth_user(), attrs, tiers_by_id)
    resp = jsonify({"data": process_post_attributes(
        attrs["id"], attrs, unlocked=unlocked, required_tier=_post_required_tier(attrs, tiers_by_id),
    )})
    # The answer depends on who asks: never let a shared cache keep (or hand out) the full post.
    resp.headers["Cache-Control"] = "private, no-store"
    resp.vary.add("Authorization")
    return resp


@app.route("/api/stream/status", methods=["GET"])
def api_stream_status():
    """Whether ChoPaeng is live on Twitch and/or YouTube right now (shared short caches; never errors).

    "primary" is the platform the website should show: Twitch wins when both are live.
    """
    from concurrent.futures import ThreadPoolExecutor

    from utils.twitch_api import get_stream_status
    from utils.youtube_live import get_live_status

    # Both lookups are cached, but on a cold cache each is a network call; run them side by side.
    with ThreadPoolExecutor(max_workers=2) as pool:
        twitch_future = pool.submit(get_stream_status)
        youtube_future = pool.submit(get_live_status)
        twitch, youtube = twitch_future.result(), youtube_future.result()

    primary = "twitch" if twitch.get("live") else "youtube" if youtube.get("live") else None
    response = jsonify({"ok": True, "live": primary is not None, "primary": primary, "twitch": twitch, "youtube": youtube})
    # Let browsers and the edge reuse the answer briefly; it only changes when a stream starts or ends.
    response.headers["Cache-Control"] = "public, max-age=30"
    return response


# --- STATUS ROUTE ---
@app.route('/status')
def status():
    """Get bot status"""
    if data_manager is None:
        return "Service unavailable â€” data manager not initialised.", 503
    with data_manager.lock:
        count = len(data_manager.cache)
        last_up = data_manager.last_update.strftime("%H:%M:%S") if data_manager.last_update else "Loading..."
    return f"Items: {count} | Last Update: {last_up}"


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Manually trigger a cache refresh from Google Sheets"""
    auth = request.headers.get("Authorization", "")
    secret_bearer_ok = (
        auth.startswith("Bearer ")
        and Config.DASHBOARD_SECRET
        and _secrets.compare_digest(auth[len("Bearer "):], Config.DASHBOARD_SECRET)
    )
    token_user = _current_auth_user() if auth.startswith("Bearer ") else None
    mod_bearer_ok = bool(
        token_user
        and (
            token_user.get("is_admin")
            or token_user.get("is_mod")
            or _is_mod(token_user.get("roles", []))
        )
    )
    session_ok = _check_dashboard_session()
    if not secret_bearer_ok and not mod_bearer_ok and not session_ok:
        return jsonify({"error": "Unauthorized"}), 401

    maintenance = get_maintenance_settings()
    if maintenance["maintenance_mode"] or maintenance["disable_refresh"]:
        return jsonify({
            "error": maintenance["message"] or "Manual refresh is temporarily disabled.",
            "code": "maintenance_mode",
        }), 503

    if data_manager is None:
        return jsonify({"error": "Service unavailable â€” data manager not initialised"}), 503

    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"status": "refresh already in progress"}), 429

    def _run():
        try:
            data_manager.update_cache()
        finally:
            _refresh_lock.release()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "refresh started"}), 202


@app.route("/api/v1/villager/<name>")
def villager_route(name):
    data = NookipediaClient.get_villager_info_sync(name)
    return jsonify({
        "success": True,
        "villager": data
    })


_HUMAN_MEMBERS_CACHE: dict[str, Any] = {"payload": None, "ts": 0.0, "failed_ts": 0.0}
_HUMAN_MEMBERS_TTL = 600         # a good count is reused for 10 minutes
_HUMAN_MEMBERS_FAIL_TTL = 60     # after a failure, wait a minute before asking Discord again
_DISCORD_INVITE_CODE = "chopaeng"
_DISCORD_API = "https://discord.com/api/v10"


def _discord_member_count(guild_id: str) -> tuple[int, str] | None:
    """(approximate member count, guild name) from one Discord request, or None.

    Asks the guild endpoint with the bot token first, then the public invite (no token needed),
    so the count survives a missing or revoked token. Discord's approximate count includes bots;
    it is fine for a "34k+" badge and needs neither the privileged members intent nor paging.
    """
    token = str(Config.DISCORD_TOKEN or "").strip()
    attempts: list[tuple[str, dict[str, str]]] = []
    if token:
        auth = token if token.lower().startswith("bot ") else f"Bot {token}"
        attempts.append((f"{_DISCORD_API}/guilds/{guild_id}", {"Authorization": auth}))
    attempts.append((f"{_DISCORD_API}/invites/{_DISCORD_INVITE_CODE}", {}))

    for url, headers in attempts:
        try:
            resp = requests.get(url, params={"with_counts": "true"}, headers=headers, timeout=5)
            data = resp.json() if resp.ok else None
        except (requests.RequestException, ValueError) as exc:
            logger.warning("member count: %s failed: %s", url.rsplit("/", 2)[-2], exc)
            continue
        if not isinstance(data, dict):
            logger.warning("member count: %s returned HTTP %s", url.rsplit("/", 2)[-2], resp.status_code)
            continue
        guild = data.get("guild") if "guild" in data else data
        if not isinstance(guild, dict) or str(guild.get("id")) != guild_id:
            continue  # an invite that points at some other server
        count = data.get("approximate_member_count")
        if isinstance(count, int) and count > 0:
            return count, str(guild.get("name") or "")
    return None


_TIER_ICONS_CACHE: dict[str, Any] = {"payload": None, "ts": 0.0, "failed_ts": 0.0}
_TIER_ICONS_TTL = 3600       # role icons rarely change
_TIER_ICONS_FAIL_TTL = 60


def _tier_base_name(name: str) -> str:
    """'y ChoSoup' -> 'chosoup', 'ChoTato Club' / 'y ChoTato' -> 'chotato': the tier without its platform marks."""
    n = re.sub(r"^\s*(yt?|tw|p)\s+", "", str(name or ""), flags=re.I).lower()
    n = re.sub(r"\s+club\s*$", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


def _role_icon(role: dict) -> dict | None:
    if role.get("icon"):
        return {"url": f"https://cdn.discordapp.com/role-icons/{role['id']}/{role['icon']}.png?size=64"}
    if role.get("unicode_emoji"):
        return {"emoji": role["unicode_emoji"]}
    return None


def build_tier_role_icons(roles: list[dict], tier_roles: dict[str, str]) -> dict:
    """Icon per subscription tier role. A tier role without its own icon uses the icon of the
    role with the same tier name (YouTube 'y ChoSoup' -> 'ChoSoup'), preferring other tier roles."""
    by_id = {str(r.get("id")): r for r in roles if isinstance(r, dict) and r.get("id")}
    tier_ids = set(tier_roles)
    by_base: dict[str, tuple[dict, str]] = {}
    # Tier roles first, so their icons win over unrelated roles with the same name
    for r in sorted(by_id.values(), key=lambda r: str(r.get("id")) not in tier_ids):
        icon = _role_icon(r)
        base = _tier_base_name(r.get("name") or "")
        if icon and base and base not in by_base:
            by_base[base] = (icon, str(r.get("name") or ""))

    icons: dict[str, dict] = {}
    for role_id, tier_name in tier_roles.items():
        role = by_id.get(role_id)
        name = str((role or {}).get("name") or tier_name)
        icon = _role_icon(role) if role else None
        source = name
        if not icon:
            match = by_base.get(_tier_base_name(name)) or by_base.get(_tier_base_name(tier_name))
            if match:
                icon, source = match
        if icon:
            icons[role_id] = {**icon, "name": tier_name, "from": source}
    return icons


def _staff_role_ids() -> list[str]:
    """Islander (admin), Baby Islander, Senior Mod and Baby Mods role ids, from config."""
    ids = (Config.ADMIN_ROLE_ID, getattr(Config, "BABY_ISLANDER_ROLE_ID", None), Config.SENIOR_MOD_ROLE_ID, Config.BABY_MOD_ROLE_ID)
    return [str(i) for i in ids if str(i or "").isdigit() and str(i) != "0"]


def build_staff_role_icons(roles: list[dict], staff_ids: list[str]) -> list[dict]:
    """Staff roles with their icon and a clean label ('✨ Senior Mod ✨' -> 'Senior Mod'), highest first."""
    out = []
    for r in roles:
        if not isinstance(r, dict) or str(r.get("id")) not in staff_ids:
            continue
        label = re.sub(r"^[^\w]+|[^\w]+$", "", str(r.get("name") or "")).strip() or str(r.get("name") or "")
        out.append({"id": str(r["id"]), "name": label, "position": int(r.get("position") or 0), **(_role_icon(r) or {})})
    return sorted(out, key=lambda x: -x["position"])


# Highest tier first: the role whose icon a member's crown shows
_TIER_ROLE_ORDER = ("ChoSoup", "y ChoSoup", "ChoColate", "y ChoColate", "ChoTato Club", "y ChoTato", "ChoFries")


def _presence_role_ids(user: dict | None) -> tuple[str, str]:
    """(tier role id, staff role id) of a signed-in user, for role icons in the online list."""
    if not user:
        return "", ""
    from api.sub_islands import _normalize_user_roles
    from utils.island_access import TIER_NAME_TO_ROLE_ID
    roles = _normalize_user_roles(user.get("roles") or [])
    tier = next((TIER_NAME_TO_ROLE_ID.get(n, "") for n in _TIER_ROLE_ORDER if n in roles), "")
    staff = next((i for i in _staff_role_ids() if i in roles), "")
    return tier, staff


@app.route("/api/v1/guild/tier-icons")
def guild_tier_icons():
    """Public: the Discord role icon for each subscription tier, keyed by role id and tier name.

    {success, by_id: {role_id: {url|emoji, name, from}}, by_name: {lowercase tier name: same},
     staff: [{id, name, position, url|emoji}] (Islander, Baby Islander, Senior Mod, Baby Mods; highest first)}
    """
    now = time.time()
    cached = _TIER_ICONS_CACHE["payload"]
    if cached and now - _TIER_ICONS_CACHE["ts"] < _TIER_ICONS_TTL:
        return jsonify(cached)
    if now - _TIER_ICONS_CACHE["failed_ts"] < _TIER_ICONS_FAIL_TTL:
        return (jsonify(cached), 200) if cached else (jsonify({"success": False, "by_id": {}, "by_name": {}}), 503)

    guild_id = str(Config.GUILD_ID or "").strip()
    token = str(Config.DISCORD_TOKEN or "").strip()
    if not guild_id or not token:
        return jsonify({"success": False, "by_id": {}, "by_name": {}}), 503
    auth = token if token.lower().startswith("bot ") else f"Bot {token}"
    try:
        resp = requests.get(f"{_DISCORD_API}/guilds/{guild_id}/roles", headers={"Authorization": auth}, timeout=8)
        roles = resp.json() if resp.ok else None
    except (requests.RequestException, ValueError) as exc:
        logger.warning("tier icons: role fetch failed: %s", exc)
        roles = None
    if not isinstance(roles, list):
        _TIER_ICONS_CACHE["failed_ts"] = now
        return (jsonify(cached), 200) if cached else (jsonify({"success": False, "by_id": {}, "by_name": {}}), 503)

    from utils.island_access import ROLE_ID_TO_TIER_NAME
    by_id = build_tier_role_icons(roles, dict(ROLE_ID_TO_TIER_NAME))
    payload = {
        "success": True,
        "by_id": by_id,
        "by_name": {v["name"].lower(): v for v in by_id.values()},
        "staff": build_staff_role_icons(roles, _staff_role_ids()),
    }
    _TIER_ICONS_CACHE.update(payload=payload, ts=now, failed_ts=0.0)
    return jsonify(payload)


@app.route("/api/v1/guild/human-members")
@app.route("/api/stats")
def guild_human_members():
    """Public Discord member count for the website's "Join the Discord" badges.

    200 {success, guild_id, guild_name, discord_members, human_members}
    503 when Discord can't be reached; the website then keeps its built-in fallback number.
    (``human_members`` is kept for old callers; it is the same approximate count.)
    """
    # Public and unauthenticated: serve from cache so callers can't drive Discord traffic.
    now = time.time()
    cached = _HUMAN_MEMBERS_CACHE["payload"]
    if cached and now - _HUMAN_MEMBERS_CACHE["ts"] < _HUMAN_MEMBERS_TTL:
        return jsonify(cached)

    unavailable = {"success": False, "error": "Member count is temporarily unavailable."}
    if now - _HUMAN_MEMBERS_CACHE["failed_ts"] < _HUMAN_MEMBERS_FAIL_TTL:
        return (jsonify(cached), 200) if cached else (jsonify(unavailable), 503)

    guild_id = str(Config.GUILD_ID or "").strip()
    if not guild_id or guild_id in ("0", "None"):
        logger.error("member count: GUILD_ID is not configured")
        return jsonify(unavailable), 503

    result = _discord_member_count(guild_id)
    if result is None:
        _HUMAN_MEMBERS_CACHE["failed_ts"] = now
        # A stale count beats none.
        return (jsonify(cached), 200) if cached else (jsonify(unavailable), 503)

    count, guild_name = result
    payload = {
        "success": True,
        "guild_id": int(guild_id),
        "guild_name": guild_name,
        "human_members": count,
        "discord_members": count,
    }
    _HUMAN_MEMBERS_CACHE.update(payload=payload, ts=now, failed_ts=0.0)
    return jsonify(payload)


# ============================================================================
# POCKET BUNDLES API (Persistent database presets & custom loadouts)
# ============================================================================

_POCKET_BUNDLES_LIST_LIMIT = 300


def _owns_pocket_bundle(auth_user: dict | None, created_by: Any) -> bool:
    """True when the signed-in user created this bundle (created_by holds their username or user id)."""
    if not auth_user or not created_by:
        return False
    owner = str(created_by).strip().lower()
    mine = {str(auth_user.get(k) or "").strip().lower() for k in ("username", "user_id")}
    mine.discard("")
    return owner in mine


def _bundle_schedule_from(data: dict, existing: Any = None) -> tuple[int | None, int | None, int]:
    """(starts_at, ends_at, repeat_yearly) from a create/update body; keeps existing values for
    keys the body leaves out. Raises ValueError for an end before the start."""
    old = dict(existing) if existing is not None else {}

    def ts(key: str, col: str) -> int | None:
        if key not in data:
            return old.get(col) or None
        v = data.get(key)
        if v in (None, "", 0):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a unix timestamp")

    starts, ends = ts("startsAt", "starts_at"), ts("endsAt", "ends_at")
    repeat = int(bool(data["repeatYearly"])) if "repeatYearly" in data else int(old.get("repeat_yearly") or 0)
    if starts and ends and ends <= starts:
        raise ValueError("The schedule must end after it starts.")
    if repeat and not (starts and ends):
        repeat = 0  # a yearly window needs both dates
    return starts, ends, repeat


def _viewer_utc_offset() -> int:
    """The website viewer's UTC offset in minutes (?tz=), for seasonal bundle windows."""
    from utils.bundle_schedule import parse_utc_offset
    return parse_utc_offset(request.args.get("tz"))


def _bundle_schedule_json(row: Any) -> dict:
    from utils.bundle_schedule import bundle_window
    r = dict(row)
    w = bundle_window(r.get("starts_at"), r.get("ends_at"), r.get("repeat_yearly"),
                      utc_offset_minutes=_viewer_utc_offset())
    return {
        "startsAt": r.get("starts_at") or None,
        "endsAt": r.get("ends_at") or None,
        "repeatYearly": bool(r.get("repeat_yearly")),
        # Current (or next) occurrence and whether it is showing now
        "schedule": {"status": w["status"], "start": w["start"], "end": w["end"]},
    }


@app.route("/api/bundles", methods=["GET"])
def get_pocket_bundles():
    """Pocket bundles, official first, then community bundles newest first. Public.

    Seasonal bundles outside their window are left out; admins get every bundle with ?all=1
    (the dashboard), each with its schedule status. Windows are local dates: ?tz=<minutes east
    of UTC> is the viewer's offset (Philippine time when missing).
    """
    from utils.bundle_schedule import is_bundle_live
    show_all = request.args.get("all") in ("1", "true")
    if show_all:
        viewer = _current_auth_user()
        show_all = bool(viewer and viewer.get("is_admin"))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM pocket_bundles ORDER BY is_official DESC, created_at DESC LIMIT ?",
            (_POCKET_BUNDLES_LIST_LIMIT,),
        ).fetchall()

        bundles = []
        for r in rows:
            if not show_all and not is_bundle_live(dict(r), utc_offset_minutes=_viewer_utc_offset()):
                continue
            bundles.append({
                **_bundle_schedule_json(r),
                "id": r["id"],
                "name": r["name"],
                "description": r["description"] or "",
                "category": r["category"] or "Popular",
                "icon": r["icon"] or "fa-box-open",
                "isOfficial": bool(r["is_official"]),
                "createdBy": r["created_by"] or "Community",
                "orderItems": json.loads(r["order_items"] or "[]"),
                "dropItems": json.loads(r["drop_items"] or "[]"),
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"]
            })
        return jsonify(bundles)
    except Exception as exc:
        logger.warning("Error fetching pocket bundles: %s", exc)
        return jsonify([]), 200
    finally:
        conn.close()


@app.route("/api/bundles", methods=["POST"])
def create_pocket_bundle():
    """Create a new pocket bundle (custom or official). Bundles are public, so sign-in is required."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"error": "Sign in to save a bundle."}), 401
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Bundle name is required"}), 400

    bundle_id = data.get("id") or f"bundle-{int(time.time()*1000)}-{_secrets.token_hex(3)}"
    category = data.get("category") or "Popular"
    icon = data.get("icon") or "fa-box-open"
    description = data.get("description") or ""
    order_items = json.dumps(data.get("orderItems") or [])
    drop_items = json.dumps(data.get("dropItems") or [])
    is_official_req = bool(data.get("isOfficial"))

    is_admin = bool(auth_user.get("is_admin")) if auth_user else False
    is_official = 1 if (is_official_req and is_admin) else 0
    created_by = auth_user.get("username") or auth_user.get("user_id") or "Community"
    try:
        starts_at, ends_at, repeat_yearly = _bundle_schedule_from(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    now_iso = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO pocket_bundles
            (id, name, description, category, icon, is_official, created_by, order_items, drop_items,
             starts_at, ends_at, repeat_yearly, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bundle_id, name, description, category, icon, is_official, created_by, order_items, drop_items,
            starts_at, ends_at, repeat_yearly, now_iso, now_iso
        ))
        conn.commit()
        return jsonify({
            "success": True,
            "bundle": {
                **_bundle_schedule_json({"starts_at": starts_at, "ends_at": ends_at, "repeat_yearly": repeat_yearly}),
                "id": bundle_id,
                "name": name,
                "description": description,
                "category": category,
                "icon": icon,
                "isOfficial": bool(is_official),
                "createdBy": created_by,
                "orderItems": json.loads(order_items),
                "dropItems": json.loads(drop_items),
                "createdAt": now_iso,
                "updatedAt": now_iso
            }
        }), 201
    except Exception as exc:
        logger.warning("Error creating pocket bundle: %s", exc)
        return jsonify({"error": "Failed to create bundle in database"}), 500
    finally:
        conn.close()


@app.route("/api/bundles/<bundle_id>", methods=["PUT"])
def update_pocket_bundle(bundle_id):
    """Update a pocket bundle."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM pocket_bundles WHERE id = ?", (bundle_id,)).fetchone()
        if not existing:
            return jsonify({"error": "Bundle not found"}), 404

        is_admin = bool(auth_user.get("is_admin")) if auth_user else False
        if existing["is_official"] and not is_admin:
            return jsonify({"error": "Only admins can edit official bundles"}), 403
        if not is_admin and not _owns_pocket_bundle(auth_user, existing["created_by"]):
            return jsonify({"error": "You can only edit bundles you created"}), 403

        name = data.get("name") or existing["name"]
        description = data.get("description") if "description" in data else existing["description"]
        category = data.get("category") or existing["category"]
        icon = data.get("icon") or existing["icon"]
        is_official = 1 if (data.get("isOfficial") and is_admin) else (existing["is_official"] if not is_admin else 0)
        order_items = json.dumps(data["orderItems"]) if "orderItems" in data else existing["order_items"]
        drop_items = json.dumps(data["dropItems"]) if "dropItems" in data else existing["drop_items"]
        try:
            starts_at, ends_at, repeat_yearly = _bundle_schedule_from(data, existing)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        now_iso = datetime.utcnow().isoformat()

        conn.execute("""
            UPDATE pocket_bundles
            SET name = ?, description = ?, category = ?, icon = ?, is_official = ?, order_items = ?, drop_items = ?,
                starts_at = ?, ends_at = ?, repeat_yearly = ?, updated_at = ?
            WHERE id = ?
        """, (name, description, category, icon, is_official, order_items, drop_items,
              starts_at, ends_at, repeat_yearly, now_iso, bundle_id))
        conn.commit()

        return jsonify({
            "success": True,
            "bundle": {
                **_bundle_schedule_json({"starts_at": starts_at, "ends_at": ends_at, "repeat_yearly": repeat_yearly}),
                "id": bundle_id,
                "name": name,
                "description": description,
                "category": category,
                "icon": icon,
                "isOfficial": bool(is_official),
                "createdBy": existing["created_by"],
                "orderItems": json.loads(order_items),
                "dropItems": json.loads(drop_items),
                "createdAt": existing["created_at"],
                "updatedAt": now_iso
            }
        })
    except Exception as exc:
        logger.warning("Error updating pocket bundle: %s", exc)
        return jsonify({"error": "Failed to update bundle"}), 500
    finally:
        conn.close()


@app.route("/api/bundles/<bundle_id>", methods=["DELETE"])
def delete_pocket_bundle(bundle_id):
    """Delete a pocket bundle."""
    auth_user = _current_auth_user()
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM pocket_bundles WHERE id = ?", (bundle_id,)).fetchone()
        if not existing:
            return jsonify({"success": True}), 200

        is_admin = bool(auth_user.get("is_admin")) if auth_user else False
        if existing["is_official"] and not is_admin:
            return jsonify({"error": "Only admins can delete official bundles"}), 403
        if not is_admin and not _owns_pocket_bundle(auth_user, existing["created_by"]):
            return jsonify({"error": "You can only delete bundles you created"}), 403

        conn.execute("DELETE FROM pocket_bundles WHERE id = ?", (bundle_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        logger.warning("Error deleting pocket bundle: %s", exc)
        return jsonify({"error": "Failed to delete bundle"}), 500
    finally:
        conn.close()
# ============================================================================
# SHARED POCKETS API (Short unique-id pocket URLs)
# ============================================================================

# Crockford's Base32 Alphabet (excludes I, L, O, U to avoid confusion)
_ULID_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_ulid() -> str:
    """Generate a standard 26-character Crockford Base32 ULID.
    - 48 bits of UNIX timestamp in milliseconds (10 characters)
    - 80 bits of cryptographic randomness (16 characters)
    - Lexicographically sortable by creation time.
    """
    ts_ms = int(time.time() * 1000)
    time_chars = [_ULID_ENCODING[(ts_ms >> (45 - 5 * i)) & 0x1F] for i in range(10)]
    rand_val = _secrets.randbits(80)
    rand_chars = [_ULID_ENCODING[(rand_val >> (75 - 5 * i)) & 0x1F] for i in range(16)]
    return "".join(time_chars + rand_chars)


@app.route("/api/pockets/share", methods=["POST"])
def create_shared_pocket():
    """Save a pocket configuration and return a unique ULID."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    name = (data.get("name") or "ACNH Pocket").strip()[:60]
    order_items = data.get("orderItems") or []
    drop_items = data.get("dropItems") or []

    if not order_items and not drop_items:
        return jsonify({"error": "Cannot share an empty pocket"}), 400

    conn = get_db()
    try:
        pocket_id = _generate_ulid()

        created_by = auth_user.get("username") or auth_user.get("user_id") if auth_user else "Guest"
        now_iso = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT INTO shared_pockets (id, name, order_items, drop_items, created_by, views, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
        """, (
            pocket_id,
            name,
            json.dumps(order_items),
            json.dumps(drop_items),
            created_by,
            now_iso
        ))
        conn.commit()

        return jsonify({
            "success": True,
            "id": pocket_id,
            "name": name,
            "orderItems": order_items,
            "dropItems": drop_items,
            "createdAt": now_iso
        }), 201
    except Exception as exc:
        logger.warning("Error saving shared pocket: %s", exc)
        return jsonify({"error": "Failed to save shared pocket"}), 500
    finally:
        conn.close()


@app.route("/api/pockets/share/<pocket_id>", methods=["GET"])
def get_shared_pocket(pocket_id: str):
    """Retrieve shared pocket data by short unique ID."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM shared_pockets WHERE id = ?", (pocket_id,)).fetchone()
        if not row:
            return jsonify({"error": "Shared pocket not found"}), 404

        # Increment view count
        try:
            conn.execute("UPDATE shared_pockets SET views = views + 1 WHERE id = ?", (pocket_id,))
            conn.commit()
        except Exception:
            pass

        return jsonify({
            "id": row["id"],
            "name": row["name"],
            "orderItems": json.loads(row["order_items"] or "[]"),
            "dropItems": json.loads(row["drop_items"] or "[]"),
            "createdBy": row["created_by"],
            "views": row["views"],
            "createdAt": row["created_at"]
        })
    except Exception as exc:
        logger.warning("Error fetching shared pocket %s: %s", pocket_id, exc)
        return jsonify({"error": "Failed to retrieve shared pocket"}), 500
    finally:
        conn.close()



# ============================================================================
# ORDER BOT — SysBot.ACNHOrders REST API PROXY
# All /api/order/* routes proxy directly to the SysBot HTTP API.
# Configure via SYSBOT_API_URL (+ optional SYSBOT_API_KEY) in .env
# ============================================================================

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3.exceptions

# Module-level connection-pooled session with automatic retry on transient
# Cloudflare / network errors (502, 503, 504).  Created lazily on first use.
_sysbot_session: requests.Session | None = None
_sysbot_offline_until: float = 0.0
_SYSBOT_OFFLINE_COOLDOWN = 10.0  # seconds to wait before probing SysBot again after failure


def _get_sysbot_session() -> requests.Session:
    """Return (or lazily create) a pooled requests.Session for the SysBot API."""
    global _sysbot_session
    if _sysbot_session is None:
        _sysbot_session = requests.Session()
        retry_strategy = Retry(
            total=1,
            connect=False,                # DO NOT retry connection refused / unreachable — fail immediately!
            read=1,
            status=1,
            backoff_factor=0.3,           # minimal backoff
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "DELETE"],
            raise_on_status=False,        # let us read the response body even on errors
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=2,
            pool_maxsize=5,
        )
        _sysbot_session.mount("https://", adapter)
        _sysbot_session.mount("http://", adapter)
    return _sysbot_session


def _sysbot_headers() -> dict:
    """Build request headers for the SysBot API, including optional API key."""
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    key = getattr(Config, "SYSBOT_API_KEY", "") or ""
    if key:
        h["X-API-Key"] = key
    return h


def _sysbot_parse_response(resp: requests.Response, method: str, path: str) -> tuple:
    """
    Safely parse a SysBot response.  Handles Cloudflare HTML error pages
    and non-JSON bodies gracefully.  Returns (dict, http_status).
    """
    try:
        return resp.json(), resp.status_code
    except (ValueError, requests.exceptions.JSONDecodeError):
        # Cloudflare or local dev servers sometimes return non-JSON / empty responses
        body_preview = resp.text[:200] if resp.text else "(empty)"
        logger.debug(
            "[SysBot] %s %s returned non-JSON (HTTP %d): %s",
            method, path, resp.status_code, body_preview,
        )
        return {
            "success": False,
            "error": f"SysBot returned HTTP {resp.status_code} (non-JSON response).",
        }, resp.status_code


def _sysbot_get(path: str, **params) -> tuple:
    """Forward a GET to the SysBot API.  Returns (dict, http_status)."""
    global _sysbot_offline_until
    base = getattr(Config, "SYSBOT_API_URL", "") or ""
    if not base:
        return {"success": False, "error": "SysBot API is not configured (set SYSBOT_API_URL in .env)."}, 503

    now = time.monotonic()
    if path in ("/api/status", "/api/queue") and now < _sysbot_offline_until:
        return {"success": False, "error": "SysBot API is currently offline."}, 503

    url = f"{base}{path}"
    t0 = time.monotonic()
    timeout = (1.5, 3.0) if path in ("/api/status", "/api/queue") else (3.0, 15.0)
    try:
        resp = _get_sysbot_session().get(
            url,
            headers=_sysbot_headers(),
            params={k: v for k, v in params.items() if v is not None},
            timeout=timeout,
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        logger.debug("[SysBot] GET %s → %d (%dms)", path, resp.status_code, elapsed)
        if 200 <= resp.status_code < 300:
            _sysbot_offline_until = 0.0
        elif resp.status_code in (502, 503, 504):
            _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        return _sysbot_parse_response(resp, "GET", path)
    except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, OSError):
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.debug("[SysBot] GET %s — connection refused / unreachable", path)
        return {"success": False, "error": "SysBot API is unreachable."}, 503
    except requests.exceptions.Timeout:
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.warning("[SysBot] GET %s — timed out after %.1fs", path, time.monotonic() - t0)
        return {"success": False, "error": "SysBot API request timed out."}, 504
    except Exception as exc:
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.warning("[SysBot] GET %s failed: %s", path, exc)
        return {"success": False, "error": _safe_error(exc)}, 500


def _sysbot_post(path: str, body: dict | list | None = None) -> tuple:
    """Forward a POST to the SysBot API.  Returns (dict, http_status)."""
    global _sysbot_offline_until
    base = getattr(Config, "SYSBOT_API_URL", "") or ""
    if not base:
        return {"success": False, "error": "SysBot API is not configured (set SYSBOT_API_URL in .env)."}, 503

    now = time.monotonic()
    if now < _sysbot_offline_until and path != "/api/order/cancel":
        return {"success": False, "error": "SysBot API is currently offline."}, 503

    url = f"{base}{path}"
    t0 = time.monotonic()
    try:
        resp = _get_sysbot_session().post(
            url,
            headers=_sysbot_headers(),
            json=body if body is not None else {},
            timeout=(3.0, 15.0),
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        logger.debug("[SysBot] POST %s → %d (%dms)", path, resp.status_code, elapsed)
        if 200 <= resp.status_code < 300:
            _sysbot_offline_until = 0.0
        elif resp.status_code in (502, 503, 504):
            _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        return _sysbot_parse_response(resp, "POST", path)
    except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, OSError):
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.debug("[SysBot] POST %s — connection refused / unreachable", path)
        return {"success": False, "error": "SysBot API is unreachable."}, 503
    except requests.exceptions.Timeout:
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.warning("[SysBot] POST %s — timed out after %.1fs", path, time.monotonic() - t0)
        return {"success": False, "error": "SysBot API request timed out."}, 504
    except Exception as exc:
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.warning("[SysBot] POST %s failed: %s", path, exc)
        return {"success": False, "error": _safe_error(exc)}, 500


def _sysbot_delete(path: str, **params) -> tuple:
    """Forward a DELETE to the SysBot API.  Returns (dict, http_status)."""
    global _sysbot_offline_until
    base = getattr(Config, "SYSBOT_API_URL", "") or ""
    if not base:
        return {"success": False, "error": "SysBot API is not configured (set SYSBOT_API_URL in .env)."}, 503

    now = time.monotonic()
    if now < _sysbot_offline_until:
        return {"success": False, "error": "SysBot API is currently offline."}, 503

    url = f"{base}{path}"
    t0 = time.monotonic()
    try:
        resp = _get_sysbot_session().delete(
            url,
            headers=_sysbot_headers(),
            params={k: v for k, v in params.items() if v is not None},
            timeout=(3.0, 15.0),
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        logger.debug("[SysBot] DELETE %s → %d (%dms)", path, resp.status_code, elapsed)
        if 200 <= resp.status_code < 300:
            _sysbot_offline_until = 0.0
        elif resp.status_code in (502, 503, 504):
            _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        return _sysbot_parse_response(resp, "DELETE", path)
    except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, OSError):
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.debug("[SysBot] DELETE %s — connection refused / unreachable", path)
        return {"success": False, "error": "SysBot API is unreachable."}, 503
    except requests.exceptions.Timeout:
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        return {"success": False, "error": "SysBot API request timed out."}, 504
    except Exception as exc:
        _sysbot_offline_until = time.monotonic() + _SYSBOT_OFFLINE_COOLDOWN
        logger.warning("[SysBot] DELETE %s failed: %s", path, exc)
        return {"success": False, "error": _safe_error(exc)}, 500


# ── Normalization helpers ────────────────────────────────────────────────────

# Map SysBot status values → frontend-friendly status values
_SYSBOT_STATUS_MAP = {
    "queued": "queued",
    "next": "preparing",         # first in queue, being prepared
    "ready": "ready",            # dodo code available
    "completed": "completed",
    "cancelled": "cancelled",
    "not_found": "error",        # order expired or unknown
    "error": "error",
    # Pass-through values used by the existing backend
    "active": "preparing",
    "in_progress": "preparing",
    "preparing": "preparing",
}

_INVALID_DODO_CODES = frozenset({"00000", "-----", "null", "None", ""})


def _parse_eta_minutes(data: dict) -> int | None:
    """Extract estimated minutes from estimated_seconds or eta string."""
    if data.get("estimated_seconds") is not None:
        try:
            return max(1, int(round(float(data["estimated_seconds"]) / 60.0)))
        except (ValueError, TypeError):
            pass
    if data.get("eta"):
        m = re.search(r"(\d+)m", str(data["eta"]))
        if m:
            return max(1, int(m.group(1)))
    return None


def _normalize_order_response(data: dict) -> dict:
    """
    Normalize a SysBot order/status response for the frontend:
    - Map status values (next → preparing)
    - Parse eta → estimated_minutes
    - Validate dodo_code
    - Clamp queue_position
    """
    if not isinstance(data, dict):
        return data

    # Dodo code validation
    dodo = data.get("dodo_code")
    has_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
    if not has_dodo:
        data["dodo_code"] = None

    # Status normalization
    raw_status = str(data.get("status") or "queued").lower()
    if has_dodo and raw_status not in ("completed", "cancelled", "error"):
        data["status"] = "ready"
    else:
        data["status"] = _SYSBOT_STATUS_MAP.get(raw_status, raw_status)

    # Queue position — allow 0 for ready/preparing (it's valid from Sinta)
    raw_pos = data.get("queue_position")
    if raw_pos is not None:
        try:
            pos_int = int(raw_pos)
            if pos_int <= 0 and data["status"] not in ("ready", "preparing"):
                pos_int = 1
            data["queue_position"] = max(0, pos_int)
        except (ValueError, TypeError):
            data["queue_position"] = 1

    # Estimated minutes
    if "estimated_minutes" not in data or data.get("estimated_minutes") is None:
        parsed = _parse_eta_minutes(data)
        data["estimated_minutes"] = parsed if parsed is not None else (0 if data["status"] == "ready" else 2)

    return data


# ── Bot Status ───────────────────────────────────────────────────────────────

@app.route("/api/order/bot-status", methods=["GET"])
def get_order_bot_status():
    """
    Proxy GET /api/status from SysBot.ACNHOrders.

    Returns the full bot state including:
    - mode (DropMode / OrderMode), is_drop_mode, is_order_mode
    - is_running, accepting_commands
    - island_name, dodo_code (in Drop Mode), layer
    - visitors_count, visitor_list
    - queue_count, battery_charge
    - last_dodo_fetch, server_time, is_dirty

    Public, so the site can show the bot online/offline and its queue to signed-out visitors.
    Without a login the Dodo code and visitor names are removed (counts stay); ordering still needs one.
    """
    auth_user = _current_auth_user()

    data, code = _sysbot_get("/api/status")

    # Forward all fields as-is from Sinta — the frontend will pick what it needs.
    # Only enrich with defaults for critical fields the frontend relies on.
    if isinstance(data, dict):
        island_name = str(data.get("island_name") or "").strip()
        if island_name.lower().rstrip(".") == "no town name yet":
            data["success"] = False
            data["is_running"] = False
            data["accepting_commands"] = False
            data.setdefault("error", "Bot is currently offline (no town loaded).")
            data.setdefault("queue_count", 0)
            data.setdefault("visitor_list", [])
            data.setdefault("visitors_count", 0)
        elif data.get("success"):
            data.setdefault("island_name", getattr(Config, "ORDER_BOT_ISLAND", "Sinta"))
            data.setdefault("accepting_commands", True)
            data.setdefault("queue_count", 0)
            from utils.sysbot_telemetry import parse_sysbot_visitors
            v_count, v_names = parse_sysbot_visitors(data)
            data["visitor_list"] = v_names
            data["visitors_count"] = v_count
        else:
            # Safe defaults when offline so frontend widgets don't crash or show undefined
            data.setdefault("is_running", False)
            data.setdefault("accepting_commands", False)
            data.setdefault("island_name", getattr(Config, "ORDER_BOT_ISLAND", "Sinta"))
            data.setdefault("queue_count", 0)
            data.setdefault("visitor_list", [])
            data.setdefault("visitors_count", 0)

        if not auth_user:
            for private_key in _BOT_STATUS_PRIVATE_KEYS:
                data.pop(private_key, None)
            data["visitor_list"] = []

    return jsonify(data), code


# Bot-status fields only signed-in users get: the Dodo code lets anyone fly in, and visitor names are players'.
_BOT_STATUS_PRIVATE_KEYS = ("dodo_code", "dodo", "visitors", "visitor_names", "players")


# ── Submit Order ─────────────────────────────────────────────────────────────

# ── Guest orders (no login) ──────────────────────────────────────────────────
# A signed-out visitor can order with their IGN and island name. Without an account to hold them
# to the rules, guests get one open order at a time and a few per hour, keyed to a salted hash of
# their IP (the IP itself is never stored). Each guest order gets a private token, returned once,
# which is the only way to read its status (and Dodo code) or cancel it.

GUEST_ORDER_MAX_PER_HOUR = 3
GUEST_OPEN_ORDER_WINDOW_S = 2 * 3600  # an order the bot hasn't touched in 2h no longer blocks a new one
GUEST_NAME_MAX = 10  # ACNH's own limit for player and island names
_OPEN_ORDER_STATUSES = ("queued", "preparing", "ready")
_guest_column_ready = False


def _ensure_guest_order_column() -> None:
    global _guest_column_ready
    if _guest_column_ready:
        return
    db = get_db()
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(order_bot_queue)").fetchall()}
        if "guest_token_hash" not in cols:
            db.execute("ALTER TABLE order_bot_queue ADD COLUMN guest_token_hash TEXT")
            db.commit()
        _guest_column_ready = True
    except Exception as exc:
        logger.warning("[OrderBot] Could not add guest_token_hash column: %s", exc)
    finally:
        db.close()


def _guest_key() -> str:
    salt = str(getattr(Config, "FLASK_SECRET_KEY", "") or "chopaeng-guest")
    return "guest:" + hashlib.sha256(f"{salt}|{_client_ip()}".encode()).hexdigest()[:24]


def _hash_order_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _clean_guest_name(value) -> str | None:
    """A player or island name as typed, or None when it can't be one (empty, too long, has a |)."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or "|" in text or len(text) > GUEST_NAME_MAX:
        return None
    return text


def _guest_order_token_ok(order_id: str) -> bool:
    """True when the request carries the private token of this guest order."""
    token = (request.headers.get("X-Order-Token") or request.args.get("token") or "").strip()
    if not token or not order_id:
        return False
    _ensure_guest_order_column()
    db = get_db()
    try:
        row = db.execute("SELECT guest_token_hash FROM order_bot_queue WHERE id = ?", (order_id,)).fetchone()
    finally:
        db.close()
    stored = row["guest_token_hash"] if row else None
    return bool(stored) and _secrets.compare_digest(stored, _hash_order_token(token))


def _order_access_ok(order_id: str, auth_user: dict | None) -> bool:
    """
    May this caller see or cancel this order? Yes for the guest holding its token, for admins, and
    for the signed-in account that placed it (any linked id). Being signed in is not enough: order
    ids are visible in the public queue, so without this anyone could read another player's Dodo
    code or cancel their order.
    """
    if _guest_order_token_ok(order_id):
        return True
    if not auth_user:
        return False
    if auth_user.get("is_admin"):
        return True
    db = get_db()
    try:
        row = db.execute("SELECT user_id FROM order_bot_queue WHERE id = ?", (order_id,)).fetchone()
    finally:
        db.close()
    if not row or not row["user_id"]:
        return False
    from utils.unified_auth import resolve_all_user_ids
    return str(row["user_id"]) in set(resolve_all_user_ids(auth_user))


def _guest_order_block(guest_key: str):
    """A (response, status) refusing this guest's order, or None when they may order."""
    now = int(time.time())
    db = get_db()
    try:
        open_count = db.execute(
            f"""SELECT COUNT(*) FROM order_bot_queue
                WHERE user_id = ? AND status IN ({",".join("?" for _ in _OPEN_ORDER_STATUSES)}) AND updated_at > ?""",
            (guest_key, *_OPEN_ORDER_STATUSES, now - GUEST_OPEN_ORDER_WINDOW_S),
        ).fetchone()[0]
        hour_count = db.execute(
            "SELECT COUNT(*) FROM order_bot_queue WHERE user_id = ? AND created_at > ?",
            (guest_key, now - 3600),
        ).fetchone()[0]
    finally:
        db.close()
    if open_count:
        return jsonify({
            "success": False,
            "error": "You already have an order in the queue. Wait for it to finish, or sign in with Discord.",
            "code": "GUEST_ORDER_ACTIVE",
        }), 429
    if hour_count >= GUEST_ORDER_MAX_PER_HOUR:
        return jsonify({
            "success": False,
            "error": f"Guests can place {GUEST_ORDER_MAX_PER_HOUR} orders an hour. Sign in with Discord to order more.",
            "code": "GUEST_RATE_LIMITED",
        }), 429
    return None


@app.route("/api/order/submit", methods=["POST"])
def submit_order_to_bot():
    """
    Proxy POST /api/order to SysBot.ACNHOrders and persist to SQLite DB.

    Accepted body formats:
      { "order": "Gold nugget 30, Iron nugget 10", "villager": "Raymond", "username": "...", "order_id": "..." }
      { "preset": "materials", "username": "..." }
      { "items": ["Gold nugget 30", "Iron nugget 10"], "username": "..." }

    Returns: success, order_id, queue_position, eta, estimated_seconds, item_count, message.
    Save the returned order_id and poll /api/order/status to get the Dodo code when ready.

    Signed-out visitors may order too, with "guest_ign", "guest_island" and "accepted_rules": true.
    Their response adds "guest_token": send it as X-Order-Token to /api/order/status and /cancel.
    """
    auth_user = _current_auth_user()
    data = request.get_json() or {}

    order_text = (data.get("order") or data.get("command") or "").strip()
    # Strip leading bot-command prefix (e.g. "!order ") so SysBot only sees raw item codes.
    order_text = re.sub(r"^!order\s*", "", order_text, flags=re.IGNORECASE).strip()
    items_list = data.get("items")
    preset = (data.get("preset") or "").strip()

    if not order_text and not items_list and not preset:
        return jsonify({"success": False, "error": "Provide 'order', 'items', or 'preset' in the request body."}), 400

    _ensure_guest_order_column()
    guest_token: str | None = None
    if not auth_user:
        guest_ign = _clean_guest_name(data.get("guest_ign"))
        guest_island = _clean_guest_name(data.get("guest_island"))
        if not guest_ign or not guest_island or not is_valid_acnh_nickname(f"{guest_ign} | {guest_island}"):
            return jsonify({
                "success": False,
                "error": f"Enter your in-game name and island name (up to {GUEST_NAME_MAX} characters each), or sign in with Discord.",
                "code": "GUEST_IDENTITY_REQUIRED",
            }), 400
        if data.get("accepted_rules") is not True:
            return jsonify({
                "success": False,
                "error": "Review and accept the Order Bot Rules before ordering.",
                "code": "missing_order_rules_acceptance",
                "requires_order_rules": True,
            }), 403
        guest_key = _guest_key()
        blocked = _guest_order_block(guest_key)
        if blocked:
            return blocked
        username = f"{guest_ign} | {guest_island}"
        user_id = guest_key
        guest_token = _secrets.token_urlsafe(24)
    else:
        username = (
            data.get("username")
            or auth_user.get("nickname")
            or auth_user.get("username")
            or "WebUser"
        )
        user_id = str(auth_user.get("user_id") or auth_user.get("id") or "")

    # Check for active moderation suspension (accounts only; guests are held by the limits above)
    if auth_user:
        try:
            from api.control_panel import check_user_active_restriction
            active_rest = check_user_active_restriction(
                auth_user.get("user_id"),
                auth_user.get("discord_id") or auth_user.get("id"),
            )
            if active_rest:
                return jsonify({
                    "success": False,
                    "error": f"Your account is currently suspended ({active_rest['action_type']}). Reason: {active_rest.get('reason') or 'Policy violation'}.",
                    "code": "access_suspended",
                }), 403
        except Exception as rest_err:
            logger.warning("[OrderSubmit] Moderation check error: %s", rest_err)

    # Require user to accept order rules and setup their server nickname or register their In-Game Character before ordering
    if auth_user and not auth_user.get("is_admin") and user_id not in ("bot_system", ""):
        if not _user_has_order_rules(auth_user):
            return jsonify({
                "success": False,
                "error": "You must review and accept the Order Bot Rules first before using the order bot.",
                "code": "missing_order_rules_acceptance",
                "requires_order_rules": True,
            }), 403

        current_nick = str(auth_user.get("nickname") or "").strip()
        candidate_nick = str(data.get("username") or data.get("display_name") or current_nick).strip()

        # Check if user has a registered In-Game Character
        saved_char = None
        from utils.unified_auth import resolve_all_user_ids
        cand_user_ids = resolve_all_user_ids(auth_user)
        if cand_user_ids:
            char_db = get_db()
            try:
                placeholders = ",".join("?" for _ in cand_user_ids)
                saved_char = char_db.execute(
                    f"""
                    SELECT ign, island_name FROM user_saved_characters
                    WHERE user_id IN ({placeholders}) AND TRIM(ign) != ''
                    ORDER BY is_default DESC, updated_at DESC LIMIT 1
                    """,
                    cand_user_ids,
                ).fetchone()
            finally:
                char_db.close()

        if saved_char and (saved_char["ign"] or "").strip():
            char_ign = (saved_char["ign"] or "").strip()
            char_island = (saved_char["island_name"] or "").strip()
            if not is_valid_acnh_nickname(candidate_nick) and not is_valid_acnh_nickname(current_nick):
                username = f"{char_ign} | {char_island}" if char_island else char_ign
        elif not is_valid_acnh_nickname(candidate_nick) and not is_valid_acnh_nickname(current_nick):
            return jsonify({
                "success": False,
                "error": "You must register your In-Game Character (IGN & Island) in your Profile Settings or set your Discord server nickname to 'Character Name | Island Name' before ordering.",
                "code": "NICKNAME_REQUIRED",
            }), 400

    payload: dict = {"username": username}
    if order_text:
        payload["order"] = order_text
    elif items_list:
        payload["items"] = items_list
    else:
        payload["preset"] = preset

    if data.get("villager"):
        payload["villager"] = data["villager"]
    if data.get("order_id"):
        payload["order_id"] = data["order_id"]
    if user_id:
        payload["user_id"] = user_id

    result, code = _sysbot_post("/api/order", payload)

    # Normalize result fields for frontend and DB
    result = _normalize_order_response(result)

    # Persist order to database
    if isinstance(result, dict) and (result.get("success") or code in (200, 201)):
        order_id = str(result.get("order_id") or result.get("id") or data.get("order_id") or "")
        if order_id:
            now_ts = int(time.time())
            command_str = order_text or (json.dumps(items_list) if items_list else preset)
            db = get_db()
            try:
                db.execute(
                    """REPLACE INTO order_bot_queue
                           (id, user_id, username, command, order_type, status,
                            queue_position, estimated_minutes, dodo_code, island_name,
                            message, created_at, updated_at, guest_token_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        order_id,
                        user_id or "anonymous",
                        username,
                        command_str,
                        "order",
                        result.get("status") or "queued",
                        int(result.get("queue_position") or 1),
                        int(result.get("estimated_minutes") or 2),
                        result.get("dodo_code"),
                        result.get("island_name") or "Sinta",
                        result.get("message") or "Order placed",
                        now_ts,
                        now_ts,
                        _hash_order_token(guest_token) if guest_token else None,
                    ),
                )
                db.commit()
                if guest_token:
                    result["guest_token"] = guest_token
            except Exception as exc:
                logger.warning("[OrderBot] Failed to persist order %s: %s", order_id, exc)
            finally:
                db.close()

    return jsonify(result), code


# ── Poll Order Status ────────────────────────────────────────────────────────

@app.route("/api/order/status", methods=["GET"])
def get_order_status():
    """
    Proxy GET /api/order/status?id={order_id} from SysBot.
    Poll this after submitting an order to track queue position, ETA, and Dodo code.

    status values: queued | preparing | ready | completed | cancelled | error
    dodo_code is non-null once status == "ready".
    Guest orders: send the order's guest_token as X-Order-Token instead of signing in.
    """
    order_id = request.args.get("id") or request.args.get("order_id") or ""
    if not order_id:
        return jsonify({"success": False, "error": "order_id is required."}), 400

    auth_user = _current_auth_user()
    if not auth_user and not _guest_order_token_ok(order_id):
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401
    if not _order_access_ok(order_id, auth_user):
        return jsonify({"success": False, "error": "Order not found."}), 404
    data, code = _sysbot_get("/api/order/status", id=order_id)

    # Normalize response fields from SysBot
    data = _normalize_order_response(data)

    # Sync status to SQLite database
    if isinstance(data, dict) and data.get("status"):
        now_ts = int(time.time())
        db = get_db()
        try:
            db.execute(
                """UPDATE order_bot_queue
                   SET status = ?,
                       queue_position = COALESCE(?, queue_position),
                       estimated_minutes = COALESCE(?, estimated_minutes),
                       dodo_code = COALESCE(?, dodo_code),
                       island_name = COALESCE(?, island_name),
                       message = COALESCE(?, message),
                       updated_at = ?
                   WHERE id = ?""",
                (
                    data.get("status"),
                    data.get("queue_position"),
                    data.get("estimated_minutes"),
                    data.get("dodo_code"),
                    data.get("island_name"),
                    data.get("message"),
                    now_ts,
                    order_id,
                ),
            )
            db.commit()
        except Exception as exc:
            logger.warning("[OrderBot] Failed to update DB status for %s: %s", order_id, exc)
        finally:
            db.close()

    return jsonify(data), code


# ── Cancel Order ─────────────────────────────────────────────────────────────

@app.route("/api/order/cancel", methods=["POST", "DELETE"])
def cancel_order():
    """
    Proxy POST /api/order/cancel to SysBot.
    Body: { "id": "order_id" }
    Also accepts DELETE /api/order/cancel?id=...
    Guest orders: send the order's guest_token as X-Order-Token instead of signing in.
    """
    if request.method == "DELETE":
        order_id = (request.args.get("id") or request.args.get("order_id") or "").strip()
    else:
        body = request.get_json(silent=True) or {}
        order_id = (body.get("id") or body.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"success": False, "error": "order_id is required."}), 400

    auth_user = _current_auth_user()
    if not auth_user and not _guest_order_token_ok(order_id):
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401
    if not _order_access_ok(order_id, auth_user):
        return jsonify({"success": False, "error": "Order not found."}), 404

    result, code = _sysbot_post("/api/order/cancel", {"id": order_id})

    # Mark as cancelled in DB
    db = get_db()
    try:
        db.execute(
            "UPDATE order_bot_queue SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (int(time.time()), order_id),
        )
        db.commit()
    except Exception as exc:
        logger.warning("[OrderBot] Failed to mark order %s as cancelled: %s", order_id, exc)
    finally:
        db.close()

    return jsonify(result), code


# ── User Order History ───────────────────────────────────────────────────────

@app.route("/api/order/user-history", methods=["GET"])
def get_user_order_history():
    """
    Returns the authenticated user's order history from SQLite order_bot_queue.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required", "orders": []}), 401

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(auth_user)
    req_uid = str(request.args.get("user_id") or "").strip()
    if req_uid and req_uid not in cand_ids:
        cand_ids.extend(resolve_all_user_ids(req_uid))
    cand_ids = list(dict.fromkeys([c for c in cand_ids if c]))

    if not cand_ids:
        return jsonify({"success": True, "orders": []})

    limit = min(int(request.args.get("limit", 50)), 100)
    db = get_db()
    try:
        placeholders = ",".join("?" for _ in cand_ids)
        rows = db.execute(
            f"""SELECT id, user_id, username, command, order_type, status,
                      queue_position, estimated_minutes, dodo_code, island_name,
                      message, created_at, updated_at
               FROM order_bot_queue
               WHERE user_id IN ({placeholders})
               ORDER BY created_at DESC
               LIMIT ?""",
            tuple(cand_ids) + (limit,),
        ).fetchall()

        orders = []
        for r in rows:
            orders.append({
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"] or "",
                "command": r["command"] or "",
                "order_type": r["order_type"] or "order",
                "status": r["status"] or "queued",
                "queue_position": r["queue_position"],
                "estimated_minutes": r["estimated_minutes"],
                "dodo_code": r["dodo_code"],
                "island_name": r["island_name"] or "Sinta",
                "message": r["message"] or "",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })

        return jsonify({"success": True, "orders": orders})
    except Exception as exc:
        logger.warning("[OrderBot] Failed to query user order history: %s", exc)
        return jsonify({"success": False, "error": _safe_error(exc), "orders": []}), 500
    finally:
        db.close()


# ── Queue ────────────────────────────────────────────────────────────────────

@app.route("/api/order/queue", methods=["GET"])
def get_order_queue():
    """
    Proxy GET /api/queue from SysBot.
    Returns the full list of pending orders with queue position, ETA, and username.
    Normalizes the response so the frontend gets consistent field names.

    Public: guests ordering from the website need to see the line too. Signed-out callers get only
    the fields the queue list shows (no account ids, islands or item lists); an order id alone
    unlocks nothing, since /api/order/status still needs a sign-in or that order's guest token.
    """
    auth_user = _current_auth_user()

    data, code = _sysbot_get("/api/queue")

    # Normalize each order entry for the frontend
    if isinstance(data, dict) and isinstance(data.get("orders"), list):
        for entry in data["orders"]:
            # Sinta uses "position", frontend expects "queue_position"
            if "position" in entry and "queue_position" not in entry:
                entry["queue_position"] = entry["position"]
            # Normalize status (next → preparing)
            raw_st = str(entry.get("status") or "queued").lower()
            entry["status"] = _SYSBOT_STATUS_MAP.get(raw_st, raw_st)
            # Parse eta → estimated_minutes
            if "estimated_minutes" not in entry:
                parsed = _parse_eta_minutes(entry)
                if parsed is not None:
                    entry["estimated_minutes"] = parsed

        if not auth_user:
            data = {
                "success": data.get("success", True),
                "orders": [
                    {k: e[k] for k in _PUBLIC_QUEUE_FIELDS if k in e}
                    for e in data["orders"]
                    if isinstance(e, dict)
                ],
                **{k: data[k] for k in ("total", "count", "island_name", "current_active_user") if k in data},
            }

        # Also populate the legacy "queue" key for backward compat with frontend
        data["queue"] = data["orders"]
    elif not auth_user and isinstance(data, dict):
        # Unexpected shape from the bot: tell a guest nothing rather than pass it through.
        data = {"success": False, "error": data.get("error") or "Queue unavailable."}

    return jsonify(data), code


# What a signed-out visitor may see of each queue entry (matches the website's queue list).
_PUBLIC_QUEUE_FIELDS = (
    "order_id", "username", "position", "queue_position", "status",
    "eta", "estimated_minutes", "item_count", "villager", "created_at",
)


# ── Dodo Code ────────────────────────────────────────────────────────────────

@app.route("/api/order/dodo", methods=["GET"])
def get_order_dodo():
    """
    Proxy GET /api/dodo from SysBot.
    Drop Mode  -> returns dodo_code immediately (no params needed).
    Order Mode -> pass ?order_id=... to get the code once the order is ready.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    order_id = request.args.get("order_id") or request.args.get("id")
    # Order mode hands out a private code, so only to whoever placed that order.
    if order_id and not _order_access_ok(str(order_id), auth_user):
        return jsonify({"success": False, "error": "Order not found."}), 404
    user_id = request.args.get("user_id") or str(auth_user.get("user_id") or auth_user.get("id") or "")
    if not auth_user.get("is_admin") and user_id not in ("bot_system", ""):
        if not _user_has_order_rules(auth_user):
            return jsonify({
                "success": False,
                "error": "You must review and accept the Order Bot Rules first before accessing order Dodo codes.",
                "code": "missing_order_rules_acceptance",
                "requires_order_rules": True,
            }), 403

    data, code = _sysbot_get("/api/dodo", order_id=order_id, user_id=user_id)
    return jsonify(data), code


# ── Item Drop ────────────────────────────────────────────────────────────────

@app.route("/api/order/drop", methods=["POST"])
def order_drop():
    """
    Proxy POST /api/drop to SysBot.
    Body: { "items": "Gold nugget 30", "type": "items"|"diy", "username": "...", "island": "..." }
    """
    body_peek = request.get_json(silent=True) or {}
    targets_island = bool(body_peek.get("island") or body_peek.get("island_name"))
    # Island drops need Island Access: re-read roles from Discord so a removed role counts at once
    auth_user = _current_auth_user(force_refresh=targets_island)
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401
    if targets_island:
        unverified = _roles_unverified(auth_user)
        if unverified:
            return unverified

    if not auth_user.get("is_admin"):
        if not _user_has_order_rules(auth_user):
            return jsonify({
                "success": False,
                "error": "You must review and accept the Order Bot Rules first before dropping items.",
                "code": "missing_order_rules_acceptance",
                "requires_order_rules": True,
            }), 403

    body = request.get_json(silent=True) or {}
    island_target = body.get("island") or body.get("island_name")
    if island_target:
        clean_name = re.sub(r"^sub\s+island\s+", "", str(island_target).strip(), flags=re.IGNORECASE).strip().title()
        is_viewer_admin = bool(auth_user.get("is_admin"))
        is_viewer_mod = bool(auth_user.get("is_mod")) or is_viewer_admin
        if not is_viewer_mod and not _has_island_access(auth_user.get("roles", []), [], is_viewer_mod, island_name=clean_name, user=auth_user):
            return jsonify({"success": False, "error": f"You do not have access to drop on {clean_name}."}), 403

        if not is_viewer_admin and not _user_has_sub_rules(auth_user):
            return jsonify({
                "success": False,
                "error": "You must review and accept the Subscriber Island Rules first before dropping items.",
                "code": "missing_sub_rules_acceptance",
                "requires_sub_rules": True,
            }), 403

        # Presence check: verify user is physically on the island before dropping
        from utils.island_presence import verify_user_on_island
        is_present, matched_ign, current_visitors = verify_user_on_island(
            clean_name, user=auth_user, request_data=body, allow_admin_bypass=True
        )
        if not is_present:
            return jsonify({
                "success": False,
                "error": f"You must be on {clean_name} before dropping items. We checked via the SysBot API and your character was not detected on the island. Please fly to {clean_name} first and step outside the airport gates.",
                "code": "not_on_island",
                "island": clean_name,
                "visitors": current_visitors,
            }), 403

        from utils.sysbot_api import get_island_sysbot_client
        client = get_island_sysbot_client(clean_name, timeout=5)
        items = body.get("items") or body.get("item") or ""
        username = body.get("username") or auth_user.get("nickname") or auth_user.get("username") or "WebUser"
        drop_type = body.get("type") or "items"
        count = body.get("count")
        result = client.submit_drop_sync(items=items, drop_type=drop_type, username=username, count=count)
        if not result.get("success"):
            _record_api_audit_event("island_drop_failed", clean_name.upper(), {
                "user_id": auth_user.get("user_id"),
                "username": username,
                "items": str(items)[:200],
                "error": str(result.get("error") or "Drop failed")[:300],
            })
            result["alert"] = True
            result["alert_title"] = "Drop Request Rejected"
        return jsonify(result), (200 if result.get("success") else 500 if "error" in result else 200)

    username = (
        data.get("username")
        if (data := body) and data.get("username")
        else (auth_user.get("nickname") or auth_user.get("username") or "WebUser")
    )
    items = body.get("items") or body.get("item") or ""
    if not items:
        return jsonify({"success": False, "error": "items is required."}), 400
    payload = {"items": items, "type": body.get("type") or "items", "username": username}
    if body.get("count"):
        payload["count"] = body["count"]
    result, code = _sysbot_post("/api/drop", payload)
    if not result.get("success"):
        result["alert"] = True
        result["alert_title"] = "Drop Request Rejected"
    return jsonify(result), code


@app.route("/api/islands/<name>/drops/logs", methods=["GET"])
@app.route("/api/drops/logs", methods=["GET"])
def api_island_drops_logs(name=None):
    """Fetch recent drop logs and failure alerts (mods only: rows carry Discord user ids)."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required"}), 401
    if not (auth_user.get("is_admin") or auth_user.get("is_mod") or _is_mod(auth_user.get("roles", []))):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 100)
    except (TypeError, ValueError):
        limit = 50
    status_filter = request.args.get("status")
    try:
        with connect_db() as conn:
            query = "SELECT id, island_name, items, requested_by_user_id, status, response_message, created_at FROM island_drops_log"
            params = []
            clauses = []
            if name:
                clauses.append("LOWER(island_name) = LOWER(?)")
                params.append(name.strip())
            if status_filter:
                clauses.append("LOWER(status) = LOWER(?)")
                params.append(status_filter.strip())
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            logs = [dict(r) for r in rows]
            return jsonify({"success": True, "logs": logs, "count": len(logs)})
    except Exception as exc:
        return jsonify({"success": False, "error": _safe_error(exc)}), 500


# ── Presets ──────────────────────────────────────────────────────────────────

@app.route("/api/order/presets", methods=["GET"])
def get_order_presets():
    """Proxy GET /api/presets — list all configured SysBot preset names."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data, code = _sysbot_get("/api/presets")
    return jsonify(data), code


# ── Speak (In-Game Chat) ─────────────────────────────────────────────────────

@app.route("/api/order/speak", methods=["POST"])
def order_speak():
    """
    Proxy POST /api/speak to SysBot.
    Sends a message to the in-game Switch chat.
    Body: { "message": "Welcome to Sinta!" }
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    body = request.get_json() or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message is required."}), 400
    result, code = _sysbot_post("/api/speak", {"message": message})
    return jsonify(result), code


# ── Turnips (Stalk Market) ───────────────────────────────────────────────────

@app.route("/api/order/turnips", methods=["POST"])
def order_set_turnips():
    """
    Proxy POST /api/turnips to SysBot.
    Sets the island's turnip (stalk market) price.
    Body: { "value": 999999999 }
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    body = request.get_json() or {}
    value = body.get("value")
    if value is None:
        return jsonify({"success": False, "error": "value is required."}), 400
    try:
        value = int(value)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "value must be an integer."}), 400
    result, code = _sysbot_post("/api/turnips", {"value": value})
    return jsonify(result), code


@app.route("/api/order/turnips/max", methods=["GET"])
def order_set_turnips_max():
    """Proxy GET /api/turnips/max — set the maximum turnip price directly."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    data, code = _sysbot_get("/api/turnips/max")
    return jsonify(data), code


# ── Clean (Pick Up Items) ────────────────────────────────────────────────────

@app.route("/api/order/clean", methods=["POST"])
def order_clean():
    """
    Proxy POST /api/clean to SysBot.
    Triggers the bot to pick up (clean) all dropped items on the ground.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    result, code = _sysbot_post("/api/clean")
    return jsonify(result), code


# ── Villagers ────────────────────────────────────────────────────────────────

@app.route("/api/order/villagers", methods=["GET", "POST"])
@app.route("/api/order/villagers/inject", methods=["POST"])
@app.route("/api/order/villager/inject", methods=["POST"])
def order_villagers():
    """
    GET  -> List villagers currently on the island from SysBot.
    POST -> Inject villager(s) into island plots via SysBot POST /api/villagers.
    """
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required. Please log in with Discord."}), 401

    if request.method == "POST":
        if not auth_user.get("is_admin"):
            if not _user_has_order_rules(auth_user):
                return jsonify({
                    "success": False,
                    "error": "You must review and accept the Order Bot Rules first before injecting villagers.",
                    "code": "missing_order_rules_acceptance",
                    "requires_order_rules": True,
                }), 403

        body = request.get_json(silent=True)
        if body is None:
            raw_text = request.get_data(as_text=True).strip()
            if not raw_text:
                return jsonify({"success": False, "error": "Request body is empty."}), 400
            body = {"command": raw_text}

        island_target = body.get("island") or body.get("island_name") if isinstance(body, dict) else None
        if island_target:
            clean_name = re.sub(r"^sub\s+island\s+", "", str(island_target).strip(), flags=re.IGNORECASE).strip().title()
            is_viewer_admin = bool(auth_user.get("is_admin"))
            is_viewer_mod = bool(auth_user.get("is_mod")) or is_viewer_admin
            if not is_viewer_mod and not _has_island_access(auth_user.get("roles", []), [], is_viewer_mod, island_name=clean_name, user=auth_user):
                return jsonify({"success": False, "error": f"You do not have access to inject on {clean_name}."}), 403

            if not is_viewer_admin and not _user_has_sub_rules(auth_user):
                return jsonify({
                    "success": False,
                    "error": "You must review and accept the Subscriber Island Rules first before injecting villagers.",
                    "code": "missing_sub_rules_acceptance",
                    "requires_sub_rules": True,
                }), 403

            from utils.sysbot_api import get_island_sysbot_client
            client = get_island_sysbot_client(clean_name, timeout=5)
            cmd = body.get("command")
            if cmd:
                result = client.inject_villagers_sync(cmd)
            else:
                vill = body.get("villager") or body.get("villagers") or body.get("villager_name")
                plot = body.get("plot") if body.get("plot") is not None else body.get("plot_number")
                result = client.inject_villagers_sync(villagers=vill, plot=plot)
            return jsonify(result), (200 if result.get("success") else 500 if "error" in result else 200)

        result, code = _sysbot_post("/api/villagers", body)
        return jsonify(result), code

    data, code = _sysbot_get("/api/villagers")
    return jsonify(data), code




# ============================================================================
# COMMUNITY LOADOUTS & CLOUD SYNC API
# ============================================================================

POPULAR_LOADOUT_COUNT = 3
POPULAR_LOADOUT_MIN_VIEWS = 3


def _loadout_viewer_id(auth_user) -> str:
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if user_id:
        return user_id
    client_header = request.headers.get("x-client-id") or ""
    ip = request.remote_addr or "127.0.0.1"
    return f"anon_{client_header}_{ip}"[:64]


def _loadout_weekly_views(conn) -> dict:
    """Distinct viewer-days per loadout over the last 7 UTC days."""
    since = (datetime.utcnow() - timedelta(days=6)).strftime("%Y-%m-%d")
    try:
        rows = conn.execute(
            "SELECT loadout_id, COUNT(*) AS cnt FROM community_loadout_views WHERE day >= ? GROUP BY loadout_id",
            (since,),
        ).fetchall()
        return {r["loadout_id"]: int(r["cnt"] or 0) for r in rows}
    except Exception as exc:
        logger.debug("[Loadouts] weekly views unavailable: %s", exc)
        return {}


def _popular_loadout_ids(weekly_views: dict) -> set:
    """Top few loadouts by views this week, if they have enough views to mean anything."""
    ranked = sorted(
        (item for item in weekly_views.items() if item[1] >= POPULAR_LOADOUT_MIN_VIEWS),
        key=lambda item: item[1],
        reverse=True,
    )
    return {loadout_id for loadout_id, _ in ranked[:POPULAR_LOADOUT_COUNT]}


@app.route("/api/loadouts/<loadout_id>/view", methods=["POST"])
def view_loadout(loadout_id: str):
    """Count a loadout preview: at most once per viewer per UTC day."""
    target_id = loadout_id.strip()[:64]
    if not target_id:
        return jsonify({"success": False}), 400
    viewer = _loadout_viewer_id(_current_auth_user())
    day = datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_db()
    try:
        exists = conn.execute(
            "SELECT 1 FROM community_loadouts WHERE id = ?", (target_id,)
        ).fetchone()
        if not exists:
            return jsonify({"success": False, "error": "Loadout not found"}), 404
        seen = conn.execute(
            "SELECT 1 FROM community_loadout_views WHERE loadout_id = ? AND viewer = ? AND day = ?",
            (target_id, viewer, day),
        ).fetchone()
        if not seen:
            conn.execute(
                "REPLACE INTO community_loadout_views (loadout_id, viewer, day) VALUES (?, ?, ?)",
                (target_id, viewer, day),
            )
            conn.execute("UPDATE community_loadouts SET views = views + 1 WHERE id = ?", (target_id,))
            conn.commit()
        return jsonify({"success": True, "counted": not seen})
    except Exception as exc:
        logger.warning("Error counting view for loadout %s: %s", loadout_id, exc)
        return jsonify({"success": False}), 500
    finally:
        conn.close()


@app.route("/api/loadouts", methods=["GET"])
def get_community_loadouts():
    """List public community and official loadouts."""
    auth_user = _current_auth_user()
    category = request.args.get("category")
    tag = request.args.get("tag")
    author = request.args.get("author")
    search = request.args.get("q", "").strip().lower()

    conn = get_db()
    try:
        sql = "SELECT * FROM community_loadouts WHERE 1=1"
        params = []
        if category and category != "All":
            sql += " AND category = ?"
            params.append(category)
        if author:
            sql += " AND created_by = ?"
            params.append(author)
        
        sql += " ORDER BY is_official DESC, upvotes DESC, created_at DESC LIMIT 100"
        rows = conn.execute(sql, tuple(params)).fetchall()
        
        auth_user = _current_auth_user()
        user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
        if not user_id:
            client_header = request.headers.get("x-client-id") or ""
            ip = request.remote_addr or "127.0.0.1"
            user_id = f"anon_{client_header}_{ip}"[:64]

        upvoted_rows = conn.execute(
            "SELECT loadout_id FROM community_loadout_upvotes WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        user_upvoted_ids = {u["loadout_id"] for u in upvoted_rows}

        weekly_views = _loadout_weekly_views(conn)
        popular_ids = _popular_loadout_ids(weekly_views)

        loadouts = []
        for r in rows:
            name = r["name"] or ""
            desc = r["description"] or ""
            tags_list = json.loads(r["tags"] or "[]")
            
            if search:
                if search not in name.lower() and search not in desc.lower() and not any(search in t.lower() for t in tags_list):
                    continue
                    
            loadouts.append({
                "id": r["id"],
                "shortCode": r["short_code"],
                "name": name,
                "description": desc,
                "category": r["category"] or "General",
                "tags": tags_list,
                "orderItems": json.loads(r["order_items"] or "[]"),
                "dropItems": json.loads(r["drop_items"] or "[]"),
                "author": r["created_by"] or "Community",
                "userId": r["user_id"],
                "upvotes": r["upvotes"] or 0,
                "views": r["views"] or 0,
                "weeklyViews": weekly_views.get(r["id"], 0),
                "isPopular": r["id"] in popular_ids,
                "isOfficial": bool(r["is_official"]),
                "hasUpvoted": r["id"] in user_upvoted_ids,
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"]
            })
        return jsonify(loadouts)
    except Exception as exc:
        logger.warning("Error fetching community loadouts: %s", exc)
        return jsonify([]), 200
    finally:
        conn.close()


@app.route("/api/loadouts", methods=["POST"])
def create_community_loadout():
    """Create a new community loadout."""
    auth_user = _current_auth_user()
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()[:60]
    if not name:
        return jsonify({"error": "Loadout name is required"}), 400
    
    order_items = data.get("orderItems") or []
    drop_items = data.get("dropItems") or []
    if not order_items and not drop_items:
        return jsonify({"error": "Cannot publish an empty loadout"}), 400
    
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    random_code = "CHOP-" + "".join(_secrets.choice(chars) for _ in range(4))
    short_code = (data.get("shortCode") or random_code).strip().upper()[:16]
    
    loadout_id = data.get("id") or f"loadout-{int(time.time()*1000)}-{_secrets.token_hex(3)}"
    category = data.get("category") or "General"
    description = (data.get("description") or "").strip()[:300]
    tags = json.dumps(data.get("tags") or [])
    user_id = str(auth_user.get("user_id", "")) if auth_user else None
    created_by = (auth_user.get("username") or data.get("author") or "Community") if auth_user else (data.get("author") or "Community")
    is_admin = bool(auth_user.get("is_admin")) if auth_user else False
    is_official = 1 if (data.get("isOfficial") and is_admin) else 0
    now_iso = datetime.utcnow().isoformat()

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO community_loadouts
            (id, short_code, name, description, tags, category, order_items, drop_items, user_id, created_by, upvotes, views, is_official, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
        """, (
            loadout_id, short_code, name, description, tags, category,
            json.dumps(order_items), json.dumps(drop_items),
            user_id, created_by, is_official, now_iso, now_iso
        ))
        conn.commit()

        return jsonify({
            "success": True,
            "loadout": {
                "id": loadout_id,
                "shortCode": short_code,
                "name": name,
                "description": description,
                "tags": json.loads(tags),
                "category": category,
                "orderItems": order_items,
                "dropItems": drop_items,
                "author": created_by,
                "upvotes": 0,
                "views": 1,
                "isOfficial": bool(is_official),
                "createdAt": now_iso,
                "updatedAt": now_iso
            }
        }), 201
    except Exception as exc:
        logger.warning("Error saving community loadout: %s", exc)
        return jsonify({"error": "Failed to save loadout to database"}), 500
    finally:
        conn.close()


@app.route("/api/loadouts/code/<short_code>", methods=["GET"])
def get_loadout_by_code(short_code: str):
    """Lookup loadout by shortcode or ID (supports community loadouts, staff curated bundles, and shared pockets)."""
    code = short_code.strip().upper()
    conn = get_db()
    try:
        # 1. Community Loadouts
        row = conn.execute(
            "SELECT * FROM community_loadouts WHERE UPPER(short_code) = ? OR id = ?",
            (code, short_code)
        ).fetchone()

        # 2. Staff Curated Pocket Bundles
        if not row:
            clean_bundle_id = code.replace("STAFF-", "").replace("BDL-", "").replace("CHOP-", "").lower()
            bundle_row = conn.execute(
                "SELECT * FROM pocket_bundles WHERE UPPER(id) = ? OR UPPER(name) = ? OR LOWER(id) = ? OR id = ?",
                (code, code, clean_bundle_id, short_code)
            ).fetchone()
            if bundle_row:
                order_items = json.loads(bundle_row["order_items"] or "[]")
                drop_items = json.loads(bundle_row["drop_items"] or "[]")
                return jsonify({
                    "id": f"bundle-{bundle_row['id']}",
                    "shortCode": f"STAFF-{bundle_row['id'][:6].upper()}",
                    "name": bundle_row["name"],
                    "description": bundle_row["description"] or "Official Staff Curated Pocket Build",
                    "category": bundle_row["category"] or "Starter Kits",
                    "tags": ["official", "staff", bundle_row["category"] or "bundle"],
                    "orderItems": order_items,
                    "dropItems": drop_items,
                    "author": bundle_row["created_by"] or "Chopaeng Staff",
                    "userId": None,
                    "upvotes": 0,
                    "views": 1,
                    "isOfficial": True,
                    "createdAt": bundle_row["created_at"],
                    "updatedAt": bundle_row["updated_at"]
                })

        # 3. Shared Pockets
        if not row:
            shared_row = conn.execute(
                "SELECT * FROM shared_pockets WHERE id = ?",
                (short_code,)
            ).fetchone()
            if shared_row:
                return jsonify({
                    "id": shared_row["id"],
                    "shortCode": shared_row["id"],
                    "name": shared_row["name"],
                    "description": "Shared pocket build",
                    "category": "Custom Builds",
                    "tags": ["shared"],
                    "orderItems": json.loads(shared_row["order_items"] or "[]"),
                    "dropItems": json.loads(shared_row["drop_items"] or "[]"),
                    "author": shared_row["created_by"],
                    "upvotes": 0,
                    "views": shared_row["views"],
                    "isOfficial": False,
                    "createdAt": shared_row["created_at"],
                    "updatedAt": shared_row["created_at"]
                })
            return jsonify({"error": "Loadout not found"}), 404

        try:
            conn.execute("UPDATE community_loadouts SET views = views + 1 WHERE id = ?", (row["id"],))
            conn.commit()
        except Exception:
            pass

        return jsonify({
            "id": row["id"],
            "shortCode": row["short_code"],
            "name": row["name"],
            "description": row["description"] or "",
            "category": row["category"] or "General",
            "tags": json.loads(row["tags"] or "[]"),
            "orderItems": json.loads(row["order_items"] or "[]"),
            "dropItems": json.loads(row["drop_items"] or "[]"),
            "author": row["created_by"] or "Community",
            "userId": row["user_id"],
            "upvotes": row["upvotes"] or 0,
            "views": (row["views"] or 0) + 1,
            "isOfficial": bool(row["is_official"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"]
        })
    except Exception as exc:
        logger.warning("Error fetching loadout %s: %s", short_code, exc)
        return jsonify({"error": "Failed to retrieve loadout"}), 500
    finally:
        conn.close()


@app.route("/api/loadouts/<loadout_id>/upvote", methods=["POST"])
def upvote_loadout(loadout_id: str):
    """Toggle upvote for a loadout or staff bundle by user/session stored in database."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        # Fallback to client IP / anonymous token from header
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"anon_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        # Clean loadout_id
        target_id = loadout_id.strip()

        # Check if already upvoted in database
        existing = conn.execute(
            "SELECT 1 FROM community_loadout_upvotes WHERE loadout_id = ? AND user_id = ?",
            (target_id, user_id)
        ).fetchone()

        now_iso = datetime.utcnow().isoformat()
        if existing:
            # Remove upvote (toggle off)
            conn.execute(
                "DELETE FROM community_loadout_upvotes WHERE loadout_id = ? AND user_id = ?",
                (target_id, user_id)
            )
            is_upvoted = False
        else:
            # Insert upvote in database
            conn.execute(
                "REPLACE INTO community_loadout_upvotes (loadout_id, user_id, created_at) VALUES (?, ?, ?)",
                (target_id, user_id, now_iso)
            )
            is_upvoted = True

        # Sync count in community_loadouts table if it exists
        count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM community_loadout_upvotes WHERE loadout_id = ?",
            (target_id,)
        ).fetchone()
        count = count_row["cnt"] if count_row else (1 if is_upvoted else 0)

        # Also update raw ID without bundle- prefix if applicable
        raw_id = target_id.replace("bundle-", "")
        conn.execute(
            "UPDATE community_loadouts SET upvotes = ? WHERE id = ? OR id = ?",
            (count, target_id, raw_id)
        )
        conn.commit()

        return jsonify({
            "success": True,
            "upvoted": is_upvoted,
            "upvotes": count
        })
    except Exception as exc:
        logger.warning("Error toggling upvote for loadout %s: %s", loadout_id, exc)
        return jsonify({"error": "Failed to upvote"}), 500
    finally:
        conn.close()

@app.route("/api/loadouts/<loadout_id>", methods=["DELETE"])
def delete_loadout(loadout_id: str):
    """Delete a loadout (owner or admin)."""
    auth_user = _current_auth_user()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM community_loadouts WHERE id = ?", (loadout_id,)).fetchone()
        if not row:
            return jsonify({"error": "Loadout not found"}), 404

        is_admin = bool(auth_user.get("is_admin")) if auth_user else False
        user_id = str(auth_user.get("user_id", "")) if auth_user else ""
        if not is_admin and (not user_id or str(row["user_id"]) != user_id):
            return jsonify({"error": "Permission denied"}), 403

        conn.execute("DELETE FROM community_loadouts WHERE id = ?", (loadout_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        logger.warning("Error deleting loadout %s: %s", loadout_id, exc)
        return jsonify({"error": "Failed to delete"}), 500
    finally:
        conn.close()


# ─── User Custom Presets (Vault Sync) ─────────────────────────────────────────

@app.route("/api/user/presets", methods=["GET"])
def get_user_presets():
    """Fetch user's private custom presets synced across devices."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM user_custom_presets WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,)
        ).fetchall()

        presets = []
        for r in rows:
            presets.append({
                "id": r["id"],
                "title": r["title"],
                "description": r["description"] or "",
                "category": r["category"] or "Custom Builds",
                "tags": json.loads(r["tags"] or "[]"),
                "orderItems": json.loads(r["order_items"] or "[]"),
                "dropItems": json.loads(r["drop_items"] or "[]"),
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"],
            })
        return jsonify(presets)
    except Exception as exc:
        logger.warning("Error fetching user presets: %s", exc)
        return jsonify([])
    finally:
        conn.close()


@app.route("/api/user/presets", methods=["POST"])
def save_user_preset():
    """Create or update a user's private custom preset."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    data = request.get_json() or {}
    preset_id = (data.get("id") or f"local-preset-{int(time.time()*1000)}").strip()
    title = (data.get("title") or data.get("name") or "Untitled Preset").strip()[:80]
    description = (data.get("description") or "").strip()[:300]
    category = data.get("category") or "Custom Builds"
    tags = json.dumps(data.get("tags") or [])
    order_items = json.dumps(data.get("orderItems") or [])
    drop_items = json.dumps(data.get("dropItems") or [])
    now_iso = datetime.utcnow().isoformat()

    conn = get_db()
    try:
        conn.execute("""
            REPLACE INTO user_custom_presets
            (id, user_id, title, description, category, tags, order_items, drop_items, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            preset_id, user_id, title, description, category, tags,
            order_items, drop_items, data.get("createdAt") or now_iso, now_iso
        ))
        conn.commit()

        return jsonify({
            "success": True,
            "preset": {
                "id": preset_id,
                "title": title,
                "description": description,
                "category": category,
                "tags": json.loads(tags),
                "orderItems": json.loads(order_items),
                "dropItems": json.loads(drop_items),
                "createdAt": data.get("createdAt") or now_iso,
                "updatedAt": now_iso
            }
        }), 201
    except Exception as exc:
        logger.warning("Error saving user preset: %s", exc)
        return jsonify({"error": "Failed to save preset to database"}), 500
    finally:
        conn.close()


@app.route("/api/user/presets/<preset_id>", methods=["DELETE"])
def delete_user_preset(preset_id: str):
    """Delete a user's private custom preset."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM user_custom_presets WHERE id = ? AND (user_id = ? OR ? = '1')",
            (preset_id, user_id, "1" if auth_user and auth_user.get("is_admin") else "0")
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        logger.warning("Error deleting user preset %s: %s", preset_id, exc)
        return jsonify({"error": "Failed to delete"}), 500
    finally:
        conn.close()



# ============================================================================
# USER FAVORITE ISLANDS ENDPOINTS
# ============================================================================

@app.route("/api/user/favorites", methods=["GET", "POST"])
@app.route("/api/profile/favorites", methods=["GET", "POST"])
@app.route("/api/user/favorite-islands", methods=["GET", "POST"])
def api_user_favorite_islands():
    """Get or toggle authenticated user favorite islands."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]

    conn = get_db()
    try:
        if request.method == "GET":
            rows = conn.execute(
                "SELECT island_id FROM user_favorite_islands WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()
            favorite_islands = [r["island_id"] for r in rows if r["island_id"]]
            return jsonify({
                "ok": True,
                "success": True,
                "favorite_islands": favorite_islands,
                "count": len(favorite_islands)
            })

        # POST: Save or toggle single favorite or save batch
        data = request.get_json(silent=True) or {}
        now_iso = datetime.utcnow().isoformat()

        # Case A: Batch list of favorite islands
        if "favorites" in data or "favorite_islands" in data or isinstance(data, list):
            fav_list = data.get("favorites") or data.get("favorite_islands") or data
            if isinstance(fav_list, list):
                conn.execute("DELETE FROM user_favorite_islands WHERE user_id = ?", (user_id,))
                for i_id in fav_list:
                    clean_id = str(i_id).strip().lower()[:64]
                    if clean_id:
                        conn.execute("""
                            REPLACE INTO user_favorite_islands (user_id, island_id, created_at)
                            VALUES (?, ?, ?)
                        """, (user_id, clean_id, now_iso))
                conn.commit()
                return jsonify({"ok": True, "success": True, "count": len(fav_list)})

        # Case B: Single island toggle / set
        island_id = str(data.get("island_id") or data.get("id") or "").strip().lower()[:64]
        if not island_id:
            return jsonify({"ok": False, "error": "island_id is required"}), 400

        is_fav = data.get("is_favorite")
        if is_fav is None:
            is_fav = data.get("isFavorite", True)
        is_fav = bool(is_fav)

        if is_fav:
            conn.execute("""
                REPLACE INTO user_favorite_islands (user_id, island_id, created_at)
                VALUES (?, ?, ?)
            """, (user_id, island_id, now_iso))
        else:
            conn.execute(
                "DELETE FROM user_favorite_islands WHERE user_id = ? AND island_id = ?",
                (user_id, island_id)
            )

        conn.commit()
        return jsonify({
            "ok": True,
            "success": True,
            "island_id": island_id,
            "is_favorite": is_fav
        })
    except Exception as exc:
        logger.warning("Error in user favorite islands API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()

# ============================================================================
# USER SAVED CHARACTERS & PUBLIC PASSPORT ENDPOINTS
# ============================================================================

@app.route("/api/user/characters", methods=["GET", "POST"])
@app.route("/api/profile/characters", methods=["GET", "POST"])
def api_user_characters():
    """Get or save authenticated user in-game characters."""
    auth_user = _current_auth_user()
    from utils.unified_auth import resolve_all_user_ids
    cand_user_ids = resolve_all_user_ids(auth_user) if auth_user else []
    if not cand_user_ids:
        user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
        if not user_id:
            client_header = request.headers.get("x-client-id") or ""
            ip = request.remote_addr or "127.0.0.1"
            user_id = f"client_{client_header}_{ip}"[:64]
        cand_user_ids = [user_id]

    primary_uid = cand_user_ids[0]

    conn = get_db()
    try:
        if request.method == "GET":
            placeholders = ",".join("?" for _ in cand_user_ids)
            rows = conn.execute(
                f"SELECT * FROM user_saved_characters WHERE user_id IN ({placeholders}) ORDER BY is_default DESC, updated_at DESC",
                cand_user_ids,
            ).fetchall()
            characters = []
            seen_ids = set()
            for r in rows:
                c_id = r["id"]
                if c_id in seen_ids:
                    continue
                seen_ids.add(c_id)
                characters.append({
                    "id": c_id,
                    "ign": r["ign"],
                    "islandName": r["island_name"],
                    "title": r["title"] or "Island Resident",
                    "icon": r["icon"] or "fa-leaf",
                    "isDefault": bool(r["is_default"]),
                    "createdAt": r["created_at"],
                    "updatedAt": r["updated_at"],
                })
            return jsonify({"ok": True, "characters": characters})

        # POST: Save character list (up to 3 slots)
        data = request.get_json(silent=True) or {}
        char_list = data.get("characters") if isinstance(data, dict) else data
        if not isinstance(char_list, list):
            char_list = [data] if isinstance(data, dict) and data.get("ign") else []

        now_iso = datetime.utcnow().isoformat()
        # Clear existing across all candidate alias IDs and replace with new state
        del_ph = ",".join("?" for _ in cand_user_ids)
        conn.execute(f"DELETE FROM user_saved_characters WHERE user_id IN ({del_ph})", cand_user_ids)
        for idx, item in enumerate(char_list[:3]):
            c_id = str(item.get("id") or f"char_{int(time.time()*1000)}_{idx}")
            ign = str(item.get("ign") or "").strip()[:10]
            island = str(item.get("islandName") or item.get("island_name") or "").strip()[:10]
            if not ign:
                continue
            title = str(item.get("title") or "Island Resident").strip()[:40]
            icon = str(item.get("icon") or "fa-leaf").strip()[:32]
            is_def = 1 if bool(item.get("isDefault") or item.get("is_default") or (idx == 0 and len(char_list) == 1)) else 0
            created_at = str(item.get("createdAt") or item.get("created_at") or now_iso)

            conn.execute("""
                INSERT OR REPLACE INTO user_saved_characters
                (user_id, id, ign, island_name, title, icon, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (primary_uid, c_id, ign, island, title, icon, is_def, created_at, now_iso))

        conn.commit()

        # Auto-sync the Discord nickname from every saved character, not just the default one,
        # so adding a second character or island shows up on Discord too.
        nickname_sync = "skipped"
        discord_id = str(auth_user.get("discord_id") or auth_user.get("user_id") or "").strip() if auth_user else ""
        if discord_id and discord_id.isdigit():
            _ok, nickname_sync = _sync_user_discord_nickname_from_characters(discord_id, char_list[:3])

        return jsonify({"ok": True, "success": True, "count": min(len(char_list), 3), "nickname_sync": nickname_sync})
    except Exception as exc:
        logger.warning("Error in user_characters API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


def _sync_user_discord_nickname_from_characters(discord_id: str, char_list: list) -> tuple[bool, str]:
    """Set the Discord nickname from all saved characters, default first.

    "Bit | A" + "Bit | B" -> "Bit | A/B"; two characters -> "C1 | I1 | C2 | I2". Characters
    without an island are left out. If everything together is over 32 characters, only the
    default character is used.
    """
    chars = [c for c in char_list if isinstance(c, dict) and str(c.get("ign") or "").strip()]
    chars.sort(key=lambda c: not (c.get("isDefault") or c.get("is_default")))
    pairs = [
        (str(c.get("ign") or "").strip()[:10], str(c.get("islandName") or c.get("island_name") or "").strip()[:10])
        for c in chars
    ]
    full = [(ign, isl) for ign, isl in pairs if isl]
    if not full:
        return False, "no_island"
    nick, errors = build_nickname(full)
    if not nick:
        nick, errors = build_nickname(full[:1])
    if not nick:
        logger.warning("Discord nickname sync for user_id=%s: could not build a nickname: %s", discord_id, errors)
        return False, "invalid_nickname"
    return _set_discord_nickname(discord_id, nick)


def _set_discord_nickname(discord_id: str, nick: str) -> tuple[bool, str]:
    """Set the member's Discord server nickname.

    Returns (success, reason). Skips gracefully if user is Owner/Admin (HTTP 403).
    """
    if not discord_id or not nick:
        return False, "missing_params"
    nick = nick[:32]
    bot_auth = _discord_bot_auth_value()
    guild_id = str(Config.GUILD_ID or "").strip()
    if not bot_auth or not guild_id:
        return False, "discord_not_configured"

    discord_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}"
    req_body = json.dumps({"nick": nick}).encode("utf-8")
    try:
        discord_request(
            discord_url,
            method="PATCH",
            headers={
                "Authorization": bot_auth,
                "Content-Type": "application/json",
            },
            data=req_body,
            timeout=8,
        )
        logger.info("Auto-synced Discord nickname for user_id=%s to '%s'", discord_id, nick)
        return True, "updated"
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            logger.info("Discord nickname sync skipped for user_id=%s (HTTP 403: user is Owner/Admin or higher role)", discord_id)
            return False, "forbidden_hierarchy"
        logger.warning("Discord nickname sync failed for user_id=%s: HTTP %s", discord_id, exc.code)
        return False, f"http_{exc.code}"
    except Exception as exc:
        logger.warning("Discord nickname sync error for user_id=%s: %s", discord_id, exc)
        return False, "error"


@app.route("/api/user/sub-rules", methods=["GET", "POST"])
@app.route("/api/profile/sub-rules", methods=["GET", "POST"])
def api_user_sub_rules():
    """Check or mark the one-time acceptance of Sub Member Rules."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(auth_user)

    if request.method == "POST":
        # A warn only removes access until the rules are re-accepted; an active
        # timeout or ban must not be undone by accepting them again.
        from api.control_panel import check_user_active_restriction
        active_rest = check_user_active_restriction(
            auth_user.get("user_id"),
            auth_user.get("discord_id") or auth_user.get("id"),
        )
        if active_rest:
            return jsonify({
                "ok": False,
                "error": (
                    f"Your island access is suspended ({active_rest['action_type']}). "
                    "You can accept the rules again once it ends."
                ),
                "active_restriction": active_rest,
            }), 403

    conn = get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS user_sub_rules_accepted (user_id TEXT PRIMARY KEY, accepted_at TEXT)")
        if request.method == "POST":
            now_iso = datetime.utcnow().isoformat()
            for uid in set(cand_ids):
                conn.execute(
                    "INSERT OR REPLACE INTO user_sub_rules_accepted (user_id, accepted_at) VALUES (?, ?)",
                    (uid, now_iso),
                )
            conn.commit()

            role_added = False
            discord_uids = [str(uid) for uid in cand_ids if str(uid).isdigit()]
            d_uid = str(auth_user.get("discord_id") or auth_user.get("user_id") or "").strip()
            if d_uid.isdigit() and d_uid not in discord_uids:
                discord_uids.append(d_uid)
            conn_accs = auth_user.get("connected_accounts") or {}
            if isinstance(conn_accs, dict) and "discord" in conn_accs:
                disc_acc = conn_accs["discord"]
                if isinstance(disc_acc, dict) and disc_acc.get("id"):
                    disc_id = str(disc_acc["id"]).strip()
                    if disc_id.isdigit() and disc_id not in discord_uids:
                        discord_uids.append(disc_id)

            from utils.discord_membership import add_guild_member_role
            role_id = getattr(Config, "SUB_RULES_ROLE_ID", 1077997850165772398) or 1077997850165772398
            role_str = str(role_id)
            for du in discord_uids:
                if add_guild_member_role(du, role_id):
                    role_added = True

            current_roles = auth_user.setdefault("roles", [])
            if role_str not in [str(r) for r in current_roles]:
                current_roles.append(role_str)

            return jsonify({"ok": True, "accepted": True, "accepted_at": now_iso, "role_added": role_added})

        accepted = _user_has_sub_rules(auth_user)
        row = None
        if cand_ids:
            placeholders = ",".join("?" for _ in cand_ids)
            row = conn.execute(
                f"SELECT accepted_at FROM user_sub_rules_accepted WHERE user_id IN ({placeholders}) LIMIT 1",
                cand_ids,
            ).fetchone()
        return jsonify({
            "ok": True,
            "accepted": accepted,
            "accepted_at": row["accepted_at"] if row else (datetime.utcnow().isoformat() if accepted else None),
        })
    except Exception as exc:
        logger.warning("Error in api_user_sub_rules: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/order-rules", methods=["GET", "POST"])
@app.route("/api/profile/order-rules", methods=["GET", "POST"])
@app.route("/api/order/rules", methods=["GET", "POST"])
def api_user_order_rules():
    """Check or mark the one-time acceptance of Order Bot Rules."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(auth_user)

    conn = get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS user_order_rules_accepted (user_id TEXT PRIMARY KEY, accepted_at TEXT)")
        if request.method == "POST":
            now_iso = datetime.utcnow().isoformat()
            for uid in set(cand_ids):
                conn.execute(
                    "INSERT OR REPLACE INTO user_order_rules_accepted (user_id, accepted_at) VALUES (?, ?)",
                    (uid, now_iso),
                )
            conn.commit()

            role_added = False
            discord_uids = [str(uid) for uid in cand_ids if str(uid).isdigit()]
            d_uid = str(auth_user.get("discord_id") or auth_user.get("user_id") or "").strip()
            if d_uid.isdigit() and d_uid not in discord_uids:
                discord_uids.append(d_uid)
            conn_accs = auth_user.get("connected_accounts") or {}
            if isinstance(conn_accs, dict) and "discord" in conn_accs:
                disc_acc = conn_accs["discord"]
                if isinstance(disc_acc, dict) and disc_acc.get("id"):
                    disc_id = str(disc_acc["id"]).strip()
                    if disc_id.isdigit() and disc_id not in discord_uids:
                        discord_uids.append(disc_id)

            from utils.discord_membership import add_guild_member_role
            role_id = getattr(Config, "ORDER_RULES_ROLE_ID", 1176191811438510231) or 1176191811438510231
            role_str = str(role_id)
            for du in discord_uids:
                if add_guild_member_role(du, role_id):
                    role_added = True

            current_roles = auth_user.setdefault("roles", [])
            if role_str not in [str(r) for r in current_roles]:
                current_roles.append(role_str)

            return jsonify({"ok": True, "accepted": True, "accepted_at": now_iso, "role_added": role_added})

        accepted = _user_has_order_rules(auth_user)
        row = None
        if cand_ids:
            placeholders = ",".join("?" for _ in cand_ids)
            row = conn.execute(
                f"SELECT accepted_at FROM user_order_rules_accepted WHERE user_id IN ({placeholders}) LIMIT 1",
                cand_ids,
            ).fetchone()
        return jsonify({
            "ok": True,
            "accepted": accepted,
            "accepted_at": row["accepted_at"] if row else (datetime.utcnow().isoformat() if accepted else None),
        })
    except Exception as exc:
        logger.warning("Error in api_user_order_rules: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/features/seen", methods=["GET", "POST"])
@app.route("/api/user/features/dismiss", methods=["POST"])
def api_user_features_seen():
    """Tracks and persists feature announcement dismissals/acknowledgements."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(auth_user)

    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_features_seen (
                user_id VARCHAR(64) NOT NULL,
                feature_id VARCHAR(128) NOT NULL,
                seen_at VARCHAR(64),
                PRIMARY KEY (user_id, feature_id)
            )
        """)

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            feature_id = str(data.get("feature_id") or data.get("id") or "").strip()
            if not feature_id:
                return jsonify({"ok": False, "error": "feature_id required"}), 400

            now_iso = datetime.now(timezone.utc).isoformat()
            for uid in set(cand_ids):
                conn.execute(
                    "INSERT OR REPLACE INTO user_features_seen (user_id, feature_id, seen_at) VALUES (?, ?, ?)",
                    (uid, feature_id, now_iso),
                )
            conn.commit()
            return jsonify({"ok": True, "feature_id": feature_id, "seen_at": now_iso})

        # GET: return dictionary of seen feature IDs
        seen_features = {}
        if cand_ids:
            placeholders = ",".join("?" for _ in cand_ids)
            rows = conn.execute(
                f"SELECT feature_id, seen_at FROM user_features_seen WHERE user_id IN ({placeholders})",
                cand_ids,
            ).fetchall()
            for r in rows:
                seen_features[r["feature_id"]] = r["seen_at"]

        return jsonify({"ok": True, "seen_features": seen_features})
    except Exception as exc:
        logger.warning("Error in api_user_features_seen: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/nickname", methods=["POST"])
@app.route("/api/profile/nickname", methods=["POST"])
@app.route("/api/user/update-nickname", methods=["POST"])
@app.route("/api/profile/update-nickname", methods=["POST"])
def api_update_discord_nickname():
    """Update the authenticated user's Discord server nickname via Discord Bot API."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "Discord User ID not found"}), 401

    data = request.get_json(silent=True) or {}
    new_nickname = str(data.get("nickname") or data.get("nick") or "").strip()

    if not new_nickname:
        return jsonify({"ok": False, "success": False, "error": "Nickname cannot be empty."}), 400

    if len(new_nickname) > 32:
        return jsonify({"ok": False, "success": False, "error": "Discord nicknames cannot exceed 32 characters."}), 400

    bot_auth = _discord_bot_auth_value()
    guild_id = str(Config.GUILD_ID or "").strip()
    if not bot_auth or not guild_id:
        return jsonify({
            "ok": False,
            "success": False,
            "error": "Discord bot or server ID is not configured on this backend.",
        }), 503

    discord_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    req_body = json.dumps({"nick": new_nickname}).encode("utf-8")

    try:
        resp = discord_request(
            discord_url,
            method="PATCH",
            headers={
                "Authorization": bot_auth,
                "Content-Type": "application/json",
            },
            data=req_body,
            timeout=10,
        )
        logger.info("Successfully updated Discord nickname for user_id=%s to '%s'", user_id, new_nickname)
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode(errors="replace")
        except Exception:
            body_text = str(exc)
        logger.warning("Discord API nickname update failed (status=%s) for user_id=%s: %s", exc.code, user_id, body_text)

        if exc.code == 403:
            return jsonify({
                "ok": False,
                "success": False,
                "error": "Discord permission denied: The bot cannot change nicknames for the server owner or roles higher than the bot.",
            }), 403
        elif exc.code == 404:
            return jsonify({
                "ok": False,
                "success": False,
                "error": "You are not currently a member of the ChoPaeng Discord server.",
            }), 404
        elif exc.code == 400:
            return jsonify({
                "ok": False,
                "success": False,
                "error": "Discord rejected this nickname format or characters.",
            }), 400
        else:
            return jsonify({
                "ok": False,
                "success": False,
                "error": f"Discord API returned status {exc.code}.",
            }), exc.code
    except Exception as exc:
        logger.error("Unexpected error updating Discord nickname for user_id=%s: %s", user_id, exc)
        return jsonify({
            "ok": False,
            "success": False,
            "error": "Failed to update Discord nickname.",
        }), 500

    # Update in-memory auth token cache so subsequent profile calls return the new nickname immediately
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        auth_user["nickname"] = new_nickname
        try:
            update_auth_user(token, auth_user)
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "success": True,
        "nickname": new_nickname,
        "nick": new_nickname,
        "message": f"Successfully updated your Discord server nickname to '{new_nickname}'!",
    })


# Handles that would collide with site routes or read as staff accounts.
_RESERVED_HANDLES = frozenset({
    "admin", "administrator", "mod", "moderator", "staff", "support", "help", "system", "official",
    "chopaeng", "chobot", "bot", "api", "console", "dashboard", "profile", "settings", "account",
    "login", "logout", "auth", "u", "user", "users", "islands", "island", "order", "drop", "membership",
    "tiers", "contact", "about", "privacy", "terms", "cookies", "null", "undefined", "resident", "guest",
})
_HANDLE_RE = re.compile(r"[a-z0-9_-]{3,30}")
_PASSPORT_COLUMNS_READY = False


def _ensure_passport_columns(conn) -> None:
    """Older passport tables predate the subscriber tier column."""
    global _PASSPORT_COLUMNS_READY
    if _PASSPORT_COLUMNS_READY:
        return
    try:
        conn.execute("ALTER TABLE user_public_passports ADD COLUMN sub_tier INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists
    _PASSPORT_COLUMNS_READY = True


def _handle_problem(conn, handle: str, user_id: str) -> tuple[str, int] | None:
    """Why this public handle can't be used by this user, as (message, http status), or None if fine."""
    if not _HANDLE_RE.fullmatch(handle):
        return "Choose a username of 3-30 letters, numbers, hyphens or underscores.", 400
    if handle in _RESERVED_HANDLES:
        return "That username is reserved. Please choose another.", 400
    taken = conn.execute(
        "SELECT 1 FROM user_public_passports WHERE LOWER(username) = ? AND user_id != ?",
        (handle, user_id),
    ).fetchone()
    if taken:
        return "That username is already taken.", 409
    return None


def _passport_sub_tier(user: dict | None) -> int:
    from api.sub_islands import get_user_paid_tier as get_user_sub_tier
    # Only a real tier role counts. The user payload's bare "is_subscriber" flag is a loose
    # access hint (set for staff and by heuristics), so it must not invent a ChoTato tier.
    return int(get_user_sub_tier(user) or 0) if user else 0


@app.route("/api/passport/username-available", methods=["GET"])
def api_passport_username_available():
    """Live availability check for the passport handle picker."""
    handle = str(request.args.get("username") or "").strip().lower()
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    conn = get_db()
    try:
        problem = _handle_problem(conn, handle, user_id)
        if problem:
            return jsonify({"ok": True, "available": False, "reason": problem[0]})
        return jsonify({"ok": True, "available": True})
    except Exception as exc:
        logger.warning("Error checking passport handle: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/passport", methods=["GET", "POST", "DELETE"])
@app.route("/api/profile/passport", methods=["GET", "POST", "DELETE"])
def api_user_passport():
    """Get or save authenticated user public passport and profile customizer data."""
    auth_user = _current_auth_user()
    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "") if auth_user else ""
    username = str(auth_user.get("discord_name") or auth_user.get("username") or "") if auth_user else ""
    if not user_id:
        client_header = request.headers.get("x-client-id") or ""
        ip = request.remote_addr or "127.0.0.1"
        user_id = f"client_{client_header}_{ip}"[:64]
        if not username:
            username = user_id

    conn = get_db()
    try:
        _ensure_passport_columns(conn)
        if request.method == "DELETE":
            if not auth_user:
                return jsonify({"ok": False, "error": "Authentication required"}), 401
            conn.execute("DELETE FROM user_public_passports WHERE user_id = ?", (user_id,))
            conn.commit()
            _invalidate_presence_cache()
            return jsonify({"ok": True, "success": True})

        if request.method == "GET":
            row = conn.execute(
                "SELECT * FROM user_public_passports WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if not row and username:
                row = conn.execute(
                    "SELECT * FROM user_public_passports WHERE LOWER(username) = LOWER(?)",
                    (username,)
                ).fetchone()

            if not row:
                return jsonify({"ok": True, "passport": None})

            passport = {
                "username": row["username"],
                "isPublic": bool(row["is_public"]),
                "showCharacterAndIsland": bool(row["show_character_and_island"]),
                "pronouns": row["pronouns"] or "",
                "birthDay": row["birth_day"] or "1",
                "birthMonth": row["birth_month"] or "January",
                "nativeFruit": row["native_fruit"] or "Apple",
                "favouriteColour": row["favourite_colour"] or "#37b06d",
                "favouriteSong": row["favourite_song"] or "K.K. Cruisin'",
                "country": row["country"] or "Island Paradise",
                "language": row["language"] or "English",
                "personality": row["personality"] or "Normal",
                "hobbies": row["hobbies"] or "",
                "favouriteShowsFilms": row["favourite_shows_films"] or "",
                "aboutYou": row["about_you"] or "",
                "favouriteVillagers": json.loads(row["favourite_villagers"] or "[]"),
                "primaryIgn": row["primary_ign"] or "",
                "primaryIsland": row["primary_island"] or "",
                "avatarUrl": row.get("avatar_url") or "",
                "subTier": int(row.get("sub_tier") or 0),
                "isSubscriber": int(row.get("sub_tier") or 0) > 0,
                "updatedAt": row["updated_at"],
            }
            return jsonify({"ok": True, "passport": passport})

        # POST: Save passport
        data = request.get_json(silent=True) or {}
        p = data.get("passport") or data.get("public_passport") or data
        # The public handle is always chosen by the user; never derived from Discord, IGN or island name.
        uname = str(p.get("username") or "").strip().lower()
        problem = _handle_problem(conn, uname, user_id)
        if problem:
            return jsonify({"ok": False, "success": False, "error": problem[0]}), problem[1]
        is_pub = 1 if bool(p.get("isPublic") or p.get("is_public")) else 0
        show_char = 1 if bool(p.get("showCharacterAndIsland", p.get("show_character_and_island", True))) else 0
        pronouns = str(p.get("pronouns") or "")[:40]
        b_day = str(p.get("birthDay") or p.get("birth_day") or "1")[:5]
        b_month = str(p.get("birthMonth") or p.get("birth_month") or "January")[:20]
        fruit = str(p.get("nativeFruit") or p.get("native_fruit") or "Apple")[:20]
        colour = str(p.get("favouriteColour") or p.get("favourite_colour") or "#37b06d")[:20]
        song = str(p.get("favouriteSong") or p.get("favourite_song") or "K.K. Cruisin'")[:60]
        country = str(p.get("country") or "Island Paradise")[:60]
        lang = str(p.get("language") or "English")[:40]
        personality = str(p.get("personality") or "Normal")[:30]
        hobbies = str(p.get("hobbies") or "")[:120]
        shows = str(p.get("favouriteShowsFilms") or p.get("favourite_shows_films") or "")[:120]
        about = str(p.get("aboutYou") or p.get("about_you") or "")[:200]
        villagers = json.dumps(p.get("favouriteVillagers") or p.get("favourite_villagers") or [])
        p_ign = str(p.get("primaryIgn") or p.get("primary_ign") or "")[:32]
        p_island = str(p.get("primaryIsland") or p.get("primary_island") or "")[:32]
        avatar_url = str(p.get("avatarUrl") or p.get("avatar_url") or "")[:500]
        now_iso = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT OR REPLACE INTO user_public_passports (
                user_id, username, is_public, show_character_and_island, pronouns,
                birth_day, birth_month, native_fruit, favourite_colour, favourite_song,
                country, language, personality, hobbies, favourite_shows_films,
                about_you, favourite_villagers, primary_ign, primary_island, avatar_url, updated_at, sub_tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, uname, is_pub, show_char, pronouns,
            b_day, b_month, fruit, colour, song,
            country, lang, personality, hobbies, shows,
            about, villagers, p_ign, p_island, avatar_url, now_iso, _passport_sub_tier(auth_user)
        ))
        conn.commit()

        return jsonify({
            "ok": True,
            "success": True,
            "passport": {
                "username": uname,
                "isPublic": bool(is_pub),
                "showCharacterAndIsland": bool(show_char),
                "pronouns": pronouns,
                "birthDay": b_day,
                "birthMonth": b_month,
                "nativeFruit": fruit,
                "favouriteColour": colour,
                "favouriteSong": song,
                "country": country,
                "language": lang,
                "personality": personality,
                "hobbies": hobbies,
                "favouriteShowsFilms": shows,
                "aboutYou": about,
                "favouriteVillagers": json.loads(villagers),
                "primaryIgn": p_ign,
                "primaryIsland": p_island,
                "updatedAt": now_iso,
            }
        })
    except Exception as exc:
        logger.warning("Error saving user passport: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/public/passport/<username>", methods=["GET"])
@app.route("/api/user/passport/<username>", methods=["GET"])
def api_public_passport_get(username: str):
    """Retrieve public passport data for any user by Discord username or account ID."""
    clean_uname = urllib.parse.unquote(username).strip()
    if not clean_uname:
        return jsonify({"error": "Username is required"}), 400

    auth_user = _current_auth_user()
    current_uid = str(auth_user.get("user_id") or "") if auth_user else ""
    current_uname = str(auth_user.get("discord_name") or auth_user.get("username") or "").lower() if auth_user else ""

    conn = get_db()
    try:
        _ensure_passport_columns(conn)
        row = conn.execute(
            """SELECT * FROM user_public_passports 
               WHERE LOWER(username) = LOWER(?) OR user_id = ?""",
            (clean_uname, clean_uname)
        ).fetchone()

        if not row:
            return jsonify({"error": "Passport not found", "passport": None}), 404

        is_owner = (current_uid and row["user_id"] == current_uid) or (current_uname and row["username"].lower() == current_uname)
        if not bool(row["is_public"]) and not is_owner:
            return jsonify({"error": "This profile is private", "isPrivate": True}), 403

        passport = {
            "username": row["username"],
            "isPublic": bool(row["is_public"]),
            "showCharacterAndIsland": bool(row["show_character_and_island"]),
            "pronouns": row["pronouns"] or "",
            "birthDay": row["birth_day"] or "1",
            "birthMonth": row["birth_month"] or "January",
            "nativeFruit": row["native_fruit"] or "Apple",
            "favouriteColour": row["favourite_colour"] or "#37b06d",
            "favouriteSong": row["favourite_song"] or "K.K. Cruisin'",
            "country": row["country"] or "Island Paradise",
            "language": row["language"] or "English",
            "personality": row["personality"] or "Normal",
            "hobbies": row["hobbies"] or "",
            "favouriteShowsFilms": row["favourite_shows_films"] or "",
            "aboutYou": row["about_you"] or "",
            "favouriteVillagers": json.loads(row["favourite_villagers"] or "[]"),
            "primaryIgn": row["primary_ign"] if bool(row["show_character_and_island"]) or is_owner else "",
            "primaryIsland": row["primary_island"] if bool(row["show_character_and_island"]) or is_owner else "",
            "avatarUrl": row.get("avatar_url") or "",
            "subTier": int(row.get("sub_tier") or 0),
            "isSubscriber": int(row.get("sub_tier") or 0) > 0,
            "isOwner": bool(is_owner),
            "updatedAt": row["updated_at"],
        }
        return jsonify({"ok": True, "passport": passport})
    except Exception as exc:
        logger.warning("Error fetching public passport for %s: %s", clean_uname, exc)
        return jsonify({"error": _safe_error(exc)}), 500
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# REAL-TIME ONLINE PRESENCE & COMMUNITY RADAR ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

_PRESENCE_COLUMNS_READY = False


def _ensure_presence_table(conn):
    """Ensure user_online_presence table exists with correct schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_online_presence (
            session_id          VARCHAR(64) PRIMARY KEY,
            user_id             VARCHAR(64),
            username            VARCHAR(255) NOT NULL,
            display_name        VARCHAR(255),
            avatar_url          TEXT,
            role                VARCHAR(32) DEFAULT 'resident',
            status              VARCHAR(32) DEFAULT 'online',
            current_activity    VARCHAR(255),
            `current_path`      VARCHAR(255),
            current_island      VARCHAR(64),
            ign                 VARCHAR(64),
            island_name         VARCHAR(64),
            native_fruit        VARCHAR(32),
            has_public_passport INTEGER DEFAULT 0,
            is_guest            INTEGER DEFAULT 0,
            ip_hash             VARCHAR(64),
            ip_address          VARCHAR(64),
            viewing_item        VARCHAR(160),
            ordering_items      TEXT,
            last_heartbeat      INTEGER NOT NULL,
            created_at          INTEGER NOT NULL
        )
    """)
    global _PRESENCE_COLUMNS_READY
    if not _PRESENCE_COLUMNS_READY:
        # The table may predate these columns (or be created by the dashboard without them).
        for column, ddl in (
            ("ip_address", "VARCHAR(64)"),
            ("viewing_item", "VARCHAR(160)"),
            ("ordering_items", "TEXT"),
            ("is_subscriber", "INTEGER DEFAULT 0"),
            ("sub_tier", "INTEGER DEFAULT 0"),
            ("is_booster", "INTEGER DEFAULT 0"),
            # Which tier / staff role to draw the Discord role icon for (see /api/v1/guild/tier-icons)
            ("tier_role_id", "VARCHAR(32)"),
            ("staff_role_id", "VARCHAR(32)"),
        ):
            try:
                conn.execute(f"ALTER TABLE user_online_presence ADD COLUMN {column} {ddl}")
                conn.commit()
            except Exception:
                pass  # column already exists
        _PRESENCE_COLUMNS_READY = True
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_presence_last_heartbeat ON user_online_presence (last_heartbeat)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_presence_user_id ON user_online_presence (user_id)")
    except Exception:
        pass


def _ensure_waves_table(conn):
    """Ensure user_waves table exists for real-time cross-user wave delivery."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_waves (
            id                VARCHAR(64) PRIMARY KEY,
            from_username     VARCHAR(255) NOT NULL,
            from_display_name VARCHAR(255) NOT NULL,
            from_avatar_url   TEXT,
            to_username       VARCHAR(255) NOT NULL,
            to_display_name   VARCHAR(255),
            to_session_id     VARCHAR(64),
            status            VARCHAR(32) DEFAULT 'pending',
            created_at        INTEGER NOT NULL,
            expires_at        INTEGER NOT NULL
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_waves_to_user ON user_waves (to_username, status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_waves_to_sess ON user_waves (to_session_id, status, created_at)")
    except Exception:
        pass


PRESENCE_ACTIVE_SECONDS = 75   # a session counts as online while its last beat is this recent
PRESENCE_ROW_SECONDS = 90      # rows older than this are deleted
# Hidden/parked tabs keep beating (as idle) forever; past this they leave the radar until they
# come back. created_at marks the start of the current active or idle stretch.
PRESENCE_IDLE_HIDE_SECONDS = 30 * 60
PRESENCE_ROSTER_LIMIT = 50
PRESENCE_CACHE_SECONDS = 2.0
PRESENCE_PRUNE_INTERVAL = 30
PRESENCE_STREAM_MAX_SECONDS = 600
PRESENCE_STREAMS_PER_SESSION = 3
PRESENCE_LEAVE_GRACE_SECONDS = 8   # after a leave beacon, how long a reload has to beat again
PRESENCE_SWEEP_SECONDS = 10        # while anyone listens, re-check who silently dropped out
PRESENCE_ROLE_RANK = {"admin": 1, "mod": 2, "member": 3}
_PRESENCE_UTC_OFFSET = 8 * 3600  # "today" follows Philippine time, where the community lives

_presence_cache_lock = threading.Lock()
_presence_cache: dict = {"at": 0.0, "rows": None}
_presence_last_prune = 0.0


def _invalidate_presence_cache() -> None:
    with _presence_cache_lock:
        _presence_cache["rows"] = None


def _ensure_visits_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_visit_daily (
            day    VARCHAR(10) PRIMARY KEY,
            visits INTEGER NOT NULL DEFAULT 0
        )
    """)


def _presence_day(now: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now + _PRESENCE_UTC_OFFSET))


def _record_site_visit(conn, now: int) -> None:
    """Count one visit when a session first appears (not on every route change)."""
    _ensure_visits_table(conn)
    conn.execute(
        "INSERT INTO site_visit_daily (day, visits) VALUES (?, 1) "
        "ON CONFLICT(day) DO UPDATE SET visits = visits + 1",
        (_presence_day(now),),
    )


def _site_traffic(conn, now: int) -> dict:
    _ensure_visits_table(conn)
    week_start = _presence_day(now - 6 * 86400)
    row = conn.execute(
        "SELECT COALESCE(SUM(visits), 0) AS total, "
        "COALESCE(SUM(CASE WHEN day >= ? THEN visits ELSE 0 END), 0) AS week, "
        "COALESCE(SUM(CASE WHEN day = ? THEN visits ELSE 0 END), 0) AS today "
        "FROM site_visit_daily",
        (week_start, _presence_day(now)),
    ).fetchone()
    return {
        "allTimeVisits": int(row["total"] or 0),
        "visitsThisWeek": int(row["week"] or 0),
        "visitsToday": int(row["today"] or 0),
    }


def _prune_presence(conn, now: int, *, force: bool = False) -> None:
    """Delete expired sessions, at most once per PRESENCE_PRUNE_INTERVAL unless forced."""
    global _presence_last_prune
    if not force and now - _presence_last_prune < PRESENCE_PRUNE_INTERVAL:
        return
    _presence_last_prune = now
    cur = conn.execute("DELETE FROM user_online_presence WHERE last_heartbeat < ?", (now - PRESENCE_ROW_SECONDS,))
    conn.commit()
    if getattr(cur, "rowcount", 0) > 0:
        _invalidate_presence_cache()
        presence_hub.broadcast_radar(_build_radar_payload)


_presence_sweeper_lock = threading.Lock()
_presence_sweeper_started = False


def _presence_sweep_once() -> None:
    """Recompute the radar so people whose beats stopped (closed tab, killed app) drop out of
    everyone's view within one sweep, instead of waiting for someone else's heartbeat."""
    if presence_hub.subscriber_count == 0:
        return
    conn = get_db()
    try:
        _ensure_presence_table(conn)
        _prune_presence(conn, int(time.time()))
    finally:
        conn.close()
    _invalidate_presence_cache()
    # Unchanged snapshots are deduped inside the hub, so a quiet sweep sends nothing.
    presence_hub.broadcast_radar(_build_radar_payload)


def _presence_sweeper_loop() -> None:
    while True:
        time.sleep(PRESENCE_SWEEP_SECONDS)
        try:
            _presence_sweep_once()
        except Exception as exc:
            logger.debug("[Presence] Sweep failed: %s", exc)


def _ensure_presence_sweeper() -> None:
    """Start the background sweeper the first time anything needs it (one per process)."""
    global _presence_sweeper_started
    if _presence_sweeper_started:
        return
    with _presence_sweeper_lock:
        if _presence_sweeper_started:
            return
        threading.Thread(target=_presence_sweeper_loop, name="presence-sweeper", daemon=True).start()
        _presence_sweeper_started = True


def _presence_people(conn, now: int) -> list[dict]:
    """Active presence rows collapsed to one entry per person, roster-ordered.

    Signed-in users appear once however many tabs or devices they have open. Cached briefly
    because every SSE listener may ask for the same snapshot after a change.
    """
    with _presence_cache_lock:
        rows = _presence_cache["rows"]
        if rows is not None and time.time() - _presence_cache["at"] < PRESENCE_CACHE_SECONDS:
            return rows

    raw = conn.execute(
        "SELECT * FROM user_online_presence WHERE last_heartbeat >= ? "
        "AND NOT (status = 'idle' AND created_at < ?) "
        "ORDER BY last_heartbeat DESC LIMIT 1000",
        (now - PRESENCE_ACTIVE_SECONDS, now - PRESENCE_IDLE_HIDE_SECONDS),
    ).fetchall()
    people: dict[str, dict] = {}
    for r in raw:
        row = dict(r)
        key = f"u:{row['user_id']}" if row.get("user_id") else f"s:{row['session_id']}"
        # Rows arrive newest first; across devices an active one beats an idle one, so a parked
        # laptop doesn't keep flipping someone to "idle" while they use their phone.
        held = people.get(key)
        if held is None or (held.get("status") == "idle" and row.get("status") != "idle"):
            people[key] = row
    def roster_rank(r: dict) -> float:
        rank = PRESENCE_ROLE_RANK.get(r.get("role") or "", 4)
        # Subs sit right after admins and mods, ahead of regular members.
        if rank >= 3 and r.get("is_subscriber"):
            return 2.5 - int(r.get("sub_tier") or 1) / 10
        return rank

    ordered = sorted(
        people.values(),
        key=lambda r: (
            int(r.get("is_guest") or 0),
            roster_rank(r),
            -int(r.get("last_heartbeat") or 0),
        ),
    )
    with _presence_cache_lock:
        _presence_cache["rows"] = ordered
        _presence_cache["at"] = time.time()
    return ordered


PRESENCE_ITEM_KEY_MAX = 120
PRESENCE_ORDERING_ITEMS_MAX = 12
PRESENCE_RADAR_ITEMS_MAX = 200


def _clean_item_key(raw) -> str:
    """Catalog keys look like "item:red-tulip"; anything else is trimmed and lower-cased."""
    return re.sub(r"\s+", " ", str(raw or "")).strip().lower()[:PRESENCE_ITEM_KEY_MAX]


def _clean_ordering_items(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for value in raw:
        key = _clean_item_key(value)
        if key and key not in seen:
            seen.append(key)
        if len(seen) >= PRESENCE_ORDERING_ITEMS_MAX:
            break
    return seen


def _row_ordering_items(row: dict) -> list[str]:
    try:
        parsed = json.loads(row.get("ordering_items") or "[]")
    except (TypeError, ValueError):
        return []
    return _clean_ordering_items(parsed)


def _radar_summary(people: list[dict]) -> dict:
    """Aggregate counts and per-island occupancy. Holds nothing volatile so unchanged radars dedupe."""
    by_status = {"online": 0, "on_island": 0, "ordering": 0, "idle": 0}
    islands: dict[str, dict] = {}
    for r in people:
        status = r.get("status") or "online"
        by_status[status] = by_status.get(status, 0) + 1
        island = (r.get("current_island") or "").strip().upper()
        if not island:
            continue
        entry = islands.setdefault(island, {"island": island, "count": 0, "residents": []})
        entry["count"] += 1
        if not r.get("is_guest") and len(entry["residents"]) < 5:
            entry["residents"].append({
                "id": _public_presence_id(r["session_id"]),
                "displayName": r.get("display_name") or r.get("username") or "Resident",
                "avatarUrl": r.get("avatar_url") or "",
            })
    guests = sum(1 for r in people if r.get("is_guest"))

    # Item counts are aggregate only: nobody is named, and each person counts once per item.
    items: dict[str, dict] = {}
    for r in people:
        viewing = _clean_item_key(r.get("viewing_item"))
        if viewing:
            items.setdefault(viewing, {"key": viewing, "viewing": 0, "ordering": 0})["viewing"] += 1
        for key in _row_ordering_items(r):
            items.setdefault(key, {"key": key, "viewing": 0, "ordering": 0})["ordering"] += 1
    top_items = sorted(
        items.values(), key=lambda e: (-(e["viewing"] + e["ordering"]), e["key"])
    )[:PRESENCE_RADAR_ITEMS_MAX]

    return {
        "totalOnline": len(people),
        "authenticatedCount": len(people) - guests,
        "guestCount": guests,
        "byStatus": by_status,
        "islands": sorted(islands.values(), key=lambda e: (-e["count"], e["island"])),
        "items": top_items,
    }


def _build_radar_payload() -> dict:
    conn = get_db()
    try:
        _ensure_presence_table(conn)
        return _radar_summary(_presence_people(conn, int(time.time())))
    finally:
        conn.close()


def _claim_pending_waves(conn, now: int, username: str, session_id: str) -> list[dict]:
    """Return waves queued for this user/session and mark them delivered."""
    _ensure_waves_table(conn)
    rows = conn.execute("""
        SELECT * FROM user_waves
        WHERE status = 'pending'
          AND expires_at >= ?
          AND (
              (to_username IS NOT NULL AND to_username != '' AND LOWER(to_username) = LOWER(?))
              OR (to_session_id IS NOT NULL AND to_session_id != '' AND to_session_id IN (?, ?))
          )
        ORDER BY created_at ASC
    """, (now, username, session_id, _public_presence_id(session_id))).fetchall()
    if not rows:
        return []
    waves = [{
        "id": w["id"],
        "fromUsername": w["from_username"],
        "fromDisplayName": w["from_display_name"],
        "fromAvatarUrl": w["from_avatar_url"] or "",
        "toUsername": w["to_username"],
        "toDisplayName": w["to_display_name"] or "",
        "toSessionId": w["to_session_id"] or "",
        "timestamp": w["created_at"] * 1000,
    } for w in rows]
    marks = ",".join(["?"] * len(waves))
    conn.execute(f"UPDATE user_waves SET status = 'delivered' WHERE id IN ({marks})", [w["id"] for w in waves])
    conn.commit()
    return waves


# First match wins, so specific paths come before their parents (e.g. /profile/<name> before /profile).
# Mirrors getActivityForPath in the website; this copy only applies when the client sent no activity.
_PRESENCE_PATH_RULES: tuple[tuple[re.Pattern, str, str], ...] = tuple(
    (re.compile(pattern), status, activity)
    for pattern, status, activity in (
        (r"^/interactive-map|^/map/|/map$", "on_island", "Scanning the Island Radar Map"),
        (r"^/order", "ordering", "Crafting Order Bot Item Bag"),
        (r"^/drop", "online", "Setting up a Drop Bot drop"),
        (r"^/pocket-guide", "online", "Checking their Pocket Guide"),
        (r"^/critters", "online", "Hunting critters for the Critterpedia"),
        (r"^/events", "online", "Checking seasonal events & birthdays"),
        (r"^/npcs", "online", "Meeting special NPC visitors"),
        (r"^/flowers", "online", "Breeding hybrid flowers"),
        (r"^/guides", "online", "Reading ACNH guides"),
        (r"^/(trip-planner|planner)", "online", "Planning Island Route & Maps"),
        (r"^/command-builder", "online", "Building item commands"),
        (r"^/wishlist", "online", "Curating a wishlist"),
        (r"^/my-collection", "online", "Tracking their collection"),
        (r"^/(pockets|pocket-inventory)", "online", "Packing their pockets"),
        (r"^/villager", "online", "Looking up a villager"),
        (r"^/item/", "online", "Inspecting a catalogue item"),
        (r"^/(u|profile)/[^/]", "online", "Admiring a Resident Passport"),
        (r"^/profile", "online", "Editing Resident Passport Studio"),
        (r"^/catalog", "online", "Browsing 40,000+ ACNH Catalogue"),
        (r"^/find", "online", "Searching Island Stock Finder"),
        (r"^/(membership|tiers)", "online", "Exploring membership tiers"),
        (r"^/jukebox", "online", "Listening to K.K. Slider Airchecks"),
    )
)


def _presence_for_path(path: str, current_island: str, client_activity: str) -> tuple[str, str]:
    """(status, activity) for a presence heartbeat. A client-supplied activity text always wins."""
    clean = (path or "/").split("?", 1)[0].split("#", 1)[0] or "/"
    if current_island or re.match(r"^/islands?(/|$)", clean):
        if current_island:
            return "on_island", client_activity or f"Exploring {current_island}"
        # /island/<id> and /islands/<id> are one island; plain /islands is the flight board
        specific = re.match(r"^/islands?/([^/]+)/?$", clean)
        if specific:
            return "on_island", client_activity or f"Visiting {specific.group(1).upper()}"
        if re.match(r"^/islands/[^/]+/map", clean):
            return "on_island", client_activity or "Scanning the Island Radar Map"
        return "on_island", client_activity or "Viewing Treasure Islands"
    for pattern, status, activity in _PRESENCE_PATH_RULES:
        if pattern.search(clean):
            return status, client_activity or activity
    return "online", client_activity or "Browsing ChoPaeng"


@app.route("/api/presence/heartbeat", methods=["POST"])
@app.route("/api/community/heartbeat", methods=["POST"])
def api_presence_heartbeat():
    """Register or refresh active user presence session."""
    data = request.get_json(silent=True) or {}
    current_path = str(data.get("path") or data.get("currentPath") or "/").strip()
    client_activity = str(data.get("activity") or data.get("currentActivity") or "").strip()
    session_id = str(data.get("sessionId") or request.headers.get("x-session-id") or "").strip()
    current_island = str(data.get("currentIsland") or "").strip()
    is_idle = bool(data.get("idle"))
    passive = bool(data.get("passive"))
    viewing_item = _clean_item_key(data.get("viewingItem"))
    ordering_items = _clean_ordering_items(data.get("orderingItems"))
    ordering_json = json.dumps(ordering_items) if ordering_items else ""

    now = int(time.time())
    auth_user = _current_auth_user()
    session_id = _presence_session_for(session_id, auth_user)
    conn = get_db()
    try:
        _ensure_presence_table(conn)

        # 1. Prune expired sessions (throttled, not on every beat)
        _prune_presence(conn, now)

        # 2. Extract identity
        tier_role_id, staff_role_id = _presence_role_ids(auth_user)
        if auth_user:
            user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or auth_user.get("id") or "")
            username = str(auth_user.get("username") or auth_user.get("discord_name") or "")
            display_name = str(auth_user.get("nickname") or auth_user.get("name") or username)
            avatar_url = str(auth_user.get("avatar") or auth_user.get("avatar_url") or "")
            is_admin = bool(auth_user.get("is_admin"))
            is_mod = bool(auth_user.get("is_mod"))
            role = "admin" if is_admin else "mod" if is_mod else "member"
            is_guest = 0
            from api.sub_islands import get_user_paid_tier, is_server_booster
            # Real (paid) tier roles only: neither the bare "is_subscriber" flag nor the Server
            # Booster perk may show up as a ChoTato tier. Boosters get their own flag.
            sub_tier = int(get_user_paid_tier(auth_user) or 0)
            is_subscriber = 1 if sub_tier else 0
            is_booster = 1 if (not sub_tier and is_server_booster(auth_user)) else 0
            if not session_id:
                session_id = f"usr_{user_id}"

            # Check passport for enrichments
            ign = str(data.get("ign") or "").strip()
            island_name = str(data.get("islandName") or "").strip()
            native_fruit = str(data.get("nativeFruit") or "Peach").strip()
            has_public_passport = 0

            _ensure_passport_columns(conn)
            pass_row = conn.execute(
                "SELECT * FROM user_public_passports WHERE user_id = ? OR LOWER(username) = LOWER(?)",
                (user_id, username)
            ).fetchone()
            if pass_row:
                # Keep the passport's crown in step with the user's current subscription.
                if int(pass_row["sub_tier"] or 0) != sub_tier:
                    conn.execute(
                        "UPDATE user_public_passports SET sub_tier = ? WHERE user_id = ?", (sub_tier, user_id)
                    )
                has_public_passport = int(bool(pass_row["is_public"]))
                if not ign:
                    ign = pass_row["primary_ign"] or ""
                if not island_name:
                    island_name = pass_row["primary_island"] or ""
                if pass_row["native_fruit"]:
                    native_fruit = pass_row["native_fruit"]
                if not avatar_url and pass_row["avatar_url"]:
                    avatar_url = pass_row["avatar_url"]

            # No passport IGN: fall back to the user's default saved character (the one the Discord nickname uses)
            if not ign or not island_name:
                try:
                    from utils.unified_auth import resolve_all_user_ids
                    cand_ids = resolve_all_user_ids(auth_user) or [user_id]
                    char_row = conn.execute(
                        f"SELECT ign, island_name FROM user_saved_characters WHERE user_id IN ({','.join('?' for _ in cand_ids)}) "
                        "ORDER BY is_default DESC, updated_at DESC LIMIT 1",
                        tuple(cand_ids),
                    ).fetchone()
                    if char_row:
                        if not ign:
                            ign = str(char_row["ign"] or "").strip()
                        if not island_name:
                            island_name = str(char_row["island_name"] or "").strip()
                except Exception as char_err:
                    logger.debug("[Presence] Saved character lookup failed: %s", char_err)
        else:
            ip = _visitor_ip() or "127.0.0.1"
            ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:12]
            if not session_id:
                session_id = f"guest_{ip_hash}_{now}"
            user_id = None
            short_id = session_id[-4:] if len(session_id) >= 4 else "0001"
            username = f"Resident #{short_id}"
            display_name = username
            avatar_url = "https://acnhcdn.com/latest/NpcIcon/der00.png"
            role = "resident"
            is_guest = 1
            is_subscriber = 0
            sub_tier = 0
            is_booster = 0
            ign = str(data.get("ign") or "").strip()
            island_name = str(data.get("islandName") or "").strip()
            native_fruit = str(data.get("nativeFruit") or "Peach").strip()
            has_public_passport = 0

        # Determine status & activity
        status, activity = _presence_for_path(current_path, current_island, client_activity)
        if is_idle:
            status = "idle"

        previous = conn.execute(
            "SELECT status, current_activity, `current_path`, current_island, viewing_item, ordering_items "
            "FROM user_online_presence WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        # A passive beat comes from a background tab while another tab of the same browser is in
        # front: it only keeps the session alive, so the two tabs don't fight over what we show.
        if passive and previous is not None:
            status = previous["status"] or status
            activity = previous["current_activity"] or activity
            current_path = previous["current_path"] or current_path
            current_island = previous["current_island"] or ""
            viewing_item = _clean_item_key(previous["viewing_item"])
            ordering_json = previous["ordering_items"] or ""

        # UPSERT active session
        conn.execute("""
            INSERT INTO user_online_presence (
                session_id, user_id, username, display_name, avatar_url, role, status,
                current_activity, `current_path`, current_island, ign, island_name,
                native_fruit, has_public_passport, is_guest, ip_hash, ip_address,
                viewing_item, ordering_items, is_subscriber, sub_tier, is_booster, tier_role_id, staff_role_id,
                last_heartbeat, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_id = excluded.user_id,
                username = excluded.username,
                display_name = excluded.display_name,
                avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), user_online_presence.avatar_url),
                role = excluded.role,
                status = excluded.status,
                current_activity = excluded.current_activity,
                `current_path` = excluded.`current_path`,
                current_island = excluded.current_island,
                ign = COALESCE(NULLIF(excluded.ign, ''), user_online_presence.ign),
                island_name = COALESCE(NULLIF(excluded.island_name, ''), user_online_presence.island_name),
                native_fruit = COALESCE(NULLIF(excluded.native_fruit, ''), user_online_presence.native_fruit),
                has_public_passport = excluded.has_public_passport,
                is_guest = excluded.is_guest,
                ip_address = excluded.ip_address,
                viewing_item = excluded.viewing_item,
                ordering_items = excluded.ordering_items,
                is_subscriber = excluded.is_subscriber,
                sub_tier = excluded.sub_tier,
                is_booster = excluded.is_booster,
                tier_role_id = excluded.tier_role_id,
                staff_role_id = excluded.staff_role_id,
                -- Restart the clock on going idle or coming back, so "since" means this stretch.
                created_at = CASE
                    WHEN (user_online_presence.status = 'idle') != (excluded.status = 'idle')
                    THEN excluded.created_at
                    ELSE user_online_presence.created_at
                END,
                last_heartbeat = excluded.last_heartbeat
        """, (
            session_id, user_id, username, display_name, avatar_url, role, status,
            activity, current_path, current_island, ign, island_name,
            native_fruit, has_public_passport, is_guest,
            hashlib.sha256((_visitor_ip() or "127.0.0.1").encode()).hexdigest()[:16],
            _visitor_ip()[:64],
            viewing_item, ordering_json, is_subscriber, sub_tier, is_booster,
            tier_role_id or None, staff_role_id or None,
            now, now
        ))
        conn.commit()

        # A new session is a visit; a status/island change reshapes the radar.
        if previous is None:
            _record_site_visit(conn, now)
            conn.commit()
        if (
            previous is None
            or previous["status"] != status
            or (previous["current_island"] or "") != current_island
            or _clean_item_key(previous["viewing_item"]) != viewing_item
            or (previous["ordering_items"] or "") != ordering_json
        ):
            _invalidate_presence_cache()
            presence_hub.broadcast_radar(_build_radar_payload)

        incoming_waves = []
        try:
            incoming_waves = _claim_pending_waves(conn, now, username, session_id)
        except Exception as wave_err:
            logger.warning("[Presence] Error checking waves on heartbeat: %s", wave_err)

        online_count = len(_presence_people(conn, now))

        return jsonify({
            "ok": True,
            "success": True,
            "sessionId": session_id,
            "online_count": online_count,
            "activeOnlineCount": online_count,
            "pending_waves": incoming_waves,
            "stream_clients": presence_hub.subscriber_count,
            "timestamp": now,
        })
    except Exception as exc:
        logger.warning("[Presence] Error recording heartbeat: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/presence/leave", methods=["POST"])
@app.route("/api/community/leave", methods=["POST"])
def api_presence_leave():
    """Remove active presence session when user navigates away or closes tab."""
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("sessionId") or request.headers.get("x-session-id") or request.args.get("sessionId") or "").strip()
    auth_user = _current_auth_user()
    session_id = _presence_session_for(session_id, auth_user)
    if not session_id and auth_user:
        uid = str(auth_user.get("user_id") or auth_user.get("discord_id") or "")
        session_id = f"usr_{uid}" if uid else ""

    if session_id:
        conn = get_db()
        try:
            # Only ever touch the exact session; never match by user id from client input.
            # pagehide also fires on reload, so instead of deleting, age the row to expire after a
            # short grace: a reload beats again within it (no offline blink, no extra visit), a real
            # close drops out when the sweeper next runs.
            expire_at = int(time.time()) - PRESENCE_ACTIVE_SECONDS + PRESENCE_LEAVE_GRACE_SECONDS
            conn.execute(
                "UPDATE user_online_presence SET last_heartbeat = "
                "CASE WHEN last_heartbeat > ? THEN ? ELSE last_heartbeat END WHERE session_id = ?",
                (expire_at, expire_at, session_id),
            )
            conn.commit()
            _ensure_presence_sweeper()
        except Exception:
            pass
        finally:
            conn.close()

    return jsonify({"ok": True, "success": True})


FRIENDS_MAX = 200


def _ensure_friends_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_friends (
            user_id         VARCHAR(64) NOT NULL,
            friend_username VARCHAR(255) NOT NULL,
            created_at      INTEGER NOT NULL,
            PRIMARY KEY (user_id, friend_username)
        )
    """)


def _friend_usernames(conn, user_id: str) -> set[str]:
    if not user_id:
        return set()
    _ensure_friends_table(conn)
    rows = conn.execute("SELECT friend_username FROM user_friends WHERE user_id = ?", (user_id,)).fetchall()
    return {str(r["friend_username"]).lower() for r in rows}


def _auth_user_id(auth_user: dict | None) -> str:
    if not auth_user:
        return ""
    return str(auth_user.get("user_id") or auth_user.get("discord_id") or auth_user.get("id") or "").strip()


@app.route("/api/user/friends", methods=["GET", "POST", "DELETE"])
def api_user_friends():
    """One-way friends list: residents you follow so their online status is easy to spot."""
    auth_user = _current_auth_user()
    user_id = _auth_user_id(auth_user)
    if not user_id:
        return jsonify({"ok": False, "error": "Authentication required"}), 401

    conn = get_db()
    try:
        _ensure_friends_table(conn)
        if request.method == "GET":
            return jsonify({"ok": True, "friends": sorted(_friend_usernames(conn, user_id))})

        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or request.args.get("username") or "").strip().lower()
        if not username or len(username) > 64:
            return jsonify({"ok": False, "error": "A valid username is required"}), 400

        if request.method == "POST":
            own = str(auth_user.get("username") or auth_user.get("discord_name") or "").strip().lower()
            if username == own:
                return jsonify({"ok": False, "error": "You cannot add yourself"}), 400
            friends = _friend_usernames(conn, user_id)
            if username not in friends:
                if len(friends) >= FRIENDS_MAX:
                    return jsonify({"ok": False, "error": f"Friends list is full ({FRIENDS_MAX})"}), 400
                conn.execute(
                    "INSERT INTO user_friends (user_id, friend_username, created_at) VALUES (?, ?, ?)",
                    (user_id, username, int(time.time())),
                )
                conn.commit()
        else:
            conn.execute(
                "DELETE FROM user_friends WHERE user_id = ? AND friend_username = ?", (user_id, username)
            )
            conn.commit()
        return jsonify({"ok": True, "friends": sorted(_friend_usernames(conn, user_id))})
    except Exception as exc:
        logger.warning("[Friends] Error handling friends request: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/presence/online", methods=["GET"])
@app.route("/api/community/online", methods=["GET"])
def api_presence_online():
    """Retrieve currently active residents for Who's Online roster."""
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_presence_table(conn)
        _prune_presence(conn, now)

        people = _presence_people(conn, now)
        viewer = _current_auth_user()
        signed_in = bool(_auth_user_id(viewer))
        viewer_session = _presence_session_for(
            str(request.headers.get("x-session-id") or request.args.get("sessionId") or "").strip(),
            viewer,
        )
        viewer_uid = str((viewer or {}).get("user_id") or (viewer or {}).get("discord_id") or "")

        def to_resident(r: dict) -> dict:
            minutes_ago = max(0, int((now - (r["created_at"] or now)) / 60))
            is_self = (bool(viewer_session) and r["session_id"] == viewer_session) or (
                bool(viewer_uid) and str(r.get("user_id") or "") == viewer_uid
            )
            return {
                "id": _public_presence_id(r["session_id"]),
                "isSelf": is_self,
                "username": r["username"],
                "displayName": r["display_name"] or r["username"],
                "avatarUrl": r["avatar_url"] or "https://acnhcdn.com/latest/NpcIcon/der00.png",
                "ign": r["ign"] or "",
                "islandName": r["island_name"] or "",
                "nativeFruit": r["native_fruit"] or "Peach",
                "role": r["role"] or "resident",
                "status": r["status"] or "online",
                "currentActivity": r["current_activity"] or "Browsing ChoPaeng",
                "currentIsland": r["current_island"] or "",
                "currentPath": r["current_path"] or "/",
                "hasPublicPassport": bool(r["has_public_passport"]),
                "isGuest": bool(r["is_guest"]),
                "isSubscriber": int(r.get("sub_tier") or 0) > 0,
                "subTier": int(r.get("sub_tier") or 0),
                "isBooster": bool(r.get("is_booster")) and not int(r.get("sub_tier") or 0),
                "tierRoleId": r.get("tier_role_id") or None,
                "staffRoleId": r.get("staff_role_id") or None,
                "joinedMinutesAgo": minutes_ago,
                "lastHeartbeat": r["last_heartbeat"],
            }

        # Who is online is for signed-in members only; signed-out visitors get the counts.
        residents = [to_resident(r) for r in people[:PRESENCE_ROSTER_LIMIT]] if signed_in else []

        # Friends are returned in full so one past the roster cap is not missed.
        friend_names = _friend_usernames(conn, viewer_uid) if signed_in else set()
        friends_online = [
            to_resident(r) for r in people
            if not r.get("is_guest") and str(r.get("username") or "").lower() in friend_names
        ]

        radar = _radar_summary(people)
        if not signed_in:
            radar = redact_radar(radar)
        return jsonify({
            "ok": True,
            "success": True,
            # Real totals: the roster is capped, the counts are not.
            "total_online": radar["totalOnline"],
            "activeOnlineCount": radar["totalOnline"],
            "authenticated_count": radar["authenticatedCount"],
            "guest_count": radar["guestCount"],
            "roster_truncated": signed_in and radar["totalOnline"] > len(residents),
            "roster_requires_auth": not signed_in,
            "residents": residents,
            "friends_online": friends_online,
            "radar": radar,
            "traffic": _site_traffic(conn, now),
            "timestamp": now,
        })
    except Exception as exc:
        logger.warning("[Presence] Error fetching online presence: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc), "residents": []}), 500
    finally:
        conn.close()


@app.route("/api/presence/radar", methods=["GET"])
@app.route("/api/community/radar", methods=["GET"])
def api_presence_radar():
    """Compact live radar (counts + per-island occupancy). Cheap fallback when SSE is unavailable."""
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_presence_table(conn)
        _prune_presence(conn, now)
        radar = _radar_summary(_presence_people(conn, now))
        if not _auth_user_id(_current_auth_user()):
            radar = redact_radar(radar)
        return jsonify({
            "ok": True,
            "radar": radar,
            "traffic": _site_traffic(conn, now),
            "timestamp": now,
        })
    except Exception as exc:
        logger.warning("[Presence] Error building radar: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/presence/stream", methods=["GET"])
@app.route("/api/community/stream", methods=["GET"])
def api_presence_stream():
    """Server-Sent Events: pushes waves and radar changes so clients need not poll.

    Clients read it with fetch() so the Authorization header never ends up in a URL.
    """
    now = int(time.time())
    auth_user = _current_auth_user()
    session_id = _presence_session_for(
        str(request.headers.get("x-session-id") or request.args.get("sessionId") or "").strip(),
        auth_user,
    )
    username = ""
    if auth_user:
        username = str(auth_user.get("username") or auth_user.get("discord_name") or "").strip()
    keys = [k for k in (session_id, _public_presence_id(session_id) if session_id else "", username) if k]
    if not keys:
        return jsonify({"ok": False, "error": "Session required"}), 400

    if session_id and presence_hub.count_for(session_id) >= PRESENCE_STREAMS_PER_SESSION:
        return jsonify({"ok": False, "error": "Too many open streams for this session"}), 429
    signed_in = bool(_auth_user_id(auth_user))
    sub = presence_hub.subscribe(keys, authed=signed_in)
    _ensure_presence_sweeper()
    if sub is None:
        return jsonify({"ok": False, "error": "Stream capacity reached"}), 503

    conn = get_db()
    try:
        _ensure_presence_table(conn)
        radar = _radar_summary(_presence_people(conn, now))
        hello = {
            "radar": radar if signed_in else redact_radar(radar),
            "traffic": _site_traffic(conn, now),
            "waves": _claim_pending_waves(conn, now, username, session_id),
        }
    except Exception as exc:
        presence_hub.unsubscribe(sub)
        logger.warning("[Presence] Error opening stream: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()

    def generate():
        deadline = time.time() + PRESENCE_STREAM_MAX_SECONDS
        try:
            yield "retry: 5000\n\n"
            yield format_sse("hello", hello)
            while time.time() < deadline:
                try:
                    event, data = sub.queue.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield format_sse(event, data)
            yield format_sse("reconnect", {})
        finally:
            presence_hub.unsubscribe(sub)

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/presence/wave", methods=["POST"])
@app.route("/api/community/wave", methods=["POST"])
def api_presence_send_wave():
    """Receive an incoming wave directed to another user/resident."""
    data = request.get_json(silent=True) or {}
    to_username = str(data.get("toUsername") or data.get("to_username") or "").strip()
    to_display_name = str(data.get("toDisplayName") or data.get("to_display_name") or "").strip()
    to_session_id = str(data.get("toSessionId") or data.get("to_session_id") or "").strip()

    if not to_username and not to_session_id:
        return jsonify({"ok": False, "error": "Target user or session is required"}), 400

    auth_user = _current_auth_user()
    if not _auth_user_id(auth_user):
        return jsonify({"ok": False, "error": "Sign in to wave at residents"}), 401
    now = int(time.time())

    # Sender identity comes only from the signed-in account, never the request body.
    from_username = str(auth_user.get("username") or auth_user.get("discord_name") or "").strip()
    from_display_name = str(auth_user.get("nickname") or auth_user.get("name") or from_username).strip()
    from_avatar_url = str(auth_user.get("avatar") or auth_user.get("avatar_url") or "").strip()
    if not from_username:
        return jsonify({"ok": False, "error": "Your account has no username"}), 400
    if not from_display_name:
        from_display_name = from_username
    if not from_avatar_url:
        from_avatar_url = "https://acnhcdn.com/latest/NpcIcon/der00.png"

    wave_id = "wave_" + hashlib.sha256(f"{from_username}:{to_username}:{to_session_id}:{now}:{time.time()}".encode()).hexdigest()[:16]
    expires_at = now + 120  # Wave valid for 2 minutes

    conn = get_db()
    try:
        _ensure_waves_table(conn)
        conn.execute("""
            INSERT INTO user_waves (
                id, from_username, from_display_name, from_avatar_url,
                to_username, to_display_name, to_session_id, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            wave_id, from_username, from_display_name, from_avatar_url,
            to_username, to_display_name, to_session_id, now, expires_at
        ))
        conn.commit()

        wave_payload = {
            "id": wave_id,
            "fromUsername": from_username,
            "fromDisplayName": from_display_name,
            "fromAvatarUrl": from_avatar_url,
            "toUsername": to_username,
            "toDisplayName": to_display_name,
            "toSessionId": to_session_id,
            "timestamp": now * 1000,
        }
        # Live listeners get it instantly; otherwise it stays pending for the next heartbeat.
        if presence_hub.publish_to([to_username, to_session_id], "wave", wave_payload):
            conn.execute("UPDATE user_waves SET status = 'delivered' WHERE id = ?", (wave_id,))
            conn.commit()
        return jsonify({
            "ok": True,
            "success": True,
            "wave": wave_payload,
        })
    except Exception as exc:
        logger.warning("[Presence] Error storing wave: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/presence/waves", methods=["GET"])
@app.route("/api/community/waves", methods=["GET"])
def api_presence_poll_waves():
    """Poll for pending waves directed to this user/session."""
    auth_user = _current_auth_user()
    now = int(time.time())
    session_id = _presence_session_for(
        str(request.headers.get("x-session-id") or request.args.get("sessionId") or "").strip(),
        auth_user,
    )
    username = ""
    if auth_user:
        username = str(auth_user.get("username") or auth_user.get("discord_name") or "").strip()

    if not username and not session_id:
        return jsonify({"ok": True, "waves": []})

    conn = get_db()
    try:
        waves = _claim_pending_waves(conn, now, username, session_id)

        return jsonify({"ok": True, "success": True, "waves": waves})
    except Exception as exc:
        logger.warning("[Presence] Error polling waves: %s", exc)
        return jsonify({"ok": False, "waves": []})
    finally:
        conn.close()


# -------------------------------------------------------------
# ISLAND MAP & SECTOR RADAR ROUTES (Powered by HttpApiServer.cs)
# -------------------------------------------------------------

from utils.nhl_map_parser import convert_sysbot_map_data as _convert_sysbot_map_data_to_website


def _resolve_target_island_and_client(island_param: Optional[str] = None):
    """
    Intelligently resolves the target island name, target SysBot URL, and client instance.
    Checks:
    1. island_param or query args (?island=..., ?name=..., ?port=...)
    2. Host header port (e.g. localhost:8220 -> Adhika on port 8220)
    3. Numeric port input (e.g. island=8220 -> Adhika)
    4. Active / configured SysBot instances or Config defaults
    """
    from utils.sysbot_api import SysBotClient, SysBotClientPool

    raw_target = (
        (island_param or "")
        or request.args.get("island")
        or request.args.get("name")
        or request.args.get("port")
        or ""
    ).strip()

    # Check host header port (e.g. localhost:8220 or 127.0.0.1:8220)
    host_port = None
    if request.host and ":" in request.host:
        try:
            host_port = int(request.host.split(":")[-1])
        except (ValueError, TypeError):
            host_port = None

    resolved_island = None
    target_url = None

    if raw_target:
        if raw_target.isdigit():
            port_num = int(raw_target)
            resolved_island = Config.get_island_name_from_port(port_num) or f"Port_{port_num}"
            target_url = f"http://localhost:{port_num}"
        elif raw_target.lower().startswith(("http://", "https://")):
            target_url = raw_target.rstrip("/")
            resolved_island = "Direct"
        else:
            resolved_island = raw_target
            target_url = Config.get_island_sysbot_url(raw_target)
    elif host_port and (8201 <= host_port <= 8248):
        resolved_island = Config.get_island_name_from_port(host_port)
        target_url = f"http://localhost:{host_port}"
    else:
        resolved_island = getattr(Config, "DEFAULT_ISLAND", None)
        target_url = Config.get_island_sysbot_url(resolved_island) if resolved_island else None

    try:
        client = SysBotClientPool.get_client(resolved_island) if resolved_island else None
    except ValueError:
        client = None  # not a configured island: no SysBot to ask
    return resolved_island, target_url, client


@app.route("/api/islands/<island_name>/map", methods=["GET"])
def api_get_island_map(island_name: str):
    """
    Returns full parsed item and sector layout for an island.
    Primary: Queries HttpApiServer.cs GET /api/island/map/data from Switch RAM.
    Fallback: Reads local .nhl file if SysBot is offline or unreachable.
    """
    from utils.cache import map_cache
    force = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    resolved_island, target_url, client = _resolve_target_island_and_client(island_name)
    if not resolved_island or str(resolved_island).strip().lower() in ("all", "*"):
        return jsonify({
            "ok": False,
            "error": "Island name is required (e.g. /api/islands/<island_name>/map or ?island=<name>).",
        }), 400

    norm = resolved_island.strip().lower()
    cache_key = f"island_map_unified:{norm}"

    if not force:
        cached_val = map_cache.get(cache_key)
        if cached_val is not None:
            return jsonify(cached_val)

    # 1. Primary: query SysBot HttpApiServer.cs live RAM map data
    try:
        if client and client.base_url:
            data = client.get_island_map_data_sync()
            if isinstance(data, dict) and data.get("dots") is not None and data.get("names") is not None:
                converted = _convert_sysbot_map_data_to_website(data, resolved_island)
                map_cache.set(cache_key, converted, ttl=30.0)
                return jsonify(converted)
    except Exception as exc:
        logging.getLogger("flask_api").debug(f"SysBot HttpApiServer live map data query note for {resolved_island}: {exc}")

    # SysBot is the only map source (no .nhl files on disk).
    return jsonify({
        "ok": False,
        "island": resolved_island,
        "status": "offline",
        "error": "Island map is unavailable: SysBot is offline or unreachable.",
        "items": [],
        "sectors": {},
        "sector_summary": {},
    }), 503


# Seconds a successful map/data response may be reused by Cloudflare's edge. The origin's uplink is
# slow (~3-10 KB/s), so the first viewer pays the download and later viewers get the edge copy.
MAP_DATA_EDGE_CACHE_SECONDS = 30


def _public_map_data(data: Any) -> Any:
    """
    Map data minus the visitors: their tiles are per-viewer (only those who could see the Dodo
    code may see them) and the map draws pins from /api/players/live anyway. Without them the
    response is identical for everyone, so Cloudflare can cache it.
    """
    if isinstance(data, dict) and "players" in data:
        data = {k: v for k, v in data.items() if k != "players"}
    return data


def _map_data_response(data: Any, *, cacheable: bool):
    resp = jsonify(data)
    resp.headers["Cache-Control"] = (
        f"public, max-age=0, s-maxage={MAP_DATA_EDGE_CACHE_SECONDS}" if cacheable else "no-store"
    )
    return resp


@app.route("/api/island/map/data", methods=["GET"])
@app.route("/api/map/data", methods=["GET"])
@app.route("/api/islands/<island_name>/map/data", methods=["GET"])
@app.route("/api/island/<island_name>/map/data", methods=["GET"])
def api_get_island_map_data_live(island_name: Optional[str] = None):
    """
    Returns live compact map data (RAM or cached).
    Direct proxy/caching for HttpApiServer.cs GET /api/island/map/data.
    """
    from utils.cache import map_cache
    resolved_island, target_url, client = _resolve_target_island_and_client(island_name)

    # When no island is specified or ?island=all, return all 48 islands
    if not resolved_island or str(resolved_island).strip().lower() in ("all", "*"):
        viewer = _current_auth_user()
        viewer_is_mod = bool(viewer and (viewer.get("is_admin") or viewer.get("is_mod") or _is_mod(viewer.get("roles", []))))
        base_port = int(getattr(Config, "SYSBOT_BASE_PORT", 8201) or 8201)
        telemetry = {}
        try:
            from utils.sysbot_telemetry import get_all_islands_health
            telemetry = get_all_islands_health()
        except Exception:
            pass

        all_islands_data = []
        for idx, name in enumerate(Config.ALL_ISLAND_PORT_ORDER):
            port = base_port + idx
            category = "sub" if idx < 20 else ("free" if idx < 47 else "order_bot")
            health = telemetry.get(name.lower(), {})
            all_islands_data.append({
                "name": name,
                "category": category,
                "is_online": health.get("is_online", False),
                "visitors_count": health.get("visitors_count", 0),
                **({"port": port, "sysbot_url": Config.get_island_sysbot_url(name)} if viewer_is_mod else {}),
                "map_data_url": f"/api/islands/{name}/map/data",
                "map_url": f"/api/islands/{name}/map",
            })
        # Mods see SysBot ports here, so this listing must never be shared through the edge cache.
        return _map_data_response({
            "ok": True,
            "success": True,
            "total_islands": len(all_islands_data),
            "count": len(all_islands_data),
            "islands": all_islands_data,
        }, cacheable=False)

    force = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    fmt = request.args.get("format", "").lower()
    norm = resolved_island.strip().lower()
    cache_key = f"live_map_data:{norm}:{fmt}"

    if not force:
        cached_val = map_cache.get(cache_key)
        if cached_val is not None:
            return _map_data_response(cached_val, cacheable=True)

    # 1. Primary: Query SysBot HttpApiServer.cs live RAM
    try:
        if client and client.base_url:
            data = client.get_island_map_data_sync()
            if isinstance(data, dict) and data.get("dots") is not None and data.get("names") is not None:
                if fmt in ("website", "unified", "modal"):
                    data = _convert_sysbot_map_data_to_website(data, resolved_island)
                data = _public_map_data(data)
                map_cache.set(cache_key, data, ttl=30.0)
                return _map_data_response(data, cacheable=not force)
    except Exception as exc:
        logging.getLogger("flask_api").debug(f"SysBot live map data fetch note for {resolved_island}: {exc}")

    # SysBot is the only map source (no .nhl files on disk).
    return _map_data_response({
        "success": False,
        "error": "Island map is unavailable: SysBot is offline or unreachable.",
        "island_name": resolved_island,
    }, cacheable=False), 503


@app.route("/api/islands/<island_name>/map/search", methods=["GET"])
def api_search_island_map(island_name: str):
    """
    Searches items or categories on an island map to return exact coordinates and sectors.
    """
    from utils.nhl_map_parser import search_island_items
    q = request.args.get("q", "").strip()
    result = search_island_items(island_name, q)
    return jsonify({"ok": True, **result})


# @app.route("/api/listing/", methods=["GET"])
# @app.route("/api/listing/<path:subpath>", methods=["GET"])
# def api_directory_listing(subpath=""):
#     """
#     List directory contents starting from C:/.

#     Query params:
#       - (none) — list C:/ root or the given subpath

#     Access is restricted to admin or mod users.
#     """

#     # Build the resolved path, always rooted at C:/
#     base_root = "C:\\"
#     if subpath:
#         # Normalise and prevent traversal outside C:/
#         requested = os.path.normpath(os.path.join(base_root, subpath.replace("/", os.sep)))
#         if not requested.startswith(base_root):
#             return jsonify({"ok": False, "error": "Path traversal not allowed"}), 400
#         target = requested
#     else:
#         target = base_root

#     if not os.path.exists(target):
#         return jsonify({"ok": False, "error": "Path not found"}), 404

#     if not os.path.isdir(target):
#         return jsonify({"ok": False, "error": "Path is not a directory"}), 400

#     entries = []
#     try:
#         for entry in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
#             try:
#                 stat = entry.stat()
#                 entries.append({
#                     "name": entry.name,
#                     "type": "directory" if entry.is_dir() else "file",
#                     "size": stat.st_size if entry.is_file() else None,
#                     "modified": int(stat.st_mtime),
#                 })
#             except (PermissionError, OSError):
#                 entries.append({
#                     "name": entry.name,
#                     "type": "directory" if entry.is_dir() else "file",
#                     "size": None,
#                     "modified": None,
#                     "error": "access_denied",
#                 })
#     except PermissionError:
#         return jsonify({"ok": False, "error": "Permission denied"}), 403

#     return jsonify({
#         "ok": True,
#         "path": target.replace("\\", "/"),
#         "entries": entries,
#         "count": len(entries),
#     })


# @app.route("/api/listing/download/<path:subpath>", methods=["GET"])
# def api_directory_download(subpath):
#     """
#     Download a file from the server filesystem, rooted at C:/.

#     Path params:
#       - subpath — relative path from C:/ to the file (e.g. Users/bitress/Desktop/file.txt)

#     Access is restricted to admin or mod users.
#     """
#     from flask import send_file

#     base_root = "C:\\"
#     subpath_clean = re.sub(r'^[A-Za-z]:[/\\]?', '', subpath).replace("/", os.sep)
#     requested = os.path.normpath(os.path.join(base_root, subpath_clean))

#     if not requested.startswith(base_root):
#         return jsonify({"ok": False, "error": "Path traversal not allowed"}), 400

#     if not os.path.exists(requested):
#         return jsonify({"ok": False, "error": "Path not found"}), 404

#     if not os.path.isfile(requested):
#         return jsonify({"ok": False, "error": "Path is not a file"}), 400

#     try:
#         return send_file(
#             requested,
#             as_attachment=True,
#             download_name=os.path.basename(requested),
#         )
#     except PermissionError:
#         return jsonify({"ok": False, "error": "Permission denied"}), 403
#     except OSError as e:
#         return jsonify({"ok": False, "error": _safe_error(e)}), 500


@app.route("/api/islands/maps", methods=["GET"])
def api_list_island_maps():
    """Lists islands whose live map is currently available from SysBot."""
    from utils.nhl_map_parser import get_island_map_telemetry
    telem = get_island_map_telemetry()
    return jsonify({"ok": True, **telem})



def _warm_stream_status() -> None:
    """Fill the Twitch/YouTube status caches at startup so no visitor waits on the first lookup."""
    try:
        from utils.twitch_api import get_stream_status
        from utils.youtube_live import get_live_status
        get_stream_status()
        get_live_status()
    except Exception as exc:  # both never raise, but a warm-up must never take the server down
        logger.debug("Stream status warm-up skipped: %s", exc)


def run_flask_app(host='0.0.0.0', port=8100):
    """Run Flask app with retry logic for port binding after OTA restart."""
    logger.info(f"[FLASK] Starting API server on {host}:{port}...")
    threading.Thread(target=_warm_stream_status, name="WarmStreamStatus", daemon=True).start()
    from utils import stall_watchdog
    stall_watchdog.start()
    max_retries = 5
    retry_delay = 3  # seconds between attempts
    for attempt in range(1, max_retries + 1):
        try:
            # ThreadedWSGIServer already sets SO_REUSEADDR before binding.
            # Using it directly (instead of app.run) gives explicit control
            # and allows retrying when the port is still in TIME_WAIT after
            # an os.execv()-based OTA restart.
            server = ThreadedWSGIServer(host, port, app)
            logger.info(f"[FLASK] API server listening on {host}:{port}")
            server.serve_forever()
            return
        except OSError as e:
            if attempt < max_retries:
                logger.warning(
                    f"[FLASK] Port {port} not available (attempt {attempt}/{max_retries}): {e}. "
                    f"Retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    f"[FLASK] Failed to bind to port {port} after {max_retries} attempts: {e}"
                )
                raise


# ============================================================================
# USER COLLECTION, WISHLIST, POCKETS, CRITTERS, AND NOTIFICATIONS ENDPOINTS
# ============================================================================

@app.route("/api/user/collection", methods=["GET", "POST", "DELETE"])
@app.route("/api/profile/collection", methods=["GET", "POST", "DELETE"])
def api_user_collection():
    """Get, update, or clear authenticated user item collection."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "User ID not found"}), 401

    conn = get_db()
    try:
        if request.method == "GET":
            rows = conn.execute(
                "SELECT item_id FROM user_collection_items WHERE user_id = ? ORDER BY id ASC",
                (user_id,)
            ).fetchall()
            collection = [r["item_id"] for r in rows if r["item_id"]]
            return jsonify({
                "ok": True,
                "success": True,
                "collection": collection,
                "count": len(collection)
            })

        if request.method == "DELETE":
            conn.execute("DELETE FROM user_collection_items WHERE user_id = ?", (user_id,))
            conn.commit()
            return jsonify({"ok": True, "success": True, "collection": [], "count": 0})

        # POST
        data = request.get_json(silent=True) or {}
        now_iso = datetime.utcnow().isoformat()

        # Batch replacement
        if "collection" in data or isinstance(data, list):
            items = data.get("collection") if isinstance(data, dict) else data
            if isinstance(items, list):
                conn.execute("DELETE FROM user_collection_items WHERE user_id = ?", (user_id,))
                for itm in items:
                    clean_id = str(itm).strip()[:128]
                    if clean_id:
                        conn.execute("""
                            INSERT OR IGNORE INTO user_collection_items (user_id, item_id, created_at)
                            VALUES (?, ?, ?)
                        """, (user_id, clean_id, now_iso))
                conn.commit()
                return jsonify({"ok": True, "success": True, "count": len(items)})

        # Single toggle / set
        item_id = str(data.get("itemId") or data.get("item_id") or data.get("id") or "").strip()[:128]
        if not item_id:
            return jsonify({"ok": False, "error": "item_id is required"}), 400

        is_collected = data.get("collected")
        if is_collected is None:
            is_collected = data.get("isCollected", True)
        is_collected = bool(is_collected)

        if is_collected:
            conn.execute("""
                INSERT OR IGNORE INTO user_collection_items (user_id, item_id, created_at)
                VALUES (?, ?, ?)
            """, (user_id, item_id, now_iso))
        else:
            conn.execute(
                "DELETE FROM user_collection_items WHERE user_id = ? AND item_id = ?",
                (user_id, item_id)
            )
        conn.commit()

        return jsonify({
            "ok": True,
            "success": True,
            "itemId": item_id,
            "collected": is_collected
        })
    except Exception as exc:
        logger.warning("Error in user collection API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/wishlist", methods=["GET", "POST", "DELETE"])
@app.route("/api/profile/wishlist", methods=["GET", "POST", "DELETE"])
@app.route("/api/user/item-favorites", methods=["GET", "POST", "DELETE"])
def api_user_wishlist():
    """Get, update, or clear authenticated user item wishlist / favorites."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "User ID not found"}), 401

    conn = get_db()
    try:
        if request.method == "GET":
            rows = conn.execute(
                "SELECT item_id FROM user_wishlist_items WHERE user_id = ? ORDER BY id ASC",
                (user_id,)
            ).fetchall()
            wishlist = [r["item_id"] for r in rows if r["item_id"]]
            return jsonify({
                "ok": True,
                "success": True,
                "wishlist": wishlist,
                "favorites": wishlist,
                "count": len(wishlist)
            })

        if request.method == "DELETE":
            conn.execute("DELETE FROM user_wishlist_items WHERE user_id = ?", (user_id,))
            conn.commit()
            return jsonify({"ok": True, "success": True, "wishlist": [], "count": 0})

        # POST
        data = request.get_json(silent=True) or {}
        now_iso = datetime.utcnow().isoformat()

        # Batch replacement
        if "wishlist" in data or "favorites" in data or isinstance(data, list):
            items = data.get("wishlist") or data.get("favorites") if isinstance(data, dict) else data
            if isinstance(items, list):
                conn.execute("DELETE FROM user_wishlist_items WHERE user_id = ?", (user_id,))
                for itm in items:
                    clean_id = str(itm).strip()[:128]
                    if clean_id:
                        conn.execute("""
                            INSERT OR IGNORE INTO user_wishlist_items (user_id, item_id, created_at)
                            VALUES (?, ?, ?)
                        """, (user_id, clean_id, now_iso))
                conn.commit()
                return jsonify({"ok": True, "success": True, "count": len(items)})

        # Single toggle / set
        item_id = str(data.get("itemId") or data.get("item_id") or data.get("id") or "").strip()[:128]
        if not item_id:
            return jsonify({"ok": False, "error": "item_id is required"}), 400

        is_fav = data.get("isFavorite")
        if is_fav is None:
            is_fav = data.get("is_favorite")
        if is_fav is None:
            is_fav = data.get("wishlist", True)
        is_fav = bool(is_fav)

        if is_fav:
            conn.execute("""
                INSERT OR IGNORE INTO user_wishlist_items (user_id, item_id, created_at)
                VALUES (?, ?, ?)
            """, (user_id, item_id, now_iso))
        else:
            conn.execute(
                "DELETE FROM user_wishlist_items WHERE user_id = ? AND item_id = ?",
                (user_id, item_id)
            )
        conn.commit()

        return jsonify({
            "ok": True,
            "success": True,
            "itemId": item_id,
            "isFavorite": is_fav
        })
    except Exception as exc:
        logger.warning("Error in user wishlist API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/pockets", methods=["GET", "POST"])
@app.route("/api/profile/pockets", methods=["GET", "POST"])
@app.route("/api/user/pocket-inventory", methods=["GET", "POST"])
def api_user_pockets():
    """Get or save authenticated user pocket inventory (order items & drop items)."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "User ID not found"}), 401

    conn = get_db()
    try:
        if request.method == "GET":
            row = conn.execute(
                "SELECT order_items, drop_items, villager, updated_at FROM user_pocket_states WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if not row:
                return jsonify({
                    "ok": True,
                    "success": True,
                    "orderItems": [],
                    "dropItems": [],
                    "villager": None
                })
            return jsonify({
                "ok": True,
                "success": True,
                "orderItems": json.loads(row["order_items"] or "[]"),
                "dropItems": json.loads(row["drop_items"] or "[]"),
                "villager": row["villager"],
                "updatedAt": row["updated_at"]
            })

        # POST: Save pocket state
        data = request.get_json(silent=True) or {}
        order_items_json = json.dumps(data.get("orderItems") or data.get("order_items") or [])
        drop_items_json = json.dumps(data.get("dropItems") or data.get("drop_items") or [])
        villager = data.get("villager")
        villager_str = str(villager).strip()[:255] if villager else None
        now_iso = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT OR REPLACE INTO user_pocket_states (user_id, order_items, drop_items, villager, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, order_items_json, drop_items_json, villager_str, now_iso))
        conn.commit()

        return jsonify({
            "ok": True,
            "success": True,
            "updatedAt": now_iso
        })
    except Exception as exc:
        logger.warning("Error in user pockets API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/critters", methods=["GET", "POST"])
@app.route("/api/profile/critters", methods=["GET", "POST"])
def api_user_critters():
    """Get or save authenticated user caught critters."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "User ID not found"}), 401

    conn = get_db()
    try:
        hemisphere = (request.args.get("hemisphere") or "north").strip().lower()

        if request.method == "GET":
            rows = conn.execute(
                "SELECT critter_name FROM user_caught_critters WHERE user_id = ? AND hemisphere = ?",
                (user_id, hemisphere)
            ).fetchall()
            caught = [r["critter_name"] for r in rows if r["critter_name"]]
            return jsonify({
                "ok": True,
                "success": True,
                "hemisphere": hemisphere,
                "caught": caught,
                "count": len(caught)
            })

        # POST: Toggle or batch save
        data = request.get_json(silent=True) or {}
        hemi = str(data.get("hemisphere") or hemisphere).strip().lower()
        now_iso = datetime.utcnow().isoformat()

        # Batch replacement
        if "caught" in data or isinstance(data, list):
            items = data.get("caught") if isinstance(data, dict) else data
            if isinstance(items, list):
                conn.execute(
                    "DELETE FROM user_caught_critters WHERE user_id = ? AND hemisphere = ?",
                    (user_id, hemi)
                )
                for itm in items:
                    clean_name = str(itm).strip()[:128]
                    if clean_name:
                        conn.execute("""
                            INSERT OR IGNORE INTO user_caught_critters (user_id, critter_name, hemisphere, caught_at)
                            VALUES (?, ?, ?, ?)
                        """, (user_id, clean_name, hemi, now_iso))
                conn.commit()
                return jsonify({"ok": True, "success": True, "hemisphere": hemi, "count": len(items)})

        # Single toggle / set
        name = str(data.get("name") or data.get("critter_name") or "").strip()[:128]
        if not name:
            return jsonify({"ok": False, "error": "name is required"}), 400

        is_caught = data.get("caught")
        if is_caught is None:
            is_caught = data.get("isCaught", True)
        is_caught = bool(is_caught)

        if is_caught:
            conn.execute("""
                INSERT OR IGNORE INTO user_caught_critters (user_id, critter_name, hemisphere, caught_at)
                VALUES (?, ?, ?, ?)
            """, (user_id, name, hemi, now_iso))
        else:
            conn.execute(
                "DELETE FROM user_caught_critters WHERE user_id = ? AND critter_name = ? AND hemisphere = ?",
                (user_id, name, hemi)
            )
        conn.commit()

        return jsonify({
            "ok": True,
            "success": True,
            "name": name,
            "caught": is_caught,
            "hemisphere": hemi
        })
    except Exception as exc:
        logger.warning("Error in user critters API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


_TRACKER_NAMES = {"todo", "turnips", "visitors"}
_TRACKER_MAX_BYTES = 16 * 1024
_TRACKERS_TABLE_READY = False


def _ensure_trackers_table(conn):
    """Per-user JSON blobs for the Pocket Guide trackers (to-do, turnips, visitors)."""
    global _TRACKERS_TABLE_READY
    if _TRACKERS_TABLE_READY:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_trackers (
            user_id    VARCHAR(64) NOT NULL,
            tracker    VARCHAR(32) NOT NULL,
            data       TEXT NOT NULL,
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (user_id, tracker)
        )
    """)
    conn.commit()
    _TRACKERS_TABLE_READY = True


@app.route("/api/user/trackers/<tracker>", methods=["GET", "POST", "PUT"])
def api_user_tracker(tracker):
    """Get or save one Pocket Guide tracker for the authenticated user (newest write wins)."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "User ID not found"}), 401
    if tracker not in _TRACKER_NAMES:
        return jsonify({"ok": False, "error": "Unknown tracker"}), 404

    conn = get_db()
    try:
        _ensure_trackers_table(conn)
        row = conn.execute(
            "SELECT data, updated_at FROM user_trackers WHERE user_id = ? AND tracker = ?",
            (user_id, tracker),
        ).fetchone()

        if request.method == "GET":
            if not row:
                return jsonify({"ok": True, "success": True, "data": None, "updated_at": 0})
            try:
                data = json.loads(row["data"])
            except ValueError:
                data = None
            return jsonify({"ok": True, "success": True, "data": data, "updated_at": int(row["updated_at"] or 0)})

        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("data"), (dict, list)):
            return jsonify({"ok": False, "error": "data object is required"}), 400
        raw = json.dumps(body["data"], separators=(",", ":"))
        if len(raw.encode("utf-8")) > _TRACKER_MAX_BYTES:
            return jsonify({"ok": False, "error": "Tracker data too large"}), 413

        try:
            incoming_ts = int(body.get("updated_at") or 0)
        except (TypeError, ValueError):
            incoming_ts = 0
        now_ms = int(time.time() * 1000)
        incoming_ts = min(incoming_ts, now_ms + 60_000) if incoming_ts > 0 else now_ms

        current_ts = int(row["updated_at"] or 0) if row else 0
        if incoming_ts < current_ts:
            # A newer copy already exists (another device); tell the client to adopt it.
            return jsonify({"ok": True, "success": True, "applied": False, "updated_at": current_ts})

        conn.execute("""
            INSERT INTO user_trackers (user_id, tracker, data, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, tracker) DO UPDATE SET
                data = excluded.data,
                updated_at = excluded.updated_at
        """, (user_id, tracker, raw, incoming_ts))
        conn.commit()
        return jsonify({"ok": True, "success": True, "applied": True, "updated_at": incoming_ts})
    except Exception as exc:
        logger.warning("Error in user trackers API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/notifications", methods=["GET", "POST", "DELETE"])
@app.route("/api/profile/notifications", methods=["GET", "POST", "DELETE"])
def api_user_notifications():
    """Get, create, or clear authenticated user in-app notifications."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "User ID not found"}), 401

    conn = get_db()
    try:
        if request.method == "GET":
            rows = conn.execute(
                "SELECT id, title, body, type, is_read, timestamp FROM user_in_app_notifications WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50",
                (user_id,)
            ).fetchall()
            notifs = []
            for r in rows:
                notifs.append({
                    "id": r["id"],
                    "title": r["title"],
                    "body": r["body"],
                    "type": r["type"],
                    "read": bool(r["is_read"]),
                    "timestamp": r["timestamp"]
                })
            return jsonify({
                "ok": True,
                "success": True,
                "notifications": notifs,
                "unreadCount": sum(1 for n in notifs if not n["read"])
            })

        if request.method == "DELETE":
            conn.execute("DELETE FROM user_in_app_notifications WHERE user_id = ?", (user_id,))
            conn.commit()
            return jsonify({"ok": True, "success": True, "notifications": []})

        # POST: Push notification
        data = request.get_json(silent=True) or {}
        notif_id = str(data.get("id") or f"notif_{int(time.time()*1000)}_{_secrets.token_hex(4)}")
        title = str(data.get("title") or "Notice").strip()[:255]
        body = str(data.get("body") or "").strip()
        ntype = str(data.get("type") or "info").strip()[:32]
        ts = int(data.get("timestamp") or (time.time() * 1000))
        is_read = 1 if bool(data.get("read") or data.get("is_read")) else 0

        conn.execute("""
            INSERT OR REPLACE INTO user_in_app_notifications (id, user_id, title, body, type, is_read, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (notif_id, user_id, title, body, ntype, is_read, ts))
        conn.commit()

        return jsonify({
            "ok": True,
            "success": True,
            "notification": {
                "id": notif_id,
                "title": title,
                "body": body,
                "type": ntype,
                "read": bool(is_read),
                "timestamp": ts
            }
        })
    except Exception as exc:
        logger.warning("Error in user notifications API: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


@app.route("/api/user/notifications/read", methods=["POST"])
@app.route("/api/profile/notifications/read", methods=["POST"])
def api_user_notifications_read():
    """Mark all authenticated user notifications as read."""
    auth_user = _current_auth_user()
    if not auth_user:
        return jsonify({"ok": False, "success": False, "error": "Authentication required"}), 401

    user_id = str(auth_user.get("user_id") or auth_user.get("discord_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "success": False, "error": "User ID not found"}), 401

    conn = get_db()
    try:
        conn.execute("UPDATE user_in_app_notifications SET is_read = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        return jsonify({"ok": True, "success": True})
    except Exception as exc:
        logger.warning("Error marking notifications read: %s", exc)
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500
    finally:
        conn.close()


# ============================================================================
# INTERACTIVE MAP, CATALOG & LIVE TELEMETRY API ENDPOINTS
# ============================================================================

@app.route("/api/items/search", methods=["GET"])
def api_items_search():
    """
    Search catalog items by name, category, or internal ID.
    Query params:
      - query / q / name: search keyword
      - category / cat: category filter
      - limit: items per page (default 50, max 200)
      - offset: pagination offset (default 0)
    """
    from utils import catalog_map_service
    q = request.args.get("query") or request.args.get("q") or request.args.get("name") or ""
    cat = request.args.get("category") or request.args.get("cat")
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        offset = 0

    data = catalog_map_service.search_catalog_items(q, category=cat, limit=limit, offset=offset)
    return jsonify(data)


@app.route("/api/recipes/search", methods=["GET"])
def api_recipes_search():
    """
    Search DIY crafting recipes by recipe name, crafted item, or material requirement.
    Query params:
      - query / q / name: search keyword
      - category / cat: recipe category filter
      - material / ingredient: required material filter
      - limit: recipes per page (default 50, max 200)
      - offset: pagination offset (default 0)
    """
    from utils import catalog_map_service
    q = request.args.get("query") or request.args.get("q") or request.args.get("name") or ""
    cat = request.args.get("category") or request.args.get("cat")
    mat = request.args.get("material") or request.args.get("ingredient")
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        offset = 0

    data = catalog_map_service.search_catalog_recipes(q, category=cat, material=mat, limit=limit, offset=offset)
    return jsonify(data)


@app.route("/api/items/customizations", methods=["GET"])
def api_items_customizations():
    """
    Get item customization variations, pattern titles, body titles, and kit requirements.
    Query params:
      - item / name / query / id: target item name or ID
    """
    from utils import catalog_map_service
    item_param = (
        request.args.get("item")
        or request.args.get("name")
        or request.args.get("query")
        or request.args.get("id")
        or ""
    ).strip()
    if not item_param:
        return jsonify({"ok": False, "error": "Query parameter 'item' is required."}), 400

    data = catalog_map_service.get_item_customizations(item_param)
    return jsonify(data)


@app.route("/api/telemetry", methods=["GET"])
def api_telemetry():
    """
    Multi-island real-time SysBot telemetry, visitor metrics, and health overview.
    Query params:
      - island / name: optional island name filter
    """
    from utils import catalog_map_service
    viewer = _current_auth_user()
    isl = request.args.get("island") or request.args.get("name")
    data = catalog_map_service.build_telemetry_payload(island_filter=isl, viewer=viewer)
    return jsonify(data)


@app.route("/api/island/items", methods=["GET"])
@app.route("/api/islands/items", methods=["GET"])
@app.route("/api/islands/<island_name>/items", methods=["GET"])
def api_island_items(island_name: Optional[str] = None):
    """
    Interactive map item locator: locates on-ground items on treasure islands
    by acre, sector, and precise map coordinates.
    Query params:
      - query / q: item name or category to search
      - island / name: island name (or 'all')
      - acre / sector: acre/sector filter (e.g. 'a0', 'B3')
      - category / cat: category filter
      - limit: results per page (default 100, max 500)
      - offset: pagination offset (default 0)
    """
    from utils import catalog_map_service
    isl = (island_name or request.args.get("island") or request.args.get("name") or "").strip()
    q = request.args.get("query") or request.args.get("q") or ""
    acre = request.args.get("acre") or request.args.get("sector")
    cat = request.args.get("category") or request.args.get("cat")
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        offset = 0

    data = catalog_map_service.search_island_items_unified(
        query=q,
        island_name=isl,
        acre=acre,
        category=cat,
        limit=limit,
        offset=offset,
    )
    return jsonify(data)


@app.route("/api/island/map/image", methods=["GET"])
@app.route("/api/islands/map/image", methods=["GET"])
@app.route("/api/islands/<island_name>/map/image", methods=["GET"])
def api_island_map_image(island_name: Optional[str] = None):
    """
    Dynamic scalable interactive island map image.
    Provides CDN cached terrain maps or procedurally generated high-resolution
    ACNH radar grid maps with live visitor radar blips.
    Query params:
      - island / name: island name (default 'Hiraya')
      - scale: magnification multiplier (default 1.0, range 0.1 .. 4.0)
      - theme: 'radar', 'satellite', 'dark', 'light' (default 'radar')
      - players: overlay live visitor radar blips ('1'/'0', default 1)
      - items: overlay items on ground ('1'/'0', default 0)
      - format: image format ('png', 'webp', 'jpeg', default 'png')
    """
    from utils import catalog_map_service
    isl = (island_name or request.args.get("island") or request.args.get("name") or "Hiraya").strip()
    try:
        scale_val = float(request.args.get("scale", 1.0))
    except (ValueError, TypeError):
        scale_val = 1.0

    theme = request.args.get("theme", "radar")
    overlay_players = request.args.get("players", "1").lower() not in ("0", "false", "no")
    overlay_items = request.args.get("items", "0").lower() in ("1", "true", "yes")
    fmt = request.args.get("format", "png")

    try:
        img_bytes, mimetype = catalog_map_service.render_island_map_image(
            island_name=isl,
            scale=scale_val,
            theme=theme,
            overlay_items=overlay_items,
            overlay_players=overlay_players,
            format_type=fmt,
        )
        resp = send_file(io.BytesIO(img_bytes), mimetype=mimetype)
        resp.headers["Cache-Control"] = "private, max-age=120" if island_access.is_member_island(None, None, isl) else "public, max-age=120"
        return resp
    except Exception as exc:
        logging.getLogger("flask_api").error(f"Failed to render island map image: {exc}")
        return jsonify({"ok": False, "error": _safe_error(exc)}), 500


@app.route("/api/players/live", methods=["GET"])
def api_players_live():
    """
    Real-time active visitor presence and player tracking across all islands.
    Query params:
      - island / name: optional island name filter
    """
    from utils import catalog_map_service
    viewer = _current_auth_user()
    isl = request.args.get("island") or request.args.get("name")
    data = catalog_map_service.build_live_players_payload(island_filter=isl, viewer=viewer)
    return jsonify(data)
