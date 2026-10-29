"""The DropBot cog: panel refresh loop and slash commands."""

from __future__ import annotations

import asyncio
import contextlib
import discord
import logging
import time
from dataclasses import dataclass
from discord import app_commands
from discord.ext import commands, tasks
from typing import Any
from utils.acnh_catalog import ACNHCatalog
from utils.config import Config

from .constants import FORCE_EDIT_SECONDS, MAX_STACK, PANEL_EMBED_COLOR_WARN, PANEL_FOOTER_ICON, PANEL_FOOTER_TEXT, PLOT_MAX, PLOT_MIN, REFRESH_CONCURRENCY, SUB_RULES_MSG
from .state import _close_http
from .db import _get_panel_row, _init_tables, _load_all_panels, _load_sanrio_wizard_state, _panel_channel_ids_cached, _upsert_panel
from .islands import _access_denied, _canonical_island, _infer_island, _sub_category_id, _user_can_target
from .parsing import _get_villager_names, _match_villagers
from .status import _build_panel_embed, _embed_signature, _err_embed, build_drop_picker_embed, build_villager_picker_embed, fetch_island_live_status
from .ui_base import SubRulesPromptView, _adopt, _reply
from .actions import _perform_drop
from .views_villager import InteractiveVillagerPickerView, SanrioInjectionWizardView, VillagerChoiceView, _start_injection
from .views_catalog import CatalogSearchModal, InteractiveDropPickerView, _show_catalog
from .panel import ALL_REGISTERED_DYNAMIC_ITEMS, IslandPanelView

logger = logging.getLogger("DropBotCog")


# ─── DropBot Cog ──────────────────────────────────────────────────────────────

@dataclass
class _PanelSig:
    sig: str
    last_edit: float
    is_running: bool
    is_valid_dodo: bool


