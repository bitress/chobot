"""
DropBot Cog — Integrated island control panels for Discord.

Provides persistent, interaction-only UI panels in sub-island channels:
  • 🦤 Get Dodo        – fetches the live Dodo from SysBot, sends it ephemerally, logs to webhook
  • 📦 Drop Items      – modal for custom item/hex code drops (validated)
  • 🔍 Search Catalog  – browse the ACNH catalog, build a drop cart (pick item colors/patterns,
                         which re-computes the dropped hex, or set a custom hex), inject villagers
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
from functools import lru_cache
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

PANEL_EMBED_COLOR      = 0xF80778   # ChoPaeng pink (#f80778)
PANEL_EMBED_COLOR_WARN = 0xFEE75C   # Yellow
PANEL_EMBED_COLOR_ERR  = 0xED4245   # Red
PANEL_FOOTER_TEXT = "ChoPaeng Camp™"
PANEL_FOOTER_ICON = "https://i.ibb.co/HryQtXz/ezgif-1-8a2d930032.gif"
DODO_GIF  = "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"
LOGO_URL  = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"
MAP_CDN_BASE = "https://cdn.chopaeng.com/maps"

ISLAND_THEMES: dict[str, str] = {
    "giliw": "Pink",
    "bahaghari": "Rainbow",
    "bituin": "Star",
    "alapaap": "Cloud",
    "kalangitan": "Sky",
    "marahuyo": "Enchanted",
    "likha": "Art & DIY",
    "bonita": "Catalog",
    "hiraya": "Dreams",
}

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
SANRIO_VILLAGER_CODES: dict[str, str] = {
    "chai": "villager:elp11",
    "chelsea": "villager:der10",
    "etoile": "villager:shp14",
    "étoile": "villager:shp14",
    "marty": "villager:cbr18",
    "rilla": "villager:gor11",
    "toby": "villager:rbt20",
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
SUB_RULES_CHANNEL_ID = getattr(Config, "SUB_RULES_CHANNEL_ID", 783677194576330792) or 783677194576330792
ORDER_RULES_ROLE_ID = getattr(Config, "ORDER_RULES_ROLE_ID", 1176191811438510231) or 1176191811438510231
GUILD_ID = getattr(Config, "GUILD_ID", 729590421478703135) or 729590421478703135


class Denied(str):
    """Structured access denial result with an identifiable error code and user-facing message."""
    code: str

    def __new__(cls, code: str, message: str):
        obj = super().__new__(cls, message)
        obj.code = code
        return obj

    @property
    def message(self) -> str:
        return str(self)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Denied):
            return self.code == other.code and str(self) == str(other)
        return str(self) == other or self.code == other

    def __hash__(self) -> int:
        return hash((self.code, str(self)))


def _build_sub_rules_msg() -> Denied:
    role_id = getattr(Config, "SUB_RULES_ROLE_ID", 1077997850165772398) or 1077997850165772398
    channel_id = 783677194576330792
    return Denied(
        "sub_rules",
        f"You need to **accept the sub rules** first before you can use the islands (role <@&{role_id}> required).\n"
        f"Please go to <#{channel_id}> to read and accept the sub rules, then come back and try again.",
    )


SUB_RULES_MSG: Denied = _build_sub_rules_msg()
ORDER_RULES_MSG: Denied = Denied(
    "order_rules",
    "You need to **accept the sub rules** first before you can drop items or inject villagers.",
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


def _pop_cooldown(user_id: int, island: str, action: str) -> None:
    """Remove an armed cooldown after an unattempted or immediately failed action."""
    _COOLDOWNS.pop((user_id, island, action), None)


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
            # Sanrio wizard resume state
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sanrio_wizard_state (
                    user_id TEXT NOT NULL,
                    island_name TEXT NOT NULL,
                    target_villager TEXT NOT NULL,
                    placeholder_villager TEXT NOT NULL,
                    step1_done INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER,
                    PRIMARY KEY (user_id, island_name)
                )
                """
            )
            with contextlib.suppress(Exception):
                conn.execute("CREATE INDEX IF NOT EXISTS idx_island_drops_log_island_id ON island_drops_log (island_name, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_villager_inj_island_id ON villager_injections_log (island_name, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_reveal_island_id ON dodo_reveal_messages (island_clean, id)")
            # Prune old logs (>30 days) and stale wizard states (>24h)
            with contextlib.suppress(Exception):
                conn.execute("DELETE FROM island_drops_log WHERE created_at < datetime('now', '-30 days')")
                conn.execute("DELETE FROM villager_injections_log WHERE created_at < datetime('now', '-30 days')")
                conn.execute("DELETE FROM sanrio_wizard_state WHERE updated_at < (strftime('%s', 'now') - 86400)")
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


def _panel_channel_ids_cached(island: str) -> list[int]:
    now = time.monotonic()
    hit = _PANEL_CHANNELS_CACHE.get(island)
    if hit and now - hit[0] < 60:
        return hit[1]
    res = _panel_channel_ids(island)
    _PANEL_CHANNELS_CACHE[island] = (now, res)
    return res


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


def build_drop_payload(entries: list[tuple[str, int]]) -> str:
    """Build a space-separated SysBot hex/item drop string with NO COMMAS."""
    if not entries:
        return ""
    try:
        from utils.data_manager import resolve_drop_items
        formatted_entries = [f"{item} {qty}" if qty > 1 else item for item, qty in entries]
        hex_str, _ = resolve_drop_items(formatted_entries)
        if hex_str:
            return hex_str
    except Exception:
        pass

    parts: list[str] = []
    for item, qty in entries:
        clean = item.strip()
        try:
            from utils.sysbot_api import sanitize_single_drop_item
            sanitized = sanitize_single_drop_item(clean)
            if sanitized:
                parts.extend([sanitized] * qty)
                continue
        except Exception:
            pass
        parts.extend([clean] * qty)
    return " ".join(parts)


def _format_item_for_sysbot(item_name: str, qty: int) -> str:
    """Format an item for SysBot/NHSE using hex codes.
    Repeats the hex code for quantity, space-separated with NO commas.
    """
    return build_drop_payload([(item_name, qty)])


def _normalize_items(entries: list[tuple[str, int]]) -> str:
    """Normalize drop items into space-separated hex codes with NO COMMAS."""
    return build_drop_payload(entries)


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
) -> int | None:
    try:
        with connect_db() as conn:
            clean = re.sub(r"[^a-zA-Z0-9]", "", island_name.lower())
            cur = conn.execute(
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
            return getattr(cur, "lastrowid", None)
    except Exception as exc:
        logger.warning("[DropBot] Could not persist dodo_reveal_message: %s", exc)
        return None


def _update_dodo_reveal_url(row_id: int, message_url: str) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                "UPDATE dodo_reveal_messages SET message_url = ? WHERE id = ?",
                (message_url, row_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Could not update dodo_reveal_url: %s", exc)


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
        # Write DB audit row FIRST so the reveal is logged even if the webhook fails
        row_id = await asyncio.to_thread(
            _persist_dodo_reveal_message, None, str(user_id), island_name, channel_id, username, display_name
        )

        url = _dodo_webhook_url()
        if not url:
            logger.warning("[DropBot] No webhook URL configured for Dodo reveals! (Audit logged in DB)")
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
        if msg_url and row_id:
            await asyncio.to_thread(_update_dodo_reveal_url, row_id, msg_url)
        logger.info("[DropBot] Dodo reveal webhook sent for %s by user %s", island_name, user_id)
    except Exception as exc:
        logger.warning("[DropBot] Dodo webhook failed: %s", exc)


# ─── Live island status ────────────────────────────────────────────────────────

_ISLAND_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
_PANEL_CHANNELS_CACHE: dict[str, tuple[float, list[int]]] = {}
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
    clean = _clean_dodo(dodo)
    if client_status:
        if client_status.get("is_dodo_valid") is False:
            is_valid_dodo = False
        else:
            is_valid_dodo = bool(clean and client_status.get("is_dodo_valid", True))
    else:
        is_valid_dodo = bool(clean)

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

    res = {
        "island_name": canonical,
        "is_running": is_running,
        "is_dodo_valid": is_valid_dodo,
        "dodo_code": clean if is_valid_dodo else dodo,
        "console_stage": console_stage,
        "game_time": game_time,
        "is_nooks_open": is_nooks_open,
        "mode": mode,
        "villagers": villagers,
        "visitors": visitors,
        "monitoring_outage": not (client_status or api_item or telem),
    }
    _ISLAND_STATUS_CACHE[canonical_key] = (time.monotonic(), res)
    return res


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


_MAP_CACHE: dict[str, tuple[float, str]] = {}


async def _map_url_async(island: str, ttl: float = 3600) -> str:
    slug = re.sub(r"[^a-z0-9]", "", island.lower())
    hit = _MAP_CACHE.get(slug)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    for ext in (".jpg", ".png", ".jpeg"):
        url = f"{MAP_CDN_BASE}/{slug}{ext}"
        try:
            async with _http().head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=2.0)) as r:
                if r.status == 200:
                    _MAP_CACHE[slug] = (time.monotonic(), url)
                    return url
        except Exception:
            continue
    fallback = f"{MAP_CDN_BASE}/{slug}.jpg"
    _MAP_CACHE[slug] = (time.monotonic() - ttl + 60, fallback)   # retry in a minute
    return fallback


_PRESENCE_LINE_CACHE: dict[tuple[int, str], tuple[float, str]] = {}


async def _presence_line(user: discord.abc.User | None, island: str) -> str:
    """Check presence up front, cached for 15s to avoid spamming the status endpoint."""
    if not user:
        return ""
    now = time.monotonic()
    key = (user.id, island.lower())
    cached = _PRESENCE_LINE_CACHE.get(key)
    if cached and now < cached[0]:
        return cached[1]
    from utils.island_presence import verify_user_on_island_async
    try:
        ok, _, _ = await verify_user_on_island_async(island, user=user, allow_admin_bypass=True)
        line = "✅ You're on the island." if ok else "✈️ Not detected yet. Fly in and step outside the airport first."
    except Exception:
        line = ""
    _PRESENCE_LINE_CACHE[key] = (now + 15.0, line)
    return line


