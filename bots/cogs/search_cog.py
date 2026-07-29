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

class SearchCog(commands.Cog):
    def __init__(self, bot, data_manager, shared_state: SharedCogState):
        self.bot = bot
        self.data_manager = data_manager
        self.shared_state = shared_state


    async def item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Filter items from cache for autocomplete"""
        try:
            if not current:
                # Return empty list for no input
                return []
            
            with self.data_manager.lock:
                # Filter out internal keys like _display and _index
                all_keys = [k for k in self.data_manager.cache.keys() if not k.startswith("_")]
                display_map = self.data_manager.cache.get("_display", {})
            
            # Limit the number of keys to search for performance
            # Discord autocomplete timeout is 3 seconds
            search_keys = all_keys[:5000] if len(all_keys) > 5000 else all_keys
            
            # Use fuzzy matching to find top matches
            matches = process.extract(current, search_keys, limit=25, scorer=fuzz.partial_ratio)
            
            choices = []
            for match_key, score in matches:
                if score > 50:
                    display_name = display_map.get(match_key, match_key.title())
                    # Truncate if too long (Discord limit is 100)
                    choices.append(app_commands.Choice(name=display_name[:100], value=match_key))
            
            return choices
        except Exception as e:
            logger.error(f"[DISCORD] Error in item_autocomplete: {e}")
            # Return empty list on error to prevent crashes
            return []

    def create_found_embed(self, ctx_or_interaction, search_term, location_string, is_villager=False, nooki_data=None, island_map=None):

        user = getattr(ctx_or_interaction, "author", getattr(ctx_or_interaction, "user", None))
        clean_name = search_term.title()
        loc_list = sorted(list(set(location_string.split(", "))))
        sub_islands_found = []
        island_map = island_map or {}

        for loc in loc_list:
            loc_key = clean_text(loc)

            # STRICT FILTER: Only allow islands explicitly listed in Config.SUB_ISLANDS
            # Verify if the cleaned location corresponds to a known sub island
            is_sub = any(clean_text(si) == loc_key for si in Config.SUB_ISLANDS)
            if not is_sub:
                continue

            # Use get_island_channel_link for robust linking with fallback
            island_link = self.get_island_channel_link(loc)
            island_payload = island_map.get(loc_key)
            is_online = None
            if isinstance(island_payload, dict):
                status_text = str(island_payload.get("status", "")).upper()
                bot_online = island_payload.get("discord_bot_online")
                is_online = bool(bot_online) if bot_online is not None else (status_text == "ONLINE")
            status_icon = "🟢" if is_online is True else "🔴" if is_online is False else "⚪"
            sub_islands_found.append(f"{status_icon} {island_link}")

        # If no Sub Islands match, return None to indicate availability failure
        if not sub_islands_found:
            return None

        island_count = len(sub_islands_found)
        island_term = "island" if island_count == 1 else "islands"
        verb_term = "is" if island_count == 1 else "are"

        if is_villager:
            embed_title = f"{Config.EMOJI_SEARCH} Found Villager: {clean_name}"
            embed_desc = f"**{clean_name}** is currently residing on this {island_term}:" if island_count == 1 else f"**{clean_name}** is currently residing on these {island_term}:"
        else:
            embed_title = f"{Config.EMOJI_SEARCH} Found Item: {clean_name}"
            embed_desc = f"**{clean_name}** {verb_term} available on these {island_term}:"


        embed = discord.Embed(
            title=embed_title,
            description=embed_desc,
            color=discord.Color.teal(),
            timestamp=datetime.now()
        )

        search_key = normalize_text(search_term)

        # Apply Nookipedia Data if available
        if is_villager and nooki_data:
            villager_id = nooki_data.get("id", "")
            personality = nooki_data.get("personality", "Unknown")
            species = nooki_data.get("species", "Unknown")
            phrase = nooki_data.get("phrase", "None")
            gender = nooki_data.get("gender", "Unknown")
            birthday_month = nooki_data.get("birthday_month", "")
            birthday_day = nooki_data.get("birthday_day", "")
            birthday = f"{birthday_month} {birthday_day}".strip() or "Unknown"
            sign = nooki_data.get("sign", "Unknown")
            quote = nooki_data.get("quote", "")

            # NH Details
            nh = nooki_data.get("nh_details", {}) or {}
            hobby = nh.get("hobby", "Unknown")
            colors = ", ".join(nh.get("fav_colors", [])) or "Unknown"

            embed.set_thumbnail(url=nooki_data.get("image_url", ""))

            if quote:
                embed.description = f"*\"{quote}\"*"

            # Info field
            info_parts = [f"**Species:** {species}", f"**Gender:** {gender}"]
            if villager_id:
                info_parts.append(f"**Code:** `{villager_id}`")
            embed.add_field(
                name=f"{Config.STAR_PINK} Info",
                value="\n".join(info_parts),
                inline=True
            )

            # Personality field
            embed.add_field(
                name=f"{Config.STAR_PINK} Personality",
                value=f"**Type:** {personality}\n**Catchphrase:** \"{phrase}\"\n**Hobby:** {hobby}",
                inline=True
            )

            # Birthday / Details field
            detail_parts = []
            if birthday != "Unknown":
                detail_parts.append(f"**Birthday:** {birthday}")
            if sign and sign != "Unknown":
                detail_parts.append(f"**Sign:** {sign}")
            if colors and colors != "Unknown":
                detail_parts.append(f"**Colors:** {colors}")
            if detail_parts:
                embed.add_field(
                    name=f"{Config.STAR_PINK} Details",
                    value="\n".join(detail_parts),
                    inline=True
                )

        elif search_key in self.data_manager.image_cache:
            embed.set_thumbnail(url=self.data_manager.image_cache[search_key])

        full_text = "\n".join(sub_islands_found)
        chunks = []

        if len(full_text) <= 1024:
            chunks.append(full_text)
        else:
            current_chunk = ""
            for line in sub_islands_found:
                if len(current_chunk) + len(line) + 1 > 1024:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    if current_chunk:
                        current_chunk += "\n" + line
                    else:
                        current_chunk = line
            if current_chunk:
                chunks.append(current_chunk)


        for i, chunk in enumerate(chunks):
            name = f"{Config.STAR_PINK} Sub {island_term.capitalize()}"
            embed.add_field(name=name, value=chunk, inline=False)

        pfp_url = user.avatar.url if user.avatar else Config.DEFAULT_PFP
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(text=f"Requested by {user.display_name}", icon_url=pfp_url)

        return embed

    def create_villager_house_embed(self, ctx_or_interaction, villager_name, nooki_data):
        """Create a house information embed for a villager"""
        if not nooki_data:
            return None

        nh = nooki_data.get("nh_details", {}) or {}

        flooring = nh.get("house_flooring") or "Unknown"
        wallpaper = nh.get("house_wallpaper") or "Unknown"
        music = nh.get("house_music") or "Unknown"
        interior_url = nh.get("house_interior_url") or nh.get("house_img") or ""
        exterior_url = nh.get("house_exterior_url") or ""

        has_house_data = (
            flooring != "Unknown"
            or wallpaper != "Unknown"
            or music != "Unknown"
            or interior_url
            or exterior_url
        )
        if not has_house_data:
            return None

        clean_name = villager_name.title()
        user = getattr(ctx_or_interaction, "author", getattr(ctx_or_interaction, "user", None))

        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} {clean_name}'s House Information",
            color=discord.Color.teal(),
            timestamp=datetime.now()
        )

        embed.add_field(name=f"{Config.STAR_PINK} Flooring", value=flooring, inline=True)
        embed.add_field(name=f"{Config.STAR_PINK} Wallpaper", value=wallpaper, inline=True)
        embed.add_field(name=f"{Config.STAR_PINK} Music", value=music, inline=True)

        links = []
        if interior_url:
            links.append(f"[Interior]({interior_url})")
        if exterior_url:
            links.append(f"[Exterior]({exterior_url})")
        if links:
            embed.add_field(
                name=f"{Config.STAR_PINK} Image Previews",
                value=" | ".join(links),
                inline=False
            )

        if exterior_url:
            embed.set_thumbnail(url=exterior_url)

        if interior_url:
            embed.set_image(url=interior_url)

        pfp_url = user.avatar.url if user.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {user.display_name}", icon_url=pfp_url)

        return embed

    @commands.hybrid_command(name="find", aliases=['locate', 'where', 'search'])
    @app_commands.describe(item="The name of the item or recipe to find")
    @app_commands.autocomplete(item=item_autocomplete)
    async def find(self, ctx, *, item: str = ""):
        """Find an item"""

        if not await self._enforce_find_channel(ctx):
            return

        if not item:
            await ctx.reply("Usage: `!find <item name>`")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term_raw = item.strip()
        search_term = normalize_text(search_term_raw)

        with self.data_manager.lock:
            cache = self.data_manager.cache
            keys = [k for k in cache.keys() if k != "_display"]
            found_locations = cache.get(search_term)

        if found_locations:
            with self.data_manager.lock:
                display_name = cache.get("_display", {}).get(search_term, search_term_raw)

            island_map, _ = await self._fetch_islands_api_snapshot()
            embed = self.create_found_embed(
                ctx,
                display_name,
                found_locations,
                is_villager=False,
                island_map=island_map or {},
            )

            if embed:
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, look what I found!", embed=embed)
                logger.info(f"[DISCORD] Item Hit: {search_term} -> Found")
            else:
                await ctx.reply(f"**{display_name}** is not currently available on any Sub Island.")
                logger.info(f"[DISCORD] Item Hit: {search_term} -> Not on Sub Islands")
            return

        suggestion_keys = get_best_suggestions(search_term, keys, limit=8)

        with self.data_manager.lock:
            display_map = cache.get("_display", {})

        suggestions = [(k, display_map.get(k, k)) for k in suggestion_keys]
        embed_fail = self.create_fail_embed(ctx, search_term_raw, [disp for _, disp in suggestions])

        if suggestions:
            view = SuggestionView(self, suggestions, "item", ctx.author.id)
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail, view=view)
        else:
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail)

    @commands.hybrid_command(name="villager")
    @app_commands.describe(name="The name of the villager")
    async def villager(self, ctx, *, name: str = ""):
        """Find a villager"""

        if not await self._enforce_find_channel(ctx):
            return

        if not name:
            await ctx.reply("Usage: `!villager <n>`")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term = normalize_text(name)
        villager_map = self.data_manager.get_villagers([
            Config.VILLAGERS_DIR,
            Config.TWITCH_VILLAGERS_DIR
        ])

        found_locations = villager_map.get(search_term)

        if found_locations:
            nooki_data = await NookipediaClient.get_villager_info(search_term)
            island_map, _ = await self._fetch_islands_api_snapshot()
            embed = self.create_found_embed(
                ctx,
                search_term,
                found_locations,
                is_villager=True,
                nooki_data=nooki_data,
                island_map=island_map or {},
            )

            if embed:
                house_embed = self.create_villager_house_embed(ctx, search_term, nooki_data) if nooki_data else None
                send_embeds = [embed] + ([house_embed] if house_embed else [])
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, look who I found!", embeds=send_embeds)
                logger.info(f"[DISCORD] Villager Hit: {search_term} -> Found")
            else:
                await ctx.reply(f"**{search_term.title()}** is not currently on any Sub Island.")
                logger.info(f"[DISCORD] Villager Hit: {search_term} -> Not on Sub Islands")
            return

        matches = process.extract(search_term, list(villager_map.keys()), limit=3, scorer=fuzz.WRatio)
        suggestions = [(m[0], m[0].title()) for m in matches if m[1] > 75]
        suggestion_display_names = [s[1] for s in suggestions]

        embed_fail = self.create_fail_embed(ctx, search_term, suggestion_display_names, is_villager=True)

        if suggestions:
            view = SuggestionView(self, suggestions, "villager", ctx.author.id)
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail, view=view)
        else:
            await ctx.reply(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail)

        logger.info(f"[DISCORD] Villager Miss: {search_term}")
