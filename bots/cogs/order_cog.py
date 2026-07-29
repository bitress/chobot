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

class OrderCog(commands.Cog):
    def __init__(self, bot, data_manager, shared_state: SharedCogState):
        self.bot = bot
        self.data_manager = data_manager
        self.shared_state = shared_state


    @commands.hybrid_command(name="visitors")
    async def visitors(self, ctx):
        """Check current visitors on the sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        def visitors_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_VISITORS_PATTERN.search(msg.content)
            )

        try:
            island_msg = await self.bot.wait_for('message', check=visitors_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)

            match = ISLAND_VISITORS_PATTERN.search(island_msg.content)
            island_name = match.group(1).strip() if match else ctx.channel.name

            visitor_lines = []
            for line in island_msg.content.split('\n'):
                m = VISITOR_LINE_PATTERN.match(line.strip())
                if m:
                    visitor_lines.append(m.group(1).strip())

            await island_msg.delete()
            await ctx.reply(embed=self._build_visitors_embed(ctx, island_name, visitor_lines))
            logger.info(f"[DISCORD] Intercepted and redesigned !visitors response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !visitors response in {ctx.channel.name}")

    @commands.hybrid_command(name="villagers")
    async def villagers(self, ctx):
        """Check current villagers on the sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        def villagers_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_VILLAGERS_PATTERN.search(msg.content)
            )

        try:
            island_msg = await self.bot.wait_for('message', check=villagers_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)

            match = ISLAND_VILLAGERS_PATTERN.search(island_msg.content)
            island_name = match.group(1).strip() if match else ctx.channel.name

            # Extract villagers from the message
            villager_text = island_msg.content
            if ":" in villager_text:
                villager_list = villager_text.split(":", 1)[1].strip()
                villagers_list = [v.strip() for v in villager_list.split(",")]
            else:
                villagers_list = []

            await island_msg.delete()
            await ctx.reply(embed=self._build_villagers_embed(ctx, island_name, villagers_list))
            logger.info(f"[DISCORD] Intercepted and redesigned !villagers response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !villagers response in {ctx.channel.name}")

    @commands.hybrid_command(name="drop")
    async def drop(self, ctx):
        """Request item drop on the sub island"""
        if self._is_order_island_channel(ctx.channel):
            island_name = Config.ORDER_BOT_ISLAND or "This island"
            await ctx.reply(
                f"{island_name} is an order-bot island. Use the order bot flow for this channel; `!drop` is only available on sub islands.",
                ephemeral=True,
            )
            return
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        def drop_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_DROP_PATTERN.search(msg.content)
            )

        try:
            island_msg = await self.bot.wait_for('message', check=drop_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)
            await island_msg.delete()
            await ctx.reply(embed=self._build_drop_embed(ctx))
            logger.info(f"[DISCORD] Intercepted and redesigned !drop response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !drop response in {ctx.channel.name}")
