"""Dodo reveal log panel: staff pick a sub island and see who revealed its Dodo code, and when.

- /revealpanel deploys the panel (staff only). It is public: picking an island updates the panel
  message in place for everyone in the channel. Only staff can operate it.
- Reads dodo_reveal_messages, which both the website (POST /api/islands/<name>/dodo) and the
  DropBot Discord flow write to. Each row is tagged with its source ('website' / 'discord');
  rows logged before source tracking show as unknown.
- Times are Discord timestamps (<t:unix:f> / <t:unix:R>), so every viewer sees their own timezone.
- State (island, window, page) lives in each component's custom_id, so the panel keeps working
  after restarts once register_reveal_panel() has run. Nothing else is stored.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import discord

from utils.config import Config

logger = logging.getLogger("DodoRevealPanel")

COLOR_PANEL = 0x7DD3C0
PAGE_SIZE = 15
SELECT_MAX = 25
# Order-bot islands hand out codes through the order channels, never through a reveal
ORDER_ISLANDS = {"sinta"}

WINDOWS: dict[str, tuple[str, int]] = {
    "1h": ("Last hour", 3_600),
    "24h": ("Last 24 hours", 86_400),
    "7d": ("Last 7 days", 7 * 86_400),
    "30d": ("Last 30 days", 30 * 86_400),
}
DEFAULT_WINDOW = "24h"

SOURCE_LABELS = {"website": ("🌐", "Website"), "discord": ("💬", "Discord")}
UNKNOWN_SOURCE = ("❔", "Before source tracking")

NO_ISLAND = "-"


# ---------------------------------------------------------------------------
# Islands and access
# ---------------------------------------------------------------------------

def _clean(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def panel_islands() -> list[str]:
    """Sub islands only (no free or order islands), alphabetical, de-duplicated."""
    seen: dict[str, str] = {}
    for name in getattr(Config, "SUB_ISLANDS", []) or []:
        key = _clean(str(name))
        if key and key not in ORDER_ISLANDS and key not in seen:
            seen[key] = str(name)
    return sorted(seen.values(), key=str.lower)


def island_clean_keys(island: str) -> list[str]:
    """Both writers clean names slightly differently (accent folding vs plain ASCII), so match either."""
    keys = {_clean(island)}
    try:
        from utils.helpers import clean_text
        keys.add(clean_text(island))
    except Exception:
        pass
    return sorted(k for k in keys if k)


def can_use_panel(user: discord.abc.User) -> bool:
    """Staff only: Discord administrators and mod roles (same rule as the nickname panel)."""
    if not isinstance(user, discord.Member):
        return False
    from bots.nickname_panel import is_staff
    return is_staff(user)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class RevealRow:
    user_id: str
    username: str
    nickname: str
    message_url: str
    created_at: int
    source: str | None


@dataclass
class RevealPage:
    rows: list[RevealRow] = field(default_factory=list)
    total: int = 0
    unique_members: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    repeaters: list[tuple[RevealRow, int]] = field(default_factory=list)


def fetch_reveals(island: str, since: int, page: int, page_size: int = PAGE_SIZE) -> RevealPage:
    """One page of reveals for an island since a Unix time, newest first, plus summary counts."""
    from utils.database import connect_db

    keys = island_clean_keys(island)
    ph = ",".join("?" for _ in keys)
    where = f"island_clean IN ({ph}) AND created_at >= ?"
    params = [*keys, int(since)]

    with connect_db() as conn:
        try:
            src_col = "source"
            conn.execute(f"SELECT source FROM dodo_reveal_messages WHERE {where} LIMIT 1", params).fetchall()
        except Exception:
            src_col = "NULL"  # column not migrated yet: everything shows as unknown source

        stats = conn.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS u FROM dodo_reveal_messages WHERE {where}",
            params,
        ).fetchone()
        result = RevealPage(total=int(stats["n"] or 0), unique_members=int(stats["u"] or 0))
        if not result.total:
            return result

        rows = conn.execute(
            f"SELECT user_id, username, nickname, message_url, created_at, {src_col} AS source "
            f"FROM dodo_reveal_messages WHERE {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, int(page_size), int(max(page, 0) * page_size)],
        ).fetchall()
        result.rows = [_row(r) for r in rows]

        for r in conn.execute(
            f"SELECT {src_col} AS source, COUNT(*) AS n FROM dodo_reveal_messages WHERE {where} GROUP BY {src_col}",
            params,
        ).fetchall():
            result.by_source[str(r["source"] or "")] = int(r["n"] or 0)

        for r in conn.execute(
            "SELECT user_id, MAX(username) AS username, MAX(nickname) AS nickname, COUNT(*) AS n "
            f"FROM dodo_reveal_messages WHERE {where} GROUP BY user_id HAVING COUNT(*) > 1 "
            "ORDER BY n DESC LIMIT 3",
            params,
        ).fetchall():
            result.repeaters.append((
                RevealRow(str(r["user_id"] or ""), str(r["username"] or ""), str(r["nickname"] or ""), "", 0, None),
                int(r["n"] or 0),
            ))
    return result


def _row(r: Any) -> RevealRow:
    return RevealRow(
        user_id=str(r["user_id"] or ""),
        username=str(r["username"] or ""),
        nickname=str(r["nickname"] or ""),
        message_url=str(r["message_url"] or ""),
        created_at=int(r["created_at"] or 0),
        source=(str(r["source"]) if r["source"] else None),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _is_discord_id(value: str) -> bool:
    return value.isdigit() and 17 <= len(value) <= 20


def _esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def format_member(row: RevealRow) -> str:
    """Mention Discord members (embeds never ping); website-only accounts show their username."""
    name = row.nickname or row.username
    if _is_discord_id(row.user_id):
        return f"<@{row.user_id}>" + (f" ({_esc(name)})" if name else "")
    return f"{_esc(row.username or 'unknown')} (website account)"


def source_badge(source: str | None) -> str:
    emoji, _ = SOURCE_LABELS.get(source or "", UNKNOWN_SOURCE)
    return emoji


def format_row(row: RevealRow) -> str:
    when = f"<t:{row.created_at}:f> · <t:{row.created_at}:R>"
    log = f" · [Log]({row.message_url})" if row.message_url.startswith("http") else ""
    return f"{when}\n{source_badge(row.source)} {format_member(row)}{log}"


def total_pages(total: int, page_size: int = PAGE_SIZE) -> int:
    return max(1, -(-total // page_size))


def build_intro_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛬 Dodo reveal log",
        description=(
            "Pick a **sub island** to see who revealed its Dodo code, and when.\n\n"
            "• Times show in **your own timezone**.\n"
            "• 🌐 revealed on the website · 💬 revealed in Discord · ❔ logged before source tracking.\n"
            "• Staff only. Results update this message for everyone in the channel."
        ),
        color=COLOR_PANEL,
    )
    embed.set_footer(text="Chopaeng Camp™ • Dodo Reveal Log")
    return embed


def build_results_embed(island: str, window: str, page: int, data: RevealPage, viewer_id: int | None) -> discord.Embed:
    label = WINDOWS.get(window, WINDOWS[DEFAULT_WINDOW])[0].lower()
    now = int(time.time())
    lines: list[str] = []
    if data.total:
        lines.append(
            f"**{data.total}** reveal{'s' if data.total != 1 else ''} by **{data.unique_members}** "
            f"member{'s' if data.unique_members != 1 else ''} · {label}"
        )
        counts = []
        for key in ("website", "discord", ""):
            n = data.by_source.get(key, 0)
            if n:
                emoji, name = SOURCE_LABELS.get(key, UNKNOWN_SOURCE)
                counts.append(f"{emoji} {name} {n}")
        if counts:
            lines.append(" · ".join(counts))
        if data.repeaters:
            lines.append("Most reveals: " + " · ".join(f"{format_member(r)} ×{n}" for r, n in data.repeaters))
    else:
        lines.append(f"No Dodo reveals for **{_esc(island)}** · {label}.")
    refreshed = f"Updated <t:{now}:R>" + (f" by <@{viewer_id}>" if viewer_id else "")
    lines.append(f"_{refreshed}_")

    body = "\n".join(lines)
    if data.rows:
        entries = []
        size = len(body) + 2
        for row in data.rows:
            entry = format_row(row)
            if size + len(entry) + 2 > 4000:  # embed description limit is 4096
                break
            entries.append(entry)
            size += len(entry) + 2
        body += "\n\n" + "\n\n".join(entries)

    embed = discord.Embed(title=f"🛬 Dodo reveals · {island}", description=body, color=COLOR_PANEL)
    pages = total_pages(data.total)
    embed.set_footer(
        text=f"Page {min(page, pages - 1) + 1}/{pages} • Times are in your timezone • Chopaeng Camp™ Dodo Reveal Log"
    )
    return embed


# ---------------------------------------------------------------------------
# Components (state encoded in custom_id so they survive restarts)
# ---------------------------------------------------------------------------

def _q(island: str | None) -> str:
    return urllib.parse.quote(island, safe="") if island else NO_ISLAND


def _uq(value: str) -> str | None:
    return None if value == NO_ISLAND else urllib.parse.unquote(value)


def _window(value: str) -> str:
    return value if value in WINDOWS else DEFAULT_WINDOW


class RevealIslandSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"dodolog:isl:(?P<win>\w+):(?P<part>\d+)"):
    """Sub island picker. Split into several menus if there are ever more than 25 sub islands."""

    def __init__(self, window: str, part: int = 0, current: str | None = None):
        self.window = _window(window)
        self.part = part
        islands = panel_islands()
        chunk = islands[part * SELECT_MAX:(part + 1) * SELECT_MAX] or ["(no sub islands)"]
        multi = len(islands) > SELECT_MAX
        placeholder = "👑 Pick a sub island…" if not multi else f"👑 Sub islands {chunk[0][0]}–{chunk[-1][0]}…"
        options = [
            discord.SelectOption(
                label=name[:100], value=name[:100], emoji="👑",
                default=bool(current) and _clean(name) == _clean(current or ""),
            )
            for name in chunk
        ]
        super().__init__(
            discord.ui.Select(
                custom_id=f"dodolog:isl:{self.window}:{part}",
                placeholder=placeholder,
                options=options,
                row=part,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /):
        return cls(match["win"], int(match["part"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        await render(interaction, values[0] if values else None, self.window, 0)


class RevealWindowSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"dodolog:win:(?P<island>[^:]+)"):
    """Time window picker."""

    def __init__(self, island: str | None, window: str = DEFAULT_WINDOW, row: int = 1):
        self.island = island
        options = [
            discord.SelectOption(label=label, value=key, emoji="🕒", default=key == _window(window))
            for key, (label, _) in WINDOWS.items()
        ]
        super().__init__(
            discord.ui.Select(
                custom_id=f"dodolog:win:{_q(island)}",
                placeholder="🕒 Time window…",
                options=options,
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /):
        return cls(_uq(match["island"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or [DEFAULT_WINDOW]
        await render(interaction, self.island, _window(values[0]), 0)


_NAV = {"prev": ("◀", "Newer"), "next": ("▶", "Older"), "ref": ("🔄", "Refresh")}


class RevealNavButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dodolog:(?P<action>prev|next|ref):(?P<island>[^:]+):(?P<win>\w+):(?P<page>\d+)",
):
    """Paging and refresh. Each button carries the page it leads to."""

    def __init__(self, action: str, island: str | None, window: str, page: int, disabled: bool = False, row: int = 2):
        self.action = action
        self.island = island
        self.window = _window(window)
        self.page = max(page, 0)
        emoji, label = _NAV[action]
        super().__init__(
            discord.ui.Button(
                custom_id=f"dodolog:{action}:{_q(island)}:{self.window}:{self.page}",
                emoji=emoji,
                label=label,
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /):
        return cls(match["action"], _uq(match["island"]), match["win"], int(match["page"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await render(interaction, self.island, self.window, self.page)


def build_view(island: str | None, window: str = DEFAULT_WINDOW, page: int = 0, total: int = 0) -> discord.ui.View:
    """Island menu(s), time window menu, then Newer / Older / Refresh."""
    view = discord.ui.View(timeout=None)
    parts = max(1, -(-len(panel_islands()) // SELECT_MAX))
    parts = min(parts, 3)  # leave rows for the window menu and buttons (5 rows max)
    for part in range(parts):
        view.add_item(RevealIslandSelect(window, part, island))
    view.add_item(RevealWindowSelect(island, window, row=parts))
    pages = total_pages(total)
    nav_row = parts + 1
    view.add_item(RevealNavButton("prev", island, window, page - 1, disabled=not island or page <= 0, row=nav_row))
    view.add_item(RevealNavButton("next", island, window, page + 1, disabled=not island or page >= pages - 1, row=nav_row))
    view.add_item(RevealNavButton("ref", island, window, page, disabled=not island, row=nav_row))
    return view


async def render(interaction: discord.Interaction, island: str | None, window: str, page: int) -> None:
    """Staff check, then update the panel message in place for everyone."""
    if not can_use_panel(interaction.user):
        await interaction.response.send_message("Only staff can use the Dodo reveal log.", ephemeral=True)
        return
    window = _window(window)
    if not island:
        await interaction.response.edit_message(embed=build_intro_embed(), view=build_view(None, window))
        return

    await interaction.response.defer()
    since = int(time.time()) - WINDOWS[window][1]
    try:
        data = await asyncio.to_thread(fetch_reveals, island, since, page)
        if data.total and page >= total_pages(data.total):  # list shrank since the button was drawn
            page = total_pages(data.total) - 1
            data = await asyncio.to_thread(fetch_reveals, island, since, page)
    except Exception as exc:
        logger.error(f"[REVEALLOG] Could not load reveals for {island}: {exc}")
        await interaction.followup.send(
            "Couldn't load the reveal log right now. Try again in a moment, or check the bot logs.", ephemeral=True
        )
        return
    embed = build_results_embed(island, window, page, data, getattr(interaction.user, "id", None))
    await interaction.edit_original_response(
        embed=embed,
        view=build_view(island, window, page, data.total),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    logger.info(f"[REVEALLOG] {interaction.user} viewed {island} ({window}, page {page + 1})")


def register_reveal_panel(bot) -> None:
    """Persistent panel components. Call from setup_hook."""
    bot.add_dynamic_items(RevealIslandSelect, RevealWindowSelect, RevealNavButton)