class DropBotCog(commands.Cog, name="DropBot"):
    """
    Manages persistent interactive island control panels across sub-island channels.
    No privileged intents required — operates entirely on Discord's Interaction API.
    Auto-refreshes panel messages in the background so users always see live status.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sigs: dict[str, _PanelSig] = {}   # channel_id -> _PanelSig
        self._dead_panels: set[str] = set()    # channel_ids whose panel message no longer exists

    async def cog_load(self) -> None:
        await asyncio.to_thread(_init_tables)
        await asyncio.to_thread(_get_villager_names)   # pre-warm cache
        # Telemetry worker is shared with other cogs; we start it but never stop it on unload.
        try:
            from utils.sysbot_telemetry import start_telemetry_worker, set_telemetry_discord_bot
            set_telemetry_discord_bot(self.bot)
            start_telemetry_worker()
            logger.info("[DropBot] Started SysBot telemetry worker and registered bot instance.")
        except Exception as exc:
            logger.warning("[DropBot] Could not start telemetry worker: %s", exc)

        # One registration covers every panel in every channel (the island is in the custom_id).
        self.bot.add_dynamic_items(*ALL_REGISTERED_DYNAMIC_ITEMS)
        self.auto_refresh_panels_loop.start()
        logger.info(
            "[DropBot] Registered %d persistent button types and started auto-refresh loop.",
            len(ALL_REGISTERED_DYNAMIC_ITEMS),
        )

    async def cog_unload(self) -> None:
        self.auto_refresh_panels_loop.cancel()
        self.bot.remove_dynamic_items(*ALL_REGISTERED_DYNAMIC_ITEMS)
        await _close_http()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await _reply(interaction, embed=_err_embed("You need the **Manage Messages** permission to use this command."))
            return
        logger.error("[DropBot] Command error: %s", error, exc_info=error)
        await _reply(interaction, embed=_err_embed("Something went wrong running that command."))

    # ── Auto-refresh ──────────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def auto_refresh_panels_loop(self) -> None:
        """Refresh all active panels in parallel; skip edits when nothing visible changed."""
        panels = await asyncio.to_thread(_load_all_panels)
        panels = [
            p for p in panels
            if p.get("channel_id") and p.get("message_id") and p.get("island_name")
            and str(p["channel_id"]) not in self._dead_panels
        ]
        if not panels:
            return
        sem = asyncio.Semaphore(REFRESH_CONCURRENCY)
        await asyncio.gather(*(self._refresh_one(p, sem) for p in panels), return_exceptions=True)

    async def _refresh_one(self, p: dict, sem: asyncio.Semaphore) -> None:
        cid = str(p["channel_id"])
        try:
            channel_id, message_id = int(cid), int(p["message_id"])
        except (TypeError, ValueError):
            return
        island = p["island_name"]

        async with sem:
            try:
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden):
                        self._dead_panels.add(cid)
                        logger.warning(
                            "[DropBot] Channel %s no longer accessible; skipping panel refreshes for %s.",
                            cid, island,
                        )
                        return
                    except Exception as exc:
                        logger.warning("[DropBot] Could not fetch channel %s: %s", cid, exc)
                        return

                status = await fetch_island_live_status(island)
                embed = await _build_panel_embed(island, status)
                sig = _embed_signature(embed)
                now = time.monotonic()
                prev = self._sigs.get(cid)

                is_running = bool(status.get("is_running", True)) if status else True
                is_valid_dodo = bool(status.get("is_dodo_valid", True)) if status else True
                dodo_changed = (
                    prev is None
                    or prev.is_running != is_running
                    or prev.is_valid_dodo != is_valid_dodo
                )

                if prev and prev.sig == sig and not dodo_changed and now - prev.last_edit < FORCE_EDIT_SECONDS:
                    return

                view_to_attach = IslandPanelView(island, status) if (prev is None or dodo_changed) else None

                # 1. Edit known message id
                if message_id:
                    partial = channel.get_partial_message(message_id)
                    kwargs: dict[str, Any] = {"embed": embed}
                    if view_to_attach is not None:
                        kwargs["view"] = view_to_attach
                    try:
                        await partial.edit(**kwargs)
                        self._sigs[cid] = _PanelSig(sig, now, is_running, is_valid_dodo)
                        return
                    except discord.NotFound:
                        logger.warning(
                            "[DropBot] Panel message %s in %s not found; attempting recovery...",
                            message_id, cid,
                        )
                    except discord.HTTPException as exc:
                        if exc.status in (403, 429):
                            logger.warning(
                                "[DropBot] HTTP %s updating panel for %s in %s: %s",
                                exc.status, island, cid, exc,
                            )
                        else:
                            logger.warning("[DropBot] HTTP error updating panel for %s: %s", island, exc)
                        return

                # 2. Recover from recent channel history
                if hasattr(channel, "history"):
                    try:
                        bot_id = getattr(self.bot.user, "id", None)
                        async for candidate in channel.history(limit=25):
                            if (
                                candidate.author.id == bot_id
                                and candidate.embeds
                                and island.lower() in (candidate.embeds[0].title or "").lower()
                            ):
                                await candidate.edit(embed=embed, view=IslandPanelView(island, status))
                                self._sigs[cid] = _PanelSig(sig, now, is_running, is_valid_dodo)
                                await asyncio.to_thread(_upsert_panel, channel_id, island, candidate.id)
                                logger.info("[DropBot] Recovered panel message %s in channel %s for %s", candidate.id, cid, island)
                                return
                    except Exception as exc:
                        logger.debug("[DropBot] Error scanning history in %s: %s", cid, exc)

                # 3. Auto-repost fresh panel
                try:
                    msg = await channel.send(embed=embed, view=IslandPanelView(island, status))
                    self._sigs[cid] = _PanelSig(sig, now, is_running, is_valid_dodo)
                    await asyncio.to_thread(_upsert_panel, channel_id, island, msg.id)
                    logger.info("[DropBot] Auto-reposted panel message %s in channel %s for %s", msg.id, cid, island)
                except Exception as exc:
                    logger.warning("[DropBot] Failed to auto-repost panel in channel %s: %s", cid, exc)

            except Exception as exc:
                logger.warning("[DropBot] Unexpected error auto-refreshing panel for %s: %s", island, exc, exc_info=exc)

    @auto_refresh_panels_loop.before_loop
    async def before_auto_refresh_panels_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── Shared: post a panel in a channel ─────────────────────────────────────

    async def _post_panel(self, channel: discord.TextChannel, island: str) -> discord.Message:
        existing = await asyncio.to_thread(_get_panel_row, channel.id)
        if existing and existing.get("message_id"):
            with contextlib.suppress(Exception):
                await (await channel.fetch_message(int(existing["message_id"]))).delete()

        status_data = await fetch_island_live_status(island)
        embed = await _build_panel_embed(island, status_data)
        panel_msg = await channel.send(embed=embed, view=IslandPanelView(island_name=island, status_data=status_data))
        await asyncio.to_thread(_upsert_panel, channel.id, island, panel_msg.id)
        self._dead_panels.discard(str(channel.id))
        self._sigs.pop(str(channel.id), None)
        return panel_msg

    # ── Slash command group ───────────────────────────────────────────────────

    dropbot_group = app_commands.Group(
        name="dropbot",
        description="Island DropBot control panels and catalog search",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    async def island_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = (current or "").strip().lower()
        islands = getattr(Config, "SUB_ISLANDS", []) or []
        user = interaction.user
        is_admin = False
        if isinstance(user, discord.Member):
            perms = user.guild_permissions
            is_admin = bool(perms.administrator or perms.manage_guild or perms.manage_messages)
        elif interaction.permissions and interaction.permissions.manage_messages:
            is_admin = True

        if is_admin:
            visible_islands = list(islands)
        else:
            current_island = await _infer_island(interaction.channel)
            guild, member = interaction.guild, interaction.user

            async def _check_island(isl: str) -> bool:
                if isl == current_island:
                    return True
                if not guild or not isinstance(member, discord.Member):
                    return False
                cids = await asyncio.to_thread(_panel_channel_ids_cached, isl)
                for cid in cids:
                    ch = guild.get_channel(cid)
                    if ch and ch.permissions_for(member).view_channel:
                        return True
                return False

            checks = await asyncio.gather(*[_check_island(i) for i in islands])
            visible_islands = [i for i, ok in zip(islands, checks) if ok]

        if not cur:
            return [app_commands.Choice(name=i, value=i) for i in visible_islands[:25]]
        return [app_commands.Choice(name=i, value=i) for i in visible_islands if cur in i.lower()][:25]

    async def item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Real-time autocomplete for orderable item drops."""
        cur = (current or "").strip()
        if not cur:
            try:
                catalog = ACNHCatalog.get_instance()
                popular = catalog.get_popular_item_names()
                return [app_commands.Choice(name=f"📦 {p}", value=p) for p in popular[:25]]
            except Exception:
                return []

        try:
            catalog = ACNHCatalog.get_instance()
            hits = await asyncio.to_thread(lambda: catalog.search_items(cur, limit=25))
            choices: list[app_commands.Choice[str]] = []
            seen: set[str] = set()
            for it in hits:
                name = it.display_name[:100]
                if name not in seen:
                    seen.add(name)
                    cat = f" ({it.category})" if it.category else ""
                    emoji = "📜" if it.diy else "📦"
                    label = f"{emoji} {name}{cat}"[:100]
                    choices.append(app_commands.Choice(name=label, value=name))
            return choices[:25]
        except Exception as exc:
            logger.warning("[DropBot] item_autocomplete error: %s", exc)
            return []

    async def villager_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Real-time autocomplete for villager injection."""
        cur = (current or "").strip()
        if not cur:
            try:
                catalog = ACNHCatalog.get_instance()
                popular_v = catalog.get_popular_villagers()
                return [app_commands.Choice(name=f"{v.emoji} {v.name}", value=v.name) for v in popular_v[:25]]
            except Exception:
                return []

        try:
            catalog = ACNHCatalog.get_instance()
            hits = await asyncio.to_thread(lambda: catalog.search_villagers(cur, limit=25))
            choices: list[app_commands.Choice[str]] = []
            seen: set[str] = set()
            for v in hits:
                name = v.display_name[:100]
                if name not in seen:
                    seen.add(name)
                    details = f" • {v.species}" if v.species else ""
                    label = f"🐱 {name}{details}"[:100]
                    choices.append(app_commands.Choice(name=label, value=name))
            return choices[:25]
        except Exception as exc:
            logger.warning("[DropBot] villager_autocomplete error: %s", exc)
            return []

    async def catalog_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete choices for item and villager catalog search."""
        if not current or len(current) < 2:
            return []
        try:
            catalog = ACNHCatalog.get_instance()
            item_hits, villager_hits = await asyncio.to_thread(
                lambda: (catalog.search_items(current, limit=18), catalog.search_villagers(current, limit=7))
            )

            choices: list[app_commands.Choice[str]] = []
            seen: set[str] = set()
            for item in item_hits:
                label = item.display_name[:100]
                if label not in seen:
                    seen.add(label)
                    choices.append(app_commands.Choice(name=f"📦 {label}"[:100], value=label))
            for v in villager_hits:
                label = v.display_name[:100]
                if label not in seen:
                    seen.add(label)
                    choices.append(app_commands.Choice(name=f"🐱 {label}"[:100], value=label))
            return choices[:25]
        except Exception as exc:
            logger.warning("[DropBot] catalog_autocomplete error: %s", exc)
            return []

    # ── /dropbot setup-panel ───────────────────────────────────────────────────

    @dropbot_group.command(name="setup-panel", description="Post or refresh the DropBot control panel in this channel")
    @app_commands.describe(island="Island name (auto-detected from channel name if omitted)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(island=island_autocomplete)
    async def setup_panel(self, interaction: discord.Interaction, island: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ Panels can only be posted in regular text channels.", ephemeral=True)
            return

        if island:
            canonical = _canonical_island(island)
            if not canonical:
                valid = ", ".join(Config.SUB_ISLANDS)
                if len(valid) > 1500:
                    valid = valid[:1500] + "…"
                await interaction.followup.send(
                    f"❌ **{island[:50]}** isn't a sub island.\nValid islands: {valid}", ephemeral=True
                )
                return
            island = canonical
        else:
            island = await _infer_island(channel)

        if not island:
            await interaction.followup.send(
                "❌ Could not auto-detect the island from this channel name.\n"
                "Pass it explicitly: `/dropbot setup-panel island:Hiraya`",
                ephemeral=True,
            )
            return

        panel_msg = await self._post_panel(channel, island)
        await interaction.followup.send(
            f"✅ DropBot panel for **{island}** posted in <#{channel.id}> (msg `{panel_msg.id}`).",
            ephemeral=True,
        )
        logger.info("[DropBot] setup-panel: island=%s channel=%s msg=%s by %s",
                    island, channel.id, panel_msg.id, interaction.user)

    # ── /dropbot setup-all ─────────────────────────────────────────────────────

    @dropbot_group.command(name="setup-all", description="Bulk-deploy DropBot panels to all sub-island channels")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def setup_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Must be used inside a server.", ephemeral=True)
            return

        category_id = _sub_category_id()
        if not category_id:
            await interaction.followup.send(
                "❌ No category is configured. Set `SUB_CATEGORY_ID` (or `CATEGORY_ID`) in `.env`.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(category_id)
        if not category:
            with contextlib.suppress(Exception):
                category = await guild.fetch_channel(category_id)

        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(f"❌ Could not find a category with ID `{category_id}`.", ephemeral=True)
            return

        results: list[str] = []
        for channel in category.text_channels:
            island = await _infer_island(channel)
            if not island:
                results.append(f"⏭️ `#{channel.name}` — no island match, skipped")
                continue
            try:
                await self._post_panel(channel, island)
                results.append(f"✅ `#{channel.name}` → **{island}**")
                logger.info("[DropBot] setup-all: deployed %s in #%s", island, channel.name)
            except discord.Forbidden:
                results.append(f"❌ `#{channel.name}` — missing permissions")
            except Exception as exc:
                results.append(f"❌ `#{channel.name}` — {exc}")

            await asyncio.sleep(0.6)   # avoid Discord rate-limits

        summary = "\n".join(results) or "No channels found in category."
        if len(summary) > 1900:
            summary = summary[:1900] + "\n…(truncated)"
        await interaction.followup.send(f"**DropBot Setup All:**\n{summary}", ephemeral=True)

    # ── /dropbot status ────────────────────────────────────────────────────────

    @dropbot_group.command(name="status", description="List all registered DropBot panels")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        panels = await asyncio.to_thread(_load_all_panels)
        if not panels:
            await interaction.followup.send(
                "No panels registered yet. Run `/dropbot setup-panel` or `/dropbot setup-all`.",
                ephemeral=True,
            )
            return

        lines = [
            f"• <#{p['channel_id']}> → **{p['island_name']}** (msg `{p.get('message_id') or '—'}`)"
            + (" ⚠️ message missing — rerun setup-panel" if str(p["channel_id"]) in self._dead_panels else "")
            for p in panels
        ]
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "\n…(truncated)"
        await interaction.followup.send(f"**Registered DropBot Panels ({len(panels)}):**\n{body}", ephemeral=True)

    # ── Command implementations ───────────────────────────────────────────────

    async def _handle_catalog_search(self, interaction: discord.Interaction, query: str, island: str | None) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
            if not await _user_can_target(interaction, target_island):
                await _reply(
                    interaction,
                    embed=_err_embed("You can only use an island you have access to. Use this in that island's channel."),
                )
                return
        else:
            target_island = await _infer_island(interaction.channel)

        query_str = query.strip()
        if query_str.startswith(("📦 ", "🐱 ")):
            query_str = query_str[2:].strip()

        if not query_str:
            await interaction.response.send_modal(CatalogSearchModal(island_name=target_island))
            return

        await _show_catalog(interaction, target_island, query_str)

    async def _handle_drop_cmd(
        self,
        interaction: discord.Interaction,
        item: str | None,
        quantity: int | None,
        island: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
        else:
            target_island = await _infer_island(interaction.channel)

        if not target_island:
            await _reply(
                interaction,
                embed=_err_embed("Please specify a target island or use this command inside a sub-island channel."),
            )
            return

        if not await _user_can_target(interaction, target_island):
            await _reply(
                interaction,
                embed=_err_embed(f"You don't have access to **{target_island}**. Use the command in that island's channel."),
            )
            return

        if not item or not item.strip():
            embed = await build_drop_picker_embed(target_island, user=interaction.user)
            view = InteractiveDropPickerView(island_name=target_island, user_id=interaction.user.id)
            await interaction.response.send_message(embed=embed, view=_adopt(view, interaction), ephemeral=True)
            return

        clean_item = item.strip()
        if clean_item.startswith(("📦 ", "📜 ")):
            clean_item = clean_item[2:].strip()
        if " (" in clean_item and clean_item.endswith(")"):
            clean_item = clean_item.split(" (")[0].strip()

        if quantity is None:
            try:
                catalog = ACNHCatalog.get_instance()
                it_obj = catalog.get_item(clean_item)
                quantity = min(int(it_obj.stack_size or 1), MAX_STACK) if it_obj else 1
            except Exception:
                quantity = 1

        quantity = max(1, min(quantity, MAX_STACK))

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, _ = await _perform_drop(
            interaction.user,
            target_island,
            [(clean_item, quantity)],
            heading="📦 Drop queued",
            contents=f"{clean_item} x{quantity}",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_inject_cmd(
        self,
        interaction: discord.Interaction,
        villager: str | None,
        plot: int | None,
        island: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
        else:
            target_island = await _infer_island(interaction.channel)

        if not target_island:
            await _reply(
                interaction,
                embed=_err_embed("Please specify a target island or use this command inside a sub-island channel."),
            )
            return

        if not await _user_can_target(interaction, target_island):
            await _reply(
                interaction,
                embed=_err_embed(f"You don't have access to **{target_island}**. Use the command in that island's channel."),
            )
            return

        if not villager or not villager.strip():
            embed = build_villager_picker_embed(target_island)
            view = InteractiveVillagerPickerView(island_name=target_island, user_id=interaction.user.id)
            await interaction.response.send_message(embed=embed, view=_adopt(view, interaction), ephemeral=True)
            return

        clean_v = villager.strip()
        if clean_v.startswith("🐱 "):
            clean_v = clean_v[2:].strip()
        if " • " in clean_v:
            clean_v = clean_v.split(" • ")[0].strip()

        await interaction.response.defer(ephemeral=True, thinking=True)
        exact, candidates = await asyncio.to_thread(_match_villagers, clean_v)

        if exact:
            await _start_injection(interaction, target_island, exact, plot)
            return

        if not candidates:
            await interaction.followup.send(
                embed=_err_embed(f"I couldn't find a villager called **{clean_v[:64]}**. Check the spelling and try again."),
                ephemeral=True,
            )
            return

        view = VillagerChoiceView(interaction.user.id, target_island, candidates, plot)
        prompt = discord.Embed(
            title="Did you mean…?",
            description=(
                f"I couldn't find **{clean_v[:64]}** exactly.\n"
                + (
                    f"Did you mean **{candidates[0]}**?"
                    if len(candidates) == 1
                    else "Pick the villager you meant from the list."
                )
            ),
            color=PANEL_EMBED_COLOR_WARN,
        )
        prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=prompt, view=_adopt(view, interaction), ephemeral=True)

    async def _handle_sanrio_cmd(
        self,
        interaction: discord.Interaction,
        island: str | None,
        villager: str | None,
    ) -> None:
        denied = await _access_denied(interaction.user)
        if denied:
            view = SubRulesPromptView() if denied == SUB_RULES_MSG else None
            await _reply(interaction, embed=_err_embed(denied), view=view)
            return

        if island:
            target_island = _canonical_island(island)
            if not target_island:
                await _reply(interaction, embed=_err_embed(f"**{island[:50]}** isn't a sub island."))
                return
        else:
            target_island = await _infer_island(interaction.channel)

        if not target_island:
            await _reply(
                interaction,
                embed=_err_embed("Please specify a target island or use this command inside a sub-island channel."),
            )
            return

        if not await _user_can_target(interaction, target_island):
            await _reply(
                interaction,
                embed=_err_embed(f"You don't have access to **{target_island}**. Use the command in that island's channel."),
            )
            return

        target_v = "Chai"
        if villager and villager.strip():
            clean_v = villager.strip()
            if clean_v.startswith("🐱 "):
                clean_v = clean_v[2:].strip()
            if " • " in clean_v:
                clean_v = clean_v.split(" • ")[0].strip()
            exact, _ = await asyncio.to_thread(_match_villagers, clean_v)
            target_v = exact or clean_v

        saved = await asyncio.to_thread(_load_sanrio_wizard_state, interaction.user.id, target_island)
        if saved and saved.get("step1_done") and (not villager or not villager.strip()):
            target_island = saved.get("island_name") or target_island
            target_v = saved.get("target_villager") or target_v
            placeholder_v = saved.get("placeholder_villager") or "Raymond"
            wizard = SanrioInjectionWizardView(
                island_name=target_island,
                user_id=interaction.user.id,
                target_villager=target_v,
                placeholder_villager=placeholder_v,
                step1_done=True,
            )
            wizard.last_status = "✈️ **Resumed Session (Step 2 Ready):** Pick up right where you left off!"
        else:
            wizard = SanrioInjectionWizardView(
                island_name=target_island,
                user_id=interaction.user.id,
                target_villager=target_v,
            )
        wizard._orig_interaction = interaction
        await interaction.response.send_message(embed=wizard.build_embed(), view=wizard, ephemeral=True)

    # ── Registered slash commands (/dropbot X and /X) ─────────────────────────

    @dropbot_group.command(name="catalog", description="Search the ACNH catalog — browse items, drop items, or inject villagers")
    @app_commands.describe(query="Item or villager name to search for", island="Target island (auto-detected from channel if omitted)")
    @app_commands.autocomplete(query=catalog_autocomplete, island=island_autocomplete)
    async def dropbot_catalog(self, interaction: discord.Interaction, query: str = "", island: str | None = None) -> None:
        await self._handle_catalog_search(interaction, query, island)

    @app_commands.command(name="catalog", description="Search the ACNH catalog — browse items, drop items, or inject villagers")
    @app_commands.describe(query="Item or villager name to search for", island="Target island (auto-detected from channel if omitted)")
    @app_commands.autocomplete(query=catalog_autocomplete, island=island_autocomplete)
    async def slash_catalog(self, interaction: discord.Interaction, query: str = "", island: str | None = None) -> None:
        await self._handle_catalog_search(interaction, query, island)

    @dropbot_group.command(name="drop", description="Drop items on a sub-island with live search autocomplete")
    @app_commands.describe(item="Item name to drop (search autocomplete)", quantity=f"Quantity (1–{MAX_STACK})", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(item=item_autocomplete, island=island_autocomplete)
    async def dropbot_drop(self, interaction: discord.Interaction, item: str | None = None, quantity: app_commands.Range[int, 1, MAX_STACK] | None = None, island: str | None = None) -> None:
        await self._handle_drop_cmd(interaction, item, quantity, island)

    @app_commands.command(name="drop", description="Drop items on a sub-island with live search autocomplete")
    @app_commands.describe(item="Item name to drop (search autocomplete)", quantity=f"Quantity (1–{MAX_STACK})", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(item=item_autocomplete, island=island_autocomplete)
    async def slash_drop(self, interaction: discord.Interaction, item: str | None = None, quantity: app_commands.Range[int, 1, MAX_STACK] | None = None, island: str | None = None) -> None:
        await self._handle_drop_cmd(interaction, item, quantity, island)

    @dropbot_group.command(name="inject", description="Inject a villager into a sub-island with live search autocomplete")
    @app_commands.describe(villager="Villager name to inject (search autocomplete)", plot=f"Plot number ({PLOT_MIN}–{PLOT_MAX}, optional)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def dropbot_inject(self, interaction: discord.Interaction, villager: str | None = None, plot: app_commands.Range[int, PLOT_MIN, PLOT_MAX] | None = None, island: str | None = None) -> None:
        await self._handle_inject_cmd(interaction, villager, plot, island)

    @app_commands.command(name="inject", description="Inject a villager into a sub-island with live search autocomplete")
    @app_commands.describe(villager="Villager name to inject (search autocomplete)", plot=f"Plot number ({PLOT_MIN}–{PLOT_MAX}, optional)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def slash_inject(self, interaction: discord.Interaction, villager: str | None = None, plot: app_commands.Range[int, PLOT_MIN, PLOT_MAX] | None = None, island: str | None = None) -> None:
        await self._handle_inject_cmd(interaction, villager, plot, island)

    @dropbot_group.command(name="sanrio", description="Interactive 2-step wizard to inject Sanrio or Amiibo villagers")
    @app_commands.describe(villager="Target Sanrio/Amiibo villager (e.g. Chai, Chelsea, Marty, Étoile)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def dropbot_sanrio(self, interaction: discord.Interaction, villager: str | None = None, island: str | None = None) -> None:
        await self._handle_sanrio_cmd(interaction, island, villager)

    @app_commands.command(name="sanrio", description="Interactive 2-step wizard to inject Sanrio or Amiibo villagers")
    @app_commands.describe(villager="Target Sanrio/Amiibo villager (e.g. Chai, Chelsea, Marty, Étoile)", island="Target island (auto-detected if omitted)")
    @app_commands.autocomplete(villager=villager_autocomplete, island=island_autocomplete)
    async def slash_sanrio(self, interaction: discord.Interaction, villager: str | None = None, island: str | None = None) -> None:
        await self._handle_sanrio_cmd(interaction, island, villager)
