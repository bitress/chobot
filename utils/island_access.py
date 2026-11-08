"""
Unified Island Access & Entitlement Engine.

Island access mirrors each island's Discord channel role requirements, which the bot syncs
into islands.required_roles (see DiscordCommandBot.fetch_islands).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from utils.config import Config
from utils.helpers import clean_text

logger = logging.getLogger("IslandAccess")

DISCORD_USER_AGENT = "DiscordBot (https://chopaeng.com, 1.0)"
ADMINISTRATOR_PERM = 0x8
VIEW_CHANNEL_PERM = 1 << 10

ROLE_NAME_CACHE_TTL = 3600
CHANNEL_OVERWRITE_CACHE_TTL = 300
GUILD_CHANNELS_CACHE_TTL = 300

# Bidirectional mapping between Discord Snowflake IDs and canonical Tier Names
ROLE_ID_TO_TIER_NAME: Dict[str, str] = {
    str(getattr(Config, "ROLE_CHOSOUP", 1031974843656192080)): "ChoSoup",
    str(getattr(Config, "ROLE_Y_CHOSOUP", 1048323845771247681)): "y ChoSoup",
    str(getattr(Config, "ROLE_CHOTATO_CLUB", 898373041799696454)): "ChoTato Club",
    str(getattr(Config, "ROLE_Y_CHOTATO_CLUB", 1048323845771247679)): "y ChoTato",
    str(getattr(Config, "ROLE_CHOCOLATE", 1039516557405081601)): "ChoColate",
    str(getattr(Config, "ROLE_Y_CHOCOLATE", 1048323845771247680)): "y ChoColate",
    str(getattr(Config, "ROLE_CHOFRIES", 1031974954113179729)): "ChoFries",
    str(getattr(Config, "ROLE_SERVER_BOOSTER", 760256226448638006)): "Server Booster",
}
TIER_NAME_TO_ROLE_ID: Dict[str, str] = {v: k for k, v in ROLE_ID_TO_TIER_NAME.items()}

# Tier display labels with platform source
TIER_LABELS: Dict[str, str] = {
    "ChoSoup": "ChoSoup (Patreon Tier 3)",
    "y ChoSoup": "y ChoSoup (YouTube Tier 3)",
    "ChoColate": "ChoColate (Patreon Tier 2)",
    "y ChoColate": "y ChoColate (YouTube Tier 2)",
    "ChoTato Club": "ChoTato Club (Twitch Tier 1)",
    "y ChoTato": "y ChoTato (YouTube Tier 1)",
    "ChoFries": "ChoFries (Patreon Tier 1)",
    "Server Booster": "Server Booster (Discord)",
}


def canonical_island_key(value: str | None) -> str:
    """Normalize an island or channel name for matching (e.g. '01 Hiraya' -> 'hiraya')."""
    if not value:
        return ""
    raw = str(value).strip().lower()
    raw = re.sub(r"^sub[\s_-]*island[\s_-]*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^\d+[\s_-]*", "", raw)
    return clean_text(raw)


def normalize_roles(roles: Union[List[Any], Set[Any], Tuple[Any, ...]]) -> Set[str]:
    """Convert a mixture of role IDs and tier names to canonical tier names & original IDs."""
    result = set()
    for r in roles or []:
        s = str(r).strip()
        if not s:
            continue
        result.add(s)
        if s in ROLE_ID_TO_TIER_NAME:
            result.add(ROLE_ID_TO_TIER_NAME[s])
        elif s in TIER_NAME_TO_ROLE_ID:
            result.add(TIER_NAME_TO_ROLE_ID[s])
    return result


@dataclass(frozen=True)
class IslandAccessInfo:
    required_roles: list[str]
    channel_id: str | None
    access_source: str

    @property
    def role_count(self) -> int:
        return len(self.required_roles)


def clear_access_caches() -> None:
    """Clear access caches."""
    global _db_roles_cache
    _db_roles_cache = None


def is_mod(roles: list[str] | set[str] | tuple[str, ...], user: dict[str, Any] | None = None) -> bool:
    """True if the user holds one of the configured moderator/admin roles or flags."""
    if user and (user.get("is_admin") or user.get("is_mod")):
        return True

    mod_ids = {
        str(Config.ADMIN_ROLE_ID or ""),
        str(Config.SENIOR_MOD_ROLE_ID or ""),
        str(Config.BABY_MOD_ROLE_ID or ""),
    } - {"", "0", "None"}

    clean_roles = {str(role_id).strip().lower() for role_id in (roles or [])}
    if any(m in clean_roles for m in ("admin", "administrator", "mod", "moderator")):
        return True

    return bool(mod_ids & {str(role_id) for role_id in (roles or [])})


def is_member_island(cat: str | None, island_type: str | None = None, island_name: str | None = None) -> bool:
    """Return whether an island should be gated behind member roles/tiers."""
    if island_name:
        clean = canonical_island_key(island_name)
        if clean in {canonical_island_key(n) for n in (Config.SUB_ISLANDS or [])}:
            return True
    return (cat or "").strip().lower() == "member" or (island_type or "").strip().upper() == "VIP"


def get_user_sub_tier(user: Optional[Dict[str, Any]]) -> Optional[int]:
    """
    Resolve numeric subscription tier for a user payload:
    - Tier 3: ChoSoup / y ChoSoup, admin, mod
    - Tier 2: ChoColate / y ChoColate
    - Tier 1: ChoTato Club / y ChoTato / ChoFries
    """
    if not user:
        return None
    if user.get("is_admin") or user.get("is_mod"):
        return 3

    roles = normalize_roles(user.get("roles", []))
    sub_t = user.get("sub_tier")
    if sub_t:
        roles.add(str(sub_t).strip())

    if "ChoSoup" in roles or "y ChoSoup" in roles:
        return 3
    if "ChoColate" in roles or "y ChoColate" in roles:
        return 2
    if "ChoTato Club" in roles or "y ChoTato" in roles or "ChoFries" in roles or "Server Booster" in roles:
        return 1
    return None


def non_subscription_gate_role_ids() -> set[str]:
    """Roles that may open a sub island channel without being a subscription.

    The island-access / sub-rules role (rules accepted) and @everyone stay with a member
    after their subscription ends, so they must never unlock a sub island's Dodo code.
    """
    ids = {
        str(getattr(Config, "ISLAND_ACCESS_ROLE", "") or ""),
        str(getattr(Config, "SUB_RULES_ROLE_ID", "") or ""),
        str(getattr(Config, "GUILD_ID", "") or ""),
    }
    return {i for i in ids if i and i not in {"0", "None"}}


def _subscription_roles_only(roles: List[Any] | Set[Any] | Tuple[Any, ...] | None) -> list[str]:
    gate = non_subscription_gate_role_ids()
    return [str(r) for r in (roles or []) if str(r).strip() and str(r).strip() not in gate]


def configured_subscription_role_ids() -> list[str]:
    """Return all subscription tier names and configured role IDs (never the rules/gate role)."""
    tiers_and_roles: list[str] = list(TIER_NAME_TO_ROLE_ID.keys()) + list(ROLE_ID_TO_TIER_NAME.keys())
    for attr_name in (
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
                    tiers_and_roles.append(str(item))
        else:
            tiers_and_roles.append(str(value))
    return list(dict.fromkeys(_subscription_roles_only(tiers_and_roles)))


def excluded_profile_role_ids() -> set[str]:
    """Role IDs that should not appear as user subscription roles."""
    excluded = {
        str(Config.GUILD_ID or ""),
        str(Config.ISLAND_BOT_ROLE_ID or ""),
    }
    return {role_id for role_id in excluded if role_id and role_id not in {"0", "None"}}


_db_roles_cache: tuple[dict[str, list[str]], float] | None = None
DB_ROLES_CACHE_TTL = 60


def discord_synced_island_roles(island_name: str | None) -> list[str]:
    """Role IDs that can view the island's Discord channel, as last synced by the bot.

    The bot's fetch_islands writes the channel's view_channel role overwrites to
    islands.required_roles, so this mirrors what members can see in Discord.
    Returns [] when the island has not been synced.
    """
    global _db_roles_cache
    key = canonical_island_key(island_name)
    if not key:
        return []

    now = time.monotonic()
    if _db_roles_cache is None or now - _db_roles_cache[1] > DB_ROLES_CACHE_TTL:
        by_island: dict[str, list[str]] = {}
        try:
            from utils.database import connect_db
            with connect_db() as conn:
                for row in conn.execute("SELECT name, required_roles FROM islands").fetchall():
                    try:
                        roles = json.loads(row.get("required_roles") or "[]")
                    except (TypeError, ValueError):
                        roles = []
                    by_island[canonical_island_key(row.get("name"))] = [str(r) for r in roles if str(r)]
        except Exception as exc:
            logger.warning("Could not load synced island roles: %s", exc)
            return _db_roles_cache[0].get(key, []) if _db_roles_cache else []
        _db_roles_cache = (by_island, now)
    return _db_roles_cache[0].get(key, [])


def effective_island_required_roles(
    cat: str | None,
    required_roles: list[str] | None = None,
    island_type: str | None = None,
    island_name: str | None = None,
) -> list[str]:
    """
    Return the roles that unlock an island.

    Priority: the Discord channel's role requirements (synced to the database by the bot),
    then, for member/VIP islands not synced yet, all configured subscription tiers.
    """
    # 1. Explicit roles passed in, or the Discord-synced roles stored for this island
    synced = list(required_roles or []) or discord_synced_island_roles(island_name)
    if is_member_island(cat, island_type, island_name):
        # A sub island needs a subscription: the rules/gate role never counts, even if
        # the channel's overwrites let it view the channel.
        synced = _subscription_roles_only(synced)
    if synced:
        expanded = list(normalize_roles(synced))
        if expanded:
            return expanded

    # 2. For member/VIP islands, return all configured subscription tiers
    if is_member_island(cat, island_type, island_name):
        return configured_subscription_role_ids()

    return []


def has_island_access(
    roles: Union[list[str], set[str], tuple[str, ...], dict[str, Any]],
    required_roles: list[str] | set[str] | tuple[str, ...] | None = None,
    is_mod_user: bool = False,
    island_name: str | None = None,
    user: dict[str, Any] | None = None,
) -> bool:
    """
    Evaluate whether a viewer can access an island based on the Unified Entitlement Engine.
    Supports user roles, subscription tiers (ChoSoup, y ChoSoup, ChoColate, ChoTato Club, ChoFries, etc.),
    legacy Discord snowflake IDs, and admin/mod bypasses.
    """
    # Defensive unwrapping if a user dict was passed as the first parameter
    if isinstance(roles, dict):
        if user is None:
            user = roles
        roles = user.get("roles", [])

    if user and (user.get("is_admin") or user.get("is_mod")):
        return True

    if is_mod_user or is_mod(roles, user=user):
        return True

    user_norm = normalize_roles(roles)
    if user and isinstance(user, dict):
        # Only Discord roles count (sign-in is Discord only): user["roles"] is refreshed live
        # from the guild. sub_tier and connected_accounts are login-time snapshots, so a
        # lapsed sub or boost would otherwise keep opening islands.
        if "roles" in user and user["roles"]:
            user_norm.update(normalize_roles(user["roles"]))

    # 1. Roles passed in, else the Discord channel's roles synced by the bot
    needed = list(required_roles or [])
    if island_name:
        if not needed:
            needed = discord_synced_island_roles(island_name)
        if is_member_island(None, None, island_name):
            # Sub islands need a subscription; the rules/gate role kept after a sub ends never counts
            needed = _subscription_roles_only(needed)
        if not needed:
            if not is_member_island(None, None, island_name):
                # Not a Sub Island and not marked as a member island -> public/free
                return True
            # Sub island Discord hasn't synced yet: any subscription tier unlocks it
            needed = configured_subscription_role_ids()

    if needed:
        return bool(normalize_roles(needed) & user_norm)

    # 2. No requirements known: public/free islands are open to all.
    return True


def resolved_island_required_roles(
    island_name: str | None,
    cat: str | None,
    required_roles: list[str] | None,
    island_type: str | None = None,
    channel_id: str | None = None,
    *,
    force_refresh: bool = False,
) -> IslandAccessInfo:
    """
    Resolve island access roles from the Discord-synced channel requirements.
    Returns immediately without failing Discord network calls.
    """
    effective_roles = effective_island_required_roles(
        cat,
        required_roles=required_roles,
        island_type=island_type,
        island_name=island_name,
    )
    has_synced = bool(list(required_roles or []) or discord_synced_island_roles(island_name))
    return IslandAccessInfo(
        required_roles=effective_roles,
        channel_id=channel_id,
        access_source="discord_channel" if has_synced else "not_synced",
    )


def get_guild_role_names() -> dict[str, str]:
    """Return friendly display names for subscription tiers and roles."""
    result: dict[str, str] = {}
    for tier, label in TIER_LABELS.items():
        result[tier] = label
        if tier in TIER_NAME_TO_ROLE_ID:
            result[TIER_NAME_TO_ROLE_ID[tier]] = label
    return result


def role_payload(role_id: str, role_names: dict[str, str] | None = None) -> dict:
    """Format role object for API responses."""
    rid_str = str(role_id).strip()
    names = role_names or get_guild_role_names()
    name = names.get(rid_str)
    if not name or name == rid_str:
        if rid_str in ROLE_ID_TO_TIER_NAME:
            tier_key = ROLE_ID_TO_TIER_NAME[rid_str]
            name = TIER_LABELS.get(tier_key, tier_key)
        elif rid_str in TIER_LABELS:
            name = TIER_LABELS[rid_str]
        else:
            name = rid_str
    return {
        "id": rid_str,
        "name": name,
    }


def sync_island_role_cache(conn, islands: list[dict], *, force_refresh: bool = True) -> dict:
    """Refresh islands.required_roles in SQLite from Discord (read-only; the bot is the writer)."""
    clear_access_caches()
    synced = 0
    skipped = 0
    errors: list[dict] = []
    items: list[dict] = []

    for island in islands:
        island_id = str(island.get("id") or island.get("name") or "").lower()
        island_name = island.get("name") or island_id
        if not island_id:
            skipped += 1
            continue

        info = resolved_island_required_roles(
            island_name,
            island.get("cat"),
            island.get("required_roles", []),
            island.get("type"),
            island.get("channel_id"),
            force_refresh=force_refresh,
        )

        # The bot's fetch_islands is the only writer of islands.required_roles (from the
        # Discord channel overwrites). The fallback is never persisted, so it can't
        # masquerade as synced data.
        if info.access_source == "discord_channel":
            synced += 1
        else:
            skipped += 1
            errors.append({
                "id": island_id,
                "name": island_name,
                "error": "Not synced from Discord yet; using any-subscription fallback",
            })

        items.append({
            "id": island_id,
            "name": island_name,
            "channel_id": info.channel_id,
            "required_roles": info.required_roles,
            "role_count": info.role_count,
            "access_source": info.access_source,
        })

    return {
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "items": items,
    }


# ============================================================================
# SAFE COMPATIBILITY STUBS (PREVENTS 401 ERRORS & CRASHES)
# ============================================================================

def discord_bot_auth_value() -> str | None:
    token = str(Config.DISCORD_TOKEN or "").strip()
    if not token or not getattr(Config, "DISCORD_LOGIN_ENABLED", False):
        return None
    return token if token.lower().startswith("bot ") else f"Bot {token}"


def discord_api_json(path: str, timeout: int = 5) -> dict | list | None:
    # Disable Discord REST calls if Discord login is disabled or token invalid
    return None


def discord_channel_overwrite_roles(channel_id: str | None, *, force_refresh: bool = False) -> tuple[list[str] | None, str | None]:
    return None, channel_id


def discord_guild_channels(*, force_refresh: bool = False) -> list[dict[str, Any]] | None:
    return None


def find_discord_island_channel_id(island_name: str | None, *, force_refresh: bool = False) -> str | None:
    return None
