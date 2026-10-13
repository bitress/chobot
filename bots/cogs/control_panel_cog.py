"""
ControlPanel Cog — Island remote control panel for SysBot.ACNHOrders.

Provides persistent, interaction-only remote control panels in Discord:
  • Command: /control-panel setup [island]
  • Role-gated: Only members with role ID 1559583848050851980 (or Administrator) can use it.
  • Connects to SysBot REST API (unified dispatcher POST /api/control with fallback to dedicated endpoints).
  • Controls:
      - Start bot (/api/control/start)
      - Stop bot (/api/control/stop)
      - Restart connection (/api/control/restart)
      - Pause requests (/api/requests/pause)
      - Resume requests (/api/requests/resume)
      - Screen on/off (/api/screen)
      - Detach controller (/api/detach)
      - New Dodo code (/api/dodo/refresh)
      - Mash B on/off (/api/togglemashb)
      - Map refresh on/off (/api/togglerefresh)
      - Guide (/api/control/guide)
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import difflib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.config import Config
from utils.database import connect_db
from utils.sysbot_api import _INVALID_DODO_CODES, get_island_sysbot_client

logger = logging.getLogger("ControlPanelCog")

# ─── Constants ────────────────────────────────────────────────────────────────

CONTROL_PANEL_ROLE_ID = getattr(Config, "CONTROL_PANEL_ROLE_ID", 1559583848050851980) or 1559583848050851980
PERSISTENT_CONTROL_CHANNEL_ID = getattr(Config, "CONTROL_PANEL_CHANNEL_ID", 1559482907762884630) or 1559482907762884630
FORCE_EDIT_SECONDS = 300.0

PANEL_COLOR_ONLINE  = 0x57F287   # Discord Green
PANEL_COLOR_WARN    = 0xFEE75C   # Yellow
PANEL_COLOR_OFFLINE = 0xED4245   # Red
PANEL_FOOTER_TEXT   = "ChoPaeng Camp • Island Control Panel • SysBot Integration"
PANEL_FOOTER_ICON   = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"


def _embed_signature(embed: discord.Embed) -> str:
    """Stable fingerprint of an embed ignoring changing timestamp fields, to skip no-op edits."""
    d = embed.to_dict()
    d.pop("timestamp", None)
    if "fields" in d:
        fields = []
        for f in d["fields"]:
            if f.get("name") == "🕒 Last Updated":
                continue
            fields.append(f)
        d["fields"] = fields
    return json.dumps(d, sort_keys=True)


def _init_panel_table() -> None:
    """Create island_control_panels table if it does not exist."""
    try:
        with connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS island_control_panels (
                    channel_id  VARCHAR(64) PRIMARY KEY,
                    island_name VARCHAR(128) NOT NULL,
                    message_id  VARCHAR(64),
                    updated_at  VARCHAR(64)
                )
                """
            )
            conn.commit()
    except Exception as exc:
        logger.error("[ControlPanel] Failed to initialize database table: %s", exc)


