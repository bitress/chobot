"""Dodo reveal log panel: staff pick a sub island and see who revealed its Dodo code, and when.

- /revealpanel deploys the panel (staff only). It is public: picking an island updates the panel
  message in place for everyone in the channel. Only staff can operate it.
- Reads dodo_reveal_messages, which both the website (POST /api/islands/<name>/dodo) and the
  DropBot Discord flow write to. Each row is tagged with its source ('website' / 'discord');
  rows logged before source tracking show as unknown.
- Times are Discord timestamps (<t:unix:f> / <t:unix:R>), so every viewer sees their own timezone.
- State (island, window, page) lives in each component's custom_id, so the panel keeps working
  after restarts once setup_reveal_panel() has run.
- Sticky: when anything else is posted in the channel, the panel is reposted at the bottom (debounced),
  keeping the same island / window / page. The panel's channel and message id live in
  dodo_reveal_panels. Deleting the panel by hand stops the sticky behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands

from utils.config import Config

logger = logging.getLogger("DodoRevealPanel")

COLOR_PANEL = 0x7DD3C0
COLOR_EMPTY = 0x99AAB5
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


def format_row(row: RevealRow, number: int | None = None) -> str:
    """Two lines: who (with source badge), then when and the Dodo Log link."""
    num = f"`{number:>2}` " if number is not None else ""
    log = f" · [Log]({row.message_url})" if row.message_url.startswith("http") else ""
    return (
        f"{num}{source_badge(row.source)} {format_member(row)}\n"
        f"<t:{row.created_at}:f> · <t:{row.created_at}:R>{log}"
    )


def total_pages(total: int, page_size: int = PAGE_SIZE) -> int:
    return max(1, -(-total // page_size))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _window_label(window: str) -> str:
    return WINDOWS.get(window, WINDOWS[DEFAULT_WINDOW])[0]


LEGEND = "🌐 website · 💬 Discord · ❔ before source tracking"


@dataclass
class IslandActivity:
    count: int = 0
    last: int = 0


def fetch_island_counts(since: int) -> dict[str, IslandActivity]:
    """Reveal count and latest reveal per sub island since a Unix time (keyed by island name)."""
    from utils.database import connect_db

    with connect_db() as conn:
        rows = conn.execute(
            "SELECT island_clean, COUNT(*) AS n, MAX(created_at) AS last FROM dodo_reveal_messages "
            "WHERE created_at >= ? GROUP BY island_clean",
            [int(since)],
        ).fetchall()
    by_key = {str(r["island_clean"] or ""): (int(r["n"] or 0), int(r["last"] or 0)) for r in rows}
    result: dict[str, IslandActivity] = {}
    for island in panel_islands():
        act = IslandActivity()
        for key in island_clean_keys(island):
            n, last = by_key.get(key, (0, 0))
            act.count += n
            act.last = max(act.last, last)
        result[island] = act
    return result


def build_overview_embed(
    window: str = DEFAULT_WINDOW,
    counts: dict[str, IslandActivity] | None = None,
    viewer_id: int | None = None,
) -> discord.Embed:
    """Landing view: how to use the panel, plus which sub islands were busiest in the window."""
    label = _window_label(window)
    embed = discord.Embed(
        title="🛬 Dodo reveal log",
        description=(
            "Pick a **sub island** below to see who revealed its Dodo code, and when.\n"
            f"Times show in your own timezone. {LEGEND}."
        ),
        color=COLOR_PANEL,
        timestamp=discord.utils.utcnow(),
    )
    if counts is not None:
        active = sorted(((i, a) for i, a in counts.items() if a.count), key=lambda x: (-x[1].count, x[0].lower()))
        quiet = sorted((i for i, a in counts.items() if not a.count), key=str.lower)
        total = sum(a.count for _, a in active)
        if active:
            lines: list[str] = []
            size = 0
            for rank, (island, act) in enumerate(active, 1):
                line = f"`{rank:>2}` 👑 **{_esc(island)}** · {_plural(act.count, 'reveal')} · last <t:{act.last}:R>"
                if size + len(line) + 1 > 980:  # field value limit is 1024
                    lines.append(f"… and {len(active) - rank + 1} more")
                    break
                lines.append(line)
                size += len(line) + 1
            embed.add_field(
                name=f"Most active · {label.lower()} · {_plural(total, 'reveal')}",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name=f"Most active · {label.lower()}",
                value="No sub island reveals yet. Try a longer window from the 🕒 menu.",
                inline=False,
            )
        if quiet:
            value = ", ".join(_esc(i) for i in quiet)
            embed.add_field(
                name=f"Quiet · no reveals ({len(quiet)})",
                value=value if len(value) <= 1024 else value[:1020] + "…",
                inline=False,
            )
    if viewer_id:
        embed.add_field(name="​", value=f"_Refreshed by <@{viewer_id}>_", inline=False)
    embed.set_footer(text="Staff only • Results update this message for everyone • Chopaeng Camp™")
    return embed


def build_intro_embed() -> discord.Embed:
    """Overview without activity counts (used when the database can't be read)."""
    return build_overview_embed()


def build_results_embed(island: str, window: str, page: int, data: RevealPage, viewer_id: int | None) -> discord.Embed:
    label = _window_label(window)
    pages = total_pages(data.total)
    page = min(max(page, 0), pages - 1)
    lines: list[str] = []
    if data.total:
        sources = "  ".join(
            f"{SOURCE_LABELS.get(key, UNKNOWN_SOURCE)[0]} {data.by_source[key]}"
            for key in ("website", "discord", "")
            if data.by_source.get(key)
        )
        lines.append(
            f"**{_plural(data.total, 'reveal')}** by **{_plural(data.unique_members, 'member')}**"
            + (f"  ·  {sources}" if sources else "")
        )
        if data.repeaters:
            lines.append("🔁 Most reveals: " + " · ".join(f"{format_member(r)} ×{n}" for r, n in data.repeaters))
    else:
        lines.append(f"No Dodo reveals for **{_esc(island)}** in the {label.lower()}.")
        lines.append("Try a longer window from the 🕒 menu, or pick another island.")
    if viewer_id:
        lines.append(f"_Refreshed by <@{viewer_id}>_")

    body = "\n".join(lines)
    first = page * PAGE_SIZE + 1
    shown = 0
    if data.rows:
        entries: list[str] = []
        size = len(body) + 4
        for i, row in enumerate(data.rows):
            entry = format_row(row, first + i)
            if size + len(entry) + 2 > 4000:  # embed description limit is 4096
                break
            entries.append(entry)
            size += len(entry) + 2
        shown = len(entries)
        body += "\n​\n" + "\n\n".join(entries)

    embed = discord.Embed(
        title=f"👑 {island}",
        description=body,
        color=COLOR_PANEL if data.total else COLOR_EMPTY,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=f"Dodo reveal log · {label}")
    if data.total and shown:
        last = first + shown - 1
        embed.set_footer(text=f"{first}–{last} of {data.total} · Page {page + 1} of {pages} · {LEGEND}")
    else:
        embed.set_footer(text=LEGEND)
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
    """Sub island picker, with each island's activity in the window. Splits past 25 islands."""

    def __init__(
        self,
        window: str,
        part: int = 0,
        current: str | None = None,
        counts: dict[str, IslandActivity] | None = None,
    ):
        self.window = _window(window)
        self.part = part
        islands = panel_islands()
        chunk = islands[part * SELECT_MAX:(part + 1) * SELECT_MAX] or ["(no sub islands)"]
        multi = len(islands) > SELECT_MAX
        placeholder = "👑 Pick a sub island…" if not multi else f"👑 Sub islands {chunk[0][0]}–{chunk[-1][0]}…"
        label = _window_label(self.window).lower()
        options = []
        for name in chunk:
            act = (counts or {}).get(name)
            if act is None:
                desc = None
            elif act.count:
                desc = f"{_plural(act.count, 'reveal')} · {label}"
            else:
                desc = f"No reveals · {label}"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=name[:100],
                    description=desc,
                    emoji="👑" if (act is None or act.count) else "🏝️",
                    default=bool(current) and _clean(name) == _clean(current or ""),
                )
            )
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
    """Time window picker. Keeps the current island (or the overview)."""

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