async def _build_panel_embed(island_name: str, status_data: dict | None = None) -> discord.Embed:
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
    else:
        is_running    = False
        is_valid_dodo = False
        villagers     = []
        visitors      = []
        game_time     = None
        is_nooks_open = False
        mode          = None
        color         = PANEL_EMBED_COLOR

    theme_name = ISLAND_THEMES.get(island_name.lower(), island_name)
    embed = discord.Embed(
        title=f"{'🟢' if is_running else '🔴'} {island_name} Map",
        url=url,
        description=f"🏝 **{theme_name} Island** · [Island page]({url})",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(name="Status", value="🟢 Online" if is_running else "🔴 Offline", inline=True)
    embed.add_field(
        name="Dodo",
        value="✅ Ready" if is_valid_dodo else ("⏳ Refreshing" if is_running else "❌ Bot Offline"),
        inline=True,
    )
    embed.add_field(name="Island time", value=game_time or "—", inline=True)

    v_count = len(visitors)
    visitor_cap = status_data.get("visitor_cap") if status_data else None
    visitor_title = (
        f"🧳 Visitors ({v_count}/{visitor_cap})" if visitor_cap
        else f"🧳 Visitors ({v_count})"
    )
    embed.add_field(name=f"🏡 Villagers ({len(villagers)}/10)", value=_bullets(villagers, 10), inline=True)
    embed.add_field(name=visitor_title, value=_bullets(visitors, 8), inline=True)
    embed.add_field(name="\u200b", value=f"-# Updated <t:{int(time.time())}:R> · 📖 Guide for help", inline=False)

    # Map image as thumbnail (upper right)
    embed.set_thumbnail(url=await _map_url_async(island_name))

    footer_parts = [PANEL_FOOTER_TEXT]
    if is_nooks_open:
        footer_parts.append("🛒 Nook's Open")
    if mode:
        footer_parts.append(f"Mode: {mode}")
    embed.set_footer(text=" • ".join(footer_parts), icon_url=PANEL_FOOTER_ICON)
    return embed


def _embed_signature(embed: discord.Embed) -> str:
    """Stable fingerprint of an embed ignoring its timestamp and freshness subtext, to skip no-op edits."""
    d = embed.to_dict()
    d.pop("timestamp", None)
    fields = d.get("fields", [])
    filtered_fields = []
    for f in fields:
        val = f.get("value", "")
        if "Updated <t:" in val:
            val = re.sub(r"Updated <t:\d+:R>", "Updated", val)
            filtered_fields.append({**f, "value": val})
        else:
            filtered_fields.append(f)
    d["fields"] = filtered_fields
    return json.dumps(d, sort_keys=True)


def _build_guide_embed(island: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📖 DropBot Guide — {island}",
        description=(
            "Everything on this panel is **private to you** — results only show up for you.\n"
            f"Island page: [{island} on chopaeng.com]({_island_url(island)})\n\n"
            "Click any of the buttons below the island panel to interact with DropBot:"
        ),
        color=PANEL_EMBED_COLOR,
    )
    embed.add_field(
        name="🛫 How to get in",
        value=(
            f"1. Click **🦤 Get Dodo** to view the live Dodo code.\n"
            f"2. Fly via Dodo Airlines at your airport.\n"
            f"3. Step outside the airport gate on [{island}]({_island_url(island)}) to activate presence.\n"
            f"4. Use **📦 Drop Items** or **🔍 Search Catalog** to queue drops!"
        ),
        inline=False,
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
            "Pick an item in the cart menu to **change its color/pattern** (the hex updates "
            "automatically) or set a **custom hex**. "
            "Use the category menu to narrow results. Your cart survives new searches."
        ),
        inline=False,
    )
    embed.add_field(
        name="🐱 Inject Villager",
        value=(
            f"Enter a villager's name and, optionally, a plot ({PLOT_MIN}–{PLOT_MAX}). "
            "Adds a villager to your island's next open plot. "
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


def _err_embed(text: str, *, title: str = "Something's off", hint: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=f"❌ {title}", description=text, color=PANEL_EMBED_COLOR_ERR)
    if hint:
        embed.add_field(name="What to do", value=hint, inline=False)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


async def build_drop_picker_embed(island: str, user: discord.abc.User | None = None) -> discord.Embed:
    p_line = await _presence_line(user, island) if user else ""
    desc = (
        f"Choose an item preset from the menu, browse or search the catalog, or enter custom hex codes.\n\n"
        f"🏝️ **Target Island:** [{island}]({_island_url(island)})\n"
    )
    if p_line:
        desc += f"📍 **Presence:** {p_line}\n\n"
    desc += (
        f"• Pick an item preset from the dropdown for instant dropping\n"
        f"• Use **🔍 Search Catalog** to search 6,000+ items and build a cart\n"
        f"• Use **📝 Custom / Hex** to enter your own items or hex list\n"
        f"• *Inject Villager adds a villager to your island's next open plot.*"
    )
    embed = discord.Embed(
        title=f"📦 Drop Items — {island}",
        description=desc,
        color=PANEL_EMBED_COLOR,
    )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


def build_villager_picker_embed(island: str) -> discord.Embed:
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
    return embed


class SubRulesPromptView(discord.ui.View):
    """Link button directly to the #sub-rules channel."""

    def __init__(self):
        super().__init__(timeout=None)
        guild_id = getattr(Config, "GUILD_ID", 729590421478703135) or 729590421478703135
        channel_id = getattr(Config, "SUB_RULES_CHANNEL_ID", 783677194576330792) or 783677194576330792
        self.add_item(
            discord.ui.Button(
                label="Go to #sub-rules",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/{guild_id}/{channel_id}",
                emoji="📜",
            )
        )


async def _reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    """Ephemeral reply that works before AND after interaction.response was used/deferred."""
    kwargs: dict[str, Any] = {"ephemeral": ephemeral}
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


async def _send(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    content: str | None = None,
    ephemeral: bool = True,
) -> None:
    """Convenience helper for ephemeral responses that stores orig_interaction on OwnedViews."""
    if view and isinstance(view, _OwnedView):
        view._orig_interaction = interaction
    await _reply(interaction, content, embed=embed, view=view, ephemeral=ephemeral)


def _sanitize_error_message(err: Any) -> str:
    s = str(err or "").strip()
    s = re.sub(r"https?://[^\s/$.?#].[^\s]*", "<redacted-url>", s)
    s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<redacted-host>", s)
    s = re.sub(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?\b", "<redacted-host>", s, flags=re.IGNORECASE)
    if any(keyword in s.lower() for keyword in ("connectionrefused", "clientconnecterror", "oserror", "socket", "traceback")):
        return "Connection to the island bot timed out or was refused."
    return s[:200]


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
    """Quick check: is this island's bot currently running? Uses cached status snapshot."""
    canonical = _canonical_island(island) or island
    key = _clean_key(canonical)
    hit = _ISLAND_STATUS_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 60.0:
        return bool(hit[1].get("is_running", True))
    try:
        status = await fetch_island_live_status(island)
        return bool(status.get("is_running", True))
    except Exception:
        return True  # fail open


async def _perform_drop(
    user: discord.abc.User,
    island_raw: str,
    items_or_entries: list[tuple[str, int]] | str,
    *,
    heading: str,
    contents: str,
) -> tuple[discord.Embed, bool]:
    """Returns (embed to show, whether the drop was queued)."""
    denied = await _access_denied(user)
    if denied:
        return _err_embed(denied), False
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG), False

    # Precheck: tell user immediately if bot is offline
    if not await _check_island_online(island):
        _pop_cooldown(user.id, island, "drop")
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
        return _err_embed(
            f"You can drop again {_cooldown_eta_ts(wait)}.",
            title="Cooldown Active",
            hint=f"Drops have a short cooldown. Try again {_cooldown_eta_ts(wait)}.",
        ), False

    # Presence check: verify user is physically on the island before dropping
    from utils.island_presence import verify_user_on_island_async
    is_present, matched_ign, current_visitors = await verify_user_on_island_async(
        island, user=user, allow_admin_bypass=True
    )
    if not is_present:
        _pop_cooldown(user.id, island, "drop")
        embed = discord.Embed(
            title="✈️ Airport Gate Verification Failed",
            description=(
                f"You must be on **[{island}]({_island_url(island)})** before dropping items!\n\n"
                f"I have checked the island, but your character was **not detected** on {island}.\n\n"
                f"• Please fly to **{island}** first and step outside the airport gates.\n"
                f"• Make sure your server nickname or profile character matches your in-game name (IGN)."
            ),
            color=PANEL_EMBED_COLOR_ERR,
        )
        if current_visitors:
            embed.add_field(
                name="Current Visitors On Island",
                value=f"```{', '.join(current_visitors)[:300]}```",
                inline=False,
            )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    if isinstance(items_or_entries, list):
        sysbot_payload = build_drop_payload(items_or_entries)
    else:
        parsed, _ = parse_drop_items(items_or_entries)
        if parsed:
            sysbot_payload = build_drop_payload(parsed)
        else:
            sysbot_payload = items_or_entries

    try:
        client = get_island_sysbot_client(island)
        result = await client.submit_drop(items=sysbot_payload, username=getattr(user, "display_name", user.name))
    except Exception as exc:
        _pop_cooldown(user.id, island, "drop")
        err_msg = str(exc) or "bot did not respond"
        logger.exception("[DropBot] Drop failed island=%s user=%s: %s", island, user.id, err_msg)
        await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, False, err_msg)

        embed = discord.Embed(
            title="⚠️ Drop Failed — Bot Offline",
            description=f"The island bot for **[{island}]({_island_url(island)})** didn't respond.",
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.add_field(name="Reason", value=f"```{_sanitize_error_message(err_msg)}```", inline=False)
        embed.add_field(name="Attempted Items", value=f"```{contents[:300]}```", inline=False)
        embed.add_field(name="Status", value="Please make sure the island bot is running and connected.", inline=False)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    if _is_failure(result):
        _pop_cooldown(user.id, island, "drop")
        err = (result or {}).get("error") if isinstance(result, dict) else None
        err = err or "unknown error"
        logger.warning("[DropBot] Drop rejected island=%s user=%s: %s", island, user.id, err)
        await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, False, str(err))

        embed = discord.Embed(
            title="⚠️ Drop Request Rejected",
            description=f"Your drop request on **[{island}]({_island_url(island)})** was not accepted by the bot.",
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.add_field(name="Error Details", value=f"```{_sanitize_error_message(err)}```", inline=False)
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
    denied = await _access_denied(user)
    if denied:
        return _err_embed(denied), False
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG), False

    # Precheck: tell user immediately if bot is offline
    if not await _check_island_online(island):
        _pop_cooldown(user.id, island, cooldown_key)
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
        return _err_embed(
            f"You can inject again {_cooldown_eta_ts(wait)}.",
            title="Cooldown Active",
            hint=f"Injections have a short cooldown. Try again {_cooldown_eta_ts(wait)}.",
        ), False

    display_name = villager
    villager_code = villager
    if is_sanrio_villager(villager):
        display_name = get_canonical_sanrio_name(villager)
        villager_code = SANRIO_VILLAGER_CODES.get(villager.strip().lower(), villager)
    else:
        try:
            from utils.data_manager import resolve_villager_code
            villager_code = resolve_villager_code(villager)
        except Exception:
            pass

    try:
        client = get_island_sysbot_client(island)
        result = await client.inject_villagers(
            villager_code,
            plot=plot,
            allow_unadoptable=is_sanrio_villager(villager),
        )
    except Exception:
        _pop_cooldown(user.id, island, cooldown_key)
        logger.exception("[DropBot] Injection failed island=%s user=%s", island, user.id)
        await asyncio.to_thread(_log_injection, island, display_name, plot, user.id, False, "bot did not respond")
        return _err_embed("The island bot didn't respond. Check that it's online and try again."), False

    if _is_failure(result):
        _pop_cooldown(user.id, island, cooldown_key)
        err: str | None = None
        if isinstance(result, dict):
            err = result.get("error")
            if not err and result.get("errors"):
                errs = result.get("errors")
                err = errs[0] if isinstance(errs, list) and errs else str(errs)
            if not err:
                err = result.get("message")
        err = err or "unknown error"
        logger.warning("[DropBot] Injection rejected island=%s user=%s: %s", island, user.id, err)
        await asyncio.to_thread(_log_injection, island, display_name, plot, user.id, False, str(err))
        if "not adoptable" in str(err).lower() and is_sanrio_villager(villager):
            return _err_embed(
                f"The island bot rejected **{display_name}**: `{_sanitize_error_message(err)}`.\n"
                f"The island bot's SysBot binary must be updated/restarted with Sanrio support enabled."
            ), False
        return _err_embed(f"The injection was rejected: **{_sanitize_error_message(err)}**\nMake sure the island bot is online."), False

    await asyncio.to_thread(
        _log_injection, island, display_name, plot, user.id, True,
        str(result.get("message") or "Queued via Discord panel"),
    )

    desc = f"**{display_name}** is queued for injection into **[{island}]({_island_url(island)})**.\n"
    if is_sanrio_villager(villager):
        desc += "Enter Plot 1's house on the island to invite them to move to your island! 🎀"
    else:
        desc += "You **must be on the island** for the injection to take effect."

    embed = discord.Embed(
        title="🐱 Villager injection queued",
        description=desc,
        color=PANEL_EMBED_COLOR,
    )
    if plot is not None:
        embed.add_field(name="Requested plot", value=str(plot), inline=True)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed, True


async def _start_injection(
    interaction: discord.Interaction,
    island_name: str,
    villager: str,
    plot: int | None = None,
) -> None:
    """Unified entry point for all villager injections.
    
    Checks is_sanrio_villager first. If Sanrio, launches the hot-swap wizard
    (ignoring the plot argument because hot-swap uses Plot 1).
    Otherwise executes the regular injection.
    """
    if is_sanrio_villager(villager):
        canonical = get_canonical_sanrio_name(villager)
        saved = await asyncio.to_thread(_load_sanrio_wizard_state, interaction.user.id, island_name)
        step1_done = False
        placeholder = "Raymond"
        if saved and saved.get("target_villager", "").lower() == canonical.lower():
            step1_done = bool(saved.get("step1_done"))
            placeholder = saved.get("placeholder_villager") or "Raymond"
        wizard = SanrioInjectionWizardView(
            island_name=island_name,
            user_id=interaction.user.id,
            target_villager=canonical,
            placeholder_villager=placeholder,
            step1_done=step1_done,
        )
        if step1_done:
            wizard.last_status = "✈️ **Resumed Session (Step 2 Ready):** Pick up right where you left off!"
        embed = wizard.build_embed()
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed, view=wizard)
        elif interaction.type == discord.InteractionType.component:
            await interaction.response.edit_message(content=None, embed=embed, view=wizard)
        else:
            await interaction.response.send_message(embed=embed, view=wizard, ephemeral=True)
        return

    embed, _ = await _perform_injection(interaction.user, island_name, villager, plot)
    if interaction.response.is_done():
        await interaction.edit_original_response(content=None, embed=embed, view=None)
    elif interaction.type == discord.InteractionType.component:
        await interaction.response.edit_message(content=None, embed=embed, view=None)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.wizard_view.user_id,
            self.wizard_view.island_name,
            self.wizard_view.target_villager,
            self.wizard_view.placeholder_villager,
            self.wizard_view.step1_done,
        )
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
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.wizard_view.user_id,
            self.wizard_view.island_name,
            self.wizard_view.target_villager,
            self.wizard_view.placeholder_villager,
            self.wizard_view.step1_done,
        )
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
                INSERT INTO sanrio_wizard_state
                    (user_id, island_name, target_villager, placeholder_villager, step1_done, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, island_name) DO UPDATE SET
                    target_villager=excluded.target_villager,
                    placeholder_villager=excluded.placeholder_villager,
                    step1_done=excluded.step1_done,
                    updated_at=excluded.updated_at
                """,
                (str(user_id), island, target, placeholder, 1 if step1_done else 0, int(time.time())),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Could not save sanrio wizard state: %s", exc)


def _load_sanrio_wizard_state(user_id: int, island: str | None = None) -> dict[str, Any] | None:
    try:
        with connect_db() as conn:
            if island:
                row = conn.execute(
                    """
                    SELECT island_name, target_villager, placeholder_villager, step1_done, updated_at
                    FROM sanrio_wizard_state
                    WHERE user_id = ? AND island_name = ?
                    """,
                    (str(user_id), island),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT island_name, target_villager, placeholder_villager, step1_done, updated_at
                    FROM sanrio_wizard_state
                    WHERE user_id = ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (str(user_id),),
                ).fetchone()

            if row:
                updated_at = row["updated_at"]
                ts = 0.0
                if isinstance(updated_at, (int, float)):
                    ts = float(updated_at)
                elif isinstance(updated_at, str):
                    try:
                        ts = datetime.fromisoformat(updated_at).timestamp()
                    except Exception:
                        ts = float(updated_at) if updated_at.isdigit() else 0.0
                if ts and (time.time() - ts > 7200):
                    _clear_sanrio_wizard_state(user_id, row["island_name"])
                    return None
                return {
                    "island": row["island_name"],
                    "island_name": row["island_name"],
                    "target_villager": row["target_villager"],
                    "placeholder_villager": row["placeholder_villager"],
                    "step1_done": bool(row["step1_done"]),
                    "updated_at": ts or time.time(),
                }
    except Exception as exc:
        logger.warning("[DropBot] Could not load sanrio wizard state: %s", exc)
    return None


def _clear_sanrio_wizard_state(user_id: int, island: str | None = None) -> None:
    try:
        with connect_db() as conn:
            if island:
                conn.execute(
                    "DELETE FROM sanrio_wizard_state WHERE user_id = ? AND island_name = ?",
                    (str(user_id), island),
                )
            else:
                conn.execute(
                    "DELETE FROM sanrio_wizard_state WHERE user_id = ?",
                    (str(user_id),),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Could not clear sanrio wizard state: %s", exc)


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
        self._interaction = interaction
        return True

    async def on_timeout(self) -> None:
        for c in self.children:
            if hasattr(c, "item") and hasattr(c.item, "disabled"):
                c.item.disabled = True
            elif hasattr(c, "disabled"):
                c.disabled = True
        target = self._interaction or self._orig_interaction
        if target:
            with contextlib.suppress(Exception):
                await target.edit_original_response(
                    content="⌛ This menu expired. Press the button on the panel to open it again.",
                    view=self,
                )
        elif hasattr(self, "message") and self.message:
            with contextlib.suppress(Exception):
                await self.message.edit(
                    content="⌛ This menu expired. Press the button on the panel to open it again.",
                    view=self,
                )


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
        self.presence_warned = False
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
        elif self.step1_done and not self.step2_done and getattr(self, "presence_warned", False):
            force_btn = discord.ui.Button(
                label="⚠️ Confirm On Island, Inject",
                style=discord.ButtonStyle.danger,
                row=2,
            )
            force_btn.callback = self._on_force_step2
            self.add_item(force_btn)

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
        if not self.step1_done and not self.step2_done:
            s1, s2, s3, s4 = "👉", "⬜", "⬜", "⬜"
        elif self.step1_done and not self.step2_done:
            s1, s2, s3, s4 = "✅", "👉", "⬜", "⬜"
        else:
            s1, s2, s3, s4 = "✅", "✅", "✅", "👉"

        tracker = (
            f"{s1} **1.** Inject {self.placeholder_villager} → Plot 1\n"
            f"{s2} **2.** Fly to {self.island_name}\n"
            f"{s3} **3.** Inject {self.target_villager} (live)\n"
            f"{s4} **4.** Talk to Plot 1 resident"
        )

        embed = discord.Embed(
            title=f"🎀 Sanrio & Amiibo Injection Wizard — {self.island_name}",
            description=(
                f"{tracker}\n\n"
                f"⚠️ **Important:** Don't leave Plot 1 empty or move the placeholder out before Step 2."
            ),
            color=SANRIO_EMBED_COLOR,
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
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            True,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_target_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        val = vals[0] if vals else ""
        if not val:
            return
        if val == "custom":
            await interaction.response.send_modal(CustomAmiiboModal(self))
            return
        self.target_villager = val
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            self.step1_done,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_placeholder_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        val = vals[0] if vals else ""
        if not val:
            return
        if val == "custom":
            await interaction.response.send_modal(CustomPlaceholderModal(self))
            return
        self.placeholder_villager = val
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
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
            await asyncio.to_thread(
                _save_sanrio_wizard_state,
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
        from utils.island_presence import verify_user_on_island_async

        is_admin = False
        guild_perms = getattr(interaction.user, "guild_permissions", None)
        if guild_perms and getattr(guild_perms, "administrator", None) is True:
            is_admin = True

        is_present, _, _ = await verify_user_on_island_async(
            self.island_name, user=interaction.user, allow_admin_bypass=True
        )
        if not is_present and not is_admin and not getattr(self, "presence_warned", False):
            self.presence_warned = True
            self.last_status = (
                f"✈️ **Not detected on {self.island_name} yet!**\n"
                f"Please fly into **{self.island_name}** and step outside the airport gates before running Step 2.\n"
                f"*If you are already outside the airport gates, click **⚠️ Confirm On Island, Inject**.*"
            )
            self._rebuild_items()
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
            return

        embed_res, ok = await _perform_injection(
            interaction.user,
            self.island_name,
            self.target_villager,
            plot=1,
            cooldown_key="sanrio2",
        )
        if ok:
            self.step2_done = True
            await asyncio.to_thread(_clear_sanrio_wizard_state, self.user_id, self.island_name)
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

    async def _on_force_step2(self, interaction: discord.Interaction) -> None:
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
            await asyncio.to_thread(_clear_sanrio_wizard_state, self.user_id, self.island_name)
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
        embed = build_villager_picker_embed(self.island_name)
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
            await _start_injection(interaction, self.island_name, exact, plot)
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
                options=[discord.SelectOption(label=c[:100], value=c[:100]) for c in candidates[:25]],
                row=0,
            )
            select.callback = self._make_select_callback(select)  # type: ignore[assignment]
            self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel)

    async def _run(self, interaction: discord.Interaction, villager: str) -> None:
        self.stop()
        await _start_injection(interaction, self.island_name, villager, self.plot)

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
    hex_override: str | None = None   # hand-typed hex; wins over the catalog hex when dropping

    @property
    def drop_hex(self) -> str:
        return self.hex_override or self.item.to_hex() or self.item.internal_id or self.item.name


def _default_qty(it: CatalogItem) -> int:
    try:
        return max(1, min(int(it.stack_size or 1), MAX_STACK))
    except (TypeError, ValueError):
        return 1


def _cart_key(it: CatalogItem) -> str:
    """Key cart entries by internal hex code or ID:variant to avoid name collisions."""
    hex_code = it.to_hex()
    if hex_code:
        return hex_code
    if it.internal_id or it.variant_id or it.variation:
        return f"{it.internal_id}:{it.variant_id or it.variation or ''}"
    return f"it_{abs(hash((it.name, it.category))):08x}"


# ─── Item colours / patterns (mirrors the chopaeng.com Command Builder) ───────

_PATTERN_VARIANT_RE = re.compile(r"^\d+_\d+$")
_HEX_INPUT_RE = re.compile(r"^(?:[0-9A-F]{4}|[0-9A-F]{16})$")
_NO_PATTERN_LABEL = "Default"


@dataclass
class _Variant:
    item: CatalogItem   # a CatalogItem for exactly this colour/pattern (its to_hex() is the drop hex)
    body: str           # colour name, e.g. "Silver"
    pattern: str        # pattern name, "" when the variant has none


def _clean_variant_text(value: Any) -> str:
    s = str(value or "").strip()
    return "" if s.upper() in ("NA", "NONE") else s


def _item_variants(it: CatalogItem) -> list[_Variant]:
    """
    Every colour/pattern of `it`, or [] if it only comes in one look.
    Hex rules match the web Command Builder (generateFullItemHex):
      • pattern variants ("body_pattern", e.g. "1_3") share one internal id and are encoded
        into a 16-char hex (0000<variant>0000<id>, fences use their own layout);
      • colours with their own internal id (clothing, accessories…) drop as that 4-char id.
    """
    if it.category == "Recipes":
        return []
    raw = it.raw_data or {}
    variations = raw.get("variations") or []
    if len(variations) < 2:
        return []

    base_name = raw.get("name") or it.name
    out: list[_Variant] = []
    for v in variations:
        if not isinstance(v, dict):
            continue
        raw_id = v.get("internalId", raw.get("internalId"))
        if raw_id is None:
            continue
        try:
            internal_id = f"{int(raw_id):04X}"
        except (TypeError, ValueError):
            internal_id = str(raw_id).strip().upper().zfill(4)

        body = _clean_variant_text(v.get("variation"))
        pattern = _clean_variant_text(v.get("pattern"))
        label = f"{body} / {pattern}" if body and pattern else (body or pattern)
        vid = str(v.get("variantId") or "").strip()

        out.append(_Variant(
            item=CatalogItem(
                name=base_name,
                category=it.category,
                internal_id=internal_id,
                image_url=v.get("image") or v.get("storageImage") or v.get("closetImage") or it.image_url,
                variation=label,
                variant_id=vid if _PATTERN_VARIANT_RE.match(vid) else "NA",
                stack_size=it.stack_size,
                diy=it.diy,
                raw_data=raw,
            ),
            body=body or _NO_PATTERN_LABEL,
            pattern=pattern,
        ))
    return out if len(out) > 1 else []


def _match_variant_index(it: CatalogItem, variants: list[_Variant]) -> int:
    """Index of the variant `it` currently represents (0 if it can't be told apart)."""
    hx = it.to_hex()
    for i, v in enumerate(variants):
        if v.item.to_hex() == hx:
            return i
    cur = (it.variation or "").strip().lower()
    for i, v in enumerate(variants):
        if v.item.internal_id == it.internal_id and v.item.variation.lower() == cur:
            return i
    for i, v in enumerate(variants):
        if v.item.internal_id == it.internal_id:
            return i
    return 0


def _unique(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))


