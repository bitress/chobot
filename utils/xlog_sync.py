"""
XLog <-> Discord sync helpers.

Mirrors moderation actions taken from the web dashboard into the Discord
#xlog-verbose channel using the REST API (no gateway / intents required).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.config import Config
from utils.discord_http import json_request

logger = logging.getLogger("XLogSync")

_ACTION_STYLE = {
    "WARN": ("\u26a0\ufe0f Warning issued", 0xFEE75C),
    "TIMEOUT": ("\u23f3 Timeout applied", 0xE67E22),
    "BAN": ("\U0001f6ab Ban applied", 0xED4245),
    "KICK": ("\U0001f462 Kick", 0xF1C40F),
    "DISMISS": ("\U0001f5d1\ufe0f Alert dismissed", 0x95A5A6),
    "AUTHORISE": ("\u2705 Flight authorised", 0x57F287),
    "NOTE": ("\U0001f4dd Staff note", 0x5865F2),
    "PARDON": ("\U0001f54a\ufe0f Sanction pardoned", 0x2ECC71),
    "UNDO": ("\u21a9\ufe0f Action reversed", 0x2ECC71),
}


def post_xlog_event(
    action: str,
    mod_name: str,
    reason: str = "",
    *,
    discord_id: str | int | None = None,
    user_id: str | int | None = None,
    ign: str | None = None,
    destination: str | None = None,
    origin: str | None = None,
    visit_id: int | str | None = None,
    expires_at: Optional[int] = None,
    role_stripped: Optional[bool] = None,
    role_restored: bool = False,
    source: str = "Web Dashboard",
) -> bool:
    """Post a moderation embed to the XLog channel. Never raises."""
    token = getattr(Config, "DISCORD_TOKEN", None)
    channel_id = getattr(Config, "XLOG_VERBOSE_CHANNEL_ID", None)
    if not token or not channel_id:
        logger.debug("[XLogSync] Skipped: token or XLog channel not configured")
        return False

    key = (action or "").upper()
    title, color = _ACTION_STYLE.get(key, (f"Moderation: {key.title()}", 0x5865F2))

    target_bits = []
    if discord_id and str(discord_id).isdigit():
        target_bits.append(f"<@{discord_id}> (`{discord_id}`)")
    elif user_id:
        target_bits.append(f"`{user_id}`")
    if ign:
        target_bits.append(f"IGN **{ign}**")

    fields: list[Dict[str, Any]] = [
        {"name": "Moderator", "value": f"**{mod_name or 'Moderator'}**", "inline": True},
        {"name": "Source", "value": source, "inline": True},
        {"name": "Target", "value": " \u2022 ".join(target_bits) or "Unknown", "inline": False},
    ]
    if destination:
        flight = f"**{destination}**" + (f" from **{origin}**" if origin else "")
        fields.append({"name": "Flight", "value": flight, "inline": True})
    if visit_id:
        fields.append({"name": "Visit ID", "value": f"`#{visit_id}`", "inline": True})
    if expires_at and key == "TIMEOUT":
        fields.append({"name": "Expires", "value": f"<t:{int(expires_at)}:R>", "inline": True})
    if role_stripped is not None:
        fields.append({
            "name": "Island Access Role",
            "value": "Removed" if role_stripped else "No change",
            "inline": True,
        })
    if role_restored:
        fields.append({"name": "Island Access Role", "value": "Restored", "inline": True})
    fields.append({"name": "Reason", "value": (reason or "No reason provided.")[:1000], "inline": False})

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": "Chopaeng Camp \u2022 XLog Sync"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        json_request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            method="POST",
            headers={"Authorization": f"Bot {token}"},
            payload={"embeds": [embed]},
            timeout=10,
        )
        return True
    except Exception as exc:
        logger.warning("[XLogSync] Failed to post %s to XLog channel: %s", key, exc)
        return False


def post_xlog_event_async(*args: Any, **kwargs: Any) -> None:
    """Fire-and-forget wrapper so HTTP handlers are never blocked by Discord."""
    import threading

    threading.Thread(target=post_xlog_event, args=args, kwargs=kwargs, daemon=True).start()
