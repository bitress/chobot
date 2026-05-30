"""
ChoBot Web Dashboard
Mod-only web interface for island management, XLog reports, and analytics.
Access is protected by a secret key (DASHBOARD_SECRET env var).
"""

import json
import os
import re
import csv
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
from utils.database import connect_db, get_backend, get_engine, get_default_tenant_id
from utils.db_migration import migrate_sqlite_to_mariadb

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

ALLOWED_CATEGORIES = ("public", "member")
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
        req = urllib.request.Request(
            f"https://discord.com/api/v10/users/{uid}",
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
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


def current_tenant_id() -> str:
    """Return the tenant scoped to this dashboard request.

    The app is still running in legacy single-tenant mode, so this defaults to
    the configured ChoPaeng tenant. Future onboarding can set this from the
    authenticated dashboard session.
    """
    if session.get("tenant_id"):
        return session["tenant_id"]
    if _check_bearer():
        requested = request.headers.get("X-Tenant-ID") or request.args.get("tenant_id")
        if requested:
            return _normalize_tenant_id(requested)
    return get_default_tenant_id()


def _normalize_tenant_id(value: str) -> str:
    """Normalize a user-facing tenant id/slug into a stable database key."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (value or "").strip().lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-_")
    return cleaned[:64]


def _tenant_exists(tenant_id: str) -> bool:
    if not tenant_id:
        return False
    db = get_db()
    try:
        return db.execute("SELECT 1 FROM tenants WHERE id = ? LIMIT 1", (tenant_id,)).fetchone() is not None
    finally:
        db.close()


def _tenant_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "status": row["status"],
        "plan": row["plan"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_tenant_row(tenant_id: str):
    db = get_db()
    try:
        return db.execute(
            "SELECT id, name, slug, status, plan, created_at, updated_at FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
    finally:
        db.close()


def _load_tenant_settings(conn, tenant_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM tenant_settings WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def _set_tenant_setting(conn, tenant_id: str, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO tenant_settings (tenant_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(tenant_id, key) DO UPDATE SET value=excluded.value
        """,
        (tenant_id, key, value or ""),
    )


def _tenant_setup_complete(settings: dict[str, str], tenant: dict | None = None) -> bool:
    if tenant and tenant.get("id") == get_default_tenant_id():
        return True
    return bool(settings.get("onboarding.completed_at"))


def _parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _string_id(value) -> str:
    return str(value or "").strip()


def _tenant_select_payload() -> list[dict]:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, slug, status, plan, created_at, updated_at FROM tenants ORDER BY name"
        ).fetchall()
        return [_tenant_row_to_dict(row) for row in rows]
    finally:
        db.close()


def _current_tenant_payload() -> dict | None:
    tenant_id = current_tenant_id()
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, name, slug, status, plan, created_at, updated_at FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if not row:
            return None
        tenant = _tenant_row_to_dict(row)
        tenant["settings"] = _load_tenant_settings(db, tenant_id)
        return tenant
    finally:
        db.close()


def _require_dashboard_admin():
    if not _check_bearer() and _get_session_role() != "admin":
        abort(403)


