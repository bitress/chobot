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
import threading
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace

import requests
from flask import Flask, jsonify, request, session, redirect, url_for
from flask_cors import CORS
from thefuzz import process, fuzz

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import ThreadedWSGIServer

from utils.config import Config
from utils import island_access
from utils.database import connect_db
from utils.discord_http import json_request as discord_json_request
from utils.discord_http import request as discord_request
from utils.helpers import format_locations_text, parse_locations_json, normalize_text, clean_text
from utils.auth_tokens import get_auth_user, make_auth_token, revoke_auth_token
from api.dashboard import dashboard, init_dashboard_db, get_db, row_to_island_dict, _parse_visitor_value, _parse_visitor_list


logger = logging.getLogger("FlaskAPI")

CHOBOT_SQLITE_DB = "chobot.db"


def _client_ip() -> str:
    """Return the most useful client IP for audit logging."""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or request.headers.get("X-Real-IP", "").strip() or request.remote_addr or ""


def _post_website_login_log_message(event_id: int, event: dict) -> None:
    """Send a website-login audit message to the configured Discord channel."""
    channel_id = getattr(Config, "WEBSITE_LOGIN_LOG_CHANNEL_ID", None)
    token = str(Config.DISCORD_TOKEN or "").strip()
    if not channel_id or not token:
        return

    auth_value = token if token.lower().startswith("bot ") else f"Bot {token}"
    role_count = int(event.get("role_count") or 0)
    username = event.get("username") or event.get("discord_name") or "Unknown user"
    user_id = event.get("user_id") or ""
    payload = {
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "Website Discord Login",
            "color": 0x28A745 if event.get("is_mod") else 0x5BC0DE,
            "timestamp": event.get("created_at"),
            "thumbnail": {"url": event.get("avatar")} if event.get("avatar") else None,
            "fields": [
                {"name": "User", "value": f"{username}\n`{user_id}`", "inline": True},
                {"name": "Access", "value": f"Mod: `{bool(event.get('is_mod'))}`\nAdmin: `{bool(event.get('is_admin'))}`", "inline": True},
                {"name": "Roles", "value": f"`{role_count}` role(s)", "inline": True},
                {"name": "IP", "value": f"`{event.get('ip_address') or 'unknown'}`", "inline": True},
                {"name": "Return To", "value": event.get("return_to") or "unknown", "inline": False},
                {"name": "Event ID", "value": f"`{event_id}`", "inline": True},
            ],
            "footer": {"text": "Chopaeng website auth"},
        }],
    }
    payload["embeds"][0]["fields"] = [
        field for field in payload["embeds"][0]["fields"]
        if field.get("value") is not None
    ]
    if payload["embeds"][0]["thumbnail"] is None:
        payload["embeds"][0].pop("thumbnail", None)

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    try:
        data = discord_json_request(
            url,
            method="POST",
            payload=payload,
            headers={"Authorization": auth_value, "User-Agent": _DISCORD_UA},
            timeout=10,
        ) or {}
        message_id = str(data.get("id") or "")
        if message_id:
            db = get_db()
            try:
                db.execute(
                    "UPDATE website_login_events SET discord_message_id = ?, discord_channel_id = ?, discord_guild_id = ? WHERE id = ?",
                    (message_id, str(channel_id), str(Config.GUILD_ID or ""), event_id),
                )
                db.commit()
            finally:
                db.close()
    except Exception as exc:
        logger.warning("Website login Discord log failed: %s", exc)