def _upsert_control_panel(channel_id: int, island_name: str, message_id: int | None = None) -> None:
    """Insert or update a control panel entry in the database."""
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO island_control_panels (channel_id, island_name, message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    island_name=excluded.island_name,
                    message_id=COALESCE(excluded.message_id, island_control_panels.message_id),
                    updated_at=excluded.updated_at
                """,
                (str(channel_id), island_name, str(message_id) if message_id else None, now_iso),
            )
            conn.commit()
    except Exception as exc:
        logger.error("[ControlPanel] Failed to upsert control panel: %s", exc)


def _get_control_panel(channel_id: int) -> dict | None:
    """Retrieve existing control panel record for a given channel."""
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT channel_id, island_name, message_id, updated_at FROM island_control_panels WHERE channel_id = ?",
                (str(channel_id),),
            ).fetchone()
            return dict(row) if row else None
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to get panel row for %s: %s", channel_id, exc)
        return None


def _load_all_control_panels() -> list[dict]:
    """Retrieve all saved control panel records."""
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT channel_id, island_name, message_id, updated_at FROM island_control_panels"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to load all panels: %s", exc)
        return []

def build_guide_embed() -> discord.Embed:
    """Build the static help guide embed for all remote controls."""
    embed = discord.Embed(
        title="📖 Island Remote Control Guide",
        description="Quick reference guide for all controls available on the Sub-Island Remote Control Panel.",
        color=PANEL_COLOR_ONLINE,
    )
    embed.add_field(
        name="🟢 Start Bot",
        value="Starts the console loop, reconnects Switch, and opens incoming requests.",
        inline=False,
    )
    embed.add_field(
        name="🔴 Stop Bot",
        value="Pauses requests, finishes active order, stops loop & disconnects Switch.",
        inline=False,
    )
    embed.add_field(
        name="🔄 Restart Connection",
        value="Pauses requests, finishes active order, then restarts the console loop.",
        inline=False,
    )
    embed.add_field(
        name="⏸️ Pause Requests",
        value="Pauses incoming requests and queue processing. Active order finishes.",
        inline=False,
    )
    embed.add_field(
        name="▶️ Resume Requests",
        value="Re-opens incoming requests while the console loop continues running.",
        inline=False,
    )
    embed.add_field(
        name="📺 Screen On / Off",
        value="Toggles Switch display output on or off to reduce hardware strain.",
        inline=False,
    )
    embed.add_field(
        name="🎮 Detach Controller",
        value="Detaches the virtual controller from the Switch.",
        inline=False,
    )
    embed.add_field(
        name="🦤 New Dodo Code",
        value="Requests a game restart and a fresh Dodo code in restore mode.",
        inline=False,
    )
    embed.add_field(
        name="🅱️ Mash B On / Off",
        value="Toggles automatic dialogue button presses to advance dialogues.",
        inline=False,
    )
    embed.add_field(
        name="🗺️ Map Refresh On / Off",
        value="Toggles automatic map refresh in restore mode.",
        inline=False,
    )
    embed.add_field(
        name="🔄 Refresh Status",
        value="Fetches live status and Dodo code from SysBot to refresh the panel in-place.",
        inline=False,
    )
    embed.add_field(
        name="🏝️ Switch Island",
        value="Use the top dropdown to switch the control panel target to any sub-island.",
        inline=False,
    )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


def check_control_panel_role(user: discord.User | discord.Member) -> bool:
    """Access is managed directly via Discord channel permissions."""
    return True


def _err_embed(message: str) -> discord.Embed:
    embed = discord.Embed(
        title="🚫 Access Denied",
        description=message,
        color=PANEL_COLOR_OFFLINE,
    )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


def _canonical_island(name: str | None) -> str:
    """Match input against all known island names."""
    if not name:
        return ""
    clean = re.sub(r"^sub\s+island\s+", "", name.strip(), flags=re.IGNORECASE).strip().lower()
    for cand in Config.ALL_ISLAND_PORT_ORDER:
        if cand.lower() == clean:
            return cand
    for cand in Config.ALL_ISLAND_PORT_ORDER:
        if clean in cand.lower() or cand.lower() in clean:
            return cand
    return name.strip().title()


def _infer_island_from_channel(channel: discord.abc.GuildChannel | None) -> str:
    """Infer island name from Discord channel name (e.g. #hiraya -> Hiraya)."""
    if not channel or not hasattr(channel, "name"):
        return Config.SUB_ISLANDS[0]
    c_name = channel.name.lower().replace("-", " ").replace("_", " ")
    for island in Config.ALL_ISLAND_PORT_ORDER:
        if island.lower() in c_name:
            return island
    return Config.SUB_ISLANDS[0]


async def fetch_island_status(island_name: str) -> dict:
    """Fetch live status of the target island via SysBotClient."""
    try:
        client = get_island_sysbot_client(island_name)
        return await client.get_bot_status()
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to fetch status for %s: %s", island_name, exc)
        return {"success": False, "error": str(exc), "island_name": island_name}


def build_control_panel_embed(island_name: str, status: dict | None = None) -> discord.Embed:
    """Construct the main remote control panel embed."""
    status = status or {}
    is_running = status.get("is_running", False)
    accepting = status.get("accepting_commands", is_running)
    console_stage = status.get("console_stage", "Running" if is_running else "Offline")
    dodo = status.get("dodo_code")
    has_valid_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
    dodo_display = f"`{dodo}`" if has_valid_dodo else "`-----` (No Dodo)"

    # Color indicator
    if is_running and accepting:
        color = PANEL_COLOR_ONLINE
        status_text = "🟢 **ONLINE & READY**"
    elif is_running and not accepting:
        color = PANEL_COLOR_WARN
        status_text = "🟡 **PAUSED / RESTORING**"
    else:
        color = PANEL_COLOR_OFFLINE
        status_text = "🔴 **OFFLINE / STOPPED**"

    port = Config.get_island_sysbot_url(island_name)

    embed = discord.Embed(
        title=f"🎛️ Island Remote Control Panel — {island_name}",
        description=(
            f"{status_text}\n"
            f"Direct hardware & SysBot loop controller for **[{island_name}](https://www.chopaeng.com/island/{island_name})**.\n\n"
            "Use the interactive controls below to manage the console loop and hardware."
        ),
        color=color,
    )

    embed.add_field(
        name="✈️ Dodo Code",
        value=dodo_display,
        inline=True,
    )
    embed.add_field(
        name="🎮 Console Stage",
        value=f"`{console_stage}`",
        inline=True,
    )
    embed.add_field(
        name="📡 Requests",
        value=f"`{'Accepting' if accepting else 'Paused'}`",
        inline=True,
    )

    features = (
        f"• **Screen:** `{status.get('screen_state', 'Unknown')}`\n"
        f"• **Mash B:** `{status.get('mash_state', 'Unknown')}`\n"
        f"• **Map Refresh:** `{status.get('map_refresh_state', 'Unknown')}`"
    )
    embed.add_field(
        name="⚙️ Console States",
        value=features,
        inline=True,
    )
    embed.add_field(
        name="🌐 SysBot Endpoint",
        value=f"`{port}`",
        inline=True,
    )
    embed.add_field(
        name="🕒 Last Updated",
        value=f"<t:{int(time.time())}:R>",
        inline=True,
    )

    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


# ─── Dynamic Items & Persistent Components ─────────────────────────────────────

class ControlPanelIslandSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"ctrlpanel:select:(?P<island>[^:]+)",
):
    """Dynamic island selector dropdown."""

    def __init__(self, current_island: str):
        self.current_island = current_island

        # Populate top islands: 20 Sub Islands + Sinta (up to 25 options limit)
        options: List[discord.SelectOption] = []
        featured = list(Config.SUB_ISLANDS)
        if "Sinta" not in featured:
            featured.append("Sinta")

        if current_island not in featured and current_island in Config.ALL_ISLAND_PORT_ORDER:
            featured = [current_island] + featured[:24]

        for name in featured[:25]:
            is_def = (name.lower() == current_island.lower())
            desc = "Order Bot Island" if name == "Sinta" else "Sub Island"
            options.append(
                discord.SelectOption(
                    label=name,
                    description=desc,
                    emoji="🏝️",
                    value=name,
                    default=is_def,
                )
            )

        select_item = discord.ui.Select(
            custom_id=f"ctrlpanel:select:{current_island}",
            placeholder=f"🏝️ Control Island: {current_island}…",
            options=options,
            row=0,
        )
        super().__init__(select_item)

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match[str], /
    ):
        return cls(match.group("island"))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = getattr(interaction.data, "get", lambda k, d: d)("values", [self.current_island])
        new_island = values[0] if values else self.current_island

        await interaction.response.defer()
        status = await fetch_island_status(new_island)
        embed = build_control_panel_embed(new_island, status)
        view = IslandControlPanelView(new_island)
        await interaction.edit_original_response(embed=embed, view=view)

        if interaction.channel_id:
            msg_id = interaction.message.id if interaction.message else None
            await asyncio.to_thread(_upsert_control_panel, interaction.channel_id, new_island, msg_id)
            cog: Any = getattr(interaction.client, "get_cog", lambda _: None)("ControlPanelCog")
            if cog and hasattr(cog, "_sigs"):
                cog._sigs[str(interaction.channel_id)] = (_embed_signature(embed), time.monotonic())


class ControlPanelActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ctrlpanel:(?P<action>[a-zA-Z0-9_-]+):(?P<island>[^:]+)",
):
    """Dynamic action buttons handling all SysBot control actions."""

    def __init__(
        self,
        action: str,
        island: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        row: int = 1,
    ):
        self.action = action
        self.island = island
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"ctrlpanel:{action}:{island}",
            row=row,
        )
        super().__init__(button)

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ):
        action = match.group("action")
        island = match.group("island")
        return cls(action=action, island=island, label=item.label or action, emoji=str(item.emoji or ""), style=item.style)

    async def callback(self, interaction: discord.Interaction) -> None:
        client = get_island_sysbot_client(self.island)

        # ─── Specialized Actions: Queue, Guide, Refresh Status ─────────────────
        if self.action == "guide":
            await interaction.response.send_message(embed=build_guide_embed(), ephemeral=True)
            return

        if self.action == "status":
            await interaction.response.defer()
            status = await fetch_island_status(self.island)
            embed = build_control_panel_embed(self.island, status)
            view = IslandControlPanelView(self.island)
            await interaction.edit_original_response(embed=embed, view=view)
            return

        # ─── Command Execution Actions ─────────────────────────────────────────
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = await client.control_action(self.action)

        ok = bool(res.get("success", True) if isinstance(res, dict) else False)
        msg = res.get("message") or res.get("error") or ""

        action_names = {
            "start": "Start bot loop",
            "stop": "Stop bot loop",
            "restart": "Restart connection",
            "pause": "Pause requests",
            "resume": "Resume requests",
            "screen": "Toggle screen",
            "detach": "Detach controller",
            "newdodo": "Request new Dodo",
            "mash": "Toggle Mash B",
            "map": "Toggle Map Refresh",
        }
        human_name = action_names.get(self.action, self.action.title())

        if ok:
            resp_embed = discord.Embed(
                title=f"✅ {human_name} Executed",
                description=f"Command sent to **{self.island}** successfully.\n{f'*{msg}*' if msg else ''}",
                color=PANEL_COLOR_ONLINE,
            )
        else:
            resp_embed = discord.Embed(
                title=f"⚠️ {human_name} Result",
                description=f"SysBot returned: {msg or 'No response message.'}",
                color=PANEL_COLOR_WARN if "warn" in msg.lower() else PANEL_COLOR_OFFLINE,
            )
        resp_embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=resp_embed, ephemeral=True)

        # Update the main panel in-place to reflect updated states
        try:
            status = await fetch_island_status(self.island)
            embed = build_control_panel_embed(self.island, status)
            view = IslandControlPanelView(self.island)
            await interaction.edit_original_response(embed=embed, view=view)
            cog: Any = getattr(interaction.client, "get_cog", lambda _: None)("ControlPanelCog")
            if cog and hasattr(cog, "_sigs") and interaction.channel_id:
                cog._sigs[str(interaction.channel_id)] = (_embed_signature(embed), time.monotonic())
        except Exception as exc:
            logger.debug("[ControlPanel] Could not update original panel: %s", exc)


