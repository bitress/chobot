"""Discord setup scanning helpers for self-hosted installs."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


DISCORD_API_BASE = "https://discord.com/api/v10"
_USER_AGENT = "DiscordBot (https://github.com/bitress/chobot, 1.0)"


def fetch_guild_channels(bot_token: str, guild_id: str | int) -> list[dict[str, Any]]:
    token = str(bot_token or "").strip()
    guild = str(guild_id or "").strip()
    if not token or not guild:
        raise ValueError("DISCORD_TOKEN and GUILD_ID are required to scan Discord channels")

    req = urllib.request.Request(
        f"{DISCORD_API_BASE}/guilds/{guild}/channels",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Discord channel scan failed with HTTP {exc.code}") from exc
    if not isinstance(data, list):
        raise RuntimeError("Discord channel scan returned an unexpected response")
    return data


def summarize_guild_channels(channels: list[dict[str, Any]]) -> dict[str, Any]:
    categories = []
    text_channels = []
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        item = {
            "id": str(channel.get("id") or ""),
            "name": str(channel.get("name") or ""),
            "type": channel.get("type"),
            "parent_id": str(channel.get("parent_id") or ""),
        }
        if item["type"] == 4:
            categories.append(item)
        elif item["type"] in {0, 5, 15}:
            text_channels.append(item)

    return {
        "categories": sorted(categories, key=lambda c: c["name"].lower()),
        "text_channels": sorted(text_channels, key=lambda c: c["name"].lower()),
        "suggestions": suggest_setup_ids(categories, text_channels),
    }


def suggest_setup_ids(categories: list[dict[str, Any]], text_channels: list[dict[str, Any]]) -> dict[str, str]:
    suggestions = {
        "SUB_CATEGORY_ID": _find_id(categories, r"\b(sub|member|vip|premium)\b"),
        "FREE_CATEGORY_ID": _find_id(categories, r"\b(free|public)\b"),
        "ORDERBOT_CHANNEL_IDS": ",".join(
            channel["id"]
            for channel in text_channels
            if _matches(channel["name"], r"\b(order|berichan|dodo|code|bot)\b")
        ),
        "FLIGHT_LISTEN_CHANNEL_ID": _find_id(text_channels, r"\b(flight|arrival|airport|listen)\b"),
        "FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID": _find_id(text_channels, r"\b(free).*(flight|arrival|airport)\b"),
        "FLIGHT_LOG_CHANNEL_ID": _find_id(text_channels, r"\b(xlog|flight-log|flightlog|mod-log|logs?)\b"),
        "FREE_DODO_BOARD_CHANNEL_ID": _find_id(text_channels, r"\b(dodo).*(board|status|codes?)\b"),
    }
    return {key: value for key, value in suggestions.items() if value}


def _find_id(channels: list[dict[str, Any]], pattern: str) -> str:
    for channel in channels:
        if _matches(str(channel.get("name") or ""), pattern):
            return str(channel.get("id") or "")
    return ""


def _matches(name: str, pattern: str) -> bool:
    normalized = re.sub(r"[-_]+", " ", name.lower())
    return bool(re.search(pattern, normalized, re.IGNORECASE))
