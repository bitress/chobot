"""
ControlPanel Cog — Island remote control panel for SysBot.ACNHOrders.

Persistent, interaction-only remote control panels in Discord.

  • /control-panel setup [island]   (Manage Server) – post a panel in this channel
  • Access: members with CONTROL_PANEL_ROLE_ID, or Administrators (see REQUIRE_ROLE)
  • Controls: start / stop / restart, pause / resume requests, screen, detach controller,
    new Dodo, mash B, map refresh, refresh status, guide, and an island switcher.
  • Risky actions (stop, restart, new Dodo) ask for confirmation first.
  • Every action is written to `control_panel_audit` and the log.

Buttons and the island select are DynamicItems (custom_id carries action + island), registered
once in cog_load(), so panels keep working across restarts. Requires discord.py >= 2.4.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import math
import re
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.config import Config
from utils.database import connect_db
from utils.sysbot_api import _INVALID_DODO_CODES, get_island_sysbot_client

try:
    from utils.sysbot_telemetry import (
        parse_sysbot_visitors,
        get_cached_island_telemetry,
        get_recent_island_events,
    )
except ImportError:
    parse_sysbot_visitors = None  # type: ignore
    get_cached_island_telemetry = None  # type: ignore
    get_recent_island_events = None  # type: ignore

logger = logging.getLogger("ControlPanelCog")

# ─── Constants ────────────────────────────────────────────────────────────────

CONTROL_PANEL_ROLE_ID = int(getattr(Config, "CONTROL_PANEL_ROLE_ID", 0) or 1559583848050851980)
PERSISTENT_CONTROL_CHANNEL_ID = int(getattr(Config, "CONTROL_PANEL_CHANNEL_ID", 0) or 1559482907762884630)

# True: only CONTROL_PANEL_ROLE_ID holders / Administrators can press anything (as documented).
# False: anyone who can see the channel can (the previous behaviour). Set Config.CONTROL_PANEL_REQUIRE_ROLE.
REQUIRE_ROLE = bool(getattr(Config, "CONTROL_PANEL_REQUIRE_ROLE", True))
# The Dodo is a secret; set Config.CONTROL_PANEL_SHOW_DODO = False to hide it on the panel.
SHOW_DODO_ON_PANEL = bool(getattr(Config, "CONTROL_PANEL_SHOW_DODO", True))

WEBSITE_BASE = "https://www.chopaeng.com"
DEFAULT_CONTROL_ISLAND = Config.SUB_ISLANDS[0] if Config.SUB_ISLANDS else "Sinta"

FORCE_EDIT_SECONDS  = 300.0   # re-edit an unchanged panel at least this often
REFRESH_CONCURRENCY = 4
STATUS_TIMEOUT      = 6.0
ACTION_TIMEOUT      = 25.0
ACTION_COOLDOWN     = 3.0     # per island + action
SETTLE_DELAY        = 1.0     # let SysBot settle before re-reading status after an action

PANEL_COLOR_ONLINE      = 0x57F287   # green
PANEL_COLOR_WARN        = 0xFEE75C   # yellow
PANEL_COLOR_OFFLINE     = 0xED4245   # red
PANEL_COLOR_UNREACHABLE = 0x95A5A6   # grey
PANEL_FOOTER_TEXT = "ChoPaeng Camp • Island Control Panel • SysBot Integration"
PANEL_FOOTER_ICON = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"


# ─── Action registry (single source of truth for buttons, guide and result text) ─

@dataclass(frozen=True)
class ActionSpec:
    key: str
    label: str
    emoji: str
    style: discord.ButtonStyle
    row: int
    title: str               # shown in result embeds
    guide_name: str          # shown in the guide
    help: str                # shown in the guide
    confirm: str | None = None   # if set, the user must confirm first
    local: bool = False          # handled here, not sent to SysBot


_S = discord.ButtonStyle
ACTIONS: dict[str, ActionSpec] = {
    s.key: s
    for s in (
        ActionSpec("start", "Start", "🟢", _S.success, 1, "Start bot loop", "Start Bot",
                   "Starts the console loop, reconnects Switch, and opens incoming requests."),
        ActionSpec("stop", "Stop", "🔴", _S.danger, 1, "Stop bot loop", "Stop Bot",
                   "Pauses requests, finishes active order, stops loop & disconnects Switch.",
                   confirm="Pauses requests, finishes the active order, stops the loop and disconnects the Switch."),
        ActionSpec("restart", "Restart", "🔄", _S.primary, 1, "Restart connection", "Restart Connection",
                   "Pauses requests, finishes active order, then restarts the console loop.",
                   confirm="Pauses requests, finishes the active order, then restarts the console loop."),
        ActionSpec("pause", "Pause", "⏸️", _S.secondary, 1, "Pause requests", "Pause Requests",
                   "Pauses incoming requests and queue processing. Active order finishes."),
        ActionSpec("resume", "Resume", "▶️", _S.success, 1, "Resume requests", "Resume Requests",
                   "Re-opens incoming requests while the console loop continues running."),
        ActionSpec("screen", "Screen", "📺", _S.secondary, 2, "Toggle screen", "Screen On / Off",
                   "Toggles Switch display output on or off to reduce hardware strain."),
        ActionSpec("detach", "Detach", "🎮", _S.secondary, 2, "Detach controller", "Detach Controller",
                   "Detaches the virtual controller from the Switch."),
        ActionSpec("newdodo", "New Dodo", "🦤", _S.primary, 2, "Request new Dodo", "New Dodo Code",
                   "Requests a game restart and a fresh Dodo code in restore mode.",
                   confirm="Restarts the game to get a fresh Dodo code — anyone currently on the island will be disconnected."),
        ActionSpec("mash", "Mash B", "🅱️", _S.secondary, 2, "Toggle Mash B", "Mash B On / Off",
                   "Toggles automatic dialogue button presses to advance dialogues."),
        ActionSpec("map", "Map Refresh", "🗺️", _S.secondary, 2, "Toggle Map Refresh", "Map Refresh On / Off",
                   "Toggles automatic map refresh in restore mode."),
        ActionSpec("status", "Refresh Status", "🔄", _S.secondary, 3, "Refresh status", "Refresh Status",
                   "Fetches live status and Dodo code from SysBot to refresh the panel in-place.", local=True),
        ActionSpec("echo", "Echo", "📣", _S.secondary, 3, "Echo broadcast settings", "Echo Broadcasts",
                   "Toggles automated announcement echoes for Dodo codes, arrivals, and updates.", local=True),
        ActionSpec("logs", "Logs", "📜", _S.secondary, 3, "Recent logs", "Recent Logs",
                   "Shows the most recent drop logs for this island.", local=True),
        ActionSpec("guide", "Guide", "📖", _S.secondary, 3, "Guide", "Guide",
                   "Shows this help.", local=True),
        ActionSpec("switch", "All Islands", "🌐", _S.secondary, 3, "Browse all islands", "Browse Islands",
                   "Switch the control panel to any of the 48 Sub and Free islands across the cluster.", local=True),
    )
}


# ─── Small in-memory guards ────────────────────────────────────────────────────

_COOLDOWNS: dict[tuple[str, str], float] = {}
_ISLAND_LOCKS: dict[str, asyncio.Lock] = {}
_CONSOLE_STATES: dict[str, dict[str, Any]] = {}


def _cooldown_left(island: str, action: str, seconds: float = ACTION_COOLDOWN) -> float:
    """Seconds still to wait; 0.0 (and the cooldown is armed) if the action may proceed."""
    now = time.monotonic()
    for k in [k for k, t in _COOLDOWNS.items() if now - t > 60]:
        del _COOLDOWNS[k]
    key = (island.lower(), action)
    last = _COOLDOWNS.get(key)
    if last is not None and now - last < seconds:
        return seconds - (now - last)
    _COOLDOWNS[key] = now
    return 0.0


def _island_lock(island: str) -> asyncio.Lock:
    return _ISLAND_LOCKS.setdefault(island.lower(), asyncio.Lock())


# ─── Database (synchronous; call through asyncio.to_thread) ────────────────────

def _init_panel_table() -> None:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS control_panel_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    island_name TEXT,
                    action TEXT,
                    user_id TEXT,
                    user_name TEXT,
                    ok INTEGER,
                    message TEXT,
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
                CREATE TABLE IF NOT EXISTS island_echo_settings (
                    island_name     TEXT PRIMARY KEY,
                    echo_dodo       INTEGER NOT NULL DEFAULT 1,
                    echo_arrivals   INTEGER NOT NULL DEFAULT 1,
                    echo_updates    INTEGER NOT NULL DEFAULT 1,
                    updated_at      TEXT
                )
                """
            )
            conn.commit()
    except Exception as exc:
        logger.error("[ControlPanel] Failed to initialize database tables: %s", exc)


def _get_echo_settings(island_name: str) -> dict[str, bool] | None:
    """Retrieve saved echo broadcast settings from the backend database."""
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT echo_dodo, echo_arrivals, echo_updates FROM island_echo_settings WHERE island_name = ?",
                (island_name.strip().lower(),),
            ).fetchone()
            if row:
                return {
                    "echo_dodo_enabled": bool(row["echo_dodo"]),
                    "echo_arrivals_enabled": bool(row["echo_arrivals"]),
                    "echo_island_updates_enabled": bool(row["echo_updates"]),
                }
    except Exception as exc:
        logger.debug("[ControlPanel] Failed to get echo settings for %s: %s", island_name, exc)
    return None


