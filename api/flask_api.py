"""
Flask API Module
Combines all API endpoints:
- Item/Villager Search
- Dodo Code/Island Status
- Patreon Posts
"""

import os
import re
import time
import json
import secrets as _secrets
import logging
import sqlite3
import threading
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request, session, redirect, url_for
from flask_cors import CORS
from thefuzz import process, fuzz

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import ThreadedWSGIServer

from utils.config import Config
from utils.helpers import format_locations_text, parse_locations_json, normalize_text
from api.dashboard import dashboard, init_dashboard_db, get_db, row_to_island_dict, _parse_visitor_value, _parse_visitor_list


logger = logging.getLogger("FlaskAPI")

# Initialize Flask app
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY
# Trust one level of X-Forwarded-For / X-Forwarded-Proto headers from the
# reverse proxy (nginx, Cloudflare Tunnel, etc.) so that url_for(_external=True)
# produces the correct https:// URL instead of http://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
CORS(app, resources={r"/*": {"origins": [
    "https://www.chopaeng.com",
    "https://chopaeng.com",
    "http://localhost:5173",
    "http://localhost:3000",
]}}, supports_credentials=True)

# Register the mod-only web dashboard
app.register_blueprint(dashboard, url_prefix="/dashboard")
init_dashboard_db()

# Suppress Flask/Werkzeug standard logs
logging.getLogger('werkzeug').setLevel(logging.ERROR)


# Patreon cache
patreon_cache = {
    "list": {"data": None, "timestamp": None},
    "posts": {}
}

# Data manager will be set from main.py
data_manager = None

# Guard: prevents multiple concurrent cache-refresh operations
_refresh_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Auth — short-lived opaque tokens for Discord OAuth (website subscribers)
# Works cross-domain: frontend stores the token in localStorage and sends it
# as "Authorization: Bearer <token>" on every authenticated request.
# ---------------------------------------------------------------------------
_AUTH_TOKEN_TTL = 86400  # 24 hours
_auth_tokens: dict[str, dict] = {}   # token → {user_data, expires_at}
_auth_tokens_lock = threading.Lock()

_DISCORD_UA = "DiscordBot (https://chopaeng.com, 1.0)"
_ADMINISTRATOR_PERM = 0x8   # Discord Administrator permission bit

def _make_auth_token(user_data: dict) -> str:
    token = _secrets.token_urlsafe(32)
    expires_at = time.monotonic() + _AUTH_TOKEN_TTL
    with _auth_tokens_lock:
        _auth_tokens[token] = {"user": user_data, "expires_at": expires_at}
    return token

def _get_auth_user(token: str) -> dict | None:
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

