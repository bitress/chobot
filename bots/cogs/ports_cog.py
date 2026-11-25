"""
/ports — lists the TCP ports listening on the PC Chobot runs on, with the program that
owns each one, and marks which of Chobot's own ports (SysBot islands, bridge, API,
database) are open or closed. Admins only; the reply is visible only to the caller.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import subprocess

import discord
from discord import app_commands
from discord.ext import commands

from utils.bridge_launcher import _bridge_env_value
from utils.config import Config

logger = logging.getLogger("PortsCog")

API_PORT = 8100   # main.py runs the Flask API here
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


# ─── Reading the port list ────────────────────────────────────────────────────

def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout


def _process_names() -> dict[int, str]:
    """PID -> program name, from tasklist."""
    names: dict[int, str] = {}
    for row in csv.reader(io.StringIO(_run(["tasklist", "/fo", "csv", "/nh"]))):
        if len(row) >= 2 and row[1].isdigit():
            names[int(row[1])] = row[0]
    return names


def _listening() -> list[dict]:
    """One row per (port, program): {port, pid, name, addrs}. Raises OSError on failure."""
    if os.name != "nt":
        raise OSError("/ports only works when Chobot runs on Windows.")
    out = _run(["netstat", "-ano", "-p", "TCP"]) + _run(["netstat", "-ano", "-p", "TCPv6"])
    names = _process_names()
    rows: dict[tuple[int, int], dict] = {}
    for line in out.splitlines():
        parts = line.split()
        # Proto  Local Address  Foreign Address  State  PID
        if len(parts) != 5 or parts[3].upper() != "LISTENING" or not parts[4].isdigit():
            continue
        addr, _, port = parts[1].rpartition(":")
        if not port.isdigit():
            continue
        pid = int(parts[4])
        row = rows.setdefault((int(port), pid), {
            "port": int(port), "pid": pid, "name": names.get(pid, "?"), "addrs": set(),
        })
        row["addrs"].add(addr.strip("[]"))
    if not rows and not out.strip():
        raise OSError("netstat returned nothing.")
    return sorted(rows.values(), key=lambda r: (r["port"], r["pid"]))


def _reach(addrs: set[str]) -> str:
    """Who can connect: 'network' (any address), 'this PC' (loopback) or the bound IP."""
    if addrs & {"0.0.0.0", "::"}:
        return "network"
    if addrs <= {"127.0.0.1", "::1"}:
        return "this PC"
    return ", ".join(sorted(addrs))


# ─── Chobot's own ports ───────────────────────────────────────────────────────

def _expected() -> dict[int, str]:
    """Port -> label for the ports Chobot itself relies on."""
    ports = {API_PORT: "Chobot API (Flask)"}
    try:
        ports[int(_bridge_env_value("BRIDGE_PORT", "8200"))] = "SysBot bridge"
    except ValueError:
        pass
    if Config.DB_BACKEND != "sqlite" and str(Config.MYSQL_HOST).lower() in _LOCAL_HOSTS:
        ports[int(Config.MYSQL_PORT)] = "Database (MySQL/MariaDB)"
    return ports


def _island_ports() -> dict[int, str]:
    base = int(getattr(Config, "SYSBOT_BASE_PORT", 8201) or 8201)
    return {base + i: name for i, name in enumerate(Config.ALL_ISLAND_PORT_ORDER)}


def _cap(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_ports_report() -> tuple[discord.Embed, discord.File | None]:
    rows = _listening()
    open_ports = {r["port"] for r in rows}

    islands = _island_ports()
    closed = [f"{name} ({port})" for port, name in islands.items() if port not in open_ports]
    services = [
        f"{'🟢' if port in open_ports else '🔴'} `{port}` {label}"
        for port, label in sorted(_expected().items())
    ]

    embed = discord.Embed(
        title="🔌 Open ports on the Chobot PC",
        description=(f"**{len(open_ports)}** TCP ports are listening. "
                     "**network** = other devices can connect; **this PC** = only local programs."),
        color=0x57F287 if not closed else 0xFEE75C,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Chobot services", value="\n".join(services), inline=False)
    if not closed:
        island_note = "🟢 All island ports are open."
    elif len(closed) == len(islands):
        island_note = (f"🔴 None are listening (ports {min(islands)}–{max(islands)}). "
                       "Are the SysBots running on this PC?")
    else:
        island_note = _cap("🔴 Not listening: " + ", ".join(closed))
    embed.add_field(
        name=f"SysBot islands: {len(islands) - len(closed)}/{len(islands)} listening",
        value=island_note,
        inline=False,
    )

    labels = {**islands, **_expected()}
    lines = [f"{'PORT':>5}  {'REACH':<8}  {'PID':>6}  PROGRAM"]
    for r in rows:
        tag = f"  ← {labels[r['port']]}" if r["port"] in labels else ""
        lines.append(f"{r['port']:>5}  {_reach(r['addrs']):<8}  {r['pid']:>6}  {r['name']}{tag}")
    table = "\n".join(lines)

    # The full table goes inline when it fits an embed field, otherwise as a text file.
    if len(table) + 8 <= 1024:
        embed.add_field(name="All listening ports", value=f"```\n{table}\n```", inline=False)
        return embed, None
    embed.add_field(name="All listening ports", value="Full list attached as `ports.txt`.", inline=False)
    return embed, discord.File(io.BytesIO(table.encode("utf-8")), filename="ports.txt")


# ─── Command ──────────────────────────────────────────────────────────────────

def _can_use(user: discord.abc.User) -> bool:
    """Admins only: Administrator permission, the server owner, or the Admin role."""
    perms = getattr(user, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    guild = getattr(user, "guild", None)
    if guild and guild.owner_id == user.id:
        return True
    admin_role = getattr(Config, "ADMIN_ROLE_ID", None)
    return bool(admin_role) and any(r.id == admin_role for r in getattr(user, "roles", []))


class PortsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ports", description="[Admin Only] List the open ports on the Chobot PC")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def ports(self, interaction: discord.Interaction):
        if not _can_use(interaction.user):
            await interaction.response.send_message(
                "⛔ **Access Denied:** `/ports` is restricted to Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed, file = await asyncio.to_thread(build_ports_report)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("[Ports] Could not read open ports: %s", exc)
            await interaction.followup.send(f"❌ Couldn't read the port list: {exc}", ephemeral=True)
            return
        kwargs = {"file": file} if file else {}
        await interaction.followup.send(embed=embed, ephemeral=True, **kwargs)
