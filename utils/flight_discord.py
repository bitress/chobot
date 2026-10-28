"""
Flight-logger Discord messages, sent over the REST API.

The web dashboard (and its offline auto-check) uses these so members and mods get
exactly what bots/flight_logger.py sends: the "Chobot Notification" DM with a case ID,
the case log in the sub-mod channel, "Verified Flight" posts in #xlog-verbose,
"Unknown Traveler" alerts with the logger's buttons, and "CASE CLOSED" alert edits.
No gateway connection or intents are needed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.config import Config
from utils.discord_http import json_request
from utils.helpers import clean_text

logger = logging.getLogger("FlightDiscord")

API = "https://discord.com/api/v10"

# Same look as bots/flight_logger.py
COLOR_SUCCESS = 0x2ECC71
COLOR_INVESTIGATION = 0xF1C40F
COLOR_WARN = 0xE67E22
COLOR_KICK = 0xF1C40F
COLOR_BAN = 0x992D22
COLOR_DISMISS = 0x95A5A6
COLOR_ALERT = 0xED4245

EMOJI_CHECK = "<:Cho_Check:1456715827213504593>"
EMOJI_INVESTIGATE = "<:Cho_Investigate:1474310726381338666>"
EMOJI_LOVE = "<:ChoLove:818216528449241128>"
EMOJI_STAR = "<a:starpink:784055540321091584>"
EMOJI_HEART = "<a:heartside:784055539881214002>"
EMOJI_WARNING = "<a:CampWarning:1172346431542140961>"
SIREN_GIF = "https://i.ibb.co/HXyRH3R/2668-Siren.gif"
SUB_RULES_CHANNEL = "<#783677194576330792>"

# ---------------------------------------------------------------------------
# Punishment reasons (shared with the Discord flight logger's reason picker)
# ---------------------------------------------------------------------------

DEFAULT_REASON_TEXT = (
    "Breaking [Sub Rule #2](https://discord.com/channels/729590421478703135/"
    "783677194576330792/1137904975553499217). We have removed your island access "
    "for now. Please read the <#783677194576330792> again to gain access."
)

REASON_TEMPLATES = {
    "rule_top_1": "Breaking [Sub Top Rule](https://discord.com/channels/729590421478703135/783677194576330792/1249835404098801756) or [Sub Rule #1](https://discord.com/channels/729590421478703135/783677194576330792/1249835467067752461). We have removed your island access for now. Please contact someone on the mod team in regards to your recent warning. If we do not hear anything within 24 hours you will be banned.",
    "rule_2": "Breaking [Sub Rule #2](https://discord.com/channels/729590421478703135/783677194576330792/1137904975553499217). We have removed your island access for now. Please read the <#783677194576330792> again to gain access.",
    "rule_3_4": "Breaking [Sub Rule #3](https://discord.com/channels/729590421478703135/783677194576330792/1137905005433733211)/[Sub Rule #4](https://discord.com/channels/729590421478703135/783677194576330792/1137905033699151893). We have removed your island access for now.",
    "rule_6": "Breaking [Sub Rule #6](https://discord.com/channels/729590421478703135/783677194576330792/1137905106919096442). We have removed your island access for now. Please read the <#783677194576330792> again to gain access.",
    "rule_8": "Breaking [Sub Rule #8](https://discord.com/channels/729590421478703135/783677194576330792/1137905158257397875). We have removed your island access for now. Please read the <#783677194576330792> again to gain access.",
}

REASON_LABELS = {
    "rule_top_1": "Sub Top Rule / Rule #1",
    "rule_2": "Sub Rule #2",
    "rule_3_4": "Sub Rule #3 / #4",
    "rule_6": "Sub Rule #6",
    "rule_8": "Sub Rule #8",
}

# Web action -> (verb shown to members/mods, embed color, DM emoji)
_ACTIONS = {
    "authorise": ("AUTHORIZED", COLOR_SUCCESS, ""),
    "dismiss": ("DISMISSED", COLOR_DISMISS, ""),
    "warn": ("WARNED", COLOR_WARN, "<:Cho_Warn:1456712416271405188> "),
    "timeout": ("TIMED OUT", COLOR_WARN, "<:Cho_Warn:1456712416271405188> "),
    "kick": ("KICKED", COLOR_KICK, "<:Cho_Kick:1456714701630214349> "),
    "ban": ("BANNED", COLOR_BAN, "<:Cho_Ban:1473530840725061793> "),
}


def make_case_id(now: datetime | None = None) -> str:
    """Case IDs in the flight logger's format: FL-YYMM-XXXX."""
    now = now or datetime.now(timezone.utc)
    return f"FL-{now.strftime('%y%m')}-{hex(int(now.timestamp()))[2:][-4:].upper()}"


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str] | None:
    token = getattr(Config, "DISCORD_TOKEN", None)
    return {"Authorization": f"Bot {token}"} if token else None


