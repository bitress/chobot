"""
Discord Command Bot Module
Handles Discord commands for item and villager search with rich embeds
"""

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

    async def item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Filter items from cache for autocomplete"""
        if not current:
            # Maybe show some defaults or empty?
            return []
        
        with self.data_manager.lock:
            # Filter out internal keys like _display and _index
            all_keys = [k for k in self.data_manager.cache.keys() if not k.startswith("_")]
            display_map = self.data_manager.cache.get("_display", {})

        # Use fuzzy matching to find top matches
        matches = process.extract(current, all_keys, limit=25, scorer=fuzz.partial_ratio)
        
        choices = []
        for match_key, score in matches:
            if score > 50:
                display_name = display_map.get(match_key, match_key.title())
                # Truncate if too long (Discord limit is 100)
                choices.append(app_commands.Choice(name=display_name[:100], value=match_key))
        
        return choices

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

            if loc_key in self.sub_island_lookup:
                channel_id = self.sub_island_lookup[loc_key]
                sub_islands_found.append(f"<#{channel_id}>")
            else:
                sub_islands_found.append(f"**{loc}**")

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
            await ctx.send("Usage: `!find <item name>`")
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
                await ctx.send(content=f"Hey <@{ctx.author.id}>, look what I found!", embed=embed)
                logger.info(f"[DISCORD] Item Hit: {search_term} -> Found")
            else:
                await ctx.send(f"**{display_name}** is not currently available on any Sub Island.")
                logger.info(f"[DISCORD] Item Hit: {search_term} -> Not on Sub Islands")
            return

        suggestion_keys = get_best_suggestions(search_term, keys, limit=8)

        with self.data_manager.lock:
            display_map = cache.get("_display", {})

        suggestions = [(k, display_map.get(k, k)) for k in suggestion_keys]
        embed_fail = self.create_fail_embed(ctx, search_term_raw, [disp for _, disp in suggestions])

        if suggestions:
            view = SuggestionView(self, suggestions, "item", ctx.author.id)
            await ctx.send(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail, view=view)
        else:
            await ctx.send(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail)

    @commands.hybrid_command(name="villager")
    @app_commands.describe(name="The name of the villager")
    async def villager(self, ctx, *, name: str = ""):
        """Find a villager"""
        if not name:
            await ctx.send("Usage: `!villager <n>`")
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
                await ctx.send(content=f"Hey <@{ctx.author.id}>, look who I found!", embed=embed)
                logger.info(f"[DISCORD] Villager Hit: {search_term} -> Found")
            else:
                await ctx.send(f"**{search_term.title()}** is not currently on any Sub Island.")
                logger.info(f"[DISCORD] Villager Hit: {search_term} -> Not on Sub Islands")
            return

        matches = process.extract(search_term, list(villager_map.keys()), limit=3, scorer=fuzz.WRatio)
        suggestions = [(m[0], m[0].title()) for m in matches if m[1] > 75]
        suggestion_display_names = [s[1] for s in suggestions]

        embed_fail = self.create_fail_embed(ctx, search_term, suggestion_display_names, is_villager=True)

        if suggestions:
            view = SuggestionView(self, suggestions, "villager", ctx.author.id)
            await ctx.send(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail, view=view)
        else:
            await ctx.send(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail)

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
            name=f"{Config.STAR_PINK} Utility Commands",
            value=(
                "`!status` - Show bot status and cache info\n"
                "`!ping` - Check bot response time\n"
                "`!random` - Get a random item suggestion\n"
                "`!help` - Show this help message"
            ),
            inline=False
        )
        
        embed.add_field(
            name=f"{Config.STAR_PINK} Admin Commands",
            value="`!refresh` - Manually refresh cache (Admin only)",
            inline=False
        )
        
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", 
                        icon_url=ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP)
        embed.set_image(url=Config.FOOTER_LINE)
        
        await ctx.send(embed=embed)
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
        
        await ctx.send(embed=embed)
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
            await ctx.send("No items in cache yet. Try again later!")
            return
        
        # Pick a random item
        random_key = random.choice(all_items)
        display_name = display_map.get(random_key, random_key.title())
        found_locations = cache.get(random_key)
        
        if found_locations:
            embed = self.create_found_embed(ctx, display_name, found_locations, is_villager=False)
            
            if embed:
                embed.title = f"🎲 Random Item: {display_name}"
                await ctx.send(content=f"Hey <@{ctx.author.id}>, here's a random item for you!", embed=embed)
                logger.info(f"[DISCORD] Random item: {random_key}")
            else:
                # Item exists but not on sub islands
                await ctx.send(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to see where it's available!")
        else:
            await ctx.send(f"🎲 Random suggestion: **{display_name}** - use `!find {display_name}` to check availability!")

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
                
                await ctx.send(
                    f"**System Status**\n"
                    f"Items Cached: `{len(self.data_manager.cache)}`\n"
                    f"Islands Linked: `{island_count}`\n"
                    f"Last Update: `{t_str}`\n"
                    f"Uptime: `{uptime_str}`"
                )
            else:
                await ctx.send("Database loading...")

    @commands.hybrid_command(name="refresh")
    @commands.has_permissions(administrator=True)
    async def refresh(self, ctx):
        """Manually refresh cache (Mods only)"""
        await ctx.send("Refreshing cache and island links...")
        self.data_manager.update_cache()
        await self.fetch_islands()
        count = len(getattr(self, 'island_map', {})) 
        await ctx.send(f"Done. Linked {count} islands.")

    @refresh.error
    async def refresh_error(self, ctx, error):
        """Handle permission errors cleanly"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You do not have permission to use this command.")


class DiscordCommandBot(commands.Bot):
    """Main Discord bot with command functionality"""

    def __init__(self, data_manager):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        # Disable default help command to use our custom one
        super().__init__(command_prefix='!', intents=intents, help_command=None)

        self.data_manager = data_manager
        self.start_time = datetime.now()  # Track bot start time for uptime

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
        ])

    async def setup_hook(self):
        """Setup bot cogs and sync commands"""
        await self.add_cog(DiscordCommandCog(self, self.data_manager))

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

    @tasks.loop(minutes=3)
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
        await self.process_commands(message)