class ItemHexModal(discord.ui.Modal, title="#️⃣ Custom Item Hex"):
    """Override the hex that an item drops as. Blank resets to the catalog hex."""

    def __init__(self, item_view: "ItemDetailView"):
        super().__init__()
        self.item_view = item_view
        current = item_view.drop_hex
        self.hex_input = discord.ui.TextInput(
            label="Hex ID to drop (4 or 16 characters)",
            placeholder="e.g. 0B64 or 0000002000003A5E (blank = reset)",
            default=current or None,
            max_length=24,
            required=False,
        )
        self.add_item(self.hex_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = re.sub(r"[\s_\-]", "", self.hex_input.value or "").upper()
        if raw.startswith("0X"):
            raw = raw[2:]
        catalog_hex = (self.item_view.current_item.to_hex() or "").upper()
        if not raw or raw == catalog_hex:
            self.item_view.hex_override = None
            notice = f"↩️ Reset to catalog hex `{catalog_hex or self.item_view.drop_hex}`."
        elif not _HEX_INPUT_RE.match(raw):
            await _reply(interaction, embed=_err_embed(
                f"`{raw[:24]}` isn't a valid item hex.",
                hint="Use exactly **4** or **16** hex characters (0–9, A–F), e.g. `0B64` or `0000002000003A5E`.",
            ))
            return
        else:
            self.item_view.hex_override = raw
            notice = f"#️⃣ Custom drop hex set to `{raw}`."
        await self.item_view.refresh(interaction, notice)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] ItemHexModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong setting the hex code.")