def _send(channel_id: Any, payload: dict) -> dict | None:
    headers = _headers()
    if not headers or not channel_id:
        return None
    try:
        return json_request(f"{API}/channels/{channel_id}/messages", method="POST", headers=headers, payload=payload, timeout=10)
    except Exception as exc:
        logger.warning("[FlightDiscord] Could not post to channel %s: %s", channel_id, exc)
        return None


def message_url(message: dict | None) -> str | None:
    if not message or not message.get("id"):
        return None
    return f"https://discord.com/channels/{Config.GUILD_ID}/{message.get('channel_id')}/{message['id']}"


_guild_cache: dict[str, Any] = {"at": 0.0, "guild": None, "channels": None}


def _guild() -> dict:
    """Guild name/icon and channel list, cached for 10 minutes."""
    if _guild_cache["guild"] is not None and time.time() - _guild_cache["at"] < 600:
        return _guild_cache
    headers = _headers()
    if not headers or not Config.GUILD_ID:
        return {"guild": {}, "channels": []}
    try:
        guild = json_request(f"{API}/guilds/{Config.GUILD_ID}", headers=headers, timeout=10) or {}
        channels = json_request(f"{API}/guilds/{Config.GUILD_ID}/channels", headers=headers, timeout=10) or []
        _guild_cache.update(at=time.time(), guild=guild, channels=channels)
    except Exception as exc:
        logger.debug("[FlightDiscord] Guild lookup failed: %s", exc)
        _guild_cache.update(at=time.time(), guild=_guild_cache["guild"] or {}, channels=_guild_cache["channels"] or [])
    return _guild_cache


def guild_name() -> str:
    return (_guild().get("guild") or {}).get("name") or "ChoPaeng Camp"


def guild_icon_url() -> str | None:
    g = _guild().get("guild") or {}
    return f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("id") and g.get("icon") else None


def island_channel_link(destination: str) -> str:
    """'<#channel>' for the island's Discord channel (matched by name, like the logger)."""
    island = clean_text(destination or "")
    for ch in _guild().get("channels") or []:
        name = clean_text(ch.get("name") or "")
        if ch.get("type") == 0 and island and (name == island or island in name):
            return f"<#{ch['id']}>"
    return (destination or "Unknown").title()


def _user_label(user_id: Any, name: str | None = None) -> str:
    """Matches the logger's '_format_user_for_embed': 'Name (<@id>)'."""
    if not user_id:
        return name or "Visitor (unlinked)"
    return f"{name} (<@{user_id}>)" if name else f"<@{user_id}>"


def _footer(text: str) -> dict:
    icon = guild_icon_url()
    return {"text": text, **({"icon_url": icon} if icon else {})}


# ---------------------------------------------------------------------------
# Punishments: DM + case log
# ---------------------------------------------------------------------------

