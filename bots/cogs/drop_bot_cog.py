"""
DropBot Cog — Integrated island control panels for Discord.

Provides persistent, interaction-only UI panels in sub-island channels:
  • 🦤 Get Dodo        – fetches the live Dodo from SysBot, sends it ephemerally, logs to webhook
  • 📦 Drop Items      – modal for custom item/hex code drops (validated)
  • 🔍 Search Catalog  – browse the ACNH catalog, build a drop cart, inject villagers
  • 🐱 Inject Villager – modal with fuzzy villager matching and a confirm step
  • 📖 Guide           – ephemeral how-to for the island panel
  • 🌐 Island Page     – link button to the island's page on chopaeng.com

The panel embed auto-refreshes every 30s (status, clock, villagers and visitors).

Admin slash commands (under /dropbot group, Manage Messages required):
  • /dropbot setup-panel [island]  – post/refresh panel in current channel
  • /dropbot setup-all             – bulk-deploy panels under the sub-island category
  • /dropbot status                – list all registered panels
Anyone:
  • /catalog and /dropbot catalog  – catalog search (the island option is restricted to
                                      islands whose channel the user can see)

Requires ZERO privileged gateway intents.

Persistence: every panel button is a discord.ui.DynamicItem whose custom_id
carries the island (``dropbot:<action>:<island>``). The item classes are
registered once in cog_load(), so buttons keep working after a restart without
re-adding a view per message. Requires discord.py >= 2.4.
"""

from __future__ import annotations

import requests
import asyncio
import contextlib
import difflib
import json
import logging
import math
import os
import re
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.acnh_catalog import ACNHCatalog, CatalogItem, CatalogVillager
from utils.config import Config
from utils.database import connect_db
from utils.sysbot_api import _INVALID_DODO_CODES, get_island_sysbot_client

logger = logging.getLogger("DropBotCog")

# ─── Constants ────────────────────────────────────────────────────────────────

PANEL_EMBED_COLOR      = 0x57F287   # Discord green
PANEL_EMBED_COLOR_WARN = 0xFEE75C   # Yellow
PANEL_EMBED_COLOR_ERR  = 0xED4245   # Red
PANEL_FOOTER_TEXT = "Chopaeng Camp • Sub-Island DropBot"
PANEL_FOOTER_ICON = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"
DODO_GIF  = "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"
LOGO_URL  = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"
MAP_CDN_BASE = "https://cdn.chopaeng.com/maps"

# Island pages live at <WEBSITE_BASE>/island/<island_name>
WEBSITE_BASE = "https://www.chopaeng.com"

# Limits (enforced for custom item drops)
MAX_DROP_SLOTS = 9      # different items per drop, matches the site's 9-slot Drop Bot
MAX_STACK      = 30     # highest quantity per item
MAX_ITEM_NAME  = 40

# Villager plots: 1 through 10 (matching in-game plots and API backend).
PLOT_MIN = 1
PLOT_MAX = 10

# Sanrio / Amiibo villager injection
SANRIO_EMBED_COLOR = 0xF06292
SANRIO_VILLAGERS: list[tuple[str, str, str]] = [
    ("Chai", "Cinnamoroll • Peppy Elephant", "☕"),
    ("Chelsea", "My Melody • Sweet Deer", "🌸"),
    ("Étoile", "Little Twin Stars • Normal Sheep", "⭐"),
    ("Marty", "Pompompurin • Lazy Bear Cub", "🍮"),
    ("Rilla", "Hello Kitty • Peppy Gorilla", "🎀"),
    ("Toby", "Kerokerokeroppi • Smug Rabbit", "🍵"),
]
SANRIO_NAMES_MAP: dict[str, str] = {
    "chai": "Chai",
    "chelsea": "Chelsea",
    "etoile": "Étoile",
    "étoile": "Étoile",
    "marty": "Marty",
    "rilla": "Rilla",
    "toby": "Toby",
}
PLACEHOLDER_VILLAGERS: list[tuple[str, str, str]] = [
    ("Raymond", "Smug • Cat (Recommended)", "🐱"),
    ("Marshal", "Smug • Squirrel", "🐿️"),
    ("Shino", "Peppy • Deer", "🦌"),
    ("Sasha", "Lazy • Rabbit", "🐰"),
    ("Judy", "Snooty • Bear cub", "🧸"),
    ("Ankha", "Snooty • Cat", "🐱"),
    ("Lucky", "Lazy • Dog", "🐶"),
]


def is_sanrio_villager(name: str) -> bool:
    """True if name matches one of the Sanrio amiibo characters."""
    return (name or "").strip().lower() in SANRIO_NAMES_MAP


def get_canonical_sanrio_name(name: str) -> str:
    """Returns canonical name for Sanrio character (e.g. 'etoile' -> 'Étoile')."""
    return SANRIO_NAMES_MAP.get((name or "").strip().lower(), (name or "").strip().title())

# Access gates (see _check_access)
ENFORCE_BLOCKLIST = True
# Flip to True once _has_registered_character() is implemented against the real table.
REQUIRE_REGISTERED_CHARACTER = False
SUB_RULES_ROLE_ID = getattr(Config, "SUB_RULES_ROLE_ID", 1077997850165772398) or 1077997850165772398
SUB_RULES_MSG = (
    "You need to **accept the sub rules** first before you can use the islands (role <@&1077997850165772398> required).\n"
    "Please go to <#783677194576330792>, review and accept the rules, then come back and try again."
)
ORDER_RULES_ROLE_ID = getattr(Config, "ORDER_RULES_ROLE_ID", 1176191811438510231) or 1176191811438510231
ORDER_RULES_MSG = (
    "You need to **accept the Order Bot rules** first before you can drop items or inject villagers. "
    "Use `/rules` or review and accept them on the website, then come back and try again."
)

# Timings
ACTION_COOLDOWN       = 10.0   # seconds between drops / injections per user+island
ACCESS_CACHE_TTL      = 30.0   # seconds a blocklist verdict is cached
WEBHOOK_DEBOUNCE_TTL  = 3.0    # seconds, per (user, island)
FORCE_EDIT_SECONDS    = 300.0  # re-edit an unchanged panel at least this often (keeps timestamp fresh)
REFRESH_CONCURRENCY   = 4      # parallel panel refreshes
CATALOG_ITEM_LIMIT    = 100

UNKNOWN_ISLAND_MSG = (
    "This panel points to an island I don't recognise. "
    "Ask a moderator to run `/dropbot setup-panel` in this channel."
)

_PLACEHOLDER_DODOS = {"00000", "-----", "GETTIN'", "NONE", "NULL"}

# Background tasks need a strong reference or they can be garbage-collected mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# ─── Small in-memory guards ────────────────────────────────────────────────────

_WEBHOOK_DEBOUNCE: dict[str, float] = {}
_COOLDOWNS: dict[tuple[int, str, str], float] = {}
_ACCESS_CACHE: dict[int, tuple[float, str | None]] = {}


def _debounced(key: str) -> bool:
    """True if `key` fired within the TTL. Prunes expired entries so the dict can't grow forever."""
    now = time.monotonic()
    for k in [k for k, t in _WEBHOOK_DEBOUNCE.items() if now - t > WEBHOOK_DEBOUNCE_TTL]:
        del _WEBHOOK_DEBOUNCE[k]
    if key in _WEBHOOK_DEBOUNCE:
        return True
    _WEBHOOK_DEBOUNCE[key] = now
    return False


def _cooldown_left(user_id: int, island: str, action: str, seconds: float = ACTION_COOLDOWN) -> float:
    """Seconds the user still has to wait; 0.0 (and the cooldown is armed) if they may proceed."""
    now = time.monotonic()
    for k in [k for k, t in _COOLDOWNS.items() if now - t > 60]:
        del _COOLDOWNS[k]
    key = (user_id, island, action)
    last = _COOLDOWNS.get(key)
    if last is not None and now - last < seconds:
        return seconds - (now - last)
    _COOLDOWNS[key] = now
    return 0.0


def _cooldown_eta_ts(wait: float) -> str:
    """Return a Discord-native relative timestamp that ticks down on its own."""
    return f"<t:{int(time.time() + wait)}:R>"


# ─── Shared HTTP session ───────────────────────────────────────────────────────

_HTTP_SESSION: aiohttp.ClientSession | None = None


def _http() -> aiohttp.ClientSession:
    global _HTTP_SESSION
    if _HTTP_SESSION is None or _HTTP_SESSION.closed:
        _HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
    return _HTTP_SESSION


async def _close_http() -> None:
    global _HTTP_SESSION
    if _HTTP_SESSION is not None and not _HTTP_SESSION.closed:
        await _HTTP_SESSION.close()
    _HTTP_SESSION = None


# ─── Database helpers (all synchronous; call through asyncio.to_thread) ────────

def _init_tables() -> None:
    """Ensure the panel table and the audit-log tables exist."""
    try:
        with connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS island_dropbot_panels (
                    channel_id  VARCHAR(64) PRIMARY KEY,
                    island_name VARCHAR(128) NOT NULL,
                    message_id  VARCHAR(64),
                    updated_at  VARCHAR(64)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS villager_injections_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    island_name TEXT,
                    villager_name TEXT,
                    plot_number INTEGER,
                    requested_by_user_id TEXT,
                    status TEXT,
                    response_message TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS island_drops_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    island_name TEXT,
                    items TEXT,
                    requested_by_user_id TEXT,
                    status TEXT,
                    response_message TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Sanrio wizard resume state
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sanrio_wizard_state (
                    user_id TEXT NOT NULL,
                    island_name TEXT NOT NULL,
                    target_villager TEXT NOT NULL,
                    placeholder_villager TEXT NOT NULL,
                    step1_done INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, island_name)
                )
                """
            )
            conn.commit()
    except Exception as exc:
        logger.error("[DropBot] Failed to init tables: %s", exc)


def _upsert_panel(channel_id: int, island_name: str, message_id: int) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO island_dropbot_panels (channel_id, island_name, message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    island_name=excluded.island_name,
                    message_id=excluded.message_id,
                    updated_at=excluded.updated_at
                """,
                (str(channel_id), island_name, str(message_id), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception as exc:
        logger.error("[DropBot] Failed to upsert panel: %s", exc)


def _get_panel_row(channel_id: int) -> dict | None:
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT island_name, message_id FROM island_dropbot_panels WHERE channel_id = ?",
                (str(channel_id),),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def _load_all_panels() -> list[dict]:
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT channel_id, island_name, message_id FROM island_dropbot_panels"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _panel_channel_ids(island: str) -> list[int]:
    """Channel ids that host a panel for `island`."""
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT channel_id FROM island_dropbot_panels WHERE island_name = ?", (island,)
            ).fetchall()
        return [int(r["channel_id"]) for r in rows]
    except Exception:
        return []


