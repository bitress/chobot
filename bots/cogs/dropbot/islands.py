"""Island-name helpers and access checks (blocklist, sub/order rules)."""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
import discord
import logging
import os
import re
import time
import urllib.parse
from utils.config import Config
from utils.database import connect_db

from .constants import ACCESS_CACHE_TTL, ENFORCE_BLOCKLIST, ORDER_RULES_ROLE_ID, REQUIRE_REGISTERED_CHARACTER, SUB_RULES_MSG, SUB_RULES_ROLE_ID, WEBSITE_BASE
from .state import _ACCESS_CACHE
from .db import _panel_channel_ids_cached

if TYPE_CHECKING:
    from .constants import Denied

logger = logging.getLogger("DropBotCog")


# ─── Island name helpers ───────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase, strip emoji/punctuation, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_ISLAND_MAP: dict[str, str] = {}


_ISLAND_MAP_KEY: tuple[str, ...] = ()


def _island_map() -> dict[str, str]:
    """{normalised name: configured spelling}, rebuilt only when Config.SUB_ISLANDS changes."""
    global _ISLAND_MAP, _ISLAND_MAP_KEY
    key = tuple(Config.SUB_ISLANDS)
    if key != _ISLAND_MAP_KEY:
        _ISLAND_MAP = {_norm(i): i for i in key if _norm(i)}
        _ISLAND_MAP_KEY = key
    return _ISLAND_MAP


def _canonical_island(name: str | None) -> str | None:
    """Return the configured SUB_ISLANDS spelling for `name`, or None if it isn't a sub island."""
    key = _norm(name or "")
    return _island_map().get(key) if key else None


def _island_url(island: str) -> str:
    return f"{WEBSITE_BASE}/island/{urllib.parse.quote(island.lower(), safe='')}"


def _interactive_map_url(island: str) -> str:
    return f"{WEBSITE_BASE}/interactive-map/{urllib.parse.quote(island.lower(), safe='')}"


def _infer_island_from_channel(channel) -> str | None:
    """
    Resolve island name from a Discord channel object (blocking: touches the DB).
    Priority:
      1. island_dropbot_panels DB lookup
      2. islands table lookup by channel_id
      3. Whole-word match of the channel name against Config.SUB_ISLANDS
    Only ever returns a name from SUB_ISLANDS.
    """
    if channel is None:
        return None
    if isinstance(channel, discord.Thread):
        channel = channel.parent
    if channel is None or not isinstance(channel, discord.abc.GuildChannel):
        return None  # DMs and anything we can't map

    channel_id = str(channel.id)

    for query in (
        "SELECT island_name AS n FROM island_dropbot_panels WHERE channel_id = ?",
        "SELECT name AS n FROM islands WHERE channel_id = ?",
    ):
        try:
            with connect_db() as conn:
                row = conn.execute(query, (channel_id,)).fetchone()
            if row:
                canonical = _canonical_island(row["n"])
                if canonical:
                    return canonical
        except Exception:
            pass

    name_norm = _norm(getattr(channel, "name", "") or "")
    if not name_norm:
        return None
    padded = f" {name_norm} "
    best: str | None = None
    for inorm, island in _island_map().items():
        if f" {inorm} " in padded and (best is None or len(inorm) > len(_norm(best))):
            best = island
    return best


async def _infer_island(channel) -> str | None:
    return await asyncio.to_thread(_infer_island_from_channel, channel)


def _sub_category_id() -> int | None:
    """Support both names; the spec says SUB_CATEGORY_ID, older code used CATEGORY_ID."""
    for attr in ("SUB_CATEGORY_ID", "CATEGORY_ID"):
        val = getattr(Config, attr, None) or os.getenv(attr)
        if val:
            try:
                return int(val)
            except (TypeError, ValueError):
                logger.warning("[DropBot] %s is not a valid channel id: %r", attr, val)
    return None


async def _user_can_target(interaction: discord.Interaction, island: str) -> bool:
    """
    May this user act on `island` from where they are? Yes if they're staff, are standing in that
    island's channel, or can see any channel that hosts that island's panel.
    """
    if interaction.permissions.manage_messages:
        return True
    if await _infer_island(interaction.channel) == island:
        return True
    guild, member = interaction.guild, interaction.user
    if not guild or not isinstance(member, discord.Member):
        return False
    for cid in await asyncio.to_thread(_panel_channel_ids_cached, island):
        ch = guild.get_channel(cid)
        if ch and ch.permissions_for(member).view_channel:
            return True
    return False


