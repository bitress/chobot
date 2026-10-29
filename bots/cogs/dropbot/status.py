"""Live island status, dodo lookup/webhook, and the embeds built from them."""

from __future__ import annotations

import aiohttp
import asyncio
import discord
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from utils.config import Config
from utils.database import connect_db
from utils.sysbot_api import _INVALID_DODO_CODES, get_island_sysbot_client

from .constants import ACTION_COOLDOWN, CART_TTL_SECONDS, DODO_GIF, LOGO_URL, MAP_CDN_BASE, MAX_DROP_SLOTS, MAX_STACK, PANEL_EMBED_COLOR, PANEL_EMBED_COLOR_ERR, PANEL_EMBED_COLOR_WARN, PANEL_FOOTER_ICON, PANEL_FOOTER_TEXT, PLOT_MAX, PLOT_MIN, _PLACEHOLDER_DODOS
from .state import MAX_DROP_QUEUE, _debounced, _http
from .db import _load_user_last_drop
from .islands import _canonical_island, _interactive_map_url, _island_url

logger = logging.getLogger("DropBotCog")


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
        url = getattr(Config, "ISLANDS_API_URL", "") or f"{Config.API_BASE_URL}/api/islands"
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


_ISLAND_DB_INFO_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def _get_island_db_info(island_name: str, ttl: float = 60.0) -> dict[str, str]:
    """Fetch island description, type, and map_url from the database, cached for `ttl` seconds."""
    slug = island_name.lower().strip()
    now = time.monotonic()
    cached = _ISLAND_DB_INFO_CACHE.get(slug)
    if cached and now < cached[0]:
        return cached[1]

    info: dict[str, str] = {"description": "", "type": "", "map_url": ""}
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT description, type, map_url FROM islands WHERE LOWER(id) = ? OR LOWER(name) = ? LIMIT 1",
                (slug, slug),
            ).fetchone()
            if row:
                info["description"] = (row.get("description") or "").strip()
                info["type"] = (row.get("type") or "").strip()
                info["map_url"] = (row.get("map_url") or "").strip()
    except Exception as exc:
        logger.debug("[DropBot] Could not fetch island info for %s from DB: %s", island_name, exc)

    _ISLAND_DB_INFO_CACHE[slug] = (now + ttl, info)
    return info


