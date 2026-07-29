from bots.cogs.cog_utils import _upsert_bot_status
import asyncio
import contextlib
import os
import subprocess
import time
import re
import random
import logging
from datetime import datetime, timezone, timedelta
from itertools import cycle

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from thefuzz import process, fuzz

from utils.config import Config
from utils.database import connect_db
from utils.helpers import normalize_text, get_best_suggestions, clean_text
from utils.island_access import configured_subscription_role_ids, is_mod, resolved_island_required_roles
from utils.nookipedia import NookipediaClient
from utils.nickname_format import is_valid_acnh_nickname, nickname_warning_for, NICKNAME_FORMAT_EXAMPLE
from utils.chopaeng_ai import get_ai_answer, conversation_store, add_chat_message
from utils.ops_status import create_sqlite_backup, get_maintenance_settings

from bots.cogs.shared_state import SharedCogState
from bots.cogs.cog_utils import *

logger = logging.getLogger("DiscordCommandBot")

class MonitorCog(commands.Cog):
    def __init__(self, bot, data_manager, shared_state: SharedCogState):
        self.bot = bot
        self.data_manager = data_manager
        self.shared_state = shared_state


    @tasks.loop(seconds=FREE_DODO_BOARD_INTERVAL_SECONDS)
    async def free_dodo_board_loop(self):
        """Keep the public free-island Dodo board updated."""
        channel_id = Config.FREE_DODO_BOARD_CHANNEL_ID
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                logger.warning(f"[DISCORD] Free Dodo board channel {channel_id} unavailable: {exc}")
                return

        if not isinstance(channel, discord.TextChannel):
            logger.warning(f"[DISCORD] Free Dodo board target {channel_id} is not a text channel.")
            return

        if not self.shared_state.free_dodo_board_startup_cleanup_done:
            await self._delete_existing_free_dodo_board_messages(channel)
            self.shared_state.free_dodo_board_startup_cleanup_done = True

        await self.fetch_free_islands()
        checked_at = datetime.now(timezone.utc)
        free_items, _ = await self._fetch_free_dodo_board_data()
        footer_icon_url = channel.guild.icon.url if channel.guild and channel.guild.icon else None
        embeds = [self._build_free_dodo_embed(item, checked_at, footer_icon_url) for item in free_items]
        if not embeds:
            embeds = [self._build_free_dodo_empty_embed(checked_at, footer_icon_url)]

        await self._publish_free_dodo_board(channel, embeds)

    @free_dodo_board_loop.before_loop
    async def before_free_dodo_board_loop(self):
        """Wait until ready before starting the public Free Dodo board."""
        await self.bot.wait_until_ready()
        await self.fetch_free_islands()

    @tasks.loop(seconds=60)
    async def island_status_sticky_loop(self):
        channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        if not self.shared_state.island_status_sticky_startup_cleanup_done:
            await self._delete_existing_island_status_sticky_messages(channel)
            self.shared_state.island_status_sticky_startup_cleanup_done = True

        try:
            await self._refresh_island_status_sticky_message(channel, force_repost=True)
        except Exception as exc:
            logger.error(f"[DISCORD] island_status_sticky_loop iteration failed: {exc}", exc_info=True)

    @island_status_sticky_loop.before_loop
    async def before_island_status_sticky_loop(self):
        """Wait until ready before starting the island status sticky loop."""
        await self.bot.wait_until_ready()

    @island_status_sticky_loop.error
    async def island_status_sticky_loop_error(self, error: Exception):
        """Safety net: log and restart the loop if it still somehow crashes."""
        logger.error(f"[DISCORD] island_status_sticky_loop crashed: {error}", exc_info=True)
        if not self.island_status_sticky_loop.is_running():
            self.island_status_sticky_loop.restart()

    @tasks.loop(seconds=300)
    async def island_monitor_loop(self):
        """Background task: detect island down/up transitions and notify in channel."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        if not self.shared_state.sub_island_lookup:
            try:
                await self.fetch_islands()
            except Exception as e:
                logger.error(f"[DISCORD] island_monitor_loop failed to fetch islands: {e}")
                return
        self._refresh_order_island_lookup()

        for island in Config.SUB_ISLANDS:
            island_clean = clean_text(island)
            channel_id = self.shared_state.sub_island_lookup.get(island_clean)
            if not channel_id:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            try:
                is_online = await self._check_island_online(guild, island)
            except Exception as e:
                logger.error(f"[DISCORD] island_monitor_loop error checking {island}: {e}")
                continue

            # Persist current status to the database so the REST API can expose it
            _upsert_bot_status(island.lower(), island, is_online)

            previous = self.shared_state.island_down_states.get(island_clean)  # None = first run

            if previous is None:
                # First run: always initialize as "not down" so that a "back up"
                # notification is only ever sent after we have sent a "Bot is Down"
                # embed in this session (i.e. never on a cold start when the island
                # is already online).
                self.shared_state.island_down_states[island_clean] = False
                continue

            was_down = previous  # True means it was down

            if not is_online and not was_down:
                # Transition: online → offline
                self.shared_state.island_down_states[island_clean] = True
                embed = discord.Embed(
                    title="🏝️ Island is Down",
                    description=f"**{island}** island is currently **offline**.",
                    color=discord.Color.red(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_image(url=ISLAND_DOWN_IMAGE_URL)
                try:
                    msg = await channel.send(embed=embed)
                    self.shared_state.island_down_messages[island_clean] = msg
                    logger.info(f"[DISCORD] Island monitor: {island} went OFFLINE")
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to send island-down embed for {island}: {e}")

                # DM subscribers about the outage
                await self._notify_island_subscribers(island_clean, island, online=False)

            elif is_online and was_down:
                # Transition: offline → online
                self.shared_state.island_down_states[island_clean] = False
                # Remove the sticky "island is down" embed
                sticky_msg = self.shared_state.island_down_messages.pop(island_clean, None)
                if sticky_msg:
                    try:
                        await sticky_msg.delete()
                    except discord.NotFound:
                        pass  # Already deleted externally — nothing to do
                    except Exception as e:
                        logger.warning(f"[DISCORD] Could not delete sticky down embed for {island}: {e}")
                embed = discord.Embed(
                    title="🏝️ Island is Back Up!",
                    description=f"**{island}** island is back online and ready to visit! 🎉",
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_image(url=Config.FOOTER_LINE)
                try:
                    await channel.send(embed=embed)
                    logger.info(f"[DISCORD] Island monitor: {island} is back ONLINE")
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to send island-back-up embed for {island}: {e}")

                # DM subscribers who opted in to alerts for this island
                await self._notify_island_subscribers(island_clean, island, online=True)

        # --- Free island status ---
        if self.shared_state.free_island_lookup:
            for island in Config.FREE_ISLANDS:
                free_island_clean = clean_text(island)
                try:
                    is_online = await self._check_island_online(guild, island, lookup=self.shared_state.free_island_lookup)
                except Exception as e:
                    logger.error(f"[DISCORD] island_monitor_loop error checking free island {island}: {e}")
                    continue
                _upsert_bot_status(island.lower(), island, is_online)

                # Track transitions for free islands so subscribers can be notified
                free_was_down = self.shared_state.island_down_states.get(f"free:{free_island_clean}")
                if free_was_down is None:
                    self.shared_state.island_down_states[f"free:{free_island_clean}"] = False
                    continue
                if not is_online and not free_was_down:
                    self.shared_state.island_down_states[f"free:{free_island_clean}"] = True
                    await self._notify_island_subscribers(free_island_clean, island, online=False)
                elif is_online and free_was_down:
                    self.shared_state.island_down_states[f"free:{free_island_clean}"] = False
                    await self._notify_island_subscribers(free_island_clean, island, online=True)

        # --- Order-bot island status ---
        if self.shared_state.order_island_lookup:
            for island in Config.ORDER_BOT_ISLANDS:
                order_island_clean = clean_text(island)
                channel_id = self.shared_state.order_island_lookup.get(order_island_clean)
                channel = guild.get_channel(channel_id) if channel_id else None
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    is_online = await self._check_island_online(guild, island, lookup=self.shared_state.order_island_lookup)
                except Exception as e:
                    logger.error(f"[DISCORD] island_monitor_loop error checking order island {island}: {e}")
                    continue
                _upsert_bot_status(island.lower(), island, is_online)

                state_key = f"order:{order_island_clean}"
                order_was_down = self.shared_state.island_down_states.get(state_key)
                if order_was_down is None:
                    self.shared_state.island_down_states[state_key] = False
                    continue
                if not is_online and not order_was_down:
                    self.shared_state.island_down_states[state_key] = True
                    await self._send_order_island_status_alert(channel, island, online=False)
                elif is_online and order_was_down:
                    self.shared_state.island_down_states[state_key] = False
                    await self._send_order_island_status_alert(channel, island, online=True)

        # Sticky message is managed by island_status_sticky_loop; just
        # request a non-reposting (edit-in-place) refresh so the embed
        # data stays current without sending duplicate messages.
        try:
            await self._refresh_island_status_sticky_message(force_repost=False)
        except Exception as exc:
            logger.warning(f"[DISCORD] Failed to refresh island status sticky embed: {exc}")

    @island_monitor_loop.before_loop
    async def before_island_monitor_loop(self):
        """Wait until bot is ready before starting the island monitor."""
        await self.bot.wait_until_ready()
        await self.fetch_islands()
        await self.fetch_free_islands()
        self._refresh_order_island_lookup()