def _upsert_tenant_configs(
    conn,
    tenant_id: str,
    discord_cfg: dict,
    twitch_cfg: dict | None,
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO tenant_discord_configs (
            tenant_id, guild_id, member_category_id, free_category_id, log_channel_id,
            flight_listen_channel_id, free_flight_listen_channel_id, flight_log_channel_id,
            mod_role_id, island_access_role_id, bot_enabled, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tenant_id) DO UPDATE SET
            guild_id=excluded.guild_id,
            member_category_id=excluded.member_category_id,
            free_category_id=excluded.free_category_id,
            log_channel_id=excluded.log_channel_id,
            flight_listen_channel_id=excluded.flight_listen_channel_id,
            free_flight_listen_channel_id=excluded.free_flight_listen_channel_id,
            flight_log_channel_id=excluded.flight_log_channel_id,
            mod_role_id=excluded.mod_role_id,
            island_access_role_id=excluded.island_access_role_id,
            bot_enabled=excluded.bot_enabled,
            updated_at=excluded.updated_at
        """,
        (
            tenant_id,
            _string_id(discord_cfg.get("guild_id")),
            _string_id(discord_cfg.get("member_category_id")),
            _string_id(discord_cfg.get("free_category_id")),
            _string_id(discord_cfg.get("log_channel_id")),
            _string_id(discord_cfg.get("flight_listen_channel_id")),
            _string_id(discord_cfg.get("free_flight_listen_channel_id")),
            _string_id(discord_cfg.get("flight_log_channel_id")),
            _string_id(discord_cfg.get("mod_role_id")),
            _string_id(discord_cfg.get("island_access_role_id")),
            1 if _parse_bool(discord_cfg.get("bot_enabled"), True) else 0,
            now,
        ),
    )

    if twitch_cfg is not None:
        conn.execute(
            """
            INSERT INTO tenant_twitch_configs (tenant_id, channel_name, bot_enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                channel_name=excluded.channel_name,
                bot_enabled=excluded.bot_enabled,
                updated_at=excluded.updated_at
            """,
            (
                tenant_id,
                _string_id(twitch_cfg.get("channel_name")),
                1 if _parse_bool(twitch_cfg.get("bot_enabled"), bool(twitch_cfg.get("channel_name"))) else 0,
                now,
            ),
        )


def _save_onboarding_islands(conn, tenant_id: str, islands: list[dict]) -> int:
    saved = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for item in islands or []:
        raw_name = _string_id(item.get("name") or item.get("id"))
        island_id = _normalize_tenant_id(item.get("id") or raw_name)
        if not island_id or not raw_name:
            continue
        storage_id = _storage_island_id(tenant_id, island_id)
        cat = item.get("cat") if item.get("cat") in ALLOWED_CATEGORIES else "public"
        theme = item.get("theme") if item.get("theme") in ALLOWED_THEMES else "teal"
        status = item.get("status") if item.get("status") in ALLOWED_STATUSES else "OFFLINE"
        items = item.get("items") or []
        if isinstance(items, str):
            items = [part.strip() for part in items.split(",") if part.strip()]
        required_roles = item.get("required_roles") or []
        if isinstance(required_roles, str):
            required_roles = [part.strip() for part in required_roles.split(",") if part.strip()]
        conn.execute(
            """
            INSERT INTO islands
                (id, tenant_id, name, type, items, theme, cat, description, seasonal,
                 status, visitors, dodo_code, map_url, updated_at, required_roles, channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id=excluded.tenant_id,
                name=excluded.name,
                type=excluded.type,
                items=excluded.items,
                theme=excluded.theme,
                cat=excluded.cat,
                description=excluded.description,
                seasonal=excluded.seasonal,
                status=excluded.status,
                visitors=excluded.visitors,
                dodo_code=excluded.dodo_code,
                map_url=excluded.map_url,
                updated_at=excluded.updated_at,
                required_roles=excluded.required_roles,
                channel_id=excluded.channel_id
            """,
            (
                storage_id,
                tenant_id,
                raw_name.upper(),
                _string_id(item.get("type")),
                json.dumps(items),
                theme,
                cat,
                _string_id(item.get("description")),
                _string_id(item.get("seasonal")),
                status,
                int(item.get("visitors") or 0),
                item.get("dodo_code"),
                item.get("map_url"),
                now_iso,
                json.dumps([str(role) for role in required_roles if str(role)]),
                _string_id(item.get("channel_id")) or None,
            ),
        )
        saved += 1
    return saved


def init_dashboard_db():
    """Create dashboard-specific tables if they do not already exist."""
    try:
        conn = get_db()

        # Full IslandData-compatible table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS islands (
                id             TEXT PRIMARY KEY,
                tenant_id      TEXT NOT NULL DEFAULT 'chopaeng',
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
                channel_id     TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE islands ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'chopaeng'")
            conn.commit()
        except Exception:
            pass

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

        # Live island bot presence, written by the Discord bot's monitor loop
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_bot_status (
                island_id   TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL DEFAULT 'chopaeng',
                island_name TEXT NOT NULL,
                is_online   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE island_bot_status ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'chopaeng'")
            conn.commit()
        except Exception:
            pass

        # Legacy table kept for backward compatibility
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_metadata (
                name       TEXT PRIMARY KEY,
                tenant_id  TEXT NOT NULL DEFAULT 'chopaeng',
                category   TEXT NOT NULL DEFAULT 'public',
                theme      TEXT NOT NULL DEFAULT 'teal',
                notes      TEXT NOT NULL DEFAULT '',
                updated_at TEXT
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
        return auth[len("Bearer "):] == Config.DASHBOARD_SECRET
    return False


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
    """Decorator for JSON API routes — returns 401 when token/session is missing."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_bearer() and not _check_session():
            return jsonify({"error": "Unauthorized — send 'Authorization: Bearer <DASHBOARD_SECRET>'"}), 401
        return f(*args, **kwargs)
    return _decorated


# ---------------------------------------------------------------------------
# Template context processor — injects current_role into every page
# ---------------------------------------------------------------------------
@dashboard.context_processor
def _inject_user():
    try:
        available_tenants = _tenant_select_payload() if _check_session() else []
        current_tenant = _current_tenant_payload() if _check_session() else None
    except Exception:
        available_tenants = []
        current_tenant = None
    return {
        "current_role":       session.get("mod_role", "admin"),
        "current_tenant_id":  current_tenant_id(),
        "current_tenant":     current_tenant,
        "available_tenants":  available_tenants,
        "discord_username":   session.get("discord_username", ""),
        "discord_user_id":    session.get("discord_user_id", ""),
        "discord_avatar_url": session.get("discord_avatar_url", ""),
        "oauth_configured":   bool(Config.DISCORD_CLIENT_ID),
        "discord_client_id":  Config.DISCORD_CLIENT_ID,
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


def _public_island_id(stored_id: str | None) -> str:
    """Return the tenant-local island id exposed to dashboards and APIs."""
    value = str(stored_id or "")
    if ":" in value:
        return value.split(":", 1)[1]
    return value


def _storage_island_id(tenant_id: str, island_id: str) -> str:
    """Return the globally unique island id used by the current legacy schema."""
    local_id = _normalize_tenant_id(island_id)
    if tenant_id == get_default_tenant_id():
        return local_id
    return f"{tenant_id}:{local_id}"


def row_to_island_dict(row: dict) -> dict:
    """Decode JSON columns and return a plain dict."""
    row["_storage_id"] = row.get("id")
    row["id"] = _public_island_id(row.get("id"))
    try:
        row["items"] = json.loads(row.get("items") or "[]")
    except (ValueError, TypeError):
        row["items"] = []
    try:
        row["required_roles"] = json.loads(row.get("required_roles") or "[]")
    except (ValueError, TypeError):
        row["required_roles"] = []
    return row


def _load_bot_status_map(conn) -> dict:
    """Return a dict of island_id → bool (is_online) from island_bot_status."""
    try:
        rows = conn.execute(
            "SELECT island_id, is_online FROM island_bot_status WHERE tenant_id = ?",
            (current_tenant_id(),),
        ).fetchall()
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
    "map_url", "name", "required_roles", "seasonal", "status", "theme",
    "type", "updated_at", "visitors",
)


def _island_api_dict(isl: dict) -> dict:
    """Return a clean API-facing dict containing only canonical island fields."""
    return {field: isl.get(field) for field in _API_ISLAND_FIELDS}


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


@dashboard.before_request
def _restrict_to_console_domain():
    """Return 404 for any request that did not arrive via an allowed dashboard host."""
    host = request.host.split(":")[0]  # strip optional port
    if host not in _ALLOWED_DASHBOARD_HOSTS:
        abort(404)


@dashboard.errorhandler(403)
def _forbidden(_e):
    return render_template("dashboard/403.html"), 403

@dashboard.errorhandler(500)
def _internal_server_error(e):
    logger.exception("Internal server error: %s", e)
    return render_template("dashboard/500.html"), 500

@dashboard.route("/login", methods=["GET", "POST"])
def login():
    if _check_session():
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        secret = request.form.get("secret", "")
        if secret and Config.DASHBOARD_SECRET and secret == Config.DASHBOARD_SECRET:
            session["mod_logged_in"] = True
            session["mod_role"]      = "admin"
            session["tenant_id"]     = get_default_tenant_id()
            session.permanent        = True
            return redirect(url_for("dashboard.index"))
        flash("Invalid secret key. Please try again.", "error")
    return render_template("dashboard/login.html")


@dashboard.route("/logout")
def logout():
    session.pop("mod_logged_in",       None)
    session.pop("mod_role",            None)
    session.pop("discord_user_id",     None)
    session.pop("discord_username",    None)
    session.pop("discord_avatar_url",  None)
    session.pop("oauth_state",         None)
    session.pop("tenant_id",           None)
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
        req = urllib.request.Request(
            "https://discord.com/api/oauth2/token",
            data=token_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent":   _DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_resp = json.loads(resp.read().decode())
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
        mem_req = urllib.request.Request(
            f"https://discord.com/api/users/@me/guilds/{Config.GUILD_ID}/member",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
        )
        with urllib.request.urlopen(mem_req, timeout=10) as resp:
            member_data = json.loads(resp.read().decode())
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
        user_req = urllib.request.Request(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent":    _DISCORD_USER_AGENT,
            },
        )
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            user_data = json.loads(resp.read().decode())
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
    session["tenant_id"]          = get_default_tenant_id()
    session.permanent          = True
    logger.info("OAuth login: user=%s role=%s", discord_username, role)
    tenant = _current_tenant_payload()
    if tenant and not _tenant_setup_complete(tenant.get("settings", {}), tenant):
        return redirect(url_for("dashboard.onboarding"))
    return redirect(url_for("dashboard.index"))


@dashboard.route("/")
@admin_required
def index():
    tenant = _current_tenant_payload()
    if tenant and not _tenant_setup_complete(tenant.get("settings", {}), tenant):
        return redirect(url_for("dashboard.onboarding"))
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
        rows2        = db2.execute(
            "SELECT * FROM islands WHERE tenant_id = ? ORDER BY name",
            (current_tenant_id(),),
        ).fetchall()
        db_islands2  = [_row_to_island_dict(dict(r)) for r in rows2]
        bot_status2  = _load_bot_status_map(db2)
    except Exception:
        db_islands2 = []
        bot_status2 = {}
    finally:
        db2.close()

    for isl in db_islands2:
        isl["discord_bot_online"] = bot_status2.get(isl.get("_storage_id", ""), bot_status2.get(isl.get("id", "")))

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


@dashboard.route("/onboarding")
@admin_required
def onboarding():
    """Tenant setup wizard for SaaS customer onboarding."""
    tenant = _current_tenant_payload()
    tenant_id = current_tenant_id()
    db = get_db()
    try:
        discord_cfg = db.execute(
            "SELECT * FROM tenant_discord_configs WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        twitch_cfg = db.execute(
            "SELECT * FROM tenant_twitch_configs WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    finally:
        db.close()
    return render_template(
        "dashboard/onboarding.html",
        tenant=tenant,
        discord_cfg=dict(discord_cfg) if discord_cfg else {},
        twitch_cfg=dict(twitch_cfg) if twitch_cfg else {},
        allowed_categories=ALLOWED_CATEGORIES,
        allowed_themes=ALLOWED_THEMES,
        default_tenant_id=get_default_tenant_id(),
    )


@dashboard.route("/islands")
@admin_required
def islands():
    tenant_id = current_tenant_id()
    db = get_db()
    try:
        rows       = db.execute(
            "SELECT * FROM islands WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ).fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except Exception:
        db_islands = []
    finally:
        db.close()

    fs_map     = _collect_fs_islands()
    merged     = []
    seen_names = set()

    for isl in db_islands:
        uname = isl["name"].upper()
        seen_names.add(uname)
        merged.append(_merge_island(isl, fs_map.get(uname)))

    # Islands on filesystem but not yet in DB
    for uname, fs in fs_map.items():
        if uname not in seen_names:
            stub = {
                "id": uname.lower(), "name": uname, "type": "", "items": [],
                "theme": "teal", "cat": "public", "description": "", "seasonal": "",
                "status": "OFFLINE", "visitors": 0, "dodo_code": None,
                "map_url": None, "updated_at": None,
            }
            merged.append(_merge_island(stub, fs))

    merged.sort(key=lambda x: x["name"])
    return render_template("dashboard/islands.html", islands=merged)


@dashboard.route("/islands/<name>", methods=["GET", "POST"])
@admin_required
def island_detail(name):
    island_id = name.lower()
    storage_id = _storage_island_id(current_tenant_id(), island_id)
    upper     = name.upper()
    tenant_id = current_tenant_id()

    db = get_db()
    try:
        row  = db.execute(
            "SELECT * FROM islands WHERE tenant_id = ? AND id = ?",
            (tenant_id, storage_id),
        ).fetchone()
        meta = _row_to_island_dict(dict(row)) if row else None
    finally:
        db.close()

    # Locate filesystem path
    fs_path = fs_type = None
    for directory, itype in [(Config.DIR_FREE, "Free"), (Config.DIR_VIP, "VIP")]:
        if not directory:
            continue
        for candidate_name in [upper, name]:
            candidate = os.path.join(directory, candidate_name)
            if os.path.isdir(candidate):
                fs_path, fs_type = candidate, itype
                break
        if fs_path:
            break

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
                           (id, tenant_id, name, type, items, theme, cat, description, seasonal,
                            status, visitors, dodo_code, map_url, updated_at, required_roles)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                           name=excluded.name, type=excluded.type, items=excluded.items,
                           theme=excluded.theme, cat=excluded.cat,
                           description=excluded.description, seasonal=excluded.seasonal,
                           status=excluded.status, visitors=excluded.visitors,
                           dodo_code=excluded.dodo_code, updated_at=excluded.updated_at""",
                    (
                        storage_id, tenant_id, upper, isl_type, json.dumps(items_list),
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

    # Per-island 7-day visit sparkline
    sparkline_7d = []
    db_sp = get_db()
    try:
        sparkline_7d = [
            dict(r) for r in db_sp.execute(
                "SELECT DATE(timestamp, 'unixepoch', '+8 hours') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE tenant_id = ? AND LOWER(destination) = LOWER(?) "
                "AND timestamp > strftime('%s','now','-7 days') "
                "GROUP BY day ORDER BY day",
                (tenant_id, upper),
            ).fetchall()
        ]
    except Exception:
        sparkline_7d = []
    finally:
        db_sp.close()

    r2_configured = bool(Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY)

    return render_template(
        "dashboard/island_detail.html",
        island=island,
        allowed_categories=ALLOWED_CATEGORIES,
        allowed_themes=ALLOWED_THEMES,
        allowed_statuses=ALLOWED_STATUSES,
        r2_configured=r2_configured,
        sparkline_7d=sparkline_7d,
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
                "SELECT name FROM islands WHERE tenant_id = ? ORDER BY name",
                (current_tenant_id(),),
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
        rows = db.execute(
            "SELECT * FROM islands WHERE tenant_id = ? ORDER BY name",
            (current_tenant_id(),),
        ).fetchall()
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
        isl["discord_bot_online"] = bot_status.get(isl.get("_storage_id", ""), bot_status.get(isl.get("id", "")))

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

def _sqlite_table_counts() -> dict[str, int]:
    db = get_db()
    try:
        if get_backend() == "mysql":
            from sqlalchemy import inspect

            table_names = sorted(inspect(get_engine()).get_table_names())
        else:
            table_names = [
                row["name"] for row in db.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                ).fetchall()
            ]
        counts: dict[str, int] = {}
        for table_name in table_names:
            safe_table = str(table_name).replace('"', '""')
            count = db.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
            counts[table_name] = int(count or 0)
        return counts
    finally:
        db.close()


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


@dashboard.route("/api/tenants", methods=["GET"])
@api_auth_required
def api_tenants_list():
    """List tenant accounts available to this ChoBot deployment."""
    db = get_db()
    try:
        tenant_rows = db.execute(
            "SELECT id, name, slug, status, plan, created_at, updated_at FROM tenants ORDER BY name"
        ).fetchall()
        island_counts = {
            row["tenant_id"]: int(row["count"] or 0)
            for row in db.execute(
                "SELECT tenant_id, COUNT(*) AS count FROM islands GROUP BY tenant_id"
            ).fetchall()
        }
        tenants = []
        for row in tenant_rows:
            tenant = _tenant_row_to_dict(row)
            tenant["island_count"] = island_counts.get(tenant["id"], 0)
            tenant["current"] = tenant["id"] == current_tenant_id()
            tenants.append(tenant)
    finally:
        db.close()
    return jsonify({"current_tenant_id": current_tenant_id(), "tenants": tenants})


@dashboard.route("/api/tenants/current", methods=["GET", "PATCH"])
@api_auth_required
def api_tenant_current():
    """Return the tenant currently selected for dashboard/API operations."""
    if request.method == "PATCH":
        _require_dashboard_admin()
        data = request.get_json(silent=True) or {}
        tenant_id = current_tenant_id()
        name = (data.get("name") or "").strip()
        slug = _normalize_tenant_id(data.get("slug") or data.get("public_slug") or "")
        plan = (data.get("plan") or "").strip().lower()[:64]
        status = (data.get("status") or "").strip().lower()[:64]
        settings = data.get("settings") or {}
        branding = data.get("branding") or {}
        now = int(time.time())

        db = get_db()
        try:
            row = db.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if not row:
                return jsonify({"error": f'Tenant "{tenant_id}" not found'}), 404
            if name or slug or plan or status:
                current = db.execute(
                    "SELECT name, slug, plan, status FROM tenants WHERE id = ?",
                    (tenant_id,),
                ).fetchone()
                db.execute(
                    "UPDATE tenants SET name = ?, slug = ?, plan = ?, status = ?, updated_at = ? WHERE id = ?",
                    (
                        name or current["name"],
                        slug or current["slug"],
                        plan or current["plan"],
                        status or current["status"],
                        now,
                        tenant_id,
                    ),
                )
            merged_settings = {
                "brand.logo_url": branding.get("logo_url", settings.get("brand.logo_url", "")),
                "brand.theme_color": branding.get("theme_color", settings.get("brand.theme_color", "")),
                "brand.public_slug": branding.get("public_slug", settings.get("brand.public_slug", slug or "")),
            }
            for key, value in merged_settings.items():
                if value not in (None, ""):
                    _set_tenant_setting(db, tenant_id, key, str(value))
            db.commit()
        finally:
            db.close()
        return jsonify({"status": "updated", "tenant_id": tenant_id})

    tenant_id = current_tenant_id()
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, name, slug, status, plan, created_at, updated_at FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": f'Tenant "{tenant_id}" not found'}), 404
        tenant = _tenant_row_to_dict(row)
        discord = db.execute(
            "SELECT * FROM tenant_discord_configs WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        twitch = db.execute(
            "SELECT * FROM tenant_twitch_configs WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        settings = _load_tenant_settings(db, tenant_id)
    finally:
        db.close()
    return jsonify({
        "tenant": tenant,
        "settings": settings,
        "discord": dict(discord) if discord else None,
        "twitch": dict(twitch) if twitch else None,
    })


@dashboard.route("/api/tenants/current/discord", methods=["PATCH"])
@api_auth_required
def api_tenant_current_discord():
    """Update Discord configuration for the selected tenant."""
    _require_dashboard_admin()
    data = request.get_json(silent=True) or {}
    tenant_id = current_tenant_id()
    now = int(time.time())
    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone():
            return jsonify({"error": f'Tenant "{tenant_id}" not found'}), 404
        current = db.execute("SELECT * FROM tenant_twitch_configs WHERE tenant_id = ?", (tenant_id,)).fetchone()
        _upsert_tenant_configs(db, tenant_id, data, dict(current) if current else None, now)
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "updated", "tenant_id": tenant_id})


@dashboard.route("/api/tenants/current/twitch", methods=["PATCH"])
@api_auth_required
def api_tenant_current_twitch():
    """Update optional Twitch configuration for the selected tenant."""
    _require_dashboard_admin()
    data = request.get_json(silent=True) or {}
    tenant_id = current_tenant_id()
    now = int(time.time())
    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone():
            return jsonify({"error": f'Tenant "{tenant_id}" not found'}), 404
        discord = db.execute("SELECT * FROM tenant_discord_configs WHERE tenant_id = ?", (tenant_id,)).fetchone()
        _upsert_tenant_configs(db, tenant_id, dict(discord) if discord else {}, data, now)
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "updated", "tenant_id": tenant_id})


@dashboard.route("/api/tenants", methods=["POST"])
@api_auth_required
def api_tenant_create():
    """Create a tenant shell for a future customer/community."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    tenant_id = _normalize_tenant_id(data.get("id") or data.get("slug") or name)
    slug = _normalize_tenant_id(data.get("slug") or tenant_id)
    plan = (data.get("plan") or "trial").strip().lower()[:64] or "trial"
    status = (data.get("status") or "setup").strip().lower()[:64] or "setup"

    if not tenant_id or not name:
        return jsonify({"error": "name and a valid id/slug are required"}), 400

    now = int(time.time())
    discord_cfg = data.get("discord") or {}
    twitch_cfg = data.get("twitch") or {}

    db = get_db()
    try:
        existing = db.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if existing:
            return jsonify({"error": f'Tenant "{tenant_id}" already exists'}), 409

        db.execute(
            """
            INSERT INTO tenants (id, name, slug, status, plan, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tenant_id, name, slug, status, plan, now, now),
        )
        db.execute(
            """
            INSERT INTO tenant_discord_configs (
                tenant_id, guild_id, member_category_id, free_category_id, log_channel_id,
                flight_listen_channel_id, free_flight_listen_channel_id, flight_log_channel_id,
                mod_role_id, island_access_role_id, bot_enabled, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                str(discord_cfg.get("guild_id") or ""),
                str(discord_cfg.get("member_category_id") or ""),
                str(discord_cfg.get("free_category_id") or ""),
                str(discord_cfg.get("log_channel_id") or ""),
                str(discord_cfg.get("flight_listen_channel_id") or ""),
                str(discord_cfg.get("free_flight_listen_channel_id") or ""),
                str(discord_cfg.get("flight_log_channel_id") or ""),
                str(discord_cfg.get("mod_role_id") or ""),
                str(discord_cfg.get("island_access_role_id") or ""),
                1 if discord_cfg.get("bot_enabled", True) else 0,
                now,
            ),
        )
        db.execute(
            """
            INSERT INTO tenant_twitch_configs (tenant_id, channel_name, bot_enabled, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                tenant_id,
                str(twitch_cfg.get("channel_name") or ""),
                1 if twitch_cfg.get("bot_enabled", True) else 0,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()

    return jsonify({"status": "created", "tenant_id": tenant_id}), 201


@dashboard.route("/api/tenants/switch", methods=["POST"])
@api_auth_required
def api_tenant_switch():
    """Switch the active dashboard tenant for the current session."""
    data = request.get_json(silent=True) or {}
    tenant_id = _normalize_tenant_id(data.get("tenant_id") or data.get("id") or "")
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400
    if not _tenant_exists(tenant_id):
        return jsonify({"error": f'Tenant "{tenant_id}" not found'}), 404
    session["tenant_id"] = tenant_id
    session.permanent = True
    return jsonify({"status": "switched", "tenant_id": tenant_id})


def _discord_bot_auth_header() -> str:
    token = str(Config.DISCORD_TOKEN or "").strip()
    if not token:
        return ""
    return token if token.lower().startswith("bot ") else f"Bot {token}"


@dashboard.route("/api/onboarding/discord-scan", methods=["POST"])
@api_auth_required
def api_onboarding_discord_scan():
    """Scan Discord category channels and return island import candidates."""
    _require_dashboard_admin()
    data = request.get_json(silent=True) or {}
    guild_id = _string_id(data.get("guild_id") or Config.GUILD_ID)
    free_category_id = _string_id(data.get("free_category_id"))
    member_category_id = _string_id(data.get("member_category_id") or data.get("category_id"))

    # Test/dev fallback: allow callers to pass channel objects directly.
    provided_channels = data.get("channels")
    if isinstance(provided_channels, list):
        channels = provided_channels
    else:
        auth = _discord_bot_auth_header()
        if not guild_id or not auth:
            return jsonify({"error": "guild_id and DISCORD_TOKEN are required for Discord scan"}), 400
        try:
            req = urllib.request.Request(
                f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                headers={"Authorization": auth, "User-Agent": _DISCORD_USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                channels = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode(errors="replace")
            except Exception:
                pass
            return jsonify({"error": f"Discord channel scan failed: HTTP {exc.code}", "details": body}), 502
        except Exception as exc:
            return jsonify({"error": f"Discord channel scan failed: {exc}"}), 502

    candidates = []
    for channel in channels:
        if str(channel.get("type", "")) not in {"0", "5", "15"}:
            continue
        parent_id = _string_id(channel.get("parent_id"))
        cat = None
        if free_category_id and parent_id == free_category_id:
            cat = "public"
        elif member_category_id and parent_id == member_category_id:
            cat = "member"
        if not cat:
            continue
        name = _string_id(channel.get("name")).replace("-", " ").replace("_", " ").strip()
        if not name:
            continue
        candidates.append({
            "id": _normalize_tenant_id(name),
            "name": name.upper(),
            "cat": cat,
            "type": "Free" if cat == "public" else "VIP",
            "theme": "teal",
            "items": [],
            "description": "",
            "seasonal": "",
            "required_roles": [],
            "channel_id": _string_id(channel.get("id")),
        })

    return jsonify({"guild_id": guild_id, "islands": candidates, "count": len(candidates)})


@dashboard.route("/api/onboarding/complete", methods=["POST"])
@api_auth_required
def api_onboarding_complete():
    """Create/update a tenant from the onboarding wizard and mark setup complete."""
    _require_dashboard_admin()
    data = request.get_json(silent=True) or {}
    tenant_data = data.get("tenant") or data.get("basics") or {}
    branding = data.get("branding") or {}
    discord_cfg = data.get("discord") or {}
    twitch_cfg = data.get("twitch") or {}
    islands = data.get("islands") or []

    name = _string_id(tenant_data.get("name") or data.get("name"))
    tenant_id = _normalize_tenant_id(tenant_data.get("id") or tenant_data.get("slug") or data.get("tenant_id") or name)
    slug = _normalize_tenant_id(tenant_data.get("slug") or branding.get("public_slug") or tenant_id)
    plan = _string_id(tenant_data.get("plan") or data.get("plan") or "trial").lower()[:64] or "trial"

    if not name or not tenant_id:
        return jsonify({"error": "Tenant name and slug are required"}), 400
    if not _string_id(discord_cfg.get("guild_id")):
        return jsonify({"error": "Discord guild_id is required"}), 400

    now = int(time.time())
    completed_at = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        existing = db.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if existing:
            db.execute(
                "UPDATE tenants SET name = ?, slug = ?, status = 'active', plan = ?, updated_at = ? WHERE id = ?",
                (name, slug, plan, now, tenant_id),
            )
        else:
            db.execute(
                """
                INSERT INTO tenants (id, name, slug, status, plan, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (tenant_id, name, slug, plan, now, now),
            )

        _upsert_tenant_configs(
            db,
            tenant_id,
            discord_cfg,
            twitch_cfg if twitch_cfg else None,
            now,
        )
        _set_tenant_setting(db, tenant_id, "brand.logo_url", _string_id(branding.get("logo_url")))
        _set_tenant_setting(db, tenant_id, "brand.theme_color", _string_id(branding.get("theme_color") or "teal"))
        _set_tenant_setting(db, tenant_id, "brand.public_slug", slug)
        _set_tenant_setting(db, tenant_id, "onboarding.completed_at", completed_at)
        _set_tenant_setting(db, tenant_id, "onboarding.source", "discord_oauth")
        island_count = _save_onboarding_islands(db, tenant_id, islands)
        db.commit()
    finally:
        db.close()

    session["tenant_id"] = tenant_id
    session.permanent = True
    return jsonify({
        "status": "completed",
        "tenant_id": tenant_id,
        "island_count": island_count,
        "redirect": url_for("dashboard.index"),
    })


@dashboard.route("/api/mariadb-migration/status", methods=["GET"])
@api_auth_required
def api_mariadb_migration_status():
    """Return SQLite source counts and MariaDB migration configuration status."""
    try:
        source_tables = _sqlite_table_counts()
    except Exception as exc:
        return jsonify({"error": f"Could not inspect SQLite database: {exc}"}), 500

    return jsonify({
        "runtime_database": get_backend(),
        "sqlite_path": _DB_PATH,
        "sqlite_exists": os.path.exists(_DB_PATH),
        "source_tables": source_tables,
        "source_total_rows": sum(source_tables.values()),
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
        source_tables = _sqlite_table_counts()
        if dry_run:
            _mariadb_migration_last_result = {
                "ok": True,
                "dry_run": True,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "source_tables": source_tables,
                "source_total_rows": sum(source_tables.values()),
                "mariadb": _mariadb_settings_payload(),
            }
            return jsonify(_mariadb_migration_last_result)

        summary = migrate_sqlite_to_mariadb(
            sqlite_path=_DB_PATH,
            host=Config.MARIADB_HOST,
            port=Config.MARIADB_PORT,
            user=Config.MARIADB_USER,
            password=Config.MARIADB_PASSWORD,
            database=Config.MARIADB_DATABASE,
            truncate_before_import=truncate_before_import,
        )
        _mariadb_migration_last_result = {
            "ok": True,
            "dry_run": False,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "truncate_before_import": truncate_before_import,
            "tables": summary,
            "total_rows_copied": sum(summary.values()),
            "runtime_database": get_backend(),
            "sqlite_preserved": True,
            "note": "Migration copied data to MariaDB. Set DB_BACKEND=mysql to use it as the app database.",
        }
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


@dashboard.route("/api/status-summary", methods=["GET"])
@api_auth_required
def api_status_summary():
    """Return live island status counts and per-island effective statuses."""
    tenant_id = current_tenant_id()
    db = get_db()
    try:
        rows       = db.execute(
            "SELECT * FROM islands WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ).fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
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

    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("_storage_id", ""), bot_status.get(isl.get("id", "")))
        s = _effective_status(isl)
        islands_out.append({"id": isl.get("id", ""), "name": isl.get("name", ""), "status": s})
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
        "islands":          islands_out,
    })


@dashboard.route("/api/islands", methods=["GET"])
@api_auth_required
def api_islands_list():
    """List all islands."""
    tenant_id = current_tenant_id()
    db = get_db()
    try:
        rows       = db.execute(
            "SELECT * FROM islands WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ).fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
        bot_status = _load_bot_status_map(db)
    except Exception:
        db_islands = []
        bot_status = {}
    finally:
        db.close()

    result = []
    for isl in db_islands:
        isl["discord_bot_online"] = bot_status.get(isl.get("_storage_id", ""), bot_status.get(isl.get("id", "")))
        result.append(_island_api_dict(isl))
    return jsonify(result)


@dashboard.route("/api/islands", methods=["POST"])
@api_auth_required
def api_island_create():
    """Create or upsert a full island record."""
    tenant_id = current_tenant_id()
    data      = request.get_json(silent=True) or {}
    island_id = (data.get("id") or data.get("name", "")).strip().lower()
    storage_id = _storage_island_id(tenant_id, island_id)
    name      = (data.get("name") or island_id).strip().upper()
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
                   (id, tenant_id, name, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (storage_id, tenant_id, name, isl_type, json.dumps(items),
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
    tenant_id = current_tenant_id()
    storage_id = _storage_island_id(tenant_id, island_id)
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM islands WHERE tenant_id = ? AND id = ?",
            (tenant_id, storage_id),
        ).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": f'Island "{name}" not found'}), 404
    return jsonify(_island_api_dict(_row_to_island_dict(dict(row))))


@dashboard.route("/api/islands/<name>", methods=["PUT"])
@api_auth_required
def api_island_update(name):
    """Update a single island record (partial or full)."""
    island_id = name.lower()
    tenant_id = current_tenant_id()
    storage_id = _storage_island_id(tenant_id, island_id)
    data      = request.get_json(silent=True) or {}

    db = get_db()
    try:
        row      = db.execute(
            "SELECT * FROM islands WHERE tenant_id = ? AND id = ?",
            (tenant_id, storage_id),
        ).fetchone()
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

    dodo_code = data.get("dodoCode") or data.get("dodo_code") or existing.get("dodo_code")
    if (dodo_code or "").strip().upper() == REFRESHING_DODO_CODE:
        status = STATUS_REFRESHING

    db2 = get_db()
    try:
        db2.execute(
            """INSERT INTO islands
                   (id, tenant_id, name, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (
                storage_id,
                tenant_id,
                data.get("name", existing.get("name", island_id.upper())).upper(),
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
    tenant_id = current_tenant_id()
    storage_id = _storage_island_id(tenant_id, island_id)
    db = get_db()
    try:
        db.execute("DELETE FROM islands WHERE tenant_id = ? AND id = ?", (tenant_id, storage_id))
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "deleted", "id": island_id})


@dashboard.route("/api/islands/<name>/map", methods=["POST"])
@api_auth_required
def api_island_upload_map(name):
    """Upload an island map image to Cloudflare R2 and store the URL."""
    island_id = name.lower()
    tenant_id = current_tenant_id()
    storage_id = _storage_island_id(tenant_id, island_id)

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
            "UPDATE islands SET map_url = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
            (map_url, datetime.now(timezone.utc).isoformat(), tenant_id, storage_id),
        )
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO islands (id, tenant_id, name, map_url, updated_at) VALUES (?,?,?,?,?)",
                (storage_id, tenant_id, island_id.upper(), map_url, datetime.now(timezone.utc).isoformat()),
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
    tenant_id = current_tenant_id()

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
            storage_id = _storage_island_id(tenant_id, island_id)
            map_url = f"{base}/{key}"
            try:
                db.execute(
                    "UPDATE islands SET map_url = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
                    (map_url, now, tenant_id, storage_id),
                )
                if db.execute("SELECT changes()").fetchone()[0] == 0:
                    # Island row doesn't exist yet — create a minimal one
                    db.execute(
                        "INSERT OR IGNORE INTO islands (id, tenant_id, name, map_url, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (storage_id, tenant_id, island_id.upper(), map_url, now),
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
            r[0] for r in db.execute(
                "SELECT name FROM islands WHERE tenant_id = ? ORDER BY name",
                (current_tenant_id(),),
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
        rows2       = db2.execute(
            "SELECT * FROM islands WHERE tenant_id = ? ORDER BY name",
            (current_tenant_id(),),
        ).fetchall()
        db_islands2 = [_row_to_island_dict(dict(r)) for r in rows2]
        bot_status2 = _load_bot_status_map(db2)
    except Exception:
        db_islands2 = []
        bot_status2 = {}
    finally:
        db2.close()

    for isl in db_islands2:
        isl["discord_bot_online"] = bot_status2.get(isl.get("_storage_id", ""), bot_status2.get(isl.get("id", "")))

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

