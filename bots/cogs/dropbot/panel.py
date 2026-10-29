"""Persistent island panel: DynamicItem buttons and the panel view."""

from __future__ import annotations

import asyncio
import discord
import logging
import re
import urllib.parse
from typing import Any

from .constants import DODO_GIF, PANEL_EMBED_COLOR, PANEL_EMBED_COLOR_WARN, PANEL_FOOTER_ICON, PANEL_FOOTER_TEXT, SUB_RULES_MSG, UNKNOWN_ISLAND_MSG
from .state import _cooldown_left, _pop_cooldown, _spawn
from .islands import _access_denied, _canonical_island, _interactive_map_url, _island_url
from .status import _build_guide_embed, _build_panel_embed, _err_embed, _find_dodo, _fire_dropbot_dodo_webhook, build_drop_picker_embed, build_villager_picker_embed, fetch_island_live_status
from .ui_base import SubRulesPromptView, _reply, _send
from .actions import _resolve_client
from .views_villager import InteractiveVillagerPickerView
from .views_catalog import CatalogSearchModal, InteractiveDropPickerView

logger = logging.getLogger("DropBotCog")


# ─── Persistent panel buttons (DynamicItem) ────────────────────────────────────

def _cid(action: str, island: str) -> str:
    """custom_id = dropbot:<action>:<url-quoted island>. Stays under Discord's 100-character limit."""
    return f"dropbot:{action}:{urllib.parse.quote(island, safe='')}"


