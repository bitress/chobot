"""
DropBot Cog — Integrated island control panels for Discord.

Provides persistent, interaction-only UI panels in sub-island channels:
  • 🦤 Get Dodo Code   – fetches live Dodo from SysBot, sends ephemerally, logs to webhook
  • 📦 Drop Items      – modal for custom item/hex code drops (validated)
  • 🐱 Inject Villager – modal with fuzzy villager matching and a confirm step
  • 🔄 Refresh Status  – live island telemetry (status, queue, villagers)

Admin slash commands (under /dropbot group):
  • /dropbot setup-panel [island]  – post/refresh panel in current channel
  • /dropbot setup-all             – bulk-deploy panels under the sub-island category
  • /dropbot status                – list all registered panels

Requires ZERO privileged gateway intents.

Persistence: every panel button is a discord.ui.DynamicItem whose custom_id
carries the island (``dropbot:<action>:<island>``). The four item classes are
registered once in cog_load(), so buttons keep working after a restart without
re-adding a view per message. Requires discord.py >= 2.4.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

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

# Limits (enforced for custom item drops)
MAX_DROP_SLOTS = 9      # different items per drop, matches the site's 9-slot Drop Bot
MAX_STACK      = 30     # highest quantity per item
MAX_ITEM_NAME  = 40

# Villager plots: 1 through 10 (matching in-game plots and API backend).
PLOT_MIN = 1
PLOT_MAX = 10

# Access gates (see _check_access)
ENFORCE_BLOCKLIST = True
# Flip to True once _has_registered_character() is implemented against the real table.
REQUIRE_REGISTERED_CHARACTER = False

UNKNOWN_ISLAND_MSG = (
    "This panel points to an island I don't recognise. "
    "Ask a moderator to run `/dropbot setup-panel` in this channel."
)

# Debounce per (user_id, island_name) to avoid log spam
_WEBHOOK_DEBOUNCE: dict[str, float] = {}
_WEBHOOK_DEBOUNCE_TTL = 180  # 3 minutes


# ─── Database helpers ──────────────────────────────────────────────────────────

def _init_panel_table() -> None:
    """Ensure the island_dropbot_panels table exists."""
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
            conn.commit()
    except Exception as exc:
        logger.error("[DropBot] Failed to init panel table: %s", exc)


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
                (
                    str(channel_id),
                    island_name,
                    str(message_id),
                    datetime.now(timezone.utc).isoformat(),
                ),
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


# ─── Island name helpers ───────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase, strip emoji/punctuation, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _canonical_island(name: str | None) -> str | None:
    """Return the configured SUB_ISLANDS spelling for `name`, or None if it isn't a sub island."""
    key = _norm(name or "")
    if not key:
        return None
    for island in Config.SUB_ISLANDS:
        if _norm(island) == key:
            return island
    return None