def _log_drop(island: str, user_id: int, items: str, ok: bool, message: str) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO island_drops_log (island_name, items, requested_by_user_id, status, response_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (island, items[:500], str(user_id), "queued" if ok else "failed", message[:500]),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Failed to write drop audit log: %s", exc)


def _upsert_sanrio_state(
    user_id: int, island: str, target: str, placeholder: str, step1_done: bool
) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO sanrio_wizard_state
                    (user_id, island_name, target_villager, placeholder_villager, step1_done, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, island_name) DO UPDATE SET
                    target_villager=excluded.target_villager,
                    placeholder_villager=excluded.placeholder_villager,
                    step1_done=excluded.step1_done,
                    updated_at=excluded.updated_at
                """,
                (
                    str(user_id), island, target, placeholder,
                    1 if step1_done else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Could not save sanrio wizard state: %s", exc)


def _get_sanrio_state(user_id: int, island: str) -> dict | None:
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT * FROM sanrio_wizard_state WHERE user_id=? AND island_name=?",
                (str(user_id), island),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def _clear_sanrio_state(user_id: int, island: str) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                "DELETE FROM sanrio_wizard_state WHERE user_id=? AND island_name=?",
                (str(user_id), island),
            )
            conn.commit()
    except Exception:
        pass


def _log_injection(island: str, villager: str, plot: int | None, user_id: int, ok: bool, message: str) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO villager_injections_log
                (island_name, villager_name, plot_number, requested_by_user_id, status, response_message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (island, villager, plot if plot is not None else 0, str(user_id),
                 "queued" if ok else "failed", message[:500]),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Failed to write injection audit log: %s", exc)


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
    for cid in await asyncio.to_thread(_panel_channel_ids, island):
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
            with connect_db() as conn:
                row = conn.execute(
                    "SELECT 1 FROM warnings WHERE (user_id = ? OR user_id = ?) AND action_type = 'BAN' LIMIT 1",
                    (uid, int(discord_user_id)),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT 1 FROM user_moderation_status WHERE discord_id = ? AND action_type = 'BAN' AND is_active = 1 LIMIT 1",
                        (uid,),
                    ).fetchone()
            if row:
                return (
                    "You can't use island controls right now. "
                    "If you think this is a mistake, contact a moderator."
                )
        except Exception:
            logger.exception("[DropBot] Blocklist check failed (allowing). Verify the warnings schema.")

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


async def _access_denied(user: discord.abc.User, *, require_order_rules: bool = False) -> str | None:
    """Role check first (cheap, no DB), then order rules if required, then the blocklist/registration checks."""
    if not _has_sub_role(user):
        return SUB_RULES_MSG
    if require_order_rules and not _has_order_role(user):
        return ORDER_RULES_MSG
    return await _denied_reason(user.id)


# ─── Drop input parsing ────────────────────────────────────────────────────────

_ITEM_NAME_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9 '\-\.\(\)&]{{0,{MAX_ITEM_NAME - 1}}}$")
_ENTRY_RE = re.compile(r"^(?P<item>.+?)(?:\s*[x×]\s*(?P<qx>[0-9]+)|\s+(?P<q>[0-9]+))?$", re.IGNORECASE)


def parse_drop_items(raw: str) -> tuple[list[tuple[str, int]] | None, str | None]:
    """
    Parse "Gold nugget 30, 09C9 x10, Royal Crown" into [(item, qty), ...].
    Returns (entries, None) or (None, user-facing error).
    Item names are checked for length and characters only; SysBot remains the authority on names/hex.
    """
    entries = [e.strip() for e in re.split(r"[,\n;]+", raw or "") if e.strip()]
    if not entries:
        return None, "Enter at least one item."
    if len(entries) > MAX_DROP_SLOTS:
        return None, (
            f"You can drop at most **{MAX_DROP_SLOTS}** different items at once "
            f"(you entered {len(entries)})."
        )

    parsed: list[tuple[str, int]] = []
    for entry in entries:
        m = _ENTRY_RE.match(entry)
        if not m:
            return None, f"I couldn't read “{entry[:40]}”. Use `name quantity`, for example `Wood 30`."
        item = m.group("item").strip()
        qty = int(m.group("qx") or m.group("q") or 1)
        if not _ITEM_NAME_RE.match(item):
            return None, (
                f"“{item[:40]}” isn't a valid item name or code. "
                f"Use letters, numbers and spaces only (up to {MAX_ITEM_NAME} characters)."
            )
        if not 1 <= qty <= MAX_STACK:
            return None, f"Quantity for **{item}** must be between 1 and {MAX_STACK}."
        parsed.append((item, qty))
    return parsed, None


def _format_item_for_sysbot(item_name: str, qty: int) -> str:
    """Format an item for SysBot/NHSE using hex codes.
    Repeats the hex code for quantity, space-separated with NO commas.
    """
    clean = item_name.strip()
    try:
        from utils.data_manager import resolve_drop_items
        hex_str, _ = resolve_drop_items(f"{clean} {qty}" if qty > 1 else clean)
        if hex_str:
            return hex_str
    except Exception:
        pass

    try:
        from utils.sysbot_api import sanitize_single_drop_item
        sanitized = sanitize_single_drop_item(clean)
        if sanitized:
            return " ".join(sanitized * qty)
    except Exception:
        pass

    return " ".join([clean] * qty)


def _normalize_items(entries: list[tuple[str, int]]) -> str:
    """Normalize drop items into space-separated hex codes with NO COMMAS."""
    try:
        from utils.data_manager import resolve_drop_items
        formatted_entries = []
        for item, qty in entries:
            if qty > 1:
                formatted_entries.append(f"{item} {qty}")
            else:
                formatted_entries.append(item)
        hex_str, _ = resolve_drop_items(formatted_entries)
        if hex_str:
            return hex_str
    except Exception:
        pass
    return " ".join(_format_item_for_sysbot(item, qty) for item, qty in entries)


# ─── Villager matching ─────────────────────────────────────────────────────────

_VILLAGER_NAMES: list[str] = []
_VILLAGER_NAMES_LOADED = False


def _get_villager_names() -> list[str]:
    global _VILLAGER_NAMES, _VILLAGER_NAMES_LOADED
    if _VILLAGER_NAMES_LOADED:
        return _VILLAGER_NAMES
    try:
        # villagers.json is in the project root
        vpath = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "villagers.json",
        )
        with open(vpath, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _VILLAGER_NAMES = sorted(
                (v.get("name") or v.get("Name") or k.title())
                for k, v in data.items()
                if isinstance(v, dict)
            ) or sorted(k.title() for k in data)
        elif isinstance(data, list):
            _VILLAGER_NAMES = sorted(
                v.get("name") or v.get("Name") or ""
                for v in data
                if isinstance(v, dict)
            )
        _VILLAGER_NAMES = [n for n in _VILLAGER_NAMES if n]
        _VILLAGER_NAMES_LOADED = True
    except Exception as exc:
        logger.warning("[DropBot] Could not load villagers.json: %s", exc)
        _VILLAGER_NAMES = []
    return _VILLAGER_NAMES


def _match_villagers(query: str, limit: int = 5) -> tuple[str | None, list[str]]:
    """
    Returns (exact, candidates).
      exact      – the villager name if `query` matches one case-insensitively, else None
      candidates – up to `limit` plausible names (prefix, substring, then close spellings)
    """
    q = (query or "").strip()
    if not q:
        return None, []

    # ACNHCatalog is the primary source (all official villagers)
    try:
        catalog = ACNHCatalog.get_instance()
        exact_v = catalog.get_villager(q)
        if exact_v:
            return exact_v.name, []

        hits = catalog.search_villagers(q, limit=limit)
        if hits:
            return None, [h.name for h in hits]
    except Exception as exc:
        logger.debug("[DropBot] ACNHCatalog search in _match_villagers failed: %s", exc)

    # Fallback to local villagers.json
    names = _get_villager_names()
    if not names:
        return None, []

    lower_map = {n.lower(): n for n in names}
    if q.lower() in lower_map:
        return lower_map[q.lower()], []

    pool: list[str] = [n for n in names if n.lower().startswith(q.lower())]
    pool += [n for n in names if q.lower() in n.lower() and n not in pool]
    for close in difflib.get_close_matches(q.lower(), list(lower_map), n=limit, cutoff=0.6):
        name = lower_map[close]
        if name not in pool:
            pool.append(name)
    return None, pool[:limit]


def _search_catalog(query: str) -> tuple[list[CatalogItem], list[CatalogVillager]]:
    """Blocking catalog search; run in a thread."""
    catalog = ACNHCatalog.get_instance()
    return (
        catalog.search_items(query, limit=CATALOG_ITEM_LIMIT),
        catalog.search_villagers(query, limit=10),
    )


# ─── Dodo audit webhook ────────────────────────────────────────────────────────

def _dodo_webhook_url() -> str:
    """Check all possible webhook URLs for Dodo logging."""
    for name in ("DODO_WEBHOOK_URL", "DODO_LOG_WEBHOOK_URL", "SUB_FLIGHT_WEBHOOK_URL"):
        val = getattr(Config, name, "") or os.getenv(name, "")
        if val:
            return val
    return ""


def _persist_dodo_reveal_message(
    message_url: str | None,
    user_id: str,
    island_name: str,
    channel_id: str,
    username: str,
    nickname: str,
) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dodo_reveal_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    island_clean TEXT,
                    channel_id TEXT,
                    message_url TEXT,
                    username TEXT,
                    nickname TEXT,
                    created_at INTEGER
                )
                """
            )
            clean = re.sub(r"[^a-zA-Z0-9]", "", island_name.lower())
            conn.execute(
                """
                INSERT INTO dodo_reveal_messages
                (user_id, island_clean, channel_id, message_url, username, nickname, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(user_id),
                    clean,
                    str(channel_id) if channel_id else None,
                    message_url,
                    username or "",
                    nickname or "",
                    int(time.time()),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("[DropBot] Could not persist dodo_reveal_message: %s", exc)


def _lookup_saved_character(user_id: str) -> tuple[str, str] | None:
    try:
        with connect_db() as conn:
            row = conn.execute(
                """
                SELECT ign, island_name FROM user_saved_characters
                WHERE user_id = ? OR user_id = ? OR user_id = ?
                ORDER BY is_default DESC, updated_at DESC LIMIT 1
                """,
                (str(user_id), f"dc_{user_id}", f"cp_usr_{user_id}"),
            ).fetchone()
            if row and row["ign"]:
                return row["ign"], row["island_name"] or ""
    except Exception as exc:
        logger.debug("[DropBot] Saved character lookup note: %s", exc)
    return None


async def _fire_dropbot_dodo_webhook(
    user_id: str,
    username: str,
    display_name: str,
    island_name: str,
    channel_id: str,
) -> None:
    """POST a Dodo reveal audit embed matching Chopaeng's standard format. Never raises."""
    try:
        url = _dodo_webhook_url()
        if not url:
            logger.warning("[DropBot] No webhook URL configured for Dodo reveals!")
            return
        if _debounced(f"{user_id}:{island_name}"):
            return

        fields = [
            {"name": "Member", "value": f"{display_name} (<@{user_id}>)"},
            {
                "name": "Island",
                "value": (f"<#{channel_id}>\n" if channel_id else "") + f"[View Island]({_island_url(island_name)})",
            },
        ]
        char = await asyncio.to_thread(_lookup_saved_character, user_id)
        if char:
            ign, char_isl = char
            fields.append({
                "name": "In-Game Character",
                "value": f"IGN: **{ign}**" + (f" | Island: **{char_isl}**" if char_isl else ""),
            })

        embed = {
            "title": "Dodo Code Revealed",
            "color": 0x2ECC71,
            "description": (
                f"<@{user_id}> has revealed the Dodo code for island <#{channel_id}>"
                if channel_id else f"<@{user_id}> has revealed the Dodo code for **{island_name}**"
            ),
            "fields": fields,
            "image": {"url": DODO_GIF},
            "footer": {"text": "Chopaeng Camp - Dodo Log", "icon_url": LOGO_URL},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        sep = "&" if "?" in url else "?"
        async with _http().post(
            f"{url}{sep}wait=true",
            json={"embeds": [embed]},
            headers={"User-Agent": "ChoBot/2.0"},
        ) as resp:
            if resp.status >= 400:
                logger.warning("[DropBot] Dodo webhook returned HTTP %s", resp.status)
                return
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}

        msg_id = (data or {}).get("id")
        # The webhook message lives in the *log* channel, so use the channel id Discord returns.
        log_channel = (data or {}).get("channel_id") or channel_id
        msg_url = (
            f"https://discord.com/channels/{getattr(Config, 'GUILD_ID', '')}/{log_channel}/{msg_id}"
            if msg_id else None
        )
        await asyncio.to_thread(
            _persist_dodo_reveal_message, msg_url, str(user_id), island_name, channel_id, username, display_name
        )
        logger.info("[DropBot] Dodo reveal webhook sent for %s by user %s", island_name, user_id)
    except Exception as exc:
        logger.warning("[DropBot] Dodo webhook failed: %s", exc)


# ─── Live island status ────────────────────────────────────────────────────────

_CONSOLE_API_CACHE: tuple[float, list[dict]] = (0.0, [])
_CONSOLE_API_LOCK: asyncio.Lock | None = None


async def _get_console_api_islands() -> list[dict]:
    global _CONSOLE_API_CACHE, _CONSOLE_API_LOCK
    if _CONSOLE_API_LOCK is None:
        _CONSOLE_API_LOCK = asyncio.Lock()

    def fresh() -> bool:
        return bool(_CONSOLE_API_CACHE[1]) and (time.monotonic() - _CONSOLE_API_CACHE[0]) < 15.0

    if fresh():
        return _CONSOLE_API_CACHE[1]

    async with _CONSOLE_API_LOCK:   # parallel panel refreshes share one fetch
        if fresh():
            return _CONSOLE_API_CACHE[1]
        url = getattr(Config, "ISLANDS_API_URL", "") or "https://console.chopaeng.com/api/islands"
        items: list[dict] = []
        try:
            async with _http().get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        except Exception as exc:
            logger.debug("[DropBot] Could not fetch console islands API: %s", exc)
        if items:
            _CONSOLE_API_CACHE = (time.monotonic(), items)
            return items
        return _CONSOLE_API_CACHE[1]   # stale data beats none


def _clean_key(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", str(text or "")).lower()


async def _safe_call(coro, timeout: float, label: str):
    """Await `coro` with a timeout; log and return None on any failure."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception as exc:
        logger.debug("[DropBot] %s failed: %s", label, exc)
        return None


async def fetch_island_live_status(island_name: str) -> dict:
    """Fetch live bot state, villagers, in-game clock, and visitors for an island."""
    canonical = _canonical_island(island_name) or island_name
    canonical_key = _clean_key(canonical)

    # 1. Console API snapshot (same source as discord_command_bot status)
    api_item = None
    try:
        for it in await _get_console_api_islands():
            if isinstance(it, dict) and _clean_key(it.get("name")) == canonical_key:
                api_item = it
                break
    except Exception as exc:
        logger.debug("[DropBot] Error finding island in console API: %s", exc)

    # 2. Telemetry cache
    telem = None
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry
        telem = get_cached_island_telemetry(canonical, mask_sub_dodo=False)
    except Exception as exc:
        logger.debug("[DropBot] telemetry cache unavailable for %s: %s", canonical, exc)

    # 3. SysBot client: status, clock and villagers fetched in parallel
    client = None
    try:
        client = get_island_sysbot_client(canonical, timeout=2)
    except Exception as exc:
        logger.debug("[DropBot] no SysBot client for %s: %s", canonical, exc)

    raw_status = raw_time = raw_vill = None
    if client:
        raw_status, raw_time, raw_vill = await asyncio.gather(
            _safe_call(client.get_bot_status(), 4, f"get_bot_status[{canonical}]"),
            _safe_call(client.get_game_time(), 4, f"get_game_time[{canonical}]"),
            _safe_call(client.get_villagers(), 4, f"get_villagers[{canonical}]"),
        )
    client_status: dict = raw_status if isinstance(raw_status, dict) else {}

    # Online?
    is_running = False
    if client_status.get("is_running") and client_status.get("is_connected"):
        is_running = True
    elif api_item and (api_item.get("bot_online") or api_item.get("discord_bot_online")):
        is_running = True
    elif telem and telem.get("is_online"):
        is_running = True

    # Console stage
    console_stage = "Running" if is_running else "Offline"
    if client_status.get("console_stage") and client_status.get("console_stage") not in ("Stopped", "Offline", "Unknown"):
        console_stage = client_status["console_stage"]
    elif api_item:
        stage = api_item.get("console_stage")
        if stage and stage not in ("Offline", "Stopped"):
            console_stage = stage
        elif is_running:
            console_stage = api_item.get("status") or "Online"
    elif telem and telem.get("console_stage"):
        console_stage = telem["console_stage"]

    # Dodo validity: only a real code counts (being online alone is not enough)
    dodo = (
        client_status.get("dodo_code")
        or (telem.get("raw_dodo") if telem else None)
        or (api_item.get("dodo_code") if api_item else None)
    )
    is_valid_dodo = bool(client_status.get("is_dodo_valid") or _clean_dodo(dodo))

    # Clock & mode
    game_time = client_status.get("game_time")
    is_nooks_open = client_status.get("is_nooks_open")
    mode = client_status.get("mode")
    if not game_time and api_item:
        game_time = api_item.get("game_time")
        is_nooks_open = api_item.get("is_nooks_open")
        mode = api_item.get("mode")
    if not game_time and isinstance(raw_time, dict) and raw_time.get("success"):
        game_time = raw_time.get("formatted") or raw_time.get("game_time")
        is_nooks_open = raw_time.get("is_nooks_open")
        mode = raw_time.get("mode")

    # Visitors
    visitors: list[str] = []
    if client_status:
        try:
            from utils.sysbot_telemetry import parse_sysbot_visitors
            _, v_names = parse_sysbot_visitors(client_status)
            if v_names:
                visitors = list(v_names)
        except Exception as exc:
            logger.debug("[DropBot] visitor parse failed for %s: %s", canonical, exc)
    if not visitors and api_item:
        v_raw = api_item.get("visitor_list")
        if isinstance(v_raw, list):
            visitors = [str(x) for x in v_raw if str(x).strip()]
        elif isinstance(api_item.get("visitors"), list):
            visitors = [str(x) for x in api_item["visitors"]]
    if not visitors and telem and telem.get("visitor_list"):
        visitors = list(telem["visitor_list"])

    # Villagers
    villagers: list[str] = []
    if isinstance(raw_vill, dict):
        villagers = [
            (v.get("name") or v.get("Name") or str(v)) if isinstance(v, dict) else str(v)
            for v in raw_vill.get("villagers", [])
        ]
    if not villagers and api_item:
        raw_v = api_item.get("villagers") or api_item.get("residents") or []
        if isinstance(raw_v, list):
            villagers = [str(v) for v in raw_v]

    return {
        "island_name": canonical,
        "is_running": is_running,
        "is_dodo_valid": is_valid_dodo,
        "dodo_code": dodo,
        "console_stage": console_stage,
        "game_time": game_time,
        "is_nooks_open": is_nooks_open,
        "mode": mode,
        "villagers": villagers,
        "visitors": visitors,
    }


# ─── Dodo lookup (ordered sources, one validity check) ─────────────────────────

def _clean_dodo(value) -> str | None:
    s = str(value or "").strip()
    if not s or s in _INVALID_DODO_CODES or s.upper() in _PLACEHOLDER_DODOS:
        return None
    return s


async def _dodo_from_telemetry(island: str, client) -> Any:
    from utils.sysbot_telemetry import get_cached_island_telemetry, poll_single_island
    telem = get_cached_island_telemetry(island, mask_sub_dodo=False)
    if not telem or not (telem.get("raw_dodo") or telem.get("dodo_code")):
        telem = await asyncio.to_thread(poll_single_island, island)
    telem = telem or {}
    return telem.get("raw_dodo") or telem.get("dodo_code")


async def _dodo_from_status(island: str, client) -> Any:
    data = await client.get_bot_status()
    return data.get("dodo_code") or data.get("dodo")


async def _dodo_from_direct(island: str, client) -> Any:
    data = await client.get_dodo()
    return data.get("dodo_code") or data.get("dodo")


async def _dodo_from_snapshot(island: str, client) -> Any:
    return (await fetch_island_live_status(island)).get("dodo_code")


def _read_dodo_file(island: str) -> str | None:
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry
        telem = get_cached_island_telemetry(island, mask_sub_dodo=False)
        if telem and telem.get("raw_dodo"):
            return telem["raw_dodo"]
    except Exception:
        pass
    return None


async def _dodo_from_disk(island: str, client) -> Any:
    return await asyncio.to_thread(_read_dodo_file, island)


_DODO_SOURCES = (
    (_dodo_from_telemetry, 10),
    (_dodo_from_status, 5),
    (_dodo_from_direct, 5),
    (_dodo_from_snapshot, 8),
    (_dodo_from_disk, 3),
)


async def _find_dodo(island: str, client) -> str | None:
    for source, timeout in _DODO_SOURCES:
        try:
            code = _clean_dodo(await asyncio.wait_for(source(island, client), timeout))
        except Exception as exc:
            logger.debug("[DropBot] dodo source %s failed for %s: %s", source.__name__, island, exc)
            continue
        if code:
            return code
    return None


# ─── Embeds + reply helpers ────────────────────────────────────────────────────

def _bullets(names: list[str], cap: int = 10) -> str:
    if not names:
        return "*None*"
    lines = [f"• {n}" for n in names[:cap]]
    if len(names) > cap:
        lines.append(f"*…and {len(names) - cap} more*")
    return "\n".join(lines)[:1024]


def _map_url(island_name: str) -> str:
    """Return the CDN URL for an island's map image (.png, .jpg, or .jpeg)."""
    slug = re.sub(r"[^a-zA-Z0-9]", "", island_name.lower())

    for ext in (".png", ".jpg", ".jpeg"):
        url = f"{MAP_CDN_BASE.rstrip('/')}/{slug}{ext}"
        try:
            response = requests.head(url, timeout=3, allow_redirects=True)
            if response.status_code == 200:
                return url
        except requests.RequestException:
            continue

    return f"{MAP_CDN_BASE.rstrip('/')}/{slug}.jpg"


def _build_panel_embed(island_name: str, status_data: dict | None = None) -> discord.Embed:
    url = _island_url(island_name)

    if status_data:
        is_running    = status_data.get("is_running", False)
        is_valid_dodo = status_data.get("is_dodo_valid", False)
        villagers     = status_data.get("villagers", [])
        visitors      = status_data.get("visitors", [])
        game_time     = status_data.get("game_time")
        is_nooks_open = status_data.get("is_nooks_open")
        mode          = status_data.get("mode")

        # Embed colour reflects island state
        if not is_running:
            color = PANEL_EMBED_COLOR_ERR
        elif not is_valid_dodo:
            color = PANEL_EMBED_COLOR_WARN
        else:
            color = PANEL_EMBED_COLOR

        status_part = f"{'🟢 Online' if is_running else '🔴 Offline'}"
        dodo_part   = "✅ Dodo ready" if is_valid_dodo else "🔄 Refreshing…"
        clock_part  = f"🕒 {game_time}" if game_time else ""
        shop_part   = "🛒 Nook's Open" if is_nooks_open else ""
        mode_part   = f"`{mode}`" if mode else ""
        summary_parts = [p for p in [status_part, dodo_part, clock_part, shop_part, mode_part] if p]
        summary = " · ".join(summary_parts)
    else:
        is_running    = False
        is_valid_dodo = False
        villagers     = []
        visitors      = []
        color         = PANEL_EMBED_COLOR
        summary       = "🔄 Loading live data…"

    embed = discord.Embed(
        title=f"🏝️ {island_name}",
        url=url,
        description=f"{summary}\nNew here? Press **Guide**.",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Villagers and visitors are the only fields (compact layout)
    v_count = len(visitors)
    visitor_cap = status_data.get("visitor_cap") if status_data else None
    visitor_title = (
        f"🧳 Visitors ({v_count}/{visitor_cap})" if visitor_cap
        else f"🧳 Visitors ({v_count})"
    )
    embed.add_field(name=f"🏡 Villagers ({len(villagers)})", value=_bullets(villagers), inline=True)
    embed.add_field(name=visitor_title, value=_bullets(visitors), inline=True)

    # Sticky map image at the bottom
    embed.set_image(url=_map_url(island_name))
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


def _embed_signature(embed: discord.Embed) -> str:
    """Stable fingerprint of an embed ignoring its timestamp, to skip no-op edits."""
    d = embed.to_dict()
    d.pop("timestamp", None)
    return json.dumps(d, sort_keys=True)


def _build_guide_embed(island: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📖 DropBot Guide — {island}",
        description=(
            "Everything on this panel is **private to you** — results only show up for you.\n"
            f"Island page: [{island} on chopaeng.com]({_island_url(island)})"
        ),
        color=PANEL_EMBED_COLOR,
    )
    embed.add_field(
        name="🦤 Get Dodo",
        value=(
            "Shows the island's Dodo code just to you. Reveals are logged for moderators.\n"
            "If it says the code is refreshing, wait a moment and press it again."
        ),
        inline=False,
    )
    embed.add_field(
        name="📦 Drop Items",
        value=(
            "Type items or hex codes with a quantity, separated by commas:\n"
            "```Gold nugget 30, Iron nugget 30, 09C9 x10```"
            f"Up to **{MAX_DROP_SLOTS}** different items per drop, **{MAX_STACK}** max per item."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔍 Search Catalog",
        value=(
            "Search by item or villager name. Pick items to add them to your **drop cart**, "
            "tweak amounts with **Edit Qty**, then press **Drop Cart**. "
            "Use the category menu to narrow results. Your cart survives new searches."
        ),
        inline=False,
    )
    embed.add_field(
        name="🐱 Inject Villager",
        value=(
            f"Enter a villager's name and, optionally, a plot ({PLOT_MIN}–{PLOT_MAX}). "
            "Misspelled it? I'll suggest close matches before anything happens."
        ),
        inline=False,
    )
    embed.add_field(
        name="✈️ While you're on the island",
        value=(
            "• **Do NOT press minus (−)** to leave — walk to the airport!\n"
            "• Set your nickname to `Character | Island`.\n"
            "• Don't litter — use the trash bins.\n"
            "• Never share the Dodo code outside the queue."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏱️ Good to know",
        value=(
            f"Drops and injections have a **{ACTION_COOLDOWN:.0f}s** cooldown per island. "
            "If the bot is offline, the request is rejected — check the status above and try again."
        ),
        inline=False,
    )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


def _err_embed(text: str) -> discord.Embed:
    embed = discord.Embed(description=f"❌ {text}", color=PANEL_EMBED_COLOR_ERR)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


class SubRulesPromptView(discord.ui.View):
    """Link button directly to the #sub-rules channel."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Go to #sub-rules",
                style=discord.ButtonStyle.link,
                url="https://discord.com/channels/729590421478703135/783677194576330792",
                emoji="📜",
            )
        )


async def _reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Ephemeral reply that works before AND after interaction.response was used/deferred."""
    kwargs: dict[str, Any] = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except discord.HTTPException as exc:
        logger.warning("[DropBot] Could not send reply: %s", exc)


async def _resolve_client(interaction: discord.Interaction, island_raw: str | None) -> tuple[str, Any] | None:
    """Validate the island and build the SysBot client. Replies to the user and returns None on failure."""
    island = _canonical_island(island_raw)
    if not island:
        await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
        return None
    try:
        client = get_island_sysbot_client(island)
    except Exception:
        logger.exception("[DropBot] Could not create SysBot client for %s", island)
        await _reply(interaction, embed=_err_embed("This island's bot isn't configured right now. Try again later."))
        return None
    return island, client


def _is_failure(result: Any) -> bool:
    return not isinstance(result, dict) or result.get("success") is False or (
        bool(result.get("error")) and not result.get("success")
    )


# ─── Shared actions (used by modals, catalog view and confirm view) ────────────

async def _check_island_online(island: str) -> bool:
    """Quick check: is this island's bot currently running? Uses cached telemetry data."""
    try:
        status = await fetch_island_live_status(island)
        return bool(status.get("is_running"))
    except Exception:
        return True  # fail open


async def _perform_drop(
    user: discord.abc.User,
    island_raw: str,
    items_text: str,
    *,
    heading: str,
    contents: str,
) -> tuple[discord.Embed, bool]:
    """Returns (embed to show, whether the drop was queued)."""
    denied = await _access_denied(user, require_order_rules=True)
    if denied:
        return _err_embed(denied), False
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG), False

    # Precheck: tell user immediately if bot is offline
    if not await _check_island_online(island):
        embed = discord.Embed(
            title="🔴 Island Bot Offline",
            description=(
                f"**{island}**'s bot appears to be offline right now.\n"
                "Your drop was **not** submitted. Please try again when the bot is back online."
            ),
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    wait = _cooldown_left(user.id, island, "drop")
    if wait:
        return _err_embed(f"Slow down — you can drop again {_cooldown_eta_ts(wait)}."), False

    # Resolve to hex for the SysBot; keep human-readable name for display
    hex_items: str | None = None
    try:
        from utils.data_manager import resolve_drop_items
        hex_items, _ = resolve_drop_items(items_text)
    except Exception:
        pass
    sysbot_payload = hex_items if hex_items else items_text

    try:
        client = get_island_sysbot_client(island)
        result = await client.submit_drop(items=sysbot_payload, username=getattr(user, "display_name", user.name))
    except Exception as exc:
        err_msg = str(exc) or "bot did not respond"
        logger.exception("[DropBot] Drop failed island=%s user=%s: %s", island, user.id, err_msg)
        await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, False, err_msg)

        embed = discord.Embed(
            title="⚠️ Drop Failed — Bot Offline",
            description=f"The island bot for **[{island}]({_island_url(island)})** didn't respond.",
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.add_field(name="Reason", value=f"```{err_msg[:300]}```", inline=False)
        embed.add_field(name="Attempted Items", value=f"```{contents[:300]}```", inline=False)
        embed.add_field(name="Status", value="Please make sure the island bot is running and connected.", inline=False)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    if _is_failure(result):
        err = (result or {}).get("error") if isinstance(result, dict) else None
        err = err or "unknown error"
        logger.warning("[DropBot] Drop rejected island=%s user=%s: %s", island, user.id, err)
        await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, False, str(err))

        embed = discord.Embed(
            title="⚠️ Drop Request Rejected",
            description=f"Your drop request on **[{island}]({_island_url(island)})** was not accepted by the bot.",
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.add_field(name="Error Details", value=f"```{str(err)[:300]}```", inline=False)
        embed.add_field(name="Items", value=f"```{contents[:300]}```", inline=False)
        embed.add_field(
            name="💡 Troubleshooting",
            value=(
                "• Ensure item names match the in-game English name.\n"
                "• For non-stackable items (furniture, tools, clothes), do not add a count.\n"
                "• Check that the island drop area is not full."
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    # Audit log uses hex payload; success message shows human-readable names
    await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, True, str(result.get("message") or "queued"))

    embed = discord.Embed(
        title=heading,
        description=(
            f"Your drop on **[{island}]({_island_url(island)})** is queued.\n"
            "Items will appear near you in a few seconds. **You must be on the island.**"
        ),
        color=PANEL_EMBED_COLOR,
    )
    embed.add_field(name="Items", value=f"```{contents[:400]}```", inline=False)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed, True


async def _perform_injection(
    user: discord.abc.User,
    island_raw: str,
    villager: str,
    plot: int | None,
    *,
    cooldown_key: str = "inject",
) -> tuple[discord.Embed, bool]:
    """Returns (embed to show, whether the injection was queued)."""
    denied = await _access_denied(user, require_order_rules=True)
    if denied:
        return _err_embed(denied), False
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG), False

    # Precheck: tell user immediately if bot is offline
    if not await _check_island_online(island):
        embed = discord.Embed(
            title="🔴 Island Bot Offline",
            description=(
                f"**{island}**'s bot appears to be offline right now.\n"
                "Your injection was **not** submitted. Please try again when the bot is back online."
            ),
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    wait = _cooldown_left(user.id, island, cooldown_key)
    if wait:
        return _err_embed(f"Slow down — you can inject again {_cooldown_eta_ts(wait)}."), False

    try:
        from utils.data_manager import resolve_villager_code
        villager = resolve_villager_code(villager)
    except Exception:
        pass

    try:
        client = get_island_sysbot_client(island)
        result = await client.inject_villagers(villager, plot=plot)
    except Exception:
        logger.exception("[DropBot] Injection failed island=%s user=%s", island, user.id)
        await asyncio.to_thread(_log_injection, island, villager, plot, user.id, False, "bot did not respond")
        return _err_embed("The island bot didn't respond. Check that it's online and try again."), False

    if _is_failure(result):
        err = (result or {}).get("error") if isinstance(result, dict) else None
        err = err or "unknown error"
        logger.warning("[DropBot] Injection rejected island=%s user=%s: %s", island, user.id, err)
        await asyncio.to_thread(_log_injection, island, villager, plot, user.id, False, str(err))
        return _err_embed(f"The injection was rejected: **{err}**\nMake sure the island bot is online."), False

    await asyncio.to_thread(
        _log_injection, island, villager, plot, user.id, True,
        str(result.get("message") or "Queued via Discord panel"),
    )

    embed = discord.Embed(
        title="🐱 Villager injection queued",
        description=(
            f"**{villager}** is queued for injection into **[{island}]({_island_url(island)})**.\n"
            "You **must be on the island** for the injection to take effect."
        ),
        color=PANEL_EMBED_COLOR,
    )
    if plot is not None:
        embed.add_field(name="Requested plot", value=str(plot), inline=True)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed, True


# ─── Modals ───────────────────────────────────────────────────────────────────




class RetryDropView(discord.ui.View):
    """Shown when CustomDropModal validation fails — pre-fills the modal so users don't lose their work."""

    def __init__(self, island: str, text: str):
        super().__init__(timeout=120)
        self.island = island
        self.text = text

    @discord.ui.button(label="Fix & retry", emoji="✏️", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CustomDropModal(island_name=self.island, default_text=self.text))


class CustomDropModal(discord.ui.Modal, title="📦 Custom Item Drop"):
    def __init__(self, island_name: str, default_text: str = ""):
        super().__init__()
        self.island_name = island_name
        self.items_input = discord.ui.TextInput(
            label=f"Items to drop (up to {MAX_DROP_SLOTS} items)",
            style=discord.TextStyle.paragraph,
            placeholder="Gold nugget 30, Iron nugget 30, 09C9 30",
            default=default_text or None,
            min_length=2,
            max_length=500,
            required=True,
        )
        self.add_item(self.items_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        parsed, err = parse_drop_items(self.items_input.value)
        if err or not parsed:
            retry_view = RetryDropView(island=self.island_name, text=self.items_input.value)
            await interaction.followup.send(
                f"❌ {err}\n\nPress **Fix & retry** to re-open the form with your input pre-filled.",
                view=retry_view,
                ephemeral=True,
            )
            return

        # Human-readable summary for success; hex payload for SysBot
        display_contents = ", ".join(f"{name} ×{qty}" for name, qty in parsed)
        normalized = _normalize_items(parsed)
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            normalized,
            heading="📦 Drop queued",
            contents=display_contents,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CustomDropModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")




class CustomAmiiboModal(discord.ui.Modal, title="✏️ Custom Amiibo / Villager"):
    name_input = discord.ui.TextInput(
        label="Target Amiibo / Villager Name",
        placeholder="e.g. Chai, Chelsea, Cece, Viché, W. Link",
        min_length=2,
        max_length=40,
        required=True,
    )

    def __init__(self, wizard_view: "SanrioInjectionWizardView"):
        super().__init__()
        self.wizard_view = wizard_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_name = self.name_input.value.strip()
        exact, _ = _match_villagers(raw_name)
        target = exact or raw_name.title()
        if is_sanrio_villager(target):
            target = get_canonical_sanrio_name(target)
        self.wizard_view.target_villager = target
        self.wizard_view._rebuild_items()
        await interaction.response.edit_message(
            embed=self.wizard_view.build_embed(),
            view=self.wizard_view,
        )


class CustomPlaceholderModal(discord.ui.Modal, title="✏️ Custom Plot 1 Placeholder"):
    name_input = discord.ui.TextInput(
        label="Placeholder Villager Name (Plot 1)",
        placeholder="e.g. Raymond, Marshal, Shino, Sasha",
        min_length=2,
        max_length=40,
        required=True,
    )

    def __init__(self, wizard_view: "SanrioInjectionWizardView"):
        super().__init__()
        self.wizard_view = wizard_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_name = self.name_input.value.strip()
        exact, _ = _match_villagers(raw_name)
        self.wizard_view.placeholder_villager = exact or raw_name.title()
        self.wizard_view._rebuild_items()
        await interaction.response.edit_message(
            embed=self.wizard_view.build_embed(),
            view=self.wizard_view,
        )


def _save_sanrio_wizard_state(
    user_id: int,
    island: str,
    target: str,
    placeholder: str,
    step1_done: bool,
) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sanrio_wizard_state (
                    user_id TEXT PRIMARY KEY,
                    island TEXT,
                    target_villager TEXT,
                    placeholder_villager TEXT,
                    step1_done INTEGER,
                    updated_at INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO sanrio_wizard_state
                (user_id, island, target_villager, placeholder_villager, step1_done, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(user_id), island, target, placeholder, 1 if step1_done else 0, int(time.time())),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("[DropBot] Could not save sanrio wizard state: %s", exc)


def _load_sanrio_wizard_state(user_id: int) -> dict[str, Any] | None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sanrio_wizard_state (
                    user_id TEXT PRIMARY KEY,
                    island TEXT,
                    target_villager TEXT,
                    placeholder_villager TEXT,
                    step1_done INTEGER,
                    updated_at INTEGER
                )
                """
            )
            row = conn.execute(
                """
                SELECT island, target_villager, placeholder_villager, step1_done, updated_at
                FROM sanrio_wizard_state
                WHERE user_id = ?
                """,
                (str(user_id),),
            ).fetchone()
            if row:
                if int(time.time()) - (row["updated_at"] or 0) > 7200:
                    _clear_sanrio_wizard_state(user_id)
                    return None
                return {
                    "island": row["island"],
                    "target_villager": row["target_villager"],
                    "placeholder_villager": row["placeholder_villager"],
                    "step1_done": bool(row["step1_done"]),
                    "updated_at": row["updated_at"],
                }
    except Exception as exc:
        logger.debug("[DropBot] Could not load sanrio wizard state: %s", exc)
    return None


def _clear_sanrio_wizard_state(user_id: int) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                "DELETE FROM sanrio_wizard_state WHERE user_id = ?",
                (str(user_id),),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("[DropBot] Could not clear sanrio wizard state: %s", exc)


class _OwnedView(discord.ui.View):
    """Base view that restricts interactions to the owning user and disables buttons on timeout."""

    def __init__(self, user_id: int, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self._orig_interaction: discord.Interaction | None = None
        self._interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for c in self.children:
            if hasattr(c, "disabled"):
                c.disabled = True
        target = self._orig_interaction or self._interaction
        if target:
            with contextlib.suppress(discord.HTTPException):
                await target.edit_original_response(view=self)


class SanrioInjectionWizardView(_OwnedView):
    """
    Interactive 2-step live hot-swap wizard for Sanrio and Amiibo villagers.
    Progress is persisted to the DB so users can resume after flights.
    """

    def __init__(
        self,
        island_name: str,
        user_id: int,
        target_villager: str = "Chai",
        placeholder_villager: str = "Raymond",
        step1_done: bool = False,
    ):
        super().__init__(user_id=user_id, timeout=900)  # 15 min — matches interaction token lifetime
        self.island_name = island_name
        self.target_villager = get_canonical_sanrio_name(target_villager) if is_sanrio_villager(target_villager) else target_villager.title()
        self.placeholder_villager = placeholder_villager.title()
        self.step1_done = step1_done
        self.step2_done = False
        self.last_status: str | None = None
        self._rebuild_items()

    def _rebuild_items(self) -> None:
        self.clear_items()

        # Row 0: Target Sanrio / Amiibo Select
        target_opts: list[discord.SelectOption] = []
        in_presets = False
        for name, desc, emoji in SANRIO_VILLAGERS:
            is_def = (name.lower() == self.target_villager.lower())
            if is_def:
                in_presets = True
            target_opts.append(
                discord.SelectOption(
                    label=name,
                    description=desc[:100],
                    emoji=emoji,
                    value=name,
                    default=is_def,
                )
            )
        if not in_presets:
            target_opts.insert(
                0,
                discord.SelectOption(
                    label=self.target_villager[:100],
                    description="Custom target character"[:100],
                    emoji="✨",
                    value=self.target_villager[:100],
                    default=True,
                ),
            )
        target_opts.append(
            discord.SelectOption(
                label="Type Custom Amiibo Name…",
                description="Enter any custom amiibo character"[:100],
                emoji="✏️",
                value="custom",
            )
        )
        target_select = discord.ui.Select(
            placeholder=f"🎯 Target: {self.target_villager} (Plot 1)",
            options=target_opts[:25],
            row=0,
            disabled=self.step2_done,
        )
        target_select.callback = self._on_target_select
        self.add_item(target_select)

        # Row 1: Placeholder Select (Plot 1)
        ph_opts: list[discord.SelectOption] = []
        ph_in_presets = False
        for name, desc, emoji in PLACEHOLDER_VILLAGERS:
            is_def = (name.lower() == self.placeholder_villager.lower())
            if is_def:
                ph_in_presets = True
            ph_opts.append(
                discord.SelectOption(
                    label=name,
                    description=desc[:100],
                    emoji=emoji,
                    value=name,
                    default=is_def,
                )
            )
        if not ph_in_presets:
            ph_opts.insert(
                0,
                discord.SelectOption(
                    label=self.placeholder_villager[:100],
                    description="Custom placeholder for Plot 1"[:100],
                    emoji="🐱",
                    value=self.placeholder_villager[:100],
                    default=True,
                ),
            )
        ph_opts.append(
            discord.SelectOption(
                label="Type Custom Placeholder…",
                description="Enter any villager to hold Plot 1"[:100],
                emoji="✏️",
                value="custom",
            )
        )
        placeholder_select = discord.ui.Select(
            placeholder=f"📦 Placeholder: {self.placeholder_villager} (Plot 1)",
            options=ph_opts[:25],
            row=1,
            disabled=self.step1_done or self.step2_done,
        )
        placeholder_select.callback = self._on_placeholder_select
        self.add_item(placeholder_select)

        # Row 2: Step 1 and Step 2 Action Buttons
        step1_label = f"1️⃣ Inject {self.placeholder_villager} (Plot 1)"[:80]
        step1_btn = discord.ui.Button(
            label=step1_label,
            style=discord.ButtonStyle.primary if not self.step1_done else discord.ButtonStyle.secondary,
            disabled=self.step1_done or self.step2_done,
            row=2,
        )
        step1_btn.callback = self._on_step1
        self.add_item(step1_btn)

        step2_label = f"2️⃣ Inject {self.target_villager} (Live on Island)"[:80]
        step2_btn = discord.ui.Button(
            label=step2_label,
            style=discord.ButtonStyle.success if self.step1_done else discord.ButtonStyle.secondary,
            disabled=(not self.step1_done) or self.step2_done,
            row=2,
        )
        step2_btn.callback = self._on_step2
        self.add_item(step2_btn)

        if not self.step1_done and not self.step2_done:
            skip_btn = discord.ui.Button(
                label="I already did Step 1",
                emoji="⏭️",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            skip_btn.callback = self._on_skip_step1
            self.add_item(skip_btn)

        # Row 3: Navigation Buttons
        back_btn = discord.ui.Button(
            label="Regular Villagers",
            emoji="🔙",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

        close_btn = discord.ui.Button(
            label="Close",
            emoji="✖",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    def build_embed(self) -> discord.Embed:
        target_emoji = "🎀"
        for name, _, emoji in SANRIO_VILLAGERS:
            if name.lower() == self.target_villager.lower():
                target_emoji = emoji
                break

        s1 = "✅" if self.step1_done else "⬜"
        s2 = "✅" if self.step2_done else ("👉" if self.step1_done else "⬜")
        s3 = "✅" if self.step2_done else "⬜"
        s4 = "👉" if self.step2_done else "⬜"

        tracker = (
            f"{s1} **1.** Inject {self.placeholder_villager} → Plot 1\n"
            f"{s2} **2.** Fly to {self.island_name}\n"
            f"{s3} **3.** Inject {self.target_villager} (live)\n"
            f"{s4} **4.** Talk to Plot 1 resident"
        )

        embed = discord.Embed(
            title=f"🎀 Sanrio & Amiibo Injection Wizard — {self.island_name}",
            description=tracker,
            color=SANRIO_EMBED_COLOR,
        )

        embed.add_field(
            name="🎯 Target Amiibo",
            value=f"{target_emoji} **{self.target_villager}**",
            inline=True,
        )
        embed.add_field(
            name="📦 Plot 1 Placeholder",
            value=f"🐱 **{self.placeholder_villager}**",
            inline=True,
        )

        if not self.step1_done and not self.step2_done:
            curr = f"Click **1️⃣ Inject {self.placeholder_villager}** before boarding your flight."
        elif self.step1_done and not self.step2_done:
            curr = f"Fly to **[{self.island_name}]({_island_url(self.island_name)})** now! Once you land, click **2️⃣ Inject {self.target_villager}**."
        else:
            curr = "🎉 **VILLAGER INJECTED!** Walk into Plot 1's house on the island, talk to the resident inside, and invite them to move in!"

        embed.add_field(name="📌 Current Step", value=curr, inline=False)

        if self.last_status:
            embed.add_field(name="📢 Latest Update", value=self.last_status[:1024], inline=False)

        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed

    async def _on_skip_step1(self, interaction: discord.Interaction) -> None:
        self.step1_done = True
        self.last_status = "⏭️ Marked Step 1 as done. Click **2️⃣ Inject** once you land on the island!"
        _save_sanrio_wizard_state(
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            True,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_target_select(self, interaction: discord.Interaction) -> None:
        vals = getattr(interaction.data, "get", lambda k, d: d)("values", [""])
        val = vals[0] if vals else ""
        if not val:
            return
        if val == "custom":
            await interaction.response.send_modal(CustomAmiiboModal(self))
            return
        self.target_villager = val
        _save_sanrio_wizard_state(
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            self.step1_done,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_placeholder_select(self, interaction: discord.Interaction) -> None:
        vals = getattr(interaction.data, "get", lambda k, d: d)("values", [""])
        val = vals[0] if vals else ""
        if not val:
            return
        if val == "custom":
            await interaction.response.send_modal(CustomPlaceholderModal(self))
            return
        self.placeholder_villager = val
        _save_sanrio_wizard_state(
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            self.step1_done,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_step1(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed_res, ok = await _perform_injection(
            interaction.user,
            self.island_name,
            self.placeholder_villager,
            plot=1,
            cooldown_key="sanrio1",
        )
        if ok:
            self.step1_done = True
            _save_sanrio_wizard_state(
                self.user_id,
                self.island_name,
                self.target_villager,
                self.placeholder_villager,
                True,
            )
            self.last_status = (
                f"✅ **Placeholder Injected!** `{self.placeholder_villager}` is queued in Plot 1.\n"
                f"🛫 **Take flight to [{self.island_name}]({_island_url(self.island_name)}) now!**\n"
                f"Once you land on the island, click **Step 2** below."
            )
        else:
            reason = embed_res.description or "Bot did not respond"
            self.last_status = f"⚠️ **Step 1 Error:** {reason}"

        self._rebuild_items()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    async def _on_step2(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed_res, ok = await _perform_injection(
            interaction.user,
            self.island_name,
            self.target_villager,
            plot=1,
            cooldown_key="sanrio2",
        )
        if ok:
            self.step2_done = True
            _clear_sanrio_wizard_state(self.user_id)
            self.last_status = (
                f"🎉 **VILLAGER INJECTED!**\n\n"
                f"🏠 **Go to Plot 1's house now!**\n"
                f"• Enter Plot 1's house on the island.\n"
                f"• Talk to the resident inside *(they will visually look like `{self.placeholder_villager}`)*.\n"
                f"• Invite them to move to your island! 🎀"
            )
        else:
            reason = embed_res.description or "Bot did not respond"
            self.last_status = f"⚠️ **Step 2 Error:** {reason}"

        self._rebuild_items()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        picker = InteractiveVillagerPickerView(self.island_name, self.user_id)
        embed = discord.Embed(
            title=f"🐱 Inject Villager — {self.island_name}",
            description=(
                f"Select a popular villager from the dropdown below, search the catalog, or enter any name.\n\n"
                f"🏝️ **Target Island:** [{self.island_name}]({_island_url(self.island_name)})\n"
                f"• Choose a villager from the menu to inject\n"
                f"• Use **🔍 Search Villager** to search by name or species\n"
                f"• Use **🎀 Sanrio / Amiibo** for special Sanrio villager injections\n"
                f"• Use **✏️ Type Any Name** to enter any name manually"
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.response.edit_message(embed=embed, view=picker)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Sanrio Wizard closed.", embed=None, view=None)
        self.stop()



class InjectVillagerModal(discord.ui.Modal, title="🐱 Inject Villager"):
    villager_input = discord.ui.TextInput(
        label="Villager name",
        style=discord.TextStyle.short,
        placeholder="e.g. Raymond, Marshal, Shino",
        min_length=2,
        max_length=64,
        required=True,
    )
    plot_input = discord.ui.TextInput(
        label=f"Plot ({PLOT_MIN}–{PLOT_MAX}, optional)",
        style=discord.TextStyle.short,
        placeholder="Leave blank for the next available plot",
        min_length=0,
        max_length=2,
        required=False,
    )

    def __init__(self, island_name: str):
        super().__init__()
        self.island_name = island_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_name = self.villager_input.value.strip()
        raw_plot = self.plot_input.value.strip()

        plot: int | None = None
        if raw_plot:
            if not re.fullmatch(r"[0-9]{1,2}", raw_plot) or not PLOT_MIN <= int(raw_plot) <= PLOT_MAX:
                await interaction.followup.send(
                    f"❌ Plot must be a number from {PLOT_MIN} to {PLOT_MAX}.", ephemeral=True
                )
                return
            plot = int(raw_plot)

        exact, candidates = await asyncio.to_thread(_match_villagers, raw_name)

        if exact:
            if is_sanrio_villager(exact):
                canonical = get_canonical_sanrio_name(exact)
                wizard = SanrioInjectionWizardView(
                    island_name=self.island_name,
                    user_id=interaction.user.id,
                    target_villager=canonical,
                )
                await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)
                return

            embed, _ = await _perform_injection(interaction.user, self.island_name, exact, plot)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if not candidates:
            await interaction.followup.send(
                f"❌ I couldn't find a villager called **{raw_name[:64]}**. Check the spelling and try again.",
                ephemeral=True,
            )
            return

        view = VillagerChoiceView(interaction.user.id, self.island_name, candidates, plot)
        prompt = discord.Embed(
            title="Did you mean…?",
            description=(
                f"I couldn't find **{raw_name[:64]}** exactly.\n"
                + (
                    f"Did you mean **{candidates[0]}**?"
                    if len(candidates) == 1
                    else "Pick the villager you meant from the list."
                )
            ),
            color=PANEL_EMBED_COLOR_WARN,
        )
        prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=prompt, view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] InjectVillagerModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


# ─── Ephemeral views ───────────────────────────────────────────────────────────

class VillagerChoiceView(_OwnedView):
    """Confirm (one candidate) or pick (several candidates) before injecting a villager."""

    def __init__(self, user_id: int, island_name: str, candidates: list[str], plot: int | None):
        super().__init__(user_id=user_id, timeout=90)
        self.island_name = island_name
        self.candidates = candidates
        self.plot = plot

        if len(candidates) == 1:
            confirm = discord.ui.Button(
                label=f"Yes, inject {candidates[0]}"[:80], style=discord.ButtonStyle.success, row=0
            )
            confirm.callback = self._on_confirm  # type: ignore[assignment]
            self.add_item(confirm)
        else:
            select = discord.ui.Select(
                placeholder="Choose a villager…",
                options=[discord.SelectOption(label=c[:100], value=c[:100]) for c in candidates],
                row=0,
            )
            select.callback = self._make_select_callback(select)  # type: ignore[assignment]
            self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel)

    async def _run(self, interaction: discord.Interaction, villager: str) -> None:
        await interaction.response.edit_message(
            content=f"⏳ Injecting **{villager}**…", embed=None, view=None
        )
        embed, _ = await _perform_injection(interaction.user, self.island_name, villager, self.plot)
        await interaction.edit_original_response(content=None, embed=embed, view=None)
        self.stop()

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.candidates[0])

    def _make_select_callback(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction) -> None:
            await self._run(interaction, select.values[0])

        return callback

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[DropBot] VillagerChoiceView error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


# ─── Catalog search (drop cart, category filter, persistent across searches) ───

@dataclass
class _CartEntry:
    item: CatalogItem
    qty: int


def _default_qty(it: CatalogItem) -> int:
    try:
        return max(1, min(int(it.stack_size or 1), MAX_STACK))
    except (TypeError, ValueError):
        return 1


def _cart_key(it: CatalogItem) -> str:
    return it.display_name.lower()


async def _show_catalog(
    interaction: discord.Interaction,
    island: str | None,
    query: str,
    parent: "CatalogSearchView | None" = None,
) -> None:
    """Run a search and show it: edits `parent` in place if given, else sends a new ephemeral message."""
    if not interaction.response.is_done():
        if parent is not None:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        items, villagers = await asyncio.to_thread(_search_catalog, query)
    except Exception:
        logger.exception("[DropBot] Catalog search failed for %r", query)
        await _reply(interaction, embed=_err_embed("The catalog search failed. Please try again."))
        return

    if parent is not None:
        parent.apply_search(query, items, villagers)
        await interaction.edit_original_response(embed=parent.build_search_embed(), view=parent)
        return

    view = CatalogSearchView(
        island_name=island, query=query, items=items, villagers=villagers, user_id=interaction.user.id
    )
    view.message = await interaction.followup.send(embed=view.build_search_embed(), view=view, ephemeral=True)


class CatalogSearchModal(discord.ui.Modal, title="🔍 Search ACNH Catalog"):
    search_query = discord.ui.TextInput(
        label="Search Keyword (Items & Villagers)",
        placeholder="e.g. royal crown, gold, Raymond, painting, diy",
        required=True,
        max_length=60,
    )

    def __init__(self, island_name: str | None = None, parent: "CatalogSearchView | None" = None):
        super().__init__()
        self.island_name = island_name
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _show_catalog(interaction, self.island_name, self.search_query.value.strip(), self.parent_view)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CatalogSearchModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong while searching the catalog.")


class CartEditModal(discord.ui.Modal, title="🛒 Edit Drop Cart"):
    """Edit quantities in bulk. One 'Item: quantity' per line; 0 removes the item."""

    def __init__(self, parent: "CatalogSearchView"):
        super().__init__()
        self.parent_view = parent
        self.lines = discord.ui.TextInput(
            label=f"Item: quantity  (1–{MAX_STACK}, 0 removes)",
            style=discord.TextStyle.paragraph,
            default="\n".join(f"{e.item.display_name}: {e.qty}" for e in parent.cart.values())[:4000],
            max_length=4000,
            required=True,
        )
        self.add_item(self.lines)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cart = self.parent_view.cart
        updates: dict[str, int] = {}
        for raw in self.lines.value.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            name, sep, qty_s = raw.rpartition(":")
            qty_s = qty_s.strip()
            if not sep or not qty_s.isdigit():
                await _reply(interaction, embed=_err_embed(f"I couldn't read “{raw[:50]}”. Use `Item: quantity`."))
                return
            qty = int(qty_s)
            if qty > MAX_STACK:
                await _reply(interaction, embed=_err_embed(f"Quantity for **{name.strip()[:40]}** can't exceed {MAX_STACK}."))
                return
            key = name.strip().lower()
            if key not in cart:
                await _reply(interaction, embed=_err_embed(f"I couldn't find **{name.strip()[:40]}** in your cart."))
                return
            updates[key] = qty

        for key, qty in updates.items():
            if qty == 0:
                cart.pop(key, None)
            else:
                cart[key].qty = qty

        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_search_embed("✏️ Cart updated."), view=self.parent_view
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CartEditModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


class CatalogSearchView(_OwnedView):
    """
    Catalog results with: category filter, pagination, a drop cart (add many items, edit quantities,
    then one explicit Drop), villager confirm-before-inject, and thumbnails.
    Without a target island it is read-only (inspect hex codes / villager info).
    """

    def __init__(
        self,
        island_name: str | None = None,
        query: str = "",
        items: list[CatalogItem] | None = None,
        villagers: list[CatalogVillager] | None = None,
        user_id: int | None = None,
        page_size: int = 10,
    ):
        super().__init__(user_id=user_id or 0, timeout=300)
        self.island_name = island_name
        self.query = query
        self.all_items: list[CatalogItem] = list(items or [])
        self.items: list[CatalogItem] = self.all_items
        self.villagers: list[CatalogVillager] = list(villagers or [])
        self.page_size = page_size
        self.page = 0
        self.category: str | None = None
        self.cart: dict[str, _CartEntry] = {}
        self.message: discord.Message | None = None
        self._busy = False
        self._rebuild()

    # ── state ──
    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(len(self.items) / self.page_size))

    def page_items(self) -> list[CatalogItem]:
        start = self.page * self.page_size
        return self.items[start : start + self.page_size]

    def apply_search(self, query: str, items: list[CatalogItem], villagers: list[CatalogVillager]) -> None:
        self.query = query
        self.all_items = list(items)
        self.items = self.all_items
        self.villagers = list(villagers)
        self.category = None
        self.page = 0
        self._rebuild()

    def _set_category(self, category: str | None) -> None:
        self.category = category
        self.items = [i for i in self.all_items if i.category == category] if category else self.all_items
        self.page = 0
        self._rebuild()

    # ── components ──
    def _rebuild(self) -> None:
        self.clear_items()
        row = 0
        page_items = self.page_items()

        # Items dropdown
        if page_items:
            options: list[discord.SelectOption] = []
            for idx, it in enumerate(page_items):
                bits: list[str] = []
                if _cart_key(it) in self.cart:
                    bits.append("✅ In cart")
                bits.append(str(it.category or "Item"))
                bits.append(f"×{_default_qty(it)}")
                options.append(
                    discord.SelectOption(
                        label=it.display_name[:100],
                        description=" • ".join(bits)[:100],
                        value=f"item_{idx}",
                        emoji="📜" if it.diy else "📦",
                    )
                )
            placeholder = (
                "Add item(s) to your drop cart…"
                if self.island_name
                else f"Select item(s) to inspect (page {self.page + 1}/{self.total_pages})…"
            )
            item_select = discord.ui.Select(
                placeholder=placeholder[:100],
                min_values=1,
                max_values=min(len(options), MAX_DROP_SLOTS),
                options=options,
                row=row,
            )
            item_select.callback = self.on_item_select  # type: ignore[assignment]
            self.add_item(item_select)
            row += 1

        # Villagers dropdown (first page only)
        if self.villagers and self.page == 0:
            v_options = [
                discord.SelectOption(
                    label=v.display_name[:100],
                    description=f"ID: {v.villager_id} • {v.personality}"[:100],
                    value=f"villager_{idx}",
                    emoji="🐱",
                )
                for idx, v in enumerate(self.villagers[:10])
            ]
            villager_select = discord.ui.Select(
                placeholder=(f"Inject a villager into {self.island_name}…" if self.island_name
                             else "Select villager to inspect…")[:100],
                min_values=1,
                max_values=1,
                options=v_options,
                row=row,
            )
            villager_select.callback = self.on_villager_select  # type: ignore[assignment]
            self.add_item(villager_select)
            row += 1

        # Category filter
        counts = Counter(i.category for i in self.all_items if i.category)
        if len(counts) > 1:
            cat_options = [
                discord.SelectOption(
                    label=f"All categories ({len(self.all_items)})", value="__all__", default=self.category is None
                )
            ]
            for cat, n in counts.most_common(24):
                cat_options.append(
                    discord.SelectOption(
                        label=str(cat)[:100], description=f"{n} item(s)", value=str(cat)[:100],
                        default=(cat == self.category),
                    )
                )
            cat_select = discord.ui.Select(
                placeholder="Filter by category…", min_values=1, max_values=1, options=cat_options, row=row
            )
            cat_select.callback = self.on_category_select  # type: ignore[assignment]
            self.add_item(cat_select)
            row += 1

        # Navigation
        prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page <= 0, row=row)
        prev_btn.callback = self.on_prev_page  # type: ignore[assignment]
        self.add_item(prev_btn)
        self.add_item(
            discord.ui.Button(
                label=f"{self.page + 1}/{self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True, row=row
            )
        )
        next_btn = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1, row=row
        )
        next_btn.callback = self.on_next_page  # type: ignore[assignment]
        self.add_item(next_btn)
        search_btn = discord.ui.Button(label="Search", emoji="🔍", style=discord.ButtonStyle.primary, row=row)
        search_btn.callback = self.on_search_button  # type: ignore[assignment]
        self.add_item(search_btn)
        close_btn = discord.ui.Button(label="Close", emoji="✖", style=discord.ButtonStyle.secondary, row=row)
        close_btn.callback = self.on_close  # type: ignore[assignment]
        self.add_item(close_btn)
        row += 1

        # Cart actions (only when a target island is set)
        if self.island_name:
            n = len(self.cart)
            drop_btn = discord.ui.Button(
                label=f"Drop Cart ({n})", emoji="📦", style=discord.ButtonStyle.success, disabled=n == 0, row=row
            )
            drop_btn.callback = self.on_drop_cart  # type: ignore[assignment]
            self.add_item(drop_btn)
            edit_btn = discord.ui.Button(
                label="Edit Qty", emoji="✏️", style=discord.ButtonStyle.secondary, disabled=n == 0, row=row
            )
            edit_btn.callback = self.on_edit_cart  # type: ignore[assignment]
            self.add_item(edit_btn)
            clear_btn = discord.ui.Button(
                label="Clear", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=n == 0, row=row
            )
            clear_btn.callback = self.on_clear_cart  # type: ignore[assignment]
            self.add_item(clear_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message("This search menu is not yours.", ephemeral=True)
            return False
        return True

    # ── embed ──
    def build_search_embed(self, notice: str | None = None) -> discord.Embed:
        page_items = self.page_items()
        desc = f"Found **{len(self.all_items)}** items and **{len(self.villagers)}** villagers."
        if self.category:
            desc += f"\n🗂️ Filter: **{self.category}** ({len(self.items)} shown)"
        desc += f"\n**Page:** `{self.page + 1} / {self.total_pages}`"
        if self.island_name:
            desc += f"\n🏝️ **Target island:** [{self.island_name}]({_island_url(self.island_name)})"
            desc += "\n*Pick items to add them to your cart, then press **Drop Cart**.*"
        else:
            desc += "\n*Use the dropdowns to inspect items and villagers.*"
        if notice:
            desc = f"{notice}\n\n{desc}"

        embed = discord.Embed(title=f"🔍 Catalog Search: '{self.query or 'All'}'", description=desc, color=PANEL_EMBED_COLOR)

        if self.villagers and self.villagers[0].photo_url and self.page == 0:
            embed.set_thumbnail(url=self.villagers[0].photo_url)
        elif page_items and page_items[0].image_url:
            embed.set_thumbnail(url=page_items[0].image_url)

        if page_items:
            lines = []
            start_num = self.page * self.page_size + 1
            for i, it in enumerate(page_items, start_num):
                cart_mark = "🛒 " if _cart_key(it) in self.cart else ""
                diy_badge = " *(DIY)*" if it.diy else ""
                hex_code = it.to_hex()
                hex_part = f" • `{hex_code}`" if hex_code else ""
                lines.append(f"`{i:02d}.` {cart_mark}**{it.display_name}**{diy_badge} — `{it.category}`{hex_part}")

            chunk: list[str] = []
            c_len = 0
            first = True
            for line in lines + [None]:  # type: ignore[list-item]
                if line is None or c_len + len(line) + 1 > 950:
                    if chunk:
                        embed.add_field(
                            name=f"📦 Matching Items (Page {self.page + 1}/{self.total_pages})" if first else "📦 Items (cont.)",
                            value="\n".join(chunk),
                            inline=False,
                        )
                        first = False
                    chunk, c_len = ([line], len(line)) if line else ([], 0)
                else:
                    chunk.append(line)
                    c_len += len(line) + 1

        if self.villagers and self.page == 0:
            v_lines = [
                f"• **{v.name}** (`ID: {v.villager_id}`) — *{v.species} / {v.personality}*" for v in self.villagers[:6]
            ]
            embed.add_field(name="🐱 Matching Villagers", value="\n".join(v_lines)[:1024], inline=False)

        if self.island_name and self.cart:
            cart_lines = [f"• **{e.item.display_name}** ×{e.qty}" for e in self.cart.values()]
            embed.add_field(
                name=f"🛒 Drop Cart ({len(self.cart)}/{MAX_DROP_SLOTS})",
                value="\n".join(cart_lines)[:1024],
                inline=False,
            )

        if not self.all_items and not self.villagers:
            embed.description = (
                f"❌ No items or villagers found matching **{self.query}**.\n\n"
                "**Suggestions:**\n"
                "• Try a shorter keyword or check spelling\n"
                "• Filter by category or search with a broader term\n"
                "• For villagers, try searching with the **Inject Villager** button"
            )

        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed

    async def _refresh(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_search_embed(notice), view=self)

    # ── navigation / filter ──
    async def on_prev_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    async def on_next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._refresh(interaction)

    async def on_category_select(self, interaction: discord.Interaction) -> None:
        val = (interaction.data or {}).get("values", ["__all__"])[0]
        self._set_category(None if val == "__all__" else val)
        await interaction.response.edit_message(embed=self.build_search_embed(), view=self)

    async def on_search_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CatalogSearchModal(island_name=self.island_name, parent=self))

    async def on_close(self, interaction: discord.Interaction) -> None:
        self.stop()
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    # ── items ──
    async def on_item_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        page_items = self.page_items()
        chosen: list[CatalogItem] = []
        for val in values:
            try:
                idx = int(val.replace("item_", ""))
            except ValueError:
                continue
            if 0 <= idx < len(page_items):
                chosen.append(page_items[idx])

        if not chosen:
            await interaction.response.defer()
            return

        if self.island_name:
            added, already, skipped = 0, 0, 0
            for it in chosen:
                key = _cart_key(it)
                if key in self.cart:
                    already += 1
                elif len(self.cart) >= MAX_DROP_SLOTS:
                    skipped += 1
                else:
                    self.cart[key] = _CartEntry(it, _default_qty(it))
                    added += 1
            notes = []
            if added:
                notes.append(f"🛒 Added **{added}** item(s) to your cart. *(Default stack amounts added — use **Edit Qty** to adjust)*")
            if already:
                notes.append(f"{already} already in cart.")
            if skipped:
                notes.append(f"⚠️ Cart is full ({MAX_DROP_SLOTS} max) — {skipped} not added.")
            await self._refresh(interaction, " ".join(notes))
            return

        # Inspect mode (no target island)
        embed = discord.Embed(
            title=f"📦 Selected Items ({len(chosen)})",
            description="Catalog details and hex codes for your selection:",
            color=PANEL_EMBED_COLOR,
        )
        for it in chosen:
            diy_txt = " *(DIY Recipe)*" if it.diy else ""
            hex_code = it.to_hex() or it.internal_id or "N/A"
            embed.add_field(
                name=f"{it.display_name}{diy_txt}",
                value=f"• Category: `{it.category}`\n• Hex Code: `{hex_code}`\n• Stack Size: `{it.stack_size}`",
                inline=False,
            )
        if chosen[0].image_url:
            embed.set_thumbnail(url=chosen[0].image_url)
        embed.set_footer(
            text="💡 Tip: use /catalog inside an island channel, or the Search Catalog button on a panel, to drop directly."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── cart ──
    async def on_edit_cart(self, interaction: discord.Interaction) -> None:
        if not self.cart:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(CartEditModal(self))

    async def on_clear_cart(self, interaction: discord.Interaction) -> None:
        self.cart.clear()
        await self._refresh(interaction, "🗑️ Cart cleared.")

    async def on_drop_cart(self, interaction: discord.Interaction) -> None:
        if not self.cart or not self.island_name or self._busy:
            await interaction.response.defer()
            return
        self._busy = True
        try:
            await interaction.response.defer()   # silent ack; we edit the search message below
            entries = list(self.cart.values())
            drop_text = ", ".join(_format_item_for_sysbot(e.item.name, e.qty) for e in entries)
            summary = ", ".join(f"{e.item.display_name} ×{e.qty}" for e in entries)
            embed, ok = await _perform_drop(
                interaction.user,
                self.island_name,
                drop_text,
                heading="📦 Catalog Drop Queued",
                contents=summary,
            )
            if ok:
                self.cart.clear()
            self._rebuild()
            notice = "✅ Drop queued — cart cleared." if ok else "⚠️ Drop not sent — your cart is kept."
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(embed=self.build_search_embed(notice), view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)
        finally:
            self._busy = False

    # ── villagers ──
    async def on_villager_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        try:
            v = self.villagers[int(values[0].replace("villager_", ""))]
        except (ValueError, IndexError):
            await interaction.response.defer()
            return

        if self.island_name:
            prompt = discord.Embed(
                title="Inject this villager?",
                description=f"**{v.name}** → **{self.island_name}** (next available plot)",
                color=PANEL_EMBED_COLOR_WARN,
            )
            if v.photo_url:
                prompt.set_thumbnail(url=v.photo_url)
            prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            view = VillagerChoiceView(interaction.user.id, self.island_name, [v.name], None)
            await interaction.response.send_message(embed=prompt, view=view, ephemeral=True)
        else:
            embed = discord.Embed(
                title=f"🐱 Villager: {v.name}",
                description=(
                    f"**Species:** {v.species}\n"
                    f"**Personality:** {v.personality}\n"
                    f"**Internal ID:** `{v.villager_id}`\n\n"
                    f"💡 To inject **{v.name}**, use `/catalog` in an island channel or the "
                    "**Search Catalog** button on an island panel."
                ),
                color=PANEL_EMBED_COLOR,
            )
            if v.photo_url:
                embed.set_thumbnail(url=v.photo_url)
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[DropBot] CatalogSearchView error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True  # type: ignore[union-attr]
        if self.message:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self)


# ─── Interactive Drop & Villager Pickers ───────────────────────────────────────


class DropFollowupView(_OwnedView):
    """Shown after a successful drop: Drop again, New drop menu, etc."""

    def __init__(self, island_name: str, user_id: int, last_drop: str, display_name: str = ""):
        super().__init__(user_id=user_id, timeout=120)
        self.island_name = island_name
        self.last_drop = last_drop
        self.display_name = display_name or last_drop

    @discord.ui.button(label="Drop Again", emoji="🔁", style=discord.ButtonStyle.primary)
    async def on_repeat(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            self.last_drop,
            heading="📦 Drop queued",
            contents=self.display_name,
        )
        followup = DropFollowupView(self.island_name, self.user_id, self.last_drop, self.display_name)
        await interaction.followup.send(embed=embed, view=followup, ephemeral=True)

    @discord.ui.button(label="New Drop Menu", emoji="📦", style=discord.ButtonStyle.secondary)
    async def on_new_drop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picker = InteractiveDropPickerView(self.island_name, self.user_id)
        embed = discord.Embed(
            title=f"📦 Drop Items — {self.island_name}",
            description=(
                f"Choose an item preset below, search the catalog, or type custom items.\n\n"
                f"🏝️ **Target Island:** [{self.island_name}]({_island_url(self.island_name)})\n"
                f"• Up to **{MAX_DROP_SLOTS}** item slots per drop\n"
                f"• Maximum stack size: **{MAX_STACK}** per item"
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.response.send_message(embed=embed, view=picker, ephemeral=True)


class PresetDropConfirmView(_OwnedView):
    """Confirm preset item drop before submitting to prevent accidental drops on mobile."""

    def __init__(self, island_name: str, user_id: int, item_code: str, label: str, emoji: str = "📦"):
        super().__init__(user_id=user_id, timeout=90)
        self.island_name = island_name
        self.item_code = item_code
        self.label = label
        self.emoji = emoji

    @discord.ui.button(label="Drop Now", emoji="📦", style=discord.ButtonStyle.success)
    async def on_confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            self.item_code,
            heading="📦 Drop queued",
            contents=self.label,
        )
        followup = DropFollowupView(self.island_name, self.user_id, self.item_code, self.label)
        await interaction.followup.send(embed=embed, view=followup, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Back", emoji="🔙", style=discord.ButtonStyle.secondary)
    async def on_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picker = InteractiveDropPickerView(self.island_name, self.user_id)
        embed = discord.Embed(
            title=f"📦 Drop Items — {self.island_name}",
            description=(
                f"Choose an item preset below, search the catalog, or type custom items.\n\n"
                f"🏝️ **Target Island:** [{self.island_name}]({_island_url(self.island_name)})\n"
                f"• Up to **{MAX_DROP_SLOTS}** item slots per drop\n"
                f"• Maximum stack size: **{MAX_STACK}** per item"
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.response.edit_message(embed=embed, view=picker)
        self.stop()


class InteractiveDropPickerView(_OwnedView):
    """Interactive item drop picker: presets dropdown, search catalog, browse all, or custom text."""

    def __init__(self, island_name: str, user_id: int):
        super().__init__(user_id=user_id, timeout=180)
        self.island_name = island_name

        catalog = ACNHCatalog.get_instance()
        presets_data = catalog.get_drop_presets()
        if not presets_data:
            from utils.acnh_catalog import POPULAR_DROP_PRESETS
            presets_data = list(POPULAR_DROP_PRESETS)
        preset_options = [
            discord.SelectOption(
                label=label[:100],
                description=desc[:100],
                emoji=emoji,
                value=item_code[:100],
            )
            for label, desc, item_code, emoji in presets_data
        ]
        preset_select = discord.ui.Select(
            placeholder="📦 Select an item preset to drop…",
            options=preset_options,
            row=0,
        )
        preset_select.callback = self._on_preset_select
        self.add_item(preset_select)

        search_btn = discord.ui.Button(label="Search Catalog", emoji="🔍", style=discord.ButtonStyle.primary, row=1)
        search_btn.callback = self._on_search_catalog
        self.add_item(search_btn)

        browse_btn = discord.ui.Button(label="Browse All Items", emoji="📦", style=discord.ButtonStyle.secondary, row=1)
        browse_btn.callback = self._on_browse_all
        self.add_item(browse_btn)

        custom_btn = discord.ui.Button(label="Custom / Hex", emoji="📝", style=discord.ButtonStyle.secondary, row=1)
        custom_btn.callback = self._on_custom_modal
        self.add_item(custom_btn)

        close_btn = discord.ui.Button(label="Close", emoji="✖", style=discord.ButtonStyle.secondary, row=1)
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _on_preset_select(self, interaction: discord.Interaction) -> None:
        vals = getattr(interaction.data, "get", lambda k, d: d)("values", [""])
        select = vals[0] if vals else ""
        if not select:
            return

        catalog = ACNHCatalog.get_instance()
        presets_data = catalog.get_drop_presets() or []
        label = select
        emoji = "📦"
        for l, d, code, em in presets_data:
            if code == select:
                label = l
                emoji = em
                break

        confirm_embed = discord.Embed(
            title=f"📦 Confirm Preset Drop — {label}",
            description=(
                f"You are about to drop **{emoji} {label}** on **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                "👉 **Make sure you are standing on the island before confirming!**\n"
                "Items will drop near your character in a few seconds."
            ),
            color=PANEL_EMBED_COLOR,
        )
        confirm_embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        confirm_view = PresetDropConfirmView(self.island_name, self.user_id, select, label, emoji)
        await interaction.response.edit_message(embed=confirm_embed, view=confirm_view)

    async def _on_search_catalog(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CatalogSearchModal(island_name=self.island_name))

    async def _on_browse_all(self, interaction: discord.Interaction) -> None:
        await _show_catalog(interaction, self.island_name, query="")

    async def _on_custom_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CustomDropModal(island_name=self.island_name))

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Menu closed.", embed=None, view=None)
        self.stop()


class VillagerPlotPickView(_OwnedView):
    """Pick plot (1–10 or next available) and confirm villager injection."""

    def __init__(self, user_id: int, island_name: str, villager_name: str):
        super().__init__(user_id=user_id, timeout=90)
        self.island_name = island_name
        self.villager_name = villager_name
        self.selected_plot: int | None = None

        plot_options = [
            discord.SelectOption(
                label="Next Available Plot (Default)",
                description="Automatically uses the next available house plot",
                value="none",
                default=True,
            )
        ] + [
            discord.SelectOption(
                label=f"Plot #{p}",
                description=f"House plot slot #{p}",
                value=str(p),
            )
            for p in range(PLOT_MIN, PLOT_MAX + 1)
        ]

        plot_select = discord.ui.Select(
            placeholder="Select house plot (optional)…",
            options=plot_options,
            row=0,
        )
        plot_select.callback = self._on_plot_select
        self.add_item(plot_select)

        inject_btn = discord.ui.Button(
            label=f"Inject {villager_name}"[:80],
            emoji="⚡",
            style=discord.ButtonStyle.success,
            row=1,
        )
        inject_btn.callback = self._on_inject
        self.add_item(inject_btn)

        cancel_btn = discord.ui.Button(
            label="Cancel",
            emoji="✖",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_plot_select(self, interaction: discord.Interaction) -> None:
        vals = getattr(interaction.data, "get", lambda k, d: d)("values", ["none"])
        val = vals[0] if vals else "none"
        if val != "none" and val.isdigit():
            self.selected_plot = int(val)
        else:
            self.selected_plot = None
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def _on_inject(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content=f"⏳ Injecting **{self.villager_name}** into **{self.island_name}**…",
            embed=None,
            view=None,
        )
        embed, _ = await _perform_injection(
            interaction.user, self.island_name, self.villager_name, self.selected_plot
        )
        await interaction.edit_original_response(content=None, embed=embed, view=None)
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Injection cancelled.", embed=None, view=None)
        self.stop()


class VillagerSearchModal(discord.ui.Modal, title="🔍 Search Villager"):
    search_query = discord.ui.TextInput(
        label="Villager Name or Keyword",
        placeholder="e.g. Raymond, Marshal, Cat, Rabbit, Peppy",
        required=True,
        max_length=60,
    )

    def __init__(self, island_name: str):
        super().__init__()
        self.island_name = island_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_name = self.search_query.value.strip()

        exact, candidates = await asyncio.to_thread(_match_villagers, raw_name, 15)
        if exact:
            if is_sanrio_villager(exact):
                canonical = get_canonical_sanrio_name(exact)
                saved = await asyncio.to_thread(_load_sanrio_wizard_state, interaction.user.id)
                step1_done = bool(saved.get("step1_done")) if (saved and saved.get("target_villager", "").lower() == canonical.lower()) else False
                wizard = SanrioInjectionWizardView(
                    island_name=self.island_name,
                    user_id=interaction.user.id,
                    target_villager=canonical,
                    step1_done=step1_done,
                )
                if step1_done:
                    wizard.last_status = "✈️ **Resumed Session (Step 2 Ready):** Pick up right where you left off!"
                await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)
                return

            view = VillagerPlotPickView(
                user_id=interaction.user.id,
                island_name=self.island_name,
                villager_name=exact,
            )
            embed = discord.Embed(
                title=f"🐱 Inject {exact}",
                description=(
                    f"Found **{exact}**! Choose where to inject on **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                    f"• Click **⚡ Inject {exact}** for quick injection\n"
                    f"• Or choose a specific plot ({PLOT_MIN}–{PLOT_MAX}) from the dropdown."
                ),
                color=PANEL_EMBED_COLOR,
            )
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return

        if not candidates:
            await interaction.followup.send(
                embed=_err_embed(f"I couldn't find any villager matching **{raw_name[:50]}**. Check the spelling and try again."),
                ephemeral=True,
            )
            return

        view = VillagerChoiceView(interaction.user.id, self.island_name, candidates, None)
        prompt = discord.Embed(
            title="🔍 Villager Search Results",
            description=(
                f"Matching villagers for **{raw_name[:50]}**:\n"
                "Pick the villager you want to inject from the list below."
            ),
            color=PANEL_EMBED_COLOR,
        )
        prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=prompt, view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] VillagerSearchModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong searching villagers.")


class InteractiveVillagerPickerView(_OwnedView):
    """Interactive villager selection: top villagers dropdown, live search, manual text."""

    def __init__(self, island_name: str, user_id: int):
        super().__init__(user_id=user_id, timeout=180)
        self.island_name = island_name

        catalog = ACNHCatalog.get_instance()
        popular_villagers = catalog.get_popular_villagers()
        if not popular_villagers:
            from utils.acnh_catalog import POPULAR_VILLAGERS
            v_options = [
                discord.SelectOption(
                    label=name[:100],
                    description=desc[:100],
                    emoji=emoji_char,
                    value=name[:100],
                )
                for name, desc, emoji_char in POPULAR_VILLAGERS
            ]
        else:
            v_options = [
                discord.SelectOption(
                    label=v.name[:100],
                    description=(f"{v.personality} • {v.species}" if v.personality and v.species else v.species or "Villager")[:100],
                    emoji=v.emoji,
                    value=v.name[:100],
                )
                for v in popular_villagers
            ]
        v_select = discord.ui.Select(
            placeholder="🐱 Choose a popular villager to inject…",
            options=v_options,
            row=0,
        )
        v_select.callback = self._on_villager_select
        self.add_item(v_select)

        search_btn = discord.ui.Button(label="Search Villager", emoji="🔍", style=discord.ButtonStyle.primary, row=1)
        search_btn.callback = self._on_search_villager
        self.add_item(search_btn)

        sanrio_btn = discord.ui.Button(label="Sanrio / Amiibo", emoji="🎀", style=discord.ButtonStyle.success, row=1)
        sanrio_btn.callback = self._on_sanrio_wizard
        self.add_item(sanrio_btn)

        type_btn = discord.ui.Button(label="Type Any Name", emoji="✏️", style=discord.ButtonStyle.secondary, row=1)
        type_btn.callback = self._on_type_modal
        self.add_item(type_btn)

        close_btn = discord.ui.Button(label="Close", emoji="✖", style=discord.ButtonStyle.secondary, row=1)
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _on_villager_select(self, interaction: discord.Interaction) -> None:
        vals = getattr(interaction.data, "get", lambda k, d: d)("values", [""])
        selected_name = vals[0] if vals else ""
        if not selected_name:
            return
        if is_sanrio_villager(selected_name):
            canonical = get_canonical_sanrio_name(selected_name)
            saved = await asyncio.to_thread(_load_sanrio_wizard_state, self.user_id)
            step1_done = bool(saved.get("step1_done")) if (saved and saved.get("target_villager", "").lower() == canonical.lower()) else False
            wizard = SanrioInjectionWizardView(
                island_name=self.island_name,
                user_id=self.user_id,
                target_villager=canonical,
                step1_done=step1_done,
            )
            if step1_done:
                wizard.last_status = "✈️ **Resumed Session (Step 2 Ready):** Pick up right where you left off!"
            await interaction.response.edit_message(embed=wizard.build_embed(), view=wizard)
            return

        view = VillagerPlotPickView(
            user_id=self.user_id,
            island_name=self.island_name,
            villager_name=selected_name,
        )
        embed = discord.Embed(
            title=f"🐱 Inject {selected_name}",
            description=(
                f"Choose where to inject **{selected_name}** into **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                f"• Click **⚡ Inject {selected_name}** for quick injection\n"
                f"• Or choose a specific plot ({PLOT_MIN}–{PLOT_MAX}) from the dropdown."
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _on_sanrio_wizard(self, interaction: discord.Interaction) -> None:
        saved = await asyncio.to_thread(_load_sanrio_wizard_state, self.user_id)
        if saved and saved.get("step1_done"):
            wizard = SanrioInjectionWizardView(
                island_name=saved.get("island") or self.island_name,
                user_id=self.user_id,
                target_villager=saved.get("target_villager") or "Chai",
                placeholder_villager=saved.get("placeholder_villager") or "Raymond",
                step1_done=True,
            )
            wizard.last_status = "✈️ **Resumed Session (Step 2 Ready):** Pick up right where you left off!"
        else:
            wizard = SanrioInjectionWizardView(island_name=self.island_name, user_id=self.user_id)
        await interaction.response.edit_message(embed=wizard.build_embed(), view=wizard)

    async def _on_search_villager(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(VillagerSearchModal(island_name=self.island_name))

    async def _on_type_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(InjectVillagerModal(island_name=self.island_name))

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Menu closed.", embed=None, view=None)
        self.stop()


# ─── Persistent panel buttons (DynamicItem) ────────────────────────────────────

def _cid(action: str, island: str) -> str:
    """custom_id = dropbot:<action>:<url-quoted island>. Stays under Discord's 100-character limit."""
    return f"dropbot:{action}:{urllib.parse.quote(island, safe='')}"


class _IslandButton:
    """
    Shared behaviour for panel buttons. Concrete classes only set the `_btn_*` attributes, pass
    their own `template=` (DynamicItem needs it per class) and implement callback().
    Attribute names are prefixed so they can't shadow discord.ui.Item properties like `row`/`label`.
    """

    _btn_action: str
    _btn_label: str
    _btn_emoji: str
    _btn_style: discord.ButtonStyle = discord.ButtonStyle.secondary
    _btn_row: int = 0

    def __init__(self, island: str):
        super().__init__(  # type: ignore[call-arg]
            discord.ui.Button(
                label=self._btn_label,
                emoji=self._btn_emoji,
                style=self._btn_style,
                custom_id=_cid(self._btn_action, island),
                row=self._btn_row,
            )
        )
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def _gate(self, interaction: discord.Interaction, *, require_order_rules: bool = False) -> str | None:
        """Canonical island if the click may proceed; otherwise replies with the reason and returns None."""
        denied = await _access_denied(interaction.user, require_order_rules=require_order_rules)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return None
        island = _canonical_island(self.island)
        if not island:
            await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
            return None
        return island

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("[DropBot] Button error island=%s: %s", getattr(self, "island", "?"), error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again in a moment.")


class GetDodoButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:getdodo:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "getdodo", "Get Dodo", "🦤"
    _btn_style = discord.ButtonStyle.success

    async def callback(self, interaction: discord.Interaction) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        resolved = await _resolve_client(interaction, self.island)
        if not resolved:
            return
        island, client = resolved

        dodo = await _find_dodo(island, client)

        if not dodo:
            embed = discord.Embed(
                title="🔄 Dodo code unavailable",
                description=(
                    f"The Dodo code for **{island}** is refreshing, or the bot is offline.\n"
                    "Please try again in a moment."
                ),
                color=PANEL_EMBED_COLOR_WARN,
            )
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🦤 Dodo Code — {island}",
            url=_island_url(island),
            description=(
                f"Here is your **private** Dodo code for **{island}**.\n"
                "⚠️ _Do NOT share this with anyone outside the queue._"
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.add_field(name="✈️ Dodo Code", value=f"```{dodo}```", inline=False)
        embed.add_field(
            name="📋 Reminders",
            value=(
                "• **Do NOT press minus (−)** to leave — walk to the airport!\n"
                "• Change your nickname to `Character | Island` format.\n"
                "• Do not litter — use the trash bins on the island."
            ),
            inline=False,
        )
        embed.add_field(name="🌐 Island Page", value=f"[View {island} on chopaeng.com]({_island_url(island)})", inline=False)
        embed.set_image(url=DODO_GIF)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Audit webhook — background task (strong reference held in _BACKGROUND_TASKS)
        _spawn(
            _fire_dropbot_dodo_webhook(
                str(interaction.user.id),
                interaction.user.name,
                interaction.user.display_name,
                island,
                str(interaction.channel_id or ""),
            )
        )

        # FlightLogger integration
        flight_cog = interaction.client.get_cog("FlightLoggerCog")
        if flight_cog and hasattr(flight_cog, "register_dodo_request"):
            try:
                guild_icon = interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None
                flight_cog.register_dodo_request(
                    interaction.user.id,
                    interaction.user,
                    interaction.channel,
                    None,
                    guild_icon,
                )
            except Exception as exc:
                logger.debug("[DropBot] flight_cog note: %s", exc)


class DropItemsButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:drop:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "drop", "Drop Items", "📦"
    _btn_style = discord.ButtonStyle.primary

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if island := await self._gate(interaction, require_order_rules=True):
                embed = discord.Embed(
                    title=f"📦 Drop Items — {island}",
                    description=(
                        f"Choose an item preset from the menu, browse or search the catalog, or enter custom hex codes.\n\n"
                        f"🏝️ **Target Island:** [{island}]({_island_url(island)})\n"
                        f"• Pick an item preset from the dropdown for instant dropping\n"
                        f"• Use **🔍 Search Catalog** to search 6,000+ items and build a cart\n"
                        f"• Use **📝 Custom / Hex** to enter your own items or hex list"
                    ),
                    color=PANEL_EMBED_COLOR,
                )
                embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
                view = InteractiveDropPickerView(island_name=island, user_id=interaction.user.id)
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            logger.error("[DropBot] DropItemsButton error: %s", exc, exc_info=exc)
            await _reply(interaction, "❌ Something went wrong opening the item drop picker.")


class SearchCatalogButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:search:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "search", "Search Catalog", "🔍"

    async def callback(self, interaction: discord.Interaction) -> None:
        if island := await self._gate(interaction, require_order_rules=True):
            await interaction.response.send_modal(CatalogSearchModal(island_name=island))


class InjectVillagerButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:villager:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "villager", "Inject Villager", "🐱"

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if island := await self._gate(interaction, require_order_rules=True):
                embed = discord.Embed(
                    title=f"🐱 Inject Villager — {island}",
                    description=(
                        f"Select a popular villager from the dropdown below, search the catalog, or enter any name.\n\n"
                        f"🏝️ **Target Island:** [{island}]({_island_url(island)})\n"
                        f"• Choose a villager from the menu to inject\n"
                        f"• Use **🔍 Search Villager** to search by name or species\n"
                        f"• Use **🎀 Sanrio / Amiibo** for special Sanrio villager injections\n"
                        f"• Use **✏️ Type Any Name** to enter any name manually"
                    ),
                    color=PANEL_EMBED_COLOR,
                )
                embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
                view = InteractiveVillagerPickerView(island_name=island, user_id=interaction.user.id)
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            logger.error("[DropBot] InjectVillagerButton error: %s", exc, exc_info=exc)
            await _reply(interaction, "❌ Something went wrong opening the villager picker.")


class GuideButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:guide:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "guide", "Guide", "📖"
    _btn_row = 1

    async def callback(self, interaction: discord.Interaction) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return
        island = _canonical_island(self.island)
        if not island:
            await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
            return
        await _reply(interaction, embed=_build_guide_embed(island))


class RefreshButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:refresh:(?P<island>[^:]+)",
):
    """Legacy: panels posted before auto-refresh carry this button. It upgrades them to the current layout."""

    _btn_action, _btn_label, _btn_emoji = "refresh", "Refresh", "🔄"
    _btn_style = discord.ButtonStyle.grey

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()   # silent ack; the panel message is edited below

        resolved = await _resolve_client(interaction, self.island)
        if not resolved:
            return
        island, _ = resolved

        status_data = await fetch_island_live_status(island)
        await interaction.edit_original_response(
            embed=_build_panel_embed(island, status_data),
            view=IslandPanelView(island, status_data),
        )


PANEL_ITEMS = (GetDodoButton, DropItemsButton, SearchCatalogButton, InjectVillagerButton, GuideButton)
ALL_REGISTERED_DYNAMIC_ITEMS = PANEL_ITEMS + (RefreshButton,)


class IslandPanelView(discord.ui.View):
    """
    Row 0: Get Dodo · Drop Items · Search Catalog · Inject Villager
    Row 1: Guide · Island Page (link)
    The action buttons are DynamicItems registered in cog_load(), so this view needs no per-message
    bot.add_view() and survives restarts. The link button needs no registration.
    """

    def __init__(self, island_name: str, status_data: dict | None = None):
        super().__init__(timeout=None)
        self.island_name = island_name
        is_running = True
        is_valid_dodo = True
        if status_data:
            is_running = bool(status_data.get("is_running", True))
            raw_dodo = (status_data.get("dodo_code") or "").strip().upper()
            is_valid_dodo = bool(raw_dodo and raw_dodo not in _INVALID_DODO_CODES)

        for item_cls in PANEL_ITEMS:
            btn = item_cls(island_name)
            if item_cls is GetDodoButton:
                if not is_running:
                    btn.disabled = True
                    btn.label = "Bot Offline"
                    btn.style = discord.ButtonStyle.secondary
                elif not is_valid_dodo:
                    btn.disabled = True
                    btn.label = "Dodo refreshing…"
                    btn.style = discord.ButtonStyle.secondary
            self.add_item(btn)
        self.add_item(
            discord.ui.Button(
                label="Island Page",
                emoji="🌐",
                style=discord.ButtonStyle.link,
                url=_island_url(island_name),
                row=1,
            )
        )


# ─── DropBot Cog ──────────────────────────────────────────────────────────────

class DropBotCog(commands.Cog, name="DropBot"):
    """
    Manages persistent interactive island control panels across sub-island channels.
    No privileged intents required — operates entirely on Discord's Interaction API.
    Auto-refreshes panel messages in the background so users always see live status.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sigs: dict[str, tuple[str, float]] = {}   # channel_id -> (embed signature, last edit time)
        self._dead_panels: set[str] = set()              # channel_ids whose panel message no longer exists

    async def cog_load(self) -> None:
        await asyncio.to_thread(_init_tables)
        await asyncio.to_thread(_get_villager_names)   # pre-warm cache
        # Telemetry worker is shared with other cogs; we start it but never stop it on unload.
        try:
            from utils.sysbot_telemetry import start_telemetry_worker, set_telemetry_discord_bot
            set_telemetry_discord_bot(self.bot)
            start_telemetry_worker()
            logger.info("[DropBot] Started SysBot telemetry worker and registered bot instance.")
        except Exception as exc:
            logger.warning("[DropBot] Could not start telemetry worker: %s", exc)
        # One registration covers every panel in every channel (the island is in the custom_id).
        self.bot.add_dynamic_items(*ALL_REGISTERED_DYNAMIC_ITEMS)
        self.auto_refresh_panels_loop.start()
        logger.info(
            "[DropBot] Registered %d persistent button types and started auto-refresh loop.",
            len(ALL_REGISTERED_DYNAMIC_ITEMS),
        )

    async def cog_unload(self) -> None:
        self.auto_refresh_panels_loop.cancel()
        self.bot.remove_dynamic_items(*ALL_REGISTERED_DYNAMIC_ITEMS)
        await _close_http()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await _reply(interaction, embed=_err_embed("You need the **Manage Messages** permission to use this command."))
            return
        logger.error("[DropBot] Command error: %s", error, exc_info=error)
        await _reply(interaction, embed=_err_embed("Something went wrong running that command."))

    # ── Auto-refresh ──────────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def auto_refresh_panels_loop(self) -> None:
        """Refresh all active panels in parallel; skip edits when nothing visible changed."""
        panels = await asyncio.to_thread(_load_all_panels)
        panels = [
            p for p in panels
            if p.get("channel_id") and p.get("message_id") and p.get("island_name")
            and str(p["channel_id"]) not in self._dead_panels
        ]
        if not panels:
            return
        sem = asyncio.Semaphore(REFRESH_CONCURRENCY)
        await asyncio.gather(*(self._refresh_one(p, sem) for p in panels), return_exceptions=True)

    async def _refresh_one(self, p: dict, sem: asyncio.Semaphore) -> None:
        cid = str(p["channel_id"])
        try:
            channel_id, message_id = int(cid), int(p["message_id"])
        except (TypeError, ValueError):
            return
        island = p["island_name"]

        async with sem:
            try:
                status = await fetch_island_live_status(island)
                embed = _build_panel_embed(island, status)
                sig = _embed_signature(embed)
                now = time.monotonic()
                prev = self._sigs.get(cid)

                is_running = bool(status.get("is_running", True)) if status else True
                raw_dodo = (status.get("dodo_code") or "").strip().upper() if status else ""
                is_valid_dodo = bool(raw_dodo and raw_dodo not in _INVALID_DODO_CODES) if status else True
                dodo_state = (is_running, is_valid_dodo)

                if prev and prev[0] == sig and (len(prev) > 2 and prev[2] == dodo_state) and now - prev[1] < FORCE_EDIT_SECONDS:
                    return

                partial = self.bot.get_partial_messageable(channel_id).get_partial_message(message_id)
                kwargs: dict[str, Any] = {"embed": embed}
                if prev is None or (len(prev) > 2 and prev[2] != dodo_state):
                    # First edit or whenever dodo availability state flips: re-attach the updated view
                    kwargs["view"] = IslandPanelView(island, status)
                await partial.edit(**kwargs)
                self._sigs[cid] = (sig, now, dodo_state)
            except discord.NotFound:
                self._dead_panels.add(cid)
                logger.warning(
                    "[DropBot] Panel message for %s (channel %s) is gone; will stop refreshing it. "
                    "Run /dropbot setup-panel there to repost.", island, cid,
                )
            except discord.HTTPException as exc:
                logger.debug("[DropBot] HTTP error updating panel for %s: %s", island, exc)
            except Exception as exc:
                logger.warning("[DropBot] Unexpected error auto-refreshing panel for %s: %s", island, exc, exc_info=exc)

    @auto_refresh_panels_loop.before_loop
    async def before_auto_refresh_panels_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── Shared: post a panel in a channel ─────────────────────────────────────

    async def _post_panel(self, channel: discord.TextChannel, island: str) -> discord.Message:
        existing = await asyncio.to_thread(_get_panel_row, channel.id)
        if existing and existing.get("message_id"):
            with contextlib.suppress(Exception):
                await (await channel.fetch_message(int(existing["message_id"]))).delete()

        status_data = await fetch_island_live_status(island)
        embed = _build_panel_embed(island, status_data)
        panel_msg = await channel.send(embed=embed, view=IslandPanelView(island_name=island, status_data=status_data))
        await asyncio.to_thread(_upsert_panel, channel.id, island, panel_msg.id)
        self._dead_panels.discard(str(channel.id))
        self._sigs.pop(str(channel.id), None)
        return panel_msg

    # ── Slash command group ───────────────────────────────────────────────────

    dropbot_group = app_commands.Group(
        name="dropbot",
        description="Island DropBot control panels and catalog search",
        guild_only=True,
    )

    async def island_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = (current or "").strip().lower()
        islands = getattr(Config, "SUB_ISLANDS", []) or []
        if not cur:
            return [app_commands.Choice(name=i, value=i) for i in islands[:25]]
        return [app_commands.Choice(name=i, value=i) for i in islands if cur in i.lower()][:25]

    async def item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Real-time autocomplete for orderable item drops."""
        cur = (current or "").strip()
        if not cur:
            try:
                catalog = ACNHCatalog.get_instance()
                popular = catalog.get_popular_item_names()
                return [app_commands.Choice(name=f"📦 {p}", value=p) for p in popular[:25]]
            except Exception:
                return []

        try:
            catalog = ACNHCatalog.get_instance()
            hits = await asyncio.to_thread(lambda: catalog.search_items(cur, limit=25))
            choices: list[app_commands.Choice[str]] = []
            seen: set[str] = set()
            for it in hits:
                name = it.display_name[:100]
                if name not in seen:
                    seen.add(name)
                    cat = f" ({it.category})" if it.category else ""
                    emoji = "📜" if it.diy else "📦"
                    label = f"{emoji} {name}{cat}"[:100]
                    choices.append(app_commands.Choice(name=label, value=name))
            return choices[:25]
        except Exception as exc:
            logger.warning("[DropBot] item_autocomplete error: %s", exc)
            return []

    async def villager_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Real-time autocomplete for villager injection."""
        cur = (current or "").strip()
        if not cur:
            try:
                catalog = ACNHCatalog.get_instance()
                popular_v = catalog.get_popular_villagers()
                return [app_commands.Choice(name=f"{v.emoji} {v.name}", value=v.name) for v in popular_v[:25]]
            except Exception:
                return []

        try:
            catalog = ACNHCatalog.get_instance()
            hits = await asyncio.to_thread(lambda: catalog.search_villagers(cur, limit=25))
            choices: list[app_commands.Choice[str]] = []
            seen: set[str] = set()
            for v in hits:
                name = v.display_name[:100]
                if name not in seen:
                    seen.add(name)
                    details = f" • {v.species}" if v.species else ""
                    label = f"🐱 {name}{details}"[:100]
                    choices.append(app_commands.Choice(name=label, value=name))
            return choices[:25]
        except Exception as exc:
            logger.warning("[DropBot] villager_autocomplete error: %s", exc)
            return []

    async def catalog_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete choices for item and villager catalog search."""
        if not current or len(current) < 2:
            return []
        try:
            catalog = ACNHCatalog.get_instance()
            item_hits, villager_hits = await asyncio.to_thread(
                lambda: (catalog.search_items(current, limit=18), catalog.search_villagers(current, limit=7))
            )

            choices: list[app_commands.Choice[str]] = []
            seen: set[str] = set()
            for item in item_hits:
                label = item.display_name[:100]
                if label not in seen:
                    seen.add(label)
                    choices.append(app_commands.Choice(name=f"📦 {label}"[:100], value=label))
            for v in villager_hits:
                label = v.display_name[:100]
                if label not in seen:
                    seen.add(label)
                    choices.append(app_commands.Choice(name=f"🐱 {label}"[:100], value=label))
            return choices[:25]
        except Exception as exc:
            logger.warning("[DropBot] catalog_autocomplete error: %s", exc)
            return []

    # ── /dropbot setup-panel ───────────────────────────────────────────────────

    @dropbot_group.command(name="setup-panel", description="Post or refresh the DropBot control panel in this channel")
    @app_commands.describe(island="Island name (auto-detected from channel name if omitted)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(island=island_autocomplete)
    async def setup_panel(self, interaction: discord.Interaction, island: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ Panels can only be posted in regular text channels.", ephemeral=True)
            return

        if island:
            canonical = _canonical_island(island)
            if not canonical:
                valid = ", ".join(Config.SUB_ISLANDS)
                if len(valid) > 1500:
                    valid = valid[:1500] + "…"
                await interaction.followup.send(
                    f"❌ **{island[:50]}** isn't a sub island.\nValid islands: {valid}", ephemeral=True
                )
                return
            island = canonical
        else:
            island = await _infer_island(channel)

        if not island:
            await interaction.followup.send(
                "❌ Could not auto-detect the island from this channel name.\n"
                "Pass it explicitly: `/dropbot setup-panel island:Hiraya`",
                ephemeral=True,
            )
            return

        panel_msg = await self._post_panel(channel, island)
        await interaction.followup.send(
            f"✅ DropBot panel for **{island}** posted in <#{channel.id}> (msg `{panel_msg.id}`).",
            ephemeral=True,
        )
        logger.info("[DropBot] setup-panel: island=%s channel=%s msg=%s by %s",
                    island, channel.id, panel_msg.id, interaction.user)

    # ── /dropbot setup-all ─────────────────────────────────────────────────────

    @dropbot_group.command(name="setup-all", description="Bulk-deploy DropBot panels to all sub-island channels")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def setup_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Must be used inside a server.", ephemeral=True)
            return

        category_id = _sub_category_id()
        if not category_id:
            await interaction.followup.send(
                "❌ No category is configured. Set `SUB_CATEGORY_ID` (or `CATEGORY_ID`) in `.env`.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(category_id)
        if not category:
            with contextlib.suppress(Exception):
                category = await guild.fetch_channel(category_id)

        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(f"❌ Could not find a category with ID `{category_id}`.", ephemeral=True)
            return

        results: list[str] = []
        for channel in category.text_channels:
            island = await _infer_island(channel)
            if not island:
                results.append(f"⏭️ `#{channel.name}` — no island match, skipped")
                continue
            try:
                await self._post_panel(channel, island)
                results.append(f"✅ `#{channel.name}` → **{island}**")
                logger.info("[DropBot] setup-all: deployed %s in #%s", island, channel.name)
            except discord.Forbidden:
                results.append(f"❌ `#{channel.name}` — missing permissions")
            except Exception as exc:
                results.append(f"❌ `#{channel.name}` — {exc}")

            await asyncio.sleep(0.6)   # avoid Discord rate-limits

        summary = "\n".join(results) or "No channels found in category."
        if len(summary) > 1900:
            summary = summary[:1900] + "\n…(truncated)"
        await interaction.followup.send(f"**DropBot Setup All:**\n{summary}", ephemeral=True)

    # ── /dropbot status ────────────────────────────────────────────────────────

    @dropbot_group.command(name="status", description="List all registered DropBot panels")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        panels = await asyncio.to_thread(_load_all_panels)
        if not panels:
            await interaction.followup.send(
                "No panels registered yet. Run `/dropbot setup-panel` or `/dropbot setup-all`.",
                ephemeral=True,
            )
            return

        lines = [
            f"• <#{p['channel_id']}> → **{p['island_name']}** (msg `{p.get('message_id') or '—'}`)"
            + (" ⚠️ message missing — rerun setup-panel" if str(p["channel_id"]) in self._dead_panels else "")
            for p in panels
        ]
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "\n…(truncated)"
        await interaction.followup.send(f"**Registered DropBot Panels ({len(panels)}):**\n{body}", ephemeral=True)

    # ── /dropbot catalog & /catalog ────────────────────────────────────────────

    async def _handle_catalog_search(self, interaction: discord.Interaction, query: str, island: str | None) -> None:
        denied = await _access_denied(interaction.user, require_order_rules=True)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
            if not await _user_can_target(interaction, target_island):
                await _reply(
                    interaction,
                    embed=_err_embed("You can only use an island you have access to. Use this in that island's channel."),
                )
                return
        else:
            target_island = await _infer_island(interaction.channel)

        query_str = query.strip()
        # Clean prefix if chosen from autocomplete (📦 or 🐱)
        if query_str.startswith(("📦 ", "🐱 ")):
            query_str = query_str[2:].strip()

        if not query_str:
            await interaction.response.send_modal(CatalogSearchModal(island_name=target_island))
            return

        await _show_catalog(interaction, target_island, query_str)

    @dropbot_group.command(
        name="catalog",
        description="Search the ACNH catalog — browse items, drop items, or inject villagers",
    )
    @app_commands.describe(
        query="Item or villager name to search for",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(query=catalog_autocomplete, island=island_autocomplete)
    async def dropbot_catalog(
        self,
        interaction: discord.Interaction,
        query: str = "",
        island: str | None = None,
    ) -> None:
        await self._handle_catalog_search(interaction, query, island)

    @app_commands.command(
        name="catalog",
        description="Search the ACNH catalog — browse items, drop items, or inject villagers",
    )
    @app_commands.describe(
        query="Item or villager name to search for",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(query=catalog_autocomplete, island=island_autocomplete)
    async def slash_catalog(
        self,
        interaction: discord.Interaction,
        query: str = "",
        island: str | None = None,
    ) -> None:
        await self._handle_catalog_search(interaction, query, island)

    # ── /dropbot drop & /drop ──────────────────────────────────────────────────

    async def _handle_drop_cmd(
        self,
        interaction: discord.Interaction,
        item: str | None,
        quantity: int | None,
        island: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user, require_order_rules=True)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
        else:
            target_island = await _infer_island(interaction.channel)

        if not target_island:
            await _reply(
                interaction,
                embed=_err_embed("Please specify a target island or use this command inside a sub-island channel."),
            )
            return

        if not await _user_can_target(interaction, target_island):
            await _reply(
                interaction,
                embed=_err_embed(f"You don't have access to **{target_island}**. Use the command in that island's channel."),
            )
            return

        # If no item is specified, open the interactive drop picker view!
        if not item or not item.strip():
            embed = discord.Embed(
                title=f"📦 Drop Items — {target_island}",
                description=(
                    f"Choose an item preset from the menu, browse or search the catalog, or enter custom hex codes.\n\n"
                    f"🏝️ **Target Island:** [{target_island}]({_island_url(target_island)})\n"
                    f"• Pick an item preset from the dropdown for instant dropping\n"
                    f"• Use **🔍 Search Catalog** to search 6,000+ items and build a cart\n"
                    f"• Use **📝 Custom / Hex** to enter your own items or hex list"
                ),
                color=PANEL_EMBED_COLOR,
            )
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            view = InteractiveDropPickerView(island_name=target_island, user_id=interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        clean_item = item.strip()
        if clean_item.startswith(("📦 ", "📜 ")):
            clean_item = clean_item[2:].strip()
        if " (" in clean_item and clean_item.endswith(")"):
            clean_item = clean_item.split(" (")[0].strip()

        if quantity is None:
            try:
                catalog = ACNHCatalog.get_instance()
                it_obj = catalog.get_item(clean_item)
                quantity = min(int(it_obj.stack_size or 1), MAX_STACK) if it_obj else 1
            except Exception:
                quantity = 1

        quantity = max(1, min(quantity, MAX_STACK))
        drop_str = _format_item_for_sysbot(clean_item, quantity)

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, _ = await _perform_drop(
            interaction.user,
            target_island,
            drop_str,
            heading="📦 Drop queued",
            contents=f"{clean_item} x{quantity}",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @dropbot_group.command(
        name="drop",
        description="Drop items on a sub-island with live search autocomplete",
    )
    @app_commands.describe(
        item="Item name to drop (search autocomplete)",
        quantity="Quantity to drop (default: max stack size)",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(item=item_autocomplete, island=island_autocomplete)
    async def dropbot_drop(
        self,
        interaction: discord.Interaction,
        item: str | None = None,
        quantity: int | None = None,
        island: str | None = None,
    ) -> None:
        await self._handle_drop_cmd(interaction, item, quantity, island)

    @app_commands.command(
        name="drop",
        description="Drop items on a sub-island with live search autocomplete",
    )
    @app_commands.describe(
        item="Item name to drop (search autocomplete)",
        quantity="Quantity to drop (default: max stack size)",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(item=item_autocomplete, island=island_autocomplete)
    async def slash_drop(
        self,
        interaction: discord.Interaction,
        item: str | None = None,
        quantity: int | None = None,
        island: str | None = None,
    ) -> None:
        await self._handle_drop_cmd(interaction, item, quantity, island)

    # ── /dropbot inject & /inject ──────────────────────────────────────────────

    async def _handle_inject_cmd(
        self,
        interaction: discord.Interaction,
        villager: str | None,
        plot: int | None,
        island: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user, require_order_rules=True)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
        else:
            target_island = await _infer_island(interaction.channel)

        if not target_island:
            await _reply(
                interaction,
                embed=_err_embed("Please specify a target island or use this command inside a sub-island channel."),
            )
            return

        if not await _user_can_target(interaction, target_island):
            await _reply(
                interaction,
                embed=_err_embed(f"You don't have access to **{target_island}**. Use the command in that island's channel."),
            )
            return

        # If no villager is specified, open the interactive villager picker view!
        if not villager or not villager.strip():
            embed = discord.Embed(
                title=f"🐱 Inject Villager — {target_island}",
                description=(
                    f"Select a popular villager from the dropdown below, search the catalog, or enter any name.\n\n"
                    f"🏝️ **Target Island:** [{target_island}]({_island_url(target_island)})\n"
                    f"• Choose a villager from the menu to inject\n"
                    f"• Use **🔍 Search Villager** to search by name or species\n"
                    f"• Use **🎀 Sanrio / Amiibo** for special Sanrio villager injections\n"
                    f"• Use **✏️ Type Any Name** to enter any name manually"
                ),
                color=PANEL_EMBED_COLOR,
            )
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            view = InteractiveVillagerPickerView(island_name=target_island, user_id=interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        clean_v = villager.strip()
        if clean_v.startswith("🐱 "):
            clean_v = clean_v[2:].strip()
        if " • " in clean_v:
            clean_v = clean_v.split(" • ")[0].strip()

        if plot is not None and not (PLOT_MIN <= plot <= PLOT_MAX):
            await _reply(interaction, embed=_err_embed(f"Plot must be between {PLOT_MIN} and {PLOT_MAX}."))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        exact, candidates = await asyncio.to_thread(_match_villagers, clean_v)

        if exact:
            if is_sanrio_villager(exact):
                canonical = get_canonical_sanrio_name(exact)
                wizard = SanrioInjectionWizardView(
                    island_name=target_island,
                    user_id=interaction.user.id,
                    target_villager=canonical,
                )
                await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)
                return

            embed, _ = await _perform_injection(interaction.user, target_island, exact, plot)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if not candidates:
            await interaction.followup.send(
                embed=_err_embed(f"I couldn't find a villager called **{clean_v[:64]}**. Check the spelling and try again."),
                ephemeral=True,
            )
            return

        view = VillagerChoiceView(interaction.user.id, target_island, candidates, plot)
        prompt = discord.Embed(
            title="Did you mean…?",
            description=(
                f"I couldn't find **{clean_v[:64]}** exactly.\n"
                + (
                    f"Did you mean **{candidates[0]}**?"
                    if len(candidates) == 1
                    else "Pick the villager you meant from the list."
                )
            ),
            color=PANEL_EMBED_COLOR_WARN,
        )
        prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=prompt, view=view, ephemeral=True)

    @dropbot_group.command(
        name="inject",
        description="Inject a villager into a sub-island with live search autocomplete",
    )
    @app_commands.describe(
        villager="Villager name to inject (search autocomplete)",
        plot=f"Plot number ({PLOT_MIN}–{PLOT_MAX}, optional)",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def dropbot_inject(
        self,
        interaction: discord.Interaction,
        villager: str | None = None,
        plot: int | None = None,
        island: str | None = None,
    ) -> None:
        await self._handle_inject_cmd(interaction, villager, plot, island)

    @app_commands.command(
        name="inject",
        description="Inject a villager into a sub-island with live search autocomplete",
    )
    @app_commands.describe(
        villager="Villager name to inject (search autocomplete)",
        plot=f"Plot number ({PLOT_MIN}–{PLOT_MAX}, optional)",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def slash_inject(
        self,
        interaction: discord.Interaction,
        villager: str | None = None,
        plot: int | None = None,
        island: str | None = None,
    ) -> None:
        await self._handle_inject_cmd(interaction, villager, plot, island)

    # ── /dropbot sanrio & /sanrio ──────────────────────────────────────────────

    async def _handle_sanrio_cmd(
        self,
        interaction: discord.Interaction,
        island: str | None,
        villager: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user, require_order_rules=True)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
        else:
            target_island = await _infer_island(interaction.channel)

        if not target_island:
            await _reply(
                interaction,
                embed=_err_embed("Please specify a target island or use this command inside a sub-island channel."),
            )
            return

        if not await _user_can_target(interaction, target_island):
            await _reply(
                interaction,
                embed=_err_embed(f"You don't have access to **{target_island}**. Use the command in that island's channel."),
            )
            return

        target_v = "Chai"
        if villager and villager.strip():
            clean_v = villager.strip()
            if clean_v.startswith("🐱 "):
                clean_v = clean_v[2:].strip()
            if " • " in clean_v:
                clean_v = clean_v.split(" • ")[0].strip()
            exact, _ = await asyncio.to_thread(_match_villagers, clean_v)
            target_v = exact or clean_v

        saved = await asyncio.to_thread(_load_sanrio_wizard_state, interaction.user.id)
        if saved and saved.get("step1_done") and (not villager or not villager.strip()):
            target_island = saved.get("island") or target_island
            target_v = saved.get("target_villager") or target_v
            placeholder_v = saved.get("placeholder_villager") or "Raymond"
            wizard = SanrioInjectionWizardView(
                island_name=target_island,
                user_id=interaction.user.id,
                target_villager=target_v,
                placeholder_villager=placeholder_v,
                step1_done=True,
            )
            wizard.last_status = "✈️ **Resumed Session (Step 2 Ready):** Pick up right where you left off!"
        else:
            wizard = SanrioInjectionWizardView(
                island_name=target_island,
                user_id=interaction.user.id,
                target_villager=target_v,
            )
        wizard._orig_interaction = interaction
        await interaction.response.send_message(embed=wizard.build_embed(), view=wizard, ephemeral=True)

    @dropbot_group.command(
        name="sanrio",
        description="Interactive 2-step wizard to inject Sanrio or Amiibo villagers",
    )
    @app_commands.describe(
        villager="Target Sanrio/Amiibo villager (e.g. Chai, Chelsea, Marty, Étoile)",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def dropbot_sanrio(
        self,
        interaction: discord.Interaction,
        villager: str | None = None,
        island: str | None = None,
    ) -> None:
        await self._handle_sanrio_cmd(interaction, island, villager)

    @app_commands.command(
        name="sanrio",
        description="Interactive 2-step wizard to inject Sanrio or Amiibo villagers",
    )
    @app_commands.describe(
        villager="Target Sanrio/Amiibo villager (e.g. Chai, Chelsea, Marty, Étoile)",
        island="Target island (auto-detected from channel if omitted)",
    )
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def slash_sanrio(
        self,
        interaction: discord.Interaction,
        villager: str | None = None,
        island: str | None = None,
    ) -> None:
        await self._handle_sanrio_cmd(interaction, island, villager)