"""
Discord Command Bot Module
Handles Discord commands for item and villager search with rich embeds
"""

import asyncio
import time
import re
import random
import logging
from datetime import datetime
from itertools import cycle

import discord
from discord import app_commands
from discord.ext import commands, tasks
from thefuzz import process, fuzz

from utils.config import Config
from utils.helpers import normalize_text, get_best_suggestions, clean_text
from utils.nookipedia import NookipediaClient

logger = logging.getLogger("DiscordCommandBot")

# Island status check constants
DODO_CODE_PATTERN = re.compile(r'\b[A-HJ-NP-Z0-9]{5}\b')
ISLAND_HOST_NAME = "chopaeng"
MESSAGE_HISTORY_LIMIT = 30
ISLAND_DOWN_IMAGE_URL = "https://cdn.chopaeng.com/misc/Bot-is-Down.jpg"


class SuggestionSelect(discord.ui.Select):
    """Dropdown select for choosing from suggestions"""

    def __init__(self, cog, suggestions, search_type):
        self.cog = cog
        self.search_type = search_type

        options = [
            discord.SelectOption(label=str(disp)[:100], value=str(norm_key)[:100])
            for (norm_key, disp) in suggestions[:25]
        ]

        super().__init__(
            placeholder="Select the correct item...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle selection"""
        selected_key = self.values[0]

        with self.cog.data_manager.lock:
            display_name = self.cog.data_manager.cache.get("_display", {}).get(
                selected_key, selected_key.title()
            )

        found_locations = None
        is_villager = False

        if self.search_type == "item":
            with self.cog.data_manager.lock:
                found_locations = self.cog.data_manager.cache.get(selected_key)
            is_villager = False
        elif self.search_type == "villager":
            v_map = self.cog.data_manager.get_villagers([
                Config.VILLAGERS_DIR,
                Config.TWITCH_VILLAGERS_DIR
            ])
            found_locations = v_map.get(selected_key)
            is_villager = True

        if found_locations:
            nooki_data = None
            if is_villager:
                nooki_data = await NookipediaClient.get_villager_info(display_name)

            embed = self.cog.create_found_embed(interaction, display_name, found_locations, is_villager, nooki_data)

            if embed:
                await interaction.response.edit_message(
                    content=f"Hey <@{interaction.user.id}>, look what I found!",
                    embed=embed,
                    view=None
                )
            else:
                await interaction.response.edit_message(
                    content=f"**{display_name}** is not currently available on any Sub Island.",
                    embed=None,
                    view=None
                )
        else:
            await interaction.response.send_message(
                "Error: Item data lost. Please try searching again.",
                ephemeral=True
            )


class SuggestionView(discord.ui.View):
    """View containing suggestion dropdown"""

    def __init__(self, cog, suggestions, search_type, author_id):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.add_item(SuggestionSelect(cog, suggestions, search_type))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only requester can use the menu"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu is for the requester only.",
                ephemeral=True
            )
            return False
        return True