class ItemDetailView(_OwnedView):
    """
    Focused Item Detail Card (ChoPaeng Command Builder style):
    - Live thumbnail & hex preview
    - Color dropdown & pattern dropdown
    - Quantity stepper: [ − ] [ ×qty ] [ + ] [ ×1 ] [ ×Max ]
    - Actions:
      ⚡ Drop Now (1-click drop without needing a cart)
      🛒 Add to Cart (or Update Cart)
      🔙 Back to Results
      #️⃣ Custom Hex
      🗑️ Remove (if editing an existing cart item)
    """

    def __init__(
        self,
        search_view: "CatalogSearchView",
        item: CatalogItem,
        *,
        cart_key: str | None = None,
        initial_qty: int | None = None,
        initial_hex_override: str | None = None,
    ):
        super().__init__(user_id=search_view.user_id, timeout=300)
        self.search_view = search_view
        self.base_item = item
        self.cart_key = cart_key
        self.variants: list[_Variant] = _item_variants(item)

        if self.variants:
            idx = _match_variant_index(item, self.variants)
            self.current_variant: _Variant | None = self.variants[idx]
            self.current_item: CatalogItem = self.variants[idx].item
        else:
            self.current_variant = None
            self.current_item = item

        if initial_qty is not None:
            self.qty = max(1, min(MAX_STACK, initial_qty))
        elif cart_key and cart_key in search_view.cart:
            self.qty = search_view.cart[cart_key].qty
        else:
            self.qty = _default_qty(self.current_item)

        if initial_hex_override is not None:
            self.hex_override: str | None = initial_hex_override
        elif cart_key and cart_key in search_view.cart:
            self.hex_override = search_view.cart[cart_key].hex_override
        else:
            self.hex_override = None

        self._rebuild()

    @property
    def drop_hex(self) -> str:
        return self.hex_override or self.current_item.to_hex() or self.current_item.internal_id or self.current_item.name

    @property
    def current_key(self) -> str:
        return _cart_key(self.current_item)

    @property
    def is_in_cart(self) -> bool:
        return (bool(self.cart_key and self.cart_key in self.search_view.cart) or
                (self.current_key in self.search_view.cart))

    @property
    def bodies(self) -> list[str]:
        return _unique([v.body for v in self.variants])

    @property
    def patterns(self) -> list[str]:
        return _unique([v.pattern for v in self.variants])

    def _find(self, body: str, pattern: str) -> _Variant | None:
        exact = next((v for v in self.variants if v.body == body and v.pattern == pattern), None)
        if exact:
            return exact
        return next((v for v in self.variants if v.body == body), None) if body else \
            next((v for v in self.variants if v.pattern == pattern), None)

    def _rebuild(self) -> None:
        self.clear_items()
        bodies, patterns = self.bodies, self.patterns
        cur = self.current_variant

        # Row 0: Color Select (if > 1 color)
        if len(bodies) > 1:
            opts = []
            for idx, body in enumerate(bodies[:25]):
                sample = next(v for v in self.variants if v.body == body)
                opts.append(discord.SelectOption(
                    label=body[:100],
                    value=str(idx),
                    description=(f"Hex: {sample.item.to_hex()}" if not sample.pattern or len(patterns) <= 1
                                 else f"{sum(1 for v in self.variants if v.body == body)} pattern(s)")[:100],
                    default=bool(cur and cur.body == body),
                ))
            sel = discord.ui.Select(
                placeholder=f"🎨 Color: {cur.body if cur else 'Pick color'}"[:100],
                options=opts,
                row=0,
            )
            sel.callback = self.on_body_select  # type: ignore[assignment]
            self.add_item(sel)

        # Row 1: Pattern Select (if > 1 pattern)
        if len(patterns) > 1:
            opts = []
            for idx, pat in enumerate(patterns[:25]):
                opts.append(discord.SelectOption(
                    label=(pat or _NO_PATTERN_LABEL)[:100],
                    value=str(idx),
                    default=bool(cur and cur.pattern == pat),
                ))
            sel = discord.ui.Select(
                placeholder=f"🖌️ Pattern: {(cur.pattern or _NO_PATTERN_LABEL) if cur else 'Pick pattern'}"[:100],
                options=opts,
                row=1,
            )
            sel.callback = self.on_pattern_select  # type: ignore[assignment]
            self.add_item(sel)

        # Row 2: Quantity buttons
        minus = discord.ui.Button(label="−", style=discord.ButtonStyle.secondary, disabled=self.qty <= 1, row=2)
        minus.callback = self.on_minus  # type: ignore[assignment]
        self.add_item(minus)

        self.add_item(discord.ui.Button(label=f"×{self.qty}", style=discord.ButtonStyle.secondary, disabled=True, row=2))

        plus = discord.ui.Button(label="+", style=discord.ButtonStyle.secondary, disabled=self.qty >= MAX_STACK, row=2)
        plus.callback = self.on_plus  # type: ignore[assignment]
        self.add_item(plus)

        one = discord.ui.Button(label="×1", style=discord.ButtonStyle.secondary, disabled=self.qty == 1, row=2)
        one.callback = self.on_qty_one  # type: ignore[assignment]
        self.add_item(one)

        mx = discord.ui.Button(label=f"×{MAX_STACK}", style=discord.ButtonStyle.secondary, disabled=self.qty >= MAX_STACK, row=2)
        mx.callback = self.on_qty_max  # type: ignore[assignment]
        self.add_item(mx)

        # Row 3: Action Buttons
        row_act = 3
        if self.search_view.island_name:
            drop_btn = discord.ui.Button(label=f"Drop Now (×{self.qty})", emoji="⚡", style=discord.ButtonStyle.success, row=row_act)
            drop_btn.callback = self.on_drop_now  # type: ignore[assignment]
            self.add_item(drop_btn)

            cart_label = "Update Cart" if self.is_in_cart else "Add to Cart"
            cart_btn = discord.ui.Button(label=cart_label, emoji="🛒", style=discord.ButtonStyle.primary, row=row_act)
            cart_btn.callback = self.on_add_to_cart  # type: ignore[assignment]
            self.add_item(cart_btn)

        back_btn = discord.ui.Button(label="Back to Results", emoji="🔙", style=discord.ButtonStyle.secondary, row=row_act)
        back_btn.callback = self.on_back_to_search  # type: ignore[assignment]
        self.add_item(back_btn)

        # Row 4: Tools
        row_tools = 4
        hex_btn = discord.ui.Button(label="Custom Hex", emoji="#️⃣", style=discord.ButtonStyle.secondary, row=row_tools)
        hex_btn.callback = self.on_custom_hex  # type: ignore[assignment]
        self.add_item(hex_btn)

        if self.hex_override:
            reset_btn = discord.ui.Button(label="Reset Hex", emoji="↩️", style=discord.ButtonStyle.secondary, row=row_tools)
            reset_btn.callback = self.on_reset_hex  # type: ignore[assignment]
            self.add_item(reset_btn)

        if self.is_in_cart:
            rm_btn = discord.ui.Button(label="Remove from Cart", emoji="🗑️", style=discord.ButtonStyle.danger, row=row_tools)
            rm_btn.callback = self.on_remove_from_cart  # type: ignore[assignment]
            self.add_item(rm_btn)

    def build_embed(self, notice: str | None = None) -> discord.Embed:
        it = self.current_item
        cur = self.current_variant
        lines = [
            f"**Item:** {it.name}",
            f"**Category:** `{it.category}`" + (" *(DIY Recipe)*" if it.diy else ""),
        ]
        if cur:
            lines.append(f"**Selected Color:** **{cur.body}**")
            if len(self.patterns) > 1:
                lines.append(f"**Pattern:** **{cur.pattern or _NO_PATTERN_LABEL}**")
        lines.append(f"**Quantity:** **×{self.qty}** (Stack size: {it.stack_size or 1})")

        hex_line = f"**Drop Hex:** `{self.drop_hex}`"
        if self.hex_override:
            hex_line += f" *(Custom override — catalog hex is `{it.to_hex() or '—'}`)*"
        lines.append(hex_line)

        if self.search_view.island_name:
            lines.append(f"🏝️ **Island:** [{self.search_view.island_name}]({_island_url(self.search_view.island_name)})")

        if self.is_in_cart:
            lines.append("🛒 *This item is in your drop cart.*")

        if self.search_view.island_name:
            lines.append("\n• **⚡ Drop Now**: Drops immediately near you on the island.")
            lines.append("• **🛒 Add to Cart**: Keeps this item in your cart while you browse.")

        desc = "\n".join(lines)
        if notice:
            desc = f"{notice}\n\n{desc}"

        embed = discord.Embed(
            title=f"📦 {it.display_name}",
            description=desc,
            color=PANEL_EMBED_COLOR,
        )
        if it.image_url:
            embed.set_thumbnail(url=it.image_url)

        if len(self.bodies) > 1:
            embed.add_field(
                name=f"🎨 Available Colors ({len(self.bodies)})",
                value=" · ".join(f"**▸{b}**" if cur and b == cur.body else b for b in self.bodies)[:1024],
                inline=False,
            )
        if len(self.patterns) > 1:
            embed.add_field(
                name=f"🖌️ Available Patterns ({len(self.patterns)})",
                value=" · ".join(
                    f"**▸{p or _NO_PATTERN_LABEL}**" if cur and p == cur.pattern else (p or _NO_PATTERN_LABEL)
                    for p in self.patterns
                )[:1024],
                inline=False,
            )

        n_cart = len(self.search_view.cart)
        embed.set_footer(
            text=f"{PANEL_FOOTER_TEXT} • Cart: {n_cart}/{MAX_DROP_SLOTS} slots",
            icon_url=PANEL_FOOTER_ICON,
        )
        return embed

    async def refresh(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(notice), view=self)

    async def on_body_select(self, interaction: discord.Interaction) -> None:
        val = (interaction.data or {}).get("values", ["0"])[0]
        bodies = self.bodies
        try:
            body = bodies[int(val)]
        except (ValueError, IndexError):
            await interaction.response.defer()
            return
        cur = self.current_variant
        variant = self._find(body, cur.pattern if cur else "")
        if variant:
            self.current_variant = variant
            self.current_item = variant.item
            self.hex_override = None
            if self.cart_key and self.cart_key in self.search_view.cart:
                self.search_view.cart.pop(self.cart_key, None)
                new_key = _cart_key(self.current_item)
                self.search_view.cart[new_key] = _CartEntry(self.current_item, self.qty, None)
                self.cart_key = new_key
            await self.refresh(interaction, f"🎨 Color changed to **{variant.body}**.")
        else:
            await interaction.response.defer()

    async def on_pattern_select(self, interaction: discord.Interaction) -> None:
        val = (interaction.data or {}).get("values", ["0"])[0]
        patterns = self.patterns
        try:
            pattern = patterns[int(val)]
        except (ValueError, IndexError):
            await interaction.response.defer()
            return
        cur = self.current_variant
        body = cur.body if cur else ""
        variant = next((v for v in self.variants if v.body == body and v.pattern == pattern), None) \
            or next((v for v in self.variants if v.pattern == pattern), None)
        if variant:
            self.current_variant = variant
            self.current_item = variant.item
            self.hex_override = None
            if self.cart_key and self.cart_key in self.search_view.cart:
                self.search_view.cart.pop(self.cart_key, None)
                new_key = _cart_key(self.current_item)
                self.search_view.cart[new_key] = _CartEntry(self.current_item, self.qty, None)
                self.cart_key = new_key
            pat_name = variant.pattern or _NO_PATTERN_LABEL
            await self.refresh(interaction, f"🖌️ Pattern changed to **{pat_name}**.")
        else:
            await interaction.response.defer()

    async def on_minus(self, interaction: discord.Interaction) -> None:
        self.qty = max(1, self.qty - 1)
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = self.qty
        await self.refresh(interaction)

    async def on_plus(self, interaction: discord.Interaction) -> None:
        self.qty = min(MAX_STACK, self.qty + 1)
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = self.qty
        await self.refresh(interaction)

    async def on_qty_one(self, interaction: discord.Interaction) -> None:
        self.qty = 1
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = 1
        await self.refresh(interaction)

    async def on_qty_max(self, interaction: discord.Interaction) -> None:
        self.qty = MAX_STACK
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = MAX_STACK
        await self.refresh(interaction)

    async def on_drop_now(self, interaction: discord.Interaction) -> None:
        if not self.search_view.island_name:
            await interaction.response.defer()
            return
        await interaction.response.defer(ephemeral=True)
        summary = f"{self.current_item.display_name} ×{self.qty}"
        drop_entries = [(self.drop_hex, self.qty)]
        embed, ok = await _perform_drop(
            interaction.user,
            self.search_view.island_name,
            drop_entries,
            heading="⚡ Quick Drop Queued",
            contents=summary,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_add_to_cart(self, interaction: discord.Interaction) -> None:
        cart = self.search_view.cart
        new_key = _cart_key(self.current_item)
        if self.cart_key and self.cart_key in cart and self.cart_key != new_key:
            cart.pop(self.cart_key, None)

        if len(cart) >= MAX_DROP_SLOTS and new_key not in cart:
            await self.refresh(interaction, f"⚠️ Cart is full ({MAX_DROP_SLOTS}/{MAX_DROP_SLOTS}). Drop or clear first.")
            return

        cart[new_key] = _CartEntry(self.current_item, self.qty, self.hex_override)
        self.cart_key = new_key
        notice = f"✅ Added **{self.current_item.display_name} ×{self.qty}** to your cart! ({len(cart)}/{MAX_DROP_SLOTS} slots)"
        await self.refresh(interaction, notice)

    async def on_back_to_search(self, interaction: discord.Interaction) -> None:
        self.stop()
        self.search_view._rebuild()
        await interaction.response.edit_message(embed=self.search_view.build_search_embed(), view=self.search_view)

    async def on_custom_hex(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ItemHexModal(self))

    async def on_reset_hex(self, interaction: discord.Interaction) -> None:
        self.hex_override = None
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].hex_override = None
        await self.refresh(interaction, "↩️ Reset to catalog hex.")

    async def on_remove_from_cart(self, interaction: discord.Interaction) -> None:
        cart = self.search_view.cart
        if self.cart_key and self.cart_key in cart:
            cart.pop(self.cart_key, None)
        elif self.current_key in cart:
            cart.pop(self.current_key, None)
        self.cart_key = None
        await self.refresh(interaction, f"🗑️ Removed **{self.current_item.display_name}** from cart.")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[DropBot] ItemDetailView error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