def _infer_island_from_channel(channel) -> str | None:
    """
    Resolve island name from a Discord channel object.
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
    for island in Config.SUB_ISLANDS:
        inorm = _norm(island)
        if inorm and f" {inorm} " in padded and (best is None or len(inorm) > len(_norm(best))):
            best = island
    return best


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
            "https://www.chopaeng.com/profile, then try again."
        )

    return None


async def _denied_reason(discord_user_id: int) -> str | None:
    return await asyncio.to_thread(_check_access, discord_user_id)


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


def _normalize_items(entries: list[tuple[str, int]]) -> str:
    return ", ".join(f"{item} {qty}" for item, qty in entries)


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

    # First check ACNHCatalog for all 417 official villagers
    try:
        catalog = ACNHCatalog.get_instance()
        exact_v = catalog.get_villager(q)
        if exact_v:
            return exact_v.name, []

        hits = catalog.search_villagers(q, limit=limit)
        if hits:
            candidates = [h.name for h in hits]
            return None, candidates
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


# ─── Dodo audit webhook ────────────────────────────────────────────────────────

def _dodo_webhook_url() -> str:
    """The spec calls it DODO_WEBHOOK_URL; older code used DODO_LOG_WEBHOOK_URL. Accept both."""
    for name in ("DODO_WEBHOOK_URL", "DODO_LOG_WEBHOOK_URL"):
        val = getattr(Config, name, "") or os.getenv(name, "")
        if val:
            return val
    return ""


def _fire_dropbot_dodo_webhook(
    user_id: str,
    username: str,
    display_name: str,
    island_name: str,
    channel_id: str,
    dodo_code: str,
) -> None:
    """POST a Dodo reveal audit embed matching Chopaeng's standard format."""
    url = _dodo_webhook_url()
    if not url:
        return

    cache_key = f"{user_id}:{island_name}"
    now = time.monotonic()
    last = _WEBHOOK_DEBOUNCE.get(cache_key)
    if last and now - last < _WEBHOOK_DEBOUNCE_TTL:
        return
    _WEBHOOK_DEBOUNCE[cache_key] = now

    island_link = f"https://www.chopaeng.com/island/{urllib.parse.quote(island_name).lower()}"
    embed = {
        "title": "Dodo Code Revealed",
        "color": 0x2ECC71,
        "description": f"<@{user_id}> has revealed the Dodo code for island <#{channel_id}>",
        "fields": [
            {"name": "Member", "value": f"{display_name} (<@{user_id}>)"},
            {"name": "Island", "value": f"<#{channel_id}>\n[View Island]({island_link})"},
        ],
        "image": {"url": DODO_GIF},
        "footer": {"text": "Chopaeng Camp - Dodo Log", "icon_url": LOGO_URL},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = json.dumps({"embeds": [embed]}).encode()
    sep = "&" if "?" in url else "?"
    try:
        req = urllib.request.Request(
            f"{url}{sep}wait=true",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "ChoBot/2.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception as exc:
        logger.warning("[DropBot] Dodo webhook failed: %s", exc)


# ─── Embed + reply helpers ─────────────────────────────────────────────────────

def _build_panel_embed(island_name: str, status_data: dict | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏝️ {island_name} — Island Control Panel",
        description=(
            "Welcome to your island's interactive control panel!\n"
            "Use the buttons below to interact with this island's SysBot.\n\n"
            "_Only people with access to this channel can use the controls._"
        ),
        color=PANEL_EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )

    if status_data:
        is_running     = status_data.get("is_running", False)
        is_valid_dodo  = status_data.get("is_dodo_valid", False)
        queue_count    = status_data.get("queue_count", 0)
        console_stage  = status_data.get("console_stage", "Unknown")
        villagers      = status_data.get("villagers", [])

        status_emoji   = "🟢" if is_running else "🔴"
        dodo_line      = "✅ Dodo code is active" if is_valid_dodo else "🔄 Dodo code is refreshing…"
        embed.add_field(
            name="Bot Status",
            value=f"{status_emoji} **{'Online' if is_running else 'Offline'}** — {console_stage}\n{dodo_line}",
            inline=True,
        )
        embed.add_field(name="Queue", value=f"**{queue_count}** order(s)", inline=True)

        if villagers:
            embed.add_field(
                name="Current Villagers",
                value="\n".join(f"• {v}" for v in villagers[:10]),
                inline=False,
            )
    else:
        embed.add_field(
            name="Status",
            value="Click **🔄 Refresh** to load live island data.",
            inline=False,
        )

    embed.add_field(
        name="Available Actions",
        value=(
            "🦤 **Get Dodo** — Reveal this island's Dodo code privately\n"
            "📦 **Drop Items** — Enter items or codes to drop on the island\n"
            "🔍 **Search Catalog** — Browse & drop items or villagers with images\n"
            "🐱 **Inject Villager** — Request a villager injection\n"
            "🔄 **Refresh** — Update this panel with live data"
        ),
        inline=False,
    )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


def _err_embed(text: str) -> discord.Embed:
    embed = discord.Embed(description=f"❌ {text}", color=PANEL_EMBED_COLOR_ERR)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


async def _reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
) -> None:
    """Ephemeral reply that works before AND after interaction.response was used/deferred."""
    kwargs: dict[str, Any] = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
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


# ─── Shared actions (used by modal, select and confirm view) ───────────────────

async def _perform_drop(
    user: discord.abc.User,
    island_raw: str,
    items_text: str,
    *,
    heading: str,
    contents: str,
) -> discord.Embed:
    denied = await _denied_reason(user.id)
    if denied:
        return _err_embed(denied)
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG)

    try:
        client = get_island_sysbot_client(island)
        result = await client.submit_drop(
            items=items_text, username=getattr(user, "display_name", user.name)
        )
    except Exception:
        logger.exception("[DropBot] Drop failed island=%s user=%s", island, user.id)
        return _err_embed("The island bot didn't respond. Check that it's online and try again.")

    if result.get("success") is False or (result.get("error") and not result.get("success")):
        err = result.get("error") or "unknown error"
        logger.warning("[DropBot] Drop rejected island=%s user=%s: %s", island, user.id, err)
        return _err_embed(f"The drop was rejected: **{err}**\nMake sure the island bot is online.")

    embed = discord.Embed(
        title=heading,
        description=f"Your drop on **{island}** is queued.",
        color=PANEL_EMBED_COLOR,
    )
    embed.add_field(name="Items", value=f"```{contents[:400]}```", inline=False)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


