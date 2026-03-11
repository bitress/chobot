"""
ChoBot Web Dashboard
Mod-only web interface for island management, XLog reports, and analytics.
Access is protected by a secret key (DASHBOARD_SECRET env var).
"""

import json
import os
import re
import secrets
import sqlite3
import logging
import mimetypes
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import wraps

import boto3
from botocore.client import Config as BotocoreConfig
from botocore.exceptions import ClientError, NoCredentialsError

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify, abort, g,
)

from utils.config import Config

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

# Absolute path to the shared SQLite database
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "chobot.db",
)

ALLOWED_CATEGORIES = ("public", "member")
ALLOWED_THEMES     = ("pink", "teal", "purple", "gold")
ALLOWED_STATUSES   = ("ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE")

# Moderator role IDs used during Discord OAuth login
ADMIN_ROLE_ID    = Config.ADMIN_ROLE_ID
BABY_MOD_ROLE_ID = Config.BABY_MOD_ROLE_ID

# Day-of-week label order (SQLite strftime('%w'): 0=Sunday … 6=Saturday)
_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Max map upload size: 5 MB
MAX_MAP_SIZE      = 5 * 1024 * 1024
ALLOWED_MAP_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


# ---------------------------------------------------------------------------
# Discord user resolution
# ---------------------------------------------------------------------------
# Cache: maps user_id → (display_name, cache_time)
_discord_user_cache: dict[str, tuple[str, float]] = {}
_discord_user_cache_lock = threading.Lock()
_DISCORD_CACHE_TTL = 3600  # seconds — refresh names after 1 hour


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
            headers={"Authorization": f"Bot {token}"},
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
    """Return a synchronous SQLite connection to chobot.db."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_dashboard_db():
    """Create dashboard-specific tables if they do not already exist."""
    try:
        conn = get_db()

        # Full IslandData-compatible table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS islands (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL DEFAULT '',
                items       TEXT NOT NULL DEFAULT '[]',
                theme       TEXT NOT NULL DEFAULT 'teal',
                cat         TEXT NOT NULL DEFAULT 'public',
                description TEXT NOT NULL DEFAULT '',
                seasonal    TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'OFFLINE',
                visitors    INTEGER NOT NULL DEFAULT 0,
                dodo_code   TEXT,
                map_url     TEXT,
                updated_at  TEXT
            )
        """)

        # Live island bot presence, written by the Discord bot's monitor loop
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_bot_status (
                island_id   TEXT PRIMARY KEY,
                island_name TEXT NOT NULL,
                is_online   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT
            )
        """)

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

        conn.commit()
        conn.close()
        logger.info("Dashboard DB initialised")
    except sqlite3.Error as exc:
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
    """Return the current session role ('admin' or 'baby_mod')."""
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
    """Decorator for admin-only web routes — returns 403 for baby_mod role."""
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
    return {
        "current_role":      session.get("mod_role", "admin"),
        "discord_username":  session.get("discord_username", ""),
        "discord_user_id":   session.get("discord_user_id", ""),
        "oauth_configured":  bool(Config.DISCORD_CLIENT_ID),
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


def row_to_island_dict(row: dict) -> dict:
    """Decode the items JSON column and return a plain dict."""
    try:
        row["items"] = json.loads(row.get("items") or "[]")
    except (ValueError, TypeError):
        row["items"] = []
    return row


def _load_bot_status_map(conn) -> dict:
    """Return a dict of island_id → bool (is_online) from island_bot_status."""
    try:
        rows = conn.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        return {r["island_id"]: bool(r["is_online"]) for r in rows}
    except sqlite3.Error:
        return {}


# Backward-compatible alias for internal callers
_row_to_island_dict = row_to_island_dict


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

@dashboard.errorhandler(403)
def _forbidden(_e):
    return render_template("dashboard/403.html"), 403

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
    session.pop("mod_logged_in",    None)
    session.pop("mod_role",         None)
    session.pop("discord_user_id",  None)
    session.pop("discord_username", None)
    session.pop("oauth_state",      None)
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
            headers={"Content-Type": "application/x-www-form-urlencoded"},
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

    # Fetch the user's guild-member record (includes roles)
    role = None
    try:
        mem_req = urllib.request.Request(
            f"https://discord.com/api/users/@me/guilds/{Config.GUILD_ID}/member",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(mem_req, timeout=10) as resp:
            member_data = json.loads(resp.read().decode())
        member_roles = [str(r) for r in member_data.get("roles", [])]
        if str(ADMIN_ROLE_ID) in member_roles:
            role = "admin"
        elif str(BABY_MOD_ROLE_ID) in member_roles:
            role = "baby_mod"
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
        flash("You do not have a moderator role on this server.", "error")
        return redirect(url_for("dashboard.login"))

    # Fetch basic user info for display
    discord_username = ""
    discord_user_id  = ""
    try:
        user_req = urllib.request.Request(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            user_data = json.loads(resp.read().decode())
        discord_user_id  = str(user_data.get("id", ""))
        discord_username = user_data.get("global_name") or user_data.get("username", "")
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        pass  # Non-critical — display name is optional

    session["mod_logged_in"]   = True
    session["mod_role"]        = role
    session["discord_user_id"] = discord_user_id
    session["discord_username"]= discord_username
    session.permanent          = True
    logger.info("OAuth login: user=%s role=%s", discord_username, role)
    return redirect(url_for("dashboard.index"))


@dashboard.route("/")
@login_required
def index():
    db = get_db()
    try:
        total_visits   = db.execute("SELECT COUNT(*) FROM island_visits").fetchone()[0]
        total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        visits_today   = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','start of day')"
        ).fetchone()[0]
        visits_week    = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        recent_raw     = db.execute(
            "SELECT ign, destination, authorized, timestamp "
            "FROM island_visits ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
    except sqlite3.Error:
        total_visits = total_warnings = visits_today = visits_week = 0
        recent_raw = []
    finally:
        db.close()

    recent = [
        {
            "ign":         r["ign"],
            "destination": r["destination"],
            "authorized":  bool(r["authorized"]),
            "timestamp":   _ts_to_str(r["timestamp"]),
        }
        for r in recent_raw
    ]

    db2 = get_db()
    try:
        island_count = db2.execute("SELECT COUNT(*) FROM islands").fetchone()[0]
        status_rows  = db2.execute(
            "SELECT status, COUNT(*) AS cnt FROM islands GROUP BY status"
        ).fetchall()
    except sqlite3.Error:
        island_count = 0
        status_rows  = []
    finally:
        db2.close()

    status_map = {r["status"]: r["cnt"] for r in status_rows}
    online_count = status_map.get("ONLINE", 0)

    return render_template(
        "dashboard/index.html",
        total_visits=total_visits,
        total_warnings=total_warnings,
        visits_today=visits_today,
        visits_week=visits_week,
        recent=recent,
        island_count=island_count,
        status_map=status_map,
        online_count=online_count,
    )


@dashboard.route("/islands")
@admin_required
def islands():
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except sqlite3.Error:
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
    upper     = name.upper()

    db = get_db()
    try:
        row  = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
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
                db2.execute(
                    """INSERT INTO islands
                           (id, name, type, items, theme, cat, description, seasonal,
                            status, visitors, dodo_code, map_url, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                           name=excluded.name, type=excluded.type, items=excluded.items,
                           theme=excluded.theme, cat=excluded.cat,
                           description=excluded.description, seasonal=excluded.seasonal,
                           status=excluded.status, visitors=excluded.visitors,
                           dodo_code=excluded.dodo_code, updated_at=excluded.updated_at""",
                    (
                        island_id, upper, isl_type, json.dumps(items_list),
                        isl_theme, isl_cat, isl_desc, isl_seasonal,
                        isl_status, isl_visitors, isl_dodo,
                        meta["map_url"] if meta else None,
                        datetime.now(timezone.utc).isoformat(),
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
        "map_url": None, "updated_at": None,
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
                "SELECT DATE(timestamp, 'unixepoch') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE LOWER(destination) = LOWER(?) "
                "AND timestamp > strftime('%s','now','-7 days') "
                "GROUP BY day ORDER BY day",
                (upper,),
            ).fetchall()
        ]
    except sqlite3.Error:
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
@login_required
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
                [r["user_id"] for r in rows] + [r["mod_id"] for r in rows if r["mod_id"]]
            )
            entries = [
                {
                    "user_id":     r["user_id"],
                    "user_name":   name_map.get(str(r["user_id"]), str(r["user_id"])),
                    "reason":      r["reason"],
                    "mod_id":      r["mod_id"],
                    "mod_name":    name_map.get(str(r["mod_id"]), str(r["mod_id"])) if r["mod_id"] else "—",
                    "timestamp":   _ts_to_str(r["timestamp"]),
                    "ign":         r["ign"],
                    "destination": r["destination"],
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
                }
                for r in rows
            ]
    except sqlite3.Error:
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
    )


@dashboard.route("/status")
@login_required
def island_status():
    """Dedicated Island Status Breakdown page."""
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
        status_rows = db.execute(
            "SELECT status, COUNT(*) AS cnt FROM islands GROUP BY status"
        ).fetchall()
    except sqlite3.Error:
        db_islands = []
        status_rows = []
    finally:
        db.close()

    status_map = {r["status"]: r["cnt"] for r in status_rows}
    island_count = sum(status_map.values())
    online_count = status_map.get("ONLINE", 0)

    def _pct(count):
        return round(count * 100 / island_count) if island_count else 0

    online_pct = _pct(online_count)
    sub_pct = _pct(status_map.get("SUB ONLY", 0))
    ref_pct = _pct(status_map.get("REFRESHING", 0))
    off_pct = _pct(status_map.get("OFFLINE", 0))

    # Group islands by status for the per-section tables
    grouped = {"ONLINE": [], "SUB ONLY": [], "REFRESHING": [], "OFFLINE": []}
    for isl in db_islands:
        bucket = isl.get("status", "OFFLINE")
        grouped.setdefault(bucket, []).append(isl)

    return render_template(
        "dashboard/status.html",
        island_count=island_count,
        status_map=status_map,
        online_pct=online_pct,
        sub_pct=sub_pct,
        ref_pct=ref_pct,
        off_pct=off_pct,
        grouped=grouped,
    )


@dashboard.route("/analytics")
@login_required
def analytics():
    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        visits_by_day = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE timestamp > strftime('%s','now','-7 days') "
                "GROUP BY day ORDER BY day"
            ).fetchall()
        ]
        visits_by_day_30 = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE timestamp > strftime('%s','now','-30 days') "
                "GROUP BY day ORDER BY day"
            ).fetchall()
        ]
        visits_by_hour = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%H', timestamp, 'unixepoch') AS INTEGER) AS hour, "
                "COUNT(*) AS count "
                "FROM island_visits "
                "GROUP BY hour ORDER BY hour"
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count FROM island_visits GROUP BY authorized"
        ).fetchall()
        # Visits by island category (public vs member/VIP)
        cat_raw = db.execute(
            "SELECT isl.cat, COUNT(*) AS visit_count "
            "FROM island_visits iv "
            "JOIN islands isl ON LOWER(iv.destination) = isl.id "
            "GROUP BY isl.cat"
        ).fetchall()
        # Top warned users
        top_warned = [
            dict(r) for r in db.execute(
                "SELECT user_id, COUNT(*) AS warn_count "
                "FROM warnings GROUP BY user_id "
                "ORDER BY warn_count DESC LIMIT 10"
            ).fetchall()
        ]
        warned_name_map = _resolve_discord_usernames(r["user_id"] for r in top_warned)
        for row in top_warned:
            row["user_name"] = warned_name_map.get(str(row["user_id"]), str(row["user_id"]))
        # Quick summary stats
        visits_today = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','start of day')"
        ).fetchone()[0]
        visits_week = db.execute(
            "SELECT COUNT(*) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        warnings_week = db.execute(
            "SELECT COUNT(*) FROM warnings "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        # Day-of-week breakdown (0=Sunday … 6=Saturday)
        dow_raw = [
            dict(r) for r in db.execute(
                "SELECT CAST(strftime('%w', timestamp, 'unixepoch') AS INTEGER) AS dow, "
                "COUNT(*) AS count "
                "FROM island_visits GROUP BY dow ORDER BY dow"
            ).fetchall()
        ]
        # New vs returning travelers (7d and 30d)
        new_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit FROM island_visits GROUP BY ign"
            ") WHERE first_visit > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        total_unique_7d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-7 days')"
        ).fetchone()[0]
        new_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM ("
            "  SELECT ign, MIN(timestamp) AS first_visit FROM island_visits GROUP BY ign"
            ") WHERE first_visit > strftime('%s','now','-30 days')"
        ).fetchone()[0]
        total_unique_30d = db.execute(
            "SELECT COUNT(DISTINCT ign) FROM island_visits "
            "WHERE timestamp > strftime('%s','now','-30 days')"
        ).fetchone()[0]
    except sqlite3.Error:
        top_islands = top_travelers = visits_by_day = visits_by_day_30 = []
        visits_by_hour = []
        auth_raw = []
        cat_raw = []
        top_warned = []
        visits_today = visits_week = warnings_week = 0
        dow_raw = []
        new_7d = total_unique_7d = new_30d = total_unique_30d = 0
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
        visits_today=visits_today,
        visits_week=visits_week,
        warnings_week=warnings_week,
        new_returning=new_returning,
    )


# ===========================================================================
# JSON CRUD API  (Bearer token OR active browser session)
# ===========================================================================

@dashboard.route("/api/islands", methods=["GET"])
@api_auth_required
def api_islands_list():
    """List all islands (DB records merged with live filesystem data)."""
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except sqlite3.Error:
        db_islands = []
    finally:
        db.close()

    fs_map  = _collect_fs_islands()
    result  = []
    seen    = set()
    for isl in db_islands:
        uname = isl["name"].upper()
        seen.add(uname)
        result.append(_merge_island(isl, fs_map.get(uname)))
    for uname, fs in fs_map.items():
        if uname not in seen:
            stub = {
                "id": uname.lower(), "name": uname, "type": "", "items": [],
                "theme": "teal", "cat": "public", "description": "", "seasonal": "",
                "status": "OFFLINE", "visitors": 0, "dodo_code": None,
                "map_url": None, "updated_at": None,
            }
            result.append(_merge_island(stub, fs))
    result.sort(key=lambda x: x["name"])
    return jsonify(result)


@dashboard.route("/api/islands", methods=["POST"])
@api_auth_required
def api_island_create():
    """Create or upsert a full island record."""
    data      = request.get_json(silent=True) or {}
    island_id = (data.get("id") or data.get("name", "")).strip().lower()
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

    db = get_db()
    try:
        db.execute(
            """INSERT INTO islands
                   (id, name, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (island_id, name, isl_type, json.dumps(items),
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
    if not row:
        return jsonify({"error": f'Island "{name}" not found'}), 404
    isl    = _row_to_island_dict(dict(row))
    fs_map = _collect_fs_islands()
    return jsonify(_merge_island(isl, fs_map.get(isl["name"].upper())))


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

    db2 = get_db()
    try:
        db2.execute(
            """INSERT INTO islands
                   (id, name, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (
                island_id,
                data.get("name", existing.get("name", island_id.upper())).upper(),
                data.get("type",        existing.get("type",        "")),
                json.dumps(items_in),
                theme, cat,
                data.get("description", existing.get("description", "")),
                data.get("seasonal",    existing.get("seasonal",    "")),
                status,
                int(data.get("visitors", existing.get("visitors", 0))),
                data.get("dodoCode") or data.get("dodo_code") or existing.get("dodo_code"),
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
            except sqlite3.Error as exc:
                errors.append(f"{island_id}: {exc}")
        db.commit()
    finally:
        db.close()

    return jsonify({"synced": synced, "skipped": skipped, "errors": errors})


@dashboard.route("/api/analytics", methods=["GET"])
@api_auth_required
def api_analytics():
    """Return analytics summary as JSON."""
    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count FROM island_visits GROUP BY authorized"
        ).fetchall()
    except sqlite3.Error:
        top_islands = top_travelers = []
        auth_raw = []
    finally:
        db.close()

    auth_map = {r["authorized"]: r["count"] for r in auth_raw}
    return jsonify({
        "top_islands":         top_islands,
        "top_travelers":       top_travelers,
        "authorized_visits":   auth_map.get(1, 0),
        "unauthorized_visits": auth_map.get(0, 0),
    })


@dashboard.route("/api/logs", methods=["GET"])
@api_auth_required
def api_logs():
    """Return paginated flight-log entries as JSON."""
    page          = request.args.get("page", 1, type=int)
    per_page      = min(request.args.get("per_page", 25, type=int), 100)
    island_filter = request.args.get("island", "").strip()
    ign_filter    = request.args.get("ign", "").strip()

    db = get_db()
    try:
        conditions, params = [], []
        if island_filter:
            conditions.append("destination LIKE ?")
            params.append(f"%{island_filter}%")
        if ign_filter:
            conditions.append("LOWER(ign) LIKE LOWER(?)")
            params.append(f"%{ign_filter}%")
        where = _where_clause(conditions)
        total = db.execute(
            f"SELECT COUNT(*) FROM island_visits {where}", params
        ).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM island_visits {where} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    except sqlite3.Error:
        total, rows = 0, []
    finally:
        db.close()

    return jsonify({
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "entries":  [dict(r) for r in rows],
    })