class _IslandButton:
    """
    Shared behaviour for panel buttons. Concrete classes only set the `_btn_*` attributes, pass
    their own `template=` (DynamicItem needs it per class) and implement callback().
    Attribute names are prefixed so they can't shadow discord.ui.Item properties like `row`/`label`.
    """

    _btn_action: str
    _btn_label: str
    _btn_emoji: str
    _btn_style: discord.ButtonStyle = discord.ButtonStyle.secondary
    _btn_row: int = 0

    def __init__(self, island: str):
        super().__init__(  # type: ignore[call-arg]
            discord.ui.Button(
                label=self._btn_label,
                emoji=self._btn_emoji,
                style=self._btn_style,
                custom_id=_cid(self._btn_action, island),
                row=self._btn_row,
            )
        )
        self.island = island

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(urllib.parse.unquote(match["island"]))

    async def _gate(self, interaction: discord.Interaction, *, require_order_rules: bool = False) -> str | None:
        """Canonical island if the click may proceed; otherwise replies with the reason and returns None."""
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return None
        island = _canonical_island(self.island)
        if not island:
            await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
            return None
        return island

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("[DropBot] Button error island=%s: %s", getattr(self, "island", "?"), error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again in a moment.")


_IN_FLIGHT_DODO_LOOKUPS: dict[str, asyncio.Future[str | None]] = {}


async def _find_dodo_coalesced(island: str, client: Any) -> str | None:
    """Coalesce concurrent Dodo lookups for the same island into a single in-flight task."""
    loop = asyncio.get_running_loop()
    if island in _IN_FLIGHT_DODO_LOOKUPS:
        fut = _IN_FLIGHT_DODO_LOOKUPS[island]
        return await fut

    fut = loop.create_future()
    _IN_FLIGHT_DODO_LOOKUPS[island] = fut
    try:
        res = await _find_dodo(island, client)
        if not fut.done():
            fut.set_result(res)
        return res
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _IN_FLIGHT_DODO_LOOKUPS.pop(island, None)


class GetDodoButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:getdodo:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "getdodo", "Get Dodo", "🦤"
    _btn_style = discord.ButtonStyle.success

    async def callback(self, interaction: discord.Interaction) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        cd = _cooldown_left(interaction.user.id, self.island, "dodo", 5.0)
        if cd > 0:
            await _reply(interaction, embed=_err_embed(f"⏳ Please wait **{cd:.1f}s** before requesting the Dodo code again."))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        resolved = await _resolve_client(interaction, self.island)
        if not resolved:
            _pop_cooldown(interaction.user.id, self.island, "dodo")
            return
        island, client = resolved

        dodo = await _find_dodo_coalesced(island, client)

        if not dodo:
            _pop_cooldown(interaction.user.id, self.island, "dodo")
            embed = discord.Embed(
                title="🔄 Dodo code unavailable",
                description=(
                    f"The Dodo code for **{island}** is refreshing, or the bot is offline.\n"
                    "Please try again in a moment."
                ),
                color=PANEL_EMBED_COLOR_WARN,
            )
            embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🦤 Dodo Code — {island}",
            url=_island_url(island),
            description=(
                f"# `{dodo}`\n\n"
                f"**[Visit {island} Page]({_island_url(island)})**\n\n"
                "-# ⚠️ Private code • Walk to airport to leave • Character | Island nick • Don't litter"
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.set_image(url=DODO_GIF)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Audit webhook — background task (strong reference held in _BACKGROUND_TASKS)
        _spawn(
            _fire_dropbot_dodo_webhook(
                str(interaction.user.id),
                interaction.user.name,
                interaction.user.display_name,
                island,
                str(interaction.channel_id or ""),
            )
        )

        # FlightLogger integration
        flight_cog = interaction.client.get_cog("FlightLoggerCog")
        if flight_cog and hasattr(flight_cog, "register_dodo_request"):
            try:
                guild_icon = interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None
                flight_cog.register_dodo_request(
                    interaction.user.id,
                    interaction.user,
                    interaction.channel,
                    None,
                    guild_icon,
                )
            except Exception as exc:
                logger.debug("[DropBot] flight_cog note: %s", exc)


class DropItemsButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:drop:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "drop", "Drop Items", "📦"
    _btn_style = discord.ButtonStyle.primary

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if island := await self._gate(interaction):
                embed = await build_drop_picker_embed(island, user=interaction.user)
                view = InteractiveDropPickerView(island_name=island, user_id=interaction.user.id)
                await _send(interaction, embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            logger.error("[DropBot] DropItemsButton error: %s", exc, exc_info=exc)
            await _reply(interaction, "❌ Something went wrong opening the item drop picker.")


class SearchCatalogButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:search:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "search", "Search Catalog", "🔍"

    async def callback(self, interaction: discord.Interaction) -> None:
        if island := await self._gate(interaction):
            await interaction.response.send_modal(CatalogSearchModal(island_name=island))


class InjectVillagerButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:villager:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "villager", "Inject Villager", "🐱"

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if island := await self._gate(interaction):
                embed = build_villager_picker_embed(island)
                view = InteractiveVillagerPickerView(island_name=island, user_id=interaction.user.id)
                await _send(interaction, embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            logger.error("[DropBot] InjectVillagerButton error: %s", exc, exc_info=exc)
            await _reply(interaction, "❌ Something went wrong opening the villager picker.")


class GuideButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:guide:(?P<island>[^:]+)",
):
    _btn_action, _btn_label, _btn_emoji = "guide", "Guide", "📖"
    _btn_row = 1

    async def callback(self, interaction: discord.Interaction) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return
        island = _canonical_island(self.island)
        if not island:
            await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
            return
        await _reply(interaction, embed=_build_guide_embed(island))


class RefreshButton(
    _IslandButton,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dropbot:refresh:(?P<island>[^:]+)",
):
    """Legacy: panels posted before auto-refresh carry this button. It upgrades them to the current layout."""

    _btn_action, _btn_label, _btn_emoji = "refresh", "Refresh", "🔄"
    _btn_style = discord.ButtonStyle.grey

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()   # silent ack; the panel message is edited below

        resolved = await _resolve_client(interaction, self.island)
        if not resolved:
            return
        island, _ = resolved

        status_data = await fetch_island_live_status(island)
        embed = await _build_panel_embed(island, status_data)
        await interaction.edit_original_response(
            embed=embed,
            view=IslandPanelView(island, status_data),
        )


PANEL_ITEMS = (GetDodoButton, DropItemsButton, SearchCatalogButton, InjectVillagerButton, GuideButton)


ALL_REGISTERED_DYNAMIC_ITEMS = PANEL_ITEMS + (RefreshButton,)


class IslandPanelView(discord.ui.View):
    """
    Row 0: Get Dodo · Drop Items · Search Catalog · Inject Villager
    Row 1: Guide · Island Page (link)
    The action buttons are DynamicItems registered in cog_load(), so this view needs no per-message
    bot.add_view() and survives restarts. The link button needs no registration.
    """

    def __init__(self, island_name: str, status_data: dict | None = None):
        super().__init__(timeout=None)
        self.island_name = island_name
        is_running = True
        is_valid_dodo = True
        monitoring_outage = False
        if status_data:
            is_running = bool(status_data.get("is_running", True))
            monitoring_outage = bool(status_data.get("monitoring_outage", False))
            is_valid_dodo = bool(status_data.get("is_dodo_valid", True))

        for item_cls in PANEL_ITEMS:
            btn = item_cls(island_name)
            if item_cls is GetDodoButton:
                if not is_running and not monitoring_outage:
                    btn.item.disabled = True
                    btn.item.label = "Bot Offline"
                    btn.item.style = discord.ButtonStyle.secondary
                elif not is_valid_dodo and not monitoring_outage:
                    btn.item.disabled = True
                    btn.item.label = "Dodo not ready"
                    btn.item.style = discord.ButtonStyle.secondary
            self.add_item(btn)
        self.add_item(
            discord.ui.Button(
                label="Island Page",
                emoji="🌐",
                style=discord.ButtonStyle.link,
                url=_island_url(island_name),
                row=1,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Interactive Map",
                emoji="🗺️",
                style=discord.ButtonStyle.link,
                url=_interactive_map_url(island_name),
                row=1,
            )
        )
