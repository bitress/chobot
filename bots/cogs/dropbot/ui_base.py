"""Shared view plumbing: the owner-only base view, ephemeral reply helpers."""

from __future__ import annotations

import contextlib
import discord
import logging
from typing import Any
from utils.config import Config

logger = logging.getLogger("DropBotCog")


class SubRulesPromptView(discord.ui.View):
    """Link button directly to the #sub-rules channel."""

    def __init__(self):
        super().__init__(timeout=None)
        guild_id = getattr(Config, "GUILD_ID", 729590421478703135) or 729590421478703135
        channel_id = getattr(Config, "SUB_RULES_CHANNEL_ID", 783677194576330792) or 783677194576330792
        self.add_item(
            discord.ui.Button(
                label="Go to #sub-rules",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/{guild_id}/{channel_id}",
                emoji="📜",
            )
        )


def _adopt(view: discord.ui.View, interaction: discord.Interaction) -> discord.ui.View:
    """Remember which interaction owns `view`'s message so it can show an expiry notice on timeout."""
    if isinstance(view, _OwnedView) and view._orig_interaction is None:
        view._orig_interaction = interaction
    return view


async def _reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    """Ephemeral reply that works before AND after interaction.response was used/deferred."""
    kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
        if isinstance(view, _OwnedView) and view._orig_interaction is None:
            view._orig_interaction = interaction   # lets the view expire cleanly even if never clicked
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except discord.HTTPException as exc:
        logger.warning("[DropBot] Could not send reply: %s", exc)


async def _send(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    content: str | None = None,
    ephemeral: bool = True,
) -> None:
    """Convenience helper for ephemeral responses that stores orig_interaction on OwnedViews."""
    if view and isinstance(view, _OwnedView):
        view._orig_interaction = interaction
    await _reply(interaction, content, embed=embed, view=view, ephemeral=ephemeral)


class _OwnedView(discord.ui.View):
    """
    Base for every ephemeral menu: owner-only interactions, one shared error reply,
    and a clean "expired" state (controls disabled, notice shown) when the view times out.
    """

    not_owner_msg = "This menu isn't yours."
    expired_notice = "⌛ This menu expired. Press the button on the panel to open it again."

    def __init__(self, user_id: int, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.message: discord.Message | None = None
        self._orig_interaction: discord.Interaction | None = None
        self._interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message(self.not_owner_msg, ephemeral=True)
            return False
        self._interaction = interaction
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[DropBot] %s error: %s", type(self).__name__, error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")

    def disable_all(self) -> None:
        for c in self.children:
            if hasattr(c, "item") and hasattr(c.item, "disabled"):
                c.item.disabled = True
            elif hasattr(c, "disabled"):
                c.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        target = self._interaction or self._orig_interaction
        if target:
            editor = target.edit_original_response
        elif self.message:
            editor = self.message.edit
        else:
            return
        with contextlib.suppress(Exception):
            await editor(content=self.expired_notice, view=self)