async def _perform_injection(
    user: discord.abc.User,
    island_raw: str,
    villager: str,
    plot: int | None,
) -> discord.Embed:
    denied = await _denied_reason(user.id)
    if denied:
        return _err_embed(denied)
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG)

    try:
        client = get_island_sysbot_client(island)
        result = await client.inject_villagers(villager, plot=plot)
    except Exception:
        logger.exception("[DropBot] Injection failed island=%s user=%s", island, user.id)
        return _err_embed("The island bot didn't respond. Check that it's online and try again.")

    if result.get("success") is False or (result.get("error") and not result.get("success")):
        err = result.get("error") or "unknown error"
        logger.warning("[DropBot] Injection rejected island=%s user=%s: %s", island, user.id, err)
        return _err_embed(f"The injection was rejected: **{err}**\nMake sure the island bot is online.")

    # Record injection audit log to SQLite
    status_str = "queued" if (result.get("success") or not result.get("error")) else "failed"
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO villager_injections_log
                (island_name, villager_name, plot_number, requested_by_user_id, status, response_message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    island,
                    villager,
                    plot or 0,
                    str(user.id),
                    status_str,
                    str(result.get("message") or result.get("error") or "Queued via Discord panel"),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("[DropBot] Failed to log villager injection: %s", exc)

    embed = discord.Embed(
        title="🐱 Villager injection queued",
        description=f"**{villager}** is queued for injection into **{island}**.",
        color=PANEL_EMBED_COLOR,
    )
    if plot is not None:  # plot 0 is valid, so don't test truthiness
        embed.add_field(name="Requested plot", value=str(plot), inline=True)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


# ─── Modals ───────────────────────────────────────────────────────────────────

class CustomDropModal(discord.ui.Modal, title="📦 Custom Item Drop"):
    items_input = discord.ui.TextInput(
        label=f"Items to drop (up to {MAX_DROP_SLOTS} items)",
        style=discord.TextStyle.paragraph,
        # Discord caps placeholders at 100 characters.
        placeholder="Gold nugget 30, Iron nugget 30, 09C9 30",
        min_length=2,
        max_length=500,
        required=True,
    )

    def __init__(self, island_name: str):
        super().__init__()
        self.island_name = island_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        parsed, err = parse_drop_items(self.items_input.value)
        if err or not parsed:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return

        normalized = _normalize_items(parsed)
        embed = await _perform_drop(
            interaction.user,
            self.island_name,
            normalized,
            heading="📦 Drop queued",
            contents=normalized,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CustomDropModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


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

        if not _get_villager_names():
            await interaction.followup.send(
                "❌ The villager list isn't available right now. Please ask a moderator.", ephemeral=True
            )
            return

        exact, candidates = _match_villagers(raw_name)

        if exact:
            embed = await _perform_injection(interaction.user, self.island_name, exact, plot)
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

class VillagerChoiceView(discord.ui.View):
    """Confirm (one candidate) or pick (several candidates) before injecting a fuzzy-matched villager."""

    def __init__(self, user_id: int, island_name: str, candidates: list[str], plot: int | None):
        super().__init__(timeout=90)
        self.user_id = user_id
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
                options=[discord.SelectOption(label=c[:100], value=c) for c in candidates],
                row=0,
            )
            select.callback = self._make_select_callback(select)  # type: ignore[assignment]
            self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    async def _run(self, interaction: discord.Interaction, villager: str) -> None:
        await interaction.response.edit_message(
            content=f"⏳ Injecting **{villager}**…", embed=None, view=None
        )
        embed = await _perform_injection(interaction.user, self.island_name, villager, self.plot)
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


# ─── Catalog Search Modal & View ───────────────────────────────────────────────

class CatalogSearchModal(discord.ui.Modal, title="🔍 Search ACNH Catalog"):
    search_query = discord.ui.TextInput(
        label="Search Keyword (Items & Villagers)",
        placeholder="e.g. royal crown, gold, Raymond, painting, diy",
        required=True,
        max_length=60,
    )

    def __init__(self, island_name: str | None = None):
        super().__init__()
        self.island_name = island_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = self.search_query.value.strip()
        catalog = ACNHCatalog.get_instance()
        items = catalog.search_items(query, limit=100)
        villagers = catalog.search_villagers(query, limit=10)

        view = CatalogSearchView(
            island_name=self.island_name,
            query=query,
            items=items,
            villagers=villagers,
            page=0,
            user_id=interaction.user.id,
        )
        embed = view.build_search_embed()

        if interaction.response.is_done():
            msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            view.message = msg
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CatalogSearchModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong while searching the catalog.")


class CatalogSearchView(discord.ui.View):
    """Catalog search results view with interactive pagination, dropdowns, and images."""

    def __init__(
        self,
        island_name: str | None = None,
        query: str = "",
        items: list[CatalogItem] | None = None,
        villagers: list[CatalogVillager] | None = None,
        page: int = 0,
        page_size: int = 10,
        user_id: int | None = None,
    ):
        super().__init__(timeout=180)
        self.island_name = island_name
        self.query = query
        self.items = items or []
        self.villagers = villagers or []
        self.page = page
        self.page_size = page_size
        self.user_id = user_id
        self.total_pages = max(1, (len(self.items) + self.page_size - 1) // self.page_size)
        self.message: discord.Message | None = None
        self._build_components()

    def get_current_page_items(self) -> list[CatalogItem]:
        start = self.page * self.page_size
        return self.items[start : start + self.page_size]

    def _build_components(self) -> None:
        self.clear_items()
        current_items = self.get_current_page_items()

        # Row 0: Items select dropdown (up to 10 options on this page)
        if current_items:
            options: list[discord.SelectOption] = []
            for idx, it in enumerate(current_items):
                desc = it.category
                hex_code = it.to_hex()
                if hex_code:
                    desc += f" • Hex: {hex_code}"
                options.append(
                    discord.SelectOption(
                        label=it.display_name[:100],
                        description=desc[:100],
                        value=f"item_{idx}",
                        emoji="📜" if it.diy else "📦",
                    )
                )
            max_opts = min(len(options), 10)
            placeholder = (
                f"Select item(s) to drop on {self.island_name}..."
                if self.island_name
                else f"Select item(s) to inspect (Page {self.page+1}/{self.total_pages})..."
            )
            item_select = discord.ui.Select(
                placeholder=placeholder[:100],
                min_values=1,
                max_values=max_opts,
                options=options,
                custom_id="catalog_item_select",
                row=0,
            )
            item_select.callback = self.on_item_select  # type: ignore[assignment]
            self.add_item(item_select)

        # Row 1: Villager select dropdown (shown on page 0 if any matched)
        has_villagers = bool(self.villagers and self.page == 0)
        if has_villagers:
            v_options: list[discord.SelectOption] = []
            for idx, v in enumerate(self.villagers[:10]):
                v_options.append(
                    discord.SelectOption(
                        label=v.display_name[:100],
                        description=f"ID: {v.villager_id} • {v.personality}"[:100],
                        value=f"villager_{idx}",
                        emoji="🐱",
                    )
                )
            v_placeholder = (
                f"Select villager to inject into {self.island_name}..."
                if self.island_name
                else "Select villager to inspect..."
            )
            villager_select = discord.ui.Select(
                placeholder=v_placeholder[:100],
                min_values=1,
                max_values=1,
                options=v_options,
                custom_id="catalog_villager_select",
                row=1,
            )
            villager_select.callback = self.on_villager_select  # type: ignore[assignment]
            self.add_item(villager_select)

        # Row 2 (or 1 if no villagers): Navigation & Actions
        nav_row = 2 if has_villagers else 1

        btn_prev = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page <= 0),
            row=nav_row,
        )
        btn_prev.callback = self.on_prev_page  # type: ignore[assignment]
        self.add_item(btn_prev)

        btn_next = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.total_pages - 1),
            row=nav_row,
        )
        btn_next.callback = self.on_next_page  # type: ignore[assignment]
        self.add_item(btn_next)

        btn_search = discord.ui.Button(
            label="New Search",
            style=discord.ButtonStyle.primary,
            emoji="🔍",
            row=nav_row,
        )
        btn_search.callback = self.on_search_button  # type: ignore[assignment]
        self.add_item(btn_search)

        btn_close = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.secondary,
            emoji="✖",
            row=nav_row,
        )
        btn_close.callback = self.on_close  # type: ignore[assignment]
        self.add_item(btn_close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message("This search menu is not yours.", ephemeral=True)
            return False
        return True

    def build_search_embed(self) -> discord.Embed:
        current_items = self.get_current_page_items()
        desc = (
            f"Found **{len(self.items)}** items and **{len(self.villagers)}** villagers.\n"
            f"**Page:** `{self.page + 1} / {self.total_pages}`"
        )
        if self.island_name:
            desc += f"\n🏝️ **Target Island:** **{self.island_name}**"
        desc += "\n*Use the dropdowns below to drop items or inject a villager!*"

        embed = discord.Embed(
            title=f"🔍 Catalog Search: '{self.query or 'All'}'",
            description=desc,
            color=PANEL_EMBED_COLOR,
        )

        if self.villagers and self.villagers[0].photo_url:
            embed.set_thumbnail(url=self.villagers[0].photo_url)
        elif current_items and current_items[0].image_url:
            embed.set_thumbnail(url=current_items[0].image_url)

        if current_items:
            lines = []
            start_num = self.page * self.page_size + 1
            for i, it in enumerate(current_items, start_num):
                diy_badge = " *(DIY)*" if it.diy else ""
                hex_code = it.to_hex()
                hex_part = f" • `{hex_code}`" if hex_code else ""
                lines.append(f"`{i:02d}.` **{it.display_name}**{diy_badge} — `{it.category}`{hex_part}")

            chunk: list[str] = []
            c_len = 0
            field_num = 1
            for line in lines:
                if c_len + len(line) + 1 > 950:
                    embed.add_field(
                        name=(
                            f"📦 Matching Items (Page {self.page + 1}/{self.total_pages})"
                            if field_num == 1
                            else "📦 Items (cont.)"
                        ),
                        value="\n".join(chunk),
                        inline=False,
                    )
                    chunk = [line]
                    c_len = len(line)
                    field_num += 1
                else:
                    chunk.append(line)
                    c_len += len(line) + 1
            if chunk:
                embed.add_field(
                    name=(
                        f"📦 Matching Items (Page {self.page + 1}/{self.total_pages})"
                        if field_num == 1
                        else "📦 Items (cont.)"
                    ),
                    value="\n".join(chunk),
                    inline=False,
                )

        if self.villagers and self.page == 0:
            v_lines = []
            for v in self.villagers[:6]:
                v_lines.append(f"• **{v.name}** (`ID: {v.villager_id}`) — *{v.species} / {v.personality}*")
            embed.add_field(
                name="🐱 Matching Villagers (Move-in Plot)",
                value="\n".join(v_lines),
                inline=False,
            )

        if not self.items and not self.villagers:
            embed.description = "❌ No items or villagers found matching that query. Try another keyword!"

        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed

    async def on_prev_page(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
            self._build_components()
            await interaction.response.edit_message(embed=self.build_search_embed(), view=self)

    async def on_next_page(self, interaction: discord.Interaction) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1
            self._build_components()
            await interaction.response.edit_message(embed=self.build_search_embed(), view=self)

    async def on_search_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CatalogSearchModal(island_name=self.island_name))

    async def on_close(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                if interaction.message:
                    await interaction.message.delete()
            except Exception:
                pass

    async def on_item_select(self, interaction: discord.Interaction) -> None:
        selected_values = interaction.data.get("values", [])
        current_items = self.get_current_page_items()
        chosen: list[CatalogItem] = []
        for val in selected_values:
            try:
                idx = int(val.replace("item_", ""))
                if idx < len(current_items):
                    chosen.append(current_items[idx])
            except ValueError:
                pass

        if not chosen:
            await interaction.response.defer()
            return

        if self.island_name:
            await interaction.response.defer(ephemeral=True, thinking=True)
            entries = [f"{it.name} {min(it.stack_size, 30)}" for it in chosen]
            drop_text = ", ".join(entries)
            embed = await _perform_drop(
                interaction.user,
                self.island_name,
                drop_text,
                heading="📦 Catalog Drop Queued",
                contents=drop_text,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title=f"📦 Selected Items ({len(chosen)})",
                description="Here are the catalog details and hex codes for your selected item(s):",
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
                text="💡 Tip: Run /catalog with an island or click 'Search Catalog' on an island panel to drop directly!"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_villager_select(self, interaction: discord.Interaction) -> None:
        selected_values = interaction.data.get("values", [""])
        if not selected_values:
            await interaction.response.defer()
            return
        val = selected_values[0]
        try:
            idx = int(val.replace("villager_", ""))
            v = self.villagers[idx]
        except (ValueError, IndexError):
            await interaction.response.defer()
            return

        if self.island_name:
            await interaction.response.defer(ephemeral=True, thinking=True)
            embed = await _perform_injection(
                interaction.user,
                self.island_name,
                v.name,
                plot=None,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title=f"🐱 Villager: {v.name}",
                description=(
                    f"**Species:** {v.species}\n"
                    f"**Personality:** {v.personality}\n"
                    f"**Internal ID:** `{v.villager_id}`\n\n"
                    f"💡 To inject **{v.name}**, select an island in `/catalog` or click **Search Catalog** on an island panel."
                ),
                color=PANEL_EMBED_COLOR,
            )
            if v.photo_url:
                embed.set_thumbnail(url=v.photo_url)
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self)


# ─── Persistent panel buttons (DynamicItem) ────────────────────────────────────

def _cid(action: str, island: str) -> str:
    """custom_id = dropbot:<action>:<url-quoted island>. Stays under Discord's 100-character limit."""
    return f"dropbot:{action}:{urllib.parse.quote(island, safe='')}"


class _PanelItem:
    """Shared behaviour for panel buttons. Plain mixin (DynamicItem subclasses need their own template)."""

    island: str

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error(
            "[DropBot] Button error island=%s: %s", getattr(self, "island", "?"), error, exc_info=error
        )
        await _reply(interaction, "❌ Something went wrong. Please try again in a moment.")


class GetDodoButton(
    _PanelItem,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:getdodo:(?P<island>[^:]+)",
):
    def __init__(self, island: str):
        super().__init__(
            discord.ui.Button(
                label="Get Dodo",
                emoji="🦤",
                style=discord.ButtonStyle.success,
                custom_id=_cid("getdodo", island),
                row=0,
            )
        )
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        resolved = await _resolve_client(interaction, self.island)
        if not resolved:
            return
        island, client = resolved

        denied = await _denied_reason(interaction.user.id)
        if denied:
            await _reply(interaction, embed=_err_embed(denied))
            return

        try:
            data = await client.get_bot_status()
        except Exception:
            logger.exception("[DropBot] get_bot_status failed for %s", island)
            data = {"is_running": False}

        dodo = data.get("dodo_code") or data.get("dodo")
        is_valid = bool(data.get("is_dodo_valid") or (dodo and str(dodo).strip() not in _INVALID_DODO_CODES))

        if not is_valid:
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
        embed.set_image(url=DODO_GIF)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Audit webhook — fire-and-forget in a worker thread
        asyncio.get_running_loop().run_in_executor(
            None,
            _fire_dropbot_dodo_webhook,
            str(interaction.user.id),
            interaction.user.name,
            interaction.user.display_name,
            island,
            str(interaction.channel_id or ""),
            str(dodo),
        )


class DropItemsButton(
    _PanelItem,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:drop:(?P<island>[^:]+)",
):
    def __init__(self, island: str):
        super().__init__(
            discord.ui.Button(
                label="Drop Items",
                emoji="📦",
                style=discord.ButtonStyle.primary,
                custom_id=_cid("drop", island),
                row=0,
            )
        )
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        island = _canonical_island(self.island)
        if not island:
            await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
            return
        denied = await _denied_reason(interaction.user.id)
        if denied:
            await _reply(interaction, embed=_err_embed(denied))
            return
        await interaction.response.send_modal(CustomDropModal(island_name=island))


class SearchCatalogButton(
    _PanelItem,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:search:(?P<island>[^:]+)",
):
    def __init__(self, island: str):
        super().__init__(
            discord.ui.Button(
                label="Search Catalog",
                emoji="🔍",
                style=discord.ButtonStyle.secondary,
                custom_id=_cid("search", island),
                row=0,
            )
        )
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        island = _canonical_island(self.island)
        if not island:
            await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
            return
        denied = await _denied_reason(interaction.user.id)
        if denied:
            await _reply(interaction, embed=_err_embed(denied))
            return
        await interaction.response.send_modal(CatalogSearchModal(island_name=island))


class InjectVillagerButton(
    _PanelItem,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:villager:(?P<island>[^:]+)",
):
    def __init__(self, island: str):
        super().__init__(
            discord.ui.Button(
                label="Inject Villager",
                emoji="🐱",
                style=discord.ButtonStyle.secondary,
                custom_id=_cid("villager", island),
                row=0,
            )
        )
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        island = _canonical_island(self.island)
        if not island:
            await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
            return
        denied = await _denied_reason(interaction.user.id)
        if denied:
            await _reply(interaction, embed=_err_embed(denied))
            return
        await interaction.response.send_modal(InjectVillagerModal(island_name=island))


class RefreshButton(
    _PanelItem,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:refresh:(?P<island>[^:]+)",
):
    def __init__(self, island: str):
        super().__init__(
            discord.ui.Button(
                label="Refresh",
                emoji="🔄",
                style=discord.ButtonStyle.grey,
                custom_id=_cid("refresh", island),
                row=0,
            )
        )
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # Silent acknowledge; the panel message itself is edited below.
        await interaction.response.defer()

        resolved = await _resolve_client(interaction, self.island)
        if not resolved:
            return
        island, client = resolved

        try:
            status_data: dict = await client.get_bot_status()
        except Exception:
            logger.exception("[DropBot] get_bot_status failed for %s", island)
            status_data = {"is_running": False}

        try:
            vdata = await client.get_villagers()
            raw_list = vdata.get("villagers", [])
            status_data["villagers"] = [
                (v.get("name") or v.get("Name") or str(v)) if isinstance(v, dict) else str(v)
                for v in raw_list
            ]
        except Exception:
            status_data["villagers"] = []

        # Same deterministic custom_ids as before, so the buttons keep working.
        await interaction.edit_original_response(
            embed=_build_panel_embed(island, status_data),
            view=IslandPanelView(island),
        )


PANEL_ITEMS = (GetDodoButton, DropItemsButton, SearchCatalogButton, InjectVillagerButton, RefreshButton)


class IslandPanelView(discord.ui.View):
    """
    The panel's five buttons. All are DynamicItems registered in cog_load(), so this view
    needs no per-message bot.add_view() and survives restarts.
    """

    def __init__(self, island_name: str):
        super().__init__(timeout=None)
        self.island_name = island_name
        for item_cls in PANEL_ITEMS:
            self.add_item(item_cls(island_name))


# ─── DropBot Cog ──────────────────────────────────────────────────────────────

class DropBotCog(commands.Cog, name="DropBot"):
    """
    Manages persistent interactive island control panels across sub-island channels.
    No privileged intents required — operates entirely on Discord's Interaction API.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        _init_panel_table()
        _get_villager_names()   # pre-warm cache
        # One registration covers every panel in every channel (the island is in the custom_id).
        self.bot.add_dynamic_items(*PANEL_ITEMS)
        logger.info("[DropBot] Registered %d persistent button types.", len(PANEL_ITEMS))

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*PANEL_ITEMS)

    # ── Slash command group ───────────────────────────────────────────────────

    dropbot_group = app_commands.Group(
        name="dropbot",
        description="Manage island DropBot control panels",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    async def island_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        return [
            app_commands.Choice(name=i, value=i)
            for i in Config.SUB_ISLANDS
            if cur in i.lower()
        ][:25]

    async def catalog_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete choices for item and villager catalog search."""
        if not current or len(current) < 2:
            return []
        try:
            catalog = ACNHCatalog.get_instance()
            item_hits = catalog.search_items(current, limit=18)
            villager_hits = catalog.search_villagers(current, limit=7)

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
    @app_commands.autocomplete(island=island_autocomplete)
    async def setup_panel(self, interaction: discord.Interaction, island: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel

        if island:
            canonical = _canonical_island(island)
            if not canonical:
                valid = ", ".join(Config.SUB_ISLANDS)
                if len(valid) > 1500:
                    valid = valid[:1500] + "…"
                await interaction.followup.send(
                    f"❌ **{island[:50]}** isn't a sub island.\nValid islands: {valid}",
                    ephemeral=True,
                )
                return
            island = canonical
        else:
            island = _infer_island_from_channel(channel)

        if not island:
            await interaction.followup.send(
                "❌ Could not auto-detect the island from this channel name.\n"
                "Pass it explicitly: `/dropbot setup-panel island:Hiraya`",
                ephemeral=True,
            )
            return

        # Delete old panel if exists
        existing = _get_panel_row(channel.id)
        if existing and existing.get("message_id"):
            try:
                old_msg = await channel.fetch_message(int(existing["message_id"]))
                await old_msg.delete()
            except Exception:
                pass

        embed = _build_panel_embed(island)
        view = IslandPanelView(island_name=island)
        panel_msg = await channel.send(embed=embed, view=view)
        _upsert_panel(channel.id, island, panel_msg.id)

        await interaction.followup.send(
            f"✅ DropBot panel for **{island}** posted in <#{channel.id}> (msg `{panel_msg.id}`).",
            ephemeral=True,
        )
        logger.info("[DropBot] setup-panel: island=%s channel=%s msg=%s by %s",
                    island, channel.id, panel_msg.id, interaction.user)

    # ── /dropbot setup-all ─────────────────────────────────────────────────────

    @dropbot_group.command(name="setup-all", description="Bulk-deploy DropBot panels to all sub-island channels")
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
            await interaction.followup.send(
                f"❌ Could not find a category with ID `{category_id}`.", ephemeral=True
            )
            return

        results: list[str] = []
        for channel in category.text_channels:
            island = _infer_island_from_channel(channel)
            if not island:
                results.append(f"⏭️ `#{channel.name}` — no island match, skipped")
                continue

            existing = _get_panel_row(channel.id)
            if existing and existing.get("message_id"):
                try:
                    old_msg = await channel.fetch_message(int(existing["message_id"]))
                    await old_msg.delete()
                except Exception:
                    pass

            try:
                embed = _build_panel_embed(island)
                view = IslandPanelView(island_name=island)
                panel_msg = await channel.send(embed=embed, view=view)
                _upsert_panel(channel.id, island, panel_msg.id)
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
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        panels = _load_all_panels()
        if not panels:
            await interaction.followup.send(
                "No panels registered yet. Run `/dropbot setup-panel` or `/dropbot setup-all`.",
                ephemeral=True,
            )
            return

        lines = [
            f"• <#{p['channel_id']}> → **{p['island_name']}** (msg `{p.get('message_id', '—')}`)"
            for p in panels
        ]
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "\n…(truncated)"
        await interaction.followup.send(
            f"**Registered DropBot Panels ({len(panels)}):**\n{body}", ephemeral=True
        )

    # ── /dropbot catalog & /catalog ────────────────────────────────────────────

    async def _handle_catalog_search(
        self,
        interaction: discord.Interaction,
        query: str,
        island: str | None,
    ) -> None:
        target_island = _canonical_island(island) if island else _infer_island_from_channel(interaction.channel)

        query_str = query.strip()
        # Clean prefix if chosen from autocomplete (📦 or 🐱)
        if query_str.startswith(("📦 ", "🐱 ")):
            query_str = query_str[2:].strip()

        if not query_str:
            await interaction.response.send_modal(CatalogSearchModal(island_name=target_island))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        catalog = ACNHCatalog.get_instance()
        items = catalog.search_items(query_str, limit=100)
        villagers = catalog.search_villagers(query_str, limit=10)

        view = CatalogSearchView(
            island_name=target_island,
            query=query_str,
            items=items,
            villagers=villagers,
            page=0,
            user_id=interaction.user.id,
        )
        embed = view.build_search_embed()
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = msg

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