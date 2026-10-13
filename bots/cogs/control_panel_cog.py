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
import difflib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import Config
from utils.sysbot_api import _INVALID_DODO_CODES, get_island_sysbot_client

logger = logging.getLogger("ControlPanelCog")

# ─── Constants ────────────────────────────────────────────────────────────────

CONTROL_PANEL_ROLE_ID = 1559583848050851980

PANEL_COLOR_ONLINE  = 0x57F287   # Discord Green
PANEL_COLOR_WARN    = 0xFEE75C   # Yellow
PANEL_COLOR_OFFLINE = 0xED4245   # Red
PANEL_FOOTER_TEXT   = "ChoPaeng Camp • Island Control Panel • SysBot Integration"
PANEL_FOOTER_ICON   = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"

CONTROL_GUIDE_DATA = {
    "Start bot": ("POST /api/control/start", "Starts the console loop / reconnects Switch and opens incoming requests."),
    "Stop bot": ("POST /api/control/stop", "Pauses requests, waits for active order, stops console loop & disconnects Switch."),
    "Restart connection": ("POST /api/control/restart", "Pauses requests, finishes active order, then restarts console loop."),
    "Pause requests": ("POST /api/requests/pause", "Pauses incoming requests and queue processing. Active order finishes."),
    "Resume requests": ("POST /api/requests/resume", "Re-opens requests while the console loop continues running."),
    "Screen on / off": ("POST /api/screen", "Toggles Switch display on or off to reduce hardware strain."),
    "Detach controller": ("POST /api/detach", "Detaches the virtual controller from the Switch."),
    "New Dodo code": ("POST /api/dodo/refresh", "Requests a game restart and a fresh Dodo code in restore mode."),
    "Mash B on / off": ("POST /api/togglemashb", "Toggles auto dialogue button presses to advance game dialogues."),
    "Map refresh on / off": ("POST /api/togglerefresh", "Toggles automatic map refresh in restore mode."),
    "Guide": ("GET /api/control/guide", "Returns explanations and metadata for all controls."),
}


def check_control_panel_role(user: discord.User | discord.Member) -> bool:
    """Check if the user has the required control panel role or administrator permissions."""
    if isinstance(user, discord.Member):
        if any(r.id == CONTROL_PANEL_ROLE_ID for r in user.roles):
            return True
        if user.guild_permissions.administrator:
            return True
    return False


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
            f"🔒 **Security:** Restricted to <@&{CONTROL_PANEL_ROLE_ID}>\n"
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
        if not check_control_panel_role(interaction.user):
            await interaction.response.send_message(
                embed=_err_embed(f"You need the <@&{CONTROL_PANEL_ROLE_ID}> role to use the Island Control Panel."),
                ephemeral=True,
            )
            return

        values = getattr(interaction.data, "get", lambda k, d: d)("values", [self.current_island])
        new_island = values[0] if values else self.current_island

        await interaction.response.defer()
        status = await fetch_island_status(new_island)
        embed = build_control_panel_embed(new_island, status)
        view = IslandControlPanelView(new_island)
        await interaction.edit_original_response(embed=embed, view=view)


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
        if not check_control_panel_role(interaction.user):
            await interaction.response.send_message(
                embed=_err_embed(f"You need the <@&{CONTROL_PANEL_ROLE_ID}> role to execute controls."),
                ephemeral=True,
            )
            return

        client = get_island_sysbot_client(self.island)

        # ─── Specialized Actions: Queue, Guide, Refresh Status ─────────────────
        if self.action == "guide":
            embed = discord.Embed(
                title="📖 SysBot Remote Control Guide",
                description="Descriptions and parameters for all 12 SysBot island controls:",
                color=PANEL_COLOR_ONLINE,
            )
            for name, (ep, desc) in CONTROL_GUIDE_DATA.items():
                embed.add_field(name=f"• {name}", value=f"`{ep}`\n{desc}", inline=False)
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            await interaction.response.send_message(embed=embed, ephemeral=True)
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

    async def cog_load(self) -> None:
        """Register dynamic items on cog load for persistent interaction across bot restarts."""
        try:
            self.bot.add_dynamic_items(ControlPanelIslandSelect, ControlPanelActionButton)
            logger.info("[ControlPanelCog] Dynamic items registered successfully.")
        except Exception as exc:
            logger.warning("[ControlPanelCog] Failed to register dynamic items: %s", exc)

    async def cog_unload(self) -> None:
        try:
            self.bot.remove_dynamic_items(ControlPanelIslandSelect, ControlPanelActionButton)
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
        # 1. Enforce Role ID 1559583848050851980 restriction
        if not check_control_panel_role(interaction.user):
            await interaction.response.send_message(
                embed=_err_embed(
                    f"You must have the <@&{CONTROL_PANEL_ROLE_ID}> role to set up or use the Island Control Panel."
                ),
                ephemeral=True,
            )
            return

        # 2. Determine target island
        target_island = _canonical_island(island) if island else _infer_island_from_channel(interaction.channel)

        await interaction.response.defer(thinking=True)
        status = await fetch_island_status(target_island)
        embed = build_control_panel_embed(target_island, status)
        view = IslandControlPanelView(target_island)

        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ControlPanelCog(bot))
