"""
Revive Cog — /revive restarts a crashed SysBot.ACNHOrders instance from Discord.

Port of the "Single Island Rescue" .bat: pick the fleet and island, check the island's
folder exists, then open a new Windows Terminal tab in that fleet's window running the
bot in a restart loop. Chobot must run on the same PC (and user session) as the bots.

  • /revive fleet:<General|VIP> island:<name>
  • "Sinta" always routes to the Order Bot folder in the General window.
  • Access: CONTROL_PANEL_ROLE_ID holders or Administrators (same as the control panel).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from bots.cogs.control_panel_cog import _err_embed, _embed, _log_audit, panel_access
from utils.config import Config

logger = logging.getLogger("ReviveCog")

EXE_NAME = "SysBot.ACNHOrders.exe"
_BASE = r"C:\Users\ChoPaeng\Documents\ACNH\Orders\Orders\SysBot-ACNH-Orders"
DIR_GEN = os.getenv("REVIVE_DIR_GENERAL", rf"{_BASE}\Orders New ACNH General discord")
DIR_VIP = os.getenv("REVIVE_DIR_VIP", rf"{_BASE}\Orders New Discord")
DIR_SINTA = os.getenv("REVIVE_DIR_SINTA", rf"{_BASE}\Order Bot\SysBot-ACNH-Orders")

# fleet -> (folder, label, wt window name)
FLEETS: dict[str, tuple[str, str, str]] = {
    "General": (DIR_GEN, "General", "GeneralFarm"),
    "VIP": (DIR_VIP, "VIP", "Farm"),
}

# Semicolons escaped as \; so wt.exe doesn't split them into separate commands.
LOOP_CMD = (
    f"while($true) {{ .\\{EXE_NAME} \\; "
    "Write-Host 'Bot down. Restarting in 5 seconds...' -ForegroundColor Red \\; "
    "Start-Sleep -Seconds 5 }"
)

COLOR_OK = 0x57F287
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _launch(folder: str, title: str, window: str) -> tuple[bool, str]:
    wt = shutil.which("wt")
    if not wt:
        return False, "Windows Terminal (wt.exe) was not found on this PC."
    cmd = [wt, "-w", window, "nt", "--title", title, "-d", folder, "powershell", "-Command", LOOP_CMD]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False, "wt.exe did not respond in time."
    except OSError as exc:
        return False, f"Could not start wt.exe: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"wt.exe exited with code {proc.returncode}").strip()[:300]
    return True, ""


_REVIVE_LOCK = asyncio.Lock()


def _resolve_folder(name: str, fleet: str | None) -> tuple[str, str, str] | discord.Embed:
    """(folder, fleet label, wt window) for an island, or an error embed.

    With no fleet, it's found by checking which fleet folder has the island."""
    if name.lower() == "sinta":
        # Sinta lives in the Order Bot folder and always runs in the General window.
        return DIR_SINTA, "General (Orderbot)", "GeneralFarm"

    if fleet:
        base_dir, label, window = FLEETS[fleet]
        folder = os.path.join(base_dir, name)
        if Path(folder).is_dir():
            return folder, label, window
        other = "VIP" if fleet == "General" else "General"
        return _err_embed(
            f"There's no **{name}** folder in the **{label}** fleet, so nothing was launched.\n"
            f"Looked in: `{folder}`\n\n"
            f"Check the spelling, or try `fleet:{other}` if the island is in the other fleet.",
            "❌ Island not found")

    found = [(os.path.join(d, name), label, window) for d, label, window in FLEETS.values()
             if Path(d, name).is_dir()]
    if len(found) == 1:
        return found[0]
    if not found:
        return _err_embed(
            f"There's no **{name}** folder in either fleet, so nothing was launched. "
            "Ask an owner to check the island's folder.", "❌ Island not found")
    return _err_embed(
        f"**{name}** has a folder in both fleets, so I can't tell which one to start. "
        f"Use `/revive fleet:<General|VIP> island:{name}` instead.", "❌ Which fleet?")


async def revive_island(island: str, fleet: str | None, user: discord.abc.User) -> discord.Embed:
    """Relaunch an island's SysBot in its fleet window and return the embed to show.

    Used by /revive and the control panel's Revive button. fleet=None auto-detects it."""
    # Strip accidental spaces (same as the bat), then allow only plain folder names.
    name = island.replace(" ", "")
    if not _SAFE_NAME.fullmatch(name):
        return _err_embed(
            f"**{island[:40]}** isn't a valid island name. Use only letters, numbers, `-` and `_` "
            "(for example `Hiraya`).", "❌ Invalid island name")

    resolved = _resolve_folder(name, fleet)
    if isinstance(resolved, discord.Embed):
        return resolved
    folder, label, window = resolved

    if not Path(folder, EXE_NAME).is_file():
        return _err_embed(
            f"The **{name}** folder exists, but `{EXE_NAME}` isn't in it, so nothing was launched.\n"
            f"Folder: `{folder}`",
            "❌ Bot program missing")

    async with _REVIVE_LOCK:
        ok, err = await asyncio.to_thread(_launch, folder, name, window)

    await asyncio.to_thread(_log_audit, name, f"revive-{label.split()[0].lower()}", user, ok, err or "launched")
    logger.info("[Revive] %s [%s] -> %s by %s: %s", name, label, window, user, "ok" if ok else err)

    if ok:
        embed = _embed(
            f"🚑 {name} is back up",
            f"**{name}** is starting now. Give it a minute to reconnect to the Switch "
            "and get a new Dodo code.",
            COLOR_OK,
        )
    else:
        embed = _err_embed(
            f"Couldn't start **{name}**.\n```{err}```\nNothing was launched. You can try again, "
            "or ask an owner to revive it.",
            f"❌ Revive failed: {name}",
        )
    embed.add_field(name="🏝️ Island", value=name, inline=True)
    embed.add_field(name="🚢 Fleet", value=label, inline=True)
    embed.add_field(name="👤 Revived by", value=user.mention, inline=True)
    embed.add_field(name="📁 Folder", value=f"`{folder}`", inline=False)
    if ok:
        embed.add_field(
            name="⚠️ Heads up",
            value="If the island was already restarting on its own, ask an owner to check "
                  "there aren't two bots running on it.",
            inline=False,
        )
    embed.timestamp = discord.utils.utcnow()
    return embed


class ReviveCog(commands.Cog, name="ReviveCog"):
    """Re-deploys a crashed SysBot island into its fleet's Windows Terminal window."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def island_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cur = (current or "").strip().lower()
        return [app_commands.Choice(name=i, value=i) for i in Config.ALL_ISLAND_PORT_ORDER if cur in i.lower()][:25]

    @app_commands.command(name="revive", description="Restart a crashed SysBot island in its fleet window")
    @app_commands.describe(fleet="Which fleet the Dodo crashed in", island="Exact island name to revive")
    @app_commands.guild_only()
    @panel_access()
    @app_commands.autocomplete(island=island_autocomplete)
    async def revive(self, interaction: discord.Interaction, fleet: Literal["General", "VIP"], island: str) -> None:
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=await revive_island(island, fleet, interaction.user))