# ─── Access checks ─────────────────────────────────────────────────────────────

def _has_registered_character(discord_user_id: int) -> bool:
    """Check if Discord user has a registered character linked via unified account."""
    try:
        with connect_db() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM user_saved_characters sc
                JOIN user_provider_links pl ON pl.unified_user_id = sc.user_id
                WHERE pl.provider = 'discord' AND pl.provider_user_id = ?
                LIMIT 1
                """,
                (str(discord_user_id),),
            ).fetchone()
            return bool(row)
    except Exception:
        return True  # Fail open on database error


def _check_access(discord_user_id: int) -> str | None:
    """Return a message if this user must not use the panel, else None. Fails open on DB errors."""
    uid = str(discord_user_id)

    if ENFORCE_BLOCKLIST:
        try:
            # Only an *active* ban blocks (pardons and undos deactivate it). Discord-button
            # bans are synced into user_moderation_status by the flight logger.
            with connect_db() as conn:
                row = conn.execute(
                    "SELECT 1 FROM user_moderation_status WHERE (discord_id = ? OR user_id = ?) "
                    "AND action_type = 'BAN' AND is_active = 1 LIMIT 1",
                    (uid, uid),
                ).fetchone()
            if row:
                return (
                    "You can't use island controls right now. "
                    "If you think this is a mistake, contact a moderator."
                )
        except Exception as exc:
            logger.error("[DropBot] ALERT: Blocklist check failed (allowing). Verify the warnings schema: %s", exc, exc_info=True)

    if REQUIRE_REGISTERED_CHARACTER and not _has_registered_character(discord_user_id):
        return (
            "Register an in-game character first (IGN and island name) at "
            f"{WEBSITE_BASE}/profile, then try again."
        )

    return None


async def _denied_reason(discord_user_id: int) -> str | None:
    """Cached for ACCESS_CACHE_TTL so a click doesn't cost two queries every time."""
    now = time.monotonic()
    hit = _ACCESS_CACHE.get(discord_user_id)
    if hit and now - hit[0] < ACCESS_CACHE_TTL:
        return hit[1]
    result = await asyncio.to_thread(_check_access, discord_user_id)
    if len(_ACCESS_CACHE) > 2000:
        for k in [k for k, (t, _) in _ACCESS_CACHE.items() if now - t >= ACCESS_CACHE_TTL]:
            del _ACCESS_CACHE[k]
    _ACCESS_CACHE[discord_user_id] = (now, result)
    return result


def _has_sub_role(user: discord.abc.User) -> bool:
    """True if the user is a guild member holding sub-rules role 1077997850165772398."""
    if isinstance(user, discord.Member):
        roles = getattr(user, "roles", []) or []
        if any(getattr(r, "id", None) == SUB_RULES_ROLE_ID for r in roles):
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms and getattr(perms, "administrator", None) is True:
            return True
        return False
    guild = getattr(user, "guild", None)
    if guild and hasattr(guild, "get_member"):
        m = guild.get_member(user.id)
        if m:
            roles = getattr(m, "roles", []) or []
            if any(getattr(r, "id", None) == SUB_RULES_ROLE_ID for r in roles):
                return True
            perms = getattr(m, "guild_permissions", None)
            if perms and getattr(perms, "administrator", None) is True:
                return True
    return False


def _has_order_role(user: discord.abc.User) -> bool:
    """True if the user is a guild member holding the order-rules role or accepted in DB."""
    if isinstance(user, discord.Member) and any(getattr(r, "id", None) == ORDER_RULES_ROLE_ID for r in getattr(user, "roles", [])):
        return True
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_order_rules_accepted WHERE user_id = ? LIMIT 1",
                (str(getattr(user, "id", "")),),
            ).fetchone()
            if row:
                return True
    except Exception:
        pass
    return False


async def _access_denied(user: discord.abc.User, *, require_order_rules: bool = False) -> Denied | str | None:
    """Sub rules role check first (cheap, no DB), then the blocklist/registration checks.
    Order rules are not required for DropBot; only sub rules are enforced.
    """
    if not _has_sub_role(user):
        return SUB_RULES_MSG
    return await _denied_reason(user.id)