def send_member_dm(discord_id: Any, action: str, reason: str, case_id: str) -> str:
    """The flight logger's 'Chobot Notification' DM.

    Returns "sent", "dms_closed" (the member blocks DMs from the bot), "failed" or "skipped".
    """
    headers = _headers()
    uid = str(discord_id or "").strip()
    if not headers or not uid.isdigit():
        return "skipped"
    verb, color, emoji = _ACTIONS.get(action, (action.upper(), COLOR_WARN, ""))
    if action == "undo":
        title, color = "Chobot Notification", COLOR_SUCCESS
        description = f"A moderator reversed your recent case in **{guild_name()}**. Your island access has been restored."
    else:
        title = f"{emoji}Chobot Notification"
        description = f"You have been **{verb.lower()}** from **{guild_name()}**."
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": [{"name": "Reason", "value": (reason or "No reason provided.")[:1024], "inline": False}],
        "footer": {"text": f"Case ID: {case_id}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    icon = guild_icon_url()
    if icon:
        embed["thumbnail"] = {"url": icon}
    try:
        channel = json_request(f"{API}/users/@me/channels", method="POST", headers=headers, payload={"recipient_id": uid}, timeout=10)
        json_request(f"{API}/channels/{channel['id']}/messages", method="POST", headers=headers, payload={"embeds": [embed]}, timeout=10)
        return "sent"
    except Exception as exc:
        if getattr(exc, "code", None) == 403:
            logger.info("[FlightDiscord] DM to %s not delivered: DMs closed", uid)
            return "dms_closed"
        logger.warning("[FlightDiscord] DM to %s failed: %s", uid, exc)
        return "failed"


def post_case_log(
    discord_id: Any,
    member_name: str | None,
    mod_name: str,
    action: str,
    reason: str,
    case_id: str,
    *,
    warn_count: int = 0,
    expires_at: int | None = None,
) -> str | None:
    """The logger's case log ('Warned Case ID: FL-…') in the sub-mod channel. Returns its URL."""
    verb = _ACTIONS.get(action, (action.upper(),))[0]
    member = _user_label(discord_id, member_name)
    responsible = f"{mod_name} (Website)"
    if verb in ("KICKED", "BANNED"):
        lines = [
            f"> **{member}** has been {verb.lower()}!",
            f"> **Reason:** {reason}",
            f"> **Responsible:** {responsible}",
        ]
    else:
        duration = "Until rules are re-accepted"
        if expires_at:
            hours = max(1, round((expires_at - time.time()) / 3600))
            duration = f"{hours}h"
        lines = [
            f"> **{member}** has been {verb.lower()}!",
            f"> **Reason:** {reason}",
            f"> **Duration:** {duration}",
            f"> **Count:** {warn_count}",
            f"> **Responsible:** {responsible}",
        ]
        if expires_at:
            lines.append(f"> Automatically expires <t:{int(expires_at)}:R>")
        lines.extend([
            "> **Proof:** Verified (Log System)",
            "> ",
            f"> **For Sub Members**: Please double check our {SUB_RULES_CHANNEL} channel.",
            "> **For Free Members**: Kindly refer to our <#755522711492493342> channel.",
        ])
    embed = {
        "title": f"**{verb.title()} Case ID: {case_id}**",
        "description": "\n".join(lines),
        "color": 0xFF0000,
        "thumbnail": {"url": SIREN_GIF},
        "footer": {"text": f"Mod: {mod_name} (Website)"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = f"<@{discord_id}>" if str(discord_id or "").isdigit() else None
    msg = _send(getattr(Config, "SUB_MOD_CHANNEL_ID", None), {"content": content, "embeds": [embed]})
    return message_url(msg)


# ---------------------------------------------------------------------------
# Flight alerts
# ---------------------------------------------------------------------------

def _button(label: str, style: int, custom_id: str, emoji: dict) -> dict:
    return {"type": 2, "label": label, "style": style, "custom_id": custom_id, "emoji": emoji}


def _emoji(name: str, emoji_id: str) -> dict:
    return {"name": name, "id": emoji_id}


# The logger's persistent TravelerActionView (handled by the bot when it is online)
TRAVELER_ACTION_COMPONENTS = [
    {"type": 1, "components": [
        _button("Investigate", 2, "fl_investigate", _emoji("Cho_Investigate", "1474310726381338666")),
        _button("Admit", 3, "fl_admit", _emoji("Cho_Check", "1456715827213504593")),
    ]},
    {"type": 1, "components": [
        _button("Warn", 1, "fl_warn", _emoji("Cho_Warn", "1456712416271405188")),
        _button("Kick", 2, "fl_kick", _emoji("Cho_Kick", "1456714701630214349")),
        _button("Ban", 4, "fl_ban", _emoji("Cho_Ban", "1473530840725061793")),
    ]},
    {"type": 1, "components": [
        _button("Dismiss", 2, "fl_dismiss", _emoji("Cho_Dismiss", "1474955282026332180")),
        _button("Note", 2, "fl_note", _emoji("Cho_Notes", "1474311464688029817")),
    ]},
]
VERIFIED_FLAG_COMPONENTS = [
    {"type": 1, "components": [{"type": 2, "label": "Flag", "style": 2, "custom_id": "fl_flag_verified", "emoji": {"name": "\U0001f6a9"}}]},
]


def post_verified_flight(visit: dict, member_id: str, member_name: str, has_access: bool, sub_role_ids: list[str]) -> str | None:
    """The logger's 'Verified Flight' post in #xlog-verbose."""
    lines = [f"Member Linked: {_user_label(member_id, member_name)}"]
    if has_access:
        lines.append(f"{EMOJI_STAR} **Has Island Access?** Yes")
        if sub_role_ids:
            lines.append(f"{EMOJI_HEART} **Subscription(s):**\n> " + "\n> ".join(f"<@&{r}>" for r in sub_role_ids))
        else:
            lines.append(f"{EMOJI_WARNING} **Subscription(s):**\n> None detected for this island.")
    else:
        lines.append(f"{EMOJI_WARNING} **No Island Access Role**")
    lines.append("\nLog details matched with a member.")
    embed = {
        "title": f"{EMOJI_CHECK} Verified Flight",
        "description": "\n".join(lines),
        "color": COLOR_SUCCESS,
        "fields": [
            {"name": "IGN", "value": f"```yaml\n{visit['ign']}```", "inline": True},
            {"name": "Island Name", "value": f"```yaml\n{(visit['origin_island'] or '?').title()}```", "inline": True},
            {"name": "Destination", "value": island_channel_link(visit["destination"]), "inline": True},
            {"name": "Visit ID", "value": f"`#{visit['id']}`", "inline": True},
        ],
        "image": {"url": Config.FOOTER_LINE},
        "footer": _footer("Chopaeng Camp™ • Match Log (website check)"),
        "timestamp": datetime.fromtimestamp(visit["timestamp"], tz=timezone.utc).isoformat(),
    }
    return message_url(_send(Config.XLOG_VERBOSE_CHANNEL_ID, {"embeds": [embed], "components": VERIFIED_FLAG_COMPONENTS}))


def post_flight_alert(visit: dict, verdict: str, detail: dict) -> dict | None:
    """The logger's review alert in #flight-logs, with its action buttons. Returns the message."""
    dest = island_channel_link(visit["destination"])
    if verdict == "ambiguous_match":
        heading, color = f"### {Config.EMOJI_FAIL} Ambiguous Traveler Match", COLOR_INVESTIGATION
        body = f"Multiple members matched this flight for **{dest}**.\nManual review is required before admitting."
    elif verdict == "unknown_traveler":
        heading, color = f"### {Config.EMOJI_FAIL} Unknown Traveler Detected", COLOR_ALERT
        body = f"An unregistered visitor is attempting to join **{dest}**.\nUse the buttons below to take action."
    else:
        heading, color = f"### {EMOJI_WARNING} Flagged Flight", COLOR_INVESTIGATION
        body = f"{detail.get('reason') or 'Flagged for review'}\nUse the buttons below to take action."
    fields = [
        {"name": "Traveler (IGN)", "value": f"```yaml\n{visit['ign']}```", "inline": True},
        {"name": "Origin Island", "value": f"```yaml\n{(visit['origin_island'] or '?').title()}```", "inline": True},
        {"name": "Destination", "value": f"```yaml\n{visit['destination'].title()}```", "inline": True},
    ]
    if detail.get("member"):
        member = detail["member"]
        fields.append({"name": "Member", "value": _user_label(member.get("id"), member.get("name")), "inline": False})
    if detail.get("candidates"):
        fields.append({
            "name": "Possible Matches",
            "value": "\n".join(_user_label(c.get("id"), c.get("name")) for c in detail["candidates"][:15]),
            "inline": False,
        })
    fields += [
        {"name": "Detected", "value": f"<t:{int(visit['timestamp'])}:R>", "inline": True},
        {"name": "Status", "value": f"{EMOJI_INVESTIGATE} **PENDING REVIEW**", "inline": True},
        {"name": "Visit ID", "value": f"`#{visit['id']}`", "inline": True},
    ]
    embed = {
        "description": f"{heading}\n{body}",
        "color": color,
        "fields": fields,
        "image": {"url": Config.FOOTER_LINE},
        "footer": _footer("Chopaeng Camp™ • Flight Logger (website check)"),
        "timestamp": datetime.fromtimestamp(visit["timestamp"], tz=timezone.utc).isoformat(),
    }
    return _send(Config.FLIGHT_LOG_CHANNEL_ID, {"embeds": [embed], "components": TRAVELER_ACTION_COMPONENTS})


def _clean_embed(embed: dict[str, Any]) -> dict[str, Any]:
    """Keep only the embed keys Discord accepts when editing a message."""
    out: dict[str, Any] = {k: embed[k] for k in ("title", "description", "url", "color", "timestamp") if embed.get(k)}
    if embed.get("fields"):
        out["fields"] = [
            {"name": f.get("name", ""), "value": f.get("value", ""), "inline": bool(f.get("inline"))}
            for f in embed["fields"]
        ]
    for key, allowed in (("footer", ("text", "icon_url")), ("author", ("name", "url", "icon_url")),
                         ("image", ("url",)), ("thumbnail", ("url",))):
        if isinstance(embed.get(key), dict):
            part = {k: embed[key][k] for k in allowed if embed[key].get(k)}
            if part:
                out[key] = part
    return out


def close_flight_alert(
    channel_id: Any,
    message_id: Any,
    action: str,
    mod_name: str,
    *,
    target_id: Any = None,
    target_name: str | None = None,
    reason: str | None = None,
    mod_log_url: str | None = None,
    has_access: bool | None = None,
) -> bool:
    """Edit a #flight-logs alert the way the logger's buttons do: CASE CLOSED + Action Taken."""
    headers = _headers()
    if not headers or not channel_id or not message_id:
        return False
    url = f"{API}/channels/{channel_id}/messages/{message_id}"
    try:
        message = json_request(url, headers=headers, timeout=10)
        embeds = message.get("embeds") if isinstance(message, dict) else None
        if not embeds:
            return False
        embed = _clean_embed(embeds[0])
        fields = [f for f in embed.get("fields", []) if "Investigating" not in f["name"] and "Action Taken" not in f["name"]]

        if action == "undo":
            status = f"{EMOJI_INVESTIGATE} **REOPENED** on the website — review it on the dashboard"
            color = COLOR_INVESTIGATION
            embed.pop("author", None)
        else:
            label, color, _ = _ACTIONS.get(action, (action.upper(), COLOR_WARN, ""))
            status = f"{EMOJI_CHECK} **{label}**"
            embed["author"] = {"name": f"CASE CLOSED: {label}"}
            resolved = int(time.time())
            action_value = (
                f"**{label}** by {mod_name} (Website)\n"
                f"Target: {_user_label(target_id, target_name)}\n"
                f"Resolved <t:{resolved}:R>"
            )
            if has_access is not None and target_id:
                action_value += f"\n**Has Island Access?** {'Yes' if has_access else 'No'}"
            if reason:
                action_value += f"\n**Reason:** {reason}"
            if mod_log_url:
                action_value += f"\n[View in Mod Log]({mod_log_url})"
            fields.append({"name": f"{EMOJI_LOVE} Action Taken", "value": action_value[:1024], "inline": False})

        status_field = next((f for f in fields if f["name"] == "Status"), None)
        if status_field:
            status_field["value"] = status
        else:
            fields.append({"name": "Status", "value": status, "inline": True})
        embed["fields"] = fields
        embed["color"] = color
        json_request(url, method="PATCH", headers=headers, payload={"embeds": [embed], "components": []}, timeout=10)
        return True
    except Exception as exc:
        logger.warning("[FlightDiscord] Could not update alert %s/%s: %s", channel_id, message_id, exc)
        return False


def run_async(fn, *args, **kwargs) -> None:
    """Fire-and-forget so HTTP handlers are never blocked by Discord."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def warn_count(conn, discord_id: Any, days: int = 3) -> int:
    """Active warnings in the logger's sense (WARN_EXPIRY_DAYS = 3)."""
    if not str(discord_id or "").isdigit():
        return 0
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    row = conn.execute(
        "SELECT COUNT(*) FROM warnings WHERE user_id = ? AND timestamp > ? AND action_type IN ('WARN', 'TIMEOUT', 'BAN')",
        (int(discord_id), cutoff),
    ).fetchone()
    return int(row[0]) if row else 0
