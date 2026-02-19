"""
Twitch Bot Module
Handles Twitch chat commands for item and villager search
"""

import time
import random
import logging
from twitchio.ext import commands
from thefuzz import process, fuzz

from utils.config import Config
from utils.helpers import normalize_text, get_best_suggestions, clean_text, format_locations_text

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
        self.start_time = time.time()  # Track bot start time for uptime

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

        # Periodic cleanup: prune entries older than 60s every 100 entries
        if len(self.cooldowns) > 100:
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < 60}

        return False

    @commands.command(aliases=['locate', 'where', 'lookup', 'lp', 'search'])
    async def find(self, ctx: commands.Context, *, item: str = ""):
        """Find an item command"""
        if not item:
            await ctx.send(f"Usage: !find <item name>")
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        search_term_raw = item.strip()
        search_term = normalize_text(search_term_raw)

        with self.data_manager.lock:
            cache = self.data_manager.cache
            display_map = cache.get("_display", {})
            keys = [k for k in cache.keys() if k != "_display"]

        found_locs_raw = cache.get(search_term)

        if found_locs_raw:
            # Filter: SUB_ISLANDS + FREE_ISLANDS for items
            loc_list = found_locs_raw.split(", ")
            allowed_islands = Config.SUB_ISLANDS + Config.FREE_ISLANDS
            all_found = [loc for loc in loc_list if any(clean_text(si) == clean_text(loc) for si in allowed_islands)]
            
            display_name = display_map.get(search_term, search_term_raw.title())
            
            if all_found:
                final_msg = format_locations_text(", ".join(all_found))
                await ctx.send(f"Hey @{ctx.author.name}, I found {display_name} {final_msg}")
                logger.info(f"[TWITCH] Item Hit: {search_term} -> {final_msg}")
            else:
                await ctx.send(f"Hey @{ctx.author.name}, {display_name} is not currently available on any Island.")
                logger.info(f"[TWITCH] Item Hit: {search_term} -> Not on Islands")
            return

        # Fuzzy search using unified helper
        suggestion_keys = get_best_suggestions(search_term, keys, limit=5)
        valid_suggestions = [display_map.get(k, k.title()) for k in suggestion_keys]

        if valid_suggestions:
            suggestions_str = ", ".join(valid_suggestions)
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find \"{search_term_raw}\" - "
                f"Did you mean: {suggestions_str}? If not, try using !orderbot."
            )
        else:
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find \"{search_term_raw}\" or anything similar. "
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

        search_term_raw = name.strip()
        search_term = normalize_text(search_term_raw)
        
        villager_map = self.data_manager.get_villagers([
            Config.VILLAGERS_DIR,
            Config.TWITCH_VILLAGERS_DIR
        ])
        
        found_locs_raw = villager_map.get(search_term)

        if found_locs_raw:
            # Filter: only SUB_ISLANDS
            loc_list = found_locs_raw.split(", ")
            allowed_islands = Config.SUB_ISLANDS + Config.FREE_ISLANDS
            sub_only = [loc for loc in loc_list if any(clean_text(si) == clean_text(loc) for si in allowed_islands)]
            
            display_name = search_term.title()
            
            if sub_only:
                final_msg = format_locations_text(", ".join(sub_only))
                await ctx.send(f"Hey @{ctx.author.name}, I found villager {display_name} {final_msg}")
                logger.info(f"[TWITCH] Villager Hit: {search_term} -> {final_msg}")
            else:
                await ctx.send(f"Hey @{ctx.author.name}, {display_name} is not currently on any Sub Island.")
                logger.info(f"[TWITCH] Villager Hit: {search_term} -> Not on Sub Islands")
            return

        # Fuzzy search
        matches = process.extract(
            search_term,
            list(villager_map.keys()),
            limit=3,
            scorer=fuzz.WRatio
        )
        valid_suggestions = [m[0].title() for m in matches if m[1] > 75]

        if valid_suggestions:
            suggestions_str = ", ".join(valid_suggestions)
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find villager \"{search_term_raw}\" - "
                f"Did you mean: {suggestions_str}? If not, try using !orderbot."
            )
        else:
            await ctx.send(
                f"Hey @{ctx.author.name}, I couldn't find a villager named \"{search_term_raw}\". "
                f"Try using !orderbot."
            )

    @commands.command()
    async def refresh(self, ctx: commands.Context):
        """Manually refresh cache (Mods only)"""
        if not (ctx.author.is_mod or ctx.author.name.lower() == ctx.channel.name.lower()):
            await ctx.send(f"@{ctx.author.name} You do not have permission to use this command.")
            return
        await ctx.send("Refreshing cache...")
        try:
            self.data_manager.update_cache()
            await ctx.send(f"Done. Cache updated with {len(self.data_manager.cache)} items.")
            logger.info(f"[TWITCH] Cache refreshed by {ctx.author.name}")
        except Exception as e:
            await ctx.send(f"Failed to refresh cache: {e}")
            logger.error(f"[TWITCH] Cache refresh failed: {e}")

    @commands.command()
    async def help(self, ctx: commands.Context):
        """Show help message"""
        await ctx.send("Commands: !find <item> | !villager <name> | !random | !status | !refresh (mods)")

    @commands.command()
    async def random(self, ctx: commands.Context):
        """Get a random item suggestion"""
        with self.data_manager.lock:
            cache = self.data_manager.cache
            # Filter out internal keys
            all_items = [k for k in cache.keys() if not k.startswith("_")]
            display_map = cache.get("_display", {})
        
        if not all_items:
            await ctx.send(f"@{ctx.author.name} No items in cache yet. Try again later!")
            return
        
        # Pick a random item
        random_key = random.choice(all_items)
        display_name = display_map.get(random_key, random_key.title())
        found_locs_raw = cache.get(random_key)
        
        if found_locs_raw:
            # Filter: SUB_ISLANDS + FREE_ISLANDS
            loc_list = found_locs_raw.split(", ")
            allowed_islands = Config.SUB_ISLANDS + Config.FREE_ISLANDS
            all_found = [loc for loc in loc_list if any(clean_text(si) == clean_text(loc) for si in allowed_islands)]
            
            if all_found:
                final_msg = format_locations_text(", ".join(all_found))
                await ctx.send(f"🎲 Random item for @{ctx.author.name}: {display_name} {final_msg}")
                logger.info(f"[TWITCH] Random item: {random_key}")
            else:
                await ctx.send(f"🎲 Random suggestion for @{ctx.author.name}: {display_name}")
        else:
            await ctx.send(f"🎲 Random suggestion for @{ctx.author.name}: {display_name}")

    @commands.command()
    async def status(self, ctx: commands.Context):
        """Show bot status"""
        with self.data_manager.lock:
            if self.data_manager.last_update:
                time_str = self.data_manager.last_update.strftime("%H:%M:%S")
                
                # Calculate uptime
                uptime_seconds = int(time.time() - self.start_time)
                hours = uptime_seconds // 3600
                minutes = (uptime_seconds % 3600) // 60
                uptime_str = f"{hours}h {minutes}m"
                
                await ctx.send(f"Items: {len(self.data_manager.cache)} | Last Update: {time_str} | Uptime: {uptime_str}")
            else:
                await ctx.send("Database loading...")