_NAV = {
    "home": ("🏝️", "All islands"),
    "prev": ("◀", "Newer"),
    "next": ("▶", "Older"),
    "ref": ("🔄", "Refresh"),
}


class RevealNavButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dodolog:(?P<action>home|prev|next|ref):(?P<island>[^:]+):(?P<win>\w+):(?P<page>\d+)",
):
    """Overview, paging and refresh. Each button carries the island and page it leads to."""

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
                style=discord.ButtonStyle.primary if action == "ref" else discord.ButtonStyle.secondary,
                disabled=disabled,
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /):
        return cls(match["action"], _uq(match["island"]), match["win"], int(match["page"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await render(interaction, None if self.action == "home" else self.island, self.window, self.page)


def build_view(
    island: str | None,
    window: str = DEFAULT_WINDOW,
    page: int = 0,
    total: int = 0,
    counts: dict[str, IslandActivity] | None = None,
) -> discord.ui.View:
    """Island menu(s), time window menu, then All islands / Newer / Older / Refresh."""
    view = discord.ui.View(timeout=None)
    parts = max(1, -(-len(panel_islands()) // SELECT_MAX))
    parts = min(parts, 3)  # leave rows for the window menu and buttons (5 rows max)
    for part in range(parts):
        view.add_item(RevealIslandSelect(window, part, island, counts))
    view.add_item(RevealWindowSelect(island, window, row=parts))
    pages = total_pages(total)
    nav = parts + 1
    if island:
        view.add_item(RevealNavButton("home", island, window, 0, row=nav))
        view.add_item(RevealNavButton("prev", island, window, page - 1, disabled=page <= 0, row=nav))
        view.add_item(RevealNavButton("next", island, window, page + 1, disabled=page >= pages - 1, row=nav))
    view.add_item(RevealNavButton("ref", island, window, page, row=nav))
    return view


async def build_state(
    island: str | None, window: str = DEFAULT_WINDOW, page: int = 0, viewer_id: int | None = None
) -> tuple[discord.Embed, discord.ui.View, int]:
    """Load data and build the panel for a state. Returns the page actually shown (lists can shrink)."""
    window = _window(window)
    since = int(time.time()) - WINDOWS[window][1]
    counts = await asyncio.to_thread(fetch_island_counts, since)
    if not island:
        return build_overview_embed(window, counts, viewer_id), build_view(None, window, counts=counts), 0
    data = await asyncio.to_thread(fetch_reveals, island, since, page)
    if data.total and page >= total_pages(data.total):  # list shrank since the button was drawn
        page = total_pages(data.total) - 1
        data = await asyncio.to_thread(fetch_reveals, island, since, page)
    embed = build_results_embed(island, window, page, data, viewer_id)
    return embed, build_view(island, window, page, data.total, counts), page


async def build_panel_message(window: str = DEFAULT_WINDOW) -> tuple[discord.Embed, discord.ui.View]:
    """The overview message used when deploying the panel (falls back to no counts if the DB fails)."""
    try:
        embed, view, _ = await build_state(None, window)
        return embed, view
    except Exception as exc:
        logger.warning(f"[REVEALLOG] Could not load island activity: {exc}")
        return build_overview_embed(window), build_view(None, window)


async def render(interaction: discord.Interaction, island: str | None, window: str, page: int) -> None:
    """Staff check, then update the panel message in place for everyone."""
    if not can_use_panel(interaction.user):
        await interaction.response.send_message("Only staff can use the Dodo reveal log.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        embed, view, page = await build_state(island, window, page, getattr(interaction.user, "id", None))
    except Exception as exc:
        logger.error(f"[REVEALLOG] Could not load reveals for {island or 'overview'}: {exc}")
        await interaction.followup.send(
            "Couldn't load the reveal log right now. Try again in a moment, or check the bot logs.", ephemeral=True
        )
        return
    await interaction.edit_original_response(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
    logger.info(f"[REVEALLOG] {interaction.user} viewed {island or 'overview'} ({_window(window)}, page {page + 1})")


# ---------------------------------------------------------------------------
# Sticky: keep the panel as the newest message in its channel
# ---------------------------------------------------------------------------

STICKY_DELAY_SECONDS = 5.0       # wait for the channel to go quiet this long before reposting
STICKY_MAX_WAIT_SECONDS = 30.0   # ...but never hold a repost back longer than this in busy channels

_STATE_RE = re.compile(r"dodolog:ref:(?P<island>[^:]+):(?P<win>\w+):(?P<page>\d+)")


def panel_state_from_components(custom_ids: list[str]) -> tuple[str | None, str, int]:
    """Read (island, window, page) back from the panel's Refresh button, so a repost keeps the view."""
    for cid in custom_ids:
        match = _STATE_RE.fullmatch(cid or "")
        if match:
            return _uq(match["island"]), _window(match["win"]), int(match["page"])
    return None, DEFAULT_WINDOW, 0


def _custom_ids(message: discord.Message) -> list[str]:
    ids: list[str] = []
    for row in message.components:
        for child in getattr(row, "children", []) or []:
            if getattr(child, "custom_id", None):
                ids.append(child.custom_id)
    return ids


def is_panel_message(message: discord.Message) -> bool:
    return any(cid.startswith("dodolog:") for cid in _custom_ids(message))


def _init_panel_table() -> None:
    from utils.database import connect_db

    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dodo_reveal_panels (
                channel_id VARCHAR(32) PRIMARY KEY,
                message_id VARCHAR(32) NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """
        )
        conn.commit()


def load_panels() -> dict[int, int]:
    """channel_id -> message_id of every deployed reveal log panel."""
    from utils.database import connect_db

    _init_panel_table()
    with connect_db() as conn:
        rows = conn.execute("SELECT channel_id, message_id FROM dodo_reveal_panels").fetchall()
    panels: dict[int, int] = {}
    for r in rows:
        try:
            panels[int(r["channel_id"])] = int(r["message_id"])
        except (TypeError, ValueError):
            continue
    return panels


def save_panel(channel_id: int, message_id: int) -> None:
    from utils.database import connect_db

    _init_panel_table()
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO dodo_reveal_panels (channel_id, message_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id, updated_at=excluded.updated_at
            """,
            (str(channel_id), str(message_id), int(time.time())),
        )
        conn.commit()


def forget_panel(channel_id: int) -> None:
    from utils.database import connect_db

    _init_panel_table()
    with connect_db() as conn:
        conn.execute("DELETE FROM dodo_reveal_panels WHERE channel_id = ?", (str(channel_id),))
        conn.commit()


class DodoRevealPanelCog(commands.Cog):
    """Deploys the reveal log panel and keeps it at the bottom of its channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panels: dict[int, int] = {}                   # channel_id -> panel message_id
        self._locks: dict[int, asyncio.Lock] = {}           # serialises deploy vs. repost per channel
        self._bump_tasks: dict[int, asyncio.Task] = {}      # pending debounced reposts
        self._bump_first_seen: dict[int, float] = {}        # when the pending repost was first requested

    async def cog_load(self) -> None:
        try:
            self._panels = await asyncio.to_thread(load_panels)
        except Exception as exc:
            logger.warning(f"[REVEALLOG] Could not load panel locations: {exc}")

    async def cog_unload(self) -> None:
        for task in self._bump_tasks.values():
            task.cancel()

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        return self._locks.setdefault(channel_id, asyncio.Lock())

    async def deploy(self, channel: discord.abc.Messageable) -> discord.Message:
        """Post a fresh overview panel and remove the channel's previous one."""
        async with self._lock_for(channel.id):
            embed, view = await build_panel_message()
            message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            old_id = self._panels.get(channel.id)
            self._panels[channel.id] = message.id
            await asyncio.to_thread(save_panel, channel.id, message.id)
            if old_id and old_id != message.id:
                with contextlib.suppress(discord.HTTPException):
                    await channel.get_partial_message(old_id).delete()
            return message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """A new message buries the panel: queue a repost to the bottom."""
        panel_id = self._panels.get(message.channel.id)
        if panel_id is None or message.id == panel_id:
            return
        if self.bot.user and message.author.id == self.bot.user.id and is_panel_message(message):
            return  # our own freshly posted panel
        self._schedule_bump(message.channel.id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Someone deleted the panel by hand: stop keeping it sticky (redeploy with /revealpanel)."""
        if self._panels.get(payload.channel_id) == payload.message_id:
            self._panels.pop(payload.channel_id, None)
            pending = self._bump_tasks.pop(payload.channel_id, None)
            if pending:
                pending.cancel()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(forget_panel, payload.channel_id)
            logger.info(f"[REVEALLOG] Panel in channel {payload.channel_id} was deleted; no longer sticky")

    def _schedule_bump(self, channel_id: int) -> None:
        """Debounce: wait for STICKY_DELAY_SECONDS of quiet, capped at STICKY_MAX_WAIT_SECONDS."""
        now = time.monotonic()
        pending = self._bump_tasks.get(channel_id)
        if pending and not pending.done():
            first = self._bump_first_seen.get(channel_id, now)
            if now - first >= STICKY_MAX_WAIT_SECONDS - STICKY_DELAY_SECONDS:
                return  # busy channel: let the pending repost fire instead of postponing it again
            pending.cancel()
        else:
            self._bump_first_seen[channel_id] = now
        self._bump_tasks[channel_id] = asyncio.create_task(self._bump_after_delay(channel_id))

    async def _bump_after_delay(self, channel_id: int) -> None:
        try:
            await asyncio.sleep(STICKY_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await self._bump(channel_id)
        except Exception as exc:
            logger.warning(f"[REVEALLOG] Sticky repost failed in channel {channel_id}: {exc}", exc_info=exc)
        finally:
            if self._bump_tasks.get(channel_id) is asyncio.current_task():
                self._bump_tasks.pop(channel_id, None)
                self._bump_first_seen.pop(channel_id, None)

    async def _bump(self, channel_id: int) -> None:
        """Repost the panel as the newest message (same island / window / page), then delete the old one.

        Send-then-delete, so the channel is never without a panel.
        """
        async with self._lock_for(channel_id):
            old_id = self._panels.get(channel_id)
            channel = self.bot.get_channel(channel_id)
            if not old_id or channel is None or not hasattr(channel, "history"):
                return
            async for last in channel.history(limit=1):
                if last.id == old_id:
                    return  # already at the bottom

            island, window, page = None, DEFAULT_WINDOW, 0
            with contextlib.suppress(discord.HTTPException):
                old = await channel.fetch_message(old_id)
                island, window, page = panel_state_from_components(_custom_ids(old))
            try:
                embed, view, _ = await build_state(island, window, page)
            except Exception as exc:
                logger.warning(f"[REVEALLOG] Repost falling back to overview in {channel_id}: {exc}")
                embed, view = build_overview_embed(window), build_view(None, window)

            new = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            self._panels[channel_id] = new.id
            await asyncio.to_thread(save_panel, channel_id, new.id)
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(old_id).delete()


async def setup_reveal_panel(bot: commands.Bot) -> None:
    """Persistent components plus the sticky cog. Call from setup_hook."""
    bot.add_dynamic_items(RevealIslandSelect, RevealWindowSelect, RevealNavButton)
    if bot.get_cog("DodoRevealPanelCog") is None:
        await bot.add_cog(DodoRevealPanelCog(bot))