def _current_auth_user() -> dict | None:
    """Extract Bearer token from request and return user dict, or None."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _get_auth_user(auth[len("Bearer "):])
    return None

def _is_mod(roles: list[str]) -> bool:
    """True if the user holds one of the configured moderator roles."""
    mod_ids = {
        str(Config.ADMIN_ROLE_ID),
        str(Config.SENIOR_MOD_ROLE_ID),
        str(Config.BABY_MOD_ROLE_ID),
    } - {"None", "0", ""}
    return bool(mod_ids & set(roles))

def _has_island_access(roles: list[str], required_roles: list[str], is_mod: bool = False) -> bool:
    """True if the user may see this island's dodo code.

    Access is granted when:
    - The island has no required_roles (free/public)
    - The user is a mod (token is_mod=true, ADMIN_ROLE_ID, SENIOR_MOD_ROLE_ID, or BABY_MOD_ROLE_ID)
    - The user holds at least one of the island's required_roles
    """
    if not required_roles:
        return True
    if is_mod:
        return True
    if _is_mod(roles):
        return True
    return bool(set(required_roles) & set(roles))

def _configured_subscription_role_ids() -> list[str]:
    """Return every configured subscriber role ID as strings.

    Includes tier roles and the legacy ISLAND_ACCESS_ROLE fallback when set.
    """
    role_ids = {rid for rid, _name in Config.subscription_roles()}
    if Config.ISLAND_ACCESS_ROLE:
        role_ids.add(str(Config.ISLAND_ACCESS_ROLE))
    return [rid for rid in role_ids if rid not in {"", "0", "None"}]

def _fire_dodo_webhook(
    username: str,
    nickname: str,
    user_id: str,
    avatar_url: str,
    island_name: str,
    dodo_code: str,
) -> None:
    """POST a Discord webhook message in the background."""
    url = Config.DODO_LOG_WEBHOOK_URL
    if not url:
        return

    display_name = (nickname or "").strip() or (username or "").strip() or "Unknown User"
    account_name = (username or "").strip() or "Unknown User"
    nick_value = (nickname or "").strip() or "(none)"
    user_id_value = (user_id or "").strip() or "(unknown)"

    embed = {
        "description": f"**{display_name}** revealed a dodo code at **{island_name}**",
        "color": 0x57F287,
        "fields": [
            {"name": "Guild Nickname", "value": nick_value, "inline": True},
            {"name": "Discord Username", "value": account_name, "inline": True},
            {"name": "Island", "value": island_name, "inline": True},
        ],
        "footer": {"text": "Chopaeng Dodo Reveal Log"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    # Discord rejects empty embed objects like "thumbnail": {}.
    if avatar_url:
        embed["thumbnail"] = {"url": avatar_url}

    payload = json.dumps({"embeds": [embed]}).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _DISCORD_UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            # Discord commonly returns 204 No Content for webhook success.
            if resp.status not in (200, 204):
                logger.warning("Dodo webhook unexpected HTTP status: %s", resp.status)
            else:
                logger.debug("Dodo webhook delivered for island=%s user=%s", island_name, username)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            pass
        logger.warning("Dodo webhook failed HTTP %s: %s", exc.code, body)
    except Exception as exc:
        logger.warning("Dodo webhook failed: %s", exc)


def set_data_manager(dm):
    """Set the data manager instance"""
    global data_manager
    data_manager = dm

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_image_from_html(html_content):
    """Extract image URL from HTML content"""
    if not html_content:
        return None
    match = re.search(r'<img [^>]*src="([^"]+)"', html_content)
    return match.group(1) if match else None


def process_post_attributes(post_id, attrs):
    """Process Patreon post attributes"""
    image_url = None

    if attrs.get("embed_data"):
        embed = attrs["embed_data"]
        if "image" in embed and "url" in embed["image"]:
            image_url = embed["image"]["url"]
        elif "thumbnail_url" in embed:
            image_url = embed["thumbnail_url"]

    if not image_url:
        image_url = extract_image_from_html(attrs.get("content"))

    return {
        "id": post_id,
        "attributes": {
            "embed_data": attrs.get("embed_data"),
            "title": attrs["title"],
            "content": attrs["content"],
            "published_at": attrs["published_at"],
            "url": attrs["url"],
            "is_public": attrs["is_public"],
            "image": {"large_url": image_url}
        },
        "type": "post"
    }


_file_cache: dict = {}
_file_cache_lock = threading.Lock()
_FILE_CACHE_TTL = 3  # seconds


def get_file_content(folder_path, filename):
    """Read file content safely with caching and retry to reduce file-lock contention.

    The C# SysBot writes to these files with exclusive access (FileShare.None).
    Caching minimises how often the file is opened, and the retry handles the
    brief window where C# holds an exclusive write lock.
    """
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
    """Process island data for Dodo API"""
    name = entry.name.upper()

    raw_dodo = get_file_content(entry.path, "Dodo.txt")
    raw_visitors = _parse_visitor_value(get_file_content(entry.path, "Visitors.txt"))

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


def _build_island_response(entry, island_type, db_island, discord_bot_online=None, viewer_is_mod=False):
    """Build the enriched island response merging live filesystem data with DB metadata."""
    name = entry.name.upper()

    raw_dodo = get_file_content(entry.path, "Dodo.txt")
    visitors, visitor_list = _parse_visitor_list(get_file_content(entry.path, "Visitors.txt"))

    # Determine live status and dodo_code from filesystem
    if island_type == "VIP" and not viewer_is_mod:
        status = "SUB ONLY"
        dodo_code = None  # Do not expose dodo code for subscriber-only islands
    elif raw_dodo is None:
        status = "OFFLINE"
        dodo_code = None
    elif raw_dodo in ["00000", "-----", ""]:
        status = "REFRESHING"
        dodo_code = None
    else:
        status = "ONLINE"
        dodo_code = raw_dodo

    # When the Discord bot is not confirmed online, hide live data to avoid stale values
    if not discord_bot_online:
        visitors = 0
        visitor_list = []
        dodo_code = None

    return {
        "id":                db_island.get("id", name.lower()),
        "name":              name,
        "cat":               db_island.get("cat", "public"),
        "description":       db_island.get("description", ""),
        "dodo_code":         dodo_code,
        "visitors":          visitors,
        "visitor_list":      visitor_list,
        "items":             db_island.get("items", []),
        "map_url":           db_island.get("map_url"),
        "seasonal":          db_island.get("seasonal", ""),
        "status":            status,
        "theme":             db_island.get("theme", "teal"),
        "type":              db_island.get("type", ""),
        "updated_at":        db_island.get("updated_at"),
        "discord_bot_online": discord_bot_online,
        "required_roles":    db_island.get("required_roles", []),
    }

# ============================================================================
# ISLAND METADATA CRUD (separate from /api/islands Dodo-status endpoint)
# ============================================================================

ALLOWED_CATEGORIES = {"public", "member"}
ALLOWED_THEMES = {"pink", "teal", "purple", "gold"}
ALLOWED_STATUSES = {"ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE"}

# ============================================================================
# AUTH ROUTES  (Discord OAuth for public website subscribers)
# ============================================================================

@app.route("/api/auth/discord")
def auth_discord():
    """Initiate Discord OAuth flow for public website subscribers."""
    if not Config.DISCORD_CLIENT_ID:
        return jsonify({"error": "Discord OAuth not configured"}), 503
    if not Config.GUILD_ID:
        return jsonify({"error": "GUILD_ID not set"}), 503

    return_to = request.args.get("return_to", "")
    # Whitelist: only allow redirect back to chopaeng.com or localhost
    allowed_hosts = {"www.chopaeng.com", "chopaeng.com", "localhost"}
    try:
        parsed = urllib.parse.urlparse(return_to)
        if parsed.hostname not in allowed_hosts:
            return_to = "https://www.chopaeng.com/auth/callback"
    except Exception:
        return_to = "https://www.chopaeng.com/auth/callback"

    state = _secrets.token_hex(16)
    session["sub_oauth_state"] = state
    session["sub_return_to"] = return_to
    callback_url = url_for("auth_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id":     Config.DISCORD_CLIENT_ID,
        "redirect_uri":  callback_url,
        "response_type": "code",
        "scope":         "identify guilds.members.read",
        "state":         state,
    })
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@app.route("/api/auth/callback")
def auth_callback():
    """Handle Discord OAuth callback for public website subscribers."""
    error = request.args.get("error")
    if error:
        return_to = session.pop("sub_return_to", "https://www.chopaeng.com/auth/callback")
        return redirect(f"{return_to}?error={urllib.parse.quote(error)}")

    state = request.args.get("state", "")
    if state != session.pop("sub_oauth_state", ""):
        return_to = session.pop("sub_return_to", "https://www.chopaeng.com/auth/callback")
        return redirect(f"{return_to}?error=invalid_state")

    code = request.args.get("code", "")
    return_to = session.pop("sub_return_to", "https://www.chopaeng.com/auth/callback")

    callback_url = url_for("auth_callback", _external=True)
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
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _DISCORD_UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_resp = json.loads(resp.read().decode())
    except Exception:
        return redirect(f"{return_to}?error=token_exchange_failed")

    access_token = token_resp.get("access_token")
    if not access_token:
        return redirect(f"{return_to}?error=no_access_token")

    # Fetch guild member record (roles + permissions)
    member_roles: list[str] = []
    member_nickname = ""
    member_perms = 0
    try:
        mem_req = urllib.request.Request(
            f"https://discord.com/api/users/@me/guilds/{Config.GUILD_ID}/member",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": _DISCORD_UA},
        )
        with urllib.request.urlopen(mem_req, timeout=10) as resp:
            member_data = json.loads(resp.read().decode())
        member_roles = [str(r) for r in member_data.get("roles", [])]
        member_nickname = (member_data.get("nick") or "").strip()
        try:
            member_perms = int(member_data.get("permissions", "0") or 0)
        except (ValueError, TypeError):
            member_perms = 0
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return redirect(f"{return_to}?error=not_a_member")
        return redirect(f"{return_to}?error=roles_fetch_failed")
    except Exception:
        return redirect(f"{return_to}?error=roles_fetch_failed")

    # Fetch basic user info
    discord_user_id = discord_username = discord_avatar_url = ""
    try:
        user_req = urllib.request.Request(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": _DISCORD_UA},
        )
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            user_data = json.loads(resp.read().decode())
        discord_user_id  = str(user_data.get("id", ""))
        discord_username = (
            member_nickname
            or user_data.get("global_name")
            or user_data.get("username", "")
        )
        avatar_hash = user_data.get("avatar") or ""
        if discord_user_id and avatar_hash and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar_hash):
            discord_avatar_url = (
                f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png?size=64"
            )
    except Exception:
        pass

    is_admin = bool(member_perms & _ADMINISTRATOR_PERM)
    token = _make_auth_token({
        "user_id":   discord_user_id,
        "username":  discord_username,
        "nickname":  member_nickname,
        "avatar":    discord_avatar_url,
        "roles":     member_roles,
        "is_admin":  is_admin,
        "is_mod":    _is_mod(member_roles) or is_admin,
    })

    logger.info("Website OAuth login: user=%s is_mod=%s", discord_username, _is_mod(member_roles) or is_admin)
    return redirect(f"{return_to}?token={urllib.parse.quote(token)}")


@app.route("/api/auth/me")
def auth_me():
    """Return the current authenticated user's info."""
    user = _current_auth_user()
    if not user:
        return jsonify({"logged_in": False}), 200
    return jsonify({
        "logged_in":  True,
        "user_id":    user["user_id"],
        "username":   user["username"],
        "nickname":   user.get("nickname", ""),
        "avatar":     user["avatar"],
        "roles":      user["roles"],
        "is_admin":   user.get("is_admin", False),
        "is_mod":     user["is_mod"],
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Invalidate the current auth token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        with _auth_tokens_lock:
            _auth_tokens.pop(token, None)
    return jsonify({"logged_out": True})


# ============================================================================
# DODO REVEAL — authenticated, fires webhook
# ============================================================================

@app.route("/api/islands/<name>/dodo", methods=["POST"])
def reveal_dodo(name):
    """Return the dodo code for an island if the user has the required role.

    The client must send:   Authorization: Bearer <token>
    On success, fires a Discord webhook and returns the dodo code.
    """
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    target = name.upper()

    # Load island metadata (cat + required_roles)
    db = get_db()
    try:
        row = db.execute(
            "SELECT cat, required_roles FROM islands WHERE UPPER(name) = ?", (target,)
        ).fetchone()
    finally:
        db.close()

    island_cat = ""
    required_roles: list[str] = []
    if row:
        island_cat = (row["cat"] or "").strip().lower()
        try:
            required_roles = json.loads(row["required_roles"] or "[]")
        except (ValueError, TypeError):
            required_roles = []

    # Safety: member islands must never become public because required_roles is empty.
    effective_required_roles = required_roles
    if island_cat == "member" and not effective_required_roles:
        effective_required_roles = _configured_subscription_role_ids()

    if island_cat == "member" and not effective_required_roles and not bool(user.get("is_mod")):
        return jsonify({"error": "Subscriber roles are not configured for this island"}), 403

    if not _has_island_access(user.get("roles", []), effective_required_roles, bool(user.get("is_mod"))):
        return jsonify({"error": "You don't have the required subscription for this island"}), 403

    # After confirming subscription access, member islands require the island access gate role
    # unless the user is an admin.
    island_access_role = str(Config.ISLAND_ACCESS_ROLE) if Config.ISLAND_ACCESS_ROLE else ""
    if island_cat == "member" and island_access_role and not bool(user.get("is_admin", False)):
        if island_access_role not in set(user.get("roles", [])):
            return jsonify({"error": "You need island access role to reveal this dodo code"}), 403

    # Find the dodo code from the filesystem
    dodo_code = None
    for base_dir in [Config.DIR_FREE, Config.DIR_VIP]:
        if not base_dir or not os.path.exists(base_dir):
            continue
        for candidate in [target, name]:
            path = os.path.join(base_dir, candidate)
            if os.path.isdir(path):
                raw = get_file_content(path, "Dodo.txt")
                if raw and raw not in ["00000", "-----", "", "GETTIN'"]:
                    dodo_code = raw
                break
        if dodo_code:
            break

    if not dodo_code:
        return jsonify({"error": "Dodo code not available right now"}), 404

    # Fire webhook in background thread so the response isn't delayed
    threading.Thread(
        target=_fire_dodo_webhook,
        args=(
            user["username"],
            user.get("nickname", ""),
            user.get("user_id", ""),
            user["avatar"],
            target,
            dodo_code,
        ),
        daemon=True,
    ).start()

    return jsonify({"island": target, "dodo_code": dodo_code})

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def home():
    """API home with endpoint info"""
    return jsonify({
        "status": "online",
        "message": "ChoBot API - All systems operational",
        "endpoints": {
            "items": "/find, /api/find",
            "villagers": "/villager, /api/villager, /api/villagers/list",
            "islands": "/api/islands, /api/islands/<name>/visitors",
            "patreon": "/api/patreon/posts, /api/patreon/posts/<id>",
            "status": "/status",
            "refresh": "/api/refresh (POST)",
            "health": "/health"
        },
        "data_freshness": {
            "islands": (
                f"Near-real-time — dodo codes and visitor counts are read directly from "
                f"island bot files, cached for up to {_FILE_CACHE_TTL} seconds."
            ),
            "items_villagers": (
                "Refreshed from Google Sheets on a scheduled interval. "
                "See /health for refresh_interval_seconds and next_update."
            ),
        },
    })

@app.route('/health')
@app.route('/api/health')
def health():
    """Health check endpoint for monitoring"""
    if data_manager is None:
        return jsonify({"status": "unavailable", "error": "Data manager not initialised"}), 503

    with data_manager.lock:
        cache_count = len(data_manager.cache)
        last_update = data_manager.last_update

    is_healthy = cache_count > 0 and last_update is not None

    refresh_interval_seconds = int(data_manager.cache_refresh_hours * 3600)
    if last_update is not None:
        next_update = (last_update + timedelta(seconds=refresh_interval_seconds)).isoformat()
    else:
        next_update = None

    response = {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": datetime.now().isoformat(),
        "cache": {
            "items": cache_count,
            "last_update": last_update.isoformat() if last_update else None,
            "refresh_interval_seconds": refresh_interval_seconds,
            "next_update": next_update,
        },
        "islands": {
            "file_cache_ttl_seconds": _FILE_CACHE_TTL,
        },
    }

    status_code = 200 if is_healthy else 503
    return jsonify(response), status_code

# --- ITEM SEARCH ROUTES ---

@app.route('/find')
def find_item():
    """Text response for item search"""
    user = request.args.get('user', 'User')
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return f"Hey {user}, type !find <item name> to search."

    if data_manager is None:
        return f"Hey {user}, the search service is not available right now. Please try again later."

    with data_manager.lock:
        cache = data_manager.cache

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
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !find <item name> to search."})

    if data_manager is None:
        return jsonify({"error": "Service unavailable — data manager not initialised"}), 503

    with data_manager.lock:
        cache = data_manager.cache

    found_locs = cache.get(query)

    if found_locs:
        free, sub = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        return jsonify({
            "found": True,
            "query": query,
            "results": {"free": free, "sub": sub},
            "suggestions": [],
            "message": f"Hey {user}, I found {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(cache.keys()), limit=5, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        return jsonify({
            "found": False,
            "query": query,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    return jsonify({
        "found": False,
        "query": query,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find \"{query}\" or anything similar."
    })


# --- VILLAGER SEARCH ROUTES ---

@app.route('/villager')
def find_villager():
    """Text response for villager search"""
    user = request.args.get('user', 'User')
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return f"Hey {user}, type !villager <n> to search."

    if data_manager is None:
        return f"Hey {user}, the search service is not available right now. Please try again later."

    villager_map = data_manager.get_villagers([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
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
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !villager <n> to search."})

    if data_manager is None:
        return jsonify({"error": "Service unavailable — data manager not initialised"}), 503

    villager_map = data_manager.get_villagers([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
    found_locs = villager_map.get(query)

    if found_locs:
        free, sub = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        return jsonify({
            "found": True,
            "query": query,
            "results": {"free": free, "sub": sub},
            "suggestions": [],
            "message": f"Hey {user}, I found villager {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(villager_map.keys()), limit=3, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        return jsonify({
            "found": False,
            "query": query,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find villager \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    return jsonify({
        "found": False,
        "query": query,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find a villager named \"{query}\"."
    })


@app.route('/api/villagers/list')
def api_list_villagers_by_island():
    """List all villagers grouped by island"""
    if data_manager is None:
        return jsonify({"error": "Service unavailable — data manager not initialised"}), 503

    villager_map = data_manager.get_villagers([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
    island_manifest = {}

    for villager_name, locations in villager_map.items():
        loc_list = locations.split(", ")
        for loc in loc_list:
            if loc not in island_manifest:
                island_manifest[loc] = []
            island_manifest[loc].append(villager_name.title())

    for loc in island_manifest:
        island_manifest[loc].sort()

    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "total_islands": len(island_manifest),
        "islands": island_manifest
    })


# --- DODO CODE / ISLAND STATUS ROUTES ---

@app.route('/api/islands', methods=['GET'])
def get_islands():
    """Get all island statuses and Dodo codes with full metadata."""
    viewer = _current_auth_user()
    viewer_roles = viewer.get("roles", []) if viewer else []
    viewer_is_mod = bool(viewer and (viewer.get("is_mod") or _is_mod(viewer_roles)))

    # Load island metadata from DB, keyed by uppercase name
    db_map = {}
    discord_status = {}
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, cat, description, items, map_url, seasonal, theme, type, updated_at, required_roles "
            "FROM islands ORDER BY name"
        ).fetchall()
        for row in rows:
            isl = row_to_island_dict(dict(row))
            # Keep frontend gating aligned with reveal endpoint safety logic.
            if (isl.get("cat") or "").strip().lower() == "member" and not (isl.get("required_roles") or []):
                isl["required_roles"] = _configured_subscription_role_ids()
            if isl.get("name"):
                db_map[isl["name"].upper()] = isl
        # Load Discord bot presence data
        bot_rows = db.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        for r in bot_rows:
            discord_status[r["island_id"]] = bool(r["is_online"])
    except sqlite3.Error:
        logger.exception("Failed to load island metadata from DB for /api/islands")
    finally:
        db.close()

    results = []

    if os.path.exists(Config.DIR_FREE):
        with os.scandir(Config.DIR_FREE) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    results.append(_build_island_response(
                        entry, "Free", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                        viewer_is_mod,
                    ))

    if os.path.exists(Config.DIR_VIP):
        with os.scandir(Config.DIR_VIP) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    results.append(_build_island_response(
                        entry, "VIP", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                        viewer_is_mod,
                    ))

    results.sort(key=lambda x: x['name'])
    return jsonify({
        "meta": {
            "timestamp": datetime.now().isoformat(),
            "cache_ttl_seconds": _FILE_CACHE_TTL,
            "note": (
                f"Dodo codes and visitor counts are read directly from files written by "
                f"the C# island bot. Each file read is cached for up to "
                f"{_FILE_CACHE_TTL} seconds, so data is near-real-time."
            ),
        },
        "data": results,
    })


# --- PATREON ROUTES ---


@app.route('/api/islands/<name>/visitors', methods=['GET'])
def get_island_visitors(name):
    """Get the current visitor list for a single island by name.

    Reads the live Visitors.txt file written by the C# island bot and returns
    the parsed list of in-game names currently on the island.

    Returns 404 if no island directory with that name is found.
    """
    target = name.upper()

    # Load bot online status for all islands (same pattern as get_islands)
    discord_status = {}
    db = get_db()
    try:
        bot_rows = db.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        for r in bot_rows:
            discord_status[r["island_id"]] = bool(r["is_online"])
    except sqlite3.Error:
        pass
    finally:
        db.close()

    # Search Free and VIP directories for a matching island folder
    for base_dir, island_type in [(Config.DIR_FREE, "Free"), (Config.DIR_VIP, "VIP")]:
        if not base_dir or not os.path.exists(base_dir):
            continue
        with os.scandir(base_dir) as entries:
            for entry in entries:
                if entry.is_dir() and entry.name.upper() == target:
                    discord_bot_online = discord_status.get(target.lower())

                    raw_content = get_file_content(entry.path, "Visitors.txt")
                    visitor_count, visitor_list = _parse_visitor_list(raw_content)

                    # Hide live data when the Discord bot is offline
                    if not discord_bot_online:
                        visitor_count = 0
                        visitor_list = []

                    return jsonify({
                        "island":        target,
                        "type":          island_type,
                        "visitor_count": visitor_count,
                        "visitor_list":  visitor_list,
                        "bot_online":    discord_bot_online,
                        "timestamp":     datetime.now().isoformat(),
                    })

    return jsonify({"error": f"Island '{name}' not found"}), 404


@app.route("/api/patreon/posts", methods=["GET"])
def get_patreon_posts():
    """Get recent Patreon posts (cached 15 min)"""
    now = datetime.now()
    if patreon_cache["list"]["data"] and patreon_cache["list"]["timestamp"]:
        if (now - patreon_cache["list"]["timestamp"]) < timedelta(minutes=15):
            return jsonify(patreon_cache["list"]["data"])

    url = f"https://www.patreon.com/api/oauth2/v2/campaigns/{Config.PATREON_CAMPAIGN_ID}/posts"
    headers = {"Authorization": f"Bearer {Config.PATREON_TOKEN}"}
    params = {
        "fields[post]": "title,content,published_at,url,is_public,embed_data,embed_url",
        "sort": "-published_at",
        "page[count]": 10
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            return jsonify({"error": "Patreon API Error", "details": response.text}), response.status_code

        raw_data = response.json()
        processed_data = [process_post_attributes(p["id"], p["attributes"]) for p in raw_data["data"]]

        result = {"data": processed_data}
        patreon_cache["list"] = {"data": result, "timestamp": now}
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Server error", "details": str(e)}), 500


@app.route("/api/patreon/posts/<post_id>", methods=["GET"])
def get_single_post(post_id):
    """Get a specific Patreon post (cached 15 min)"""
    now = datetime.now()

    if post_id in patreon_cache["posts"]:
        cached_post = patreon_cache["posts"][post_id]
        if (now - cached_post["timestamp"]) < timedelta(minutes=15):
            return jsonify(cached_post["data"])

    url = f"https://www.patreon.com/api/oauth2/v2/posts/{post_id}"
    headers = {"Authorization": f"Bearer {Config.PATREON_TOKEN}"}
    params = {"fields[post]": "title,content,published_at,url,is_public,embed_data,embed_url"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            return jsonify({"error": "Post not found or API error", "details": response.text}), response.status_code

        raw_data = response.json()
        processed_post = process_post_attributes(raw_data["data"]["id"], raw_data["data"]["attributes"])

        result = {"data": processed_post}
        patreon_cache["posts"][post_id] = {"data": result, "timestamp": now}
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Server error", "details": str(e)}), 500


# --- STATUS ROUTE ---
@app.route('/status')
def status():
    """Get bot status"""
    if data_manager is None:
        return "Service unavailable — data manager not initialised.", 503
    with data_manager.lock:
        count = len(data_manager.cache)
        last_up = data_manager.last_update.strftime("%H:%M:%S") if data_manager.last_update else "Loading..."
    return f"Items: {count} | Last Update: {last_up}"


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Manually trigger a cache refresh from Google Sheets"""
    if data_manager is None:
        return jsonify({"error": "Service unavailable — data manager not initialised"}), 503

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


def run_flask_app(host='0.0.0.0', port=8100):
    """Run Flask app with retry logic for port binding after OTA restart."""
    logger.info(f"[FLASK] Starting API server on {host}:{port}...")
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