"""Filter menu shared by the DropBot and OrderBot catalogs: category, colour, season and language."""

from __future__ import annotations

import asyncio
import dataclasses
import discord
import logging
from collections import Counter
from typing import Awaitable, Callable

from utils import catalog_filters as cf
from utils.acnh_catalog import CatalogItem
from utils.catalog_filters import CatalogFilters

from .constants import PANEL_EMBED_COLOR, PANEL_FOOTER_ICON, PANEL_FOOTER_TEXT
from .ui_base import _OwnedView

logger = logging.getLogger("DropBotCog")

# Called when the member is done: (interaction, chosen filters, chosen language code).
FiltersDone = Callable[[discord.Interaction, CatalogFilters, str], Awaitable[None]]


class CatalogFilterView(_OwnedView):
    """
    Row 0: category · Row 1: colour · Row 2: season/event · Row 3: language · Row 4: Show results, Clear.

    Each menu only lists choices that still have results with the other filters applied, with counts,
    so a member can't filter themselves into an empty list. The language is saved as soon as it is
    picked; the filters are handed to `on_done` when "Show results" is pressed.
    """

    expired_notice = "⌛ This filter menu expired. Open the catalog again to continue."

    def __init__(
        self,
        user_id: int,
        items: list[CatalogItem],
        filters: CatalogFilters,
        language: str,
        on_done: FiltersDone,
        *,
        timeout: float = 180,
    ):
        super().__init__(user_id, timeout=timeout)
        self.items = list(items)
        self.filters = dataclasses.replace(filters)
        self.language = language
        self.on_done = on_done
        self._build()

    # ── components ──
    def _matching(self, **overrides: str | None) -> list[CatalogItem]:
        return dataclasses.replace(self.filters, **overrides).apply(self.items)

    def _add_select(self, row: int, placeholder: str, options: list[discord.SelectOption], handler) -> None:
        if len(options) < 2:
            return   # nothing to choose between
        select = discord.ui.Select(placeholder=placeholder[:100], options=options[:25], row=row)
        select.callback = handler   # type: ignore[method-assign]
        self.add_item(select)

    def _build(self) -> None:
        self.clear_items()
        f = self.filters

        # Category: counted with the colour and season filters applied.
        pool = self._matching(category=None)
        counts = Counter(i.category for i in pool if i.category)
        options = [discord.SelectOption(label=f"All categories ({len(pool)})", value="", emoji="🗂️", default=f.category is None)]
        for category, n in counts.most_common(24):
            options.append(discord.SelectOption(label=str(category)[:100], description=f"{n} item(s)", value=str(category)[:100], default=category == f.category))
        self._add_select(0, "🗂️ Category…", options, self._on_category)

        pool = self._matching(color=None)
        options = [discord.SelectOption(label="Any colour", value="", emoji="🎨", default=f.color is None)]
        for color in cf.available_colors(pool):
            n = sum(1 for i in pool if color in cf.item_colors(i))
            options.append(discord.SelectOption(label=color, description=f"{n} item(s)", value=color, emoji=cf.COLOR_EMOJI.get(color), default=color == f.color))
        self._add_select(1, "🎨 Colour…", options, self._on_color)

        pool = self._matching(season=None)
        options = [discord.SelectOption(label="Any season", value="", emoji="📅", default=f.season is None)]
        for season in cf.available_seasons(pool):
            n = sum(1 for i in pool if season in cf.item_seasons(i))
            options.append(discord.SelectOption(label=season, description=f"{n} item(s)", value=season, emoji=cf.SEASON_EMOJI.get(season), default=season == f.season))
        self._add_select(2, "📅 Season or event…", options, self._on_season)

        options = [
            discord.SelectOption(label=label, value=code, emoji=flag, default=code == self.language)
            for code, label, flag in cf.LANGUAGES
        ]
        self._add_select(3, "🌐 Item name language…", options, self._on_language)

        shown = len(f.apply(self.items))
        done = discord.ui.Button(label=f"Show results ({shown})", emoji="✅", style=discord.ButtonStyle.success, row=4)
        done.callback = self._on_done   # type: ignore[method-assign]
        self.add_item(done)
        clear = discord.ui.Button(label="Clear filters", emoji="🧹", style=discord.ButtonStyle.secondary, disabled=not f.active, row=4)
        clear.callback = self._on_clear   # type: ignore[method-assign]
        self.add_item(clear)

    def embed(self) -> discord.Embed:
        shown = len(self.filters.apply(self.items))
        embed = discord.Embed(
            title="🎛️ Catalog filters",
            description=(
                f"**{shown}** of **{len(self.items)}** results match.\n"
                "Narrow the list by category, colour or season, and choose the language item names are shown in."
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.add_field(name="Filters", value=self.filters.describe() or "None", inline=True)
        embed.add_field(name="Language", value=cf.LANGUAGE_LABELS.get(self.language, self.language), inline=True)
        if not cf.available_colors(self.items):
            embed.add_field(name="ℹ️", value="These results have no colour data (DIY recipes, for example).", inline=False)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed

    # ── handlers ──
    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @staticmethod
    def _value(interaction: discord.Interaction) -> str | None:
        return ((interaction.data or {}).get("values") or [""])[0] or None

    async def _on_category(self, interaction: discord.Interaction) -> None:
        self.filters.category = self._value(interaction)
        await self._refresh(interaction)

    async def _on_color(self, interaction: discord.Interaction) -> None:
        self.filters.color = self._value(interaction)
        await self._refresh(interaction)

    async def _on_season(self, interaction: discord.Interaction) -> None:
        self.filters.season = self._value(interaction)
        await self._refresh(interaction)

    async def _on_language(self, interaction: discord.Interaction) -> None:
        chosen = self._value(interaction) or cf.DEFAULT_LANGUAGE
        self.language = await asyncio.to_thread(cf.set_user_language, self.user_id, chosen)
        await self._refresh(interaction)

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        self.filters = CatalogFilters()
        await self._refresh(interaction)

    async def _on_done(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.on_done(interaction, self.filters, self.language)