class IslandControlPanelView(discord.ui.View):
    """Interactive control panel view containing island select and action buttons."""

    def __init__(self, island_name: str):
        super().__init__(timeout=None)
        self.island_name = island_name

        # Row 0: Island selector
        self.add_item(ControlPanelIslandSelect(island_name))

        # Row 1: Bot loop operations
        self.add_item(ControlPanelActionButton("start", island_name, "Start", "🟢", discord.ButtonStyle.success, row=1))
        self.add_item(ControlPanelActionButton("stop", island_name, "Stop", "🔴", discord.ButtonStyle.danger, row=1))
        self.add_item(ControlPanelActionButton("restart", island_name, "Restart", "🔄", discord.ButtonStyle.primary, row=1))
        self.add_item(ControlPanelActionButton("pause", island_name, "Pause", "⏸️", discord.ButtonStyle.secondary, row=1))
        self.add_item(ControlPanelActionButton("resume", island_name, "Resume", "▶️", discord.ButtonStyle.success, row=1))

        # Row 2: Console & hardware controls
        self.add_item(ControlPanelActionButton("screen", island_name, "Screen", "📺", discord.ButtonStyle.secondary, row=2))
        self.add_item(ControlPanelActionButton("detach", island_name, "Detach", "🎮", discord.ButtonStyle.secondary, row=2))
        self.add_item(ControlPanelActionButton("newdodo", island_name, "New Dodo", "🦤", discord.ButtonStyle.primary, row=2))
        self.add_item(ControlPanelActionButton("mash", island_name, "Mash B", "🅱️", discord.ButtonStyle.secondary, row=2))
        self.add_item(ControlPanelActionButton("map", island_name, "Map Refresh", "🗺️", discord.ButtonStyle.secondary, row=2))

        # Row 3: Diagnostics & Info
        self.add_item(ControlPanelActionButton("status", island_name, "Refresh Status", "🔄", discord.ButtonStyle.secondary, row=3))
        self.add_item(ControlPanelActionButton("guide", island_name, "Guide", "📖", discord.ButtonStyle.secondary, row=3))


