"""
ChoBot Web Dashboard
Mod-only web interface for island management, XLog reports, and analytics.
Access is protected by a secret key (DASHBOARD_SECRET env var).
"""

import json
import os
import re
import csv
import contextlib
import io
import secrets
import logging
import mimetypes
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import wraps

import boto3
from botocore.client import Config as BotocoreConfig
from botocore.exceptions import ClientError, NoCredentialsError

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify, abort, g, Response,
)

from utils.config import Config
from utils.auth_tokens import get_auth_user
from utils.database import connect_db, get_backend
from utils.discord_http import request as discord_request
from utils.db_migration import (
    dry_run_sqlite_to_mariadb,
    inspect_sqlite_source,
    migrate_sqlite_to_mariadb_detailed,
)
from utils.helpers import clean_text
from utils import island_access

logger = logging.getLogger("Dashboard")

# ---------------------------------------------------------------------------
# Blueprint setup
# ---------------------------------------------------------------------------
dashboard = Blueprint(
    "dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)


@dashboard.app_template_filter("intcomma")
def _intcomma(value):
    """Format a number with thousands comma separators (e.g. 2000 → 2,000)."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value

# Absolute path to the shared SQLite database
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "chobot.db",
)

ALLOWED_CATEGORIES = ("public", "member", "order")
ALLOWED_THEMES     = ("pink", "teal", "purple", "gold")
ALLOWED_STATUSES   = ("ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE")

# Dodo code value that signals a gate-refresh is in progress
REFRESHING_DODO_CODE = "GETTIN'"

# Display status keys (derived from live fields, not the stored status column)
STATUS_ONLINE     = "ONLINE"
STATUS_REFRESHING = "REFRESHING"
STATUS_OFFLINE    = "OFFLINE"

# Senior Mod role ID used during Discord OAuth login
ADMIN_ROLE_ID = Config.ADMIN_ROLE_ID

# Day-of-week label order (SQLite strftime('%w'): 0=Sunday … 6=Saturday)
_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Max map upload size: 5 MB
MAX_MAP_SIZE      = 5 * 1024 * 1024
ALLOWED_MAP_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_mariadb_migration_lock = threading.Lock()
_mariadb_migration_last_result: dict | None = None


# ---------------------------------------------------------------------------
# Discord user resolution
# ---------------------------------------------------------------------------
# Cache: maps user_id → (display_name, cache_time)
_discord_user_cache: dict[str, tuple[str, float]] = {}
_discord_user_cache_lock = threading.Lock()
_DISCORD_CACHE_TTL = 3600  # seconds — refresh names after 1 hour

# User-Agent sent with every Discord API request.
# Discord (via Cloudflare) blocks requests that use the default Python-urllib
# User-Agent (error 1010).  The DiscordBot format is the accepted convention.
_DISCORD_USER_AGENT = "DiscordBot (https://github.com/bitress/chobot, 1.0)"

# Discord permission bit for the built-in Administrator privilege.
# Guild members with this bit set bypass role-ID checks and always get
# full admin access to the dashboard.
_ADMINISTRATOR_PERM = 0x8


def _resolve_discord_username(user_id) -> str:
    """Return the display name for a Discord user ID.

    Calls GET /api/v10/users/{id} using the Bot token and caches results for
    up to one hour.  Falls back to the raw ID string on any failure or when
    the token is not configured.
    """
    if not user_id:
        return "—"
    uid = str(user_id)
    with _discord_user_cache_lock:
        cached = _discord_user_cache.get(uid)
        if cached and (time.monotonic() - cached[1]) < _DISCORD_CACHE_TTL:
            return cached[0]
    token = Config.DISCORD_TOKEN
    if not token:
        return uid
    try:
        resp = discord_request(
            f"https://discord.com/api/v10/users/{uid}",
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
            timeout=5,
        )
        data = json.loads(resp.body)
        name = data.get("global_name") or data.get("username") or uid
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            logger.debug("Discord user lookup HTTP 403 for %s (user inaccessible)", uid)
        else:
            logger.warning("Discord user lookup HTTP %s for %s", exc.code, uid)
        name = uid
    except Exception as exc:
        logger.debug("Discord user lookup failed for %s: %s", uid, exc)
        name = uid
    with _discord_user_cache_lock:
        _discord_user_cache[uid] = (name, time.monotonic())
    return name


def _resolve_discord_usernames(user_ids) -> dict[str, str]:
    """Resolve a collection of Discord user IDs to display names in one pass.

    Deduplicates the input so each distinct ID is fetched at most once per
    call.  Returns a mapping of id → display name.
    """
    result: dict[str, str] = {}
    for uid in dict.fromkeys(str(i) for i in user_ids if i):
        result[uid] = _resolve_discord_username(uid)
    return result


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    """Return a synchronous configured database connection."""
    return connect_db()


def init_dashboard_db():
    """Create dashboard-specific tables if they do not already exist."""
    try:
        conn = get_db()

        # Full IslandData-compatible table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS islands (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                type           TEXT NOT NULL DEFAULT '',
                items          TEXT NOT NULL DEFAULT '[]',
                theme          TEXT NOT NULL DEFAULT 'teal',
                cat            TEXT NOT NULL DEFAULT 'public',
                description    TEXT NOT NULL DEFAULT '',
                seasonal       TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'OFFLINE',
                visitors       INTEGER NOT NULL DEFAULT 0,
                dodo_code      TEXT,
                map_url        TEXT,
                updated_at     TEXT,
                required_roles TEXT NOT NULL DEFAULT '[]',
                channel_id     TEXT,
                display_name   TEXT,
                is_visible     INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Migrate: add required_roles column if it was created without it
        try:
            conn.execute("ALTER TABLE islands ADD COLUMN required_roles TEXT NOT NULL DEFAULT '[]'")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE islands ADD COLUMN channel_id TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE islands ADD COLUMN display_name TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE islands ADD COLUMN is_visible INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Live island bot presence, written by the Discord bot's monitor loop
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_bot_status (
                island_id   TEXT PRIMARY KEY,
                island_name TEXT NOT NULL,
                is_online   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS website_login_events (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           TEXT NOT NULL,
                username          TEXT,
                discord_name      TEXT,
                global_name       TEXT,
                account_name      TEXT,
                nickname          TEXT,
                avatar            TEXT,
                roles             TEXT NOT NULL DEFAULT '[]',
                role_count        INTEGER NOT NULL DEFAULT 0,
                is_admin          INTEGER NOT NULL DEFAULT 0,
                is_mod            INTEGER NOT NULL DEFAULT 0,
                ip_address        TEXT,
                user_agent        TEXT,
                return_to         TEXT,
                discord_message_id TEXT,
                discord_channel_id TEXT,
                discord_guild_id TEXT,
                created_at        TEXT NOT NULL
            )
        """)

        try:
            conn.execute("ALTER TABLE website_login_events ADD COLUMN discord_guild_id TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Legacy table kept for backward compatibility
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_metadata (
                name       TEXT PRIMARY KEY,
                category   TEXT NOT NULL DEFAULT 'public',
                theme      TEXT NOT NULL DEFAULT 'teal',
                notes      TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_audit_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id TEXT,
                actor_name    TEXT,
                action        TEXT NOT NULL,
                target        TEXT,
                details       TEXT NOT NULL,
                ip_address    TEXT,
                created_at    INTEGER NOT NULL
            )
        """)

        conn.commit()
        conn.close()
        logger.info("Dashboard DB initialised")
    except Exception as exc:
        logger.warning("Could not initialise dashboard DB: %s", exc)


# ---------------------------------------------------------------------------
# R2 / S3 helpers
# ---------------------------------------------------------------------------
def _get_r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2, or None if unconfigured."""
    if not (Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY):
        return None
    endpoint = f"https://{Config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=Config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=Config.R2_SECRET_ACCESS_KEY,
        config=BotocoreConfig(signature_version="s3v4"),
        region_name="auto",
    )


def _upload_map_to_r2(file_bytes: bytes, content_type: str, island_id: str) -> str:
    """Upload map image bytes to R2 and return the public URL."""
    client = _get_r2_client()
    if client is None:
        raise RuntimeError(
            "R2 is not configured — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, and R2_PUBLIC_URL in .env"
        )
    ext = mimetypes.guess_extension(content_type) or ".png"
    ext = {".jpe": ".jpg", ".jfif": ".jpg"}.get(ext, ext)
    key = f"maps/{island_id}{ext}"

    # Delete any pre-existing map files for this island (different extension)
    existing = client.list_objects_v2(
        Bucket=Config.R2_BUCKET_NAME,
        Prefix=f"maps/{island_id}",
    )
    for obj in existing.get("Contents", []):
        if obj["Key"] != key:
            client.delete_object(Bucket=Config.R2_BUCKET_NAME, Key=obj["Key"])

    client.put_object(
        Bucket=Config.R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )
    base = Config.R2_PUBLIC_URL.rstrip("/")
    return f"{base}/{key}"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _check_session():
    return bool(session.get("mod_logged_in"))


def _get_session_role():
    """Return the current session role (always 'admin' for authenticated sessions)."""
    return session.get("mod_role", "admin")


def _check_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and Config.DASHBOARD_SECRET:
        return secrets.compare_digest(auth[len("Bearer "):], Config.DASHBOARD_SECRET)
    return False


def _dashboard_bearer_user() -> dict | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return get_auth_user(auth[len("Bearer "):])


def _is_dashboard_mod_user(user: dict | None) -> bool:
    if not user:
        return False
    if user.get("is_admin") or user.get("is_mod"):
        return True
    roles = {str(role) for role in user.get("roles", [])}
    mod_role_ids = {
        str(Config.ADMIN_ROLE_ID or ""),
        str(Config.SENIOR_MOD_ROLE_ID or ""),
        str(Config.BABY_MOD_ROLE_ID or ""),
    } - {"", "0", "None"}
    return bool(roles & mod_role_ids)


def login_required(f):
    """Decorator for web routes — redirects to /dashboard/login if not authenticated."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_session():
            return redirect(url_for("dashboard.login"))
        return f(*args, **kwargs)
    return _decorated


def admin_required(f):
    """Decorator for admin-only web routes — redirects to login if not authenticated,
    or returns 403 Forbidden if authenticated but lacking admin privileges."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_session():
            return redirect(url_for("dashboard.login"))
        if _get_session_role() != "admin":
            abort(403)
        return f(*args, **kwargs)
    return _decorated


def api_auth_required(f):
    """Decorator for JSON API routes; accepts dashboard secret, session, or mod bearer token."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if _check_bearer() or _check_session():
            return f(*args, **kwargs)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        user = _dashboard_bearer_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if not _is_dashboard_mod_user(user):
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return _decorated


def _csrf_token() -> str:
    """Return the current CSRF token, creating one for this browser session."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_is_valid() -> bool:
    expected = session.get("csrf_token", "")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    return bool(expected and provided and secrets.compare_digest(expected, provided))


# ---------------------------------------------------------------------------
# Template context processor — injects current_role into every page
# ---------------------------------------------------------------------------
@dashboard.context_processor
def _inject_user():
    return {
        "current_role":       session.get("mod_role", "admin"),
        "discord_username":   session.get("discord_username", ""),
        "discord_user_id":    session.get("discord_user_id", ""),
        "discord_avatar_url": session.get("discord_avatar_url", ""),
        "oauth_configured":   bool(Config.DISCORD_CLIENT_ID),
        "csrf_token":         _csrf_token(),
    }


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _read_file(folder_path, filename):
    try:
        with open(os.path.join(folder_path, filename), "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except (FileNotFoundError, IOError, UnicodeDecodeError):
        return None


def _write_file(folder_path, filename, content):
    with open(os.path.join(folder_path, filename), "w", encoding="utf-8") as fh:
        fh.write(content)


def _parse_visitor_value(raw):
    """Normalize the content of Visitors.txt.

    The C# SysBot may write the file as a plain number ("3") or with a label
    ("Visitors: 3").  This strips any leading label so callers always receive
    the bare value ("3", "FULL", etc.).
    """
    if not raw:
        return raw
    cleaned = re.sub(r'(?i)^\s*visitors\s*:\s*', '', raw).strip()
    return cleaned if cleaned else None


def _parse_visitor_list(raw):
    """Parse Visitors.txt content into a (count, names) tuple.

    Handles the multi-line format produced by the C# SysBot::

        The following visitors are on {TownName}:
        #1: PlayerName
        #2: Available slot
        ...

    Also handles the legacy single-value format ("3", "Visitors: 3", "FULL").

    Returns:
        (visitor_count: int, visitor_names: list[str])
    """
    if not raw:
        return 0, []

    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]

    # New multi-line format from C# bot
    if lines and lines[0].lower().startswith("the following visitors are on"):
        names = []
        for line in lines[1:]:
            m = re.match(r'^#\d+:\s*(.+)$', line)
            if m:
                name = m.group(1).strip()
                if name.lower() != "available slot":
                    names.append(name)
        return len(names), names

    # Legacy single-value format
    cleaned = _parse_visitor_value(raw)
    if not cleaned:
        return 0, []
    if cleaned.isdigit():
        return int(cleaned), []
    if cleaned.upper() == "FULL":
        return 7, []
    return 0, []