async def _get_island_db_info_async(island_name: str, ttl: float = 60.0) -> dict[str, str]:
    """Async wrapper for _get_island_db_info to avoid blocking the event loop."""
    return await asyncio.to_thread(_get_island_db_info, island_name, ttl)


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
        color         = PANEL_EMBED_COLOR

    db_info = await _get_island_db_info_async(island_name)
    island_desc = db_info.get("description") or ""
    island_type = db_info.get("type") or ""
    db_map_url = db_info.get("map_url") or ""

    header_parts = []
    if island_type:
        header_parts.append(f"🏝 **{island_type}**")

    desc_lines = [" · ".join(header_parts)]
    if island_desc:
        desc_lines.append(island_desc[:1000])

    embed = discord.Embed(
        title=f"{'🟢' if is_running else '🔴'} {island_name} Map",
        url=url,
        description="\n".join(desc_lines),
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

    # Map image as thumbnail (upper right): prefer DB map_url if set, otherwise fetch from CDN
    thumbnail_url = db_map_url if db_map_url else await _map_url_async(island_name)
    embed.set_thumbnail(url=thumbnail_url)

    footer_parts = [PANEL_FOOTER_TEXT]
    if is_nooks_open:
        footer_parts.append("🛒 Nook's Open")
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
            "Everything here is **private to you** — only you see the menus and results.\n"
            f"Island page: [{island} on chopaeng.com]({_island_url(island)}) · "
            f"Interactive map: [{island} map]({_interactive_map_url(island)})"
        ),
        color=PANEL_EMBED_COLOR,
    )
    embed.add_field(
        name="🛫 Getting started",
        value=(
            "1. Press **🦤 Get Dodo** to see the live Dodo code.\n"
            "2. Fly in through the airport and **step outside the gate** — drops only work once the bot sees you on the island.\n"
            "3. Press **📦 Drop Items** or **🔍 Search Catalog** and queue what you need.\n"
            "Your in-game name must match your server nickname (`Character | Island`) or the bot won't find you."
        ),
        inline=False,
    )
    embed.add_field(
        name="🦤 Get Dodo",
        value=(
            "Shows the Dodo code just to you; reveals are logged for moderators. "
            "If it says the code is refreshing, wait a few seconds and press it again."
        ),
        inline=False,
    )
    embed.add_field(
        name="📦 Drop Items",
        value=(
            "Pick a **preset** from the menu for an instant drop, or use:\n"
            "• **🎒 Saved Pockets** — ready-made inventory sets\n"
            "• **🔁 Re-drop Last Cart** — repeat your previous drop\n"
            "• **📝 Custom / Hex** — type your own list:\n"
            "```Gold nugget 30, Iron nugget 30, 09C9 x10```"
            f"Up to **{MAX_DROP_SLOTS}** different items per drop, **{MAX_STACK}** max of each."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔍 Search Catalog",
        value=(
            "Search items **and** villagers. Words can be in any order and small typos are fine "
            "(`crown royal` finds *Royal Crown*).\n"
            "• **Type** switches between everything, items only, and DIY recipes.\n"
            "• **Series** jumps to collections like Cherry Blossom or Golden.\n"
            "• Tap an item to pick its **color/pattern** (the hex updates for you), set the amount with "
            "**− / + / ×1 / ×30**, or enter a **Custom Hex**.\n"
            "• **⚡ Drop Now** sends just that item; **🛒 Add to Cart** keeps it while you keep browsing."
        ),
        inline=False,
    )
    embed.add_field(
        name="🛒 Your drop cart",
        value=(
            f"Holds up to **{MAX_DROP_SLOTS}** items. Press **View Cart** to change colors or amounts, "
            "or remove things, then **Drop Cart** to send everything at once.\n"
            "• The cart shows your total items and warns you if it's full or has the same item twice.\n"
            f"• It's **saved for {CART_TTL_SECONDS // 3600} hours** per island, so closing the menu or a bot restart won't lose it — "
            "just reopen **Search Catalog**.\n"
            "• **Load Last Cart** rebuilds your previous drop so you can tweak it."
        ),
        inline=False,
    )
    embed.add_field(
        name="🐱 Inject Villager",
        value=(
            f"Enter a name and, optionally, a plot ({PLOT_MIN}–{PLOT_MAX}); leave the plot blank for the next open one. "
            "Misspelled it? I'll suggest close matches before anything happens.\n"
            "• You can't inject someone who already lives on the island.\n"
            "• If the bot can see that the plot you chose is occupied, you'll get a **before / after** screen and must confirm the swap.\n"
            "• **🎀 Sanrio / Amiibo** villagers use a 2-step wizard. If you get disconnected, reopen it within "
            "about 2 hours and it picks up where you left off."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏱️ Cooldowns & the queue",
        value=(
            f"Each drop or injection has a **{ACTION_COOLDOWN:.0f}s** cooldown per island. "
            "Drops on an island are processed one at a time; if others are ahead of you, your result "
            "tells you how many. "
            f"If **{MAX_DROP_QUEUE}** are already waiting you'll get *Island Busy* — try again in a minute."
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ If something goes wrong",
        value=(
            "• **Gate verification failed** — step outside the airport and check your nickname.\n"
            "• **Island bot offline** — nothing was sent; check the status above and retry later.\n"
            "• **Menu expired** — menus close after a few minutes; press the panel button again "
            "(your cart is kept).\n"
            "• **Drop rejected** — check item spelling, and don't add a count to non-stackable items."
        ),
        inline=False,
    )
    embed.add_field(
        name="✈️ While you're on the island",
        value=(
            "• **Don't press minus (−)** to leave — walk to the airport!\n"
            "• Don't litter — use the trash bins.\n"
            "• Never share the Dodo code outside the queue."
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
    if user:
        last = await _load_user_last_drop(user.id)
        if last:
            where = f" on {last['island']}" if last.get("island") and last["island"] != island else ""
            desc += (
                f"🔁 **Last order{where}:** `{last['display_name'][:60]}` "
                "*(press **Re-drop Last Cart** to repeat it here)*\n\n"
            )
    desc += (
        f"• Pick an item preset from the dropdown for instant dropping\n"
        f"• Use **🎒 Saved Pockets** for full curated inventory sets\n"
        f"• Use **🔍 Search Catalog** to search 6,000+ items, series, and recipes\n"
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
