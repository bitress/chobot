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
    target_id = str(user.get("discord_id") or user.get("user_id") or "").strip()
    if not target_id.isdigit():
        conn_disc = (user.get("connected_accounts") or {}).get("discord") or {}
        if conn_disc.get("id") and str(conn_disc["id"]).strip().isdigit():
            target_id = str(conn_disc["id"]).strip()
    if not target_id.isdigit():
        return user

    snapshot = fetch_guild_member_snapshot(target_id)
    refreshed = dict(user)
    for key, value in snapshot.items():
        if key == "user_id" and user.get("user_id") and not str(user["user_id"]).isdigit():
            continue
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


def add_guild_member_role(user_id: int | str, role_id: int | str) -> bool:
    """Add a role to a Discord guild member via REST API (zero gateway intents)."""
    uid = str(user_id).strip()
    rid = str(role_id).strip()
    guild_id = str(Config.GUILD_ID or "").strip()
    if not uid.isdigit() or not rid.isdigit() or not guild_id:
        return False
    try:
        headers = _bot_headers()
    except Exception:
        return False

    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{uid}/roles/{rid}"
    try:
        discord_request(url, method="PUT", headers=headers, timeout=8)
        logger.info("Added Discord role %s to member %s", rid, uid)
        return True
    except Exception as exc:
        logger.warning("Failed to add Discord role %s to member %s: %s", rid, uid, exc)
        return False


def remove_guild_member_role(user_id: int | str, role_id: int | str) -> bool:
    """Remove a role from a Discord guild member via REST API (zero gateway intents)."""
    uid = str(user_id).strip()
    rid = str(role_id).strip()
    guild_id = str(Config.GUILD_ID or "").strip()
    if not uid.isdigit() or not rid.isdigit() or not guild_id:
        return False
    try:
        headers = _bot_headers()
    except Exception:
        return False

    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{uid}/roles/{rid}"
    try:
        discord_request(url, method="DELETE", headers=headers, timeout=8)
        logger.info("Removed Discord role %s from member %s", rid, uid)
        return True
    except Exception as exc:
        logger.warning("Failed to remove Discord role %s from member %s: %s", rid, uid, exc)
        return False


def _sub_rules_candidate_ids(uid: str) -> list[str]:
    """All user IDs linked to this user (Discord ID, unified ID, provider aliases)."""
    cand_ids = [uid]
    try:
        from utils.unified_auth import resolve_all_user_ids
        resolved = resolve_all_user_ids({"user_id": uid, "discord_id": uid})
        if resolved:
            cand_ids = list(set(cand_ids + resolved))
    except Exception:
        pass
    return cand_ids


def _island_gate_role_ids() -> list[str]:
    """The island-access gate role(s) revoked on warn/timeout/ban (never subscriber tiers)."""
    role_ids: list[str] = []
    for candidate in (getattr(Config, "ISLAND_ACCESS_ROLE", None), getattr(Config, "SUB_RULES_ROLE_ID", None)):
        value = str(candidate or "").strip()
        if value and value not in {"0", "None"} and value not in role_ids:
            role_ids.append(value)
    return role_ids or [str(1077997850165772398)]


def snapshot_sub_rules_acceptance(user_id: int | str) -> dict:
    """Record what revoke_sub_rules_acceptance is about to remove, so an undo can put it back."""
    uid = str(user_id).strip()
    snapshot: dict = {"acceptance": [], "gate_roles": []}
    if not uid:
        return snapshot
    cand_ids = _sub_rules_candidate_ids(uid)
    try:
        from utils.database import connect_db
        with connect_db() as conn:
            ph = ",".join("?" for _ in cand_ids)
            rows = conn.execute(
                f"SELECT user_id, accepted_at FROM user_sub_rules_accepted WHERE user_id IN ({ph})", cand_ids
            ).fetchall()
            snapshot["acceptance"] = [{"user_id": str(r[0]), "accepted_at": r[1]} for r in rows]
    except Exception as exc:
        logger.warning("Failed to snapshot user_sub_rules_accepted for %s: %s", uid, exc)
    if uid.isdigit():
        try:
            roles = set(fetch_guild_member_snapshot(uid).get("roles", []))
            snapshot["gate_roles"] = [rid for rid in _island_gate_role_ids() if rid in roles]
        except Exception as exc:
            logger.debug("Could not snapshot gate roles for %s: %s", uid, exc)
    return snapshot


def restore_sub_rules_acceptance(discord_id: int | str | None, snapshot: dict | None) -> bool:
    """Undo revoke_sub_rules_acceptance using a snapshot taken before it ran."""
    snapshot = snapshot or {}
    restored = False
    try:
        from utils.database import connect_db
        with connect_db() as conn:
            for row in snapshot.get("acceptance") or []:
                conn.execute(
                    "INSERT OR REPLACE INTO user_sub_rules_accepted (user_id, accepted_at) VALUES (?, ?)",
                    (row["user_id"], row["accepted_at"]),
                )
                restored = True
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to restore user_sub_rules_accepted for %s: %s", discord_id, exc)
    if discord_id and str(discord_id).isdigit():
        for role_id in snapshot.get("gate_roles") or []:
            restored = add_guild_member_role(discord_id, role_id) or restored
    return restored


def revoke_sub_rules_acceptance(user_id: int | str) -> bool:
    """Revoke website island-access acceptance without stripping subscriber tiers."""
    uid = str(user_id).strip()
    if not uid:
        return False

    # 1. Resolve all candidate user IDs linked to this user.
    cand_ids = _sub_rules_candidate_ids(uid)

    # 2. Delete from user_sub_rules_accepted in SQLite.
    try:
        from utils.database import connect_db
        with connect_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS user_sub_rules_accepted (user_id TEXT PRIMARY KEY, accepted_at TEXT)")
            del_ph = ",".join("?" for _ in cand_ids)
            conn.execute(f"DELETE FROM user_sub_rules_accepted WHERE user_id IN ({del_ph})", cand_ids)
            conn.commit()
            logger.info("Revoked user_sub_rules_accepted for IDs: %s", cand_ids)
    except Exception as exc:
        logger.warning("Failed to delete user_sub_rules_accepted for %s: %s", uid, exc)

    # 3. Remove the configured island-access gate role(s), not subscriber tiers.
    island_access_role = getattr(Config, "ISLAND_ACCESS_ROLE", None)
    sub_rules_role = getattr(Config, "SUB_RULES_ROLE_ID", None)
    role_ids: list[str] = []
    for candidate in (island_access_role, sub_rules_role):
        value = str(candidate or "").strip()
        if value and value not in {"0", "None"} and value not in role_ids:
            role_ids.append(value)
    if not role_ids:
        role_ids = [str(1077997850165772398)]

    # Only real Discord IDs: the aliases also hold numeric Twitch/YouTube/Patreon IDs.
    discord_uids = [uid] if uid.isdigit() else []
    try:
        from utils.database import connect_db
        with connect_db() as conn:
            del_ph = ",".join("?" for _ in cand_ids)
            rows = conn.execute(
                f"SELECT provider_user_id FROM user_provider_links WHERE provider = 'discord' AND unified_user_id IN ({del_ph})",
                cand_ids,
            ).fetchall()
            for r in rows:
                p_uid = str(r["provider_user_id"] or "").strip()
                if p_uid.isdigit():
                    discord_uids.append(p_uid)
    except Exception:
        pass

    for d_uid in set(discord_uids):
        for role_id in role_ids:
            remove_guild_member_role(d_uid, role_id)

    return True

