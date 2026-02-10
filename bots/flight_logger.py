"""
Discord Flight Logger Module
Tracks island visitor arrivals and alerts on unknown travelers
"""

import re
import logging
import unicodedata

import discord
from discord.ext import commands, tasks

from utils.config import Config

logger = logging.getLogger("FlightLogger")


class FlightLoggerCog(commands.Cog):
    """Cog for tracking island visitors"""

    def __init__(self, bot):
        self.bot = bot
        self.island_map = {}
        self.join_pattern = re.compile(
            r"\[.*?\]\s*.*?\s+(.*?)\s+from\s+(.*?)\s+is joining\s+(.*?)(?:\.|$)",
            re.IGNORECASE
        )
        self.fetch_islands_task.start()

    def cog_unload(self):
        """Cleanup on unload"""
        self.fetch_islands_task.cancel()

    @tasks.loop(hours=1)
    async def fetch_islands_task(self):
        """Periodically fetch island channels"""
        await self.fetch_islands()

    @fetch_islands_task.before_loop
    async def before_fetch(self):
        """Wait until ready"""
        await self.bot.wait_until_ready()
        await self.fetch_islands()

    async def fetch_islands(self):
        """Fetch island channels from Discord"""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            logger.error(f"[FLIGHT] Guild {Config.GUILD_ID} not found.")
            return

        category = discord.utils.get(guild.categories, id=Config.CATEGORY_ID)
        if not category:
            logger.error(f"[FLIGHT] Category {Config.CATEGORY_ID} not found.")
            return

        temp_map = {}
        count = 0

        for channel in category.channels:
            if channel.id == Config.FLIGHT_LISTEN_CHANNEL_ID:
                continue

            clean_name_raw = re.sub(r'[^a-zA-Z0-9\s]', '', channel.name).strip()
            key = self.clean_text(clean_name_raw)

            temp_map[key] = channel.id
            count += 1

        self.island_map = temp_map
        logger.info(f"[FLIGHT] Dynamic Island Fetch Complete. Mapped {count} islands.")

    def clean_text(self, text):
        """Clean text for matching"""
        if not text:
            return ""
        normalized = unicodedata.normalize('NFKD', text)
        no_accents = "".join([c for c in normalized if not unicodedata.category(c).startswith('Mn')])
        return "".join(ch for ch in no_accents if ch.isalnum()).lower()

    def get_island_channel_link(self, island_name):
        """Get channel link for island"""
        island_clean = self.clean_text(island_name)

        if island_clean in self.island_map:
            channel_id = self.island_map[island_clean]
            return f"<#{channel_id}>"

        return island_name.title()

    def split_options(self, raw: str):
        """Split options separated by /"""
        if not raw:
            return []
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        return [self.clean_text(p) for p in parts if self.clean_text(p)]

    def parse_member_nick(self, display_name: str):
        """Parse member nickname for IGN and islands"""
        if not display_name or "|" not in display_name:
            return [], []

        chunks = [c.strip() for c in display_name.split("|") if c.strip()]
        if not chunks:
            return [], []

        ign_chunk = chunks[0]
        island_chunk = " | ".join(chunks[1:]) if len(chunks) > 1 else ""

        ign_options = self.split_options(ign_chunk)
        island_options = self.split_options(island_chunk)

        return ign_options, island_options

    def find_matching_members(self, guild, ign_log, island_log):
        """Find members matching the traveler info"""
        found_members = []
        ign_log_clean = self.clean_text(ign_log)
        island_log_clean = self.clean_text(island_log)

        checked_count = 0

        for member in guild.members:
            nick = member.display_name
            ign_opts, island_opts = self.parse_member_nick(nick)

            if not ign_opts and not island_opts:
                continue

            checked_count += 1

            ign_match = ign_log_clean in ign_opts

            if island_log_clean:
                island_match = island_log_clean in island_opts if island_opts else False
            else:
                island_match = True

            if ign_match and island_match:
                logger.info(f"[FLIGHT] ✅ FULL Match found: {member.display_name} (ID: {member.id})")
                found_members.append(member)

        logger.info(f"[FLIGHT] 📊 Checked {checked_count} members with '|' format")
        if not found_members:
            logger.warning(
                f"[FLIGHT] ❌ No matches found for IGN: '{ign_log_clean}' | Island: '{island_log_clean}'"
            )

        return found_members

    @commands.Cog.listener()
    async def on_message(self, message):
        """Listen for join messages"""
        if message.author == self.bot.user or message.channel.id != Config.FLIGHT_LISTEN_CHANNEL_ID:
            return

        match = self.join_pattern.search(message.content)
        if match:
            ign_raw = match.group(1).strip()
            island_raw = match.group(2).strip()
            dest_raw = match.group(3).strip()

            logger.info(f"[FLIGHT] 🔍 Detected join: {ign_raw} from {island_raw} -> {dest_raw}")

            found = self.find_matching_members(message.guild, ign_raw, island_raw)
            await self.log_result(found, "JOINING", ign_raw, island_raw, dest_raw)

    async def log_result(self, found_members, status, ign, island, destination):
        """Log the result of traveler check"""
        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        if not output_channel:
            logger.error("[FLIGHT] Output channel not found!")
            return

        if found_members:
            mentions = " ".join([m.mention for m in found_members])
            logger.info(
                f"[FLIGHT] ✅ Verified {status} Event: {ign} from {island.title()} -> "
                f"{destination.title()} | Members: {mentions}"
            )
        else:
            destination_link = self.get_island_channel_link(destination)

            embed = discord.Embed(
                title=f"{Config.EMOJI_FAIL} UNKNOWN TRAVELER in {destination_link}",
                description=(
                    f"Traveler **`{ign}`** from **`{island.title()}`** is not linked. "
                    f"Check if this is a member or they didn't change their nickname. Thank you!"
                ),
                color=0xFF0000,
                timestamp=discord.utils.utcnow()
            )

            embed.add_field(
                name="👤 Traveler (IGN)",
                value=f"```yaml\n{ign}```",
                inline=True
            )
            embed.add_field(
                name="🏝️ Origin Island",
                value=f"```yaml\n{island.title()}```",
                inline=True
            )
            embed.add_field(
                name="📍 Island Destination",
                value=f"{destination_link}",
                inline=False
            )
            embed.set_image(url=Config.FOOTER_LINE)
            guild = self.bot.get_guild(Config.GUILD_ID)
            guild_icon = guild.icon.url if guild and guild.icon else None
            embed.set_footer(text="Chopaeng Camp™", icon_url=guild_icon)


            logger.warning(
                f"[FLIGHT] ⚠️ UNKNOWN TRAVELER: {ign} from {island.title()} -> {destination.title()}"
            )
            await output_channel.send(embed=embed)