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
from bots.cogs.cog_utils import _discord_conv_key, _is_subscriber_member, _is_mod_member, _get_accessible_islands

logger = logging.getLogger("DiscordCommandBot")

class FunCog(commands.Cog):
    def __init__(self, bot, data_manager, shared_state: SharedCogState):
        self.bot = bot
        self.data_manager = data_manager
        self.shared_state = shared_state


    @commands.hybrid_command(name="ping")
    async def ping(self, ctx):
        """Check bot latency"""
        latency_ms = round(self.bot.latency * 1000, 2)
        
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Bot latency: **{latency_ms}ms**",
            color=discord.Color.green() if latency_ms < 200 else discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        await ctx.reply(embed=embed)
        logger.info(f"[DISCORD] Ping: {latency_ms}ms")

    @commands.hybrid_command(name="trivia", aliases=["acnhquiz", "quiz"])
    async def trivia(self, ctx):
        """Play an ACNH trivia question! Answer with the buttons before time runs out."""
        q = random.choice(ACNH_TRIVIA_QUESTIONS)
        letter = TRIVIA_LETTER
        choices_text = "\n".join(
            f"{letter[i]} {choice}" for i, choice in enumerate(q["c"])
        )
        embed = discord.Embed(
            title="🏝️ ACNH Trivia!",
            description=f"**{q['q']}**\n\n{choices_text}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=f"You have {TRIVIA_TIMEOUT} seconds to answer! • Asked by {ctx.author.display_name}",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP,
        )
        view = TriviaView(q, timeout=TRIVIA_TIMEOUT)
        msg = await ctx.reply(embed=embed, view=view)
        # Store the message reference so on_timeout can edit it
        view.message = msg
        logger.info(f"[DISCORD] Trivia question asked by {ctx.author.name}: {q['q'][:60]}")

    @commands.hybrid_command(name="status")
    @is_admin_or_senior_mod()
    async def status(self, ctx):
        """Show bot status"""
        with self.data_manager.lock:
            if self.data_manager.last_update:
                t_str = self.data_manager.last_update.strftime("%H:%M:%S")
                island_count = len(self.shared_state.sub_island_lookup)
                
                # Calculate uptime
                uptime_seconds = (datetime.now() - self.bot.start_time).total_seconds()
                hours = int(uptime_seconds // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                uptime_str = f"{hours}h {minutes}m"
                
                await ctx.reply(
                    f"**System Status**\n"
                    f"Items Cached: `{len(self.data_manager.cache)}`\n"
                    f"Islands Linked: `{island_count}`\n"
                    f"Last Update: `{t_str}`\n"
                    f"Uptime: `{uptime_str}`"
                )
            else:
                await ctx.reply("Database loading...")

    @commands.hybrid_command(name="random")
    async def random_item(self, ctx):
        """Get a random item suggestion"""
        with self.data_manager.lock:
            cache = self.data_manager.cache
            # Filter out internal keys
            all_items = [k for k in cache.keys() if not k.startswith("_")]
            display_map = cache.get("_display", {})
        
        if not all_items:
            await ctx.reply("No items in cache yet. Try again later!")
            return
        
        # Pick a random item
        random_key = random.choice(all_items)
        display_name = display_map.get(random_key, random_key.title())
        found_locations = cache.get(random_key)
        
        if found_locations:
            search_cog = self.bot.get_cog("SearchCog")
            embed = search_cog.create_found_embed(ctx, display_name, found_locations, is_villager=False) if search_cog else None
            
            if embed:
                embed.title = f"🎲 Random Item: {display_name}"
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, here's a random item for you!", embed=embed)
                logger.info(f"[DISCORD] Random item: {random_key}")
            else:
                # Item exists but not on sub islands
                await ctx.reply(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to see where it's available!")
        else:
            await ctx.reply(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to check availability!")

    @commands.hybrid_command(name="ask")
    @app_commands.describe(question="Your question about the Chopaeng community")
    async def ask_ai(self, ctx, *, question: str = ""):
        """Ask the Chopaeng AI anything about the community"""
        if not question:
            await ctx.reply("Usage: `!ask <question>` — e.g. `!ask how do I get a Dodo code?`")
            return

        await ctx.defer()
        conv_key = _discord_conv_key(ctx.message)
        channel_name = getattr(ctx.channel, "name", None)
        answer = await get_ai_answer(
            question,
            gemini_api_key=Config.GEMINI_API_KEY,
            openai_api_key=Config.OPENAI_API_KEY,
            openai_base_url=Config.OPENAI_BASE_URL,
            provider=Config.AI_PROVIDER,
            gemini_model=Config.GEMINI_MODEL,
            openai_model=Config.OPENAI_MODEL,
            conversation_key=conv_key,
            channel_context=channel_name,
            is_subscriber=_is_subscriber_member(ctx.author),
            is_mod_user=_is_mod_member(ctx.author),
            accessible_islands=_get_accessible_islands(ctx.author),
        )

        await ctx.reply(f"{answer}")
        logger.info(f"[DISCORD] Ask command by {ctx.author.name}: {question[:80]}")