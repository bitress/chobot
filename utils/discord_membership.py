"""Live Discord guild membership checks for OAuth sessions."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error

from utils import island_access
from utils.config import Config
from utils.discord_http import request as discord_request

logger = logging.getLogger("DiscordMembership")

REFRESH_SECONDS = max(int(getattr(Config, "AUTH_DISCORD_REFRESH_SECONDS", 300) or 300), 60)
STALE_GRACE_SECONDS = max(int(getattr(Config, "AUTH_DISCORD_STALE_GRACE_SECONDS", 3600) or 3600), REFRESH_SECONDS)

_admin_role_cache: tuple[set[str], float] | None = None
_ADMIN_ROLE_CACHE_TTL = 300


class DiscordMembershipError(Exception):
    """Base class for live Discord membership failures."""


class DiscordMembershipUnavailable(DiscordMembershipError):
    """Discord membership could not be verified right now."""


class DiscordNotGuildMember(DiscordMembershipError):
    """The user is no longer a member of the configured guild."""


def _bot_headers() -> dict[str, str]:
    auth_value = island_access.discord_bot_auth_value()
    if not auth_value:
        raise DiscordMembershipUnavailable("DISCORD_TOKEN is not configured")
    return {
        "Authorization": auth_value,
        "User-Agent": island_access.DISCORD_USER_AGENT,
    }


def _admin_role_ids() -> set[str]:
    """Return role IDs that currently grant Discord Administrator."""
    configured_admin = str(Config.ADMIN_ROLE_ID or "")
    result = {configured_admin} - {"", "0", "None"}

    global _admin_role_cache
    now = time.monotonic()
    if _admin_role_cache and now - _admin_role_cache[1] < _ADMIN_ROLE_CACHE_TTL:
        return result | _admin_role_cache[0]

    guild_id = str(Config.GUILD_ID or "")
    if not guild_id:
        raise DiscordMembershipUnavailable("GUILD_ID is not configured")

    try:
        resp = discord_request(
            f"https://discord.com/api/v10/guilds/{guild_id}/roles",
            headers=_bot_headers(),
            timeout=10,
        )
        roles = json.loads(resp.body)
    except urllib.error.HTTPError as exc:
        raise DiscordMembershipUnavailable(f"Discord role fetch HTTP {exc.code}") from exc
    except Exception as exc:
        raise DiscordMembershipUnavailable(f"Discord role fetch failed: {exc}") from exc

    admin_ids: set[str] = set()
    if isinstance(roles, list):
        for role in roles:
            if not isinstance(role, dict) or not role.get("id"):
                continue
            try:
                permissions = int(role.get("permissions") or 0)
            except (TypeError, ValueError):
                permissions = 0
            if permissions & island_access.ADMINISTRATOR_PERM:
                admin_ids.add(str(role["id"]))
    _admin_role_cache = (admin_ids, now)
    return result | admin_ids


def fetch_guild_member_snapshot(user_id: str) -> dict:
    """Fetch the user's current guild roles and display profile from Discord."""
    uid = str(user_id or "").strip()
    guild_id = str(Config.GUILD_ID or "")
    if not uid or not guild_id:
        raise DiscordMembershipUnavailable("Missing user_id or GUILD_ID")

    try:
        resp = discord_request(
            f"https://discord.com/api/v10/guilds/{guild_id}/members/{uid}",
            headers=_bot_headers(),
            timeout=10,
        )
        member = json.loads(resp.body)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DiscordNotGuildMember("User is not in the configured guild") from exc
        raise DiscordMembershipUnavailable(f"Discord member fetch HTTP {exc.code}") from exc
    except Exception as exc:
        raise DiscordMembershipUnavailable(f"Discord member fetch failed: {exc}") from exc

    if not isinstance(member, dict):
        raise DiscordMembershipUnavailable("Discord member response was not an object")

    roles = [str(role_id) for role_id in member.get("roles", []) if str(role_id)]
    admin_roles = _admin_role_ids()
    is_admin = bool(set(roles) & admin_roles)
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    global_name = str(user.get("global_name") or "")
    account_name = str(user.get("username") or "")
    nickname = str(member.get("nick") or "").strip()
    display_name = nickname or global_name or account_name or uid
    avatar_hash = str(user.get("avatar") or "")
    avatar = ""
    if uid and avatar_hash and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar_hash):
        avatar = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=64"

    return {
        "user_id": uid,
        "username": display_name,
        "discord_name": global_name or account_name,
        "global_name": global_name,
        "account_name": account_name,
        "nickname": nickname,
        "joined_at": str(member.get("joined_at") or ""),
        "avatar": avatar,
        "roles": roles,
        "is_admin": is_admin,
        "is_mod": island_access.is_mod(roles) or is_admin,
        "discord_checked_at": int(time.time()),
    }


def refresh_user_payload(user: dict) -> dict:
    """Return a user dict updated with live Discord guild membership."""
    snapshot = fetch_guild_member_snapshot(str(user.get("user_id") or ""))
    refreshed = dict(user)
    for key, value in snapshot.items():
        refreshed[key] = value
    if refreshed.get("joined_at"):
        try:
            from datetime import datetime

            iso_value = str(refreshed["joined_at"]).replace("Z", "+00:00")
            refreshed["joined_timestamp"] = int(datetime.fromisoformat(iso_value).timestamp())
        except Exception:
            pass
    return refreshed


def should_refresh(user: dict) -> bool:
    checked_at = int(user.get("discord_checked_at") or user.get("auth_checked_at") or 0)
    return time.time() - checked_at >= REFRESH_SECONDS


def is_beyond_stale_grace(user: dict) -> bool:
    checked_at = int(user.get("discord_checked_at") or user.get("auth_checked_at") or 0)
    return not checked_at or time.time() - checked_at >= STALE_GRACE_SECONDS
