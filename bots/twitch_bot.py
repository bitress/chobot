"""
Twitch Bot Module
Handles Twitch chat commands for item and villager search
"""

import time
import logging
from twitchio.ext import commands
from thefuzz import process, fuzz

from utils.config import Config
from utils.helpers import format_locations_text

logger = logging.getLogger("TwitchBot")


class TwitchBot(commands.Bot):
    """Twitch bot for treasure hunt commands"""

    def __init__(self, data_manager):
        super().__init__(
            token=Config.TWITCH_TOKEN,
            prefix='!',
            initial_channels=[Config.TWITCH_CHANNEL]
        )
        self.data_manager = data_manager
        self.cooldowns = {}

    async def event_ready(self):
        """Called when bot is connected"""
        logger.info(f"[TWITCH] Logged in as: {self.nick}")
        logger.info(f"[TWITCH] Monitoring channel: {Config.TWITCH_CHANNEL}")

    async def event_message(self, message):
        """Called on every message"""
        if message.echo:
            return

        try:
            author = message.author.name if message.author else "Unknown"
            logger.info(f"[TWITCH CHAT] {author}: {message.content}")
        except Exception as e:
            logger.error(f"Failed to log Twitch message: {e}")

        await self.handle_commands(message)

    def check_cooldown(self, user_id: str, cooldown_sec: int = 3) -> bool:
        """Check if user is on cooldown"""
        now = time.time()
        if user_id in self.cooldowns:
            if now - self.cooldowns[user_id] < cooldown_sec:
                return True
        self.cooldowns[user_id] = now
        return False

    @commands.command(aliases=['locate', 'where'])
    async def find(self, ctx: commands.Context, *, item: str = ""):
        """Find an item command"""
        if not item:
            await ctx.send(f"Usage: !find <item name>")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term = item.lower().strip()

        with self.data_manager.lock:
            cache = self.data_manager.cache

        found_locs = cache.get(search_term)

        if found_locs:
            final_msg = format_locations_text(found_locs)
            await ctx.send(f"Hey @{ctx.author.name}, I found {search_term.upper()} {final_msg}")
            logger.info(f"[TWITCH] Item Hit: {search_term} -> {final_msg}")
            return

        # Fuzzy search
        matches = process.extract(
            search_term,
            list(cache.keys()),
            limit=5,
            scorer=fuzz.token_set_ratio
        )
        valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

        if valid_suggestions:
            suggestions_str = ", ".join(valid_suggestions)
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find \"{search_term}\" - "
                f"Did you mean: {suggestions_str}? If not, try using !orderbot."
            )
        else:
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find \"{search_term}\" or anything similar. "
                f"Please check spelling. If not, try using !orderbot."
            )

    @commands.command()
    async def villager(self, ctx: commands.Context, *, name: str = ""):
        """Find a villager command"""
        if not name:
            await ctx.send(f"Usage: !villager <name>")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term = name.lower().strip()
        villager_map = self.data_manager.get_villagers([
            Config.VILLAGERS_DIR
        ])
        found_locs = villager_map.get(search_term)

        if found_locs:
            final_msg = format_locations_text(found_locs)
            await ctx.send(f"Hey @{ctx.author.name}, I found villager {search_term.upper()} {final_msg}")
            logger.info(f"[TWITCH] Villager Hit: {search_term} -> {final_msg}")
            return

        # Fuzzy search
        matches = process.extract(
            search_term,
            list(villager_map.keys()),
            limit=3,
            scorer=fuzz.token_set_ratio
        )
        valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

        if valid_suggestions:
            suggestions_str = ", ".join(valid_suggestions)
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find villager \"{search_term}\" - "
                f"Did you mean: {suggestions_str}? If not, try using !orderbot."
            )
        else:
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find a villager named \"{search_term}\". "
                f"Try using !orderbot."
            )

    @commands.command()
    async def help(self, ctx: commands.Context):
        """Show help message"""
        await ctx.send("Commands: !find <item> | !villager <name> | !status")

    @commands.command()
    async def status(self, ctx: commands.Context):
        """Show bot status"""
        with self.data_manager.lock:
            if self.data_manager.last_update:
                time_str = self.data_manager.last_update.strftime("%H:%M:%S")
                await ctx.send(f"Items: {len(self.data_manager.cache)} | Last Update: {time_str}")
            else:
                await ctx.send("Database loading...")