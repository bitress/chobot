"""
Discord Moderation Notifier Module
Dispatches Discord DMs to members when a moderation action (Warn, Timeout, Ban) is applied.
Uses Discord REST API with the bot token. Does NOT require any Discord Gateway Privileged Intents!
"""

from __future__ import annotations

import logging
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.config import Config
from utils.discord_http import json_request

logger = logging.getLogger("DiscordModerationNotifier")


def send_discord_moderation_dm(
    discord_id: str | int,
    action: str,
    reason: str,
    mod_name: str,
    expires_at: Optional[int] = None,
    flight_details: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send an embed DM to a user using Discord REST API with the Bot Token.
    
    Safe & non-blocking:
    - Does NOT require Gateway Privileged Intents.
    - Catches HTTP 403 (user has DMs closed or no shared server) cleanly without errors.
    """
    token = Config.DISCORD_TOKEN
    uid = str(discord_id or "").strip()
    if not token or not uid or not uid.isdigit():
        logger.debug("[ModerationDM] Skipping DM: token present=%s, valid uid=%s", bool(token), uid)
        return False

    headers = {
        "Authorization": f"Bot {token}",
    }

    try:
        # Step 1: Open/Fetch DM channel with user
        dm_channel = json_request(
            "https://discord.com/api/v10/users/@me/channels",
            method="POST",
            headers=headers,
            payload={"recipient_id": uid},
            timeout=10,
        )
        channel_id = dm_channel.get("id") if isinstance(dm_channel, dict) else None
        if not channel_id:
            logger.warning("[ModerationDM] Failed to create DM channel for %s", uid)
            return False

        # Step 2: Format Discord Embed
        action_upper = action.upper()
        if action_upper == "WARN":
            title = "⚠️ Official Warning - ChoPaeng Islands"
            color = 0xFEE75C  # Yellow
            action_desc = "You have received an official warning from server moderation."
        elif action_upper == "TIMEOUT":
            title = "⏳ Temporary Timeout - Island Access Suspended"
            color = 0xE67E22  # Orange
            exp_str = f"<t:{expires_at}:F> (<t:{expires_at}:R>)" if expires_at else "Temporary"
            action_desc = f"Your island access has been temporarily suspended until {exp_str}."
        elif action_upper == "BAN":
            title = "🚫 Account Suspended - Island Access Revoked"
            color = 0xED4245  # Red
            action_desc = "Your island access has been permanently suspended."
        else:
            title = f"Notice: Moderation Action ({action.capitalize()})"
            color = 0x5865F2  # Blurple
            action_desc = f"A moderation action ({action}) has been applied to your account."

        fields = [
            {"name": "Moderator", "value": f"**{mod_name or 'Moderator'}**", "inline": True},
            {"name": "Action", "value": action.capitalize(), "inline": True},
        ]
        if expires_at and action_upper == "TIMEOUT":
            fields.append({"name": "Expires", "value": f"<t:{expires_at}:R>", "inline": True})

        fields.append({"name": "Reason", "value": reason or "No specific reason provided.", "inline": False})

        if flight_details:
            dest = flight_details.get("destination") or "Unknown"
            ign = flight_details.get("flight_ign") or ""
            origin = flight_details.get("flight_origin") or ""
            flight_str = f"Flight to **{dest}**"
            if ign and origin:
                flight_str += f" with character **{ign}** from **{origin}**"
            elif ign:
                flight_str += f" with character **{ign}**"
            fields.append({"name": "Flight Context", "value": flight_str, "inline": False})

        fields.append({
            "name": "Appeals & Support",
            "value": "If you believe this action was made in error, please open a support ticket in our Discord server.",
            "inline": False,
        })

        embed = {
            "title": title,
            "description": action_desc,
            "color": color,
            "fields": fields,
            "footer": {"text": "ChoPaeng Island Moderation System"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Step 3: Send DM message
        json_request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            method="POST",
            headers=headers,
            payload={"embeds": [embed]},
            timeout=10,
        )
        logger.info("[ModerationDM] Sent %s notification DM to Discord user %s", action, uid)
        return True

    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # 50007 = Cannot send messages to this user (DMs closed or no shared guild)
            logger.info("[ModerationDM] User %s has DMs closed or blocked bot (HTTP 403)", uid)
        else:
            logger.warning("[ModerationDM] HTTP %s error sending DM to %s: %s", exc.code, uid, exc)
    except Exception as exc:
        logger.warning("[ModerationDM] Failed to dispatch moderation DM to %s: %s", uid, exc)

    return False