def _record_website_login(event: dict) -> None:
    """Persist and asynchronously announce a successful website Discord OAuth login."""
    db = get_db()
    try:
        cur = db.execute(
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
        event_id = int(cur.lastrowid or 0)
    except Exception as exc:
        logger.warning("Website login DB log failed: %s", exc)
        return
    finally:
        db.close()

    threading.Thread(
        target=_post_website_login_log_message,
        args=(event_id, dict(event)),
        daemon=True,
    ).start()


def _persist_dodo_reveal_message(
    user_id: str,
    island_name: str,
    channel_id: str | None,
    message_url: str,
    username: str,
    nickname: str,
) -> None:
    """Store webhook message URL so Flight Logger can link unverified flights to dodo reveals."""
    island_clean = clean_text(island_name)
    if not island_clean:
        island_clean = clean_text(island_name.lower())
    try:
        conn = connect_db()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dodo_reveal_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    island_clean TEXT NOT NULL,
                    channel_id TEXT,
                    message_url TEXT NOT NULL,
                    username TEXT,
                    nickname TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO dodo_reveal_messages
                (user_id, island_clean, channel_id, message_url, username, nickname, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(user_id),
                    island_clean,
                    str(channel_id) if channel_id else None,
                    message_url,
                    username or "",
                    nickname or "",
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("dodo_reveal_messages insert failed: %s", exc)


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


# Initialize Flask app
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY
# Trust one level of X-Forwarded-For / X-Forwarded-Proto headers from the
# reverse proxy (nginx, Cloudflare Tunnel, etc.) so that url_for(_external=True)
# produces the correct https:// URL instead of http://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
CORS(app, resources={r"/*": {"origins": Config.FRONTEND_ORIGINS}}, supports_credentials=True)

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
_DISCORD_UA = "DiscordBot (https://chopaeng.com, 1.0)"
_ADMINISTRATOR_PERM = 0x8   # Discord Administrator permission bit
_VIEW_CHANNEL_PERM = 1 << 10
_ROLE_NAME_CACHE: dict[str, tuple[dict[str, str], float]] = {}
_ROLE_NAME_CACHE_TTL = 3600
_CHANNEL_OVERWRITE_CACHE: dict[str, tuple[list[str] | None, str | None, float]] = {}
_GUILD_CHANNELS_CACHE: tuple[list[dict], float] | None = None
_CHANNEL_OVERWRITE_CACHE_TTL = 300
_GUILD_CHANNELS_CACHE_TTL = 300

def _current_auth_user() -> dict | None:
    """Extract Bearer token from request and return user dict, or None."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return get_auth_user(auth[len("Bearer "):])
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
    """Return configured subscription role IDs for member-only islands."""
    role_ids: list[str] = []
    for attr_name in (
        "ISLAND_ACCESS_ROLE",
        "SUBSCRIPTION_ROLE_ID",
        "SUBSCRIPTION_ROLE_IDS",
        "MEMBER_ROLE_ID",
        "MEMBER_ROLE_IDS",
    ):
        value = getattr(Config, attr_name, None)
        if value in (None, "", "0", "None"):
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if item not in (None, "", "0", "None"):
                    role_ids.append(str(item))
        else:
            role_ids.append(str(value))
    return list(dict.fromkeys(role_ids))


def _discord_bot_auth_value() -> str | None:
    token = str(Config.DISCORD_TOKEN or "").strip()
    if not token:
        return None
    return token if token.lower().startswith("bot ") else f"Bot {token}"


def _discord_api_json(path: str, timeout: int = 10) -> dict | list | None:
    auth_value = _discord_bot_auth_value()
    if not auth_value:
        return None
    try:
        resp = discord_request(
            f"https://discord.com/api/v10{path}",
            headers={"Authorization": auth_value, "User-Agent": _DISCORD_UA},
            timeout=timeout,
        )
        return json.loads(resp.body)
    except Exception as exc:
        logger.warning("Discord API request failed for %s: %s", path, exc)
        return None


def _discord_channel_overwrite_roles(channel_id: str | None) -> tuple[list[str] | None, str | None]:
    """Return role IDs allowed to view a channel from Discord, or None when unavailable."""
    if not channel_id:
        return None, None

    channel_key = str(channel_id)
    now = time.monotonic()
    cached = _CHANNEL_OVERWRITE_CACHE.get(channel_key)
    if cached and now - cached[2] < _CHANNEL_OVERWRITE_CACHE_TTL:
        return cached[0], cached[1]

    payload = _discord_api_json(f"/channels/{channel_key}")
    if not isinstance(payload, dict):
        _CHANNEL_OVERWRITE_CACHE[channel_key] = (None, None, now)
        return None, None

    role_ids: list[str] = []
    for overwrite in payload.get("permission_overwrites") or []:
        if str(overwrite.get("type")) not in {"0", "role"}:
            continue
        role_id = str(overwrite.get("id") or "")
        if not role_id or role_id == str(Config.GUILD_ID or ""):
            continue
        try:
            allow_bits = int(overwrite.get("allow") or 0)
        except (TypeError, ValueError):
            allow_bits = 0
        if allow_bits & _VIEW_CHANNEL_PERM:
            role_ids.append(role_id)

    resolved = list(dict.fromkeys(role_ids))
    resolved_channel_id = str(payload.get("id") or channel_key)
    _CHANNEL_OVERWRITE_CACHE[channel_key] = (resolved, resolved_channel_id, now)
    return resolved, resolved_channel_id


def _discord_guild_channels() -> list[dict] | None:
    global _GUILD_CHANNELS_CACHE
    guild_id = str(Config.GUILD_ID or "")
    if not guild_id:
        return None

    now = time.monotonic()
    if _GUILD_CHANNELS_CACHE and now - _GUILD_CHANNELS_CACHE[1] < _GUILD_CHANNELS_CACHE_TTL:
        return _GUILD_CHANNELS_CACHE[0]

    payload = _discord_api_json(f"/guilds/{guild_id}/channels")
    if not isinstance(payload, list):
        return None

    channels = [channel for channel in payload if isinstance(channel, dict)]
    _GUILD_CHANNELS_CACHE = (channels, now)
    return channels


def _find_discord_island_channel_id(island_name: str | None) -> str | None:
    """Find a member island channel by normalized island name in the configured sub category."""
    island_clean = re.sub(r"^\d+", "", clean_text(island_name or ""))
    if not island_clean:
        return None

    category_id = str(Config.CATEGORY_ID or "")
    channels = _discord_guild_channels()
    if not channels:
        return None

    for channel in channels:
        if category_id and str(channel.get("parent_id") or "") != category_id:
            continue
        channel_clean = re.sub(r"^\d+", "", clean_text(str(channel.get("name") or "")))
        if channel_clean == island_clean:
            return str(channel.get("id") or "")
    return None


def _is_member_island(cat: str | None, island_type: str | None = None) -> bool:
    """Return whether an island should be gated behind member roles."""
    return (cat or "").strip().lower() == "member" or (island_type or "").strip().upper() == "VIP"


def _effective_island_required_roles(
    cat: str | None,
    required_roles: list[str] | None,
    island_type: str | None = None,
) -> list[str]:
    """Return explicit island roles, or configured member roles for member/VIP islands."""
    roles = [str(role_id) for role_id in (required_roles or []) if str(role_id)]
    if _is_member_island(cat, island_type) and not roles:
        roles = _configured_subscription_role_ids()
    return roles


def _resolved_island_required_roles(
    island_name: str | None,
    cat: str | None,
    required_roles: list[str] | None,
    island_type: str | None = None,
    channel_id: str | None = None,
) -> tuple[list[str], str | None, str]:
    """Resolve island access roles using live Discord channel overwrites first."""
    if _is_member_island(cat, island_type):
        resolved_channel_id = str(channel_id) if channel_id else None
        dynamic_roles: list[str] | None = None
        if resolved_channel_id:
            dynamic_roles, fetched_channel_id = _discord_channel_overwrite_roles(resolved_channel_id)
            resolved_channel_id = fetched_channel_id or resolved_channel_id
        if dynamic_roles is None:
            found_channel_id = _find_discord_island_channel_id(island_name)
            if found_channel_id:
                dynamic_roles, fetched_channel_id = _discord_channel_overwrite_roles(found_channel_id)
                resolved_channel_id = fetched_channel_id or found_channel_id
        if dynamic_roles is not None:
            return dynamic_roles, resolved_channel_id, "discord_channel"

    return _effective_island_required_roles(cat, required_roles, island_type), channel_id, "database"


def _excluded_profile_role_ids() -> set[str]:
    """Role IDs that should not appear as user subscription roles."""
    excluded = {
        str(Config.GUILD_ID or ""),
        str(Config.ISLAND_BOT_ROLE_ID or ""),
    }
    return {rid for rid in excluded if rid and rid not in {"0", "None"}}


def _get_guild_role_names() -> dict[str, str]:
    """Fetch guild role ID -> name mapping from Discord, cached briefly."""
    guild_id = str(Config.GUILD_ID or "")
    if not guild_id or not Config.DISCORD_TOKEN:
        return {}

    now = time.monotonic()
    cached = _ROLE_NAME_CACHE.get(guild_id)
    if cached and now - cached[1] < _ROLE_NAME_CACHE_TTL:
        return cached[0]

    auth_value = _discord_bot_auth_value()
    if not auth_value:
        return {}
    try:
        resp = discord_request(
            f"https://discord.com/api/v10/guilds/{guild_id}/roles",
            headers={"Authorization": auth_value, "User-Agent": _DISCORD_UA},
            timeout=10,
        )
        roles = json.loads(resp.body)
        role_names = {
            str(role.get("id")): str(role.get("name") or role.get("id"))
            for role in roles
            if role.get("id") and role.get("name") != "@everyone"
        }
        _ROLE_NAME_CACHE[guild_id] = (role_names, now)
        return role_names
    except Exception as exc:
        logger.warning("Failed to fetch Discord guild role names: %s", exc)
        return cached[0] if cached else {}


def _role_payload(role_id: str, role_names: dict[str, str]) -> dict:
    """Small role object for profile responses."""
    return {
        "id": str(role_id),
        "name": role_names.get(str(role_id), str(role_id)),
    }


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
        "role_names": [role["name"] for role in subscription_roles],
        "roles": subscription_roles,
        "configured_subscription_role_ids": sorted(configured_role_ids - excluded_role_ids),
        "configured_subscription_role_names": [role["name"] for role in configured_subscription_roles],
        "configured_subscription_roles": configured_subscription_roles,
        "matched_subscription_role_ids": matched_subscription_role_ids,
        "matched_subscription_role_names": [role["name"] for role in matched_subscription_roles],
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


def _fire_dodo_webhook(
    username: str,
    nickname: str,
    user_id: str,
    avatar_url: str,
    island_name: str,
    dodo_code: str,
    channel_id: str = None,
) -> None:
    """POST a Discord webhook message in the background."""
    url = Config.DODO_LOG_WEBHOOK_URL
    if not url:
        return

    display_name = (nickname or "").strip() or (username or "").strip() or "Unknown User"

    island_url_name = urllib.parse.quote(island_name)
    island_link = f"https://www.chopaeng.com/island/{island_url_name.lower()}"

    embed = {
        "title": f"✈️ Dodo Code Revealed",
        "color": 0x2ecc71,  # Emerald Green
        "description": f"<@{user_id}> has revealed the Dodo code for island: <#{channel_id}>",
        "fields": [
            {
                "name": "Member",
                "value": f"{display_name} (<@{user_id}>)",
            },
            {
                "name": "Island",
                "value": (
                    (f"<#{channel_id}>" if channel_id else "") +
                    f"\n[View Island]({island_link})"
                ),
            }
        ],
        "image": {
            "url": "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"
        },
        "footer": {
            "text": "Chopaeng Camp™ • Dodo Reveal",
            "icon_url": "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

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
        if message_url:
            _persist_dodo_reveal_message(
                user_id=str(user_id),
                island_name=island_name,
                channel_id=channel_id,
                message_url=message_url,
                username=username or "",
                nickname=nickname or "",
            )
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


def _build_island_response(
    entry,
    island_type,
    db_island,
    discord_bot_online=None,
    viewer_roles=None,
    viewer_is_mod=False,
):
    """Build the enriched island response merging live filesystem data with DB metadata."""
    name = entry.name.upper()
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
    is_member_locked = _is_member_island(island_cat, island_type) and not required_roles and not viewer_is_mod
    viewer_has_access = False if is_member_locked else _has_island_access(
        viewer_roles,
        required_roles,
        viewer_is_mod,
    )

    raw_dodo = get_file_content(entry.path, "Dodo.txt")
    visitors, visitor_list = _parse_visitor_list(get_file_content(entry.path, "Visitors.txt"))

    # Determine live status and dodo_code from filesystem
    if _is_member_island(island_cat, island_type) and not viewer_has_access:
        status = "SUB ONLY"
        dodo_code = None  # Do not expose dodo code for subscriber-only islands
    elif raw_dodo is None:
        status = "OFFLINE"
        dodo_code = None
    elif raw_dodo in ["00000", "-----", "", "GETTIN'"]:
        status = "REFRESHING"
        dodo_code = None
    else:
        status = "ONLINE"
        dodo_code = raw_dodo

    # Keep member/order codes behind their controlled channels/endpoints.
    if _is_member_island(island_cat, island_type) or island_type == "Order" or island_cat == "order":
        dodo_code = None

    # When the Discord bot is not confirmed online, hide live data to avoid stale values
    if not discord_bot_online:
        visitors = 0
        visitor_list = []
        dodo_code = None

    return {
        "id":                db_island.get("id", name.lower()),
        "name":              (db_island.get("display_name") or name),
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
        "updated_at":        db_island.get("updated_at"),
        "discord_bot_online": discord_bot_online,
        "channel_id":        resolved_channel_id,
        "required_roles":    required_roles,
        "access_source":     access_source,
        "accessible":        viewer_has_access,
        "viewer_has_access": viewer_has_access,
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
        resp = discord_request(
            "https://discord.com/api/oauth2/token",
            data=token_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _DISCORD_UA},
            method="POST",
            timeout=10,
        )
        token_resp = json.loads(resp.body)
    except Exception:
        return redirect(f"{return_to}?error=token_exchange_failed")

    access_token = token_resp.get("access_token")
    if not access_token:
        return redirect(f"{return_to}?error=no_access_token")

    # Fetch guild member record (roles + permissions)
    member_roles: list[str] = []
    member_nickname = ""
    member_joined_at = ""
    member_perms = 0
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
    discord_global_name = discord_account_name = ""
    try:
        resp = discord_request(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": _DISCORD_UA},
            timeout=10,
        )
        user_data = json.loads(resp.body)
        discord_user_id  = str(user_data.get("id", ""))
        discord_global_name = str(user_data.get("global_name") or "")
        discord_account_name = str(user_data.get("username") or "")
        discord_username = (
            member_nickname
            or discord_global_name
            or discord_account_name
        )
        avatar_hash = user_data.get("avatar") or ""
        if discord_user_id and avatar_hash and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar_hash):
            discord_avatar_url = (
                f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png?size=64"
            )
    except Exception:
        pass

    is_admin = bool(member_perms & _ADMINISTRATOR_PERM)
    token = make_auth_token({
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
        "is_mod":    _is_mod(member_roles) or is_admin,
    })

    is_mod_user = _is_mod(member_roles) or is_admin
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

    logger.info("Website OAuth login: user=%s is_mod=%s", discord_username, is_mod_user)
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
        "discord_name": user.get("discord_name", user["username"]),
        "nickname":   user.get("nickname", ""),
        "joined_at":  user.get("joined_at", ""),
        "avatar":     user["avatar"],
        "roles":      user["roles"],
        "is_admin":   user.get("is_admin", False),
        "is_mod":     user["is_mod"],
    })


@app.route("/api/profile")
def api_profile():
    """Return the authenticated user's Discord profile and ChoPaeng activity."""
    user = _current_auth_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    subscriptions = _load_profile_subscriptions(user)
    visits = _load_profile_visit_stats(user.get("user_id", ""))

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
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Invalidate the current auth token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        revoke_auth_token(token)
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
        _log_dodo_reveal_attempt(None, name.upper(), "denied", "not_logged_in")
        return jsonify({"error": "Authentication required"}), 401

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
            return jsonify({"error": "Island is not available"}), 404
        island_cat = (row["cat"] or "").strip().lower()
        island_type = row["type"] or ""
        channel_id = row["channel_id"]
        try:
            required_roles = json.loads(row["required_roles"] or "[]")
        except (ValueError, TypeError):
            required_roles = []
    elif Config.DIR_VIP:
        for candidate in [target, name]:
            if os.path.isdir(os.path.join(Config.DIR_VIP, candidate)):
                island_cat = "member"
                island_type = "VIP"
                break

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

    if _is_member_island(island_cat, island_type) and not effective_required_roles and not is_viewer_mod:
        _log_dodo_reveal_attempt(user, target, "denied", "no_member_roles_configured", channel_id=channel_id)
        return jsonify({"error": "Subscriber roles are not configured for this island"}), 403

    # Check for general island access role first
    island_access_role = str(Config.ISLAND_ACCESS_ROLE) if Config.ISLAND_ACCESS_ROLE else ""
    if island_access_role and not is_viewer_admin:
        if island_access_role not in set(user.get("roles", [])):
            _log_dodo_reveal_attempt(
                user,
                target,
                "denied",
                "missing_global_island_access_role",
                required_role=island_access_role,
                channel_id=channel_id,
            )
            return jsonify({
                "error": "You need the Discord island access role to reveal this Dodo code.",
                "code": "missing_global_island_access_role",
            }), 403

    if not _has_island_access(user.get("roles", []), effective_required_roles, is_viewer_mod):
        _log_dodo_reveal_attempt(
            user,
            target,
            "denied",
            "missing_island_channel_role",
            required_roles=effective_required_roles,
            channel_id=channel_id,
        )
        return jsonify({
            "error": "You do not have the Discord role required for this island channel.",
            "code": "missing_island_channel_role",
        }), 403

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
        _log_dodo_reveal_attempt(user, target, "failed", "dodo_unavailable", channel_id=channel_id)
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
            channel_id,
        ),
        daemon=True,
    ).start()

    _log_dodo_reveal_attempt(user, target, "allowed", "revealed", channel_id=channel_id)
    return jsonify({"island": target, "dodo_code": dodo_code})

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def home():
    """API home with endpoint info and system status"""
    cache_count = 0
    last_update = None
    if data_manager:
        with data_manager.lock:
            cache_count = len(data_manager.cache)
            last_update = data_manager.last_update

    return jsonify({
        "system": {
            "name": "ChoBot API",
            "version": "1.1.0",
            "status": "online" if cache_count > 0 else "initializing",
            "server_time": datetime.now().isoformat(),
        },
        "data_stats": {
            "items_in_cache": cache_count,
            "last_gsheets_sync": last_update.isoformat() if last_update else None,
            "island_file_cache_ttl": f"{_FILE_CACHE_TTL}s"
        },
        "endpoints": {
            "islands": {
                "path": "/api/islands",
                "description": "Get real-time status, visitors, and dodo codes for all islands"
            },
            "search_items": {
                "path": "/api/find?q=<item>",
                "description": "Search for item availability across all islands"
            },
            "search_villagers": {
                "path": "/api/villager?q=<name>",
                "description": "Locate specific villagers on islands"
            },
            "villager_list": {
                "path": "/api/villagers/list",
                "description": "Get all current villagers grouped by island"
            },
            "patreon_posts": {
                "path": "/api/patreon/posts",
                "description": "List cached community posts"
            },
            "health": {
                "path": "/api/health",
                "description": "Detailed system health and synchronization metrics"
            }
        }
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
        free, sub, order = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        return jsonify({
            "found": True,
            "query": query,
            "results": {"free": free, "sub": sub, "order": order},
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
        free, sub, order = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        return jsonify({
            "found": True,
            "query": query,
            "results": {"free": free, "sub": sub, "order": order},
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

    villager_map = data_manager.get_villagers([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR, Config.ORDER_BOT_DIR])
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
    viewer_is_admin = bool(viewer and viewer.get("is_admin"))
    viewer_is_mod = bool(viewer and (viewer.get("is_mod") or viewer_is_admin or _is_mod(viewer_roles)))

    # Load island metadata from DB, keyed by uppercase name
    db_map = {}
    discord_status = {}
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, display_name, is_visible, cat, description, items, map_url, seasonal, theme, type, updated_at, required_roles, channel_id "
            "FROM islands ORDER BY name"
        ).fetchall()
        for row in rows:
            isl = row_to_island_dict(dict(row))
            # Keep frontend gating aligned with reveal endpoint safety logic.
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
        # Load Discord bot presence data
        bot_rows = db.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        for r in bot_rows:
            discord_status[r["island_id"]] = bool(r["is_online"])
    except Exception:
        logger.exception("Failed to load island metadata from DB for /api/islands")
    finally:
        db.close()

    results = []

    if os.path.exists(Config.DIR_FREE):
        with os.scandir(Config.DIR_FREE) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    if db_map.get(name, {}).get("is_visible") is False:
                        continue
                    results.append(_build_island_response(
                        entry, "Free", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                        viewer_roles,
                        viewer_is_mod,
                    ))

    if os.path.exists(Config.DIR_VIP):
        with os.scandir(Config.DIR_VIP) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    if db_map.get(name, {}).get("is_visible") is False:
                        continue
                    results.append(_build_island_response(
                        entry, "VIP", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                        viewer_roles,
                        viewer_is_mod,
                    ))

    if Config.DIR_ORDER and os.path.exists(Config.DIR_ORDER):
        order_entries = []
        direct_order_files = [
            os.path.join(Config.DIR_ORDER, "Dodo.txt"),
            os.path.join(Config.DIR_ORDER, "Visitors.txt"),
            os.path.join(Config.DIR_ORDER, "Villagers.txt"),
        ]
        order_name = Config.ORDER_BOT_ISLAND or os.path.basename(Config.DIR_ORDER)
        basename_matches = clean_text(os.path.basename(Config.DIR_ORDER)) == clean_text(order_name)
        if basename_matches or any(os.path.exists(path) for path in direct_order_files):
            order_entries.append(SimpleNamespace(
                name=order_name,
                path=Config.DIR_ORDER,
            ))
        with os.scandir(Config.DIR_ORDER) as entries:
            order_entries.extend(entry for entry in entries if entry.is_dir())
        for entry in order_entries:
            name = entry.name.upper()
            default_order_meta = {
                "id": name.lower(),
                "name": name,
                "cat": "order",
                "type": "Order Bot",
                "description": "Order bot island. Dodo access is handled in the configured Discord and Twitch channels.",
                "theme": "teal",
                "seasonal": "Year-Round",
                "channel_id": str(Config.ORDER_BOT_CHANNEL_ID or ""),
                "is_visible": True,
            }
            db_meta = {**default_order_meta, **db_map.get(name, {})}
            if db_meta.get("is_visible") is False:
                continue
            results.append(_build_island_response(
                entry, "Order", db_meta,
                discord_status.get(name.lower()),
                viewer_roles,
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
            user_roles = {str(role_id) for role_id in user.get("roles", [])}
            matched = sorted(user_roles & set(required_roles))
            accessible_flag = (
                str(island.get("id") or "").lower() in accessible_ids
                or str(island.get("name") or "").upper() in accessible_names
                or _has_island_access(user.get("roles", []), required_roles, bool(user.get("is_mod") or user.get("is_admin")))
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
    except Exception:
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
    auth = request.headers.get("Authorization", "")
    secret_bearer_ok = (
        auth.startswith("Bearer ")
        and Config.DASHBOARD_SECRET
        and _secrets.compare_digest(auth[len("Bearer "):], Config.DASHBOARD_SECRET)
    )
    token_user = get_auth_user(auth[len("Bearer "):]) if auth.startswith("Bearer ") else None
    mod_bearer_ok = bool(
        token_user
        and (
            token_user.get("is_admin")
            or token_user.get("is_mod")
            or _is_mod(token_user.get("roles", []))
        )
    )
    session_ok = bool(session.get("mod_logged_in"))
    if not secret_bearer_ok and not mod_bearer_ok and not session_ok:
        return jsonify({"error": "Unauthorized"}), 401

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
