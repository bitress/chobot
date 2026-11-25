"""
Revive — relaunches a crashed SysBot.ACNHOrders instance (used by the control panel's
Revive button; the /revive slash command lives in discord_command_bot.py).

Port of the "Single Island Rescue" .bat: find the island's folder in the General or VIP
fleet, then open a new Windows Terminal tab in that fleet's window running the bot in a
restart loop. Chobot must run on the same PC (and user session) as the bots.
"Sinta" always routes to the Order Bot folder in the General window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import discord

from bots.cogs.control_panel_cog import _err_embed, _embed, _log_audit, fetch_island_status
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
COLOR_WARN = 0xFEE75C
COLOR_FAIL = 0xED4245
STARTUP_GRACE = 90   # seconds a fresh bot gets to bring its API up before it counts as frozen
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
        "Use `/revive` and pick the fleet instead.", "❌ Which fleet?")


# ─── Duplicate check ──────────────────────────────────────────────────────────

# One row per running SysBot.ACNHOrders.exe: its path, age, and whether the restart
# loop (the powershell window that launched it) is still alive.
_PS_LIST = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "$rows = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '" + EXE_NAME + "' } | ForEach-Object { "
    "$parent = Get-Process -Id $_.ParentProcessId; "
    "[pscustomobject]@{ pid = [int]$_.ProcessId; path = [string]$_.ExecutablePath; "
    "age = [int]((Get-Date) - $_.CreationDate).TotalSeconds; "
    "loop = [bool]($parent -and $parent.ProcessName -match 'powershell|pwsh') } }); "
    "ConvertTo-Json -InputObject $rows -Compress"
)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _running_copies(exe_path: str) -> list[dict] | None:
    """Running copies of this exact exe, or None if the process list couldn't be read."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST],
            capture_output=True, text=True, timeout=20,
        )
        rows = json.loads(proc.stdout.strip() or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("[Revive] Could not list running bots: %s", exc)
        return None
    if isinstance(rows, dict):
        rows = [rows]
    return [r for r in rows if isinstance(r, dict) and r.get("path") and _same_path(r["path"], exe_path)]


def _kill(pids: list[int]) -> list[int]:
    """Terminate the given processes; returns the ones that couldn't be closed."""
    failed = []
    for pid in pids:
        try:
            os.kill(pid, 9)   # TerminateProcess on Windows
        except OSError:
            failed.append(pid)
    return failed


def _api_answers(status: dict) -> bool:
    return bool(status.get("success")) and not status.get("unreachable")


# ─── Revive ───────────────────────────────────────────────────────────────────

def _result(title: str, description: str, color: int, name: str, label: str,
            folder: str, user: discord.abc.User) -> discord.Embed:
    embed = _embed(title, description, color)
    embed.add_field(name="🏝️ Island", value=name, inline=True)
    embed.add_field(name="🚢 Fleet", value=label, inline=True)
    embed.add_field(name="👤 Requested by", value=user.mention, inline=True)
    embed.add_field(name="📁 Folder", value=f"`{folder}`", inline=False)
    embed.timestamp = discord.utils.utcnow()
    return embed


async def revive_island(island: str, fleet: str | None, user: discord.abc.User) -> discord.Embed:
    """Relaunch an island's SysBot in its fleet window and return the embed to show.

    Checks for a copy that's already running first, so a revive never puts two bots on
    one island. fleet=None auto-detects it. Used by the control panel's Revive button."""
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
    exe_path = os.path.join(folder, EXE_NAME)

    if not Path(exe_path).is_file():
        return _err_embed(
            f"The **{name}** folder exists, but `{EXE_NAME}` isn't in it, so nothing was launched.\n"
            f"Folder: `{folder}`",
            "❌ Bot program missing")

    def done(action: str, ok: bool, note: str) -> None:
        _log_audit(name, action, user, ok, note)
        logger.info("[Revive] %s [%s] by %s: %s (%s)", name, label, user, action, note)

    async with _REVIVE_LOCK:
        copies = await asyncio.to_thread(_running_copies, exe_path)
        if copies is None:
            await asyncio.to_thread(done, "revive-skipped", False, "could not read process list")
            return _err_embed(
                f"Couldn't check whether **{name}** is already running, so nothing was launched "
                "(to avoid starting a second bot). Try again, or ask an owner to revive it.",
                f"❌ Revive failed: {name}")

        closed_frozen = False
        if copies:
            known = Config.resolve_known_island(name)
            status = await fetch_island_status(known) if known else {}
            if not known or _api_answers(status):
                await asyncio.to_thread(done, "revive-skipped", True, "already running")
                reason = ("its API is answering" if known
                          else "its program is open (I can't check its API for this island name)")
                return _result(
                    f"✋ {name} is already running",
                    f"Nothing was launched: **{name}**'s bot is already running and {reason}.\n"
                    "If it's not working properly, try **Restart** or **Start** on the panel instead.",
                    COLOR_WARN, name, label, folder, user)

            if any(int(c.get("age") or 0) < STARTUP_GRACE for c in copies):
                await asyncio.to_thread(done, "revive-skipped", True, "still starting up")
                return _result(
                    f"⏳ {name} is still starting up",
                    f"Nothing was launched: **{name}**'s bot was started less than "
                    f"{STARTUP_GRACE} seconds ago and is still booting. "
                    "Give it a minute, then refresh the panel.",
                    COLOR_WARN, name, label, folder, user)

            # Program is open but its API doesn't answer: it's frozen. Close it.
            stuck = await asyncio.to_thread(_kill, [int(c["pid"]) for c in copies])
            if stuck:
                await asyncio.to_thread(done, "revive-failed", False, f"could not close PIDs {stuck}")
                return _err_embed(
                    f"**{name}**'s bot is frozen and I couldn't close it, so nothing was launched. "
                    "Ask an owner to close it on the PC.",
                    f"❌ Revive failed: {name}")

            if any(c.get("loop") for c in copies):
                await asyncio.to_thread(done, "revive-unfroze", True, "closed frozen bot; restart loop relaunches it")
                return _result(
                    f"🚑 {name} is restarting",
                    f"**{name}**'s bot was frozen, so I closed it. Its restart loop is bringing it "
                    "back now (about 5 seconds). Give it a minute to reconnect to the Switch "
                    "and get a new Dodo code.",
                    COLOR_OK, name, label, folder, user)
            closed_frozen = True

        ok, err = await asyncio.to_thread(_launch, folder, name, window)

    await asyncio.to_thread(done, f"revive-{label.split()[0].lower()}", ok,
                            err or ("closed frozen bot, launched" if closed_frozen else "launched"))
    if not ok:
        return _result(
            f"❌ Revive failed: {name}",
            f"Couldn't start **{name}**.\n```{err}```\nNothing was launched. You can try again, "
            "or ask an owner to revive it.",
            COLOR_FAIL, name, label, folder, user)
    frozen_note = "Its old bot was frozen, so I closed it first. " if closed_frozen else ""
    return _result(
        f"🚑 {name} is back up",
        f"{frozen_note}**{name}** is starting now. Give it a minute to reconnect to the Switch "
        "and get a new Dodo code.",
        COLOR_OK, name, label, folder, user)