class DiscordCommandCog(commands.Cog):
    """Cog for Discord treasure hunt commands"""

    def __init__(self, bot, data_manager):
        self.bot = bot
        self.data_manager = data_manager
        self.cooldowns = {}
        self.sub_island_lookup = {}

        self.auto_refresh_cache.start()
        # island_clean -> True (down) / False (up); None = not yet initialized
        self.island_down_states: dict[str, bool | None] = {}
        self.island_monitor_loop.start()

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

    async def fetch_islands(self):
        """Fetch island channels from Discord using robust matching"""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            logger.error(f"[DISCORD] Guild {Config.GUILD_ID} not found.")
            return

        category = discord.utils.get(guild.categories, id=Config.CATEGORY_ID)
        if not category:
            logger.error(f"[DISCORD] Category {Config.CATEGORY_ID} not found.")
            return

        temp_lookup = {}
        count = 0
        all_possible_islands = Config.SUB_ISLANDS

        for channel in category.channels:
            if channel.id == Config.IGNORE_CHANNEL_ID:
                continue

            chan_clean = clean_text(channel.name)

            for island in all_possible_islands:
                island_clean = clean_text(island)
                if island_clean in chan_clean:
                    # Use clean name as key for consistent lookups
                    temp_lookup[island_clean] = channel.id
                    count += 1
                    break

        self.sub_island_lookup = temp_lookup
        logger.info(f"[DISCORD] Dynamic Island Fetch Complete. Found {count} islands.")

    def cog_unload(self):
        """Cleanup on unload"""
        self.auto_refresh_cache.cancel()
        self.island_monitor_loop.cancel()

    def check_cooldown(self, user_id: str, cooldown_sec: int = 3) -> bool:
        """Check if user is on cooldown"""
        now = time.time()
        if user_id in self.cooldowns:
            if now - self.cooldowns[user_id] < cooldown_sec:
                return True
        self.cooldowns[user_id] = now

        # Periodic cleanup: prune entries older than 60s every 100 entries
        if len(self.cooldowns) > 100:
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < 60}

        return False

    def get_island_channel_link(self, island_name):
        """Get channel link for an island with robust fallback search"""
        island_clean = clean_text(island_name)
        if not island_clean:
            return f"**{island_name.title()}**"
        
        # First check our cached lookup
        if island_clean in self.sub_island_lookup:
            return f"<#{self.sub_island_lookup[island_clean]}>"
        
        # Fallback: search through guild channels matching island name
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild:
            category = discord.utils.get(guild.categories, id=Config.CATEGORY_ID)
            if category:
                for channel in category.channels:
                    if channel.id == Config.IGNORE_CHANNEL_ID:
                        continue
                    chan_clean = clean_text(channel.name)
                    # Match if island name is in channel name (e.g., "alapaap" in "01-alapaap")
                    if island_clean in chan_clean:
                        # Cache it for next time
                        self.sub_island_lookup[island_clean] = channel.id
                        return f"<#{channel.id}>"
        
        # If no channel found, return bold text
        return f"**{island_name.title()}**"

    def create_found_embed(self, ctx_or_interaction, search_term, location_string, is_villager=False, nooki_data=None):

        user = getattr(ctx_or_interaction, "author", getattr(ctx_or_interaction, "user", None))
        clean_name = search_term.title()
        loc_list = sorted(list(set(location_string.split(", "))))
        sub_islands_found = []

        for loc in loc_list:
            loc_key = clean_text(loc)

            # STRICT FILTER: Only allow islands explicitly listed in Config.SUB_ISLANDS
            # Verify if the cleaned location corresponds to a known sub island
            is_sub = any(clean_text(si) == loc_key for si in Config.SUB_ISLANDS)
            if not is_sub:
                continue

            # Use get_island_channel_link for robust linking with fallback
            island_link = self.get_island_channel_link(loc)
            sub_islands_found.append(island_link)

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
            personality = nooki_data.get("personality", "Unknown")
            species = nooki_data.get("species", "Unknown")
            phrase = nooki_data.get("phrase", "None")
            
            # NH Details
            nh = nooki_data.get("nh_details", {}) or {}
            hobby = nh.get("hobby", "Unknown")
            colors = ", ".join(nh.get("fav_colors", [])) or "Unknown"
            
            embed.set_thumbnail(url=nooki_data.get("image_url", ""))
            if nh.get("house_img"):
                embed.set_image(url=nh.get("house_img"))
            
            embed.add_field(name=f"{Config.STAR_PINK} Details", 
                            value=f"**Species:** {species}\n**Personality:** {personality}\n**Catchphrase:** \"{phrase}\"", 
                            inline=True)
            embed.add_field(name=f"{Config.STAR_PINK} Faves", 
                            value=f"**Hobby:** {hobby}\n**Colors:** {colors}", 
                            inline=True)

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

    def create_fail_embed(self, ctx, search_term, suggestions, is_villager=False):

        category = "Villager" if is_villager else "Item"

        embed = discord.Embed(
            title=f"{Config.EMOJI_FAIL} {category} Not Found: {search_term.title()}",
            description=f"I couldn't find exactly that. Did you mean one of these?",
            color=0xFF4444,
            timestamp=discord.utils.utcnow()
        )

        if suggestions:
            embed.add_field(
                name=f"{Config.STAR_PINK} Suggestions",
                value="\n".join([f"{Config.INDENT} {s.title()}" for s in suggestions[:5]]),
                inline=False
            )
        else:
            embed.description = f"I searched everywhere but couldn't find it.\n\n{Config.DROPBOT_INFO}"


        user_avatar = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=user_avatar)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    @tasks.loop(hours=1)
    async def auto_refresh_cache(self):
        """Auto refresh island channel links (cache is refreshed by DataManager's own thread)"""
        await self.fetch_islands()

    @auto_refresh_cache.before_loop
    async def before_refresh(self):
        """Wait until ready before starting refresh loop"""
        await self.bot.wait_until_ready()
        await self.fetch_islands()

    @commands.hybrid_command(name="find", aliases=['locate', 'where', 'lookup', 'lp', 'search'])
    @app_commands.describe(item="The name of the item or recipe to find")
    @app_commands.autocomplete(item=item_autocomplete)
    async def find(self, ctx, *, item: str = ""):
        """Find an item"""
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

            embed = self.create_found_embed(ctx, display_name, found_locations, is_villager=False)

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
            embed = self.create_found_embed(ctx, search_term, found_locations, is_villager=True, nooki_data=nooki_data)

            if embed:
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, look who I found!", embed=embed)
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

    @commands.hybrid_command(name="help")
    async def help_command(self, ctx):
        """Show all available commands"""
        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} Chobot Commands",
            description="Here are all the commands you can use:",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name=f"{Config.STAR_PINK} Search Commands",
            value=(
                "`!find <item>` - Find an item across islands\n"
                "`!villager <name>` - Find a villager\n"
                "*Aliases: !locate, !where, !lookup, !lp, !search*"
            ),
            inline=False
        )
        
        embed.add_field(
            name=f"{Config.STAR_PINK} Sub Island Commands",
            value=(
                "`!senddodo` or `!sd` - Get the dodo code for this sub island\n"
                "`!visitors` - Check current visitors on this sub island\n"
                "*Use these in a sub island channel. If the island is offline, you'll see an 'island is down' message.*"
            ),
            inline=False
        )

        embed.add_field(
            name=f"{Config.STAR_PINK} Utility Commands",
            value=(
                "`!islandstatus` - Check if all 18 sub island bots are active\n"
                "`!status` - Show bot status and cache info\n"
                "`!ping` - Check bot response time\n"
                "`!random` - Get a random item suggestion\n"
                "`!help` - Show this help message"
            ),
            inline=False
        )
        
        embed.add_field(
            name=f"{Config.STAR_PINK} Flight Logger (Automatic)",
            value=(
                "🛫 Monitors island visitor arrivals in real time\n"
                "🔍 Alerts staff when unknown travelers are detected\n"
                "🛡️ Staff can Admit, Warn, Kick, or Ban via buttons\n"
                "📋 Tracks warnings and moderation history per user"
            ),
            inline=False
        )

        embed.add_field(
            name=f"{Config.STAR_PINK} Admin Commands",
            value="`!refresh` - Manually refresh cache (Admin only)",
            inline=False
        )

        embed.add_field(
            name="💡 Tips",
            value=(
                "• Use `/find` or `/villager` for slash command support\n"
                "• Try `!random` to discover items you might have missed\n"
                "• All search commands support fuzzy matching"
            ),
            inline=False
        )

        embed.set_footer(text=f"Requested by {ctx.author.display_name}", 
                        icon_url=ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP)
        embed.set_image(url=Config.FOOTER_LINE)
        
        await ctx.reply(embed=embed)
        logger.info(f"[DISCORD] Help command used by {ctx.author.name}")

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
            embed = self.create_found_embed(ctx, display_name, found_locations, is_villager=False)
            
            if embed:
                embed.title = f"🎲 Random Item: {display_name}"
                await ctx.reply(content=f"Hey <@{ctx.author.id}>, here's a random item for you!", embed=embed)
                logger.info(f"[DISCORD] Random item: {random_key}")
            else:
                # Item exists but not on sub islands
                await ctx.reply(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to see where it's available!")
        else:
            await ctx.reply(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to check availability!")

    @commands.hybrid_command(name="status")
    async def status(self, ctx):
        """Show bot status"""
        with self.data_manager.lock:
            if self.data_manager.last_update:
                t_str = self.data_manager.last_update.strftime("%H:%M:%S")
                island_count = len(self.sub_island_lookup)
                
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

    @commands.hybrid_command(name="islands", aliases=["islandstatus", "checkislands"])
    async def island_status(self, ctx):
        """Check the status of all sub islands"""
        await ctx.defer()

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            await ctx.reply("Guild not found.")
            return

        # Ensure the channel lookup is fresh before checking
        await self.fetch_islands()

        results = []
        online_count = 0

        # Resolve the shared island-bot role once
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        if Config.ISLAND_BOT_ROLE_ID and not island_bot_role:
            logger.warning(f"[DISCORD] ISLAND_BOT_ROLE_ID {Config.ISLAND_BOT_ROLE_ID} not found in guild; bot name matching disabled")

        for island in Config.SUB_ISLANDS:
            island_clean = clean_text(island)
            channel_id = self.sub_island_lookup.get(island_clean)

            # Fallback: scan all guild text channels if not found via category lookup
            if not channel_id:
                for ch in guild.channels:
                    if isinstance(ch, discord.TextChannel) and island_clean in clean_text(ch.name):
                        channel_id = ch.id
                        break

            if not channel_id:
                results.append((island, "❓", "Channel not found", None))
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                results.append((island, "❓", "Channel not found", None))
                continue

            # Find the bot for this island by name: "Chobot <island name>"
            # Bots share ISLAND_BOT_ROLE_ID and may use fancy Unicode in their display name
            # (e.g. "ℂ𝕙𝕠𝔹𝕠𝕥 (ᴀʟᴀᴘᴀᴀᴘ)") — clean_text normalises both sides for matching
            island_bot = None
            if island_bot_role:
                island_clean_target = clean_text(f"chobot {island}")
                for member in island_bot_role.members:
                    if member.bot and clean_text(member.display_name) == island_clean_target:
                        island_bot = member
                        break

            # Check 1: If the island's bot is found and online or idle, it's working
            if island_bot and island_bot.status in (discord.Status.online, discord.Status.idle):
                results.append((island, "✅", "Bot online", channel_id))
                online_count += 1
                continue

            # Check 2: Scan recent channel messages for dodo codes or Chopaeng visitor
            try:
                messages = [msg async for msg in channel.history(limit=25)]
            except discord.Forbidden:
                results.append((island, "❓", "No channel access", channel_id))
                continue

            island_up = False
            status_reason = ""

            for msg in messages:
                # Only examine messages from this island's bot (if known),
                # otherwise fall back to any bot in the channel
                if island_bot:
                    if msg.author.id != island_bot.id:
                        continue
                elif not msg.author.bot:
                    continue

                # Dodo code: 5 uppercase alphanumeric chars (ACNH format excludes I and O)
                if DODO_CODE_PATTERN.search(msg.content):
                    island_up = True
                    status_reason = "Dodo code active"
                    break

                # Chopaeng present in visitors response
                if ISLAND_HOST_NAME in msg.content.lower():
                    island_up = True
                    status_reason = "Chopaeng is visiting"
                    break

            if island_up:
                results.append((island, "✅", status_reason, channel_id))
                online_count += 1
            else:
                results.append((island, "❌", "No recent activity", channel_id))

        # Build embed
        total = len(Config.SUB_ISLANDS)
        embed = discord.Embed(
            title="🏝️ Sub Island Status",
            description=f"**{online_count}/{total}** islands active",
            color=discord.Color.green() if online_count == total else (
                discord.Color.orange() if online_count > 0 else discord.Color.red()
            ),
            timestamp=discord.utils.utcnow()
        )

        online_lines = [f"<#{ch_id}>" if ch_id else f"**{name}**" for name, status, _, ch_id in results if status == "✅"]
        offline_lines = [f"<#{ch_id}>" if ch_id else f"**{name}**" for name, status, _, ch_id in results if status != "✅"]

        embed.add_field(name="🟢 ONLINE", value="\n".join(online_lines) or "*none*", inline=True)
        embed.add_field(name="🔴 OFFLINE", value="\n".join(offline_lines) or "*none*", inline=True)

        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)

        await ctx.reply(embed=embed)
        logger.info(f"[DISCORD] Island status check: {online_count}/{total} online")

    def _get_island_bot_for_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        """Return the island bot member for the given channel, or None if not found."""
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        if not island_bot_role:
            return None

        chan_clean = clean_text(channel.name)
        for island in Config.SUB_ISLANDS:
            if clean_text(island) in chan_clean:
                target = clean_text(f"chobot {island}")
                for member in island_bot_role.members:
                    if member.bot and clean_text(member.display_name) == target:
                        return member
                break
        return None

    def _is_sub_island_channel(self, channel) -> bool:
        """Return True if the channel belongs to the sub-islands category."""
        if not Config.CATEGORY_ID:
            return False
        return getattr(channel, "category_id", None) == Config.CATEGORY_ID

    def _build_status_embed(self, ctx, title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build a status embed with the given title, description and color."""
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_image(url=Config.FOOTER_LINE)
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        return embed

    def _create_island_down_embed(self, ctx) -> discord.Embed:
        """Build the standard 'island is down' embed."""
        return self._build_status_embed(
            ctx,
            title="🏝️ Island is Down",
            description=(
                "This island is currently **offline** or no information is available.\n\n"
                "Please use another island in the meantime or wait for this island to come back up."
            ),
            color=discord.Color.red(),
        )

    def _create_no_info_embed(self, ctx, title: str, description: str) -> discord.Embed:
        """Build an embed for when the island is online but no info is found in recent messages."""
        return self._build_status_embed(ctx, title=title, description=description, color=discord.Color.orange())

    async def _check_island_online(self, guild: discord.Guild, island: str) -> bool:
        """Return True if the island appears to be online, False otherwise."""
        island_clean = clean_text(island)
        channel_id = self.sub_island_lookup.get(island_clean)
        if not channel_id:
            return False

        channel = guild.get_channel(channel_id)
        if not channel:
            return False

        # Check island bot presence first (fast, no API call)
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        island_bot = None
        if island_bot_role:
            target = clean_text(f"chobot {island}")
            for member in island_bot_role.members:
                if member.bot and clean_text(member.display_name) == target:
                    island_bot = member
                    break

        if island_bot:
            return island_bot.status in (discord.Status.online, discord.Status.idle)

        # Fallback: scan recent channel messages for dodo code / host presence
        try:
            messages = [msg async for msg in channel.history(limit=MESSAGE_HISTORY_LIMIT)]
        except discord.Forbidden:
            return False

        for msg in messages:
            if not msg.author.bot:
                continue
            if DODO_CODE_PATTERN.search(msg.content) or ISLAND_HOST_NAME in msg.content.lower():
                return True

        return False

    @tasks.loop(minutes=5)
    async def island_monitor_loop(self):
        """Background task: detect island down/up transitions and notify in channel."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        if not self.sub_island_lookup:
            try:
                await self.fetch_islands()
            except Exception as e:
                logger.error(f"[DISCORD] island_monitor_loop failed to fetch islands: {e}")
                return

        for island in Config.SUB_ISLANDS:
            island_clean = clean_text(island)
            channel_id = self.sub_island_lookup.get(island_clean)
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

            previous = self.island_down_states.get(island_clean)  # None = first run

            if previous is None:
                # First run: always initialize as "not down" so that a "back up"
                # notification is only ever sent after we have sent a "Bot is Down"
                # embed in this session (i.e. never on a cold start when the island
                # is already online).
                self.island_down_states[island_clean] = False
                continue

            was_down = previous  # True means it was down

            if not is_online and not was_down:
                # Transition: online → offline
                self.island_down_states[island_clean] = True
                embed = discord.Embed(
                    title="🏝️ Island is Down",
                    description=f"**{island}** island is currently **offline**.",
                    color=discord.Color.red(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_image(url=ISLAND_DOWN_IMAGE_URL)
                try:
                    await channel.send(embed=embed)
                    logger.info(f"[DISCORD] Island monitor: {island} went OFFLINE")
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to send island-down embed for {island}: {e}")

            elif is_online and was_down:
                # Transition: offline → online
                self.island_down_states[island_clean] = False
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

    @island_monitor_loop.before_loop
    async def before_island_monitor_loop(self):
        """Wait until bot is ready before starting the island monitor."""
        await self.bot.wait_until_ready()
        await self.fetch_islands()

    @commands.hybrid_command(name="senddodo", aliases=["sd"])
    async def send_dodo(self, ctx):
        """Get the dodo code for this sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        # If the island bot is found and offline, report immediately
        if island_bot and island_bot.status not in (discord.Status.online, discord.Status.idle):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        # Scan recent messages for a dodo code from the island bot
        try:
            messages = [msg async for msg in ctx.channel.history(limit=MESSAGE_HISTORY_LIMIT)]
        except discord.Forbidden:
            await ctx.reply("I don't have permission to read this channel's history.")
            return

        dodo_code = None
        for msg in messages:
            if island_bot and msg.author.id != island_bot.id:
                continue
            elif not island_bot and not msg.author.bot:
                continue
            match = DODO_CODE_PATTERN.search(msg.content)
            if match:
                dodo_code = match.group(0)
                break

        if dodo_code:
            embed = discord.Embed(
                title="✈️ Dodo Code",
                description=f"The current dodo code for **{ctx.channel.name}** is:\n\n## `{dodo_code}`",
                color=discord.Color.teal(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_image(url=Config.FOOTER_LINE)
            pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Dodo code retrieved for {ctx.channel.name}: {dodo_code}")
        elif island_bot and island_bot.status in (discord.Status.online, discord.Status.idle):
            await ctx.reply(embed=self._create_no_info_embed(
                ctx,
                "✈️ No Dodo Code Found",
                "The island bot is **online** but no dodo code was found in recent messages.\n\n"
                "The dodo code may have been sent via DM or the island session hasn't started yet. "
                "Please wait a moment and try again."
            ))
            logger.info(f"[DISCORD] Dodo code not found for {ctx.channel.name} (island bot online)")
        else:
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            logger.info(f"[DISCORD] Dodo code not found for {ctx.channel.name}")

    @commands.hybrid_command(name="visitors")
    async def visitors(self, ctx):
        """Check current visitors on this sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        # If the island bot is found and offline, report immediately
        if island_bot and island_bot.status not in (discord.Status.online, discord.Status.idle):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        # Scan recent messages from the island bot for visitor information
        try:
            messages = [msg async for msg in ctx.channel.history(limit=MESSAGE_HISTORY_LIMIT)]
        except discord.Forbidden:
            await ctx.reply("I don't have permission to read this channel's history.")
            return

        visitor_msg = None
        for msg in messages:
            if island_bot and msg.author.id != island_bot.id:
                continue
            elif not island_bot and not msg.author.bot:
                continue
            content_lower = msg.content.lower()
            if "visitor" in content_lower or "arrive" in content_lower or ISLAND_HOST_NAME in content_lower:
                visitor_msg = msg.content
                break

        if visitor_msg:
            embed = discord.Embed(
                title="👥 Island Visitors",
                description=f"**Latest visitor update for {ctx.channel.name}:**\n\n{visitor_msg}",
                color=discord.Color.teal(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_image(url=Config.FOOTER_LINE)
            pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Visitor info retrieved for {ctx.channel.name}")
        elif island_bot and island_bot.status in (discord.Status.online, discord.Status.idle):
            await ctx.reply(embed=self._create_no_info_embed(
                ctx,
                "👥 No Visitor Info Found",
                "The island bot is **online** but no visitor information was found in recent messages.\n\n"
                "There may be no active visitors right now. Please try again later."
            ))
            logger.info(f"[DISCORD] Visitor info not found for {ctx.channel.name} (island bot online)")
        else:
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            logger.info(f"[DISCORD] Visitor info not found for {ctx.channel.name}")

    @commands.hybrid_command(name="refresh")
    @commands.has_permissions(administrator=True)
    async def refresh(self, ctx):
        """Manually refresh cache (Mods only)"""
        await ctx.reply("Refreshing cache and island links...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.data_manager.update_cache)
        await self.fetch_islands()
        count = len(getattr(self, 'island_map', {})) 
        await ctx.reply(f"Done. Linked {count} islands.")

    @refresh.error
    async def refresh_error(self, ctx, error):
        """Handle permission errors cleanly"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")


class DiscordCommandBot(commands.Bot):
    """Main Discord bot with command functionality"""

    def __init__(self, data_manager, load_command_cog: bool = True):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)

        self.data_manager = data_manager
        self._load_command_cog = load_command_cog
        self.start_time = datetime.now()

        self.status_list = cycle([
            discord.Activity(type=discord.ActivityType.watching, name="flights arrive ✈️ | !find"),
            discord.Activity(type=discord.ActivityType.watching, name="villagers pack up 📦 | !villager"),
            discord.Activity(type=discord.ActivityType.watching, name="shooting stars 🌠"),
            discord.Activity(type=discord.ActivityType.watching, name="the turnip market 📉"),

            discord.Activity(type=discord.ActivityType.playing, name="with the Item Database 📚"),
            discord.Activity(type=discord.ActivityType.playing, name="Animal Crossing: New Horizons 🍃"),
            discord.Activity(type=discord.ActivityType.playing, name="Browsing chopaeng.com 🌐"),
            discord.Activity(type=discord.ActivityType.playing, name="Hide and Seek with Dodo 🦤"),

            discord.Activity(type=discord.ActivityType.competing, name="the Fishing Tourney 🎣"),
            discord.Activity(type=discord.ActivityType.competing, name="the Bug-Off 🦋"),
            discord.Activity(type=discord.ActivityType.competing, name="island traffic 🚦"),

            discord.Activity(type=discord.ActivityType.listening, name="K.K. Slider 🎸"),
            discord.Activity(type=discord.ActivityType.listening, name="Isabelle's announcements 📢"),

            discord.Activity(type=discord.ActivityType.watching, name="twitch.tv/chopaeng 📺"),
            discord.Activity(type=discord.ActivityType.watching, name="46x Treasure Islands 🏝️"),
            discord.Activity(type=discord.ActivityType.watching, name="chat spam !order 🤖"),
            discord.Activity(type=discord.ActivityType.watching, name="someone break the max bells glitch 💰 | !maxbells"),
            discord.Activity(type=discord.ActivityType.watching, name="endless dodocode interference ✈️"),

            discord.Activity(type=discord.ActivityType.playing, name="traffic controller for Sub Islands 💎"),
            discord.Activity(type=discord.ActivityType.playing, name="DropBot delivery simulator 📦"),
            discord.Activity(type=discord.ActivityType.playing, name="spamming 'A' at the airport 🛫"),

            discord.Activity(type=discord.ActivityType.competing, name="who can join Marahuyo fastest 🏃"),

            discord.Activity(type=discord.ActivityType.listening, name="Kuya Cho sipping coffee ☕"),
            discord.Activity(type=discord.ActivityType.listening, name="Discord ping spam 🔔 | !discord"),
            discord.Activity(type=discord.ActivityType.listening, name="someone leaving quietly... 😡"),

            discord.Activity(type=discord.ActivityType.watching, name="interference with total indifference 🧘"),
            discord.Activity(type=discord.ActivityType.watching, name="turnips rot; such is life 🥀"),
            discord.Activity(type=discord.ActivityType.watching, name="the void of a lost connection 🔌"),
            discord.Activity(type=discord.ActivityType.watching, name="Amor Fati: loving the Sea Bass 🐟"),

            discord.Activity(type=discord.ActivityType.playing, name="Memento Mori: the island wipes ⏳"),
            discord.Activity(type=discord.ActivityType.playing, name="controlling only what I can: the 'A' button 🔘"),

            discord.Activity(type=discord.ActivityType.listening, name="Meditations by Marcus Aurelius (K.K. Version) 📖"),
            discord.Activity(type=discord.ActivityType.listening, name="the silence of an empty queue 🤫"),
            discord.Activity(type=discord.ActivityType.listening, name="complaints, unbothered 🗿"),
        ])

    async def setup_hook(self):
        """Setup bot cogs and sync commands"""
        if self._load_command_cog:
            await self.add_cog(DiscordCommandCog(self, self.data_manager))

        # Add global interaction check for slash commands in FIND_BOT_CHANNEL
        async def check_find_channel_restriction(interaction: discord.Interaction) -> bool:
            """Restrict slash commands in FIND_BOT_CHANNEL to only allowed commands"""
            if not Config.FIND_BOT_CHANNEL_ID:
                return True  # No restriction if channel ID not set
            
            if interaction.channel_id == Config.FIND_BOT_CHANNEL_ID:
                # Allowed commands in FIND_BOT_CHANNEL
                allowed_commands = {
                    'find', 'locate', 'where', 'lookup', 'lp', 'search',  # find and aliases
                    'villager',
                    'refresh'
                }
                
                # Get the command name
                command_name = interaction.command.name if interaction.command else None
                
                # If it's a command and not allowed, block it
                if command_name and command_name not in allowed_commands:
                    await interaction.channel.send(
                        "You can only use `/find` (and its aliases), `/villager` commands in this channel.",
                        delete_after=5
                    )

                    logger.info(f"[DISCORD] Blocked slash command '/{command_name}' in FIND_BOT_CHANNEL from {interaction.user}")
                    return False
            
            return True
        
        self.tree.interaction_check = check_find_channel_restriction

        if Config.GUILD_ID:
            guild_obj = discord.Object(id=Config.GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info(f"[DISCORD] Slash commands synced to Guild ID: {Config.GUILD_ID}")
        else:
            await self.tree.sync()
            logger.info("[DISCORD] Slash commands synced globally")

        self.change_status_loop.start()

    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f"[DISCORD] Logged in as: {self.user} (ID: {self.user.id})")

    @tasks.loop(minutes=1)
    async def change_status_loop(self):
        """Cycle through status messages"""
        new_activity = next(self.status_list)
        await self.change_presence(activity=new_activity)

    @change_status_loop.before_loop
    async def before_status_loop(self):
        """Wait until ready"""
        await self.wait_until_ready()

    async def on_message(self, message):
        """Handle messages"""
        if message.author == self.user:
            return
        if Config.LOG_CHANNEL_ID and message.channel.id == Config.LOG_CHANNEL_ID:
            guild = message.guild.name if message.guild else "DM"
            channel = message.channel.name if hasattr(message.channel, 'name') else "DM"
            logger.info(f"[DISCORD {guild} #{channel}] {message.author}: {message.content}")
        
        # Check if message is in FIND_BOT_CHANNEL_ID and starts with command prefix
        if Config.FIND_BOT_CHANNEL_ID and message.channel.id == Config.FIND_BOT_CHANNEL_ID:
            if message.content.startswith(self.command_prefix):
                # Extract command name (first word after prefix)
                command_content = message.content[len(self.command_prefix):].strip()
                command_text = command_content.split()[0].lower() if command_content else ""
                
                # Allowed commands in FIND_BOT_CHANNEL
                allowed_commands = {
                    'find', 'locate', 'where', 'lookup', 'lp', 'search',  # find and aliases
                    'villager',
                    'refresh'
                }
                
                # If command is not allowed, send ephemeral message and delete
                if command_text and command_text not in allowed_commands:
                    try:
                        # Delete the command message
                        await message.delete()
                        # Send DM to user (hidden from channel)
                        try:
                            await message.channel.send(
                                f"{message.author.mention} You can only use `!find` (and its aliases), `!villager` commands in this channel. *(Enable DMs to receive this privately)*",
                                delete_after=5
                            )
                        except discord.Forbidden:
                            # If DM fails, send a temporary message in channel
                            await message.channel.send(
                                f"{message.author.mention} You can only use `!find` (and its aliases), `!villager` commands in this channel. *(Enable DMs to receive this privately)*",
                                delete_after=5
                            )
                        logger.info(f"[DISCORD] Blocked command '{command_text}' in FIND_BOT_CHANNEL from {message.author}")
                    except discord.Forbidden:
                        logger.warning(f"[DISCORD] Missing permissions to delete message in FIND_BOT_CHANNEL")
                    return  # Don't process the command
        
        await self.process_commands(message)