def _collect_fs_islands():
    """Return a dict keyed by uppercase island name with live filesystem data."""
    result = {}

    def _scan(directory, itype):
        if not directory or not os.path.exists(directory):
            return
        if itype == "Order" and os.path.isdir(directory):
            direct_files = [
                os.path.join(directory, "Dodo.txt"),
                os.path.join(directory, "Visitors.txt"),
                os.path.join(directory, "Villagers.txt"),
            ]
            configured_name = getattr(Config, "ORDER_BOT_ISLAND", None) or os.path.basename(directory)
            basename_matches = clean_text(os.path.basename(directory)) == clean_text(configured_name)
            if basename_matches or any(os.path.exists(path) for path in direct_files):
                uname = configured_name.upper()
                result[uname] = {
                    "name":        uname,
                    "fs_path":     directory,
                    "fs_type":     itype,
                    "fs_dodo":     _read_file(directory, "Dodo.txt"),
                    "fs_visitors": _parse_visitor_value(_read_file(directory, "Visitors.txt")),
                }
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir():
                    uname = entry.name.upper()
                    result[uname] = {
                        "name":        uname,
                        "fs_path":     entry.path,
                        "fs_type":     itype,
                        "fs_dodo":     _read_file(entry.path, "Dodo.txt"),
                        "fs_visitors": _parse_visitor_value(_read_file(entry.path, "Visitors.txt")),
                    }

    _scan(Config.DIR_FREE, "Free")
    _scan(Config.DIR_VIP,  "VIP")
    _scan(getattr(Config, "DIR_ORDER", None), "Order")
    return result


def _ts_to_str(ts):
    """Convert a Unix timestamp int to a human-readable UTC string."""
    if ts is None:
        return "\u2014"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return str(ts)


def _where_clause(conditions: list) -> str:
    """Build a safe WHERE clause from a list of predefined SQL fragment strings.

    Only hardcoded SQL condition strings (containing '?' placeholders) may be
    passed here — never raw user input.  User-supplied values must be passed
    separately as a params list to the db.execute() call.
    """
    return ("WHERE " + " AND ".join(conditions)) if conditions else ""


def row_to_island_dict(row: dict) -> dict:
    """Decode JSON columns and return a plain dict."""
    try:
        row["items"] = json.loads(row.get("items") or "[]")
    except (ValueError, TypeError):
        row["items"] = []
    try:
        row["required_roles"] = json.loads(row.get("required_roles") or "[]")
    except (ValueError, TypeError):
        row["required_roles"] = []
    row["is_visible"] = bool(row.get("is_visible", 1))
    return row


def _load_bot_status_map(conn) -> dict:
    """Return a dict of island_id → bool (is_online) from island_bot_status."""
    try:
        rows = conn.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        return {r["island_id"]: bool(r["is_online"]) for r in rows}
    except Exception:
        return {}


def _effective_status(isl: dict) -> str:
    """Derive display status from live fields, ignoring the stored status key.

    Rules (in priority order):
      1. dodo_code == REFRESHING_DODO_CODE  → STATUS_REFRESHING
      2. discord_bot_online                  → STATUS_ONLINE
      3. otherwise                           → STATUS_OFFLINE
    """
    if (isl.get("dodo_code") or "").strip().upper() == REFRESHING_DODO_CODE:
        return STATUS_REFRESHING
    if isl.get("discord_bot_online"):
        return STATUS_ONLINE
    return STATUS_OFFLINE


# Backward-compatible alias for internal callers
_row_to_island_dict = row_to_island_dict

# Canonical fields exposed by the public API (in consistent order)
_API_ISLAND_FIELDS = (
    "cat", "description", "discord_bot_online", "dodo_code", "id", "items",
    "map_url", "name", "display_name", "is_visible", "required_roles", "seasonal", "status", "theme",
    "type", "updated_at", "visitors", "channel_id", "access_source",
)


def _island_api_dict(isl: dict) -> dict:
    """Return a clean API-facing dict containing only canonical island fields."""
    return {field: isl.get(field) for field in _API_ISLAND_FIELDS}


def _json_bool(data: dict, key: str, fallback: bool = True) -> bool:
    """Read a JSON boolean-ish value without treating omitted fields as false."""
    if key not in data:
        return fallback
    value = data.get(key)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _island_access_status(isl: dict, *, force_refresh: bool = False) -> dict:
    """Return dashboard-facing access status for one island."""
    info = island_access.resolved_island_required_roles(
        isl.get("name"),
        isl.get("cat"),
        isl.get("required_roles") or [],
        isl.get("type"),
        isl.get("channel_id"),
        force_refresh=force_refresh,
    )
    role_names = island_access.get_guild_role_names()
    is_member = island_access.is_member_island(isl.get("cat"), isl.get("type"))
    warnings = []
    if is_member and not info.channel_id:
        warnings.append("missing_channel_id")
    if is_member and info.role_count == 0:
        warnings.append("no_view_roles")
    if is_member and info.access_source != "discord_channel":
        warnings.append("using_database_fallback")
    return {
        "id": isl.get("id"),
        "name": isl.get("name"),
        "cat": isl.get("cat"),
        "type": isl.get("type"),
        "is_member": is_member,
        "channel_id": info.channel_id,
        "access_source": info.access_source,
        "required_roles": [island_access.role_payload(role_id, role_names) for role_id in info.required_roles],
        "required_role_ids": info.required_roles,
        "role_count": info.role_count,
        "warnings": warnings,
        "ok": not warnings,
    }


def _load_dashboard_islands() -> list[dict]:
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        return _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
    finally:
        db.close()


def _fs_island_stub(fs: dict) -> dict:
    """Build dashboard metadata for an island folder that has no DB row yet."""
    name = fs.get("name", "")
    fs_type = fs.get("fs_type") or ""
    cat = "member" if fs_type == "VIP" else "order" if fs_type == "Order" else "public"
    island_type = "Order Bot" if fs_type == "Order" else fs_type
    channel_id = str(Config.ORDER_BOT_CHANNEL_ID or "") if fs_type == "Order" else None
    return {
        "id": name.lower(),
        "name": name,
        "display_name": None,
        "is_visible": True,
        "type": island_type,
        "items": [],
        "theme": "teal",
        "cat": cat,
        "description": "",
        "seasonal": "Year-Round" if fs_type == "Order" else "",
        "status": "OFFLINE",
        "visitors": 0,
        "dodo_code": None,
        "map_url": None,
        "updated_at": None,
        "required_roles": [],
        "channel_id": channel_id,
    }


def _merge_dashboard_fs_islands(db_islands: list[dict]) -> list[dict]:
    """Return DB islands plus filesystem-only islands such as Sinta/order bot."""
    fs_map = _collect_fs_islands()
    merged = []
    seen = set()

    for isl in db_islands:
        uname = str(isl.get("name") or isl.get("id") or "").upper()
        if uname:
            seen.add(uname)
        merged.append(_merge_island(isl, fs_map.get(uname)))

    for uname, fs in fs_map.items():
        if uname in seen:
            continue
        merged.append(_merge_island(_fs_island_stub(fs), fs))

    merged.sort(key=lambda item: str(item.get("name") or item.get("id") or ""))
    return merged


def _find_island_filesystem_meta(island_id: str, display_name: str | None = None) -> dict:
    """Return filesystem metadata used by the legacy island detail page."""
    upper = (display_name or island_id).upper()
    fs_path = fs_type = None
    for directory, itype in [(Config.DIR_FREE, "Free"), (Config.DIR_VIP, "VIP"), (getattr(Config, "DIR_ORDER", None), "Order")]:
        if not directory:
            continue
        if itype == "Order" and os.path.isdir(directory):
            order_key = (getattr(Config, "ORDER_BOT_ISLAND", None) or os.path.basename(directory)).upper()
            if upper == order_key or island_id.lower() == order_key.lower():
                basename_matches = clean_text(os.path.basename(directory)) == clean_text(order_key)
                has_order_files = any(os.path.exists(os.path.join(directory, fname)) for fname in ("Dodo.txt", "Visitors.txt", "Villagers.txt"))
                if basename_matches or has_order_files:
                    fs_path, fs_type = directory, itype
                    break
        for candidate_name in [upper, island_id]:
            candidate = os.path.join(directory, candidate_name)
            if os.path.isdir(candidate):
                fs_path, fs_type = candidate, itype
                break
        if fs_path:
            break
    return {
        "fs_path": fs_path,
        "fs_type": fs_type,
        "fs_dodo": _read_file(fs_path, "Dodo.txt") if fs_path else None,
        "fs_visitors": _parse_visitor_value(_read_file(fs_path, "Visitors.txt")) if fs_path else None,
    }


def _island_sparkline_7d(destination: str) -> list[dict]:
    """Return per-island visit counts for the last seven days."""
    db_sp = get_db()
    try:
        return [
            dict(r) for r in db_sp.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE LOWER(destination) = LOWER(?) "
                "AND timestamp > strftime('%s','now','-7 days') "
                "GROUP BY day ORDER BY day",
                (destination,),
            ).fetchall()
        ]
    except Exception:
        return []
    finally:
        db_sp.close()


def _island_detail_api_dict(isl: dict) -> dict:
    """Return the React dashboard island editor payload."""
    payload = _island_api_dict(isl)
    payload.update(_find_island_filesystem_meta(isl.get("id", ""), isl.get("name")))
    payload.update({
        "allowed_categories": list(ALLOWED_CATEGORIES),
        "allowed_themes": list(ALLOWED_THEMES),
        "allowed_statuses": list(ALLOWED_STATUSES),
        "r2_configured": bool(
            Config.R2_ACCOUNT_ID
            and Config.R2_ACCESS_KEY_ID
            and Config.R2_SECRET_ACCESS_KEY
            and Config.R2_BUCKET_NAME
            and Config.R2_PUBLIC_URL
        ),
        "sparkline_7d": _island_sparkline_7d(isl.get("name") or isl.get("id", "")),
    })
    return payload


def _merge_island(db_row: dict, fs: dict | None) -> dict:
    """Overlay live filesystem data (Dodo / Visitors) onto a DB island record."""
    db_row["fs_dodo"]     = fs["fs_dodo"]     if fs else None
    db_row["fs_visitors"] = fs["fs_visitors"] if fs else None
    db_row["fs_type"]     = fs["fs_type"]     if fs else None
    db_row["fs_path"]     = fs["fs_path"]     if fs else None
    return db_row


# ===========================================================================
# WEB ROUTES
# ===========================================================================

# ---------------------------------------------------------------------------
# Domain restriction — dashboard is served from console.chopaeng.com,
# and localhost for local development.
# ---------------------------------------------------------------------------
_ALLOWED_DASHBOARD_HOSTS = {"console.chopaeng.com", "localhost", "127.0.0.1"}


def _legacy_jinja_enabled() -> bool:
    return bool(getattr(Config, "DASHBOARD_LEGACY_JINJA", True))


def _dashboard_frontend_response():
    """Send legacy page requests to the frontend app when Jinja pages are disabled."""
    frontend = getattr(Config, "DASHBOARD_FRONTEND_URL", "").strip().rstrip("/")
    if frontend:
        path = request.path
        if path.startswith("/dashboard"):
            path = path[len("/dashboard"):] or "/"
        query = f"?{request.query_string.decode()}" if request.query_string else ""
        return redirect(f"{frontend}{path}{query}")
    return jsonify({
        "error": "Dashboard Jinja pages are disabled",
        "api": "/dashboard/api",
        "path": request.path,
    }), 404


def _is_legacy_dashboard_page_request() -> bool:
    """True for server-rendered dashboard pages that a frontend can own instead."""
    if request.method != "GET":
        return False

    path = request.path.rstrip("/") or "/dashboard"
    if path.startswith("/dashboard/api"):
        return False
    if path.startswith("/dashboard/static"):
        return False
    if path.startswith("/dashboard/oauth2"):
        return False
    if path.endswith("/analytics/export.csv"):
        return False

    exact_pages = {
        "/dashboard",
        "/dashboard/login",
        "/dashboard/islands",
        "/dashboard/logs",
        "/dashboard/status",
        "/dashboard/analytics",
        "/dashboard/database",
    }
    return path in exact_pages or path.startswith("/dashboard/islands/")


@dashboard.before_request
def _restrict_to_console_domain():
    """Return 404 for any request that did not arrive via an allowed dashboard host."""
    host = request.host.split(":")[0]  # strip optional port
    if host not in _ALLOWED_DASHBOARD_HOSTS:
        abort(404)
    if not _legacy_jinja_enabled() and _is_legacy_dashboard_page_request():
        return _dashboard_frontend_response()
    if request.method in {"POST", "PUT", "DELETE"} and not request.path.startswith("/dashboard/api/"):
        if not _csrf_is_valid():
            abort(403)


@dashboard.errorhandler(403)
def _forbidden(_e):
    if not _legacy_jinja_enabled() or request.path.startswith("/dashboard/api/"):
        return jsonify({"error": "Forbidden"}), 403
    return render_template("dashboard/403.html"), 403