def _save_echo_settings(island_name: str, dodo: bool, arrivals: bool, updates: bool) -> None:
    """Persist echo broadcast settings for an island in the backend database."""
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO island_echo_settings (island_name, echo_dodo, echo_arrivals, echo_updates, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(island_name) DO UPDATE SET
                    echo_dodo = excluded.echo_dodo,
                    echo_arrivals = excluded.echo_arrivals,
                    echo_updates = excluded.echo_updates,
                    updated_at = excluded.updated_at
                """,
                (island_name.strip().lower(), int(dodo), int(arrivals), int(updates), now_iso),
            )
            conn.commit()
    except Exception as exc:
        logger.error("[ControlPanel] Failed to save echo settings for %s: %s", island_name, exc)


def _save_echo_settings_all(dodo: bool, arrivals: bool, updates: bool) -> None:
    """Persist echo broadcast settings for all 48 islands in the backend database."""
    try:
        sub_list, free_list = get_sub_and_free_islands()
        all_islands = sorted(set(sub_list + free_list), key=lambda s: s.lower())
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with connect_db() as conn:
            for isl in all_islands:
                conn.execute(
                    """
                    INSERT INTO island_echo_settings (island_name, echo_dodo, echo_arrivals, echo_updates, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(island_name) DO UPDATE SET
                        echo_dodo = excluded.echo_dodo,
                        echo_arrivals = excluded.echo_arrivals,
                        echo_updates = excluded.echo_updates,
                        updated_at = excluded.updated_at
                    """,
                    (isl.strip().lower(), int(dodo), int(arrivals), int(updates), now_iso),
                )
            conn.commit()
        logger.info("[ControlPanel] Persisted echo settings for %d islands to backend database.", len(all_islands))
    except Exception as exc:
        logger.error("[ControlPanel] Failed to save cluster echo settings: %s", exc)


async def _broadcast_echo_to_cluster(dodo: bool, arrivals: bool, updates: bool) -> None:
    """Push echo settings via HTTP to all reachable SysBots across the cluster."""
    sub_list, free_list = get_sub_and_free_islands()
    all_islands = sorted(set(sub_list + free_list), key=lambda s: s.lower())
    sem = asyncio.Semaphore(8)

    async def _push_one(isl: str) -> None:
        async with sem:
            try:
                client = get_island_sysbot_client(isl)
                await asyncio.wait_for(
                    client.toggle_echo(
                        echo_type="all",
                        enabled=None,
                        echo_dodo_enabled=dodo,
                        echo_arrivals_enabled=arrivals,
                        echo_island_updates_enabled=updates,
                    ),
                    timeout=3.0,
                )
            except Exception as err:
                logger.debug("[ControlPanel] Cluster echo push to %s: %s", isl, err)

    await asyncio.gather(*(_push_one(i) for i in all_islands), return_exceptions=True)
    logger.info("[ControlPanel] Finished cluster broadcast of echo settings to %d islands.", len(all_islands))


def _upsert_control_panel(channel_id: int, island_name: str, message_id: int | None = None) -> None:
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
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT channel_id, island_name, message_id, updated_at FROM island_control_panels"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to load all panels: %s", exc)
        return []


def _log_audit(island: str, action: str, user: discord.abc.User, ok: bool, message: str) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO control_panel_audit (island_name, action, user_id, user_name, ok, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (island, action, str(user.id), str(user), 1 if ok else 0, (message or "")[:500]),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to write audit log: %s", exc)


# ─── Island helpers ────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _island_map() -> dict[str, str]:
    return {_norm(i): i for i in Config.ALL_ISLAND_PORT_ORDER if _norm(i)}


def _canonical_island(name: str | None) -> str | None:
    """Exact (normalised) match, else a *unique* prefix match; None if unknown or ambiguous."""
    key = _norm(re.sub(r"^sub\s+island\s+", "", name or "", flags=re.IGNORECASE))
    if not key:
        return None
    islands = _island_map()
    if key in islands:
        return islands[key]
    prefixed = [i for n, i in islands.items() if n.startswith(key)]
    return prefixed[0] if len(prefixed) == 1 else None


def _infer_island_from_channel(channel: discord.abc.GuildChannel | discord.Thread | None) -> str | None:
    """Whole-word, longest match of the channel name against known islands (e.g. #hiraya -> Hiraya)."""
    if isinstance(channel, discord.Thread):
        channel = channel.parent
    name = _norm(getattr(channel, "name", "") or "")
    if not name:
        return None
    padded = f" {name} "
    best: str | None = None
    for inorm, island in _island_map().items():
        if f" {inorm} " in padded and (best is None or len(inorm) > len(_norm(best))):
            best = island
    return best


def _island_url(island: str) -> str:
    return f"{WEBSITE_BASE}/island/{urllib.parse.quote(island.lower(), safe='')}"


def _endpoint_port(island: str) -> str | None:
    """Only the port is shown; the full SysBot URL (host/IP) is never put in a channel."""
    try:
        port = urllib.parse.urlparse(str(Config.get_island_sysbot_url(island))).port
        return str(port) if port else None
    except Exception:
        return None


def _cid(action: str, island: str) -> str:
    """custom_id = ctrlpanel:<action>:<url-quoted island> (quoting keeps ':' out of the island part)."""
    return f"ctrlpanel:{action}:{urllib.parse.quote(island, safe='')}"


# ─── Access ────────────────────────────────────────────────────────────────────

def is_authorized(user: discord.User | discord.Member) -> bool:
    """Administrators and CONTROL_PANEL_ROLE_ID holders (or everyone, if REQUIRE_ROLE is off)."""
    if not REQUIRE_ROLE:
        return True
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return any(r.id == CONTROL_PANEL_ROLE_ID for r in user.roles)


check_control_panel_role = is_authorized   # backwards-compatible name


def panel_access():
    """Slash command check requiring control-panel role or Administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_authorized(interaction.user):
            raise app_commands.CheckFailure("You need the control-panel role (or Administrator) to use these controls.")
        return True
    return app_commands.check(predicate)


async def _resolve_target(interaction: discord.Interaction, island: str | None) -> str | None:
    """Resolve target island for commands/panels, prioritizing explicit name, then channel DB row, then channel name."""
    if island:
        return _canonical_island(island)      # None -> caller replies "Unknown island"
    row = await asyncio.to_thread(_get_control_panel, interaction.channel_id)
    if row and (t := _canonical_island(row.get("island_name"))):
        return t
    return _infer_island_from_channel(interaction.channel)


def get_sub_and_free_islands() -> tuple[list[str], list[str]]:
    """Single source of truth for Sub/Order and Free islands across the cluster."""
    sub_list = list(dict.fromkeys(list(Config.SUB_ISLANDS) + ["Sinta"]))
    free_list = [i for i in Config.ALL_ISLAND_PORT_ORDER if i not in Config.SUB_ISLANDS and i != "Sinta"]
    return sub_list, free_list


# ─── Embeds ────────────────────────────────────────────────────────────────────

def _embed_signature(embed: discord.Embed) -> str:
    """Stable fingerprint of an embed ignoring changing timestamp fields, to skip no-op edits."""
    d = embed.to_dict()
    d.pop("timestamp", None)
    if "fields" in d:
        d["fields"] = [f for f in d["fields"] if f.get("name") != "🕒 Last Updated"]
    return json.dumps(d, sort_keys=True)


def _embed(title: str, description: str, color: int) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return e


def _err_embed(message: str, title: str = "🚫 Access Denied") -> discord.Embed:
    return _embed(title, message, PANEL_COLOR_OFFLINE)


def _denied_embed() -> discord.Embed:
    return _err_embed("You need the control-panel role (or Administrator) to use these controls.")


def build_guide_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📖 Island Remote Control Guide",
        description="Quick reference for the controls on the Sub-Island Remote Control Panel.",
        color=PANEL_COLOR_ONLINE,
    )
    for spec in ACTIONS.values():
        if spec.key == "guide":
            continue
        warn = "  ⚠️ _asks to confirm_" if spec.confirm else ""
        embed.add_field(name=f"{spec.emoji} {spec.guide_name}", value=spec.help + warn, inline=False)
    embed.add_field(
        name="🏝️ Switch Island",
        value="Use the top dropdown to switch the control panel target to any sub-island.",
        inline=False,
    )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


def _build_island_logs_embed(island: str, limit: int = 20, status: dict | None = None) -> discord.Embed:
    """Build an ephemeral embed showing the most recent drop logs and echo broadcast data for the island."""
    drops_rows = []
    echo_audit_rows = []
    try:
        with connect_db() as conn:
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
            drops_rows = conn.execute(
                """
                SELECT id, island_name, items, requested_by_user_id, status, response_message, created_at
                FROM island_drops_log
                WHERE LOWER(island_name) = LOWER(?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (island.strip(), limit),
            ).fetchall()

            echo_audit_rows = conn.execute(
                """
                SELECT action, user_name, ok, message, created_at
                FROM control_panel_audit
                WHERE LOWER(island_name) = LOWER(?)
                  AND (action LIKE 'echo%' OR action = 'echo')
                ORDER BY id DESC
                LIMIT 5
                """,
                (island.strip(),),
            ).fetchall()
    except Exception as exc:
        logger.error("[ControlPanel] Failed to query logs for %s: %s", island, exc, exc_info=True)
        return _err_embed("Unable to retrieve logs at this time. Please try again later.", "❌ Database Error")

    # Fetch live events from SysBot
    events = []
    client = None
    try:
        client = get_island_sysbot_client(island)
        resp = client.get_events_sync(limit=40)
        events = (resp.get("events") if isinstance(resp, dict) else resp) or []
    except Exception as exc:
        logger.debug("[ControlPanel] Failed to fetch live events for %s: %s", island, exc)

    # Fallback to events in status if available
    if not events and status and isinstance(status, dict):
        events = list(status.get("recent_events") or [])

    # Fallback to SQLite island_events_log if events buffer is empty (e.g. Echo disabled)
    if not events and get_recent_island_events is not None:
        try:
            db_evts = get_recent_island_events(island_name=island, limit=40)
            if db_evts:
                events = [
                    {
                        "Type": de.get("event_type"),
                        "Message": de.get("message"),
                        "Player": de.get("player"),
                        "Island": de.get("origin_island"),
                        "Timestamp": de.get("timestamp"),
                    }
                    for de in db_evts
                ]
        except Exception as exc:
            logger.debug("[ControlPanel] Failed to fetch db events for %s: %s", island, exc)

    # If status wasn't provided, attempt to fetch status/echo synchronously
    if status is None:
        try:
            client = client or get_island_sysbot_client(island)
            status = client.get_status_sync()
            if not status or not status.get("success"):
                status = client.get_echo_status_sync()
        except Exception:
            status = None

    embed = discord.Embed(
        title=f"📜 Live Activity Logs & Echo Data — {island}",
        color=PANEL_COLOR_ONLINE if (drops_rows or echo_audit_rows or events) else PANEL_COLOR_UNREACHABLE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)

    data = status or {}

    def _badge(val: Any) -> str:
        if val is True or str(val).lower() in ("true", "1", "on"):
            return "🟢 `ON`"
        if val is False or str(val).lower() in ("false", "0", "off"):
            return "🔴 `OFF`"
        return "⚪ `—`"

    def _fmt_evt_time(evt: dict) -> str:
        lt = str(evt.get("LocalTime") or evt.get("localTime") or evt.get("local_time") or "")
        if " " in lt:
            parts = lt.split(" ")
            if len(parts) >= 3:
                return f"{parts[1][:5]} {parts[2]}"
            return parts[1][:8]
        ts = str(evt.get("Timestamp") or evt.get("timestamp") or "")
        if "T" in ts:
            return ts.split("T")[1][:8]
        return lt[:10] or ts[:8] or "—"

    # 1. Echo Broadcast Status
    dodo_on = data.get("echo_dodo_enabled")
    arrivals_on = data.get("echo_arrivals_enabled")
    updates_on = data.get("echo_island_updates_enabled")

    echo_summary = (
        f"• **Dodo Broadcast:** {_badge(dodo_on)}  "
        f"• **Arrivals/Departures:** {_badge(arrivals_on)}  "
        f"• **Island State Updates:** {_badge(updates_on)}"
    )
    embed.add_field(name="📣 Live Echo Configuration", value=echo_summary, inline=False)

    # 2. Dodo Broadcast Data Log (Always shown in logs even if Discord channel broadcast is disabled)
    dodo_code = data.get("dodo_code") or "—"
    is_valid = data.get("is_dodo_valid")
    last_dodo_fetch = data.get("last_dodo_fetch") or "—"
    valid_str = "🟢 Valid" if is_valid is True else ("🔴 Expired/Pending" if is_valid is False else "⚪ —")

    dodo_events = []
    state_events = []
    flight_events = []

    for e in events:
        t = str(e.get("Type") or e.get("type") or e.get("event_type") or "").lower()
        m = str(e.get("Message") or e.get("message") or "")
        m_lower = m.lower()
        dodo_code_val = e.get("DodoCode") or e.get("dodoCode") or e.get("dodo_code")
        if t in ("dodo", "crash") or "dodo" in m_lower or "crash" in m_lower or dodo_code_val:
            dodo_events.append(e)
        elif t in ("arrival", "departure") or "joining" in m_lower or "departed" in m_lower:
            flight_events.append(e)
        elif t in ("layer", "system", "state", "clean", "gate", "echo") or any(
            k in m_lower for k in ("layer", "unstick", "controller", "refresh", "clean", "mash", "commands", "gate", "queue")
        ):
            state_events.append(e)

    # Fallback to recorded island visits if flight_events is empty
    if not flight_events:
        try:
            with connect_db() as conn:
                v_rows = conn.execute(
                    """
                    SELECT ign, origin_island, timestamp
                    FROM island_visits
                    WHERE LOWER(destination) = LOWER(?)
                    ORDER BY timestamp DESC
                    LIMIT 5
                    """,
                    (island.strip(),),
                ).fetchall()
                for vr in v_rows:
                    vr_dict = dict(vr)
                    ign = vr_dict.get("ign") or "Visitor"
                    orig = vr_dict.get("origin_island")
                    ts = vr_dict.get("timestamp") or 0
                    t_str = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%H:%M:%S") if ts else "—"
                    flight_events.append({
                        "Type": "arrival",
                        "Message": f"{ign} from {orig} joined {island}." if orig else f"{ign} joined {island}.",
                        "Timestamp": t_str,
                    })
        except Exception as exc:
            logger.debug("[ControlPanel] Fallback visit query error for %s: %s", island, exc)

    dodo_lines = [
        f"• **Current Dodo:** `{dodo_code}` ({valid_str}) • **Last Update:** `{last_dodo_fetch}`"
    ]
    if dodo_events:
        for e in dodo_events[:5]:
            t_str = _fmt_evt_time(e)
            msg = str(e.get("Message") or e.get("message") or "").strip()
            icon = "⚠️" if "crash" in str(e.get("Type") or e.get("type") or "").lower() or "crash" in msg.lower() else "🦤"
            dodo_lines.append(f"`{t_str}` {icon} {msg[:80]}")
    elif dodo_on is False:
        dodo_lines.append("• *Channel echo broadcast is OFF. Live Dodo code is tracked privately above.*")
    else:
        dodo_lines.append("*No recent Dodo broadcast events in live buffer.*")

    embed.add_field(name="🦤 Dodo Code Broadcast Logs", value="\n".join(dodo_lines)[:1020], inline=False)

    # 3. Island State Updates Data Log (Always shown in logs even if Discord channel broadcast is disabled)
    current_layer = data.get("layer") or data.get("current_layer") or "Default"
    queue_count = data.get("queue_count", 0)
    accepting = data.get("accepting_commands")
    accept_str = "🟢 Yes" if accepting is True else ("🔴 No" if accepting is False else "⚪ —")

    state_lines = [
        f"• **Layer:** `{current_layer}` • **Queue:** `{queue_count}` • **Accepting Commands:** {accept_str}"
    ]
    if state_events:
        for e in state_events[:5]:
            t_str = _fmt_evt_time(e)
            msg = str(e.get("Message") or e.get("message") or "").strip()
            msg_lower = msg.lower()
            if "layer" in msg_lower:
                icon = "🗺️"
            elif "unstick" in msg_lower or "mash" in msg_lower:
                icon = "🕹️"
            elif "clean" in msg_lower or "refresh" in msg_lower:
                icon = "🧹"
            elif "echo" in msg_lower:
                icon = "📣"
            else:
                icon = "📢"
            state_lines.append(f"`{t_str}` {icon} {msg[:80]}")
    elif updates_on is False:
        state_lines.append("• *Channel echo broadcast is OFF. Live states tracked privately above.*")
    else:
        state_lines.append("*No recent island state update events in live buffer.*")

    embed.add_field(name="📢 Island State Updates Logs", value="\n".join(state_lines)[:1020], inline=False)

    # 4. Flight Events (Always shown in logs even if Discord channel broadcast is disabled)
    if flight_events:
        flight_lines = []
        for e in flight_events[:5]:
            t_str = _fmt_evt_time(e)
            msg = str(e.get("Message") or e.get("message") or "").strip()
            icon = "🛬" if "arrival" in str(e.get("Type") or e.get("type") or "").lower() or "joining" in msg.lower() else "🛫"
            flight_lines.append(f"`{t_str}` {icon} {msg[:80]}")
        embed.add_field(name="🛬 Recent Flight Activity", value="\n".join(flight_lines)[:1020], inline=False)
    else:
        cnt_val, names_val = _visitors_from_status(island, data)
        if names_val:
            v_summary = f"• **Current Visitors:** {', '.join(names_val[:5])}\n• *Channel echo is OFF. Real-time flights logged privately here.*"
            embed.add_field(name="🛬 Recent Flight Activity", value=v_summary[:1020], inline=False)
        elif arrivals_on is False:
            embed.add_field(name="🛬 Recent Flight Activity", value="*Channel echo broadcast is OFF. No recent flights recorded.*", inline=False)

    # 5. Recent Echo & Control Audit History
    if echo_audit_rows:
        audit_lines = []
        for r in echo_audit_rows:
            r_dict = dict(r)
            act = str(r_dict.get("action") or "").replace("echo-", "").replace("echo_", "").title()
            user = str(r_dict.get("user_name") or "User")
            ok_mark = "✅" if r_dict.get("ok") else "⚠️"
            raw_ts = str(r_dict.get("created_at") or "")
            t_part = raw_ts.split(" ")[1][:8] if " " in raw_ts else raw_ts[:16]
            msg = str(r_dict.get("message") or "")[:40]
            audit_lines.append(f"`{t_part}` {ok_mark} **{user}**: `{act}`" + (f" ({msg})" if msg else ""))
        embed.add_field(name="📜 Discord Action Audit", value="\n".join(audit_lines)[:1020], inline=False)

    # 6. Drop Request Logs
    if not drops_rows:
        embed.add_field(
            name="📦 Drop Request History",
            value=f"No drop activity recorded yet for **{island}**.\n*Logs will appear here whenever users queue or drop items on this island.*",
            inline=False,
        )
        return embed

    lines = []
    for r in drops_rows:
        r_dict = dict(r)
        drop_status = str(r_dict.get("status") or "unknown").lower()
        is_ok = drop_status in ("queued", "success", "ok", "completed")
        emoji = "✅" if is_ok else "❌"

        raw_ts = str(r_dict.get("created_at") or "")
        if " " in raw_ts:
            time_part = raw_ts.split(" ")[1][:8]
        elif "T" in raw_ts:
            time_part = raw_ts.split("T")[1][:8]
        else:
            time_part = raw_ts[:16]

        uid = str(r_dict.get("requested_by_user_id") or "web")
        user_mention = f"<@{uid}>" if uid.isdigit() else f"`{uid}`"

        items = str(r_dict.get("items") or "").strip()
        if len(items) > 40:
            items = items[:37] + "…"

        msg = str(r_dict.get("response_message") or "").strip()
        if len(msg) > 60:
            msg = msg[:57] + "…"

        line = f"`{time_part}` {emoji} {user_mention}: `{items}`"
        if not is_ok and msg:
            line += f"\n└ ⚠️ **Reason:** *{msg}*"
        lines.append(line)

    full_text = "\n".join(lines)
    if len(full_text) <= 1000:
        embed.add_field(name=f"📦 Drop Request History ({len(drops_rows)})", value=full_text, inline=False)
    else:
        chunk = []
        chunk_len = 0
        part = 1
        for l in lines:
            if chunk_len + len(l) > 1000:
                embed.add_field(name=f"📦 Drop Request History (Part {part})", value="\n".join(chunk), inline=False)
                chunk = [l]
                chunk_len = len(l)
                part += 1
            else:
                chunk.append(l)
                chunk_len += len(l)
        if chunk:
            embed.add_field(name=f"📦 Drop Request History (Part {part})", value="\n".join(chunk), inline=False)

    return embed


def _extract_console_state(
    status: dict,
    keys: tuple[str, ...],
    island: str | None = None,
    state_key: str | None = None,
) -> Any:
    for k in keys:
        if k in status and status[k] is not None:
            return status[k]
    sw = status.get("switch_telemetry")
    if isinstance(sw, dict):
        for k in keys:
            if k in sw and sw[k] is not None:
                return sw[k]
    if island and state_key:
        cached_action = _CONSOLE_STATES.get(island.lower(), {}).get(state_key)
        if cached_action is not None:
            return cached_action
    if island and get_cached_island_telemetry is not None:
        try:
            telem = get_cached_island_telemetry(island)
            if telem and isinstance(telem, dict):
                for k in keys:
                    if k in telem and telem[k] is not None:
                        return telem[k]
        except Exception:
            pass
    # If the bot is online and connected, screen default is ON unless explicitly turned off
    if state_key == "screen_on" and status.get("is_running") and status.get("is_connected"):
        return True
    return None


def _format_console_state(val: Any, unreachable: bool = False) -> str:
    if unreachable:
        return "`—`"
    if val is True or str(val).lower() in ("true", "1", "on"):
        return "🟢 `ON`"
    if val is False or str(val).lower() in ("false", "0", "off"):
        return "🔴 `OFF`"
    return "`Unknown`"


def _state(status: dict, key: str) -> str:
    unreachable = status.get("success") is False or bool(status.get("unreachable"))
    val = status.get(key)
    if val is not None:
        return _format_console_state(val, unreachable)
    return "`—`" if unreachable else "`Unknown`"


def _bullets(names: list[str], cap: int = 10) -> str:
    lines = [f"• **{n[:32]}**" for n in names[:cap]]
    if len(names) > cap:
        lines.append(f"*…and {len(names) - cap} more*")
    return "\n".join(lines)[:1024]


def _visitors_from_status(island: str, status: dict | None) -> tuple[int | None, list[str] | None]:
    """
    Extract (count, names) from status.
    Returns:
      (None, None) -> unknown (no visitor data present in status or cache)
      (count, names) -> confirmed visitor data (including 0, [] for confirmed empty)
    """
    if not status or not isinstance(status, dict):
        status = {}

    unreachable = status.get("success") is False or bool(status.get("unreachable"))
    if unreachable:
        return None, None

    has_live_keys = any(k in status for k in ("visitor_list", "visitors", "visitors_count"))
    if has_live_keys and parse_sysbot_visitors is not None:
        try:
            cnt, names = parse_sysbot_visitors(status)
            return cnt, list(names) if names is not None else []
        except Exception as exc:
            logger.debug("[ControlPanel] visitor parse failed for %s: %s", island, exc)

    # Fallback to telemetry cache ONLY if live status lacked visitor keys entirely
    if get_cached_island_telemetry is not None:
        try:
            telem = get_cached_island_telemetry(island)
            if telem and (telem.get("visitor_list") is not None or telem.get("visitors_count") is not None):
                t_list = [str(x) for x in telem.get("visitor_list") or [] if str(x).strip()]
                t_cnt = telem.get("visitors_count")
                if t_cnt is None:
                    t_cnt = len(t_list)
                return int(t_cnt), t_list
        except Exception as exc:
            logger.debug("[ControlPanel] telemetry cache visitor fallback failed for %s: %s", island, exc)

    return None, None


def build_control_panel_embed(island_name: str, status: dict | None = None) -> discord.Embed:
    status = status or {}
    unreachable = status.get("success") is False or bool(status.get("unreachable"))
    is_running = bool(status.get("is_running", False))
    accepting = bool(status.get("accepting_commands", is_running))
    console_stage = "—" if unreachable else status.get("console_stage", "Running" if is_running else "Offline")

    if unreachable:
        color = PANEL_COLOR_UNREACHABLE
        status_text = "⚪ **SYSBOT UNREACHABLE** — couldn't read status (the API or console may be down)"
    elif is_running and accepting:
        color, status_text = PANEL_COLOR_ONLINE, "🟢 **ONLINE & READY**"
    elif is_running:
        color, status_text = PANEL_COLOR_WARN, "🟡 **PAUSED / RESTORING**"
    else:
        color, status_text = PANEL_COLOR_OFFLINE, "🔴 **OFFLINE / STOPPED**"

    dodo = status.get("dodo_code")
    has_valid_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
    if unreachable:
        dodo_display = "`—`"
    elif has_valid_dodo:
        dodo_display = f"`{dodo}`" if SHOW_DODO_ON_PANEL else "🔒 Hidden"
    else:
        dodo_display = "`-----` (No Dodo)"

    cnt, names = _visitors_from_status(island_name, status)
    if cnt is None and names is None:
        field_name = "👥 Current Visitors"
        visitors_display = "`—`"
    else:
        names = names or []
        effective_count = max(cnt or 0, len(names))
        field_name = f"👥 Current Visitors ({effective_count})"
        if effective_count == 0:
            visitors_display = "*No visitors on island*"
        else:
            rendered = _bullets(names)
            if (cnt or 0) > len(names):
                unresolved = (cnt or 0) - len(names)
                extra = f"*+{unresolved} anonymous/loading*"
                if rendered:
                    visitors_display = f"{rendered}\n• {extra}"[:1024]
                else:
                    visitors_display = extra
            else:
                visitors_display = rendered or "*No visitors on island*"

    embed = discord.Embed(
        title=f"🎛️ Island Remote Control Panel — {island_name}",
        description=(
            f"{status_text}\n"
            f"Direct hardware & SysBot loop controller for **[{island_name}]({_island_url(island_name)})**.\n\n"
            "Use the interactive controls below to manage the console loop and hardware."
        ),
        color=color,
    )
    embed.add_field(name="✈️ Dodo Code", value=dodo_display, inline=True)
    embed.add_field(name="🎮 Console Stage", value=f"`{console_stage}`", inline=True)
    embed.add_field(
        name="📡 Requests",
        value="`—`" if unreachable else f"`{'Accepting' if accepting else 'Paused'}`",
        inline=True,
    )
    embed.add_field(
        name=field_name,
        value=visitors_display,
        inline=False,
    )
    screen_val = _extract_console_state(
        status, ("screen_on", "screen", "screen_state", "is_screen_on"), island_name, "screen_on"
    )
    mash_val = _extract_console_state(
        status, ("mash_b", "mashb", "mash_state", "is_mash_b"), island_name, "mash_b"
    )
    map_val = _extract_console_state(
        status, ("refresh_map", "map_refresh", "map_refresh_state", "togglerefresh", "is_refresh_map"), island_name, "refresh_map"
    )

    embed.add_field(
        name="⚙️ Console States",
        value=(
            f"• **Screen:** {_format_console_state(screen_val, unreachable)}\n"
            f"• **Mash B:** {_format_console_state(mash_val, unreachable)}\n"
            f"• **Map Refresh:** {_format_console_state(map_val, unreachable)}"
        ),
        inline=True,
    )

    def _bool_icon(val: Any) -> str:
        if val is True or str(val).lower() in ("true", "1", "on"):
            return "🟢 `ON`"
        if val is False or str(val).lower() in ("false", "0", "off"):
            return "🔴 `OFF`"
        return "`—`"

    echo_dodo = _bool_icon(status.get("echo_dodo_enabled"))
    echo_arr = _bool_icon(status.get("echo_arrivals_enabled"))
    echo_upd = _bool_icon(status.get("echo_island_updates_enabled"))

    embed.add_field(
        name="📣 Echo Broadcasts",
        value=(
            f"• **Dodo:** {echo_dodo}\n"
            f"• **Arrivals:** {echo_arr}\n"
            f"• **Updates:** {echo_upd}"
        ),
        inline=True,
    )
    port = _endpoint_port(island_name)
    embed.add_field(name="🔌 SysBot Port", value=f"`{port}`" if port else "`—`", inline=True)
    embed.add_field(name="🕒 Last Updated", value=f"<t:{int(time.time())}:R>", inline=True)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


async def fetch_island_status(island_name: str) -> dict:
    """Live status via SysBotClient. Failures return an explicit 'unreachable' marker."""
    try:
        client = get_island_sysbot_client(island_name)
        data = await asyncio.wait_for(client.get_bot_status(), STATUS_TIMEOUT)
        if not isinstance(data, dict):
            raise TypeError(f"unexpected status payload: {type(data).__name__}")
        if data.get("success") and not data.get("unreachable"):
            # If screen_on is missing from status payload (e.g. older SysBot build),
            # check switch_telemetry or do a fast telemetry query
            if data.get("screen_on") is None:
                try:
                    telem = await asyncio.wait_for(client.get_telemetry(), 1.5)
                    if isinstance(telem, dict):
                        sw = telem.get("switch_telemetry") or {}
                        if sw.get("screen_on") is not None:
                            data["screen_on"] = sw["screen_on"]
                except Exception:
                    pass
            # Cache known states into _CONSOLE_STATES
            isl_entry = _CONSOLE_STATES.setdefault(island_name.lower(), {})
            if data.get("screen_on") is not None:
                isl_entry["screen_on"] = data["screen_on"]
            if data.get("mash_b") is not None:
                isl_entry["mash_b"] = data["mash_b"]
            if data.get("refresh_map") is not None:
                isl_entry["refresh_map"] = data["refresh_map"]

            # Auto-reconcile Echo Broadcast settings with backend database
            saved_echo = await asyncio.to_thread(_get_echo_settings, island_name)
            if saved_echo:
                needs_sync = False
                for k, v in saved_echo.items():
                    if data.get(k) is not None and data.get(k) != v:
                        needs_sync = True
                        break
                if needs_sync:
                    try:
                        logger.info("[ControlPanel] Auto-syncing saved echo settings to SysBot for %s: %s", island_name, saved_echo)
                        await client.toggle_echo(
                            echo_type="all",
                            enabled=None,
                            echo_dodo_enabled=saved_echo["echo_dodo_enabled"],
                            echo_arrivals_enabled=saved_echo["echo_arrivals_enabled"],
                            echo_island_updates_enabled=saved_echo["echo_island_updates_enabled"],
                        )
                        data.update(saved_echo)
                    except Exception as err:
                        logger.debug("[ControlPanel] Echo auto-sync failed for %s: %s", island_name, err)
                else:
                    for k, v in saved_echo.items():
                        if data.get(k) is None:
                            data[k] = v
            elif any(data.get(k) is not None for k in ("echo_dodo_enabled", "echo_arrivals_enabled", "echo_island_updates_enabled")):
                d_val = bool(data.get("echo_dodo_enabled", True))
                a_val = bool(data.get("echo_arrivals_enabled", True))
                u_val = bool(data.get("echo_island_updates_enabled", True))
                await asyncio.to_thread(_save_echo_settings, island_name, d_val, a_val, u_val)
        return data
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to fetch status for %s: %s", island_name, exc)
        return {"success": False, "unreachable": True, "error": str(exc), "island_name": island_name}


async def _update_panel_message(
    message: discord.Message | discord.PartialMessage | None, island: str, bot: commands.Bot
) -> None:
    """Re-read status and edit the panel message in place (and remember its signature)."""
    if message is None:
        return
    status = await fetch_island_status(island)
    embed = build_control_panel_embed(island, status)
    await message.edit(embed=embed, view=IslandControlPanelView(island))
    cog: Any = bot.get_cog("ControlPanelCog")
    channel_id = getattr(message, "channel", None) and message.channel.id
    if cog and channel_id:
        cog._sigs[str(channel_id)] = (_embed_signature(embed), time.monotonic())


async def switch_panel(
    bot: commands.Bot,
    channel_id: int,
    message: discord.Message | discord.PartialMessage | None,
    new_island: str,
    user: discord.abc.User,
    source: str,
) -> tuple[bool, str]:
    """
    Centralized island switcher:
    - Locks channel to prevent race conditions.
    - Writes to DB before editing message so auto-refresh loop doesn't read stale state.
    - Edits the message and updates signature.
    - Reverts DB row if message edit fails.
    - Records audit log.
    """
    cog: Any = bot.get_cog("ControlPanelCog")
    old_row = await asyncio.to_thread(_get_control_panel, channel_id)
    old_island = old_row.get("island_name") if old_row else None
    lock = cog._locks[channel_id] if cog else asyncio.Lock()
    msg_id = message.id if message else (int(old_row["message_id"]) if old_row and old_row.get("message_id") else None)

    ok, err_msg = False, ""
    async with lock:
        await asyncio.to_thread(_upsert_control_panel, channel_id, new_island, msg_id)
        try:
            status = await fetch_island_status(new_island)
            embed = build_control_panel_embed(new_island, status)
            if message:
                await message.edit(embed=embed, view=IslandControlPanelView(new_island))
            if cog:
                cog._sigs[str(channel_id)] = (_embed_signature(embed), time.monotonic())
            ok = True
        except Exception as exc:
            logger.error("[ControlPanel] Failed to switch panel in %s to %s: %s", channel_id, new_island, exc, exc_info=True)
            err_msg = "Failed to update the control panel message."
            # Rollback DB row if edit failed
            if old_island:
                await asyncio.to_thread(_upsert_control_panel, channel_id, old_island, msg_id)

    await asyncio.to_thread(_log_audit, new_island, "switch", user, ok, f"{source} (from {old_island or 'unknown'})")
    return ok, err_msg


def _interpret(res: Any) -> tuple[bool, str]:
    """(ok, message) from a SysBot control response. A missing 'success' counts as ok only if there's no error."""
    if not isinstance(res, dict):
        return False, "Unexpected response from SysBot."
    msg = str(res.get("message") or res.get("error") or "")
    success = res.get("success")
    ok = bool(success) if success is not None else not res.get("error")
    return ok, msg


# ─── Running an action ─────────────────────────────────────────────────────────

async def run_action(
    interaction: discord.Interaction,
    island: str,
    spec: ActionSpec,
    panel_message: discord.Message | None,
) -> None:
    """
    Send `spec` to the island's SysBot, report the result in the (already acknowledged, ephemeral)
    interaction response, then refresh the real panel message.
    """
    lock = _island_lock(island)
    if lock.locked():
        await interaction.edit_original_response(
            content=None, view=None,
            embed=_embed("⏳ Busy", f"Another action is already running on **{island}**. Try again in a moment.", PANEL_COLOR_WARN),
        )
        return

    ok, msg = False, ""
    async with lock:
        wait = _cooldown_left(island, spec.key)
        if wait:
            await interaction.edit_original_response(
                content=None, view=None,
                embed=_embed("⏳ Slow down", f"**{spec.title}** was just used. Try again in {math.ceil(wait)}s.", PANEL_COLOR_WARN),
            )
            return
        try:
            client = get_island_sysbot_client(island)
            res = await asyncio.wait_for(client.control_action(spec.key), ACTION_TIMEOUT)
            ok, msg = _interpret(res)
        except asyncio.TimeoutError:
            msg = "SysBot didn't answer in time."
        except Exception as exc:
            logger.exception("[ControlPanel] %s failed on %s", spec.key, island)
            msg = "Couldn't reach SysBot."
            logger.debug("[ControlPanel] cause: %s", exc)
        if not ok:
            _COOLDOWNS.pop((island.lower(), spec.key), None)   # let them retry right away

        if ok and isinstance(res, dict):
            st_update = _CONSOLE_STATES.setdefault(island.lower(), {})
            if "screen_on" in res:
                st_update["screen_on"] = res["screen_on"]
            elif spec.key == "screen":
                if "screen on" in msg.lower():
                    st_update["screen_on"] = True
                elif "screen off" in msg.lower():
                    st_update["screen_on"] = False
                elif "screen_on" in st_update:
                    st_update["screen_on"] = not st_update["screen_on"]
            if "mash_b" in res:
                st_update["mash_b"] = res["mash_b"]
            elif spec.key == "mash" and "mash b set to:" in msg.lower():
                st_update["mash_b"] = "true" in msg.lower()
            if "refresh_map" in res:
                st_update["refresh_map"] = res["refresh_map"]
            elif spec.key == "map" and "refreshmap set to:" in msg.lower():
                st_update["refresh_map"] = "true" in msg.lower()

    logger.info("[ControlPanel] %s on %s by %s (%s) ok=%s msg=%r", spec.key, island, interaction.user, interaction.user.id, ok, msg)
    await asyncio.to_thread(_log_audit, island, spec.key, interaction.user, ok, msg)

    if ok:
        result = _embed(
            f"✅ {spec.title} executed",
            f"Command sent to **{island}** successfully." + (f"\n*{msg}*" if msg else ""),
            PANEL_COLOR_ONLINE,
        )
    else:
        result = _embed(f"⚠️ {spec.title} failed", f"SysBot returned: {msg or 'No response message.'}", PANEL_COLOR_OFFLINE)
    await interaction.edit_original_response(content=None, embed=result, view=None)

    # Refresh the real panel (not the ephemeral result message).
    try:
        await asyncio.sleep(SETTLE_DELAY)
        await _update_panel_message(panel_message, island, interaction.client)
    except Exception as exc:
        logger.debug("[ControlPanel] Could not update panel after %s: %s", spec.key, exc)


class ConfirmActionView(discord.ui.View):
    """Ephemeral confirm step for risky actions. Only the person who clicked can answer."""

    def __init__(self, user_id: int, island: str, spec: ActionSpec, panel_message: discord.Message | None):
        super().__init__(timeout=45)
        self.user_id = user_id
        self.island = island
        self.spec = spec
        self.panel_message = panel_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation isn't yours.", ephemeral=True)
            return False
        if not is_authorized(interaction.user):
            await interaction.response.send_message(embed=_denied_embed(), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content=f"⏳ Sending **{self.spec.title}**…", embed=None, view=None)
        await run_action(interaction, self.island, self.spec, self.panel_message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[ControlPanel] ConfirmActionView error: %s", error, exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send("❌ Something went wrong.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ─── Echo Settings & Island Directory Components ──────────────────────────────

KEYS = {
    "dodo": "echo_dodo_enabled",
    "arrivals": "echo_arrivals_enabled",
    "island_updates": "echo_island_updates_enabled",
}


def build_echo_settings_embed(island_name: str, status: dict | None) -> discord.Embed:
    data = status or {}
    online = bool(data.get("is_running") or data.get("success") or data.get("ip"))
    dodo = data.get("echo_dodo_enabled")
    arrivals = data.get("echo_arrivals_enabled")
    updates = data.get("echo_island_updates_enabled")

    def _badge(val: Any) -> str:
        if val is True:
            return "🟢 **ENABLED**"
        if val is False:
            return "🔴 **DISABLED**"
        return "⚪ *Unknown*"

    embed = discord.Embed(
        title=f"📣 Echo Broadcast Settings — {island_name}",
        description=(
            f"Manage automated Discord announcements for **{island_name}**.\n\n"
            f"• **🦤 Dodo Code Broadcast**: {_badge(dodo)}\n"
            f"  *Echoes when a new Dodo code is generated or detected.*\n\n"
            f"• **🛬 Arrivals & Departures**: {_badge(arrivals)}\n"
            f"  *Echoes player arrival and departure flight events in real-time.*\n\n"
            f"• **📢 Island State Updates**: {_badge(updates)}\n"
            f"  *Echoes island gate, clean, and queue state changes.*\n"
        ),
        color=PANEL_COLOR_ONLINE if online else PANEL_COLOR_OFFLINE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text=f"Island: {island_name} • ChoPaeng SysBot Echo Control", icon_url=PANEL_FOOTER_ICON)
    return embed


class EchoSettingsView(discord.ui.View):
    """Ephemeral control panel for toggling SysBot echo announcements."""

    def __init__(
        self,
        island: str,
        status: dict | None,
        parent_message: discord.Message | None = None,
        interaction: discord.Interaction | None = None,
    ):
        super().__init__(timeout=180)
        self.island = island
        self.status = status or {}
        self.parent_message = parent_message
        self.interaction = interaction
        self._build_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_authorized(interaction.user):
            await interaction.response.send_message(embed=_denied_embed(), ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.interaction:
            with contextlib.suppress(Exception):
                await self.interaction.edit_original_response(view=self)

    def _build_buttons(self) -> None:
        self.clear_items()
        dodo_on = self.status.get("echo_dodo_enabled") is True
        arr_on = self.status.get("echo_arrivals_enabled") is True
        upd_on = self.status.get("echo_island_updates_enabled") is True

        # Row 0: Individual category toggles
        b_dodo = discord.ui.Button(
            label=f"Dodo: {'ON' if dodo_on else 'OFF'}",
            emoji="🦤",
            style=discord.ButtonStyle.success if dodo_on else discord.ButtonStyle.secondary,
            row=0,
        )
        b_dodo.callback = self._on_toggle_dodo
        self.add_item(b_dodo)

        b_arr = discord.ui.Button(
            label=f"Arrivals: {'ON' if arr_on else 'OFF'}",
            emoji="🛬",
            style=discord.ButtonStyle.success if arr_on else discord.ButtonStyle.secondary,
            row=0,
        )
        b_arr.callback = self._on_toggle_arrivals
        self.add_item(b_arr)

        b_upd = discord.ui.Button(
            label=f"Updates: {'ON' if upd_on else 'OFF'}",
            emoji="📢",
            style=discord.ButtonStyle.success if upd_on else discord.ButtonStyle.secondary,
            row=0,
        )
        b_upd.callback = self._on_toggle_updates
        self.add_item(b_upd)

        # Row 1: Batch toggle, cluster-wide apply, and refresh
        b_all = discord.ui.Button(
            label="Toggle All",
            emoji="🔄",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        b_all.callback = self._on_toggle_all
        self.add_item(b_all)

        b_apply_all = discord.ui.Button(
            label="Apply to All",
            emoji="🌐",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        b_apply_all.callback = self._on_apply_to_all
        self.add_item(b_apply_all)

        b_refresh = discord.ui.Button(
            label="Refresh",
            emoji="🔃",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        b_refresh.callback = self._refresh
        self.add_item(b_refresh)

    async def _on_toggle_dodo(self, interaction: discord.Interaction) -> None:
        await self._toggle_echo(interaction, "dodo")

    async def _on_toggle_arrivals(self, interaction: discord.Interaction) -> None:
        await self._toggle_echo(interaction, "arrivals")

    async def _on_toggle_updates(self, interaction: discord.Interaction) -> None:
        await self._toggle_echo(interaction, "island_updates")

    async def _on_toggle_all(self, interaction: discord.Interaction) -> None:
        await self._toggle_echo(interaction, "all")

    async def _toggle_echo(self, interaction: discord.Interaction, echo_type: str) -> None:
        await interaction.response.defer()
        ok, msg = False, ""
        try:
            client = get_island_sysbot_client(self.island)
            if echo_type == "all":
                want = not all(self.status.get(k) is True for k in KEYS.values())
            else:
                key_name = KEYS.get(echo_type)
                want = (self.status.get(key_name) is not True) if key_name else True

            res = await asyncio.wait_for(client.toggle_echo(echo_type=echo_type, enabled=want), ACTION_TIMEOUT)
            ok, msg = _interpret(res)
        except asyncio.TimeoutError:
            msg = "SysBot didn't answer in time."
            logger.warning("[ControlPanel] Echo toggle timeout on %s", self.island)
        except Exception as exc:
            msg = "Failed to communicate with SysBot."
            logger.error("[ControlPanel] Error toggling echo on %s: %s", self.island, exc, exc_info=True)
        finally:
            await asyncio.to_thread(_log_audit, self.island, f"echo-{echo_type}", interaction.user, ok, msg)

        # Re-fetch status and persist new settings to backend database
        self.status = await fetch_island_status(self.island)
        dodo_val = self.status.get("echo_dodo_enabled") is True
        arr_val = self.status.get("echo_arrivals_enabled") is True
        upd_val = self.status.get("echo_island_updates_enabled") is True
        await asyncio.to_thread(_save_echo_settings, self.island, dodo_val, arr_val, upd_val)

        self._build_buttons()
        embed = build_echo_settings_embed(self.island, self.status)
        if not ok:
            embed.set_footer(text=f"⚠️ Action failed: {msg or 'Unknown error'}", icon_url=PANEL_FOOTER_ICON)
        else:
            embed.set_footer(text=f"💾 Saved to backend & synced to SysBot • {self.island}", icon_url=PANEL_FOOTER_ICON)
        await interaction.edit_original_response(embed=embed, view=self)

        if ok and self.parent_message:
            try:
                await _update_panel_message(self.parent_message, self.island, interaction.client)
            except Exception as exc:
                logger.debug("[ControlPanel] Could not update parent panel: %s", exc)

    async def _on_apply_to_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        dodo_val = self.status.get("echo_dodo_enabled") is True
        arr_val = self.status.get("echo_arrivals_enabled") is True
        upd_val = self.status.get("echo_island_updates_enabled") is True

        # Persist to database for all 48 islands
        await asyncio.to_thread(_save_echo_settings_all, dodo_val, arr_val, upd_val)

        # Broadcast asynchronously to all reachable SysBots
        asyncio.create_task(_broadcast_echo_to_cluster(dodo_val, arr_val, upd_val))

        embed = build_echo_settings_embed(self.island, self.status)
        d_str = "ON" if dodo_val else "OFF"
        a_str = "ON" if arr_val else "OFF"
        u_str = "ON" if upd_val else "OFF"
        embed.set_footer(
            text=f"🌐 Saved & broadcasted to all 48 islands: Dodo={d_str}, Arr={a_str}, Upd={u_str}",
            icon_url=PANEL_FOOTER_ICON,
        )
        await interaction.edit_original_response(embed=embed, view=self)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.status = await fetch_island_status(self.island)
        self._build_buttons()
        embed = build_echo_settings_embed(self.island, self.status)
        await interaction.edit_original_response(embed=embed, view=self)


def build_all_islands_embed(current_island: str) -> discord.Embed:
    sub_list, free_list = get_sub_and_free_islands()
    free_sorted = sorted(free_list, key=lambda s: s.lower())
    free_a_k = [i for i in free_sorted if i[0].upper() <= "K"]
    free_l_z = [i for i in free_sorted if i[0].upper() > "K"]
    total = len(sub_list) + len(free_list)

    embed = discord.Embed(
        title=f"🌐 Island Cluster Directory ({total} Islands)",
        description=(
            f"Currently active panel island: **{current_island}**\n\n"
            "Select any island below to immediately switch this channel's control panel:\n\n"
            f"• **👑 Sub & Order Islands ({len(sub_list)})**:\n  {', '.join(sub_list)}\n\n"
            f"• **🌴 Free Islands A - K ({len(free_a_k)})**:\n  {', '.join(free_a_k)}\n\n"
            f"• **🌴 Free Islands L - Z ({len(free_l_z)})**:\n  {', '.join(free_l_z)}"
        ),
        color=PANEL_COLOR_ONLINE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text=f"ChoPaeng Island Cluster • {total} Total Nodes", icon_url=PANEL_FOOTER_ICON)
    return embed


class AllIslandsBrowserView(discord.ui.View):
    """Allows selecting any of the Sub and Free islands across the cluster."""

    def __init__(
        self,
        current_island: str,
        panel_message: discord.Message | None = None,
        interaction: discord.Interaction | None = None,
    ):
        super().__init__(timeout=120)
        self.current_island = current_island
        self.panel_message = panel_message
        self.interaction = interaction
        self._build_dropdowns()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_authorized(interaction.user):
            await interaction.response.send_message(embed=_denied_embed(), ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.interaction:
            with contextlib.suppress(Exception):
                await self.interaction.edit_original_response(view=self)

    def _build_dropdowns(self) -> None:
        sub_list, free_list = get_sub_and_free_islands()

        # 1. Sub & Order Islands
        sub_options = [
            discord.SelectOption(
                label=name,
                description="Order Bot Island" if name == "Sinta" else "Sub / VIP Island",
                emoji="🤖" if name == "Sinta" else "👑",
                value=name,
                default=(_norm(name) == _norm(self.current_island)),
            )
            for name in sub_list[:25]
        ]
        sel_sub = discord.ui.Select(
            placeholder=f"👑 Sub & Order Islands ({len(sub_list)})...",
            options=sub_options,
            row=0,
            custom_id="browser_sub",
        )
        sel_sub.callback = self._on_select
        self.add_item(sel_sub)

        # 2. Free Islands A - K and L - Z
        free_sorted = sorted(free_list, key=lambda s: s.lower())
        free_a_k = [i for i in free_sorted if i[0].upper() <= "K"]
        free_l_z = [i for i in free_sorted if i[0].upper() > "K"]

        free_ak_options = [
            discord.SelectOption(
                label=name,
                description="Free / Twitch Island",
                emoji="🌴",
                value=name,
                default=(_norm(name) == _norm(self.current_island)),
            )
            for name in free_a_k[:25]
        ]
        sel_ak = discord.ui.Select(
            placeholder=f"🌴 Free Islands A - K ({len(free_a_k)})...",
            options=free_ak_options,
            row=1,
            custom_id="browser_free_ak",
        )
        sel_ak.callback = self._on_select
        self.add_item(sel_ak)

        free_lz_options = [
            discord.SelectOption(
                label=name,
                description="Free / Twitch Island",
                emoji="🌴",
                value=name,
                default=(_norm(name) == _norm(self.current_island)),
            )
            for name in free_l_z[:25]
        ]
        sel_lz = discord.ui.Select(
            placeholder=f"🌴 Free Islands L - Z ({len(free_l_z)})...",
            options=free_lz_options,
            row=2,
            custom_id="browser_free_lz",
        )
        sel_lz.callback = self._on_select
        self.add_item(sel_lz)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        new_island = _canonical_island(values[0])
        if not new_island:
            await interaction.response.send_message(embed=_err_embed("Unknown island.", "❌ Error"), ephemeral=True)
            return

        await interaction.response.defer()
        if self.panel_message and interaction.channel_id:
            ok, err_msg = await switch_panel(
                interaction.client, interaction.channel_id, self.panel_message, new_island, interaction.user, "browser"
            )
            if not ok:
                await interaction.edit_original_response(embed=_err_embed(err_msg or "Failed to switch island.", "❌ Error"), view=None)
                return

        await interaction.edit_original_response(
            content=f"✅ Switched control panel to **{new_island}**!",
            embed=None,
            view=None,
        )


# ─── Dynamic Items & Persistent Components ─────────────────────────────────────

class _PanelErrors:
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("[ControlPanel] Component error: %s", error, exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Something went wrong. Please try again.", ephemeral=True)


class ControlPanelIslandSelect(
    _PanelErrors,
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"ctrlpanel:select:(?P<island>[^:]+)",
):
    """Island switcher dropdown."""

    def __init__(self, current_island: str):
        self.current_island = current_island

        sub_list, free_list = get_sub_and_free_islands()
        is_free = any(_norm(current_island) == _norm(f) for f in free_list)
        active_list = free_list if is_free else sub_list

        ordered = [current_island] + [i for i in active_list if _norm(i) != _norm(current_island)]
        options = []
        for name in ordered[:25]:
            is_order = (name == "Sinta")
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    description="Order Bot Island" if is_order else ("Free / Twitch Island" if is_free else "Sub / VIP Island"),
                    emoji="🤖" if is_order else ("🌴" if is_free else "🏝️"),
                    value=name[:100],
                    default=(_norm(name) == _norm(current_island)),
                )
            )

        super().__init__(
            discord.ui.Select(
                custom_id=_cid("select", current_island),
                placeholder=f"🏝️ Control Island: {current_island}…"[:150],
                options=options[:25],
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_authorized(interaction.user):
            await interaction.response.send_message(embed=_denied_embed(), ephemeral=True)
            return

        values = (interaction.data or {}).get("values") or [self.current_island]
        new_island = _canonical_island(values[0])
        if not new_island:
            await interaction.response.send_message(embed=_err_embed("Unknown island.", "❌ Error"), ephemeral=True)
            return

        await interaction.response.defer()
        if interaction.message and interaction.channel_id:
            ok, err_msg = await switch_panel(
                interaction.client, interaction.channel_id, interaction.message, new_island, interaction.user, "select dropdown"
            )
            if not ok:
                await interaction.followup.send(embed=_err_embed(err_msg or "Failed to switch island.", "❌ Error"), ephemeral=True)


class ControlPanelActionButton(
    _PanelErrors,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ctrlpanel:(?:btn:)?(?P<action>(?!select\b)[a-zA-Z0-9_-]+):(?P<island>[^:]+)",
):
    """One button per ACTIONS entry. Look and behaviour come from the registry."""

    def __init__(self, action: str, island: str):
        spec = ACTIONS.get(action)
        super().__init__(
            discord.ui.Button(
                label=spec.label if spec else action.title(),
                emoji=spec.emoji if spec else None,
                style=spec.style if spec else discord.ButtonStyle.secondary,
                custom_id=_cid(action, island),
                row=spec.row if spec else 1,
            )
        )
        self.action = action
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /):
        return cls(match["action"], urllib.parse.unquote(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        spec = ACTIONS.get(self.action)
        if spec is None:
            await interaction.response.send_message(
                embed=_err_embed("This button is out of date. Ask a moderator to repost the panel.", "❌ Outdated panel"),
                ephemeral=True,
            )
            return

        if spec.key == "guide":     # read-only, no role needed
            await interaction.response.send_message(embed=build_guide_embed(), ephemeral=True)
            return

        if not is_authorized(interaction.user):
            await interaction.response.send_message(embed=_denied_embed(), ephemeral=True)
            return

        island = _canonical_island(self.island)
        if not island:
            await interaction.response.send_message(embed=_err_embed("Unknown island.", "❌ Error"), ephemeral=True)
            return

        if spec.key == "logs":
            await interaction.response.defer(ephemeral=True)
            status = await fetch_island_status(island)
            embed = await asyncio.to_thread(_build_island_logs_embed, island, 20, status)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if spec.key == "echo":
            status = await fetch_island_status(island)
            embed = build_echo_settings_embed(island, status)
            view = EchoSettingsView(island, status, interaction.message, interaction=interaction)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        if spec.key == "switch":
            embed = build_all_islands_embed(island)
            view = AllIslandsBrowserView(island, interaction.message, interaction=interaction)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        if spec.key == "status":
            await interaction.response.defer()
            if _cooldown_left(island, "status", 2.0):
                return   # ignore rapid re-clicks
            status = await fetch_island_status(island)
            embed = build_control_panel_embed(island, status)
            await interaction.edit_original_response(embed=embed, view=IslandControlPanelView(island))
            cog: Any = interaction.client.get_cog("ControlPanelCog")
            if cog and interaction.channel_id:
                cog._sigs[str(interaction.channel_id)] = (_embed_signature(embed), time.monotonic())
            return

        if spec.local:
            await interaction.response.send_message(
                embed=_err_embed(f"Action '{spec.title}' is marked local but has no handler configured.", "❌ Not Implemented"),
                ephemeral=True,
            )
            return

        if spec.confirm:
            view = ConfirmActionView(interaction.user.id, island, spec, interaction.message)
            await interaction.response.send_message(
                embed=_embed(f"⚠️ Confirm: {spec.title}", f"{spec.confirm}\n\nIsland: **{island}**", PANEL_COLOR_WARN),
                view=view,
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await run_action(interaction, island, spec, interaction.message)


class IslandControlPanelView(discord.ui.View):
    """Row 0: island select · Rows 1-3: action buttons (laid out by the ACTIONS registry)."""

    def __init__(self, island_name: str):
        super().__init__(timeout=None)
        self.island_name = island_name
        self.add_item(ControlPanelIslandSelect(island_name))
        for spec in ACTIONS.values():
            self.add_item(ControlPanelActionButton(spec.key, island_name))


# ─── Cog ───────────────────────────────────────────────────────────────────────

class ControlPanelCog(commands.Cog, name="ControlPanelCog"):
    """Manages the interactive remote control panel for SysBot islands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sigs: dict[str, tuple[str, float]] = {}   # channel_id -> (signature, last edit time)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)   # one per channel
        self._dead_channels: set[int] = set()           # secondary channels that no longer exist

    async def cog_load(self) -> None:
        try:
            await asyncio.to_thread(_init_panel_table)
            self.bot.add_dynamic_items(ControlPanelIslandSelect, ControlPanelActionButton)
            logger.info("[ControlPanelCog] Dynamic items registered successfully.")
        except Exception as exc:
            logger.warning("[ControlPanelCog] Failed to register dynamic items: %s", exc)
        if not self.auto_refresh_control_panel_loop.is_running():
            self.auto_refresh_control_panel_loop.start()

    async def cog_unload(self) -> None:
        self.auto_refresh_control_panel_loop.cancel()
        with contextlib.suppress(Exception):
            self.bot.remove_dynamic_items(ControlPanelIslandSelect, ControlPanelActionButton)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        title = "❌ Error"
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission to use this command."
            title = "🚫 Access Denied"
        elif isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "You need the control-panel role (or Administrator) to use this command."
            title = "🚫 Access Denied"
        else:
            msg = "Something went wrong running that command."
            logger.error("[ControlPanelCog] Command error: %s", error, exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(embed=_err_embed(msg, title), ephemeral=True)
            else:
                await interaction.response.send_message(embed=_err_embed(msg, title), ephemeral=True)

    # ── Keeping a channel's panel alive ───────────────────────────────────────

    async def _resolve_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            if channel_id != PERSISTENT_CONTROL_CHANNEL_ID:
                self._dead_channels.add(channel_id)
                logger.warning("[ControlPanelCog] Channel %s is gone/inaccessible; skipping it from now on.", channel_id)
        except Exception as exc:
            logger.debug("[ControlPanelCog] Could not resolve channel %s: %s", channel_id, exc)
        return None

    async def ensure_persistent_panel(
        self, channel_id: int = PERSISTENT_CONTROL_CHANNEL_ID
    ) -> discord.Message | discord.PartialMessage | None:
        """
        Make sure a fresh panel exists in `channel_id`: edit the known message, else recover one the bot
        posted recently, else post a new one. Unchanged panels aren't re-edited until FORCE_EDIT_SECONDS.
        Returns the panel message (possibly a PartialMessage), or None if nothing could be done.
        """
        async with self._locks[channel_id]:
            channel = await self._resolve_channel(channel_id)
            if channel is None or not hasattr(channel, "send"):
                return None

            row = await asyncio.to_thread(_get_control_panel, channel_id)
            island = _canonical_island(row["island_name"]) if row else None
            if not island:
                island = await asyncio.to_thread(_infer_island_from_channel, channel)
            if not island and channel_id == PERSISTENT_CONTROL_CHANNEL_ID:
                island = DEFAULT_CONTROL_ISLAND   # the dedicated channel has an island switcher, so a default is safe
            if not island:
                logger.debug("[ControlPanelCog] No island for channel %s; skipping.", channel_id)
                return None

            message_id = int(row["message_id"]) if (row and row.get("message_id")) else None
            cid = str(channel_id)

            status = await fetch_island_status(island)
            embed = build_control_panel_embed(island, status)
            sig = _embed_signature(embed)
            now = time.monotonic()
            prev = self._sigs.get(cid)
            # Attach the view on the first edit after startup so older panels pick up new buttons.
            edit_kwargs: dict[str, Any] = {"embed": embed}
            if prev is None:
                edit_kwargs["view"] = IslandControlPanelView(island)

            # 1. Known message id
            if message_id:
                partial = channel.get_partial_message(message_id)
                if prev and prev[0] == sig and now - prev[1] < FORCE_EDIT_SECONDS:
                    return partial
                try:
                    await partial.edit(**edit_kwargs)
                    self._sigs[cid] = (sig, now)
                    return partial
                except discord.NotFound:
                    pass   # fall through to recovery
                except discord.HTTPException as exc:
                    logger.warning("[ControlPanelCog] Failed to edit panel in %s: %s", channel_id, exc)
                    return None

            # 2. Recover a panel this bot posted recently
            if hasattr(channel, "history"):
                try:
                    bot_id = getattr(self.bot.user, "id", None)
                    async for candidate in channel.history(limit=25):
                        if candidate.author.id == bot_id and candidate.embeds and \
                                "Island Remote Control Panel" in (candidate.embeds[0].title or ""):
                            await candidate.edit(embed=embed, view=IslandControlPanelView(island))
                            self._sigs[cid] = (sig, now)
                            await asyncio.to_thread(_upsert_control_panel, channel_id, island, candidate.id)
                            return candidate
                except Exception as exc:
                    logger.debug("[ControlPanelCog] Error scanning history in %s: %s", channel_id, exc)

            # 3. Post a new one
            try:
                msg = await channel.send(embed=embed, view=IslandControlPanelView(island))
                self._sigs[cid] = (sig, now)
                await asyncio.to_thread(_upsert_control_panel, channel_id, island, msg.id)
                logger.info("[ControlPanelCog] Created control panel in channel %s for %s", channel_id, island)
                return msg
            except Exception as exc:
                logger.error("[ControlPanelCog] Failed to send panel in channel %s: %s", channel_id, exc)
                return None

    @tasks.loop(seconds=30)
    async def auto_refresh_control_panel_loop(self) -> None:
        """Refresh the dedicated channel's panel plus every panel recorded in the database, in parallel."""
        channel_ids = {PERSISTENT_CONTROL_CHANNEL_ID}
        for p in await asyncio.to_thread(_load_all_control_panels):
            with contextlib.suppress(TypeError, ValueError):
                channel_ids.add(int(p["channel_id"]))
        channel_ids -= self._dead_channels

        sem = asyncio.Semaphore(REFRESH_CONCURRENCY)

        async def refresh(cid: int) -> None:
            async with sem:
                try:
                    await self.ensure_persistent_panel(cid)
                except Exception as exc:
                    logger.warning("[ControlPanelCog] Error refreshing panel in %s: %s", cid, exc)

        await asyncio.gather(*(refresh(c) for c in channel_ids))

    @auto_refresh_control_panel_loop.before_loop
    async def before_auto_refresh(self) -> None:
        await self.bot.wait_until_ready()

    # ── Slash commands ────────────────────────────────────────────────────────

    async def island_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cur = (current or "").strip().lower()
        islands = [i for i in Config.ALL_ISLAND_PORT_ORDER if cur in i.lower()][:25]
        return [app_commands.Choice(name=f"🏝️ {i}", value=i) for i in islands]

    control_panel_group = app_commands.Group(
        name="control-panel",
        description="SysBot island remote control console",
        guild_only=True,
    )

    @control_panel_group.command(name="setup", description="Post the interactive island control panel in this channel")
    @app_commands.describe(island="Island to control (auto-detected from the channel name if omitted)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.autocomplete(island=island_autocomplete)
    async def control_panel_setup(self, interaction: discord.Interaction, island: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(embed=_err_embed("Panels can only be posted in regular text channels.", "❌ Error"), ephemeral=True)
            return

        if island:
            target = _canonical_island(island)
            if not target:
                await interaction.followup.send(
                    embed=_err_embed(f"**{island[:50]}** isn't a known island (or is ambiguous). Pick one from the list.", "❌ Unknown island"),
                    ephemeral=True,
                )
                return
        else:
            target = await asyncio.to_thread(_infer_island_from_channel, channel)
            if not target:
                await interaction.followup.send(
                    embed=_err_embed("I couldn't tell which island this channel is for. Pass it explicitly: `/control-panel setup island:Hiraya`", "❌ Error"),
                    ephemeral=True,
                )
                return

        old = await asyncio.to_thread(_get_control_panel, channel.id)
        status = await fetch_island_status(target)
        embed = build_control_panel_embed(target, status)
        msg = await channel.send(embed=embed, view=IslandControlPanelView(target))

        if old and old.get("message_id") and str(old["message_id"]) != str(msg.id):
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(int(old["message_id"])).delete()

        await asyncio.to_thread(_upsert_control_panel, channel.id, target, msg.id)
        self._sigs[str(channel.id)] = (_embed_signature(embed), time.monotonic())
        self._dead_channels.discard(channel.id)
        logger.info("[ControlPanelCog] setup: island=%s channel=%s msg=%s by %s", target, channel.id, msg.id, interaction.user)
        await interaction.followup.send(f"✅ Control panel for **{target}** posted in {channel.mention}.", ephemeral=True)

    @control_panel_group.command(name="echo", description="View or toggle SysBot echo broadcast settings for an island")
    @app_commands.describe(
        island="Island to configure (defaults to this channel's panel island)",
        category="Echo category to toggle (or all)",
        enabled="Explicit true/false state (optional, toggles current state if omitted)",
    )
    @panel_access()
    @app_commands.autocomplete(island=island_autocomplete)
    async def control_panel_echo(
        self,
        interaction: discord.Interaction,
        island: str | None = None,
        category: Literal["all", "dodo", "arrivals", "island_updates"] = "all",
        enabled: bool | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if island:
            target = _canonical_island(island)
            if not target:
                await interaction.followup.send(
                    embed=_err_embed(f"**{island[:50]}** is not a recognized island.", "❌ Unknown Island"),
                    ephemeral=True,
                )
                return
        else:
            target = await _resolve_target(interaction, None)
            if not target:
                await interaction.followup.send(
                    embed=_err_embed("Could not determine target island. Pass it explicitly: `/control-panel echo island:Kakanggata`", "❌ Unknown Island"),
                    ephemeral=True,
                )
                return

        ok, msg = False, ""
        try:
            client = get_island_sysbot_client(target)
            curr_status = await fetch_island_status(target)
            if enabled is not None:
                want = enabled
            elif category == "all":
                want = not all(curr_status.get(k) is True for k in KEYS.values())
            else:
                key_name = KEYS.get(category)
                want = (curr_status.get(key_name) is not True) if key_name else True

            res = await asyncio.wait_for(client.toggle_echo(echo_type=category, enabled=want), ACTION_TIMEOUT)
            ok, msg = _interpret(res)
        except asyncio.TimeoutError:
            msg = "SysBot didn't answer in time."
            logger.warning("[ControlPanel] Echo slash toggle timeout on %s", target)
        except Exception as exc:
            msg = "Failed to communicate with SysBot."
            logger.error("[ControlPanel] Error toggling echo via slash command on %s: %s", target, exc, exc_info=True)
        finally:
            await asyncio.to_thread(_log_audit, target, f"echo-{category}", interaction.user, ok, msg)

        new_status = await fetch_island_status(target)
        embed = build_echo_settings_embed(target, new_status)
        if not ok:
            embed.set_footer(text=f"⚠️ Action failed: {msg or 'Unknown error'}", icon_url=PANEL_FOOTER_ICON)
        view = EchoSettingsView(target, new_status, None, interaction=interaction)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @control_panel_group.command(name="switch", description="Switch the control panel in this channel to a different island")
    @app_commands.describe(island="Island to switch the panel to")
    @panel_access()
    @app_commands.autocomplete(island=island_autocomplete)
    async def control_panel_switch(self, interaction: discord.Interaction, island: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = _canonical_island(island)
        if not target:
            await interaction.followup.send(embed=_err_embed(f"**{island[:50]}** is not a recognized island.", "❌ Unknown Island"), ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(embed=_err_embed("Panels can only be switched in regular text channels.", "❌ Error"), ephemeral=True)
            return

        panel = await asyncio.to_thread(_get_control_panel, channel.id)
        msg: discord.Message | discord.PartialMessage | None = None
        if panel and panel.get("message_id"):
            msg = channel.get_partial_message(int(panel["message_id"]))
        else:
            msg = await self.ensure_persistent_panel(channel.id)

        if not msg:
            await interaction.followup.send(embed=_err_embed("Could not find or create a control panel in this channel.", "❌ Error"), ephemeral=True)
            return

        ok, err_msg = await switch_panel(self.bot, channel.id, msg, target, interaction.user, "slash command")
        if ok:
            await interaction.followup.send(f"✅ Control panel switched to **{target}**.", ephemeral=True)
        else:
            await interaction.followup.send(embed=_err_embed(err_msg or "Failed to switch panel.", "❌ Error"), ephemeral=True)



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ControlPanelCog(bot))