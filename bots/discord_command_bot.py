"""
Discord Command Bot Module
Handles Discord commands for item and villager search with rich embeds
"""

import time
import re
import logging
from datetime import datetime
from itertools import cycle

import discord
from discord.ext import commands, tasks
from thefuzz import process, fuzz

from utils.config import Config
from utils.helpers import normalize_text, get_best_suggestions

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
            embed = self.cog.create_found_embed(interaction, display_name, found_locations, is_villager)
            await interaction.response.edit_message(
                content=f"Hey <@{interaction.user.id}>, look what I found!",
                embed=embed,
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

    async def fetch_islands(self):
        """Fetch island channels from Discord"""
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

        for channel in category.channels:
            if channel.id == Config.IGNORE_CHANNEL_ID:
                continue

            clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', channel.name).strip()
            island_name = clean_name.capitalize()

            temp_lookup[island_name] = channel.id
            count += 1

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
        return False

    def create_found_embed(self, ctx_or_interaction, search_term, location_string, is_villager=False):

        user = getattr(ctx_or_interaction, "author", getattr(ctx_or_interaction, "user", None))
        clean_name = search_term.title()
        loc_list = sorted(list(set(location_string.split(", "))))
        sub_islands_found = []


        for loc in loc_list:
            loc_key = loc.strip().capitalize()
            if loc_key.upper() in [name.upper() for name in Config.FREE_ISLANDS]:
                continue

            if loc_key in self.sub_island_lookup:
                channel_id = self.sub_island_lookup[loc_key]
                sub_islands_found.append(f"<#{channel_id}>")
            else:
                sub_islands_found.append(f"**{loc}**")

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

        if search_key in self.data_manager.image_cache:
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

    @tasks.loop(hours=24)
    async def auto_refresh_cache(self):
        """Auto refresh cache and islands"""
        self.data_manager.update_cache()
        await self.fetch_islands()

    @auto_refresh_cache.before_loop
    async def before_refresh(self):
        """Wait until ready before starting refresh loop"""
        await self.bot.wait_until_ready()
        await self.fetch_islands()

    @commands.command(aliases=['locate', 'where'])
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
            await ctx.send(content=f"Hey <@{ctx.author.id}>, look what I found!", embed=embed)
            logger.info(f"[DISCORD] Item Hit: {search_term} -> Found")
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

    @commands.command()
    async def villager(self, ctx, *, name: str = ""):
        """Find a villager"""
        if not name:
            await ctx.send("Usage: `!villager <n>`")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term = name.lower().strip()
        villager_map = self.data_manager.get_villagers([
            Config.VILLAGERS_DIR,
            Config.TWITCH_VILLAGERS_DIR
        ])

        found_locations = villager_map.get(search_term)

        if found_locations:
            embed = self.create_found_embed(ctx, search_term, found_locations, is_villager=True)
            await ctx.send(content=f"Hey <@{ctx.author.id}>, look who I found!", embed=embed)
            logger.info(f"[DISCORD] Villager Hit: {search_term} -> Found")
            return

        matches = process.extract(search_term, list(villager_map.keys()), limit=3, scorer=fuzz.WRatio)
        suggestions = [(m[0], m[0].title()) for m in matches if m[1] > 75]
        suggestion_display_names = [s[1] for s in suggestions]

        embed_fail = self.create_fail_embed(ctx, search_term, suggestion_display_names)

        if suggestions:
            view = SuggestionView(self, suggestions, "villager", ctx.author.id)
            await ctx.send(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail, view=view)
        else:
            await ctx.send(content=f"Hey <@{ctx.author.id}>...", embed=embed_fail)

        logger.info(f"[DISCORD] Villager Miss: {search_term}")

    @commands.command()
    async def status(self, ctx):
        """Show bot status"""
        with self.data_manager.lock:
            if self.data_manager.last_update:
                t_str = self.data_manager.last_update.strftime("%H:%M:%S")
                island_count = len(self.sub_island_lookup)
                await ctx.send(
                    f"**System Status**\n"
                    f"Items Cached: `{len(self.data_manager.cache)}`\n"
                    f"Islands Linked: `{island_count}`\n"
                    f"Last Update: `{t_str}`"
                )
            else:
                await ctx.send("Database loading...")

    @commands.command()
    async def refresh(self, ctx):
        """Manually refresh cache"""
        await ctx.send("Refreshing cache and island links...")
        self.data_manager.update_cache()
        await self.fetch_islands()
        await ctx.send(f"Done. Linked {len(self.sub_island_lookup)} islands.")


class DiscordCommandBot(commands.Bot):
    """Main Discord bot with command functionality"""

    def __init__(self, data_manager):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents)

        self.data_manager = data_manager

        self.status_list = cycle([
            discord.Activity(type=discord.ActivityType.watching, name="over the islands | !find"),
            discord.Activity(type=discord.ActivityType.watching, name="villagers move out | !villager"),
            discord.Activity(type=discord.ActivityType.playing, name="with the item database"),
            discord.Activity(type=discord.ActivityType.competing, name="island traffic"),
            discord.Activity(type=discord.ActivityType.playing, name="https://www.chopaeng.com"),
        ])

    async def setup_hook(self):
        """Setup bot cogs"""
        await self.add_cog(DiscordCommandCog(self, self.data_manager))
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