@dashboard.errorhandler(500)
def _internal_server_error(e):
    logger.exception("Internal server error: %s", e)
    if not _legacy_jinja_enabled() or request.path.startswith("/dashboard/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return render_template("dashboard/500.html"), 500


@dashboard.route("/api/session", methods=["GET"])
def api_session():
    """Return the current dashboard session state for a React dashboard."""
    user = _dashboard_bearer_user()
    bearer_mod = _is_dashboard_mod_user(user)
    session_auth = _check_session()
    role = _get_session_role() if session_auth else ("admin" if bearer_mod else None)
    return jsonify({
        "authenticated": bool(session_auth or bearer_mod or _check_bearer()),
        "role": role,
        "legacy_jinja_enabled": _legacy_jinja_enabled(),
        "csrf_token": _csrf_token() if session_auth else None,
        "user": user if bearer_mod else {
            "id": session.get("discord_user_id"),
            "username": session.get("discord_username"),
            "avatar": session.get("discord_avatar_url"),
        } if session_auth else None,
    })


@dashboard.route("/api/login", methods=["POST"])
def api_login():
    """Secret-key login endpoint for a React dashboard."""
    payload = request.get_json(silent=True) or {}
    secret = payload.get("secret") or request.form.get("secret", "")
    if secret and Config.DASHBOARD_SECRET and secrets.compare_digest(secret, Config.DASHBOARD_SECRET):
        session["mod_logged_in"] = True
        session["mod_role"] = "admin"
        session.permanent = True
        return jsonify({
            "ok": True,
            "role": "admin",
            "csrf_token": _csrf_token(),
        })
    return jsonify({"ok": False, "error": "Invalid secret key"}), 401


@dashboard.route("/api/logout", methods=["POST"])
def api_logout():
    """Clear the browser dashboard session for a React dashboard."""
    _clear_dashboard_session()
    return jsonify({"ok": True})


def _clear_dashboard_session() -> None:
    session.pop("mod_logged_in",       None)
    session.pop("mod_role",            None)
    session.pop("discord_user_id",     None)
    session.pop("discord_username",    None)
    session.pop("discord_avatar_url",  None)
    session.pop("oauth_state",         None)
    session.pop("csrf_token",          None)


@dashboard.route("/login", methods=["GET", "POST"])
def login():
    if _check_session():
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        secret = request.form.get("secret", "")
        if secret and Config.DASHBOARD_SECRET and secret == Config.DASHBOARD_SECRET:
            session["mod_logged_in"] = True
            session["mod_role"]      = "admin"
            session.permanent        = True
            return redirect(url_for("dashboard.index"))
        flash("Invalid secret key. Please try again.", "error")
    return render_template("dashboard/login.html")


@dashboard.route("/logout")
def logout():
    _clear_dashboard_session()
    return redirect(url_for("dashboard.login"))


# ---------------------------------------------------------------------------
# Discord OAuth2 routes
# ---------------------------------------------------------------------------

@dashboard.route("/oauth2/redirect")
def oauth2_redirect():
    """Redirect the user to Discord's authorization page."""
    if not Config.DISCORD_CLIENT_ID:
        flash("Discord OAuth is not configured on this server.", "error")
        return redirect(url_for("dashboard.login"))
    if not Config.GUILD_ID:
        flash("Discord OAuth is not fully configured on this server (GUILD_ID missing).", "error")
        return redirect(url_for("dashboard.login"))
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    # Derive the callback URL from the current request so operators don't need
    # to set a DISCORD_REDIRECT_URI env var — just register this URL in the
    # Discord application's OAuth2 Redirects list:
    #   https://your-domain/dashboard/oauth2/callback
    callback_url = url_for("dashboard.oauth2_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id":     Config.DISCORD_CLIENT_ID,
        "redirect_uri":  callback_url,
        "response_type": "code",
        "scope":         "identify guilds.members.read",
        "state":         state,
    })
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@dashboard.route("/oauth2/callback")
def oauth2_callback():
    """Handle the OAuth2 callback from Discord."""
    error = request.args.get("error")
    if error:
        flash(f"Discord authorization denied: {error}", "error")
        return redirect(url_for("dashboard.login"))

    state = request.args.get("state", "")
    if state != session.pop("oauth_state", ""):
        flash("Invalid OAuth state — possible CSRF. Please try again.", "error")
        return redirect(url_for("dashboard.login"))

    code = request.args.get("code", "")
    if not code:
        flash("No authorization code received from Discord.", "error")
        return redirect(url_for("dashboard.login"))

    # Exchange authorization code for access token
    # The redirect_uri must exactly match what was sent during the authorization request.
    callback_url = url_for("dashboard.oauth2_callback", _external=True)
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
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent":   _DISCORD_USER_AGENT,
            },
            method="POST",
            timeout=10,
        )
        token_resp = json.loads(resp.body)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            pass
        logger.error(
            "OAuth token exchange HTTP %s — redirect_uri=%s — Discord response: %s",
            exc.code, callback_url, body,
        )
        flash("Failed to exchange authorization code with Discord.", "error")
        return redirect(url_for("dashboard.login"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.error("OAuth token exchange failed: %s", exc)
        flash("Failed to exchange authorization code with Discord.", "error")
        return redirect(url_for("dashboard.login"))

    access_token = token_resp.get("access_token")
    if not access_token:
        flash("No access token returned by Discord.", "error")
        return redirect(url_for("dashboard.login"))

    # Fetch the user's guild-member record (includes roles and computed permissions)
    role = None
    member_perms = 0
    try:
        resp = discord_request(
            f"https://discord.com/api/users/@me/guilds/{Config.GUILD_ID}/member",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
            timeout=10,
        )
        member_data = json.loads(resp.body)
        member_roles = [str(r) for r in member_data.get("roles", [])]
        try:
            member_perms = int(member_data.get("permissions", "0") or 0)
        except (ValueError, TypeError):
            member_perms = 0
        # Guild administrators (ADMINISTRATOR permission bit) always get admin access,
        # regardless of whether ADMIN_ROLE_ID is configured.
        if member_perms & _ADMINISTRATOR_PERM:
            role = "admin"
        elif ADMIN_ROLE_ID and str(ADMIN_ROLE_ID) in member_roles:
            role = "admin"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            flash("You are not a member of this server.", "error")
        else:
            logger.error("OAuth member fetch HTTP error %s", exc.code)
            flash("Could not fetch your server roles. Please try again.", "error")
        return redirect(url_for("dashboard.login"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.error("OAuth member fetch failed: %s", exc)
        flash("Could not fetch your server roles. Please try again.", "error")
        return redirect(url_for("dashboard.login"))

    if role is None:
        logger.warning(
            "OAuth role check: no qualifying role — "
            "member_roles=%s, admin_id=%s, permissions=%s",
            member_roles, ADMIN_ROLE_ID, member_perms,
        )
        flash("You do not have a moderator role on this server.", "error")
        return redirect(url_for("dashboard.login"))

    # Fetch basic user info for display
    discord_username   = ""
    discord_user_id    = ""
    discord_avatar_url = ""
    try:
        resp = discord_request(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
            timeout=10,
        )
        user_data = json.loads(resp.body)
        discord_user_id  = str(user_data.get("id", ""))
        discord_username = user_data.get("global_name") or user_data.get("username", "")
        avatar_hash      = user_data.get("avatar") or ""
        # Discord avatar hashes are lowercase hex strings (32 chars) or
        # animated variants prefixed with 'a_'.  Validate before using.
        if (discord_user_id and avatar_hash
                and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar_hash)):
            discord_avatar_url = (
                f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png?size=64"
            )
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        pass  # Non-critical — display info is optional

    session["mod_logged_in"]      = True
    session["mod_role"]           = role
    session["discord_user_id"]    = discord_user_id
    session["discord_username"]   = discord_username
    session["discord_avatar_url"] = discord_avatar_url
    session.permanent          = True
    logger.info("OAuth login: user=%s role=%s", discord_username, role)
    return redirect(url_for("dashboard.index"))


@dashboard.route("/")
@admin_required
def index():
    db = get_db()
    try:
        total_visits   = db.execute("SELECT COUNT(*) FROM island_visits").fetchone()[0]
        total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        visits_today   = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
        ).fetchone()[0]
        visits_week    = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        warnings_week  = db.execute(
            "SELECT COUNT(*) FROM warnings "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        recent_raw     = db.execute(
            "SELECT ign, destination, authorized, timestamp, user_id "
            "FROM island_visits ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        top_islands_raw = db.execute(
            "SELECT destination, COUNT(*) AS visit_count "
            "FROM island_visits "
            "GROUP BY destination "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        top_travelers_raw = db.execute(
            "SELECT ign, COUNT(*) AS visit_count "
            "FROM island_visits "
            "GROUP BY ign "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        trend_raw = db.execute(
            "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
            "FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days') "
            "GROUP BY day ORDER BY day"
        ).fetchall()
    except Exception:
        total_visits = total_warnings = visits_today = visits_week = warnings_week = 0
        recent_raw = []
        top_islands_raw = []
        top_travelers_raw = []
        trend_raw = []
    finally:
        db.close()

    recent_user_ids = [r["user_id"] for r in recent_raw if r["user_id"]]
    recent_name_map = _resolve_discord_usernames(recent_user_ids) if recent_user_ids else {}

    recent = [
        {
            "ign":         r["ign"],
            "destination": r["destination"],
            "authorized":  bool(r["authorized"]),
            "timestamp":   _ts_to_str(r["timestamp"]),
            "user_name":   recent_name_map.get(str(r["user_id"])) if r["user_id"] else None,
        }
        for r in recent_raw
    ]

    top_islands  = [{"name": r["destination"], "count": r["visit_count"]} for r in top_islands_raw]
    top_travelers = [{"ign": r["ign"], "count": r["visit_count"]} for r in top_travelers_raw]

    # Build a complete 7-day trend (fill gaps with 0)
    trend_map = {r["day"]: r["count"] for r in trend_raw}
    today_dt  = datetime.now(timezone.utc)
    trend_labels = []
    trend_counts = []
    for offset in range(6, -1, -1):
        d = (today_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        trend_labels.append(d[-5:])  # "MM-DD"
        trend_counts.append(trend_map.get(d, 0))

    warn_rate_7d = round(warnings_week / visits_week * 100, 1) if visits_week > 0 else 0

    db2 = get_db()
    try:
        rows2        = db2.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands2  = [_row_to_island_dict(dict(r)) for r in rows2]
        bot_status2  = _load_bot_status_map(db2)
    except Exception:
        db_islands2 = []
        bot_status2 = {}
    finally:
        db2.close()

    for isl in db_islands2:
        isl["discord_bot_online"] = bot_status2.get(isl.get("id", ""))

    island_count = len(db_islands2)
    status_map: dict[str, int] = {STATUS_ONLINE: 0, STATUS_REFRESHING: 0, STATUS_OFFLINE: 0}
    for isl in db_islands2:
        s = _effective_status(isl)
        status_map[s] = status_map.get(s, 0) + 1

    online_count = status_map[STATUS_ONLINE]

    return render_template(
        "dashboard/index.html",
        total_visits=total_visits,
        total_warnings=total_warnings,
        visits_today=visits_today,
        visits_week=visits_week,
        warnings_week=warnings_week,
        warn_rate_7d=warn_rate_7d,
        recent=recent,
        island_count=island_count,
        status_map=status_map,
        online_count=online_count,
        top_islands=top_islands,
        top_travelers=top_travelers,
        trend_labels=trend_labels,
        trend_counts=trend_counts,
    )


@dashboard.route("/islands")
@admin_required
def islands():
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except Exception:
        db_islands = []
    finally:
        db.close()

    merged = _merge_dashboard_fs_islands(db_islands)
    return render_template("dashboard/islands.html", islands=merged)


@dashboard.route("/islands/<name>", methods=["GET", "POST"])
@admin_required
def island_detail(name):
    island_id = name.lower()
    upper     = name.upper()

    db = get_db()
    try:
        row  = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
        meta = _row_to_island_dict(dict(row)) if row else None
    finally:
        db.close()

    fs_meta = _find_island_filesystem_meta(island_id, upper)
    fs_path = fs_meta["fs_path"]
    fs_type = fs_meta["fs_type"]

    if request.method == "POST":
        isl_type         = request.form.get("type", "").strip()
        isl_seasonal     = request.form.get("seasonal", "").strip()
        isl_desc         = request.form.get("description", "").strip()
        isl_cat          = request.form.get("cat", "public")
        isl_theme        = request.form.get("theme", "teal")
        isl_status       = request.form.get("status", "OFFLINE")
        # required_roles comes as a JSON array from the hidden input
        roles_raw = request.form.get("required_roles_json", "") or "[]"
        try:
            isl_required_roles = json.loads(roles_raw) if roles_raw.startswith("[") else []
            # Only keep string role IDs to avoid injecting arbitrary data
            isl_required_roles = [str(r) for r in isl_required_roles if str(r).isdigit()]
        except (ValueError, TypeError):
            isl_required_roles = []
        isl_dodo         = meta["dodo_code"] if meta else (_read_file(fs_path, "Dodo.txt") if fs_path else None)
        _fs_visitors_raw = _parse_visitor_value(_read_file(fs_path, "Visitors.txt")) if not meta and fs_path else None
        isl_visitors_raw = str(meta["visitors"]) if meta else (_fs_visitors_raw or "0")

        # items come as a JSON array from the hidden input
        items_raw = request.form.get("items_json", "") or request.form.get("items", "")
        try:
            items_list = json.loads(items_raw) if items_raw.startswith("[") else [
                i.strip() for i in items_raw.split(",") if i.strip()
            ]
        except (ValueError, TypeError):
            items_list = []

        errors = []
        if isl_cat    not in ALLOWED_CATEGORIES: errors.append("Invalid category.")
        if isl_theme  not in ALLOWED_THEMES:     errors.append("Invalid theme.")
        if isl_status not in ALLOWED_STATUSES:   errors.append("Invalid status.")

        try:
            isl_visitors = int(isl_visitors_raw)
        except ValueError:
            isl_visitors = 0

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            # dodo_code and visitors are managed by island bots; do not write to filesystem

            db2 = get_db()
            try:
                # We do NOT include `required_roles` in the DO UPDATE SET clause
                # so that we do not overwrite the background sync performed by the bot.
                # It is included in INSERT so that new records get the default '[]'.
                db2.execute(
                    """INSERT INTO islands
                           (id, name, display_name, is_visible, type, items, theme, cat, description, seasonal,
                            status, visitors, dodo_code, map_url, updated_at, required_roles)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                           name=excluded.name, type=excluded.type, items=excluded.items,
                           theme=excluded.theme, cat=excluded.cat,
                           description=excluded.description, seasonal=excluded.seasonal,
                           status=excluded.status, visitors=excluded.visitors,
                           dodo_code=excluded.dodo_code, updated_at=excluded.updated_at""",
                    (
                        island_id, upper, meta.get("display_name") if meta else None,
                        int(bool(meta.get("is_visible", True) if meta else True)),
                        isl_type, json.dumps(items_list),
                        isl_theme, isl_cat, isl_desc, isl_seasonal,
                        isl_status, isl_visitors, isl_dodo,
                        meta["map_url"] if meta else None,
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(isl_required_roles),
                    ),
                )
                db2.commit()
            finally:
                db2.close()

            flash(f'Island "{upper}" saved successfully.', "success")
            return redirect(url_for("dashboard.islands"))

    island = meta or {
        "id": island_id, "name": upper, "type": "", "items": [],
        "theme": "teal", "cat": "public", "description": "", "seasonal": "",
        "status": "OFFLINE", "visitors": 0, "dodo_code": None,
        "map_url": None, "updated_at": None, "required_roles": [],
    }
    island["fs_path"]     = fs_path
    island["fs_type"]     = fs_type
    island["fs_dodo"]     = _read_file(fs_path, "Dodo.txt")     if fs_path else None
    island["fs_visitors"] = _parse_visitor_value(_read_file(fs_path, "Visitors.txt")) if fs_path else None
    island["items_text"]  = ", ".join(island["items"]) if isinstance(island.get("items"), list) else ""

    r2_configured = bool(Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY)

    return render_template(
        "dashboard/island_detail.html",
        island=island,
        allowed_categories=ALLOWED_CATEGORIES,
        allowed_themes=ALLOWED_THEMES,
        allowed_statuses=ALLOWED_STATUSES,
        r2_configured=r2_configured,
        sparkline_7d=_island_sparkline_7d(upper),
    )


_ALLOWED_SORT_COLS = {"ign", "destination", "timestamp"}

@dashboard.route("/logs")
@admin_required
def logs():
    page              = request.args.get("page", 1, type=int)
    per_page          = 25
    island_filter     = request.args.get("island", "").strip()
    authorized_filter = request.args.get("authorized", "")
    category_filter   = request.args.get("category", "")
    sort_by           = request.args.get("sort_by", "timestamp")
    sort_order        = request.args.get("sort_order", "desc")
    log_type          = request.args.get("type", "flights")
    ign_filter        = request.args.get("ign", "").strip()
    _ALLOWED_ACTION_TYPES = {"WARN", "KICK", "BAN", "DISMISS", "NOTE", "ADMIT"}
    action_type_filter = request.args.get("action_type", "").strip().upper()
    if action_type_filter not in _ALLOWED_ACTION_TYPES:
        action_type_filter = ""

    # Sanitise sort params
    if sort_by not in _ALLOWED_SORT_COLS:
        sort_by = "timestamp"
    sort_order = "asc" if sort_order == "asc" else "desc"

    db = get_db()
    try:
        # Fetch island list for dropdown (used in flights filter UI)
        island_names = [
            r[0] for r in db.execute(
                "SELECT name FROM islands ORDER BY name"
            ).fetchall()
        ]

        if log_type == "warnings":
            conditions, params = [], []
            if ign_filter:
                conditions.append("LOWER(iv.ign) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if action_type_filter:
                conditions.append("UPPER(w.action_type) = ?")
                params.append(action_type_filter)
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT w.*, iv.ign, iv.destination "
                f"FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where} ORDER BY w.timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            name_map = _resolve_discord_usernames(
                [r["user_id"] for r in rows if r["user_id"]] + [r["mod_id"] for r in rows if r["mod_id"]]
            )
            entries = [
                {
                    "user_id":     r["user_id"],
                    "user_name":   name_map.get(str(r["user_id"]), str(r["user_id"])) if r["user_id"] else "—",
                    "reason":      r["reason"],
                    "mod_id":      r["mod_id"],
                    "mod_name":    name_map.get(str(r["mod_id"]), str(r["mod_id"])) if r["mod_id"] else "—",
                    "timestamp":   _ts_to_str(r["timestamp"]),
                    "ign":         r["ign"],
                    "destination": r["destination"],
                    "action_type": r["action_type"],
                }
                for r in rows
            ]
        else:
            conditions, params = [], []
            use_island_join = bool(category_filter in ("public", "member"))

            if island_filter:
                col = "iv.destination" if use_island_join else "destination"
                conditions.append(f"LOWER({col}) = LOWER(?)")
                params.append(island_filter)
            if ign_filter:
                col = "iv.ign" if use_island_join else "ign"
                conditions.append(f"LOWER({col}) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if authorized_filter in ("0", "1"):
                col = "iv.authorized" if use_island_join else "authorized"
                conditions.append(f"{col} = ?")
                params.append(int(authorized_filter))
            if use_island_join:
                conditions.append("isl.cat = ?")
                params.append(category_filter)

            if use_island_join:
                join_sql = (
                    "FROM island_visits iv "
                    "JOIN islands isl ON LOWER(iv.destination) = isl.id"
                )
                order_sql = f"iv.{sort_by} {sort_order.upper()}"
                where = _where_clause(conditions)
                total = db.execute(
                    f"SELECT COUNT(*) {join_sql} {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT iv.* {join_sql} {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()
            else:
                where = _where_clause(conditions)
                order_sql = f"{sort_by} {sort_order.upper()}"
                total = db.execute(
                    f"SELECT COUNT(*) FROM island_visits {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT * FROM island_visits {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()

            entries = [
                {
                    "id":            r["id"],
                    "ign":           r["ign"],
                    "origin_island": r["origin_island"],
                    "destination":   r["destination"],
                    "authorized":    bool(r["authorized"]),
                    "timestamp":     _ts_to_str(r["timestamp"]),
                    "user_id":       r["user_id"],
                }
                for r in rows
            ]
            flight_name_map = _resolve_discord_usernames([r["user_id"] for r in rows if r["user_id"]])
            for e in entries:
                e["user_name"] = flight_name_map.get(str(e["user_id"])) if e["user_id"] else None
    except Exception:
        total, entries, island_names = 0, [], []
    finally:
        db.close()

    return render_template(
        "dashboard/logs.html",
        entries=entries,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, (total + per_page - 1) // per_page),
        island_filter=island_filter,
        authorized_filter=authorized_filter,
        category_filter=category_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        log_type=log_type,
        island_names=island_names,
        ign_filter=ign_filter,
        action_type_filter=action_type_filter,
    )


@dashboard.route("/status")
@admin_required
def island_status():
    """Dedicated Island Status Breakdown page."""
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except Exception:
        db_islands = []
    finally:
        db.close()

    island_count = len(db_islands)

    # Load live bot-presence data and annotate each island
    db2 = get_db()
    try:
        bot_status = _load_bot_status_map(db2)
    except Exception:
        bot_status = {}
    finally:
        db2.close()

    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("id", ""))

    # Derive counts from live fields (discord_bot_online / dodo_code)
    online_count    = 0
    refreshing_count = 0
    offline_count   = 0
    grouped: dict[str, list] = {STATUS_ONLINE: [], STATUS_REFRESHING: [], STATUS_OFFLINE: []}
    for isl in db_islands:
        s = _effective_status(isl)
        grouped[s].append(isl)
        if s == STATUS_ONLINE:
            online_count += 1
        elif s == STATUS_REFRESHING:
            refreshing_count += 1
        else:
            offline_count += 1

    def _pct(count):
        return round(count * 100 / island_count) if island_count else 0

    online_pct     = _pct(online_count)
    refreshing_pct = _pct(refreshing_count)
    off_pct        = _pct(offline_count)

    return render_template(
        "dashboard/status.html",
        island_count=island_count,
        online_count=online_count,
        refreshing_count=refreshing_count,
        offline_count=offline_count,
        online_pct=online_pct,
        refreshing_pct=refreshing_pct,
        off_pct=off_pct,
        grouped=grouped,
    )


@dashboard.route("/analytics")
@admin_required
def analytics():
    # ── Island-type filter (free / sub / all) ──────────────────────────────
    island_type_filter = request.args.get("island_type", "").lower()
    if island_type_filter not in ("free", "sub"):
        island_type_filter = ""

    # SQL fragment appended to WHERE clauses in island_visits queries
    it_clause = " AND island_type = ?" if island_type_filter else ""
    it_params = [island_type_filter] if island_type_filter else []

    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        visits_by_day = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_day_30 = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_hour = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY hour ORDER BY hour",
                it_params,
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY authorized",
            it_params,
        ).fetchall()
        # Visits by island category (public vs member/VIP)
        cat_raw = db.execute(
            "SELECT isl.cat, COUNT(*) AS visit_count "
            "FROM island_visits iv "
            "JOIN islands isl ON LOWER(iv.destination) = isl.id "
            f"{'WHERE iv.island_type = ?' if island_type_filter else ''} "
            "GROUP BY isl.cat",
            it_params,
        ).fetchall()
        # Top users per action type (WARN, KICK, BAN, NOTE)
        _VALID_COUNT_KEYS = {"warn_count", "kick_count", "ban_count", "note_count"}

        def _top_by_action(action: str, count_key: str):
            if count_key not in _VALID_COUNT_KEYS:
                raise ValueError(f"Invalid count_key: {count_key!r}")
            if island_type_filter:
                rows = db.execute(
                    f"SELECT w.user_id, COUNT(*) AS {count_key} "
                    "FROM warnings w "
                    "JOIN island_visits iv ON w.visit_id = iv.id "
                    "WHERE w.user_id IS NOT NULL AND iv.island_type = ? AND UPPER(w.action_type) = ? "
                    f"GROUP BY w.user_id ORDER BY {count_key} DESC LIMIT 10",
                    (island_type_filter, action),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT user_id, COUNT(*) AS {count_key} "
                    "FROM warnings WHERE user_id IS NOT NULL AND UPPER(action_type) = ? "
                    f"GROUP BY user_id ORDER BY {count_key} DESC LIMIT 10",
                    (action,),
                ).fetchall()
            return [dict(r) for r in rows]

        top_warned  = _top_by_action("WARN",    "warn_count")
        top_kicked  = _top_by_action("KICK",    "kick_count")
        top_banned  = _top_by_action("BAN",     "ban_count")
        top_noted   = _top_by_action("NOTE",    "note_count")

        all_action_user_ids = (
            [r["user_id"] for r in top_warned]
            + [r["user_id"] for r in top_kicked]
            + [r["user_id"] for r in top_banned]
            + [r["user_id"] for r in top_noted]
        )
        action_name_map = _resolve_discord_usernames(all_action_user_ids)
        for collection in (top_warned, top_kicked, top_banned, top_noted):
            for row in collection:
                row["user_name"] = action_name_map.get(str(row["user_id"]), str(row["user_id"]))
        # Quick summary stats
        visits_today = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours'){it_clause}",
            it_params,
        ).fetchone()[0]
        visits_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        if island_type_filter:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','-7 days') "
                "AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
        else:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings "
                "WHERE timestamp > strftime('%s','now','-7 days')"
            ).fetchone()[0]
        # Day-of-week breakdown (0=Sunday … 6=Saturday)
        dow_raw = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%w', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS dow, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY dow ORDER BY dow",
                it_params,
            ).fetchall()
        ]
        # New vs returning travelers (7d and 30d)
        new_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-7 days')",
            it_params,
        ).fetchone()[0]
        total_unique_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        new_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-30 days')",
            it_params,
        ).fetchone()[0]
        total_unique_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        # All-time unique travelers and islands
        total_unique_travelers = db.execute(
            f"SELECT COUNT(DISTINCT ign) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
        total_unique_islands = db.execute(
            f"SELECT COUNT(DISTINCT destination) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
        # Visits in the previous week (7–14 days ago) for week-over-week delta
        visits_prev_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-14 days') "
            f"AND timestamp <= strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        # Warnings issued today
        if island_type_filter:
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','+8 hours','start of day','-8 hours') "
                "AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
        else:
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings "
                "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
            ).fetchone()[0]
        # Peak hour (hour with the most visits all-time, in UTC+8)
        peak_hour_row = db.execute(
            "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
            "COUNT(*) AS cnt "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY hour ORDER BY cnt DESC LIMIT 1",
            it_params,
        ).fetchone()
        peak_hour = peak_hour_row["hour"] if peak_hour_row else None
        # Average visits per day over the last 30 days
        avg_visits_30d_row = db.execute(
            "SELECT COUNT(*) * 1.0 / 30 AS avg FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()
        avg_visits_30d = round(avg_visits_30d_row["avg"] or 0, 1)
    except Exception:
        top_islands = top_travelers = visits_by_day = visits_by_day_30 = []
        visits_by_hour = []
        auth_raw = []
        cat_raw = []
        top_warned = []
        top_kicked = []
        top_banned = []
        top_noted = []
        visits_today = visits_week = warnings_week = 0
        dow_raw = []
        new_7d = total_unique_7d = new_30d = total_unique_30d = 0
        total_unique_travelers = total_unique_islands = 0
        visits_prev_week = warnings_today = 0
        peak_hour = None
        avg_visits_30d = 0.0
    finally:
        db.close()

    auth_map   = {r["authorized"]: r["count"] for r in auth_raw}
    auth_stats = {"authorized": auth_map.get(1, 0), "unauthorized": auth_map.get(0, 0)}
    cat_map    = {r["cat"]: r["visit_count"] for r in cat_raw}
    cat_stats  = {"public": cat_map.get("public", 0), "member": cat_map.get("member", 0)}

    # Build full 24-hour array (fill missing hours with 0)
    hour_map = {r["hour"]: r["count"] for r in visits_by_hour}
    visits_by_hour_full = [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]

    # Build full 7-day-of-week array (fill missing days with 0)
    dow_map = {r["dow"]: r["count"] for r in dow_raw}
    visits_by_dow = [{"dow": d, "label": _DOW_LABELS[d], "count": dow_map.get(d, 0)} for d in range(7)]

    returning_7d  = max(total_unique_7d  - new_7d,  0)
    returning_30d = max(total_unique_30d - new_30d, 0)
    new_returning = {
        "new_7d":  new_7d,  "returning_7d":  returning_7d,  "total_7d":  total_unique_7d,
        "new_30d": new_30d, "returning_30d": returning_30d, "total_30d": total_unique_30d,
    }

    total_visits = auth_stats["authorized"] + auth_stats["unauthorized"]
    auth_rate_pct = round(auth_stats["authorized"] / total_visits * 100) if total_visits else None
    warn_rate_week = round(warnings_week / visits_week * 100, 1) if visits_week else 0.0

    return render_template(
        "dashboard/analytics.html",
        top_islands=top_islands,
        top_travelers=top_travelers,
        visits_by_day=visits_by_day,
        visits_by_day_30=visits_by_day_30,
        visits_by_hour=visits_by_hour_full,
        visits_by_dow=visits_by_dow,
        auth_stats=auth_stats,
        cat_stats=cat_stats,
        top_warned=top_warned,
        top_kicked=top_kicked,
        top_banned=top_banned,
        top_noted=top_noted,
        visits_today=visits_today,
        visits_week=visits_week,
        warnings_week=warnings_week,
        warnings_today=warnings_today,
        new_returning=new_returning,
        island_type_filter=island_type_filter,
        total_unique_travelers=total_unique_travelers,
        total_unique_islands=total_unique_islands,
        visits_prev_week=visits_prev_week,
        peak_hour=peak_hour,
        avg_visits_30d=avg_visits_30d,
        auth_rate_pct=auth_rate_pct,
        warn_rate_week=warn_rate_week,
    )


@dashboard.route("/database")
@admin_required
def database():
    """Admin database tools: inspect DB backend and run SQLite -> MariaDB copy."""
    return render_template(
        "dashboard/database.html",
        db_backend=get_backend(),
        mariadb=_mariadb_settings_payload(),
    )


@dashboard.route("/analytics/export.csv")
@admin_required
def analytics_export_csv():
    """Export visit log data as a CSV download."""
    return _analytics_csv_response()


@dashboard.route("/api/analytics/export.csv")
@api_auth_required
def api_analytics_export_csv():
    """Export visit log data as CSV for the React dashboard."""
    return _analytics_csv_response()


def _analytics_csv_response():
    """Build a CSV export response for dashboard analytics."""
    island_type_filter = request.args.get("island_type", "").lower()
    if island_type_filter not in ("free", "sub"):
        island_type_filter = ""

    it_clause = " AND island_type = ?" if island_type_filter else ""
    it_params = [island_type_filter] if island_type_filter else []

    db = get_db()
    try:
        # Limit to 10 000 rows to keep response size and memory usage reasonable.
        rows = db.execute(
            "SELECT ign, origin_island, destination, island_type, authorized, "
            "datetime(timestamp, 'unixepoch', '+8 hours') AS visit_time "
            f"FROM island_visits WHERE 1=1{it_clause} "
            "ORDER BY timestamp DESC LIMIT 10000",
            it_params,
        ).fetchall()
    except Exception:
        rows = []
    finally:
        db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["IGN", "Origin Island", "Destination", "Island Type", "Authorized", "Visit Time (UTC+8)"])
    for r in rows:
        writer.writerow([
            r["ign"],
            r["origin_island"],
            r["destination"],
            r["island_type"],
            "Yes" if r["authorized"] else "No",
            r["visit_time"],
        ])

    filename = f"chobot_visits{'_' + island_type_filter if island_type_filter else ''}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===========================================================================
# JSON CRUD API  (Bearer token OR active browser session)
# ===========================================================================

def _parse_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _parse_positive_int(value, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _request_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or ""


def _dashboard_actor() -> tuple[str | None, str | None]:
    user_id = session.get("discord_user_id")
    name = session.get("discord_username") or session.get("discord_global_name")
    bearer_user = _dashboard_bearer_user()
    if bearer_user:
        user_id = user_id or str(bearer_user.get("user_id") or bearer_user.get("id") or "")
        name = name or bearer_user.get("username") or bearer_user.get("global_name")
    return (str(user_id) if user_id else None, str(name) if name else None)


def _record_audit_event(action: str, target: str | None = None, details: dict | None = None) -> None:
    db = get_db()
    try:
        actor_user_id, actor_name = _dashboard_actor()
        db.execute(
            """
            INSERT INTO dashboard_audit_events
            (actor_user_id, actor_name, action, target, details, ip_address, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                actor_name,
                action,
                target,
                json.dumps(details or {}, sort_keys=True),
                _request_ip(),
                int(time.time()),
            ),
        )
        db.commit()
    except Exception as exc:
        logger.debug("Audit event insert failed: %s", exc)
    finally:
        db.close()


def _sqlite_table_counts() -> dict[str, int]:
    source = inspect_sqlite_source(_DB_PATH)
    return {name: int(meta["rows"] or 0) for name, meta in source["tables"].items()}


def _mariadb_settings_payload() -> dict:
    missing = []
    if not Config.MYSQL_HOST:
        missing.append("MYSQL_HOST")
    if not Config.MYSQL_USER:
        missing.append("MYSQL_USER")
    if not Config.MYSQL_DATABASE:
        missing.append("MYSQL_DATABASE")

    return {
        "configured": not missing,
        "missing": missing,
        "host": Config.MYSQL_HOST,
        "port": Config.MYSQL_PORT,
        "user": Config.MYSQL_USER,
        "database": Config.MYSQL_DATABASE,
        "default_truncate_before_import": Config.MARIADB_TRUNCATE_BEFORE_IMPORT,
    }


@dashboard.route("/api/mariadb-migration/status", methods=["GET"])
@api_auth_required
def api_mariadb_migration_status():
    """Return SQLite source counts and MariaDB migration configuration status."""
    try:
        source = inspect_sqlite_source(_DB_PATH)
        source_tables = {name: int(meta["rows"] or 0) for name, meta in source["tables"].items()}
    except Exception as exc:
        return jsonify({"error": f"Could not inspect SQLite database: {exc}"}), 500

    return jsonify({
        "runtime_database": get_backend(),
        "sqlite_path": _DB_PATH,
        "sqlite_exists": os.path.exists(_DB_PATH),
        "source_tables": source_tables,
        "source_total_rows": source["total_rows"],
        "persistent_total_rows": source["persistent_rows"],
        "skipped_tables": [name for name, meta in source["tables"].items() if meta["skipped"]],
        "mariadb": _mariadb_settings_payload(),
        "migration_running": _mariadb_migration_lock.locked(),
        "last_result": _mariadb_migration_last_result,
        "note": "Use DB_BACKEND=mysql to run ChoBot against MariaDB/MySQL after migrating data.",
    })


@dashboard.route("/api/mariadb-migration", methods=["POST"])
@api_auth_required
def api_mariadb_migration_run():
    """Copy existing SQLite data into MariaDB without changing the running SQLite flow."""
    global _mariadb_migration_last_result

    data = request.get_json(silent=True) or {}
    dry_run = _parse_bool(data.get("dry_run"), False)
    truncate_before_import = _parse_bool(
        data.get("truncate_before_import"),
        Config.MARIADB_TRUNCATE_BEFORE_IMPORT,
    )

    if not _mariadb_migration_lock.acquire(blocking=False):
        return jsonify({"error": "MariaDB migration is already running"}), 409

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        if dry_run:
            report = dry_run_sqlite_to_mariadb(
                sqlite_path=_DB_PATH,
                host=Config.MARIADB_HOST,
                port=Config.MARIADB_PORT,
                user=Config.MARIADB_USER,
                password=Config.MARIADB_PASSWORD,
                database=Config.MARIADB_DATABASE,
            )
            source_tables = {
                name: int(meta["rows"] or 0)
                for name, meta in report["source"]["tables"].items()
            }
            _mariadb_migration_last_result = {
                "ok": True,
                "dry_run": True,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "source_tables": source_tables,
                "source_total_rows": report["source"]["total_rows"],
                "persistent_total_rows": report["source"]["persistent_rows"],
                "target_database_exists": report["target_database_exists"],
                "target_tables": report["target_tables"],
                "schema_drift": report["schema_drift"],
                "warnings": report["warnings"],
                "mariadb": _mariadb_settings_payload(),
            }
            _record_audit_event(
                "mariadb_migration_dry_run",
                Config.MARIADB_DATABASE,
                {
                    "source_total_rows": report["source"]["total_rows"],
                    "schema_drift_tables": sorted(report["schema_drift"]),
                },
            )
            return jsonify(_mariadb_migration_last_result)

        summary = migrate_sqlite_to_mariadb_detailed(
            sqlite_path=_DB_PATH,
            host=Config.MARIADB_HOST,
            port=Config.MARIADB_PORT,
            user=Config.MARIADB_USER,
            password=Config.MARIADB_PASSWORD,
            database=Config.MARIADB_DATABASE,
            truncate_before_import=truncate_before_import,
        )
        _mariadb_migration_last_result = {
            **summary,
            "runtime_database": get_backend(),
            "sqlite_preserved": True,
            "note": "Migration copied data to MariaDB. Set DB_BACKEND=mysql to use it as the app database.",
        }
        _record_audit_event(
            "mariadb_migration_run",
            Config.MARIADB_DATABASE,
            {
                "truncate_before_import": truncate_before_import,
                "total_rows_copied": summary["total_rows_copied"],
                "backup_path": summary["backup_path"],
                "validation_ok": bool(summary.get("validation", {}).get("ok")),
            },
        )
        return jsonify(_mariadb_migration_last_result)
    except Exception as exc:
        logger.exception("MariaDB migration failed")
        _mariadb_migration_last_result = {
            "ok": False,
            "dry_run": dry_run,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        return jsonify(_mariadb_migration_last_result), 500
    finally:
        _mariadb_migration_lock.release()


@dashboard.route("/api/database/maintenance", methods=["POST"])
@api_auth_required
def api_database_maintenance():
    """Prune volatile and stale operational rows."""
    data = request.get_json(silent=True) or {}
    warning_days = _parse_positive_int(data.get("warning_days"), 3)
    reveal_days = _parse_positive_int(data.get("reveal_days"), 30)
    audit_days = _parse_positive_int(data.get("audit_days"), 180)
    now = int(time.time())

    deleted: dict[str, int] = {}
    db = get_db()
    try:
        cur = db.execute("DELETE FROM command_claims")
        deleted["command_claims"] = max(cur.rowcount, 0)

        cur = db.execute(
            "DELETE FROM warnings WHERE timestamp IS NOT NULL AND timestamp < ?",
            (now - warning_days * 86400,),
        )
        deleted["expired_warnings"] = max(cur.rowcount, 0)

        cur = db.execute(
            "DELETE FROM dodo_reveal_messages WHERE created_at < ?",
            (now - reveal_days * 86400,),
        )
        deleted["stale_dodo_reveals"] = max(cur.rowcount, 0)

        cur = db.execute(
            "DELETE FROM dashboard_audit_events WHERE created_at < ?",
            (now - audit_days * 86400,),
        )
        deleted["old_audit_events"] = max(cur.rowcount, 0)

        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Database maintenance failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    _record_audit_event(
        "database_maintenance",
        "database",
        {
            "deleted": deleted,
            "warning_days": warning_days,
            "reveal_days": reveal_days,
            "audit_days": audit_days,
        },
    )
    return jsonify({"ok": True, "deleted": deleted})


@dashboard.route("/api/audit-events", methods=["GET"])
@api_auth_required
def api_audit_events():
    """Return recent dashboard/system audit entries."""
    limit = min(max(request.args.get("limit", 25, type=int), 1), 100)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM dashboard_audit_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "entries": []}), 500
    finally:
        db.close()

    entries = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.get("details") or "{}")
        except (TypeError, ValueError):
            item["details"] = {}
        item["created_at_text"] = _ts_to_str(item.get("created_at"))
        entries.append(item)

    return jsonify({"ok": True, "entries": entries})


@dashboard.route("/api/status-summary", methods=["GET"])
@api_auth_required
def api_status_summary():
    """Return live island status counts and per-island effective statuses."""
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
        bot_status = _load_bot_status_map(db)
    except Exception:
        db_islands = []
        bot_status = {}
    finally:
        db.close()

    island_count     = len(db_islands)
    online_count     = 0
    refreshing_count = 0
    offline_count    = 0
    islands_out      = []
    access_problem_count = 0

    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("id", ""))
        s = _effective_status(isl)
        access_status = _island_access_status(isl)
        if access_status["warnings"]:
            access_problem_count += 1
        islands_out.append({
            "id": isl.get("id", ""),
            "name": isl.get("name", ""),
            "status": s,
            "access_source": access_status["access_source"],
            "role_count": access_status["role_count"],
            "access_warnings": access_status["warnings"],
        })
        if s == STATUS_ONLINE:
            online_count += 1
        elif s == STATUS_REFRESHING:
            refreshing_count += 1
        else:
            offline_count += 1

    def _pct(count):
        return round(count * 100 / island_count) if island_count else 0

    return jsonify({
        "island_count":     island_count,
        "online_count":     online_count,
        "refreshing_count": refreshing_count,
        "offline_count":    offline_count,
        "online_pct":       _pct(online_count),
        "refreshing_pct":   _pct(refreshing_count),
        "off_pct":          _pct(offline_count),
        "access_problem_count": access_problem_count,
        "islands":          islands_out,
    })


@dashboard.route("/api/island-health", methods=["GET"])
@api_auth_required
def api_island_health():
    """Return per-island operational health signals."""
    stale_minutes = max(request.args.get("stale_minutes", 15, type=int), 1)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
        bot_status = _load_bot_status_map(db)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "islands": []}), 500
    finally:
        db.close()

    items = []
    for island in db_islands:
        updated_raw = island.get("updated_at") or ""
        updated_dt = None
        if updated_raw:
            with contextlib.suppress(Exception):
                updated_dt = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        warnings = []
        status = _effective_status(island)
        bot_online = bot_status.get(island.get("id", ""))
        if status == STATUS_OFFLINE:
            warnings.append("offline")
        if bot_online is False:
            warnings.append("bot_offline")
        if updated_dt and updated_dt < stale_cutoff:
            warnings.append("stale_update")
        access_status = _island_access_status(island)
        warnings.extend(access_status["warnings"])
        items.append({
            "id": island.get("id", ""),
            "name": island.get("name", ""),
            "status": status,
            "visitors": island.get("visitors") or 0,
            "discord_bot_online": bot_online,
            "updated_at": updated_raw,
            "access_source": access_status["access_source"],
            "warnings": warnings,
            "ok": not warnings,
        })

    return jsonify({
        "ok": True,
        "stale_minutes": stale_minutes,
        "problem_count": sum(1 for item in items if item["warnings"]),
        "islands": items,
    })


@dashboard.route("/api/user-trust-profile", methods=["GET"])
@api_auth_required
def api_user_trust_profile():
    """Return a compact moderation/trust profile for a Discord user."""
    user_id = (request.args.get("user_id") or "").strip()
    guild_id = (request.args.get("guild_id") or str(Config.GUILD_ID or "")).strip()
    if not user_id:
        return jsonify({"ok": False, "error": "user_id is required"}), 400

    db = get_db()
    try:
        params = [user_id]
        guild_clause = ""
        if guild_id:
            guild_clause = " AND guild_id = ?"
            params.append(guild_id)

        visit_summary = db.execute(
            "SELECT COUNT(*) AS total_visits, "
            "SUM(CASE WHEN authorized = 1 THEN 1 ELSE 0 END) AS authorized_visits, "
            "SUM(CASE WHEN authorized = 0 THEN 1 ELSE 0 END) AS unauthorized_visits, "
            "MAX(timestamp) AS last_visit_at "
            f"FROM island_visits WHERE user_id = ?{guild_clause}",
            params,
        ).fetchone()
        warning_summary = db.execute(
            "SELECT COUNT(*) AS total_actions, "
            "SUM(CASE WHEN UPPER(action_type) = 'WARN' THEN 1 ELSE 0 END) AS warnings, "
            "SUM(CASE WHEN UPPER(action_type) = 'KICK' THEN 1 ELSE 0 END) AS kicks, "
            "SUM(CASE WHEN UPPER(action_type) = 'BAN' THEN 1 ELSE 0 END) AS bans, "
            "MAX(timestamp) AS last_action_at "
            f"FROM warnings WHERE user_id = ?{guild_clause}",
            params,
        ).fetchone()
        recent_visits = db.execute(
            "SELECT ign, destination, authorized, timestamp "
            f"FROM island_visits WHERE user_id = ?{guild_clause} "
            "ORDER BY timestamp DESC LIMIT 10",
            params,
        ).fetchall()
        recent_actions = db.execute(
            "SELECT action_type, reason, mod_id, timestamp "
            f"FROM warnings WHERE user_id = ?{guild_clause} "
            "ORDER BY timestamp DESC LIMIT 10",
            params,
        ).fetchall()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

    total_visits = int(visit_summary["total_visits"] or 0)
    total_actions = int(warning_summary["total_actions"] or 0)
    risk_score = min(
        100,
        int(warning_summary["warnings"] or 0) * 20
        + int(warning_summary["kicks"] or 0) * 35
        + int(warning_summary["bans"] or 0) * 60
        + int(visit_summary["unauthorized_visits"] or 0) * 5,
    )
    return jsonify({
        "ok": True,
        "user_id": user_id,
        "guild_id": guild_id,
        "user_name": _resolve_discord_username(user_id),
        "risk_score": risk_score,
        "summary": {
            "total_visits": total_visits,
            "authorized_visits": int(visit_summary["authorized_visits"] or 0),
            "unauthorized_visits": int(visit_summary["unauthorized_visits"] or 0),
            "total_actions": total_actions,
            "warnings": int(warning_summary["warnings"] or 0),
            "kicks": int(warning_summary["kicks"] or 0),
            "bans": int(warning_summary["bans"] or 0),
            "last_visit_at": _ts_to_str(visit_summary["last_visit_at"]),
            "last_action_at": _ts_to_str(warning_summary["last_action_at"]),
        },
        "recent_visits": [
            {
                "ign": row["ign"],
                "destination": row["destination"],
                "authorized": bool(row["authorized"]),
                "timestamp": _ts_to_str(row["timestamp"]),
            }
            for row in recent_visits
        ],
        "recent_actions": [
            {
                "action_type": row["action_type"],
                "reason": row["reason"],
                "mod_id": row["mod_id"],
                "timestamp": _ts_to_str(row["timestamp"]),
            }
            for row in recent_actions
        ],
    })


@dashboard.route("/api/islands", methods=["GET"])
@api_auth_required
def api_islands_list():
    """List all islands."""
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = _merge_dashboard_fs_islands([_row_to_island_dict(dict(r)) for r in rows])
        bot_status = _load_bot_status_map(db)
    except Exception:
        db_islands = []
        bot_status = {}
    finally:
        db.close()

    result = []
    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("id", ""))
        isl["status"] = _effective_status(isl)
        access_info = island_access.resolved_island_required_roles(
            isl.get("name"),
            isl.get("cat"),
            isl.get("required_roles") or [],
            isl.get("type"),
            isl.get("channel_id"),
        )
        isl["required_roles"] = access_info.required_roles
        isl["channel_id"] = access_info.channel_id
        isl["access_source"] = access_info.access_source
        result.append(_island_api_dict(isl))
    return jsonify(result)


@dashboard.route("/api/islands/role-status", methods=["GET"])
@api_auth_required
def api_island_role_status():
    """Return Discord role-gating diagnostics for dashboard island rows."""
    force_refresh = request.args.get("refresh") in {"1", "true", "yes"}
    if force_refresh:
        island_access.clear_access_caches()
    islands = _load_dashboard_islands()
    statuses = [_island_access_status(isl, force_refresh=force_refresh) for isl in islands]
    problem_count = sum(1 for item in statuses if item["warnings"])
    return jsonify({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "discord_configured": bool(Config.DISCORD_TOKEN and Config.GUILD_ID),
        "category_id": str(Config.CATEGORY_ID or ""),
        "total": len(statuses),
        "member_islands": sum(1 for item in statuses if item["is_member"]),
        "problem_count": problem_count,
        "items": statuses,
    })


@dashboard.route("/api/islands/sync-roles", methods=["POST"])
@api_auth_required
def api_island_sync_roles():
    """Refresh stored island required_roles/channel_id from Discord channel permissions."""
    islands = _load_dashboard_islands()
    db = get_db()
    try:
        summary = island_access.sync_island_role_cache(db, islands, force_refresh=True)
        db.commit()
    finally:
        db.close()
    summary.update({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "discord_configured": bool(Config.DISCORD_TOKEN and Config.GUILD_ID),
    })
    return jsonify(summary)


@dashboard.route("/api/islands/test-access", methods=["POST"])
@api_auth_required
def api_island_test_access():
    """Test island access for a set of Discord role IDs or a Discord user ID."""
    data = request.get_json(silent=True) or {}
    roles = [str(role) for role in data.get("roles", []) if str(role)]
    user_id = str(data.get("user_id") or "").strip()
    is_admin = bool(data.get("is_admin"))
    is_mod_user = bool(data.get("is_mod"))

    if user_id and not roles:
        member = island_access.discord_api_json(f"/guilds/{Config.GUILD_ID}/members/{user_id}")
        if isinstance(member, dict):
            roles = [str(role) for role in member.get("roles", []) if str(role)]
        else:
            return jsonify({"error": "Discord member not found or unavailable"}), 404

    if not roles and not is_admin and not is_mod_user:
        return jsonify({"error": "Provide roles, user_id, is_mod, or is_admin"}), 400

    is_mod_user = is_mod_user or is_admin or island_access.is_mod(roles)
    role_names = island_access.get_guild_role_names()
    islands = _load_dashboard_islands()
    results = []
    for isl in islands:
        info = island_access.resolved_island_required_roles(
            isl.get("name"),
            isl.get("cat"),
            isl.get("required_roles") or [],
            isl.get("type"),
            isl.get("channel_id"),
        )
        accessible = island_access.has_island_access(roles, info.required_roles, is_mod_user)
        matched_role_ids = sorted(set(roles) & set(info.required_roles))
        results.append({
            "id": isl.get("id"),
            "name": isl.get("name"),
            "type": isl.get("type"),
            "cat": isl.get("cat"),
            "channel_id": info.channel_id,
            "access_source": info.access_source,
            "accessible": accessible,
            "required_roles": [island_access.role_payload(role_id, role_names) for role_id in info.required_roles],
            "matched_roles": [island_access.role_payload(role_id, role_names) for role_id in matched_role_ids],
        })

    return jsonify({
        "user_id": user_id or None,
        "roles": [island_access.role_payload(role_id, role_names) for role_id in roles],
        "is_mod": is_mod_user,
        "accessible_count": sum(1 for item in results if item["accessible"]),
        "items": results,
    })


@dashboard.route("/api/islands", methods=["POST"])
@api_auth_required
def api_island_create():
    """Create or upsert a full island record."""
    data      = request.get_json(silent=True) or {}
    island_id = (data.get("id") or data.get("name", "")).strip().lower()
    name      = (data.get("name") or island_id).strip().upper()
    display_name = (data.get("display_name") or data.get("displayName") or "").strip() or None
    is_visible = _json_bool(data, "is_visible", _json_bool(data, "isVisible", True))
    isl_type  = data.get("type", "")
    items     = data.get("items", [])
    theme     = data.get("theme", "teal")
    cat       = data.get("cat", "public")
    desc      = data.get("description", "")
    seasonal  = data.get("seasonal", "")
    status    = data.get("status", "OFFLINE")
    visitors  = int(data.get("visitors", 0))
    dodo_code = data.get("dodoCode") or data.get("dodo_code") or None
    map_url   = data.get("mapUrl")   or data.get("map_url")   or None

    if not island_id:
        return jsonify({"error": "id or name is required"}), 400
    if cat    not in ALLOWED_CATEGORIES: return jsonify({"error": f"cat must be one of {ALLOWED_CATEGORIES}"}),  400
    if theme  not in ALLOWED_THEMES:     return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}),    400
    if status not in ALLOWED_STATUSES:   return jsonify({"error": f"status must be one of {ALLOWED_STATUSES}"}), 400

    if (dodo_code or "").strip().upper() == REFRESHING_DODO_CODE:
        status = STATUS_REFRESHING

    db = get_db()
    try:
        db.execute(
            """INSERT INTO islands
                   (id, name, display_name, is_visible, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, display_name=excluded.display_name,
                   is_visible=excluded.is_visible, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (island_id, name, display_name, int(is_visible), isl_type, json.dumps(items),
             theme, cat, desc, seasonal, status, visitors, dodo_code, map_url,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "ok", "id": island_id}), 201


@dashboard.route("/api/islands/<name>", methods=["GET"])
@api_auth_required
def api_island_get(name):
    """Get a single island record."""
    island_id = name.lower()
    db = get_db()
    try:
        row = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
    finally:
        db.close()
    if row:
        island = _row_to_island_dict(dict(row))
    else:
        fs_match = None
        for fs in _collect_fs_islands().values():
            if str(fs.get("name", "")).lower() == island_id:
                fs_match = fs
                break
        if not fs_match:
            return jsonify({"error": f'Island "{name}" not found'}), 404
        island = _merge_island(_fs_island_stub(fs_match), fs_match)
    access_info = island_access.resolved_island_required_roles(
        island.get("name"),
        island.get("cat"),
        island.get("required_roles") or [],
        island.get("type"),
        island.get("channel_id"),
    )
    island["required_roles"] = access_info.required_roles
    island["channel_id"] = access_info.channel_id
    island["access_source"] = access_info.access_source
    payload = _island_detail_api_dict(island)
    payload["access_status"] = _island_access_status(island)
    return jsonify(payload)


@dashboard.route("/api/islands/<name>", methods=["PUT"])
@api_auth_required
def api_island_update(name):
    """Update a single island record (partial or full)."""
    island_id = name.lower()
    data      = request.get_json(silent=True) or {}

    db = get_db()
    try:
        row      = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
        existing = _row_to_island_dict(dict(row)) if row else {}
    finally:
        db.close()

    cat    = data.get("cat",    existing.get("cat",    "public"))
    theme  = data.get("theme",  existing.get("theme",  "teal"))
    status = data.get("status", existing.get("status", "OFFLINE"))

    if cat    not in ALLOWED_CATEGORIES: return jsonify({"error": f"cat must be one of {ALLOWED_CATEGORIES}"}),  400
    if theme  not in ALLOWED_THEMES:     return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}),    400
    if status not in ALLOWED_STATUSES:   return jsonify({"error": f"status must be one of {ALLOWED_STATUSES}"}), 400

    items_in = data.get("items", existing.get("items", []))
    if isinstance(items_in, str):
        try:
            items_in = json.loads(items_in)
        except ValueError:
            items_in = [i.strip() for i in items_in.split(",") if i.strip()]

    display_name = (
        data.get("display_name")
        if "display_name" in data
        else data.get("displayName", existing.get("display_name"))
    )
    display_name = (display_name or "").strip() or None
    is_visible = _json_bool(data, "is_visible", _json_bool(data, "isVisible", existing.get("is_visible", True)))

    dodo_code = data.get("dodoCode") or data.get("dodo_code") or existing.get("dodo_code")
    if (dodo_code or "").strip().upper() == REFRESHING_DODO_CODE:
        status = STATUS_REFRESHING

    db2 = get_db()
    try:
        db2.execute(
            """INSERT INTO islands
                   (id, name, display_name, is_visible, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, display_name=excluded.display_name,
                   is_visible=excluded.is_visible, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (
                island_id,
                data.get("name", existing.get("name", island_id.upper())).upper(),
                display_name,
                int(is_visible),
                data.get("type",        existing.get("type",        "")),
                json.dumps(items_in),
                theme, cat,
                data.get("description", existing.get("description", "")),
                data.get("seasonal",    existing.get("seasonal",    "")),
                status,
                int(data.get("visitors", existing.get("visitors", 0))),
                dodo_code,
                existing.get("map_url"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db2.commit()
    finally:
        db2.close()
    return jsonify({"status": "ok", "id": island_id})


@dashboard.route("/api/islands/<name>", methods=["DELETE"])
@api_auth_required
def api_island_delete(name):
    """Delete stored metadata for an island (does not touch the filesystem)."""
    island_id = name.lower()
    db = get_db()
    try:
        db.execute("DELETE FROM islands WHERE id = ?", (island_id,))
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "deleted", "id": island_id})


@dashboard.route("/api/islands/<name>/map", methods=["POST"])
@api_auth_required
def api_island_upload_map(name):
    """Upload an island map image to Cloudflare R2 and store the URL."""
    island_id = name.lower()

    if "map" not in request.files:
        return jsonify({"error": "No file part named 'map'"}), 400
    file = request.files["map"]
    if not file or not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    file_bytes = file.read()
    if len(file_bytes) > MAX_MAP_SIZE:
        return jsonify({"error": f"File too large (max {MAX_MAP_SIZE // 1024 // 1024} MB)"}), 413

    content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "image/png"
    if content_type not in ALLOWED_MAP_TYPES:
        return jsonify({"error": f"Unsupported type: {content_type}. Allowed: {sorted(ALLOWED_MAP_TYPES)}"}), 415

    try:
        map_url = _upload_map_to_r2(file_bytes, content_type, island_id)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except (ClientError, NoCredentialsError) as exc:
        logger.error("R2 upload failed for island %s: %s", island_id, exc)
        return jsonify({"error": "R2 upload failed", "details": str(exc)}), 502

    db = get_db()
    try:
        db.execute(
            "UPDATE islands SET map_url = ?, updated_at = ? WHERE id = ?",
            (map_url, datetime.now(timezone.utc).isoformat(), island_id),
        )
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO islands (id, name, map_url, updated_at) VALUES (?,?,?,?)",
                (island_id, island_id.upper(), map_url, datetime.now(timezone.utc).isoformat()),
            )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "uploaded", "id": island_id, "map_url": map_url})


@dashboard.route("/api/islands/sync-maps", methods=["POST"])
@api_auth_required
def api_sync_maps():
    """Scan the R2 bucket for existing map images and back-fill map_url in the DB.

    For every object under the ``maps/`` prefix in the configured R2 bucket,
    derive the island id from the object key (e.g. ``maps/alapaap.jpg``
    → island id ``alapaap``), construct the public URL, and write it into the
    ``islands`` table.  Rows that already have a ``map_url`` are also updated
    so that any manually renamed/re-uploaded files are corrected.

    Returns a JSON summary ``{"synced": N, "skipped": N, "errors": [...]}``.
    """
    client = _get_r2_client()
    if client is None:
        return jsonify({"error": "R2 is not configured"}), 503

    base = (Config.R2_PUBLIC_URL or "").rstrip("/")
    if not base:
        return jsonify({"error": "R2_PUBLIC_URL is not configured"}), 503

    # Collect all objects under maps/ prefix (handle paginated responses)
    keys: list[str] = []
    kwargs: dict = {"Bucket": Config.R2_BUCKET_NAME, "Prefix": "maps/"}
    while True:
        try:
            resp = client.list_objects_v2(**kwargs)
        except (ClientError, NoCredentialsError) as exc:
            return jsonify({"error": "R2 list failed", "details": str(exc)}), 502
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        else:
            break

    synced = 0
    skipped = 0
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()

    db = get_db()
    try:
        for key in keys:
            # key looks like "maps/alapaap.jpg" or "maps/subdirectory/..." – skip nested
            parts = key.split("/")
            if len(parts) != 2:
                skipped += 1
                continue
            filename = parts[1]
            if not filename:
                skipped += 1
                continue
            # Strip extension to get island id
            island_id = filename.rsplit(".", 1)[0].lower()
            if not island_id:
                skipped += 1
                continue
            map_url = f"{base}/{key}"
            try:
                db.execute(
                    "UPDATE islands SET map_url = ?, updated_at = ? WHERE id = ?",
                    (map_url, now, island_id),
                )
                if db.execute("SELECT changes()").fetchone()[0] == 0:
                    # Island row doesn't exist yet — create a minimal one
                    db.execute(
                        "INSERT OR IGNORE INTO islands (id, name, map_url, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (island_id, island_id.upper(), map_url, now),
                    )
                synced += 1
            except Exception as exc:
                errors.append(f"{island_id}: {exc}")
        db.commit()
    finally:
        db.close()

    return jsonify({"synced": synced, "skipped": skipped, "errors": errors})


@dashboard.route("/api/analytics", methods=["GET"])
@api_auth_required
def api_analytics():
    """Return full analytics dataset as JSON.

    Accepts an optional ``island_type`` query parameter (``free`` or ``sub``)
    to filter results to a specific island type.
    """
    island_type_filter = request.args.get("island_type", "").lower()
    if island_type_filter not in ("free", "sub"):
        island_type_filter = ""

    it_clause = " AND island_type = ?" if island_type_filter else ""
    it_params = [island_type_filter] if island_type_filter else []

    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY destination ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY ign ORDER BY visit_count DESC LIMIT 10",
                it_params,
            ).fetchall()
        ]
        visits_by_day = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_day_30 = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause} "
                "GROUP BY day ORDER BY day",
                it_params,
            ).fetchall()
        ]
        visits_by_hour = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY hour ORDER BY hour",
                it_params,
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY authorized",
            it_params,
        ).fetchall()
        cat_raw = db.execute(
            "SELECT isl.cat, COUNT(*) AS visit_count "
            "FROM island_visits iv "
            "JOIN islands isl ON LOWER(iv.destination) = isl.id "
            f"{'WHERE iv.island_type = ?' if island_type_filter else ''} "
            "GROUP BY isl.cat",
            it_params,
        ).fetchall()
        dow_raw = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%w', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS dow, "
                "COUNT(*) AS count "
                f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
                "GROUP BY dow ORDER BY dow",
                it_params,
            ).fetchall()
        ]
        _VALID_COUNT_KEYS = {"warn_count", "kick_count", "ban_count", "note_count"}
        _VALID_ACTIONS    = {"WARN", "KICK", "BAN", "NOTE", "ADMIT", "DISMISS"}

        def _top_by_action(action: str, count_key: str):
            if count_key not in _VALID_COUNT_KEYS:
                raise ValueError(f"Invalid count_key: {count_key!r}")
            if action not in _VALID_ACTIONS:
                raise ValueError(f"Invalid action: {action!r}")
            if island_type_filter:
                rows = db.execute(
                    f"SELECT w.user_id, COUNT(*) AS {count_key} "
                    "FROM warnings w "
                    "JOIN island_visits iv ON w.visit_id = iv.id "
                    "WHERE w.user_id IS NOT NULL AND iv.island_type = ? AND UPPER(w.action_type) = ? "
                    f"GROUP BY w.user_id ORDER BY {count_key} DESC LIMIT 10",
                    (island_type_filter, action),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT user_id, COUNT(*) AS {count_key} "
                    "FROM warnings WHERE user_id IS NOT NULL AND UPPER(action_type) = ? "
                    f"GROUP BY user_id ORDER BY {count_key} DESC LIMIT 10",
                    (action,),
                ).fetchall()
            return [dict(r) for r in rows]

        top_warned = _top_by_action("WARN", "warn_count")
        top_kicked = _top_by_action("KICK", "kick_count")
        top_banned = _top_by_action("BAN",  "ban_count")
        top_noted  = _top_by_action("NOTE", "note_count")

        all_action_user_ids = (
            [r["user_id"] for r in top_warned]
            + [r["user_id"] for r in top_kicked]
            + [r["user_id"] for r in top_banned]
            + [r["user_id"] for r in top_noted]
        )
        action_name_map = _resolve_discord_usernames(all_action_user_ids)
        for collection in (top_warned, top_kicked, top_banned, top_noted):
            for row in collection:
                row["user_name"] = action_name_map.get(str(row["user_id"]), str(row["user_id"]))

        visits_today = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours'){it_clause}",
            it_params,
        ).fetchone()[0]
        visits_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        if island_type_filter:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','-7 days') AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings w "
                "JOIN island_visits iv ON w.visit_id = iv.id "
                "WHERE w.timestamp > strftime('%s','now','+8 hours','start of day','-8 hours') "
                "AND iv.island_type = ?",
                it_params,
            ).fetchone()[0]
        else:
            warnings_week = db.execute(
                "SELECT COUNT(*) FROM warnings WHERE timestamp > strftime('%s','now','-7 days')"
            ).fetchone()[0]
            warnings_today = db.execute(
                "SELECT COUNT(*) FROM warnings "
                "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
            ).fetchone()[0]
        visits_prev_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-14 days') "
            f"AND timestamp <= strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        peak_hour_row = db.execute(
            "SELECT CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER) AS hour, "
            "COUNT(*) AS cnt "
            f"FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "GROUP BY hour ORDER BY cnt DESC LIMIT 1",
            it_params,
        ).fetchone()
        peak_hour = peak_hour_row["hour"] if peak_hour_row else None
        avg_visits_30d_row = db.execute(
            "SELECT COUNT(*) * 1.0 / 30 AS avg FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()
        avg_visits_30d = round(avg_visits_30d_row["avg"] or 0, 1)
        new_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-7 days')",
            it_params,
        ).fetchone()[0]
        total_unique_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-7 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        new_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit "
            f"  FROM island_visits {'WHERE island_type = ?' if island_type_filter else ''} "
            "  GROUP BY ign"
            f") WHERE first_visit > strftime('%s','now','-30 days')",
            it_params,
        ).fetchone()[0]
        total_unique_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            f"WHERE timestamp > strftime('%s','now','-30 days'){it_clause}",
            it_params,
        ).fetchone()[0]
        total_unique_travelers = db.execute(
            f"SELECT COUNT(DISTINCT ign) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
        total_unique_islands = db.execute(
            f"SELECT COUNT(DISTINCT destination) FROM island_visits"
            f"{' WHERE island_type = ?' if island_type_filter else ''}",
            it_params,
        ).fetchone()[0]
    except Exception:
        top_islands = top_travelers = visits_by_day = visits_by_day_30 = []
        visits_by_hour = dow_raw = []
        auth_raw = []
        cat_raw = []
        top_warned = top_kicked = top_banned = top_noted = []
        visits_today = visits_week = warnings_week = warnings_today = 0
        visits_prev_week = 0
        peak_hour = None
        avg_visits_30d = 0.0
        new_7d = total_unique_7d = new_30d = total_unique_30d = 0
        total_unique_travelers = total_unique_islands = 0
    finally:
        db.close()

    auth_map  = {r["authorized"]: r["count"] for r in auth_raw}
    cat_map   = {r["cat"]: r["visit_count"] for r in cat_raw}
    hour_map  = {r["hour"]: r["count"] for r in visits_by_hour}
    dow_map   = {r["dow"]: r["count"] for r in dow_raw}

    auth_stats = {"authorized": auth_map.get(1, 0), "unauthorized": auth_map.get(0, 0)}
    cat_stats  = {"public": cat_map.get("public", 0), "member": cat_map.get("member", 0)}
    visits_by_hour_full = [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]
    visits_by_dow = [{"dow": d, "label": _DOW_LABELS[d], "count": dow_map.get(d, 0)} for d in range(7)]

    total_visits  = auth_stats["authorized"] + auth_stats["unauthorized"]
    auth_rate_pct = round(auth_stats["authorized"] / total_visits * 100) if total_visits else None
    warn_rate_week = round(warnings_week / visits_week * 100, 1) if visits_week else 0.0

    returning_7d  = max(total_unique_7d  - new_7d,  0)
    returning_30d = max(total_unique_30d - new_30d, 0)

    return jsonify({
        # Basic summary (backward-compatible)
        "top_islands":         top_islands,
        "top_travelers":       top_travelers,
        "authorized_visits":   auth_stats["authorized"],
        "unauthorized_visits": auth_stats["unauthorized"],
        # Extended analytics
        "visits_by_day":       visits_by_day,
        "visits_by_day_30":    visits_by_day_30,
        "visits_by_hour":      visits_by_hour_full,
        "visits_by_dow":       visits_by_dow,
        "auth_stats":          auth_stats,
        "cat_stats":           cat_stats,
        "top_warned":          top_warned,
        "top_kicked":          top_kicked,
        "top_banned":          top_banned,
        "top_noted":           top_noted,
        "visits_today":        visits_today,
        "visits_week":         visits_week,
        "warnings_week":       warnings_week,
        "warnings_today":      warnings_today,
        "visits_prev_week":    visits_prev_week,
        "peak_hour":           peak_hour,
        "avg_visits_30d":      avg_visits_30d,
        "total_unique_travelers": total_unique_travelers,
        "total_unique_islands":   total_unique_islands,
        "auth_rate_pct":       auth_rate_pct,
        "warn_rate_week":      warn_rate_week,
        "new_returning": {
            "new_7d":        new_7d,
            "returning_7d":  returning_7d,
            "total_7d":      total_unique_7d,
            "new_30d":       new_30d,
            "returning_30d": returning_30d,
            "total_30d":     total_unique_30d,
        },
        "island_type_filter": island_type_filter,
    })


@dashboard.route("/api/website-logins", methods=["GET"])
@api_auth_required
def api_website_logins():
    """Return paginated Discord website login audit events."""
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 25, type=int), 1), 100)
    search = (request.args.get("q") or "").strip()
    access = (request.args.get("access") or "all").strip().lower()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    conditions = []
    params = []

    if search:
        like = f"%{search}%"
        conditions.append(
            "(user_id LIKE ? OR username LIKE ? OR discord_name LIKE ? OR "
            "global_name LIKE ? OR account_name LIKE ? OR nickname LIKE ? OR ip_address LIKE ?)"
        )
        params.extend([like, like, like, like, like, like, like])

    if access == "mod":
        conditions.append("is_mod = 1")
    elif access == "admin":
        conditions.append("is_admin = 1")
    elif access == "regular":
        conditions.append("is_mod = 0 AND is_admin = 0")

    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= ?")
        params.append(f"{date_to}T23:59:59Z" if len(date_to) == 10 else date_to)

    where_sql = _where_clause(conditions)
    offset = (page - 1) * per_page

    db = get_db()
    try:
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM website_login_events {where_sql}",
            params,
        ).fetchone()["count"]
        rows = db.execute(
            "SELECT * FROM website_login_events "
            f"{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
        summary = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN is_mod = 1 THEN 1 ELSE 0 END) AS mod_count, "
            "SUM(CASE WHEN is_admin = 1 THEN 1 ELSE 0 END) AS admin_count "
            "FROM website_login_events"
        ).fetchone()
    except Exception as exc:
        logger.exception("Failed to load website login events")
        return jsonify({"error": str(exc)}), 500
    finally:
        db.close()

    entries = []
    for row in rows:
        item = dict(row)
        try:
            item["roles"] = json.loads(item.get("roles") or "[]")
        except (TypeError, ValueError):
            item["roles"] = []
        item["is_admin"] = bool(item.get("is_admin"))
        item["is_mod"] = bool(item.get("is_mod"))
        entries.append(item)

    return jsonify({
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "entries": entries,
        "filters": {
            "q": search,
            "access": access,
            "from": date_from,
            "to": date_to,
        },
        "summary": {
            "total": int(summary["total"] or 0),
            "mod_count": int(summary["mod_count"] or 0),
            "admin_count": int(summary["admin_count"] or 0),
        },
    })


@dashboard.route("/api/logs", methods=["GET"])
@api_auth_required
def api_logs():
    """Return paginated flight-log or warning entries as JSON.

    Query parameters
    ----------------
    type            : ``flights`` (default) or ``warnings``
    page            : page number (default 1)
    per_page        : rows per page, capped at 100 (default 25)
    ign             : IGN substring filter
    island          : island name filter (flights only)
    authorized      : ``0`` or ``1`` (flights only)
    category        : ``public`` or ``member`` (flights only)
    sort_by         : ``timestamp`` (default), ``ign``, or ``destination`` (flights only)
    sort_order      : ``desc`` (default) or ``asc`` (flights only)
    action_type     : ``WARN``, ``KICK``, ``BAN``, ``DISMISS``, ``NOTE``, ``ADMIT`` (warnings only)
    """
    log_type          = request.args.get("type", "flights")
    page              = request.args.get("page", 1, type=int)
    per_page          = min(request.args.get("per_page", 25, type=int), 100)
    ign_filter        = request.args.get("ign", "").strip()
    island_filter     = request.args.get("island", "").strip()
    authorized_filter = request.args.get("authorized", "")
    category_filter   = request.args.get("category", "")
    sort_by           = request.args.get("sort_by", "timestamp")
    sort_order        = request.args.get("sort_order", "desc")
    _ALLOWED_ACTION_TYPES = {"WARN", "KICK", "BAN", "DISMISS", "NOTE", "ADMIT"}
    action_type_filter = request.args.get("action_type", "").strip().upper()
    if action_type_filter not in _ALLOWED_ACTION_TYPES:
        action_type_filter = ""
    if sort_by not in _ALLOWED_SORT_COLS:
        sort_by = "timestamp"
    sort_order = "asc" if sort_order == "asc" else "desc"

    db = get_db()
    try:
        island_names = [
            r[0] for r in db.execute("SELECT name FROM islands ORDER BY name").fetchall()
        ]
        if log_type == "warnings":
            conditions, params = [], []
            if ign_filter:
                conditions.append("LOWER(iv.ign) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if action_type_filter:
                conditions.append("UPPER(w.action_type) = ?")
                params.append(action_type_filter)
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT w.*, iv.ign, iv.destination "
                f"FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where} ORDER BY w.timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            name_map = _resolve_discord_usernames(
                [r["user_id"] for r in rows if r["user_id"]]
                + [r["mod_id"] for r in rows if r["mod_id"]]
            )
            entries = [
                {
                    "user_id":     r["user_id"],
                    "user_name":   name_map.get(str(r["user_id"]), str(r["user_id"])) if r["user_id"] else "—",
                    "reason":      r["reason"],
                    "mod_id":      r["mod_id"],
                    "mod_name":    name_map.get(str(r["mod_id"]), str(r["mod_id"])) if r["mod_id"] else "—",
                    "timestamp":   _ts_to_str(r["timestamp"]),
                    "ign":         r["ign"],
                    "destination": r["destination"],
                    "action_type": r["action_type"],
                }
                for r in rows
            ]
        else:
            conditions, params = [], []
            use_island_join = bool(category_filter in ("public", "member"))

            if island_filter:
                col = "iv.destination" if use_island_join else "destination"
                conditions.append(f"LOWER({col}) = LOWER(?)")
                params.append(island_filter)
            if ign_filter:
                col = "iv.ign" if use_island_join else "ign"
                conditions.append(f"LOWER({col}) LIKE LOWER(?)")
                params.append(f"%{ign_filter}%")
            if authorized_filter in ("0", "1"):
                col = "iv.authorized" if use_island_join else "authorized"
                conditions.append(f"{col} = ?")
                params.append(int(authorized_filter))
            if use_island_join:
                conditions.append("isl.cat = ?")
                params.append(category_filter)

            if use_island_join:
                join_sql   = ("FROM island_visits iv "
                              "JOIN islands isl ON LOWER(iv.destination) = isl.id")
                order_sql  = f"iv.{sort_by} {sort_order.upper()}"
                where      = _where_clause(conditions)
                total      = db.execute(
                    f"SELECT COUNT(*) {join_sql} {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT iv.* {join_sql} {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()
            else:
                where      = _where_clause(conditions)
                order_sql  = f"{sort_by} {sort_order.upper()}"
                total      = db.execute(
                    f"SELECT COUNT(*) FROM island_visits {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT * FROM island_visits {where} "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    params + [per_page, (page - 1) * per_page],
                ).fetchall()

            entries = [
                {
                    "id":            r["id"],
                    "ign":           r["ign"],
                    "origin_island": r["origin_island"],
                    "destination":   r["destination"],
                    "authorized":    bool(r["authorized"]),
                    "timestamp":     _ts_to_str(r["timestamp"]),
                    "user_id":       r["user_id"],
                }
                for r in rows
            ]
            flight_name_map = _resolve_discord_usernames([r["user_id"] for r in rows if r["user_id"]])
            for e in entries:
                e["user_name"] = flight_name_map.get(str(e["user_id"])) if e["user_id"] else None
    except Exception:
        total, entries, island_names = 0, [], []
    finally:
        db.close()

    return jsonify({
        "page":        page,
        "per_page":    per_page,
        "total":       total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "log_type":    log_type,
        "entries":     entries,
        "island_names": island_names,
    })


@dashboard.route("/api/overview", methods=["GET"])
@api_auth_required
def api_overview():
    """Return the data powering the Overview dashboard page as JSON."""
    db = get_db()
    try:
        total_visits   = db.execute("SELECT COUNT(*) FROM island_visits").fetchone()[0]
        total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        visits_today   = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','+8 hours','start of day','-8 hours')"
        ).fetchone()[0]
        visits_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        warnings_week = db.execute(
            "SELECT COUNT(*) FROM warnings "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        recent_raw = db.execute(
            "SELECT ign, destination, authorized, timestamp, user_id "
            "FROM island_visits ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        top_islands_raw = db.execute(
            "SELECT destination, COUNT(*) AS visit_count "
            "FROM island_visits GROUP BY destination "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        top_travelers_raw = db.execute(
            "SELECT ign, COUNT(*) AS visit_count "
            "FROM island_visits GROUP BY ign "
            "ORDER BY visit_count DESC LIMIT 5"
        ).fetchall()
        trend_raw = db.execute(
            "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
            "FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days') "
            "GROUP BY day ORDER BY day"
        ).fetchall()
    except Exception:
        total_visits = total_warnings = visits_today = visits_week = warnings_week = 0
        recent_raw = []
        top_islands_raw = []
        top_travelers_raw = []
        trend_raw = []
    finally:
        db.close()

    recent_user_ids = [r["user_id"] for r in recent_raw if r["user_id"]]
    recent_name_map = _resolve_discord_usernames(recent_user_ids) if recent_user_ids else {}

    recent = [
        {
            "ign":         r["ign"],
            "destination": r["destination"],
            "authorized":  bool(r["authorized"]),
            "timestamp":   _ts_to_str(r["timestamp"]),
            "user_name":   recent_name_map.get(str(r["user_id"])) if r["user_id"] else None,
        }
        for r in recent_raw
    ]

    top_islands  = [{"name": r["destination"], "count": r["visit_count"]} for r in top_islands_raw]
    top_travelers = [{"ign": r["ign"], "count": r["visit_count"]} for r in top_travelers_raw]

    trend_map = {r["day"]: r["count"] for r in trend_raw}
    today_dt  = datetime.now(timezone.utc)
    trend_labels, trend_counts = [], []
    for offset in range(6, -1, -1):
        d = (today_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        trend_labels.append(d[-5:])
        trend_counts.append(trend_map.get(d, 0))

    warn_rate_7d = round(warnings_week / visits_week * 100, 1) if visits_week > 0 else 0

    db2 = get_db()
    try:
        rows2       = db2.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands2 = [_row_to_island_dict(dict(r)) for r in rows2]
        bot_status2 = _load_bot_status_map(db2)
    except Exception:
        db_islands2 = []
        bot_status2 = {}
    finally:
        db2.close()

    for isl in db_islands2:
        isl["discord_bot_online"] = bot_status2.get(isl.get("id", ""))

    island_count = len(db_islands2)
    status_map: dict[str, int] = {STATUS_ONLINE: 0, STATUS_REFRESHING: 0, STATUS_OFFLINE: 0}
    for isl in db_islands2:
        s = _effective_status(isl)
        status_map[s] = status_map.get(s, 0) + 1

    online_count = status_map[STATUS_ONLINE]
    online_pct   = round(online_count / island_count * 100) if island_count else 0

    return jsonify({
        "total_visits":   total_visits,
        "total_warnings": total_warnings,
        "visits_today":   visits_today,
        "visits_week":    visits_week,
        "warnings_week":  warnings_week,
        "warn_rate_7d":   warn_rate_7d,
        "island_count":   island_count,
        "online_count":   online_count,
        "online_pct":     online_pct,
        "status_map":     status_map,
        "top_islands":    top_islands,
        "top_travelers":  top_travelers,
        "trend_labels":   trend_labels,
        "trend_counts":   trend_counts,
        "recent":         recent,
    })