# ─── Cog Implementation ────────────────────────────────────────────────────────

class ControlPanelCog(commands.Cog, name="ControlPanelCog"):
    """Manages the interactive remote control panel for SysBot islands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sigs: dict[str, tuple[str, float]] = {}  # channel_id -> (signature, last_edit_time)
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        """Register dynamic items on cog load for persistent interaction across bot restarts."""
        try:
            await asyncio.to_thread(_init_panel_table)
            self.bot.add_dynamic_items(ControlPanelIslandSelect, ControlPanelActionButton)
            logger.info("[ControlPanelCog] Dynamic items registered successfully.")
        except Exception as exc:
            logger.warning("[ControlPanelCog] Failed to register dynamic items: %s", exc)

        try:
            if not self.auto_refresh_control_panel_loop.is_running():
                self.auto_refresh_control_panel_loop.start()
        except Exception as exc:
            logger.warning("[ControlPanelCog] Could not start auto_refresh_control_panel_loop: %s", exc)

    async def cog_unload(self) -> None:
        try:
            if self.auto_refresh_control_panel_loop.is_running():
                self.auto_refresh_control_panel_loop.cancel()
        except Exception:
            pass
        try:
            self.bot.remove_dynamic_items(ControlPanelIslandSelect, ControlPanelActionButton)
        except Exception:
            pass

    async def ensure_persistent_panel(self, channel_id: int = PERSISTENT_CONTROL_CHANNEL_ID) -> discord.Message | None:
        """
        Ensure that a persistent, interactive control panel exists and is kept fresh in channel_id.
        Recovers existing messages, edits them with fresh status, or posts a new one if missing.
        """
        async with self._lock:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception as exc:
                    logger.debug("[ControlPanelCog] Could not resolve channel %s: %s", channel_id, exc)
                    return None

            if not hasattr(channel, "send"):
                return None

            panel_data = await asyncio.to_thread(_get_control_panel, channel_id)
            island = (panel_data.get("island_name") if panel_data else None) or _infer_island_from_channel(channel)
            message_id = int(panel_data["message_id"]) if (panel_data and panel_data.get("message_id")) else None

            msg: Optional[discord.Message] = None
            if message_id:
                try:
                    msg = await channel.fetch_message(message_id)
                except (discord.NotFound, discord.HTTPException):
                    msg = None

            # If not found by message_id, check recent history for an existing control panel embed by this bot
            if msg is None and hasattr(channel, "history"):
                try:
                    bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
                    async for candidate in channel.history(limit=25):
                        is_bot_msg = (candidate.author.id == bot_user_id) if bot_user_id else getattr(candidate.author, "bot", False)
                        if is_bot_msg and candidate.embeds:
                            title = candidate.embeds[0].title or ""
                            if "Island Remote Control Panel" in title:
                                msg = candidate
                                break
                except Exception as exc:
                    logger.debug("[ControlPanelCog] Error scanning history in %s: %s", channel_id, exc)

            status = await fetch_island_status(island)
            embed = build_control_panel_embed(island, status)
            view = IslandControlPanelView(island)
            sig = _embed_signature(embed)
            now = time.monotonic()
            cid_str = str(channel_id)

            if msg is not None:
                prev = self._sigs.get(cid_str)
                # If neither the status content changed nor FORCE_EDIT_SECONDS passed, skip edit
                if prev and prev[0] == sig and (now - prev[1]) < FORCE_EDIT_SECONDS:
                    return msg
                try:
                    await msg.edit(embed=embed, view=view)
                    self._sigs[cid_str] = (sig, now)
                    if not message_id or message_id != msg.id:
                        await asyncio.to_thread(_upsert_control_panel, channel_id, island, msg.id)
                    return msg
                except discord.NotFound:
                    msg = None
                except Exception as exc:
                    logger.warning("[ControlPanelCog] Failed to edit panel in %s: %s", channel_id, exc)

            # If no message found or it was deleted, create new panel message
            try:
                msg = await channel.send(embed=embed, view=view)
                self._sigs[cid_str] = (sig, now)
                await asyncio.to_thread(_upsert_control_panel, channel_id, island, msg.id)
                logger.info("[ControlPanelCog] Created persistent control panel in channel %s for %s", channel_id, island)
                return msg
            except Exception as exc:
                logger.error("[ControlPanelCog] Failed to send persistent panel in channel %s: %s", channel_id, exc)
                return None

    # ── Auto-refresh loop ─────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def auto_refresh_control_panel_loop(self) -> None:
        """Background task: periodically refreshes the persistent panel and any other configured panels."""
        if not self.bot.is_ready():
            return

        # 1. Always ensure persistent panel in the dedicated channel (1559482907762884630)
        try:
            await self.ensure_persistent_panel(PERSISTENT_CONTROL_CHANNEL_ID)
        except Exception as exc:
            logger.warning("[ControlPanelCog] Error in ensure_persistent_panel for %s: %s", PERSISTENT_CONTROL_CHANNEL_ID, exc)

        # 2. Also refresh any other channels recorded in the database
        try:
            all_panels = await asyncio.to_thread(_load_all_control_panels)
            for p in all_panels:
                cid = p.get("channel_id")
                if not cid or int(cid) == PERSISTENT_CONTROL_CHANNEL_ID:
                    continue
                try:
                    await self.ensure_persistent_panel(int(cid))
                except Exception as exc:
                    logger.debug("[ControlPanelCog] Error refreshing secondary panel in %s: %s", cid, exc)
        except Exception as exc:
            logger.debug("[ControlPanelCog] Error loading panels in loop: %s", exc)

    @auto_refresh_control_panel_loop.before_loop
    async def before_auto_refresh(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:
            pass

    # ── Autocomplete ──────────────────────────────────────────────────────────

    async def island_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for island choices."""
        cur = (current or "").strip().lower()
        islands = Config.ALL_ISLAND_PORT_ORDER
        if not cur:
            return [app_commands.Choice(name=f"🏝️ {isl}", value=isl) for isl in islands[:25]]

        matches = [isl for isl in islands if cur in isl.lower()][:25]
        return [app_commands.Choice(name=f"🏝️ {m}", value=m) for m in matches]

    # ── Slash Command Group ────────────────────────────────────────────────────

    control_panel_group = app_commands.Group(
        name="control-panel",
        description="SysBot island remote control console",
    )

    @control_panel_group.command(
        name="setup",
        description="Display the interactive island control panel in this channel",
    )
    @app_commands.describe(
        island="Island to control (choose from list or auto-detect from channel)",
    )
    @app_commands.autocomplete(island=island_autocomplete)
    async def control_panel_setup(
        self,
        interaction: discord.Interaction,
        island: str | None = None,
    ) -> None:
        """Handle /control-panel setup command."""
        target_island = _canonical_island(island) if island else _infer_island_from_channel(interaction.channel)

        await interaction.response.defer(thinking=True)
        status = await fetch_island_status(target_island)
        embed = build_control_panel_embed(target_island, status)
        view = IslandControlPanelView(target_island)

        sent_msg = await interaction.followup.send(embed=embed, view=view, wait=True)
        msg_id = getattr(sent_msg, "id", None)
        if interaction.channel_id:
            await asyncio.to_thread(_upsert_control_panel, interaction.channel_id, target_island, msg_id)
            self._sigs[str(interaction.channel_id)] = (_embed_signature(embed), time.monotonic())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ControlPanelCog(bot))
