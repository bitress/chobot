"""The Discord "Dodo Log" embed posted for every Dodo code reveal.

Both writers use it: the website (api/flask_api.py, POST /api/islands/<name>/dodo) and the
DropBot Discord flow (bots/cogs/dropbot/status.py). Keeping one builder means the log channel
reads the same whichever way the code was revealed, with the source clearly marked.

Plain dicts (webhook JSON), no discord.py import, so the Flask process can use it too.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

logger = logging.getLogger("DodoLogEmbed")

DODO_GIF = "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"
LOGO_URL = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"

COLOR_OK = 0x2ECC71      # normal reveal
COLOR_REPEAT = 0xF1C40F  # same member revealed this island several times today
COLOR_WARN = 0xE67E22    # nickname not in the ACNH format, or many repeats

REPEAT_WINDOW_SECONDS = 86_400
REPEAT_WARN_AT = 3       # this many reveals of one island by one member in 24h turns the embed orange

SOURCES = {
    "website": ("🌐", "Website"),
    "discord": ("💬", "Discord"),
}


def _is_discord_id(value: str) -> bool:
    value = str(value or "").strip()
    return value.isdigit() and 17 <= len(value) <= 20


def _md(text: str) -> str:
    """Escape Discord markdown and neutralise @mentions in user-supplied text."""
    text = re.sub(r"([\\*_`~|>\[\]()])", r"\\\1", str(text or ""))
    return text.replace("@", "@​")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def island_url(island_name: str) -> str:
    return f"https://www.chopaeng.com/island/{urllib.parse.quote((island_name or '').lower(), safe='')}"


def _island_keys(island_name: str) -> list[str]:
    """Writers clean names slightly differently (accent folding vs plain ASCII): match either."""
    keys = {re.sub(r"[^a-z0-9]", "", (island_name or "").lower())}
    try:
        from utils.helpers import clean_text
        keys.add(clean_text(island_name))
    except Exception:
        pass
    return sorted(k for k in keys if k)


def count_recent_reveals(user_id: str, island_name: str, window: int = REPEAT_WINDOW_SECONDS) -> int:
    """How many times this member revealed this island in the window (including the reveal just saved).

    Returns 0 when it can't be read; the embed then just leaves the repeat line out.
    """
    keys = _island_keys(island_name)
    if not user_id or not keys:
        return 0
    try:
        from utils.database import connect_db

        ph = ",".join("?" for _ in keys)
        with connect_db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM dodo_reveal_messages "
                f"WHERE user_id = ? AND island_clean IN ({ph}) AND created_at >= ?",
                [str(user_id), *keys, int(time.time()) - int(window)],
            ).fetchone()
        return int(row["n"] or 0) if row else 0
    except Exception as exc:
        logger.debug("Repeat count unavailable for %s/%s: %s", user_id, island_name, exc)
        return 0


def build_dodo_log_embed(
    *,
    source: str,
    island_name: str,
    display_name: str,
    user_id: str = "",
    channel_id: str | None = None,
    avatar_url: str = "",
    char_ign: str = "",
    char_island: str = "",
    repeat_count: int = 0,
    nickname_warning: bool = False,
) -> dict:
    """Compact reveal log entry: who, which island, their in-game character, and any flags for mods."""
    emoji, source_label = SOURCES.get(source, ("❔", "Unknown"))
    name = _md((display_name or "").strip() or "Unknown user")
    mention = str(user_id).strip() if _is_discord_id(user_id) else ""
    channel = str(channel_id).strip() if channel_id and str(channel_id).strip().isdigit() else ""
    now = int(time.time())

    member = f"<@{mention}>\n{name}" if mention else f"{name}\n_website account, not in Discord_"
    island = (f"<#{channel}>\n" if channel else f"**{_md(island_name)}**\n") + f"[Open island page]({island_url(island_name)})"
    fields = [
        {"name": "Member", "value": member, "inline": True},
        {"name": "Island", "value": island, "inline": True},
        {
            "name": "In-game character",
            "value": (
                f"**{_md(char_ign)}**" + (f"\nfrom {_md(char_island)}" if char_island else "")
                if char_ign else "_none saved_"
            ),
            "inline": True,
        },
    ]

    flags: list[str] = []
    if repeat_count > 1:
        flags.append(f"🔁 {_ordinal(repeat_count)} reveal of this island by this member in 24h")
    if nickname_warning:
        flags.append("⚠️ Nickname isn't in the required ACNH format")
    if flags:
        fields.append({"name": "Flags", "value": "\n".join(flags), "inline": False})

    if nickname_warning or repeat_count >= REPEAT_WARN_AT:
        color = COLOR_WARN
    elif repeat_count > 1:
        color = COLOR_REPEAT
    else:
        color = COLOR_OK

    author = {"name": f"{emoji} Revealed on {source_label}"}
    if avatar_url and str(avatar_url).startswith("http"):
        author["icon_url"] = str(avatar_url)

    return {
        "author": author,
        "title": f"🛬 Dodo code revealed on {island_name.lower()}"[:256],
        "url": island_url(island_name),
        "description": f"<t:{now}:f> · <t:{now}:R>",
        "color": color,
        "fields": fields,
        "image": {"url": DODO_GIF},
        "footer": {"text": f"Chopaeng Camp™ · Dodo Log · {source_label}", "icon_url": LOGO_URL},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
