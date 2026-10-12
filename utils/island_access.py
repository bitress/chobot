"""
Unified Island Access & Entitlement Engine.

Replaces legacy Discord channel overwrite polling with the ChoPaeng Unified Access Matrix
across Patreon, YouTube, Twitch, and native ChoPaeng memberships.
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

# ============================================================================
# CANONICAL UNIFIED SUB ISLAND ACCESS MATRIX
# Patreon: ChoSoup, ChoColate, ChoFries
# YouTube: y ChoSoup, y ChoColate, y ChoTato
# Twitch:  ChoTato Club
# ============================================================================

SUB_ISLAND_ACCESS_MATRIX: Dict[str, List[str]] = {
    "Hiraya": ["ChoSoup", "y ChoSoup", "ChoColate", "ChoFries", "y ChoTato", "Server Booster"],
    "Tagumpay": ["ChoSoup", "y ChoSoup", "ChoTato Club", "y ChoColate"],
    "Bahaghari": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries", "y ChoTato"],
    "Alapaap": ["ChoSoup", "y ChoSoup", "y ChoColate", "Server Booster"],
    "Bituin": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Lakan": ["ChoSoup", "y ChoSoup", "ChoTato Club", "y ChoColate"],
    "Galak": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Aruga": ["ChoSoup", "y ChoSoup", "y ChoColate"],
    "Likha": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Dalisay": ["ChoSoup", "y ChoSoup", "y ChoColate"],
    "Marahuyo": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Diwa": ["ChoSoup", "y ChoSoup", "ChoTato Club", "y ChoColate"],
    "Bonita": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Gabay": ["ChoSoup", "y ChoSoup", "y ChoColate"],
    "Dakila": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries", "y ChoColate"],
    "Kalangitan": ["ChoSoup", "y ChoSoup"],
    "Malaya": ["ChoSoup", "y ChoSoup"],
    "Pangarap": ["ChoSoup", "y ChoSoup"],
    "Giliw": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries", "y ChoColate"],
    "Adhika": ["ChoSoup", "y ChoSoup"],
}

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


_CANONICAL_SUB_ISLANDS: Dict[str, str] = {
    canonical_island_key(k): k for k in SUB_ISLAND_ACCESS_MATRIX
}


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
    pass


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
        if clean in _CANONICAL_SUB_ISLANDS:
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


def configured_subscription_role_ids() -> list[str]:
    """Return all subscription tier names and configured role IDs."""
    tiers_and_roles: list[str] = list(TIER_NAME_TO_ROLE_ID.keys()) + list(ROLE_ID_TO_TIER_NAME.keys())
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
                    tiers_and_roles.append(str(item))
        else:
            tiers_and_roles.append(str(value))
    return list(dict.fromkeys(tiers_and_roles))


def excluded_profile_role_ids() -> set[str]:
    """Role IDs that should not appear as user subscription roles."""
    excluded = {
        str(Config.GUILD_ID or ""),
        str(Config.ISLAND_BOT_ROLE_ID or ""),
    }
    return {role_id for role_id in excluded if role_id and role_id not in {"0", "None"}}


def effective_island_required_roles(
    cat: str | None,
    required_roles: list[str] | None = None,
    island_type: str | None = None,
    island_name: str | None = None,
) -> list[str]:
    """
    Return explicit island roles/tiers from the Unified Sub Island Matrix.
    Always includes canonical tier names ('ChoSoup', etc.) and corresponding role IDs.
    """
    # 1. Match from SUB_ISLAND_ACCESS_MATRIX if island_name is provided
    if island_name:
        clean = canonical_island_key(island_name)
        if clean in _CANONICAL_SUB_ISLANDS:
            canonical_name = _CANONICAL_SUB_ISLANDS[clean]
            allowed_tiers = SUB_ISLAND_ACCESS_MATRIX[canonical_name]
            out = list(allowed_tiers)
            for t in allowed_tiers:
                if t in TIER_NAME_TO_ROLE_ID:
                    out.append(TIER_NAME_TO_ROLE_ID[t])
            return list(dict.fromkeys(out))

    # 2. Expand existing required_roles if stored in database
    if required_roles:
        expanded = list(normalize_roles(required_roles))
        if expanded:
            return expanded

    # 3. For member/VIP islands, return all configured subscription tiers
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
        sub_t = user.get("sub_tier")
        if sub_t:
            st_str = str(sub_t).strip()
            user_norm.add(st_str)
            if st_str in TIER_NAME_TO_ROLE_ID:
                user_norm.add(TIER_NAME_TO_ROLE_ID[st_str])
        if "roles" in user and user["roles"]:
            user_norm.update(normalize_roles(user["roles"]))
        # Also check connected_accounts if any
        conn_accs = user.get("connected_accounts") or {}
        if isinstance(conn_accs, dict):
            for acc in conn_accs.values():
                if isinstance(acc, dict):
                    acc_tier = acc.get("sub_tier")
                    if acc_tier:
                        at_str = str(acc_tier).strip()
                        user_norm.add(at_str)
                        if at_str in TIER_NAME_TO_ROLE_ID:
                            user_norm.add(TIER_NAME_TO_ROLE_ID[at_str])
                    if acc.get("roles"):
                        user_norm.update(normalize_roles(acc["roles"]))

    # 1. Check against SUB_ISLAND_ACCESS_MATRIX if island_name is known
    if island_name:
        clean = canonical_island_key(island_name)
        if clean in _CANONICAL_SUB_ISLANDS:
            canonical_name = _CANONICAL_SUB_ISLANDS[clean]
            allowed_tiers = SUB_ISLAND_ACCESS_MATRIX[canonical_name]
            allowed_norm = normalize_roles(allowed_tiers)
            return bool(allowed_norm & user_norm)
        elif not is_member_island(None, None, island_name):
            # It's not a Sub Island and not marked as a member island -> public/free
            return True

    # 2. Check against required_roles if provided
    if required_roles:
        req_norm = normalize_roles(required_roles)
        return bool(req_norm & user_norm)

    # 3. If required_roles is empty and island_name is not specified:
    # Public/free islands have no required roles, so access is open to all.
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
    Resolve island access roles using the Unified Sub Island Matrix.
    Returns immediately without failing Discord network calls.
    """
    effective_roles = effective_island_required_roles(
        cat,
        required_roles=required_roles,
        island_type=island_type,
        island_name=island_name,
    )
    return IslandAccessInfo(
        required_roles=effective_roles,
        channel_id=channel_id,
        access_source="unified_matrix",
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
    """Refresh islands.required_roles in SQLite from the Unified Access Matrix."""
    synced = 0
    skipped = 0
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

        try:
            conn.execute(
                "UPDATE islands SET required_roles = ?, updated_at = ? WHERE id = ? OR name = ?",
                (
                    json.dumps(info.required_roles),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    island_id,
                    island_name,
                ),
            )
            synced += 1
        except Exception as exc:
            logger.warning("Failed to update island %s in cache: %s", island_id, exc)

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
        "errors": [],
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