class CartManagerView(_OwnedView):
    """Clean Cart overview: lists all items, select one to edit/customize, or drop cart."""

    def __init__(self, search_view: "CatalogSearchView"):
        super().__init__(user_id=search_view.user_id, timeout=300)
        self.search_view = search_view
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        cart = self.search_view.cart

        if cart:
            opts = []
            for k, e in list(cart.items())[:25]:
                n_colors = len(_item_variants(e.item))
                desc = f"×{e.qty} • Hex: {e.drop_hex}"
                opts.append(
                    discord.SelectOption(
                        label=e.item.display_name[:100],
                        description=desc[:100],
                        value=k[:100],
                        emoji="🎨" if n_colors else "📦",
                    )
                )
            sel = discord.ui.Select(
                placeholder="Tap an item to change color, quantity, or remove…",
                options=opts,
                row=0,
            )
            sel.callback = self.on_select_item  # type: ignore[assignment]
            self.add_item(sel)

        n = len(cart)
        drop_btn = discord.ui.Button(label=f"Drop Cart ({n})", emoji="📦", style=discord.ButtonStyle.success, disabled=n == 0, row=1)
        drop_btn.callback = self.on_drop_cart  # type: ignore[assignment]
        self.add_item(drop_btn)

        clear_btn = discord.ui.Button(label="Clear Cart", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=n == 0, row=1)
        clear_btn.callback = self.on_clear_cart  # type: ignore[assignment]
        self.add_item(clear_btn)

        back_btn = discord.ui.Button(label="Back to Search", emoji="🔙", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.on_back  # type: ignore[assignment]
        self.add_item(back_btn)

    def build_embed(self, notice: str | None = None) -> discord.Embed:
        cart = self.search_view.cart
        lines = []
        if not cart:
            lines.append("*Your drop cart is empty. Pick items from the search catalog to add them here.*")
        else:
            for idx, (k, e) in enumerate(cart.items(), 1):
                custom_tag = " `(custom hex)`" if e.hex_override else ""
                lines.append(f"`{idx:02d}.` **{e.item.display_name}** ×{e.qty} — `{e.drop_hex}`{custom_tag}")

        if self.search_view.island_name:
            lines.append(f"\n🏝️ **Target island:** [{self.search_view.island_name}]({_island_url(self.search_view.island_name)})")

        desc = "\n".join(lines)
        if notice:
            desc = f"{notice}\n\n{desc}"

        embed = discord.Embed(
            title=f"🛒 Drop Cart ({len(cart)}/{MAX_DROP_SLOTS} Slots)",
            description=desc,
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=f"{PANEL_FOOTER_TEXT} • Tap an item above to customize", icon_url=PANEL_FOOTER_ICON)
        return embed

    async def on_select_item(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        if not values or values[0] not in self.search_view.cart:
            await interaction.response.defer()
            return
        key = values[0]
        entry = self.search_view.cart[key]
        view = ItemDetailView(
            self.search_view,
            entry.item,
            cart_key=key,
            initial_qty=entry.qty,
            initial_hex_override=entry.hex_override,
        )
        view._orig_interaction = self.search_view._orig_interaction
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_drop_cart(self, interaction: discord.Interaction) -> None:
        await self.search_view.on_drop_cart(interaction)

    async def on_clear_cart(self, interaction: discord.Interaction) -> None:
        self.search_view.cart.clear()
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed("🗑️ Cart cleared."), view=self)

    async def on_back(self, interaction: discord.Interaction) -> None:
        self.stop()
        self.search_view._rebuild()
        await interaction.response.edit_message(embed=self.search_view.build_search_embed(), view=self.search_view)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[DropBot] CartManagerView error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


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

    is_staff = False
    if isinstance(interaction.user, discord.Member):
        perms = interaction.user.guild_permissions
        is_staff = bool(perms.administrator or perms.manage_guild or perms.manage_messages)
    elif interaction.permissions and interaction.permissions.manage_messages:
        is_staff = True

    if parent is not None:
        parent.is_staff = is_staff
        parent.apply_search(query, items, villagers)
        await interaction.edit_original_response(embed=parent.build_search_embed(), view=parent)
        return

    view = CatalogSearchView(
        island_name=island,
        query=query,
        items=items,
        villagers=villagers,
        user_id=interaction.user.id,
        is_staff=is_staff,
    )
    view._orig_interaction = interaction
    if island:
        view._presence_status = await _presence_line(interaction.user, island)
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


class CatalogSearchView(_OwnedView):
    """
    Streamlined Catalog Search View:
    - Row 0: Tap an item to open ItemDetailView (color, quantity, drop now, add to cart)
    - Row 1: Category filter OR Villagers select (if matching query)
    - Row 2: Navigation (◀, 1/N, ▶, Search, Close)
    - Row 3: Cart bar (Drop Cart, View Cart, Clear) when cart has items
    """

    def __init__(
        self,
        island_name: str | None = None,
        query: str = "",
        items: list[CatalogItem] | None = None,
        villagers: list[CatalogVillager] | None = None,
        user_id: int | None = None,
        page_size: int = 10,
        is_staff: bool = False,
    ):
        super().__init__(user_id=user_id or 0, timeout=300)
        self.island_name = island_name
        self.query = query
        self.all_items: list[CatalogItem] = list(items or [])
        self.items: list[CatalogItem] = self.all_items
        self.villagers: list[CatalogVillager] = list(villagers or [])
        self._items_by_key: dict[str, CatalogItem] = {}
        for idx, i in enumerate(self.all_items):
            k = _cart_key(i) or f"item_{idx}"
            self._items_by_key[k] = i
        self.page_size = page_size
        self.page = 0
        self.category: str | None = None
        self.cart: dict[str, _CartEntry] = {}
        self.message: discord.Message | None = None
        self._busy = False
        self.is_staff = is_staff
        self._presence_status: str | None = None
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
        self._items_by_key = {}
        for idx, i in enumerate(self.all_items):
            k = _cart_key(i) or f"item_{idx}"
            self._items_by_key[k] = i
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
        page_items = self.page_items()
        counts = Counter(i.category for i in self.all_items if i.category)

        # Row 0: Items dropdown
        if page_items:
            options: list[discord.SelectOption] = []
            seen_values: set[str] = set()
            for idx, it in enumerate(page_items):
                bits: list[str] = []
                key = _cart_key(it)
                if not key or key in seen_values:
                    key = f"{key or 'item'}_{self.page}_{idx}"
                    self._items_by_key[key] = it
                seen_values.add(key)

                if key in self.cart or _cart_key(it) in self.cart:
                    bits.append("✅ In cart")
                bits.append(str(it.category or "Item"))
                bits.append(f"×{_default_qty(it)}")
                n_colors = len(_item_variants(it))
                if n_colors:
                    bits.append(f"🎨 {n_colors} colors")
                options.append(
                    discord.SelectOption(
                        label=it.display_name[:100],
                        description=" • ".join(bits)[:100],
                        value=key[:100],
                        emoji="📜" if it.diy else "📦",
                    )
                )
            placeholder = (
                "📦 Select item to customize color, qty, or drop…"
                if self.island_name
                else f"Select item to inspect (page {self.page + 1}/{self.total_pages})…"
            )
            item_select = discord.ui.Select(
                placeholder=placeholder[:100],
                min_values=1,
                max_values=min(len(options), MAX_DROP_SLOTS),
                options=options[:25],
                row=0,
            )
            item_select.callback = self.on_item_select  # type: ignore[assignment]
            self.add_item(item_select)

        # Row 1: Villagers (page 0) OR Category filter
        if self.villagers and self.page == 0:
            v_options = []
            seen_v: set[str] = set()
            for idx, v in enumerate(self.villagers[:10]):
                val = f"v_{v.villager_id}"
                if val in seen_v:
                    val = f"{val}_{idx}"
                seen_v.add(val)
                v_options.append(
                    discord.SelectOption(
                        label=v.display_name[:100],
                        description=f"ID: {v.villager_id} • {v.personality}"[:100],
                        value=val[:100],
                        emoji="🐱",
                    )
                )
            villager_select = discord.ui.Select(
                placeholder=(f"🐱 Inject a villager into {self.island_name}…" if self.island_name
                             else "Select villager to inspect…")[:100],
                min_values=1,
                max_values=1,
                options=v_options[:25],
                row=1,
            )
            villager_select.callback = self.on_villager_select  # type: ignore[assignment]
            self.add_item(villager_select)
        elif len(counts) > 1:
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
                placeholder="🗂️ Filter by category…", min_values=1, max_values=1, options=cat_options[:25], row=1
            )
            cat_select.callback = self.on_category_select  # type: ignore[assignment]
            self.add_item(cat_select)

        # Row 2: Navigation & Search
        prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page <= 0, row=2)
        prev_btn.callback = self.on_prev_page  # type: ignore[assignment]
        self.add_item(prev_btn)

        page_btn = discord.ui.Button(
            label=f"{self.page + 1}/{self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True, row=2
        )
        self.add_item(page_btn)

        next_btn = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1, row=2
        )
        next_btn.callback = self.on_next_page  # type: ignore[assignment]
        self.add_item(next_btn)

        search_btn = discord.ui.Button(label="Search", emoji="🔍", style=discord.ButtonStyle.primary, row=2)
        search_btn.callback = self.on_search_button  # type: ignore[assignment]
        self.add_item(search_btn)

        close_btn = discord.ui.Button(label="Close", emoji="✖", style=discord.ButtonStyle.secondary, row=2)
        close_btn.callback = self.on_close  # type: ignore[assignment]
        self.add_item(close_btn)

        # Row 3: Cart Actions (Only shown when cart has items)
        if self.island_name and self.cart:
            n = len(self.cart)
            drop_btn = discord.ui.Button(
                label=f"Drop Cart ({n})", emoji="📦", style=discord.ButtonStyle.success, row=3
            )
            drop_btn.callback = self.on_drop_cart  # type: ignore[assignment]
            self.add_item(drop_btn)

            view_cart_btn = discord.ui.Button(
                label="View Cart", emoji="🛒", style=discord.ButtonStyle.primary, row=3
            )
            view_cart_btn.callback = self.on_open_cart_manager  # type: ignore[assignment]
            self.add_item(view_cart_btn)

            clear_btn = discord.ui.Button(
                label="Clear", emoji="🗑️", style=discord.ButtonStyle.danger, row=3
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
            if self._presence_status:
                desc += f"\n📍 **Presence:** {self._presence_status}"
            desc += "\n*Tap an item below to pick color, stack size, and drop or add to cart.*"
        else:
            desc += "\n*Select an item below to inspect variations and hex codes.*"
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
                n_colors = len(_item_variants(it))
                color_badge = f" • *{n_colors} colors*" if n_colors > 1 else ""
                hex_code = it.to_hex()
                hex_part = f" • `{hex_code}`" if (hex_code and self.is_staff) else ""
                lines.append(f"`{i:02d}.` {cart_mark}**{it.display_name}**{diy_badge} — `{it.category}`{color_badge}{hex_part}")

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
            cart_summary = ", ".join(
                f"**{e.item.display_name}** ×{e.qty}" + (f" `#{e.hex_override}`" if e.hex_override else "")
                for e in self.cart.values()
            )
            embed.add_field(
                name=f"🛒 Drop Cart ({len(self.cart)}/{MAX_DROP_SLOTS} slots)",
                value=f"{cart_summary[:950]}\n*Press **Drop Cart** to drop all, or **View Cart** to customize.*",
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

    # ── item selection ──
    async def on_item_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        if not values:
            await interaction.response.defer()
            return

        chosen: list[CatalogItem] = []
        for val in values:
            it = self._items_by_key.get(val)
            if not it and val.startswith("item_"):
                try:
                    idx = int(val.replace("item_", ""))
                    if 0 <= idx < len(self.page_items()):
                        it = self.page_items()[idx]
                except ValueError:
                    pass
            if it:
                chosen.append(it)

        if not chosen:
            await interaction.response.defer()
            return

        # Single item selected → Open Item Detail Card immediately (ChoPaeng Command Builder style!)
        if len(chosen) == 1:
            it = chosen[0]
            cart_key = _cart_key(it)
            initial_qty = self.cart[cart_key].qty if cart_key in self.cart else _default_qty(it)
            initial_hex = self.cart[cart_key].hex_override if cart_key in self.cart else None
            detail_view = ItemDetailView(
                self,
                it,
                cart_key=cart_key if cart_key in self.cart else None,
                initial_qty=initial_qty,
                initial_hex_override=initial_hex,
            )
            detail_view._orig_interaction = self._orig_interaction
            await interaction.response.edit_message(embed=detail_view.build_embed(), view=detail_view)
            return

        # Multiple items selected → Batch add directly to cart
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
                notes.append(f"🛒 Added **{added}** items to your drop cart.")
            if already:
                notes.append(f"{already} already in cart.")
            if skipped:
                notes.append(f"⚠️ Cart full — {skipped} skipped.")
            await self._refresh(interaction, " ".join(notes))
            return

        # Inspect mode with multiple selections (no island)
        embed = discord.Embed(
            title=f"📦 Selected Items ({len(chosen)})",
            description="Catalog details and hex codes for your selection:",
            color=PANEL_EMBED_COLOR,
        )
        for it in chosen:
            diy_txt = " *(DIY Recipe)*" if it.diy else ""
            hex_code = it.to_hex() or it.internal_id or "N/A"
            value = f"• Category: `{it.category}`\n• Hex Code: `{hex_code}`\n• Stack Size: `{it.stack_size}`"
            variants = _item_variants(it)
            if variants:
                colors = ", ".join(f"{v.item.variation} `{v.item.to_hex()}`" for v in variants[:8])
                value += f"\n• Colors ({len(variants)}): {colors}"
            embed.add_field(
                name=f"{it.display_name}{diy_txt}",
                value=value[:1024],
                inline=False,
            )
        if chosen[0].image_url:
            embed.set_thumbnail(url=chosen[0].image_url)
        embed.set_footer(text="💡 Tip: Tap a single item to view its dedicated customizer.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── cart actions ──
    async def on_open_cart_manager(self, interaction: discord.Interaction) -> None:
        cart_view = CartManagerView(self)
        cart_view._orig_interaction = self._orig_interaction
        await interaction.response.edit_message(embed=cart_view.build_embed(), view=cart_view)

    async def on_clear_cart(self, interaction: discord.Interaction) -> None:
        self.cart.clear()
        await self._refresh(interaction, "🗑️ Cart cleared.")

    async def on_drop_cart(self, interaction: discord.Interaction) -> None:
        if not self.cart or not self.island_name or self._busy:
            await interaction.response.defer()
            return
        self._busy = True
        try:
            await interaction.response.defer()
            self._presence_status = await _presence_line(interaction.user, self.island_name)
            entries = list(self.cart.values())
            drop_entries = [(e.drop_hex, e.qty) for e in entries]
            summary = ", ".join(f"{e.item.display_name} ×{e.qty}" for e in entries)
            embed, ok = await _perform_drop(
                interaction.user,
                self.island_name,
                drop_entries,
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
        if not values:
            await interaction.response.defer()
            return
        val = values[0]
        v: CatalogVillager | None = None
        if val.startswith("v_"):
            vid = val[2:]
            v = next((x for x in self.villagers if str(x.villager_id) == vid), None)
        elif val.startswith("villager_"):
            try:
                idx = int(val.replace("villager_", ""))
                if 0 <= idx < len(self.villagers):
                    v = self.villagers[idx]
            except (ValueError, IndexError):
                pass

        if not v:
            await interaction.response.defer()
            return

        if self.island_name:
            if is_sanrio_villager(v.name):
                await _start_injection(interaction, self.island_name, v.name)
                return
            prompt = discord.Embed(
                title="Inject this villager?",
                description=f"**{v.name}** → **{self.island_name}** (next available plot)\n\n*Adds {v.name} to your island's next open plot.*",
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
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[DropBot] CatalogSearchView error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")

    async def on_timeout(self) -> None:
        await super().on_timeout()


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
        await interaction.response.defer()
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            self.last_drop,
            heading="📦 Drop queued",
            contents=self.display_name,
        )
        followup = DropFollowupView(self.island_name, self.user_id, self.last_drop, self.display_name)
        await interaction.edit_original_response(
            embed=embed,
            view=followup,
        )
        self.stop()

    @discord.ui.button(label="New Drop Menu", emoji="📦", style=discord.ButtonStyle.secondary)
    async def on_new_drop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picker = InteractiveDropPickerView(self.island_name, self.user_id)
        embed = await build_drop_picker_embed(self.island_name, user=interaction.user)
        await interaction.response.edit_message(embed=embed, view=picker)
        self.stop()


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
        await interaction.response.defer()
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            self.item_code,
            heading="📦 Drop queued",
            contents=self.label,
        )
        await interaction.edit_original_response(
            embed=embed,
            view=DropFollowupView(self.island_name, self.user_id, self.item_code, self.label),
        )
        self.stop()

    @discord.ui.button(label="Back", emoji="🔙", style=discord.ButtonStyle.secondary)
    async def on_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picker = InteractiveDropPickerView(self.island_name, self.user_id)
        embed = await build_drop_picker_embed(self.island_name, user=interaction.user)
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
        ][:25]
        if preset_options:
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
        vals = (interaction.data or {}).get("values", [])
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
            options=plot_options[:25],
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
        vals = (interaction.data or {}).get("values", ["none"])
        val = vals[0] if vals else "none"
        if val != "none" and val.isdigit():
            self.selected_plot = int(val)
        else:
            self.selected_plot = None
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def _on_inject(self, interaction: discord.Interaction) -> None:
        self.stop()
        await _start_injection(
            interaction, self.island_name, self.villager_name, self.selected_plot
        )

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
            await _start_injection(interaction, self.island_name, exact)
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
        v_options = v_options[:25]
        if v_options:
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
        vals = (interaction.data or {}).get("values", [])
        selected_name = vals[0] if vals else ""
        if not selected_name:
            return
        if is_sanrio_villager(selected_name):
            await _start_injection(interaction, self.island_name, selected_name)
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
        saved = await asyncio.to_thread(_load_sanrio_wizard_state, self.user_id, self.island_name)
        resumed = bool(saved and saved.get("step1_done"))
        wizard = SanrioInjectionWizardView(
            island_name=(saved or {}).get("island_name") or self.island_name,
            user_id=self.user_id,
            target_villager=(saved or {}).get("target_villager") or "Chai",
            placeholder_villager=(saved or {}).get("placeholder_villager") or "Raymond",
            step1_done=resumed,
        )
        if resumed:
            wizard.last_status = "✈️ **Resumed (Step 2 ready):** pick up where you left off."
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
        denied = await _access_denied(interaction.user)
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


_IN_FLIGHT_DODO_LOOKUPS: dict[str, asyncio.Future[str | None]] = {}


async def _find_dodo_coalesced(island: str, client: Any) -> str | None:
    """Coalesce concurrent Dodo lookups for the same island into a single in-flight task."""
    loop = asyncio.get_running_loop()
    if island in _IN_FLIGHT_DODO_LOOKUPS:
        fut = _IN_FLIGHT_DODO_LOOKUPS[island]
        return await fut

    fut = loop.create_future()
    _IN_FLIGHT_DODO_LOOKUPS[island] = fut
    try:
        res = await _find_dodo(island, client)
        if not fut.done():
            fut.set_result(res)
        return res
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _IN_FLIGHT_DODO_LOOKUPS.pop(island, None)


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

        cd = _cooldown_left(interaction.user.id, self.island, "dodo", 5.0)
        if cd > 0:
            await _reply(interaction, embed=_err_embed(f"⏳ Please wait **{cd:.1f}s** before requesting the Dodo code again."))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        resolved = await _resolve_client(interaction, self.island)
        if not resolved:
            _pop_cooldown(interaction.user.id, self.island, "dodo")
            return
        island, client = resolved

        dodo = await _find_dodo_coalesced(island, client)

        if not dodo:
            _pop_cooldown(interaction.user.id, self.island, "dodo")
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
                f"# `{dodo}`\n\n"
                f"**[Visit {island} Page]({_island_url(island)})**\n\n"
                "-# ⚠️ Private code • Walk to airport to leave • Character | Island nick • Don't litter"
            ),
            color=PANEL_EMBED_COLOR,
        )
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
            if island := await self._gate(interaction):
                embed = await build_drop_picker_embed(island, user=interaction.user)
                view = InteractiveDropPickerView(island_name=island, user_id=interaction.user.id)
                await _send(interaction, embed=embed, view=view, ephemeral=True)
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
        if island := await self._gate(interaction):
            await interaction.response.send_modal(CatalogSearchModal(island_name=island))


class InjectVillagerButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:villager:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "villager", "Inject Villager", "🐱"

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if island := await self._gate(interaction):
                embed = build_villager_picker_embed(island)
                view = InteractiveVillagerPickerView(island_name=island, user_id=interaction.user.id)
                await _send(interaction, embed=embed, view=view, ephemeral=True)
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
        embed = await _build_panel_embed(island, status_data)
        await interaction.edit_original_response(
            embed=embed,
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
        monitoring_outage = False
        if status_data:
            is_running = bool(status_data.get("is_running", True))
            monitoring_outage = bool(status_data.get("monitoring_outage", False))
            is_valid_dodo = bool(status_data.get("is_dodo_valid", True))

        for item_cls in PANEL_ITEMS:
            btn = item_cls(island_name)
            if item_cls is GetDodoButton:
                if not is_running and not monitoring_outage:
                    btn.item.disabled = True
                    btn.item.label = "Bot Offline"
                    btn.item.style = discord.ButtonStyle.secondary
                elif not is_valid_dodo and not monitoring_outage:
                    btn.item.disabled = True
                    btn.item.label = "Dodo not ready"
                    btn.item.style = discord.ButtonStyle.secondary
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

@dataclass
class _PanelSig:
    sig: str
    last_edit: float
    is_running: bool
    is_valid_dodo: bool


class DropBotCog(commands.Cog, name="DropBot"):
    """
    Manages persistent interactive island control panels across sub-island channels.
    No privileged intents required — operates entirely on Discord's Interaction API.
    Auto-refreshes panel messages in the background so users always see live status.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sigs: dict[str, _PanelSig] = {}   # channel_id -> _PanelSig
        self._dead_panels: set[str] = set()    # channel_ids whose panel message no longer exists

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
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden):
                        self._dead_panels.add(cid)
                        logger.warning(
                            "[DropBot] Channel %s no longer accessible; skipping panel refreshes for %s.",
                            cid, island,
                        )
                        return
                    except Exception as exc:
                        logger.warning("[DropBot] Could not fetch channel %s: %s", cid, exc)
                        return

                status = await fetch_island_live_status(island)
                embed = await _build_panel_embed(island, status)
                sig = _embed_signature(embed)
                now = time.monotonic()
                prev = self._sigs.get(cid)

                is_running = bool(status.get("is_running", True)) if status else True
                is_valid_dodo = bool(status.get("is_dodo_valid", True)) if status else True
                dodo_changed = (
                    prev is None
                    or prev.is_running != is_running
                    or prev.is_valid_dodo != is_valid_dodo
                )

                if prev and prev.sig == sig and not dodo_changed and now - prev.last_edit < FORCE_EDIT_SECONDS:
                    return

                view_to_attach = IslandPanelView(island, status) if (prev is None or dodo_changed) else None

                # 1. Edit known message id
                if message_id:
                    partial = channel.get_partial_message(message_id)
                    kwargs: dict[str, Any] = {"embed": embed}
                    if view_to_attach is not None:
                        kwargs["view"] = view_to_attach
                    try:
                        await partial.edit(**kwargs)
                        self._sigs[cid] = _PanelSig(sig, now, is_running, is_valid_dodo)
                        return
                    except discord.NotFound:
                        logger.warning(
                            "[DropBot] Panel message %s in %s not found; attempting recovery...",
                            message_id, cid,
                        )
                    except discord.HTTPException as exc:
                        if exc.status in (403, 429):
                            logger.warning(
                                "[DropBot] HTTP %s updating panel for %s in %s: %s",
                                exc.status, island, cid, exc,
                            )
                        else:
                            logger.warning("[DropBot] HTTP error updating panel for %s: %s", island, exc)
                        return

                # 2. Recover from recent channel history
                if hasattr(channel, "history"):
                    try:
                        bot_id = getattr(self.bot.user, "id", None)
                        async for candidate in channel.history(limit=25):
                            if (
                                candidate.author.id == bot_id
                                and candidate.embeds
                                and island.lower() in (candidate.embeds[0].title or "").lower()
                            ):
                                await candidate.edit(embed=embed, view=IslandPanelView(island, status))
                                self._sigs[cid] = _PanelSig(sig, now, is_running, is_valid_dodo)
                                await asyncio.to_thread(_upsert_panel, channel_id, island, candidate.id)
                                logger.info("[DropBot] Recovered panel message %s in channel %s for %s", candidate.id, cid, island)
                                return
                    except Exception as exc:
                        logger.debug("[DropBot] Error scanning history in %s: %s", cid, exc)

                # 3. Auto-repost fresh panel
                try:
                    msg = await channel.send(embed=embed, view=IslandPanelView(island, status))
                    self._sigs[cid] = _PanelSig(sig, now, is_running, is_valid_dodo)
                    await asyncio.to_thread(_upsert_panel, channel_id, island, msg.id)
                    logger.info("[DropBot] Auto-reposted panel message %s in channel %s for %s", msg.id, cid, island)
                except Exception as exc:
                    logger.warning("[DropBot] Failed to auto-repost panel in channel %s: %s", cid, exc)

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
        embed = await _build_panel_embed(island, status_data)
        panel_msg = await channel.send(embed=embed, view=IslandPanelView(island_name=island, status_data=status_data))
        await asyncio.to_thread(_upsert_panel, channel.id, island, panel_msg.id)
        self._dead_panels.discard(str(channel.id))
        self._sigs.pop(str(channel.id), None)
        return panel_msg

    # ── Slash command group ───────────────────────────────────────────────────

    dropbot_group = app_commands.Group(
        name="dropbot",
        description="Island DropBot control panels and catalog search",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    async def island_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = (current or "").strip().lower()
        islands = getattr(Config, "SUB_ISLANDS", []) or []
        user = interaction.user
        is_admin = False
        if isinstance(user, discord.Member):
            perms = user.guild_permissions
            is_admin = bool(perms.administrator or perms.manage_guild or perms.manage_messages)
        elif interaction.permissions and interaction.permissions.manage_messages:
            is_admin = True

        if is_admin:
            visible_islands = list(islands)
        else:
            current_island = await _infer_island(interaction.channel)
            guild, member = interaction.guild, interaction.user

            async def _check_island(isl: str) -> bool:
                if isl == current_island:
                    return True
                if not guild or not isinstance(member, discord.Member):
                    return False
                cids = await asyncio.to_thread(_panel_channel_ids_cached, isl)
                for cid in cids:
                    ch = guild.get_channel(cid)
                    if ch and ch.permissions_for(member).view_channel:
                        return True
                return False

            checks = await asyncio.gather(*[_check_island(i) for i in islands])
            visible_islands = [i for i, ok in zip(islands, checks) if ok]

        if not cur:
            return [app_commands.Choice(name=i, value=i) for i in visible_islands[:25]]
        return [app_commands.Choice(name=i, value=i) for i in visible_islands if cur in i.lower()][:25]

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

    # ── Command implementations ───────────────────────────────────────────────

    async def _handle_catalog_search(self, interaction: discord.Interaction, query: str, island: str | None) -> None:
        denied = await _access_denied(interaction.user)
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
        if query_str.startswith(("📦 ", "🐱 ")):
            query_str = query_str[2:].strip()

        if not query_str:
            await interaction.response.send_modal(CatalogSearchModal(island_name=target_island))
            return

        await _show_catalog(interaction, target_island, query_str)

    async def _handle_drop_cmd(
        self,
        interaction: discord.Interaction,
        item: str | None,
        quantity: int | None,
        island: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user)
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

        if not item or not item.strip():
            embed = await build_drop_picker_embed(target_island, user=interaction.user)
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, _ = await _perform_drop(
            interaction.user,
            target_island,
            [(clean_item, quantity)],
            heading="📦 Drop queued",
            contents=f"{clean_item} x{quantity}",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_inject_cmd(
        self,
        interaction: discord.Interaction,
        villager: str | None,
        plot: int | None,
        island: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user)
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

        if not villager or not villager.strip():
            embed = build_villager_picker_embed(target_island)
            view = InteractiveVillagerPickerView(island_name=target_island, user_id=interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        clean_v = villager.strip()
        if clean_v.startswith("🐱 "):
            clean_v = clean_v[2:].strip()
        if " • " in clean_v:
            clean_v = clean_v.split(" • ")[0].strip()

        await interaction.response.defer(ephemeral=True, thinking=True)
        exact, candidates = await asyncio.to_thread(_match_villagers, clean_v)

        if exact:
            await _start_injection(interaction, target_island, exact, plot)
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

    async def _handle_sanrio_cmd(
        self,
        interaction: discord.Interaction,
        island: str | None,
        villager: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user)
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

        saved = await asyncio.to_thread(_load_sanrio_wizard_state, interaction.user.id, target_island)
        if saved and saved.get("step1_done") and (not villager or not villager.strip()):
            target_island = saved.get("island_name") or target_island
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

    # ── Registered slash commands (/dropbot X and /X) ─────────────────────────

    @dropbot_group.command(name="catalog", description="Search the ACNH catalog — browse items, drop items, or inject villagers")
    @app_commands.describe(query="Item or villager name to search for", island="Target island (auto-detected from channel if omitted)")
    @app_commands.autocomplete(query=catalog_autocomplete, island=island_autocomplete)
    async def dropbot_catalog(self, interaction: discord.Interaction, query: str = "", island: str | None = None) -> None:
        await self._handle_catalog_search(interaction, query, island)

    @app_commands.command(name="catalog", description="Search the ACNH catalog — browse items, drop items, or inject villagers")
    @app_commands.describe(query="Item or villager name to search for", island="Target island (auto-detected from channel if omitted)")
    @app_commands.autocomplete(query=catalog_autocomplete, island=island_autocomplete)
    async def slash_catalog(self, interaction: discord.Interaction, query: str = "", island: str | None = None) -> None:
        await self._handle_catalog_search(interaction, query, island)

    @dropbot_group.command(name="drop", description="Drop items on a sub-island with live search autocomplete")
    @app_commands.describe(item="Item name to drop (search autocomplete)", quantity=f"Quantity (1–{MAX_STACK})", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(item=item_autocomplete, island=island_autocomplete)
    async def dropbot_drop(self, interaction: discord.Interaction, item: str | None = None, quantity: app_commands.Range[int, 1, MAX_STACK] | None = None, island: str | None = None) -> None:
        await self._handle_drop_cmd(interaction, item, quantity, island)

    @app_commands.command(name="drop", description="Drop items on a sub-island with live search autocomplete")
    @app_commands.describe(item="Item name to drop (search autocomplete)", quantity=f"Quantity (1–{MAX_STACK})", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(item=item_autocomplete, island=island_autocomplete)
    async def slash_drop(self, interaction: discord.Interaction, item: str | None = None, quantity: app_commands.Range[int, 1, MAX_STACK] | None = None, island: str | None = None) -> None:
        await self._handle_drop_cmd(interaction, item, quantity, island)

    @dropbot_group.command(name="inject", description="Inject a villager into a sub-island with live search autocomplete")
    @app_commands.describe(villager="Villager name to inject (search autocomplete)", plot=f"Plot number ({PLOT_MIN}–{PLOT_MAX}, optional)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def dropbot_inject(self, interaction: discord.Interaction, villager: str | None = None, plot: app_commands.Range[int, PLOT_MIN, PLOT_MAX] | None = None, island: str | None = None) -> None:
        await self._handle_inject_cmd(interaction, villager, plot, island)

    @app_commands.command(name="inject", description="Inject a villager into a sub-island with live search autocomplete")
    @app_commands.describe(villager="Villager name to inject (search autocomplete)", plot=f"Plot number ({PLOT_MIN}–{PLOT_MAX}, optional)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def slash_inject(self, interaction: discord.Interaction, villager: str | None = None, plot: app_commands.Range[int, PLOT_MIN, PLOT_MAX] | None = None, island: str | None = None) -> None:
        await self._handle_inject_cmd(interaction, villager, plot, island)

    @dropbot_group.command(name="sanrio", description="Interactive 2-step wizard to inject Sanrio or Amiibo villagers")
    @app_commands.describe(villager="Target Sanrio/Amiibo villager (e.g. Chai, Chelsea, Marty, Étoile)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def dropbot_sanrio(self, interaction: discord.Interaction, villager: str | None = None, island: str | None = None) -> None:
        await self._handle_sanrio_cmd(interaction, island, villager)

    @app_commands.command(name="sanrio", description="Interactive 2-step wizard to inject Sanrio or Amiibo villagers")
    @app_commands.describe(villager="Target Sanrio/Amiibo villager (e.g. Chai, Chelsea, Marty, Étoile)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def slash_sanrio(self, interaction: discord.Interaction, villager: str | None = None, island: str | None = None) -> None:
        await self._handle_sanrio_cmd(interaction, island, villager)